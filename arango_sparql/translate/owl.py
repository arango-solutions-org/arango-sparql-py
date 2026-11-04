"""OWL/Turtle ↔ :class:`MappingBundle` round-trip.

Two helpers, kept symmetric so a UI's Import/Export button cycle is
information-preserving:

* :func:`turtle_to_mapping` parses an OWL/Turtle ontology produced by
  ``arangodb-schema-analyzer`` (or hand-authored against the same
  ``phys:*`` vocabulary documented in PRD §6.2) into a
  :class:`MappingBundle`. The original Turtle is preserved on the
  bundle's :attr:`MappingBundle.owl_turtle` slot so a downstream
  :func:`SchemaResolver.from_mapping_bundle` call routes through the
  faster inline-OWL path rather than the synthesizer.

* :func:`mapping_to_turtle` is the inverse — when the bundle already
  carries an inline ``owl_turtle`` it is returned verbatim (the
  analyzer's serialisation is the canonical form), otherwise the
  bundle's ``physical_mapping`` is round-tripped through
  :func:`_synthesize_graph_from_bundle` and serialised by ``rdflib``.

Both halves understand the OWL-bomb defences mandated by PRD §8.6
T7 — the route layer is the canonical enforcement site for the byte
ceiling (it gets to short-circuit before the parser ever runs), but
:func:`turtle_to_mapping` defends the post-parse triple cap so a
direct library call cannot bypass it. :class:`OwlBombError` is the
typed escape hatch the route layer translates to ``422
E_OWL_TOO_LARGE``.

Public surface:

* :func:`turtle_to_mapping` — Turtle string → :class:`MappingBundle`
* :func:`mapping_to_turtle` — :class:`MappingBundle` → Turtle string
* :func:`count_triples` — cheap pre-flight triple count for a
  parsed graph; surfaced so the route handler can include the value
  in its log line without re-walking the graph.
* :data:`DEFAULT_MAPPING_IMPORT_MAX_TRIPLES` — module-level default
  cap; the route layer reads :envvar:`MAPPING_IMPORT_MAX_TRIPLES` at
  request time so an operator can tune the limit without a code
  change.
* :class:`OwlBombError` — typed exception with ``code`` matching the
  PRD's stable error code (``E_OWL_TOO_LARGE``).
"""

from __future__ import annotations

import os
import re
from typing import Any

from rdflib import OWL, RDF, RDFS, Graph, Literal, URIRef
from rdflib.collection import Collection

from ..errors import SparqlError
from .mapping import (
    MappingBundle,
    MappingError,
    MappingSource,
    is_valid_collection_name,
    is_valid_field_name,
)
from .resolver import (
    _PHYS_NAMESPACES,
    _SYNTHETIC_PHYS_NS,
    _synthesize_graph_from_bundle,
    local_name,
)

__all__ = [
    "DEFAULT_MAPPING_IMPORT_MAX_TRIPLES",
    "MAPPING_IMPORT_MAX_TRIPLES_ENV",
    "OwlBombError",
    "OwlParseError",
    "count_triples",
    "format_from_mime",
    "mapping_to_turtle",
    "owl_graph_view",
    "parse_owl_graph",
    "resolve_max_triples",
    "turtle_to_mapping",
]


# ---------------------------------------------------------------------------
# OWL-bomb defence — post-parse triple cap (PRD §8.6 T7)
# ---------------------------------------------------------------------------
#
# The byte cap (PRD A.2 ``MAPPING_IMPORT_MAX_BYTES``, default 2 MB) is
# enforced at the route boundary before this module ever sees the
# request. The triple cap is enforced *here* so a direct library call
# (e.g. from the OWL-import smoke test or from a future mapping CLI)
# cannot bypass it.

MAPPING_IMPORT_MAX_TRIPLES_ENV: str = "MAPPING_IMPORT_MAX_TRIPLES"
DEFAULT_MAPPING_IMPORT_MAX_TRIPLES: int = 200_000


