"""Schema-fixture corpus tests (PRD §13.3).

The corpus at ``tests/schema/fixtures/*.export.json`` is the shared
test bed for every layer that reads a ``MappingBundle``: the wire-dict
normaliser, the resolver, the (future) heuristic detector, the route
layer, and the UI. By exercising the same 9 fixtures across every
slice we ensure a regression in one layer cannot hide behind green
tests in another.

This module asserts all four of the §13.3 contracts:

1. **Wire-dict round-trip** — every fixture parses through
   :func:`mapping_from_wire_dict` and re-emits an equivalent
   ``physicalMapping`` via :func:`mapping_to_wire_dict`.
2. **Resolver smoke** — every entity and relationship in the
   fixture's ``conceptualSchema`` half resolves through
   :meth:`SchemaResolver.from_mapping_bundle` without raising.
3. **Translator emits model-correct AQL per entity** — for every
   conceptual entity in every fixture, a type-pattern query
   translates to non-empty AQL that references the entity's resolved
   physical collection. This exercises PG (one collection per class),
   LPG (shared collection + ``typeField`` discriminator), RPT (rows in
   a ``triplesCollection``), the RPT/PG/LPG hybrids, multitenant
   (tenant-scoped FOR), and sharded (cross-shard ``WITH``) — i.e. the
   actual translator, not just the resolver, against all nine models.
4. **RPT AQL references the legacy columns** — for every RPT-style
   entity, the emitted AQL references the fixture's declared
   ``triplesCollection`` and the legacy Foxx column overrides
   (``subject_uri`` / ``predicate`` / ``object_uri``).

Contracts #3 and #4 were promoted from xfail stubs to hard asserts in
the multi-model cross-validation slice (the translator slices they
waited on — RPT/LPG/hybrid/sharded/multitenant emission — have all
landed).
"""

from __future__ import annotations

import json
from pathlib import Path
from urllib.parse import quote

import pytest

from arango_sparql.api import translate
from arango_sparql.errors import SchemaResolutionError, UnsupportedSparqlError
from arango_sparql.translate.mapping import (
    MappingBundle,
    mapping_from_wire_dict,
    mapping_to_wire_dict,
)
from arango_sparql.translate.resolver import SchemaResolver

FIXTURES_DIR = Path(__file__).parent / "fixtures"

# The corpus enumerated in PRD §13.3. Hard-coded rather than
# discovered-by-glob so an accidentally-deleted fixture surfaces as a
# missing-file error here rather than as silent green coverage.
FIXTURE_NAMES: tuple[str, ...] = (
    "pg",
    "lpg",
    "hybrid",
    "rpt",
    "rpt_pg_hybrid",
    "rpt_lpg_hybrid",
    "rpt_pg_lpg_hybrid",
    "multitenant",
    "sharded",
)

RPT_FIXTURE_NAMES: frozenset[str] = frozenset(
    {"rpt", "rpt_pg_hybrid", "rpt_lpg_hybrid", "rpt_pg_lpg_hybrid", "sharded"}
)


def _fixture_path(name: str) -> Path:
    return FIXTURES_DIR / f"{name}.export.json"


def _load_fixture(name: str) -> dict[str, object]:
    """Read a fixture file and return its parsed JSON dict."""

    path = _fixture_path(name)
    if not path.is_file():
        raise FileNotFoundError(
            f"Schema fixture missing: {path}. PRD §13.3 mandates all "
            f"{len(FIXTURE_NAMES)} fixtures be present."
        )
    return json.loads(path.read_text(encoding="utf-8"))


def _synthetic_iri(label: str) -> str:
    """Return the URI that :func:`_synthesize_graph_from_bundle`
    assigns to *label* (must stay in sync with the resolver)."""

    return f"urn:arango-sparql:concept#{quote(label, safe='')}"


#: Namespace for the class IRIs a fixture's data would store in ``rdf:type``.
_DATA_NS = "http://example.org/fixture#"


