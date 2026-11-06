"""Pydantic request / response models + ``_MAX_*`` length constants.

Mirror of ``arango_cypher.service.models``: every user-controlled
string field has a ``Field(..., max_length=_MAX_*)`` envelope so a
10 MB POST body cannot wedge the parser, balloon a SQLite store, or
push novel-length prompts at an LLM.

The ``_MAX_*`` constants live as module-level attributes (intentionally
underscore-prefixed) so an operator who needs to raise one for a
specific deployment can grep, monkeypatch, and rebuild the affected
model.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from ..editor import EditorVocabV1

# Stricter-than-type field bounds for the user-controlled string fields on
# every request model below. Real interactive payloads are well below these
# limits; the constants exist to bound an attack rather than restrict
# normal use.
_MAX_SPARQL_LENGTH = 100_000  # ~100 KB; a real interactive query is < 10 KB
_MAX_AQL_LENGTH = 100_000  # raw AQL on /execute-aql; same envelope
_MAX_NL_QUESTION_LENGTH = 4_000  # bounds LLM context-window cost
_MAX_TURTLE_LENGTH = 1_000_000  # OWL ontologies can be sizeable; 1 MB cap
_MAX_FIELD_LENGTH = 256  # urls, usernames, db names, tenant fields

# OWL-bomb defence — request-body byte ceiling on ``/mapping/import-owl``
# (PRD §8.6 T7 + Appendix A.2 ``MAPPING_IMPORT_MAX_BYTES``). Overridable
# per request via :func:`arango_sparql.service.routes.mapping._resolve_max_bytes`
# which reads ``MAPPING_IMPORT_MAX_BYTES`` lazily; this constant is the
# default the route handler falls back to. Two megabytes matches the
# PRD default and is well above any hand-authored mapping ontology
# (Microsoft Ontology Playground exports for FIBO sit at ~400 KB).
_DEFAULT_MAPPING_IMPORT_MAX_BYTES: int = 2_000_000

# Hard cap on materialised /execute and /execute-aql cursor rows. Mirrors
# the Cypher project's policy of bounding an interactive run before it
# pages a multi-GB cursor through the FastAPI worker. Operators who need
# more should run the query against the database directly. The number is
# generous (10k rows is well above any UI table render budget) but
# bounded so a runaway query can't OOM the service host.
_MAX_RESULT_DOCS = 10_000


class ConnectRequest(BaseModel):
    url: str = Field(default="http://localhost:8529", max_length=_MAX_FIELD_LENGTH)
    database: str = Field(default="_system", max_length=_MAX_FIELD_LENGTH)
    username: str = Field(default="root", max_length=_MAX_FIELD_LENGTH)
    password: str = Field(default="", max_length=_MAX_FIELD_LENGTH)

    @field_validator("url")
    @classmethod
    def _url_shape(cls, v: str) -> str:
        # Defensive shape-check before the SSRF guard at /connect runs.
        # The /connect endpoint already rejects bad targets at runtime
        # via the SSRF allowlist; this validator catches the cheaper,
        # more obviously-broken cases at request-validation time so the
        # caller gets a 422 with a clear "url is malformed" hint instead
        # of a deeper 4xx/5xx after the connect machinery has spun up.
        if not v:
            return v
        lowered = v.lower()
        if not (lowered.startswith("http://") or lowered.startswith("https://")):
            raise ValueError("url must start with http:// or https://")
        return v


class ConnectResponse(BaseModel):
    token: str
    databases: list[str]
    # The database the session opened. ``/connect/platform`` chooses it when
    # the request names none (the mount database may not be one the user can
    # open), so the UI learns which DB it actually landed in. ``None`` on the
    # classic ``/connect`` path keeps backward compatibility for older callers.
    database: str | None = None
    # ``/connect/platform`` only: the platform user the session runs as, as
    # the platform's integration sidecar names them (``None`` when unknown).
    user: str | None = None


class PlatformConnectRequest(BaseModel):
    """Request body for ``POST /connect/platform``.

    Only the (optional) database name — the credential is the gateway-
    forwarded platform JWT, never a field. ``None`` lets the server open the
    database this user can best open (the instance's mount database).
    """

    database: str | None = Field(default=None, max_length=_MAX_FIELD_LENGTH)


class PlatformStatus(BaseModel):
    """Response body for ``GET /connect/platform``.

    ``available`` is true only when the service can open a session from the
    forwarded platform login (platform auth on, a cluster endpoint
    configured, and a forwarded JWT on the request). ``database`` is the DB a
    platform session would open by default; ``reason`` explains an
    unavailable status. Discloses no credential and no endpoint.
    """

    available: bool
    database: str
    reason: str | None = None


class BindGraphRequest(BaseModel):
    """Request body for ``POST /session/graph``.

    ``graphName`` is the ArangoDB named graph to scope this session's
    schema acquisition to. ``null`` (or omitted) clears the scope back to
    "all collections". camelCase matches the sister project's contract so
    the shared UI client works against either backend.
    """

    graphName: str | None = Field(default=None, max_length=_MAX_FIELD_LENGTH)


class TranslateRequest(BaseModel):
    sparql: str = Field(..., max_length=_MAX_SPARQL_LENGTH)
    ontology_ttl: str | None = Field(default=None, max_length=_MAX_TURTLE_LENGTH)
    mapping: dict[str, Any] | None = None
    params: dict[str, Any] | None = None


class TranslateResponse(BaseModel):
    aql: str
    bind_vars: dict[str, Any]
    warnings: list[dict[str, Any]] = Field(default_factory=list)
    # Subset of :attr:`warnings` whose ``code`` starts with ``W_SCHEMA_``.
    # Duplicated as a convenience so the UI can render schema-mapping
    # advisories (unmapped IRI, default-collection fallback) in a
    # dedicated sidebar without re-filtering on every paint. The full
    # ``warnings`` field still carries every entry — this is purely a
    # projection. See :func:`arango_sparql.api.translate` for the
    # population point.
    schema_warnings: list[dict[str, Any]] = Field(default_factory=list)
    elapsed_ms: float | None = None


class SparqlExecuteRequest(BaseModel):
    sparql: str = Field(..., max_length=_MAX_SPARQL_LENGTH)
    ontology_ttl: str | None = Field(default=None, max_length=_MAX_TURTLE_LENGTH)
    mapping: dict[str, Any] | None = None
    params: dict[str, Any] | None = None
    database: str = Field(default="_system", max_length=_MAX_FIELD_LENGTH)
    # Run for the graph view: a plain SELECT is widened to every variable in its patterns,
    # and the response carries the patterns (``graph``), so the entities behind the rows and
    # how they connect can be drawn (``translate/graph_shape.py``).
    graph: bool = False


class GraphShape(BaseModel):
    """What the graph view draws a result with: the query's triple patterns (each term
    ``{"var"|"iri"|"literal": …}``, and a ``kind`` — ``type``, ``relationship`` or
    ``property`` — when the schema says), and whether the run projected every variable."""

    patterns: list[dict[str, Any]] = Field(default_factory=list)
    widened: bool = False


class SparqlExecuteResponse(BaseModel):
    bindings: list[dict[str, Any]] = Field(default_factory=list)
    warnings: list[dict[str, Any]] = Field(default_factory=list)
    aql: str | None = None
    bind_vars: dict[str, Any] | None = None
    elapsed_ms: float | None = None
    # Wall-clock time spent in the SPARQL → AQL transpiler on this
    # request. Surfaced separately from ``elapsed_ms`` (the
    # translate+execute total) so a UI can show both badges side-by-side
    # after a Run; otherwise users lose visibility into translation cost
    # the moment they execute.
    translate_ms: float | None = None
    exec_ms: float | None = None
    # ``True`` when the cursor materialisation was capped at
    # :data:`_MAX_RESULT_DOCS`. UI surfaces this as a "results truncated"
    # banner so the operator knows to refine the query rather than
    # assume they saw the full set.
    truncated: bool = False

    # Set on a ``graph`` run: the patterns the graph view draws the rows with.
    graph: GraphShape | None = None


class RawAqlRequest(BaseModel):
    """Raw-AQL pass-through payload for ``/execute-aql``.

    Mirrors the Cypher project's ``ExecuteAqlRequest`` — exposed so the
    UI can run an already-translated query without paying the
    parse/translate cost again, and so power users can hand-author AQL
    against the same session that ran a SPARQL execution.
    """

    aql: str = Field(..., max_length=_MAX_AQL_LENGTH)
    bind_vars: dict[str, Any] = Field(default_factory=dict)


class RawAqlResponse(BaseModel):
    results: list[Any] = Field(default_factory=list)
    aql: str
    bind_vars: dict[str, Any] = Field(default_factory=dict)
    warnings: list[dict[str, Any]] = Field(default_factory=list)
    exec_ms: float | None = None
    truncated: bool = False


class SparqlExplainResponse(BaseModel):
    """Response shape for ``POST /explain`` — the AQL execution plan
    produced by ``db.aql.explain()`` for the AQL transpiled from the
    incoming SPARQL.

    Mirrors the Cypher project's ``/explain`` envelope: the SPARQL
    source is echoed back so a UI can pair the plan with the originating
    query without holding extra request state, ``aql`` + ``bind_vars``
    are the artefacts of the SPARQL→AQL transpilation, and ``plan`` is
    the raw ArangoDB explain output (``{nodes, rules, collections,
    variables, estimatedCost, ...}``). The schema/topology of ``plan``
    follows ArangoDB's contract — we surface it verbatim rather than
    pinning it to a Pydantic shape because the operator-facing UI
    already renders against the native ArangoDB explain JSON.
    """

    sparql: str
    aql: str
    bind_vars: dict[str, Any] = Field(default_factory=dict)
    plan: dict[str, Any] = Field(default_factory=dict)
    warnings: list[dict[str, Any]] = Field(default_factory=list)
    translate_ms: float | None = None


class SparqlProfileResponse(BaseModel):
    """Response shape for ``POST /profile`` — the per-stage timing and
    actual result rows from executing the transpiled AQL with
    ``profile=2`` (full).

    ``bindings`` carries the materialised cursor rows (capped at
    :data:`_MAX_RESULT_DOCS`), ``profile`` is the ArangoDB profile
    object (``{plan, stats, profile, warnings, ...}``) returned by
    ``cursor.profile()``. We expose both because the UI shows the rows
    *and* the per-node timings side-by-side; without the rows the
    operator cannot tell whether the slow step actually produced the
    expected output.
    """

    sparql: str
    aql: str
    bind_vars: dict[str, Any] = Field(default_factory=dict)
    bindings: list[dict[str, Any]] = Field(default_factory=list)
    truncated: bool = False
    profile: dict[str, Any] = Field(default_factory=dict)
    warnings: list[dict[str, Any]] = Field(default_factory=list)
    translate_ms: float | None = None
    exec_ms: float | None = None


class ValidateRequest(BaseModel):
    sparql: str = Field(..., max_length=_MAX_SPARQL_LENGTH)
    ontology_ttl: str | None = Field(default=None, max_length=_MAX_TURTLE_LENGTH)
    mapping: dict[str, Any] | None = None
    params: dict[str, Any] | None = None


class ValidateResponse(BaseModel):
    """Result of a parse-only ``/validate`` round-trip.

    ``valid`` is ``True`` when the SPARQL string parses cleanly under
    ``rdflib.plugins.sparql``; ``False`` otherwise. ``errors`` carries
    a list of ``{code, message}`` records — one per error surfaced by
    the parser. ``warnings`` is reserved for non-fatal advisories the
    transpiler emits during validation (e.g. unmapped predicate IRIs)
    so a fully-passing ``valid=true`` can still ship guidance to the
    operator.
    """

    valid: bool
    errors: list[dict[str, str]] = Field(default_factory=list)
    warnings: list[dict[str, Any]] = Field(default_factory=list)


class ErrorResponse(BaseModel):
    error: str
    code: str


class HealthResponse(BaseModel):
    """``/health`` — liveness, plus what a bug report needs (PRD §10.19):
    which service and versions answered. Same keys as arango-cypher-py's."""

    status: str
    service: str = "arango-sparql-py"
    version: str
    analyzer_version: str | None = None


