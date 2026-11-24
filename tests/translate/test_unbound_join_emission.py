"""Emission of the join of a variable that can be unbound (SPARQL 1.1 §18.3).

``AqlQueryBuilder.split_scan_on_value`` turns a collection scan joined to a
value that can be null into two sub-queries, an index lookup where the value
is bound and a scan where it is null (review of #196: the single
``(x == null || match)`` FILTER scanned the collection on every row). The
behaviour is cross-validated in ``tests/cross/test_unbound_join_cross.py``
and its index use checked live in
``tests/integration/test_unbound_join_index_live.py``; these tests pin the
shape and the cases that must not change.
"""

from __future__ import annotations

from arango_sparql.api import translate
from arango_sparql.translate.builder import AqlQueryBuilder, subject_matches
from arango_sparql.translate.resolver import SchemaResolver

ONTOLOGY = """
@prefix : <http://ex.org/> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix phys: <https://arango.solutions/phys#> .
:Person a owl:Class ; phys:collectionName "Person" .
:knows a owl:ObjectProperty ; phys:edgeCollectionName "knows" ; rdfs:domain :Person ; rdfs:range :Person .
:email a owl:DatatypeProperty ; rdfs:domain :Person ; phys:attributeName "email" .
"""


def _aql(body: str) -> str:
    return translate("PREFIX : <http://ex.org/>\n" + body, resolver=SchemaResolver.from_turtle(ONTOLOGY)).aql


def test_a_plain_scan_splits_into_an_index_lookup_and_a_null_scan() -> None:
    builder = AqlQueryBuilder()
    scan = builder.fresh_alias()
    builder.for_(scan, "Person")
    assert builder.split_scan_on_value(scan, "row.x", lambda doc: subject_matches(doc, "row.x"))
    builder.return_scalar(scan)
    aql, _ = builder.finalize()
    assert aql.splitlines()[:9] == [
        "FOR doc1 IN UNION(",
        "(",
        "  FOR doc2 IN @@c1_Person",
        "  FILTER (row.x != null && (doc2._uri == row.x || (doc2._uri == null && doc2._id == row.x)))",
        "  RETURN doc2",
        "),",
        "(",
        "  FOR doc3 IN @@c1_Person",
        "  FILTER row.x == null",
    ]


def test_an_alias_that_is_not_a_plain_scan_is_left_alone() -> None:
    builder = AqlQueryBuilder()
    scan = builder.fresh_alias()
    builder.for_(scan, "Person")
    builder.for_traversal("v2", "e3", scan, "knows", direction="OUTBOUND")
    before = list(builder._body_clauses)
    assert not builder.split_scan_on_value("v2", "row.x", lambda doc: f"{doc}._uri == row.x")
    assert not builder.split_scan_on_value("doc9", "row.x", lambda doc: f"{doc}._uri == row.x")
    assert builder._body_clauses == before


def test_a_variable_that_can_be_unbound_is_joined_through_the_split() -> None:
    aql = _aql("SELECT ?s ?x ?m WHERE { ?s a :Person OPTIONAL { ?s :knows ?x } ?x :email ?m }")
    assert "IN UNION(" in aql
    assert "== null ||" not in aql


def test_a_certainly_bound_variable_keeps_the_plain_indexed_join() -> None:
    aql = _aql("SELECT ?s ?x ?m WHERE { ?s a :Person ; :knows ?x . ?x :email ?m }")
    assert "UNION(" not in aql
    assert "== null" not in aql


def test_an_attribute_value_that_can_be_unbound_is_joined_by_a_filter() -> None:
    # An OPTIONAL value is often null: splitting the scan cost 9-170x at
    # 12,000 people (recheck of #236), so the join stays a FILTER, as on main.
    aql = _aql("SELECT ?s ?x ?y WHERE { ?s a :Person OPTIONAL { ?s :email ?x } ?y :email ?x }")
    assert "== null || doc" in aql


def test_a_computed_value_joined_to_an_attribute_splits_the_document_scan() -> None:
    # Null only where ``?a + 1`` errors: the bound value keeps an index on the
    # attribute (one FILTER cost 0.19 s -> 2.8 s; review of #236).
    aql = _aql("SELECT ?s ?t WHERE { ?s :age ?a BIND(?a + 1 AS ?b) ?t :age ?b }")
    assert "bv4 != null && " in aql
    assert ".age == bv4" in aql


def test_an_attribute_joined_to_a_value_read_after_its_scan_is_filtered() -> None:
    # The value is computed from the same document after its scan; the scan
    # cannot read it, so the join stays a FILTER (#226 recheck).
    aql = _aql("SELECT ?s ?b WHERE { ?s :age ?a BIND(?a + 1 AS ?b) ?s :email ?b }")
    assert "(bv4 == null || doc7.email == bv4)" in aql
    assert "bv4 != null" not in aql  # no split arm reads it


