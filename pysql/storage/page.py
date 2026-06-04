"""
Page-Based Storage Engine.

This module implements the lowest level of the storage hierarchy: fixed-size
disk pages. All data in the database is stored in 4KB pages, which are the
unit of I/O between disk and memory.

Page Layout (4096 bytes):
    ┌─────────────────────────────────────────────────────┐
    │ Page Header (32 bytes)                              │
    │   ├─ page_id    (4 bytes) - unique page identifier  │
    │   ├─ page_type  (1 byte)  - type enum               │
    │   ├─ flags      (1 byte)  - dirty, pinned, etc.     │
    │   ├─ num_slots  (2 bytes) - number of record slots   │
    │   ├─ free_space (2 bytes) - bytes of free space      │
    │   ├─ data_end   (2 bytes) - end of data region       │
    │   ├─ next_page  (4 bytes) - linked list pointer      │
    │   ├─ prev_page  (4 bytes) - linked list pointer      │
    │   ├─ lsn        (8 bytes) - Log Sequence Number      │
    │   └─ checksum   (4 bytes) - CRC32 integrity check    │
    ├─────────────────────────────────────────────────────┤
    │ Slot Directory (grows downward from header)         │
    │   Each slot: (offset: 2 bytes, length: 2 bytes)     │
    ├─────────────────────────────────────────────────────┤
    │ Free Space                                          │
    ├─────────────────────────────────────────────────────┤
    │ Record Data (grows upward from page end)            │
    └─────────────────────────────────────────────────────┘

This is a slotted-page architecture, commonly used in real databases
(PostgreSQL, SQLite, etc.). Records are accessed via slot directory
indirection, which allows compaction without invalidating external pointers.
"""

from __future__ import annotations

import struct
import zlib
from dataclasses import dataclass, field
from enum import IntEnum
from pathlib import Path
from typing import Optional


# Page configuration constants
PAGE_SIZE = 4096           # 4KB pages (standard for most databases)
HEADER_SIZE = 32           # Fixed header size in bytes
SLOT_SIZE = 4              # Each slot entry: 2 bytes offset + 2 bytes length
INVALID_PAGE_ID = 0xFFFFFFFF


class PageType(IntEnum):
    """Enumeration of page types stored in the page header."""
    FREE = 0           # Unallocated page
    HEAP_DATA = 1      # Heap file data page
    BTREE_INTERNAL = 2 # B+Tree internal node page
    BTREE_LEAF = 3     # B+Tree leaf node page
    OVERFLOW = 4       # Overflow page for large records
    METADATA = 5       # Database/table metadata page


class PageFlags(IntEnum):
    """Bit flags stored in the page header."""
    CLEAN = 0x00
    DIRTY = 0x01
    PINNED = 0x02


@dataclass
class SlotEntry:
    """A single entry in the page's slot directory.

    Each slot points to a record's location within the page.
    A length of 0 indicates a deleted (tombstoned) slot.
    """
    offset: int    # Byte offset from page start to record data
    length: int    # Length of the record in bytes (0 = deleted)

    def is_deleted(self) -> bool:
        return self.length == 0

    def pack(self) -> bytes:
        return struct.pack(">HH", self.offset, self.length)

    @classmethod
    def unpack(cls, data: bytes) -> "SlotEntry":
        offset, length = struct.unpack(">HH", data[:SLOT_SIZE])
        return cls(offset=offset, length=length)


@dataclass
class PageHeader:
    """Fixed-size header at the beginning of every page.

    Contains metadata needed for page management, linking,
    and crash recovery (via LSN).
    """
    page_id: int = 0
    page_type: PageType = PageType.FREE
    flags: int = PageFlags.CLEAN
    num_slots: int = 0
    free_space: int = PAGE_SIZE - HEADER_SIZE
    data_end: int = PAGE_SIZE  # Points to the end of used data (grows backward)
    next_page: int = INVALID_PAGE_ID
    prev_page: int = INVALID_PAGE_ID
    lsn: int = 0              # Log Sequence Number for WAL recovery
    checksum: int = 0

    def pack(self) -> bytes:
        """Serialize the header to 32 bytes."""
        return struct.pack(
            ">IBBHHHIIqI",
            self.page_id,
            self.page_type,
            self.flags,
            self.num_slots,
            self.free_space,
            self.data_end,
            self.next_page,
            self.prev_page,
            self.lsn,
            self.checksum,
        )

    @classmethod
    def unpack(cls, data: bytes) -> "PageHeader":
        """Deserialize header from bytes."""
        (
            page_id, page_type, flags, num_slots,
            free_space, data_end, next_page, prev_page,
            lsn, checksum,
        ) = struct.unpack(">IBBHHHIIqI", data[:HEADER_SIZE])
        return cls(
            page_id=page_id,
            page_type=PageType(page_type),
            flags=flags,
            num_slots=num_slots,
            free_space=free_space,
            data_end=data_end,
            next_page=next_page,
            prev_page=prev_page,
            lsn=lsn,
            checksum=checksum,
        )


