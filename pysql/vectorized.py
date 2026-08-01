"""
Vectorized Query Execution & Columnar Engine.

Implements Apache Arrow / DuckDB / Velox style vectorized batch processing.
Instead of the traditional Volcano (tuple-at-a-time) model, the vectorized
engine processes data in tight loops over column vectors (RecordBatch).

Key Benefits:
- Data locality: Contiguous column arrays fit in CPU L1/L2 cache
- Reduced instruction overhead: 1 virtual method call per 2048 rows (vs 1 per row)
- SIMD-friendly loop structures for arithmetic and comparison operations
- Bit-packed validity vectors (null masks)

Architecture:
    RecordBatch (Columnar data container)
      ├─ Int64Vector    (64-bit integer array)
      ├─ Float64Vector  (64-bit float array)
      ├─ StringVector   (UTF-8 offset array)
      └─ ValidityMask   (Bit-packed null mask)

Vectorized Physical Operators:
    VectorizedScan → VectorizedFilter → VectorizedProject → VectorizedAggregate
"""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Iterator, Optional, Sequence

from .ast_nodes import BinaryOp, BinaryOpType, ColumnRef, Expression, Literal
from .types import NULL, DataType, NullValue


# Default vector batch size (matches Arrow / DuckDB defaults)
VECTOR_SIZE = 2048


# ─── Column Vectors ─────────────────────────────────────────────────────────

class ColumnVector(ABC):
    """Abstract base class for high-performance columnar vectors."""

    def __init__(self, capacity: int = VECTOR_SIZE) -> None:
        self._capacity = capacity
        self._length = 0
        self._null_mask = bytearray(math.ceil(capacity / 8))  # Bit-packed null mask

    @property
    def length(self) -> int:
        return self._length

    @length.setter
    def length(self, val: int) -> None:
        self._length = val

    def is_null(self, index: int) -> bool:
        """Check if the value at `index` is NULL using bit-packed mask."""
        byte_idx = index >> 3
        bit_idx = index & 7
        return bool(self._null_mask[byte_idx] & (1 << bit_idx))

    def set_null(self, index: int) -> None:
        """Mark `index` as NULL in the validity mask."""
        byte_idx = index >> 3
        bit_idx = index & 7
        self._null_mask[byte_idx] |= (1 << bit_idx)

    @abstractmethod
    def get(self, index: int) -> Any:
        """Get the value at `index` (returns NULL if null)."""
        ...

    @abstractmethod
    def append(self, value: Any) -> None:
        """Append a value to the vector."""
        ...

    @abstractmethod
    def slice(self, selection_vector: list[int]) -> ColumnVector:
        """Produce a new vector containing only the indices in `selection_vector`."""
        ...


class Int64Vector(ColumnVector):
    """Dense array of 64-bit integers."""

    def __init__(self, capacity: int = VECTOR_SIZE) -> None:
        super().__init__(capacity)
        self._data: list[int] = [0] * capacity

    def get(self, index: int) -> Any:
        if self.is_null(index):
            return NULL
        return self._data[index]

    def append(self, value: Any) -> None:
        if isinstance(value, NullValue) or value is None:
            self.set_null(self._length)
        else:
            self._data[self._length] = int(value)
        self._length += 1

    def slice(self, selection_vector: list[int]) -> Int64Vector:
        res = Int64Vector(len(selection_vector))
        for idx in selection_vector:
            res.append(self.get(idx))
        return res

    def vec_add(self, other: Int64Vector) -> Int64Vector:
        """Vectorized addition: self + other."""
        res = Int64Vector(self._length)
        res.length = self._length
        for i in range(self._length):
            if self.is_null(i) or other.is_null(i):
                res.set_null(i)
            else:
                res._data[i] = self._data[i] + other._data[i]
        return res

    def vec_gt(self, scalar: int) -> list[int]:
        """Vectorized comparison: self > scalar. Returns matching indices."""
        matched = []
        for i in range(self._length):
            if not self.is_null(i) and self._data[i] > scalar:
                matched.append(i)
        return matched


