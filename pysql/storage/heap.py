"""
Heap File — Unordered Table Storage.

A heap file is the primary storage structure for table data. Records are
stored in an unordered collection of pages, with new records appended to
the first page with sufficient free space.

Architecture:
    Table "users" → HeapFile
        ├─ Page 0 (header page with metadata)
        ├─ Page 1 (data page with records)
        ├─ Page 2 (data page with records)
        └─ Page N (data page with records)

Each record is identified by a RecordId (page_id, slot_index), which
remains stable across updates (thanks to the slotted page architecture).

Record Format:
    ┌──────────────────────────────────────────┐
    │ Null Bitmap (⌈num_cols/8⌉ bytes)         │
    │   Each bit: 1 = column is NULL            │
    ├──────────────────────────────────────────┤
    │ Column 1 data (serialized via DataType)   │
    │ Column 2 data (serialized via DataType)   │
    │ ...                                       │
    │ Column N data (serialized via DataType)   │
    └──────────────────────────────────────────┘

NULL values are encoded in the bitmap and occupy zero bytes in the
data portion, saving storage space.
"""

from __future__ import annotations

import math
import struct
from dataclasses import dataclass
from typing import Any, Iterator, Optional

from ..types import NULL, DataType, NullValue, type_from_string
from .buffer_pool import BufferPool
from .page import INVALID_PAGE_ID, Page, PageType


@dataclass(frozen=True)
class RecordId:
    """A unique identifier for a record within a heap file.

    Consists of a page_id and a slot_index within that page.
    This tuple uniquely identifies any record in the database.
    """
    page_id: int
    slot_index: int

    def __repr__(self) -> str:
        return f"RID({self.page_id}:{self.slot_index})"


@dataclass
class TableSchema:
    """Schema definition for a table.

    Contains column names, types, and constraints needed for
    record serialization/deserialization.
    """
    table_name: str
    column_names: list[str]
    column_types: list[DataType]
    primary_key: Optional[str] = None

    @property
    def num_columns(self) -> int:
        return len(self.column_names)

    def column_index(self, name: str) -> int:
        """Get the index of a column by name. Raises ValueError if not found."""
        try:
            return self.column_names.index(name)
        except ValueError:
            raise ValueError(f"Column '{name}' not found in table '{self.table_name}'")


class RecordSerializer:
    """Handles serialization/deserialization of records to/from bytes.

    Uses a null bitmap to efficiently handle NULL values, followed by
    the serialized column data in schema order.
    """

    def __init__(self, schema: TableSchema) -> None:
        self._schema = schema
        self._null_bitmap_size = math.ceil(schema.num_columns / 8)

    def serialize(self, values: list[Any]) -> bytes:
        """Serialize a list of column values into a byte record.

        Args:
            values: List of Python values, one per column. Use NULL for null values.

        Returns:
            The serialized record bytes.
        """
        if len(values) != self._schema.num_columns:
            raise ValueError(
                f"Expected {self._schema.num_columns} values, got {len(values)}"
            )

        # Build null bitmap
        null_bitmap = bytearray(self._null_bitmap_size)
        for i, val in enumerate(values):
            if isinstance(val, NullValue) or val is None:
                byte_idx = i // 8
                bit_idx = i % 8
                null_bitmap[byte_idx] |= (1 << bit_idx)

        # Serialize non-null columns
        parts = [bytes(null_bitmap)]
        for i, val in enumerate(values):
            if isinstance(val, NullValue) or val is None:
                continue  # NULL columns take zero space
            col_type = self._schema.column_types[i]
            parts.append(col_type.serialize(val))

        return b"".join(parts)

    def deserialize(self, data: bytes) -> list[Any]:
        """Deserialize a byte record into a list of column values.

        Returns a list of Python values, with NULL for null columns.
        """
        # Read null bitmap
        null_bitmap = data[:self._null_bitmap_size]
        offset = self._null_bitmap_size

        values: list[Any] = []
        for i in range(self._schema.num_columns):
            byte_idx = i // 8
            bit_idx = i % 8
            is_null = bool(null_bitmap[byte_idx] & (1 << bit_idx))

            if is_null:
                values.append(NULL)
            else:
                col_type = self._schema.column_types[i]
                val = col_type.deserialize(data[offset:])
                values.append(val)
                offset += col_type.storage_size(val)

        return values