def _declare_data_class(resolver: SchemaResolver, label: str) -> str:
    """Declare the data's own class IRI for *label*, with the physical mapping
    the bundle gave its generated IRI, and return it.

    This is what an ontology for a triple store supplies (PRD §6.3.2): on a
    triples table a generated ``urn:arango-sparql:concept#`` IRI is refused,
    because no ``rdf:type`` row holds one. Copying the generated class's
    ``phys:*`` annotations keeps the fixture's own physical spec (table and
    column overrides) the thing under test.
    """

    from rdflib import URIRef

    generated = URIRef(_synthetic_iri(label))
    data = URIRef(_DATA_NS + quote(label, safe=""))
    for predicate, value in list(resolver.ontology.predicate_objects(generated)):
        resolver.ontology.add((data, predicate, value))
    return str(data)


# ---------------------------------------------------------------------------
# Corpus completeness
# ---------------------------------------------------------------------------


def test_all_prd_13_3_fixtures_present() -> None:
    """Each name in :data:`FIXTURE_NAMES` must correspond to an actual
    file under ``tests/schema/fixtures/``.
    """

    missing = [n for n in FIXTURE_NAMES if not _fixture_path(n).is_file()]
    assert not missing, f"Missing fixtures: {missing!r}"


def test_no_unexpected_fixtures_in_corpus() -> None:
    """Catch the inverse — fixtures on disk that aren't enumerated in
    :data:`FIXTURE_NAMES`. Prevents drift between the PRD §13.3 list
    and what's actually exercised.
    """

    on_disk = {p.stem.removesuffix(".export") for p in FIXTURES_DIR.glob("*.export.json")}
    enumerated = set(FIXTURE_NAMES)
    extras = on_disk - enumerated
    assert not extras, (
        f"Fixtures on disk but not enumerated in FIXTURE_NAMES: {extras!r}. "
        f"Add them to FIXTURE_NAMES (and PRD §13.3) or remove the files."
    )


# ---------------------------------------------------------------------------
# Round-trip (PRD §13.3 contract #1)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", FIXTURE_NAMES)
def test_fixture_parses_through_wire_dict(name: str) -> None:
    """Every fixture must parse to a :class:`MappingBundle` without
    raising :class:`MappingError`. This is the precondition for every
    other test in this module.
    """

    bundle = mapping_from_wire_dict(_load_fixture(name))
    assert isinstance(bundle, MappingBundle)
    # Empty physical mappings are allowed (the empty bundle path) but
    # not silently truncated — confirm at least one of the two halves
    # is non-empty for every named fixture.
    assert bundle.entities() or bundle.relationships(), (
        f"Fixture {name!r} produced an empty bundle; expected at least one entity or relationship."
    )


@pytest.mark.parametrize("name", FIXTURE_NAMES)
def test_fixture_round_trips_physical_mapping(name: str) -> None:
    """``mapping_from_wire_dict`` → ``mapping_to_wire_dict`` must
    preserve the physical mapping verbatim once the canonicalization
    of relationship-level ``collectionName`` → ``edgeCollectionName``
    is applied.
    """

    raw = _load_fixture(name)
    bundle = mapping_from_wire_dict(raw)
    out = mapping_to_wire_dict(bundle)

    # The output's relationship-level edge-collection field should
    # carry the canonical ``edgeCollectionName`` spelling regardless
    # of how the fixture spelled it on disk.
    rels = out["physicalMapping"].get("relationships", {})
    assert isinstance(rels, dict)
    for rtype, spec in rels.items():
        assert "collectionName" not in spec, (
            f"Relationship {rtype!r} in fixture {name!r} retained the "
            "bare 'collectionName' spelling after round-trip; expected "
            "normalisation to 'edgeCollectionName'."
        )

    # ConceptualSchema and metadata should round-trip as-is.
    assert out["conceptualSchema"] == raw.get("conceptualSchema", {})
    assert out["metadata"] == raw.get("metadata", {})