def resolve_max_triples(override: int | None = None) -> int:
    """Return the active triple cap.

    Precedence: explicit *override* (route handler tests) →
    :envvar:`MAPPING_IMPORT_MAX_TRIPLES` → module default. Garbage
    env values fall through to the default rather than raising —
    a deployment YAML typo must not silently disable the cap (PRD
    §6.3.4 motif applied to OWL-bomb defence).
    """

    if override is not None:
        return max(1, int(override))
    raw = (os.getenv(MAPPING_IMPORT_MAX_TRIPLES_ENV) or "").strip()
    if not raw:
        return DEFAULT_MAPPING_IMPORT_MAX_TRIPLES
    try:
        parsed = int(raw)
        if parsed > 0:
            return parsed
    except ValueError:
        pass
    return DEFAULT_MAPPING_IMPORT_MAX_TRIPLES


class OwlBombError(SparqlError):
    """Raised when an imported OWL/Turtle document exceeds a configured
    safety bound.

    Carries ``code = "E_OWL_TOO_LARGE"`` so the route layer's existing
    ``{"error": ..., "code": ...}`` envelope picks it up automatically
    via :class:`SparqlError.code`.
    """

    code = "E_OWL_TOO_LARGE"


class OwlParseError(SparqlError):
    """Raised when ``rdflib`` cannot parse the supplied Turtle / OWL.

    Distinct code from :class:`OwlBombError` so a downstream UI can
    distinguish "your ontology is malformed" (``E_OWL_PARSE``) from
    "your ontology is too big" (``E_OWL_TOO_LARGE``).
    """

    code = "E_OWL_PARSE"


# ---------------------------------------------------------------------------
# Format dispatch — Turtle, RDF/XML, JSON-LD, N-Triples (PRD §11.3/§12.2)
# ---------------------------------------------------------------------------
#
# All four formats are natively supported by the installed rdflib (no new
# dependency). ``_FORMAT_ALIASES`` maps the MIME strings a client sends on
# ``Content-Type``/``Accept`` to the bare rdflib format name
# ``Graph.parse``/``Graph.serialize`` expect; :func:`_resolve_rdflib_format`
# additionally accepts the bare rdflib name itself so library callers don't
# have to spell out a MIME type.

_FORMAT_ALIASES: dict[str, str] = {
    "text/turtle": "turtle",
    "application/x-turtle": "turtle",
    "application/rdf+xml": "xml",  # rdflib's format name for RDF/XML is "xml"
    "application/ld+json": "json-ld",
    "application/n-triples": "nt",
}

_VALID_RDFLIB_FORMATS: frozenset[str] = frozenset(_FORMAT_ALIASES.values())


def format_from_mime(mime: str | None) -> str | None:
    """Return the rdflib format name for *mime*, or ``None`` if unrecognised.

    *mime* should already have any ``;`` parameters stripped (the route
    layer does this before calling in). Used by both the import
    Content-Type sniff and the export Accept negotiation so they share a
    single source of truth (:data:`_FORMAT_ALIASES`).
    """

    if not mime:
        return None
    return _FORMAT_ALIASES.get(mime.strip().lower())


def _resolve_rdflib_format(format: str) -> str:
    """Resolve *format* — a MIME string or a bare rdflib format name — to
    the rdflib format name ``Graph.parse``/``Graph.serialize`` expect.

    Raises :class:`OwlParseError` (not a bare ``KeyError``/``ValueError``)
    for anything unrecognised, matching this module's "never raise a raw
    rdflib/stdlib exception" contract.
    """

    if not format or not isinstance(format, str):
        raise OwlParseError(f"unrecognised OWL format: {format!r}")
    normalized = format.strip().lower()
    if normalized in _FORMAT_ALIASES:
        return _FORMAT_ALIASES[normalized]
    if normalized in _VALID_RDFLIB_FORMATS:
        return normalized
    raise OwlParseError(
        f"unrecognised OWL format {format!r}; expected one of "
        f"{sorted(_VALID_RDFLIB_FORMATS)} or a matching MIME type "
        f"({sorted(_FORMAT_ALIASES)})"
    )


