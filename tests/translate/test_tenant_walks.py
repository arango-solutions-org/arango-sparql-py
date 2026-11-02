"""Issue #118 — a document a walk reaches is held to the tenant.

An edge's far vertex, the source of a backward walk, and the endpoints of a
relationship node read on its own were never opened by their class, so no
class applied its tenant FILTER, and another tenant's documents came back.
Answers are cross-checked against pyoxigraph, on the interpreter and on
ArangoDB, in ``tests/cross/test_multi_document_iri_cross.py`` (the
``tenant_*`` cases); these pin the emission and the refusals.
"""

from __future__ import annotations

import pytest

from arango_sparql.api import translate
from arango_sparql.errors import CrossTenantJoinError
from arango_sparql.translate.resolver import SchemaResolver

_PREFIXES = """
@prefix :     <http://ex.org/> .
@prefix owl:  <http://www.w3.org/2002/07/owl#> .
@prefix phys: <https://arango.solutions/phys#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
"""
_PG = (
    _PREFIXES
    + """
:Person a owl:Class ; phys:collectionName "Person" ; phys:tenantField "tenant" .
:City   a owl:Class ; phys:collectionName "City" .
:knows   a owl:ObjectProperty ; phys:edgeCollectionName "knows" ; rdfs:domain :Person ; rdfs:range :Person .
:livesIn a owl:ObjectProperty ; phys:edgeCollectionName "lives" ; rdfs:domain :Person ; rdfs:range :City .
:likes   a owl:ObjectProperty ; phys:edgeCollectionName "likes" ; rdfs:range :Person .
"""
)
_LPG = (
    _PREFIXES
    + """
:Person a owl:Class ; phys:collectionName "nodes" ; phys:mappingStyle "LABEL" ;
    phys:typeField "labels" ; phys:typeFieldIsArray "true" ; phys:typeValue "Person" ; phys:tenantField "org" .
:Place a owl:Class ; phys:collectionName "nodes" ; phys:mappingStyle "LABEL" ;
    phys:typeField "labels" ; phys:typeFieldIsArray "true" ; phys:typeValue "Place" .
:knows a owl:ObjectProperty ; phys:edgeCollectionName "edges" ; rdfs:domain :Person ; rdfs:range :Person .
"""
)
_RPT = (
    _PREFIXES
    + """
:Person a owl:Class ; phys:mappingStyle "RPT" ; phys:triplesCollection "triples" ; phys:tenantField "tenant" .
"""
)


def _aql(ttl: str, body: str, tenant: str | None = "t1") -> str:
    result = translate(
        "PREFIX : <http://ex.org/>\n" + body, resolver=SchemaResolver.from_turtle(ttl), tenant_id=tenant
    )
    return result.aql


_GUARD = "FIRST(SPLIT(NOT_NULL({v}._id, @_p{e}_empty), @_p{s}_slash)) IN @_p{c}_tenant_colls"


def test_an_edges_far_vertex_is_held_to_the_tenant() -> None:
    aql = _aql(_PG, "SELECT ?b WHERE { ?a a :Person . ?a :knows ?b }")
    assert "FOR v2, e3 IN OUTBOUND doc1 @@c2_knows\n" in aql
    guard = _GUARD.format(v="v2", e=2, s=3, c=4)
    assert f"FILTER ({guard} ? v2.tenant == @_p1_tenant : true)\n" in aql


def test_one_ternary_per_tenant_field_whatever_the_number_of_classes() -> None:
    # An AND of per-class ``(!in_class || tenant)`` terms over the hops of a
    # ``:p+`` path ran ArangoDB's planner out of memory; one ternary per
    # walked document, keyed by its collection, does not. The collection
    # list is bound once per sub-query (each length of the path is an arm),
    # not once per class and hop.
    ttl = _PG + "".join(
        f':C{i} a owl:Class ; phys:collectionName "C{i}" ; phys:tenantField "tenant" .\n' for i in range(20)
    )
    result = translate(
        "PREFIX : <http://ex.org/>\nSELECT ?x WHERE { :a :knows+ ?x }",
        resolver=SchemaResolver.from_turtle(ttl),
        tenant_id="t1",
    )
    lists = [k for k in result.bind_vars if k.endswith("_tenant_colls")]
    hops = result.aql.count(" IN OUTBOUND ")
    assert hops > len(lists) >= 1
    assert sorted(result.bind_vars[lists[0]]) == sorted(["Person", *(f"C{i}" for i in range(20))])
    assert result.aql.count(" ? ") == hops and "IS_SAME_COLLECTION" not in result.aql


