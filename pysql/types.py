"""
SQL Data Type System.

Implements a complete type hierarchy for the database engine, including:
- Primitive types (INTEGER, FLOAT, BOOLEAN, TEXT, BLOB)
- Parameterized types (VARCHAR(n), CHAR(n), DECIMAL(p,s))
- Type coercion rules and compatibility checking
- Serialization/deserialization for storage layer integration

Each type knows its storage size, alignment requirements, and how to
serialize/deserialize values to/from raw bytes for the page-based storage engine.
"""

from __future__ import annotations

import struct
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any


class TypeId(Enum):
    """Enumeration of all supported SQL data types."""
    NULL = auto()
    BOOLEAN = auto()
    INTEGER = auto()      # 64-bit signed integer
    FLOAT = auto()        # 64-bit IEEE 754
    TEXT = auto()          # Variable-length UTF-8 string
    VARCHAR = auto()      # Length-limited variable string
    CHAR = auto()         # Fixed-length string
    BLOB = auto()         # Variable-length binary data
    DECIMAL = auto()      # Fixed-point decimal (precision, scale)
    TIMESTAMP = auto()    # Unix timestamp (64-bit integer)


class NullValue:
    """Singleton representing SQL NULL."""
    _instance = None

    def __new__(cls) -> "NullValue":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "NULL"

    def __bool__(self) -> bool:
        return False

    def __eq__(self, other: object) -> bool:
        return isinstance(other, NullValue)

    def __hash__(self) -> int:
        return hash(None)


NULL = NullValue()


@dataclass(frozen=True)
class DataType(ABC):
    """Abstract base class for all SQL data types.

    Each data type must define its serialization format, storage requirements,
    and comparison semantics. Types are immutable value objects.
    """
    type_id: TypeId

    @abstractmethod
    def storage_size(self, value: Any) -> int:
        """Return the number of bytes needed to store `value`.

        For fixed-size types, this is constant regardless of value.
        For variable-size types, this depends on the actual value.
        """
        ...

    @abstractmethod
    def serialize(self, value: Any) -> bytes:
        """Convert a Python value to its on-disk byte representation.

        The serialization format must be deterministic and support
        efficient comparison (ideally preserving sort order in bytes).
        """
        ...

    @abstractmethod
    def deserialize(self, data: bytes) -> Any:
        """Reconstruct a Python value from its byte representation."""
        ...

    @abstractmethod
    def validate(self, value: Any) -> bool:
        """Check whether `value` is a valid instance of this type."""
        ...

    def is_nullable(self) -> bool:
        """All types are nullable by default in SQL."""
        return True

    def is_compatible(self, other: "DataType") -> bool:
        """Check if values of `other` type can be implicitly cast to this type."""
        return self.type_id == other.type_id

    @abstractmethod
    def default_value(self) -> Any:
        """Return the default value for this type (used in DEFAULT clauses)."""
        ...


@dataclass(frozen=True)
class NullType(DataType):
    """The type of SQL NULL literals."""
    type_id: TypeId = field(default=TypeId.NULL, init=False)

    def storage_size(self, value: Any) -> int:
        return 0

    def serialize(self, value: Any) -> bytes:
        return b""

    def deserialize(self, data: bytes) -> Any:
        return NULL

    def validate(self, value: Any) -> bool:
        return isinstance(value, NullValue)

    def default_value(self) -> Any:
        return NULL


@dataclass(frozen=True)
class BooleanType(DataType):
    """SQL BOOLEAN type. Stored as a single byte (0x00 or 0x01)."""
    type_id: TypeId = field(default=TypeId.BOOLEAN, init=False)

    def storage_size(self, value: Any) -> int:
        return 1

    def serialize(self, value: Any) -> bytes:
        return struct.pack("?", bool(value))

    def deserialize(self, data: bytes) -> Any:
        return struct.unpack("?", data[:1])[0]

    def validate(self, value: Any) -> bool:
        return isinstance(value, (bool, int))

    def default_value(self) -> Any:
        return False

    def is_compatible(self, other: DataType) -> bool:
        return other.type_id in (TypeId.BOOLEAN, TypeId.INTEGER)


