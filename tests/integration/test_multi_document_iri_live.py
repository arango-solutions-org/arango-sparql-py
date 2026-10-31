"""One IRI stored as several documents, on a real ArangoDB (PRD §6.1, #105).

The cases of ``tests/cross/test_multi_document_iri_cross.py`` (an entity of
two PG classes, the W3C loader's copies, one document per named graph, an
edge into a document outside its declared range), run on ArangoDB instead of
the AQL interpreter and compared with pyoxigraph on the same facts. Live
because a backward walk from every copy of an IRI (``FOR … IN UNION(…)``
feeding a traversal) is about what ArangoDB executes. Strict xfails are wrong
on ``main`` and name their issue. Gated like the rest of the integration
suite (``RUN_INTEGRATION=1``).
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from arango_sparql.api import translate
from arango_sparql.translate.resolver import SchemaResolver
from tests.cross.test_multi_document_iri_cross import PREFIXES, Store, case_params
from tests.helpers.oxi import assert_bindings_equal, drop_null_bindings, normalize_oxi_row, oxi_bindings
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
    yield ArangoClient(hosts=DEFAULT_ARANGO_URL).db(
        DEFAULT_ARANGO_DB, username=DEFAULT_ARANGO_USER, password=DEFAULT_ARANGO_PASSWORD
    )


def _load(db: Any, store: Store) -> None:
    for name, docs in store.docs.items():
        if db.has_collection(name):
            db.delete_collection(name)
        handle = db.create_collection(name, edge=any("_from" in d for d in docs))
        for doc in docs:
            doc = dict(doc)
            doc["_key"] = doc.pop("_id").split("/", 1)[1]
            handle.insert(doc)


@pytest.mark.parametrize("store, query", case_params())
def test_arangodb_agrees_with_pyoxigraph(db: Any, store: Store, query: str) -> None:
    _load(db, store)
    try:
        sparql = "PREFIX : <http://ex.org/>\n" + query
        result = translate(
            sparql, resolver=SchemaResolver.from_turtle(store.ontology, **store.resolver_kwargs)
        )
        rows = [drop_null_bindings(r) for r in db.aql.execute(result.aql, bind_vars=result.bind_vars)]
    finally:
        for name in store.docs:
            if db.has_collection(name):
                db.delete_collection(name)
    oracle = oxi.Store()
    oracle.load(
        (PREFIXES + store.data).encode(), format=oxi.RdfFormat.TRIG if store.trig else oxi.RdfFormat.TURTLE
    )
    assert_bindings_equal([normalize_oxi_row(r) for r in oxi_bindings(oracle, sparql)], rows)
