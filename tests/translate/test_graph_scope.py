"""GRAPH scope: what the translation emits and what it refuses (#195).

``GRAPH ?g`` ranges over the named graphs only, and everything inside a
GRAPH (a MINUS / EXISTS probe, an OPTIONAL body, a sub-SELECT) is matched in
the scope's graph. The answers are checked against an oracle in
``tests/cross/test_named_graph_scope_cross.py`` and live in
``tests/integration/test_named_graph_scope_live.py``; this module pins the
emitted shape and the queries the translation refuses, which would otherwise
be wrong answers.
"""

from __future__ import annotations

import re

import pytest

from arango_sparql.errors import UnsupportedSparqlError
from tests.cross.test_multi_document_iri_cross import Store, translate_case
from tests.cross.test_named_graph_scope_cross import SPLIT

STORES = [pytest.param(SPLIT[m], id=m) for m in ("pg", "lpg", "rpt")]

_PER_GRAPH = "otherwise the group is evaluated once per named graph"
#: On a triples table the OPTIONAL's own scan refuses first, for the same reason.
_PER_GRAPH_OR_SCAN = "(?:otherwise the group is evaluated once per named graph|before any pattern binds)"


@pytest.mark.parametrize("store", STORES)
def test_graph_variable_excludes_the_default_graph(store: Store) -> None:
    aql = translate_case(store, "SELECT ?g ?s WHERE { GRAPH ?g { ?s a :Person } }").aql
    assert "._graph != null" in aql


@pytest.mark.parametrize("store", STORES)
def test_a_bound_graph_variable_is_joined_without_a_null_test(store: Store) -> None:
    # The second scan is joined on the first one's graph; ``!= null`` would
    # add nothing (review of #198).
    aql = translate_case(store, "SELECT ?g ?s ?t WHERE { GRAPH ?g { ?s :name ?n . ?t :email ?e } }").aql
    joined = re.findall(r"FILTER (\w+)\._graph == (\w+)\._graph", aql)
    assert joined, aql
    for alias, _outer in joined:
        assert f"{alias}._graph != null" not in aql


@pytest.mark.parametrize("store", STORES)
@pytest.mark.parametrize(
    "query, message",
    [
        pytest.param(
            "SELECT ?g WHERE { GRAPH ?g { OPTIONAL { :b :email ?m } } }", _PER_GRAPH, id="only_optional"
        ),
        pytest.param(
            "SELECT ?g ?n WHERE { GRAPH ?g { OPTIONAL { ?s :name ?n } } }",
            _PER_GRAPH,
            id="only_optional_binding_g",
        ),
        # An OPTIONAL before the group's first required pattern binds ?g
        # first; a later pattern must not then join it as if an OPTIONAL
        # outside the GRAPH had left it unbound (review of #198).
        pytest.param(
            "SELECT ?g ?s ?x WHERE { GRAPH ?g { OPTIONAL { ?s :knows ?x } ?s :email ?e } }",
            _PER_GRAPH,
            id="optional_first",
        ),
        pytest.param(
            "SELECT ?g ?s ?n WHERE { GRAPH ?g { OPTIONAL { ?s :name ?n } ?s :email ?e } }",
            _PER_GRAPH,
            id="optional_first_attribute",
        ),
        pytest.param(
            "SELECT ?s ?x ?g ?m WHERE { ?s :knows ?x . GRAPH ?g { OPTIONAL { ?x :email ?m } ?x :name ?n } }",
            _PER_GRAPH_OR_SCAN,
            id="optional_first_bound_subject",
        ),
        pytest.param(
            "SELECT ?g ?s ?n WHERE { GRAPH ?g { OPTIONAL { ?s :name ?n } { SELECT ?s WHERE { ?s :email ?e } } } }",
            _PER_GRAPH,
            id="optional_first_then_subselect",
        ),
        # Inside a sub-SELECT first in the group, ?g is the sub-SELECT's graph
        # column, bound first by the OPTIONAL just the same (recheck of #198).
        pytest.param(
            "SELECT ?g ?s ?x WHERE { GRAPH ?g { { SELECT ?s ?x WHERE { OPTIONAL { ?s :knows ?x } ?s :email ?e } } } }",
            _PER_GRAPH,
            id="optional_first_in_subselect",
        ),
        # ?g can be unbound on entry (VALUES UNDEF, an OPTIONAL outside): an
        # OPTIONAL before a required pattern binds it is refused too.
        pytest.param(
            "SELECT ?g ?s ?x WHERE { VALUES ?g { UNDEF } GRAPH ?g { OPTIONAL { ?s :knows ?x } ?s :email ?e } }",
            _PER_GRAPH,
            id="optional_first_after_values_undef",
        ),
        pytest.param(
            "SELECT ?s ?g ?x WHERE { ?s :name ?n0 OPTIONAL { GRAPH ?g { ?s :email ?e0 } } "
            "GRAPH ?g { OPTIONAL { ?s :knows ?x } ?s a :Person } }",
            _PER_GRAPH,
            id="optional_first_after_outer_optional",
        ),
        # A nested GRAPH <iri> reads nothing of ?g's graph.
        pytest.param(
            "SELECT DISTINCT ?s WHERE { GRAPH ?g { GRAPH :g1 { ?s :name ?n } } }",
            _PER_GRAPH,
            id="nested_constant",
        ),
        pytest.param("SELECT ?g ?x WHERE { GRAPH ?g { BIND(1 AS ?x) } }", _PER_GRAPH, id="only_bind"),
        pytest.param(
            "SELECT ?g ?s WHERE { GRAPH ?g { { SELECT ?s WHERE { ?s :name ?n } LIMIT 1 } } }",
            "a sub-SELECT with LIMIT, OFFSET or an aggregate inside GRAPH ?g",
            id="subselect_first_limit",
        ),
        pytest.param(
            "SELECT ?g ?c WHERE { GRAPH ?g { { SELECT (COUNT(?s) AS ?c) WHERE { ?s :name ?n } } } }",
            "a sub-SELECT with LIMIT, OFFSET or an aggregate inside GRAPH ?g",
            id="subselect_first_aggregate",
        ),
    ],
)
def test_graph_variable_shapes_without_a_binding_pattern_are_refused(
    store: Store, query: str, message: str
) -> None:
    pattern = message if message.startswith("(?:") else re.escape(message)
    with pytest.raises(UnsupportedSparqlError, match=pattern):
        translate_case(store, query)


