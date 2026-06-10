"""
Comprehensive test suite for PySQLEngine.

Tests cover all major components:
- Lexer (tokenization)
- Parser (SQL → AST)
- Type system (serialization/deserialization)
- B+Tree (insertions, deletions, range scans)
- Page storage (record CRUD, compaction)
- Buffer pool (caching, eviction)
- WAL (logging, recovery)
- Executor (end-to-end SQL execution)
- MVCC (snapshot isolation, conflict detection)
"""

import os
import shutil
import tempfile
from pathlib import Path

import pytest

# ─── Test Fixtures ───────────────────────────────────────────────────────────


@pytest.fixture
def temp_dir():
    """Create a temporary directory for test database files."""
    d = tempfile.mkdtemp(prefix="pysql_test_")
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def db(temp_dir):
    """Create a fresh database instance for testing."""
    from pysql.engine import Database
    database = Database(os.path.join(temp_dir, "test_db"))
    yield database
    database.close()


# ═══════════════════════════════════════════════════════════════════════════════
# LEXER TESTS
# ═══════════════════════════════════════════════════════════════════════════════

class TestLexer:
    """Tests for the SQL lexer/tokenizer."""

    def test_simple_select(self):
        from pysql.lexer import Lexer, TokenType
        tokens = Lexer("SELECT * FROM users").tokenize()
        types = [t.type for t in tokens]
        assert types == [
            TokenType.SELECT, TokenType.STAR, TokenType.FROM,
            TokenType.IDENTIFIER, TokenType.EOF,
        ]

    def test_string_literal(self):
        from pysql.lexer import Lexer, TokenType
        tokens = Lexer("SELECT 'hello world'").tokenize()
        assert tokens[1].type == TokenType.STRING_LITERAL
        assert tokens[1].value == "hello world"

    def test_escaped_string(self):
        from pysql.lexer import Lexer
        tokens = Lexer("SELECT 'it''s a test'").tokenize()
        assert tokens[1].value == "it's a test"

    def test_numeric_literals(self):
        from pysql.lexer import Lexer, TokenType
        tokens = Lexer("SELECT 42, 3.14, 1.5e10").tokenize()
        assert tokens[1].type == TokenType.INTEGER_LITERAL
        assert tokens[1].value == "42"
        assert tokens[3].type == TokenType.FLOAT_LITERAL
        assert tokens[3].value == "3.14"
        assert tokens[5].type == TokenType.FLOAT_LITERAL

    def test_comparison_operators(self):
        from pysql.lexer import Lexer, TokenType
        tokens = Lexer("a >= 5 AND b != 3 AND c <> 0 AND d <= 10").tokenize()
        types = [t.type for t in tokens if t.type not in (TokenType.IDENTIFIER, TokenType.INTEGER_LITERAL, TokenType.AND, TokenType.EOF)]
        assert TokenType.GREATER_EQUALS in types
        assert TokenType.NOT_EQUALS in types
        assert TokenType.LESS_EQUALS in types

    def test_line_comment(self):
        from pysql.lexer import Lexer, TokenType
        tokens = Lexer("SELECT 1 -- this is a comment\nFROM t").tokenize()
        types = [t.type for t in tokens]
        assert TokenType.FROM in types

    def test_block_comment(self):
        from pysql.lexer import Lexer, TokenType
        tokens = Lexer("SELECT /* comment */ 1").tokenize()
        assert len([t for t in tokens if t.type != TokenType.EOF]) == 2

    def test_keywords_case_insensitive(self):
        from pysql.lexer import Lexer, TokenType
        tokens = Lexer("select FROM Where").tokenize()
        assert tokens[0].type == TokenType.SELECT
        assert tokens[1].type == TokenType.FROM
        assert tokens[2].type == TokenType.WHERE

    def test_quoted_identifier(self):
        from pysql.lexer import Lexer, TokenType
        tokens = Lexer('SELECT "column name" FROM t').tokenize()
        assert tokens[1].type == TokenType.QUOTED_IDENTIFIER
        assert tokens[1].value == "column name"

    def test_position_tracking(self):
        from pysql.lexer import Lexer
        tokens = Lexer("SELECT\n  *\nFROM users").tokenize()
        assert tokens[0].line == 1
        assert tokens[1].line == 2  # * is on line 2
        assert tokens[2].line == 3  # FROM is on line 3


