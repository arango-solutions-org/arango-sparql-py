"""Schema HTTP surface — PRD §6.4 (seven routes).

Mirror of ``arango_cypher.service.routes.schema`` with two
adaptations called out in :mod:`arango_sparql.service.models`:

* RPT collections get their own breakout in the conceptual summary
  because the SPARQL planner's RPT translator routes off ``style="RPT"``
  (the sister project's planner is Cypher-only and does not need
  the breakout).
* The cache is keyed by ``db.name`` rather than the sister's
  ``(db.name, key="mapping")`` tuple — Slice 5's
  :class:`~arango_sparql.schema.cache.SchemaCache` already does the
  per-db scoping so we don't re-thread the cache_key through every
  route signature.

A single :class:`~arango_sparql.schema.cache.SchemaCache` instance
lives at module scope as the process-wide L1 cache. Test isolation
is via :func:`_reset_cache` (called from ``conftest.py`` fixtures
and from individual tests via the public ``invalidate``-route).
"""

from __future__ import annotations

import logging as _log
import os
import time
from collections.abc import Callable
from typing import Any

from fastapi import Depends, HTTPException

from ...editor import ARANGODB_KIND, EditorVocabV1, editor_vocab
from ...schema.acquire import (
    ANALYZER_INSTALL_HINT,
    AnalyzerNotInstalledError,
    Strategy,
    acquire_mapping_bundle,
    analyzer_available,
    db_counts_fingerprint,
    db_shape_fingerprint,
    refresh_statistics,
)
from ...schema.cache import SchemaCache
from ...schema.fingerprint import FingerprintDrift, classify_live_drift
from ...schema.graph_scope import scope_bundle_to_graph
from ...schema.vocabulary import default_namespace
from ...translate.mapping import (
    MappingBundle,
    MappingError,
    mapping_from_wire_dict,
    mapping_to_wire_dict,
)
from ...translate.owl import (
    OwlBombError,
    OwlParseError,
    mapping_to_turtle,
    owl_graph_view,
)
from .. import control_plane, schema_warm
from ..app import app
from ..models import (
    OwlSchemaResponse,
    SchemaFingerprintBlock,
    SchemaForceReacquireResponse,
    SchemaIntrospectResponse,
    SchemaInvalidateCacheResponse,
    SchemaPropertiesResponse,
    SchemaStatisticsResponse,
    SchemaStatusResponse,
    SchemaSummaryRequest,
    SchemaSummaryResponse,
)
from ..observability import log_endpoint_timing
from ..security import (
    _check_compute_rate_limit,
    _get_session,
    _sanitize_error,
    _Session,
    background_database,
)

logger = _log.getLogger("arango_sparql.service.routes.schema")

# ---------------------------------------------------------------------------
# Process-wide cache singleton
# ---------------------------------------------------------------------------
#
# Per Slice 5 cache.py docstring: "every consumer that needs caching
# gets a SchemaCache instance from the FastAPI app factory". This is
# that instance for the route layer. Tests reset it via
# :func:`_reset_cache`; the underlying L1 dict has its own
# :class:`threading.Lock` so concurrent uvicorn worker threads do not
# need a route-side lock.

# Under a governance control plane the database is the project's and the service only reads
# it (PRD §8.7): the analyzed schema stays in memory, never written into that database.
_schema_cache: SchemaCache = SchemaCache(l2_writes=lambda: not control_plane.mode())


def _reset_cache() -> None:
    """Test hook: drop the process-wide cache so a fresh test does
    not see entries written by an earlier test. Safe to call in
    production too — equivalent to invalidating every connected DB.
    """

    _schema_cache.clear()


def _resolve_schema_cache() -> SchemaCache:
    """Return the live :class:`SchemaCache` instance.

    Looked up via this indirection (rather than imported by name)
    so a test can monkey-patch the module-level binding for one
    case without altering the real cache for the rest of the suite.
    """

    return _schema_cache


# ---------------------------------------------------------------------------
# Per-request env-var resolution (PRD §6.3.4)
# ---------------------------------------------------------------------------
#
# Both env vars are read on *every* request rather than cached at
# import time. This is deliberate: an operator can flip a feature
# flag mid-deployment (e.g. via a rolling restart of the surrounding
# control plane) without restarting the service. The per-request
# read costs ~200 ns and is dominated by the rate-limit + session
# dependency chain that runs before this point.


def _allow_heuristic_fallback() -> bool:
    """Read ``ARANGO_SPARQL_ALLOW_HEURISTIC`` — defaults to ``True``,
    forced ``False`` in :data:`arango_sparql.service.app._PUBLIC_MODE`.

    Opt-out is deliberately verbose (PRD §6.3.4): only an explicit
    known-false value (``false``/``0``/``no``) disables fallback.
    Empty / unrecognised values stay at the safe ``True`` default so
    a typo in deployment YAML does not silently degrade the surface.
    """

    from ..app import _PUBLIC_MODE

    if _PUBLIC_MODE:
        return False
    raw = (os.getenv("ARANGO_SPARQL_ALLOW_HEURISTIC") or "").strip().lower()
    return raw not in ("false", "0", "no")


def _analyzer_required() -> bool:
    """Read ``SCHEMA_ANALYZER_REQUIRED`` — defaults to ``True``.

    Same deliberately-verbose opt-out semantics as
    :func:`_allow_heuristic_fallback` above. Garbage values stay
    at the safe default.
    """

    raw = (os.getenv("SCHEMA_ANALYZER_REQUIRED") or "").strip().lower()
    return raw not in ("false", "0", "no")


# ---------------------------------------------------------------------------
# Bundle → summary projection
# ---------------------------------------------------------------------------


