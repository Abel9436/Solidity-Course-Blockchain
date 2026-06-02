"""
SQL Lexer — Tokenizer for the SQL language.

Converts a raw SQL string into a stream of typed tokens using a hand-written
state-machine lexer. This approach (vs regex-based) gives us:
- Better error messages with exact positions
- Proper handling of string escaping and numeric literals
- Single-pass O(n) tokenization

Token types include keywords, identifiers, literals, operators, and punctuation.
The lexer handles:
- SQL keywords (SELECT, INSERT, UPDATE, DELETE, CREATE, DROP, etc.)
- String literals with single-quote escaping ('it''s')
- Numeric literals (integers and floats, including scientific notation)
- Multi-character operators (>=, <=, !=, <>)
- Quoted identifiers ("column name with spaces")
- Comments (-- line comments and /* block comments */)
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum, auto
from typing import Iterator


class TokenType(Enum):
    """All token types recognized by the SQL lexer."""

    # ── Literals ──
    INTEGER_LITERAL = auto()
    FLOAT_LITERAL = auto()
    STRING_LITERAL = auto()
    BOOLEAN_LITERAL = auto()
    NULL_LITERAL = auto()

    # ── Identifiers ──
    IDENTIFIER = auto()
    QUOTED_IDENTIFIER = auto()

    # ── Keywords ──
    SELECT = auto()
    FROM = auto()
    WHERE = auto()
    INSERT = auto()
    INTO = auto()
    VALUES = auto()
    UPDATE = auto()
    SET = auto()
    DELETE = auto()
    CREATE = auto()
    TABLE = auto()
    DROP = auto()
    ALTER = auto()
    ADD = auto()
    COLUMN = auto()
    INDEX = auto()
    ON = auto()
    PRIMARY = auto()
    KEY = auto()
    FOREIGN = auto()
    REFERENCES = auto()
    UNIQUE = auto()
    NOT = auto()
    DEFAULT = auto()
    NULL = auto()
    AND = auto()
    OR = auto()
    IN = auto()
    BETWEEN = auto()
    LIKE = auto()
    IS = auto()
    AS = auto()
    JOIN = auto()
    INNER = auto()
    LEFT = auto()
    RIGHT = auto()
    OUTER = auto()
    CROSS = auto()
    FULL = auto()
    ORDER = auto()
    BY = auto()
    ASC = auto()
    DESC = auto()
    GROUP = auto()
    HAVING = auto()
    LIMIT = auto()
    OFFSET = auto()
    DISTINCT = auto()
    ALL = auto()
    EXISTS = auto()
    CASE = auto()
    WHEN = auto()
    THEN = auto()
    ELSE = auto()
    END = auto()
    CAST = auto()
    BEGIN = auto()
    COMMIT = auto()
    ROLLBACK = auto()
    TRANSACTION = auto()
    IF = auto()
    EXPLAIN = auto()
    UNION = auto()
    INTERSECT = auto()
    EXCEPT = auto()
    TRUE = auto()
    FALSE = auto()
    CHECK = auto()
    CONSTRAINT = auto()
    CASCADE = auto()
    RESTRICT = auto()
    AUTOINCREMENT = auto()
    COUNT = auto()
    SUM = auto()
    AVG = auto()
    MIN = auto()
    MAX = auto()
    USING = auto()
    NATURAL = auto()

    # ── Operators ──
    PLUS = auto()          # +
    MINUS = auto()         # -
    STAR = auto()          # *
    SLASH = auto()         # /
    PERCENT = auto()       # %
    EQUALS = auto()        # =
    NOT_EQUALS = auto()    # != or <>
    LESS = auto()          # <
    GREATER = auto()       # >
    LESS_EQUALS = auto()   # <=
    GREATER_EQUALS = auto()  # >=
    CONCAT = auto()        # ||

    # ── Punctuation ──
    LPAREN = auto()        # (
    RPAREN = auto()        # )
    COMMA = auto()         # ,
    SEMICOLON = auto()     # ;
    DOT = auto()           # .

    # ── Special ──
    EOF = auto()
    INVALID = auto()


# Map of SQL keywords to their token types
_KEYWORDS: dict[str, TokenType] = {
    "SELECT": TokenType.SELECT,
    "FROM": TokenType.FROM,
    "WHERE": TokenType.WHERE,
    "INSERT": TokenType.INSERT,
    "INTO": TokenType.INTO,
    "VALUES": TokenType.VALUES,
    "UPDATE": TokenType.UPDATE,
    "SET": TokenType.SET,
    "DELETE": TokenType.DELETE,
    "CREATE": TokenType.CREATE,
    "TABLE": TokenType.TABLE,
    "DROP": TokenType.DROP,
    "ALTER": TokenType.ALTER,
    "ADD": TokenType.ADD,
    "COLUMN": TokenType.COLUMN,
    "INDEX": TokenType.INDEX,
    "ON": TokenType.ON,
    "PRIMARY": TokenType.PRIMARY,
    "KEY": TokenType.KEY,
    "FOREIGN": TokenType.FOREIGN,
    "REFERENCES": TokenType.REFERENCES,
    "UNIQUE": TokenType.UNIQUE,
    "NOT": TokenType.NOT,
    "DEFAULT": TokenType.DEFAULT,
    "NULL": TokenType.NULL_LITERAL,
    "AND": TokenType.AND,
    "OR": TokenType.OR,
    "IN": TokenType.IN,
    "BETWEEN": TokenType.BETWEEN,
    "LIKE": TokenType.LIKE,
    "IS": TokenType.IS,
    "AS": TokenType.AS,
    "JOIN": TokenType.JOIN,
    "INNER": TokenType.INNER,
    "LEFT": TokenType.LEFT,
    "RIGHT": TokenType.RIGHT,
    "OUTER": TokenType.OUTER,
    "CROSS": TokenType.CROSS,
    "FULL": TokenType.FULL,
    "ORDER": TokenType.ORDER,
    "BY": TokenType.BY,
    "ASC": TokenType.ASC,
    "DESC": TokenType.DESC,
    "GROUP": TokenType.GROUP,
    "HAVING": TokenType.HAVING,
    "LIMIT": TokenType.LIMIT,
    "OFFSET": TokenType.OFFSET,
    "DISTINCT": TokenType.DISTINCT,
    "ALL": TokenType.ALL,
    "EXISTS": TokenType.EXISTS,
    "CASE": TokenType.CASE,
    "WHEN": TokenType.WHEN,
    "THEN": TokenType.THEN,
    "ELSE": TokenType.ELSE,
    "END": TokenType.END,
    "CAST": TokenType.CAST,
    "BEGIN": TokenType.BEGIN,
    "COMMIT": TokenType.COMMIT,
    "ROLLBACK": TokenType.ROLLBACK,
    "TRANSACTION": TokenType.TRANSACTION,
    "IF": TokenType.IF,
    "EXPLAIN": TokenType.EXPLAIN,
    "UNION": TokenType.UNION,
    "INTERSECT": TokenType.INTERSECT,
    "EXCEPT": TokenType.EXCEPT,
    "TRUE": TokenType.TRUE,
    "FALSE": TokenType.FALSE,
    "CHECK": TokenType.CHECK,
    "CONSTRAINT": TokenType.CONSTRAINT,
    "CASCADE": TokenType.CASCADE,
    "RESTRICT": TokenType.RESTRICT,
    "AUTOINCREMENT": TokenType.AUTOINCREMENT,
    "COUNT": TokenType.COUNT,
    "SUM": TokenType.SUM,
    "AVG": TokenType.AVG,
    "MIN": TokenType.MIN,
    "MAX": TokenType.MAX,
    "USING": TokenType.USING,
    "NATURAL": TokenType.NATURAL,
}


@dataclass(frozen=True)
class Token:
    """A single token produced by the lexer.

    Attributes:
        type: The token's type classification
        value: The raw string value of the token
        line: Line number (1-indexed) where the token starts
        column: Column number (1-indexed) where the token starts
    """
    type: TokenType
    value: str
    line: int
    column: int

    def __repr__(self) -> str:
        return f"Token({self.type.name}, {self.value!r}, L{self.line}:C{self.column})"


class LexerError(Exception):
    """Raised when the lexer encounters invalid input."""

    def __init__(self, message: str, line: int, column: int) -> None:
        self.line = line
        self.column = column
        super().__init__(f"Lexer error at L{line}:C{column}: {message}")


class Lexer:
    """Hand-written state-machine SQL tokenizer.

    Usage:
        lexer = Lexer("SELECT * FROM users WHERE age > 18")
        tokens = list(lexer.tokenize())
    """

    def __init__(self, source: str) -> None:
        self._source = source
        self._pos = 0
        self._line = 1
        self._column = 1
        self._tokens: list[Token] = []

    @property
    def _current(self) -> str:
        """The character at the current position, or '\\0' if at end."""
        if self._pos >= len(self._source):
            return "\0"
        return self._source[self._pos]

    def _peek(self, offset: int = 1) -> str:
        """Look ahead at a future character without consuming it."""
        idx = self._pos + offset
        if idx >= len(self._source):
            return "\0"
        return self._source[idx]

    def _advance(self) -> str:
        """Consume and return the current character, updating position tracking."""
        ch = self._current
        self._pos += 1
        if ch == "\n":
            self._line += 1
            self._column = 1
        else:
            self._column += 1
        return ch

    def _skip_whitespace(self) -> None:
        """Skip whitespace characters."""
        while self._pos < len(self._source) and self._current in " \t\r\n":
            self._advance()

    def _skip_line_comment(self) -> None:
        """Skip a -- line comment."""
        while self._pos < len(self._source) and self._current != "\n":
            self._advance()

    def _skip_block_comment(self) -> None:
        """Skip a /* ... */ block comment, handling nesting."""
        depth = 1
        self._advance()  # skip /
        self._advance()  # skip *
        while self._pos < len(self._source) and depth > 0:
            if self._current == "/" and self._peek() == "*":
                depth += 1
                self._advance()
                self._advance()
            elif self._current == "*" and self._peek() == "/":
                depth -= 1
                self._advance()
                self._advance()
            else:
                self._advance()
        if depth > 0:
            raise LexerError("Unterminated block comment", self._line, self._column)

    def _read_string(self) -> Token:
        """Read a single-quoted string literal, handling '' escape sequences.

        SQL uses doubled single quotes for escaping: 'it''s' → it's
        """
        start_line = self._line
        start_col = self._column
        self._advance()  # skip opening quote

        chars: list[str] = []
        while self._pos < len(self._source):
            if self._current == "'":
                if self._peek() == "'":
                    # Escaped quote
                    chars.append("'")
                    self._advance()
                    self._advance()
                else:
                    # End of string
                    self._advance()
                    return Token(TokenType.STRING_LITERAL, "".join(chars), start_line, start_col)
            else:
                chars.append(self._advance())

        raise LexerError("Unterminated string literal", start_line, start_col)

    def _read_number(self) -> Token:
        """Read a numeric literal (integer or float).

        Handles:
        - Plain integers: 42
        - Floats: 3.14
        - Scientific notation: 1.5e10, 2E-3
        """
        start_line = self._line
        start_col = self._column
        chars: list[str] = []
        is_float = False

        # Read integer part
        while self._pos < len(self._source) and self._current.isdigit():
            chars.append(self._advance())

        # Decimal point
        if self._current == "." and self._peek().isdigit():
            is_float = True
            chars.append(self._advance())  # consume '.'
            while self._pos < len(self._source) and self._current.isdigit():
                chars.append(self._advance())

        # Scientific notation
        if self._current in ("e", "E"):
            is_float = True
            chars.append(self._advance())
            if self._current in ("+", "-"):
                chars.append(self._advance())
            if not self._current.isdigit():
                raise LexerError("Invalid numeric literal", start_line, start_col)
            while self._pos < len(self._source) and self._current.isdigit():
                chars.append(self._advance())

        value = "".join(chars)
        token_type = TokenType.FLOAT_LITERAL if is_float else TokenType.INTEGER_LITERAL
        return Token(token_type, value, start_line, start_col)

    def _read_identifier_or_keyword(self) -> Token:
        """Read an identifier or keyword.

        SQL identifiers start with a letter or underscore, followed by
        letters, digits, or underscores. Keywords are case-insensitive.
        """
        start_line = self._line
        start_col = self._column
        chars: list[str] = []

        while self._pos < len(self._source) and (
            self._current.isalnum() or self._current == "_"
        ):
            chars.append(self._advance())

        value = "".join(chars)
        upper = value.upper()

        # Check for keywords
        if upper in _KEYWORDS:
            token_type = _KEYWORDS[upper]
            # Handle boolean literals specially
            if upper == "TRUE":
                return Token(TokenType.BOOLEAN_LITERAL, "TRUE", start_line, start_col)
            elif upper == "FALSE":
                return Token(TokenType.BOOLEAN_LITERAL, "FALSE", start_line, start_col)
            return Token(token_type, upper, start_line, start_col)

        return Token(TokenType.IDENTIFIER, value, start_line, start_col)

    def _read_quoted_identifier(self) -> Token:
        """Read a double-quoted identifier: "column name"."""
        start_line = self._line
        start_col = self._column
        self._advance()  # skip opening "

        chars: list[str] = []
        while self._pos < len(self._source) and self._current != '"':
            chars.append(self._advance())

        if self._pos >= len(self._source):
            raise LexerError("Unterminated quoted identifier", start_line, start_col)

        self._advance()  # skip closing "
        return Token(TokenType.QUOTED_IDENTIFIER, "".join(chars), start_line, start_col)

    def tokenize(self) -> list[Token]:
        """Tokenize the entire source string and return a list of tokens.

        The list always ends with an EOF token.
        """
        tokens: list[Token] = []

        while self._pos < len(self._source):
            self._skip_whitespace()
            if self._pos >= len(self._source):
                break

            start_line = self._line
            start_col = self._column
            ch = self._current

            # ── Comments ──
            if ch == "-" and self._peek() == "-":
                self._skip_line_comment()
                continue
            if ch == "/" and self._peek() == "*":
                self._skip_block_comment()
                continue

            # ── String literals ──
            if ch == "'":
                tokens.append(self._read_string())
                continue

            # ── Numeric literals ──
            if ch.isdigit():
                tokens.append(self._read_number())
                continue

            # ── Identifiers and keywords ──
            if ch.isalpha() or ch == "_":
                tokens.append(self._read_identifier_or_keyword())
                continue

            # ── Quoted identifiers ──
            if ch == '"':
                tokens.append(self._read_quoted_identifier())
                continue

            # ── Two-character operators ──
            two_char = ch + self._peek()
            if two_char == "!=":
                self._advance()
                self._advance()
                tokens.append(Token(TokenType.NOT_EQUALS, "!=", start_line, start_col))
                continue
            if two_char == "<>":
                self._advance()
                self._advance()
                tokens.append(Token(TokenType.NOT_EQUALS, "<>", start_line, start_col))
                continue
            if two_char == "<=":
                self._advance()
                self._advance()
                tokens.append(Token(TokenType.LESS_EQUALS, "<=", start_line, start_col))
                continue
            if two_char == ">=":
                self._advance()
                self._advance()
                tokens.append(Token(TokenType.GREATER_EQUALS, ">=", start_line, start_col))
                continue
            if two_char == "||":
                self._advance()
                self._advance()
                tokens.append(Token(TokenType.CONCAT, "||", start_line, start_col))
                continue

            # ── Single-character operators and punctuation ──
            single_char_map: dict[str, TokenType] = {
                "+": TokenType.PLUS,
                "-": TokenType.MINUS,
                "*": TokenType.STAR,
                "/": TokenType.SLASH,
                "%": TokenType.PERCENT,
                "=": TokenType.EQUALS,
                "<": TokenType.LESS,
                ">": TokenType.GREATER,
                "(": TokenType.LPAREN,
                ")": TokenType.RPAREN,
                ",": TokenType.COMMA,
                ";": TokenType.SEMICOLON,
                ".": TokenType.DOT,
            }

            if ch in single_char_map:
                self._advance()
                tokens.append(Token(single_char_map[ch], ch, start_line, start_col))
                continue

            raise LexerError(f"Unexpected character: {ch!r}", start_line, start_col)

        tokens.append(Token(TokenType.EOF, "", self._line, self._column))
        return tokens

    def tokenize_iter(self) -> Iterator[Token]:
        """Lazy iterator version of tokenize() for streaming parsers."""
        yield from self.tokenize()