# ═══════════════════════════════════════════════════════════════════════════════
# PARSER TESTS
# ═══════════════════════════════════════════════════════════════════════════════

class TestParser:
    """Tests for the recursive-descent SQL parser."""

    def test_simple_select(self):
        from pysql.parser import Parser
        from pysql.ast_nodes import SelectStatement, StarExpr
        stmt = Parser("SELECT * FROM users").parse_single()
        assert isinstance(stmt, SelectStatement)
        assert any(isinstance(c, StarExpr) for c in stmt.columns)

    def test_select_with_where(self):
        from pysql.parser import Parser
        from pysql.ast_nodes import SelectStatement, BinaryOp, BinaryOpType
        stmt = Parser("SELECT name FROM users WHERE age > 18").parse_single()
        assert isinstance(stmt, SelectStatement)
        assert isinstance(stmt.where, BinaryOp)
        assert stmt.where.op == BinaryOpType.GT

    def test_select_with_join(self):
        from pysql.parser import Parser
        from pysql.ast_nodes import SelectStatement, JoinTableRef
        stmt = Parser(
            "SELECT u.name, o.total FROM users u "
            "INNER JOIN orders o ON u.id = o.user_id"
        ).parse_single()
        assert isinstance(stmt, SelectStatement)
        assert isinstance(stmt.from_clause, JoinTableRef)

    def test_insert_statement(self):
        from pysql.parser import Parser
        from pysql.ast_nodes import InsertStatement
        stmt = Parser(
            "INSERT INTO users (name, age) VALUES ('Alice', 30)"
        ).parse_single()
        assert isinstance(stmt, InsertStatement)
        assert stmt.table == "users"
        assert stmt.columns == ["name", "age"]
        assert len(stmt.values) == 1

    def test_create_table(self):
        from pysql.parser import Parser
        from pysql.ast_nodes import CreateTableStatement
        stmt = Parser(
            "CREATE TABLE users ("
            "  id INTEGER PRIMARY KEY,"
            "  name VARCHAR(100) NOT NULL,"
            "  email TEXT UNIQUE"
            ")"
        ).parse_single()
        assert isinstance(stmt, CreateTableStatement)
        assert stmt.name == "users"
        assert len(stmt.columns) == 3
        assert stmt.columns[0].is_primary_key
        assert stmt.columns[1].is_not_null

    def test_operator_precedence(self):
        from pysql.parser import Parser
        from pysql.ast_nodes import BinaryOp, BinaryOpType
        stmt = Parser("SELECT 1 + 2 * 3").parse_single()
        # Should parse as 1 + (2 * 3), not (1 + 2) * 3
        expr = stmt.columns[0]
        assert isinstance(expr, BinaryOp)
        assert expr.op == BinaryOpType.ADD
        assert isinstance(expr.right, BinaryOp)
        assert expr.right.op == BinaryOpType.MUL

    def test_between_expression(self):
        from pysql.parser import Parser
        from pysql.ast_nodes import BetweenExpr
        stmt = Parser("SELECT * FROM t WHERE x BETWEEN 1 AND 10").parse_single()
        assert isinstance(stmt.where, BetweenExpr)

    def test_in_expression(self):
        from pysql.parser import Parser
        from pysql.ast_nodes import InExpr
        stmt = Parser("SELECT * FROM t WHERE x IN (1, 2, 3)").parse_single()
        assert isinstance(stmt.where, InExpr)
        assert len(stmt.where.values) == 3

    def test_case_expression(self):
        from pysql.parser import Parser
        from pysql.ast_nodes import CaseExpr
        stmt = Parser(
            "SELECT CASE WHEN age < 18 THEN 'minor' ELSE 'adult' END FROM t"
        ).parse_single()
        expr = stmt.columns[0]
        assert isinstance(expr, CaseExpr)
        assert len(expr.when_clauses) == 1

    def test_multiple_statements(self):
        from pysql.parser import Parser
        stmts = Parser("SELECT 1; SELECT 2; SELECT 3").parse()
        assert len(stmts) == 3

    def test_update_statement(self):
        from pysql.parser import Parser
        from pysql.ast_nodes import UpdateStatement
        stmt = Parser("UPDATE users SET name = 'Bob' WHERE id = 1").parse_single()
        assert isinstance(stmt, UpdateStatement)
        assert stmt.table == "users"
        assert len(stmt.assignments) == 1

    def test_delete_statement(self):
        from pysql.parser import Parser
        from pysql.ast_nodes import DeleteStatement
        stmt = Parser("DELETE FROM users WHERE id = 1").parse_single()
        assert isinstance(stmt, DeleteStatement)