@pytest.mark.parametrize("name", FIXTURE_NAMES)
def test_fixture_round_trip_is_idempotent(name: str) -> None:
    """A bundle re-loaded from its own emitted wire-dict must equal
    the first parse. Guards against mutating-in-place bugs in the
    normaliser.
    """

    raw = _load_fixture(name)
    once = mapping_from_wire_dict(raw)
    twice = mapping_from_wire_dict(mapping_to_wire_dict(once))

    assert once.physical_mapping == twice.physical_mapping
    assert once.conceptual_schema == twice.conceptual_schema
    assert once.metadata == twice.metadata
    assert once.source == twice.source


# ---------------------------------------------------------------------------
# Resolver smoke (PRD §13.3 contract #2)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", FIXTURE_NAMES)
def test_fixture_builds_resolver(name: str) -> None:
    """Every fixture must produce a usable :class:`SchemaResolver`
    via the synthetic-IRI synthesizer (no inline OWL in the corpus).
    """

    bundle = mapping_from_wire_dict(_load_fixture(name))
    resolver = SchemaResolver.from_mapping_bundle(bundle)
    assert resolver is not None
    assert resolver.ontology is not None


@pytest.mark.parametrize("name", FIXTURE_NAMES)
def test_fixture_resolves_every_conceptual_entity(name: str) -> None:
    """For every entity name in the conceptual half of the fixture,
    the resolver must return a :class:`ResolvedClass` without raising
    ``SchemaResolutionError``.

    This is the §13.3 contract #2: "no MAPPING_NOT_FOUND for any
    conceptual entity".
    """

    raw = _load_fixture(name)
    bundle = mapping_from_wire_dict(raw)
    resolver = SchemaResolver.from_mapping_bundle(bundle)

    conceptual_entities = (raw.get("conceptualSchema") or {}).get("entities", [])
    physical_entities = bundle.entities()
    for entity in conceptual_entities:
        assert isinstance(entity, dict)
        ename = entity.get("name")
        assert isinstance(ename, str), f"entity {entity!r} has no name"
        # Skip conceptual entries that have no corresponding physical
        # mapping (the sister project's fixtures sometimes ship a
        # conceptual entity without a physical home — that is a
        # mapper-side bug for *those* layers to surface, not the
        # resolver's job). Every entity in the SPARQL corpus is
        # expected to have a physical home, so a missing entry here
        # is a real defect.
        assert ename in physical_entities, (
            f"Fixture {name!r}: conceptual entity {ename!r} has no entry in physicalMapping.entities"
        )
        iri = _synthetic_iri(ename)
        resolved = resolver.resolve_class(iri)
        assert resolved.iri == iri
        # For RPT entities the collection falls back to the triples
        # table; for everything else it is the explicit collectionName.
        assert resolved.collection, (
            f"Resolver returned empty collection for entity {ename!r} in fixture {name!r}"
        )


@pytest.mark.parametrize("name", FIXTURE_NAMES)
def test_fixture_resolves_every_conceptual_relationship(name: str) -> None:
    """For every relationship type the conceptual half declares, the
    resolver must return a :class:`ResolvedProperty` flagged
    ``is_object_property=True`` without raising.
    """

    raw = _load_fixture(name)
    bundle = mapping_from_wire_dict(raw)
    resolver = SchemaResolver.from_mapping_bundle(bundle)

    conceptual_rels = (raw.get("conceptualSchema") or {}).get("relationships", [])
    physical_rels = bundle.relationships()
    for rel in conceptual_rels:
        assert isinstance(rel, dict)
        rtype = rel.get("type")
        assert isinstance(rtype, str), f"relationship {rel!r} has no type"
        assert rtype in physical_rels, (
            f"Fixture {name!r}: conceptual relationship {rtype!r} has no "
            f"entry in physicalMapping.relationships"
        )
        iri = _synthetic_iri(rtype)
        resolved = resolver.resolve_property(iri)
        assert resolved.iri == iri
        assert resolved.is_object_property is True


