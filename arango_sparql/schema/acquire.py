"""Mapping acquisition (PRD §6.3.2).

Three tiers, in priority order:

1. **Analyzer (canonical).** Wraps
   :class:`schema_analyzer.AgenticSchemaAnalyzer` (the optional
   ``arangodb-schema-analyzer`` package, version pin
   ``>=0.14.1,<0.18.0``, matching pyproject.toml). Produces a :class:`MappingBundle` that mirrors
   the wire shape PRD §6.2 specifies.
2. **Heuristic (fallback).** Calls
   :func:`arango_sparql.schema.detect.build_heuristic_mapping` (Slice
   4) when the analyzer extra is not installed. Attaches a
   ``W_SCHEMA_HEURISTIC_FALLBACK`` warning so downstream consumers
   (UI, NL→SPARQL planner) can degrade gracefully.
3. **RPT enrichment (always).** Runs
   :func:`arango_sparql.schema.detect.detect_rpt_pattern` on top of
   *whichever* tier produced the bundle and merges any
   ``is_rpt=True`` collections into ``physicalMapping.entities`` with
   ``style="RPT"``. The analyzer alone only knows PG/LPG; this pass
   is what gives us correct routing for legacy ``_triples`` layouts
   even when the analyzer is installed (PRD §6.3.2 step 2). The same
   pass then synthesizes ``RPT_EDGE`` relationships for every object
   property in the triples store via
   :func:`arango_sparql.schema.detect.infer_rpt_object_property_relationships`,
   typing each relationship's ``fromEntity`` / ``toEntity`` from the
   subject's and object's ``rdf:type`` — the cross-collection
   inference neither the analyzer (Cypher-centric) nor the bare RPT
   entity overlay performs.

4. **Edge-endpoint enrichment (always).** Runs
   :func:`_apply_edge_endpoint_enrichment` over whichever bundle the
   tiers above produced, filling relationship ``fromEntity`` /
   ``toEntity`` that are still ``"Any"`` by sampling ``_from`` / ``_to``
   (:func:`arango_sparql.schema.detect.infer_edge_endpoints_from_db`).
   Strictly additive — an endpoint a producer already pinned is never
   overwritten — so it closes the analyzer's cross-collection gap
   without fighting it.

Every tier sees the database through :func:`hide_service_collections`, so
the collections the transpilers keep for themselves in a customer database —
the schema caches (:data:`~arango_sparql.schema.cache.SCHEMA_CACHE_COLLECTIONS`)
and the mined examples (:func:`~arango_sparql.query_mining.store.example_collections`),
this service's and arango-cypher-py's — never become classes in the analyzed
schema (:func:`service_collections`).

Public surface:

* :func:`acquire_mapping_bundle` — the orchestration entry point.
* :func:`analyzer_available` — light probe for the startup guard
  (Slice 6 ``service/app.py`` ``_require_analyzer_unless_opted_out``).
* :func:`db_shape_fingerprint` / :func:`db_counts_fingerprint` —
  thin wrappers around the analyzer's cheap live-DB fingerprints,
  used by the cache layer to decide whether to re-acquire without
  loading a full bundle.

Caching is **not** done here — the route layer (Slice 7) wires a
:class:`SchemaCache` instance and invokes :func:`acquire_mapping_bundle`
on cache miss / drift. Keeping caching out of this module preserves
the "acquisition is a pure function of (db, strategy, include_owl)"
contract that makes the unit tests in ``tests/schema/test_acquire.py``
straightforward.
"""

from __future__ import annotations

import dataclasses
import logging
import os
import re
from datetime import UTC, datetime
from typing import Any, Literal

from arango_sparql.query_mining.store import example_collections
from arango_sparql.schema.cache import SCHEMA_CACHE_COLLECTIONS
from arango_sparql.schema.detect import (
    build_heuristic_mapping,
    detect_rpt_pattern,
    infer_edge_endpoints_from_db,
    infer_rpt_object_property_relationships,
)
from arango_sparql.translate.mapping import (
    MappingBundle,
    MappingSource,
    mapping_from_wire_dict,
)

from .._privacy import tenant_name

logger = logging.getLogger(__name__)

# PRD line 610: when ``strategy="auto"`` falls back to the heuristic
# path because the analyzer extra is unimportable, attach this code
# to ``metadata.warnings`` so the route layer (Slice 7) can surface
# "you should install arangodb-schema-analyzer" without reading
# ``source.kind``. Distinct from :data:`W_SCHEMA_HEURISTIC_FALLBACK`
# (PRD §6.3.4), which is the bundle's own provenance marker — that
# one is attached by the heuristic builder and is present on every
# heuristic bundle, including those produced by an explicit
# ``strategy="heuristic"`` request.
W_ANALYZER_NOT_INSTALLED: str = "ANALYZER_NOT_INSTALLED"

# Code stamped on a warning that arrived as a *bare string* rather than the
# canonical ``{code, message}`` dict. The analyzer (and some of its
# downstream exporters) emit free-text advisories — e.g. "LLM provider not
# configured, falling back to heuristic baseline inference" — as plain
# strings, but every ``warnings`` field in the wire contract
# (``service/models.py``) is typed ``list[dict]``. Coercing at the bundle
# boundary (:func:`_normalize_bundle_warnings`) keeps that invariant so a
# string advisory cannot 500 the response-model validation.
W_ANALYZER_ADVISORY: str = "W_ANALYZER_ADVISORY"

# Kept for backward-import compat — older slices referenced this
# name. Both codes are emitted on the auto-fallback path: the
# provenance marker and the install hint coexist on the same
# bundle.
W_SCHEMA_HEURISTIC_FALLBACK: str = "W_SCHEMA_HEURISTIC_FALLBACK"

# Pin matches pyproject.toml extras and the README install hint.
# When this gets bumped, update both call sites in lockstep.
ANALYZER_VERSION_RANGE: str = ">=0.14.1,<0.18.0"
ANALYZER_INSTALL_HINT: str = f"pip install 'arangodb-schema-analyzer{ANALYZER_VERSION_RANGE}'"