def _summary_from_bundle(bundle: MappingBundle) -> dict[str, Any]:
    """Project a :class:`MappingBundle` into the UI-friendly summary
    shape PRD §6.4 row 3 documents.

    Keeps the projection deterministic — entities and relationships
    are sorted by name so two callers (UI, planner-cache) receive
    byte-identical output for the same bundle.
    """

    physical = bundle.physical_mapping or {}
    entities_raw = physical.get("entities") or {}
    relationships_raw = physical.get("relationships") or {}

    entities: list[dict[str, Any]] = []
    rpt_collections: list[dict[str, Any]] = []
    for label in sorted(entities_raw):
        spec = entities_raw[label]
        if not isinstance(spec, dict):
            continue
        style = spec.get("style", "COLLECTION")
        entry: dict[str, Any] = {
            "label": label,
            "style": style,
            "collection": spec.get("collectionName") or spec.get("triplesCollection"),
            "property_count": _count_properties(spec),
        }
        if style == "LABEL":
            entry["typeField"] = spec.get("typeField")
            entry["typeValue"] = spec.get("typeValue")
        if style == "RPT":
            rpt_collections.append(
                {
                    "label": label,
                    "triplesCollection": spec.get("triplesCollection"),
                    "subjectColumn": spec.get("subjectColumn"),
                    "predicateColumn": spec.get("predicateColumn"),
                    "objectUriColumn": spec.get("objectUriColumn"),
                    "objectValueColumn": spec.get("objectValueColumn"),
                    "rptCoverage": spec.get("rptCoverage"),
                }
            )
        entities.append(entry)

    relationships: list[dict[str, Any]] = []
    for rtype in sorted(relationships_raw):
        spec = relationships_raw[rtype]
        if not isinstance(spec, dict):
            continue
        relationships.append(
            {
                "type": rtype,
                "style": spec.get("style", "DEDICATED_COLLECTION"),
                "edgeCollection": spec.get("edgeCollectionName"),
                "domain": spec.get("domain") or spec.get("fromEntity"),
                "range": spec.get("range") or spec.get("toEntity"),
                "typeField": spec.get("typeField"),
                "typeValue": spec.get("typeValue"),
            }
        )

    return {
        "entities": entities,
        "relationships": relationships,
        "rpt_collections": rpt_collections,
        "entity_count": len(entities),
        "relationship_count": len(relationships),
    }


def _count_properties(spec: dict[str, Any]) -> int:
    """Number of mapped properties for an entity spec, or ``0`` when
    the spec carries no ``properties`` block.
    """

    props = spec.get("properties")
    if isinstance(props, dict):
        return len(props)
    if isinstance(props, list):
        return len(props)
    return 0


def _bundle_source_dict(bundle: MappingBundle) -> dict[str, Any] | None:
    """Render :attr:`MappingBundle.source` as a plain dict for the
    JSON response, or ``None`` when no provenance was stamped.
    """

    if bundle.source is None:
        return None
    return {"kind": bundle.source.kind, "notes": bundle.source.notes}


def _last_acquired_at(bundle: MappingBundle) -> str | None:
    """Pull the acquisition timestamp from bundle metadata. Two
    keys are checked because the heuristic builder uses
    ``timestamp`` while the acquire layer stamps ``acquisitionTimestamp``.
    """

    metadata = bundle.metadata or {}
    return metadata.get("acquisitionTimestamp") or metadata.get("timestamp") or metadata.get("acquiredAt")


# ---------------------------------------------------------------------------
# Cache-aware acquisition orchestration
# ---------------------------------------------------------------------------


def _strategy_or_422(value: str) -> Strategy:
    """Coerce a query-string ``strategy`` into the Literal type the
    acquire layer accepts. Maps invalid values to a 422 with a
    stable code so the UI can surface the supported set.
    """

    if value not in ("auto", "analyzer", "heuristic"):
        raise HTTPException(
            status_code=422,
            detail={
                "error": (f"strategy must be 'auto', 'analyzer', or 'heuristic', got {value!r}"),
                "code": "E_SCHEMA_STRATEGY_INVALID",
            },
        )
    return value  # type: ignore[return-value]


def _scoped_cache_key(db_name: str, graph_name: str | None) -> str:
    """Cache key for *db_name* under an optional named-graph scope.

    Unscoped (``graph_name is None``) keeps the bare ``db_name`` so the
    full-DB bundle stays addressable by every existing caller. A scoped
    view gets a distinct ``"<db>::graph::<name>"`` slot so binding /
    unbinding a graph never serves the wrong bundle from L1.
    """
    if not graph_name:
        return db_name
    return f"{db_name}::graph::{graph_name}"


def _build_and_cache_full_bundle(
    db: Any,
    *,
    strategy: Strategy = "auto",
    fingerprints: tuple[str | None, str | None] | None = None,
) -> MappingBundle:
    """Analyze the WHOLE database and store the result in the cache.

    The one place a full analysis runs. Always builds unscoped and with
    inline OWL: a named-graph scope is a cheap filter of this bundle
    (:func:`schema.graph_scope.scope_bundle_to_graph`), so one analysis serves
    every scope, and the OWL export is cheap next to the physical scan.
    Called synchronously by explicit paths (``force-reacquire``, the
    heuristic strategy) and in the background by
    :mod:`arango_sparql.service.schema_warm`. *fingerprints* passes live
    ``(shape, counts)`` fingerprints a caller just took.
    """

    # The live fingerprints are taken *before* the analysis, so a change that
    # lands while it runs is caught by the next drift check rather than
    # recorded as already analyzed.
    db_shape, db_counts = (
        fingerprints if fingerprints is not None else (db_shape_fingerprint(db), db_counts_fingerprint(db))
    )
    bundle = acquire_mapping_bundle(
        db,
        include_owl=True,
        strategy=strategy,
        force_refresh=True,
        graph_name=None,
    )
    db_name = getattr(db, "name", "") or ""
    if db_name:
        # ``db=`` writes through to the persistent tier, so the next process
        # (a redeploy) serves this analysis instead of repeating it.
        _resolve_schema_cache().put(db_name, bundle, db=db, db_shape=db_shape, db_counts=db_counts)
    return bundle


