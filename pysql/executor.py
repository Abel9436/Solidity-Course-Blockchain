"""
Volcano-Model Query Executor.

The executor takes a physical query plan from the planner and executes it,
producing result rows. It uses the Volcano (iterator) model where each
plan node implements an open/next/close interface:

    open()  → Initialize the operator
    next()  → Return the next result row (or None if exhausted)
    close() → Release resources

This allows pipelining: rows flow through the plan tree one at a time
(or in small batches), minimizing memory usage for large result sets.

The executor is the bridge between the relational algebra (plan nodes)
and the physical storage (heap files, indexes, buffer pool).
"""

from __future__ import annotations

import fnmatch
import re
from typing import Any, Optional

from .ast_nodes import (
    BetweenExpr,
    BinaryOp,
    BinaryOpType,
    CaseExpr,
    CastExpr,
    ColumnDef,
    ColumnRef,
    Constraint,
    ConstraintType,
    CreateIndexStatement,
    CreateTableStatement,
    DeleteStatement,
    DropTableStatement,
    ExistsExpr,
    Expression,
    FunctionCall,
    InExpr,
    InsertStatement,
    IsNullExpr,
    LikeExpr,
    Literal,
    StarExpr,
    SubqueryExpr,
    UnaryOp,
    UnaryOpType,
    UpdateStatement,
)
from .catalog import Catalog, IndexInfo, TableInfo
from .mvcc import MVCCManager, Transaction
from .planner import PlanNode, PlanNodeType
from .storage.btree import BPlusTree
from .storage.buffer_pool import BufferPool
from .storage.heap import HeapFile, RecordId, TableSchema
from .storage.page import DiskManager
from .storage.wal import WALManager
from .types import NULL, NullValue, type_from_string


@dataclass
class QueryResult:
    """Result of a query execution.

    Contains column headers and rows for SELECT queries,
    or affected row counts for DML statements.
    """
    columns: list[str]
    rows: list[list[Any]]
    affected_rows: int = 0
    message: str = ""

    def __repr__(self) -> str:
        if self.rows:
            return f"QueryResult({len(self.rows)} rows, columns={self.columns})"
        return f"QueryResult(affected={self.affected_rows}, msg={self.message})"

    def to_table_string(self, max_width: int = 30) -> str:
        """Format result as a ASCII table string (like psql output)."""
        if not self.columns:
            return self.message or f"{self.affected_rows} row(s) affected"

        # Calculate column widths
        widths = [len(str(c)) for c in self.columns]
        for row in self.rows:
            for i, val in enumerate(row):
                if i < len(widths):
                    widths[i] = max(widths[i], min(len(str(val)), max_width))

        # Build header
        header = " | ".join(
            str(c).ljust(widths[i]) for i, c in enumerate(self.columns)
        )
        separator = "-+-".join("-" * w for w in widths)

        # Build rows
        lines = [header, separator]
        for row in self.rows:
            line = " | ".join(
                str(val if not isinstance(val, NullValue) else "NULL").ljust(widths[i])[:max_width]
                for i, val in enumerate(row)
            )
            lines.append(line)

        lines.append(f"({len(self.rows)} row{'s' if len(self.rows) != 1 else ''})")
        return "\n".join(lines)