Strategy = Literal["auto", "analyzer", "heuristic"]
_VALID_STRATEGIES: tuple[Strategy, ...] = ("auto", "analyzer", "heuristic")


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------


def acquire_mapping_bundle(
    db: Any,
    *,
    include_owl: bool = False,
    strategy: Strategy = "auto",
    force_refresh: bool = False,
    graph_name: str | None = None,
    now: datetime | None = None,
) -> MappingBundle:
    """Acquire a fresh :class:`MappingBundle` for the live database *db*.

    Parameters
    ----------
    db:
        A duck-typed ``StandardDatabase``-like handle. Must expose
        ``.collections()``, ``.aql.execute(query, bind_vars=...)``,
        and (optionally) ``.name``. The analyzer needs the same
        surface; the heuristic fallback uses only what's listed.
    include_owl:
        When ``True``, asks the analyzer to materialise the OWL
        Turtle representation of the conceptual model. The heuristic
        fallback ignores this flag (its bundles never carry inline
        OWL — PRD §6.3.4 calls this out as a known gap, satisfied
        by ``/mapping/export-owl`` in Slice 7).
    strategy:
        One of ``"auto"`` (analyzer-first, heuristic fallback on
        ``ImportError``), ``"analyzer"`` (raise if missing), or
        ``"heuristic"`` (skip the analyzer entirely; useful for
        smoke tests and air-gapped deployments).
    force_refresh:
        Accepted for forward-compat with the route layer's caching
        wrapper — this function does not cache, so the flag is a
        no-op here. Documented so downstream callers can pass it
        through unchanged.
    graph_name:
        Optional ArangoDB named-graph scope. When set, the fully
        enriched bundle is down-selected to that graph's vertex/edge
        collections (see :func:`schema.graph_scope.scope_bundle_to_graph`)
        before return. ``None`` (default) means "all collections".
    now:
        Injectable clock for the bundle's ``acquisitionTimestamp``
        metadata. Tests pin it; production callers leave ``None``.

    Returns
    -------
    MappingBundle
        A new bundle with the conceptual schema, physical mapping,
        provenance metadata, and (optionally) inline OWL. Always
        post-processed by :func:`_apply_rpt_enrichment` so RPT
        collections show up regardless of which tier ran.

    Raises
    ------
    ValueError
        If *strategy* is not one of the documented values.
    AnalyzerNotInstalledError
        When ``strategy="analyzer"`` is explicitly requested but
        the optional extra is missing.
    """

    if strategy not in _VALID_STRATEGIES:
        raise ValueError(f"strategy must be one of {_VALID_STRATEGIES!r}, got {strategy!r}")

    when = now if now is not None else datetime.now(UTC)
    db = hide_service_collections(db)
    # `force_refresh` is accepted for signature compat with the
    # cache-wrapping orchestrator (PRD signature includes it). The
    # acquisition path itself is stateless so the flag does not
    # change behavior here — but we still log it at DEBUG so a
    # confused operator can see the request landed.
    if force_refresh:
        logger.debug(
            "acquire_mapping_bundle called with force_refresh=True; "
            "this layer does not cache — flag has no effect here"
        )

    bundle = _acquire_via_strategy(
        db,
        strategy=strategy,
        include_owl=include_owl,
        when=when,
    )
    bundle = _apply_rpt_enrichment(db, bundle, when=when)
    bundle = _apply_edge_endpoint_enrichment(db, bundle, when=when)
    bundle = _apply_label_shape_enrichment(db, bundle, when=when)
    bundle = _normalize_bundle_warnings(bundle)
    bundle = _stamp_acquisition_timestamp(bundle, when=when)
    # Named-graph down-select (no-op when graph_name is None). Applied
    # last so the scope sees the fully-enriched bundle (RPT, edge
    # endpoints, OWL) before filtering. See schema/graph_scope.py.
    if graph_name:
        from .graph_scope import scope_bundle_to_graph

        bundle = scope_bundle_to_graph(db, bundle, graph_name)
    return bundle


def service_collections() -> frozenset[str]:
    """Collections the transpilers keep in a customer database for themselves:
    both schema caches and both examples collections (this service's as
    configured). Never user data, so never analyzed or fingerprinted."""

    return SCHEMA_CACHE_COLLECTIONS | example_collections()


class _ServiceCollectionsHidden:
    """A database handle whose ``collections()`` leaves out :func:`service_collections`.

    Every analysis pass enumerates collections through ``db.collections()``
    (the analyzer's snapshot and statistics, this module's RPT and edge
    passes, the heuristic detector), and the analyzer offers no way to
    exclude one. Filtering the enumeration once keeps them out of every
    derived artifact at the same time: classes, shard families, statistics,
    the sharding profile and the OWL export. Everything else is delegated to
    the wrapped handle unchanged.
    """

    def __init__(self, db: Any) -> None:
        self._db = db

    def collections(self) -> Any:
        rows = self._db.collections()
        if not isinstance(rows, list):
            return rows
        hidden = service_collections()
        return [row for row in rows if not (isinstance(row, dict) and row.get("name") in hidden)]

    def __getattr__(self, name: str) -> Any:
        return getattr(self._db, name)


def hide_service_collections(db: Any) -> Any:
    """Return *db* seen without :func:`service_collections` (idempotent)."""

    return db if isinstance(db, _ServiceCollectionsHidden) else _ServiceCollectionsHidden(db)