def test_a_backward_walks_source_is_held_to_the_tenant() -> None:
    # ``likes`` declares no domain, so nothing else places the source
    # (issue #113: another tenant's liker came back).
    aql = _aql(_PG, "SELECT ?x WHERE { ?y a :Person . ?x :likes ?y }")
    walk = aql.split(" IN INBOUND ", 1)[1].splitlines()
    assert any(
        line.startswith("FILTER (FIRST(SPLIT(NOT_NULL(v") and "tenant == @_p1_tenant : true)" in line
        for line in walk
    )


def test_in_a_shared_collection_only_the_tenant_scoped_label_is_tested() -> None:
    # A Place in ``nodes`` carries no tenant; only a Person document must.
    aql = _aql(_LPG, "SELECT ?b WHERE { ?a a :Person . ?a :knows ?b }")
    guard = _GUARD.format(v="v2", e=3, s=4, c=5)
    assert (
        f"FILTER ({guard} && LENGTH(INTERSECTION(TO_ARRAY(v2.labels), @_p6_tenant_labels)) > 0"
        " ? v2.org == @_p2_tenant : true)\n"
    ) in aql


def test_a_default_collection_read_is_held_to_the_tenant() -> None:
    # A subject no class places reads the default collection, which can hold
    # tenant-scoped documents (here it is Person's own).
    ttl = _PG + ":nick a owl:DatatypeProperty .\n"
    result = translate(
        "PREFIX : <http://ex.org/>\nSELECT ?s ?n WHERE { ?s :nick ?n }",
        resolver=SchemaResolver.from_turtle(ttl, default_collection="Person"),
        tenant_id="t1",
    )
    assert "? doc1.tenant == @" in result.aql


def test_without_tenants_in_the_mapping_a_walk_is_unchanged() -> None:
    ttl = _PG.replace(' ; phys:tenantField "tenant"', "")
    assert "tenant" not in _aql(ttl, "SELECT ?b WHERE { ?a a :Person . ?a :knows ?b }", tenant=None)


def test_without_a_tenant_a_walk_sees_no_tenant_scoped_document() -> None:
    # As a reader with no tenant sees none of them; the walk is not refused.
    ttl = _PG.replace(
        ':Person a owl:Class ; phys:collectionName "Person" ; phys:tenantField "tenant" .', ""
    ) + (
        ':Person a owl:Class ; phys:collectionName "Person" .\n'
        ':Secret a owl:Class ; phys:collectionName "Secret" ; phys:tenantField "tenant" .\n'
    )
    aql = _aql(ttl, "SELECT ?b WHERE { ?a a :Person . ?a :knows ?b }", tenant=None)
    assert "_tenant_colls ? false : true)" in aql


def test_classes_under_several_tenant_roots_cannot_be_walked() -> None:
    ttl = (
        _PREFIXES
        + """
:Person a owl:Class ; phys:collectionName "Person" ; phys:tenantField "org" ; phys:tenantEntity "Org" .
:Doc a owl:Class ; phys:collectionName "Doc" ; phys:tenantField "ws" ; phys:tenantEntity "Workspace" .
:wrote a owl:ObjectProperty ; phys:edgeCollectionName "wrote" ; rdfs:domain :Person ; rdfs:range :Doc .
"""
    )
    with pytest.raises(CrossTenantJoinError, match="several tenant roots"):
        _aql(ttl, "SELECT ?d WHERE { ?p a :Person . ?p :wrote ?d }")


@pytest.mark.parametrize("body", ["SELECT ?s ?n WHERE { ?s :name ?n }", "SELECT ?n WHERE { :b :name ?n }"])
def test_an_untyped_triple_store_subject_under_tenants_is_refused(body: str) -> None:
    # A typed one already was: RPT rows have no tenant column.
    with pytest.raises(
        CrossTenantJoinError, match="declares phys:tenantField 'tenant' on triples table 'triples'"
    ):
        _aql(_RPT, body)
