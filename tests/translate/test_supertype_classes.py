"""Class mapping styles from the sibling libraries (LDBC plan decision D9).

arango-schema-analyzer's ``LABELS`` and arango-query-core's ``MULTI_LABEL``
map a class to one label in an array field; arango-query-core's ``UNION``
maps a supertype (LDBC's ``Message``) to its member classes, declared as
``rdfs:subClassOf`` subclasses. All three used to read as a plain collection
— a collection named after the supertype (no rows), or every document of the
shared collection (no label test) — with no error. An unknown style is now
refused instead.

Semantics are cross-checked against pyoxigraph in
``tests/cross/test_supertype_cross.py``; these pin the resolution and the AQL.
"""

from __future__ import annotations

from typing import Any

import pytest

from arango_sparql.api import translate
from arango_sparql.errors import SchemaResolutionError, UnsupportedSparqlError
from arango_sparql.translate.resolver import SchemaResolver

_PREFIXES = """
@prefix : <http://ex.org/> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix phys: <https://arango.solutions/phys#> .
"""

UNION_TTL = (
    _PREFIXES
    + """
:Post a owl:Class ; phys:collectionName "Post" .
:Comment a owl:Class ; phys:collectionName "Comment" .
:Message a owl:Class ; phys:mappingStyle "UNION" .
:Post rdfs:subClassOf :Message .
:Comment rdfs:subClassOf :Message .
:content a owl:DatatypeProperty ; rdfs:domain :Message .
"""
)


def _resolver(ttl: str) -> SchemaResolver:
    return SchemaResolver(ontology=SchemaResolver.from_turtle(ttl).ontology, strict_subject_resolution=True)


def _aql(ttl: str, body: str, **kwargs: Any) -> str:
    return translate("PREFIX : <http://ex.org/>\n" + body, resolver=_resolver(ttl), **kwargs).aql


# -- styles ---------------------------------------------------------------------


def test_an_unknown_class_style_is_refused_not_read_as_a_collection() -> None:
    ttl = _PREFIXES + ':Doc a owl:Class ; phys:mappingStyle "SOMETHING_NEW" ; phys:collectionName "docs" .'
    with pytest.raises(SchemaResolutionError, match="SOMETHING_NEW"):
        _aql(ttl, "SELECT ?d WHERE { ?d a :Doc }")


@pytest.mark.parametrize("style", ["LABELS", "MULTI_LABEL"])
def test_a_label_array_class_tests_its_label(style: str) -> None:
    # Before: no label test, so every document of `nodes` was a :Node.
    ttl = (
        _PREFIXES + f':Node a owl:Class ; phys:mappingStyle "{style}" ; phys:collectionName "nodes" ; '
        'phys:labelsField "labels" ; phys:labelValue "Message" .'
    )
    assert _aql(ttl, "SELECT ?m WHERE { ?m a :Node }") == (
        "FOR doc1 IN @@c1_nodes\nFILTER @_p1_labels IN doc1.labels\nRETURN { m: NOT_NULL(doc1._uri, doc1._id) }"
    )


def test_a_label_array_class_without_a_label_value_uses_its_local_name() -> None:
    ttl = (
        _PREFIXES + ':Message a owl:Class ; phys:mappingStyle "MULTI_LABEL" ; phys:collectionName "nodes" ; '
        'phys:labelsField "labels" .'
    )
    resolved = _resolver(ttl).resolve_class("http://ex.org/Message")
    assert (resolved.type_field, resolved.type_value, resolved.type_field_is_array) == (
        "labels",
        "Message",
        True,
    )


def test_a_label_array_class_needs_its_labels_field() -> None:
    ttl = _PREFIXES + ':Node a owl:Class ; phys:mappingStyle "LABELS" ; phys:collectionName "nodes" .'
    with pytest.raises(SchemaResolutionError, match="labelsField"):
        _aql(ttl, "SELECT ?m WHERE { ?m a :Node }")


# -- UNION supertypes -----------------------------------------------------------


