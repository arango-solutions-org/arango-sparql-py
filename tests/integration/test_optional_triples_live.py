"""An OPTIONAL on a subject read from a triples table (RPT) reads the table.

A type pattern on a triples table opens the subject as a *row*. An OPTIONAL
on that subject was lowered as a document attribute read (``doc.knows``), a
field no triples row has, so it bound nothing for every subject: silent
wrong answers (found in the review of PR #189). It now runs as a correlated
subquery over the triples table, like any OPTIONAL the inline emitter
cannot lower.

Live because the bug is about what ArangoDB returns. Every query also runs
through pyoxigraph on the same triples, the W3C ground truth, and the
answers must agree. Gated like the rest of the integration suite
(``RUN_INTEGRATION=1``).
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import pytest
import rdflib

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
_TABLE = "OptionalRptTriples"
_TTL = f"""
@prefix :     <{EX}> .
@prefix owl:  <http://www.w3.org/2002/07/owl#> .
@prefix phys: <https://arango.solutions/phys#> .
:Person a owl:Class ; phys:mappingStyle "RPT" ; phys:triplesCollection "{_TABLE}" .
"""
# a knows two people, d one, c nobody; only b has an email.
_DATA = f"""
@prefix : <{EX}> .
:a a :Person ; :knows :b , :c .
:d a :Person ; :knows :e .
:c a :Person .
:b :email "b@x.org" .
"""
_PREFIX = f"PREFIX : <{EX}>\n"


def _rows() -> list[dict[str, Any]]:
    """One triples-table row per RDF triple: IRIs in ``object_uri``, literals in ``object_value``."""
    graph = rdflib.Graph().parse(data=_DATA, format="turtle")
    rows = []
    for s, p, o in graph:
        row: dict[str, Any] = {"subject_uri": str(s), "predicate": str(p)}
        if isinstance(o, rdflib.URIRef):
            row["object_uri"] = str(o)
        else:
            row["object_value"] = o.toPython()
        rows.append(row)
    return rows


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
    if handle.has_collection(_TABLE):
        handle.delete_collection(_TABLE)
    handle.create_collection(_TABLE).insert_many(_rows())
    yield handle
    handle.delete_collection(_TABLE)


@pytest.fixture(scope="module")
def oxi_store() -> Any:
    store = oxi.Store()
    store.load(_DATA.encode("utf-8"), oxi.RdfFormat.TURTLE)
    return store


def _multiset(rows: list[dict[str, Any]]) -> list[str]:
    return sorted(json.dumps(r, sort_keys=True) for r in rows)


def _arango(db: Any, body: str) -> list[dict[str, Any]]:
    res = translate(_PREFIX + body, resolver=SchemaResolver.from_turtle(_TTL))
    db.aql.validate(res.aql)
    return [drop_null_bindings(r) for r in db.aql.execute(res.aql, bind_vars=res.bind_vars)]


def _oxigraph(store: Any, body: str) -> list[dict[str, Any]]:
    return [normalize_oxi_row(r) for r in oxi_bindings(store, _PREFIX + body)]


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        # Two matches for a, one for d, none for c (it bound nothing for all three).
        (
            "SELECT ?s ?o WHERE { ?s a :Person OPTIONAL { ?s :knows ?o } }",
            [
                {"s": EX + "a", "o": EX + "b"},
                {"s": EX + "a", "o": EX + "c"},
                {"s": EX + "c"},
                {"s": EX + "d", "o": EX + "e"},
            ],
        ),
        # A two-triple body joins inside the OPTIONAL: only b has an email.
        (
            "SELECT ?s ?o ?m WHERE { ?s a :Person OPTIONAL { ?s :knows ?o . ?o :email ?m } }",
            [{"s": EX + "a", "o": EX + "b", "m": "b@x.org"}, {"s": EX + "c"}, {"s": EX + "d"}],
        ),
        # An inner FILTER tests each match.
        (
            "SELECT ?s ?o WHERE { ?s a :Person OPTIONAL { ?s :knows ?o FILTER(?o != :b) } }",
            [{"s": EX + "a", "o": EX + "c"}, {"s": EX + "c"}, {"s": EX + "d", "o": EX + "e"}],
        ),
        # Counts see every match (2 + 0 + 1).
        ("SELECT (COUNT(?o) AS ?n) WHERE { ?s a :Person OPTIONAL { ?s :knows ?o } }", [{"n": 3}]),
        # Inside MINUS, an OPTIONAL variable the outer query does not share
        # cannot change the result: c is known by a Person, so it goes.
        (
            "SELECT ?s WHERE { ?s a :Person MINUS { ?t a :Person ; :knows ?s OPTIONAL { ?t :email ?m } } }",
            [{"s": EX + "a"}, {"s": EX + "d"}],
        ),
    ],
)
def test_an_optional_on_a_triples_table_subject_matches_pyoxigraph(
    db: Any, oxi_store: Any, body: str, expected: list[dict[str, Any]]
) -> None:
    arango = _arango(db, body)

    assert _multiset(arango) == _multiset(_oxigraph(oxi_store, body))
    assert _multiset(arango) == _multiset(expected)
