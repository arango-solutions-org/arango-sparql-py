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
from tests.cross import test_mixed_store_graph_cross as graph_cases
from tests.cross.test_mixed_store_cross import PREFIX, cases, collections, facts, make_resolver
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
    stored = {**collections(), **graph_cases.collections()}
    for name, docs in stored.items():
        if handle.has_collection(name):
            handle.delete_collection(name)
        collection = handle.create_collection(name)
        if docs:
            collection.insert_many(docs)
        # The indexes the readers rely on: a table's subject column, and the
        # documents' ``_uri`` (PRD §6.3; #146).
        fields = ["subject_uri", "predicate"] if name.startswith("T") else ["_uri"]
        collection.add_index({"type": "persistent", "fields": fields})
    yield handle
    for name in stored:
        handle.delete_collection(name)


def _store(strict: bool) -> Any:
    store = oxi.Store()
    store.load(facts(strict).encode("utf-8"), oxi.RdfFormat.TURTLE)
    return store


def _multiset(rows: list[dict[str, Any]]) -> list[str]:
    return sorted(json.dumps(r, sort_keys=True) for r in rows)


@pytest.mark.parametrize("strict", [False, True], ids=["library", "strict"])
@pytest.mark.parametrize(("where", "ontology"), cases())
def test_an_unplaced_subject_in_a_mixed_database_matches_pyoxigraph(
    db: Any, where: str, ontology: str, strict: bool
) -> None:
    query = PREFIX + where
    result = translate(query, resolver=make_resolver(ontology, strict))
    db.aql.validate(result.aql)

    arango = [drop_null_bindings(r) for r in db.aql.execute(result.aql, bind_vars=result.bind_vars)]

    assert _multiset(arango) == _multiset([normalize_oxi_row(r) for r in oxi_bindings(_store(strict), query)])


@pytest.mark.parametrize(
    "lax_default_graph", [False, True], ids=["default_graph_alone", "union_default_graph"]
)
@pytest.mark.parametrize("strict", [False, True], ids=["library", "strict"])
@pytest.mark.parametrize("where", graph_cases.ANSWERED)
def test_a_routed_read_in_a_named_or_the_default_graph_matches_pyoxigraph(
    db: Any, where: str, strict: bool, lax_default_graph: bool
) -> None:
    query = graph_cases.PREFIX + where
    result = translate(query, resolver=graph_cases.make_resolver(strict, lax_default_graph))

    arango = [drop_null_bindings(r) for r in db.aql.execute(result.aql, bind_vars=result.bind_vars)]

    store = oxi.Store()
    store.load(graph_cases.quads().encode("utf-8"), oxi.RdfFormat.N_QUADS)
    expected = [
        normalize_oxi_row(r) for r in oxi_bindings(store, query, union_default_graph=lax_default_graph)
    ]
    assert _multiset(arango) == _multiset(expected)


def test_a_missing_default_collection_fails_loudly_on_the_library_resolver(db: Any) -> None:
    # The non-strict resolver reads its default collection too; a database
    # without one is an error, not an answer without it (review of #199).
    from arango.exceptions import AQLQueryExecuteError

    resolver = make_resolver("undeclared", strict=False)
    resolver.default_collection = "NoSuchDefault191"
    result = translate(PREFIX + "SELECT ?m WHERE { :acme :email ?m }", resolver=resolver)

    with pytest.raises(AQLQueryExecuteError, match="1203"):
        list(db.aql.execute(result.aql, bind_vars=result.bind_vars))


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
    result = translate(PREFIX + where, resolver=make_resolver(ontology, strict=True))

    # Only the type pattern scans: the people, on the table's rdf:type rows.
    assert _full_scans(db, result.aql, result.bind_vars) == ["T191"]