class ReadyResponse(BaseModel):
    """``/health/ready`` — readiness (vs. ``/health`` liveness).

    ``arango`` is one of ``"ok"`` (configured default ArangoDB
    responded), ``"unreachable"`` (configured but not responding —
    the endpoint also returns HTTP 503), or ``"unconfigured"``
    (BYOC deployment with no default server; readiness degrades to
    liveness and the endpoint stays 200).
    """

    status: str
    version: str
    arango: str


# ---------------------------------------------------------------------------
# NL → SPARQL pipeline request / response surface (frozen for round-3 UI).
# ---------------------------------------------------------------------------
#
# Defined inline here (rather than re-exported from
# :mod:`arango_sparql.nl2sparql.models`) to break the import cycle:
# the nl2sparql package imports the ``_MAX_*`` constants from this
# module, so the Pydantic models live here and the dataclass-only
# pipeline types (LLMResponse, LLMCallRecord, PipelineOutcome) live in
# the nl2sparql package. Tests can import either path — both names
# resolve to the same class identity.


class NlTranslateRequest(BaseModel):
    """Request body for ``POST /nl-translate``.

    Per rule 300, ``ontology_ttl`` is the canonical schema slot — the
    LLM reads Turtle better than any other RDF serialisation. The
    ``mapping`` slot is reserved for the JSON payload some callers
    already hold from an earlier endpoint round-trip; it is converted
    into a SchemaResolver via the same adapter as ``/translate``.
    """

    nl: str = Field(..., min_length=1, max_length=_MAX_NL_QUESTION_LENGTH)
    ontology_ttl: str | None = Field(default=None, max_length=_MAX_TURTLE_LENGTH)
    mapping: dict[str, Any] | None = None
    max_repairs: int = Field(default=2, ge=0, le=5)