class Page:
    """A fixed-size (4KB) database page using slotted-page architecture.

    Records are stored at the end of the page and grow backward.
    The slot directory starts after the header and grows forward.
    Free space is the gap between the slot directory and record data.

    This design allows:
    1. Record IDs (page_id, slot_index) to remain stable after compaction
    2. Variable-length records within fixed-size pages
    3. Efficient space reclamation through compaction
    """

    def __init__(self, page_id: int = 0, page_type: PageType = PageType.FREE) -> None:
        self._data = bytearray(PAGE_SIZE)
        self.header = PageHeader(page_id=page_id, page_type=page_type)
        self._slots: list[SlotEntry] = []
        self._write_header()

    @classmethod
    def from_bytes(cls, data: bytes) -> "Page":
        """Deserialize a page from its raw byte representation."""
        page = cls.__new__(cls)
        page._data = bytearray(data[:PAGE_SIZE])
        page.header = PageHeader.unpack(page._data[:HEADER_SIZE])

        # Read slot directory
        page._slots = []
        slot_start = HEADER_SIZE
        for i in range(page.header.num_slots):
            offset = slot_start + i * SLOT_SIZE
            slot = SlotEntry.unpack(page._data[offset:offset + SLOT_SIZE])
            page._slots.append(slot)

        return page

    def to_bytes(self) -> bytes:
        """Serialize the page to its raw byte representation.

        Computes a CRC32 checksum over the entire page contents
        (excluding the checksum field itself) for integrity verification.
        """
        self._write_header()
        self._write_slots()

        # Compute checksum over everything except the checksum field
        # Checksum is the last 4 bytes of the header
        data_for_checksum = bytes(self._data[:HEADER_SIZE - 4]) + bytes(self._data[HEADER_SIZE:])
        self.header.checksum = zlib.crc32(data_for_checksum) & 0xFFFFFFFF
        struct.pack_into(">I", self._data, HEADER_SIZE - 4, self.header.checksum)

        return bytes(self._data)

    def verify_checksum(self) -> bool:
        """Verify the page's CRC32 checksum for data integrity."""
        data_for_checksum = bytes(self._data[:HEADER_SIZE - 4]) + bytes(self._data[HEADER_SIZE:])
        expected = zlib.crc32(data_for_checksum) & 0xFFFFFFFF
        return self.header.checksum == expected

    # ─── Record Operations ───────────────────────────────────────────────

    def insert_record(self, record: bytes) -> Optional[int]:
        """Insert a record into the page.

        Returns the slot index if successful, None if there's not enough space.
        The record is placed at the end of the data region (growing backward).
        """
        record_len = len(record)
        space_needed = record_len + SLOT_SIZE  # Need space for data + slot entry

        if space_needed > self.header.free_space:
            return None  # Page is full

        # Place record data at the end (growing backward)
        new_data_end = self.header.data_end - record_len
        self._data[new_data_end:new_data_end + record_len] = record

        # Create slot entry
        slot = SlotEntry(offset=new_data_end, length=record_len)

        # Try to reuse a deleted slot first
        reused = False
        for i, existing_slot in enumerate(self._slots):
            if existing_slot.is_deleted():
                self._slots[i] = slot
                reused = True
                slot_index = i
                break

        if not reused:
            self._slots.append(slot)
            self.header.num_slots += 1
            slot_index = len(self._slots) - 1

        # Update header
        self.header.data_end = new_data_end
        self.header.free_space -= space_needed if not reused else record_len
        self.header.flags |= PageFlags.DIRTY

        return slot_index

    def read_record(self, slot_index: int) -> Optional[bytes]:
        """Read a record by its slot index.

        Returns None if the slot is deleted or doesn't exist.
        """
        if slot_index >= len(self._slots):
            return None

        slot = self._slots[slot_index]
        if slot.is_deleted():
            return None

        return bytes(self._data[slot.offset:slot.offset + slot.length])

    def update_record(self, slot_index: int, new_record: bytes) -> bool:
        """Update a record in-place.

        If the new record is the same size or smaller, update in-place.
        If larger, delete + re-insert (may fail if page is full).
        """
        if slot_index >= len(self._slots):
            return False

        slot = self._slots[slot_index]
        if slot.is_deleted():
            return False

        if len(new_record) <= slot.length:
            # Update in-place (may waste space if smaller, but that's OK)
            self._data[slot.offset:slot.offset + len(new_record)] = new_record
            old_length = slot.length
            self._slots[slot_index] = SlotEntry(offset=slot.offset, length=len(new_record))
            self.header.free_space += old_length - len(new_record)
            self.header.flags |= PageFlags.DIRTY
            return True
        else:
            # Need more space: delete and re-insert
            self.delete_record(slot_index)
            new_slot = self.insert_record(new_record)
            if new_slot is not None:
                # Swap slot entries to preserve the original slot index
                if new_slot != slot_index:
                    self._slots[slot_index], self._slots[new_slot] = (
                        self._slots[new_slot], self._slots[slot_index]
                    )
                return True
            return False

    def delete_record(self, slot_index: int) -> bool:
        """Delete a record by marking its slot as tombstoned.

        The space is not immediately reclaimed — call compact() to reclaim.
        """
        if slot_index >= len(self._slots):
            return False

        slot = self._slots[slot_index]
        if slot.is_deleted():
            return False

        self.header.free_space += slot.length
        self._slots[slot_index] = SlotEntry(offset=0, length=0)
        self.header.flags |= PageFlags.DIRTY
        return True

    def compact(self) -> None:
        """Compact the page by eliminating gaps from deleted records.

        This reorganizes records to be contiguous at the end of the page,
        eliminating fragmentation. Slot indices remain stable (slot directory
        entries are updated, not removed).
        """
        # Collect all live records with their slot indices
        live_records: list[tuple[int, bytes]] = []
        for i, slot in enumerate(self._slots):
            if not slot.is_deleted():
                record = bytes(self._data[slot.offset:slot.offset + slot.length])
                live_records.append((i, record))

        # Clear data region
        data_end = PAGE_SIZE
        for slot_idx, record in live_records:
            data_end -= len(record)
            self._data[data_end:data_end + len(record)] = record
            self._slots[slot_idx] = SlotEntry(offset=data_end, length=len(record))

        self.header.data_end = data_end
        # Recalculate free space
        slot_dir_end = HEADER_SIZE + len(self._slots) * SLOT_SIZE
        self.header.free_space = data_end - slot_dir_end
        self.header.flags |= PageFlags.DIRTY

    def get_free_space(self) -> int:
        """Return available space for new records (accounting for slot overhead)."""
        return self.header.free_space

    def get_num_records(self) -> int:
        """Return the number of live (non-deleted) records."""
        return sum(1 for s in self._slots if not s.is_deleted())

    def get_all_records(self) -> list[tuple[int, bytes]]:
        """Return all live records as (slot_index, data) tuples."""
        records = []
        for i, slot in enumerate(self._slots):
            if not slot.is_deleted():
                data = bytes(self._data[slot.offset:slot.offset + slot.length])
                records.append((i, data))
        return records

    # ─── Internal Methods ────────────────────────────────────────────────

    def _write_header(self) -> None:
        """Write the header to the page's byte buffer."""
        header_bytes = self.header.pack()
        self._data[:HEADER_SIZE] = header_bytes

    def _write_slots(self) -> None:
        """Write all slot entries to the page's byte buffer."""
        for i, slot in enumerate(self._slots):
            offset = HEADER_SIZE + i * SLOT_SIZE
            self._data[offset:offset + SLOT_SIZE] = slot.pack()


