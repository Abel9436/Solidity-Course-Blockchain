"""
Tests for the Vectorized Query Execution & Columnar Engine.
"""

import pytest
from pysql.vectorized import (
    Float64Vector,
    Int64Vector,
    RecordBatch,
    StringVector,
    VectorizedBatchScan,
    VectorizedEngine,
    VectorizedFilter,
    VectorizedProject,
)
from pysql.ast_nodes import BinaryOp, BinaryOpType, ColumnRef, Literal
from pysql.types import NULL


class TestColumnVectors:
    """Tests for individual column vector operations."""

    def test_int64_vector(self):
        vec = Int64Vector(capacity=100)
        vec.append(10)
        vec.append(20)
        vec.append(NULL)
        vec.append(40)

        assert vec.length == 4
        assert vec.get(0) == 10
        assert vec.get(1) == 20
        assert isinstance(vec.get(2), type(NULL))
        assert vec.get(3) == 40

    def test_int64_vectorized_gt(self):
        vec = Int64Vector(capacity=10)
        for val in [5, 15, 25, 5, 30]:
            vec.append(val)

        matched_indices = vec.vec_gt(10)
        assert matched_indices == [1, 2, 4]

    def test_float64_vector(self):
        vec = Float64Vector(capacity=10)
        vec.append(1.5)
        vec.append(3.14)
        assert vec.get(0) == 1.5
        assert vec.get(1) == 3.14

    def test_string_vector(self):
        vec = StringVector(capacity=10)
        vec.append("hello")
        vec.append("world")
        assert vec.get(0) == "hello"
        assert vec.get(1) == "world"


class TestRecordBatch:
    """Tests for RecordBatch columnar container."""

    def test_record_batch_conversion(self):
        schema = ["id", "score", "name"]
        rows = [
            [1, 95.5, "Alice"],
            [2, 88.0, "Bob"],
            [3, 92.3, "Charlie"],
        ]

        batch = RecordBatch.from_rows(schema, rows)
        assert batch.num_rows == 3
        assert len(batch.vectors) == 3
        assert batch.to_rows() == rows

    def test_record_batch_slicing(self):
        schema = ["id", "name"]
        rows = [
            [1, "Alice"],
            [2, "Bob"],
            [3, "Charlie"],
            [4, "David"],
        ]
        batch = RecordBatch.from_rows(schema, rows)
        sliced = batch.filter_by_selection([0, 2])

        assert sliced.num_rows == 2
        assert sliced.to_rows() == [[1, "Alice"], [3, "Charlie"]]


class TestVectorizedExecution:
    """Tests for vectorized execution pipeline."""

    def test_vectorized_pipeline_scan_and_filter(self):
        schema = ["id", "age", "name"]
        rows = [
            [1, 25, "Alice"],
            [2, 35, "Bob"],
            [3, 18, "Charlie"],
            [4, 42, "David"],
        ]

        # WHERE age > 20
        predicate = BinaryOp(
            op=BinaryOpType.GT,
            left=ColumnRef("age"),
            right=Literal(20, "integer"),
        )

        results = VectorizedEngine.execute_pipeline(
            schema=schema,
            rows=rows,
            predicate=predicate,
            select_cols=["name", "age"],
        )

        assert len(results) == 3
        names = [r[0] for r in results]
        assert "Alice" in names
        assert "Bob" in names
        assert "David" in names
        assert "Charlie" not in names
