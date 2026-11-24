"""Cross-validation for subject routing without type patterns (PRD §13.6).

SPARQL over a property graph rarely names every subject's class: LDBC SNB's
reference queries write ``?m :hasCreator ?p`` and ``<person> :directKnows
?f`` and let the predicate imply the rest. Three translator capabilities
make that work in PG and LPG, each checked here against pyoxigraph on the
same facts:

* **Symmetric properties** (``owl:SymmetricProperty``) traverse ``ANY``, so
  a relationship stored once reads in both directions (LDBC's ``knows``
  read as ``directKnows``; plan decision D10).
* **Reverse traversal**: a subject nothing bound, with a known object, is
  reached by walking the edge backwards — from a bound object document, or
  from a constant object looked up in its ``rdfs:range`` class. The edge
  collection reaches every subject class by itself (posts *and* comments).
* **Domain routing**: a constant, value-bound or fresh subject is opened in
  the collection its property's ``rdfs:domain`` names; a union of LPG
  labels becomes an any-of label test, and a union across PG collections
  reads each of them (plan decision D9).

PG and LPG stores and the RDF data all derive from :data:`PEOPLE`,
:data:`MESSAGES` and :data:`KNOWS`, so they cannot drift.
"""

from __future__ import annotations

from typing import Any

import pytest

from arango_sparql.api import translate
from arango_sparql.errors import UnsupportedSparqlError
from arango_sparql.translate.resolver import SchemaResolver
from tests.helpers.aql_interp import run_aql_subset
from tests.helpers.oxi import assert_bindings_equal, drop_null_bindings, normalize_oxi_row, oxi_bindings

oxi = pytest.importorskip("pyoxigraph", reason="pyoxigraph required for cross tests")

EX = "http://ex.org/"
PEOPLE = {"alice": "Alice", "bob": "Bob", "carol": "Carol", "dan": "Dan"}
#: (local name, class, creator, content, reply-of)
MESSAGES = [
    ("post1", "Post", "bob", "hello", None),
    ("post2", "Post", "dan", "news", None),
    ("comm1", "Comment", "carol", "nice", "post1"),
    ("comm2", "Comment", "bob", "thanks", "comm1"),
]
#: Each friendship once, in one direction — as LDBC stores ``knows``.
KNOWS = [("alice", "bob"), ("carol", "alice"), ("bob", "dan")]


def _data_ttl() -> str:
    lines = ["@prefix : <http://ex.org/> ."]
    for local, name in PEOPLE.items():
        lines.append(f':{local} a :Person ; :firstName "{name}" .')
    for local, cls, creator, content, reply_of in MESSAGES:
        lines.append(f':{local} a :{cls} ; :hasCreator :{creator} ; :content "{content}" .')
        if reply_of:
            lines.append(f":{local} :replyOf :{reply_of} .")
    for a, b in KNOWS:  # the symmetric closure, as the normaliser materialises it
        lines.append(f":{a} :directKnows :{b} . :{b} :directKnows :{a} .")
    return "\n".join(lines)


def _pg_docs() -> dict[str, list[dict[str, Any]]]:
    cls_coll = {"Post": "Post", "Comment": "Comment"}
    return {
        "Person": [{"_id": f"Person/{k}", "_uri": EX + k, "firstName": n} for k, n in PEOPLE.items()],
        "Post": [
            {"_id": f"Post/{m}", "_uri": EX + m, "content": c}
            for m, cls, _cr, c, _r in MESSAGES
            if cls == "Post"
        ],
        "Comment": [
            {"_id": f"Comment/{m}", "_uri": EX + m, "content": c}
            for m, cls, _cr, c, _r in MESSAGES
            if cls == "Comment"
        ],
        "knows": [{"_from": f"Person/{a}", "_to": f"Person/{b}"} for a, b in KNOWS],
        "has_creator": [
            {"_from": f"{cls_coll[cls]}/{m}", "_to": f"Person/{cr}"} for m, cls, cr, _c, _r in MESSAGES
        ],
        "reply_of": [
            {"_from": f"Comment/{m}", "_to": f"{'Post' if r.startswith('post') else 'Comment'}/{r}"}
            for m, _cls, _cr, _c, r in MESSAGES
            if r
        ],
    }


