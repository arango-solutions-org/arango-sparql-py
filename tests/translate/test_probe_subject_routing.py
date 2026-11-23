"""Issue #90 — a MINUS / EXISTS probe reads a subject typed outside it from
the subject's class collection.

When no probe triple's ``rdfs:domain`` routes the subject (here ``:age``,
which the mapping does not declare), the probe opened the default collection
``Document``: a missing-collection error where it does not exist, wrong rows
where it does, and a refusal under strict subject resolution although the
outer pattern types the subject. On a document mapping the same BGP outside a
probe reads the class collection; each case here must answer as that does.

A mapping with a triples table keeps #191's routing inside a probe: a fact
about a typed document subject can be a triples-table row, so the probe reads
every store, as it did before this fix. An edge predicate is not #191's to
route, so there the probe walks the edge from the subject's home, as on a
document mapping.
"""

from __future__ import annotations

from typing import Any

import pytest

from arango_sparql.api import translate
from arango_sparql.errors import SchemaResolutionError
from arango_sparql.translate.mapping import MappingBundle, MappingSource
from arango_sparql.translate.resolver import SchemaResolver
from tests.helpers.aql_interp import run_aql_subset

_C = "urn:arango-sparql:concept#"
_BUNDLE = MappingBundle(
    conceptual_schema={"entities": [{"name": "Person", "properties": [{"name": "name"}]}]},
    physical_mapping={
        "entities": {
            "Person": {
                "style": "COLLECTION",
                "collectionName": "people",
                "properties": {"name": {"field": "name"}},
            }
        },
        "relationships": {},
    },
    source=MappingSource(kind="manual"),
)
# A document in ``Document`` that the outer subject's identity matches: read
# from there, the probe answers from the wrong store.
_DOCS: dict[str, list[dict[str, Any]]] = {
    "people": [
        {"_id": "people/a", "_uri": "http://ex.org/a", "name": "A", "age": 7},
        {"_id": "people/b", "_uri": "http://ex.org/b", "name": "B"},
    ],
    "Document": [{"_id": "Document/b", "_uri": "http://ex.org/b", "age": 9}],
}
_CASES = [
    ("FILTER EXISTS { ?p :age ?c }", ["http://ex.org/a"]),
    ("FILTER NOT EXISTS { ?p :age ?c }", ["http://ex.org/b"]),
    ("MINUS { ?p :age ?c }", ["http://ex.org/b"]),
]


def _translate(body: str, *, strict: bool) -> Any:
    resolver = SchemaResolver.from_mapping_bundle(_BUNDLE)
    resolver.strict_subject_resolution = strict
    return translate(f"PREFIX : <{_C}>\nSELECT ?p WHERE {{ ?p a :Person {body} }}", resolver=resolver)


@pytest.mark.parametrize("strict", [False, True], ids=["default", "strict"])
@pytest.mark.parametrize(("body", "expected"), _CASES, ids=["exists", "not-exists", "minus"])
def test_probe_reads_an_outer_typed_subject_from_its_class(
    body: str, expected: list[str], strict: bool
) -> None:
    result = _translate(body, strict=strict)
    assert "Document" not in result.bind_vars.values()
    assert sorted(r["p"] for r in run_aql_subset(result.aql, result.bind_vars, _DOCS)) == expected


def test_probe_subject_typed_nowhere_still_takes_the_default_contract() -> None:
    # A subject only the probe uses has no outer home: non-strict keeps the
    # documented default-collection fallback, strict refuses (#90's second action).
    query = f"PREFIX : <{_C}>\nSELECT ?p WHERE {{ ?p a :Person FILTER EXISTS {{ ?q :age ?c }} }}"
    resolver = SchemaResolver.from_mapping_bundle(_BUNDLE)
    assert "Document" in translate(query, resolver=resolver).bind_vars.values()
    resolver = SchemaResolver.from_mapping_bundle(_BUNDLE)
    resolver.strict_subject_resolution = True
    with pytest.raises(SchemaResolutionError, match="cannot route subject variable 'q'"):
        translate(query, resolver=resolver)


def test_probe_on_an_outer_typed_constant_subject_reads_its_class() -> None:
    query = f"PREFIX : <{_C}>\nSELECT * WHERE {{ <http://ex.org/a> a :Person FILTER EXISTS {{ <http://ex.org/a> :age ?c }} }}"
    for strict in (False, True):
        resolver = SchemaResolver.from_mapping_bundle(_BUNDLE)
        resolver.strict_subject_resolution = strict
        result = translate(query, resolver=resolver)
        assert "Document" not in result.bind_vars.values()
        assert len(run_aql_subset(result.aql, result.bind_vars, _DOCS)) == 1


