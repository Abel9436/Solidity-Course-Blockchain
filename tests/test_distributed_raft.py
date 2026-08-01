"""
Tests for Raft Consensus Protocol and Distributed 2PC Engine.
"""

import pytest
from pysql.raft import (
    AppendEntriesArgs,
    NodeState,
    RaftNode,
    RequestVoteArgs,
)
from pysql.distributed import (
    DistributedCoordinator,
    HashPartitioner,
    TransactionPhase,
)
from pysql.engine import Database
import tempfile
import shutil
import os


class TestRaftProtocol:
    """Tests for Raft consensus protocol logic."""

    def test_raft_initial_state(self):
        node = RaftNode(node_id="node1", peers=["node2", "node3"])
        assert node.state == NodeState.FOLLOWER
        assert node.current_term == 0
        assert node.voted_for is None

    def test_start_election(self):
        node = RaftNode(node_id="node1", peers=["node2", "node3"])
        vote_requests = node.start_election()

        assert node.state == NodeState.CANDIDATE
        assert node.current_term == 1
        assert node.voted_for == "node1"
        assert len(vote_requests) == 2
        assert vote_requests[0][1].candidate_id == "node1"

    def test_vote_granting(self):
        node = RaftNode(node_id="node2", peers=["node1", "node3"])

        args = RequestVoteArgs(
            term=1,
            candidate_id="node1",
            last_log_index=0,
            last_log_term=0,
        )

        reply = node.handle_request_vote(args)
        assert reply.vote_granted is True
        assert node.voted_for == "node1"

        # Deny double voting in same term
        args2 = RequestVoteArgs(
            term=1,
            candidate_id="node3",
            last_log_index=0,
            last_log_term=0,
        )
        reply2 = node.handle_request_vote(args2)
        assert reply2.vote_granted is False

    def test_log_replication(self):
        node = RaftNode(node_id="node2", peers=["node1", "node3"])

        # Receive AppendEntries from leader
        args = AppendEntriesArgs(
            term=1,
            leader_id="node1",
            prev_log_index=0,
            prev_log_term=0,
            entries=[],
            leader_commit=0,
        )

        reply = node.handle_append_entries(args)
        assert reply.success is True
        assert node.current_term == 1


class TestDistributedEngine:
    """Tests for Hash partitioning and Two-Phase Commit (2PC)."""

    def test_hash_partitioner(self):
        partitioner = HashPartitioner(shards=["node1", "node2", "node3"])
        shard1 = partitioner.get_shard("user_100")
        shard2 = partitioner.get_shard("user_200")

        assert shard1 in ["node1", "node2", "node3"]
        assert shard2 in ["node1", "node2", "node3"]

    def test_2pc_coordinator_commit(self):
        tmp1 = tempfile.mkdtemp()
        tmp2 = tempfile.mkdtemp()

        try:
            db1 = Database(os.path.join(tmp1, "db1"))
            db2 = Database(os.path.join(tmp2, "db2"))

            cluster = {"node1": db1, "node2": db2}
            coordinator = DistributedCoordinator(node_id="node1", cluster_nodes=cluster)

            # Setup tables on both nodes
            db1.execute("CREATE TABLE users (id INT, name TEXT)")
            db2.execute("CREATE TABLE users (id INT, name TEXT)")

            # Execute 2PC distributed insert
            commands = {
                "node1": "INSERT INTO users VALUES (1, 'Alice')",
                "node2": "INSERT INTO users VALUES (2, 'Bob')",
            }

            committed = coordinator.execute_2pc(txn_id="txn_001", commands=commands)
            assert committed is True

            # Scatter-gather query
            res = coordinator.execute_scatter_gather("SELECT * FROM users")
            assert len(res.rows) == 2

            db1.close()
            db2.close()
        finally:
            shutil.rmtree(tmp1, ignore_errors=True)
            shutil.rmtree(tmp2, ignore_errors=True)
