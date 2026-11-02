"""``xsd:dateTime ± xsd:duration`` on a real ArangoDB, against pyoxigraph.

The translation goldens (``tests/translate/test_translate_datetime_arithmetic.py``)
pin the AQL; this checks what ArangoDB's ``DATE_ADD`` / ``DATE_SUBTRACT``
actually return for it, which the in-memory interpreter cannot. Every
answer must equal pyoxigraph's on the same data (the W3C ground truth).
Gated like the rest of the integration suite (``RUN_INTEGRATION=1``).
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from arango_sparql.api import translate
from arango_sparql.translate.resolver import SchemaResolver
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

_EVENTS = "DateArithmeticEvents"
_TTL = f"""
@prefix :     <http://example.org/> .
@prefix owl:  <http://www.w3.org/2002/07/owl#> .
@prefix phys: <https://arango.solutions/phys#> .
:Event a owl:Class ; phys:collectionName "{_EVENTS}" .
:at a owl:DatatypeProperty .
:n a owl:DatatypeProperty .
"""
# Around a 30-day window from 2010-12-01: before it, inside it (both ends),
# on the excluded end, and after it. Timestamps use the LDBC form (ms + Z).
_EVENT = "http://example.org/event/"
_DOCS = [
    {"_key": key, "_uri": _EVENT + key, "at": at, "n": n}
    for key, at, n in (
        ("before", "2010-11-30T23:59:59.999Z", 1),
        ("start", "2010-12-01T00:00:00.000Z", 2),
        ("inside", "2010-12-15T12:00:00.000Z", 3),
        ("last", "2010-12-30T23:59:59.999Z", 4),
        ("end", "2010-12-31T00:00:00.000Z", 5),
    )
]
_PREFIX = "PREFIX : <http://example.org/>\nPREFIX xsd: <http://www.w3.org/2001/XMLSchema#>\n"


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
    if handle.has_collection(_EVENTS):
        handle.delete_collection(_EVENTS)
    handle.create_collection(_EVENTS).insert_many(_DOCS)
    yield handle
    handle.delete_collection(_EVENTS)


@pytest.fixture(scope="module")
def oxi_store() -> Any:
    store = oxi.Store()
    triples = "\n".join(
        f"<{d['_uri']}> a <http://example.org/Event> ; "
        f'<http://example.org/at> "{d["at"]}"^^<http://www.w3.org/2001/XMLSchema#dateTime> ; '
        f"<http://example.org/n> {d['n']} ."
        for d in _DOCS
    )
    store.load(triples.encode("utf-8"), oxi.RdfFormat.TURTLE)
    return store


def _arango(db: Any, body: str) -> list[dict[str, Any]]:
    res = translate(_PREFIX + body, resolver=SchemaResolver.from_turtle(_TTL))
    db.aql.validate(res.aql)
    return [drop_null_bindings(r) for r in db.aql.execute(res.aql, bind_vars=res.bind_vars)]


def _oxigraph(store: Any, body: str) -> list[dict[str, Any]]:
    return [normalize_oxi_row(r) for r in oxi_bindings(store, _PREFIX + body)]


_START = '"2010-12-01T00:00:00.000Z"^^xsd:dateTime'


@pytest.mark.parametrize(
    "body",
    [
        # The LDBC IC3 / IC4 window: [start, start + 30 days).
        f'SELECT ?x WHERE {{ ?x a :Event ; :at ?at . FILTER(?at >= {_START} && ?at < {_START} + "P30D"^^xsd:duration) }}',
        # duration + date, and a dayTimeDuration with hours.
        f'SELECT ?x WHERE {{ ?x a :Event ; :at ?at . FILTER(?at < "PT36H"^^xsd:dayTimeDuration + {_START}) }}',
        # date - duration.
        f'SELECT ?x WHERE {{ ?x a :Event ; :at ?at . FILTER(?at >= {_START} - "P1D"^^xsd:duration) }}',
        # number + duration is a type error in SPARQL: ?e stays unbound.
        'SELECT ?x ?e WHERE { ?x a :Event ; :n ?n . BIND(?n + "P1D"^^xsd:duration AS ?e) }',
    ],
)
def test_date_arithmetic_matches_oxigraph(db: Any, oxi_store: Any, body: str) -> None:
    expected = _oxigraph(oxi_store, body)
    assert expected, "the case must have a non-empty answer to prove anything"
    assert_bindings_equal(expected, _arango(db, body))


# Issue #123: a date or dateTime in another lexical form than DATE_ADD's
# (``…T00:00:00.000Z``) — an xsd:date, a zone other than Z, no milliseconds.
_MIXED = "DateArithmeticMixed"
_MIXED_TTL = f"""
@prefix :     <http://example.org/> .
@prefix owl:  <http://www.w3.org/2002/07/owl#> .
@prefix phys: <https://arango.solutions/phys#> .
:Day a owl:Class ; phys:collectionName "{_MIXED}" .
:d a owl:DatatypeProperty .
:t a owl:DatatypeProperty .
"""
_MIXED_DOCS = [
    {"_key": "e1", "_uri": _EVENT + "e1", "d": "2020-01-31", "t": "2020-01-01T23:30:00Z"},
    {"_key": "e2", "_uri": _EVENT + "e2", "d": "2020-03-01", "t": "2020-01-02T10:00:00+02:00"},
    {"_key": "e3", "_uri": _EVENT + "e3", "d": "2019-12-31", "t": "2020-01-31T10:00:00.500-05:00"},
]
_XSD = "http://www.w3.org/2001/XMLSchema#"


@pytest.fixture(scope="module")
def mixed_db(db: Any) -> Iterator[Any]:
    if db.has_collection(_MIXED):
        db.delete_collection(_MIXED)
    db.create_collection(_MIXED).insert_many(_MIXED_DOCS)
    yield db
    db.delete_collection(_MIXED)


@pytest.fixture(scope="module")
def mixed_store() -> Any:
    store = oxi.Store()
    triples = "\n".join(
        f"<{d['_uri']}> a <http://example.org/Day> ; "
        f'<http://example.org/d> "{d["d"]}"^^<{_XSD}date> ; '
        f'<http://example.org/t> "{d["t"]}"^^<{_XSD}dateTime> .'
        for d in _MIXED_DOCS
    )
    store.load(triples.encode("utf-8"), oxi.RdfFormat.TURTLE)
    return store


def _arango_mixed(db: Any, body: str) -> list[dict[str, Any]]:
    res = translate(_PREFIX + body, resolver=SchemaResolver.from_turtle(_MIXED_TTL))
    db.aql.validate(res.aql)
    cursor = db.aql.execute(res.aql, bind_vars=res.bind_vars)
    assert not cursor.warnings(), cursor.warnings()
    return [drop_null_bindings(r) for r in cursor]


@pytest.mark.parametrize(
    "body",
    [
        # A shifted xsd:date equals, or follows, a date literal (it compared as
        # ``"2020-02-01T00:00:00.000Z"`` against ``"2020-02-01"``).
        'SELECT ?x WHERE { ?x a :Day ; :d ?d FILTER(?d + "P1D"^^xsd:dayTimeDuration = "2020-02-01"^^xsd:date) }',
        'SELECT ?x WHERE { ?x a :Day ; :d ?d FILTER(?d + "P1D"^^xsd:dayTimeDuration > "2020-02-01"^^xsd:date) }',
        # A shifted dateTime against another zone: instants, not text.
        'SELECT ?x WHERE { ?x a :Day ; :t ?t FILTER(?t + "PT1H"^^xsd:dayTimeDuration < "2020-01-02T08:30:00Z"^^xsd:dateTime) }',
        'SELECT ?x WHERE { ?x a :Day ; :t ?t FILTER(?t + "PT1H"^^xsd:dayTimeDuration = "2020-01-02T00:30:00Z"^^xsd:dateTime) }',
        'SELECT ?x WHERE { ?x a :Day ; :t ?t FILTER(?t + "PT1H"^^xsd:dayTimeDuration >= "2020-01-02T00:30:00Z"^^xsd:dateTime) }',
        # The projected dateTime keeps its zone and canonical seconds.
        'SELECT ?x ?n WHERE { ?x a :Day ; :t ?t BIND(?t - "PT30M"^^xsd:dayTimeDuration AS ?n) }',
    ],
)
def test_a_shifted_date_matches_oxigraph_in_any_form(mixed_db: Any, mixed_store: Any, body: str) -> None:
    expected = _oxigraph(mixed_store, body)
    assert expected, "the case must have a non-empty answer to prove anything"
    assert_bindings_equal(expected, _arango_mixed(mixed_db, body))


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        # XPath clamps a month end (op:add-yearMonthDuration-to-date); ArangoDB's
        # DATE_ADD overflows (``2020-03-02``). pyoxigraph (0.5.9) returns
        # nothing for a year-month duration, so the values are XPath's,
        # written out (the day-time row agrees with pyoxigraph).
        ('?d + "P1M"^^xsd:yearMonthDuration', {"e1": "2020-02-29", "e2": "2020-04-01", "e3": "2020-01-31"}),
        ('?d - "P1Y1M"^^xsd:yearMonthDuration', {"e1": "2018-12-31", "e2": "2019-02-01", "e3": "2018-11-30"}),
        ('?d + "P1D"^^xsd:dayTimeDuration', {"e1": "2020-02-01", "e2": "2020-03-02", "e3": "2020-01-01"}),
        # A dateTime month shift runs on the local clock and keeps the zone.
        (
            '?t + "P1M"^^xsd:yearMonthDuration',
            {
                "e1": "2020-02-01T23:30:00Z",
                "e2": "2020-02-02T10:00:00+02:00",
                "e3": "2020-02-29T10:00:00.5-05:00",
            },
        ),
    ],
)
def test_a_date_shift_follows_xpath(mixed_db: Any, body: str, expected: dict[str, str]) -> None:
    rows = _arango_mixed(mixed_db, f"SELECT ?x ?n WHERE {{ ?x a :Day ; :d ?d ; :t ?t BIND({body} AS ?n) }}")
    assert {r["x"].rsplit("/", 1)[1]: r["n"] for r in rows} == expected
