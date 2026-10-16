"""Issue #39 — ``SELECT DISTINCT … LIMIT n`` de-duplicates before it slices.

SPARQL evaluates ``Slice(Distinct(Project(OrderBy(…))))``: duplicates go
first, then the slice counts rows. The translator used to emit
``LIMIT n`` above ``RETURN DISTINCT``, and AQL runs a LIMIT where it is
written, so a duplicate inside the first n rows cost a row of the answer.

Live because the bug is about what ArangoDB returns, and the in-memory
interpreter the cross tests use evaluated that AQL the SPARQL way (it
applied DISTINCT before LIMIT), so it could not see it. Every query here is
also run through pyoxigraph on the same data — the W3C ground truth — and
the two answers must agree. Gated like the rest of the integration suite
(``RUN_INTEGRATION=1``).
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from arango_sparql.api import translate
from arango_sparql.translate.resolver import SchemaResolver
from tests.helpers.oxi import normalize_oxi_row, oxi_bindings
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

_PEOPLE = "Issue39People"

_TTL = f"""
@prefix :     <http://example.org/> .
@prefix owl:  <http://www.w3.org/2002/07/owl#> .
@prefix phys: <https://arango.solutions/phys#> .
:Person a owl:Class ; phys:mappingStyle "COLLECTION" ; phys:collectionName "{_PEOPLE}" .
"""

# Ordered by name the departments run eng, eng, eng, ops, eng, sales — so the
# first rows of any slice are mostly duplicates, which is what the bug needs.
_DOCS = [
    {"_key": "ada", "name": "Ada", "dept": "eng"},
    {"_key": "ben", "name": "Ben", "dept": "eng"},
    {"_key": "cy", "name": "Cy", "dept": "eng"},
    {"_key": "di", "name": "Di", "dept": "ops"},
    {"_key": "ed", "name": "Ed", "dept": "eng"},
    {"_key": "flo", "name": "Flo", "dept": "sales"},
]

_PREFIX = "PREFIX : <http://example.org/>\n"


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
    if handle.has_collection(_PEOPLE):
        handle.delete_collection(_PEOPLE)
    handle.create_collection(_PEOPLE).insert_many(_DOCS)
    yield handle
    handle.delete_collection(_PEOPLE)


@pytest.fixture(scope="module")
def oxi_store() -> Any:
    """The same people as RDF, keyed by the documents' ``_id``."""
    store = oxi.Store()
    triples = "\n".join(
        f"<{_PEOPLE}/{d['_key']}> a <http://example.org/Person> ; "
        f'<http://example.org/name> "{d["name"]}" ; <http://example.org/dept> "{d["dept"]}" .'
        for d in _DOCS
    )
    store.load(triples.encode("utf-8"), oxi.RdfFormat.TURTLE, base_iri="http://example.org/")
    return store


def _arango(db: Any, body: str) -> list[dict[str, Any]]:
    res = translate(_PREFIX + body, resolver=SchemaResolver.from_turtle(_TTL))
    db.aql.validate(res.aql)
    return list(db.aql.execute(res.aql, bind_vars=res.bind_vars))


def _oxigraph(store: Any, body: str) -> list[dict[str, Any]]:
    return [normalize_oxi_row(r) for r in oxi_bindings(store, _PREFIX + body)]


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        # The issue's shape, ordered so the slice is deterministic: slicing
        # first kept [eng, eng] and answered [eng].
        (
            "SELECT DISTINCT ?d WHERE { ?p a :Person ; :name ?n ; :dept ?d } ORDER BY ?n LIMIT 2",
            ["eng", "ops"],
        ),
        # OFFSET counts de-duplicated rows too: slicing first gave [eng].
        (
            "SELECT DISTINCT ?d WHERE { ?p a :Person ; :name ?n ; :dept ?d } ORDER BY ?n LIMIT 2 OFFSET 1",
            ["ops", "sales"],
        ),
        # ORDER BY still decides which rows the slice keeps after the
        # subquery: Flo (sales) sorts first, then eng, then ops.
        (
            "SELECT DISTINCT ?d WHERE { ?p a :Person ; :name ?n ; :dept ?d } ORDER BY DESC(?n) LIMIT 2",
            ["sales", "eng"],
        ),
        # Without DISTINCT nothing changes: two rows, duplicates and all.
        ("SELECT ?d WHERE { ?p a :Person ; :name ?n ; :dept ?d } ORDER BY ?n LIMIT 2", ["eng", "eng"]),
    ],
)
def test_ordered_distinct_slice_matches_oxigraph(
    db: Any, oxi_store: Any, body: str, expected: list[str]
) -> None:
    rows = _arango(db, body)
    assert [r["d"] for r in rows] == expected
    assert rows == _oxigraph(oxi_store, body)


@pytest.mark.parametrize(
    "body",
    [
        # No ORDER BY: which rows come first is up to the engine, but every
        # department is distinct, so LIMIT 3 must return all three.
        "SELECT DISTINCT ?d WHERE { ?p a :Person ; :dept ?d } LIMIT 3",
        # A sub-SELECT runs through its own builder; the outer query adds no
        # order, so compare as sets.
        "SELECT ?d WHERE { { SELECT DISTINCT ?d WHERE { ?p a :Person ; :name ?n ; :dept ?d } ORDER BY ?n LIMIT 2 } }",
    ],
)
def test_unordered_distinct_slice_matches_oxigraph(db: Any, oxi_store: Any, body: str) -> None:
    rows = _arango(db, body)
    expected = _oxigraph(oxi_store, body)
    assert len(rows) == len(expected)
    assert sorted(r["d"] for r in rows) == sorted(r["d"] for r in expected)
