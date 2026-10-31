"""AQL shapes for routing subjects without type patterns.

Semantics are cross-checked against pyoxigraph in
``tests/cross/test_routing_cross.py``; these pin what the translator emits:
``ANY`` for an ``owl:SymmetricProperty``, ``INBOUND`` from a known object,
an ``rdfs:range`` lookup for a constant object, and an ``rdfs:domain``
collection (with an any-of label test for an LPG union, and a ``UNION`` of
the collections for a PG one: plan decision D9).
"""

from __future__ import annotations

from arango_sparql.api import translate
from arango_sparql.translate.resolver import SchemaResolver
from tests.cross.test_routing_cross import LPG_ONTOLOGY, PG_ONTOLOGY


def _aql(ttl: str, body: str) -> str:
    resolver = SchemaResolver(
        ontology=SchemaResolver.from_turtle(ttl).ontology, strict_subject_resolution=True
    )
    return translate("PREFIX : <http://ex.org/>\n" + body, resolver=resolver).aql


def test_a_symmetric_property_from_a_domain_routed_constant_traverses_any() -> None:
    assert _aql(PG_ONTOLOGY, "SELECT ?f WHERE { :alice :directKnows ?f }") == (
        "FOR doc1 IN @@c1_Person\n"
        "FILTER (doc1._uri == @_p1_uri || (doc1._uri == null && doc1._id == @_p1_uri))\n"
        "FOR v2, e3 IN ANY doc1 @@c2_knows\n"
        "RETURN { f: NOT_NULL(v2._uri, v2._id) }"
    )


def test_a_constant_object_is_looked_up_in_its_range_and_walked_inbound() -> None:
    assert _aql(PG_ONTOLOGY, "SELECT ?m WHERE { ?m :hasCreator :bob }") == (
        "FOR doc1 IN @@c1_Person\n"
        "FILTER (doc1._uri == @_p1_uri || (doc1._uri == null && doc1._id == @_p1_uri))\n"
        "FOR v2, e3 IN INBOUND doc1 @@c2_has_creator\n"
        # The forward plan reads ?m from hasCreator's domain (Post ∪ Comment).
        "FILTER (IS_SAME_COLLECTION(@_p2_coll, v2) || IS_SAME_COLLECTION(@_p3_coll, v2))\n"
        "RETURN { m: NOT_NULL(v2._uri, v2._id) }"
    )


def test_a_reverse_walk_keeps_only_the_subjects_collections() -> None:
    """W3C ``sparqldl-07``: a resource stored in two collections, each copy with
    its own edges, must not be reached twice by walking an edge backwards."""
    resolver = SchemaResolver.from_turtle(PG_ONTOLOGY)
    result = translate(
        "PREFIX : <http://ex.org/>\nSELECT ?m WHERE { ?m :hasCreator :bob }", resolver=resolver
    )
    assert {result.bind_vars["_p2_coll"], result.bind_vars["_p3_coll"]} == {"Comment", "Post"}


def test_an_lpg_union_domain_is_an_any_of_label_test() -> None:
    aql = _aql(LPG_ONTOLOGY, "SELECT ?c WHERE { VALUES ?m { :post2 } ?m :content ?c }")
    assert "FOR doc" in aql and "IN @@c1_nodes" in aql
    assert "(@_p" in aql and " IN doc" in aql and " || " in aql


def test_a_pg_union_domain_reads_each_collection() -> None:
    # Plan D9: ?m is a Post or a Comment, so both collections are scanned and
    # the traversal starts from either kind of document.
    assert _aql(PG_ONTOLOGY, "SELECT ?m ?n WHERE { ?m :hasCreator ?p . ?p :firstName ?n }") == (
        "FOR doc3 IN UNION(\n"
        "(\n"
        "  FOR doc1 IN @@c1_Comment\n"
        "  RETURN doc1\n"
        "),\n"
        "(\n"
        "  FOR doc2 IN @@c2_Post\n"
        "  RETURN doc2\n"
        ")\n"
        ")\n"
        "FOR v4, e5 IN OUTBOUND doc3 @@c3_has_creator\n"
        'FILTER HAS(v4, "firstName")\n'
        "RETURN { m: NOT_NULL(doc3._uri, doc3._id), n: v4.firstName }"
    )


def test_a_constant_subject_in_a_pg_union_domain_is_looked_up_in_each_collection() -> None:
    # The indexed _uri match sits inside each arm, so each collection's index
    # serves it, instead of filtering the union of two full scans.
    assert _aql(PG_ONTOLOGY, "SELECT ?c WHERE { :comm1 :content ?c }") == (
        "FOR doc3 IN UNION(\n"
        "(\n"
        "  FOR doc1 IN @@c1_Comment\n"
        "  FILTER (doc1._uri == @_p1_uri || (doc1._uri == null && doc1._id == @_p1_uri))\n"
        "  RETURN doc1\n"
        "),\n"
        "(\n"
        "  FOR doc2 IN @@c2_Post\n"
        "  FILTER (doc2._uri == @_p1_uri || (doc2._uri == null && doc2._id == @_p1_uri))\n"
        "  RETURN doc2\n"
        ")\n"
        ")\n"
        'FILTER HAS(doc3, "content")\n'
        "RETURN { c: doc3.content }"
    )


def test_a_pg_union_row_counts_as_a_scanned_document() -> None:
    # Every union row is a whole document, never null, so COUNT(?m) counts
    # rows (issue #46) as it would for a single-collection scan.
    aql = _aql(PG_ONTOLOGY, "SELECT (COUNT(?m) AS ?n) WHERE { ?m :content ?c }")
    assert aql.endswith("COLLECT WITH COUNT INTO agg4\nRETURN { n: agg4 }")


