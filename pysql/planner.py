"""
Query Planner and Cost-Based Optimizer.

The query planner transforms a parsed SQL AST into an executable query plan.
The optimizer then chooses the most efficient physical plan by estimating
costs and exploring alternative plans.

Query Processing Pipeline:
    SQL String → Lexer → Parser → AST
        ↓
    Logical Plan (what to compute)
        ↓
    Optimized Logical Plan (rewritten for efficiency)
        ↓
    Physical Plan (how to compute it)
        ↓
    Executor (iterates and produces results)

Logical Plan Operators:
    - Scan (full table scan)
    - IndexScan (B+Tree index lookup)
    - Filter (WHERE clause)
    - Project (SELECT columns)
    - Join (nested loop, hash, sort-merge)
    - Sort (ORDER BY)
    - Aggregate (GROUP BY + aggregate functions)
    - Limit (LIMIT/OFFSET)
    - Distinct (DISTINCT)

Optimization Rules:
    1. Predicate pushdown (move filters closer to scans)
    2. Projection pushdown (only read needed columns)
    3. Join reordering (smaller tables first)
    4. Index selection (use indexes when beneficial)
    5. Constant folding (evaluate constant expressions)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Optional

from .ast_nodes import (
    BinaryOp,
    BinaryOpType,
    ColumnRef,
    CreateIndexStatement,
    CreateTableStatement,
    DeleteStatement,
    DropTableStatement,
    Expression,
    ExplainStatement,
    FunctionCall,
    InsertStatement,
    JoinTableRef,
    JoinType,
    Literal,
    SelectStatement,
    SimpleTableRef,
    StarExpr,
    Statement,
    SubqueryTableRef,
    UpdateStatement,
)
from .catalog import Catalog, IndexInfo, TableInfo


# ─── Logical Plan Nodes ─────────────────────────────────────────────────────

class PlanNodeType(Enum):
    """Types of plan nodes."""
    SEQ_SCAN = auto()
    INDEX_SCAN = auto()
    FILTER = auto()
    PROJECT = auto()
    NESTED_LOOP_JOIN = auto()
    HASH_JOIN = auto()
    SORT_MERGE_JOIN = auto()
    SORT = auto()
    AGGREGATE = auto()
    LIMIT = auto()
    DISTINCT = auto()
    INSERT = auto()
    UPDATE = auto()
    DELETE = auto()
    CREATE_TABLE = auto()
    DROP_TABLE = auto()
    CREATE_INDEX = auto()
    VALUES = auto()  # Literal value source for INSERT


@dataclass
class PlanNode:
    """A node in the query execution plan tree.

    Each node represents a relational algebra operator.
    The plan forms a tree where data flows from leaves (scans)
    to the root (final output).
    """
    type: PlanNodeType
    table_name: Optional[str] = None
    alias: Optional[str] = None

    # Filter/predicate
    predicate: Optional[Expression] = None

    # Projection columns
    columns: Optional[list[Expression | tuple[Expression, Optional[str]]]] = None
    output_columns: Optional[list[str]] = None  # Resolved column names

    # Sort
    sort_keys: Optional[list[tuple[Expression, bool]]] = None  # (expr, is_desc)

    # Aggregate
    group_by: Optional[list[Expression]] = None
    aggregates: Optional[list[FunctionCall]] = None

    # Limit/Offset
    limit: Optional[int] = None
    offset: Optional[int] = None

    # Join
    join_type: Optional[JoinType] = None
    join_condition: Optional[Expression] = None

    # Index scan
    index_name: Optional[str] = None
    index_columns: Optional[list[str]] = None
    scan_range: Optional[tuple[Any, Any]] = None  # (low, high) for range scans

    # Children
    children: list["PlanNode"] = field(default_factory=list)

    # For INSERT/UPDATE/DELETE
    assignments: Optional[list[tuple[str, Expression]]] = None
    values: Optional[list[list[Expression]]] = None
    insert_columns: Optional[list[str]] = None

    # Cost estimation
    estimated_rows: float = 0
    estimated_cost: float = 0

    # For CREATE TABLE
    statement: Optional[Statement] = None

    def __repr__(self) -> str:
        parts = [self.type.name]
        if self.table_name:
            parts.append(f"table={self.table_name}")
        if self.alias:
            parts.append(f"alias={self.alias}")
        if self.estimated_cost > 0:
            parts.append(f"cost={self.estimated_cost:.1f}")
        if self.estimated_rows > 0:
            parts.append(f"rows={self.estimated_rows:.0f}")
        return f"PlanNode({', '.join(parts)})"

    def explain(self, indent: int = 0) -> str:
        """Generate a human-readable execution plan (like EXPLAIN in PostgreSQL)."""
        prefix = "  " * indent
        line = f"{prefix}→ {self.type.name}"

        if self.table_name:
            alias_str = f" AS {self.alias}" if self.alias else ""
            line += f" on {self.table_name}{alias_str}"

        if self.index_name:
            line += f" using index {self.index_name}"

        if self.predicate:
            line += f"  (filter: {self._expr_to_str(self.predicate)})"

        if self.join_type:
            line += f"  ({self.join_type.name} JOIN)"

        if self.sort_keys:
            sorts = []
            for expr, desc in self.sort_keys:
                s = self._expr_to_str(expr)
                sorts.append(f"{s} {'DESC' if desc else 'ASC'}")
            line += f"  (sort: {', '.join(sorts)})"

        if self.limit is not None:
            line += f"  (limit: {self.limit}"
            if self.offset:
                line += f", offset: {self.offset}"
            line += ")"

        line += f"  [est. {self.estimated_rows:.0f} rows, cost: {self.estimated_cost:.1f}]"

        result = line + "\n"
        for child in self.children:
            result += child.explain(indent + 1)

        return result

    @staticmethod
    def _expr_to_str(expr: Expression) -> str:
        """Convert an expression to a readable string for EXPLAIN output."""
        if isinstance(expr, ColumnRef):
            if expr.table:
                return f"{expr.table}.{expr.column}"
            return expr.column
        elif isinstance(expr, Literal):
            if isinstance(expr.value, str):
                return f"'{expr.value}'"
            return str(expr.value)
        elif isinstance(expr, BinaryOp):
            op_map = {
                BinaryOpType.EQ: "=",
                BinaryOpType.NEQ: "!=",
                BinaryOpType.LT: "<",
                BinaryOpType.GT: ">",
                BinaryOpType.LTE: "<=",
                BinaryOpType.GTE: ">=",
                BinaryOpType.AND: "AND",
                BinaryOpType.OR: "OR",
                BinaryOpType.ADD: "+",
                BinaryOpType.SUB: "-",
                BinaryOpType.MUL: "*",
                BinaryOpType.DIV: "/",
            }
            op_str = op_map.get(expr.op, str(expr.op))
            left = PlanNode._expr_to_str(expr.left)
            right = PlanNode._expr_to_str(expr.right)
            return f"{left} {op_str} {right}"
        elif isinstance(expr, FunctionCall):
            args_str = ", ".join(PlanNode._expr_to_str(a) for a in expr.args)
            return f"{expr.name}({args_str})"
        return str(expr)


class QueryPlanner:
    """Converts AST statements into executable plan trees.

    The planner handles:
    1. Resolving table and column references
    2. Building the initial logical plan
    3. Optimizing the plan (predicate pushdown, index selection, etc.)
    4. Estimating costs for plan comparison
    """

    def __init__(self, catalog: Catalog) -> None:
        self._catalog = catalog

    def plan(self, stmt: Statement) -> PlanNode:
        """Generate an optimized plan for a SQL statement."""
        if isinstance(stmt, SelectStatement):
            plan = self._plan_select(stmt)
            plan = self._optimize(plan)
            return plan
        elif isinstance(stmt, InsertStatement):
            return self._plan_insert(stmt)
        elif isinstance(stmt, UpdateStatement):
            return self._plan_update(stmt)
        elif isinstance(stmt, DeleteStatement):
            return self._plan_delete(stmt)
        elif isinstance(stmt, CreateTableStatement):
            return PlanNode(type=PlanNodeType.CREATE_TABLE, statement=stmt)
        elif isinstance(stmt, DropTableStatement):
            return PlanNode(type=PlanNodeType.DROP_TABLE, statement=stmt, table_name=stmt.name)
        elif isinstance(stmt, CreateIndexStatement):
            return PlanNode(type=PlanNodeType.CREATE_INDEX, statement=stmt)
        elif isinstance(stmt, ExplainStatement):
            return self.plan(stmt.statement)
        else:
            raise ValueError(f"Unsupported statement type: {type(stmt).__name__}")

    def _plan_select(self, stmt: SelectStatement) -> PlanNode:
        """Build a logical plan for a SELECT statement.

        Plan structure (bottom to top):
            Scan → Filter → Join → Aggregate → Having → Project → Sort → Limit → Distinct
        """
        # Build the scan/join plan from the FROM clause
        if stmt.from_clause is not None:
            current = self._plan_table_ref(stmt.from_clause)
        else:
            # SELECT without FROM (e.g., SELECT 1+1)
            current = PlanNode(type=PlanNodeType.VALUES, estimated_rows=1, estimated_cost=0)

        # WHERE clause → Filter
        if stmt.where is not None:
            filter_node = PlanNode(
                type=PlanNodeType.FILTER,
                predicate=stmt.where,
                children=[current],
                estimated_rows=current.estimated_rows * 0.33,  # Selectivity heuristic
                estimated_cost=current.estimated_cost + current.estimated_rows * 0.1,
            )
            current = filter_node

        # GROUP BY + aggregates
        if stmt.group_by is not None or self._has_aggregates(stmt.columns):
            agg_node = PlanNode(
                type=PlanNodeType.AGGREGATE,
                group_by=stmt.group_by,
                aggregates=self._extract_aggregates(stmt.columns),
                children=[current],
                estimated_rows=max(1, current.estimated_rows * 0.1),
                estimated_cost=current.estimated_cost + current.estimated_rows * 0.5,
            )
            current = agg_node

        # HAVING clause → Filter after aggregation
        if stmt.having is not None:
            having_node = PlanNode(
                type=PlanNodeType.FILTER,
                predicate=stmt.having,
                children=[current],
                estimated_rows=current.estimated_rows * 0.5,
                estimated_cost=current.estimated_cost + current.estimated_rows * 0.1,
            )
            current = having_node

        # SELECT list → Project
        project_node = PlanNode(
            type=PlanNodeType.PROJECT,
            columns=stmt.columns,
            children=[current],
            estimated_rows=current.estimated_rows,
            estimated_cost=current.estimated_cost + current.estimated_rows * 0.01,
        )
        current = project_node

        # ORDER BY → Sort
        if stmt.order_by:
            sort_keys = [(item.expr, item.descending) for item in stmt.order_by]
            sort_node = PlanNode(
                type=PlanNodeType.SORT,
                sort_keys=sort_keys,
                children=[current],
                estimated_rows=current.estimated_rows,
                # Sort cost: n * log(n)
                estimated_cost=current.estimated_cost + (
                    current.estimated_rows * max(1, current.estimated_rows).bit_length() * 0.5
                ),
            )
            current = sort_node

        # DISTINCT
        if stmt.distinct:
            distinct_node = PlanNode(
                type=PlanNodeType.DISTINCT,
                children=[current],
                estimated_rows=current.estimated_rows * 0.8,
                estimated_cost=current.estimated_cost + current.estimated_rows * 0.3,
            )
            current = distinct_node

        # LIMIT/OFFSET
        if stmt.limit is not None:
            limit_node = PlanNode(
                type=PlanNodeType.LIMIT,
                limit=stmt.limit,
                offset=stmt.offset or 0,
                children=[current],
                estimated_rows=min(stmt.limit, current.estimated_rows),
                estimated_cost=current.estimated_cost + 0.1,
            )
            current = limit_node

        return current

    def _plan_table_ref(self, table_ref: Any) -> PlanNode:
        """Build a plan node for a table reference."""
        if isinstance(table_ref, SimpleTableRef):
            table_info = self._catalog.get_table(table_ref.name)
            est_rows = table_info.row_count_estimate if table_info else 1000

            return PlanNode(
                type=PlanNodeType.SEQ_SCAN,
                table_name=table_ref.name,
                alias=table_ref.alias,
                estimated_rows=float(est_rows),
                estimated_cost=float(est_rows) * 1.0,  # Sequential I/O cost
            )

        elif isinstance(table_ref, JoinTableRef):
            left = self._plan_table_ref(table_ref.left)
            right = self._plan_table_ref(table_ref.right)

            # Choose join algorithm based on estimated sizes
            if left.estimated_rows * right.estimated_rows < 100000:
                join_type = PlanNodeType.NESTED_LOOP_JOIN
                cost = left.estimated_cost + right.estimated_cost + (
                    left.estimated_rows * right.estimated_rows * 0.01
                )
            else:
                join_type = PlanNodeType.HASH_JOIN
                cost = left.estimated_cost + right.estimated_cost + (
                    left.estimated_rows + right.estimated_rows
                ) * 2.0

            est_rows = (left.estimated_rows * right.estimated_rows * 0.1)

            return PlanNode(
                type=join_type,
                join_type=table_ref.join_type,
                join_condition=table_ref.condition,
                children=[left, right],
                estimated_rows=est_rows,
                estimated_cost=cost,
            )

        elif isinstance(table_ref, SubqueryTableRef):
            subplan = self._plan_select(table_ref.subquery)
            subplan.alias = table_ref.alias
            return subplan

        raise ValueError(f"Unsupported table reference: {type(table_ref)}")

    def _plan_insert(self, stmt: InsertStatement) -> PlanNode:
        return PlanNode(
            type=PlanNodeType.INSERT,
            table_name=stmt.table,
            insert_columns=stmt.columns,
            values=stmt.values,
            estimated_rows=float(len(stmt.values)),
            estimated_cost=float(len(stmt.values)) * 1.0,
        )

    def _plan_update(self, stmt: UpdateStatement) -> PlanNode:
        # Build scan + filter for the WHERE clause
        scan = PlanNode(
            type=PlanNodeType.SEQ_SCAN,
            table_name=stmt.table,
            estimated_rows=1000,
            estimated_cost=1000,
        )

        if stmt.where:
            scan = PlanNode(
                type=PlanNodeType.FILTER,
                predicate=stmt.where,
                children=[scan],
                estimated_rows=330,
                estimated_cost=1100,
            )

        return PlanNode(
            type=PlanNodeType.UPDATE,
            table_name=stmt.table,
            assignments=stmt.assignments,
            children=[scan],
            estimated_rows=scan.estimated_rows,
            estimated_cost=scan.estimated_cost + scan.estimated_rows * 2.0,
        )

    def _plan_delete(self, stmt: DeleteStatement) -> PlanNode:
        scan = PlanNode(
            type=PlanNodeType.SEQ_SCAN,
            table_name=stmt.table,
            estimated_rows=1000,
            estimated_cost=1000,
        )

        if stmt.where:
            scan = PlanNode(
                type=PlanNodeType.FILTER,
                predicate=stmt.where,
                children=[scan],
                estimated_rows=330,
                estimated_cost=1100,
            )

        return PlanNode(
            type=PlanNodeType.DELETE,
            table_name=stmt.table,
            children=[scan],
            estimated_rows=scan.estimated_rows,
            estimated_cost=scan.estimated_cost + scan.estimated_rows * 1.0,
        )

    # ─── Optimization ───────────────────────────────────────────────────

    def _optimize(self, plan: PlanNode) -> PlanNode:
        """Apply optimization rules to the plan.

        Rules are applied in order:
        1. Predicate pushdown
        2. Index scan selection
        3. Constant folding
        """
        plan = self._pushdown_predicates(plan)
        plan = self._select_indexes(plan)
        self._estimate_costs(plan)
        return plan

    def _pushdown_predicates(self, node: PlanNode) -> PlanNode:
        """Push filter predicates as close to the data source as possible.

        This reduces the number of rows flowing through the plan,
        which is one of the most impactful optimizations.

        Example:
            Before: Scan(users) → Join → Filter(age > 18)
            After:  Scan(users, filter: age > 18) → Join
        """
        if node.type == PlanNodeType.FILTER and len(node.children) == 1:
            child = node.children[0]

            # If the child is a scan, merge the filter into the scan
            if child.type == PlanNodeType.SEQ_SCAN and child.predicate is None:
                child.predicate = node.predicate
                child.estimated_rows *= 0.33
                return child

            # If the child is a join, try to push predicates to join children
            if child.type in (
                PlanNodeType.NESTED_LOOP_JOIN,
                PlanNodeType.HASH_JOIN,
                PlanNodeType.SORT_MERGE_JOIN,
            ):
                pushed = self._try_push_to_join(node.predicate, child)
                if pushed:
                    return child

        # Recurse into children
        node.children = [self._pushdown_predicates(c) for c in node.children]
        return node

    def _try_push_to_join(self, predicate: Expression, join_node: PlanNode) -> bool:
        """Try to push a predicate down through a join.

        A predicate can be pushed to a join's left/right child if it only
        references columns from that child's tables.
        """
        if not isinstance(predicate, BinaryOp):
            return False

        # Simple case: predicate references a single table
        tables = self._extract_tables_from_expr(predicate)
        if len(tables) == 1:
            table_name = next(iter(tables))
            for i, child in enumerate(join_node.children):
                child_tables = self._get_plan_tables(child)
                if table_name in child_tables:
                    # Push the filter to this child
                    filter_node = PlanNode(
                        type=PlanNodeType.FILTER,
                        predicate=predicate,
                        children=[child],
                        estimated_rows=child.estimated_rows * 0.33,
                        estimated_cost=child.estimated_cost + child.estimated_rows * 0.1,
                    )
                    join_node.children[i] = filter_node
                    return True
        return False

    def _select_indexes(self, node: PlanNode) -> PlanNode:
        """Replace sequential scans with index scans when beneficial.

        An index scan is beneficial when:
        1. There's a suitable index on the filter columns
        2. The selectivity is low enough (< 15% of table)
        """
        if node.type == PlanNodeType.SEQ_SCAN and node.predicate and node.table_name:
            # Check for applicable indexes
            index = self._find_applicable_index(node.table_name, node.predicate)
            if index is not None:
                scan_range = self._extract_scan_range(node.predicate)
                node.type = PlanNodeType.INDEX_SCAN
                node.index_name = index.name
                node.index_columns = index.columns
                node.scan_range = scan_range
                # Index scan is cheaper (log n vs n)
                node.estimated_cost = max(1, node.estimated_rows).bit_length() * 2.0

        # Recurse
        node.children = [self._select_indexes(c) for c in node.children]
        return node

    def _find_applicable_index(
        self, table_name: str, predicate: Expression
    ) -> Optional[IndexInfo]:
        """Find an index that can accelerate the given predicate."""
        indexes = self._catalog.get_indexes_for_table(table_name)
        pred_columns = self._extract_columns_from_expr(predicate)

        for index in indexes:
            # Check if the index's first column is in the predicate
            if index.columns and index.columns[0] in pred_columns:
                return index

        return None

    def _extract_scan_range(
        self, predicate: Expression
    ) -> Optional[tuple[Any, Any]]:
        """Extract a scan range from a comparison predicate.

        For example: age > 18 → (18, None)
                     age BETWEEN 18 AND 65 → (18, 65)
        """
        if isinstance(predicate, BinaryOp):
            if isinstance(predicate.right, Literal):
                val = predicate.right.value
                if predicate.op in (BinaryOpType.EQ,):
                    return (val, val)
                elif predicate.op in (BinaryOpType.GT, BinaryOpType.GTE):
                    return (val, None)
                elif predicate.op in (BinaryOpType.LT, BinaryOpType.LTE):
                    return (None, val)
        return None

    def _estimate_costs(self, node: PlanNode) -> None:
        """Re-estimate costs after optimization."""
        for child in node.children:
            self._estimate_costs(child)

        if node.children:
            child_cost = sum(c.estimated_cost for c in node.children)
            if node.estimated_cost < child_cost:
                node.estimated_cost = child_cost + node.estimated_rows * 0.1

    # ─── Helper Methods ──────────────────────────────────────────────────

    @staticmethod
    def _has_aggregates(columns: list) -> bool:
        """Check if the select list contains aggregate functions."""
        for col in columns:
            expr = col[0] if isinstance(col, tuple) else col
            if isinstance(expr, FunctionCall) and expr.is_aggregate:
                return True
        return False

    @staticmethod
    def _extract_aggregates(columns: list) -> list[FunctionCall]:
        """Extract aggregate function calls from the select list."""
        aggs = []
        for col in columns:
            expr = col[0] if isinstance(col, tuple) else col
            if isinstance(expr, FunctionCall) and expr.is_aggregate:
                aggs.append(expr)
        return aggs

    @staticmethod
    def _extract_tables_from_expr(expr: Expression) -> set[str]:
        """Extract all table names referenced in an expression."""
        tables: set[str] = set()
        if isinstance(expr, ColumnRef) and expr.table:
            tables.add(expr.table)
        elif isinstance(expr, BinaryOp):
            tables.update(QueryPlanner._extract_tables_from_expr(expr.left))
            tables.update(QueryPlanner._extract_tables_from_expr(expr.right))
        return tables

    @staticmethod
    def _extract_columns_from_expr(expr: Expression) -> set[str]:
        """Extract all column names referenced in an expression."""
        columns: set[str] = set()
        if isinstance(expr, ColumnRef):
            columns.add(expr.column)
        elif isinstance(expr, BinaryOp):
            columns.update(QueryPlanner._extract_columns_from_expr(expr.left))
            columns.update(QueryPlanner._extract_columns_from_expr(expr.right))
        return columns

    @staticmethod
    def _get_plan_tables(node: PlanNode) -> set[str]:
        """Get all table names in a plan subtree."""
        tables: set[str] = set()
        if node.table_name:
            tables.add(node.table_name)
            if node.alias:
                tables.add(node.alias)
        for child in node.children:
            tables.update(QueryPlanner._get_plan_tables(child))
        return tables
