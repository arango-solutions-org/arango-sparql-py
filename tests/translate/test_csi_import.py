"""Tests for the CSI v1 -> MappingBundle adapter (translate/csi.py).

Covers the A1->A3 seam: a forward ``CSI v1`` document (the exact shape r2g's
emitter produces) becomes a :class:`MappingBundle` that the SPARQL->AQL
:class:`SchemaResolver` can resolve classes and object-properties from.
"""

from __future__ import annotations

import pytest
from rdflib import URIRef

from arango_sparql.translate.csi import CSI_SOURCE_KIND, mapping_bundle_from_csi
from arango_sparql.translate.mapping import MappingBundle, MappingError
from arango_sparql.translate.resolver import (
    _SYNTHETIC_CONCEPT_NS,
    SchemaResolver,
)


def _forward_csi() -> dict:
    """A CSI v1 document shaped exactly like r2g's forward emitter output."""
    return {
        "csiVersion": "1",
        "conceptualModel": {
            "entities": [
                {"name": "User", "labels": ["User"], "properties": [{"name": "name"}]},
                {"name": "Order", "labels": ["Order"], "properties": []},
            ],
            "relationships": [
                {"type": "placed_by", "fromEntity": "Order", "toEntity": "User"},
            ],
        },
        "arangoPhysicalMapping": {
            "entities": {
                "User": {"style": "COLLECTION", "collectionName": "User"},
                "Order": {"style": "COLLECTION", "collectionName": "Order"},
            },
            "relationships": {
                "placed_by": {
                    "style": "DEDICATED_COLLECTION",
                    "edgeCollectionName": "placed_by",
                },
            },
        },
        "provenance": {
            "producer": "r2g",
            "producerVersion": "0.2.0",
            "direction": "forward",
            "source": {"kind": "postgresql", "ref": "shop", "fingerprint": None},
            "generatedAt": None,
        },
    }


def test_none_yields_empty_bundle():
    bundle = mapping_bundle_from_csi(None)
    assert isinstance(bundle, MappingBundle)
    assert bundle.entities() == {}
    assert bundle.relationships() == {}


def test_physical_entities_pass_through():
    bundle = mapping_bundle_from_csi(_forward_csi())
    entities = bundle.entities()
    assert set(entities) == {"User", "Order"}
    assert entities["User"]["collectionName"] == "User"
    assert entities["User"]["style"] == "COLLECTION"


def test_relationship_gets_endpoints_from_conceptual_model():
    bundle = mapping_bundle_from_csi(_forward_csi())
    rel = bundle.relationships()["placed_by"]
    assert rel["edgeCollectionName"] == "placed_by"
    assert rel["style"] == "DEDICATED_COLLECTION"
    # fromEntity / toEntity lifted from conceptualModel.relationships.
    assert rel["fromEntity"] == "Order"
    assert rel["toEntity"] == "User"


def test_conceptual_schema_and_provenance_preserved():
    csi = _forward_csi()
    bundle = mapping_bundle_from_csi(csi)
    assert bundle.conceptual_schema == csi["conceptualModel"]
    assert bundle.metadata["csiProvenance"] == csi["provenance"]


def test_source_kind_is_imported_csi_and_trusted():
    bundle = mapping_bundle_from_csi(_forward_csi())
    assert bundle.source is not None
    assert bundle.source.kind == CSI_SOURCE_KIND == "imported_csi"
    assert "r2g" in (bundle.source.notes or "")


def test_resolver_round_trip_class_and_property():
    """A1->A3->resolver: the bundle drives real class/edge resolution."""
    bundle = mapping_bundle_from_csi(_forward_csi())
    resolver = SchemaResolver.from_mapping_bundle(bundle)

    user_iri = URIRef(str(_SYNTHETIC_CONCEPT_NS) + "User")
    assert resolver.resolve_class(user_iri).collection == "User"

    placed_by_iri = URIRef(str(_SYNTHETIC_CONCEPT_NS) + "placed_by")
    prop = resolver.resolve_property(placed_by_iri)
    assert prop.is_object_property is True
    assert prop.edge_collection == "placed_by"