class Float64Vector(ColumnVector):
    """Dense array of 64-bit floating point numbers."""

    def __init__(self, capacity: int = VECTOR_SIZE) -> None:
        super().__init__(capacity)
        self._data: list[float] = [0.0] * capacity

    def get(self, index: int) -> Any:
        if self.is_null(index):
            return NULL
        return self._data[index]

    def append(self, value: Any) -> None:
        if isinstance(value, NullValue) or value is None:
            self.set_null(self._length)
        else:
            self._data[self._length] = float(value)
        self._length += 1

    def slice(self, selection_vector: list[int]) -> Float64Vector:
        res = Float64Vector(len(selection_vector))
        for idx in selection_vector:
            res.append(self.get(idx))
        return res


class StringVector(ColumnVector):
    """Offset-based UTF-8 string vector for cache efficiency."""

    def __init__(self, capacity: int = VECTOR_SIZE) -> None:
        super().__init__(capacity)
        self._data: list[str] = [""] * capacity

    def get(self, index: int) -> Any:
        if self.is_null(index):
            return NULL
        return self._data[index]

    def append(self, value: Any) -> None:
        if isinstance(value, NullValue) or value is None:
            self.set_null(self._length)
        else:
            self._data[self._length] = str(value)
        self._length += 1

    def slice(self, selection_vector: list[int]) -> StringVector:
        res = StringVector(len(selection_vector))
        for idx in selection_vector:
            res.append(self.get(idx))
        return res


# ─── RecordBatch ─────────────────────────────────────────────────────────────

@dataclass
class RecordBatch:
    """A columnar batch of up to VECTOR_SIZE rows.

    Contains named column vectors and a row count.
    """
    schema: list[str]
    vectors: list[ColumnVector]
    num_rows: int

    @classmethod
    def from_rows(cls, schema: list[str], rows: list[list[Any]]) -> "RecordBatch":
        """Convert row-oriented tuples into a columnar RecordBatch."""
        num_rows = len(rows)
        if num_rows == 0:
            return cls(schema=schema, vectors=[], num_rows=0)

        num_cols = len(schema)
        vectors: list[ColumnVector] = []

        # Infer vector type from first non-null sample
        for col_idx in range(num_cols):
            sample = None
            for row in rows:
                if row[col_idx] is not None and not isinstance(row[col_idx], NullValue):
                    sample = row[col_idx]
                    break

            if isinstance(sample, float):
                vec = Float64Vector(capacity=max(num_rows, VECTOR_SIZE))
            elif isinstance(sample, int) and not isinstance(sample, bool):
                vec = Int64Vector(capacity=max(num_rows, VECTOR_SIZE))
            else:
                vec = StringVector(capacity=max(num_rows, VECTOR_SIZE))

            for row in rows:
                vec.append(row[col_idx])

            vectors.append(vec)

        return cls(schema=schema, vectors=vectors, num_rows=num_rows)

    def to_rows(self) -> list[list[Any]]:
        """Convert columnar RecordBatch back into row tuples."""
        rows = []
        for i in range(self.num_rows):
            row = [vec.get(i) for vec in self.vectors]
            rows.append(row)
        return rows

    def filter_by_selection(self, selection: list[int]) -> "RecordBatch":
        """Produce a new RecordBatch containing only the selected row indices."""
        sliced_vectors = [vec.slice(selection) for vec in self.vectors]
        return RecordBatch(
            schema=self.schema,
            vectors=sliced_vectors,
            num_rows=len(selection),
        )


# ─── Vectorized Physical Operators ──────────────────────────────────────────

class VectorizedOperator(ABC):
    """Abstract base for vectorized iterator pipeline."""

    @abstractmethod
    def open(self) -> None:
        """Initialize the operator state."""
        ...

    @abstractmethod
    def next_batch(self) -> Optional[RecordBatch]:
        """Produce the next RecordBatch (returns None when exhausted)."""
        ...

    @abstractmethod
    def close(self) -> None:
        """Clean up operator resources."""
        ...


class VectorizedBatchScan(VectorizedOperator):
    """Scans rows from an underlying source in batches of VECTOR_SIZE."""

    def __init__(self, schema: list[str], rows: list[list[Any]]) -> None:
        self._schema = schema
        self._rows = rows
        self._cursor = 0

    def open(self) -> None:
        self._cursor = 0

    def next_batch(self) -> Optional[RecordBatch]:
        if self._cursor >= len(self._rows):
            return None

        end = min(self._cursor + VECTOR_SIZE, len(self._rows))
        chunk = self._rows[self._cursor:end]
        self._cursor = end

        return RecordBatch.from_rows(self._schema, chunk)

    def close(self) -> None:
        pass