def test_a_triples_table_optional_before_the_graph_variable_is_bound_is_refused() -> None:
    # The cross-subject OPTIONAL scan would have to bind ?g per triple.
    query = "SELECT ?s ?x ?g ?m WHERE { ?s a :Person ; :knows ?x . GRAPH ?g { OPTIONAL { ?x :email ?m } } }"
    with pytest.raises(UnsupportedSparqlError, match="before any pattern binds"):
        translate_case(SPLIT["rpt"], query)


@pytest.mark.parametrize("store", STORES)
def test_a_constant_graph_needs_no_binding_pattern(store: Store) -> None:
    # ``GRAPH <g> { OPTIONAL { … } }`` is one left join in one graph.
    result = translate_case(store, "SELECT ?m WHERE { GRAPH :g1 { OPTIONAL { :a :email ?m } } }")
    assert "http://ex.org/g1" in result.bind_vars.values()


@pytest.mark.parametrize("store", STORES)
def test_a_sub_select_after_the_graph_is_bound_is_correlated_with_it(store: Store) -> None:
    # The sub-query reads the graph the outer row is in, so its LIMIT runs
    # per graph (rows checked in the cross ``subselect_limit_after`` case).
    query = "SELECT ?g ?t ?s WHERE { GRAPH ?g { ?t :email ?e { SELECT ?s WHERE { ?s :name ?n } LIMIT 1 } } }"
    aql = translate_case(store, query).aql
    outer = re.search(r"FOR (\w+) IN @@\w+", aql)
    assert outer is not None, aql
    sub = aql[aql.index("IN (") :]
    assert re.search(rf"FILTER \w+\._graph == {outer.group(1)}\._graph", sub), aql
    assert "LIMIT" in sub


@pytest.mark.parametrize("store", STORES)
def test_an_optional_subquery_is_matched_in_the_rows_graph(store: Store) -> None:
    # The body names no ?g, but it is matched in the outer row's graph, not
    # in any named graph (review of #198).
    query = "SELECT ?g ?s ?n WHERE { GRAPH ?g { ?s :email ?e OPTIONAL { :c :name ?n } } }"
    aql = translate_case(store, query).aql
    sub = aql[aql.index("LET ") :]
    assert re.search(r"FILTER \w+\._graph == \w+\._graph", sub), aql


def test_the_probe_and_the_sub_select_carry_the_graph_scope() -> None:
    query = (
        "SELECT ?s WHERE { GRAPH :g1 { ?s :email ?e FILTER EXISTS { ?s :name ?n } "
        "{ SELECT ?s WHERE { ?s :name ?k } } } }"
    )
    result = translate_case(SPLIT["pg"], query)
    graph_binds = [k for k, v in result.bind_vars.items() if v == "http://ex.org/g1"]
    # The outer scan, the EXISTS probe and the sub-SELECT each test the graph.
    assert sum(result.aql.count(f"._graph == @{k}") for k in graph_binds) == 3
