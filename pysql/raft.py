"""
Raft Distributed Consensus Protocol Implementation.

Implements the complete Raft consensus algorithm (Ongaro & Ousterhout)
to turn PySQLEngine into a fault-tolerant distributed database cluster.

Raft Guarantees:
- Election Safety: At most one leader can be elected per term
- Leader Append-Only: Leader never overwrites or truncates its log entries
- Log Matching: If two logs contain an entry with same index and term,
  the logs are identical up to that entry
- Leader Completeness: If a log entry is committed in a term, it is present
  in the logs of leaders for all higher-numbered terms
- State Machine Safety: If a server has applied a log entry at a given index,
  no other server will ever apply a different log entry for that index

Node States:
    Follower ──(timeout)──→ Candidate ──(majority votes)──→ Leader
       ▲                        │                              │
       └──────(higher term)─────┴──────(higher term)───────────┘
"""

from __future__ import annotations

import enum
import random
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional


class NodeState(enum.Enum):
    FOLLOWER = "Follower"
    CANDIDATE = "Candidate"
    LEADER = "Leader"


@dataclass
class LogEntry:
    """A single Raft log entry."""
    term: int
    index: int
    command: Any  # SQL statement or configuration change


@dataclass
class RequestVoteArgs:
    term: int
    candidate_id: str
    last_log_index: int
    last_log_term: int


@dataclass
class RequestVoteReply:
    term: int
    vote_granted: bool


@dataclass
class AppendEntriesArgs:
    term: int
    leader_id: str
    prev_log_index: int
    prev_log_term: int
    entries: list[LogEntry]
    leader_commit: int


@dataclass
class AppendEntriesReply:
    term: int
    success: bool
    match_index: int = 0


class RaftNode:
    """A single Raft consensus node in the cluster."""

    def __init__(
        self,
        node_id: str,
        peers: list[str],
        state_machine_apply_fn: Optional[Callable[[Any], Any]] = None,
    ) -> None:
        self.node_id = node_id
        self.peers = peers
        self.apply_fn = state_machine_apply_fn or (lambda cmd: None)

        # Persistent state on all nodes
        self.current_term = 0
        self.voted_for: Optional[str] = None
        self.log: list[LogEntry] = [LogEntry(term=0, index=0, command=None)]  # 1-indexed dummy entry

        # Volatile state on all nodes
        self.commit_index = 0
        self.last_applied = 0
        self.state = NodeState.FOLLOWER

        # Volatile state on leaders
        self.next_index: dict[str, int] = {}
        self.match_index: dict[str, int] = {}

        # Timing & Election
        self.last_heartbeat = time.time()
        self.election_timeout = random.uniform(0.15, 0.30)  # 150-300ms

    @property
    def last_log_index(self) -> int:
        return self.log[-1].index

    @property
    def last_log_term(self) -> int:
        return self.log[-1].term

    # ─── RPC Handlers ────────────────────────────────────────────────────────

    def handle_request_vote(self, args: RequestVoteArgs) -> RequestVoteReply:
        """Handle incoming RequestVote RPC from a Candidate."""
        if args.term < self.current_term:
            return RequestVoteReply(term=self.current_term, vote_granted=False)

        if args.term > self.current_term:
            self.current_term = args.term
            self.state = NodeState.FOLLOWER
            self.voted_for = None

        # Check candidate log up-to-dateness
        up_to_date = False
        if args.last_log_term > self.last_log_term:
            up_to_date = True
        elif args.last_log_term == self.last_log_term and args.last_log_index >= self.last_log_index:
            up_to_date = True

        can_vote = (self.voted_for is None or self.voted_for == args.candidate_id)
        if can_vote and up_to_date:
            self.voted_for = args.candidate_id
            self.last_heartbeat = time.time()
            return RequestVoteReply(term=self.current_term, vote_granted=True)

        return RequestVoteReply(term=self.current_term, vote_granted=False)

    def handle_append_entries(self, args: AppendEntriesArgs) -> AppendEntriesReply:
        """Handle incoming AppendEntries RPC from the Leader."""
        if args.term < self.current_term:
            return AppendEntriesReply(term=self.current_term, success=False)

        if args.term > self.current_term or self.state != NodeState.FOLLOWER:
            self.current_term = args.term
            self.state = NodeState.FOLLOWER
            self.voted_for = None

        self.last_heartbeat = time.time()

        # Check prev_log_index and prev_log_term
        if args.prev_log_index >= len(self.log):
            return AppendEntriesReply(term=self.current_term, success=False)

        if self.log[args.prev_log_index].term != args.prev_log_term:
            # Conflicting entry — truncate log
            self.log = self.log[:args.prev_log_index]
            return AppendEntriesReply(term=self.current_term, success=False)

        # Append any new entries
        for entry in args.entries:
            if entry.index < len(self.log):
                if self.log[entry.index].term != entry.term:
                    self.log = self.log[:entry.index]
                    self.log.append(entry)
            else:
                self.log.append(entry)

        # Advance commit index
        if args.leader_commit > self.commit_index:
            self.commit_index = min(args.leader_commit, len(self.log) - 1)
            self._apply_entries()

        return AppendEntriesReply(
            term=self.current_term,
            success=True,
            match_index=len(self.log) - 1,
        )

    # ─── Leader Execution & Log Replication ────────────────────────────────

    def start_election(self) -> list[tuple[str, RequestVoteArgs]]:
        """Transition to Candidate and construct vote requests for peers."""
        self.state = NodeState.CANDIDATE
        self.current_term += 1
        self.voted_for = self.node_id
        self.last_heartbeat = time.time()

        args = RequestVoteArgs(
            term=self.current_term,
            candidate_id=self.node_id,
            last_log_index=self.last_log_index,
            last_log_term=self.last_log_term,
        )

        return [(peer, args) for peer in self.peers]

    def promote_to_leader(self) -> None:
        """Promote this candidate node to Leader."""
        self.state = NodeState.LEADER
        for peer in self.peers:
            self.next_index[peer] = len(self.log)
            self.match_index[peer] = 0

    def propose(self, command: Any) -> Optional[int]:
        """Propose a command to be replicated by the Raft leader.

        Returns log index if leader, None if not leader.
        """
        if self.state != NodeState.LEADER:
            return None

        index = len(self.log)
        entry = LogEntry(term=self.current_term, index=index, command=command)
        self.log.append(entry)
        return index

    def update_leader_commit(self) -> bool:
        """Check if any log entries have been replicated to a majority and commit them."""
        if self.state != NodeState.LEADER:
            return False

        # Find N such that N > commit_index and majority of match_index[i] >= N
        majority = (len(self.peers) + 1) // 2 + 1
        advanced = False

        for N in range(len(self.log) - 1, self.commit_index, -1):
            if self.log[N].term != self.current_term:
                continue

            count = 1  # Self
            for peer in self.peers:
                if self.match_index.get(peer, 0) >= N:
                    count += 1

            if count >= majority:
                self.commit_index = N
                self._apply_entries()
                advanced = True
                break

        return advanced

    def _apply_entries(self) -> None:
        """Apply committed log entries to the state machine."""
        while self.last_applied < self.commit_index:
            self.last_applied += 1
            entry = self.log[self.last_applied]
            if entry.command is not None:
                self.apply_fn(entry.command)