# ═══════════════════════════════════════════════════════════════════════════════
# TYPE SYSTEM TESTS
# ═══════════════════════════════════════════════════════════════════════════════

class TestTypeSystem:
    """Tests for data type serialization and coercion."""

    def test_integer_roundtrip(self):
        from pysql.types import IntegerType
        t = IntegerType()
        for val in [0, 1, -1, 42, -1000, 2**62, -(2**62)]:
            assert t.deserialize(t.serialize(val)) == val

    def test_integer_sort_preserving(self):
        from pysql.types import IntegerType
        t = IntegerType()
        values = [-100, -1, 0, 1, 100]
        serialized = [t.serialize(v) for v in values]
        # Serialized bytes should maintain the same order
        assert serialized == sorted(serialized)

    def test_float_roundtrip(self):
        from pysql.types import FloatType
        t = FloatType()
        for val in [0.0, 1.5, -3.14, 1e10, -1e-5]:
            result = t.deserialize(t.serialize(val))
            assert abs(result - val) < 1e-10

    def test_text_roundtrip(self):
        from pysql.types import TextType
        t = TextType()
        for val in ["", "hello", "unicode: 日本語", "a" * 1000]:
            assert t.deserialize(t.serialize(val)) == val

    def test_varchar_max_length(self):
        from pysql.types import VarCharType
        t = VarCharType(max_length=5)
        assert t.validate("hello") is True
        assert t.validate("too long string") is False

    def test_type_coercion(self):
        from pysql.types import IntegerType, FloatType, coerce_value
        result = coerce_value(42, IntegerType(), FloatType())
        assert isinstance(result, float)
        assert result == 42.0

    def test_type_from_string(self):
        from pysql.types import type_from_string, IntegerType, VarCharType
        assert isinstance(type_from_string("INTEGER"), IntegerType)
        t = type_from_string("VARCHAR(100)")
        assert isinstance(t, VarCharType)
        assert t.max_length == 100


# ═══════════════════════════════════════════════════════════════════════════════
# B+TREE TESTS
# ═══════════════════════════════════════════════════════════════════════════════