class NlTranslateResponse(BaseModel):
    """Response body for ``POST /nl-translate``."""

    nl: str
    sparql: str
    aql: str
    bind_vars: dict[str, Any] = Field(default_factory=dict)
    warnings: list[dict[str, Any]] = Field(default_factory=list)
    llm_calls: int = 0
    cost_usd: float = 0.0
    latency_ms: int = 0
    repaired: bool = False


class NlExplainRequest(BaseModel):
    """Request body for ``POST /nl-explain``."""

    nl: str | None = Field(default=None, max_length=_MAX_NL_QUESTION_LENGTH)
    sparql: str | None = Field(default=None, max_length=_MAX_SPARQL_LENGTH)
    ontology_ttl: str | None = Field(default=None, max_length=_MAX_TURTLE_LENGTH)
    mapping: dict[str, Any] | None = None
    max_repairs: int = Field(default=2, ge=0, le=5)


class NlExplainResponse(NlTranslateResponse):
    """Response body for ``POST /nl-explain`` — adds an LLM-generated explanation."""

    explanation: str = ""


class NlExecuteRequest(NlTranslateRequest):
    """Request body for ``POST /nl-execute``.

    Auth comes from ``X-Arango-Session`` via the existing
    ``_get_session`` dependency — there is no token field in the body
    so a hostile caller cannot point one user's NL request at another
    user's database by guessing an unauthenticated body field.
    """

    database: str = Field(default="_system", max_length=_MAX_FIELD_LENGTH)


