"""
Write-Ahead Logging (WAL) — Crash Recovery System.

WAL is the cornerstone of database durability (the "D" in ACID).
The core principle: before ANY modification reaches the data file,
a log record describing the change is written to the WAL file first.

This ensures that even if the system crashes mid-write, we can
recover to a consistent state by replaying the log.

WAL Protocol:
    1. Before modifying a page, write a log record to the WAL
    2. The log record contains both UNDO and REDO information
    3. The WAL must be flushed to disk BEFORE the dirty page
    4. On recovery, replay the log to restore consistency

Log Record Format:
    ┌─────────────────────────────────────────────┐
    │ LSN (8 bytes) - Log Sequence Number          │
    │ TxnId (4 bytes) - Transaction ID             │
    │ Type (1 byte) - Log record type              │
    │ PrevLSN (8 bytes) - Previous LSN for txn     │
    │ TableId (4 bytes) - Affected table           │
    │ PageId (4 bytes) - Affected page             │
    │ SlotId (2 bytes) - Affected slot             │
    │ UndoDataLen (4 bytes) - Length of undo data  │
    │ UndoData (variable) - Old value (for UNDO)   │
    │ RedoDataLen (4 bytes) - Length of redo data  │
    │ RedoData (variable) - New value (for REDO)   │
    └─────────────────────────────────────────────┘

Recovery Algorithm (ARIES-inspired):
    1. Analysis Pass: Scan WAL to find active transactions at crash
    2. Redo Pass: Replay all committed changes (forward scan)
    3. Undo Pass: Rollback all uncommitted changes (backward scan)
"""

from __future__ import annotations

import struct
import os
import time
from dataclasses import dataclass, field
from enum import IntEnum
from pathlib import Path
from typing import BinaryIO, Optional


class LogRecordType(IntEnum):
    """Types of WAL log records."""
    BEGIN = 1          # Transaction begin
    COMMIT = 2         # Transaction commit
    ABORT = 3          # Transaction abort
    INSERT = 4         # Record insertion
    DELETE = 5         # Record deletion
    UPDATE = 6         # Record update
    CHECKPOINT = 7     # Checkpoint marker
    CLR = 8            # Compensation Log Record (for undo recovery)
    END = 9            # Transaction end (cleanup complete)


# Fixed portion of log record header (before variable-length data)
LOG_HEADER_SIZE = 35  # 8 + 4 + 1 + 8 + 4 + 4 + 2 + 4 = 35 bytes


@dataclass
class LogRecord:
    """A single WAL log record.

    Contains sufficient information to both redo (replay forward) and
    undo (rollback) the described operation.
    """
    lsn: int = 0                          # Monotonically increasing sequence number
    txn_id: int = 0                       # Transaction that made this change
    record_type: LogRecordType = LogRecordType.BEGIN
    prev_lsn: int = 0                     # Previous LSN for this transaction
    table_id: int = 0                     # Which table was affected
    page_id: int = 0                      # Which page was affected
    slot_id: int = 0                      # Which slot was affected
    undo_data: bytes = b""                # Old data (for UNDO/rollback)
    redo_data: bytes = b""                # New data (for REDO/replay)

    def serialize(self) -> bytes:
        """Serialize the log record to bytes.

        Format: fixed header + undo_data_len + undo_data + redo_data_len + redo_data
        """
        header = struct.pack(
            ">qIBqIIH",
            self.lsn,
            self.txn_id,
            self.record_type,
            self.prev_lsn,
            self.table_id,
            self.page_id,
            self.slot_id,
        )
        undo_part = struct.pack(">I", len(self.undo_data)) + self.undo_data
        redo_part = struct.pack(">I", len(self.redo_data)) + self.redo_data

        record = header + undo_part + redo_part

        # Append record length at the end for backward scanning
        record += struct.pack(">I", len(record))

        return record

    @classmethod
    def deserialize(cls, data: bytes, offset: int = 0) -> tuple["LogRecord", int]:
        """Deserialize a log record from bytes.

        Returns the LogRecord and the number of bytes consumed.
        """
        (
            lsn, txn_id, record_type, prev_lsn,
            table_id, page_id, slot_id,
        ) = struct.unpack(">qIBqIIH", data[offset:offset + LOG_HEADER_SIZE])

        pos = offset + LOG_HEADER_SIZE

        undo_len = struct.unpack(">I", data[pos:pos + 4])[0]
        pos += 4
        undo_data = data[pos:pos + undo_len]
        pos += undo_len

        redo_len = struct.unpack(">I", data[pos:pos + 4])[0]
        pos += 4
        redo_data = data[pos:pos + redo_len]
        pos += redo_len

        # Skip the trailing length field
        pos += 4

        record = cls(
            lsn=lsn,
            txn_id=txn_id,
            record_type=LogRecordType(record_type),
            prev_lsn=prev_lsn,
            table_id=table_id,
            page_id=page_id,
            slot_id=slot_id,
            undo_data=undo_data,
            redo_data=redo_data,
        )

        return record, pos - offset


