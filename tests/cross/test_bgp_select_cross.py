"""Cross-validation: BGP + SELECT (+ FILTER) semantics against pyoxigraph.

We treat ``pyoxigraph`` as the W3C-compliant ground truth (per
``.cursor/rules/200-testing.mdc``) and assert that the bindings our
translator would produce — when AQL is "executed" against an in-memory
mock store derived from the same triples — match what pyoxigraph
returns for the same SPARQL query.

We do **not** require a live ArangoDB for this test (those go under
the ``integration`` marker). Instead, we run the translated AQL
through the shared pure-Python AQL-subset interpreter in
:mod:`tests.helpers.aql_interp`. As the visitor grows new clauses the
interpreter grows alongside (in that one module), *or* this test gets
re-pointed at python-arango behind the ``cross`` + ``integration``
markers.

This module exercises the **PG** (collection-per-class) model. The
LPG and RPT models are cross-validated against the same logical data
in :mod:`tests.cross.test_multimodel_cross`.
"""

from __future__ import annotations

from typing import Any

import pytest

from arango_sparql.api import translate
from arango_sparql.translate.resolver import SchemaResolver
from tests.helpers.aql_interp import run_aql_subset
from tests.helpers.oxi import (
    assert_bindings_equal,
    assert_bindings_equal_ordered,
    oxi_bindings,
)
from tests.helpers.oxi import (
    drop_null_bindings as _normalize_arango_row,
)
from tests.helpers.oxi import (
    normalize_oxi_row as _normalize_oxi_row,
)

oxi = pytest.importorskip("pyoxigraph", reason="pyoxigraph required for cross tests")

# A toy ontology + dataset that exercises BGP / type / property / literal-filter
# behavior end-to-end. Same triples are loaded into pyoxigraph (as RDF)
# and into the mock store (as ArangoDB-like docs).
ONTOLOGY_TTL = """
@prefix : <http://ex.org/> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix phys: <https://arango.solutions/phys#> .

:Person  a owl:Class ; phys:collectionName "Person" .
:Project a owl:Class ; phys:collectionName "Project" .
"""

DATA_TTL = """
@prefix : <http://ex.org/> .
:alice a :Person ; :name "Alice" ; :age 30 ; :dept "eng" ; :email "alice@example.com" .
:bob   a :Person ; :name "Bob"   ; :age 42 ; :dept "eng" ; :phone "+1-555-0123" .
:carol a :Person ; :name "Carol" ; :age 30 ; :dept "ops" .

:p1 a :Project ; :title "Apollo"   ; :owner :alice .
:p2 a :Project ; :title "Beacon"   ; :owner :bob .
:p3 a :Project ; :title "Catalyst" ; :owner :alice .
:p4 a :Project ; :title "Orphan" .
"""

# The same data, in the document shape the translator's AQL expects.
# ``_uri`` is the convention used by the legacy translator and adopted
# by ``visit_BGP``. Note ``email`` / ``phone`` are deliberately
# missing on some rows so OPTIONAL cross-validation exercises
# null-binding behaviour against pyoxigraph; ``dept`` is set on every
# row so GROUP BY cross-validation has stable group keys to compare.
ARANGO_DOCS: dict[str, list[dict[str, Any]]] = {
    "Person": [
        {
            "_uri": "http://ex.org/alice",
            "name": "Alice",
            "age": 30,
            "dept": "eng",
            "email": "alice@example.com",
        },
        {
            "_uri": "http://ex.org/bob",
            "name": "Bob",
            "age": 42,
            "dept": "eng",
            "phone": "+1-555-0123",
        },
        {"_uri": "http://ex.org/carol", "name": "Carol", "age": 30, "dept": "ops"},
    ],
    # ``Project`` exists for join cross-validation against pyoxigraph.
    # ``owner`` is deliberately missing on ``p4`` so a join via
    # ``?prj :owner ?p`` filters it out — same row pyoxigraph drops via
    # the unbound predicate.
    "Project": [
        {"_uri": "http://ex.org/p1", "title": "Apollo", "owner": "http://ex.org/alice"},
        {"_uri": "http://ex.org/p2", "title": "Beacon", "owner": "http://ex.org/bob"},
        {"_uri": "http://ex.org/p3", "title": "Catalyst", "owner": "http://ex.org/alice"},
        {"_uri": "http://ex.org/p4", "title": "Orphan"},
    ],
}