def refresh_statistics(db: Any, bundle: MappingBundle) -> MappingBundle:
    """Re-derive *bundle*'s ``metadata.statistics`` against *db*, keeping its mapping.

    The stats-only branch of the drift policy (PRD §6.3.3): the live shape
    fingerprint still matches, so the conceptual schema and physical mapping
    are reused and only the cardinality block is recomputed — the same
    ``schema_analyzer.statistics.compute_statistics`` the full analysis runs,
    fed the cheap collection listing it needs instead of a full snapshot
    (mirrors ``schema_analyzer.incremental.refresh_statistics``, which works
    on an ``AnalysisResult`` rather than this repo's enriched bundle).

    Best-effort, like cypher's ``_safe_refresh_statistics``: without the
    analyzer, or when the recount fails, *bundle* is returned unchanged and
    the failure logged — stale statistics never cost the caller its mapping.
    """

    try:
        from schema_analyzer.statistics import compute_statistics
    except ImportError:
        return bundle

    view = hide_service_collections(db)
    try:
        rows = view.collections()
        snapshot = {
            "collections": [
                {"name": row["name"], "type": row.get("type")}
                for row in (rows if isinstance(rows, list) else [])
                if isinstance(row, dict)
                and isinstance(row.get("name"), str)
                and not row["name"].startswith("_")
            ]
        }
        stats = compute_statistics(
            view, snapshot, bundle.physical_mapping or {}, bundle.conceptual_schema or {}
        )
    except Exception:  # noqa: BLE001 — best-effort; the mapping is still valid
        logger.warning(
            "statistics refresh failed for db=%r; keeping the previous statistics",
            tenant_name(getattr(db, "name", "?")),
            exc_info=True,
        )
        return bundle
    if not isinstance(stats, dict):
        return bundle
    metadata = dict(bundle.metadata or {})
    metadata["statistics"] = stats
    metadata["statisticsStatus"] = stats.get("status")
    return dataclasses.replace(bundle, metadata=metadata)


def analyzer_available() -> bool:
    """Return ``True`` iff ``schema_analyzer`` can be imported in
    the current process. Cheap probe — used by:

    * Slice 6's ``_require_analyzer_unless_opted_out`` startup guard.
    * The route layer's ``/schema/info`` capability report.
    * Tests that need to assert behavior under both installed and
      missing-extra conditions.
    """

    try:  # noqa: SIM105 — the explicit shape mirrors the lazy import
        # pattern used everywhere else in this module
        import schema_analyzer  # noqa: F401
    except ImportError:
        return False
    return True


def db_shape_fingerprint(db: Any) -> str | None:
    """Return the analyzer's live shape fingerprint for *db*, or
    ``None`` if the analyzer extra is not installed.

    Thin wrapper around
    :func:`schema_analyzer.fingerprint_physical_shape` that:

    * Excludes the L2 cache collection (PRD §6.3.3) so a freshly-
      written cache row does not invalidate the cache it just landed
      in. This is correctness, not optimization — without the
      exclusion the cache will perma-thrash.
    * Returns ``None`` rather than raising on missing analyzer, so
      the cache layer can degrade to bundle-side fingerprints
      without a try/except in every call site.
    """

    return _live_fingerprint(db, kind="shape")


def db_counts_fingerprint(db: Any) -> str | None:
    """Return the analyzer's live shape+counts fingerprint, or
    ``None`` when the analyzer is not installed.

    Same exclusion semantics as :func:`db_shape_fingerprint`.
    """

    return _live_fingerprint(db, kind="counts")


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class AnalyzerNotInstalledError(RuntimeError):
    """Raised by :func:`acquire_mapping_bundle` when ``strategy="analyzer"``
    is requested but the optional extra is missing.

    Carries the install hint as the message so a user-facing error
    response (Slice 7 routes) can surface it verbatim.
    """

    def __init__(self, *, install_hint: str = ANALYZER_INSTALL_HINT) -> None:
        super().__init__(f"arangodb-schema-analyzer is not installed; install it with: {install_hint}")
        self.install_hint = install_hint


# ---------------------------------------------------------------------------
# Strategy dispatch
# ---------------------------------------------------------------------------


def _acquire_via_strategy(
    db: Any,
    *,
    strategy: Strategy,
    include_owl: bool,
    when: datetime,
) -> MappingBundle:
    """Dispatch to the analyzer or heuristic path based on *strategy*."""

    if strategy == "analyzer":
        if not analyzer_available():
            raise AnalyzerNotInstalledError()
        return _acquire_via_analyzer(db, include_owl=include_owl)

    if strategy == "heuristic":
        return _acquire_via_heuristic(db, when=when)

    # strategy == "auto": analyzer first, heuristic fallback on ImportError.
    if analyzer_available():
        return _acquire_via_analyzer(db, include_owl=include_owl)
    logger.warning(
        "arangodb-schema-analyzer is not installed; falling back to the "
        "heuristic mapping path. Install with: %s",
        ANALYZER_INSTALL_HINT,
    )
    bundle = _acquire_via_heuristic(db, when=when)
    return _attach_warning(
        bundle,
        code=W_ANALYZER_NOT_INSTALLED,
        message=(
            "Mapping built by the heuristic fallback because the "
            "schema-analyzer extra is not installed. Hybrid schemas "
            "(LPG with multi-class collections) may be misclassified."
        ),
        install_hint=ANALYZER_INSTALL_HINT,
    )


# ---------------------------------------------------------------------------
# Analyzer path
# ---------------------------------------------------------------------------


# Client SDK each provider needs; OpenRouter speaks the OpenAI wire protocol
# so it rides the ``openai`` package.
_PROVIDER_SDK_MODULE: dict[str, str] = {
    "openai": "openai",
    "anthropic": "anthropic",
    "openrouter": "openai",
}

# The analyzer's bundled per-provider default model can lag the live API:
# its Anthropic default ``claude-3-5-sonnet-latest`` now 404s. Pin a
# current, verified id so the LLM path works out of the box; override per
# deployment with ``SCHEMA_ANALYZER_MODEL``. Only providers whose default
# we have verified are listed — others fall through to the analyzer's own
# default.
#
# NOTE: the analyzer (v0.9) hard-codes ``temperature`` on every request,
# which the Claude 5 family (Sonnet 5, Opus 4.8) rejects with a 400
# ("temperature is deprecated for this model"). ``claude-haiku-4-5`` is the
# current Anthropic model that still accepts that request shape, so it is
# the safe default until the analyzer stops sending ``temperature``. Track
# that upstream fix; once shipped, this can move to a Claude 5 model.
_DEFAULT_ANALYZER_MODEL: dict[str, str] = {
    "anthropic": "claude-haiku-4-5-20251001",
}


