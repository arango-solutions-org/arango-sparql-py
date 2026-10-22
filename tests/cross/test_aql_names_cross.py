"""Cross-validation: names ArangoDB cannot take bare (issues #75, #76).

The translated AQL runs through :mod:`tests.helpers.aql_interp` and must give
the bindings pyoxigraph gives on the same triples. The interpreter refuses the
bare spellings ArangoDB refuses (``doc1._2``, ``doc1.filter``, ``{ _2: … }``;
ERR 1501 on the server), so before the fix every case here failed with that
refusal rather than silently "passing" AQL the server would reject — the
infidelity that let both issues through the existing cross tests.

Data: documents whose fields are named ``_2``, ``filter``, ``desc``, ``graph``,
``in`` and ``count``, and SPARQL variables named the same way, plus an RPT
triples table whose mapping columns are named ``graph`` / ``in`` / ``_2``.
"""

from __future__ import annotations

from typing import Any

import pytest

from arango_sparql.api import translate
from arango_sparql.translate.resolver import SchemaResolver
from tests.helpers.aql_interp import run_aql_subset
from tests.helpers.oxi import assert_bindings_equal, assert_bindings_equal_ordered, oxi_bindings
from tests.helpers.oxi import drop_null_bindings as _normalize_arango_row
from tests.helpers.oxi import normalize_oxi_row as _normalize_oxi_row

oxi = pytest.importorskip("pyoxigraph", reason="pyoxigraph required for cross tests")

ONTOLOGY_TTL = """
@prefix : <http://ex.org/> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix phys: <https://arango.solutions/phys#> .
:Doc a owl:Class ; phys:mappingStyle "COLLECTION" ; phys:collectionName "Docs" .
"""

DATA_TTL = """
@prefix : <http://ex.org/> .
:a a :Doc ; :_2 "second-a" ; :filter 3 ; :desc "alpha" ; :graph "g1" ; :in 5 ; :count 10 .
:b a :Doc ; :_2 "second-b" ; :filter 1 ; :desc "beta"  ; :graph "g1" ; :count 20 .
:c a :Doc ;                  :filter 2 ;                  :graph "g2" ; :in 5 .
"""

ARANGO_DOCS: dict[str, list[dict[str, Any]]] = {
    "Docs": [
        {
            "_uri": "http://ex.org/a",
            "_2": "second-a",
            "filter": 3,
            "desc": "alpha",
            "graph": "g1",
            "in": 5,
            "count": 10,
        },
        {
            "_uri": "http://ex.org/b",
            "_2": "second-b",
            "filter": 1,
            "desc": "beta",
            "graph": "g1",
            "count": 20,
        },
        {"_uri": "http://ex.org/c", "filter": 2, "graph": "g2", "in": 5},
    ],
}

P = "PREFIX : <http://ex.org/> "

CASES = [
    pytest.param(P + "SELECT ?_2 WHERE { ?_2 a :Doc }", id="underscore_digit_variable"),
    pytest.param(P + "SELECT ?_ WHERE { ?_ a :Doc }", id="underscore_only_variable"),
    pytest.param(P + "SELECT ?2 WHERE { ?2 a :Doc }", id="digit_variable"),
    pytest.param(P + "SELECT ?desc WHERE { ?desc a :Doc }", id="keyword_variable"),
    pytest.param(P + "SELECT ?d ?v WHERE { ?d a :Doc ; :_2 ?v }", id="underscore_digit_attribute"),
    pytest.param(P + "SELECT ?d ?f WHERE { ?d a :Doc ; :filter ?f }", id="keyword_attribute"),
    pytest.param(
        P + "SELECT ?d ?filter WHERE { ?d a :Doc ; :filter ?filter }", id="keyword_attribute_and_variable"
    ),
    pytest.param(P + "SELECT ?d WHERE { ?d a :Doc ; :in 5 }", id="keyword_attribute_constant"),
    pytest.param(
        P + "SELECT ?d WHERE { ?d a :Doc ; :filter ?f FILTER(?f > 1) }", id="keyword_attribute_in_filter"
    ),
    pytest.param(
        P + "SELECT ?d ?g WHERE { ?d a :Doc OPTIONAL { ?d :desc ?g } }", id="keyword_attribute_optional"
    ),
    pytest.param(
        P + "SELECT ?graph (COUNT(?d) AS ?_n) WHERE { ?d a :Doc ; :graph ?graph } GROUP BY ?graph",
        id="group_by_keyword_variable",
    ),
    pytest.param(P + "SELECT ?d ?count WHERE { ?d a :Doc ; :count ?count }", id="context_word_stays_bare"),
    pytest.param(P + "SELECT ?desc WHERE { VALUES ?desc { 1 2 } }", id="values_keyword_variable"),
]