def test_an_attribute_joined_to_its_own_document_is_never_split() -> None:
    # ``?s :name ?x`` with ``?x`` read from doc1 itself: a split around doc1's
    # scan referenced doc1 inside it (ERR 1203; recheck of #236).
    aql = _aql("SELECT ?s ?x WHERE { ?s a :Person OPTIONAL { ?s :email ?x } ?s :name ?x }")
    assert "FOR doc1 IN UNION(" not in aql


def _minus_body(where: str) -> object:
    from rdflib.plugins.sparql import prepareQuery

    algebra = prepareQuery(
        "PREFIX : <http://ex.org/> SELECT * WHERE { ?s a :Person MINUS { " + where + " } }"
    ).algebra
    node = algebra.p.p
    while getattr(node, "name", None) != "Minus":
        node = node.p
    return node.p2


def test_certain_variables_follow_every_solution() -> None:
    from arango_sparql.translate.optional_subquery import certain_variables

    # Recheck of #196: the MINUS overlap test needs the variables every
    # solution of the body binds, not every variable it mentions.
    assert certain_variables(_minus_body("?s :name ?n . ?x :email ?m")) == {"s", "n", "x", "m"}
    assert certain_variables(_minus_body('{ ?x :email ?m } UNION { ?s :name "Z" }')) == set()
    assert certain_variables(_minus_body("?s :name ?n OPTIONAL { ?s :knows ?x }")) == {"s", "n"}
    assert certain_variables(_minus_body("VALUES (?s ?x) { (:a UNDEF) (:b :c) }")) == {"s"}
    assert certain_variables(_minus_body("?y :name ?n FILTER(?y = ?x)")) == {"y", "n"}
    # A sub-select's column counts only where its own pattern binds it in
    # every solution; DISTINCT and LIMIT keep that.
    assert certain_variables(_minus_body("{ SELECT ?s WHERE { ?s :name ?n } }")) == {"s"}
    assert certain_variables(_minus_body("{ SELECT DISTINCT ?s WHERE { ?s :name ?n } LIMIT 3 }")) == {"s"}
    assert certain_variables(
        _minus_body("{ SELECT ?s ?x WHERE { ?s :name ?n OPTIONAL { ?s :knows ?x } } }")
    ) == {"s"}
    # A BIND target or an aggregate can be unbound (an expression error,
    # SAMPLE over nothing), so neither counts.
    assert certain_variables(_minus_body("?s :name ?n BIND(?n + 1 AS ?x)")) == {"s", "n"}
    assert certain_variables(
        _minus_body("{ SELECT ?s (SAMPLE(?n) AS ?x) WHERE { ?s :name ?n } GROUP BY ?s }")
    ) == {"s"}
    assert certain_variables(
        _minus_body("{ SELECT ?s (MIN(?n) AS ?x) WHERE { ?s :name ?n } GROUP BY ?s }")
    ) == {"s"}
    # Not even a plain copy in a group: the group-scoped OPTIONAL would
    # substitute it by the outer value, which a computed one cannot take
    # (review of #213).
    assert certain_variables(_minus_body("?s :name ?n BIND(?n AS ?x)")) == {"s", "n"}
    assert certain_variables(_minus_body("?s :name ?n BIND(:c AS ?x)")) == {"s", "n"}
    # A sub-select column is joined by value: a plain copy of a variable
    # bound in every solution, or a constant, counts; an expression or a
    # copy of a variable that can be unbound does not.
    assert certain_variables(_minus_body("{ SELECT ?s (?n AS ?x) WHERE { ?s :name ?n } }")) == {"s", "x"}
    assert certain_variables(_minus_body("{ SELECT ?s ?x WHERE { ?s :name ?n BIND(?n AS ?x) } }")) == {
        "s",
        "x",
    }
    assert certain_variables(_minus_body("{ SELECT ?s (:c AS ?x) WHERE { ?s :name ?n } }")) == {"s", "x"}
    assert certain_variables(_minus_body("{ SELECT ?s (STR(?n) AS ?x) WHERE { ?s :name ?n } }")) == {"s"}
    assert certain_variables(
        _minus_body("{ SELECT ?s ?x WHERE { ?s :name ?n OPTIONAL { ?s :knows ?k } BIND(?k AS ?x) } }")
    ) == {"s"}
    # A pattern the walker does not look inside binds nothing for certain.
    assert certain_variables(_minus_body("SERVICE <http://ex.org/sparql> { ?s :name ?n }")) == set()


def test_minus_and_the_group_scoped_optional_share_one_walker() -> None:
    # Two "bound in every solution" walkers had drifted apart (code-quality
    # review of #196–#199); MINUS now imports the OPTIONAL one.
    from arango_sparql.translate import minus_exists, optional_subquery

    assert minus_exists.certain_variables is optional_subquery.certain_variables
    assert not hasattr(minus_exists, "certainly_bound_variables")