# ---------------------------------------------------------------------------
# RDF/XML pre-parse DOCTYPE/ENTITY guard (billion-laughs / XXE defence)
# ---------------------------------------------------------------------------
#
# The post-parse triple cap (below) cannot defend against an entity-
# expansion ("billion laughs") bomb: the bomb detonates memory DURING
# ``graph.parse()``, before a ``Graph`` exists for ``count_triples`` to
# inspect. RDF/XML never legitimately requires a DTD, so rejecting any
# ``<!DOCTYPE`` / ``<!ENTITY`` declaration before the bytes ever reach
# rdflib's SAX-based RDF/XML parser is a safe, format-appropriate
# rejection — not a functional limitation. This also blocks external-
# entity (XXE) references (``<!ENTITY xxe SYSTEM "file:///etc/passwd">``)
# since the SAX parser never sees the declaration that would trigger
# resolution.

_XML_DTD_PATTERN = re.compile(rb"<!\s*(?:DOCTYPE|ENTITY)\b", re.IGNORECASE)


def _reject_xml_dtd(data: str | bytes) -> None:
    """Raise :class:`OwlParseError` if *data* contains a ``<!DOCTYPE`` or
    ``<!ENTITY`` declaration anywhere in the document.

    Only meaningful for RDF/XML input — callers must gate this on the
    resolved format being ``"xml"``. Scans the raw bytes directly
    (case-insensitively) rather than parsing, so the check itself cannot
    be tricked into doing expensive work on a hostile payload.
    """

    raw = data.encode("utf-8", errors="ignore") if isinstance(data, str) else data
    if raw.startswith(b"\xef\xbb\xbf"):  # tolerate a leading UTF-8 BOM
        raw = raw[3:]
    if _XML_DTD_PATTERN.search(raw):
        raise OwlParseError(
            "RDF/XML DOCTYPE/ENTITY declarations are not permitted "
            "(billion-laughs / XXE defence); RDF/XML never legitimately "
            "requires a DTD"
        )


def parse_owl_graph(data: str, format: str = "turtle") -> Graph:
    """Parse *data* into a fresh :class:`rdflib.Graph`.

    Resolves *format* via :func:`_resolve_rdflib_format`, applies the
    pre-parse :func:`_reject_xml_dtd` guard when the resolved format is
    RDF/XML, then hands the bytes to ``rdflib``. Any parse failure —
    including a rejected DTD — is wrapped in :class:`OwlParseError`
    (``E_OWL_PARSE``) so callers never see a raw rdflib/stdlib exception.
    """

    resolved = _resolve_rdflib_format(format)
    if resolved == "xml":
        _reject_xml_dtd(data)
    graph = Graph()
    try:
        graph.parse(data=data, format=resolved)
    except OwlParseError:
        raise
    except Exception as exc:
        raise OwlParseError(f"failed to parse OWL ({format}): {exc}") from exc
    return graph


# ---------------------------------------------------------------------------
# Cheap helpers
# ---------------------------------------------------------------------------


def count_triples(graph: Graph) -> int:
    """Return the triple count of *graph*.

    Wraps ``len(graph)`` so callers get a stable name even if the
    underlying ``rdflib`` API ever changes (it's been stable for
    ten years, but the indirection is free).
    """

    return len(graph)


# ---------------------------------------------------------------------------
# Turtle → MappingBundle
# ---------------------------------------------------------------------------


# Reverse-lookup map: ``phys:*`` annotation local-name → bundle field
# spelling. Mirrors the analyzer's annotation vocabulary documented
# in PRD §6.2. Two ``phys:*`` namespaces are accepted on read (see
# :data:`resolver._PHYS_NAMESPACES`); both produce the same bundle
# field on import.
_PHYS_TO_ENTITY_FIELD: dict[str, str] = {
    "collectionName": "collectionName",
    "edgeCollectionName": "edgeCollectionName",
    "typeField": "typeField",
    "typeValue": "typeValue",
    "typeFieldIsArray": "typeFieldIsArray",
    "triplesCollection": "triplesCollection",
    "subjectColumn": "subjectColumn",
    "predicateColumn": "predicateColumn",
    "objectUriColumn": "objectUriColumn",
    "objectValueColumn": "objectValueColumn",
    "tenantField": "tenantField",
    "tenantEntity": "tenantEntity",
    "mappingStyle": "style",
}