# ----------------------------------------------------------------------
# Helpers — load shared data into pyoxigraph and into the mock store.
# ----------------------------------------------------------------------
@pytest.fixture(scope="module")
def oxi_store() -> Any:
    store = oxi.Store()
    store.load(DATA_TTL.encode("utf-8"), oxi.RdfFormat.TURTLE)
    return store


# ``_normalize_oxi_row`` / ``_normalize_arango_row`` are imported from
# :mod:`tests.helpers.oxi` (as ``normalize_oxi_row`` / ``drop_null_bindings``)
# so the PG and multi-model cross modules share one definition.


# ----------------------------------------------------------------------
# Cases
# ----------------------------------------------------------------------
CASES = [
    pytest.param(
        "PREFIX : <http://ex.org/> SELECT ?s WHERE { ?s a :Person }",
        id="type_pattern",
    ),
    pytest.param(
        "PREFIX : <http://ex.org/> SELECT ?s ?n WHERE { ?s a :Person ; :name ?n }",
        id="type_plus_property",
    ),
    pytest.param(
        'PREFIX : <http://ex.org/> SELECT ?s WHERE { ?s a :Person ; :name "Alice" }',
        id="literal_filter",
    ),
    pytest.param(
        "PREFIX : <http://ex.org/> SELECT ?s WHERE { ?s a :Person ; :age 30 }",
        id="integer_literal_filter",
    ),
    pytest.param(
        "PREFIX : <http://ex.org/> SELECT DISTINCT ?n WHERE { ?s a :Person ; :name ?n }",
        id="distinct_projection",
    ),
    # Alice and Bob share ``dept "eng"``, so the first two rows hold a
    # duplicate: SPARQL de-duplicates first and returns both departments
    # (issue #39 — the AQL used to slice first and return one).
    pytest.param(
        "PREFIX : <http://ex.org/> SELECT DISTINCT ?d WHERE { ?s a :Person ; :dept ?d } LIMIT 2",
        id="distinct_with_limit",
    ),
    # ----- FILTER cases ---------------------------------------------------
    pytest.param(
        'PREFIX : <http://ex.org/> SELECT ?s ?n WHERE { ?s a :Person ; :name ?n . FILTER(?n = "Alice") }',
        id="filter_equality",
    ),
    pytest.param(
        'PREFIX : <http://ex.org/> SELECT ?s ?n WHERE { ?s a :Person ; :name ?n . FILTER(?n != "Bob") }',
        id="filter_not_equals",
    ),
    pytest.param(
        "PREFIX : <http://ex.org/> SELECT ?s ?a WHERE { ?s a :Person ; :age ?a . FILTER(?a > 30) }",
        id="filter_gt",
    ),
    pytest.param(
        "PREFIX : <http://ex.org/> SELECT ?s ?a WHERE { ?s a :Person ; :age ?a . FILTER(?a >= 30 && ?a <= 40) }",
        id="filter_range_and",
    ),
    pytest.param(
        'PREFIX : <http://ex.org/> SELECT ?s ?n WHERE { ?s a :Person ; :name ?n . FILTER(?n = "Alice" || ?n = "Bob") }',
        id="filter_or",
    ),
    pytest.param(
        'PREFIX : <http://ex.org/> SELECT ?s ?n WHERE { ?s a :Person ; :name ?n . FILTER(REGEX(?n, "^A")) }',
        id="filter_regex",
    ),
    pytest.param(
        'PREFIX : <http://ex.org/> SELECT ?s ?n WHERE { ?s a :Person ; :name ?n . FILTER(REGEX(?n, "^a", "i")) }',
        id="filter_regex_case_insensitive",
    ),
    pytest.param(
        'PREFIX : <http://ex.org/> SELECT ?s ?n WHERE { ?s a :Person ; :name ?n . FILTER(CONTAINS(?n, "li")) }',
        id="filter_contains",
    ),
    pytest.param(
        'PREFIX : <http://ex.org/> SELECT ?s ?n WHERE { ?s a :Person ; :name ?n . FILTER(STRSTARTS(?n, "Al")) }',
        id="filter_strstarts",
    ),
]