class WALManager:
    """Manages the Write-Ahead Log file and provides recovery capabilities.

    The WAL file is an append-only sequential log of all modifications.
    It is the source of truth for crash recovery.

    Usage:
        wal = WALManager("/path/to/db.wal")
        lsn = wal.append(log_record)
        wal.flush()  # Ensure durability
    """

    def __init__(self, wal_path: str | Path) -> None:
        self._path = Path(wal_path)
        self._next_lsn = 1
        self._flushed_lsn = 0
        self._buffer: list[LogRecord] = []
        self._txn_table: dict[int, int] = {}  # txn_id → last_lsn

        # Load existing WAL if present
        if self._path.exists():
            self._recover_lsn()
        else:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.touch()

    @property
    def next_lsn(self) -> int:
        """The next LSN that will be assigned."""
        return self._next_lsn

    @property
    def flushed_lsn(self) -> int:
        """The highest LSN that has been flushed to disk."""
        return self._flushed_lsn

    def append(self, record: LogRecord) -> int:
        """Append a log record to the WAL buffer.

        Assigns an LSN to the record and tracks the transaction chain.
        The record is NOT yet on disk — call flush() for durability.

        Returns the assigned LSN.
        """
        record.lsn = self._next_lsn
        record.prev_lsn = self._txn_table.get(record.txn_id, 0)

        self._txn_table[record.txn_id] = record.lsn
        self._buffer.append(record)
        self._next_lsn += 1

        return record.lsn

    def log_begin(self, txn_id: int) -> int:
        """Log a transaction begin."""
        record = LogRecord(txn_id=txn_id, record_type=LogRecordType.BEGIN)
        return self.append(record)

    def log_commit(self, txn_id: int) -> int:
        """Log a transaction commit.

        IMPORTANT: The WAL MUST be flushed after logging a commit
        to guarantee durability.
        """
        record = LogRecord(txn_id=txn_id, record_type=LogRecordType.COMMIT)
        lsn = self.append(record)
        self.flush()  # Force flush on commit for durability
        return lsn

    def log_abort(self, txn_id: int) -> int:
        """Log a transaction abort."""
        record = LogRecord(txn_id=txn_id, record_type=LogRecordType.ABORT)
        return self.append(record)

    def log_insert(
        self, txn_id: int, table_id: int, page_id: int, slot_id: int,
        redo_data: bytes
    ) -> int:
        """Log a record insertion.

        For inserts, undo_data is empty (undo = delete the record).
        """
        record = LogRecord(
            txn_id=txn_id,
            record_type=LogRecordType.INSERT,
            table_id=table_id,
            page_id=page_id,
            slot_id=slot_id,
            redo_data=redo_data,
        )
        return self.append(record)

    def log_delete(
        self, txn_id: int, table_id: int, page_id: int, slot_id: int,
        undo_data: bytes
    ) -> int:
        """Log a record deletion.

        For deletes, redo_data is empty (redo = delete the record).
        undo_data contains the full record for rollback.
        """
        record = LogRecord(
            txn_id=txn_id,
            record_type=LogRecordType.DELETE,
            table_id=table_id,
            page_id=page_id,
            slot_id=slot_id,
            undo_data=undo_data,
        )
        return self.append(record)

    def log_update(
        self, txn_id: int, table_id: int, page_id: int, slot_id: int,
        undo_data: bytes, redo_data: bytes
    ) -> int:
        """Log a record update with both old and new values."""
        record = LogRecord(
            txn_id=txn_id,
            record_type=LogRecordType.UPDATE,
            table_id=table_id,
            page_id=page_id,
            slot_id=slot_id,
            undo_data=undo_data,
            redo_data=redo_data,
        )
        return self.append(record)

    def log_checkpoint(self) -> int:
        """Log a checkpoint marker.

        A checkpoint indicates that all dirty pages have been flushed
        to disk up to this point, limiting recovery time.
        """
        record = LogRecord(record_type=LogRecordType.CHECKPOINT)
        lsn = self.append(record)
        self.flush()
        return lsn

    def flush(self) -> None:
        """Flush all buffered log records to disk.

        This is the critical durability operation. After flush() returns,
        all buffered records are guaranteed to be on stable storage.
        """
        if not self._buffer:
            return

        with open(self._path, "ab") as f:
            for record in self._buffer:
                f.write(record.serialize())
            f.flush()
            os.fsync(f.fileno())

        if self._buffer:
            self._flushed_lsn = self._buffer[-1].lsn
        self._buffer.clear()

    def read_all(self) -> list[LogRecord]:
        """Read all log records from the WAL file (for recovery)."""
        records: list[LogRecord] = []

        if not self._path.exists() or self._path.stat().st_size == 0:
            return records

        data = self._path.read_bytes()
        offset = 0

        while offset < len(data):
            try:
                record, consumed = LogRecord.deserialize(data, offset)
                records.append(record)
                offset += consumed
            except (struct.error, IndexError):
                # Corrupted record at the end (partial write before crash)
                break

        return records

    def recover(self) -> tuple[set[int], set[int]]:
        """Perform crash recovery using a simplified ARIES algorithm.

        Returns:
            (committed_txns, aborted_txns) - Sets of transaction IDs

        Recovery phases:
        1. Analysis: Determine which transactions were active at crash
        2. Committed transactions need their changes redone
        3. Uncommitted transactions need their changes undone
        """
        records = self.read_all()

        # Analysis phase: determine transaction states
        active_txns: dict[int, list[LogRecord]] = {}
        committed: set[int] = set()
        aborted: set[int] = set()

        for record in records:
            txn_id = record.txn_id

            if record.record_type == LogRecordType.BEGIN:
                active_txns[txn_id] = []
            elif record.record_type == LogRecordType.COMMIT:
                committed.add(txn_id)
                active_txns.pop(txn_id, None)
            elif record.record_type == LogRecordType.ABORT:
                aborted.add(txn_id)
                active_txns.pop(txn_id, None)
            elif record.record_type in (
                LogRecordType.INSERT, LogRecordType.DELETE, LogRecordType.UPDATE
            ):
                if txn_id in active_txns:
                    active_txns[txn_id].append(record)

        # Transactions that were active at crash time need to be aborted
        for txn_id in active_txns:
            aborted.add(txn_id)

        # Update next LSN
        if records:
            self._next_lsn = records[-1].lsn + 1
            self._flushed_lsn = records[-1].lsn

        return committed, aborted

    def truncate(self) -> None:
        """Truncate the WAL file (called after checkpoint)."""
        with open(self._path, "wb") as f:
            f.truncate(0)
        self._flushed_lsn = 0

    def _recover_lsn(self) -> None:
        """Recover the next LSN from existing WAL file."""
        records = self.read_all()
        if records:
            self._next_lsn = records[-1].lsn + 1
            self._flushed_lsn = records[-1].lsn
