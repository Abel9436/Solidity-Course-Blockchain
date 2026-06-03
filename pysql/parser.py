"""
Recursive-Descent SQL Parser.

Transforms a stream of tokens (from the Lexer) into an Abstract Syntax Tree (AST).
This is a hand-written recursive-descent parser that implements the following
SQL grammar with correct operator precedence:

    statement       → select | insert | update | delete | create_table
                    | drop_table | create_index | explain | begin | commit | rollback
    select          → SELECT [DISTINCT] select_list FROM table_ref
                      [WHERE expr] [GROUP BY expr_list] [HAVING expr]
                      [ORDER BY order_list] [LIMIT int [OFFSET int]]
    expr            → or_expr
    or_expr         → and_expr (OR and_expr)*
    and_expr        → not_expr (AND not_expr)*
    not_expr        → NOT not_expr | comparison
    comparison      → addition ((=|!=|<|>|<=|>=) addition)?
                    | addition IS [NOT] NULL
                    | addition [NOT] BETWEEN addition AND addition
                    | addition [NOT] IN (expr_list | subquery)
                    | addition [NOT] LIKE pattern
    addition        → multiplication ((+|-|'||') multiplication)*
    multiplication  → unary ((*|/|%) unary)*
    unary           → (-) unary | primary
    primary         → literal | column_ref | function_call | (expr) | subquery
                    | CASE expr? (WHEN expr THEN expr)+ [ELSE expr] END
                    | CAST(expr AS type) | EXISTS(subquery)

Operator Precedence (lowest to highest):
    OR → AND → NOT → comparison → addition → multiplication → unary → primary
"""

from __future__ import annotations

from typing import Optional

from .ast_nodes import (
    ASTNode,
    BeginStatement,
    BetweenExpr,
    BinaryOp,
    BinaryOpType,
    CaseExpr,
    CastExpr,
    ColumnDef,
    ColumnRef,
    CommitStatement,
    Constraint,
    ConstraintType,
    CreateIndexStatement,
    CreateTableStatement,
    DeleteStatement,
    DropTableStatement,
    ExistsExpr,
    ExplainStatement,
    Expression,
    FunctionCall,
    InExpr,
    InsertStatement,
    IsNullExpr,
    JoinTableRef,
    JoinType,
    LikeExpr,
    Literal,
    OrderByItem,
    RollbackStatement,
    SelectStatement,
    SimpleTableRef,
    StarExpr,
    Statement,
    SubqueryExpr,
    SubqueryTableRef,
    TableRef,
    UnaryOp,
    UnaryOpType,
    UpdateStatement,
)
from .lexer import Lexer, Token, TokenType


class ParseError(Exception):
    """Raised when the parser encounters a syntax error."""

    def __init__(self, message: str, token: Token) -> None:
        self.token = token
        super().__init__(
            f"Parse error at L{token.line}:C{token.column} "
            f"near '{token.value}': {message}"
        )