class Executor:
    """Query executor using the Volcano (iterator) model.

    Coordinates between the query planner, storage engine, catalog,
    and MVCC manager to execute SQL statements.
    """

    def __init__(
        self,
        catalog: Catalog,
        buffer_pool: BufferPool,
        disk_manager: DiskManager,
        wal_manager: WALManager,
        mvcc: MVCCManager,
    ) -> None:
        self._catalog = catalog
        self._pool = buffer_pool
        self._disk = disk_manager
        self._wal = wal_manager
        self._mvcc = mvcc
        self._heap_files: dict[str, HeapFile] = {}
        self._indexes: dict[str, BPlusTree] = {}

    def execute(self, plan: PlanNode, txn: Optional[Transaction] = None) -> QueryResult:
        """Execute a query plan and return the result.

        If no transaction is provided, a new auto-commit transaction is created.
        """
        auto_commit = False
        if txn is None:
            txn = self._mvcc.begin_transaction()
            auto_commit = True

        try:
            result = self._execute_node(plan, txn)
            if auto_commit:
                self._mvcc.commit_transaction(txn)
            return result
        except Exception as e:
            if auto_commit:
                self._mvcc.abort_transaction(txn)
            raise

    def _execute_node(self, node: PlanNode, txn: Transaction) -> QueryResult:
        """Recursively execute a plan node."""
        dispatch = {
            PlanNodeType.CREATE_TABLE: self._exec_create_table,
            PlanNodeType.DROP_TABLE: self._exec_drop_table,
            PlanNodeType.CREATE_INDEX: self._exec_create_index,
            PlanNodeType.INSERT: self._exec_insert,
            PlanNodeType.UPDATE: self._exec_update,
            PlanNodeType.DELETE: self._exec_delete,
            PlanNodeType.SEQ_SCAN: self._exec_seq_scan,
            PlanNodeType.INDEX_SCAN: self._exec_index_scan,
            PlanNodeType.FILTER: self._exec_filter,
            PlanNodeType.PROJECT: self._exec_project,
            PlanNodeType.SORT: self._exec_sort,
            PlanNodeType.LIMIT: self._exec_limit,
            PlanNodeType.DISTINCT: self._exec_distinct,
            PlanNodeType.NESTED_LOOP_JOIN: self._exec_nested_loop_join,
            PlanNodeType.HASH_JOIN: self._exec_hash_join,
            PlanNodeType.AGGREGATE: self._exec_aggregate,
            PlanNodeType.VALUES: self._exec_values,
        }

        handler = dispatch.get(node.type)
        if handler is None:
            raise RuntimeError(f"Unsupported plan node type: {node.type}")

        return handler(node, txn)

    # ─── DDL Execution ───────────────────────────────────────────────────

    def _exec_create_table(self, node: PlanNode, txn: Transaction) -> QueryResult:
        """Execute CREATE TABLE."""
        stmt: CreateTableStatement = node.statement  # type: ignore

        if self._catalog.table_exists(stmt.name):
            if stmt.if_not_exists:
                return QueryResult(
                    columns=[], rows=[],
                    message=f"Table '{stmt.name}' already exists (skipped)"
                )
            raise RuntimeError(f"Table '{stmt.name}' already exists")

        # Build column metadata
        columns = []
        primary_key = None
        for col_def in stmt.columns:
            col_info = {
                "name": col_def.name,
                "type": col_def.data_type.upper(),
                "constraints": [],
            }
            for c in col_def.constraints:
                col_info["constraints"].append(c.type.name)
                if c.type == ConstraintType.PRIMARY_KEY:
                    primary_key = col_def.name
            columns.append(col_info)

        # Create the table info and heap file
        table_info = TableInfo(
            name=stmt.name,
            columns=columns,
            primary_key=primary_key,
        )

        schema = table_info.get_schema()
        heap_file = HeapFile(schema, self._pool)
        table_info.first_page_id = heap_file.first_page_id

        self._catalog.create_table(table_info)
        self._heap_files[stmt.name] = heap_file

        # Auto-create index on primary key
        if primary_key:
            idx = BPlusTree(order=64)
            idx_name = f"pk_{stmt.name}_{primary_key}"
            self._indexes[idx_name] = idx
            self._catalog.create_index(IndexInfo(
                name=idx_name,
                table_name=stmt.name,
                columns=[primary_key],
                is_unique=True,
                is_primary=True,
            ))

        return QueryResult(
            columns=[], rows=[],
            message=f"Table '{stmt.name}' created successfully"
        )

    def _exec_drop_table(self, node: PlanNode, txn: Transaction) -> QueryResult:
        """Execute DROP TABLE."""
        table_name = node.table_name or ""
        stmt = node.statement

        if not self._catalog.table_exists(table_name):
            if stmt and hasattr(stmt, "if_exists") and stmt.if_exists:
                return QueryResult(
                    columns=[], rows=[],
                    message=f"Table '{table_name}' does not exist (skipped)"
                )
            raise RuntimeError(f"Table '{table_name}' does not exist")

        # Remove indexes
        for idx in self._catalog.get_indexes_for_table(table_name):
            self._indexes.pop(idx.name, None)

        # Remove heap file reference
        self._heap_files.pop(table_name, None)
        self._catalog.drop_table(table_name)

        return QueryResult(
            columns=[], rows=[],
            message=f"Table '{table_name}' dropped successfully"
        )

    def _exec_create_index(self, node: PlanNode, txn: Transaction) -> QueryResult:
        """Execute CREATE INDEX."""
        stmt: CreateIndexStatement = node.statement  # type: ignore

        # Create B+Tree index
        idx = BPlusTree(order=64)
        self._indexes[stmt.name] = idx

        # Register in catalog
        self._catalog.create_index(IndexInfo(
            name=stmt.name,
            table_name=stmt.table,
            columns=stmt.columns,
            is_unique=stmt.unique,
        ))

        # Populate index with existing data
        heap = self._get_heap_file(stmt.table)
        if heap:
            table_info = self._catalog.get_table(stmt.table)
            col_names = table_info.column_names() if table_info else []
            col_idx = col_names.index(stmt.columns[0]) if stmt.columns[0] in col_names else -1

            if col_idx >= 0:
                for rid, values in heap.scan():
                    key = values[col_idx]
                    if stmt.unique:
                        idx.insert_unique(key, rid)
                    else:
                        idx.insert(key, rid)

        return QueryResult(
            columns=[], rows=[],
            message=f"Index '{stmt.name}' created on {stmt.table}({', '.join(stmt.columns)})"
        )

    # ─── DML Execution ───────────────────────────────────────────────────

    def _exec_insert(self, node: PlanNode, txn: Transaction) -> QueryResult:
        """Execute INSERT INTO ... VALUES ..."""
        table_name = node.table_name or ""
        table_info = self._catalog.get_table(table_name)
        if table_info is None:
            raise RuntimeError(f"Table '{table_name}' does not exist")

        schema = table_info.get_schema()
        heap = self._get_or_create_heap(table_name, schema)
        col_names = table_info.column_names()

        rows_inserted = 0
        for value_exprs in (node.values or []):
            # Evaluate expressions
            row_values: list[Any] = []
            for i, expr in enumerate(value_exprs):
                val = self._eval_expr(expr, {}, col_names, txn)
                row_values.append(val)

            # Handle column list (reorder values to match schema)
            if node.insert_columns:
                full_row: list[Any] = [NULL] * len(col_names)
                for i, col_name in enumerate(node.insert_columns):
                    col_idx = col_names.index(col_name)
                    full_row[col_idx] = row_values[i]

                # Handle auto-increment
                for j, col in enumerate(table_info.columns):
                    if "AUTOINCREMENT" in col.get("constraints", []) and isinstance(full_row[j], NullValue):
                        full_row[j] = self._catalog.increment_auto_counter(table_name)
                    elif "PRIMARY_KEY" in col.get("constraints", []) and isinstance(full_row[j], NullValue):
                        if "AUTOINCREMENT" in col.get("constraints", []):
                            full_row[j] = self._catalog.increment_auto_counter(table_name)

                row_values = full_row
            else:
                # Handle auto-increment for positional inserts
                for j, col in enumerate(table_info.columns):
                    if j < len(row_values):
                        continue
                    if "AUTOINCREMENT" in col.get("constraints", []):
                        row_values.append(self._catalog.increment_auto_counter(table_name))
                    else:
                        row_values.append(NULL)

            # Pad with NULLs if needed
            while len(row_values) < len(col_names):
                row_values.append(NULL)

            # Insert into heap
            rid = heap.insert(row_values)

            # Update indexes
            self._update_indexes_on_insert(table_name, row_values, rid, col_names)

            # WAL log
            self._wal.log_insert(
                txn.txn_id, hash(table_name) & 0xFFFFFFFF,
                rid.page_id, rid.slot_index,
                b""  # Simplified: not serializing full record for WAL
            )

            rows_inserted += 1

        # Update stats
        current_count = table_info.row_count_estimate
        self._catalog.update_table_stats(table_name, current_count + rows_inserted)

        return QueryResult(
            columns=[], rows=[],
            affected_rows=rows_inserted,
            message=f"{rows_inserted} row(s) inserted"
        )

    def _exec_update(self, node: PlanNode, txn: Transaction) -> QueryResult:
        """Execute UPDATE ... SET ... WHERE ..."""
        table_name = node.table_name or ""
        table_info = self._catalog.get_table(table_name)
        if table_info is None:
            raise RuntimeError(f"Table '{table_name}' does not exist")

        # Get rows from child (scan + filter)
        child_result = self._execute_node(node.children[0], txn)
        col_names = table_info.column_names()
        schema = table_info.get_schema()
        heap = self._get_or_create_heap(table_name, schema)

        rows_updated = 0
        for rid, values in heap.scan():
            row_dict = dict(zip(col_names, values))

            # Check WHERE condition (re-evaluate on actual data)
            if node.children and node.children[0].predicate:
                if not self._eval_predicate(node.children[0].predicate, row_dict, col_names, txn):
                    continue

            # Apply assignments
            new_values = list(values)
            for col_name, expr in (node.assignments or []):
                col_idx = col_names.index(col_name)
                new_values[col_idx] = self._eval_expr(expr, row_dict, col_names, txn)

            heap.update(rid, new_values)
            rows_updated += 1

        return QueryResult(
            columns=[], rows=[],
            affected_rows=rows_updated,
            message=f"{rows_updated} row(s) updated"
        )

    def _exec_delete(self, node: PlanNode, txn: Transaction) -> QueryResult:
        """Execute DELETE FROM ... WHERE ..."""
        table_name = node.table_name or ""
        table_info = self._catalog.get_table(table_name)
        if table_info is None:
            raise RuntimeError(f"Table '{table_name}' does not exist")

        col_names = table_info.column_names()
        schema = table_info.get_schema()
        heap = self._get_or_create_heap(table_name, schema)

        rows_deleted = 0
        rids_to_delete: list[RecordId] = []

        # Collect RIDs first (can't delete during iteration)
        for rid, values in heap.scan():
            row_dict = dict(zip(col_names, values))

            # Check WHERE condition
            predicate = None
            if node.children and node.children[0].type == PlanNodeType.FILTER:
                predicate = node.children[0].predicate
            elif node.children and node.children[0].predicate:
                predicate = node.children[0].predicate

            if predicate:
                if not self._eval_predicate(predicate, row_dict, col_names, txn):
                    continue

            rids_to_delete.append(rid)

        for rid in rids_to_delete:
            heap.delete(rid)
            rows_deleted += 1

        # Update stats
        current_count = table_info.row_count_estimate
        self._catalog.update_table_stats(table_name, max(0, current_count - rows_deleted))

        return QueryResult(
            columns=[], rows=[],
            affected_rows=rows_deleted,
            message=f"{rows_deleted} row(s) deleted"
        )

    # ─── Query Execution ─────────────────────────────────────────────────

    def _exec_seq_scan(self, node: PlanNode, txn: Transaction) -> QueryResult:
        """Execute a sequential (full table) scan."""
        table_name = node.table_name or ""
        table_info = self._catalog.get_table(table_name)
        if table_info is None:
            raise RuntimeError(f"Table '{table_name}' does not exist")

        col_names = table_info.column_names()
        schema = table_info.get_schema()
        heap = self._get_or_create_heap(table_name, schema)

        rows: list[list[Any]] = []
        for rid, values in heap.scan():
            # Apply predicate if present (pushed down from filter)
            if node.predicate:
                row_dict = dict(zip(col_names, values))
                if not self._eval_predicate(node.predicate, row_dict, col_names, txn):
                    continue
            rows.append(values)

        # Use alias as column prefix if available
        output_cols = col_names
        return QueryResult(columns=output_cols, rows=rows)

    def _exec_index_scan(self, node: PlanNode, txn: Transaction) -> QueryResult:
        """Execute an index scan using B+Tree."""
        table_name = node.table_name or ""
        table_info = self._catalog.get_table(table_name)
        if table_info is None:
            raise RuntimeError(f"Table '{table_name}' does not exist")

        col_names = table_info.column_names()
        schema = table_info.get_schema()
        heap = self._get_or_create_heap(table_name, schema)

        idx = self._indexes.get(node.index_name or "")
        if idx is None:
            # Fall back to sequential scan
            return self._exec_seq_scan(node, txn)

        rows: list[list[Any]] = []

        if node.scan_range:
            low, high = node.scan_range
            for key, rid in idx.range_scan(low=low, high=high):
                values = heap.read(rid)
                if values:
                    rows.append(values)
        else:
            # Full index scan
            for key, rid in idx.scan_all():
                values = heap.read(rid)
                if values:
                    if node.predicate:
                        row_dict = dict(zip(col_names, values))
                        if not self._eval_predicate(node.predicate, row_dict, col_names, txn):
                            continue
                    rows.append(values)

        return QueryResult(columns=col_names, rows=rows)

    def _exec_filter(self, node: PlanNode, txn: Transaction) -> QueryResult:
        """Execute a filter (WHERE clause)."""
        child_result = self._execute_node(node.children[0], txn)
        col_names = child_result.columns

        filtered_rows = []
        for row in child_result.rows:
            row_dict = dict(zip(col_names, row))
            if self._eval_predicate(node.predicate, row_dict, col_names, txn):
                filtered_rows.append(row)

        return QueryResult(columns=col_names, rows=filtered_rows)

    def _exec_project(self, node: PlanNode, txn: Transaction) -> QueryResult:
        """Execute a projection (SELECT column list)."""
        child_result = self._execute_node(node.children[0], txn)
        input_cols = child_result.columns

        if not node.columns:
            return child_result

        output_cols: list[str] = []
        projected_rows: list[list[Any]] = []

        # Determine output columns
        for col_spec in node.columns:
            if isinstance(col_spec, tuple):
                expr, alias = col_spec
            else:
                expr = col_spec
                alias = None

            if isinstance(expr, StarExpr):
                if expr.table:
                    # table.* — add all columns from that table
                    for c in input_cols:
                        if c.startswith(f"{expr.table}.") or not "." in c:
                            output_cols.append(c)
                else:
                    output_cols.extend(input_cols)
            elif alias:
                output_cols.append(alias)
            elif isinstance(expr, ColumnRef):
                output_cols.append(expr.column)
            elif isinstance(expr, FunctionCall):
                output_cols.append(f"{expr.name}({', '.join(str(a) for a in expr.args)})")
            else:
                output_cols.append(str(expr))

        # Project rows
        for row in child_result.rows:
            row_dict = dict(zip(input_cols, row))
            projected = []

            for col_spec in node.columns:
                if isinstance(col_spec, tuple):
                    expr, alias = col_spec
                else:
                    expr = col_spec
                    alias = None

                if isinstance(expr, StarExpr):
                    if expr.table:
                        for i, c in enumerate(input_cols):
                            if c.startswith(f"{expr.table}.") or not "." in c:
                                projected.append(row[i])
                    else:
                        projected.extend(row)
                else:
                    val = self._eval_expr(expr, row_dict, input_cols, txn)
                    projected.append(val)

            projected_rows.append(projected)

        return QueryResult(columns=output_cols, rows=projected_rows)

    def _exec_sort(self, node: PlanNode, txn: Transaction) -> QueryResult:
        """Execute ORDER BY using Python's timsort."""
        child_result = self._execute_node(node.children[0], txn)
        col_names = child_result.columns

        if not node.sort_keys:
            return child_result

        def sort_key(row: list[Any]) -> tuple:
            row_dict = dict(zip(col_names, row))
            key_vals = []
            for expr, desc in node.sort_keys:  # type: ignore
                val = self._eval_expr(expr, row_dict, col_names, txn)
                # Handle NULLs (sort last)
                if isinstance(val, NullValue):
                    key_vals.append((1, None))
                else:
                    if desc:
                        # For descending, negate numeric values
                        if isinstance(val, (int, float)):
                            key_vals.append((0, -val))
                        else:
                            key_vals.append((0, val))
                    else:
                        key_vals.append((0, val))
            return tuple(key_vals)

        # Sort with custom key
        sorted_rows = sorted(child_result.rows, key=sort_key)

        # Handle descending for non-numeric types
        if node.sort_keys and any(desc for _, desc in node.sort_keys):
            # If all sort keys are descending, just reverse
            if all(desc for _, desc in node.sort_keys):
                sorted_rows = list(reversed(sorted_rows))

        return QueryResult(columns=col_names, rows=sorted_rows)

    def _exec_limit(self, node: PlanNode, txn: Transaction) -> QueryResult:
        """Execute LIMIT/OFFSET."""
        child_result = self._execute_node(node.children[0], txn)

        offset = node.offset or 0
        limit = node.limit or len(child_result.rows)
        sliced = child_result.rows[offset:offset + limit]

        return QueryResult(columns=child_result.columns, rows=sliced)

    def _exec_distinct(self, node: PlanNode, txn: Transaction) -> QueryResult:
        """Execute DISTINCT using a hash set."""
        child_result = self._execute_node(node.children[0], txn)

        seen: set[tuple] = set()
        unique_rows: list[list[Any]] = []

        for row in child_result.rows:
            key = tuple(
                str(v) if isinstance(v, NullValue) else v
                for v in row
            )
            if key not in seen:
                seen.add(key)
                unique_rows.append(row)

        return QueryResult(columns=child_result.columns, rows=unique_rows)

    def _exec_nested_loop_join(self, node: PlanNode, txn: Transaction) -> QueryResult:
        """Execute a nested-loop join.

        For each row in the left table, scan all rows in the right table
        and check the join condition. O(n*m) complexity.
        """
        left_result = self._execute_node(node.children[0], txn)
        right_result = self._execute_node(node.children[1], txn)

        output_cols = left_result.columns + right_result.columns
        joined_rows: list[list[Any]] = []

        for left_row in left_result.rows:
            matched = False
            for right_row in right_result.rows:
                combined = left_row + right_row
                row_dict = dict(zip(output_cols, combined))

                if node.join_condition:
                    if self._eval_predicate(node.join_condition, row_dict, output_cols, txn):
                        joined_rows.append(combined)
                        matched = True
                else:
                    # Cross join
                    joined_rows.append(combined)
                    matched = True

            # Handle LEFT JOIN: add NULL-padded row if no match
            if not matched and node.join_type in (
                JoinType.LEFT, JoinType.FULL
            ):
                null_right = [NULL] * len(right_result.columns)
                joined_rows.append(left_row + null_right)

        # Handle RIGHT/FULL JOIN
        if node.join_type in (JoinType.RIGHT, JoinType.FULL):
            for right_row in right_result.rows:
                has_match = False
                for left_row in left_result.rows:
                    combined = left_row + right_row
                    row_dict = dict(zip(output_cols, combined))
                    if node.join_condition:
                        if self._eval_predicate(node.join_condition, row_dict, output_cols, txn):
                            has_match = True
                            break
                    else:
                        has_match = True
                        break

                if not has_match:
                    null_left = [NULL] * len(left_result.columns)
                    joined_rows.append(null_left + right_row)

        return QueryResult(columns=output_cols, rows=joined_rows)

    def _exec_hash_join(self, node: PlanNode, txn: Transaction) -> QueryResult:
        """Execute a hash join.

        Build a hash table on the smaller relation, then probe with
        the larger relation. O(n + m) expected time.
        """
        left_result = self._execute_node(node.children[0], txn)
        right_result = self._execute_node(node.children[1], txn)

        output_cols = left_result.columns + right_result.columns

        # Extract join key columns
        left_key_col = None
        right_key_col = None
        if node.join_condition and isinstance(node.join_condition, BinaryOp):
            if node.join_condition.op == BinaryOpType.EQ:
                if isinstance(node.join_condition.left, ColumnRef):
                    left_key_col = node.join_condition.left.column
                if isinstance(node.join_condition.right, ColumnRef):
                    right_key_col = node.join_condition.right.column

        if left_key_col is None or right_key_col is None:
            # Fall back to nested loop
            return self._exec_nested_loop_join(node, txn)

        # Build phase: hash the right table
        left_key_idx = (
            left_result.columns.index(left_key_col)
            if left_key_col in left_result.columns
            else None
        )
        right_key_idx = (
            right_result.columns.index(right_key_col)
            if right_key_col in right_result.columns
            else None
        )

        if left_key_idx is None or right_key_idx is None:
            return self._exec_nested_loop_join(node, txn)

        # Build hash table on right
        hash_table: dict[Any, list[list[Any]]] = {}
        for row in right_result.rows:
            key = row[right_key_idx]
            if key not in hash_table:
                hash_table[key] = []
            hash_table[key].append(row)

        # Probe phase
        joined_rows: list[list[Any]] = []
        for left_row in left_result.rows:
            key = left_row[left_key_idx]
            matching_right = hash_table.get(key, [])

            if matching_right:
                for right_row in matching_right:
                    joined_rows.append(left_row + right_row)
            elif node.join_type in (JoinType.LEFT, JoinType.FULL):
                null_right = [NULL] * len(right_result.columns)
                joined_rows.append(left_row + null_right)

        return QueryResult(columns=output_cols, rows=joined_rows)

    def _exec_aggregate(self, node: PlanNode, txn: Transaction) -> QueryResult:
        """Execute GROUP BY and aggregate functions.

        Groups rows by the GROUP BY expressions, then computes
        aggregate values for each group.
        """
        child_result = self._execute_node(node.children[0], txn)
        col_names = child_result.columns

        # Group rows
        groups: dict[tuple, list[list[Any]]] = {}
        for row in child_result.rows:
            row_dict = dict(zip(col_names, row))
            if node.group_by:
                key = tuple(
                    self._eval_expr(expr, row_dict, col_names, txn)
                    for expr in node.group_by
                )
            else:
                key = ()  # Single group for aggregate without GROUP BY

            if key not in groups:
                groups[key] = []
            groups[key].append(row)

        # If no rows and no group by, still produce one row for aggregates
        if not groups and not node.group_by:
            groups[()] = []

        # Compute aggregates for each group
        result_rows: list[list[Any]] = []
        output_cols: list[str] = []

        # Build output columns
        if node.group_by:
            for expr in node.group_by:
                if isinstance(expr, ColumnRef):
                    output_cols.append(expr.column)
                else:
                    output_cols.append(str(expr))

        if node.aggregates:
            for agg in node.aggregates:
                name = f"{agg.name}({', '.join(str(a) for a in agg.args)})"
                output_cols.append(name)

        # If output_cols is still empty, use input columns
        if not output_cols:
            output_cols = col_names

        for key, group_rows in groups.items():
            result_row: list[Any] = list(key)

            if node.aggregates:
                for agg in node.aggregates:
                    val = self._compute_aggregate(agg, group_rows, col_names, txn)
                    result_row.append(val)

            result_rows.append(result_row)

        return QueryResult(columns=output_cols, rows=result_rows)

    def _exec_values(self, node: PlanNode, txn: Transaction) -> QueryResult:
        """Execute a VALUES node (literal row source)."""
        return QueryResult(columns=[], rows=[[]])

    # ─── Aggregate Computation ───────────────────────────────────────────

    def _compute_aggregate(
        self, agg: FunctionCall, rows: list[list[Any]],
        col_names: list[str], txn: Transaction
    ) -> Any:
        """Compute an aggregate function over a group of rows."""
        func = agg.name.upper()

        if func == "COUNT":
            if agg.args and isinstance(agg.args[0], StarExpr):
                return len(rows)
            if not agg.args:
                return len(rows)
            # COUNT(expr) — count non-null values
            count = 0
            for row in rows:
                row_dict = dict(zip(col_names, row))
                val = self._eval_expr(agg.args[0], row_dict, col_names, txn)
                if not isinstance(val, NullValue):
                    count += 1
            return count

        # For SUM, AVG, MIN, MAX — extract values
        values: list[Any] = []
        for row in rows:
            row_dict = dict(zip(col_names, row))
            val = self._eval_expr(agg.args[0], row_dict, col_names, txn)
            if not isinstance(val, NullValue):
                values.append(val)

        if not values:
            return NULL

        if func == "SUM":
            return sum(values)
        elif func == "AVG":
            return sum(values) / len(values)
        elif func == "MIN":
            return min(values)
        elif func == "MAX":
            return max(values)

        raise RuntimeError(f"Unknown aggregate function: {func}")

    # ─── Expression Evaluation ───────────────────────────────────────────

    def _eval_expr(
        self, expr: Expression, row: dict[str, Any],
        col_names: list[str], txn: Transaction
    ) -> Any:
        """Evaluate an expression against a row of data."""
        if isinstance(expr, Literal):
            if expr.literal_type == "null":
                return NULL
            return expr.value

        elif isinstance(expr, ColumnRef):
            # Try qualified name first
            if expr.table and f"{expr.table}.{expr.column}" in row:
                return row[f"{expr.table}.{expr.column}"]
            # Try unqualified
            if expr.column in row:
                return row[expr.column]
            # Try case-insensitive
            for key in row:
                if key.lower() == expr.column.lower():
                    return row[key]
            return NULL

        elif isinstance(expr, BinaryOp):
            left = self._eval_expr(expr.left, row, col_names, txn)
            right = self._eval_expr(expr.right, row, col_names, txn)
            return self._eval_binary_op(expr.op, left, right)

        elif isinstance(expr, UnaryOp):
            operand = self._eval_expr(expr.operand, row, col_names, txn)
            if expr.op == UnaryOpType.NOT:
                if isinstance(operand, NullValue):
                    return NULL
                return not operand
            elif expr.op == UnaryOpType.NEGATE:
                if isinstance(operand, NullValue):
                    return NULL
                return -operand

        elif isinstance(expr, FunctionCall):
            if expr.is_aggregate:
                # Aggregates are handled at the aggregate node level
                # If we reach here, it's a scalar context (shouldn't happen normally)
                return NULL
            # Handle built-in scalar functions
            args = [self._eval_expr(a, row, col_names, txn) for a in expr.args]
            return self._eval_function(expr.name, args)

        elif isinstance(expr, IsNullExpr):
            val = self._eval_expr(expr.expr, row, col_names, txn)
            is_null = isinstance(val, NullValue)
            return not is_null if expr.negated else is_null

        elif isinstance(expr, BetweenExpr):
            val = self._eval_expr(expr.expr, row, col_names, txn)
            low = self._eval_expr(expr.low, row, col_names, txn)
            high = self._eval_expr(expr.high, row, col_names, txn)
            result = low <= val <= high
            return not result if expr.negated else result

        elif isinstance(expr, InExpr):
            val = self._eval_expr(expr.expr, row, col_names, txn)
            values = [self._eval_expr(v, row, col_names, txn) for v in expr.values]
            result = val in values
            return not result if expr.negated else result

        elif isinstance(expr, LikeExpr):
            val = self._eval_expr(expr.expr, row, col_names, txn)
            pattern = self._eval_expr(expr.pattern, row, col_names, txn)
            if isinstance(val, NullValue) or isinstance(pattern, NullValue):
                return NULL
            # Convert SQL LIKE pattern to Python regex
            regex = self._like_to_regex(str(pattern))
            result = bool(re.match(regex, str(val), re.IGNORECASE))
            return not result if expr.negated else result

        elif isinstance(expr, CaseExpr):
            if expr.operand:
                # Simple CASE
                operand_val = self._eval_expr(expr.operand, row, col_names, txn)
                for when_expr, then_expr in expr.when_clauses:
                    when_val = self._eval_expr(when_expr, row, col_names, txn)
                    if operand_val == when_val:
                        return self._eval_expr(then_expr, row, col_names, txn)
            else:
                # Searched CASE
                for when_expr, then_expr in expr.when_clauses:
                    if self._eval_expr(when_expr, row, col_names, txn):
                        return self._eval_expr(then_expr, row, col_names, txn)

            if expr.else_clause:
                return self._eval_expr(expr.else_clause, row, col_names, txn)
            return NULL

        elif isinstance(expr, CastExpr):
            val = self._eval_expr(expr.expr, row, col_names, txn)
            return self._cast_value(val, expr.target_type)

        elif isinstance(expr, StarExpr):
            return NULL  # Stars are handled at the project level

        return NULL

    def _eval_predicate(
        self, predicate: Optional[Expression], row: dict[str, Any],
        col_names: list[str], txn: Transaction
    ) -> bool:
        """Evaluate a predicate expression, returning True/False."""
        if predicate is None:
            return True
        result = self._eval_expr(predicate, row, col_names, txn)
        if isinstance(result, NullValue):
            return False
        return bool(result)

    def _eval_binary_op(self, op: BinaryOpType, left: Any, right: Any) -> Any:
        """Evaluate a binary operation."""
        # NULL propagation
        if isinstance(left, NullValue) or isinstance(right, NullValue):
            if op in (BinaryOpType.AND, BinaryOpType.OR):
                # Special NULL handling for boolean operators
                if op == BinaryOpType.AND:
                    if isinstance(left, NullValue) and right is False:
                        return False
                    if isinstance(right, NullValue) and left is False:
                        return False
                elif op == BinaryOpType.OR:
                    if isinstance(left, NullValue) and right is True:
                        return True
                    if isinstance(right, NullValue) and left is True:
                        return True
                return NULL
            return NULL

        ops = {
            BinaryOpType.ADD: lambda a, b: a + b,
            BinaryOpType.SUB: lambda a, b: a - b,
            BinaryOpType.MUL: lambda a, b: a * b,
            BinaryOpType.DIV: lambda a, b: a / b if b != 0 else NULL,
            BinaryOpType.MOD: lambda a, b: a % b if b != 0 else NULL,
            BinaryOpType.EQ: lambda a, b: a == b,
            BinaryOpType.NEQ: lambda a, b: a != b,
            BinaryOpType.LT: lambda a, b: a < b,
            BinaryOpType.GT: lambda a, b: a > b,
            BinaryOpType.LTE: lambda a, b: a <= b,
            BinaryOpType.GTE: lambda a, b: a >= b,
            BinaryOpType.AND: lambda a, b: a and b,
            BinaryOpType.OR: lambda a, b: a or b,
            BinaryOpType.CONCAT: lambda a, b: str(a) + str(b),
        }

        func = ops.get(op)
        if func:
            try:
                return func(left, right)
            except (TypeError, ValueError):
                return NULL

        return NULL

    def _eval_function(self, name: str, args: list[Any]) -> Any:
        """Evaluate a scalar function."""
        name_upper = name.upper()

        if name_upper == "ABS":
            return abs(args[0]) if args else NULL
        elif name_upper == "UPPER":
            return str(args[0]).upper() if args else NULL
        elif name_upper == "LOWER":
            return str(args[0]).lower() if args else NULL
        elif name_upper == "LENGTH" or name_upper == "LEN":
            return len(str(args[0])) if args else NULL
        elif name_upper == "COALESCE":
            for arg in args:
                if not isinstance(arg, NullValue):
                    return arg
            return NULL
        elif name_upper == "NULLIF":
            if len(args) >= 2 and args[0] == args[1]:
                return NULL
            return args[0] if args else NULL
        elif name_upper == "SUBSTR" or name_upper == "SUBSTRING":
            if len(args) >= 2:
                s = str(args[0])
                start = int(args[1]) - 1  # SQL is 1-indexed
                length = int(args[2]) if len(args) >= 3 else len(s)
                return s[start:start + length]
            return NULL
        elif name_upper == "REPLACE":
            if len(args) >= 3:
                return str(args[0]).replace(str(args[1]), str(args[2]))
            return NULL
        elif name_upper == "TRIM":
            return str(args[0]).strip() if args else NULL
        elif name_upper == "ROUND":
            if args:
                decimals = int(args[1]) if len(args) >= 2 else 0
                return round(float(args[0]), decimals)
            return NULL
        elif name_upper == "TYPEOF":
            if args:
                if isinstance(args[0], NullValue):
                    return "null"
                elif isinstance(args[0], int):
                    return "integer"
                elif isinstance(args[0], float):
                    return "real"
                elif isinstance(args[0], str):
                    return "text"
                return "blob"
            return NULL

        return NULL

    @staticmethod
    def _like_to_regex(pattern: str) -> str:
        """Convert SQL LIKE pattern to Python regex.

        SQL LIKE patterns:
        - % matches any sequence of characters
        - _ matches any single character
        """
        regex = "^"
        for ch in pattern:
            if ch == "%":
                regex += ".*"
            elif ch == "_":
                regex += "."
            else:
                regex += re.escape(ch)
        regex += "$"
        return regex

    @staticmethod
    def _cast_value(value: Any, target_type: str) -> Any:
        """Cast a value to a target SQL type."""
        if isinstance(value, NullValue):
            return NULL

        target = target_type.upper()
        try:
            if target in ("INTEGER", "INT"):
                return int(value)
            elif target in ("FLOAT", "DOUBLE", "REAL"):
                return float(value)
            elif target in ("TEXT", "VARCHAR"):
                return str(value)
            elif target in ("BOOLEAN", "BOOL"):
                return bool(value)
        except (ValueError, TypeError):
            return NULL

        return value

    # ─── Helper Methods ──────────────────────────────────────────────────

    def _get_heap_file(self, table_name: str) -> Optional[HeapFile]:
        """Get the heap file for a table, loading it if needed."""
        if table_name in self._heap_files:
            return self._heap_files[table_name]

        table_info = self._catalog.get_table(table_name)
        if table_info is None:
            return None

        schema = table_info.get_schema()
        if table_info.first_page_id >= 0:
            heap = HeapFile(schema, self._pool, table_info.first_page_id)
        else:
            heap = HeapFile(schema, self._pool)
            self._catalog.update_first_page_id(table_name, heap.first_page_id)

        self._heap_files[table_name] = heap
        return heap

    def _get_or_create_heap(self, table_name: str, schema: TableSchema) -> HeapFile:
        """Get or create a heap file for a table."""
        heap = self._get_heap_file(table_name)
        if heap is None:
            heap = HeapFile(schema, self._pool)
            self._heap_files[table_name] = heap
            self._catalog.update_first_page_id(table_name, heap.first_page_id)
        return heap

    def _update_indexes_on_insert(
        self, table_name: str, values: list[Any],
        rid: RecordId, col_names: list[str]
    ) -> None:
        """Update all indexes for a table after an insert."""
        indexes = self._catalog.get_indexes_for_table(table_name)
        for idx_info in indexes:
            idx = self._indexes.get(idx_info.name)
            if idx is None:
                continue

            # Get the key value from the indexed column
            if idx_info.columns:
                col_idx = col_names.index(idx_info.columns[0]) if idx_info.columns[0] in col_names else -1
                if col_idx >= 0:
                    key = values[col_idx]
                    if idx_info.is_unique:
                        idx.insert_unique(key, rid)
                    else:
                        idx.insert(key, rid)