def test_unmapped_iri_raises_or_degrades_gracefully() -> None:
    """The negative case — an IRI that is in *no* fixture's conceptual
    half must surface as a clear failure rather than silently emit
    bogus AQL.
    """

    bundle = mapping_from_wire_dict(_load_fixture("pg"))
    resolver = SchemaResolver.from_mapping_bundle(bundle)
    with pytest.raises(SchemaResolutionError):
        resolver.resolve_class("urn:arango-sparql:concept#NonExistentClass")


# ---------------------------------------------------------------------------
# Fixture-specific shape assertions
# ---------------------------------------------------------------------------


def test_pg_fixture_uses_collection_style_throughout() -> None:
    """``pg.export.json`` should declare *only* ``COLLECTION`` entities
    and ``DEDICATED_COLLECTION`` relationships per PRD §13.3.
    """

    bundle = mapping_from_wire_dict(_load_fixture("pg"))
    for label, spec in bundle.entities().items():
        assert spec.get("style") == "COLLECTION", (
            f"pg fixture entity {label!r} must be COLLECTION; got {spec.get('style')!r}"
        )
    for rtype, spec in bundle.relationships().items():
        assert spec.get("style") == "DEDICATED_COLLECTION", (
            f"pg fixture relationship {rtype!r} must be DEDICATED_COLLECTION; got {spec.get('style')!r}"
        )


def test_lpg_fixture_uses_label_style_throughout() -> None:
    """``lpg.export.json`` should declare *only* ``LABEL`` entities and
    ``GENERIC_WITH_TYPE`` relationships per PRD §13.3.
    """

    bundle = mapping_from_wire_dict(_load_fixture("lpg"))
    for label, spec in bundle.entities().items():
        assert spec.get("style") == "LABEL"
        assert spec.get("typeField"), f"LPG entity {label!r} missing typeField"
        assert spec.get("typeValue"), f"LPG entity {label!r} missing typeValue"
    for rtype, spec in bundle.relationships().items():
        assert spec.get("style") == "GENERIC_WITH_TYPE"
        assert spec.get("typeField"), f"LPG rel {rtype!r} missing typeField"
        assert spec.get("typeValue"), f"LPG rel {rtype!r} missing typeValue"
        # Sister-project LPG fixtures spell the edge collection as bare
        # ``collectionName``; our normaliser must rewrite it.
        assert spec.get("edgeCollectionName"), (
            f"LPG rel {rtype!r} missing edgeCollectionName after "
            "normalisation (sister-project collectionName alias)"
        )


def test_hybrid_fixture_mixes_styles() -> None:
    """The carry-over ``hybrid.export.json`` must contain at least one
    of each entity style and at least one of each relationship style
    it claims to cover.
    """

    bundle = mapping_from_wire_dict(_load_fixture("hybrid"))
    entity_styles = {spec.get("style") for spec in bundle.entities().values()}
    rel_styles = {spec.get("style") for spec in bundle.relationships().values()}
    assert {"COLLECTION", "LABEL"} <= entity_styles, (
        f"hybrid fixture missing one of {{COLLECTION, LABEL}}: {entity_styles}"
    )
    assert rel_styles, "hybrid fixture has no relationships"


