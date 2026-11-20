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
    # No document to look up by index: the value is read from the document.
    aql = _aql("SELECT ?s ?x ?y WHERE { ?s a :Person OPTIONAL { ?s :email ?x } ?y :email ?x }")
    assert "== null || doc" in aql
