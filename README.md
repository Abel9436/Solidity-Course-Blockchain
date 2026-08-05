# PySQLEngine — A SQL Database Engine Built from Scratch

<p align="center">
  <strong>A fully functional relational database engine implemented in pure Python.</strong><br>
  <em>No external database dependencies — every component built from first principles.</em>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.10+-blue.svg" alt="Python 3.10+">
  <img src="https://img.shields.io/badge/License-MIT-green.svg" alt="MIT License">
  <img src="https://img.shields.io/badge/Tests-60+-brightgreen.svg" alt="Tests">
  <img src="https://img.shields.io/badge/Zero_Dependencies-pure_python-orange.svg" alt="Pure Python">
</p>

---

## Why This Exists

This project is an educational deep-dive into database internals. Most developers use databases daily but never understand what happens beneath the SQL interface. PySQLEngine implements every layer — from the SQL parser down to the page-level disk I/O — to demystify how real databases like PostgreSQL, MySQL, and SQLite work under the hood.

## Architecture

```
                    ┌─────────────────────────────┐
                    │         SQL String           │
                    └──────────────┬──────────────┘
                                   │
                    ┌──────────────▼──────────────┐
                    │    Lexer (Tokenizer)         │
                    │    State-machine tokenizer   │
                    └──────────────┬──────────────┘
                                   │
                    ┌──────────────▼──────────────┐
                    │    Parser (SQL → AST)        │
                    │    Recursive-descent parser  │
                    └──────────────┬──────────────┘
                                   │
                    ┌──────────────▼──────────────┐
                    │    Query Planner/Optimizer   │
                    │    Cost-based optimization   │
                    │    • Predicate pushdown      │
                    │    • Index selection          │
                    │    • Join ordering            │
                    └──────────────┬──────────────┘
                                   │
                    ┌──────────────▼──────────────┐
                    │    Executor (Volcano Model)  │
                    │    Iterator-based execution  │
                    └───┬──────────┬──────────┬───┘
                        │          │          │
            ┌───────────▼┐   ┌────▼────┐  ┌──▼──────────┐
            │ Heap Files  │   │ B+Tree  │  │    MVCC     │
            │ (Tables)    │   │ Indexes │  │ (Snapshot   │
            └──────┬──────┘   └────┬────┘  │  Isolation) │
                   │               │       └──────┬──────┘
            ┌──────▼───────────────▼──────────────▼──────┐
            │           Buffer Pool Manager               │
            │           (Clock-sweep LRU)                  │
            └──────────────────┬──────────────────────────┘
                               │
            ┌──────────────────▼──────────────────────────┐
            │    Page-Based Storage    │    WAL (Crash     │
            │    (Slotted Pages)       │    Recovery)      │
            └──────────────────────────┴──────────────────┘
                               │
                    ┌──────────▼──────────┐
                    │    Disk (Files)      │
                    └─────────────────────┘
```

## Features

### SQL Parser
- **Hand-written recursive-descent parser** with correct operator precedence
- Supports: `SELECT`, `INSERT`, `UPDATE`, `DELETE`, `CREATE TABLE`, `DROP TABLE`, `CREATE INDEX`
- **JOINs**: `INNER`, `LEFT`, `RIGHT`, `FULL`, `CROSS`, `NATURAL`
- **Expressions**: arithmetic, comparison, `BETWEEN`, `IN`, `LIKE`, `IS NULL`, `CASE/WHEN`, `CAST`
- **Aggregates**: `COUNT`, `SUM`, `AVG`, `MIN`, `MAX` with `GROUP BY` and `HAVING`
- **Subqueries** in `WHERE`, `FROM`, and `SELECT` clauses
- Multi-statement execution with semicolons

### Storage Engine
- **4KB slotted pages** with CRC32 checksums
- **Buffer pool** with clock-sweep LRU eviction (configurable size)
- **Heap files** with null bitmap record format
- Record compaction for space reclamation

### B+Tree Indexes
- **O(log n)** point lookups and insertions
- **O(log n + k)** range scans via leaf-level linked list
- Node splitting and merging with redistribution
- Bulk loading for O(n) initial construction
- Support for unique and non-unique indexes

### Transaction Management (MVCC)
- **Snapshot isolation** — readers never block writers
- Version chains with visibility checking
- First-updater-wins conflict detection
- Transaction rollback with version chain unwinding
- Garbage collection of old versions

### Write-Ahead Logging (WAL)
- **ARIES-inspired** recovery protocol
- Log record serialization with backward scanning support
- Crash recovery: analysis → redo → undo phases
- Checkpoint support for bounded recovery time

### Query Optimizer
- **Cost-based optimization** with cardinality estimation
- **Predicate pushdown** (move filters closer to scans)
- **Index selection** (automatically use indexes when beneficial)
- **Join algorithm selection** (nested-loop vs hash join)
- **EXPLAIN** command for plan visualization

## Quick Start

### Installation

```bash
git clone [https://github.com/Abel9436/Solidity-Course-Blockchain.git](https://github.com/Abel9436/PySQLEngine.git)
cd PySQLEngine
pip install -e ".[dev]"
```

### Interactive REPL

```bash
pysql my_database
```

