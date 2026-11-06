"""Inline VALUES: a trailing ``VALUES`` clause's rows given as data.

``translate(sparql, values=...)`` must translate exactly as the same rows
written into the query text do (arango-solutions-org/arango-sparql-py#160).
Each case below is written once as text. Its rows are read back from the
algebra rdflib builds for that text, then passed as data with the clause
removed, and the two translations must give the same AQL and bind variables.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from rdflib import BNode, Literal, URIRef, Variable
from rdflib.plugins.sparql.algebra import translateQuery
from rdflib.plugins.sparql.parser import parseQuery

from arango_sparql.api import InlineValues, translate
from arango_sparql.errors import SparqlParseError, UnsupportedSparqlError
from arango_sparql.translate.resolver import SchemaResolver

_TTL = """@prefix owl: <http://www.w3.org/2002/07/owl#> .
<http://ex.org/name> a owl:DatatypeProperty .
<http://ex.org/age> a owl:DatatypeProperty .
"""

_PREFIX = "PREFIX : <http://ex.org/>\n"

_SUBSELECT_GOLDENS = Path(__file__).parent / "subselect.yml"

#: (case, query without its trailing VALUES, the trailing VALUES clause)
CASES = [
    (
        "one_iri_variable",
        "SELECT ?s ?n WHERE { ?s :name ?n }",
        "VALUES (?s) { (:alice) (:bob) }",
    ),
    (
        "two_variables_mixed_terms",
        "SELECT ?s ?n WHERE { ?s :name ?n }",
        'VALUES (?s ?n) { (:alice "Alice") (<http://ex.org/bob> "Bob"@en) '
        '(:carol "C\\"arol"^^<http://www.w3.org/2001/XMLSchema#string>) }',
    ),
    (
        "numbers_and_booleans",
        "SELECT ?s ?a WHERE { ?s :age ?a }",
        "VALUES (?a) { (1) (-2) (2.5) (1.0e3) (true) (false) }",
    ),
    (
        "undef_cells",
        "SELECT ?s ?n WHERE { ?s :name ?n }",
        'VALUES (?s ?n) { (UNDEF "Alice") (:bob UNDEF) (UNDEF UNDEF) }',
    ),
    (
        "repeated_rows",
        "SELECT ?s ?n WHERE { ?s :name ?n }",
        'VALUES (?n) { ("Alice") ("Alice") ("Bob") }',
    ),
    (
        "after_optional_order_and_limit",
        "SELECT ?s ?n ?a WHERE { ?s :name ?n OPTIONAL { ?s :age ?a } } ORDER BY ?n LIMIT 5",
        'VALUES (?s ?n) { (:alice "Alice") (:bob "Bob") }',
    ),
    (
        "a_bind_join_seed",
        "SELECT ?s ?n WHERE { ?s :name ?n }",
        "VALUES (?n) { " + " ".join(f'("key-{i}")' for i in range(250)) + " }",
    ),
]


def _resolver() -> SchemaResolver:
    return SchemaResolver.from_turtle(_TTL, default_collection="Document")


def _rows_rdflib_reads(text: str) -> InlineValues:
    """The trailing VALUES rows, as rdflib's algebra for *text* holds them."""
    parsed = parseQuery(text)
    variables = tuple(str(v) for v in parsed[1]["valuesClause"]["var"])
    found: list[Any] = []

    def visit(node: Any) -> None:
        if getattr(node, "name", None) == "values":
            found.append(node["res"])
        if isinstance(node, dict):
            for child in node.values():
                visit(child)

    visit(translateQuery(parsed).algebra)
    res = found[0] if found else []
    rows = [
        tuple(None if row[Variable(v)] == "UNDEF" else row[Variable(v)] for v in variables) for row in res
    ]
    return InlineValues(variables=variables, rows=rows)


@pytest.mark.parametrize(("case", "query", "clause"), CASES, ids=[c[0] for c in CASES])
def test_rows_as_data_translate_as_the_same_rows_written(case: str, query: str, clause: str) -> None:
    written = translate(_PREFIX + query + "\n" + clause, resolver=_resolver())
    as_data = translate(
        _PREFIX + query, resolver=_resolver(), values=_rows_rdflib_reads(_PREFIX + query + "\n" + clause)
    )
    assert as_data.aql == written.aql
    assert as_data.bind_vars == written.bind_vars