_PHYS_TO_RELATIONSHIP_FIELD: dict[str, str] = {
    "edgeCollectionName": "edgeCollectionName",
    "typeField": "typeField",
    "typeValue": "typeValue",
    "triplesCollection": "triplesCollection",
    "tenantField": "tenantField",
    "tenantEntity": "tenantEntity",
    "mappingStyle": "style",
}


def turtle_to_mapping(
    turtle: str,
    *,
    format: str = "turtle",
    max_triples: int | None = None,
    preserve_owl: bool = True,
    source_notes: str | None = None,
    derive_conceptual: bool = False,
) -> MappingBundle:
    """Parse *turtle* and project it into a :class:`MappingBundle`.

    *format* selects the rdflib parser: ``"turtle"`` (default),
    ``"xml"`` (RDF/XML), ``"json-ld"``, or ``"nt"`` (N-Triples) — or
    the equivalent MIME string for any of those (see
    :data:`_FORMAT_ALIASES`). All four are native to the installed
    rdflib; no new dependency is required.

    Steps:

    1. Hand the input to :func:`parse_owl_graph`, which resolves
       *format*, applies the pre-parse RDF/XML DOCTYPE/ENTITY guard
       (billion-laughs/XXE defence) when the resolved format is
       ``"xml"``, and parses via ``rdflib``. Parse errors — including
       a rejected DTD — are wrapped in :class:`OwlParseError` with
       the PRD's stable ``E_OWL_PARSE`` code.
    2. Enforce the triple cap (PRD §8.6 T7). The default is
       :data:`DEFAULT_MAPPING_IMPORT_MAX_TRIPLES` (200 000); an
       explicit *max_triples* override is honoured by the route
       layer's tests but is otherwise read from the
       :envvar:`MAPPING_IMPORT_MAX_TRIPLES` env var. This cap is
       applied to the parsed ``Graph`` — i.e. it is format-agnostic
       and fires identically regardless of *format*.
    3. Walk every ``owl:Class`` / ``owl:ObjectProperty`` /
       ``owl:DatatypeProperty`` resource and harvest its ``phys:*``
       annotations into the analyzer-canonical
       ``physicalMapping.{entities, relationships}`` shape.
    4. Build a :class:`MappingBundle` with the ontology on
       :attr:`MappingBundle.owl_turtle` (if *preserve_owl* is true)
       so the resolver can re-use it without reserialisation, and a
       :class:`MappingSource` tagged ``imported_owl`` with the
       supplied *source_notes*. ``owl_turtle`` is documented
       elsewhere (:mod:`resolver`, the schema routes) as always being
       Turtle text regardless of the import format — when *format*
       is not ``"turtle"`` the parsed graph is re-serialised to
       canonical Turtle before being stored, so that invariant holds.

    The conceptual half is left empty by design — the analyzer's
    OWL emission is the canonical conceptual schema, and we don't
    want to fabricate one. Downstream callers that need the
    conceptual block can derive it from the OWL or push it via
    a separate API.
    """

    if turtle is None or not isinstance(turtle, str):
        raise OwlParseError("turtle input must be a non-empty string")
    if not turtle.strip():
        # rdflib happily parses ``""`` into an empty graph; from the
        # library's perspective an empty input is a misuse — the
        # caller meant to supply Turtle and supplied nothing. The
        # route layer already enforces this at the body level
        # (``E_OWL_EMPTY_BODY``); raise here so a direct library
        # call surfaces the same shape.
        raise OwlParseError(
            "turtle input is empty; supply at least one prefix declaration or class statement"
        )

    resolved_format = _resolve_rdflib_format(format)
    graph = parse_owl_graph(turtle, resolved_format)

    cap = resolve_max_triples(max_triples)
    triples = count_triples(graph)
    if triples > cap:
        raise OwlBombError(
            f"OWL ontology exceeds the {MAPPING_IMPORT_MAX_TRIPLES_ENV} "
            f"cap ({triples} > {cap} triples). Lower the cap, split the "
            "ontology, or push it directly to the analyzer."
        )

    entities, entity_warnings = _entities_from_graph(graph)
    relationships, rel_warnings = _relationships_from_graph(graph)

    metadata: dict[str, Any] = {
        "source": "imported_owl",
        "tripleCount": triples,
    }
    warnings = entity_warnings + rel_warnings
    if warnings:
        metadata["warnings"] = warnings

    owl_turtle_value: str | None
    if not preserve_owl:
        owl_turtle_value = None
    elif resolved_format == "turtle":
        owl_turtle_value = turtle
    else:
        # ``MappingBundle.owl_turtle`` is consumed elsewhere (resolver.py,
        # the schema routes) under the hard assumption that it is Turtle
        # text — re-serialise so that invariant holds regardless of the
        # format the caller imported from.
        owl_turtle_value = graph.serialize(format="turtle")

    bundle = MappingBundle(
        conceptual_schema=(
            conceptual_schema_from_graph(graph)
            if derive_conceptual
            else {"entities": [], "relationships": []}
        ),
        physical_mapping={
            "entities": entities,
            "relationships": relationships,
        },
        metadata=metadata,
        owl_turtle=owl_turtle_value,
        source=MappingSource(
            kind="imported_owl",
            notes=source_notes,
        ),
    )
    return bundle