def _analyzer_timeout_ms() -> int:
    """Per-call LLM budget for the analyzer, in milliseconds.

    Overridable via ``SCHEMA_ANALYZER_TIMEOUT_MS``; defaults to 180s (the
    library default of 60s times out on large schemas, silently degrading
    to baseline). Garbage / non-positive values fall back to the default.
    """
    raw = os.getenv("SCHEMA_ANALYZER_TIMEOUT_MS", "")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return 180_000
    return value if value > 0 else 180_000


def _provider_sdk_available(provider: str) -> bool:
    """True iff the client SDK the analyzer needs for *provider* can be
    imported. Uses :func:`importlib.util.find_spec` so we probe without
    paying the import cost or risking import side effects.
    """

    import importlib.util

    module = _PROVIDER_SDK_MODULE.get(provider)
    return bool(module and importlib.util.find_spec(module) is not None)


def _resolve_analyzer_provider() -> str | None:
    """The LLM provider for the schema analyzer — ``None`` (no LLM) by default.

    The analyzer runs its deterministic baseline inference unless an operator
    opts in with ``SCHEMA_ANALYZER_PROVIDER=openai|anthropic|openrouter``. This
    matches ``arango-cypher-py``, whose service always constructs
    ``AgenticSchemaAnalyzer()`` without an LLM.

    Why the default changed (2026-10-08): the analyzer's ``timeout_ms`` is a
    *total* budget shared by its physical scan and its LLM call. On a large
    database the scan alone outlasts it (prod.demo ``IAM``: ~7 min), so the LLM
    call is floored to the analyzer's 1 s minimum, times out three times, and
    falls back to the baseline anyway — the LLM only ever added waiting. The
    generic ``LLM_PROVIDER`` and the API keys belong to the NL pipeline and are
    deliberately *not* inherited here, so configuring NL never switches the
    analyzer back onto the LLM path.

    Trade-off: the baseline can misclassify hybrid / LABEL-style collections
    (e.g. a ``name`` column read as a type discriminator). Operators who need
    the LLM classification on a database small enough for it to finish inside
    ``SCHEMA_ANALYZER_TIMEOUT_MS`` can still opt in explicitly.
    """

    provider = (os.getenv("SCHEMA_ANALYZER_PROVIDER") or "").strip().lower()
    if not provider:
        return None
    if provider not in ("openai", "anthropic", "openrouter"):
        logger.warning(
            "Unknown schema-analyzer provider %r; using deterministic "
            "baseline. Set SCHEMA_ANALYZER_PROVIDER to "
            "openai/anthropic/openrouter, or unset it for no LLM.",
            provider,
        )
        return None
    if not _provider_sdk_available(provider):
        logger.warning(
            "SCHEMA_ANALYZER_PROVIDER=%s but its client SDK is not installed; "
            "the analyzer will degrade to its deterministic baseline.",
            provider,
        )
    return provider


def _acquire_via_analyzer(db: Any, *, include_owl: bool) -> MappingBundle:
    """Run :class:`schema_analyzer.AgenticSchemaAnalyzer` and reshape
    its output into a :class:`MappingBundle`.

    The analyzer returns an ``AnalysisResult`` whose three sub-fields
    (``conceptual_schema``, ``physical_mapping``, ``metadata``) are
    consumed by :func:`schema_analyzer.export_mapping` to produce the
    canonical wire dict that :func:`mapping_from_wire_dict` expects.
    Sister project ``arango-cypher-py`` follows the same pattern;
    keeping the call sequence identical means cross-repo navigation
    is trivial (AGENTS.md §"Mimic, don't invent").
    """

    try:
        from schema_analyzer import AgenticSchemaAnalyzer, export_mapping
        from schema_analyzer.owl_export import (
            export_conceptual_model_as_owl_turtle,
        )
    except ImportError as exc:  # pragma: no cover — caller pre-checks
        raise AnalyzerNotInstalledError() from exc

    # No LLM unless SCHEMA_ANALYZER_PROVIDER opts in (cypher-py parity; see
    # _resolve_analyzer_provider for why). With a provider, the analyzer
    # reads the matching API key itself and degrades to baseline on any
    # provider error, so this stays best-effort.
    provider = _resolve_analyzer_provider()
    model = os.getenv("SCHEMA_ANALYZER_MODEL") or (
        _DEFAULT_ANALYZER_MODEL.get(provider) if provider else None
    )
    if provider:
        logger.info(
            "schema-analyzer using LLM provider=%s%s",
            provider,
            f" model={model}" if model else "",
        )
    else:
        logger.info(
            "schema-analyzer running deterministic baseline (no LLM — the "
            "default; set SCHEMA_ANALYZER_PROVIDER to opt in)"
        )
    analyzer = AgenticSchemaAnalyzer(llm_provider=provider, model=model)
    # ``timeout_ms`` is the analyzer's TOTAL budget (physical scan + LLM call);
    # it only matters when an LLM provider is opted in — the remaining budget
    # after the scan is what the LLM call gets (floored at 1 s by the analyzer).
    # The result is cached by the route layer and built off the request path
    # (service/schema_warm.py), so the cost is paid once per database.
    timeout_ms = _analyzer_timeout_ms()
    analysis_result = analyzer.analyze_physical_schema(db, timeout_ms=timeout_ms)

    analysis_dict = {
        "conceptualSchema": analysis_result.conceptual_schema,
        "physicalMapping": analysis_result.physical_mapping,
        "metadata": _coerce_metadata_to_dict(analysis_result.metadata),
    }

    # The analyzer's only documented export target today is "cypher";
    # the wire shape for SPARQL is identical (PRD §6.2 — both
    # adapters consume the same MappingBundle), so re-using the
    # cypher target is correct, not a workaround.
    export = export_mapping(analysis_dict, target="cypher")

    owl_turtle: str | None = None
    if include_owl:
        try:
            owl_turtle = export_conceptual_model_as_owl_turtle(analysis_dict)
        except Exception:
            # OWL export is best-effort — a bundle without OWL is
            # still useful (the planner reads conceptual+physical
            # directly). We log at WARNING so an operator sees it
            # in the request log without it cascading into a 500.
            logger.warning(
                "schema_analyzer OWL export failed; bundle will lack "
                "owl_turtle. The /mapping/export-owl route will still "
                "work via the local turtle synthesizer.",
                exc_info=True,
            )

    wire_dict = {
        "conceptualSchema": export.get("conceptualSchema", {}),
        "physicalMapping": export.get("physicalMapping", {}),
        "metadata": export.get("metadata", {}),
        "owl_turtle": owl_turtle,
        "source": {
            "kind": "analyzer",
            "notes": "Generated by arangodb-schema-analyzer baseline path",
        },
    }
    return mapping_from_wire_dict(wire_dict)


