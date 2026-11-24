"""A read of classes scoped by different tenant fields holds each document to its own (#218).

``Person`` documents carry the tenant in ``org`` and ``Doc`` documents in
``ws``, both under the ``Org`` root; ``Tag`` is not tenant-scoped. A read
that covers several of these classes (a property whose domain names them,
an LPG collection holding them, a mixed-store routed read, a union of their
collections) filtered every document on the first class's field: Person
rows were dropped, and a Person whose ``ws`` happened to equal the tenant
passed whatever its ``org`` (a leak). Each query runs for tenant ``t1`` on
ArangoDB and must match pyoxigraph over the facts ``t1`` may see.

The data carries both traps: ``p2`` is a t2 Person with ``ws: t1``, and ``d2``
a t2 Doc with ``org: t1``. ``pd`` and ``pe`` are labelled Person and Doc, and
carry their tenant in both fields: ``pd`` is t1's, ``pe`` is t2's.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import pytest

from arango_sparql.api import translate
from arango_sparql.translate.resolver import SchemaResolver
from tests.helpers.oxi import drop_null_bindings, normalize_oxi_row, oxi_bindings
from tests.integration.conftest import (
    DEFAULT_ARANGO_DB,
    DEFAULT_ARANGO_PASSWORD,
    DEFAULT_ARANGO_URL,
    DEFAULT_ARANGO_USER,
    arangodb_reachable,
    ensure_test_database,
    integration_enabled,
    try_boot_arangodb_via_compose,
)

pytestmark = pytest.mark.integration

oxi = pytest.importorskip("pyoxigraph", reason="pyoxigraph is the ground truth for these tests")

EX = "http://ex.org/"
_NODES, _TRIPLES, _PERSON, _DOC = (
    "TenantClassNodes",
    "TenantClassTriples",
    "TenantClassPerson",
    "TenantClassDoc",
)

_HEAD = f"""
@prefix :     <{EX}> .
@prefix owl:  <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix phys: <https://arango.solutions/phys#> .
:name a owl:DatatypeProperty ; rdfs:domain :Person, :Doc ; phys:attributeName "name" .
:title a owl:DatatypeProperty ; rdfs:domain :Person, :Tag ; phys:attributeName "title" .
:nick a owl:DatatypeProperty ; phys:attributeName "nick" .
:Secret a owl:Class ; phys:collectionName "{_NODES}" ; phys:mappingStyle "LABEL" ; phys:typeField "label" ;
    phys:typeValue "Secret" ; phys:tenantField "org" ; phys:tenantEntity "Org" .
:Agent a owl:Class ; phys:mappingStyle "UNION" .
:Person rdfs:subClassOf :Agent . :Doc rdfs:subClassOf :Agent . :Tag rdfs:subClassOf :Agent .
"""
_LABELS = f"""
:Person a owl:Class ; phys:collectionName "{_NODES}" ; phys:mappingStyle "LABEL" ; phys:typeField "label" ;
    phys:typeValue "Person" ; phys:tenantField "org" ; phys:tenantEntity "Org" .
:Doc a owl:Class ; phys:collectionName "{_NODES}" ; phys:mappingStyle "LABEL" ; phys:typeField "label" ;
    phys:typeValue "Doc" ; phys:tenantField "ws" ; phys:tenantEntity "Org" .
:Tag a owl:Class ; phys:collectionName "{_NODES}" ; phys:mappingStyle "LABEL" ; phys:typeField "label" ;
    phys:typeValue "Tag" .
"""
_ONTOLOGIES = {
    # One LPG collection holds all three classes.
    "lpg": _HEAD + _LABELS,
    # The same, plus a triples table: :name is a routed read (mixed_store).
    "mixed": _HEAD
    + _LABELS
    + f"""
:Note a owl:Class ; phys:mappingStyle "RPT" ; phys:triplesCollection "{_TRIPLES}" .
:name rdfs:domain :Note .
""",
    # A collection per class: the domain spans collections.
    "pg": _HEAD
    + f"""
:Person a owl:Class ; phys:collectionName "{_PERSON}" ; phys:tenantField "org" ; phys:tenantEntity "Org" .
:Doc a owl:Class ; phys:collectionName "{_DOC}" ; phys:tenantField "ws" ; phys:tenantEntity "Org" .
:Tag a owl:Class ; phys:collectionName "{_NODES}" ; phys:mappingStyle "LABEL" ; phys:typeField "label" ;
    phys:typeValue "Tag" .
