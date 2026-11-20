"""A subject the query never placed, in a database that keeps some classes as
RDF triples and others as documents, on ArangoDB (#191).

The same facts, mappings and queries as
``tests/cross/test_mixed_store_cross.py``, run by ArangoDB instead of the
in-memory interpreter, against pyoxigraph on the same facts. Live because
#191 was about what ArangoDB runs: reads of the default ``Document``
collection, which a mixed database need not have (ERR 1203), and an
OPTIONAL that scanned the wrong store and answered nothing. Each query is
also validated by ArangoDB's parser, and the AQL is explained to check that
the read from a routed store is an index lookup on the subject's IRI, not a
scan. Gated like the rest of the integration suite (``RUN_INTEGRATION=1``).
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import pytest

from arango_sparql.api import translate
from arango_sparql.translate.resolver import SchemaResolver
from tests.cross.test_mixed_store_cross import DOC_FACTS, DOCS, ONTOLOGIES, PREFIX, TRIPLES, _rows, cases
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
    for name, docs in (("T191", _rows()), ("C191", DOCS)):
        if handle.has_collection(name):
            handle.delete_collection(name)
        collection = handle.create_collection(name)
        collection.insert_many(docs)
    # The indexes the readers rely on: the table's subject column, and the
    # documents' ``_uri`` (PRD §6.3; #146).
    handle.collection("T191").add_index({"type": "persistent", "fields": ["subject_uri", "predicate"]})
    handle.collection("C191").add_index({"type": "persistent", "fields": ["_uri"]})
    yield handle
    handle.delete_collection("T191")
    handle.delete_collection("C191")


@pytest.fixture(scope="module")
def oxi_store() -> Any:
    store = oxi.Store()
    store.load((TRIPLES + DOC_FACTS).encode("utf-8"), oxi.RdfFormat.TURTLE)
    return store


def _multiset(rows: list[dict[str, Any]]) -> list[str]:
    return sorted(json.dumps(r, sort_keys=True) for r in rows)


def _resolver(ontology: str, strict: bool) -> SchemaResolver:
    resolver = SchemaResolver.from_turtle(ONTOLOGIES[ontology])
    resolver.strict_subject_resolution = strict
    return resolver


@pytest.mark.parametrize("strict", [False, True], ids=["library", "strict"])
@pytest.mark.parametrize(("where", "ontology"), cases())
def test_an_unplaced_subject_in_a_mixed_database_matches_pyoxigraph(
    db: Any, oxi_store: Any, where: str, ontology: str, strict: bool
) -> None:
    query = PREFIX + where
    result = translate(query, resolver=_resolver(ontology, strict))
    db.aql.validate(result.aql)

    arango = [drop_null_bindings(r) for r in db.aql.execute(result.aql, bind_vars=result.bind_vars)]

    assert _multiset(arango) == _multiset([normalize_oxi_row(r) for r in oxi_bindings(oxi_store, query)])


def _full_scans(db: Any, aql: str, bind_vars: dict[str, Any]) -> list[str]:
    """The collections ArangoDB's plan reads by a full scan."""
    plan = db.aql.explain(aql, bind_vars=bind_vars)
    return sorted(node["collection"] for node in plan["nodes"] if node["type"] == "EnumerateCollectionNode")


@pytest.mark.parametrize(
    ("ontology", "where"),
    [
        # Domain says documents: the company is looked up by its IRI.
        ("undeclared", "SELECT ?s ?c ?n WHERE { ?s a :Person ; :worksFor ?c . ?c :cname ?n }"),
        # No domain: both stores are read, each by the subject's IRI.
        ("undeclared", "SELECT ?s ?c ?m WHERE { ?s a :Person ; :worksFor ?c . ?c :email ?m }"),
        # Domain says the table: one indexed read of it.
        ("declared", "SELECT ?s ?x ?m WHERE { ?s a :Person ; :knows ?x . ?x :email ?m }"),
    ],
)
def test_a_routed_read_is_an_index_lookup_on_the_subject(db: Any, ontology: str, where: str) -> None:
    result = translate(PREFIX + where, resolver=_resolver(ontology, strict=True))

    # Only the type pattern scans: the people, on the table's rdf:type rows.
    assert _full_scans(db, result.aql, result.bind_vars) == ["T191"]
