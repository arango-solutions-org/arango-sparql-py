"""A read of several classes holds each document to its own class's tenant field (#218).

``translate/tenant_scope.py::enforce_classes_tenant_scope``. Classes scoped
the same way keep the one plain FILTER; classes scoped differently get a
per-class test, are refused when nothing tells their documents apart, and
commit every class's tenant root. Executed against pyoxigraph in
``tests/integration/test_tenant_per_class_live.py``.
"""

from __future__ import annotations

import pytest

from arango_sparql.api import translate
from arango_sparql.errors import CrossTenantJoinError, UnsupportedSparqlError
from arango_sparql.translate.resolver import SchemaResolver

_HEAD = """
@prefix :     <http://ex.org/> .
@prefix owl:  <http://www.w3.org/2002/07/owl#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
@prefix phys: <https://arango.solutions/phys#> .
"""


def _label(name: str, tenant: str = "", collection: str = "nodes") -> str:
    return (
        f':{name} a owl:Class ; phys:collectionName "{collection}" ; phys:mappingStyle "LABEL" ; '
        f'phys:typeField "label" ; phys:typeValue "{name}" {tenant}.\n'
    )


def _aql(ttl: str, query: str, tenant: str | None = "t1") -> str:
    resolver = SchemaResolver.from_turtle(_HEAD + ttl)
    return translate("PREFIX : <http://ex.org/> " + query, resolver=resolver, tenant_id=tenant).aql


_ORG = '; phys:tenantField "org" ; phys:tenantEntity "Org" '
_WS = '; phys:tenantField "ws" ; phys:tenantEntity "Org" '
_NAME = ':name a owl:DatatypeProperty ; rdfs:domain :Person, :Doc ; phys:attributeName "name" .\n'


def test_classes_scoped_alike_keep_one_plain_filter() -> None:
    ttl = _label("Person", _ORG) + _label("Doc", _ORG) + _NAME
    aql = _aql(ttl, "SELECT ?s ?n WHERE { ?s :name ?n }")
    assert "FILTER doc1.org == @_p3_tenant" in aql
    assert "!(" not in aql


def test_classes_with_different_tenant_fields_test_each_document_by_its_class() -> None:
    """Main filtered every document on the first class's field."""
    ttl = _label("Person", _ORG) + _label("Doc", _WS) + _NAME
    aql = _aql(ttl, "SELECT ?s ?n WHERE { ?s :name ?n }")
    assert "!((doc1.label == @_p4_label || @_p4_label IN doc1.label)) || doc1.ws == @_p3_tenant" in aql
    assert "!((doc1.label == @_p5_label || @_p5_label IN doc1.label)) || doc1.org == @_p3_tenant" in aql
    assert "FILTER doc1.ws == @" not in aql and "FILTER doc1.org == @" not in aql


def test_a_document_of_an_unscoped_class_passes() -> None:
    """Main held a Tag document to Person's ``org``, dropping it."""
    ttl = (
        _label("Person", _ORG)
        + _label("Tag")
        + ':title a owl:DatatypeProperty ; rdfs:domain :Person, :Tag ; phys:attributeName "title" .\n'
    )
    aql = _aql(ttl, "SELECT ?s ?t WHERE { ?s :title ?t }")
    assert (
        "FILTER (!((doc1.label == @_p4_label || @_p4_label IN doc1.label)) || doc1.org == @_p3_tenant)" in aql
    )


def test_classes_spanning_collections_are_told_apart_by_collection() -> None:
    """Refused on main ("spans classes with different tenant scoping")."""
    ttl = (
        ':Person a owl:Class ; phys:collectionName "people" ' + _ORG + ".\n"
        ':Doc a owl:Class ; phys:collectionName "docs" ' + _WS + ".\n" + _NAME
    )
    aql = _aql(ttl, "SELECT ?s ?n WHERE { ?s :name ?n }")
    assert "IS_SAME_COLLECTION(@" in aql
    assert ".org == @" in aql and ".ws == @" in aql


