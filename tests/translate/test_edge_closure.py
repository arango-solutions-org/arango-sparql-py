"""Which ``:p*`` / ``:p+`` paths are walked by a traversal (#183 follow-up).

An object property in an edge collection of its own, walked without a test
on the edge, is answered by one breadth-first traversal per start
(``paths._emit_edge_closure_arm``): complete, with no depth limit and no
depth check. Everything else keeps the bounded arms of #183 and their
refusal past ``property_path_max_depth``. These pin the routing and the
emitted walk; the answers are checked against pyoxigraph in
``tests/cross/test_edge_closure_cross.py`` and on ArangoDB in
``tests/integration/test_edge_closure_live.py``.
"""

from __future__ import annotations

import pytest

from arango_sparql.api import TranslateResult, translate
from arango_sparql.translate.paths import EDGE_CLOSURE_MAX_DEPTH
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
:Person a owl:Class ; phys:collectionName "Person" .
:name a owl:DatatypeProperty ; rdfs:domain :Person .
:knows a owl:ObjectProperty ; phys:edgeCollectionName "knows" ; rdfs:domain :Person ; rdfs:range :Person .
:likes a owl:ObjectProperty ; phys:edgeCollectionName "likes" ; rdfs:domain :Person ; rdfs:range :Person .
:friend a owl:ObjectProperty, owl:SymmetricProperty ; phys:edgeCollectionName "friends" ; rdfs:domain :Person .
:typed a owl:ObjectProperty ; phys:edgeCollectionName "edges" ; phys:mappingStyle "GENERIC_WITH_TYPE" ;
    phys:typeField "type" ; phys:typeValue "TYPED" ; rdfs:domain :Person ; rdfs:range :Person .
:reified a owl:ObjectProperty ; phys:edgeCollectionName "reified" ; phys:reifiedEdge "true" ; rdfs:domain :Person .
"""
)
_TENANT = (
    _PREFIXES
    + """
:Person a owl:Class ; phys:collectionName "Person" ; phys:tenantField "tenant" .
:City a owl:Class ; phys:collectionName "City" ; phys:tenantField "tenant" .
:knows a owl:ObjectProperty ; phys:edgeCollectionName "knows" ; rdfs:domain :Person ; rdfs:range :Person .
"""
)
_RPT = (
    _PREFIXES
    + """
