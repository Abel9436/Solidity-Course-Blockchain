"""
Multi-Version Concurrency Control (MVCC) — Snapshot Isolation.

MVCC is the concurrency control mechanism used by PostgreSQL, MySQL (InnoDB),
Oracle, and most modern databases. Instead of locking records during reads,
each transaction sees a consistent snapshot of the database as of its
start time.

Key concepts:
- Each record has a creation timestamp (xmin) and deletion timestamp (xmax)
- A transaction can only see records where xmin <= txn_start AND xmax > txn_start
- Writers don't block readers; readers don't block writers
- Write-write conflicts are detected and one transaction is aborted

Version Chain:
    Each logical record may have multiple physical versions:
    ┌──────────┐    ┌──────────┐    ┌──────────┐
    │ Version 3│───→│ Version 2│───→│ Version 1│
    │ xmin=100 │    │ xmin=50  │    │ xmin=10  │
    │ xmax=∞   │    │ xmax=100 │    │ xmax=50  │
    └──────────┘    └──────────┘    └──────────┘
    (current)       (historical)    (historical)

This implementation provides:
- Snapshot isolation (SI) — each transaction reads from a consistent snapshot
- First-updater-wins conflict detection
- Garbage collection of old versions
- Transaction ID management
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Optional


class TransactionState(Enum):
    """Lifecycle states of a transaction."""
    ACTIVE = auto()
    COMMITTED = auto()
    ABORTED = auto()


class IsolationLevel(Enum):
    """Supported transaction isolation levels."""
    READ_UNCOMMITTED = auto()
    READ_COMMITTED = auto()
    REPEATABLE_READ = auto()
    SNAPSHOT = auto()      # Our default — equivalent to PostgreSQL's REPEATABLE READ
    SERIALIZABLE = auto()


@dataclass
class VersionRecord:
    """A single version of a logical record.

    Each version captures:
    - The data at this point in time
    - Which transaction created it (xmin)
    - Which transaction deleted/superseded it (xmax)
    - A pointer to the previous version
    """
    data: list[Any]                          # Column values
    xmin: int = 0                            # Transaction ID that created this version
    xmax: int = 0                            # Transaction ID that deleted this version (0 = live)
    created_at: float = 0.0                  # Wall-clock timestamp
    prev_version: Optional["VersionRecord"] = None  # Previous version in chain

    def is_visible_to(self, txn_id: int, snapshot: "TransactionSnapshot") -> bool:
        """Determine if this version is visible to the given transaction.

        A version is visible if:
        1. Its creator (xmin) has committed and committed before our snapshot
        2. It has not been deleted (xmax == 0), OR
           its deleter (xmax) has not committed OR committed after our snapshot
        """
        # The creating transaction must be committed and visible
        if self.xmin == txn_id:
            # We created this version — it's visible to us
            if self.xmax == txn_id:
                # But we also deleted it
                return False
            if self.xmax == 0:
                return True
            # Deleted by another transaction
            return not snapshot.is_committed(self.xmax)

        if not snapshot.is_committed(self.xmin):
            return False  # Creator hasn't committed

        if self.xmin in snapshot.active_txns:
            return False  # Creator was active when we started

        # Check if it's been deleted
        if self.xmax == 0:
            return True  # Not deleted

        if self.xmax == txn_id:
            return False  # We deleted it

        if not snapshot.is_committed(self.xmax):
            return True  # Deleter hasn't committed yet

        if self.xmax in snapshot.active_txns:
            return True  # Deleter was active when we started

        return False  # Deleter has committed and was visible


@dataclass
class TransactionSnapshot:
    """A point-in-time snapshot of transaction states.

    Captured when a transaction begins (or at statement start for
    READ COMMITTED isolation). Records which transactions were
    active and what the next transaction ID was at snapshot time.
    """
    txn_id: int                              # The transaction taking the snapshot
    active_txns: frozenset[int]              # Transactions active at snapshot time
    min_active_txn: int = 0                  # Lowest active transaction ID
    next_txn_id: int = 0                     # Next transaction ID at snapshot time
    _committed: Optional[frozenset[int]] = None  # Cache of committed txns

    def is_committed(self, txn_id: int) -> bool:
        """Check if a transaction was committed at snapshot time."""
        if self._committed is not None:
            return txn_id in self._committed
        # If not in active set and < next_txn_id, it's committed
        return txn_id < self.next_txn_id and txn_id not in self.active_txns

    def set_committed(self, committed: frozenset[int]) -> None:
        self._committed = committed


@dataclass
class Transaction:
    """Represents an active database transaction.

    Tracks all modifications made within the transaction for
    rollback capability and conflict detection.
    """
    txn_id: int
    state: TransactionState = TransactionState.ACTIVE
    isolation_level: IsolationLevel = IsolationLevel.SNAPSHOT
    snapshot: Optional[TransactionSnapshot] = None
    start_time: float = field(default_factory=time.time)
    write_set: set[tuple[str, Any]] = field(default_factory=set)  # (table, key) pairs modified
    read_set: set[tuple[str, Any]] = field(default_factory=set)   # (table, key) pairs read

    def add_write(self, table: str, key: Any) -> None:
        """Record a write to the transaction's write set."""
        self.write_set.add((table, key))

    def add_read(self, table: str, key: Any) -> None:
        """Record a read to the transaction's read set."""
        self.read_set.add((table, key))