class NlExecuteResponse(NlTranslateResponse):
    """Response body for ``POST /nl-execute`` — adds execution telemetry."""

    bindings: list[dict[str, Any]] = Field(default_factory=list)
    truncated: bool = False
    exec_ms: int = 0


class NlSamplesRequest(BaseModel):
    """Request body for ``POST /nl-samples``.

    Seeds the UI "Ask" suggestions dropdown. ``ontology_ttl`` is the
    schema source (rule 300 — the SPARQL pipeline grounds everything in
    Turtle); when absent the route returns an empty list rather than
    guessing. ``use_llm`` is advisory — the route always has a
    deterministic rule-based fallback, so suggestions work even with no
    LLM provider configured.
    """

    ontology_ttl: str | None = Field(default=None, max_length=_MAX_TURTLE_LENGTH)
    count: int = Field(default=8, ge=1, le=50)
    use_llm: bool = True


class NlSamplesResponse(BaseModel):
    """Response body for ``POST /nl-samples``."""

    queries: list[str] = Field(default_factory=list)
    elapsed_ms: float = 0.0


# ---------------------------------------------------------------------------
# Schema HTTP surface (PRD §6.4) request / response models.
# ---------------------------------------------------------------------------
#
# Mirrors the sister project's schema-route shapes so a developer who
# knows ``arango_cypher.service.routes.schema`` can read this surface
# without a translation step. Differences from the sister:
#
# * Adds the ``mapping`` block to every introspect / force-reacquire
#   response (sister only returns the conceptual summary; we surface
#   the full :class:`MappingBundle` wire dict because the SPARQL
#   planner consumes it directly via ``SchemaResolver.from_mapping_bundle``).
# * Adds ``rpt_collections`` to the summary because RPT is a SPARQL-
#   side concern (the sister project's planner is Cypher-only and
#   does not need the breakout).
# * Splits the ``/schema/status`` payload into ``current`` /
#   ``cached`` fingerprint blocks rather than four flat fields, so
#   the UI's drift banner can iterate one structure.