@pytest.mark.cross
@pytest.mark.parametrize("sparql", CASES)
def test_bgp_select_matches_oxigraph(oxi_store: Any, sparql: str) -> None:
    resolver = SchemaResolver.from_turtle(ONTOLOGY_TTL)
    result = translate(sparql, resolver=resolver)
    actual = [_normalize_arango_row(r) for r in run_aql_subset(result.aql, result.bind_vars, ARANGO_DOCS)]
    expected = [_normalize_oxi_row(r) for r in oxi_bindings(oxi_store, sparql)]
    assert_bindings_equal(expected, actual)


# ORDER BY cases need a separate parametrize because ordering is the
# property under test — set-equality would mask wrong-order failures.
ORDER_BY_CASES = [
    pytest.param(
        "PREFIX : <http://ex.org/> SELECT ?n WHERE { ?s a :Person ; :name ?n } ORDER BY ?n",
        id="order_by_name_asc",
    ),
    pytest.param(
        "PREFIX : <http://ex.org/> SELECT ?a WHERE { ?s a :Person ; :age ?a } ORDER BY DESC(?a)",
        id="order_by_age_desc",
    ),
    pytest.param(
        "PREFIX : <http://ex.org/> SELECT ?n ?a WHERE { ?s a :Person ; :name ?n ; :age ?a } "
        "ORDER BY DESC(?a) ?n",
        id="order_by_age_desc_name_asc_tie_break",
    ),
    pytest.param(
        "PREFIX : <http://ex.org/> SELECT ?n WHERE { ?s a :Person ; :name ?n ; :age ?a } "
        "ORDER BY ?n LIMIT 2 OFFSET 1",
        id="order_by_with_limit_offset",
    ),
    pytest.param(
        "PREFIX : <http://ex.org/> SELECT DISTINCT ?a WHERE { ?s a :Person ; :age ?a } ORDER BY ?a",
        id="order_by_distinct",
    ),
    # Ordered by name the departments run eng (Alice), eng (Bob), ops
    # (Carol): DISTINCT then LIMIT keeps [eng, ops], and OFFSET 1 keeps
    # [ops]. Slicing before de-duplicating gives [eng] for both (#39).
    pytest.param(
        "PREFIX : <http://ex.org/> SELECT DISTINCT ?d WHERE { ?s a :Person ; :dept ?d ; :name ?n } "
        "ORDER BY ?n LIMIT 2",
        id="distinct_order_by_unprojected_var_with_limit",
    ),
    pytest.param(
        "PREFIX : <http://ex.org/> SELECT DISTINCT ?d WHERE { ?s a :Person ; :dept ?d ; :name ?n } "
        "ORDER BY ?n LIMIT 1 OFFSET 1",
        id="distinct_order_by_unprojected_var_with_limit_offset",
    ),
]


@pytest.mark.cross
@pytest.mark.parametrize("sparql", ORDER_BY_CASES)
def test_order_by_matches_oxigraph(oxi_store: Any, sparql: str) -> None:
    resolver = SchemaResolver.from_turtle(ONTOLOGY_TTL)
    result = translate(sparql, resolver=resolver)
    actual = [_normalize_arango_row(r) for r in run_aql_subset(result.aql, result.bind_vars, ARANGO_DOCS)]
    expected = [_normalize_oxi_row(r) for r in oxi_bindings(oxi_store, sparql)]
    assert_bindings_equal_ordered(expected, actual)


# BIND cases — visit_Extend lowers ``BIND(<expr> AS ?v)`` to a LET. We
# re-validate the outputs against pyoxigraph to catch any divergence in
# expression semantics (string casing, arithmetic, length, …) between
# our AQL builtins map and SPARQL's reference builtins.
EXTEND_CASES = [
    pytest.param(
        "PREFIX : <http://ex.org/> SELECT ?n ?upper WHERE { "
        "?s a :Person ; :name ?n . BIND(UCASE(?n) AS ?upper) }",
        id="bind_ucase",
    ),
    pytest.param(
        "PREFIX : <http://ex.org/> SELECT ?n ?lower WHERE { "
        "?s a :Person ; :name ?n . BIND(LCASE(?n) AS ?lower) }",
        id="bind_lcase",
    ),
    pytest.param(
        "PREFIX : <http://ex.org/> SELECT ?n ?len WHERE { "
        "?s a :Person ; :name ?n . BIND(STRLEN(?n) AS ?len) }",
        id="bind_strlen",
    ),
    pytest.param(
        "PREFIX : <http://ex.org/> SELECT ?n ?dbl WHERE { "
        "?s a :Person ; :name ?n ; :age ?a . BIND(?a * 2 AS ?dbl) "
        "FILTER(?dbl >= 60) }",
        id="bind_arith_then_filter",
    ),
]


