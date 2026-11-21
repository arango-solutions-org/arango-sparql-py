"""Named graphs on a read of an unplaced subject from several stores (#191,
review of #199).

``:acme`` has emails in a triples table and a document per graph: one in
``:g1``, one in ``:g2``, one in the default graph, and ``"both"`` stored in
``:g1`` and ``:g2``. The routed reader's arms filter on each store's graph
field.

* Inside ``GRAPH <iri>``, and outside any GRAPH in either default-graph
  mode, the answers match pyoxigraph. With the default graph the union of
  the named ones (lax mode), a triple stored in two graphs is two quads:
  each arm returns its graph field, so the set union keeps both.
* Inside ``GRAPH ?g``, the row's graph binds ``?g`` (#203). The arms did
  not bind it, so a read before any other in the group joined across
  graphs and took in the default graph, silently; #199 refused it until
  #198 held every arm to the named graphs.

Run through the in-memory interpreter here and on ArangoDB in
``tests/integration/test_mixed_store_live.py``.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from arango_sparql.api import translate
from arango_sparql.translate.resolver import SchemaResolver
from tests.helpers.aql_interp import run_aql_subset
from tests.helpers.oxi import assert_bindings_equal, drop_null_bindings, normalize_oxi_row, oxi_bindings

oxi = pytest.importorskip("pyoxigraph", reason="pyoxigraph required for cross tests")

EX = "http://ex.org/"
PREFIX = f"PREFIX : <{EX}>\n"
RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"
ONTOLOGY = """
@prefix : <http://ex.org/> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix phys: <https://arango.solutions/phys#> .
:Person a owl:Class ; phys:mappingStyle "RPT" ; phys:triplesCollection "TG191" .
:Company a owl:Class ; phys:mappingStyle "COLLECTION" ; phys:collectionName "CG191" .
:cname a owl:DatatypeProperty ; rdfs:domain :Company ; phys:attributeName "cname" .
:email a owl:DatatypeProperty ; phys:attributeName "email" .
"""
DEFAULT_COLLECTION = "DG191"

# (subject, predicate, object, object is an IRI, graph or None)
TABLE_FACTS = [
    ("a", "type", "Person", True, None),
    ("a", "worksFor", "acme", True, "g1"),
    ("a", "worksFor", "acme", True, "g2"),
    ("acme", "email", "g1mail", False, "g1"),
    ("acme", "email", "g2mail", False, "g2"),
    ("acme", "email", "dmail", False, None),
    ("acme", "email", "both", False, "g1"),
    ("acme", "email", "both", False, "g2"),
]
DOCS = [
    {"_key": "acme1", "_uri": EX + "acme", "_graph": EX + "g1", "email": "g1mail", "cname": "Acme1"},
    {"_key": "acme2", "_uri": EX + "acme", "_graph": EX + "g2", "email": "docg2", "cname": "Acme2"},
    {"_key": "acme0", "_uri": EX + "acme", "cname": "Acme0"},
]
DOC_FACTS = [
    ("acme", "email", "g1mail", False, "g1"),
    ("acme", "cname", "Acme1", False, "g1"),
    ("acme", "email", "docg2", False, "g2"),
    ("acme", "cname", "Acme2", False, "g2"),
    ("acme", "cname", "Acme0", False, None),
]

ANSWERED = [
    pytest.param("SELECT ?c ?m WHERE { GRAPH :g1 { ?c :email ?m } }", id="named_graph_first_seen"),
    pytest.param("SELECT ?m WHERE { GRAPH :g2 { :acme :email ?m } }", id="named_graph_constant"),
    pytest.param(
        "SELECT ?c ?m WHERE { :a :worksFor ?c . GRAPH :g2 { ?c :email ?m } }", id="named_graph_bound"
    ),
    pytest.param("SELECT ?m WHERE { :acme :email ?m }", id="default_graph_constant"),
    pytest.param(
        "SELECT ?c ?m WHERE { ?s a :Person . ?s :worksFor ?c . ?c :email ?m }", id="default_graph_chain"
    ),
]
# ``GRAPH ?g`` around a read of every store binds ?g from the row (#203).
GRAPH_VARIABLE = [
    pytest.param("SELECT ?g ?m WHERE { GRAPH ?g { ?c :email ?m } }", id="first_seen"),
    pytest.param("SELECT ?m WHERE { GRAPH ?g { ?c :email ?m } }", id="first_seen_unprojected"),
    pytest.param("SELECT ?c ?m WHERE { GRAPH ?g { ?c :cname ?n . ?c :email ?m } }", id="two_stores"),
    pytest.param("SELECT ?g ?m WHERE { GRAPH ?g { :acme :email ?m } }", id="constant"),
    pytest.param("SELECT ?g ?n WHERE { GRAPH ?g { :a :worksFor ?c . ?c :cname ?n } }", id="one_store"),
    pytest.param("SELECT ?g ?c ?m WHERE { GRAPH ?g { :a :worksFor ?c . ?c :email ?m } }", id="chain"),
    pytest.param("SELECT (COUNT(*) AS ?k) WHERE { GRAPH ?g { ?c :email ?m } }", id="count"),
    pytest.param("SELECT ?g ?m WHERE { VALUES ?g { :g1 :g2 } GRAPH ?g { ?c :email ?m } }", id="values_bound"),
    # The reviewer's: ?g bound by a type pattern in one GRAPH group, then
    # joined by a read of every store in the next (a default-graph row
    # leaked here until #198).
    pytest.param(
        "SELECT ?g ?m WHERE { GRAPH ?g { ?x a :Company } GRAPH ?g { :acme :email ?m } }", id="bound_by_type"
    ),
]
ANSWERED = ANSWERED + GRAPH_VARIABLE


def _row(s: str, p: str, o: str, iri: bool, g: str | None) -> dict[str, Any]:
    row: dict[str, Any] = {"subject_uri": EX + s, "predicate": RDF_TYPE if p == "type" else EX + p}
    row["object_uri" if iri else "object_value"] = EX + o if iri else o
    if g:
        row["_graph"] = EX + g
    return row


def _quad(s: str, p: str, o: str, iri: bool, g: str | None) -> str:
    predicate = f"<{RDF_TYPE}>" if p == "type" else f"<{EX}{p}>"
    obj = f"<{EX}{o}>" if iri else json.dumps(o)
    return f"<{EX}{s}> {predicate} {obj}{f' <{EX}{g}>' if g else ''} ."


def collections() -> dict[str, list[dict[str, Any]]]:
    """The stored rows and documents (the default collection is empty)."""
    return {"TG191": [_row(*f) for f in TABLE_FACTS], "CG191": DOCS, DEFAULT_COLLECTION: []}


def quads() -> str:
    """The same facts as N-Quads, with each document's class membership."""
    types = [("acme", "type", "Company", True, g) for g in ("g1", "g2", None)]
    return "\n".join(_quad(*f) for f in TABLE_FACTS + DOC_FACTS + types) + "\n"