def _coerce_metadata_to_dict(metadata: Any) -> dict[str, Any]:
    """The analyzer's ``AnalysisResult.metadata`` is a pydantic model
    in v0.6+; older releases returned a plain dict. Handle both
    shapes so a future minor-version bump cannot regress us silently.
    """

    if isinstance(metadata, dict):
        return metadata
    dump = getattr(metadata, "model_dump", None)
    if callable(dump):
        return dump(by_alias=True)
    # Last-ditch: coerce via JSON-able fallback. Loses field aliasing
    # but keeps the bundle structurally valid.
    return dict(metadata) if metadata else {}


# ---------------------------------------------------------------------------
# Heuristic path
# ---------------------------------------------------------------------------


def _acquire_via_heuristic(db: Any, *, when: datetime) -> MappingBundle:
    """Delegate to Slice 4's :func:`build_heuristic_mapping`.

    Wrapped here (rather than re-exported directly) so future
    behavior — e.g. attaching extra warnings, post-filtering — has
    one obvious place to live without churning the detect module.
    """

    return build_heuristic_mapping(db, schema_type="auto", now=when)


# ---------------------------------------------------------------------------
# RPT enrichment (PRD §6.3.2 step 2)
# ---------------------------------------------------------------------------


def _apply_rpt_enrichment(db: Any, bundle: MappingBundle, *, when: datetime) -> MappingBundle:
    """Run the RPT detector over *db* and merge any RPT-classified
    collections into the bundle's physical mapping.

    Why this runs even when the analyzer produced the bundle: the
    analyzer (v0.6.1) classifies into PG / LPG only — it does not
    recognise the legacy Foxx ``_triples`` RPT layout. A bundle
    that has a ``_triples`` collection but no RPT entry will be
    silently routed through the LPG translator, which produces
    wrong (and confusing) AQL. Running detection over the analyzer's
    output costs one extra pass over the user collections and
    closes that gap deterministically.

    Conflict resolution: when the RPT detector and the underlying
    bundle disagree about a collection (e.g. analyzer mapped
    ``_triples`` to ``COLLECTION``), the RPT entry wins — RPT
    detection has higher specificity (it requires four matching
    column names) and any false-positive would have to be hand-
    crafted to look exactly like an RPT bucket.

    Defensive: any failure in the RPT pass is logged and swallowed
    — the original bundle is returned untouched. RPT enrichment is
    additive, not load-bearing.
    """

    try:
        rpt_results = detect_rpt_pattern(db)
    except Exception:
        logger.warning(
            "RPT detection pass failed; bundle returned without RPT "
            "enrichment. Legacy _triples layouts may be misrouted.",
            exc_info=True,
        )
        return bundle

    rpt_entries: dict[str, dict[str, Any]] = {}
    for collection_name, result in rpt_results.items():
        if not result.is_rpt:
            continue
        rpt_entries[collection_name] = {
            "style": "RPT",
            "triplesCollection": result.triples_collection or collection_name,
            "subjectColumn": result.subject_column,
            "predicateColumn": result.predicate_column,
            "objectUriColumn": result.object_uri_column,
            "objectValueColumn": result.object_value_column,
            # Keep the per-collection coverage on the bundle so the
            # /schema/info route can surface "why was this an RPT?"
            # without re-running the detector.
            "rptCoverage": round(result.coverage_ratio, 4),
        }

    if not rpt_entries:
        return bundle

    # Synthesize RPT object-property relationships with rdf:type-typed
    # endpoints (PRD §6.3.2 step 2 — cross-collection inference). The
    # entity overlay above only marks the triples bucket as RPT; this
    # pass is what connects an object property (e.g. ``AUTHORED``) to
    # its typed domain/range (``Person`` → ``Doc``) so the planner and
    # NL→SPARQL surface see relationships, not just an opaque store.
    # Additive: never clobbers a relationship an upstream producer
    # (analyzer / imported OWL) already declared.
    try:
        rpt_relationships = infer_rpt_object_property_relationships(db, rpt_results)
    except Exception:
        logger.warning(
            "RPT object-property relationship synthesis failed; bundle "
            "keeps its existing relationships. Endpoint typing skipped.",
            exc_info=True,
        )
        rpt_relationships = {}

    # Build a new physical mapping with the RPT entries overlaid.
    # Preserves dict order: existing entries first, RPT-overridden
    # entries kept at their original position.
    new_physical = dict(bundle.physical_mapping)
    existing_entities = dict(new_physical.get("entities") or {})
    for name, spec in rpt_entries.items():
        existing_entities[name] = spec
    new_physical["entities"] = existing_entities

    if rpt_relationships:
        existing_relationships = dict(new_physical.get("relationships") or {})
        for name, spec in rpt_relationships.items():
            existing_relationships.setdefault(name, spec)
        new_physical["relationships"] = existing_relationships

    # Tag the bundle metadata so downstream consumers know RPT
    # enrichment ran. The cumulative pattern list is sorted to keep
    # serialisation deterministic for fingerprinting.
    new_metadata = dict(bundle.metadata)
    detected = list(new_metadata.get("detectedPatterns") or [])
    if "rpt" not in detected:
        detected.append("rpt")
    detected.sort()
    new_metadata["detectedPatterns"] = detected
    enrichment_log = list(new_metadata.get("enrichmentApplied") or [])
    entry: dict[str, Any] = {
        "kind": "rpt_overlay",
        "appliedAt": when.isoformat(),
        "collections": sorted(rpt_entries.keys()),
    }
    if rpt_relationships:
        entry["relationships"] = sorted(rpt_relationships.keys())
    enrichment_log.append(entry)
    new_metadata["enrichmentApplied"] = enrichment_log

    return MappingBundle(
        conceptual_schema=bundle.conceptual_schema,
        physical_mapping=new_physical,
        metadata=new_metadata,
        owl_turtle=bundle.owl_turtle,
        source=bundle.source,
    )