def _revalidate_or_rebuild(db: Any, *, strategy: Strategy = "auto") -> MappingBundle:
    """The TTL refresh: re-analyze only when the database actually changed.

    PRD §6.3.3's two-fingerprint policy — the slow path of
    ``arango-cypher-py``'s ``get_mapping``. The live shape and counts
    fingerprints (seconds) are compared with the ones stored with the cached
    bundle:

    * both match → the cached bundle is re-stamped and kept (no analysis);
    * only the counts differ → statistics are recounted on the cached mapping
      (:func:`~arango_sparql.schema.acquire.refresh_statistics`);
    * the shape differs, or there is nothing trustworthy to compare (no entry,
      an entry another build produced, no live fingerprints) → full analysis.

    Runs in the background (``schema_warm``), so the stale bundle is served
    throughout.
    """

    db_name = getattr(db, "name", "") or ""
    cache = _resolve_schema_cache()
    entry = cache.peek(db_name, db=db)[0] if db_name else None
    if entry is None or not entry.is_from_current_code() or entry.db_shape is None:
        return _build_and_cache_full_bundle(db, strategy=strategy)

    shape, counts = db_shape_fingerprint(db), db_counts_fingerprint(db)
    if shape is None:
        logger.info("schema drift check: no live fingerprint for db=%s; re-analyzing", db_name)
        return _build_and_cache_full_bundle(db, strategy=strategy, fingerprints=(shape, counts))
    drift = classify_live_drift(
        stored_shape=entry.db_shape,
        stored_counts=entry.db_counts,
        current_shape=shape,
        current_counts=counts,
    )
    logger.info("schema drift check: db=%s result=%s", db_name, drift.value)
    if drift is FingerprintDrift.SHAPE_CHANGED:
        return _build_and_cache_full_bundle(db, strategy=strategy, fingerprints=(shape, counts))
    if drift is FingerprintDrift.UNCHANGED:
        cache.revalidated(db_name, db=db, db_counts=counts)
        return entry.bundle
    bundle = refresh_statistics(db, entry.bundle)
    cache.put(db_name, bundle, db=db, db_shape=shape, db_counts=counts)
    return bundle


def _scoped(db: Any, bundle: MappingBundle, graph_name: str | None) -> MappingBundle:
    """The session's view of a full-DB *bundle* (no-op when unscoped)."""
    return scope_bundle_to_graph(db, bundle, graph_name) if graph_name else bundle


def _usable(entry: Any, *, include_owl: bool) -> bool:
    # The cache key does not encode ``include_owl``: a bundle cached without
    # OWL (e.g. by an older force-reacquire) cannot satisfy an OWL request.
    return entry is not None and not (include_owl and entry.bundle.owl_turtle is None)


def _get_or_acquire(
    db: Any,
    *,
    force: bool,
    strategy: Strategy,
    include_owl: bool = False,
    graph_name: str | None = None,
) -> tuple[MappingBundle, bool]:
    """SYNCHRONOUS read-or-build: ``(bundle, cache_hit)`` for *db*.

    Blocks for a full analysis on a miss, so the interactive endpoints use
    :func:`_read_or_warm` instead; this remains for the explicit, opt-in
    slow paths (``POST /schema/force-reacquire``) and the heuristic
    strategy (fast — no analyzer). The cache holds the full-DB bundle under
    ``db.name``; a named-graph scope is derived from it on every read.
    """

    db_name = getattr(db, "name", "") or ""
    if not force and db_name:
        entry = _resolve_schema_cache().get(db_name, db=db)
        if _usable(entry, include_owl=include_owl):
            return _scoped(db, entry.bundle, graph_name), True
    return _scoped(db, _build_and_cache_full_bundle(db, strategy=strategy), graph_name), False


def _read_or_warm(
    db: Any,
    *,
    strategy: Strategy,
    include_owl: bool = False,
    graph_name: str | None = None,
    force: bool = False,
    background_db: Callable[[], Any] | None = None,
) -> tuple[MappingBundle | None, bool, bool]:
    """NON-BLOCKING read: ``(bundle | None, cache_hit, warming)``.

    Serves the cached full-DB bundle (scoped to *graph_name*) and never runs
    the analyzer on the request path — the catalog model ported from
    arango-cypher-py (``service/schema_warm.py``):

    * miss → start a background analysis, return ``(None, False, True)``;
      the caller answers ``pending`` and the client retries;
    * TTL-expired entry → serve it (stale-while-revalidate) and check it
      against the live database in the background
      (:func:`_revalidate_or_rebuild`): a full re-analysis only if the
      database's structure changed;
    * ``force`` ("Refresh schema") → refresh in the background while still
      serving the current entry, with ``warming=True`` until it lands;
    * an entry another build of the acquisition code produced (the stored
      copy a previous release left, PRD §6.3.3) → served, and replaced in
      the background, so a release's schema fixes take effect.

    An L1 miss first reads the persistent tier, so the first request after a
    restart is served from the stored analysis instead of starting one.

    A background failure is re-raised once to the next caller (so the
    route's existing error mapping applies) instead of leaving the client
    polling forever. ``strategy="heuristic"`` is fast, so it stays inline.

    *background_db* gives the handle a new background analysis runs on (a
    platform session's user, on a token that outlives the request; see
    :func:`~arango_sparql.service.security.background_database`). It is
    called only when a warm actually starts, since clients poll while one runs.
    """

    if strategy == "heuristic":
        bundle, hit = _get_or_acquire(
            db, force=force, strategy=strategy, include_owl=include_owl, graph_name=graph_name
        )
        return bundle, hit, False

    db_name = getattr(db, "name", "") or ""
    if not db_name:
        bundle, hit = _get_or_acquire(
            db, force=force, strategy=strategy, include_owl=include_owl, graph_name=graph_name
        )
        return bundle, hit, False

    cache = _resolve_schema_cache()
    entry, expired = cache.peek(db_name, db=db)
    usable = _usable(entry, include_owl=include_owl)
    outdated = entry is not None and not entry.is_from_current_code()
    # A cache hit means "served what was already cached"; a bundle that this
    # call's own warm just built (inline, under test) is a fresh acquisition.
    cache_hit = usable and not force
    if force or expired or outdated or not usable:
        # Only the TTL ran out → a drift check suffices; anything else needs
        # a full analysis.
        revalidate = not (force or outdated or not usable)
        failure = schema_warm.take_error(db_name)
        if failure is not None and not usable:
            raise failure
        if not schema_warm.is_warming(db_name):
            warm_db = background_db() if background_db is not None else db
            schema_warm.schedule_warm(warm_db, strategy=strategy, revalidate=revalidate)
        elif not revalidate:
            # A drift check in flight must not absorb a requested re-analysis.
            schema_warm.request_rebuild(db_name)
        # A warm may complete inline (the test seam); re-read so the caller
        # sees a bundle that is already there. A revalidation keeps the same
        # bundle, so it is still a cache hit.
        refreshed, _ = cache.peek(db_name)
        if refreshed is None or entry is None or refreshed.bundle is not entry.bundle:
            cache_hit = False
        entry = refreshed
        usable = _usable(entry, include_owl=include_owl)
        if not usable:
            failure = schema_warm.take_error(db_name)
            if failure is not None:
                raise failure
    warming = schema_warm.is_warming(db_name)
    if not usable:
        return None, False, warming
    return _scoped(db, entry.bundle, graph_name), cache_hit, warming