def test_generic_with_type_relationship_preserved():
    csi = _forward_csi()
    csi["arangoPhysicalMapping"]["relationships"]["placed_by"] = {
        "style": "GENERIC_WITH_TYPE",
        "edgeCollectionName": "edges",
        "typeField": "_type",
        "typeValue": "placed_by",
    }
    rel = mapping_bundle_from_csi(csi).relationships()["placed_by"]
    assert rel["typeField"] == "_type"
    assert rel["typeValue"] == "placed_by"
    assert rel["edgeCollectionName"] == "edges"


def test_relationship_without_conceptual_entry_omits_endpoints():
    csi = _forward_csi()
    csi["conceptualModel"]["relationships"] = []  # no endpoint info
    rel = mapping_bundle_from_csi(csi).relationships()["placed_by"]
    assert "fromEntity" not in rel
    assert "toEntity" not in rel
    assert rel["edgeCollectionName"] == "placed_by"


@pytest.mark.parametrize("bad_version", ["2", "1.3", 1, 1.1, 1.2, None, ""])
def test_bad_version_rejected(bad_version):
    csi = _forward_csi()
    csi["csiVersion"] = bad_version
    with pytest.raises(MappingError):
        mapping_bundle_from_csi(csi)


# ---------------------------------------------------------------------------
# Issue #72: csiVersion "1.1" carries ``path``; property mappings are strict
# ---------------------------------------------------------------------------


def _with_user_properties(properties: object, version: str = "1") -> dict:
    csi = _forward_csi()
    csi["csiVersion"] = version
    csi["arangoPhysicalMapping"]["entities"]["User"]["properties"] = properties
    return csi


def test_version_1_1_is_accepted_and_carries_a_nested_path():
    csi = _with_user_properties({"city": {"path": ["address", "city"]}}, version="1.1")
    resolver = SchemaResolver.from_mapping_bundle(mapping_bundle_from_csi(csi))
    assert resolver.resolve_property(URIRef(str(_SYNTHETIC_CONCEPT_NS) + "city")).nested_path == (
        "address",
        "city",
    )


def test_version_1_1_without_path_is_accepted():
    # A "1.1" reader must read a "1.1" document that happens to use no 1.1 feature.
    csi = _with_user_properties({"name": {"field": "name"}}, version="1.1")
    assert mapping_bundle_from_csi(csi).entities()["User"]["properties"] == {"name": {"field": "name"}}


def test_path_in_a_version_1_document_is_refused():
    # A version-1 reader would ignore ``path`` and read the wrong field, so a
    # producer must say "1.1"; a document that says "1" and uses it is wrong.
    csi = _with_user_properties({"city": {"path": ["address", "city"]}}, version="1")
    with pytest.raises(MappingError, match="needs csiVersion '1.1'"):
        mapping_bundle_from_csi(csi)


@pytest.mark.parametrize("version", ["1", "1.1"])
def test_known_descriptive_keys_and_x_extensions_are_accepted(version):
    spec = {
        "field": "name",
        "type": "string",
        "indexed": True,
        "unique": False,
        "required": True,
        "role": "label",
        "sentinelValues": ["NULL"],
        "numericLike": False,
        "sampleValues": ["Ana"],
        "x-steward": "crm-team",
    }
    bundle = mapping_bundle_from_csi(_with_user_properties({"name": spec}, version=version))
    assert bundle.entities()["User"]["properties"]["name"] == spec


@pytest.mark.parametrize("version", ["1", "1.1"])
def test_unknown_property_mapping_key_is_refused(version):
    # A misspelled or future location key must not be ignored: ignoring it is
    # exactly how a reader answers from the wrong field without a word.
    csi = _with_user_properties({"city": {"field": "city", "pathh": ["address", "city"]}}, version=version)
    with pytest.raises(MappingError, match=r"unknown key\(s\) \['pathh'\]"):
        mapping_bundle_from_csi(csi)


@pytest.mark.parametrize("version", ["1", "1.1"])
@pytest.mark.parametrize("spec", [{"field": None}, {"path": None, "field": "city"}, {"path": None}])
def test_explicit_null_location_is_refused(version, spec):
    # The canonical CSI schema (arango-schema-analyzer) refuses an explicit
    # null; the reader must not quietly read it as "absent" instead.
    with pytest.raises(MappingError, match="to null"):
        mapping_bundle_from_csi(_with_user_properties({"city": spec}, version=version))