@dataclass(frozen=True)
class IntegerType(DataType):
    """SQL INTEGER type. 64-bit signed integer stored in big-endian format.

    Big-endian encoding is used so that byte-level comparison preserves
    numeric ordering — critical for B+Tree index key comparisons.
    We use an offset encoding (XOR with sign bit) so that negative numbers
    sort correctly in unsigned byte comparison.
    """
    type_id: TypeId = field(default=TypeId.INTEGER, init=False)

    def storage_size(self, value: Any) -> int:
        return 8

    def serialize(self, value: Any) -> bytes:
        # Offset binary encoding: flip the sign bit so that
        # signed integers sort correctly as unsigned bytes
        v = int(value)
        encoded = v ^ (1 << 63)  # flip sign bit for sort-preserving encoding
        return struct.pack(">Q", encoded)

    def deserialize(self, data: bytes) -> Any:
        encoded = struct.unpack(">Q", data[:8])[0]
        return encoded ^ (1 << 63)

    def validate(self, value: Any) -> bool:
        if isinstance(value, bool):
            return False
        return isinstance(value, int) and -(2**63) <= value < 2**63

    def default_value(self) -> Any:
        return 0

    def is_compatible(self, other: DataType) -> bool:
        return other.type_id in (TypeId.INTEGER, TypeId.BOOLEAN, TypeId.FLOAT)


@dataclass(frozen=True)
class FloatType(DataType):
    """SQL FLOAT/DOUBLE type. 64-bit IEEE 754 double precision.

    Uses a modified encoding that preserves sort order in byte comparisons:
    - Positive floats: flip the sign bit
    - Negative floats: flip all bits
    This ensures correct ordering for B+Tree index scans.
    """
    type_id: TypeId = field(default=TypeId.FLOAT, init=False)

    def storage_size(self, value: Any) -> int:
        return 8

    def serialize(self, value: Any) -> bytes:
        raw = struct.pack(">d", float(value))
        # Sort-preserving encoding for floating point
        int_val = int.from_bytes(raw, "big")
        if int_val >> 63:  # negative
            int_val = int_val ^ 0xFFFFFFFFFFFFFFFF  # flip all bits
        else:
            int_val = int_val ^ (1 << 63)  # flip sign bit only
        return int_val.to_bytes(8, "big")

    def deserialize(self, data: bytes) -> Any:
        int_val = int.from_bytes(data[:8], "big")
        if int_val >> 63:  # was positive (sign bit is now 1)
            int_val = int_val ^ (1 << 63)
        else:
            int_val = int_val ^ 0xFFFFFFFFFFFFFFFF
        raw = int_val.to_bytes(8, "big")
        return struct.unpack(">d", raw)[0]

    def validate(self, value: Any) -> bool:
        return isinstance(value, (int, float)) and not isinstance(value, bool)

    def default_value(self) -> Any:
        return 0.0

    def is_compatible(self, other: DataType) -> bool:
        return other.type_id in (TypeId.FLOAT, TypeId.INTEGER)


@dataclass(frozen=True)
class TextType(DataType):
    """SQL TEXT type. Variable-length UTF-8 encoded string.

    Storage format: [4-byte length prefix (big-endian)] [UTF-8 data]
    """
    type_id: TypeId = field(default=TypeId.TEXT, init=False)

    def storage_size(self, value: Any) -> int:
        return 4 + len(str(value).encode("utf-8"))

    def serialize(self, value: Any) -> bytes:
        encoded = str(value).encode("utf-8")
        return struct.pack(">I", len(encoded)) + encoded

    def deserialize(self, data: bytes) -> Any:
        length = struct.unpack(">I", data[:4])[0]
        return data[4:4 + length].decode("utf-8")

    def validate(self, value: Any) -> bool:
        return isinstance(value, str)

    def default_value(self) -> Any:
        return ""


