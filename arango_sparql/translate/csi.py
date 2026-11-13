"""CSI v1 (Conceptual Schema Interchange) → :class:`MappingBundle` adapter.

``CSI v1`` is the cross-tool interchange for a conceptual model plus its
ArangoDB physical mapping. A *forward* producer (e.g. r2g) emits it from a
relational→graph migration config; the *reverse* producer is
``arango-schema-analyzer``. Both write the same three-block contract:
``{conceptualModel, arangoPhysicalMapping, provenance}``.

This adapter lets the SPARQL→AQL translator consume a CSI document directly:
it maps the CSI physical block onto the bundle's ``physicalMapping`` (which is
already almost the same shape — collection/edge names + optional type
discriminators) and enriches each relationship spec with the ``fromEntity`` /
``toEntity`` endpoints declared in the CSI conceptual model. The CSI conceptual
model becomes the bundle's ``conceptualSchema`` (the human/LLM view), and the
CSI provenance is preserved verbatim under ``metadata["csiProvenance"]``.

See contextual-data-fabric ADR-0001 / M5 implementation-plan WP-A3, and the
forward producer in r2g (``src/r2g/csi.py``).

Versions: ``"1"``, ``"1.1"`` and ``"1.2"``. 1.1 adds ``path`` to a property
mapping (a value inside a nested object, issue #72); 1.2 adds ``multiValued``
(the attribute holds a list, one value per element; CDF #220). A producer
writes each only for a document that uses it. Property mappings are strict in
every version: a key this reader does not know is refused, since it could
change where or how a value is read.
"""

from __future__ import annotations

from typing import Any

from .mapping import MappingBundle, MappingError, mapping_from_wire_dict

# The source.kind we stamp on bundles imported from a CSI document — trusted
# like ``imported_owl`` (deterministic, schema-derived), never refused by the
# heuristic guard.
CSI_SOURCE_KIND = "imported_csi"

# Relationship-spec fields we lift from the CSI *conceptual* model onto the
# *physical* relationship spec so the translator knows each edge's endpoints.
_ENDPOINT_FIELDS = ("fromEntity", "toEntity")

# CSI versions this reader understands. "1.1" adds ``path`` to a property
# mapping (issue #72); a producer writes it only for a document that uses
# ``path``, so a reader that knows only "1" refuses just those documents
# instead of silently reading every nested property as a missing field.
CSI_VERSIONS = ("1", "1.1", "1.2")

# The versions whose property mappings may carry ``multiValued``. A reader of
# "1" or "1.1" would ignore the key and bind a whole list as one value, so a
# document that uses it declares "1.2" (arango-schema-analyzer writes it only
# then), and a "1" or "1.1" document that carries it is refused.
_MULTI_VALUED_VERSIONS = ("1.2",)

# What a CSI property mapping (``arangoPhysicalMapping.entities.<E>.properties.
# <p>``) may hold. The rest of a CSI document keeps the schema's additive rule
# (unknown fields ride along and are ignored), but a property mapping says
# where a value is read from, so an unknown key there could change the answer
# and is refused. Location keys first, then the descriptive extras producers
# write today (arango-schema-analyzer: indexed, unique; arango-cypher-py's
# acquisition: type, role, required, sentinelValues, numericLike,
# sampleValues), and ``multiValued``, which changes how a value is read (one
# per list element) and so is checked against the version. A key starting with
# ``x-`` is an explicitly ignorable extra.
_PROPERTY_LOCATION_KEYS = ("field", "path")
_PROPERTY_DESCRIPTIVE_KEYS = (
    "type",
    "indexed",
    "unique",
    "required",
    "role",
    "sentinelValues",
    "numericLike",
    "sampleValues",
    "multiValued",
)
_PROPERTY_EXTENSION_PREFIX = "x-"