@pytest.mark.parametrize("bad", ["address.city", ["address", "city"], None])
def test_property_mapping_that_is_not_an_object_is_refused(bad):
    with pytest.raises(MappingError, match="must be an object"):
        mapping_bundle_from_csi(_with_user_properties({"city": bad}))


def test_properties_block_that_is_not_an_object_is_refused():
    with pytest.raises(MappingError, match="must be an object"):
        mapping_bundle_from_csi(_with_user_properties([{"field": "city"}]))


def test_unknown_keys_elsewhere_keep_the_additive_rule():
    # Only a property mapping is strict; entity and provenance extras ride along.
    csi = _forward_csi()
    csi["arangoPhysicalMapping"]["entities"]["User"]["lastValidatedAt"] = "2026-10-26T00:00:00Z"
    csi["provenance"]["somethingNew"] = {"a": 1}
    assert mapping_bundle_from_csi(csi).entities()["User"]["lastValidatedAt"] == "2026-10-26T00:00:00Z"


def test_non_dict_rejected():
    with pytest.raises(MappingError):
        mapping_bundle_from_csi([1, 2, 3])  # type: ignore[arg-type]


def test_empty_physical_blocks_ok():
    csi = _forward_csi()
    csi["arangoPhysicalMapping"] = {"entities": {}, "relationships": {}}
    csi["conceptualModel"] = {"entities": [], "relationships": []}
    bundle = mapping_bundle_from_csi(csi)
    assert bundle.entities() == {}
    assert bundle.relationships() == {}
    assert bundle.source is not None and bundle.source.kind == "imported_csi"


def _owl_named_csi() -> dict:
    """A CC-12-conforming CSI: OWL conceptual names + per-property fields.

    The shape ``to_csi(..., owl_naming=True)`` (arango-schema-analyzer) and
    r2g's convention-conforming forward emitter produce: conceptual property
    ``accountId`` stored as document field ``account_id``.
    """
    return {
        "csiVersion": "1",
        "conceptualModel": {
            "entities": [
                {
                    "name": "Document",
                    "labels": ["Document"],
                    "properties": [{"name": "accountId"}, {"name": "citableUrl"}],
                }
            ],
            "relationships": [],
        },
        "arangoPhysicalMapping": {
            "entities": {
                "Document": {
                    "style": "COLLECTION",
                    "collectionName": "documents",
                    "properties": {
                        "accountId": {"field": "account_id", "indexed": True},
                        "citableUrl": {"field": "citable_url"},
                    },
                }
            },
            "relationships": {},
        },
        "provenance": {
            "producer": "arango-schema-analyzer",
            "direction": "reverse",
            "source": {"kind": "arango", "ref": "cmf", "fingerprint": None},
        },
    }


def test_property_field_mapping_resolves_conceptual_to_stored_name():
    """CC-12: ``c:accountId`` resolves to the stored ``account_id`` attribute."""
    bundle = mapping_bundle_from_csi(_owl_named_csi())
    resolver = SchemaResolver.from_mapping_bundle(bundle)

    resolved = resolver.resolve_property(_SYNTHETIC_CONCEPT_NS["accountId"])
    assert resolved.attribute == "account_id"
    assert not resolved.is_object_property

    resolved_url = resolver.resolve_property(_SYNTHETIC_CONCEPT_NS["citableUrl"])
    assert resolved_url.attribute == "citable_url"

    # The class still resolves to the physical collection.
    cls = resolver.resolve_class(_SYNTHETIC_CONCEPT_NS["Document"])
    assert cls.collection == "documents"


def test_property_field_mapping_reaches_the_generated_aql():
    """The generated AQL reads the stored field, not the conceptual name."""
    from arango_sparql.api import translate

    bundle = mapping_bundle_from_csi(_owl_named_csi())
    resolver = SchemaResolver.from_mapping_bundle(bundle)
    result = translate(
        "PREFIX c: <urn:arango-sparql:concept#> "
        "SELECT ?url WHERE { ?d a c:Document ; c:accountId ?acct ; c:citableUrl ?url }",
        resolver=resolver,
    )
    assert "account_id" in result.aql
    assert "citable_url" in result.aql
    assert "accountId" not in result.aql
    assert "citableUrl" not in result.aql