@dataclass(frozen=True)
class VarCharType(DataType):
    """SQL VARCHAR(n) type. Variable-length string with maximum length constraint.

    Storage format: [2-byte length prefix] [UTF-8 data (up to max_length bytes)]
    Uses 2-byte length prefix to save space for shorter strings.
    """
    type_id: TypeId = field(default=TypeId.VARCHAR, init=False)
    max_length: int = 255

    def storage_size(self, value: Any) -> int:
        return 2 + min(len(str(value).encode("utf-8")), self.max_length)

    def serialize(self, value: Any) -> bytes:
        encoded = str(value).encode("utf-8")[:self.max_length]
        return struct.pack(">H", len(encoded)) + encoded

    def deserialize(self, data: bytes) -> Any:
        length = struct.unpack(">H", data[:2])[0]
        return data[2:2 + length].decode("utf-8")

    def validate(self, value: Any) -> bool:
        return isinstance(value, str) and len(value.encode("utf-8")) <= self.max_length

    def default_value(self) -> Any:
        return ""

    def is_compatible(self, other: DataType) -> bool:
        return other.type_id in (TypeId.VARCHAR, TypeId.TEXT, TypeId.CHAR)


@dataclass(frozen=True)
class CharType(DataType):
    """SQL CHAR(n) type. Fixed-length string, right-padded with spaces.

    Storage format: [length bytes of UTF-8 data, padded with 0x20]
    No length prefix needed since the size is fixed and known from the schema.
    """
    type_id: TypeId = field(default=TypeId.CHAR, init=False)
    length: int = 1

    def storage_size(self, value: Any) -> int:
        return self.length

    def serialize(self, value: Any) -> bytes:
        encoded = str(value).encode("utf-8")[:self.length]
        return encoded.ljust(self.length, b"\x20")

    def deserialize(self, data: bytes) -> Any:
        return data[:self.length].decode("utf-8").rstrip()

    def validate(self, value: Any) -> bool:
        return isinstance(value, str) and len(value.encode("utf-8")) <= self.length

    def default_value(self) -> Any:
        return ""

    def is_compatible(self, other: DataType) -> bool:
        return other.type_id in (TypeId.CHAR, TypeId.VARCHAR, TypeId.TEXT)


@dataclass(frozen=True)
class BlobType(DataType):
    """SQL BLOB type. Variable-length binary data.

    Storage format: [4-byte length prefix (big-endian)] [raw binary data]
    """
    type_id: TypeId = field(default=TypeId.BLOB, init=False)

    def storage_size(self, value: Any) -> int:
        return 4 + len(bytes(value))

    def serialize(self, value: Any) -> bytes:
        raw = bytes(value) if not isinstance(value, bytes) else value
        return struct.pack(">I", len(raw)) + raw

    def deserialize(self, data: bytes) -> Any:
        length = struct.unpack(">I", data[:4])[0]
        return data[4:4 + length]

    def validate(self, value: Any) -> bool:
        return isinstance(value, (bytes, bytearray, memoryview))

    def default_value(self) -> Any:
        return b""


@dataclass(frozen=True)
class TimestampType(DataType):
    """SQL TIMESTAMP type. Stored as 64-bit Unix timestamp (microseconds).

    This gives microsecond precision while maintaining sort-preserving
    byte encoding (same as IntegerType).
    """
    type_id: TypeId = field(default=TypeId.TIMESTAMP, init=False)

    def storage_size(self, value: Any) -> int:
        return 8

    def serialize(self, value: Any) -> bytes:
        v = int(value)
        encoded = v ^ (1 << 63)
        return struct.pack(">Q", encoded)

    def deserialize(self, data: bytes) -> Any:
        encoded = struct.unpack(">Q", data[:8])[0]
        return encoded ^ (1 << 63)

    def validate(self, value: Any) -> bool:
        return isinstance(value, (int, float))

    def default_value(self) -> Any:
        return 0


# ─── Type Coercion Engine ─────────────────────────────────────────────────────

# Implicit coercion precedence: higher index = higher precedence
_TYPE_PRECEDENCE: dict[TypeId, int] = {
    TypeId.NULL: 0,
    TypeId.BOOLEAN: 1,
    TypeId.INTEGER: 2,
    TypeId.FLOAT: 3,
    TypeId.CHAR: 4,
    TypeId.VARCHAR: 5,
    TypeId.TEXT: 6,
    TypeId.BLOB: 7,
    TypeId.TIMESTAMP: 8,
}