def _pending_warning(warming: bool) -> dict[str, Any]:
    """The ``info`` note a ``pending`` schema response carries (cypher parity)."""
    return {
        "code": "SCHEMA_PENDING",
        "severity": "info",
        "message": (
            "Schema for this database is being analyzed in the background — "
            "this can take several minutes on a large database. Retry shortly."
            if warming
            else "Schema for this database has not been analyzed yet. Retry shortly."
        ),
    }


def _enforce_force_reacquire_policy() -> None:
    """PRD §6.3.4 row 4 — when both opt-outs are off, ``/schema/force-
    reacquire`` cannot serve any path and must return 503. Done here
    so the policy is enforced at the route boundary, not buried
    inside the acquire layer (which has no way to surface a 503).
    """

    if not _analyzer_required() and not _allow_heuristic_fallback():
        raise HTTPException(
            status_code=503,
            detail={
                "error": (
                    "Schema acquisition is disabled in this deployment. "
                    "Both SCHEMA_ANALYZER_REQUIRED and "
                    "ARANGO_SPARQL_ALLOW_HEURISTIC are set to false; "
                    "no acquisition path is available. Push a mapping "
                    "via POST /mapping/import-owl instead."
                ),
                "code": "E_SCHEMA_UNAVAILABLE",
            },
        )


# ---------------------------------------------------------------------------
# 1) GET /schema/introspect
# ---------------------------------------------------------------------------


@app.get("/schema/introspect", response_model=SchemaIntrospectResponse)
def schema_introspect(
    force: bool = False,
    strategy: str = "auto",
    include_owl: bool = False,
    _: None = Depends(_check_compute_rate_limit),
    session: _Session = Depends(_get_session),
) -> SchemaIntrospectResponse:
    """Serve the connected database's schema from the cache — never block
    on the analyzer (catalog model, ``service/schema_warm.py``).

    A database not yet analyzed answers ``status="pending"`` immediately and
    a background analysis starts; the client retries. ``force=true``
    ("Refresh schema") re-analyzes in the background while the current
    schema keeps being served with ``warming=true``. ``strategy`` ∈
    ``{auto, analyzer, heuristic}``; invalid values are 422 (``heuristic``
    is fast and stays synchronous).

    When ``include_owl=true`` the acquired mapping carries its inline
    OWL/Turtle in ``mapping.owlTurtle`` so the UI can auto-populate the
    ontology editor on connect — without it, callers would have to hand
    the ontology back in on every ``/translate``.
    """

    typed_strategy = _strategy_or_422(strategy)
    t0 = time.perf_counter()

    try:
        bundle, cache_hit, warming = _read_or_warm(
            session.db,
            background_db=lambda: background_database(session),
            force=force,
            strategy=typed_strategy,
            include_owl=include_owl,
            graph_name=getattr(session, "graph_name", None),
        )
    except AnalyzerNotInstalledError as exc:
        # Explicit ``strategy=analyzer`` on a deployment without the
        # analyzer extra. This is operator error (the request asked
        # for a path the deployment doesn't offer) — return 503 with
        # the install hint so the UI can render an actionable banner.
        log_endpoint_timing(
            "/schema/introspect",
            round((time.perf_counter() - t0) * 1000, 1),
            status="error",
            code="E_ANALYZER_NOT_INSTALLED",
        )
        raise HTTPException(
            status_code=503,
            detail={
                "error": _sanitize_error(str(exc)),
                "code": "E_ANALYZER_NOT_INSTALLED",
                "install_hint": ANALYZER_INSTALL_HINT,
            },
        ) from exc
    except MappingError as exc:
        # Bundle wire-shape validation failed — the analyzer
        # produced something we cannot consume. Surface as 422 with
        # the typed code so the UI can distinguish from transport-
        # layer errors.
        raise HTTPException(
            status_code=422,
            detail={
                "error": _sanitize_error(str(exc)),
                "code": exc.code,
            },
        ) from exc

    elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)
    if bundle is None:
        log_endpoint_timing(
            "/schema/introspect",
            elapsed_ms,
            force=force,
            strategy=typed_strategy,
            status="pending",
            warming=warming,
        )
        return SchemaIntrospectResponse(
            status="pending",
            warming=warming,
            warnings=[_pending_warning(warming)],
            elapsed_ms=elapsed_ms,
        )
    summary = _summary_from_bundle(bundle)
    warnings = (bundle.metadata or {}).get("warnings") or []
    log_endpoint_timing(
        "/schema/introspect",
        elapsed_ms,
        force=force,
        strategy=typed_strategy,
        status="ready",
        warming=warming,
        cache_hit=cache_hit,
        entities=summary["entity_count"],
        relationships=summary["relationship_count"],
        warnings=len(warnings),
        source=(bundle.source.kind if bundle.source is not None else "unknown"),
    )
    return SchemaIntrospectResponse(
        mapping=mapping_to_wire_dict(bundle),
        summary=summary,
        warnings=warnings,
        source=_bundle_source_dict(bundle),
        cache_hit=cache_hit,
        elapsed_ms=elapsed_ms,
        status="ready",
        warming=warming,
        editor_vocab=_editor_vocab_for(bundle, db_name=getattr(session.db, "name", "") or ""),
    )


