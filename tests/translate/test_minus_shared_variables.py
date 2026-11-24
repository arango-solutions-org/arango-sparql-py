"""Which variables a MINUS shares with the outer row (SPARQL 1.1 §8.3.4).

MINUS removes an outer row when some solution of its right side is
compatible with it and the two share a variable in their domains. The
domain of a right-side solution is the variables the right side *binds*:
its triples, BIND targets, VALUES columns and sub-select projections. A
variable it only reads (a FILTER's, or a sub-select's unprojected one) is
never in it.

``emit_minus`` took every variable the right side *referenced*, from
triples and expressions only. So a variable bound only by a VALUES block,
a sub-select projection or a BIND did not count, and a variable only a
FILTER read did (#216, #212, #208, #209). It now uses
``optional_subquery.bound_variables``. Each case here is pinned against
pyoxigraph on ArangoDB in ``tests/integration/test_nested_optional_scope_live.py``.
"""

from __future__ import annotations

import pytest

from arango_sparql.api import translate
from arango_sparql.translate.resolver import SchemaResolver

_PREFIX = "PREFIX : <http://ex.org/> "


def _emits_minus(aql: str) -> bool:
    """The MINUS is translated: a per-row probe, or a lookup in hoisted values."""
    return "minus_probe" in aql or "IN minus_values" in aql


def _aql(query: str) -> str:
    resolver = SchemaResolver.from_turtle("", default_collection="Document")
    return translate(_PREFIX + query, resolver=resolver).aql


@pytest.mark.parametrize(
    "query",
    [
        # Only a VALUES block binds ?s (#216).
        pytest.param("SELECT ?s WHERE { ?s :name ?n MINUS { VALUES ?s { :a :d } } }", id="values_only"),
        # VALUES with UNDEF cells (#208).
        pytest.param(
            "SELECT ?s ?x WHERE { ?s :name ?n OPTIONAL { ?s :knows ?x } "
            "MINUS { VALUES (?s ?x) { (:z UNDEF) (UNDEF :b) } } }",
            id="values_undef",
        ),
        # A sub-select whose rows come from VALUES.
        pytest.param(
            "SELECT ?s WHERE { ?s :name ?n MINUS { SELECT ?s WHERE { VALUES ?s { :a } } } }",
            id="subselect_over_values",
        ),
        # The only shared variable is an aggregate's alias (#212).
        pytest.param(
            "SELECT ?c WHERE { ?s :age ?c MINUS { SELECT (COUNT(*) AS ?c) WHERE { ?t :knows ?k } } }",
            id="aggregate_column_only",
        ),
    ],
)
def test_a_variable_only_values_or_a_projection_binds_is_shared(query: str) -> None:
    assert _emits_minus(_aql(query))


@pytest.mark.parametrize(
    "query",
    [
        # ?s is read by the FILTER, never bound by the right side: no
        # solution of it has ?s in its domain, so nothing is removed (#209).
        pytest.param(
            "SELECT ?s WHERE { ?s :name ?n MINUS { ?t :knows ?k FILTER(?t = ?s) } }",
            id="filter_only",
        ),
        # ?s is bound inside the sub-select but not projected.
        pytest.param(
            "SELECT ?s WHERE { ?s :name ?n MINUS { SELECT ?k WHERE { ?s :knows ?k } } }",
            id="subselect_unprojected",
        ),
        # A VALUES column the outer row does not have.
        pytest.param("SELECT ?s WHERE { ?s :name ?n MINUS { VALUES ?q { :a } } }", id="values_disjoint"),
    ],
)
def test_a_variable_the_right_side_only_reads_is_not_shared(query: str) -> None:
    assert not _emits_minus(_aql(query))