# #191's mixed mapping: people are triples-table rows, companies documents.
# ``:acme``'s email is a triples-table row only, so a probe on a typed company
# must read the table too.
_EX = "http://ex.org/"
_MIXED_HEAD = """
@prefix : <http://ex.org/> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix phys: <https://arango.solutions/phys#> .
:Person a owl:Class ; phys:mappingStyle "RPT" ; phys:triplesCollection "T191" .
:Company a owl:Class ; phys:mappingStyle "COLLECTION" ; phys:collectionName "C191" .
:cname a owl:DatatypeProperty ; rdfs:domain :Company ; phys:attributeName "cname" .
:partner a owl:ObjectProperty ; phys:edgeCollectionName "P191" .
"""
_MIXED_ONTOLOGIES = {
    "undeclared": _MIXED_HEAD,
    "no_domain": _MIXED_HEAD + ":email a owl:DatatypeProperty .\n",
}
_MIXED_DOCS: dict[str, list[dict[str, Any]]] = {
    "T191": [
        {"subject_uri": _EX + "b", "predicate": _EX + "email", "object_value": "b@x.org"},
        {"subject_uri": _EX + "acme", "predicate": _EX + "email", "object_value": "info@acme.example"},
    ],
    "C191": [
        {"_id": "C191/acme", "_key": "acme", "_uri": _EX + "acme", "cname": "Acme"},
        {"_id": "C191/globex", "_key": "globex", "_uri": _EX + "globex", "cname": "Globex"},
    ],
    "P191": [{"_from": "C191/acme", "_to": "C191/globex"}],
    # A decoy in the default collection: an edge probe reading there finds no home.
    "D191": [{"_id": "D191/globex", "_uri": _EX + "globex"}],
}


@pytest.mark.parametrize("strict", [False, True], ids=["default", "strict"])
@pytest.mark.parametrize("ontology", sorted(_MIXED_ONTOLOGIES))
@pytest.mark.parametrize(
    ("body", "expected"),
    [("FILTER EXISTS { ?c :email ?m }", ["acme"]), ("MINUS { ?c :email ?m }", ["globex"])],
    ids=["exists", "minus"],
)
def test_probe_on_a_typed_document_in_a_mixed_mapping_reads_every_store(
    body: str, expected: list[str], ontology: str, strict: bool
) -> None:
    resolver = SchemaResolver.from_turtle(_MIXED_ONTOLOGIES[ontology], default_collection="D191")
    resolver.strict_subject_resolution = strict
    result = translate(f"PREFIX : <{_EX}>\nSELECT ?c WHERE {{ ?c a :Company {body} }}", resolver=resolver)
    rows = run_aql_subset(result.aql, result.bind_vars, _MIXED_DOCS)
    assert sorted(r["c"] for r in rows) == [_EX + k for k in expected]


@pytest.mark.parametrize("strict", [False, True], ids=["default", "strict"])
@pytest.mark.parametrize("ontology", sorted(_MIXED_ONTOLOGIES))
@pytest.mark.parametrize(
    ("body", "expected"),
    [("FILTER EXISTS { ?c :partner ?x }", ["acme"]), ("MINUS { ?c :partner ?x }", ["globex"])],
    ids=["exists", "minus"],
)
def test_edge_probe_on_a_typed_document_in_a_mixed_mapping_walks_from_its_home(
    body: str, expected: list[str], ontology: str, strict: bool
) -> None:
    # #191 leaves edge predicates to the visitor, so without the home the
    # probe read the default collection (non-strict) or was refused (strict).
    resolver = SchemaResolver.from_turtle(_MIXED_ONTOLOGIES[ontology], default_collection="D191")
    resolver.strict_subject_resolution = strict
    result = translate(f"PREFIX : <{_EX}>\nSELECT ?c WHERE {{ ?c a :Company {body} }}", resolver=resolver)
    assert "D191" not in result.bind_vars.values()
    rows = run_aql_subset(result.aql, result.bind_vars, _MIXED_DOCS)
    assert sorted(r["c"] for r in rows) == [_EX + k for k in expected]
