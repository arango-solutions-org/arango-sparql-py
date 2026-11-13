"""CDF #220 — an attribute declared multi-valued, on a real ArangoDB.

The mapping of ``tests/cross/test_multi_valued_attribute_cross.py`` (CSI 1.2:
``questionsServed`` holds a list, ``role`` one value; ``fan_out_list_values``
off), run on ArangoDB and compared with pyoxigraph on the same facts. Live for
what the AQL interpreter cannot run: a variable predicate (``ATTRIBUTES()``),
which decides per attribute at run time whether to fan out. Gated like the
rest of the integration suite (``RUN_INTEGRATION=1``).
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from arango_sparql.api import translate
from arango_sparql.translate.csi import mapping_bundle_from_csi
from arango_sparql.translate.resolver import SchemaResolver
from tests.cross.test_multi_valued_attribute_cross import CSI, DATA_TTL, DOCS, PREFIX, PROBES
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

VARIABLE_PREDICATES = [
    pytest.param('SELECT ?d ?p WHERE { ?d a :Document ; ?p "Q2" }', id="constant_object"),
    pytest.param('SELECT ?d ?p WHERE { ?d a :Document ; ?p "pdf" }', id="scalar_constant_object"),
    pytest.param(
        "SELECT ?d ?o WHERE { ?d a :Document ; ?p ?o FILTER(?p = :questionsServed) }", id="each_element"
    ),
    pytest.param(
        "SELECT (COUNT(?o) AS ?n) WHERE { ?d a :Document ; ?p ?o FILTER(?p = :questionsServed) }",
        id="count_values",
    ),
    pytest.param('SELECT ?d ?p WHERE { VALUES ?o { "Q1" } ?d a :Document ; ?p ?o }', id="bound_object"),
]


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
    for name, docs in DOCS.items():
        if handle.has_collection(name):
            handle.delete_collection(name)
        coll = handle.create_collection(name)
        coll.insert_many(
            [{**{k: v for k, v in d.items() if k != "_id"}, "_key": d["_id"].split("/")[1]} for d in docs]
        )
    yield handle
    for name in DOCS:
        handle.delete_collection(name)


@pytest.fixture(scope="module")
def oxi_store() -> Any:
    store = oxi.Store()
    store.load(DATA_TTL.encode("utf-8"), oxi.RdfFormat.TURTLE)
    return store


@pytest.mark.parametrize("sparql", [*PROBES, *VARIABLE_PREDICATES])
def test_arangodb_agrees_with_pyoxigraph(db: Any, oxi_store: Any, sparql: str) -> None:
    resolver = SchemaResolver.from_mapping_bundle(mapping_bundle_from_csi(CSI))
    result = translate(PREFIX + sparql, resolver=resolver)
    rows = [drop_null_bindings(r) for r in db.aql.execute(result.aql, bind_vars=result.bind_vars)]
    expected = [normalize_oxi_row(r) for r in oxi_bindings(oxi_store, PREFIX + sparql)]
    assert_bindings_equal(expected, rows)