def _lpg_docs() -> dict[str, list[dict[str, Any]]]:
    nodes = [
        {"_id": f"nodes/{k}", "_uri": EX + k, "labels": ["Person"], "firstName": n} for k, n in PEOPLE.items()
    ] + [
        {"_id": f"nodes/{m}", "_uri": EX + m, "labels": [cls, "Message"], "content": c}
        for m, cls, _cr, c, _r in MESSAGES
    ]
    edges = (
        [{"_from": f"nodes/{a}", "_to": f"nodes/{b}", "type": "KNOWS"} for a, b in KNOWS]
        + [
            {"_from": f"nodes/{m}", "_to": f"nodes/{cr}", "type": "HAS_CREATOR"}
            for m, _cls, cr, _c, _r in MESSAGES
        ]
        + [
            {"_from": f"nodes/{m}", "_to": f"nodes/{r}", "type": "REPLY_OF"}
            for m, _cls, _cr, _c, r in MESSAGES
            if r
        ]
    )
    return {"nodes": nodes, "edges": edges}


_COMMON = """
@prefix : <http://ex.org/> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix phys: <https://arango.solutions/phys#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
:firstName a owl:DatatypeProperty ; rdfs:domain :Person .
:content a owl:DatatypeProperty ; rdfs:domain [ owl:unionOf ( :Post :Comment ) ] .
"""
PG_ONTOLOGY = (
    _COMMON
    + """
:Person a owl:Class ; phys:collectionName "Person" .
:Post a owl:Class ; phys:collectionName "Post" .
:Comment a owl:Class ; phys:collectionName "Comment" .
:directKnows a owl:ObjectProperty, owl:SymmetricProperty ; phys:edgeCollectionName "knows" ;
    rdfs:domain :Person ; rdfs:range :Person .
:hasCreator a owl:ObjectProperty ; phys:edgeCollectionName "has_creator" ; rdfs:range :Person ;
    rdfs:domain [ owl:unionOf ( :Post :Comment ) ] .
:replyOf a owl:ObjectProperty ; phys:edgeCollectionName "reply_of" ; rdfs:domain :Comment .
"""
)
LPG_ONTOLOGY = (
    _COMMON
    + """
:Person a owl:Class ; phys:collectionName "nodes" ; phys:mappingStyle "LABEL" ;
    phys:typeField "labels" ; phys:typeFieldIsArray "true" ; phys:typeValue "Person" .
:Post a owl:Class ; phys:collectionName "nodes" ; phys:mappingStyle "LABEL" ;
    phys:typeField "labels" ; phys:typeFieldIsArray "true" ; phys:typeValue "Post" .
:Comment a owl:Class ; phys:collectionName "nodes" ; phys:mappingStyle "LABEL" ;
    phys:typeField "labels" ; phys:typeFieldIsArray "true" ; phys:typeValue "Comment" .
:directKnows a owl:ObjectProperty, owl:SymmetricProperty ; phys:edgeCollectionName "edges" ;
    phys:mappingStyle "GENERIC_WITH_TYPE" ; phys:typeField "type" ; phys:typeValue "KNOWS" ;
    rdfs:domain :Person ; rdfs:range :Person .
:hasCreator a owl:ObjectProperty ; phys:edgeCollectionName "edges" ; phys:mappingStyle "GENERIC_WITH_TYPE" ;
    phys:typeField "type" ; phys:typeValue "HAS_CREATOR" ; rdfs:range :Person ;
    rdfs:domain [ owl:unionOf ( :Post :Comment ) ] .
:replyOf a owl:ObjectProperty ; phys:edgeCollectionName "edges" ; phys:mappingStyle "GENERIC_WITH_TYPE" ;
    phys:typeField "type" ; phys:typeValue "REPLY_OF" ; rdfs:domain :Comment .
"""
)
MODELS = [
    pytest.param(PG_ONTOLOGY, _pg_docs, id="pg"),
    pytest.param(LPG_ONTOLOGY, _lpg_docs, id="lpg"),
]


@pytest.fixture(scope="module")
def oxi_store() -> Any:
    store = oxi.Store()
    store.load(_data_ttl().encode("utf-8"), oxi.RdfFormat.TURTLE)
    return store


def _strict(ttl: str) -> SchemaResolver:
    """Resolve as the live service does: no default-collection fallback."""
    return SchemaResolver(ontology=SchemaResolver.from_turtle(ttl).ontology, strict_subject_resolution=True)


def _run(ttl: str, docs: Any, sparql: str) -> list[dict[str, Any]]:
    result = translate(sparql, resolver=_strict(ttl))
    return [drop_null_bindings(r) for r in run_aql_subset(result.aql, result.bind_vars, docs())]