class VectorizedFilter(VectorizedOperator):
    """Filters RecordBatches using SIMD-style vector evaluation."""

    def __init__(self, child: VectorizedOperator, predicate: Expression) -> None:
        self._child = child
        self._predicate = predicate

    def open(self) -> None:
        self._child.open()

    def next_batch(self) -> Optional[RecordBatch]:
        while True:
            batch = self._child.next_batch()
            if batch is None:
                return None
            if batch.num_rows == 0:
                continue

            selection = self._eval_vector_predicate(self._predicate, batch)
            if selection:
                return batch.filter_by_selection(selection)
            # All rows filtered out in this batch, try next
            continue

    def close(self) -> None:
        self._child.close()

    def _eval_vector_predicate(self, expr: Expression, batch: RecordBatch) -> list[int]:
        """Evaluate predicate on RecordBatch, returning matching row indices."""
        if isinstance(expr, BinaryOp):
            if isinstance(expr.left, ColumnRef) and isinstance(expr.right, Literal):
                col_name = expr.left.column
                val = expr.right.value

                if col_name in batch.schema:
                    col_idx = batch.schema.index(col_name)
                    vec = batch.vectors[col_idx]

                    if isinstance(vec, Int64Vector) and isinstance(val, int):
                        if expr.op == BinaryOpType.GT:
                            return vec.vec_gt(val)
                        elif expr.op == BinaryOpType.EQ:
                            return [i for i in range(batch.num_rows) if vec.get(i) == val]

        # Fallback for complex expressions: evaluate row by row on vector data
        matched = []
        for i in range(batch.num_rows):
            row_dict = {col: batch.vectors[c].get(i) for c, col in enumerate(batch.schema)}
            if self._eval_scalar_expr(expr, row_dict):
                matched.append(i)
        return matched

    def _eval_scalar_expr(self, expr: Expression, row: dict[str, Any]) -> bool:
        if isinstance(expr, BinaryOp):
            left = self._eval_val(expr.left, row)
            right = self._eval_val(expr.right, row)
            if isinstance(left, NullValue) or isinstance(right, NullValue):
                return False
            if expr.op == BinaryOpType.GT:
                return left > right
            elif expr.op == BinaryOpType.EQ:
                return left == right
            elif expr.op == BinaryOpType.LT:
                return left < right
            elif expr.op == BinaryOpType.AND:
                return bool(left) and bool(right)
        return True

    def _eval_val(self, expr: Expression, row: dict[str, Any]) -> Any:
        if isinstance(expr, Literal):
            return expr.value
        elif isinstance(expr, ColumnRef):
            return row.get(expr.column, NULL)
        return NULL


class VectorizedProject(VectorizedOperator):
    """Projects columns over RecordBatches."""

    def __init__(self, child: VectorizedOperator, select_cols: list[str]) -> None:
        self._child = child
        self._select_cols = select_cols

    def open(self) -> None:
        self._child.open()

    def next_batch(self) -> Optional[RecordBatch]:
        batch = self._child.next_batch()
        if batch is None:
            return None

        projected_vectors = []
        projected_schema = []

        for col in self._select_cols:
            if col in batch.schema:
                idx = batch.schema.index(col)
                projected_vectors.append(batch.vectors[idx])
                projected_schema.append(col)

        return RecordBatch(
            schema=projected_schema,
            vectors=projected_vectors,
            num_rows=batch.num_rows,
        )

    def close(self) -> None:
        self._child.close()


# ─── Vectorized Pipeline Runner ─────────────────────────────────────────────

class VectorizedEngine:
    """High-level runner for vectorized query plans."""

    @staticmethod
    def execute_pipeline(
        schema: list[str],
        rows: list[list[Any]],
        predicate: Optional[Expression] = None,
        select_cols: Optional[list[str]] = None,
    ) -> list[list[Any]]:
        """Execute a vectorized scan-filter-project pipeline over rows.

        Returns result rows after vectorized processing.
        """
        source: VectorizedOperator = VectorizedBatchScan(schema, rows)

        if predicate:
            source = VectorizedFilter(source, predicate)

        if select_cols:
            source = VectorizedProject(source, select_cols)

        source.open()
        result_rows = []

        while True:
            batch = source.next_batch()
            if batch is None:
                break
            result_rows.extend(batch.to_rows())

        source.close()
        return result_rows