def test_an_aggregate_column_is_joined_with_the_outer_variable() -> None:
    """``?x`` is the sub-select's MIN alias: the probe joins it, compatibly
    on both sides, instead of matching on ``?s`` alone (#212)."""
    aql = _aql(
        "SELECT ?s ?x WHERE { ?s :name ?n OPTIONAL { ?s :likes ?x } "
        "MINUS { SELECT ?s (MIN(?k) AS ?x) WHERE { ?s :knows ?k } GROUP BY ?s } }"
    )
    assert "FILTER (doc1.likes == null || row7.x == null || row7.x == doc1.likes)" in aql


def test_a_bind_target_is_shared() -> None:
    """``BIND(:a AS ?s)`` binds ``?s`` in the right side; it was not counted,
    the MINUS was a no-op and kept ``:a``. No shared variable is bound in
    every solution of a BIND, so the body is evaluated apart and the probe
    joins its ``?s`` with the outer one."""
    aql = _aql("SELECT ?s WHERE { ?s :name ?n MINUS { BIND(:a AS ?s) } }")
    assert (
        "FILTER !(NOT_NULL(doc1._uri, doc1._id) != null && NOT_NULL(doc1._uri, doc1._id) IN minus_values"
        in aql
    )


@pytest.mark.parametrize(
    "query",
    [
        pytest.param(
            "SELECT ?s WHERE { ?s :name ?n MINUS { VALUES ?s { UNDEF :b } } }", id="values_undef_row"
        ),
        pytest.param(
            "SELECT ?s ?a WHERE { ?s :age ?a MINUS { SELECT (MIN(?x) AS ?a) WHERE { ?t :age ?x } } }",
            id="aggregate_column",
        ),
        pytest.param(
            "SELECT ?s WHERE { ?s :name ?n MINUS { { ?s :likes ?y } UNION { ?t :knows ?k } } }",
            id="union_arm",
        ),
    ],
)
def test_a_solution_binding_no_shared_variable_removes_nothing(query: str) -> None:
    """No shared variable is bound in every right-side solution: the probe
    requires one bound on both sides, per solution (§8.3.4; review of #227,
    where such a solution removed every row)."""
    aql = _aql(query)
    assert _emits_minus(aql)
    # Some shared variable bound on both sides: in the probe's overlap
    # guard, or the lookup's ``outer != null && outer IN values``.
    tail = aql[aql.index("FOR doc1") :]
    assert "!= null && " in tail


def test_a_body_evaluated_apart_is_computed_once_before_the_outer_loop() -> None:
    """It reads nothing of the outer row, so it is a LET before the first FOR
    rather than a subquery per outer row (second review of #227: 0.02 s on
    main became 14 s on 12,000 documents)."""
    aql = _aql(
        "SELECT ?s ?a WHERE { ?s :age ?a MINUS { SELECT (MIN(?x) AS ?a) WHERE { ?t :age ?x FILTER(?x > 100) } } }"
    )
    assert aql.startswith("LET minus_")
    assert aql.index("LET minus_") < aql.index("FOR doc1 IN")


def test_one_shared_variable_is_a_lookup_in_its_hoisted_sorted_values() -> None:
    """A per-row scan of the hoisted rows took 28 s on 12,000 rows; one
    shared variable is a sorted IN lookup instead (third review of #227)."""
    aql = _aql("SELECT ?s ?k WHERE { ?s :knows ?k MINUS { { ?x :likes ?k } UNION { VALUES ?q { 1 } } } }")
    assert aql.startswith("LET minus_values")
    assert "SORTED_UNIQUE((" in aql
    assert "IN minus_values" in aql


def test_inside_exists_the_outer_variables_are_shared_by_a_nested_minus() -> None:
    """pyoxigraph's reading: the outer row binds ?s on both sides, so a right
    side that binds nothing else still shares ?s (main took it as a no-op)."""
    aql = _aql("SELECT ?s WHERE { ?s :name ?n FILTER NOT EXISTS { ?t :knows ?k MINUS { ?z :email ?w } } }")
    assert _emits_minus(aql)