""",
}

# key -> document fields; ``visible`` is whether t1 may see it.
_PEOPLE = {
    "p1": {"org": "t1", "name": "P1", "title": "T1", "nick": "K1"},
    "p2": {"org": "t2", "ws": "t1", "name": "P2", "title": "T2"},
}
_DOCS = {
    "d1": {"ws": "t1", "name": "D1"},
    "d2": {"ws": "t2", "org": "t1", "name": "D2"},
}
_TAGS = {"g1": {"title": "G1"}}
# A t2 document of a class no query here names, holding a copy of p1's IRI:
# a read that looks copies up by ``_uri`` met it with no class test, and the
# first head of #228 let it through (its review).
_SECRET = {"xs": {"_uri": EX + "p1", "org": "t2", "name": "LEAK-NAME", "nick": "LEAK-NICK"}}
_BOTH = {
    "pd": {"org": "t1", "ws": "t1", "name": "PD", "title": "TD"},
    "pe": {"org": "t2", "ws": "t2", "name": "PE"},
}
_NOTES = {"n1": "N1"}

_QUERIES = {
    "name": "SELECT ?s ?n WHERE { ?s :name ?n }",
    "title": "SELECT ?s ?t WHERE { ?s :title ?t }",
    "name_then_title": "SELECT ?s ?n ?t WHERE { ?s :name ?n OPTIONAL { ?s :title ?t } }",
    "name_exists_title": "SELECT ?s WHERE { ?s :name ?n FILTER EXISTS { ?s :title ?t } }",
    "person": "SELECT ?s WHERE { ?s a :Person }",
    "doc": "SELECT ?s WHERE { ?s a :Doc }",
    # A supertype over all three classes (refused on main for pg, which
    # spans collections: "different tenant scoping").
    "agent": "SELECT ?s WHERE { ?s a :Agent }",
    "agent_name": "SELECT ?s ?n WHERE { ?s a :Agent ; :name ?n }",
    # Read from every copy of the IRI, the Secret one included.
    "agent_nick": "SELECT ?s ?k WHERE { ?s a :Agent ; :nick ?k }",
    "agent_name_nick": "SELECT ?s ?n ?k WHERE { ?s a :Agent ; :name ?n ; :nick ?k }",
}


def _t1_data(mapping: str) -> str:
    lines = [f"@prefix : <{EX}> ."]
    lines += [
        ':p1 a :Person, :Agent ; :name "P1" ; :title "T1" ; :nick "K1" .',
        ':d1 a :Doc, :Agent ; :name "D1" .',
        ':g1 a :Tag, :Agent ; :title "G1" .',
    ]
    if mapping != "pg":
        lines.append(':pd a :Person, :Doc, :Agent ; :name "PD" ; :title "TD" .')
    if mapping == "mixed":
        lines += [f':{k} a :Note ; :name "{v}" .' for k, v in _NOTES.items()]
    return "\n".join(lines)


def _doc(key: str, fields: dict[str, Any], label: Any = None) -> dict[str, Any]:
    return {"_key": key, "_uri": EX + key, **fields, **({"label": label} if label else {})}


def _collections() -> dict[str, list[dict[str, Any]]]:
    nodes = (
        [_doc(k, f, "Person") for k, f in _PEOPLE.items()]
        + [_doc(k, f, "Doc") for k, f in _DOCS.items()]
        + [_doc(k, f, "Tag") for k, f in _TAGS.items()]
        + [_doc(k, f, ["Person", "Doc"]) for k, f in _BOTH.items()]
        + [{"_key": k, **f, "label": "Secret"} for k, f in _SECRET.items()]
    )
    triples = [
        row
        for k, v in _NOTES.items()
        for row in (
            {
                "subject_uri": EX + k,
                "predicate": "http://www.w3.org/1999/02/22-rdf-syntax-ns#type",
                "object_uri": EX + "Note",
            },
            {"subject_uri": EX + k, "predicate": EX + "name", "object_value": v},
        )
    ]
    return {
        _NODES: nodes,
        _TRIPLES: triples,
        _PERSON: [_doc(k, f) for k, f in _PEOPLE.items()],
        _DOC: [_doc(k, f) for k, f in _DOCS.items()],
    }


@pytest.fixture(scope="module")
def db() -> Iterator[Any]:
    if not integration_enabled():
        pytest.skip("set RUN_INTEGRATION=1 to enable integration tests")
    if not arangodb_reachable() and not try_boot_arangodb_via_compose():
        pytest.skip(f"ArangoDB at {DEFAULT_ARANGO_URL} is unreachable and could not be booted")
    from arango import ArangoClient

    ensure_test_database()
    handle = ArangoClient(hosts=DEFAULT_ARANGO_URL).db(
        DEFAULT_ARANGO_DB, username=DEFAULT_ARANGO_USER, password=DEFAULT_ARANGO_PASSWORD
    )
    for name, docs in _collections().items():
        if handle.has_collection(name):
            handle.delete_collection(name)
        handle.create_collection(name).insert_many(docs)
    yield handle
    for name in _collections():
        handle.delete_collection(name)


def _multiset(rows: list[dict[str, Any]]) -> list[str]:
    return sorted(json.dumps(r, sort_keys=True) for r in rows)


@pytest.mark.parametrize("mapping", sorted(_ONTOLOGIES))
@pytest.mark.parametrize("query", list(_QUERIES.values()), ids=list(_QUERIES))
def test_each_document_is_held_to_its_own_class_tenant_field(db: Any, mapping: str, query: str) -> None:
    full = f"PREFIX : <{EX}>\n{query}"
    store = oxi.Store()
    store.load(_t1_data(mapping).encode("utf-8"), oxi.RdfFormat.TURTLE)
    res = translate(full, resolver=SchemaResolver.from_turtle(_ONTOLOGIES[mapping]), tenant_id="t1")
    actual = [drop_null_bindings(r) for r in db.aql.execute(res.aql, bind_vars=res.bind_vars)]
    expected = [normalize_oxi_row(r) for r in oxi_bindings(store, full)]
    assert expected, "the query should have t1 answers, or it tests nothing"
    assert _multiset(actual) == _multiset(expected)
