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


def test_an_edges_far_vertex_is_held_to_the_tenant() -> None:
    aql = _aql(_PG, "SELECT ?b WHERE { ?a a :Person . ?a :knows ?b }")
    assert "FOR v2, e3 IN OUTBOUND doc1 @@c2_knows\n" in aql
    assert "FILTER (!(IS_SAME_COLLECTION(@_p2_coll, v2)) || v2.tenant == @_p1_tenant)\n" in aql


def test_a_vertex_of_a_class_without_tenants_passes_the_test() -> None:
    # ``City`` is not tenant-scoped: the test only constrains Person documents.
    aql = _aql(_PG, "SELECT ?c WHERE { ?a a :Person . ?a :livesIn ?c }")
    assert "(!(IS_SAME_COLLECTION(@_p2_coll, v2)) || v2.tenant == @_p1_tenant)" in aql


def test_a_backward_walks_source_is_held_to_the_tenant() -> None:
    # ``likes`` declares no domain, so nothing else places the source
    # (issue #113: another tenant's liker came back).
    aql = _aql(_PG, "SELECT ?x WHERE { ?y a :Person . ?x :likes ?y }")
    walk = aql.split(" IN INBOUND ", 1)[1].splitlines()
    guard = [line for line in walk if line.startswith("FILTER (!(IS_SAME_COLLECTION(@_p")]
    assert len(guard) == 1 and guard[0].endswith(".tenant == @_p1_tenant)")


def test_in_a_shared_collection_only_the_tenant_scoped_label_is_tested() -> None:
    # A Place in ``nodes`` carries no tenant; only a Person document must.
    aql = _aql(_LPG, "SELECT ?b WHERE { ?a a :Person . ?a :knows ?b }")
    assert (
        "FILTER (!((IS_SAME_COLLECTION(@_p3_coll, v2)) && @_p4_labels IN v2.labels) || v2.org == @_p2_tenant)"
        in aql
    )


def test_without_tenants_in_the_mapping_a_walk_is_unchanged() -> None:
    ttl = _PG.replace(' ; phys:tenantField "tenant"', "")
    assert "IS_SAME_COLLECTION" not in _aql(
        ttl, "SELECT ?b WHERE { ?a a :Person . ?a :knows ?b }", tenant=None
    )


def test_a_walk_that_can_reach_tenant_documents_needs_a_tenant() -> None:
    ttl = _PG.replace(
        ':Person a owl:Class ; phys:collectionName "Person" ; phys:tenantField "tenant" .', ""
    ) + (
        ':Person a owl:Class ; phys:collectionName "Person" .\n'
        ':Secret a owl:Class ; phys:collectionName "Secret" ; phys:tenantField "tenant" .\n'
    )
    with pytest.raises(CrossTenantJoinError, match="a walk can reach their documents"):
        _aql(ttl, "SELECT ?b WHERE { ?a a :Person . ?a :knows ?b }", tenant=None)


@pytest.mark.parametrize("body", ["SELECT ?s ?n WHERE { ?s :name ?n }", "SELECT ?n WHERE { :b :name ?n }"])
def test_an_untyped_triple_store_subject_under_tenants_is_refused(body: str) -> None:
    # A typed one already was: RPT rows have no tenant column.
    with pytest.raises(CrossTenantJoinError, match="the triple store's classes declare phys:tenantField"):
        _aql(_RPT, body)