def test_classes_under_two_roots_are_refused() -> None:
    """Main committed only the first class's root, so this translated."""
    ttl = (
        _label("Person", _ORG)
        + _label("Doc", '; phys:tenantField "ws" ; phys:tenantEntity "Workspace" ')
        + _NAME
    )
    with pytest.raises(CrossTenantJoinError, match="two tenant roots"):
        _aql(ttl, "SELECT ?s ?n WHERE { ?s :name ?n }")


def test_unlabelled_classes_in_one_collection_scoped_differently_are_refused() -> None:
    ttl = (
        ':Person a owl:Class ; phys:collectionName "things" ' + _ORG + ".\n"
        ':Doc a owl:Class ; phys:collectionName "things" ' + _WS + ".\n" + _NAME
    )
    with pytest.raises(UnsupportedSparqlError, match="no type field to tell their documents apart"):
        _aql(ttl, "SELECT ?s ?n WHERE { ?s :name ?n }")


def test_a_mixed_store_routed_read_tests_each_document_by_its_class() -> None:
    """The issue's mapping: a triples table beside the LPG collection."""
    ttl = (
        _label("Person", _ORG)
        + _label("Doc", _WS)
        + ':Note a owl:Class ; phys:mappingStyle "RPT" ; phys:triplesCollection "triples" .\n'
        + ":name a owl:DatatypeProperty ; rdfs:domain :Person, :Doc, :Note .\n"
    )
    aql = _aql(ttl, "SELECT ?s ?n WHERE { ?s :name ?n }")
    assert ".ws == @" in aql and ".org == @" in aql
    assert "FILTER doc2.ws == @" not in aql and "FILTER doc2.org == @" not in aql


def test_scoped_classes_without_a_tenant_context_are_refused() -> None:
    ttl = _label("Person", _ORG) + _label("Doc", _WS) + _NAME
    with pytest.raises(CrossTenantJoinError, match="no\\s+tenant context"):
        _aql(ttl, "SELECT ?s ?n WHERE { ?s :name ?n }", tenant=None)


def test_classes_on_one_field_keep_the_plain_filter_whatever_their_roots() -> None:
    """``org`` under root Org and ``org`` with no root: one indexable FILTER,
    as on main (review of #228)."""
    ttl = _label("Person", _ORG) + _label("Doc", '; phys:tenantField "org" ') + _NAME
    aql = _aql(ttl, "SELECT ?s ?n WHERE { ?s :name ?n }")
    assert "FILTER doc1.org == @" in aql
    assert "!(" not in aql


def test_a_copy_read_without_a_class_test_holds_every_class_stored_there() -> None:
    """A copy of an IRI looked up by ``_uri`` can be a document of a scoped
    class the read does not name (``Secret``, a t2 copy): it is held to the
    tenant fields of every scoped class in its collection, as a walked
    document is. The first head of #228 let it through (its review)."""
    ttl = (
        ':Agent a owl:Class ; phys:mappingStyle "UNION" .\n'
        + _label("Person", _ORG).replace(" .\n", " ; rdfs:subClassOf :Agent .\n")
        + _label("Doc", _WS).replace(" .\n", " ; rdfs:subClassOf :Agent .\n")
        + _label("Secret", _ORG)
        + ':Other a owl:Class ; rdfs:subClassOf :Agent ; phys:collectionName "others" .\n'
        + _NAME
        + ':nick a owl:DatatypeProperty ; phys:attributeName "nick" .\n'
    )
    aql = _aql(ttl, "SELECT ?n ?k WHERE { :x a :Agent ; :name ?n ; :nick ?k }")
    copies = aql[aql.index("FOR doc4 IN @@") :]
    copies = copies[: copies.index("RETURN doc4")]
    # Held to Doc's and Person's fields, and to Secret's, which the read
    # does not name: three per-class terms in the copy scan.
    assert copies.count("|| doc4.") == 3
    assert copies.count("doc4.org == @") == 2