# ---------------------------------------------------------------------------
# Edge-endpoint enrichment (producer-agnostic, PRD §6.3.2 step 2)
# ---------------------------------------------------------------------------


def _apply_edge_endpoint_enrichment(db: Any, bundle: MappingBundle, *, when: datetime) -> MappingBundle:
    """Fill ``fromEntity`` / ``toEntity`` on edge relationships that are
    still ``"Any"`` (or absent), using ``_from`` / ``_to`` sampling.

    Why this runs over *any* bundle, analyzer-produced included: the
    analyzer classifies relationship *styles* (``DEDICATED_COLLECTION`` /
    ``GENERIC_WITH_TYPE``) but may leave the endpoints unresolved for the
    legacy and hybrid shapes this service targets. Re-using the same
    cross-collection inference the heuristic path runs
    (:func:`infer_edge_endpoints_from_db`) closes that gap regardless of
    which tier produced the bundle — exactly mirroring why RPT enrichment
    is always-on.

    **Strictly additive.** An endpoint the producer already pinned to a
    real entity is never overwritten; only ``"Any"`` / missing values are
    filled, and only when the inference resolves to a single entity. So a
    better upstream answer always wins, and an ambiguous edge stays
    ``"Any"`` rather than being replaced by a guess.

    Relationship-to-edge matching is by ``edgeCollectionName`` plus
    ``typeValue`` (the discriminator distinguishes the several
    ``GENERIC_WITH_TYPE`` relationships that share one edge collection).
    ``RPT_EDGE`` relationships carry no edge collection and are skipped
    here — their endpoints come from the RPT ``rdf:type`` synthesis pass.

    Defensive: any failure is logged and swallowed; the original bundle
    is returned untouched. Endpoint enrichment is additive, not
    load-bearing.
    """

    relationships = bundle.physical_mapping.get("relationships")
    if not isinstance(relationships, dict) or not relationships:
        return bundle

    # Only do the (one classification pass) work if there's actually an
    # unresolved edge endpoint to fill — keeps the analyzer happy-path
    # and the already-resolved heuristic path from paying for a no-op.
    def _needs_fill(spec: Any) -> bool:
        if not isinstance(spec, dict):
            return False
        if spec.get("style") == "RPT_EDGE" or not spec.get("edgeCollectionName"):
            return False
        return spec.get("fromEntity", "Any") == "Any" or spec.get("toEntity", "Any") == "Any"

    if not any(_needs_fill(spec) for spec in relationships.values()):
        return bundle

    try:
        endpoint_index = infer_edge_endpoints_from_db(db)
    except Exception:
        logger.warning(
            "Edge-endpoint enrichment failed; bundle returned without "
            "endpoint inference. Relationship fromEntity/toEntity may "
            "stay 'Any'.",
            exc_info=True,
        )
        return bundle

    if not endpoint_index:
        return bundle

    # The inference names a COLLECTION-style endpoint by its *collection*
    # (``persons``) — the heuristic's own entity key. A bundle from another
    # producer may key the same collection differently (the analyzer emits
    # ``Person``), and an endpoint naming an entity the bundle does not
    # declare is unresolvable: the relationship cannot be queried at all.
    # Found by the schema_live tier against a seeded PG database.
    entities = bundle.physical_mapping.get("entities")
    entities = entities if isinstance(entities, dict) else {}
    key_by_collection: dict[str, list[str]] = {}
    for entity_key, entity_spec in entities.items():
        if isinstance(entity_spec, dict) and entity_spec.get("style") == "COLLECTION":
            collection = entity_spec.get("collectionName")
            if isinstance(collection, str):
                key_by_collection.setdefault(collection, []).append(entity_key)

    def _as_entity_key(inferred: str) -> str:
        """The bundle's key for *inferred*, or ``"Any"`` when there is none.

        ``"Any"`` rather than the raw name: an endpoint pointing at an
        undeclared entity breaks the query, while ``"Any"`` only loses
        precision. Two entities sharing one collection is ambiguous, so it
        stays ``"Any"`` too — never a guess.
        """
        if inferred == "Any" or inferred in entities:
            return inferred
        keys = key_by_collection.get(inferred, [])
        return keys[0] if len(keys) == 1 else "Any"

    new_relationships: dict[str, Any] = {}
    filled: list[str] = []
    for name, spec in relationships.items():
        if not _needs_fill(spec):
            new_relationships[name] = spec
            continue
        edge_collection = spec["edgeCollectionName"]
        type_value = spec.get("typeValue")  # None for DEDICATED_COLLECTION
        inferred = endpoint_index.get(edge_collection, {}).get(type_value)
        if inferred is None:
            new_relationships[name] = spec
            continue
        from_entity, to_entity = (_as_entity_key(side) for side in inferred)
        updated = dict(spec)
        changed = False
        if updated.get("fromEntity", "Any") == "Any" and from_entity != "Any":
            updated["fromEntity"] = from_entity
            changed = True
        if updated.get("toEntity", "Any") == "Any" and to_entity != "Any":
            updated["toEntity"] = to_entity
            changed = True
        new_relationships[name] = updated
        if changed:
            filled.append(name)

    if not filled:
        return bundle

    new_physical = dict(bundle.physical_mapping)
    new_physical["relationships"] = new_relationships

    new_metadata = dict(bundle.metadata)
    enrichment_log = list(new_metadata.get("enrichmentApplied") or [])
    enrichment_log.append(
        {
            "kind": "edge_endpoint_inference",
            "appliedAt": when.isoformat(),
            "relationships": sorted(filled),
        }
    )
    new_metadata["enrichmentApplied"] = enrichment_log

    return MappingBundle(
        conceptual_schema=bundle.conceptual_schema,
        physical_mapping=new_physical,
        metadata=new_metadata,
        owl_turtle=bundle.owl_turtle,
        source=bundle.source,
    )