class SchemaIntrospectResponse(BaseModel):
    """Response body for ``GET /schema/introspect`` (PRD §6.4 row 1).

    ``mapping`` is the canonical wire-dict form of the live
    :class:`~arango_sparql.translate.mapping.MappingBundle`. ``summary``
    is a UI-friendly digest derived from the same bundle. Both are
    emitted so a single call satisfies both the planner-side
    ("give me the bundle") and the UI-side ("just show the user what
    the schema looks like") consumers.

    Catalog model (``service/schema_warm.py``): ``status="pending"`` means
    the database has not been analyzed yet and a background analysis is
    running — ``mapping`` / ``summary`` are empty and the client retries.
    ``warming=true`` means a (re)analysis is in flight; with
    ``status="ready"`` the payload is the current cached schema and a
    fresher one is on its way (after "Refresh schema" or TTL expiry).
    """

    mapping: dict[str, Any] = Field(default_factory=dict)
    summary: dict[str, Any] = Field(default_factory=dict)
    warnings: list[dict[str, Any]] = Field(default_factory=list)
    source: dict[str, Any] | None = None
    cache_hit: bool = False
    elapsed_ms: float = 0.0
    status: Literal["ready", "pending"] = "ready"
    warming: bool = False
    #: Editor vocabulary v1 for the workbench's SPARQL completion, built from
    #: the same per-class derivation as the NL prompt (issue #68). ``None``
    #: while pending, or when the bundle's ontology names no default (``:``)
    #: namespace.
    editor_vocab: EditorVocabV1 | None = None


class SchemaPropertiesResponse(BaseModel):
    """Response body for ``GET /schema/properties`` (PRD §6.4 row 2).

    ``properties`` is keyed by attribute name; each value is
    ``{field, type, sample, required}`` per the inferred shape.
    ``collection`` is echoed so a multi-call UI can correlate the
    response to the originating request when calls fan out.
    """

    collection: str
    sample_size: int
    properties: dict[str, dict[str, Any]] = Field(default_factory=dict)


class SchemaSummaryRequest(BaseModel):
    """Request body for ``GET /schema/summary`` (PRD §6.4 row 3).

    Either ``mapping`` (preferred) or ``ontology_ttl`` (fallback)
    must be supplied. The route validates this at request time so a
    422 lands with a clear message rather than a 500 from the
    summary builder.
    """

    mapping: dict[str, Any] | None = None
    ontology_ttl: str | None = Field(default=None, max_length=_MAX_TURTLE_LENGTH)


class SchemaSummaryResponse(BaseModel):
    """Response body for ``GET /schema/summary``.

    The ``entities`` and ``relationships`` arrays are the UI-facing
    digest: each entry is small enough to render in a list view
    without further drilldown.
    """

    entities: list[dict[str, Any]] = Field(default_factory=list)
    relationships: list[dict[str, Any]] = Field(default_factory=list)
    rpt_collections: list[dict[str, Any]] = Field(default_factory=list)
    entity_count: int = 0
    relationship_count: int = 0


