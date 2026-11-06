"""The graph a SELECT draws (``translate/graph_shape.py``): its triple patterns, and a run
that projects every variable in them, so the entities behind the rows can be drawn."""

from __future__ import annotations

import pytest

from arango_sparql.translate.graph_shape import query_patterns, widen_projection

P = "PREFIX : <https://example.org/library#>\n"
LIB = "https://example.org/library#"
NAMES = P + (
    "SELECT ?memberName ?bookTitle\nWHERE {\n"
    "  ?loan a :Loan ; :borrowedBy ?member ; :ofBook ?book .\n"
    "  ?member :name ?memberName .\n  ?book :title ?bookTitle .\n} LIMIT 10"
)


def test_a_plain_select_is_widened_to_every_variable() -> None:
    widened = widen_projection(NAMES)
    assert widened is not None
    assert "SELECT DISTINCT * WHERE {" in widened and widened.endswith("} LIMIT 10")
    assert widened.startswith(P)  # prologue, pattern and modifiers kept as written


@pytest.mark.parametrize(
    "query",
    [
        P + "SELECT (COUNT(?l) AS ?n) WHERE { ?l a :Loan }",
        P + "SELECT ?m (COUNT(?l) AS ?n) WHERE { ?l :borrowedBy ?m } GROUP BY ?m",
        P + "SELECT ?m WHERE { ?l :borrowedBy ?m } GROUP BY ?m HAVING (COUNT(?l) > 1)",
        P + "SELECT ?x WHERE { { SELECT ?x WHERE { ?x a :Loan } } }",
        P + "SELECT ?x WHERE { VALUES ?x { :a } ?x a :Loan }",
        P + "SELECT ?m WHERE { ?l :borrowedBy ?m } ORDER BY DESC(COUNT(?l))",
        P + "ASK { ?x a :Loan }",
        P + "CONSTRUCT { ?x a :Loan } WHERE { ?x a :Loan }",
        "not sparql at all",
    ],
    ids=[
        "aggregate",
        "group-by",
        "having",
        "subquery",
        "values",
        "aggregate-in-order",
        "ask",
        "construct",
        "garbage",
    ],
)
def test_what_cannot_be_widened_safely_is_left_alone(query: str) -> None:
    assert widen_projection(query) is None


def test_keywords_inside_strings_iris_and_comments_do_not_count() -> None:
    q = P + '# SELECT (in a comment)\nSELECT ?x WHERE { ?x :name "SELECT ( GROUP BY" ; :p <urn:x:SELECT> }'
    widened = widen_projection(q)
    assert widened is not None and '"SELECT ( GROUP BY"' in widened and "# SELECT (in a comment)" in widened


def test_the_patterns_say_how_the_variables_connect() -> None:
    patterns = query_patterns(NAMES)
    edge = {"subject": {"var": "loan"}, "predicate": {"iri": LIB + "borrowedBy"}, "object": {"var": "member"}}
    typed = {
        "subject": {"var": "loan"},
        "predicate": {"iri": "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"},
        "object": {"iri": LIB + "Loan"},
    }
    attribute = {
        "subject": {"var": "member"},
        "predicate": {"iri": LIB + "name"},
        "object": {"var": "memberName"},
    }
    for p in (edge, typed, attribute):
        assert p in patterns
    assert len(patterns) == 5


def test_patterns_in_optional_and_union_count_and_paths_do_not() -> None:
    q = P + (
        "SELECT * WHERE { ?l :borrowedBy ?m . OPTIONAL { ?m :city ?c } "
        '{ ?l :ofBook ?b } UNION { ?l :ofBook/:title ?t } ?m :name "Ada" }'
    )
    preds = [(p["predicate"]["iri"], p["object"]) for p in query_patterns(q)]
    assert (LIB + "city", {"var": "c"}) in preds and (LIB + "ofBook", {"var": "b"}) in preds
    assert (LIB + "name", {"literal": "Ada"}) in preds
    assert all("title" not in iri for iri, _ in preds)  # a property path is not one triple
