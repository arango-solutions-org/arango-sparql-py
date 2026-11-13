"""Down-select a :class:`MappingBundle` to a single ArangoDB named graph.

When a session binds a named-graph scope (``POST /session/graph``), the
schema bundle acquired for that session is filtered to only the
collections that participate in the graph — so the resolver, OWL view,
and NL suggestions never see collections that belong to other
applications sharing the same database.

Two resolution paths, in priority order (mirrors the sister project):

1. **Membership tags** — the analyzer (>= 0.8) annotates each
   ``physicalMapping`` entity / relationship with a ``graphs`` list. When
   present we filter by ``graph_name in spec["graphs"]`` — no extra DB
   round-trip.
2. **Live lookup** — fall back to ``db.graph(name)`` and read its vertex
   and edge collections directly. Covers analyzer versions / databases
   that don't emit the membership tags.

If neither path can resolve the graph's collections the bundle is
returned **unchanged** (and a warning logged): an unfiltered schema is
safer than a silently-empty one.
"""

from __future__ import annotations

import dataclasses
import logging
from typing import Any

from .._privacy import tenant_name
from ..translate.mapping import MappingBundle

logger = logging.getLogger(__name__)

__all__ = ["scope_bundle_to_graph"]


def _membership_collections(bundle: MappingBundle, graph_name: str) -> tuple[set[str], set[str]] | None:
    """Return ``(vertex, edge)`` collections from per-entry ``graphs`` tags.

    ``None`` when no entity or relationship in the bundle carries a
    ``graphs`` annotation — the signal the analyzer didn't emit
    membership data and we should fall back to a live lookup.
    """
    vertex: set[str] = set()
    edges: set[str] = set()
    saw_tag = False

    for spec in bundle.entities().values():
        if not isinstance(spec, dict):
            continue
        tags = spec.get("graphs")
        if isinstance(tags, list):
            saw_tag = True
            if graph_name in tags:
                coll = spec.get("collectionName")
                if coll:
                    vertex.add(coll)

    for spec in bundle.relationships().values():
        if not isinstance(spec, dict):
            continue
        tags = spec.get("graphs")
        if isinstance(tags, list):
            saw_tag = True
            if graph_name in tags:
                coll = spec.get("edgeCollectionName") or spec.get("collectionName")
                if coll:
                    edges.add(coll)

    if not saw_tag:
        return None
    return vertex, edges


def _live_graph_collections(db: Any, graph_name: str) -> tuple[set[str], set[str]] | None:
    """Read a named graph's vertex/edge collections straight from *db*.

    Returns ``None`` when the graph is absent or the driver call fails —
    the caller treats that as "could not resolve, leave bundle whole".
    """
    try:
        if hasattr(db, "has_graph") and not db.has_graph(graph_name):
            return None
        graph = db.graph(graph_name)
    except Exception as exc:  # noqa: BLE001 — degrade to "unresolved"
        logger.info("live graph lookup for %r failed: %s", tenant_name(graph_name), exc)
        return None

    vertex: set[str] = set()
    edges: set[str] = set()
    try:
        for v in graph.vertex_collections() or []:
            vertex.add(v)
        for ed in graph.edge_definitions() or []:
            if not isinstance(ed, dict):
                continue
            coll = ed.get("edge_collection") or ed.get("collection")
            if coll:
                edges.add(coll)
            for v in ed.get("from_vertex_collections") or ed.get("from") or []:
                vertex.add(v)
            for v in ed.get("to_vertex_collections") or ed.get("to") or []:
                vertex.add(v)
    except Exception as exc:  # noqa: BLE001 — partial data is unsafe; bail
        logger.info("reading collections for graph %r failed: %s", tenant_name(graph_name), exc)
        return None

    return vertex, edges


def _filter_conceptual(
    conceptual: dict[str, Any], kept_entities: set[str], kept_rels: set[str]
) -> dict[str, Any]:
    """Prune the conceptual schema's entity/relationship lists by name."""
    if not isinstance(conceptual, dict):
        return conceptual
    out = dict(conceptual)

    ents = conceptual.get("entities")
    if isinstance(ents, list):
        out["entities"] = [e for e in ents if not isinstance(e, dict) or e.get("name") in kept_entities]

    rels = conceptual.get("relationships")
    if isinstance(rels, list):
        out["relationships"] = [
            r for r in rels if not isinstance(r, dict) or (r.get("type") or r.get("name")) in kept_rels
        ]

    return out


def _filter_bundle(
    bundle: MappingBundle,
    vertex: set[str],
    edges: set[str],
    graph_name: str,
) -> MappingBundle:
    """Return a copy of *bundle* keeping only collections in *vertex*/*edges*."""
    kept_entities = {
        name
        for name, spec in bundle.entities().items()
        if isinstance(spec, dict) and spec.get("collectionName") in vertex
    }
    kept_rels = {
        name
        for name, spec in bundle.relationships().items()
        if isinstance(spec, dict) and (spec.get("edgeCollectionName") or spec.get("collectionName")) in edges
    }

    physical = dict(bundle.physical_mapping or {})
    physical["entities"] = {name: spec for name, spec in bundle.entities().items() if name in kept_entities}
    physical["relationships"] = {
        name: spec for name, spec in bundle.relationships().items() if name in kept_rels
    }

    metadata = dict(bundle.metadata or {})
    metadata["graphScope"] = graph_name

    conceptual = _filter_conceptual(bundle.conceptual_schema or {}, kept_entities, kept_rels)

    return dataclasses.replace(
        bundle,
        conceptual_schema=conceptual,
        physical_mapping=physical,
        metadata=metadata,
    )


def scope_bundle_to_graph(db: Any, bundle: MappingBundle, graph_name: str | None) -> MappingBundle:
    """Down-select *bundle* to the named graph *graph_name*.

    A ``None`` / empty *graph_name* is a no-op (returns *bundle*
    unchanged). When the graph's collections can be resolved (via
    membership tags or a live lookup) the bundle is filtered to them;
    otherwise the full bundle is returned and a warning logged.
    """
    if not graph_name:
        return bundle

    resolved = _membership_collections(bundle, graph_name)
    if resolved is None:
        resolved = _live_graph_collections(db, graph_name)

    if resolved is None:
        logger.warning(
            "could not resolve collections for graph %r; returning unfiltered schema bundle",
            tenant_name(graph_name),
        )
        return bundle

    vertex, edges = resolved
    return _filter_bundle(bundle, vertex, edges, graph_name)