:Person a owl:Class ; phys:mappingStyle "RPT" ; phys:triplesCollection "triples" .
"""
)


def _translate(body: str, ttl: str = _PG, tenant: str | None = None) -> TranslateResult:
    return translate(
        "PREFIX : <http://ex.org/>\n" + body, resolver=SchemaResolver.from_turtle(ttl), tenant_id=tenant
    )


def _walks(aql: str) -> bool:
    """A traversal walks the path, and no arms or depth check are emitted."""
    if "uniqueVertices" in aql:
        assert "MINUS(" not in aql, aql
        return True
    return False


@pytest.mark.parametrize(
    "query",
    [
        pytest.param("SELECT ?s ?o WHERE { ?s :knows+ ?o }", id="plus"),
        pytest.param("SELECT ?s ?o WHERE { ?s :knows* ?o }", id="star"),
        pytest.param("SELECT ?o WHERE { :a :knows+ ?o }", id="constant_start"),
        pytest.param("SELECT ?s WHERE { ?s :knows+ :b }", id="constant_end"),
        pytest.param("ASK { :a :knows+ :b }", id="both_constant"),
        pytest.param("SELECT ?x WHERE { ?x :knows+ ?x }", id="back_to_itself"),
        pytest.param("SELECT ?s ?o WHERE { ?s ^:knows+ ?o }", id="inverse_of_plus"),
        pytest.param("SELECT ?s ?o WHERE { ?s (^:knows)+ ?o }", id="plus_of_inverse"),
        pytest.param("SELECT ?s WHERE { ?s (^:knows)* :a }", id="star_of_inverse"),
        pytest.param("SELECT ?s ?n WHERE { ?s :knows+/:name ?n }", id="then_a_step"),
        pytest.param("SELECT ?s ?o WHERE { ?s :likes/:knows+ ?o }", id="after_a_step"),
        pytest.param('SELECT ?s ?o WHERE { ?s :name "A" . ?s :knows+ ?o }', id="subject_bound_first"),
        pytest.param("SELECT ?s ?o WHERE { VALUES ?o { :b } ?s :knows+ ?o }", id="object_a_value"),
        pytest.param("SELECT ?s ?o WHERE { ?s ((:knows)+)* ?o }", id="nested_modifiers"),
        pytest.param("SELECT ?s ?o WHERE { ?s :friend+ ?o }", id="symmetric"),
    ],
)
def test_an_edge_collection_of_its_own_is_walked(query: str) -> None:
    assert _walks(_translate(query).aql)


@pytest.mark.parametrize(
    "query, ttl",
    [
        pytest.param("SELECT ?s ?o WHERE { ?s :typed+ ?o }", _PG, id="edge_type_test"),
        pytest.param("SELECT ?s ?o WHERE { ?s (:knows/:likes)+ ?o }", _PG, id="sequence"),
        pytest.param("SELECT ?s ?o WHERE { ?s :undeclared+ ?o }", _PG, id="document_attribute"),
        pytest.param("SELECT ?s ?o WHERE { ?s :knows+ ?o }", _RPT, id="triple_store"),
        pytest.param("SELECT ?s ?o WHERE { ?s :reified+ ?o }", _PG, id="reified_relationship"),
        pytest.param('SELECT ?s ?o WHERE { ?o :name "B" . ?s :knows+ ?o }', _PG, id="object_a_document"),
        pytest.param("SELECT ?s WHERE { :b :name ?n . ?s :knows* :b }", _PG, id="constant_object_read_first"),
        pytest.param("SELECT ?o WHERE { :a (^:knows)+ ?o }", _PG, id="inverse_repeated_from_a_constant"),
    ],
)
def test_everything_else_keeps_the_bounded_arms(query: str, ttl: str) -> None:
    assert not _walks(_translate(query, ttl).aql)


def test_zero_or_one_is_unchanged() -> None:
    assert not _walks(_translate("SELECT ?s ?o WHERE { ?s :knows? ?o }").aql)


def test_the_walk_is_bound_and_checked_against_the_edge_count() -> None:
    result = _translate("SELECT ?o WHERE { :a :knows+ ?o }")
    aql, binds = result.aql, result.bind_vars
    walk = next(line.strip() for line in aql.splitlines() if "uniqueVertices" in line)
    assert walk.startswith("FOR v") and " IN 1..@_p" in walk and " OUTBOUND doc" in walk
    # Options and the depth bound are bind parameters, never literals.
    assert '"bfs"' not in aql and '"global"' not in aql
    assert sorted(v for v in binds.values() if v in ("bfs", "global")) == ["bfs", "global"]
    depth = next(k for k, v in binds.items() if v == EDGE_CLOSURE_MAX_DEPTH)
    assert f"FILTER LENGTH(@@c1_knows) <= @{depth} || FAIL(" in aql
    # The start, when a cycle returns to it: one step back from it.
    assert " IN INBOUND doc" in aql and "[*]._id" in aql and "RETURN doc" in aql
    # No tenant scoping, nothing to prune.
    assert "PRUNE" not in walk


def test_a_tenant_scoped_walk_prunes_at_another_tenants_vertex() -> None:
    result = _translate("SELECT ?o WHERE { :a :knows+ ?o }", _TENANT, tenant="t1")
    aql, binds = result.aql, result.bind_vars
    walk = next(line.strip() for line in aql.splitlines() if "uniqueVertices" in line)
    vertex, edge = walk.split()[1].rstrip(","), walk.split()[2]
    test = f"(FIRST(SPLIT(NOT_NULL({vertex}._id, "
    # Pruned only past the start: ArangoDB's PRUNE sees the start at depth 0.
    assert f" PRUNE {edge} != null && !({test}" in walk
    assert f"FILTER {test}" in aql
    # The start is read as the class's document; the tenant list is bound once.
    assert "FILTER doc" in aql and ".tenant == @_p" in aql
    lists = [k for k in binds if k.endswith("_tenant_colls")]
    assert len(lists) == 1 and sorted(binds[lists[0]]) == ["City", "Person"]