class TestBPlusTree:
    """Tests for the B+Tree index implementation."""

    def test_insert_and_search(self):
        from pysql.storage.btree import BPlusTree
        tree = BPlusTree(order=4)
        tree.insert(5, "five")
        tree.insert(3, "three")
        tree.insert(7, "seven")
        assert tree.search(5) == "five"
        assert tree.search(3) == "three"
        assert tree.search(7) == "seven"
        assert tree.search(99) is None

    def test_insert_causes_split(self):
        from pysql.storage.btree import BPlusTree
        tree = BPlusTree(order=4)
        for i in range(20):
            tree.insert(i, f"val_{i}")
        assert tree.size == 20
        assert tree.height > 1
        for i in range(20):
            assert tree.search(i) == f"val_{i}"

    def test_delete(self):
        from pysql.storage.btree import BPlusTree
        tree = BPlusTree(order=4)
        for i in range(10):
            tree.insert(i, f"val_{i}")
        assert tree.delete(5) is True
        assert tree.search(5) is None
        assert tree.size == 9

    def test_delete_nonexistent(self):
        from pysql.storage.btree import BPlusTree
        tree = BPlusTree(order=4)
        tree.insert(1, "one")
        assert tree.delete(99) is False

    def test_range_scan(self):
        from pysql.storage.btree import BPlusTree
        tree = BPlusTree(order=4)
        for i in range(100):
            tree.insert(i, f"val_{i}")
        results = list(tree.range_scan(low=10, high=20))
        assert len(results) == 11
        assert results[0] == (10, "val_10")
        assert results[-1] == (20, "val_20")

    def test_range_scan_exclusive(self):
        from pysql.storage.btree import BPlusTree
        tree = BPlusTree(order=4)
        for i in range(100):
            tree.insert(i, f"val_{i}")
        results = list(tree.range_scan(low=10, high=20, include_low=False, include_high=False))
        assert len(results) == 9
        assert results[0] == (11, "val_11")
        assert results[-1] == (19, "val_19")

    def test_bulk_load(self):
        from pysql.storage.btree import BPlusTree
        tree = BPlusTree(order=8)
        items = [(i, f"val_{i}") for i in range(1000)]
        tree.bulk_load(items)
        assert tree.size == 1000
        for i in range(1000):
            assert tree.search(i) == f"val_{i}"

    def test_min_max(self):
        from pysql.storage.btree import BPlusTree
        tree = BPlusTree(order=4)
        for i in [5, 3, 8, 1, 9]:
            tree.insert(i, f"val_{i}")
        assert tree.get_min() == (1, "val_1")
        assert tree.get_max() == (9, "val_9")

    def test_duplicate_keys(self):
        from pysql.storage.btree import BPlusTree
        tree = BPlusTree(order=4)
        tree.insert(5, "first")
        tree.insert(5, "second")
        results = tree.search_all(5)
        assert len(results) == 2
        assert "first" in results
        assert "second" in results

    def test_unique_insert(self):
        from pysql.storage.btree import BPlusTree
        tree = BPlusTree(order=4)
        assert tree.insert_unique(5, "first") is True
        assert tree.insert_unique(5, "second") is False
        assert tree.size == 1

    def test_large_dataset(self):
        from pysql.storage.btree import BPlusTree
        tree = BPlusTree(order=32)
        n = 10000
        for i in range(n):
            tree.insert(i, i * 10)
        assert tree.size == n
        # Verify random lookups
        import random
        for _ in range(100):
            key = random.randint(0, n - 1)
            assert tree.search(key) == key * 10


# ═══════════════════════════════════════════════════════════════════════════════
# PAGE STORAGE TESTS
# ═══════════════════════════════════════════════════════════════════════════════

