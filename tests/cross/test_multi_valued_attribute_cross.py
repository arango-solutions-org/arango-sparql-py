"""Cross-validation: an attribute declared multi-valued is one triple per element (CDF #220).

The mapping marks one attribute as holding a list (CSI 1.2 ``multiValued``,
``phys:multiValued`` in OWL) and leaves the global ``fan_out_list_values`` off.
That attribute reads one row per element, so a constant object, a FILTER and a
count see elements; every other attribute keeps whole-value equality, which an
index can serve. The same documents go into pyoxigraph as RDF and into the AQL
interpreter as documents, and the bindings must agree.

The four probes are the ones contextual-data-fabric #220 reported on its demo
estate (``Document.questionsServed``): documents serving a question by constant
object and by FILTER, ``COUNT(?q)`` and ``COUNT(DISTINCT ?d)``.
"""

from __future__ import annotations

from typing import Any

import pytest

from arango_sparql.api import translate
from arango_sparql.translate.csi import mapping_bundle_from_csi
from arango_sparql.translate.resolver import SchemaResolver
from tests.helpers.aql_interp import run_aql_subset
from tests.helpers.oxi import assert_bindings_equal, drop_null_bindings, normalize_oxi_row, oxi_bindings

oxi = pytest.importorskip("pyoxigraph", reason="pyoxigraph required for cross tests")

NS = "urn:arango-sparql:concept#"
PREFIX = f"PREFIX : <{NS}>\n"

#: The CSI CDF hands the translator: ``questionsServed`` is stored as
#: ``questions_served`` and holds a list; ``role`` is one value.
CSI: dict[str, Any] = {
    "csiVersion": "1.2",
    "conceptualModel": {
        "entities": [
            {
                "name": "Document",
                "properties": [
                    {"name": "questionsServed", "type": "string", "multiValued": True},
                    {"name": "role", "type": "string"},
                ],
            }
        ],
        "relationships": [],
    },
    "arangoPhysicalMapping": {
        "entities": {
            "Document": {
                "style": "COLLECTION",
                "collectionName": "documents",
                "properties": {
                    "questionsServed": {"field": "questions_served", "multiValued": True},
                    "role": {"field": "role", "indexed": True},
                },
            }
        },
        "relationships": {},
    },
    "provenance": {
        "producer": "arango-schema-analyzer",
        "direction": "reverse",
        "source": {"kind": "arango"},
    },
}

#: The same mapping as OWL, the analyzer's export (``phys:multiValued``).
ONTOLOGY_TTL = f"""
@prefix : <{NS}> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix phys: <http://arangodb.com/schema/physical#> .
:Document a owl:Class ; phys:collectionName "documents" ; phys:mappingStyle "COLLECTION" .
:questionsServed a owl:DatatypeProperty ; rdfs:domain :Document .
:questionsServed phys:attributeName "questions_served" .
:questionsServed phys:multiValued "true" .
:role a owl:DatatypeProperty ; rdfs:domain :Document .
"""

DATA_TTL = f"""
@prefix : <{NS}> .
:d1 a :Document ; :questionsServed "Q1", "Q2" ; :role "pdf" .
:d2 a :Document ; :questionsServed "Q2" ; :role "deck" .
:d3 a :Document ; :role "pdf" .
:d4 a :Document ; :questionsServed "Q3" ; :role "pdf" .
:d5 a :Document ; :role "memo" .
:d6 a :Document ; :questionsServed "Q1", "Q4", "Q5" ; :role "deck" .
"""

DOCS: dict[str, list[dict[str, Any]]] = {
    "documents": [
        {"_id": "documents/d1", "_uri": NS + "d1", "questions_served": ["Q1", "Q2"], "role": "pdf"},
        {"_id": "documents/d2", "_uri": NS + "d2", "questions_served": ["Q2"], "role": "deck"},
        # An empty list is no value: not a document "with a value".
        {"_id": "documents/d3", "_uri": NS + "d3", "questions_served": [], "role": "pdf"},
        # A scalar where a list is declared is one value.
        {"_id": "documents/d4", "_uri": NS + "d4", "questions_served": "Q3", "role": "pdf"},
        {"_id": "documents/d5", "_uri": NS + "d5", "role": "memo"},
        {"_id": "documents/d6", "_uri": NS + "d6", "questions_served": ["Q1", "Q4", "Q5"], "role": "deck"},
    ]
}

PROBES = [
    pytest.param('SELECT ?d WHERE { ?d a :Document ; :questionsServed "Q2" }', id="constant_object"),
    pytest.param('SELECT ?d WHERE { ?d a :Document ; :questionsServed ?q FILTER(?q = "Q2") }', id="filter"),
    pytest.param(
        "SELECT (COUNT(?q) AS ?n) WHERE { ?d a :Document ; :questionsServed ?q }", id="count_values"
    ),
    pytest.param(
        "SELECT (COUNT(DISTINCT ?d) AS ?n) WHERE { ?d a :Document ; :questionsServed ?q }",
        id="count_documents",
    ),
    pytest.param("SELECT ?d ?q WHERE { ?d a :Document ; :questionsServed ?q }", id="bind_each_element"),
    pytest.param('SELECT ?d WHERE { ?d a :Document ; :role "pdf" }', id="scalar_constant"),
    pytest.param(
        "SELECT ?d ?q WHERE { ?d a :Document ; :role ?r OPTIONAL { ?d :questionsServed ?q } }",
        id="optional_list",
    ),
    pytest.param(
        "SELECT ?q (COUNT(?d) AS ?n) WHERE { ?d a :Document ; :questionsServed ?q } GROUP BY ?q",
        id="group_by_element",
    ),
    pytest.param(
        "SELECT ?d ?e WHERE { ?d a :Document ; :questionsServed ?q . ?e a :Document ; :questionsServed ?q . FILTER(?d != ?e) }",
        id="join_on_an_element",
    ),
]


@pytest.fixture(scope="module")
def oxi_store() -> Any:
    store = oxi.Store()
    store.load(DATA_TTL.encode("utf-8"), oxi.RdfFormat.TURTLE)
    return store


def _resolvers() -> list[Any]:
    return [
        pytest.param(lambda: SchemaResolver.from_mapping_bundle(mapping_bundle_from_csi(CSI)), id="csi"),
        pytest.param(lambda: SchemaResolver.from_turtle(ONTOLOGY_TTL), id="owl"),
    ]


@pytest.mark.cross
@pytest.mark.parametrize("make_resolver", _resolvers())
@pytest.mark.parametrize("sparql", PROBES)
def test_a_declared_list_attribute_matches_oxigraph(oxi_store: Any, make_resolver: Any, sparql: str) -> None:
    resolver = make_resolver()
    assert resolver.fan_out_list_values is False
    result = translate(PREFIX + sparql, resolver=resolver)
    actual = [drop_null_bindings(r) for r in run_aql_subset(result.aql, result.bind_vars, DOCS)]
    expected = [normalize_oxi_row(r) for r in oxi_bindings(oxi_store, PREFIX + sparql)]
    assert_bindings_equal(expected, actual)