@pytest.mark.cross
@pytest.mark.parametrize("sparql", EXTEND_CASES)
def test_extend_matches_oxigraph(oxi_store: Any, sparql: str) -> None:
    resolver = SchemaResolver.from_turtle(ONTOLOGY_TTL)
    result = translate(sparql, resolver=resolver)
    actual = [_normalize_arango_row(r) for r in run_aql_subset(result.aql, result.bind_vars, ARANGO_DOCS)]
    expected = [_normalize_oxi_row(r) for r in oxi_bindings(oxi_store, sparql)]
    assert_bindings_equal(expected, actual)


# OPTIONAL cases — the data set has some persons with email, some with
# phone, some with neither, so the LEFT-JOIN behavior is observable in
# the cross-validation diff (not just the goldens).
LEFTJOIN_CASES = [
    pytest.param(
        "PREFIX : <http://ex.org/> SELECT ?n ?email WHERE { "
        "?s a :Person ; :name ?n . OPTIONAL { ?s :email ?email } }",
        id="optional_email",
    ),
    pytest.param(
        "PREFIX : <http://ex.org/> SELECT ?n ?email ?phone WHERE { "
        "?s a :Person ; :name ?n . "
        "OPTIONAL { ?s :email ?email } "
        "OPTIONAL { ?s :phone ?phone } }",
        id="optional_two_blocks",
    ),
    pytest.param(
        "PREFIX : <http://ex.org/> SELECT ?n ?email ?phone WHERE { "
        "?s a :Person ; :name ?n . "
        "OPTIONAL { ?s :email ?email ; :phone ?phone } }",
        id="optional_multi_var_one_block",
    ),
    pytest.param(
        "PREFIX : <http://ex.org/> SELECT ?n ?email WHERE { "
        '?s a :Person ; :name ?n . OPTIONAL { ?s :email ?email . FILTER(STRSTARTS(?email, "a")) } }',
        id="optional_with_inner_filter",
    ),
]


@pytest.mark.cross
@pytest.mark.parametrize("sparql", LEFTJOIN_CASES)
def test_leftjoin_matches_oxigraph(oxi_store: Any, sparql: str) -> None:
    resolver = SchemaResolver.from_turtle(ONTOLOGY_TTL)
    result = translate(sparql, resolver=resolver)
    actual = [_normalize_arango_row(r) for r in run_aql_subset(result.aql, result.bind_vars, ARANGO_DOCS)]
    expected = [_normalize_oxi_row(r) for r in oxi_bindings(oxi_store, sparql)]
    assert_bindings_equal(expected, actual)