# Allowed implicit coercions: (from_type, to_type)
_COERCION_RULES: set[tuple[TypeId, TypeId]] = {
    (TypeId.BOOLEAN, TypeId.INTEGER),
    (TypeId.INTEGER, TypeId.FLOAT),
    (TypeId.CHAR, TypeId.VARCHAR),
    (TypeId.CHAR, TypeId.TEXT),
    (TypeId.VARCHAR, TypeId.TEXT),
    (TypeId.INTEGER, TypeId.TIMESTAMP),
}


def can_coerce(from_type: DataType, to_type: DataType) -> bool:
    """Check if implicit coercion from one type to another is allowed."""
    if from_type.type_id == TypeId.NULL:
        return True  # NULL can be coerced to any type
    if from_type.type_id == to_type.type_id:
        return True
    return (from_type.type_id, to_type.type_id) in _COERCION_RULES


def coerce_value(value: Any, from_type: DataType, to_type: DataType) -> Any:
    """Attempt to coerce a value from one type to another.

    Raises TypeError if the coercion is not supported.
    """
    if isinstance(value, NullValue):
        return NULL

    if from_type.type_id == to_type.type_id:
        return value

    if not can_coerce(from_type, to_type):
        raise TypeError(
            f"Cannot implicitly coerce {from_type.type_id.name} to {to_type.type_id.name}"
        )

    coercion_key = (from_type.type_id, to_type.type_id)

    if coercion_key == (TypeId.BOOLEAN, TypeId.INTEGER):
        return int(value)
    elif coercion_key == (TypeId.INTEGER, TypeId.FLOAT):
        return float(value)
    elif coercion_key in (
        (TypeId.CHAR, TypeId.VARCHAR),
        (TypeId.CHAR, TypeId.TEXT),
        (TypeId.VARCHAR, TypeId.TEXT),
    ):
        return str(value)
    elif coercion_key == (TypeId.INTEGER, TypeId.TIMESTAMP):
        return int(value)

    raise TypeError(f"Unsupported coercion: {coercion_key}")


def resolve_common_type(type_a: DataType, type_b: DataType) -> DataType:
    """Find the common supertype for two types (used in UNION, CASE, etc.).

    Returns the type with higher precedence if coercion is possible.
    Raises TypeError if the types are incompatible.
    """
    if type_a.type_id == TypeId.NULL:
        return type_b
    if type_b.type_id == TypeId.NULL:
        return type_a
    if type_a.type_id == type_b.type_id:
        return type_a

    prec_a = _TYPE_PRECEDENCE.get(type_a.type_id, -1)
    prec_b = _TYPE_PRECEDENCE.get(type_b.type_id, -1)

    if prec_a >= prec_b and can_coerce(type_b, type_a):
        return type_a
    elif can_coerce(type_a, type_b):
        return type_b

    raise TypeError(
        f"Incompatible types: {type_a.type_id.name} and {type_b.type_id.name}"
    )


def type_from_string(type_str: str) -> DataType:
    """Parse a SQL type string into a DataType instance.

    Examples:
        'INTEGER' -> IntegerType()
        'VARCHAR(100)' -> VarCharType(max_length=100)
        'CHAR(10)' -> CharType(length=10)
    """
    type_str = type_str.strip().upper()

    if type_str == "INTEGER" or type_str == "INT":
        return IntegerType()
    elif type_str == "FLOAT" or type_str == "DOUBLE" or type_str == "REAL":
        return FloatType()
    elif type_str == "BOOLEAN" or type_str == "BOOL":
        return BooleanType()
    elif type_str == "TEXT":
        return TextType()
    elif type_str == "BLOB":
        return BlobType()
    elif type_str == "TIMESTAMP":
        return TimestampType()
    elif type_str.startswith("VARCHAR"):
        # Parse VARCHAR(n)
        if "(" in type_str:
            n = int(type_str.split("(")[1].rstrip(")"))
            return VarCharType(max_length=n)
        return VarCharType()
    elif type_str.startswith("CHAR"):
        if "(" in type_str:
            n = int(type_str.split("(")[1].rstrip(")"))
            return CharType(length=n)
        return CharType()
    else:
        raise ValueError(f"Unknown type: {type_str}")