_LABEL_SHAPE_PROBE = "FOR d IN @@coll FILTER d[@field] != null LIMIT 1 RETURN IS_ARRAY(d[@field])"
_PN_LOCAL_SAFE = re.compile(r"^[A-Za-z_][A-Za-z0-9_\-]*$")
_OWL_DEFAULT_PREFIX = re.compile(r"@prefix\s+:\s*<([^>]+)>", re.IGNORECASE)
_OWL_PHYS_PREFIX = re.compile(r"@prefix\s+phys:\s*<([^>]+)>", re.IGNORECASE)


def _apply_label_shape_enrichment(db: Any, bundle: MappingBundle, *, when: datetime) -> MappingBundle:
    """Record whether each multi-class (``LABEL``) entity's type field is an array.

    Issue #10: r2g's LPG layout stores a node's labels as an array
    (``labels: ["Person", "Customer"]``); neither the analyzer nor CSI records
    that, and ``doc.labels == @v`` silently matches nothing. One cheap probe
    per LABEL collection (``LIMIT 1``) sets ``typeFieldIsArray`` true/false, so
    the translator emits the index-friendly form for the real shape
    (``translate/builder.py::type_predicate``) instead of the slower
    shape-agnostic one. The flag goes into ``physicalMapping`` and is appended
    to the analyzer's OWL as ``phys:typeFieldIsArray``, because the Workbench
    sends that OWL back on every translate.

    Best-effort and additive: a probe that fails, or a collection with no
    document carrying the field, leaves the entity unflagged (shape-agnostic
    emission — still correct).
    """
    entities = bundle.physical_mapping.get("entities")
    if not isinstance(entities, dict):
        return bundle

    new_entities: dict[str, Any] = dict(entities)
    probed: dict[str, bool] = {}
    for name, spec in entities.items():
        if not isinstance(spec, dict) or "typeFieldIsArray" in spec:
            continue
        field = spec.get("typeField")
        collection = spec.get("collectionName")
        if not (isinstance(field, str) and field and isinstance(collection, str) and collection):
            continue
        try:
            rows = list(db.aql.execute(_LABEL_SHAPE_PROBE, bind_vars={"@coll": collection, "field": field}))
        except Exception as exc:  # noqa: BLE001 — enrichment is best-effort
            logger.warning("label-shape probe failed for %s.%s: %s", tenant_name(collection), field, exc)
            continue
        if not rows or not isinstance(rows[0], bool):
            continue
        new_entities[name] = {**spec, "typeFieldIsArray": rows[0]}
        probed[name] = rows[0]

    if not probed:
        return bundle

    new_physical = dict(bundle.physical_mapping)
    new_physical["entities"] = new_entities
    new_metadata = dict(bundle.metadata)
    enrichment_log = list(new_metadata.get("enrichmentApplied") or [])
    enrichment_log.append(
        {
            "kind": "label_shape_probe",
            "appliedAt": when.isoformat(),
            "entities": sorted(probed),
            "arrays": sorted(n for n, is_array in probed.items() if is_array),
        }
    )
    new_metadata["enrichmentApplied"] = enrichment_log
    return MappingBundle(
        conceptual_schema=bundle.conceptual_schema,
        physical_mapping=new_physical,
        metadata=new_metadata,
        owl_turtle=_append_label_shape_annotations(bundle.owl_turtle, probed),
        source=bundle.source,
    )


def _append_label_shape_annotations(owl_turtle: str | None, probed: dict[str, bool]) -> str | None:
    """Append ``:<Class> phys:typeFieldIsArray true|false .`` to the analyzer's OWL.

    The analyzer's Turtle is returned to the UI verbatim (``translate/owl.py::
    mapping_to_turtle``), so the shape flag has to be in that text. A Turtle
    statement may follow the prefixes anywhere; appending keeps every existing
    line byte-identical. Classes are named by the analyzer's entity name in its
    default ``:`` namespace (the same names its ``owl:Class`` lines use).
    """
    if not owl_turtle or not _OWL_DEFAULT_PREFIX.search(owl_turtle):
        return owl_turtle
    phys = _OWL_PHYS_PREFIX.search(owl_turtle)
    prefix_line = "" if phys else "@prefix phys: <http://arangodb.com/schema/physical#> .\n"
    lines = [
        f":{name} phys:typeFieldIsArray {'true' if is_array else 'false'} ."
        for name, is_array in sorted(probed.items())
        if _PN_LOCAL_SAFE.match(name)
    ]
    if not lines:
        return owl_turtle
    return (
        owl_turtle.rstrip("\n")
        + "\n\n# Label-field shape (arango-sparql-py, issue #10)\n"
        + prefix_line
        + "\n".join(lines)
        + "\n"
    )


# ---------------------------------------------------------------------------
# Metadata / warning helpers
# ---------------------------------------------------------------------------


def _attach_warning(
    bundle: MappingBundle,
    *,
    code: str,
    message: str,
    install_hint: str | None = None,
) -> MappingBundle:
    """Return a copy of *bundle* with a structured warning appended
    to ``metadata.warnings``. Mirrors the sister project's
    :func:`_attach_warning`; documented at PRD §6.3.4 (warning
    catalogue) and §6.4 (route surfaces).
    """

    new_metadata = dict(bundle.metadata)
    warnings = list(new_metadata.get("warnings") or [])
    entry: dict[str, Any] = {"code": code, "message": message}
    if install_hint:
        entry["install_hint"] = install_hint
    warnings.append(entry)
    new_metadata["warnings"] = warnings
    return MappingBundle(
        conceptual_schema=bundle.conceptual_schema,
        physical_mapping=bundle.physical_mapping,
        metadata=new_metadata,
        owl_turtle=bundle.owl_turtle,
        source=bundle.source,
    )