CASES = [
    pytest.param("SELECT ?f WHERE { :alice :directKnows ?f }", id="symmetric_from_a_constant"),
    pytest.param("SELECT ?a ?b WHERE { ?a :directKnows ?b }", id="symmetric_both_ends_free"),
    pytest.param(
        "SELECT ?m WHERE { :alice :directKnows ?f . ?m :hasCreator ?f }", id="reverse_from_bound_object"
    ),
    pytest.param(
        "SELECT ?m ?c WHERE { ?m :hasCreator :bob ; :content ?c }", id="reverse_from_constant_object"
    ),
    pytest.param(
        "SELECT ?n WHERE { VALUES ?p { :alice :carol } ?p :firstName ?n }",
        id="single_domain_value_bound_subject",
    ),
    pytest.param("SELECT ?p ?n WHERE { ?p :firstName ?n }", id="single_domain_fresh_subject"),
    pytest.param("SELECT ?n WHERE { :dan :firstName ?n }", id="single_domain_constant_subject"),
    pytest.param("SELECT ?p WHERE { :comm2 :replyOf ?p }", id="edge_from_a_constant_routed_by_domain"),
    # Plan D9: Post ∪ Comment is one any-of label test in LPG and a union of
    # the two collections in PG.
    pytest.param(
        "SELECT ?m ?c WHERE { VALUES ?m { :post2 :comm1 } ?m :content ?c }", id="union_domain_value_bound"
    ),
    pytest.param("SELECT ?m ?c WHERE { ?m :content ?c }", id="union_domain_fresh_subject"),
    pytest.param("SELECT ?c WHERE { :comm1 :content ?c }", id="union_domain_constant_subject"),
    pytest.param("SELECT ?m ?n WHERE { ?m :hasCreator ?p . ?p :firstName ?n }", id="union_domain_then_edge"),
    pytest.param("SELECT (COUNT(?m) AS ?n) WHERE { ?m :content ?c }", id="union_domain_count"),
    # The object is known only by its value: looked up in its range and the
    # edge walked backwards, instead of every message read forwards.
    pytest.param("SELECT ?m WHERE { VALUES ?p { :bob :dan } ?m :hasCreator ?p }", id="reverse_from_a_value"),
    # A property path inside an OPTIONAL goes to the correlated subquery
    # (optional_subquery.py); the inline emitter reads one attribute or edge.
    pytest.param(
        "SELECT ?n ?fn WHERE { ?p :firstName ?n . OPTIONAL { ?p :directKnows/:firstName ?fn } }",
        id="path_inside_optional",
    ),
    # #100: the people whose OPTIONAL ?f is unbound join every message's
    # creator (SPARQL 1.1 §18.3); 1 row of pyoxigraph's 13 before #193.
    pytest.param(
        "SELECT ?p ?m WHERE { ?p :firstName ?n OPTIONAL { ?p :directKnows ?f FILTER(?f = :dan) } ?m :hasCreator ?f }",
        id="edge_joins_an_unbound_optional_variable",
    ),
]


@pytest.mark.cross
@pytest.mark.parametrize("ttl,docs", MODELS)
@pytest.mark.parametrize("body", CASES)
def test_routing_matches_oxigraph(oxi_store: Any, ttl: str, docs: Any, body: str) -> None:
    sparql = "PREFIX : <http://ex.org/>\n" + body
    expected = [normalize_oxi_row(r) for r in oxi_bindings(oxi_store, sparql)]
    assert expected, "the case must have a non-empty answer to prove anything"
    assert_bindings_equal(expected, _run(ttl, docs, sparql))


@pytest.mark.cross
def test_a_union_domain_in_lpg_is_an_any_of_label_test(oxi_store: Any) -> None:
    sparql = (
        "PREFIX : <http://ex.org/>\nSELECT ?m ?c WHERE { VALUES ?m { :post2 :comm1 :alice } ?m :content ?c }"
    )
    expected = [normalize_oxi_row(r) for r in oxi_bindings(oxi_store, sparql)]
    assert_bindings_equal(expected, _run(LPG_ONTOLOGY, _lpg_docs, sparql))


def test_a_pg_union_across_tenant_scopes_holds_each_member_to_its_own() -> None:
    """Only Post is tenant-scoped: its documents are held to ``org`` and
    Comments pass. Refused before #218 ("different tenant scoping")."""
    ttl = PG_ONTOLOGY.replace(
        ':Post a owl:Class ; phys:collectionName "Post" .',
        ':Post a owl:Class ; phys:collectionName "Post" ; phys:tenantField "org" .',
    )
    docs = _pg_docs()
    for post in docs["Post"]:
        post["org"] = "t2" if post["_uri"] == EX + "post2" else "t1"
    sparql = "PREFIX : <http://ex.org/>\nSELECT ?c WHERE { ?m :content ?c }"
    result = translate(sparql, resolver=_strict(ttl), tenant_id="t1")
    rows = [drop_null_bindings(r) for r in run_aql_subset(result.aql, result.bind_vars, docs)]
    assert sorted(r["c"] for r in rows) == ["hello", "nice", "thanks"]