def test_an_unlisted_class_under_another_root_is_hidden_in_a_copy_read() -> None:
    """``Secret`` keeps the tenant in ``org`` too, but under root Region: its
    t1 is another tenant than Org's t1, so its copies are not visible. Main
    and the first heads of #228 passed them (review of #228)."""
    ttl = (
        ':Agent a owl:Class ; phys:mappingStyle "UNION" .\n'
        + _label("Person", _ORG).replace(" .\n", " ; rdfs:subClassOf :Agent .\n")
        + _label("Secret", '; phys:tenantField "org" ; phys:tenantEntity "Region" ')
        + ':Other a owl:Class ; rdfs:subClassOf :Agent ; phys:collectionName "others" '
        + _ORG
        + ".\n"
        + ':name a owl:DatatypeProperty ; rdfs:domain :Person, :Other ; phys:attributeName "name" .\n'
        + ':nick a owl:DatatypeProperty ; phys:attributeName "nick" .\n'
    )
    aql = _aql(ttl, "SELECT ?n ?k WHERE { :x a :Agent ; :name ?n ; :nick ?k }")
    assert "|| false)" in aql


def test_a_copy_read_is_not_refused_for_a_root_in_another_collection() -> None:
    """A class under another root stored elsewhere does not matter to a copy
    read; the first fix refused the whole mapping (recheck of #228)."""
    ttl = (
        ':Agent a owl:Class ; phys:mappingStyle "UNION" .\n'
        + _label("Person", _ORG).replace(" .\n", " ; rdfs:subClassOf :Agent .\n")
        + _label("Doc", _WS)
        + ':Other a owl:Class ; rdfs:subClassOf :Agent ; phys:collectionName "others" '
        + _ORG
        + ".\n"
        + ':Project a owl:Class ; phys:collectionName "projects" ; phys:tenantField "pf" ; '
        + 'phys:tenantEntity "Workspace" .\n'
        + ':name a owl:DatatypeProperty ; rdfs:domain :Person, :Other ; phys:attributeName "name" .\n'
        + ':nick a owl:DatatypeProperty ; phys:attributeName "nick" .\n'
    )
    aql = _aql(ttl, "SELECT ?s ?n ?k WHERE { ?s a :Agent ; :name ?n ; :nick ?k }")
    assert ".pf" not in aql


def test_a_reverse_walk_into_classes_scoped_differently_is_answered() -> None:
    """Refused on main and on the first head ("a reverse traversal cannot
    apply one tenant filter")."""
    ttl = (
        _label("Person", _ORG)
        + _label("Doc", _WS)
        + _label("Tag")
        + ":wrote a owl:ObjectProperty ; rdfs:domain :Person, :Doc ; rdfs:range :Tag ; "
        + 'phys:edgeCollectionName "wrote" .\n'
    )
    aql = _aql(ttl, "SELECT ?s WHERE { ?s :wrote :g1 }")
    assert ".org == @" in aql and ".ws == @" in aql


def test_a_copy_read_before_any_root_is_committed_is_refused_on_a_multi_root_mapping() -> None:
    """Every class the read names is unscoped, so no root is committed when
    the copies are read; ``Person`` (root Org) is stored there, and the
    mapping also has root Workspace. Which root t1 belongs to is not known:
    hiding Person's copies dropped t1 rows depending on pattern order
    (second recheck of #228), so the read is refused."""
    ttl = (
        ':Agent a owl:Class ; phys:mappingStyle "UNION" .\n'
        + _label("Tag").replace(" .\n", " ; rdfs:subClassOf :Agent .\n")
        + _label("Person", _ORG)
        + ':Other a owl:Class ; rdfs:subClassOf :Agent ; phys:collectionName "others" .\n'
        + ':Project a owl:Class ; phys:collectionName "projects" ; phys:tenantField "pf" ; '
        + 'phys:tenantEntity "Workspace" .\n'
        + ':name a owl:DatatypeProperty ; phys:attributeName "name" .\n'
        + ':nick a owl:DatatypeProperty ; phys:attributeName "nick" .\n'
    )
    with pytest.raises(CrossTenantJoinError, match="before the query names a class"):
        _aql(ttl, "SELECT ?n ?k WHERE { :x a :Agent ; :name ?n ; :nick ?k }")


