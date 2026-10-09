"""A hyphenated collection name translates to AQL a real ArangoDB runs.

Companion to ``tests/translate/test_collection_names.py`` (prod.demo IAM's
``IAM-TERRAFORM-DOCS-DEMO_Relations`` crashed translation). Gated like the rest
of the integration suite (``RUN_INTEGRATION=1``).
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

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

_DOCS = "Live-Docs_Coll"

_TTL = f"""
@prefix :     <http://example.org/> .
@prefix owl:  <http://www.w3.org/2002/07/owl#> .
@prefix phys: <https://arango.solutions/phys#> .
:Doc a owl:Class ; phys:mappingStyle "COLLECTION" ; phys:collectionName "{_DOCS}" .
"""


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
    if handle.has_collection(_DOCS):
        handle.delete_collection(_DOCS)
    handle.create_collection(_DOCS).insert_many([{"title": "a"}, {"title": "b"}])
    yield handle
    handle.delete_collection(_DOCS)


def test_hyphenated_collection_runs(db: Any) -> None:
    res = translate(
        "PREFIX : <http://example.org/>\nSELECT ?t WHERE { ?d a :Doc ; :title ?t . }",
        resolver=SchemaResolver.from_turtle(_TTL),
    )
    db.aql.validate(res.aql)
    rows = list(db.aql.execute(res.aql, bind_vars=res.bind_vars))
    assert sorted(r["t"] for r in rows) == ["a", "b"]