# Aggregate cases — verify COUNT / SUM / AVG / MIN / MAX semantics
# against pyoxigraph's W3C-compliant evaluator. The interpreter's
# COLLECT branch has its own subtle null/distinct semantics so this
# is the cleanest place to pin them.
AGGREGATE_CASES = [
    pytest.param(
        "PREFIX : <http://ex.org/> SELECT (COUNT(*) AS ?c) WHERE { ?s a :Person }",
        id="count_star",
    ),
    pytest.param(
        "PREFIX : <http://ex.org/> SELECT (COUNT(?s) AS ?c) WHERE { ?s a :Person }",
        id="count_var",
    ),
    pytest.param(
        "PREFIX : <http://ex.org/> SELECT (COUNT(DISTINCT ?d) AS ?c) WHERE { ?s a :Person ; :dept ?d }",
        id="count_distinct_dept",
    ),
    pytest.param(
        "PREFIX : <http://ex.org/> SELECT ?d (COUNT(?s) AS ?c) WHERE { ?s a :Person ; :dept ?d } GROUP BY ?d",
        id="group_by_count",
    ),
    pytest.param(
        "PREFIX : <http://ex.org/> SELECT ?d (SUM(?a) AS ?tot) (AVG(?a) AS ?avg) "
        "WHERE { ?s a :Person ; :dept ?d ; :age ?a } GROUP BY ?d",
        id="group_by_sum_avg",
    ),
    pytest.param(
        "PREFIX : <http://ex.org/> SELECT ?d (MIN(?a) AS ?mn) (MAX(?a) AS ?mx) "
        "WHERE { ?s a :Person ; :dept ?d ; :age ?a } GROUP BY ?d",
        id="group_by_min_max",
    ),
    pytest.param(
        "PREFIX : <http://ex.org/> SELECT ?d (COUNT(?s) AS ?c) WHERE { "
        "?s a :Person ; :dept ?d } GROUP BY ?d HAVING (COUNT(?s) > 1)",
        id="group_by_having",
    ),
]


@pytest.mark.cross
@pytest.mark.parametrize("sparql", AGGREGATE_CASES)
def test_aggregate_matches_oxigraph(oxi_store: Any, sparql: str) -> None:
    resolver = SchemaResolver.from_turtle(ONTOLOGY_TTL)
    result = translate(sparql, resolver=resolver)
    actual = [_normalize_arango_row(r) for r in run_aql_subset(result.aql, result.bind_vars, ARANGO_DOCS)]
    expected = [_normalize_oxi_row(r) for r in oxi_bindings(oxi_store, sparql)]
    assert_bindings_equal(expected, actual)


# Multi-subject BGP / Join cases — verify that shared variables across
# typed FORs become equality FILTERs (effectively an inner join), and
# that disjoint groups produce a real Cartesian product. Both shapes
# need to round-trip through the AQL interpreter and match pyoxigraph.
JOIN_CASES = [
    pytest.param(
        "PREFIX : <http://ex.org/> SELECT ?p ?o WHERE { "
        "?p a :Person ; :name ?n . ?o a :Project ; :owner ?p }",
        id="multi_subject_single_bgp",
    ),
    pytest.param(
        "PREFIX : <http://ex.org/> SELECT ?n ?title WHERE { "
        "{ ?p a :Person ; :name ?n } "
        "{ ?prj a :Project ; :title ?title ; :owner ?p } }",
        id="explicit_join_grouped",
    ),
    pytest.param(
        # Disjoint groups → Cartesian product. With 3 Person rows and
        # 4 Project rows, expect 12 combinations from both sides.
        "PREFIX : <http://ex.org/> SELECT ?n ?title WHERE { "
        "{ ?p a :Person ; :name ?n } "
        "{ ?prj a :Project ; :title ?title } }",
        id="cross_join_disjoint",
    ),
    pytest.param(
        "PREFIX : <http://ex.org/> SELECT ?n ?title WHERE { "
        '{ ?p a :Person ; :name ?n . FILTER (STRSTARTS(?n, "A")) } '
        "{ ?prj a :Project ; :title ?title ; :owner ?p } }",
        id="join_with_filter_each_side",
    ),
    pytest.param(
        # 3-way join: Person ↔ Project (via owner) and an extra
        # ``?n`` projection that pulls Person.name through the join.
        "PREFIX : <http://ex.org/> SELECT ?n ?title WHERE { "
        "?p a :Person ; :name ?n . "
        "?prj a :Project ; :owner ?p ; :title ?title } "
        "ORDER BY ?n ?title",
        id="join_with_outer_order",
    ),
]


@pytest.mark.cross
@pytest.mark.parametrize("sparql", JOIN_CASES)
def test_join_matches_oxigraph(oxi_store: Any, sparql: str) -> None:
    resolver = SchemaResolver.from_turtle(ONTOLOGY_TTL)
    result = translate(sparql, resolver=resolver)
    actual = [_normalize_arango_row(r) for r in run_aql_subset(result.aql, result.bind_vars, ARANGO_DOCS)]
    expected = [_normalize_oxi_row(r) for r in oxi_bindings(oxi_store, sparql)]
    assert_bindings_equal(expected, actual)