@pytest.mark.parametrize(
    "name",
    ["rpt", "rpt_pg_hybrid", "rpt_lpg_hybrid", "rpt_pg_lpg_hybrid"],
)
def test_rpt_fixtures_preserve_legacy_column_overrides(name: str) -> None:
    """RPT fixtures must round-trip the legacy Foxx column names
    (``subject_uri`` / ``predicate`` / ``object_uri`` / ``object_value``).
    This is precisely the §13.3 contract #4 precondition: without
    column preservation no downstream layer can emit RPT-correct AQL.
    """

    bundle = mapping_from_wire_dict(_load_fixture(name))
    rpt_entities = [(label, spec) for label, spec in bundle.entities().items() if spec.get("style") == "RPT"]
    assert rpt_entities, f"Fixture {name!r} declares RPT in PRD §13.3 but has no RPT entity"
    for label, spec in rpt_entities:
        assert spec.get("triplesCollection"), f"RPT entity {label!r} missing triplesCollection"
        assert spec.get("subjectColumn") == "subject_uri", (
            f"RPT entity {label!r} subjectColumn should match legacy Foxx 'subject_uri'"
        )
        assert spec.get("predicateColumn") == "predicate"
        assert spec.get("objectUriColumn") == "object_uri"
        assert spec.get("objectValueColumn") == "object_value"


def test_rpt_pg_lpg_hybrid_contains_all_three_styles() -> None:
    """The full-hybrid fixture is the smoke test for PRD §3.4 (mixed-
    model BGP). Loss of any of the three styles would silently regress
    mixed-model coverage.
    """

    bundle = mapping_from_wire_dict(_load_fixture("rpt_pg_lpg_hybrid"))
    styles = {spec.get("style") for spec in bundle.entities().values()}
    assert {"COLLECTION", "LABEL", "RPT"} <= styles, (
        f"rpt_pg_lpg_hybrid fixture missing one of {{COLLECTION, LABEL, RPT}}: {styles}"
    )


def test_multitenant_fixture_carries_tenant_metadata() -> None:
    """The multitenant fixture must surface both blocks that PRD §6.5
    describes: per-entity ``tenantField``/``tenantEntity`` in
    ``physicalMapping`` and the top-level ``metadata.multitenancy``
    strategy descriptor.
    """

    bundle = mapping_from_wire_dict(_load_fixture("multitenant"))
    mt = bundle.metadata.get("multitenancy")
    assert isinstance(mt, dict)
    assert mt.get("strategy") in {"field", "database", "none"}
    assert mt.get("tenantRootEntity")

    scoped = [label for label, spec in bundle.entities().items() if spec.get("tenantField")]
    assert scoped, "multitenant fixture has no entity with phys:tenantField"
    for label in scoped:
        spec = bundle.entities()[label]
        assert spec.get("tenantEntity"), f"Entity {label!r} declares tenantField but missing tenantEntity"


def test_multitenant_fixture_resolver_carries_tenant_annotations() -> None:
    """The synthesizer must project ``tenantField`` and ``tenantEntity``
    from the bundle into the resolver's graph as ``phys:*`` literals,
    so downstream slices (translator's tenant-filter emit) can read
    them without re-parsing the bundle.
    """

    from rdflib import Literal, URIRef

    from arango_sparql.translate.resolver import _SYNTHETIC_PHYS_NS

    bundle = mapping_from_wire_dict(_load_fixture("multitenant"))
    resolver = SchemaResolver.from_mapping_bundle(bundle)
    person_iri = URIRef(_synthetic_iri("Person"))
    # The synthesizer emits under the canonical physical namespace (the one
    # the analyzers use); track it rather than hard-coding a spelling.
    phys = URIRef(str(_SYNTHETIC_PHYS_NS))
    tenant_field_pred = URIRef(str(phys) + "tenantField")
    tenant_entity_pred = URIRef(str(phys) + "tenantEntity")

    assert (
        person_iri,
        tenant_field_pred,
        Literal("org_id"),
    ) in resolver.ontology
    assert (
        person_iri,
        tenant_entity_pred,
        Literal("Org"),
    ) in resolver.ontology


