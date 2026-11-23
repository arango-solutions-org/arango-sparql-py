"""Every tenant-scoped read in a nested scope is held to the caller's tenant (#211).

``tests/translate/test_tenant_commitment.py`` checks that a query under two
tenant roots is refused and that one under a single root translates. This
runs the single-root shapes on ArangoDB with two tenants' data, including
edges that cross from one tenant to the other, and compares each answer for
tenant ``t1`` with pyoxigraph over ``t1``'s data alone: a probe, arm or
sub-select that dropped its tenant filter returns ``t2`` rows and fails.
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
_PERSON, _DOC, _PUB = "TenantCommitPerson", "TenantCommitDoc", "TenantCommitPub"
_MENTIONS, _CITES = "TenantCommitMentions", "TenantCommitCites"

# Person and Doc are both under the Org root (fields org / ws); Pub is public.
_ONTOLOGY = f"""
@prefix :     <{EX}> .
@prefix owl:  <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix phys: <https://arango.solutions/phys#> .
:Person a owl:Class ; phys:collectionName "{_PERSON}" ; phys:tenantField "org" ; phys:tenantEntity "Org" .
:Doc a owl:Class ; phys:collectionName "{_DOC}" ; phys:tenantField "ws" ; phys:tenantEntity "Org" .
:Pub a owl:Class ; phys:collectionName "{_PUB}" .
:name a owl:DatatypeProperty ; rdfs:domain :Person ; phys:attributeName "name" .
:mentions a owl:ObjectProperty ; phys:edgeCollectionName "{_MENTIONS}" ; rdfs:domain :Pub ; rdfs:range :Person .
:cites a owl:ObjectProperty ; phys:edgeCollectionName "{_CITES}" ; rdfs:domain :Doc ; rdfs:range :Doc .
"""

# key -> tenant; p3 and d3 belong to t2.
_PEOPLE = {"p1": "t1", "p2": "t1", "p3": "t2"}
_DOCS = {"d1": "t1", "d2": "t1", "d3": "t2"}
_PUBS = ["q1", "q2"]
_MENTION_EDGES = [("q1", "p1"), ("q1", "p3"), ("q2", "p3")]
_CITE_EDGES = [("d1", "d2"), ("d1", "d3"), ("d3", "d1")]

# The single-root shapes of the translate test, plus the bind-collision ones.
_SHAPES = {
    "same_pattern": "?p a :Person . ?d a :Doc",
    "union_after": "?p a :Person { ?d a :Doc } UNION { ?d a :Doc }",
    "optional_after": "?p a :Person OPTIONAL { ?p :name ?n }",
    "minus": "?d a :Doc MINUS { ?d :cites ?e }",
    "exists": "?p a :Person FILTER EXISTS { ?d a :Doc }",
    "not_exists": "?d a :Doc FILTER NOT EXISTS { ?d :cites ?e }",
    "subselect_after": "?p a :Person { SELECT ?d WHERE { ?d a :Doc } }",
    "subselect_first": "{ SELECT ?d WHERE { ?d a :Doc } } ?p a :Person",
    "union_first": "{ ?d a :Doc } UNION { ?p a :Person } ?q a :Pub",
    "optional_first": "OPTIONAL { ?d a :Doc } ?p a :Person",
    "exists_in_union_arm": "{ ?p a :Person FILTER EXISTS { ?d :cites ?e } } UNION { ?p a :Person }",
    "subselect_in_minus": "?d a :Doc MINUS { SELECT ?d WHERE { ?d :cites ?e } }",
    "edge_optional_then_exists": "?q a :Pub OPTIONAL { ?q :mentions ?p } FILTER EXISTS { ?d :cites ?e }",
    "edge_optional_then_subselect": "?q a :Pub OPTIONAL { ?q :mentions ?p } { SELECT * { ?d :cites ?e } }",
    "walk_in_exists": "?q a :Pub FILTER EXISTS { ?q :mentions ?p }",
}


def _t1_data() -> str:
    lines = [f"@prefix : <{EX}> ."]
    lines += [f':{k} a :Person ; :name "{k.upper()}" .' for k, t in _PEOPLE.items() if t == "t1"]
    lines += [f":{k} a :Doc ." for k, t in _DOCS.items() if t == "t1"]
    lines += [f":{k} a :Pub ." for k in _PUBS]
    lines += [f":{a} :mentions :{b} ." for a, b in _MENTION_EDGES if _PEOPLE[b] == "t1"]
    lines += [f":{a} :cites :{b} ." for a, b in _CITE_EDGES if _DOCS[a] == _DOCS[b] == "t1"]
    return "\n".join(lines)


def _collections() -> dict[str, tuple[list[dict[str, Any]], bool]]:
    def edges(pairs: list[tuple[str, str]], src: str, dst: str) -> list[dict[str, Any]]:
        return [{"_from": f"{src}/{a}", "_to": f"{dst}/{b}"} for a, b in pairs]

    return {
        _PERSON: (
            [{"_key": k, "_uri": EX + k, "org": t, "name": k.upper()} for k, t in _PEOPLE.items()],
            False,
        ),
        _DOC: ([{"_key": k, "_uri": EX + k, "ws": t} for k, t in _DOCS.items()], False),
        _PUB: ([{"_key": k, "_uri": EX + k} for k in _PUBS], False),
        _MENTIONS: (edges(_MENTION_EDGES, _PUB, _PERSON), True),
        _CITES: (edges(_CITE_EDGES, _DOC, _DOC), True),
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
    for name, (docs, is_edge) in _collections().items():
        if handle.has_collection(name):
            handle.delete_collection(name)
        handle.create_collection(name, edge=is_edge).insert_many(docs)
    yield handle
    for name in _collections():
        handle.delete_collection(name)


@pytest.fixture(scope="module")
def oxi_store() -> Any:
    store = oxi.Store()
    store.load(_t1_data().encode("utf-8"), oxi.RdfFormat.TURTLE)
    return store


def _multiset(rows: list[dict[str, Any]]) -> list[str]:
    return sorted(json.dumps(r, sort_keys=True) for r in rows)


@pytest.mark.parametrize("body", list(_SHAPES.values()), ids=list(_SHAPES))
def test_every_nested_read_is_held_to_the_callers_tenant(db: Any, oxi_store: Any, body: str) -> None:
    query = f"PREFIX : <{EX}>\nSELECT * WHERE {{ {body} }}"
    res = translate(query, resolver=SchemaResolver.from_turtle(_ONTOLOGY), tenant_id="t1")
    actual = [drop_null_bindings(r) for r in db.aql.execute(res.aql, bind_vars=res.bind_vars)]
    expected = [normalize_oxi_row(r) for r in oxi_bindings(oxi_store, query)]
    assert expected, "the shape should have t1 answers, or it tests nothing"
    assert _multiset(actual) == _multiset(expected)