def make_resolver(strict: bool, lax_default_graph: bool) -> SchemaResolver:
    resolver = SchemaResolver.from_turtle(
        ONTOLOGY, default_collection=DEFAULT_COLLECTION, default_graph_includes_named=lax_default_graph
    )
    resolver.strict_subject_resolution = strict
    return resolver


@pytest.mark.cross
@pytest.mark.parametrize(
    "lax_default_graph", [False, True], ids=["default_graph_alone", "union_default_graph"]
)
@pytest.mark.parametrize("strict", [False, True], ids=["library", "strict"])
@pytest.mark.parametrize("where", ANSWERED)
def test_a_routed_read_in_a_named_or_the_default_graph_matches_oxigraph(
    where: str, strict: bool, lax_default_graph: bool
) -> None:
    query = PREFIX + where
    result = translate(query, resolver=make_resolver(strict, lax_default_graph))
    actual = [drop_null_bindings(r) for r in run_aql_subset(result.aql, result.bind_vars, collections())]

    store = oxi.Store()
    store.load(quads().encode("utf-8"), oxi.RdfFormat.N_QUADS)
    expected = [
        normalize_oxi_row(r) for r in oxi_bindings(store, query, union_default_graph=lax_default_graph)
    ]
    assert_bindings_equal(expected, actual)
