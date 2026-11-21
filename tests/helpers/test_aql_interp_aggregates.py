"""Unit tests for the interpreter's COLLECT aggregates against ArangoDB's own.

The cross tests compare the interpreter's result with pyoxigraph, so an
interpreter that applied SPARQL's semantics instead of AQL's would pass AQL
that ArangoDB gets wrong — which is how issue #46 (COUNT counting unbound
values) went unseen: this interpreter's ``COUNT_DISTINCT`` dropped null, and
ArangoDB's does not. Each expectation here is what ArangoDB 3.12.9 returned for
the same AQL, so the interpreter is pinned to the engine, not to SPARQL.
"""

from __future__ import annotations

from typing import Any

import pytest

from tests.helpers.aql_interp import run_aql_subset

_VALUES = [{"v": "a"}, {"v": None}, {"v": "a"}, {}]  # the last has no ``v``: null


def _one(aql: str, docs: list[dict[str, Any]]) -> Any:
    rows = run_aql_subset(aql, {"@c1": "C"}, {"C": docs})
    assert len(rows) == 1, rows
    return rows[0]["r"]


@pytest.mark.parametrize(
    ("aggregate", "expected"),
    [
        # COUNT counts every value, null included (4 rows).
        ("COUNT(doc1.v)", 4),
        # COUNT_DISTINCT counts null as one more value: "a" and null.
        ("COUNT_DISTINCT(doc1.v)", 2),
        # UNIQUE keeps null as a value.
        ("UNIQUE(doc1.v)", ["a", None]),
        # SUM skips null; a ternary argument counts the bound values.
        ("SUM(doc1.v != null ? 1 : 0)", 2),
    ],
)
def test_aggregates_count_null_the_way_arangodb_does(aggregate: str, expected: Any) -> None:
    aql = f"FOR doc1 IN @@c1\nCOLLECT AGGREGATE agg = {aggregate}\nRETURN {{ r: agg }}"
    assert _one(aql, _VALUES) == expected


def test_an_ungrouped_collect_over_no_rows_still_returns_one_row() -> None:
    # ArangoDB: WITH COUNT INTO gives 0, SUM gives null, UNIQUE gives [].
    assert _one("FOR doc1 IN @@c1\nCOLLECT WITH COUNT INTO c\nRETURN { r: c }", []) == 0
    assert _one("FOR doc1 IN @@c1\nCOLLECT AGGREGATE s = SUM(doc1.v)\nRETURN { r: s }", []) is None
    assert _one("FOR doc1 IN @@c1\nCOLLECT AGGREGATE u = UNIQUE(doc1.v)\nRETURN { r: u }", []) == []


def test_sum_over_only_nulls_is_zero_not_null() -> None:
    aql = "FOR doc1 IN @@c1\nCOLLECT AGGREGATE s = SUM(doc1.v)\nRETURN { r: s }"
    assert _one(aql, [{"v": None}]) == 0


def test_unique_keeps_true_apart_from_one_but_merges_one_and_one_point_zero() -> None:
    # ArangoDB: UNIQUE([1, true, 1.0, 0, false]) == [1, true, 0, false].
    docs = [{"v": v} for v in (1, True, 1.0, 0, False)]
    aql = "FOR doc1 IN @@c1\nCOLLECT AGGREGATE u = UNIQUE(doc1.v)\nRETURN { r: u }"
    assert _one(aql, docs) == [1, True, 0, False]
    count = "FOR doc1 IN @@c1\nCOLLECT AGGREGATE c = COUNT_DISTINCT(doc1.v)\nRETURN { r: c }"
    assert _one(count, docs + [{"v": None}]) == 5


def test_the_count_distinct_read_drops_the_null() -> None:
    aql = (
        "FOR doc1 IN @@c1\nCOLLECT AGGREGATE u = UNIQUE(doc1.v)\nRETURN { r: LENGTH(REMOVE_VALUE(u, null)) }"
    )
    assert _one(aql, _VALUES) == 1


def test_null_orders_below_a_number_as_in_aql() -> None:
    # ArangoDB: ``null > 0`` is false and ``null < 0`` true (null sorts first).
    # Over no rows the aggregate error read compares a null count (#59).
    aql = (
        "FOR doc1 IN @@c1\n"
        "COLLECT AGGREGATE n = SUM(doc1.v == null ? 1 : 0)\n"
        'RETURN { r: (n > 0 ? "some" : "none"), low: (n < 0 ? "yes" : "no") }'
    )
    rows = run_aql_subset(aql, {"@c1": "C"}, {"C": []})
    assert rows == [{"r": "none", "low": "yes"}]


def test_attributes_without_system_attributes_drops_every_underscore_name() -> None:
    # ArangoDB 3.12: ATTRIBUTES({_uri:1, _key:2, a:3, _graph:4, b:null}, true)
    # == ["b", "a"] (any order), ATTRIBUTES(null, true) == null. A variable
    # predicate iterates it (#191's cross tests read documents this way).
    aql = "FOR doc1 IN @@c1\nFOR k2 IN ATTRIBUTES(doc1, true)\nRETURN { r: k2 }"
    rows = run_aql_subset(
        aql, {"@c1": "C"}, {"C": [{"_uri": 1, "_key": "2", "a": 3, "_graph": 4, "b": None}]}
    )
    assert sorted(r["r"] for r in rows) == ["a", "b"]
