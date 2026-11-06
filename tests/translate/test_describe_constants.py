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


def test_a_bare_describe_returns_a_row_per_triple() -> None:
    # One row per triple, so the row count and the result-row cap count
    # triples (review of #163: one row held the whole description).
    aql = translate("DESCRIBE <http://ex.org/s>", resolver=_resolver()).aql
    first, last = aql.splitlines()[0], aql.splitlines()[-1]
    triple = first.split()[1]
    assert first.startswith(f"FOR {triple} IN FLATTEN(")
    assert last == f"RETURN ([{triple}])"


def test_a_tenant_scoped_triples_table_is_refused() -> None:
    # PRD §6.5.1: a read of a table a tenant-scoped RPT class uses is refused,
    # whichever class reads it; DESCRIBE filtered it as the first class by IRI
    # said, so a tenant could read another's rows (review of #163).
    from arango_sparql.errors import CrossTenantJoinError

    ttl = _TTL.replace(
        ':Claim a owl:Class ; phys:mappingStyle "RPT" ; phys:collectionName "triples" .',
        ':Claim a owl:Class ; phys:mappingStyle "RPT" ; phys:collectionName "triples" ; phys:tenantField "tenant" .',
    )
    with pytest.raises(CrossTenantJoinError, match="tenant scoping for RPT is not supported"):
        translate("DESCRIBE <http://ex.org/s>", resolver=_resolver(ttl), tenant_id="A")


def test_strict_default_graph_reads_only_default_graph_copies() -> None:
    graph = Graph().parse(data=_TTL, format="turtle")
    resolver = SchemaResolver(
        ontology=graph, strict_subject_resolution=True, default_graph_includes_named=False
    )
    aql = translate("DESCRIBE <http://ex.org/s>", resolver=resolver).aql
    # Every document arm and the triples table.
    assert aql.count("._graph == null") == 3


def test_the_sources_keep_resolve_class_fallbacks() -> None:
    # A class with no phys:collectionName reads its local-name collection; an
    # RPT class may name its table with phys:triplesCollection (review of #163).
    ttl = (
        _PREFIXES
        + """
    :Person a owl:Class .
    :Fact a owl:Class ; phys:mappingStyle "RPT" ; phys:triplesCollection "facts" .
    """
    )
    collections, tables = _resolver(ttl).described_sources()
    assert collections == ("Person",)
    assert [t.collection for t in tables] == ["facts"]


def test_the_survey_leaves_no_advisory_and_no_cache_entry() -> None:
    ttl = _PREFIXES + ":Person a owl:Class ."
    resolver = _resolver(ttl)
    resolver.described_sources()
    assert resolver.warnings == []
    assert resolver._class_cache == {}  # noqa: SLF001 - the survey caches nothing


def test_an_unindexed_collection_is_warned_about() -> None:
    from arango_sparql.translate.mapping import MappingBundle, MappingSource

    uri = {"_uri": {"field": "_uri"}}
    bundle = MappingBundle(
        physical_mapping={
            "entities": {
                "Person": {"style": "COLLECTION", "collectionName": "Person", "properties": uri},
                "Org": {
                    "style": "COLLECTION",
                    "collectionName": "Org",
                    "properties": uri,
                    "indexes": [{"type": "persistent", "fields": ["_uri"], "sparse": True}],
                },
            }
        },
        source=MappingSource(kind="analyzer"),
    )
    result = translate(
        "DESCRIBE <urn:arango-sparql:concept#s>", resolver=SchemaResolver.from_mapping_bundle(bundle)
    )
    warned = {w["iri"] for w in result.schema_warnings if w["code"] == "W_SCHEMA_URI_INDEX_MISSING"}
    assert warned == {"Person"}