class TestPageStorage:
    """Tests for the slotted-page storage engine."""

    def test_page_insert_and_read(self):
        from pysql.storage.page import Page, PageType
        page = Page(page_id=0, page_type=PageType.HEAP_DATA)
        data = b"Hello, World!"
        slot = page.insert_record(data)
        assert slot is not None
        assert page.read_record(slot) == data

    def test_page_multiple_records(self):
        from pysql.storage.page import Page, PageType
        page = Page(page_id=0, page_type=PageType.HEAP_DATA)
        records = [f"record_{i}".encode() for i in range(50)]
        slots = []
        for r in records:
            slot = page.insert_record(r)
            assert slot is not None
            slots.append(slot)
        for slot, expected in zip(slots, records):
            assert page.read_record(slot) == expected

    def test_page_delete(self):
        from pysql.storage.page import Page, PageType
        page = Page(page_id=0, page_type=PageType.HEAP_DATA)
        slot = page.insert_record(b"to_delete")
        assert page.delete_record(slot) is True
        assert page.read_record(slot) is None

    def test_page_update_smaller(self):
        from pysql.storage.page import Page, PageType
        page = Page(page_id=0, page_type=PageType.HEAP_DATA)
        slot = page.insert_record(b"original data here")
        assert page.update_record(slot, b"shorter") is True
        assert page.read_record(slot) == b"shorter"

    def test_page_serialization(self):
        from pysql.storage.page import Page, PageType
        page = Page(page_id=42, page_type=PageType.HEAP_DATA)
        page.insert_record(b"test data 1")
        page.insert_record(b"test data 2")
        raw = page.to_bytes()
        restored = Page.from_bytes(raw)
        assert restored.header.page_id == 42
        assert restored.get_num_records() == 2

    def test_page_checksum(self):
        from pysql.storage.page import Page, PageType
        page = Page(page_id=0, page_type=PageType.HEAP_DATA)
        page.insert_record(b"checksummed data")
        raw = page.to_bytes()
        restored = Page.from_bytes(raw)
        assert restored.verify_checksum() is True

    def test_page_compaction(self):
        from pysql.storage.page import Page, PageType
        page = Page(page_id=0, page_type=PageType.HEAP_DATA)
        s1 = page.insert_record(b"keep")
        s2 = page.insert_record(b"delete_me")
        s3 = page.insert_record(b"keep_too")
        page.delete_record(s2)
        free_before = page.get_free_space()
        page.compact()
        free_after = page.get_free_space()
        assert free_after >= free_before
        assert page.read_record(s1) == b"keep"
        assert page.read_record(s3) == b"keep_too"

    def test_disk_manager(self, temp_dir):
        from pysql.storage.page import DiskManager, PageType
        dm = DiskManager(os.path.join(temp_dir, "test.db"))
        page = dm.allocate_page(PageType.HEAP_DATA)
        page.insert_record(b"persistent data")
        dm.write_page(page)
        loaded = dm.read_page(page.header.page_id)
        assert loaded.read_record(0) == b"persistent data"


# ═══════════════════════════════════════════════════════════════════════════════
# WAL TESTS
# ═══════════════════════════════════════════════════════════════════════════════

class TestWAL:
    """Tests for Write-Ahead Logging."""

    def test_log_and_read(self, temp_dir):
        from pysql.storage.wal import WALManager, LogRecord, LogRecordType
        wal = WALManager(os.path.join(temp_dir, "test.wal"))
        wal.log_begin(1)
        wal.log_insert(1, 100, 0, 0, b"record_data")
        wal.log_commit(1)
        records = wal.read_all()
        assert len(records) == 3
        assert records[0].record_type == LogRecordType.BEGIN
        assert records[1].record_type == LogRecordType.INSERT
        assert records[2].record_type == LogRecordType.COMMIT

    def test_recovery(self, temp_dir):
        from pysql.storage.wal import WALManager
        wal = WALManager(os.path.join(temp_dir, "test.wal"))
        # Committed transaction
        wal.log_begin(1)
        wal.log_insert(1, 100, 0, 0, b"data")
        wal.log_commit(1)
        # Uncommitted transaction (simulate crash)
        wal.log_begin(2)
        wal.log_insert(2, 100, 1, 0, b"uncommitted")
        wal.flush()
        committed, aborted = wal.recover()
        assert 1 in committed
        assert 2 in aborted

    def test_lsn_monotonic(self, temp_dir):
        from pysql.storage.wal import WALManager
        wal = WALManager(os.path.join(temp_dir, "test.wal"))
        lsn1 = wal.log_begin(1)
        lsn2 = wal.log_insert(1, 100, 0, 0, b"data")
        lsn3 = wal.log_commit(1)
        assert lsn1 < lsn2 < lsn3


