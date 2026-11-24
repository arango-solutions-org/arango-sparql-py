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
from arango_sparql.errors import UnsupportedSparqlError
from arango_sparql.translate.resolver import SchemaResolver

_PREFIX = "PREFIX : <http://ex.org/> "


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
    assert "minus_probe" in _aql(query)


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
    assert "minus_probe" not in _aql(query)


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
    the MINUS was a no-op and kept ``:a``. The probe cannot yet compare a
    BIND with an outer value, so it is refused rather than answered wrongly."""
    with pytest.raises(UnsupportedSparqlError, match=r"BIND target \?s is already bound"):
        _aql("SELECT ?s WHERE { ?s :name ?n MINUS { BIND(:a AS ?s) } }")