def _editor_vocab_for(bundle: MappingBundle, *, db_name: str) -> EditorVocabV1 | None:
    """The workbench editor's vocabulary for *bundle* (issue #68).

    Its base is the bundle's own default (``:``) namespace; the workbench
    rebases it onto the ontology it sends with NL requests, which the NL
    route reads first (``routes/nl.py``). ``None`` when the bundle's ontology
    names no default namespace (a heuristic bundle, which carries no OWL):
    the editor then completes keywords, variables and prefixes only. An
    enrichment, like the NL schema summary: a failure is logged and the
    schema is still served.
    """
    namespace = default_namespace(bundle.owl_turtle)
    if not namespace:
        return None
    try:
        return editor_vocab(bundle, namespace=namespace, source=f"{ARANGODB_KIND}:{db_name}")
    except Exception as exc:  # noqa: BLE001 — enrichment only; the schema still serves
        logger.warning("editor vocabulary skipped for database %r: %s", db_name, exc)
        return None


# ---------------------------------------------------------------------------
# 1b) GET /schema/owl
# ---------------------------------------------------------------------------


@app.get("/schema/owl", response_model=OwlSchemaResponse)
def schema_owl(
    force: bool = False,
    strategy: str = "auto",
    _: None = Depends(_check_compute_rate_limit),
    session: _Session = Depends(_get_session),
) -> OwlSchemaResponse:
    """OWL schema-graph projection of the connected database.

    Acquires (or reuses the cached) :class:`MappingBundle` for the
    session's database, serialises it to OWL/Turtle via
    :func:`mapping_to_turtle`, and projects it into the
    ``{classes, properties}`` shape the UI's GRAPH tab renders (the same
    shape the frontend's client-side ``n3`` parser produces for in-editor
    ontologies). Acquisition, cache, and ``strategy`` semantics mirror
    ``/schema/introspect``.
    """

    typed_strategy = _strategy_or_422(strategy)
    t0 = time.perf_counter()

    try:
        bundle, cache_hit, warming = _read_or_warm(
            session.db,
            background_db=lambda: background_database(session),
            force=force,
            strategy=typed_strategy,
            include_owl=True,
            graph_name=getattr(session, "graph_name", None),
        )
    except AnalyzerNotInstalledError as exc:
        log_endpoint_timing(
            "/schema/owl",
            round((time.perf_counter() - t0) * 1000, 1),
            status="error",
            code="E_ANALYZER_NOT_INSTALLED",
        )
        raise HTTPException(
            status_code=503,
            detail={
                "error": _sanitize_error(str(exc)),
                "code": "E_ANALYZER_NOT_INSTALLED",
                "install_hint": ANALYZER_INSTALL_HINT,
            },
        ) from exc
    except MappingError as exc:
        raise HTTPException(
            status_code=422,
            detail={"error": _sanitize_error(str(exc)), "code": exc.code},
        ) from exc

    if bundle is None:
        elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)
        log_endpoint_timing("/schema/owl", elapsed_ms, force=force, status="pending", warming=warming)
        return OwlSchemaResponse(
            status="pending",
            warming=warming,
            warnings=[_pending_warning(warming)],
            elapsed_ms=elapsed_ms,
        )

    try:
        turtle = mapping_to_turtle(bundle)
        view = owl_graph_view(turtle)
    except (OwlParseError, OwlBombError) as exc:
        # The Turtle we just serialised could not be re-parsed into the
        # graph view — this is an internal-consistency failure, but it is
        # driven by the (possibly customer-supplied) ontology, so surface
        # it as a 422 with the typed code rather than a 500.
        log_endpoint_timing(
            "/schema/owl",
            round((time.perf_counter() - t0) * 1000, 1),
            status="error",
            code=exc.code,
        )
        raise HTTPException(
            status_code=422,
            detail={"error": _sanitize_error(str(exc)), "code": exc.code},
        ) from exc
    except MappingError as exc:
        raise HTTPException(
            status_code=422,
            detail={"error": _sanitize_error(str(exc)), "code": exc.code},
        ) from exc

    elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)
    warnings = (bundle.metadata or {}).get("warnings") or []
    log_endpoint_timing(
        "/schema/owl",
        elapsed_ms,
        force=force,
        strategy=typed_strategy,
        cache_hit=cache_hit,
        classes=len(view["classes"]),
        properties=len(view["properties"]),
        warnings=len(warnings),
        source=(bundle.source.kind if bundle.source is not None else "unknown"),
    )
    return OwlSchemaResponse(
        classes=view["classes"],
        properties=view["properties"],
        turtle=turtle,
        source=_bundle_source_dict(bundle),
        warnings=warnings,
        elapsed_ms=elapsed_ms,
        status="ready",
        warming=warming,
    )


