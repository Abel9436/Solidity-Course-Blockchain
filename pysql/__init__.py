"""
PySQLEngine — A fully functional SQL database engine built from scratch in pure Python.

This engine implements:
- SQL Lexer & Recursive-Descent Parser → AST
- Page-based Storage Engine with Buffer Pool Manager
- B+Tree Indexes for O(log n) lookups
- Write-Ahead Logging (WAL) for crash recovery
- Multi-Version Concurrency Control (MVCC) for snapshot isolation
- Cost-Based Query Optimizer
- Volcano-model Query Executor

No external database dependencies — everything is built from first principles.
"""

__version__ = "0.1.0"
__author__ = "Abel Bekele"
