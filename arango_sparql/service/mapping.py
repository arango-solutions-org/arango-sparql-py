"""HTTP-shaped mapping helper for ``arango_sparql.service``.

Mirror of ``arango_cypher.service.mapping`` — single-function module
that adapts the wire JSON / inline Turtle payload on a translate /
execute / validate request into the in-memory
:class:`~arango_sparql.translate.resolver.SchemaResolver` the visitor
consumes.

Two payload shapes are recognised, in order of precedence:

1. ``req.ontology_ttl`` — a Turtle string carrying the OWL ontology
   produced by ``arango-schema-mapper``. Parsed into a fresh
   :class:`rdflib.Graph` and wrapped in a
   :class:`~arango_sparql.translate.resolver.SchemaResolver`. This is
   the path the ``/translate`` and ``/execute`` endpoints take today
   because it lets the UI ship a self-contained request without
   depending on a server-side cache.

2. ``req.mapping`` — a JSON dict. Today only the ``{"ttl": "<turtle>"}``
   key is honoured (which lets a JSON-only client tunnel a Turtle blob
   through the same field the Cypher project uses for its dict
   mapping). When the SPARQL service grows a richer wire shape — e.g.
   ``{"classes": [...], "properties": [...]}`` for a fully JSON-native
   mapping — extend :func:`_graph_from_request` here rather than at
   every call site.

When the caller supplies the analyzer-discovered :class:`MappingBundle`
for the connected database, :func:`_resolver_from_request` **merges** its
physical mapping into the inline ontology by local name (see
:func:`_enrich_graph_with_bundle`) so a user who queries a class they
declared but did not annotate still gets the discovered
``phys:collectionName`` rather than the local-name fallback.

Returns an *empty* resolver (graph with no triples) when neither
field is set. The resolver's unmapped-property fallback degrades any
bare URI to its local-name attribute, so simple SELECT queries
against an unmapped collection still work — see
``arango_sparql.translate.resolver.SchemaResolver.resolve_property``
for the contract. Callers that require a populated ontology should
validate the request before calling this helper.
"""

from __future__ import annotations

from typing import Any

from rdflib import OWL, RDF, Graph, Literal, URIRef

from ..translate.mapping import MappingBundle
from ..translate.resolver import (
    _PHYS_NAMESPACES,
    _SYNTHETIC_PHYS_NS,
    SchemaResolver,
    analyze_uri_indexes,
    local_name,
)

# ---------------------------------------------------------------------------
# Analyzer-bundle merge (enrich the inline ontology with discovered mapping)
# ---------------------------------------------------------------------------
#
# The translate/execute path's resolver is graph-based: it resolves a class
# IRI by reading the ``phys:*`` annotations on that IRI's ``owl:Class`` node.
# A hand-authored ontology (the Turtle a user types in the UI editor) rarely
# carries those annotations, so the resolver falls back to the IRI's local
# name and emits ``W_SCHEMA_DEFAULT_COLLECTION``.
#
# The arango-schema-analyzer *does* discover the physical mapping
# (collection names, edge collections, RPT columns, …) for the connected
# database, but it lands in a separate :class:`MappingBundle` keyed by entity
# *label* (local name) with synthetic ``urn:`` IRIs — so a blind graph union
# would never match the user's ``http://…#Person`` IRI.
#
# The merge therefore enriches *by local name*: for each ``owl:Class`` /
# ``owl:ObjectProperty`` in the inline graph, look up the analyzer entity /
# relationship with the same local name and copy its ``phys:*`` annotations
# onto the inline IRI — but only those the inline ontology did not already
# declare. Inline annotations always win; the bundle only fills gaps.

# Entity-spec field (analyzer-canonical camelCase) → ``phys:<local>`` the
# resolver reads. ``style`` is special-cased to ``phys:mappingStyle`` to match
# the synthesizer / resolver contract.
_ENTITY_FIELD_TO_PHYS_LOCAL: dict[str, str] = {
    "collectionName": "collectionName",
    "edgeCollectionName": "edgeCollectionName",
    "typeField": "typeField",
    "typeValue": "typeValue",
    "triplesCollection": "triplesCollection",
    "subjectColumn": "subjectColumn",
    "predicateColumn": "predicateColumn",
    "objectUriColumn": "objectUriColumn",
    "objectValueColumn": "objectValueColumn",
    "tenantField": "tenantField",
    "tenantEntity": "tenantEntity",
    "style": "mappingStyle",
}

# Relationship-spec field → ``phys:<local>``. Subset of the entity map —
# relationships never carry a document ``collectionName``.
_REL_FIELD_TO_PHYS_LOCAL: dict[str, str] = {
    "edgeCollectionName": "edgeCollectionName",
    "typeField": "typeField",
    "typeValue": "typeValue",
    "triplesCollection": "triplesCollection",
    "tenantField": "tenantField",
    "tenantEntity": "tenantEntity",
    "style": "mappingStyle",
}


def _has_phys(graph: Graph, subject: URIRef, phys_local: str) -> bool:
    """True if *subject* already carries ``phys:<phys_local>`` in any of the
    accepted physical namespaces (so an inline annotation is never clobbered).
    """
    for ns in _PHYS_NAMESPACES:
        if graph.value(subject, ns[phys_local]) is not None:
            return True
    return False