# ---------------------------------------------------------------------------
# 2) GET /schema/properties
# ---------------------------------------------------------------------------

# Cap on the per-call sample size. Keeps a malicious / sloppy caller
# from asking for millions of docs in one round-trip; the underlying
# AQL still uses LIMIT @n so the server-side cost is bounded too.
# Held as a module constant so an operator can monkey-patch one
# value rather than scattering magic numbers.
_PROPERTIES_SAMPLE_HARD_CAP: int = 1000


@app.get("/schema/properties", response_model=SchemaPropertiesResponse)
def schema_properties(
    collection: str,
    sample_size: int = 100,
    _: None = Depends(_check_compute_rate_limit),
    session: _Session = Depends(_get_session),
) -> SchemaPropertiesResponse:
    """Per-collection inferred property catalog. Samples up to
    *sample_size* documents (capped at 1000) and returns
    ``{<attr>: {field, type, required}}``.
    """

    if not collection or not isinstance(collection, str):
        raise HTTPException(
            status_code=422,
            detail={
                "error": "collection query parameter is required",
                "code": "E_SCHEMA_BAD_COLLECTION",
            },
        )
    capped = max(1, min(sample_size, _PROPERTIES_SAMPLE_HARD_CAP))

    t0 = time.perf_counter()
    properties = _sample_properties(session.db, collection, capped)
    log_endpoint_timing(
        "/schema/properties",
        round((time.perf_counter() - t0) * 1000, 1),
        collection=collection,
        sample_size=capped,
        properties=len(properties),
    )
    return SchemaPropertiesResponse(
        collection=collection,
        sample_size=capped,
        properties=properties,
    )


def _sample_properties(db: Any, collection_name: str, sample_size: int) -> dict[str, dict[str, Any]]:
    """Sample documents from *collection_name* and infer property
    name + dominant type + required-flag.

    Mirrors the sister project's helper. Underscore-prefixed
    fields (``_key``, ``_id``, ``_rev``, ``_from``, ``_to``) are
    skipped because they are ArangoDB-managed metadata, not
    user-visible attributes.
    """

    try:
        cursor = db.aql.execute(
            "FOR doc IN @@col LIMIT @n RETURN doc",
            bind_vars={"@col": collection_name, "n": sample_size},
        )
        docs = list(cursor)
    except Exception as exc:
        logger.info("sample failed for collection %r: %s", collection_name, exc)
        return {}

    if not docs:
        return {}

    field_types: dict[str, dict[str, int]] = {}
    presence: dict[str, int] = {}
    samples: dict[str, Any] = {}

    for doc in docs:
        if not isinstance(doc, dict):
            continue
        for key, val in doc.items():
            if key.startswith("_"):
                continue
            type_buckets = field_types.setdefault(key, {})
            t = _infer_type(val)
            type_buckets[t] = type_buckets.get(t, 0) + 1
            presence[key] = presence.get(key, 0) + 1
            samples.setdefault(key, val)

    out: dict[str, dict[str, Any]] = {}
    total = len(docs)
    for name, types in field_types.items():
        dominant = max(types, key=types.get)  # type: ignore[arg-type]
        out[name] = {
            "field": name,
            "type": dominant,
            "required": presence.get(name, 0) == total,
            "sample": samples.get(name),
        }
    return out


def _infer_type(val: Any) -> str:
    if val is None:
        return "null"
    if isinstance(val, bool):
        return "boolean"
    if isinstance(val, int | float):
        return "number"
    if isinstance(val, str):
        return "string"
    if isinstance(val, list):
        return "array"
    if isinstance(val, dict):
        return "object"
    return "string"


# ---------------------------------------------------------------------------
# 3) GET /schema/summary  (no DB access)
# ---------------------------------------------------------------------------


def _schema_summary_impl(
    req: SchemaSummaryRequest,
) -> SchemaSummaryResponse:
    """Body of the ``/schema/summary`` GET and POST handlers. Split
    out so the two route registrations share one implementation
    while keeping unique FastAPI operation IDs (which the OpenAPI
    spec requires).
    """

    if not req.mapping and not req.ontology_ttl:
        raise HTTPException(
            status_code=422,
            detail={
                "error": ("Either 'mapping' (wire dict) or 'ontology_ttl' (Turtle string) must be supplied."),
                "code": "E_SCHEMA_SUMMARY_EMPTY",
            },
        )

    t0 = time.perf_counter()
    try:
        bundle = _summary_input_to_bundle(req)
    except MappingError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "error": _sanitize_error(str(exc)),
                "code": exc.code,
            },
        ) from exc
    except Exception as exc:
        # Turtle parse failures land here — surface with a typed
        # code so the UI can distinguish "your TTL is malformed"
        # from "we don't recognise that mapping shape".
        raise HTTPException(
            status_code=422,
            detail={
                "error": _sanitize_error(f"failed to parse mapping input: {exc}"),
                "code": "E_SCHEMA_SUMMARY_PARSE",
            },
        ) from exc

    summary = _summary_from_bundle(bundle)
    log_endpoint_timing(
        "/schema/summary",
        round((time.perf_counter() - t0) * 1000, 1),
        entities=summary["entity_count"],
        relationships=summary["relationship_count"],
        rpt_collections=len(summary["rpt_collections"]),
    )
    return SchemaSummaryResponse(**summary)


@app.get(
    "/schema/summary",
    response_model=SchemaSummaryResponse,
    operation_id="schema_summary_get",
)
def schema_summary_get(
    req: SchemaSummaryRequest,
    _: None = Depends(_check_compute_rate_limit),
) -> SchemaSummaryResponse:
    """Conceptual summary of a client-supplied mapping (GET). **No
    DB access** — the only route in this surface that does not
    require a session. PRD §6.4 row 3 specifies GET-with-body; HTTP
    proxies that strip GET bodies should use the POST form below.
    """

    return _schema_summary_impl(req)


