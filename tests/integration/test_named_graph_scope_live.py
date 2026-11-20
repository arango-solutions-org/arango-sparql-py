"""The GRAPH scope on a real ArangoDB (#195, #106, #144).

The cases of ``tests/cross/test_named_graph_scope_cross.py`` (PG, LPG and RPT;
the default graph and named graphs; one IRI's triples split across graphs),
run on ArangoDB instead of the AQL interpreter and compared with the same
oracle: pyoxigraph, or rdflib for a sub-SELECT inside ``GRAPH ?g``. Live
because the sub-SELECT cases are beyond the interpreter, and because the
graph scope lands in correlated sub-queries ArangoDB plans itself. Gated like
the rest of the integration suite (``RUN_INTEGRATION=1``).
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from tests.cross.test_multi_document_iri_cross import Store, translate_case
from tests.cross.test_named_graph_scope_cross import case_params, expected_rows
from tests.helpers.oxi import assert_bindings_equal, drop_null_bindings
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
from tests.integration.test_multi_document_iri_live import _load

pytestmark = pytest.mark.integration

pytest.importorskip("pyoxigraph", reason="pyoxigraph is the ground truth for these tests")


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


@pytest.mark.parametrize("store, query, oracle", case_params())
def test_arangodb_agrees_with_the_oracle(db: Any, store: Store, query: str, oracle: str) -> None:
    _load(db, store)
    try:
        result = translate_case(store, query)
        rows = [drop_null_bindings(r) for r in db.aql.execute(result.aql, bind_vars=result.bind_vars)]
    finally:
        for name in store.docs:
            if db.has_collection(name):
                db.delete_collection(name)
    assert_bindings_equal(expected_rows(store, query, oracle), rows)