def test_supertype_members_are_its_subclasses() -> None:
    members = _resolver(UNION_TTL).supertype_members("http://ex.org/Message")
    assert [m.collection for m in members] == ["Comment", "Post"]


def test_nested_supertypes_are_flattened() -> None:
    ttl = UNION_TTL + ':Item a owl:Class ; phys:mappingStyle "UNION" . :Message rdfs:subClassOf :Item .'
    members = _resolver(ttl).supertype_members("http://ex.org/Item")
    assert [m.collection for m in members] == ["Comment", "Post"]


def test_a_supertype_cycle_is_a_mapping_error() -> None:
    ttl = (
        _PREFIXES
        + ':A a owl:Class ; phys:mappingStyle "UNION" . :B a owl:Class ; phys:mappingStyle "UNION" .'
        " :A rdfs:subClassOf :B . :B rdfs:subClassOf :A ."
    )
    with pytest.raises(SchemaResolutionError, match="cycle"):
        _resolver(ttl).supertype_members("http://ex.org/A")


def test_a_supertype_without_members_is_a_mapping_error() -> None:
    ttl = _PREFIXES + ':Message a owl:Class ; phys:mappingStyle "UNION" .'
    with pytest.raises(SchemaResolutionError, match="no members"):
        _aql(ttl, "SELECT ?m WHERE { ?m a :Message }")


def test_a_supertype_type_pattern_reads_every_member_collection() -> None:
    # Before: FOR doc1 IN @@c1_Message, a collection that does not exist.
    aql = _aql(UNION_TTL, "SELECT ?m WHERE { ?m a :Message }")
    assert aql.startswith(
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
    )
    # An IRI in both members is one Message: the Post copy is dropped (#124).
    assert "FILTER repeated5 == 0\nRETURN { m: NOT_NULL(doc3._uri, doc3._id) }" in aql


def test_a_constant_subject_of_a_supertype_is_looked_up_in_each_member() -> None:
    aql = _aql(UNION_TTL, "ASK { :comm1 a :Message }")
    assert aql.count("._uri == @_p1_uri") == 2


def test_a_supertype_domain_routes_like_its_members() -> None:
    assert "IN UNION(" in _aql(UNION_TTL, "SELECT ?c WHERE { ?m :content ?c }")


def test_a_supertype_over_one_label_collection_is_an_any_of_label_test() -> None:
    ttl = (
        _PREFIXES
        + """
:Post a owl:Class ; phys:mappingStyle "LABEL" ; phys:collectionName "nodes" ;
    phys:typeField "labels" ; phys:typeFieldIsArray "true" ; phys:typeValue "Post" .
:Comment a owl:Class ; phys:mappingStyle "LABEL" ; phys:collectionName "nodes" ;
    phys:typeField "labels" ; phys:typeFieldIsArray "true" ; phys:typeValue "Comment" .
:Message a owl:Class ; phys:mappingStyle "UNION" .
:Post rdfs:subClassOf :Message . :Comment rdfs:subClassOf :Message .
"""
    )
    assert _aql(ttl, "SELECT ?m WHERE { ?m a :Message }") == (
        "FOR doc1 IN @@c1_nodes\n"
        "FILTER (@_p1_labels IN doc1.labels || @_p2_labels IN doc1.labels)\n"
        "RETURN { m: NOT_NULL(doc1._uri, doc1._id) }"
    )


def test_a_supertype_over_triples_is_refused() -> None:
    ttl = (
        _PREFIXES
        + ':Post a owl:Class ; phys:mappingStyle "RPT" . :Message a owl:Class ; phys:mappingStyle "UNION" .'
        " :Post rdfs:subClassOf :Message ."
    )
    with pytest.raises(UnsupportedSparqlError, match="RPT"):
        _aql(ttl, "SELECT ?m WHERE { ?m a :Message }")