@app.post(
    "/schema/summary",
    response_model=SchemaSummaryResponse,
    operation_id="schema_summary_post",
)
def schema_summary_post(
    req: SchemaSummaryRequest,
    _: None = Depends(_check_compute_rate_limit),
) -> SchemaSummaryResponse:
    """Conceptual summary of a client-supplied mapping (POST). Same
    semantics as the GET form; offered for clients whose HTTP stack
    rejects GET bodies (most JS fetch implementations).
    """

    return _schema_summary_impl(req)


def _summary_input_to_bundle(req: SchemaSummaryRequest) -> MappingBundle:
    """Build a :class:`MappingBundle` from whichever input shape
    *req* supplies. Wire-dict wins when both are present (mirrors
    the precedence the translate route uses for ``ontology_ttl`` vs
    ``mapping``, just inverted because the route's primary input
    here is the JSON wire dict).
    """

    if req.mapping:
        return mapping_from_wire_dict(req.mapping)

    # ontology_ttl path: parse Turtle into a SchemaResolver first,
    # then ask it to synthesize a (small) MappingBundle for the
    # summary projection. Implemented inline to avoid a dependency
    # on the not-yet-built /mapping/import-owl machinery (Slice 7).
    from rdflib import Graph

    from ...translate.resolver import SchemaResolver

    graph = Graph()
    graph.parse(data=req.ontology_ttl or "", format="turtle")
    # We only need the bundle for projection; an empty resolver
    # would lose all the OWL annotations that drive the summary.
    # Round-trip through the resolver's own conceptual surface —
    # currently the conservative path is to wrap the parsed Turtle
    # back into a bundle whose owl_turtle field carries the source.
    SchemaResolver(ontology=graph)  # parse-validation side effect
    return MappingBundle(
        conceptual_schema={"entities": [], "relationships": []},
        physical_mapping={"entities": {}, "relationships": {}},
        metadata={"source": "summary_from_turtle_only"},
        owl_turtle=req.ontology_ttl,
    )


# ---------------------------------------------------------------------------
# 4) GET /schema/statistics
# ---------------------------------------------------------------------------


@app.get("/schema/statistics", response_model=SchemaStatisticsResponse)
def schema_statistics(
    _: None = Depends(_check_compute_rate_limit),
    session: _Session = Depends(_get_session),
) -> SchemaStatisticsResponse:
    """Surface the analyzer-supplied ``metadata.statistics`` block
    from the cached / freshly-acquired bundle.

    When statistics are absent (heuristic-only bundle, or analyzer
    skipped them), ``available=false`` and an empty ``statistics``
    dict are returned. Live recomputation is deferred to a follow-
    up slice — PRD §6.4 row 4 is satisfied by surfacing the
    block as-is.
    """

    t0 = time.perf_counter()
    try:
        bundle, _hit, warming = _read_or_warm(
            session.db,
            background_db=lambda: background_database(session),
            strategy="auto",
            graph_name=getattr(session, "graph_name", None),
        )
    except AnalyzerNotInstalledError as exc:
        raise HTTPException(
            status_code=503,
            detail={
                "error": _sanitize_error(str(exc)),
                "code": "E_ANALYZER_NOT_INSTALLED",
                "install_hint": ANALYZER_INSTALL_HINT,
            },
        ) from exc

    if bundle is None:
        log_endpoint_timing(
            "/schema/statistics",
            round((time.perf_counter() - t0) * 1000, 1),
            status="pending",
            warming=warming,
        )
        return SchemaStatisticsResponse(status="pending", warming=warming)

    stats = (bundle.metadata or {}).get("statistics") or {}
    available = bool(isinstance(stats, dict) and (stats.get("relationships") or stats.get("entities")))
    log_endpoint_timing(
        "/schema/statistics",
        round((time.perf_counter() - t0) * 1000, 1),
        available=available,
    )
    return SchemaStatisticsResponse(
        statistics=stats if isinstance(stats, dict) else {},
        available=available,
        last_acquired_at=_last_acquired_at(bundle),
        status="ready",
        warming=warming,
    )


# ---------------------------------------------------------------------------
# 5) GET /schema/status
# ---------------------------------------------------------------------------


@app.get("/schema/status", response_model=SchemaStatusResponse)
def schema_status(
    session: _Session = Depends(_get_session),
) -> SchemaStatusResponse:
    """Cheap drift report: the live database's two analyzer fingerprints
    against the ones stored with the cached bundle when it was analyzed
    (PRD §6.3.3) — the comparison the background drift check acts on.

    Status values: ``unchanged``, ``stats_only``, ``shape_changed``,
    ``no_cache``. Without fingerprints on either side (analyzer missing, a
    transient error, a bundle stored without them) the report degrades to
    ``unchanged`` rather than claim a change it cannot see.

    Before 2026-10-13 this compared the live fingerprints with a hash of the
    *bundle* — a different hash domain — so with the analyzer installed it
    reported ``shape_changed`` for every cached schema.
    """

    t0 = time.perf_counter()
    cache = _resolve_schema_cache()
    db_name = getattr(session.db, "name", "") or ""

    # peek, not get: an entry past its TTL is still the cached schema.
    cached_entry = cache.peek(db_name, db=session.db)[0] if db_name else None

    current_shape = db_shape_fingerprint(session.db)
    current_counts = db_counts_fingerprint(session.db)

    if cached_entry is None:
        status = "no_cache"
        cached_block = SchemaFingerprintBlock()
        unchanged = False
        needs_full = True
    else:
        cached_block = SchemaFingerprintBlock(shape=cached_entry.db_shape, counts=cached_entry.db_counts)
        if current_shape is None or cached_entry.db_shape is None:
            # Nothing to compare — degrade to "we have a cache but cannot
            # tell if it's stale". Treat as unchanged so the UI does not
            # flap, but log it so an operator notices.
            logger.info(
                "schema_status: live or stored fingerprints unavailable; reporting cache as stable for db=%r",
                db_name,
            )
            status = "unchanged"
            unchanged = True
            needs_full = False
        else:
            drift = classify_live_drift(
                stored_shape=cached_entry.db_shape,
                stored_counts=cached_entry.db_counts,
                current_shape=current_shape,
                current_counts=current_counts,
            )
            status = drift.value
            unchanged = drift is FingerprintDrift.UNCHANGED
            needs_full = drift is FingerprintDrift.SHAPE_CHANGED

    last_acquired = _last_acquired_at(cached_entry.bundle) if cached_entry is not None else None

    log_endpoint_timing(
        "/schema/status",
        round((time.perf_counter() - t0) * 1000, 1),
        report_status=status,
        unchanged=unchanged,
        has_cache=cached_entry is not None,
    )
    return SchemaStatusResponse(
        status=status,
        unchanged=unchanged,
        needs_full_rebuild=needs_full,
        current=SchemaFingerprintBlock(shape=current_shape, counts=current_counts),
        cached=cached_block,
        last_acquired_at=last_acquired,
        db_name=db_name or None,
    )