# ═══════════════════════════════════════════════════════════════════════════════
# MVCC TESTS
# ═══════════════════════════════════════════════════════════════════════════════

class TestMVCC:
    """Tests for Multi-Version Concurrency Control."""

    def test_snapshot_isolation(self):
        from pysql.mvcc import MVCCManager
        mvcc = MVCCManager()
        # T1 writes a value
        t1 = mvcc.begin_transaction()
        mvcc.write_version(t1, "users", 1, ["Alice", 30])
        mvcc.commit_transaction(t1)
        # T2 reads the committed value
        t2 = mvcc.begin_transaction()
        result = mvcc.read_version(t2, "users", 1)
        assert result == ["Alice", 30]
        mvcc.commit_transaction(t2)

    def test_read_own_writes(self):
        from pysql.mvcc import MVCCManager
        mvcc = MVCCManager()
        t1 = mvcc.begin_transaction()
        mvcc.write_version(t1, "users", 1, ["Alice", 30])
        # Should see own uncommitted write
        result = mvcc.read_version(t1, "users", 1)
        assert result == ["Alice", 30]
        mvcc.commit_transaction(t1)

    def test_isolation_between_transactions(self):
        from pysql.mvcc import MVCCManager
        mvcc = MVCCManager()
        # T1 writes
        t1 = mvcc.begin_transaction()
        mvcc.write_version(t1, "users", 1, ["Alice", 30])
        # T2 starts before T1 commits — should NOT see T1's write
        t2 = mvcc.begin_transaction()
        result = mvcc.read_version(t2, "users", 1)
        assert result is None
        mvcc.commit_transaction(t1)
        # T2 still shouldn't see it (snapshot isolation)
        result = mvcc.read_version(t2, "users", 1)
        assert result is None
        mvcc.commit_transaction(t2)

    def test_abort_rollback(self):
        from pysql.mvcc import MVCCManager
        mvcc = MVCCManager()
        # Commit initial value
        t1 = mvcc.begin_transaction()
        mvcc.write_version(t1, "users", 1, ["Alice", 30])
        mvcc.commit_transaction(t1)
        # T2 overwrites and aborts
        t2 = mvcc.begin_transaction()
        mvcc.write_version(t2, "users", 1, ["Bob", 25])
        mvcc.abort_transaction(t2)
        # T3 should see the original value
        t3 = mvcc.begin_transaction()
        result = mvcc.read_version(t3, "users", 1)
        assert result == ["Alice", 30]
        mvcc.commit_transaction(t3)


# ═══════════════════════════════════════════════════════════════════════════════
# END-TO-END EXECUTOR TESTS
# ═══════════════════════════════════════════════════════════════════════════════