class SchemaStatisticsResponse(BaseModel):
    """Response body for ``GET /schema/statistics`` (PRD §6.4 row 4).

    Surfaces ``bundle.metadata.statistics`` as-is when the analyzer
    populated it. When statistics are missing (e.g. heuristic-only
    bundle), an empty block is returned with ``available=false`` so
    the UI can render a "compute statistics" CTA rather than a stack
    trace.
    """

    statistics: dict[str, Any] = Field(default_factory=dict)
    available: bool = False
    last_acquired_at: str | None = None
    # Same catalog semantics as SchemaIntrospectResponse.status / .warming.
    status: Literal["ready", "pending"] = "ready"
    warming: bool = False


class SchemaFingerprintBlock(BaseModel):
    """One side of the ``/schema/status`` fingerprint comparison.

    Both sub-fingerprints (``shape`` and ``counts``) are emitted
    even when one of them is unknown (None) so the UI can render a
    consistent two-row diff without a special case for the
    "haven't acquired yet" path.
    """

    shape: str | None = None
    counts: str | None = None


class SchemaStatusResponse(BaseModel):
    """Response body for ``GET /schema/status`` (PRD §6.4 row 5).

    The ``status`` enum mirrors :class:`FingerprintDrift` plus the
    extra ``no_cache`` slot for "we have nothing to compare against
    yet". ``unchanged`` and ``needs_full_rebuild`` are convenience
    booleans so the UI doesn't have to enum-match.
    """

    status: str
    unchanged: bool
    needs_full_rebuild: bool
    current: SchemaFingerprintBlock = Field(default_factory=SchemaFingerprintBlock)
    cached: SchemaFingerprintBlock = Field(default_factory=SchemaFingerprintBlock)
    last_acquired_at: str | None = None
    db_name: str | None = None


class SchemaInvalidateCacheResponse(BaseModel):
    """Response body for ``POST /schema/invalidate-cache`` (PRD §6.4 row 6).

    ``invalidated`` is ``True`` iff a cache entry was actually
    evicted from either tier; ``persistent_dropped`` iff the stored
    copy in the database (PRD §6.3.3 L2) was deleted.
    The ``db_name`` echo lets a caller confirm they invalidated the
    expected database when one process is talking to multiple DBs.
    """

    invalidated: bool
    db_name: str
    persistent_dropped: bool = False


class SchemaForceReacquireResponse(BaseModel):
    """Response body for ``POST /schema/force-reacquire`` (PRD §6.4 row 7).

    Same shape as :class:`SchemaIntrospectResponse` so a UI's
    "refresh schema" affordance can render either response without
    a branch. ``cache_hit`` is always ``False`` here by definition.
    """

    mapping: dict[str, Any]
    summary: dict[str, Any]
    warnings: list[dict[str, Any]] = Field(default_factory=list)
    source: dict[str, Any] | None = None
    cache_hit: bool = False
    elapsed_ms: float = 0.0


# ---------------------------------------------------------------------------
# OWL Import / Export (PRD §6.4 rows 8 & 9, security §8.6 T7).
# ---------------------------------------------------------------------------
#
# Two endpoints, mirror-symmetric so an "Import OWL → tweak →
# Export OWL" UI cycle is round-trip safe:
#
# * ``POST /mapping/import-owl`` accepts ``text/turtle`` (or a JSON
#   ``{turtle, source_notes?}`` envelope for clients whose HTTP stack
#   refuses raw text bodies). Returns ``{accepted, mapping, ...}`` so
#   the caller can immediately push the parsed bundle into the schema
#   cache without a second round-trip.
# * ``POST /mapping/export-owl`` accepts ``{mapping}`` and returns
#   ``{turtle, mime_type, triple_count}``. Operates on the request
#   body rather than session state because (a) the schema cache is
#   per-DB, not per-session, and (b) the UI's "Export current"
#   affordance already holds the mapping it wants to export.
#
# Both responses surface ``triple_count`` so the UI can render a
# "1.2k triples" badge for operator situational awareness, and so
# the OWL-bomb cap (PRD §8.6 T7) emits a numeric data point in the
# 422 detail rather than a bare error message.