def test_members_with_different_tenant_scoping_are_refused() -> None:
    ttl = UNION_TTL.replace(
        ':Post a owl:Class ; phys:collectionName "Post" .',
        ':Post a owl:Class ; phys:collectionName "Post" ; phys:tenantField "org" .',
    )
    with pytest.raises(UnsupportedSparqlError, match="different tenant scoping"):
        _aql(ttl, "SELECT ?m WHERE { ?m a :Message }", tenant_id="t1")


def test_a_supertype_range_looks_the_object_up_in_each_member() -> None:
    # A constant object whose range is a supertype is looked up in each
    # member's collection, and the edge is walked backwards from it (#122;
    # it used to fall back to reading every Person forwards).
    ttl = (
        UNION_TTL + ':Person a owl:Class ; phys:collectionName "Person" .'
        ' :likes a owl:ObjectProperty ; phys:edgeCollectionName "likes" ; rdfs:domain :Person ;'
        " rdfs:range :Message ."
    )
    aql = _aql(ttl, "SELECT ?p WHERE { ?p :likes :post1 }")
    assert aql.startswith("FOR doc3 IN UNION(\n(\n  FOR doc1 IN @@c1_Comment\n")
    assert "  FOR doc2 IN @@c2_Post\n" in aql
    assert "FOR v4, e5 IN INBOUND doc3 @@c3_likes\n" in aql


# -- one model with arango-query-core ------------------------------------------


def test_supertype_members_agree_with_arango_query_core() -> None:
    aqc = pytest.importorskip("arango_query_core")
    if not hasattr(aqc, "SUPERTYPE_STYLES"):
        pytest.skip("arango-query-core < 0.2.5 has no supertypes")
    from arango_query_core.mapping import MappingBundle

    bundle = MappingBundle(
        conceptual_schema={
            "entities": [
                {"name": "Post"},
                {"name": "Comment"},
                {"name": "Message", "subtypes": ["Post", "Comment"]},
                {"name": "Item", "subtypes": ["Message"]},
                {"name": "Node"},
            ]
        },
        physical_mapping={
            "entities": {
                "Post": {"style": "COLLECTION", "collectionName": "Post"},
                "Comment": {"style": "COLLECTION", "collectionName": "Comment"},
                "Message": {"style": "UNION", "members": ["Post", "Comment"]},
                "Item": {"style": "UNION", "members": ["Message"]},
                "Node": {
                    "style": "MULTI_LABEL",
                    "collectionName": "nodes",
                    "labelsField": "labels",
                    "labelValue": "Node",
                },
            }
        },
        metadata={},
    )
    core = aqc.MappingResolver(bundle)
    ours = SchemaResolver.from_turtle(aqc.mapping_to_turtle(bundle))
    ns = "http://arangodb.com/schema/hybrid#"
    for supertype in ("Message", "Item"):
        assert sorted(m.iri.removeprefix(ns) for m in ours.supertype_members(ns + supertype)) == sorted(
            core.supertype_members(supertype)
        )
    node = ours.resolve_class(ns + "Node")
    assert (node.collection, node.type_field, node.type_value, node.type_field_is_array) == (
        "nodes",
        "labels",
        "Node",
        True,
    )


def test_a_described_subject_keeps_every_copy() -> None:
    # DESCRIBE reads every attribute of every copy (review of #146, #124).
    assert "repeated" not in _aql(UNION_TTL, "DESCRIBE ?m WHERE { ?m a :Message }")


def test_only_an_iri_is_probed_for_another_copy() -> None:
    # An ``_id`` names one collection's document, so a row without ``_uri``
    # repeats nothing; the guard keeps it from probing (a full scan per row
    # on such data, review of #146). The guard is a ternary around the
    # subquery, not a FILTER inside it, so such a row starts no subquery.
    aql = _aql(UNION_TTL, "SELECT ?m WHERE { ?m a :Message }")
    assert (
        "doc3)) && doc3._uri != null) ? LENGTH((\n  FOR doc4 IN @@c3_Comment\n  FILTER doc4._uri != null\n  FILTER doc4._uri == doc3._uri\n"
        in aql
    )
    assert ")) : 0\nFILTER repeated5 == 0\n" in aql