class HeapFile:
    """Heap file storage for a single table.

    Manages a collection of pages containing records for one table.
    Provides iterator-based full table scans and record-level CRUD.

    The first page (page 0 allocated for this table) stores metadata.
    Subsequent pages store actual record data.
    """

    def __init__(
        self,
        schema: TableSchema,
        buffer_pool: BufferPool,
        first_page_id: Optional[int] = None,
    ) -> None:
        self._schema = schema
        self._pool = buffer_pool
        self._serializer = RecordSerializer(schema)
        self._page_ids: list[int] = []

        if first_page_id is not None:
            # Existing table — load page chain
            self._load_page_chain(first_page_id)
        else:
            # New table — allocate first data page
            page = self._pool.new_page(PageType.HEAP_DATA)
            self._page_ids.append(page.header.page_id)
            self._pool.unpin_page(page.header.page_id, is_dirty=True)

    @property
    def first_page_id(self) -> int:
        """The page ID of the first data page (used for catalog storage)."""
        return self._page_ids[0] if self._page_ids else -1

    def insert(self, values: list[Any]) -> RecordId:
        """Insert a record into the heap file.

        Tries each existing page in order, allocating a new page if needed.

        Returns the RecordId of the inserted record.
        """
        record_data = self._serializer.serialize(values)

        # Try inserting into existing pages
        for page_id in self._page_ids:
            page = self._pool.fetch_page(page_id)
            slot = page.insert_record(record_data)
            if slot is not None:
                self._pool.unpin_page(page_id, is_dirty=True)
                return RecordId(page_id=page_id, slot_index=slot)
            self._pool.unpin_page(page_id)

        # All pages full — allocate a new one
        new_page = self._pool.new_page(PageType.HEAP_DATA)
        new_page_id = new_page.header.page_id
        self._page_ids.append(new_page_id)

        # Link the new page to the chain
        if len(self._page_ids) > 1:
            prev_page_id = self._page_ids[-2]
            prev_page = self._pool.fetch_page(prev_page_id)
            prev_page.header.next_page = new_page_id
            new_page.header.prev_page = prev_page_id
            self._pool.unpin_page(prev_page_id, is_dirty=True)

        slot = new_page.insert_record(record_data)
        self._pool.unpin_page(new_page_id, is_dirty=True)

        if slot is None:
            raise RuntimeError("Record too large to fit in a single page")

        return RecordId(page_id=new_page_id, slot_index=slot)

    def read(self, rid: RecordId) -> Optional[list[Any]]:
        """Read a record by its RecordId.

        Returns the deserialized column values, or None if the record is deleted.
        """
        page = self._pool.fetch_page(rid.page_id)
        data = page.read_record(rid.slot_index)
        self._pool.unpin_page(rid.page_id)

        if data is None:
            return None

        return self._serializer.deserialize(data)

    def update(self, rid: RecordId, values: list[Any]) -> bool:
        """Update a record in-place.

        Returns True if successful, False if the record was deleted or
        couldn't fit (in which case, delete + re-insert may be needed).
        """
        record_data = self._serializer.serialize(values)
        page = self._pool.fetch_page(rid.page_id)
        success = page.update_record(rid.slot_index, record_data)
        self._pool.unpin_page(rid.page_id, is_dirty=success)
        return success

    def delete(self, rid: RecordId) -> bool:
        """Delete a record by its RecordId.

        Marks the record's slot as deleted (tombstone). Space is reclaimed
        during page compaction.
        """
        page = self._pool.fetch_page(rid.page_id)
        success = page.delete_record(rid.slot_index)
        self._pool.unpin_page(rid.page_id, is_dirty=success)
        return success

    def scan(self) -> Iterator[tuple[RecordId, list[Any]]]:
        """Full table scan — iterate over all live records.

        Yields (RecordId, values) tuples for every non-deleted record.
        Pages are scanned in order, and each page is pinned only during
        its iteration.
        """
        for page_id in self._page_ids:
            page = self._pool.fetch_page(page_id)
            records = page.get_all_records()
            self._pool.unpin_page(page_id)

            for slot_index, data in records:
                values = self._serializer.deserialize(data)
                yield RecordId(page_id=page_id, slot_index=slot_index), values

    def get_record_count(self) -> int:
        """Count the total number of live records (full scan)."""
        count = 0
        for page_id in self._page_ids:
            page = self._pool.fetch_page(page_id)
            count += page.get_num_records()
            self._pool.unpin_page(page_id)
        return count

    def _load_page_chain(self, first_page_id: int) -> None:
        """Load the chain of pages starting from the first page ID."""
        self._page_ids = []
        current = first_page_id

        while current != INVALID_PAGE_ID:
            self._page_ids.append(current)
            page = self._pool.fetch_page(current)
            next_id = page.header.next_page
            self._pool.unpin_page(current)
            current = next_id
