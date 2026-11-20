"""Live: an unbound variable joins any value (SPARQL 1.1 §18.3; #193, #100).

The shapes and mappings of ``tests/cross/test_unbound_join_cross.py``, run
on ArangoDB instead of the interpreter: a variable an earlier OPTIONAL can
leave unbound, joined as a document subject (PG, LPG, a supertype read from
several collections), an edge's target, an attribute value (single- and
multi-valued), a triples-table object, and twice in one EXISTS. Before the
fix the join compared the stored identity with null and dropped the row.

Live because the bug is about what ArangoDB runs. Every query
runs through pyoxigraph on the same facts, and the answers must agree.
Gated like the rest of the integration suite (``RUN_INTEGRATION=1``).
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from typing import Any

import pytest

from arango_sparql.api import translate
from arango_sparql.translate.resolver import SchemaResolver
from tests.cross import test_unbound_join_cross as shapes
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

#: Prefix for this module's collections, so they cannot meet another test's.
_PREFIX = "UnboundJoin"

Mapping = Callable[[], tuple[str, dict[str, list[dict[str, Any]]]]]


def _renamed(mapping: Mapping) -> tuple[str, dict[str, list[dict[str, Any]]]]:
    """The mapping with every collection renamed ``UnboundJoin<name>``."""
    ttl, docs = mapping()
    names = {name: _PREFIX + name.lstrip("_") for name in docs}
    for old, new in names.items():
        ttl = ttl.replace(f'"{old}"', f'"{new}"')

    def handle(ref: str) -> str:
        collection, key = ref.split("/", 1)
        return f"{names[collection]}/{key}"

    out: dict[str, list[dict[str, Any]]] = {}
    for name, rows in docs.items():
        out[names[name]] = []
        for row in rows:
            row = dict(row)
            if "_id" in row:
                row["_key"] = row.pop("_id").split("/", 1)[1]
            for end in ("_from", "_to"):
                if end in row:
                    row[end] = handle(row[end])
            out[names[name]].append(row)
    return ttl, out


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


@pytest.fixture
def load(db: Any) -> Iterator[Callable[[Mapping], str]]:
    """Load a renamed mapping's collections; drop them after the test."""
    created: list[str] = []

    def _load(mapping: Mapping) -> str:
        ttl, docs = _renamed(mapping)
        for name, rows in docs.items():
            if db.has_collection(name):
                db.delete_collection(name)
            db.create_collection(name, edge=any("_from" in r for r in rows)).insert_many(rows)
            created.append(name)
        return ttl

    yield _load
    for name in created:
        if db.has_collection(name):
            db.delete_collection(name)


def _multiset(rows: list[dict[str, Any]]) -> list[str]:
    return sorted(json.dumps(r, sort_keys=True) for r in rows)


def _check(db: Any, ttl: str, facts: str, body: str) -> None:
    query = f"PREFIX : <{shapes.EX}>\n" + body
    expected = [normalize_oxi_row(r) for r in oxi_bindings(load_store_from_string(facts), query)]
    assert expected, "the case must have a non-empty answer to prove anything"
    result = translate(query, resolver=SchemaResolver.from_turtle(ttl))
    actual = [drop_null_bindings(r) for r in db.aql.execute(result.aql, bind_vars=result.bind_vars)]
    assert _multiset(actual) == _multiset(expected), result.aql


@pytest.mark.parametrize(
    "mapping", [pytest.param(shapes._pg_edge, id="pg_edge"), pytest.param(shapes._lpg, id="lpg")]
)
@pytest.mark.parametrize("body", shapes.SUBJECT_CASES + shapes.OBJECT_CASES)
def test_unbound_variable_joins_any_document_live(db: Any, load: Any, mapping: Mapping, body: str) -> None:
    _check(db, load(mapping), shapes._facts(), body)


@pytest.mark.parametrize(
    "mapping",
    [
        pytest.param(shapes._pg_edge, id="pg_edge"),
        pytest.param(shapes._lpg, id="lpg"),
        pytest.param(shapes._rpt, id="rpt"),
    ],
)
@pytest.mark.parametrize("body", shapes.SCOPE_CASES)
def test_unbound_variable_scope_cases_live(db: Any, load: Any, mapping: Mapping, body: str) -> None:
    _check(db, load(mapping), shapes._facts(), body)


@pytest.mark.parametrize("body", shapes.OBJECT_CASES)
def test_unbound_variable_joins_any_triples_object_live(db: Any, load: Any, body: str) -> None:
    _check(db, load(shapes._rpt), shapes._facts(), body)


@pytest.mark.parametrize("body", shapes.ATTRIBUTE_CASES)
def test_unbound_variable_joins_any_attribute_value_live(db: Any, load: Any, body: str) -> None:
    _check(db, load(shapes._pg_attr), shapes._facts(knows=False, tags=True), body)


@pytest.mark.parametrize("body", shapes.SUPERTYPE_CASES)
def test_unbound_variable_joins_any_supertype_member_live(db: Any, load: Any, body: str) -> None:
    _check(db, load(shapes._pg_union), shapes._facts(types=False, agent=True), body)