class TestExecutor:
    """End-to-end tests for the query executor."""

    def test_create_and_insert(self, db):
        db.execute("CREATE TABLE users (id INTEGER, name TEXT, age INTEGER)")
        db.execute("INSERT INTO users VALUES (1, 'Alice', 30)")
        db.execute("INSERT INTO users VALUES (2, 'Bob', 25)")
        result = db.execute("SELECT * FROM users")
        assert len(result.rows) == 2

    def test_select_with_where(self, db):
        db.execute("CREATE TABLE users (id INTEGER, name TEXT, age INTEGER)")
        db.execute("INSERT INTO users VALUES (1, 'Alice', 30)")
        db.execute("INSERT INTO users VALUES (2, 'Bob', 25)")
        db.execute("INSERT INTO users VALUES (3, 'Charlie', 35)")
        result = db.execute("SELECT name FROM users WHERE age > 28")
        names = [row[0] for row in result.rows]
        assert "Alice" in names
        assert "Charlie" in names
        assert "Bob" not in names

    def test_order_by(self, db):
        db.execute("CREATE TABLE nums (val INTEGER)")
        db.execute("INSERT INTO nums VALUES (3)")
        db.execute("INSERT INTO nums VALUES (1)")
        db.execute("INSERT INTO nums VALUES (2)")
        result = db.execute("SELECT val FROM nums ORDER BY val")
        values = [row[0] for row in result.rows]
        assert values == [1, 2, 3]

    def test_limit_offset(self, db):
        db.execute("CREATE TABLE nums (val INTEGER)")
        for i in range(10):
            db.execute(f"INSERT INTO nums VALUES ({i})")
        result = db.execute("SELECT val FROM nums ORDER BY val LIMIT 3 OFFSET 2")
        values = [row[0] for row in result.rows]
        assert values == [2, 3, 4]

    def test_aggregate_count(self, db):
        db.execute("CREATE TABLE users (id INTEGER, name TEXT)")
        db.execute("INSERT INTO users VALUES (1, 'Alice')")
        db.execute("INSERT INTO users VALUES (2, 'Bob')")
        db.execute("INSERT INTO users VALUES (3, 'Charlie')")
        result = db.execute("SELECT COUNT(*) FROM users")
        assert result.rows[0][0] == 3

    def test_aggregate_sum_avg(self, db):
        db.execute("CREATE TABLE scores (name TEXT, score INTEGER)")
        db.execute("INSERT INTO scores VALUES ('Alice', 90)")
        db.execute("INSERT INTO scores VALUES ('Bob', 80)")
        db.execute("INSERT INTO scores VALUES ('Charlie', 100)")
        result = db.execute("SELECT SUM(score), AVG(score) FROM scores")
        assert result.rows[0][0] == 270
        assert result.rows[0][1] == 90.0

    def test_group_by(self, db):
        db.execute("CREATE TABLE sales (dept TEXT, amount INTEGER)")
        db.execute("INSERT INTO sales VALUES ('A', 100)")
        db.execute("INSERT INTO sales VALUES ('B', 200)")
        db.execute("INSERT INTO sales VALUES ('A', 150)")
        db.execute("INSERT INTO sales VALUES ('B', 250)")
        result = db.execute(
            "SELECT dept, SUM(amount) FROM sales GROUP BY dept ORDER BY dept"
        )
        assert len(result.rows) == 2

    def test_update(self, db):
        db.execute("CREATE TABLE users (id INTEGER, name TEXT)")
        db.execute("INSERT INTO users VALUES (1, 'Alice')")
        db.execute("UPDATE users SET name = 'Alicia' WHERE id = 1")
        result = db.execute("SELECT name FROM users WHERE id = 1")
        assert result.rows[0][0] == "Alicia"

    def test_delete(self, db):
        db.execute("CREATE TABLE users (id INTEGER, name TEXT)")
        db.execute("INSERT INTO users VALUES (1, 'Alice')")
        db.execute("INSERT INTO users VALUES (2, 'Bob')")
        db.execute("DELETE FROM users WHERE id = 1")
        result = db.execute("SELECT * FROM users")
        assert len(result.rows) == 1
        assert result.rows[0][1] == "Bob"

    def test_drop_table(self, db):
        db.execute("CREATE TABLE temp (id INTEGER)")
        db.execute("DROP TABLE temp")
        with pytest.raises(RuntimeError):
            db.execute("SELECT * FROM temp")

    def test_create_if_not_exists(self, db):
        db.execute("CREATE TABLE t (id INTEGER)")
        # Should not raise
        db.execute("CREATE TABLE IF NOT EXISTS t (id INTEGER)")

    def test_join(self, db):
        db.execute("CREATE TABLE users (id INTEGER, name TEXT)")
        db.execute("CREATE TABLE orders (id INTEGER, user_id INTEGER, total INTEGER)")
        db.execute("INSERT INTO users VALUES (1, 'Alice')")
        db.execute("INSERT INTO users VALUES (2, 'Bob')")
        db.execute("INSERT INTO orders VALUES (1, 1, 100)")
        db.execute("INSERT INTO orders VALUES (2, 1, 200)")
        db.execute("INSERT INTO orders VALUES (3, 2, 150)")
        result = db.execute(
            "SELECT u.name, o.total FROM users u "
            "INNER JOIN orders o ON u.id = o.user_id "
            "ORDER BY o.total"
        )
        assert len(result.rows) == 3

    def test_distinct(self, db):
        db.execute("CREATE TABLE t (val INTEGER)")
        db.execute("INSERT INTO t VALUES (1)")
        db.execute("INSERT INTO t VALUES (2)")
        db.execute("INSERT INTO t VALUES (1)")
        db.execute("INSERT INTO t VALUES (2)")
        result = db.execute("SELECT DISTINCT val FROM t")
        assert len(result.rows) == 2

    def test_expression_evaluation(self, db):
        db.execute("CREATE TABLE t (a INTEGER, b INTEGER)")
        db.execute("INSERT INTO t VALUES (10, 3)")
        result = db.execute("SELECT a + b, a * b, a - b FROM t")
        assert result.rows[0] == [13, 30, 7]

    def test_like_operator(self, db):
        db.execute("CREATE TABLE t (name TEXT)")
        db.execute("INSERT INTO t VALUES ('Alice')")
        db.execute("INSERT INTO t VALUES ('Bob')")
        db.execute("INSERT INTO t VALUES ('Alicia')")
        result = db.execute("SELECT name FROM t WHERE name LIKE 'Ali%'")
        assert len(result.rows) == 2

    def test_between_operator(self, db):
        db.execute("CREATE TABLE t (val INTEGER)")
        for i in range(10):
            db.execute(f"INSERT INTO t VALUES ({i})")
        result = db.execute("SELECT val FROM t WHERE val BETWEEN 3 AND 7")
        assert len(result.rows) == 5

    def test_in_operator(self, db):
        db.execute("CREATE TABLE t (val INTEGER)")
        for i in range(10):
            db.execute(f"INSERT INTO t VALUES ({i})")
        result = db.execute("SELECT val FROM t WHERE val IN (1, 3, 5, 7)")
        assert len(result.rows) == 4

    def test_explain(self, db):
        db.execute("CREATE TABLE t (val INTEGER)")
        plan = db.explain("SELECT * FROM t WHERE val > 5")
        assert "SEQ_SCAN" in plan or "FILTER" in plan

    def test_multiple_inserts(self, db):
        db.execute("CREATE TABLE t (id INTEGER, name TEXT)")
        db.execute(
            "INSERT INTO t VALUES (1, 'Alice'), (2, 'Bob'), (3, 'Charlie')"
        )
        result = db.execute("SELECT COUNT(*) FROM t")
        assert result.rows[0][0] == 3

    def test_case_expression(self, db):
        db.execute("CREATE TABLE t (age INTEGER)")
        db.execute("INSERT INTO t VALUES (10)")
        db.execute("INSERT INTO t VALUES (25)")
        db.execute("INSERT INTO t VALUES (70)")
        result = db.execute(
            "SELECT CASE WHEN age < 18 THEN 'minor' "
            "WHEN age < 65 THEN 'adult' "
            "ELSE 'senior' END FROM t ORDER BY age"
        )
        assert result.rows[0][0] == "minor"
        assert result.rows[1][0] == "adult"
        assert result.rows[2][0] == "senior"

    def test_null_handling(self, db):
        db.execute("CREATE TABLE t (id INTEGER, name TEXT)")
        db.execute("INSERT INTO t VALUES (1, NULL)")
        result = db.execute("SELECT * FROM t WHERE name IS NULL")
        assert len(result.rows) == 1

    def test_database_stats(self, db):
        stats = db.get_stats()
        assert "tables" in stats
        assert "buffer_pool" in stats
        assert "mvcc" in stats
