"""Every GROUP BY term is a COLLECT key, projected or not (found by LDBC IC5).

rdflib synthesises a Sample only for the GROUP BY variables a query
projects, and the COLLECT keys were taken from those Samples alone. So
``GROUP BY ?title ?forum`` projecting only ``?title`` merged forums that
share a title, and ``GROUP BY STR(?d)`` grouped nothing: silently wrong
counts. IC5 surfaced only because its ORDER BY read the dropped ``?forum``
after the COLLECT, which ArangoDB rejects as an unknown collection.

Live because the bug is about what ArangoDB returns; every query also runs
through pyoxigraph on the same data. Gated like the rest of the integration
suite (``RUN_INTEGRATION=1``).
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

EX = "http://example.org/"
_FORUMS = "GroupKeyForums"

_TTL = f"""
@prefix :     <{EX}> .
@prefix owl:  <http://www.w3.org/2002/07/owl#> .
@prefix phys: <https://arango.solutions/phys#> .
:Forum a owl:Class ; phys:mappingStyle "COLLECTION" ; phys:collectionName "{_FORUMS}" .
"""

# Two forums share the title "Rock"; each has its own member count.
_DOCS = [
    {"_key": "f1", "_uri": EX + "f1", "title": "Rock", "members": 3},
    {"_key": "f2", "_uri": EX + "f2", "title": "Rock", "members": 5},
    {"_key": "f3", "_uri": EX + "f3", "title": "Jazz", "members": 4},
]

_PREFIX = f"PREFIX : <{EX}>\n"


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
    if handle.has_collection(_FORUMS):
        handle.delete_collection(_FORUMS)
    handle.create_collection(_FORUMS).insert_many(_DOCS)
    yield handle
    handle.delete_collection(_FORUMS)


@pytest.fixture(scope="module")
def oxi_store() -> Any:
    lines = [
        f'<{d["_uri"]}> a <{EX}Forum> ; <{EX}title> "{d["title"]}" ; <{EX}members> {d["members"]} .'
        for d in _DOCS
    ]
    store = oxi.Store()
    store.load("\n".join(lines).encode("utf-8"), oxi.RdfFormat.TURTLE)
    return store


def _arango(db: Any, body: str) -> list[dict[str, Any]]:
    res = translate(_PREFIX + body, resolver=SchemaResolver.from_turtle(_TTL))
    db.aql.validate(res.aql)
    return [drop_null_bindings(r) for r in db.aql.execute(res.aql, bind_vars=res.bind_vars)]


def _oxigraph(store: Any, body: str) -> list[dict[str, Any]]:
    return [normalize_oxi_row(r) for r in oxi_bindings(store, _PREFIX + body)]


def _multiset(rows: list[dict[str, Any]]) -> list[str]:
    return sorted(json.dumps(r, sort_keys=True) for r in rows)


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        # One row per forum, not one per title.
        (
            "SELECT ?t (SUM(?m) AS ?s) WHERE { ?f a :Forum ; :title ?t ; :members ?m } GROUP BY ?t ?f",
            [{"t": "Rock", "s": 3}, {"t": "Rock", "s": 5}, {"t": "Jazz", "s": 4}],
        ),
        # A GROUP BY expression with no alias groups too.
        (
            "SELECT (SUM(?m) AS ?s) WHERE { ?f a :Forum ; :title ?t ; :members ?m } GROUP BY STR(?t)",
            [{"s": 8}, {"s": 4}],
        ),
    ],
)
def test_every_group_by_term_groups(
    db: Any, oxi_store: Any, body: str, expected: list[dict[str, Any]]
) -> None:
    rows = _arango(db, body)
    assert _multiset(rows) == _multiset(expected)
    assert _multiset(rows) == _multiset(_oxigraph(oxi_store, body))


def test_order_by_an_unprojected_group_key_runs(db: Any, oxi_store: Any) -> None:
    """IC5's shape: ORDER BY a GROUP BY variable the query does not project."""
    body = (
        "SELECT ?t (SUM(?m) AS ?s) WHERE { ?f a :Forum ; :title ?t ; :members ?m } "
        "GROUP BY ?t ?f ORDER BY DESC(?s) ?f"
    )
    rows = _arango(db, body)
    assert rows == [{"t": "Rock", "s": 5}, {"t": "Jazz", "s": 4}, {"t": "Rock", "s": 3}]
    assert rows == _oxigraph(oxi_store, body)


@pytest.mark.parametrize(
    "body",
    [
        # Issue #116: after grouping, ``?f`` is out of scope, so a trailing
        # VALUES on it joins every solution (one per VALUES row); it used to
        # compare the pre-COLLECT document after the COLLECT (ERR 1203).
        "SELECT (COUNT(*) AS ?k) WHERE { ?f a :Forum ; :title ?t } VALUES ?f { UNDEF }",
        "SELECT (COUNT(*) AS ?k) WHERE { ?f a :Forum ; :title ?t } VALUES ?f { :f1 :f2 }",
        'SELECT (SUM(?m) AS ?s) WHERE { ?f a :Forum ; :members ?m } VALUES ?t { "Rock" }',
        # A GROUP BY key is in scope: VALUES restricts the groups.
        'SELECT ?t (SUM(?m) AS ?s) WHERE { ?f a :Forum ; :title ?t ; :members ?m } GROUP BY ?t VALUES ?t { "Rock" }',
        "SELECT ?t (SUM(?m) AS ?s) WHERE { ?f a :Forum ; :title ?t ; :members ?m } GROUP BY ?t "
        'VALUES (?t ?f) { ("Jazz" :f1) ("Rock" UNDEF) }',
    ],
)
def test_a_values_block_after_grouping_joins_the_groups(db: Any, oxi_store: Any, body: str) -> None:
    expected = _oxigraph(oxi_store, body)
    assert expected, "the case must have a non-empty answer to prove anything"
    assert _multiset(_arango(db, body)) == _multiset(expected)