def test_sharded_fixture_preserves_shard_families() -> None:
    """``physicalMapping.shardFamilies`` must round-trip verbatim —
    the translator's cross-shard ``WITH`` emit logic (PRD §6.5.3)
    reads this field directly.
    """

    raw = _load_fixture("sharded")
    bundle = mapping_from_wire_dict(raw)
    families = bundle.physical_mapping.get("shardFamilies")
    assert families == raw["physicalMapping"]["shardFamilies"]
    # And via round-trip
    re_emitted = mapping_to_wire_dict(bundle)
    assert re_emitted["physicalMapping"]["shardFamilies"] == raw["physicalMapping"]["shardFamilies"]


# ---------------------------------------------------------------------------
# Translator emission per model (PRD §13.3 contracts #3 and #4)
# ---------------------------------------------------------------------------


def _tenant_id_for(bundle: MappingBundle) -> str | None:
    """Return a tenant id to thread through ``translate`` when the
    fixture declares field-strategy multitenancy, else ``None``.

    Tenant-scoped classes raise ``CrossTenantJoinError`` if no tenant
    context is supplied (PRD §6.5), so the multitenant fixture's
    entities can only be translated with a tenant in scope. The actual
    value is irrelevant to the structural assertion — it only has to be
    present — so we use a stable sentinel.
    """

    mt = bundle.metadata.get("multitenancy")
    if isinstance(mt, dict) and mt.get("strategy") == "field":
        return "tenant-001"
    return None


@pytest.mark.parametrize("name", FIXTURE_NAMES)
def test_translator_emits_aql_per_entity_in_every_fixture(name: str) -> None:
    """PRD §13.3 contract #3 — every conceptual entity in every fixture
    translates to non-empty AQL that references its resolved physical
    collection.

    A bare type-pattern (``SELECT ?s WHERE { ?s a <entity-iri> }``) is
    the minimal query that forces the translator to open the entity's
    physical home, so it cleanly distinguishes the models: PG opens
    ``@@<collectionName>``, LPG opens the shared collection plus a
    ``typeField`` discriminator, RPT opens the ``triplesCollection``,
    sharded opens a cross-shard ``WITH`` list, and multitenant adds a
    tenant FILTER. We assert the resolved collection name appears in the
    emitted AQL — which holds across all of those shapes because the
    shard-suffixed and discriminated forms still contain the base
    collection token as a substring.

    An RPT entity's generated IRI is refused (no ``rdf:type`` row stores
    one, so it could only match nothing; PRD §6.3.2); its type pattern is
    emitted for the data's own class IRI, declared with the same mapping.
    """

    raw = _load_fixture(name)
    bundle = mapping_from_wire_dict(raw)
    resolver = SchemaResolver.from_mapping_bundle(bundle)
    tenant_id = _tenant_id_for(bundle)

    entities = (raw.get("conceptualSchema") or {}).get("entities", [])
    assert entities, f"Fixture {name!r} has no conceptual entities to translate"

    for entity in entities:
        ename = entity["name"]
        iri = _synthetic_iri(ename)
        resolved = resolver.resolve_class(iri)
        if resolved.style == "RPT":
            with pytest.raises(SchemaResolutionError, match="generated from the mapping"):
                translate(f"SELECT ?s WHERE {{ ?s a <{iri}> }}", resolver=resolver, tenant_id=tenant_id)
            iri = _declare_data_class(resolver, ename)
        sparql = f"SELECT ?s WHERE {{ ?s a <{iri}> }}"
        result = translate(sparql, resolver=resolver, tenant_id=tenant_id)

        assert result.aql, f"Fixture {name!r} entity {ename!r}: translator returned empty AQL"
        # The emission must open the entity's physical home. The first
        # clause is FOR (single/sharded-via-subquery) or WITH (sharded
        # cross-collection) — never an empty or RETURN-only body.
        first_clause = result.aql.lstrip().split(None, 1)[0]
        assert first_clause in {"FOR", "WITH"}, (
            f"Fixture {name!r} entity {ename!r}: AQL does not open a "
            f"collection (first clause {first_clause!r}):\n{result.aql}"
        )
        assert resolved.collection in result.aql, (
            f"Fixture {name!r} entity {ename!r}: resolved collection "
            f"{resolved.collection!r} not referenced in emitted AQL:\n{result.aql}"
        )