def conceptual_schema_from_graph(graph: Graph) -> dict[str, list[dict[str, Any]]]:
    """The conceptual half of a bundle, read from an ontology that declares its terms: each
    ``owl:Class`` (by local name, one entry per name) with the data properties whose
    ``rdfs:domain`` it or a class above it is, and each ``owl:ObjectProperty`` with its
    domain and range. A domain or range may be repeated or an ``owl:unionOf`` (either reads
    as "any of them"). For an ontology that *is* the schema (one a governance control plane
    assembles for a project, PRD §8.7), so the NL prompt and the editor list its terms; the
    analyzer's own OWL does not need it."""

    def name(node: Any) -> str:
        return local_name(node) if isinstance(node, URIRef) else ""

    def classes(node: Any) -> list[str]:
        if isinstance(node, URIRef):
            return [name(node)]
        members = graph.value(node, OWL.unionOf)
        return [name(m) for m in Collection(graph, members)] if members is not None else []

    def named(values: Any) -> list[str]:
        return [c for v in values for c in classes(v) if c]

    own: dict[str, list[str]] = {}
    for prop in sorted(set(graph.subjects(RDF.type, OWL.DatatypeProperty)), key=str):
        prop_name = name(prop)
        for cls in named(graph.objects(prop, RDFS.domain)):
            if prop_name and prop_name not in own.setdefault(cls, []):
                own[cls].append(prop_name)
    parents: dict[str, list[str]] = {}
    for sub, sup in graph.subject_objects(RDFS.subClassOf):
        if name(sub) and name(sup):
            parents.setdefault(name(sub), []).append(name(sup))

    def inherited(cls: str, seen: frozenset[str] = frozenset()) -> list[str]:
        out = list(own.get(cls, []))
        for parent in sorted(parents.get(cls, [])):
            if parent not in seen:
                out += [p for p in inherited(parent, seen | {cls}) if p not in out]
        return out

    names = sorted({name(c) for c in graph.subjects(RDF.type, OWL.Class)} - {""})
    entities = [{"name": n, "labels": [n], "properties": [{"name": p} for p in inherited(n)]} for n in names]
    relationships = []
    seen_rel: set[tuple[str, str, str]] = set()
    for prop in sorted(set(graph.subjects(RDF.type, OWL.ObjectProperty)), key=str):
        prop_name = name(prop)
        domains = named(graph.objects(prop, RDFS.domain)) or [""]
        ranges = named(graph.objects(prop, RDFS.range)) or [""]
        for source in sorted(set(domains)):
            for target in sorted(set(ranges)):
                if prop_name and (prop_name, source, target) not in seen_rel:
                    seen_rel.add((prop_name, source, target))
                    relationships.append({"type": prop_name, "fromEntity": source, "toEntity": target})
    return {"entities": entities, "relationships": relationships}


