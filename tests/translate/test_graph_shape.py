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
    # Without the limit: the graph is of the solutions behind the rows the client has, which
    # a limit on the wider rows would cut differently (the client keeps the matching ones).
    assert "SELECT DISTINCT * WHERE {" in widened and widened.endswith("}")
    assert widened.startswith(P)  # prologue and pattern kept as written


def test_dataset_clauses_stay_and_solution_modifiers_go() -> None:
    q = (
        P
        + "SELECT ?m FROM <http://g/1> FROM NAMED <http://g/2> WHERE { ?l :borrowedBy ?m } ORDER BY DESC(?m) LIMIT 3 OFFSET 2"
    )
    assert widen_projection(q) == (
        P + "SELECT DISTINCT * FROM <http://g/1> FROM NAMED <http://g/2> WHERE { ?l :borrowedBy ?m }"
    )


@pytest.mark.parametrize(
    "query",
    [
        "SELECT ?values WHERE { ?values :p ?o }",
        "SELECT ?x WHERE { ?x :having ?y }",
        "PREFIX select: <http://s/>\nSELECT ?x WHERE { ?x select:p ?y }",
        "SELECT ?from WHERE { ?from :p ?y }",
    ],
    ids=["values-variable", "having-name", "select-prefix", "from-variable"],
)
def test_a_keyword_inside_a_name_is_not_one(query: str) -> None:
    widened = widen_projection(P + query)
    assert widened is not None and "SELECT DISTINCT * " in widened


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


def test_a_pattern_in_optional_or_union_says_which_rows_it_holds_for() -> None:
    q = P + (
        "SELECT * WHERE { ?l :borrowedBy ?m . OPTIONAL { ?m :city ?c } "
        '{ ?l :ofBook ?b } UNION { ?l :ofBook/:title ?t } ?m :name "Ada" }'
    )
    found = {(p["predicate"]["iri"], str(p["object"])): p.get("when") for p in query_patterns(q)}
    assert found[(LIB + "borrowedBy", str({"var": "m"}))] is None  # every row
    assert found[(LIB + "city", str({"var": "c"}))] == [["c"]]  # rows where the OPTIONAL matched
    assert found[(LIB + "ofBook", str({"var": "b"}))] == [["b"]]  # rows from its UNION branch
    assert found[(LIB + "name", str({"literal": "Ada"}))] is None
    assert all("title" not in iri for iri, _ in found)  # a property path is not one triple


def test_a_group_no_row_can_be_traced_to_is_left_out() -> None:
    # Both branches bind only ?l and ?b: a row does not say which matched, so neither is drawn.
    union = P + "SELECT * WHERE { { ?l :ofBook ?b } UNION { ?l :about ?b } }"
    assert query_patterns(union) == []
    # Every variable of the OPTIONAL is bound outside it: a row does not say whether it matched.
    optional = P + "SELECT * WHERE { ?s a :Member . ?o a :Book OPTIONAL { ?o :by ?s } }"
    assert [p["predicate"]["iri"] for p in query_patterns(optional)] == [
        "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"
    ] * 2


def test_a_nested_group_needs_each_group_around_it() -> None:
    q = P + "SELECT * WHERE { ?l :borrowedBy ?m OPTIONAL { ?m :city ?c OPTIONAL { ?c :in ?country } } }"
    found = {p["predicate"]["iri"]: p.get("when") for p in query_patterns(q)}
    assert found[LIB + "city"] == [["c", "country"]]
    assert found[LIB + "in"] == [["c", "country"], ["country"]]


def test_what_must_not_match_is_never_drawn() -> None:
    q = P + (
        "SELECT * WHERE { ?m a :Member MINUS { ?m a :Banned } MINUS { ?m :knows :alice } "
        "FILTER NOT EXISTS { ?m :owes ?x } }"
    )
    patterns = query_patterns(q)
    assert [p["object"] for p in patterns] == [{"iri": LIB + "Member"}]
