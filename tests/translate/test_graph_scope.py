"""GRAPH scope: what the translation emits and what it refuses (#195).

``GRAPH ?g`` ranges over the named graphs only, and everything inside a
GRAPH (a MINUS / EXISTS probe, an OPTIONAL over a triples table, a
sub-SELECT) is matched in the scope's graph. The answers are checked against
pyoxigraph in ``tests/cross/test_multi_document_iri_cross.py`` (the
``graph_scope_*`` cases) and live in
``tests/integration/test_multi_document_iri_live.py``; this module pins the
shapes the translation refuses, which would otherwise be wrong answers.
"""

from __future__ import annotations

import re

import pytest

from arango_sparql.errors import UnsupportedSparqlError
from tests.cross.test_multi_document_iri_cross import SCOPE_LPG, SCOPE_PG, SCOPE_RPT, Store, translate_case

STORES = [
    pytest.param(SCOPE_PG, id="pg"),
    pytest.param(SCOPE_LPG, id="lpg"),
    pytest.param(SCOPE_RPT, id="rpt"),
]


@pytest.mark.parametrize("store", STORES)
def test_graph_variable_excludes_the_default_graph(store: Store) -> None:
    aql = translate_case(store, "SELECT ?g ?s WHERE { GRAPH ?g { ?s a :Person } }").aql
    assert "._graph != null" in aql


@pytest.mark.parametrize("store", STORES)
@pytest.mark.parametrize(
    "query, message",
    [
        pytest.param(
            "SELECT ?g WHERE { GRAPH ?g { OPTIONAL { :b :email ?m } } }",
            "GRAPH ?g needs a pattern outside OPTIONAL",
            id="only_optional",
        ),
        pytest.param(
            "SELECT ?g ?n WHERE { GRAPH ?g { OPTIONAL { ?s :name ?n } } }",
            "GRAPH ?g needs a pattern outside OPTIONAL",
            id="only_optional_binding_g",
        ),
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
    with pytest.raises(UnsupportedSparqlError, match=re.escape(message)):
        translate_case(store, query)


def test_a_triples_table_optional_before_the_graph_variable_is_bound_is_refused() -> None:
    # The cross-subject OPTIONAL scan would have to bind ?g per triple.
    query = "SELECT ?s ?x ?g ?m WHERE { ?s a :Person ; :knows ?x . GRAPH ?g { OPTIONAL { ?x :email ?m } } }"
    with pytest.raises(UnsupportedSparqlError, match="before any pattern binds"):
        translate_case(SCOPE_RPT, query)


@pytest.mark.parametrize("store", STORES)
def test_a_constant_graph_needs_no_binding_pattern(store: Store) -> None:
    # ``GRAPH <g> { OPTIONAL { … } }`` is one left join in one graph.
    result = translate_case(store, "SELECT ?m WHERE { GRAPH :g1 { OPTIONAL { :d :email ?m } } }")
    assert "http://ex.org/g1" in result.bind_vars.values()


@pytest.mark.parametrize("store", STORES)
def test_a_sub_select_after_the_graph_is_bound_may_limit_per_graph(store: Store) -> None:
    # Correlated with the earlier pattern's graph, so LIMIT runs per graph.
    query = "SELECT ?g ?s WHERE { GRAPH ?g { ?t :email ?e { SELECT ?s WHERE { ?s :name ?n } LIMIT 1 } } }"
    assert "LIMIT" in translate_case(store, query).aql


def test_the_probe_and_the_sub_select_carry_the_graph_scope() -> None:
    query = (
        "SELECT ?s WHERE { GRAPH :g1 { ?s :email ?e FILTER EXISTS { ?s :name ?n } "
        "{ SELECT ?s WHERE { ?s :name ?k } } } }"
    )
    result = translate_case(SCOPE_PG, query)
    graph_binds = [k for k, v in result.bind_vars.items() if v == "http://ex.org/g1"]
    # The outer scan, the EXISTS probe and the sub-SELECT each test the graph.
    assert sum(result.aql.count(f"._graph == @{k}") for k in graph_binds) == 3


@pytest.mark.parametrize("store", STORES)
def test_a_sub_select_alone_in_graph_binds_the_graph_variable(store: Store) -> None:
    # SPARQL 1.1 §18.6 joins each named graph's solutions with ?g -> that
    # graph, and rdflib binds ?g here; pyoxigraph leaves it unbound when the
    # sub-SELECT is the whole GRAPH body, so the cross cases
    # (``graph_scope_*_subselect_only``) compare the rows without ?g. The
    # sub-select projects the graph it read, and ?g is that column.
    aql = translate_case(store, "SELECT ?g ?s WHERE { GRAPH ?g { { SELECT ?s WHERE { ?s :name ?n } } } }").aql
    column = re.search(r"(_path_\d+): \w+\._graph \}", aql)
    assert column is not None, aql
    assert re.search(rf"RETURN \{{ g: row\d+\.{column.group(1)},", aql), aql