def _entities_from_graph(
    graph: Graph,
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    """Walk *graph* for ``owl:Class`` resources and harvest their
    ``phys:*`` annotations into the entity-spec wire shape.
    """

    entities: dict[str, dict[str, Any]] = {}
    warnings: list[dict[str, Any]] = []
    for cls_iri in sorted(set(graph.subjects(RDF.type, OWL.Class)), key=str):
        if not isinstance(cls_iri, URIRef):
            continue
        label = local_name(cls_iri)
        if not label:
            continue

        spec: dict[str, Any] = {}
        for phys_local, bundle_field in _PHYS_TO_ENTITY_FIELD.items():
            value = _physical_literal(graph, cls_iri, phys_local)
            if value is None:
                continue
            spec[bundle_field] = value

        # Default style stays "COLLECTION" if no explicit
        # phys:mappingStyle was attached — matches the resolver's
        # tolerance (PRD §6.2 second-paragraph "phys:collectionName
        # alone is enough" semantic).
        style = spec.get("style") or "COLLECTION"
        spec["style"] = style

        # Validate the headline collection name when one is given.
        # Defer the failure (warning, not raise) so a partially-mapped
        # OWL document still imports cleanly — the route layer can
        # decide whether to surface the warning prominently.
        col = spec.get("collectionName") or spec.get("triplesCollection")
        if col is not None and not is_valid_collection_name(col):
            warnings.append(
                {
                    "code": "W_SCHEMA_INVALID_COLLECTION",
                    "message": (
                        f"class {label!r} declares an invalid "
                        f"collectionName {col!r}; the bundle was kept "
                        "but a downstream resolve will fail."
                    ),
                    "iri": str(cls_iri),
                }
            )

        entities[label] = spec

    return entities, warnings


def _relationships_from_graph(
    graph: Graph,
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    """Walk *graph* for ``owl:ObjectProperty`` resources and harvest
    them as relationship specs. ``owl:DatatypeProperty`` resources
    are skipped — they belong on the entity side, attached by the
    schema-mapper as per-class property annotations.
    """

    relationships: dict[str, dict[str, Any]] = {}
    warnings: list[dict[str, Any]] = []
    for prop_iri in sorted(set(graph.subjects(RDF.type, OWL.ObjectProperty)), key=str):
        if not isinstance(prop_iri, URIRef):
            continue
        rtype = local_name(prop_iri)
        if not rtype:
            continue

        spec: dict[str, Any] = {}
        for phys_local, bundle_field in _PHYS_TO_RELATIONSHIP_FIELD.items():
            value = _physical_literal(graph, prop_iri, phys_local)
            if value is None:
                continue
            spec[bundle_field] = value

        # Pull rdfs:domain / rdfs:range to fromEntity / toEntity so
        # the planner has the endpoints without needing to follow
        # the ObjectProperty IRI back to its synthesized class.
        for rdfs_pred, bundle_field in (
            (RDFS.domain, "fromEntity"),
            (RDFS.range, "toEntity"),
        ):
            obj = next(graph.objects(prop_iri, rdfs_pred), None)
            if isinstance(obj, URIRef):
                spec[bundle_field] = local_name(obj)

        style = spec.get("style") or "DEDICATED_COLLECTION"
        spec["style"] = style

        edge = spec.get("edgeCollectionName") or spec.get("triplesCollection")
        if edge is not None and not is_valid_collection_name(edge):
            warnings.append(
                {
                    "code": "W_SCHEMA_INVALID_EDGE_COLLECTION",
                    "message": (
                        f"object property {rtype!r} declares an invalid "
                        f"edge collection name {edge!r}; the bundle was "
                        "kept but a downstream resolve will fail."
                    ),
                    "iri": str(prop_iri),
                }
            )

        # Optional sanity check on tenantField / typeField — these
        # are field names, not collection names, so use the field
        # validator. Same defer-rather-than-raise posture.
        for slot in ("tenantField", "typeField"):
            value = spec.get(slot)
            if value is not None and not is_valid_field_name(value):
                warnings.append(
                    {
                        "code": "W_SCHEMA_INVALID_FIELD",
                        "message": (
                            f"object property {rtype!r} declares an "
                            f"invalid {slot} {value!r}; the bundle was "
                            "kept but a downstream resolve will fail."
                        ),
                        "iri": str(prop_iri),
                    }
                )

        relationships[rtype] = spec

    return relationships, warnings


def _physical_literal(graph: Graph, subject: URIRef, predicate_local: str) -> str | None:
    """Lookup a ``phys:<predicate_local>`` literal on *subject*.

    Tolerates both shipped ``phys:`` namespaces — :data:`_PHYS_NAMESPACES`
    is queried in order, returning the first hit so a hand-authored
    OWL using either spelling round-trips cleanly.
    """

    for ns in _PHYS_NAMESPACES:
        obj = next(graph.objects(subject, ns[predicate_local]), None)
        if isinstance(obj, Literal):
            text = str(obj)
            if text:
                return text
    return None


# ---------------------------------------------------------------------------
# MappingBundle → Turtle
# ---------------------------------------------------------------------------


def mapping_to_turtle(
    bundle: MappingBundle | None,
    *,
    format: str = "turtle",
    rebind_prefixes: bool = True,
) -> str:
    """Serialise *bundle* as an OWL string in the requested *format*.

    *format* selects the rdflib serialiser: ``"turtle"`` (default),
    ``"xml"`` (RDF/XML), ``"json-ld"``, or ``"nt"`` (N-Triples) — or
    the equivalent MIME string for any of those (see
    :data:`_FORMAT_ALIASES`).

    Two paths:

    * If the bundle already carries :attr:`MappingBundle.owl_turtle`
      (always Turtle text, regardless of the format it was originally
      imported from — see :func:`turtle_to_mapping`) and *format*
      resolves to ``"turtle"``, it is returned verbatim: the
      analyzer's OWL serialisation is the canonical form, and round-
      tripping it through rdflib would introduce syntactic drift
      (whitespace, prefix order) that a downstream UI's diff view
      would surface as spurious changes. When a different *format* is
      requested, the inline Turtle is re-parsed and re-serialised into
      that format (still avoiding the synthesizer).

    * Otherwise we synthesise a graph via
      :func:`_synthesize_graph_from_bundle` (the same helper the
      resolver uses) and serialise it in the requested format.
      ``rdflib`` picks sensible default prefix bindings; we
      additionally bind ``phys:`` for the synthesizer's annotation
      namespace so Turtle/RDF-XML output is human-readable.

    *rebind_prefixes* is a tunable for tests that need a known
    serialisation; production code should leave it at the default.
    """

    if bundle is None:
        raise MappingError("cannot serialise a None bundle")

    resolved_format = _resolve_rdflib_format(format)

    if bundle.owl_turtle:
        if resolved_format == "turtle":
            return bundle.owl_turtle
        # bundle.owl_turtle is always canonical Turtle (see
        # turtle_to_mapping's format-agnostic storage contract) — no XML
        # DTD guard needed here, we're parsing Turtle, not RDF/XML.
        graph = parse_owl_graph(bundle.owl_turtle, "turtle")
        return graph.serialize(format=resolved_format)

    graph = _synthesize_graph_from_bundle(bundle)
    if rebind_prefixes:
        graph.bind("phys", _SYNTHETIC_PHYS_NS, replace=True)
        graph.bind("owl", OWL, replace=True)
        graph.bind("rdfs", RDFS, replace=True)
    return graph.serialize(format=resolved_format)


# ---------------------------------------------------------------------------
# OWL/Turtle → schema-graph view (classes + properties)
# ---------------------------------------------------------------------------


def _graph_view_comment(graph: Graph, subject: URIRef) -> str | None:
    """Return the first ``rdfs:comment`` literal on *subject*, or ``None``."""

    obj = next(graph.objects(subject, RDFS.comment), None)
    if isinstance(obj, Literal):
        text = str(obj)
        return text or None
    return None


def _classes_view_from_graph(graph: Graph) -> list[dict[str, Any]]:
    """Project every ``owl:Class`` resource into the UI class shape."""

    out: list[dict[str, Any]] = []
    for cls_iri in sorted(set(graph.subjects(RDF.type, OWL.Class)), key=str):
        if not isinstance(cls_iri, URIRef):
            continue
        name = local_name(cls_iri)
        if not name:
            continue
        supers = sorted(str(s) for s in graph.objects(cls_iri, RDFS.subClassOf) if isinstance(s, URIRef))
        item: dict[str, Any] = {
            "iri": str(cls_iri),
            "localName": name,
            "superClasses": supers,
        }
        comment = _graph_view_comment(graph, cls_iri)
        if comment:
            item["comment"] = comment
        out.append(item)
    return out


# OWL property types projected into the graph view, in precedence order.
# A resource typed as more than one (rare, but legal RDF) is emitted once
# under the first matching kind so the renderer never sees a duplicate.
_PROPERTY_KINDS: tuple[tuple[Any, str], ...] = (
    (OWL.ObjectProperty, "object"),
    (OWL.DatatypeProperty, "datatype"),
    (OWL.AnnotationProperty, "annotation"),
)


def _properties_view_from_graph(graph: Graph) -> list[dict[str, Any]]:
    """Project every OWL property resource into the UI property shape."""

    out: list[dict[str, Any]] = []
    seen: set[URIRef] = set()
    for rdf_type, kind in _PROPERTY_KINDS:
        for prop_iri in sorted(set(graph.subjects(RDF.type, rdf_type)), key=str):
            if not isinstance(prop_iri, URIRef) or prop_iri in seen:
                continue
            name = local_name(prop_iri)
            if not name:
                continue
            seen.add(prop_iri)
            domain = sorted(str(o) for o in graph.objects(prop_iri, RDFS.domain) if isinstance(o, URIRef))
            rng = sorted(str(o) for o in graph.objects(prop_iri, RDFS.range) if isinstance(o, URIRef))
            item: dict[str, Any] = {
                "iri": str(prop_iri),
                "localName": name,
                "domain": domain,
                "range": rng,
                "kind": kind,
            }
            comment = _graph_view_comment(graph, prop_iri)
            if comment:
                item["comment"] = comment
            out.append(item)
    return out


def owl_graph_view(turtle: str | None, *, max_triples: int | None = None) -> dict[str, list[dict[str, Any]]]:
    """Parse OWL/Turtle into the UI schema-graph shape.

    Returns ``{"classes": [...], "properties": [...]}`` where each class is
    ``{iri, localName, superClasses, comment?}`` and each property is
    ``{iri, localName, domain, range, kind, comment?}`` (``kind`` ∈
    ``"object" | "datatype" | "annotation"``).

    This is the server-side counterpart to the frontend's ``n3``-based
    parser: both produce the identical ``OwlClass`` / ``OwlProperty`` shape
    consumed by ``CytoscapeSchemaGraph``, so a database-derived schema and
    an in-editor ontology render the same way.

    An empty / whitespace-only / ``None`` input yields empty lists rather
    than raising — the GRAPH tab treats "nothing to draw" as a normal,
    non-error state. A malformed ontology raises :class:`OwlParseError`
    (``E_OWL_PARSE``); an oversized one raises :class:`OwlBombError`
    (``E_OWL_TOO_LARGE``), reusing the same triple cap as
    :func:`turtle_to_mapping` so a direct library call cannot bypass it.
    """

    if turtle is None or not isinstance(turtle, str) or not turtle.strip():
        return {"classes": [], "properties": []}

    graph = Graph()
    try:
        graph.parse(data=turtle, format="turtle")
    except Exception as exc:
        raise OwlParseError(f"failed to parse Turtle: {exc}") from exc

    cap = resolve_max_triples(max_triples)
    triples = count_triples(graph)
    if triples > cap:
        raise OwlBombError(
            f"OWL ontology exceeds the {MAPPING_IMPORT_MAX_TRIPLES_ENV} "
            f"cap ({triples} > {cap} triples). Lower the cap, split the "
            "ontology, or push it directly to the analyzer."
        )

    return {
        "classes": _classes_view_from_graph(graph),
        "properties": _properties_view_from_graph(graph),
    }