def _apply_phys_annotations(
    graph: Graph,
    subject: URIRef,
    spec: dict[str, Any],
    field_map: dict[str, str],
) -> None:
    """Copy the analyzer *spec*'s physical fields onto *subject* as
    ``phys:*`` literals, skipping any the inline ontology already declares.
    """
    for field_name, phys_local in field_map.items():
        value = spec.get(field_name)
        if value is None or value == "":
            continue
        if _has_phys(graph, subject, phys_local):
            continue
        graph.add((subject, _SYNTHETIC_PHYS_NS[phys_local], Literal(str(value))))


def _enrich_graph_with_bundle(graph: Graph, bundle: MappingBundle) -> None:
    """Fill missing ``phys:*`` annotations on the inline graph's classes and
    object properties from the analyzer-discovered *bundle*, matched by local
    name. Mutates *graph* in place. A no-op when the bundle has no entities.
    """
    entities = bundle.entities()
    if entities:
        for cls_iri in list(graph.subjects(RDF.type, OWL.Class)):
            if not isinstance(cls_iri, URIRef):
                continue
            spec = entities.get(local_name(cls_iri))
            if isinstance(spec, dict):
                _apply_phys_annotations(graph, cls_iri, spec, _ENTITY_FIELD_TO_PHYS_LOCAL)

    relationships = bundle.relationships()
    if relationships:
        for prop_iri in list(graph.subjects(RDF.type, OWL.ObjectProperty)):
            if not isinstance(prop_iri, URIRef):
                continue
            spec = relationships.get(local_name(prop_iri))
            if isinstance(spec, dict):
                _apply_phys_annotations(graph, prop_iri, spec, _REL_FIELD_TO_PHYS_LOCAL)


def _graph_from_request(req: Any) -> Graph:
    """Parse the request's inline ontology into an ``rdflib.Graph``.

    Inline ``ontology_ttl`` wins over a JSON ``mapping={"ttl": …}`` payload
    (the UI's "edit ontology" affordance overrides a tunnelled mapping for
    one-off queries). Returns an empty graph when neither is supplied.
    """
    ttl = getattr(req, "ontology_ttl", None)
    if isinstance(ttl, str) and ttl.strip():
        graph = Graph()
        graph.parse(data=ttl, format="turtle")
        return graph

    mapping = getattr(req, "mapping", None)
    if isinstance(mapping, dict) and mapping:
        inner = mapping.get("ttl")
        if isinstance(inner, str) and inner.strip():
            graph = Graph()
            graph.parse(data=inner, format="turtle")
            return graph

    return Graph()


def _is_imported_ontology(bundle: MappingBundle | None) -> bool:
    """Whether *bundle* is an imported ontology with its own Turtle, rather than an analysis
    of a database. A session holds one only under a governance control plane: the server
    caches analyses, never imports (``/mapping/import-owl`` hands its bundle back)."""
    source = getattr(bundle, "source", None)
    return bool(bundle is not None and bundle.owl_turtle and getattr(source, "kind", None) == "imported_owl")


def _resolver_from_request(req: Any, *, analyzer_bundle: MappingBundle | None = None) -> SchemaResolver:
    """Build a :class:`SchemaResolver` from the request envelope.

    Accepts any request model carrying an ``ontology_ttl`` (str | None)
    or ``mapping`` (dict | None) attribute — the ``/translate``,
    ``/execute`` and ``/validate`` request models all do. Inline
    Turtle wins over a JSON ``mapping`` payload when both are
    present so the UI's "edit ontology" affordance can override a
    cached mapping for one-off queries without first having to mutate
    the cache.

    When *analyzer_bundle* is supplied (the schema the analyzer discovered
    for the connected database), its physical mapping is **merged** into the
    inline ontology by local name: classes/properties the user declared but
    did not annotate inherit the discovered ``phys:collectionName`` /
    ``phys:edgeCollectionName`` / RPT columns, eliminating the
    ``W_SCHEMA_DEFAULT_COLLECTION`` guess. Inline annotations always win.

    Falls back to an empty :class:`rdflib.Graph` resolver (which lets
    bare-URI predicates degrade to local-name attributes) when neither
    field is supplied, matching the legacy behaviour of the original
    ``_resolver_from_ttl`` helper that this function replaces.
    """
    graph = _graph_from_request(req)
    if not len(graph) and _is_imported_ontology(analyzer_bundle):
        # The session's schema *is* an ontology (a project's, under a governance control
        # plane, PRD §8.7): a request that brings none is asked in that ontology's terms.
        assert analyzer_bundle is not None and analyzer_bundle.owl_turtle
        graph.parse(data=analyzer_bundle.owl_turtle, format="turtle")
    if analyzer_bundle is not None:
        _enrich_graph_with_bundle(graph, analyzer_bundle)
    # The live service runs against a real customer database with no
    # ``Document`` catch-all collection, so an unroutable subject must fail
    # at translate time with a typed E_SCHEMA_RESOLVE rather than emit
    # ``FOR doc IN @@…_Document`` that dies later on ``ERR 1203`` (AGENTS.md
    # hard-rule #5). The translation-only / W3C harness leave this off.
    return SchemaResolver(
        ontology=graph, strict_subject_resolution=True, uri_indexes=analyze_uri_indexes(analyzer_bundle)
    )
