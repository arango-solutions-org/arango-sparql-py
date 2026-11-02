"""``xsd:dateTime ± xsd:duration`` (SPARQL 1.1 §17.3, XPath op:add-*-to-dateTime).

Lowered by :mod:`arango_sparql.translate.date_arithmetic`: the duration is
split into signed months and milliseconds at translation time, the shift
runs on the input's local clock and is written back in the input's form,
and a comparison with a shifted date compares instants (#123). Before that,
the arithmetic went through the numeric guard (``IS_NUMBER(a) &&
IS_NUMBER(b) ? a + b : null``), which made every date window empty, and the
duration was bound as a Python ``timedelta`` that the driver could not
serialise (LDBC IC3 / IC4, PRD §13.6). Live semantics are checked in
``tests/integration/test_date_arithmetic_live.py``.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from rdflib import XSD, Literal

from arango_sparql.api import translate
from arango_sparql.errors import UnsupportedSparqlError
from arango_sparql.translate.date_arithmetic import duration_parts
from arango_sparql.translate.resolver import SchemaResolver

_ONTOLOGY = """
@prefix : <http://ex.org/> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix phys: <https://arango.solutions/phys#> .
:Event a owl:Class ; phys:collectionName "Event" .
:at a owl:DatatypeProperty .
"""
_PREFIX = "PREFIX : <http://ex.org/> PREFIX xsd: <http://www.w3.org/2001/XMLSchema#> "
_START = '"2010-12-01T00:00:00.000Z"^^xsd:dateTime'


def _translate(body: str):  # noqa: ANN202 — TranslateResult
    return translate(_PREFIX + body, resolver=SchemaResolver.from_turtle(_ONTOLOGY))


def _binds(result: Any, hint: str) -> list[Any]:
    return [v for k, v in result.bind_vars.items() if k.endswith(f"_{hint}")]


def test_date_plus_duration_binds_months_and_milliseconds() -> None:
    result = _translate(f'SELECT ?e WHERE {{ BIND({_START} + "P1Y2M3DT4H5M6.5S"^^xsd:duration AS ?e) }}')
    assert _binds(result, "months") == [14]
    assert _binds(result, "millis") == [((3 * 24 + 4) * 60 + 5) * 60_000 + 6_500]
    assert "DATE_ADD(DATE_ISO8601(" in result.aql
    json.dumps(result.bind_vars)  # the driver must be able to send it


def test_every_string_the_shift_uses_is_a_bind_parameter() -> None:
    result = _translate(f'SELECT ?e WHERE {{ BIND({_START} + "P30D"^^xsd:duration AS ?e) }}')
    assert '"' not in result.aql


def test_duration_plus_date_commutes() -> None:
    result = _translate(f'SELECT ?e WHERE {{ BIND("P1D"^^xsd:duration + {_START} AS ?e) }}')
    assert _binds(result, "millis") == [86_400_000] and _binds(result, "months") == [0]


def test_date_minus_duration_negates_both_parts() -> None:
    result = _translate(f'SELECT ?e WHERE {{ BIND({_START} - "P1MT36H"^^xsd:duration AS ?e) }}')
    assert _binds(result, "months") == [-1] and _binds(result, "millis") == [-36 * 3_600_000]


def test_a_comparison_with_a_shifted_date_compares_instants() -> None:
    result = _translate(
        f'SELECT ?x WHERE {{ ?x a :Event ; :at ?at . FILTER(?at + "P1D"^^xsd:dayTimeDuration = {_START}) }}'
    )
    assert "DATE_TIMESTAMP(" in result.aql and " == DATE_TIMESTAMP(@" in result.aql


@pytest.mark.parametrize(
    ("lexical", "parts"),
    [
        ("P30D", (0, 30 * 86_400_000)),
        ("-PT36H", (0, -36 * 3_600_000)),
        ("P1Y", (12, 0)),
        ("-P1Y2M", (-14, 0)),
        ("PT0.001S", (0, 1)),
        ("P0D", (0, 0)),
    ],
)
def test_duration_parts(lexical: str, parts: tuple[int, int]) -> None:
    assert duration_parts(Literal(lexical, datatype=XSD.duration)) == parts


@pytest.mark.parametrize("lexical", ["PT0.0001S", "P", "P1H", "1D"])
def test_a_duration_the_shift_cannot_represent_fails_closed(lexical: str) -> None:
    with pytest.raises(UnsupportedSparqlError):
        duration_parts(Literal(lexical, datatype=XSD.duration))


def test_a_date_window_filter_uses_the_shifted_date() -> None:
    result = _translate(
        f'SELECT ?x WHERE {{ ?x a :Event ; :at ?at . FILTER(?at >= {_START} && ?at < {_START} + "P30D"^^xsd:duration) }}'
    )
    assert "DATE_ADD(" in result.aql and "IS_NUMBER" not in result.aql
    # ``?at >= start`` compares as before; only the shifted side reads instants.
    assert "DATE_TIMESTAMP(" in result.aql


@pytest.mark.parametrize(
    "body",
    [
        'SELECT ?e WHERE { BIND("P1D"^^xsd:duration AS ?e) }',
        'SELECT ?x WHERE { ?x a :Event ; :at ?at . FILTER(?at = "P1D"^^xsd:duration) }',
        f'SELECT ?e WHERE {{ BIND({_START} * "P1D"^^xsd:duration AS ?e) }}',
    ],
)
def test_a_duration_anywhere_else_fails_closed(body: str) -> None:
    with pytest.raises(UnsupportedSparqlError, match="xsd:duration"):
        _translate(body)
