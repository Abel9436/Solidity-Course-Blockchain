"""
Database Catalog — Schema Metadata Management.

The catalog is the database's "data dictionary" — it stores metadata about
all tables, columns, indexes, and constraints. In real databases (PostgreSQL,
MySQL), the catalog itself is stored as regular tables (pg_catalog, information_schema).

Our catalog manages:
- Table schemas (names, column definitions, constraints)
- Index metadata (which columns are indexed, uniqueness)
- Table-to-heap-file mapping (where table data is stored)
- Auto-increment counters

The catalog is persisted as a JSON file for simplicity (a production
implementation would store it in database pages).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from .types import DataType, type_from_string
from .storage.heap import TableSchema


@dataclass
class IndexInfo:
    """Metadata for a single index."""
    name: str
    table_name: str
    columns: list[str]
    is_unique: bool = False
    is_primary: bool = False


@dataclass
class TableInfo:
    """Complete metadata for a single table."""
    name: str
    columns: list[dict[str, Any]]   # [{name, type, constraints}]
    primary_key: Optional[str] = None
    first_page_id: int = -1         # First heap page for this table
    indexes: list[IndexInfo] = field(default_factory=list)
    auto_increment_counter: int = 0
    row_count_estimate: int = 0     # Used by query optimizer

    def get_schema(self) -> TableSchema:
        """Convert to a TableSchema for the storage layer."""
        col_names = [c["name"] for c in self.columns]
        col_types = [type_from_string(c["type"]) for c in self.columns]
        return TableSchema(
            table_name=self.name,
            column_names=col_names,
            column_types=col_types,
            primary_key=self.primary_key,
        )

    def column_names(self) -> list[str]:
        return [c["name"] for c in self.columns]

    def column_type(self, name: str) -> DataType:
        for c in self.columns:
            if c["name"] == name:
                return type_from_string(c["type"])
        raise ValueError(f"Column '{name}' not found in table '{self.name}'")

    def has_column(self, name: str) -> bool:
        return any(c["name"] == name for c in self.columns)


class Catalog:
    """Database-wide schema catalog.

    Stores and manages metadata for all database objects.
    Persists to a JSON file for durability across restarts.
    """

    def __init__(self, catalog_path: str | Path) -> None:
        self._path = Path(catalog_path)
        self._tables: dict[str, TableInfo] = {}
        self._indexes: dict[str, IndexInfo] = {}

        if self._path.exists():
            self._load()

    def create_table(self, table_info: TableInfo) -> None:
        """Register a new table in the catalog."""
        if table_info.name in self._tables:
            raise ValueError(f"Table '{table_info.name}' already exists")
        self._tables[table_info.name] = table_info
        self._save()

    def drop_table(self, name: str) -> None:
        """Remove a table from the catalog."""
        if name not in self._tables:
            raise ValueError(f"Table '{name}' does not exist")

        # Remove associated indexes
        table_indexes = [
            idx_name for idx_name, idx in self._indexes.items()
            if idx.table_name == name
        ]
        for idx_name in table_indexes:
            del self._indexes[idx_name]

        del self._tables[name]
        self._save()

    def get_table(self, name: str) -> Optional[TableInfo]:
        """Get table metadata by name."""
        return self._tables.get(name)

    def table_exists(self, name: str) -> bool:
        """Check if a table exists."""
        return name in self._tables

    def list_tables(self) -> list[str]:
        """List all table names."""
        return list(self._tables.keys())

    def create_index(self, index_info: IndexInfo) -> None:
        """Register a new index in the catalog."""
        if index_info.name in self._indexes:
            raise ValueError(f"Index '{index_info.name}' already exists")
        self._indexes[index_info.name] = index_info

        # Also add to the table's index list
        table = self._tables.get(index_info.table_name)
        if table:
            table.indexes.append(index_info)

        self._save()

    def get_indexes_for_table(self, table_name: str) -> list[IndexInfo]:
        """Get all indexes for a given table."""
        return [
            idx for idx in self._indexes.values()
            if idx.table_name == table_name
        ]

    def get_index(self, name: str) -> Optional[IndexInfo]:
        """Get index metadata by name."""
        return self._indexes.get(name)

    def update_table_stats(self, table_name: str, row_count: int) -> None:
        """Update table statistics (used by the query optimizer)."""
        if table_name in self._tables:
            self._tables[table_name].row_count_estimate = row_count
            self._save()

    def increment_auto_counter(self, table_name: str) -> int:
        """Increment and return the next auto-increment value for a table."""
        table = self._tables.get(table_name)
        if table is None:
            raise ValueError(f"Table '{table_name}' does not exist")
        table.auto_increment_counter += 1
        self._save()
        return table.auto_increment_counter

    def update_first_page_id(self, table_name: str, page_id: int) -> None:
        """Update the first page ID for a table's heap file."""
        if table_name in self._tables:
            self._tables[table_name].first_page_id = page_id
            self._save()

    # ─── Persistence ─────────────────────────────────────────────────────

    def _save(self) -> None:
        """Persist the catalog to disk as JSON."""
        data = {
            "tables": {},
            "indexes": {},
        }

        for name, table in self._tables.items():
            data["tables"][name] = {
                "name": table.name,
                "columns": table.columns,
                "primary_key": table.primary_key,
                "first_page_id": table.first_page_id,
                "auto_increment_counter": table.auto_increment_counter,
                "row_count_estimate": table.row_count_estimate,
            }

        for name, index in self._indexes.items():
            data["indexes"][name] = {
                "name": index.name,
                "table_name": index.table_name,
                "columns": index.columns,
                "is_unique": index.is_unique,
                "is_primary": index.is_primary,
            }

        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(data, indent=2))

    def _load(self) -> None:
        """Load the catalog from disk."""
        try:
            data = json.loads(self._path.read_text())
        except (json.JSONDecodeError, FileNotFoundError):
            return

        for name, tdata in data.get("tables", {}).items():
            indexes = []
            self._tables[name] = TableInfo(
                name=tdata["name"],
                columns=tdata["columns"],
                primary_key=tdata.get("primary_key"),
                first_page_id=tdata.get("first_page_id", -1),
                indexes=indexes,
                auto_increment_counter=tdata.get("auto_increment_counter", 0),
                row_count_estimate=tdata.get("row_count_estimate", 0),
            )

        for name, idata in data.get("indexes", {}).items():
            idx = IndexInfo(
                name=idata["name"],
                table_name=idata["table_name"],
                columns=idata["columns"],
                is_unique=idata.get("is_unique", False),
                is_primary=idata.get("is_primary", False),
            )
            self._indexes[name] = idx

            # Link to table
            table = self._tables.get(idx.table_name)
            if table:
                table.indexes.append(idx)
