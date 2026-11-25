"""Cross-validation: a FILTER reads only the variables its own group binds (#107).

SPARQL 1.1 §18.2.2 scopes a FILTER to its group. The translator visits a
nested group, a UNION arm or a MINUS's right side after the pattern around
it, so the filter used to read the outer bindings. Each shape runs through
the in-memory interpreter on the stores the neighbouring cross tests build
(a triples table, PG documents with a dedicated edge collection, LPG
documents with a typed generic edge collection) and is compared with
pyoxigraph on the same facts. The GRAPH shapes check the exception: the
active GRAPH variable stays visible to a filter inside it.
"""

from __future__ import annotations

from typing import Any

import pytest

from arango_sparql.api import translate
from arango_sparql.translate.resolver import SchemaResolver
from tests.cross.test_multi_document_iri_cross import oracle_rows, translate_case
from tests.cross.test_named_graph_scope_cross import MAPPINGS, SCOPE
from tests.cross.test_nested_optional_scope_cross import DATA, EX, MODELS
from tests.helpers.aql_interp import run_aql_subset
from tests.helpers.oxi import (
    assert_bindings_equal,
    drop_null_bindings,
    load_store_from_string,
    normalize_oxi_row,
    oxi_bindings,
)

pytest.importorskip("pyoxigraph", reason="pyoxigraph required for cross tests")

CASES = [
    # The issue's shape: ?s is unbound in the first arm, so the EXISTS is true
    # once anyone knows anyone and the arm joins with every person.
    pytest.param(
        "SELECT ?s WHERE { ?s a :Person { FILTER EXISTS { ?s :knows ?x } } UNION { ?s :email ?e } }",
        id="issue",
    ),
    # #204 case 2: ?x is unbound in the first arm, so ``?x = :c`` drops every row.
    pytest.param(
        "SELECT ?s ?x ?n ?e WHERE { ?s :knows ?x { ?s :name ?n FILTER(?x = :c) } UNION { ?s :email ?e } }",
        id="union_arm",
    ),
    # #209 shape 1: ?x is unbound on MINUS's right side, so nothing is removed.
    pytest.param(
        "SELECT ?s ?x WHERE { ?s a :Person OPTIONAL { ?s :knows ?x } MINUS { ?y :name ?n FILTER(?y = ?x) } }",
        id="minus_side",
    ),
]


@pytest.mark.cross
@pytest.mark.parametrize("ontology_ttl,docs_factory", MODELS)
@pytest.mark.parametrize("where", CASES)
def test_group_filter_scope_matches_oxigraph(ontology_ttl: str, docs_factory: Any, where: str) -> None:
    query = f"PREFIX : <{EX}> " + where
    result = translate(query, resolver=SchemaResolver.from_turtle(ontology_ttl))
    actual = [drop_null_bindings(r) for r in run_aql_subset(result.aql, result.bind_vars, docs_factory())]
    expected = [normalize_oxi_row(r) for r in oxi_bindings(load_store_from_string(DATA), query)]
    assert_bindings_equal(expected, actual)


@pytest.mark.cross
@pytest.mark.parametrize("mapping", MAPPINGS)
@pytest.mark.parametrize(
    "query",
    [
        pytest.param("SELECT ?g ?s WHERE { GRAPH ?g { ?s a :Person FILTER(?g = :g1) } }", id="graph_group"),
        pytest.param(
            "SELECT ?g ?s WHERE { GRAPH ?g { ?s a :Person { FILTER(?g = :g1) } } }", id="graph_nested_group"
        ),
    ],
)
def test_filter_on_the_active_graph_variable_matches_oxigraph(mapping: str, query: str) -> None:
    store = SCOPE[mapping]
    result = translate_case(store, query)
    rows = [drop_null_bindings(r) for r in run_aql_subset(result.aql, result.bind_vars, store.docs)]
    expected = oracle_rows(store, query)
    assert expected, "pyoxigraph keeps :g1's people; an empty answer would not discriminate"
    assert_bindings_equal(expected, rows)