def test_a_value_bound_subject_in_a_pg_union_is_matched_inside_each_arm() -> None:
    # AQL materialises each UNION arm, so a match after the union would copy
    # every Post and Comment first (LDBC IS2 hit the memory cap that way).
    assert _aql(PG_ONTOLOGY, "SELECT ?m ?c WHERE { VALUES ?m { :post2 :comm1 } ?m :content ?c }") == (
        "FOR row1 IN @_p1_values\n"
        "FOR doc4 IN UNION(\n"
        "(\n"
        "  FOR doc2 IN @@c1_Comment\n"
        "  FILTER (row1.m != null && (doc2._uri == row1.m || (doc2._uri == null && doc2._id == row1.m)))\n"
        "  RETURN doc2\n"
        "),\n"
        "(\n"
        "  FOR doc3 IN @@c2_Post\n"
        "  FILTER (row1.m != null && (doc3._uri == row1.m || (doc3._uri == null && doc3._id == row1.m)))\n"
        "  RETURN doc3\n"
        ")\n"
        ")\n"
        'FILTER HAS(doc4, "content")\n'
        "RETURN { m: row1.m, c: doc4.content }"
    )


def test_a_value_bound_object_is_looked_up_and_walked_inbound() -> None:
    # The object is known only by its value (VALUES, a sub-select column):
    # looked up in its range by _uri, then the edge walked backwards, instead
    # of every message read forwards from hasCreator's domain.
    assert _aql(PG_ONTOLOGY, "SELECT ?m WHERE { VALUES ?p { :bob } ?m :hasCreator ?p }") == (
        "FOR row1 IN @_p1_values\n"
        "FOR doc2 IN @@c1_Person\n"
        "FILTER (row1.p != null && (doc2._uri == row1.p || (doc2._uri == null && doc2._id == row1.p)))\n"
        "FOR v3, e4 IN INBOUND doc2 @@c2_has_creator\n"
        "FILTER (IS_SAME_COLLECTION(@_p2_coll, v3) || IS_SAME_COLLECTION(@_p3_coll, v3))\n"
        "RETURN { m: NOT_NULL(v3._uri, v3._id) }"
    )


def test_a_reverse_walk_from_a_bound_document_starts_from_every_copy_in_the_range() -> None:
    # One IRI can be several documents (#105): the edge may point at a copy
    # other than the bound one, so its copies in the range are walked too.
    assert _aql(PG_ONTOLOGY, "SELECT ?m WHERE { ?p a :Person . ?m :hasCreator ?p }") == (
        "FOR doc1 IN @@c1_Person\n"
        "FOR doc4 IN UNION(\n"
        "(\n"
        "  FOR doc2 IN [doc1]\n"
        "  FILTER !(IS_SAME_COLLECTION(@_p1_coll, doc2))\n"
        "  RETURN doc2\n"
        "),\n"
        "(\n"
        "  FOR doc3 IN @@c2_Person\n"
        "  FILTER (NOT_NULL(doc1._uri, doc1._id) != null && (doc3._uri == NOT_NULL(doc1._uri, doc1._id) "
        "|| (doc3._uri == null && doc3._id == NOT_NULL(doc1._uri, doc1._id))))\n"
        "  RETURN doc3\n"
        ")\n"
        ")\n"
        "FOR v5, e6 IN INBOUND doc4 @@c3_has_creator\n"
        "FILTER (IS_SAME_COLLECTION(@_p2_coll, v5) || IS_SAME_COLLECTION(@_p3_coll, v5))\n"
        "RETURN { m: NOT_NULL(v5._uri, v5._id) }"
    )


_NO_RANGE = """
@prefix : <http://ex.org/> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix phys: <https://arango.solutions/phys#> .
:Person a owl:Class ; phys:collectionName "Person" .
:Agent a owl:Class ; phys:collectionName "Agent" .
:Fact a owl:Class ; phys:mappingStyle "RPT" ; phys:triplesCollection "Triples" .
:knows a owl:ObjectProperty ; phys:edgeCollectionName "knows" .
"""


def test_without_a_range_the_copies_are_looked_up_in_every_document_collection() -> None:
    resolver = SchemaResolver.from_turtle(_NO_RANGE, default_collection="Document")
    result = translate(
        "PREFIX : <http://ex.org/>\nSELECT ?x WHERE { ?y a :Person . ?x :knows ?y }", resolver=resolver
    )
    looked_up = sorted(v for k, v in result.bind_vars.items() if k.startswith("@c") and v != "knows")
    # The type pattern's Person, then each document collection (not the triples table).
    assert looked_up == ["Agent", "Document", "Person", "Person"]


def test_document_collections_are_the_document_classes_and_the_default_unless_strict() -> None:
    assert SchemaResolver.from_turtle(_NO_RANGE, default_collection="Document").document_collections() == (
        "Agent",
        "Document",
        "Person",
    )
    strict = SchemaResolver(
        ontology=SchemaResolver.from_turtle(_NO_RANGE).ontology, strict_subject_resolution=True
    )
    assert strict.document_collections() == ("Agent", "Person")


def test_listing_the_document_collections_reports_no_advisory_for_unnamed_classes() -> None:
    # A class without phys:collectionName resolves with a W_SCHEMA advisory;
    # listing collections must not report it, nor hide it from a later query.
    ttl = _NO_RANGE + ":Loose a owl:Class .\n"
    resolver = SchemaResolver.from_turtle(ttl)
    assert "Loose" in resolver.document_collections()
    assert resolver.warnings == []
    resolver.resolve_class("http://ex.org/Loose")
    assert [w["iri"] for w in resolver.warnings] == ["http://ex.org/Loose"]