# ---------------------------------------------------------------------------
# 6) POST /schema/invalidate-cache
# ---------------------------------------------------------------------------


@app.post(
    "/schema/invalidate-cache",
    response_model=SchemaInvalidateCacheResponse,
)
def schema_invalidate_cache(
    _: None = Depends(_check_compute_rate_limit),
    session: _Session = Depends(_get_session),
) -> SchemaInvalidateCacheResponse:
    """Drop the cached mapping for the connected DB from both tiers.

    ``invalidated`` is true iff either tier held an entry;
    ``persistent_dropped`` iff the stored copy (PRD §6.3.3 L2) was deleted —
    false when there was none, or when this user may not delete it (it is
    then still dropped from memory, and replaced by the next analysis).
    """

    t0 = time.perf_counter()
    cache = _resolve_schema_cache()
    db_name = getattr(session.db, "name", "") or ""
    dropped_l1 = bool(db_name) and cache.invalidate(db_name)
    persistent_dropped = bool(db_name) and cache.invalidate_persistent(session.db)
    invalidated = dropped_l1 or persistent_dropped
    log_endpoint_timing(
        "/schema/invalidate-cache",
        round((time.perf_counter() - t0) * 1000, 1),
        invalidated=invalidated,
        persistent_dropped=persistent_dropped,
        db=db_name or "unknown",
    )
    return SchemaInvalidateCacheResponse(
        invalidated=invalidated,
        db_name=db_name,
        persistent_dropped=persistent_dropped,
    )


# ---------------------------------------------------------------------------
# 7) POST /schema/force-reacquire
# ---------------------------------------------------------------------------


@app.post(
    "/schema/force-reacquire",
    response_model=SchemaForceReacquireResponse,
)
def schema_force_reacquire(
    strategy: str = "auto",
    _: None = Depends(_check_compute_rate_limit),
    session: _Session = Depends(_get_session),
) -> SchemaForceReacquireResponse:
    """Drop the cached mapping and rebuild from scratch.

    PRD §6.4 row 7: returns 503 when both
    ``SCHEMA_ANALYZER_REQUIRED=false`` and
    ``ARANGO_SPARQL_ALLOW_HEURISTIC=false`` (no acquisition path
    is available). When the analyzer extra is missing for an
    explicit ``strategy=analyzer`` request, returns 503 with the
    install hint.
    """

    _enforce_force_reacquire_policy()
    typed_strategy = _strategy_or_422(strategy)

    # Honour the per-request fallback gate: if the operator has
    # disabled heuristic fallback AND the analyzer is missing, we
    # cannot serve any path even when ``strategy="auto"`` would
    # normally degrade to heuristic.
    if typed_strategy == "auto" and not _allow_heuristic_fallback() and not analyzer_available():
        raise HTTPException(
            status_code=503,
            detail={
                "error": (
                    "schema-analyzer is not installed and heuristic "
                    "fallback is disabled "
                    "(ARANGO_SPARQL_ALLOW_HEURISTIC=false). Install "
                    "the analyzer or re-enable the heuristic fallback."
                ),
                "code": "E_ANALYZER_NOT_INSTALLED",
                "install_hint": ANALYZER_INSTALL_HINT,
            },
        )

    t0 = time.perf_counter()
    try:
        bundle, _hit = _get_or_acquire(
            session.db,
            force=True,
            strategy=typed_strategy,
            graph_name=getattr(session, "graph_name", None),
        )
    except AnalyzerNotInstalledError as exc:
        raise HTTPException(
            status_code=503,
            detail={
                "error": _sanitize_error(str(exc)),
                "code": "E_ANALYZER_NOT_INSTALLED",
                "install_hint": ANALYZER_INSTALL_HINT,
            },
        ) from exc
    except MappingError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "error": _sanitize_error(str(exc)),
                "code": exc.code,
            },
        ) from exc

    elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)
    summary = _summary_from_bundle(bundle)
    warnings = (bundle.metadata or {}).get("warnings") or []
    log_endpoint_timing(
        "/schema/force-reacquire",
        elapsed_ms,
        strategy=typed_strategy,
        entities=summary["entity_count"],
        relationships=summary["relationship_count"],
        warnings=len(warnings),
        source=(bundle.source.kind if bundle.source is not None else "unknown"),
    )
    return SchemaForceReacquireResponse(
        mapping=mapping_to_wire_dict(bundle),
        summary=summary,
        warnings=warnings,
        source=_bundle_source_dict(bundle),
        cache_hit=False,
        elapsed_ms=elapsed_ms,
    )


__all__ = [
    "_PROPERTIES_SAMPLE_HARD_CAP",
    "_reset_cache",
    "_resolve_schema_cache",
]