class Parser:
    """Recursive-descent SQL parser.

    Parses a list of tokens into one or more AST Statement nodes.
    Implements correct operator precedence through the recursive call hierarchy.

    Usage:
        parser = Parser("SELECT * FROM users WHERE age > 18")
        stmts = parser.parse()
    """

    def __init__(self, source: str) -> None:
        self._source = source
        self._tokens: list[Token] = Lexer(source).tokenize()
        self._pos = 0

    @property
    def _current(self) -> Token:
        """The current token being examined."""
        if self._pos >= len(self._tokens):
            return self._tokens[-1]  # EOF
        return self._tokens[self._pos]

    def _peek(self, offset: int = 1) -> Token:
        """Look ahead at a future token without consuming it."""
        idx = self._pos + offset
        if idx >= len(self._tokens):
            return self._tokens[-1]
        return self._tokens[idx]

    def _advance(self) -> Token:
        """Consume and return the current token."""
        token = self._current
        self._pos += 1
        return token

    def _expect(self, token_type: TokenType, message: str = "") -> Token:
        """Consume and return the current token, or raise an error.

        Args:
            token_type: Expected token type
            message: Optional custom error message
        """
        if self._current.type != token_type:
            msg = message or f"Expected {token_type.name}, got {self._current.type.name}"
            raise ParseError(msg, self._current)
        return self._advance()

    def _match(self, *types: TokenType) -> Optional[Token]:
        """If the current token matches any of the given types, consume and return it."""
        if self._current.type in types:
            return self._advance()
        return None

    def _check(self, *types: TokenType) -> bool:
        """Check if the current token is one of the given types without consuming."""
        return self._current.type in types

    # ─── Public API ──────────────────────────────────────────────────────────

    def parse(self) -> list[Statement]:
        """Parse all statements from the source.

        Returns a list of Statement AST nodes. Multiple statements
        are separated by semicolons.
        """
        statements: list[Statement] = []

        while not self._check(TokenType.EOF):
            stmt = self._parse_statement()
            statements.append(stmt)
            self._match(TokenType.SEMICOLON)  # optional trailing semicolon

        return statements

    def parse_single(self) -> Statement:
        """Parse a single statement (convenience method)."""
        stmts = self.parse()
        if not stmts:
            raise ParseError("No statement found", self._current)
        return stmts[0]

    # ─── Statement Parsing ───────────────────────────────────────────────────

    def _parse_statement(self) -> Statement:
        """Route to the appropriate statement parser based on the first token."""
        token = self._current

        if token.type == TokenType.SELECT:
            return self._parse_select()
        elif token.type == TokenType.INSERT:
            return self._parse_insert()
        elif token.type == TokenType.UPDATE:
            return self._parse_update()
        elif token.type == TokenType.DELETE:
            return self._parse_delete()
        elif token.type == TokenType.CREATE:
            return self._parse_create()
        elif token.type == TokenType.DROP:
            return self._parse_drop()
        elif token.type == TokenType.EXPLAIN:
            return self._parse_explain()
        elif token.type == TokenType.BEGIN:
            return self._parse_begin()
        elif token.type == TokenType.COMMIT:
            self._advance()
            return CommitStatement()
        elif token.type == TokenType.ROLLBACK:
            self._advance()
            return RollbackStatement()
        else:
            raise ParseError(f"Unexpected token: {token.value}", token)

    def _parse_select(self) -> SelectStatement:
        """Parse a SELECT statement with all optional clauses."""
        self._expect(TokenType.SELECT)

        # DISTINCT
        distinct = bool(self._match(TokenType.DISTINCT))

        # SELECT list
        columns = self._parse_select_list()

        # FROM
        from_clause = None
        if self._match(TokenType.FROM):
            from_clause = self._parse_table_ref()

        # WHERE
        where = None
        if self._match(TokenType.WHERE):
            where = self._parse_expression()

        # GROUP BY
        group_by = None
        if self._check(TokenType.GROUP):
            self._advance()
            self._expect(TokenType.BY)
            group_by = self._parse_expression_list()

        # HAVING
        having = None
        if self._match(TokenType.HAVING):
            having = self._parse_expression()

        # ORDER BY
        order_by = None
        if self._check(TokenType.ORDER):
            self._advance()
            self._expect(TokenType.BY)
            order_by = self._parse_order_by_list()

        # LIMIT
        limit = None
        offset = None
        if self._match(TokenType.LIMIT):
            limit_token = self._expect(TokenType.INTEGER_LITERAL, "Expected integer after LIMIT")
            limit = int(limit_token.value)

            if self._match(TokenType.OFFSET):
                offset_token = self._expect(
                    TokenType.INTEGER_LITERAL, "Expected integer after OFFSET"
                )
                offset = int(offset_token.value)

        return SelectStatement(
            columns=columns,
            from_clause=from_clause,
            where=where,
            group_by=group_by,
            having=having,
            order_by=order_by,
            limit=limit,
            offset=offset,
            distinct=distinct,
        )

    def _parse_select_list(self) -> list[Expression | tuple[Expression, Optional[str]]]:
        """Parse the column list after SELECT.

        Each item can be:
        - * (star)
        - table.* (qualified star)
        - expression [AS alias]
        """
        items: list[Expression | tuple[Expression, Optional[str]]] = []

        while True:
            if self._check(TokenType.STAR):
                self._advance()
                items.append(StarExpr())
            else:
                expr = self._parse_expression()

                # Check for table.* pattern
                if isinstance(expr, ColumnRef) and self._check(TokenType.DOT):
                    self._advance()
                    if self._match(TokenType.STAR):
                        items.append(StarExpr(table=expr.column))
                        if not self._match(TokenType.COMMA):
                            break
                        continue

                # Optional alias
                alias: Optional[str] = None
                if self._match(TokenType.AS):
                    alias_token = self._expect(
                        TokenType.IDENTIFIER, "Expected alias name after AS"
                    )
                    alias = alias_token.value
                elif self._check(TokenType.IDENTIFIER) and not self._check(
                    TokenType.FROM, TokenType.WHERE, TokenType.GROUP,
                    TokenType.ORDER, TokenType.LIMIT, TokenType.JOIN,
                    TokenType.INNER, TokenType.LEFT, TokenType.RIGHT,
                    TokenType.FULL, TokenType.CROSS, TokenType.NATURAL,
                    TokenType.ON, TokenType.HAVING,
                ):
                    # Implicit alias (without AS keyword)
                    alias = self._advance().value

                if alias:
                    items.append((expr, alias))
                else:
                    items.append(expr)

            if not self._match(TokenType.COMMA):
                break

        return items

    def _parse_insert(self) -> InsertStatement:
        """Parse an INSERT statement."""
        self._expect(TokenType.INSERT)
        self._expect(TokenType.INTO)

        table = self._expect(TokenType.IDENTIFIER, "Expected table name").value

        # Optional column list
        columns: Optional[list[str]] = None
        if self._match(TokenType.LPAREN):
            columns = []
            while True:
                col = self._expect(TokenType.IDENTIFIER, "Expected column name").value
                columns.append(col)
                if not self._match(TokenType.COMMA):
                    break
            self._expect(TokenType.RPAREN)

        self._expect(TokenType.VALUES)

        # Parse value rows
        values: list[list[Expression]] = []
        while True:
            self._expect(TokenType.LPAREN)
            row: list[Expression] = []
            while True:
                row.append(self._parse_expression())
                if not self._match(TokenType.COMMA):
                    break
            self._expect(TokenType.RPAREN)
            values.append(row)
            if not self._match(TokenType.COMMA):
                break

        return InsertStatement(table=table, columns=columns, values=values)

    def _parse_update(self) -> UpdateStatement:
        """Parse an UPDATE statement."""
        self._expect(TokenType.UPDATE)
        table = self._expect(TokenType.IDENTIFIER, "Expected table name").value
        self._expect(TokenType.SET)

        # Parse assignments: col = expr, ...
        assignments: list[tuple[str, Expression]] = []
        while True:
            col = self._expect(TokenType.IDENTIFIER, "Expected column name").value
            self._expect(TokenType.EQUALS, "Expected '=' in SET clause")
            expr = self._parse_expression()
            assignments.append((col, expr))
            if not self._match(TokenType.COMMA):
                break

        # Optional WHERE
        where = None
        if self._match(TokenType.WHERE):
            where = self._parse_expression()

        return UpdateStatement(table=table, assignments=assignments, where=where)

    def _parse_delete(self) -> DeleteStatement:
        """Parse a DELETE statement."""
        self._expect(TokenType.DELETE)
        self._expect(TokenType.FROM)
        table = self._expect(TokenType.IDENTIFIER, "Expected table name").value

        where = None
        if self._match(TokenType.WHERE):
            where = self._parse_expression()

        return DeleteStatement(table=table, where=where)

    def _parse_create(self) -> Statement:
        """Parse CREATE TABLE or CREATE INDEX."""
        self._expect(TokenType.CREATE)

        if self._check(TokenType.UNIQUE):
            # CREATE UNIQUE INDEX ...
            self._advance()
            return self._parse_create_index(unique=True)

        if self._check(TokenType.INDEX):
            return self._parse_create_index(unique=False)

        return self._parse_create_table()

    def _parse_create_table(self) -> CreateTableStatement:
        """Parse a CREATE TABLE statement with column definitions and constraints."""
        self._expect(TokenType.TABLE)

        # IF NOT EXISTS
        if_not_exists = False
        if self._check(TokenType.IF):
            self._advance()
            self._expect(TokenType.NOT)
            self._expect(TokenType.EXISTS)
            if_not_exists = True

        name = self._expect(TokenType.IDENTIFIER, "Expected table name").value
        self._expect(TokenType.LPAREN)

        columns: list[ColumnDef] = []
        table_constraints: list[Constraint] = []

        while not self._check(TokenType.RPAREN):
            # Check for table-level constraints
            if self._check(TokenType.PRIMARY, TokenType.UNIQUE,
                           TokenType.CHECK, TokenType.FOREIGN, TokenType.CONSTRAINT):
                tc = self._parse_table_constraint()
                table_constraints.append(tc)
            else:
                col_def = self._parse_column_def()
                columns.append(col_def)

            if not self._match(TokenType.COMMA):
                break

        self._expect(TokenType.RPAREN)

        return CreateTableStatement(
            name=name,
            columns=columns,
            if_not_exists=if_not_exists,
            table_constraints=table_constraints,
        )

    def _parse_column_def(self) -> ColumnDef:
        """Parse a single column definition: name TYPE [constraints...]."""
        name = self._expect(TokenType.IDENTIFIER, "Expected column name").value

        # Parse type (can be multi-word like VARCHAR(100))
        type_token = self._advance()
        data_type = type_token.value

        # Handle parameterized types: VARCHAR(100), CHAR(10)
        if self._match(TokenType.LPAREN):
            param = self._expect(TokenType.INTEGER_LITERAL).value
            data_type += f"({param})"
            self._expect(TokenType.RPAREN)

        # Parse column constraints
        constraints: list[Constraint] = []
        while True:
            if self._check(TokenType.PRIMARY):
                self._advance()
                self._expect(TokenType.KEY)
                constraints.append(Constraint(type=ConstraintType.PRIMARY_KEY))
                if self._match(TokenType.AUTOINCREMENT):
                    constraints.append(Constraint(type=ConstraintType.AUTOINCREMENT))
            elif self._check(TokenType.NOT):
                self._advance()
                if self._check(TokenType.NULL_LITERAL):
                    self._advance()
                    constraints.append(Constraint(type=ConstraintType.NOT_NULL))
                else:
                    raise ParseError("Expected NULL after NOT", self._current)
            elif self._match(TokenType.UNIQUE):
                constraints.append(Constraint(type=ConstraintType.UNIQUE))
            elif self._match(TokenType.DEFAULT):
                default_val = self._parse_expression()
                constraints.append(Constraint(type=ConstraintType.DEFAULT, value=default_val))
            elif self._check(TokenType.CHECK):
                self._advance()
                self._expect(TokenType.LPAREN)
                check_expr = self._parse_expression()
                self._expect(TokenType.RPAREN)
                constraints.append(Constraint(type=ConstraintType.CHECK, value=check_expr))
            elif self._match(TokenType.AUTOINCREMENT):
                constraints.append(Constraint(type=ConstraintType.AUTOINCREMENT))
            elif self._check(TokenType.REFERENCES):
                self._advance()
                ref_table = self._expect(TokenType.IDENTIFIER).value
                self._expect(TokenType.LPAREN)
                ref_col = self._expect(TokenType.IDENTIFIER).value
                self._expect(TokenType.RPAREN)
                constraints.append(Constraint(
                    type=ConstraintType.FOREIGN_KEY,
                    references_table=ref_table,
                    references_column=ref_col,
                ))
            else:
                break

        return ColumnDef(name=name, data_type=data_type, constraints=constraints)

    def _parse_table_constraint(self) -> Constraint:
        """Parse a table-level constraint in CREATE TABLE."""
        # Skip optional CONSTRAINT name
        if self._match(TokenType.CONSTRAINT):
            self._advance()  # constraint name

        if self._check(TokenType.PRIMARY):
            self._advance()
            self._expect(TokenType.KEY)
            self._expect(TokenType.LPAREN)
            cols = []
            while True:
                cols.append(self._expect(TokenType.IDENTIFIER).value)
                if not self._match(TokenType.COMMA):
                    break
            self._expect(TokenType.RPAREN)
            return Constraint(type=ConstraintType.PRIMARY_KEY, value=cols)

        elif self._match(TokenType.UNIQUE):
            self._expect(TokenType.LPAREN)
            cols = []
            while True:
                cols.append(self._expect(TokenType.IDENTIFIER).value)
                if not self._match(TokenType.COMMA):
                    break
            self._expect(TokenType.RPAREN)
            return Constraint(type=ConstraintType.UNIQUE, value=cols)

        elif self._check(TokenType.FOREIGN):
            self._advance()
            self._expect(TokenType.KEY)
            self._expect(TokenType.LPAREN)
            col = self._expect(TokenType.IDENTIFIER).value
            self._expect(TokenType.RPAREN)
            self._expect(TokenType.REFERENCES)
            ref_table = self._expect(TokenType.IDENTIFIER).value
            self._expect(TokenType.LPAREN)
            ref_col = self._expect(TokenType.IDENTIFIER).value
            self._expect(TokenType.RPAREN)
            return Constraint(
                type=ConstraintType.FOREIGN_KEY,
                value=col,
                references_table=ref_table,
                references_column=ref_col,
            )

        elif self._check(TokenType.CHECK):
            self._advance()
            self._expect(TokenType.LPAREN)
            expr = self._parse_expression()
            self._expect(TokenType.RPAREN)
            return Constraint(type=ConstraintType.CHECK, value=expr)

        raise ParseError("Expected constraint", self._current)

    def _parse_create_index(self, unique: bool) -> CreateIndexStatement:
        """Parse a CREATE [UNIQUE] INDEX statement."""
        self._expect(TokenType.INDEX)

        if_not_exists = False
        if self._check(TokenType.IF):
            self._advance()
            self._expect(TokenType.NOT)
            self._expect(TokenType.EXISTS)
            if_not_exists = True

        name = self._expect(TokenType.IDENTIFIER, "Expected index name").value
        self._expect(TokenType.ON)
        table = self._expect(TokenType.IDENTIFIER, "Expected table name").value

        self._expect(TokenType.LPAREN)
        columns: list[str] = []
        while True:
            columns.append(self._expect(TokenType.IDENTIFIER).value)
            if not self._match(TokenType.COMMA):
                break
        self._expect(TokenType.RPAREN)

        return CreateIndexStatement(
            name=name,
            table=table,
            columns=columns,
            unique=unique,
            if_not_exists=if_not_exists,
        )

    def _parse_drop(self) -> DropTableStatement:
        """Parse a DROP TABLE statement."""
        self._expect(TokenType.DROP)
        self._expect(TokenType.TABLE)

        if_exists = False
        if self._check(TokenType.IF):
            self._advance()
            self._expect(TokenType.EXISTS)
            if_exists = True

        name = self._expect(TokenType.IDENTIFIER, "Expected table name").value

        cascade = False
        if self._match(TokenType.CASCADE):
            cascade = True
        elif self._match(TokenType.RESTRICT):
            cascade = False

        return DropTableStatement(name=name, if_exists=if_exists, cascade=cascade)

    def _parse_explain(self) -> ExplainStatement:
        """Parse an EXPLAIN statement."""
        self._expect(TokenType.EXPLAIN)
        stmt = self._parse_statement()
        return ExplainStatement(statement=stmt)

    def _parse_begin(self) -> BeginStatement:
        """Parse a BEGIN [TRANSACTION] statement."""
        self._expect(TokenType.BEGIN)
        self._match(TokenType.TRANSACTION)
        return BeginStatement()

    # ─── Table Reference Parsing ─────────────────────────────────────────────

    def _parse_table_ref(self) -> TableRef:
        """Parse a table reference, handling JOINs.

        This method handles the left-to-right associativity of JOINs:
            A JOIN B ON ... JOIN C ON ...
        is parsed as:
            (A JOIN B ON ...) JOIN C ON ...
        """
        left = self._parse_primary_table_ref()

        while self._check(
            TokenType.JOIN, TokenType.INNER, TokenType.LEFT,
            TokenType.RIGHT, TokenType.FULL, TokenType.CROSS,
            TokenType.NATURAL, TokenType.COMMA,
        ):
            # Implicit cross join via comma
            if self._match(TokenType.COMMA):
                right = self._parse_primary_table_ref()
                left = JoinTableRef(
                    left=left, right=right, join_type=JoinType.CROSS
                )
                continue

            join_type = self._parse_join_type()
            right = self._parse_primary_table_ref()

            # Join condition
            condition = None
            using_columns = None
            if self._match(TokenType.ON):
                condition = self._parse_expression()
            elif self._match(TokenType.USING):
                self._expect(TokenType.LPAREN)
                using_columns = []
                while True:
                    using_columns.append(self._expect(TokenType.IDENTIFIER).value)
                    if not self._match(TokenType.COMMA):
                        break
                self._expect(TokenType.RPAREN)

            left = JoinTableRef(
                left=left,
                right=right,
                join_type=join_type,
                condition=condition,
                using_columns=using_columns,
            )

        return left

    def _parse_primary_table_ref(self) -> TableRef:
        """Parse a primary table reference (simple name or subquery)."""
        if self._match(TokenType.LPAREN):
            # Subquery as table ref
            if self._check(TokenType.SELECT):
                subquery = self._parse_select()
                self._expect(TokenType.RPAREN)
                alias = ""
                if self._match(TokenType.AS):
                    alias = self._expect(TokenType.IDENTIFIER).value
                elif self._check(TokenType.IDENTIFIER):
                    alias = self._advance().value
                return SubqueryTableRef(subquery=subquery, alias=alias)
            else:
                raise ParseError("Expected SELECT in subquery", self._current)

        name = self._expect(TokenType.IDENTIFIER, "Expected table name").value
        alias = None
        if self._match(TokenType.AS):
            alias = self._expect(TokenType.IDENTIFIER).value
        elif self._check(TokenType.IDENTIFIER) and not self._check(
            TokenType.JOIN, TokenType.INNER, TokenType.LEFT, TokenType.RIGHT,
            TokenType.FULL, TokenType.CROSS, TokenType.NATURAL, TokenType.ON,
            TokenType.WHERE, TokenType.GROUP, TokenType.ORDER, TokenType.LIMIT,
            TokenType.HAVING, TokenType.SET,
        ):
            alias = self._advance().value

        return SimpleTableRef(name=name, alias=alias)

    def _parse_join_type(self) -> JoinType:
        """Parse the JOIN type keyword(s)."""
        if self._match(TokenType.NATURAL):
            self._match(TokenType.JOIN)
            return JoinType.NATURAL
        elif self._match(TokenType.CROSS):
            self._expect(TokenType.JOIN)
            return JoinType.CROSS
        elif self._match(TokenType.INNER):
            self._expect(TokenType.JOIN)
            return JoinType.INNER
        elif self._match(TokenType.LEFT):
            self._match(TokenType.OUTER)
            self._expect(TokenType.JOIN)
            return JoinType.LEFT
        elif self._match(TokenType.RIGHT):
            self._match(TokenType.OUTER)
            self._expect(TokenType.JOIN)
            return JoinType.RIGHT
        elif self._match(TokenType.FULL):
            self._match(TokenType.OUTER)
            self._expect(TokenType.JOIN)
            return JoinType.FULL
        elif self._match(TokenType.JOIN):
            return JoinType.INNER
        else:
            raise ParseError("Expected JOIN keyword", self._current)

    # ─── Expression Parsing (Precedence Climbing) ────────────────────────────

    def _parse_expression(self) -> Expression:
        """Parse an expression using recursive descent with correct precedence."""
        return self._parse_or_expr()

    def _parse_expression_list(self) -> list[Expression]:
        """Parse a comma-separated list of expressions."""
        exprs: list[Expression] = []
        while True:
            exprs.append(self._parse_expression())
            if not self._match(TokenType.COMMA):
                break
        return exprs

    def _parse_order_by_list(self) -> list[OrderByItem]:
        """Parse ORDER BY items: expr [ASC|DESC], ..."""
        items: list[OrderByItem] = []
        while True:
            expr = self._parse_expression()
            descending = False
            if self._match(TokenType.DESC):
                descending = True
            elif self._match(TokenType.ASC):
                descending = False
            items.append(OrderByItem(expr=expr, descending=descending))
            if not self._match(TokenType.COMMA):
                break
        return items

    def _parse_or_expr(self) -> Expression:
        """Parse OR expressions (lowest precedence binary operator)."""
        left = self._parse_and_expr()

        while self._match(TokenType.OR):
            right = self._parse_and_expr()
            left = BinaryOp(op=BinaryOpType.OR, left=left, right=right)

        return left

    def _parse_and_expr(self) -> Expression:
        """Parse AND expressions."""
        left = self._parse_not_expr()

        while self._match(TokenType.AND):
            right = self._parse_not_expr()
            left = BinaryOp(op=BinaryOpType.AND, left=left, right=right)

        return left

    def _parse_not_expr(self) -> Expression:
        """Parse NOT expressions."""
        if self._match(TokenType.NOT):
            operand = self._parse_not_expr()
            return UnaryOp(op=UnaryOpType.NOT, operand=operand)
        return self._parse_comparison()

    def _parse_comparison(self) -> Expression:
        """Parse comparison expressions and special forms (IS NULL, BETWEEN, IN, LIKE)."""
        left = self._parse_addition()

        # IS [NOT] NULL
        if self._check(TokenType.IS):
            self._advance()
            negated = bool(self._match(TokenType.NOT))
            self._expect(TokenType.NULL_LITERAL, "Expected NULL after IS")
            return IsNullExpr(expr=left, negated=negated)

        # [NOT] BETWEEN ... AND ...
        negated = False
        if self._check(TokenType.NOT):
            # Look ahead to see if it's NOT BETWEEN, NOT IN, NOT LIKE
            next_tok = self._peek()
            if next_tok.type in (TokenType.BETWEEN, TokenType.IN, TokenType.LIKE):
                self._advance()  # consume NOT
                negated = True

        if self._match(TokenType.BETWEEN):
            low = self._parse_addition()
            self._expect(TokenType.AND)
            high = self._parse_addition()
            return BetweenExpr(expr=left, low=low, high=high, negated=negated)

        if self._match(TokenType.IN):
            self._expect(TokenType.LPAREN)
            if self._check(TokenType.SELECT):
                subquery = self._parse_select()
                self._expect(TokenType.RPAREN)
                return InExpr(expr=left, values=[], subquery=subquery, negated=negated)
            else:
                values = self._parse_expression_list()
                self._expect(TokenType.RPAREN)
                return InExpr(expr=left, values=values, negated=negated)

        if self._match(TokenType.LIKE):
            pattern = self._parse_addition()
            return LikeExpr(expr=left, pattern=pattern, negated=negated)

        # Standard comparison operators
        op_map = {
            TokenType.EQUALS: BinaryOpType.EQ,
            TokenType.NOT_EQUALS: BinaryOpType.NEQ,
            TokenType.LESS: BinaryOpType.LT,
            TokenType.GREATER: BinaryOpType.GT,
            TokenType.LESS_EQUALS: BinaryOpType.LTE,
            TokenType.GREATER_EQUALS: BinaryOpType.GTE,
        }

        if self._current.type in op_map:
            op = op_map[self._current.type]
            self._advance()
            right = self._parse_addition()
            return BinaryOp(op=op, left=left, right=right)

        return left

    def _parse_addition(self) -> Expression:
        """Parse addition/subtraction/concatenation."""
        left = self._parse_multiplication()

        while True:
            if self._match(TokenType.PLUS):
                right = self._parse_multiplication()
                left = BinaryOp(op=BinaryOpType.ADD, left=left, right=right)
            elif self._match(TokenType.MINUS):
                right = self._parse_multiplication()
                left = BinaryOp(op=BinaryOpType.SUB, left=left, right=right)
            elif self._match(TokenType.CONCAT):
                right = self._parse_multiplication()
                left = BinaryOp(op=BinaryOpType.CONCAT, left=left, right=right)
            else:
                break

        return left

    def _parse_multiplication(self) -> Expression:
        """Parse multiplication/division/modulo."""
        left = self._parse_unary()

        while True:
            if self._match(TokenType.STAR):
                right = self._parse_unary()
                left = BinaryOp(op=BinaryOpType.MUL, left=left, right=right)
            elif self._match(TokenType.SLASH):
                right = self._parse_unary()
                left = BinaryOp(op=BinaryOpType.DIV, left=left, right=right)
            elif self._match(TokenType.PERCENT):
                right = self._parse_unary()
                left = BinaryOp(op=BinaryOpType.MOD, left=left, right=right)
            else:
                break

        return left

    def _parse_unary(self) -> Expression:
        """Parse unary minus."""
        if self._match(TokenType.MINUS):
            operand = self._parse_unary()
            return UnaryOp(op=UnaryOpType.NEGATE, operand=operand)
        return self._parse_primary()

    def _parse_primary(self) -> Expression:
        """Parse primary expressions: literals, identifiers, function calls, subqueries."""
        token = self._current

        # ── NULL literal ──
        if self._match(TokenType.NULL_LITERAL):
            return Literal(value=None, literal_type="null")

        # ── Boolean literals ──
        if token.type == TokenType.BOOLEAN_LITERAL:
            self._advance()
            return Literal(value=token.value == "TRUE", literal_type="boolean")

        # ── Integer literal ──
        if token.type == TokenType.INTEGER_LITERAL:
            self._advance()
            return Literal(value=int(token.value), literal_type="integer")

        # ── Float literal ──
        if token.type == TokenType.FLOAT_LITERAL:
            self._advance()
            return Literal(value=float(token.value), literal_type="float")

        # ── String literal ──
        if token.type == TokenType.STRING_LITERAL:
            self._advance()
            return Literal(value=token.value, literal_type="string")

        # ── CASE expression ──
        if self._match(TokenType.CASE):
            return self._parse_case_expr()

        # ── CAST expression ──
        if self._match(TokenType.CAST):
            return self._parse_cast_expr()

        # ── EXISTS subquery ──
        if self._match(TokenType.EXISTS):
            self._expect(TokenType.LPAREN)
            subquery = self._parse_select()
            self._expect(TokenType.RPAREN)
            return ExistsExpr(subquery=subquery)

        # ── Aggregate function calls ──
        if token.type in (
            TokenType.COUNT, TokenType.SUM, TokenType.AVG,
            TokenType.MIN, TokenType.MAX,
        ):
            return self._parse_aggregate_function()

        # ── Parenthesized expression or subquery ──
        if self._match(TokenType.LPAREN):
            if self._check(TokenType.SELECT):
                subquery = self._parse_select()
                self._expect(TokenType.RPAREN)
                return SubqueryExpr(subquery=subquery)
            else:
                expr = self._parse_expression()
                self._expect(TokenType.RPAREN)
                return expr

        # ── Identifier (column ref or function call) ──
        if self._check(TokenType.IDENTIFIER, TokenType.QUOTED_IDENTIFIER):
            name = self._advance().value

            # Function call: name(...)
            if self._match(TokenType.LPAREN):
                args: list[Expression] = []
                distinct = False
                if not self._check(TokenType.RPAREN):
                    if self._match(TokenType.DISTINCT):
                        distinct = True
                    args = self._parse_expression_list()
                self._expect(TokenType.RPAREN)
                return FunctionCall(name=name, args=args, distinct=distinct)

            # Qualified column reference: table.column
            if self._match(TokenType.DOT):
                col_token = self._advance()
                return ColumnRef(column=col_token.value, table=name)

            return ColumnRef(column=name)

        # ── Star (in aggregate context) ──
        if self._match(TokenType.STAR):
            return StarExpr()

        raise ParseError(f"Unexpected token in expression: {token.value}", token)

    def _parse_aggregate_function(self) -> FunctionCall:
        """Parse an aggregate function: COUNT(*), SUM(expr), etc."""
        name = self._advance().value  # COUNT, SUM, AVG, MIN, MAX
        self._expect(TokenType.LPAREN)

        args: list[Expression] = []
        distinct = False

        if self._check(TokenType.STAR):
            self._advance()
            args.append(StarExpr())
        else:
            if self._match(TokenType.DISTINCT):
                distinct = True
            if not self._check(TokenType.RPAREN):
                args = self._parse_expression_list()

        self._expect(TokenType.RPAREN)
        return FunctionCall(name=name, args=args, distinct=distinct, is_aggregate=True)

    def _parse_case_expr(self) -> CaseExpr:
        """Parse a CASE expression.

        Two forms:
        - Simple: CASE expr WHEN val1 THEN res1 ... END
        - Searched: CASE WHEN cond1 THEN res1 ... END
        """
        # Check for simple CASE (CASE expr WHEN ...)
        operand = None
        if not self._check(TokenType.WHEN):
            operand = self._parse_expression()

        when_clauses: list[tuple[Expression, Expression]] = []
        while self._match(TokenType.WHEN):
            condition = self._parse_expression()
            self._expect(TokenType.THEN)
            result = self._parse_expression()
            when_clauses.append((condition, result))

        else_clause = None
        if self._match(TokenType.ELSE):
            else_clause = self._parse_expression()

        self._expect(TokenType.END)

        return CaseExpr(
            operand=operand,
            when_clauses=when_clauses,
            else_clause=else_clause,
        )

    def _parse_cast_expr(self) -> CastExpr:
        """Parse CAST(expr AS type)."""
        self._expect(TokenType.LPAREN)
        expr = self._parse_expression()
        self._expect(TokenType.AS)

        # Parse type name (may include parameters like VARCHAR(100))
        type_token = self._advance()
        target_type = type_token.value
        if self._match(TokenType.LPAREN):
            param = self._expect(TokenType.INTEGER_LITERAL).value
            target_type += f"({param})"
            self._expect(TokenType.RPAREN)

        self._expect(TokenType.RPAREN)
        return CastExpr(expr=expr, target_type=target_type)