@pytest.fixture(scope="module")
def oxi_store() -> Any:
    store = oxi.Store()
    store.load(DATA_TTL.encode("utf-8"), oxi.RdfFormat.TURTLE)
    return store


def _run(
    sparql: str, ontology: str = ONTOLOGY_TTL, docs: dict[str, list[dict[str, Any]]] = ARANGO_DOCS
) -> list:
    result = translate(sparql, resolver=SchemaResolver.from_turtle(ontology))
    return [_normalize_arango_row(r) for r in run_aql_subset(result.aql, result.bind_vars, docs)]


@pytest.mark.parametrize("sparql", CASES)
def test_names_match_oxigraph(oxi_store: Any, sparql: str) -> None:
    expected = [_normalize_oxi_row(r) for r in oxi_bindings(oxi_store, sparql)]
    assert_bindings_equal(expected, _run(sparql))


def test_order_by_keyword_attribute_matches_oxigraph(oxi_store: Any) -> None:
    sparql = P + "SELECT ?d ?f WHERE { ?d a :Doc ; :filter ?f } ORDER BY DESC(?f)"
    expected = [_normalize_oxi_row(r) for r in oxi_bindings(oxi_store, sparql)]
    assert_bindings_equal_ordered(expected, _run(sparql))


# RPT: the mapping's own column names are keywords / underscore-digit names.
_RPT_ONTOLOGY = """
@prefix : <http://ex.org/> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix phys: <https://arango.solutions/phys#> .
:Fact a owl:Class ; phys:mappingStyle "RPT" ; phys:triplesCollection "facts" ;
  phys:subjectColumn "subject" ; phys:predicateColumn "graph" ;
  phys:objectUriColumn "in" ; phys:objectValueColumn "_2" .
"""
_RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"
_RPT_DOCS: dict[str, list[dict[str, Any]]] = {
    "facts": [
        {"subject": "http://ex.org/f1", "graph": _RDF_TYPE, "in": "http://ex.org/Fact"},
        {"subject": "http://ex.org/f2", "graph": _RDF_TYPE, "in": "http://ex.org/Fact"},
        {"subject": "http://ex.org/f1", "graph": "http://ex.org/label", "_2": "one"},
        {"subject": "http://ex.org/f2", "graph": "http://ex.org/label", "_2": "two"},
    ],
}
_RPT_DATA = """
@prefix : <http://ex.org/> .
:f1 a :Fact ; :label "one" .
:f2 a :Fact ; :label "two" .
"""


def test_rpt_keyword_columns_match_oxigraph() -> None:
    store = oxi.Store()
    store.load(_RPT_DATA.encode("utf-8"), oxi.RdfFormat.TURTLE)
    sparql = P + "SELECT ?s ?v WHERE { ?s a :Fact ; :label ?v }"
    expected = [_normalize_oxi_row(r) for r in oxi_bindings(store, sparql)]
    assert_bindings_equal(expected, _run(sparql, _RPT_ONTOLOGY, _RPT_DOCS))


@pytest.mark.parametrize(
    "aql, message",
    [
        ("FOR doc1 IN @@c\nRETURN { v: doc1._2 }", "bare attribute read doc1._2"),
        ("FOR doc1 IN @@c\nRETURN { v: doc1.filter }", "bare attribute read doc1.filter"),
        ("FOR doc1 IN @@c\nRETURN { _2: doc1.name }", "bare object key '_2'"),
        ("FOR doc1 IN @@c\nRETURN { desc: doc1.name }", "bare object key 'desc'"),
    ],
)
def test_interpreter_refuses_what_arangodb_refuses(aql: str, message: str) -> None:
    # Fidelity guard: the interpreter must not evaluate AQL the server rejects.
    with pytest.raises(AssertionError, match=message):
        run_aql_subset(aql, {"@c": "Docs"}, ARANGO_DOCS)