```sql
pysql> CREATE TABLE users (
   ...>   id INTEGER PRIMARY KEY,
   ...>   name TEXT NOT NULL,
   ...>   email TEXT UNIQUE,
   ...>   age INTEGER
   ...> );
Table 'users' created successfully
Time: 1.23ms

pysql> INSERT INTO users VALUES
   ...>   (1, 'Alice', 'alice@example.com', 30),
   ...>   (2, 'Bob', 'bob@example.com', 25),
   ...>   (3, 'Charlie', 'charlie@example.com', 35);
3 row(s) inserted
Time: 0.89ms

pysql> SELECT name, age FROM users WHERE age > 28 ORDER BY age DESC;
name    | age
--------+----
Charlie | 35
Alice   | 30
(2 rows)
Time: 0.45ms

pysql> .explain SELECT * FROM users WHERE age > 28
Query Plan:
  → PROJECT
    → FILTER  (filter: age > 28)
      → SEQ_SCAN on users  [est. 3 rows, cost: 3.0]
```

### Python API

```python
from pysql.engine import Database

# Create/open a database
with Database("my_database") as db:
    # Create tables
    db.execute("""
        CREATE TABLE products (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            price INTEGER,
            category TEXT
        )
    """)

    # Insert data
    db.execute("INSERT INTO products VALUES (1, 'Laptop', 999, 'Electronics')")
    db.execute("INSERT INTO products VALUES (2, 'Book', 15, 'Education')")
    db.execute("INSERT INTO products VALUES (3, 'Phone', 699, 'Electronics')")

    # Query with aggregation
    result = db.execute("""
        SELECT category, COUNT(*), AVG(price)
        FROM products
        GROUP BY category
        ORDER BY category
    """)
    print(result.to_table_string())

    # Show execution plan
    print(db.explain("SELECT * FROM products WHERE price > 100"))
```

### REPL Commands

| Command | Description |
|:--------|:------------|
| `.help` | Show available commands |
| `.tables` | List all tables |
| `.schema <table>` | Show table schema |
| `.stats` | Show database statistics |
| `.explain <SQL>` | Show query execution plan |
| `.indexes <table>` | Show indexes for a table |
| `.quit` | Exit the REPL |

## Running Tests

```bash
# Install dev dependencies
pip install -e ".[dev]"

# Run all tests
pytest -v

# Run with coverage
pytest --cov=pysql --cov-report=html

# Run specific test class
pytest tests/test_pysql.py::TestBPlusTree -v
pytest tests/test_pysql.py::TestExecutor -v
```

## Project Structure

```
pysql-engine/
├── pysql/
│   ├── __init__.py          # Package metadata
│   ├── lexer.py             # SQL tokenizer (state-machine)
│   ├── ast_nodes.py         # AST node definitions (30+ types)
│   ├── parser.py            # Recursive-descent SQL parser
│   ├── types.py             # Data type system with sort-preserving serialization
│   ├── catalog.py           # Schema metadata management
│   ├── planner.py           # Query planner with cost-based optimizer
│   ├── executor.py          # Volcano-model query executor
│   ├── mvcc.py              # Multi-Version Concurrency Control
│   ├── engine.py            # Main database engine (ties everything together)
│   ├── cli.py               # Interactive REPL
│   └── storage/
│       ├── __init__.py
│       ├── page.py          # Slotted-page storage with CRC32
│       ├── buffer_pool.py   # LRU buffer pool (clock-sweep)
│       ├── btree.py         # B+Tree index implementation
│       ├── heap.py          # Heap file (table storage)
│       └── wal.py           # Write-Ahead Logging (crash recovery)
├── tests/
│   └── test_pysql.py        # Comprehensive test suite (60+ tests)
├── pyproject.toml            # Project configuration
├── LICENSE                   # MIT License
└── README.md                 # This file
```

## Technical Deep Dives

### How the B+Tree Works

The B+Tree is the most critical data structure in any database. Our implementation:

1. **Leaf nodes** store key-value pairs and form a doubly-linked list
2. **Internal nodes** store separator keys that route searches
3. **Splitting**: When a node overflows, it splits in half and promotes a key
4. **Merging**: When a node underflows, it borrows from siblings or merges
5. **Bulk loading**: Builds the tree bottom-up in O(n) time

### How MVCC Achieves Snapshot Isolation

Instead of locking rows during reads:

1. Each transaction gets a **snapshot** of the database at its start time
2. Each record has a **version chain** linking current and historical versions
3. A version is **visible** if its creator committed before our snapshot
4. **Write-write conflicts** are detected using first-updater-wins
5. Old versions are **garbage collected** when no transaction can see them

### How WAL Ensures Durability

The Write-Ahead Log guarantees that committed transactions survive crashes:

1. **Before** modifying any data page, write a log record to the WAL
2. The log record contains both **undo** (old value) and **redo** (new value)
3. On **commit**, force-flush the WAL to disk before acknowledging
4. On **crash recovery**, replay committed changes and undo uncommitted ones

## Performance Characteristics

| Operation | Complexity | Notes |
|:----------|:-----------|:------|
| Point lookup (index) | O(log n) | B+Tree traversal |
| Range scan (index) | O(log n + k) | k = result count |
| Full table scan | O(n) | Heap file sequential scan |
| Insert | O(log n) | With index maintenance |
| Buffer pool lookup | O(1) | Hash table + clock sweep |
| WAL append | O(1) | Sequential append |

## Limitations

This is an educational project. Notable simplifications vs. production databases:

- Single-threaded execution (no parallel query processing)
- In-memory B+Tree indexes (not yet persisted to pages)
- Simplified WAL recovery (full ARIES not implemented)
- No query caching or prepared statements
- No network protocol (embedded only)

## License

MIT License — see [LICENSE](LICENSE) for details.

## Author

**Abel Bekele** — [GitHub](https://github.com/Abel9436/) · [LinkedIn](https://www.linkedin.com/in/abelabekele/)

---

<p align="center">
  <em>Built from scratch to understand how databases really work.</em>
</p>
