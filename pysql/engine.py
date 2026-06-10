"""
PySQLEngine — Database Engine.

This is the main entry point that wires together all components:
- Parser (SQL → AST)
- Catalog (schema metadata)
- Storage Engine (pages, buffer pool, heap files)
- B+Tree Indexes
- WAL (crash recovery)
- MVCC (concurrency control)
- Query Planner/Optimizer
- Executor

Usage:
    db = Database("my_database")
    db.execute("CREATE TABLE users (id INTEGER PRIMARY KEY, name TEXT)")
    db.execute("INSERT INTO users VALUES (1, 'Alice')")
    result = db.execute("SELECT * FROM users")
    print(result.to_table_string())
    db.close()
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from .catalog import Catalog
from .executor import Executor, QueryResult
from .mvcc import MVCCManager, Transaction
from .parser import Parser
from .planner import QueryPlanner
from .storage.buffer_pool import BufferPool
from .storage.page import DiskManager
from .storage.wal import WALManager


class Database:
    """The main database engine — ties all components together.

    Each Database instance manages a single database stored at the
    given path. The database consists of:
    - A data file (.db) containing all table pages
    - A WAL file (.db.wal) for crash recovery
    - A catalog file (.db.catalog) for schema metadata
    """

    def __init__(
        self,
        path: str = "pysql_data",
        buffer_pool_size: int = 1024,
    ) -> None:
        self._path = Path(path)
        self._path.mkdir(parents=True, exist_ok=True)

        db_file = self._path / "data.db"
        wal_file = self._path / "data.db.wal"
        catalog_file = self._path / "data.db.catalog"

        # Initialize storage layer
        self._disk = DiskManager(db_file)
        self._pool = BufferPool(self._disk, pool_size=buffer_pool_size)
        self._wal = WALManager(wal_file)

        # Initialize metadata
        self._catalog = Catalog(catalog_file)

        # Initialize concurrency control
        self._mvcc = MVCCManager()

        # Initialize query processing
        self._planner = QueryPlanner(self._catalog)
        self._executor = Executor(
            self._catalog, self._pool, self._disk, self._wal, self._mvcc
        )

        # Recover from any crash
        self._recover()

    def execute(self, sql: str) -> QueryResult:
        """Execute a SQL statement and return the result.

        This is the primary interface for database interaction.
        Each call runs in an auto-commit transaction unless an
        explicit transaction is active.
        """
        # Parse
        parser = Parser(sql)
        statements = parser.parse()

        if not statements:
            return QueryResult(columns=[], rows=[], message="No statement to execute")

        # Execute each statement
        last_result = QueryResult(columns=[], rows=[])
        for stmt in statements:
            # Plan
            plan = self._planner.plan(stmt)
            # Execute
            last_result = self._executor.execute(plan)

        return last_result

    def execute_many(self, sql: str) -> list[QueryResult]:
        """Execute multiple SQL statements separated by semicolons."""
        parser = Parser(sql)
        statements = parser.parse()
        results = []

        for stmt in statements:
            plan = self._planner.plan(stmt)
            result = self._executor.execute(plan)
            results.append(result)

        return results

    def explain(self, sql: str) -> str:
        """Show the execution plan for a SQL statement without executing it."""
        parser = Parser(sql)
        statements = parser.parse()

        if not statements:
            return "No statement to explain"

        plan = self._planner.plan(statements[0])
        return plan.explain()

    def begin(self) -> Transaction:
        """Begin a new explicit transaction."""
        return self._mvcc.begin_transaction()

    def commit(self, txn: Transaction) -> bool:
        """Commit a transaction."""
        success = self._mvcc.commit_transaction(txn)
        if success:
            self._wal.log_commit(txn.txn_id)
        return success

    def rollback(self, txn: Transaction) -> None:
        """Rollback a transaction."""
        self._mvcc.abort_transaction(txn)
        self._wal.log_abort(txn.txn_id)

    def checkpoint(self) -> None:
        """Perform a checkpoint: flush all dirty pages and truncate WAL."""
        self._pool.flush_all()
        self._wal.log_checkpoint()

    def get_stats(self) -> dict:
        """Return database statistics."""
        return {
            "tables": self._catalog.list_tables(),
            "buffer_pool": self._pool.get_stats(),
            "mvcc": self._mvcc.get_stats(),
            "wal": {
                "next_lsn": self._wal.next_lsn,
                "flushed_lsn": self._wal.flushed_lsn,
            },
            "disk": {
                "total_pages": self._disk.get_num_pages(),
            },
        }

    def close(self) -> None:
        """Shut down the database cleanly."""
        self.checkpoint()
        self._pool.flush_all()
        self._disk.sync()

    def _recover(self) -> None:
        """Perform crash recovery using WAL."""
        committed, aborted = self._wal.recover()
        # In a full implementation, we'd replay/undo changes here
        # For now, just log the recovery info
        if committed or aborted:
            pass  # Recovery handled by WAL manager

    def __enter__(self) -> "Database":
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def __repr__(self) -> str:
        tables = self._catalog.list_tables()
        return f"Database(path={self._path}, tables={tables})"
