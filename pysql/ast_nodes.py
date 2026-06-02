"""
Abstract Syntax Tree (AST) Node Definitions for SQL.

This module defines the complete AST hierarchy used to represent parsed SQL
statements. The design follows the Visitor pattern to enable clean separation
between parsing and execution/optimization phases.

AST Node Hierarchy:
    ASTNode (base)
    ├── Statement (base for all SQL statements)
    │   ├── SelectStatement
    │   ├── InsertStatement
    │   ├── UpdateStatement
    │   ├── DeleteStatement
    │   ├── CreateTableStatement
    │   ├── DropTableStatement
    │   ├── CreateIndexStatement
    │   ├── ExplainStatement
    │   ├── BeginStatement
    │   ├── CommitStatement
    │   └── RollbackStatement
    ├── Expression (base for all expressions)
    │   ├── Literal (integer, float, string, boolean, null)
    │   ├── ColumnRef (table.column references)
    │   ├── BinaryOp (arithmetic, comparison, logical)
    │   ├── UnaryOp (NOT, negative)
    │   ├── FunctionCall (COUNT, SUM, etc.)
    │   ├── BetweenExpr
    │   ├── InExpr
    │   ├── LikeExpr
    │   ├── IsNullExpr
    │   ├── CaseExpr
    │   ├── CastExpr
    │   ├── ExistsExpr
    │   └── SubqueryExpr
    ├── TableRef (FROM clause references)
    │   ├── SimpleTableRef
    │   ├── JoinTableRef
    │   └── SubqueryTableRef
    ├── ColumnDef (CREATE TABLE column definitions)
    ├── OrderByItem
    └── Constraint
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Optional


# ─── Base Nodes ──────────────────────────────────────────────────────────────

class ASTNode:
    """Base class for all AST nodes."""
    pass


class Statement(ASTNode):
    """Base class for all SQL statements."""
    pass


class Expression(ASTNode):
    """Base class for all SQL expressions."""
    pass


class TableRef(ASTNode):
    """Base class for table references in FROM clauses."""
    pass


# ─── Expression Nodes ────────────────────────────────────────────────────────

@dataclass
class Literal(Expression):
    """A literal value: integer, float, string, boolean, or NULL.

    Examples: 42, 3.14, 'hello', TRUE, NULL
    """
    value: Any
    literal_type: str  # 'integer', 'float', 'string', 'boolean', 'null'


@dataclass
class ColumnRef(Expression):
    """A reference to a table column, optionally qualified with table name.

    Examples: name, users.name, u.age
    """
    column: str
    table: Optional[str] = None

    def __repr__(self) -> str:
        if self.table:
            return f"ColumnRef({self.table}.{self.column})"
        return f"ColumnRef({self.column})"


class BinaryOpType(Enum):
    """Types of binary operations."""
    # Arithmetic
    ADD = auto()
    SUB = auto()
    MUL = auto()
    DIV = auto()
    MOD = auto()
    # Comparison
    EQ = auto()
    NEQ = auto()
    LT = auto()
    GT = auto()
    LTE = auto()
    GTE = auto()
    # Logical
    AND = auto()
    OR = auto()
    # String
    CONCAT = auto()


@dataclass
class BinaryOp(Expression):
    """A binary operation: left OP right.

    Examples: a + b, x > 5, cond1 AND cond2
    """
    op: BinaryOpType
    left: Expression
    right: Expression


class UnaryOpType(Enum):
    """Types of unary operations."""
    NOT = auto()
    NEGATE = auto()


@dataclass
class UnaryOp(Expression):
    """A unary operation: OP expr.

    Examples: NOT active, -price
    """
    op: UnaryOpType
    operand: Expression


class AggregateFunc(Enum):
    """Supported aggregate functions."""
    COUNT = auto()
    SUM = auto()
    AVG = auto()
    MIN = auto()
    MAX = auto()


@dataclass
class FunctionCall(Expression):
    """A function call expression.

    Examples: COUNT(*), SUM(price), AVG(score)
    """
    name: str
    args: list[Expression]
    distinct: bool = False
    is_aggregate: bool = False

    def __post_init__(self) -> None:
        upper_name = self.name.upper()
        if upper_name in ("COUNT", "SUM", "AVG", "MIN", "MAX"):
            self.is_aggregate = True


@dataclass
class StarExpr(Expression):
    """The * wildcard expression (SELECT *).

    Can be qualified: table.* (SELECT u.*)
    """
    table: Optional[str] = None


@dataclass
class BetweenExpr(Expression):
    """BETWEEN expression: expr BETWEEN low AND high.

    Example: age BETWEEN 18 AND 65
    """
    expr: Expression
    low: Expression
    high: Expression
    negated: bool = False


@dataclass
class InExpr(Expression):
    """IN expression: expr IN (val1, val2, ...) or expr IN (subquery).

    Examples: status IN ('active', 'pending'), id IN (SELECT user_id FROM orders)
    """
    expr: Expression
    values: list[Expression]
    subquery: Optional["SelectStatement"] = None
    negated: bool = False


@dataclass
class LikeExpr(Expression):
    """LIKE pattern matching: expr LIKE pattern.

    Example: name LIKE 'Jo%'
    """
    expr: Expression
    pattern: Expression
    negated: bool = False


@dataclass
class IsNullExpr(Expression):
    """IS NULL / IS NOT NULL check.

    Example: email IS NOT NULL
    """
    expr: Expression
    negated: bool = False


@dataclass
class CaseExpr(Expression):
    """CASE expression (searched form).

    Example:
        CASE
            WHEN age < 18 THEN 'minor'
            WHEN age < 65 THEN 'adult'
            ELSE 'senior'
        END
    """
    operand: Optional[Expression] = None  # Simple CASE: CASE expr WHEN ...
    when_clauses: list[tuple[Expression, Expression]] = field(default_factory=list)
    else_clause: Optional[Expression] = None


@dataclass
class CastExpr(Expression):
    """CAST expression: CAST(expr AS type).

    Example: CAST(price AS INTEGER)
    """
    expr: Expression
    target_type: str


@dataclass
class ExistsExpr(Expression):
    """EXISTS subquery check.

    Example: EXISTS (SELECT 1 FROM orders WHERE user_id = u.id)
    """
    subquery: "SelectStatement"


@dataclass
class SubqueryExpr(Expression):
    """A subquery used as a scalar expression.

    Example: (SELECT MAX(price) FROM products)
    """
    subquery: "SelectStatement"


# ─── Table Reference Nodes ───────────────────────────────────────────────────

@dataclass
class SimpleTableRef(TableRef):
    """A simple table reference with optional alias.

    Examples: users, users AS u, users u
    """
    name: str
    alias: Optional[str] = None


class JoinType(Enum):
    """Types of JOIN operations."""
    INNER = auto()
    LEFT = auto()
    RIGHT = auto()
    FULL = auto()
    CROSS = auto()
    NATURAL = auto()


@dataclass
class JoinTableRef(TableRef):
    """A JOIN between two table references.

    Example: users u INNER JOIN orders o ON u.id = o.user_id
    """
    left: TableRef
    right: TableRef
    join_type: JoinType = JoinType.INNER
    condition: Optional[Expression] = None
    using_columns: Optional[list[str]] = None


@dataclass
class SubqueryTableRef(TableRef):
    """A subquery used as a table reference in FROM.

    Example: (SELECT * FROM users WHERE active = TRUE) AS active_users
    """
    subquery: "SelectStatement"
    alias: str = ""


# ─── Column Definition (for CREATE TABLE) ────────────────────────────────────

class ConstraintType(Enum):
    """Types of column/table constraints."""
    PRIMARY_KEY = auto()
    NOT_NULL = auto()
    UNIQUE = auto()
    DEFAULT = auto()
    CHECK = auto()
    FOREIGN_KEY = auto()
    AUTOINCREMENT = auto()


@dataclass
class Constraint:
    """A single constraint on a column or table.

    Examples: PRIMARY KEY, NOT NULL, DEFAULT 0, CHECK(age > 0)
    """
    type: ConstraintType
    value: Optional[Any] = None  # default value, check expression, etc.
    references_table: Optional[str] = None
    references_column: Optional[str] = None


@dataclass
class ColumnDef(ASTNode):
    """A column definition in a CREATE TABLE statement.

    Example: name VARCHAR(100) NOT NULL DEFAULT ''
    """
    name: str
    data_type: str
    constraints: list[Constraint] = field(default_factory=list)

    @property
    def is_primary_key(self) -> bool:
        return any(c.type == ConstraintType.PRIMARY_KEY for c in self.constraints)

    @property
    def is_not_null(self) -> bool:
        return any(c.type == ConstraintType.NOT_NULL for c in self.constraints)

    @property
    def is_unique(self) -> bool:
        return any(c.type == ConstraintType.UNIQUE for c in self.constraints)

    @property
    def is_autoincrement(self) -> bool:
        return any(c.type == ConstraintType.AUTOINCREMENT for c in self.constraints)

    @property
    def default_value(self) -> Optional[Any]:
        for c in self.constraints:
            if c.type == ConstraintType.DEFAULT:
                return c.value
        return None


# ─── ORDER BY ────────────────────────────────────────────────────────────────

@dataclass
class OrderByItem(ASTNode):
    """A single ORDER BY specification.

    Example: price DESC, name ASC
    """
    expr: Expression
    descending: bool = False


# ─── Statement Nodes ─────────────────────────────────────────────────────────

@dataclass
class SelectStatement(Statement):
    """A complete SELECT statement with all clauses.

    SELECT [DISTINCT] columns
    FROM table_refs
    [WHERE condition]
    [GROUP BY exprs]
    [HAVING condition]
    [ORDER BY items]
    [LIMIT n [OFFSET m]]
    """
    columns: list[Expression | tuple[Expression, Optional[str]]]  # expr or (expr, alias)
    from_clause: Optional[TableRef] = None
    where: Optional[Expression] = None
    group_by: Optional[list[Expression]] = None
    having: Optional[Expression] = None
    order_by: Optional[list[OrderByItem]] = None
    limit: Optional[int] = None
    offset: Optional[int] = None
    distinct: bool = False


@dataclass
class InsertStatement(Statement):
    """INSERT INTO table [(columns)] VALUES (vals), ...

    Example: INSERT INTO users (name, age) VALUES ('Alice', 30), ('Bob', 25)
    """
    table: str
    columns: Optional[list[str]] = None
    values: list[list[Expression]] = field(default_factory=list)


@dataclass
class UpdateStatement(Statement):
    """UPDATE table SET col=val, ... [WHERE condition]

    Example: UPDATE users SET age = age + 1 WHERE name = 'Alice'
    """
    table: str
    assignments: list[tuple[str, Expression]] = field(default_factory=list)
    where: Optional[Expression] = None


@dataclass
class DeleteStatement(Statement):
    """DELETE FROM table [WHERE condition]

    Example: DELETE FROM users WHERE age < 18
    """
    table: str
    where: Optional[Expression] = None


@dataclass
class CreateTableStatement(Statement):
    """CREATE TABLE [IF NOT EXISTS] name (column_defs, [table_constraints])

    Example:
        CREATE TABLE users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name VARCHAR(100) NOT NULL,
            email TEXT UNIQUE
        )
    """
    name: str
    columns: list[ColumnDef]
    if_not_exists: bool = False
    table_constraints: list[Constraint] = field(default_factory=list)


@dataclass
class DropTableStatement(Statement):
    """DROP TABLE [IF EXISTS] name [CASCADE|RESTRICT]

    Example: DROP TABLE IF EXISTS users CASCADE
    """
    name: str
    if_exists: bool = False
    cascade: bool = False


@dataclass
class CreateIndexStatement(Statement):
    """CREATE [UNIQUE] INDEX name ON table (columns)

    Example: CREATE UNIQUE INDEX idx_email ON users (email)
    """
    name: str
    table: str
    columns: list[str]
    unique: bool = False
    if_not_exists: bool = False


@dataclass
class ExplainStatement(Statement):
    """EXPLAIN statement — shows query execution plan.

    Example: EXPLAIN SELECT * FROM users WHERE age > 25
    """
    statement: Statement


@dataclass
class BeginStatement(Statement):
    """BEGIN [TRANSACTION] — starts a new transaction."""
    pass


@dataclass
class CommitStatement(Statement):
    """COMMIT — commits the current transaction."""
    pass


@dataclass
class RollbackStatement(Statement):
    """ROLLBACK — aborts the current transaction."""
    pass