#: Analyzer notes recognised by message prefix → (code, severity). ``info``
#: describes normal operation and the Workbench keeps it out of the warning
#: banner: since 2026-10-08 the analyzer runs without an LLM by default
#: (:func:`_resolve_analyzer_provider`), so this note is on every analyzer
#: bundle. Mirrors arango-cypher-py's ``_ANALYZER_NOTES``.
_ANALYZER_NOTES: tuple[tuple[str, str, str], ...] = (
    ("LLM provider not configured", "ANALYZER_BASELINE_NO_LLM", "info"),
)


def _normalize_warning_entry(entry: Any) -> dict[str, Any]:
    """Coerce one warning into the canonical ``{code, message}`` dict.

    Dicts pass through untouched (the catalogue producers already emit
    the right shape). A bare string is wrapped under
    :data:`W_ANALYZER_ADVISORY`; any other type is stringified so the
    wire shape stays ``dict[str, Any]`` no matter what an upstream
    producer hands us.
    """

    if isinstance(entry, dict):
        return entry
    message = str(entry)
    for prefix, code, severity in _ANALYZER_NOTES:
        if message.startswith(prefix):
            return {"code": code, "message": message, "severity": severity}
    return {"code": W_ANALYZER_ADVISORY, "message": message, "severity": "warning"}


def _normalize_bundle_warnings(bundle: MappingBundle) -> MappingBundle:
    """Ensure ``metadata.warnings`` is a list of ``{code, message}`` dicts.

    The wire contract (:mod:`arango_sparql.service.models`) types every
    ``warnings`` field as ``list[dict]``; the analyzer, however, can attach
    free-text string advisories (e.g. the "LLM provider not configured"
    baseline-inference note). Left unconverted those strings fail the
    response-model validation and surface as an opaque 500 on
    ``/schema/introspect`` and ``/schema/force-reacquire``. Normalising
    here — at the single point every bundle flows through — restores the
    invariant for *all* consumers (routes, planner, NL pipeline).

    Returns *bundle* unchanged when there is nothing to fix (no warnings,
    or all already dicts) so the common path allocates nothing.
    """

    raw = (bundle.metadata or {}).get("warnings")
    if not isinstance(raw, list) or all(isinstance(w, dict) for w in raw):
        return bundle

    new_metadata = dict(bundle.metadata)
    new_metadata["warnings"] = [_normalize_warning_entry(w) for w in raw]
    return MappingBundle(
        conceptual_schema=bundle.conceptual_schema,
        physical_mapping=bundle.physical_mapping,
        metadata=new_metadata,
        owl_turtle=bundle.owl_turtle,
        source=bundle.source,
    )


def _stamp_acquisition_timestamp(bundle: MappingBundle, *, when: datetime) -> MappingBundle:
    """Stamp ``metadata.acquisitionTimestamp`` so cache layers and
    the ``/schema/info`` route can surface "when did we last
    acquire?" without consulting an external clock.

    Idempotent: an existing timestamp is preserved (heuristic
    bundles already stamp their own — :func:`build_heuristic_mapping`
    sets ``metadata.acquiredAt``). We only fill the gap for
    analyzer bundles, whose metadata schema does not include this
    field today.
    """

    new_metadata = dict(bundle.metadata)
    if "acquisitionTimestamp" not in new_metadata:
        new_metadata["acquisitionTimestamp"] = when.isoformat()
    if bundle.source is None:
        new_source: MappingSource | None = None
    else:
        new_source = bundle.source
    return MappingBundle(
        conceptual_schema=bundle.conceptual_schema,
        physical_mapping=bundle.physical_mapping,
        metadata=new_metadata,
        owl_turtle=bundle.owl_turtle,
        source=new_source,
    )


# ---------------------------------------------------------------------------
# Live-DB fingerprint helpers
# ---------------------------------------------------------------------------


def _live_fingerprint(db: Any, *, kind: Literal["shape", "counts"]) -> str | None:
    """Common implementation behind :func:`db_shape_fingerprint` and
    :func:`db_counts_fingerprint`. Returns ``None`` on missing
    analyzer or unexpected error so the cache layer never has to
    deal with exceptions from this code path.
    """

    try:
        if kind == "shape":
            from schema_analyzer import fingerprint_physical_shape as fn
        else:
            from schema_analyzer import fingerprint_physical_counts as fn
    except ImportError:
        return None

    try:
        return fn(db, exclude_collections=set(service_collections()))
    except TypeError:
        # Older analyzer releases (< 0.5) did not accept
        # exclude_collections. Re-try without it; the cache row will
        # then count toward the fingerprint, but the cache is still
        # functionally correct (it just refreshes one extra time
        # after each write).
        try:
            return fn(db)
        except Exception:
            logger.warning(
                "Live DB fingerprint (%s) failed; cache will fall back to bundle-side fingerprints.",
                kind,
                exc_info=True,
            )
            return None
    except Exception:
        logger.warning(
            "Live DB fingerprint (%s) failed; cache will fall back to bundle-side fingerprints.",
            kind,
            exc_info=True,
        )
        return None


__all__ = [
    "ANALYZER_INSTALL_HINT",
    "ANALYZER_VERSION_RANGE",
    "W_ANALYZER_NOT_INSTALLED",
    "W_SCHEMA_HEURISTIC_FALLBACK",
    "AnalyzerNotInstalledError",
    "Strategy",
    "acquire_mapping_bundle",
    "analyzer_available",
    "db_counts_fingerprint",
    "db_shape_fingerprint",
    "hide_service_collections",
    "refresh_statistics",
    "service_collections",
]