def _trailing_values_goldens() -> list[tuple[str, str, str, str, dict[str, Any]]]:
    """``subselect.yml`` goldens whose query ends in a ``VALUES`` clause, as
    ``(name, ontology, query, expected_aql, expected_bind_vars)``."""
    data = yaml.safe_load(_SUBSELECT_GOLDENS.read_text())
    return [
        (
            case["name"],
            data["ontology"],
            case["sparql"],
            case["expected_aql"].rstrip("\n"),
            case["expected_bind_vars"],
        )
        for case in data["cases"]
        if "valuesClause" in parseQuery(case["sparql"])[1]
    ]


def test_the_trailing_values_goldens_are_found() -> None:
    # Guards the parametrization below against silently selecting nothing.
    assert len(_trailing_values_goldens()) >= 3


@pytest.mark.parametrize(
    ("name", "ontology", "query", "expected_aql", "expected_bind_vars"),
    _trailing_values_goldens(),
    ids=[c[0] for c in _trailing_values_goldens()],
)
def test_rows_as_data_give_the_golden_aql(
    name: str, ontology: str, query: str, expected_aql: str, expected_bind_vars: dict[str, Any]
) -> None:
    # The acceptance test #160 names: the existing VALUES goldens, with their
    # rows passed as data, give the AQL and bind variables the goldens pin.
    without_clause = query[: query.rindex("VALUES")]
    assert "valuesClause" not in parseQuery(without_clause)[1]
    resolver = SchemaResolver.from_turtle(ontology, default_collection="Document")
    result = translate(without_clause, resolver=resolver, values=_rows_rdflib_reads(query))
    assert result.aql.rstrip("\n") == expected_aql
    assert result.bind_vars == expected_bind_vars


def test_no_rows_are_refused_as_an_empty_block_written_is() -> None:
    # rdflib reads an empty data block as no values at all, which the
    # translator refuses; rows as data must not translate where text does not.
    query = _PREFIX + "SELECT ?s ?n WHERE { ?s :name ?n }"
    with pytest.raises(UnsupportedSparqlError) as written:
        translate(query + "\nVALUES (?s) { }", resolver=_resolver())
    with pytest.raises(UnsupportedSparqlError) as as_data:
        translate(query, resolver=_resolver(), values=InlineValues(("s",), []))
    assert str(as_data.value) == str(written.value)


def test_a_query_with_its_own_trailing_values_is_refused() -> None:
    with pytest.raises(SparqlParseError, match="already ends in a VALUES clause"):
        translate(
            _PREFIX + "SELECT ?s WHERE { ?s :name ?n } VALUES (?s) { (:a) }",
            resolver=_resolver(),
            values=InlineValues(("s",), [(URIRef("http://ex.org/b"),)]),
        )


def test_a_malformed_query_reports_its_own_error() -> None:
    with pytest.raises(SparqlParseError, match="failed to parse SPARQL"):
        translate(
            _PREFIX + "SELECT ?s WHERE { ?s :name ",
            resolver=_resolver(),
            values=InlineValues(("s",), []),
        )


@pytest.mark.parametrize(
    ("values", "message"),
    [
        (InlineValues((), []), "at least one variable"),
        (InlineValues(("s", "s"), []), "names a variable twice"),
        (InlineValues(("s) { } #",), []), "is not a SPARQL variable name"),
        (InlineValues(("1s",), []), "is not a SPARQL variable name"),
        (InlineValues(("s",), [(URIRef("http://ex.org/a"), Literal(1))]), "has 2 cells for 1"),
        (InlineValues(("s",), [("http://ex.org/a",)]), "holds str"),
        (InlineValues(("s",), [(1,)]), "holds int"),
        (InlineValues(("s",), [(BNode(),)]), "holds BNode"),
        (InlineValues(("s",), [(Variable("x"),)]), "holds Variable"),
        (InlineValues("sn", [(URIRef("http://ex.org/a"), Literal("a"))]), "not the string 'sn'"),
        (InlineValues(("s",), [URIRef("http://ex.org/a")]), "is the single term"),
    ],
    ids=[
        "no_variables",
        "duplicate_variable",
        "injected_text",
        "leading_digit",
        "row_too_wide",
        "plain_string",
        "plain_int",
        "blank_node",
        "variable_cell",
        "variables_as_one_string",
        "row_as_a_bare_term",
    ],
)
def test_malformed_values_are_refused_by_name(values: InlineValues, message: str) -> None:
    with pytest.raises(SparqlParseError, match=message):
        translate(_PREFIX + "SELECT ?s WHERE { ?s :name ?n }", resolver=_resolver(), values=values)
