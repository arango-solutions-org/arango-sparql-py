"""``DESCRIBE <iri>`` on a real ArangoDB (#150).

A constant was read from the default collection only: in a mapping without
one (the service's) ArangoDB refused the query ("collection not found:
Document"), and a copy a class places elsewhere was not described. The
documents of every class's collection and the rows of every triples table
are read now. Gated like the rest of the integration suite
(``RUN_INTEGRATION=1``).
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from rdflib import Graph

from arango_sparql.api import translate
from arango_sparql.translate.resolver import SchemaResolver
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

_TTL = """
@prefix : <http://ex.org/> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix phys: <https://arango.solutions/phys#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
:Person a owl:Class ; phys:collectionName "d150_Person" ; phys:tenantField "tenant" ; rdfs:subClassOf :Agent .
:Org a owl:Class ; phys:collectionName "d150_Org" ; rdfs:subClassOf :Agent .
:Agent a owl:Class ; phys:mappingStyle "UNION" .
:Fact a owl:Class ; phys:mappingStyle "RPT" ; phys:collectionName "d150_triples" .
:name a owl:DatatypeProperty ; rdfs:domain :Person .
"""
_DOCS: dict[str, list[dict[str, Any]]] = {
    "d150_Person": [
        {"_key": "s", "_uri": "http://ex.org/s", "tenant": "A", "name": "S", "age": 3},
        {"_key": "t", "_uri": "http://ex.org/t", "tenant": "A", "name": "T"},
        {"_key": "u", "_uri": "http://ex.org/u", "tenant": "B", "name": "U"},
        {"_key": "g", "_uri": "http://ex.org/g", "_graph": "http://ex.org/g1", "tenant": "A", "name": "G"},
    ],
    "d150_Org": [{"_key": "s", "_uri": "http://ex.org/s", "name": "S", "size": 9}],
    "d150_triples": [{"subject_uri": "http://ex.org/r", "predicate": "http://ex.org/p", "object_value": "R"}],
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
    for name, docs in _DOCS.items():
        if handle.has_collection(name):
            handle.delete_collection(name)
        handle.create_collection(name).insert_many(docs)
    try:
        yield handle
    finally:
        for name in _DOCS:
            if handle.has_collection(name):
                handle.delete_collection(name)


def _describe(db: Any, query: str, tenant_id: str = "A", **resolver_kwargs: Any) -> set[tuple[str, str, str]]:
    # The service's resolver: no default collection to fall back to.
    resolver = SchemaResolver(
        ontology=Graph().parse(data=_TTL, format="turtle"), strict_subject_resolution=True, **resolver_kwargs
    )
    result = translate("PREFIX : <http://ex.org/>\n" + query, resolver=resolver, tenant_id=tenant_id)
    rows = db.aql.execute(result.aql, bind_vars=result.bind_vars, max_runtime=15)
    return {(t["subject"], t["predicate"], str(t["object"])) for row in rows for t in row}


S = "http://ex.org/s"


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        # Both copies of :s; the shared name once.
        ("DESCRIBE :s", {(S, "name", "S"), (S, "age", "3"), (S, "size", "9"), (S, "tenant", "A")}),
        (
            "DESCRIBE :s :r",
            {
                (S, "name", "S"),
                (S, "age", "3"),
                (S, "size", "9"),
                (S, "tenant", "A"),
                ("http://ex.org/r", "http://ex.org/p", "R"),
            },
        ),
        ("DESCRIBE :nobody", set()),
        # Another tenant's document is not described.
        ("DESCRIBE :u", set()),
        # A constant beside a WHERE: described once, whatever the WHERE's rows.
        (
            'DESCRIBE :t WHERE { ?x a :Person ; :name "S" }',
            {("http://ex.org/t", "name", "T"), ("http://ex.org/t", "tenant", "A")},
        ),
        # Three described resources: the third is not dropped.
        (
            'DESCRIBE ?x ?y ?z WHERE { ?x a :Person ; :name "S" . ?y a :Person ; :name "T" . ?z a :Org }',
            {
                (S, "name", "S"),
                (S, "age", "3"),
                (S, "tenant", "A"),
                ("http://ex.org/t", "name", "T"),
                ("http://ex.org/t", "tenant", "A"),
                (S, "size", "9"),
            },
        ),
    ],
)
def test_a_constant_is_described_wherever_the_mapping_stores_it(
    db: Any, query: str, expected: set[tuple[str, str, str]]
) -> None:
    assert _describe(db, query) == expected


def test_the_other_tenant_sees_only_its_own_documents(db: Any) -> None:
    assert _describe(db, "DESCRIBE :s", tenant_id="B") == {(S, "name", "S"), (S, "size", "9")}


def test_a_strict_default_graph_does_not_describe_a_named_graph_copy(db: Any) -> None:
    # Read as a type pattern reads, under the active graph scope (review of #163).
    assert _describe(db, "DESCRIBE :g", default_graph_includes_named=False) == set()
    described = _describe(db, "DESCRIBE :g")
    assert ("http://ex.org/g", "name", "G") in described
