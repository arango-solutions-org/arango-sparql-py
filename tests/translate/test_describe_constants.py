"""``DESCRIBE <iri>`` reads every collection the mapping can store the IRI in (#150).

A constant was read from the default collection only, so a mapping without
one (the service's: strict subject resolution) failed with "collection not
found: Document", and an IRI a class places elsewhere was not described.
Live results: ``tests/integration/test_describe_constant_live.py``.
"""

from __future__ import annotations

import pytest
from rdflib import Graph

from arango_sparql.api import translate
from arango_sparql.errors import SchemaResolutionError
from arango_sparql.translate.resolver import SchemaResolver

_PREFIXES = """
@prefix : <http://ex.org/> .
@prefix owl: <http://www.w3.org/2002/07/owl#> .
@prefix phys: <https://arango.solutions/phys#> .
@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .
"""
_TTL = (
    _PREFIXES
    + """
:Person a owl:Class ; phys:collectionName "Person" ; rdfs:subClassOf :Agent .
:Org a owl:Class ; phys:collectionName "Org" ; rdfs:subClassOf :Agent .
:Agent a owl:Class ; phys:mappingStyle "UNION" .
:Fact a owl:Class ; phys:mappingStyle "RPT" ; phys:collectionName "triples" .
:Claim a owl:Class ; phys:mappingStyle "RPT" ; phys:collectionName "triples" .
"""
)


def _resolver(ttl: str = _TTL, *, strict: bool = True) -> SchemaResolver:
    graph = Graph().parse(data=ttl, format="turtle")
    return SchemaResolver(ontology=graph, strict_subject_resolution=strict)


def test_the_sources_are_every_class_collection_and_triples_table() -> None:
    collections, tables = _resolver().described_sources()
    # The supertype has no collection; two RPT classes on one table read it once.
    assert collections == ("Org", "Person")
    assert [t.collection for t in tables] == ["triples"]


def test_the_default_collection_counts_only_without_strict_resolution() -> None:
    collections, _ = _resolver(strict=False).described_sources()
    assert collections == ("Document", "Org", "Person")


def test_a_triple_store_reads_only_its_table() -> None:
    ttl = _PREFIXES + ':Fact a owl:Class ; phys:mappingStyle "RPT" ; phys:collectionName "triples" .'
    collections, tables = _resolver(ttl, strict=False).described_sources()
    assert collections == ()
    assert [t.collection for t in tables] == ["triples"]


def test_a_sharded_collection_reads_its_whole_family() -> None:
    graph = Graph().parse(data=_TTL, format="turtle")
    resolver = SchemaResolver(
        ontology=graph, strict_subject_resolution=True, shard_families=(("Person", "Person_eu"),)
    )
    assert resolver.described_sources()[0] == ("Org", "Person", "Person_eu")


def test_a_bare_describe_reads_no_default_collection_under_strict_resolution() -> None:
    result = translate("DESCRIBE <http://ex.org/s>", resolver=_resolver())
    collections = {v for k, v in result.bind_vars.items() if k.startswith("@")}
    assert collections == {"Org", "Person", "triples"}


def test_describe_with_where_does_not_multiply_the_where_rows() -> None:
    # The constant's description is a sub-query, not a FOR in the WHERE's rows.
    aql = translate("PREFIX : <http://ex.org/>\nDESCRIBE :t WHERE { ?x a :Person }", resolver=_resolver()).aql
    assert aql.splitlines()[0].startswith("FOR doc1 IN @@c1_Person")
    assert sum(line.startswith("FOR ") for line in aql.splitlines()) == 1


def test_three_described_resources_are_all_kept() -> None:
    # ``APPEND(a, b, c)`` read ``c`` as its unique flag and dropped it.
    aql = translate(
        "PREFIX : <http://ex.org/>\nDESCRIBE ?x ?y ?z WHERE { ?x a :Person . ?y a :Person . ?z a :Org }",
        resolver=_resolver(),
    ).aql
    assert "APPEND(" not in aql
    assert "RETURN (FLATTEN([(" in aql


def test_a_strict_mapping_with_no_collection_refuses() -> None:
    with pytest.raises(SchemaResolutionError, match="places no class in a collection"):
        translate("DESCRIBE <http://ex.org/s>", resolver=_resolver(_PREFIXES))


def test_a_tenant_scoped_class_holds_its_documents_to_the_tenant() -> None:
    ttl = _TTL.replace(
        ':Person a owl:Class ; phys:collectionName "Person" ;',
        ':Person a owl:Class ; phys:collectionName "Person" ; phys:tenantField "tenant" ;',
    )
    result = translate("DESCRIBE <http://ex.org/s>", resolver=_resolver(ttl), tenant_id="A")
    # A Person document must carry tenant A; an Org document passes.
    assert ".tenant == @" in result.aql
    assert ["Person"] in result.bind_vars.values()
    assert "A" in result.bind_vars.values()