@pytest.mark.parametrize("name", sorted(RPT_FIXTURE_NAMES))
def test_rpt_translator_references_legacy_columns_in_emitted_aql(name: str) -> None:
    """PRD §13.3 contract #4 — RPT-style entities emit AQL that
    references the fixture's ``triplesCollection`` and the legacy Foxx
    column overrides.

    A type-pattern over an RPT entity compiles to a triples-table scan
    of the shape ``FOR t IN @@triples FILTER t.<predicateColumn> == …
    FILTER t.<objectUriColumn> == … RETURN { s: t.<subjectColumn> }``,
    so all three of subject/predicate/object-uri columns appear in the
    one query. We read the expected names from the fixture's physical
    spec (rather than hard-coding ``subject_uri`` etc.) so a fixture
    that legitimately overrides a column name is validated against its
    own declaration.
    """

    raw = _load_fixture(name)
    bundle = mapping_from_wire_dict(raw)
    resolver = SchemaResolver.from_mapping_bundle(bundle)
    tenant_id = _tenant_id_for(bundle)

    rpt_entities = [(label, spec) for label, spec in bundle.entities().items() if spec.get("style") == "RPT"]
    assert rpt_entities, f"Fixture {name!r} is in RPT_FIXTURE_NAMES but declares no RPT entity"

    for label, spec in rpt_entities:
        iri = _declare_data_class(resolver, label)
        result = translate(
            f"SELECT ?s WHERE {{ ?s a <{iri}> }}",
            resolver=resolver,
            tenant_id=tenant_id,
        )
        triples_collection = spec.get("triplesCollection") or "_triples"
        subject_col = spec.get("subjectColumn") or "subject_uri"
        predicate_col = spec.get("predicateColumn") or "predicate"
        object_uri_col = spec.get("objectUriColumn") or "object_uri"

        # ``triplesCollection`` is a substring of the sharded forms
        # (``_triples`` ⊂ ``_triples_us``), so substring containment is
        # the right check across both plain and sharded RPT.
        assert triples_collection in result.aql, (
            f"Fixture {name!r} RPT entity {label!r}: triples collection "
            f"{triples_collection!r} not referenced:\n{result.aql}"
        )
        for column in (subject_col, predicate_col, object_uri_col):
            assert column in result.aql, (
                f"Fixture {name!r} RPT entity {label!r}: legacy column "
                f"{column!r} not referenced in emitted AQL:\n{result.aql}"
            )


@pytest.mark.parametrize("name", ["rpt_lpg_hybrid", "rpt_pg_lpg_hybrid"])
def test_rpt_edge_from_a_non_rpt_subject_is_refused_not_mistranslated(name: str) -> None:
    """``?s a :Doc ; :TAGGED_WITH ?o`` with ``Doc`` in LPG and ``TAGGED_WITH``
    in the triples store used to emit ``OUTBOUND doc @@_triples`` — a
    traversal over a document collection, which fails at execution with
    ERR 1218. Found by the schema_live tier; translation-only goldens could
    not see it. Until the PG/LPG -> triples join lands (PRD §3.4 / §6.6),
    translation must refuse rather than emit AQL that cannot run.
    """

    resolver = SchemaResolver.from_mapping_bundle(mapping_from_wire_dict(_load_fixture(name)))
    query = f"SELECT ?s ?o WHERE {{ ?s a <{_synthetic_iri('Doc')}> ; <{_synthetic_iri('TAGGED_WITH')}> ?o }}"

    with pytest.raises(UnsupportedSparqlError, match="RPT_EDGE"):
        translate(query, resolver=resolver)