def _require_dict(value: Any, what: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise MappingError(f"CSI {what} must be an object, got {type(value).__name__!r}")
    return value


def _check_property_mappings(entity: str, spec: dict[str, Any], version: str) -> None:
    """Refuse a property mapping this reader could misread (issue #72).

    Every key must be a location key, a known descriptive extra, or an
    ``x-`` extension; ``path`` needs ``csiVersion`` "1.1", and ``multiValued``
    (a boolean) needs "1.2".
    """
    properties = spec.get("properties")
    if properties is None:
        return
    where = f"arangoPhysicalMapping.entities[{entity!r}].properties"
    for prop_name, prop_spec in _require_dict(properties, where).items():
        prop_where = f"{where}[{prop_name!r}]"
        _require_dict(prop_spec, prop_where)
        unknown = sorted(
            str(key)
            for key in prop_spec
            if key not in _PROPERTY_LOCATION_KEYS
            and key not in _PROPERTY_DESCRIPTIVE_KEYS
            and not str(key).startswith(_PROPERTY_EXTENSION_PREFIX)
        )
        if unknown:
            raise MappingError(
                f"CSI {prop_where} has unknown key(s) {unknown}: a property mapping may hold "
                f"{list(_PROPERTY_LOCATION_KEYS + _PROPERTY_DESCRIPTIVE_KEYS)}, or an ignorable "
                f"extra whose name starts with {_PROPERTY_EXTENSION_PREFIX!r}"
            )
        # Absent means "the property name is the field"; an explicit null is
        # refused, as the canonical CSI schema refuses it, rather than read
        # as absent.
        nulls = [key for key in _PROPERTY_LOCATION_KEYS if key in prop_spec and prop_spec[key] is None]
        if nulls:
            raise MappingError(f"CSI {prop_where} sets {nulls} to null; leave the key out instead")
        if "path" in prop_spec and version == "1":
            raise MappingError(
                f"CSI {prop_where} uses 'path', which needs csiVersion '1.1' (the document says '1'); "
                f"a reader of version 1 would ignore it and read the wrong field"
            )
        if "multiValued" in prop_spec:
            if version not in _MULTI_VALUED_VERSIONS:
                raise MappingError(
                    f"CSI {prop_where} uses 'multiValued', which needs csiVersion '1.2' (the document "
                    f"says {version!r}); a reader of an earlier version would read the whole list as one value"
                )
            if not isinstance(prop_spec["multiValued"], bool):
                raise MappingError(
                    f"CSI {prop_where} sets 'multiValued' to {prop_spec['multiValued']!r}; expected true or false"
                )


def mapping_bundle_from_csi(csi: dict[str, Any] | None) -> MappingBundle:
    """Build a :class:`MappingBundle` from a ``CSI v1`` document.

    Args:
        csi: a parsed ``CSI v1`` document (``{csiVersion, conceptualModel,
            arangoPhysicalMapping, provenance}``). ``None`` yields an empty
            bundle (mirrors :func:`mapping_from_wire_dict`).

    Returns:
        A :class:`MappingBundle` whose ``physicalMapping`` drives the AQL
        translator, ``conceptualSchema`` carries the CSI conceptual model, and
        ``source.kind == "imported_csi"``.

    Raises:
        MappingError: when the document is not a recognisable ``CSI v1`` shape.
    """
    if csi is None:
        return MappingBundle()
    csi = _require_dict(csi, "document")

    version = csi.get("csiVersion")
    if version not in CSI_VERSIONS:
        raise MappingError(f"unsupported csiVersion {version!r} (expected one of {list(CSI_VERSIONS)})")

    conceptual = _require_dict(csi.get("conceptualModel") or {}, "conceptualModel")
    physical = _require_dict(csi.get("arangoPhysicalMapping") or {}, "arangoPhysicalMapping")

    csi_entities = _require_dict(physical.get("entities") or {}, "arangoPhysicalMapping.entities")
    csi_relationships = _require_dict(
        physical.get("relationships") or {}, "arangoPhysicalMapping.relationships"
    )

    # Endpoint lookup from the conceptual model: {relationshipType: {from,to}}.
    endpoints: dict[str, dict[str, Any]] = {}
    for rel in conceptual.get("relationships") or []:
        if not isinstance(rel, dict):
            continue
        rtype = rel.get("type")
        if rtype is None:
            continue
        # CSI conceptual endpoints (``fromEntity``/``toEntity``) share their
        # spelling with the bundle's canonical relationship-spec fields, so
        # they copy across by name.
        endpoints[rtype] = {
            field_name: rel[field_name] for field_name in _ENDPOINT_FIELDS if rel.get(field_name) is not None
        }

    # Physical entities pass through verbatim — the CSI physical entity spec
    # (style/collectionName/typeField/typeValue) is already bundle-shaped.
    entities_out: dict[str, Any] = {
        name: _require_dict(spec, f"arangoPhysicalMapping.entities[{name!r}]").copy()
        for name, spec in csi_entities.items()
    }
    for name, spec in entities_out.items():
        _check_property_mappings(str(name), spec, version)

    # Physical relationships pass through + get endpoints grafted on.
    relationships_out: dict[str, Any] = {}
    for rtype, spec in csi_relationships.items():
        merged = _require_dict(spec, f"arangoPhysicalMapping.relationships[{rtype!r}]").copy()
        for field_name, value in endpoints.get(rtype, {}).items():
            merged.setdefault(field_name, value)
        relationships_out[rtype] = merged

    provenance = csi.get("provenance")
    producer = None
    if isinstance(provenance, dict):
        producer = provenance.get("producer")
    notes = f"forward CSI v1 from {producer}" if producer else "imported CSI v1"

    wire: dict[str, Any] = {
        "conceptualSchema": conceptual,
        "physicalMapping": {
            "entities": entities_out,
            "relationships": relationships_out,
        },
        "metadata": {"csiProvenance": provenance} if provenance is not None else {},
        "source": {"kind": CSI_SOURCE_KIND, "notes": notes},
    }
    # Round-trip through the canonical normaliser so spec aliases, duplicate
    # detection and source-kind validation apply uniformly.
    return mapping_from_wire_dict(wire)
