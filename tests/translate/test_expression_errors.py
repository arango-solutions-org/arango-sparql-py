"""Issue #125 — an expression that needs an unbound variable's value is an
error in every solution, and is emitted as ``null`` as a whole.

AQL's ``null`` does not propagate as SPARQL's error does (``null != "A"`` is
true, ``CONCAT(null, "!")`` is ``"!"``). Live behaviour against pyoxigraph is
in ``tests/cross/test_multi_document_iri_cross.py`` (``optional_scope_*``).
"""

from __future__ import annotations

import pytest
from rdflib.plugins.sparql.algebra import translateQuery
from rdflib.plugins.sparql.parser import parseQuery

from arango_sparql.api import translate
from arango_sparql.translate.expression_errors import can_error
from arango_sparql.translate.resolver import SchemaResolver

_TTL = """
@prefix : <http://ex.org/> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix phys: <https://arango.solutions/phys#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
:Person a owl:Class ; phys:collectionName "Person" .
:name a owl:DatatypeProperty ; rdfs:domain :Person .
"""


def _bind(expr: str) -> str:
    result = translate(
        f"PREFIX : <http://ex.org/>\nSELECT ?s ?v WHERE {{ ?s :name ?n BIND({expr} AS ?v) }}",
        resolver=SchemaResolver.from_turtle(_TTL),
    )
    (line,) = [line for line in result.aql.splitlines() if line.startswith("LET bv")]
    return line.split(" = ", 1)[1]


@pytest.mark.parametrize(
    "expr",
    [
        'CONCAT(?z, "!")',
        '?z != "A"',
        "?z + 1",
        "STR(?z)",
        "!(?z = 1)",
        'IF(?z, "a", "b")',
        'IF(?n = "A", ?z, ?z)',
        "COALESCE(?z, ?y)",
        "?z = 1 || ?y = 2",
        "?z IN (1, 2)",
        "-?z",
    ],
)
def test_an_expression_needing_an_unbound_value_is_null(expr: str) -> None:
    assert _bind(expr) == "null"


@pytest.mark.parametrize(
    "expr",
    [
        "BOUND(?z)",
        "!BOUND(?z)",
        'COALESCE(?z, "x")',
        'IF(BOUND(?z), ?z, "x")',
        'IF(?n = "A", "a", ?z)',
        '?z = 1 || ?n = "A"',
        '?z = 1 && ?n = "A"',
        "?n IN (?z, 2)",
        'CONCAT(?n, "!")',
    ],
)
def test_an_expression_that_tolerates_or_may_avoid_the_error_is_translated(expr: str) -> None:
    assert _bind(expr) != "null"


def test_the_unbound_variable_is_still_reported() -> None:
    result = translate(
        'PREFIX : <http://ex.org/>\nSELECT ?s WHERE { ?s :name ?n FILTER(CONCAT(?nmae, "!") = "A!") }',
        resolver=SchemaResolver.from_turtle(_TTL),
    )
    assert any(
        w.get("code") == "W_UNBOUND_VARIABLE_IN_EXPR" and w.get("variable") == "nmae" for w in result.warnings
    )


@pytest.mark.parametrize(
    ("expr", "shape"),
    [
        # error && X is false when X is false, else an error (null).
        ('?z = 1 && ?n = "A"', "(((doc1.name == @_p1)) ? null : false)"),
        # error || X is true when X is true, else an error.
        ('?z = 1 || ?n = "A"', "(((doc1.name == @_p1)) ? true : null)"),
    ],
)
def test_a_boolean_operand_that_is_an_error_decides_nothing(expr: str, shape: str) -> None:
    assert _bind(expr) == shape


# -- #226: a BIND or projection target that can error can be unbound ----------


def _extend_expr(expr: str) -> object:
    algebra = translateQuery(
        parseQuery(f"PREFIX : <http://ex.org/> SELECT * WHERE {{ ?s :p ?k BIND({expr} AS ?v) }}")
    )
    node = algebra.algebra
    while getattr(node, "name", None) != "Extend":
        node = node.p
    return node.expr


@pytest.mark.parametrize(
    "expr, expected",
    [
        ("?k", False),
        ("42", False),
        (":c", False),
        ("BOUND(?zz)", False),
        ("COALESCE(?k / 0, 1)", False),
        ("COALESCE(?k, ?k / 0)", True),
        ("?k / 0", True),
        ("?k + 1", True),
        ('CONCAT(?k, "!")', True),
        ("IF(?k = :c, ?k, 1/0)", True),
    ],
)
def test_can_error(expr: str, expected: bool) -> None:
    assert can_error(_extend_expr(expr)) is expected


def test_a_sub_select_column_that_can_error_is_joined_compatibly() -> None:
    """Where the expression errors the column is unbound, compatible with any
    outer value (§18.3); joined as bound it matched nothing (#226)."""
    result = translate(
        "PREFIX : <http://ex.org/>\nSELECT ?s ?x WHERE { ?s :name ?x "
        '{ SELECT ?s (IF(?n = "A", ?n, 1/0) AS ?x) WHERE { ?s :name ?n } } }',
        resolver=SchemaResolver.from_turtle(_TTL),
    )
    assert ".x == null ||" in result.aql