_MULTI_ROOT_TAIL = (
    ':Other a owl:Class ; rdfs:subClassOf :Agent ; phys:collectionName "others" ORG_SLOT.\n'
    ':Project a owl:Class ; phys:collectionName "projects" ; phys:tenantField "pf" ; '
    'phys:tenantEntity "Workspace" .\n'
    ':name a owl:DatatypeProperty ; phys:attributeName "name" .\n'
    ':nick a owl:DatatypeProperty ; phys:attributeName "nick" .\n'
    ':Agent a owl:Class ; phys:mappingStyle "UNION" .\n'
)


@pytest.mark.parametrize(
    "query",
    [
        "SELECT ?s WHERE { ?s a :Agent }",
        "ASK { :x a :Agent }",
        "SELECT (COUNT(DISTINCT ?s) AS ?c) WHERE { ?s a :Agent }",
    ],
)
def test_a_type_only_supertype_read_is_answered_on_a_multi_root_mapping(query: str) -> None:
    """The copy-dedup probe is class-tested, so a class the supertype does
    not name cannot reach it and need not be placed under a root. The third
    head of #228 refused these, which main answered (its recheck)."""
    ttl = (
        _label("Tag").replace(" .\n", " ; rdfs:subClassOf :Agent .\n")
        + _label("Person", _ORG)
        + _label("Doc", _WS)
        + _MULTI_ROOT_TAIL.replace("ORG_SLOT", "")
    )
    _aql(ttl, query)


def test_rootless_listed_classes_on_one_field_answer_beside_a_rooted_unlisted_one() -> None:
    """Person and Other keep the tenant in ``org`` with no root; Secret, stored
    with Person, is on ``org`` under Org. One field holds them all: the plain
    FILTER applies and no root needs placing (recheck of #228)."""
    ttl = (
        _label("Person", '; phys:tenantField "org" ').replace(" .\n", " ; rdfs:subClassOf :Agent .\n")
        + _label("Secret", _ORG)
        + _MULTI_ROOT_TAIL.replace("ORG_SLOT", '; phys:tenantField "org" ')
    )
    aql = _aql(ttl, "SELECT ?n ?k WHERE { :x a :Agent ; :name ?n ; :nick ?k }")
    assert "|| false" not in aql
    assert ".org == @" in aql


def test_a_root_committed_after_a_copy_read_admitted_another_is_refused() -> None:
    """Person and Other keep the tenant in ``org`` with no root; Secret,
    stored with Person, is on ``org`` under Region. The copy read admits
    Secret's documents by their tenant id alone; a Project pattern then
    commits root Org, whose tenant t1 is not Region's: refused, as on the
    second head of #228 (its fourth recheck; main leaked the Region copy)."""
    ttl = (
        _label("Person", '; phys:tenantField "org" ').replace(" .\n", " ; rdfs:subClassOf :Agent .\n")
        + _label("Secret", '; phys:tenantField "org" ; phys:tenantEntity "Region" ')
        + ':Other a owl:Class ; rdfs:subClassOf :Agent ; phys:collectionName "others" ; phys:tenantField "org" .\n'
        + ':Project a owl:Class ; phys:collectionName "projects" ; phys:tenantField "pf" ; '
        + 'phys:tenantEntity "Org" .\n'
        + ':pn a owl:DatatypeProperty ; rdfs:domain :Project ; phys:attributeName "pn" .\n'
        + ':name a owl:DatatypeProperty ; phys:attributeName "name" .\n'
        + ':nick a owl:DatatypeProperty ; phys:attributeName "nick" .\n'
        + ':Agent a owl:Class ; phys:mappingStyle "UNION" .\n'
    )
    with pytest.raises(CrossTenantJoinError, match="admitted documents under 'Region'"):
        _aql(ttl, "SELECT ?n ?k ?pn WHERE { :x a :Agent ; :name ?n ; :nick ?k . ?p a :Project ; :pn ?pn }")
    # Org committed first: Secret is hidden instead, and the query answers.
    _aql(
        ttl,
        "SELECT ?n ?k ?pn WHERE { { ?p a :Project ; :pn ?pn } UNION { :x a :Agent ; :name ?n ; :nick ?k } }",
    )