class MVCCManager:
    """Manages multi-version concurrency control for the database.

    Provides transaction lifecycle management, snapshot creation,
    visibility checking, and conflict detection.

    Thread Safety:
        All operations are protected by a global lock for correctness.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._next_txn_id = 1
        self._active_txns: dict[int, Transaction] = {}
        self._committed_txns: set[int] = set()
        self._aborted_txns: set[int] = set()

        # Version storage: table_name → key → version_chain
        self._versions: dict[str, dict[Any, VersionRecord]] = {}

    def begin_transaction(
        self, isolation: IsolationLevel = IsolationLevel.SNAPSHOT
    ) -> Transaction:
        """Start a new transaction and take a snapshot.

        Returns a Transaction object with a unique ID and snapshot.
        """
        with self._lock:
            txn_id = self._next_txn_id
            self._next_txn_id += 1

            snapshot = TransactionSnapshot(
                txn_id=txn_id,
                active_txns=frozenset(self._active_txns.keys()),
                min_active_txn=min(self._active_txns.keys()) if self._active_txns else txn_id,
                next_txn_id=txn_id,
            )
            snapshot.set_committed(frozenset(self._committed_txns))

            txn = Transaction(
                txn_id=txn_id,
                isolation_level=isolation,
                snapshot=snapshot,
            )

            self._active_txns[txn_id] = txn
            return txn

    def commit_transaction(self, txn: Transaction) -> bool:
        """Commit a transaction.

        Validates that no write-write conflicts exist (first-updater-wins).
        Returns True if committed, False if aborted due to conflict.
        """
        with self._lock:
            if txn.state != TransactionState.ACTIVE:
                return False

            # Check for write-write conflicts
            for other_txn in self._active_txns.values():
                if other_txn.txn_id == txn.txn_id:
                    continue
                if other_txn.state == TransactionState.COMMITTED:
                    # Check if any of our writes conflict with their writes
                    if txn.write_set & other_txn.write_set:
                        self._do_abort(txn)
                        return False

            txn.state = TransactionState.COMMITTED
            self._committed_txns.add(txn.txn_id)
            del self._active_txns[txn.txn_id]
            return True

    def abort_transaction(self, txn: Transaction) -> None:
        """Abort a transaction and roll back its changes."""
        with self._lock:
            self._do_abort(txn)

    def _do_abort(self, txn: Transaction) -> None:
        """Internal abort implementation (caller holds lock)."""
        txn.state = TransactionState.ABORTED
        self._aborted_txns.add(txn.txn_id)
        self._active_txns.pop(txn.txn_id, None)

        # Roll back version changes
        for table_name, versions in self._versions.items():
            keys_to_check = list(versions.keys())
            for key in keys_to_check:
                version = versions[key]

                # If we created this version, remove it
                if version.xmin == txn.txn_id:
                    if version.prev_version:
                        # Restore previous version
                        versions[key] = version.prev_version
                        # Clear xmax on restored version if we set it
                        if versions[key].xmax == txn.txn_id:
                            versions[key].xmax = 0
                    else:
                        del versions[key]

                # If we deleted this version, undelete it
                elif version.xmax == txn.txn_id:
                    version.xmax = 0

    def write_version(
        self, txn: Transaction, table: str, key: Any, data: list[Any]
    ) -> bool:
        """Write a new version of a record.

        Creates a new version in the chain with xmin = txn_id.
        The previous version's xmax is set to txn_id.

        Returns False if a write-write conflict is detected.
        """
        with self._lock:
            if table not in self._versions:
                self._versions[table] = {}

            versions = self._versions[table]
            old_version = versions.get(key)

            if old_version is not None:
                # Check for write-write conflict
                if (old_version.xmax != 0 and
                    old_version.xmax != txn.txn_id and
                    old_version.xmax in self._active_txns):
                    return False  # Another active transaction is modifying this record

                # Mark old version as superseded
                old_version.xmax = txn.txn_id

            # Create new version
            new_version = VersionRecord(
                data=data,
                xmin=txn.txn_id,
                xmax=0,
                created_at=time.time(),
                prev_version=old_version,
            )

            versions[key] = new_version
            txn.add_write(table, key)
            return True

    def read_version(
        self, txn: Transaction, table: str, key: Any
    ) -> Optional[list[Any]]:
        """Read the visible version of a record for a transaction.

        Walks the version chain to find the version visible to
        the transaction's snapshot.
        """
        with self._lock:
            if table not in self._versions:
                return None

            version = self._versions[table].get(key)
            if version is None:
                return None

            # Walk the version chain to find the visible version
            while version is not None:
                if version.is_visible_to(txn.txn_id, txn.snapshot):  # type: ignore
                    txn.add_read(table, key)
                    return version.data
                version = version.prev_version

            return None

    def delete_version(self, txn: Transaction, table: str, key: Any) -> bool:
        """Mark a record as deleted by setting xmax on the current version.

        Returns False if a conflict is detected.
        """
        with self._lock:
            if table not in self._versions:
                return False

            version = self._versions[table].get(key)
            if version is None:
                return False

            # Check for write-write conflict
            if (version.xmax != 0 and
                version.xmax != txn.txn_id and
                version.xmax in self._active_txns):
                return False

            version.xmax = txn.txn_id
            txn.add_write(table, key)
            return True

    def garbage_collect(self) -> int:
        """Remove old versions that are no longer visible to any transaction.

        A version can be garbage collected if:
        1. It has been superseded (xmax is set)
        2. No active transaction can see it

        Returns the number of versions collected.
        """
        with self._lock:
            if not self._active_txns:
                min_active = self._next_txn_id
            else:
                min_active = min(self._active_txns.keys())

            collected = 0
            for table_versions in self._versions.values():
                for key in list(table_versions.keys()):
                    version = table_versions[key]

                    # Trim the version chain
                    prev = version.prev_version
                    while prev is not None and prev.prev_version is not None:
                        if (prev.xmax != 0 and
                            prev.xmax < min_active and
                            prev.xmax in self._committed_txns):
                            # This version and everything before it is invisible
                            prev.prev_version = None
                            collected += 1
                            break
                        prev = prev.prev_version

            return collected

    def get_stats(self) -> dict:
        """Return MVCC statistics."""
        total_versions = sum(
            sum(1 for _ in self._count_chain(v))
            for versions in self._versions.values()
            for v in versions.values()
        )
        return {
            "active_transactions": len(self._active_txns),
            "committed_transactions": len(self._committed_txns),
            "aborted_transactions": len(self._aborted_txns),
            "total_versions": total_versions,
            "tables_with_versions": len(self._versions),
        }

    @staticmethod
    def _count_chain(version: VersionRecord) -> list[VersionRecord]:
        """Count versions in a chain."""
        chain = []
        while version is not None:
            chain.append(version)
            version = version.prev_version
        return chain
