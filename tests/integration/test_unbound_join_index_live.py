"""Live: joining a variable that can be unbound keeps its index (review of #196).

An unbound variable joins any value (SPARQL 1.1 §18.3; #193), and the first
form of that join, ``FILTER (x == null || doc._uri == x || …)``, hid the
match from ArangoDB's optimizer: every row scanned the joined collection,
including rows where ``x`` was bound (4,000 people: 0.07 s became 4.8 s).
The join now splits the scan into an index lookup that runs only where ``x``
is bound and a scan that runs only where it is null
(``AqlQueryBuilder.split_scan_on_value``).

Here every person knows someone, so ``x`` is always bound at run time though
the translator must allow for it being unbound. The plan must look the
joined document up by index (an IndexNode on ``_uri`` or the primary
index), and running it must scan the collection fully only once: the
``?s a :Person`` scan. The answers are checked against pyoxigraph too.
Gated like the rest of the integration suite (``RUN_INTEGRATION=1``).
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import pytest

from arango_sparql.api import translate
from arango_sparql.translate.resolver import SchemaResolver
from tests.helpers.oxi import drop_null_bindings, load_store_from_string, normalize_oxi_row, oxi_bindings
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
#: Enough documents that a scan per row would be visible in the statistics.
PEOPLE = 300
_PERSONS = "UnboundIndexPerson"
_KNOWS = "UnboundIndexKnows"
_VERTICES = "UnboundIndexVertices"
_EDGES = "UnboundIndexEdges"

_HEAD = f"""
@prefix : <{EX}> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix phys: <https://arango.solutions/phys#> .
:email a owl:DatatypeProperty ; rdfs:domain :Person ; phys:attributeName "email" .
"""
PG_TTL = (
    _HEAD
    + f"""
:Person a owl:Class ; phys:collectionName "{_PERSONS}" .
:knows a owl:ObjectProperty ; phys:edgeCollectionName "{_KNOWS}" ; rdfs:domain :Person ; rdfs:range :Person .
"""
)
LPG_TTL = (
    _HEAD
    + f"""
:Person a owl:Class ; phys:collectionName "{_VERTICES}" ; phys:mappingStyle "LABEL" ;
    phys:typeField "type" ; phys:typeValue "Person" .
:knows a owl:ObjectProperty ; phys:edgeCollectionName "{_EDGES}" ; phys:mappingStyle "GENERIC_WITH_TYPE" ;
    phys:typeField "type" ; phys:typeValue "knows" ; rdfs:domain :Person ; rdfs:range :Person .
"""
)


def _person(i: int) -> dict[str, Any]:
    return {"_key": f"p{i}", "_uri": f"{EX}p{i}", **({"email": f"p{i}@x.org"} if i % 2 else {})}


def _facts() -> str:
    lines = [f"@prefix : <{EX}> ."]
    for i in range(PEOPLE):
        lines.append(f":p{i} a :Person ; :knows :p{(i + 1) % PEOPLE} .")
        if i % 2:
            lines.append(f':p{i} :email "p{i}@x.org" .')
    return "\n".join(lines)


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
    names = (_PERSONS, _KNOWS, _VERTICES, _EDGES)
    for name in names:
        if handle.has_collection(name):
            handle.delete_collection(name)
    persons = handle.create_collection(_PERSONS)
    vertices = handle.create_collection(_VERTICES)
    for collection in (persons, vertices):
        collection.add_index({"type": "persistent", "fields": ["_uri"]})
    persons.insert_many([_person(i) for i in range(PEOPLE)])
    vertices.insert_many([{**_person(i), "type": "Person"} for i in range(PEOPLE)])
    handle.create_collection(_KNOWS, edge=True).insert_many(
        [{"_from": f"{_PERSONS}/p{i}", "_to": f"{_PERSONS}/p{(i + 1) % PEOPLE}"} for i in range(PEOPLE)]
    )
    handle.create_collection(_EDGES, edge=True).insert_many(
        [
            {"_from": f"{_VERTICES}/p{i}", "_to": f"{_VERTICES}/p{(i + 1) % PEOPLE}", "type": "knows"}
            for i in range(PEOPLE)
        ]
    )
    yield handle
    for name in names:
        handle.delete_collection(name)


MAPPINGS = [pytest.param(PG_TTL, _PERSONS, id="pg"), pytest.param(LPG_TTL, _VERTICES, id="lpg")]
QUERIES = [
    pytest.param(
        "SELECT ?s ?x ?m WHERE { ?s a :Person OPTIONAL { ?s :knows ?x } OPTIONAL { ?x :email ?m } }",
        id="chained_optional",
    ),
    pytest.param(
        "SELECT ?s ?x ?m WHERE { ?s a :Person OPTIONAL { ?s :knows ?x } ?x :email ?m }", id="required_join"
    ),
]


@pytest.mark.parametrize("ttl,collection", MAPPINGS)
@pytest.mark.parametrize("body", QUERIES)
def test_a_join_that_can_be_unbound_keeps_its_index(db: Any, ttl: str, collection: str, body: str) -> None:
    query = f"PREFIX : <{EX}>\n" + body
    result = translate(query, resolver=SchemaResolver.from_turtle(ttl))

    nodes = db.aql.explain(result.aql, bind_vars=result.bind_vars)["nodes"]
    lookups = [
        node
        for node in nodes
        if node["type"] == "IndexNode"
        and node.get("collection") == collection
        and any(index.get("fields") in (["_uri"], ["_key"]) for index in node.get("indexes", []))
    ]
    assert lookups, f"no index lookup on {collection}:\n{result.aql}"

    cursor = db.aql.execute(result.aql, bind_vars=result.bind_vars, profile=True)
    rows = [drop_null_bindings(row) for row in cursor]
    # Only the ``?s a :Person`` loop reads the collection in full: every ?x
    # is bound, so the join's own scan branch never runs.
    assert cursor.statistics()["scanned_full"] == PEOPLE, result.aql

    expected = [normalize_oxi_row(r) for r in oxi_bindings(load_store_from_string(_facts()), query)]
    assert sorted(json.dumps(r, sort_keys=True) for r in rows) == sorted(
        json.dumps(r, sort_keys=True) for r in expected
    )