class OwlImportRequest(BaseModel):
    """Request body for ``POST /mapping/import-owl`` (JSON envelope form).

    The route also accepts a raw ``text/turtle`` body — the JSON
    envelope exists for clients whose HTTP stack chokes on raw text
    (some ``fetch`` polyfills set ``Content-Type: application/json``
    even when given a string body). When both forms are present the
    raw body wins; the JSON path is the fallback.
    """

    turtle: str = Field(..., min_length=1, max_length=_MAX_TURTLE_LENGTH)
    source_notes: str | None = Field(default=None, max_length=_MAX_FIELD_LENGTH)


class OwlImportResponse(BaseModel):
    """Response body for ``POST /mapping/import-owl``.

    ``mapping`` is the canonical wire-dict form of the parsed
    :class:`MappingBundle` (same shape as ``/schema/introspect``'s
    ``mapping`` field) so a UI client can drop the response straight
    into its existing introspect-handling code path.
    """

    accepted: bool
    mapping: dict[str, Any]
    triple_count: int
    warnings: list[dict[str, Any]] = Field(default_factory=list)
    source: dict[str, Any] | None = None
    elapsed_ms: float = 0.0


class OwlExportRequest(BaseModel):
    """Request body for ``POST /mapping/export-owl``.

    Either ``mapping`` (preferred — a full wire-dict
    :class:`MappingBundle`) or ``ontology_ttl`` (fallback — a Turtle
    blob the client wants to round-trip through the synthesizer)
    must be supplied. The route validates this at request time so a
    422 with a clear message lands instead of a 500 from the
    serializer.
    """

    mapping: dict[str, Any] | None = None
    ontology_ttl: str | None = Field(default=None, max_length=_MAX_TURTLE_LENGTH)


class OwlExportResponse(BaseModel):
    """Response body for ``POST /mapping/export-owl`` (JSON form).

    For ``Accept: text/turtle`` the route returns a
    :class:`fastapi.responses.PlainTextResponse` with the raw Turtle
    bytes — the JSON envelope here is the default for clients that
    just want to round-trip the bundle programmatically (e.g. for
    a UI download button that prepares a Blob from the body).
    """

    turtle: str
    mime_type: str = "text/turtle"
    triple_count: int = 0
    elapsed_ms: float = 0.0


# ---------------------------------------------------------------------------
# OWL schema graph view (PRD §6.4 — ``GET /schema/owl``)
# ---------------------------------------------------------------------------
#
# Structured projection of the connected database's OWL ontology for the
# UI's GRAPH tab. Classes become nodes, object properties become edges,
# datatype/annotation properties become per-class property bags. The shape
# is identical to the frontend's client-side ``n3`` parser output so the
# Cytoscape renderer can consume either source interchangeably.


class OwlClassModel(BaseModel):
    """One OWL class in the schema-graph view (a node)."""

    iri: str
    local_name: str = Field(..., alias="localName")
    super_classes: list[str] = Field(default_factory=list, alias="superClasses")
    comment: str | None = None

    model_config = {"populate_by_name": True}


class OwlPropertyModel(BaseModel):
    """One OWL property in the schema-graph view.

    ``kind`` discriminates how the renderer draws it: ``object`` →
    domain→range edge; ``datatype`` / ``annotation`` → a property bag
    glyph attached to each domain class.
    """

    iri: str
    local_name: str = Field(..., alias="localName")
    domain: list[str] = Field(default_factory=list)
    range: list[str] = Field(default_factory=list)
    kind: Literal["object", "datatype", "annotation"]
    comment: str | None = None

    model_config = {"populate_by_name": True}


class OwlSchemaResponse(BaseModel):
    """Response body for ``GET /schema/owl``.

    ``turtle`` carries the source ontology so a UI can round-trip it
    (e.g. populate the Turtle editor) without a second request. Classes
    and properties are the structured projection the GRAPH tab renders.
    """

    classes: list[OwlClassModel] = Field(default_factory=list)
    properties: list[OwlPropertyModel] = Field(default_factory=list)
    turtle: str | None = None
    source: dict[str, Any] | None = None
    warnings: list[dict[str, Any]] = Field(default_factory=list)
    elapsed_ms: float = 0.0
    # Same catalog semantics as SchemaIntrospectResponse.status / .warming.
    status: Literal["ready", "pending"] = "ready"
    warming: bool = False