class DiskManager:
    """Manages reading and writing pages to/from the database file.

    The database file is a simple sequence of fixed-size pages.
    Page N starts at byte offset N * PAGE_SIZE in the file.

    Thread safety: This class is NOT thread-safe. The BufferPool
    provides synchronized access to pages.
    """

    def __init__(self, db_path: str | Path) -> None:
        self._path = Path(db_path)
        self._num_pages = 0

        if self._path.exists():
            file_size = self._path.stat().st_size
            self._num_pages = file_size // PAGE_SIZE
        else:
            # Create the file
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.touch()

    def read_page(self, page_id: int) -> Page:
        """Read a page from disk by its ID.

        Raises ValueError if the page_id is out of range.
        """
        if page_id >= self._num_pages:
            raise ValueError(f"Page {page_id} does not exist (max: {self._num_pages - 1})")

        with open(self._path, "rb") as f:
            f.seek(page_id * PAGE_SIZE)
            data = f.read(PAGE_SIZE)
            if len(data) < PAGE_SIZE:
                data = data + b"\x00" * (PAGE_SIZE - len(data))

        return Page.from_bytes(data)

    def write_page(self, page: Page) -> None:
        """Write a page to disk at its designated position.

        The page's checksum is computed before writing.
        """
        data = page.to_bytes()
        with open(self._path, "r+b") as f:
            f.seek(page.header.page_id * PAGE_SIZE)
            f.write(data)
            f.flush()

    def allocate_page(self, page_type: PageType = PageType.FREE) -> Page:
        """Allocate a new page at the end of the file.

        Returns a new Page with a unique page_id.
        """
        page_id = self._num_pages
        self._num_pages += 1

        page = Page(page_id=page_id, page_type=page_type)

        # Extend the file
        with open(self._path, "ab") as f:
            f.write(page.to_bytes())

        return page

    def get_num_pages(self) -> int:
        """Return the total number of pages in the database file."""
        return self._num_pages

    def sync(self) -> None:
        """Force all buffered writes to disk (fsync)."""
        with open(self._path, "r+b") as f:
            f.flush()
            import os
            os.fsync(f.fileno())
