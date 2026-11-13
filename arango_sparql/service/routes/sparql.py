"""SPARQL / AQL endpoints — ``/translate``, ``/execute``, ``/execute-aql``,
``/validate``.

Mirror of ``arango_cypher.service.routes.cypher`` — same dependency
injection chain (``_check_compute_rate_limit`` + ``_get_session`` for
the DB-bound endpoints), same 422-with-stable-code error mapping for
domain errors, same correlation-tagged endpoint timing log lines.
"""

from __future__ import annotations

import logging as _log
import time
from itertools import islice
from typing import Any

from fastapi import Depends, HTTPException, Request

from ...api import translate as _translate
from ...errors import CrossTenantJoinError, SparqlError
from ...translate.graph_shape import query_patterns, widen_projection
from ...translate.parser import parse_sparql
from ..app import app
from ..mapping import _resolver_from_request
from ..models import (
    _MAX_RESULT_DOCS,
    GraphShape,
    RawAqlRequest,
    RawAqlResponse,
    SparqlExecuteRequest,
    SparqlExecuteResponse,
    SparqlExplainResponse,
    SparqlProfileResponse,
    TranslateRequest,
    TranslateResponse,
    ValidateRequest,
    ValidateResponse,
)
from ..observability import log_endpoint_timing
from ..recording import record_question
from ..security import (
    _check_compute_rate_limit,
    _get_optional_session,
    _get_session,
    _sanitize_error,
    _Session,
    _translate_errors,
)
from ..tenant import resolve_tenant_id

logger = _log.getLogger("arango_sparql.service.routes.sparql")


def _materialise_cursor(cursor: Any, cap: int) -> tuple[list[Any], bool]:
    """Drain a python-arango cursor up to ``cap`` rows.

    Returns ``(rows, truncated)`` — ``truncated`` is ``True`` iff the
    cursor still had more rows after the cap was reached. We avoid
    ``list(cursor)`` so a runaway query can't OOM the worker; the
    ``islice`` walks one row at a time and the trailing ``next``
    probe surfaces the truncation flag without buffering more rows.
    """
    rows: list[Any] = list(islice(cursor, cap))
    truncated = False
    try:
        next(iter(cursor))
        truncated = True
    except StopIteration:
        truncated = False
    except Exception:
        # ``next`` on an exhausted python-arango cursor can surface as
        # a CursorEmptyError depending on driver version. Treat any
        # post-cap probe failure as "no more rows" rather than masking
        # a real error — the original ``list(islice(...))`` would have
        # raised first if the cursor itself was broken.
        truncated = False
    return rows, truncated


def _resolver_or_422(req: Any, *, analyzer_bundle: Any | None = None) -> Any:
    """Build a resolver from ``req``; map malformed-Turtle errors to 422.

    Centralised so every route that accepts an inline ontology renders
    the same 422 ``{error, code}`` shape on a parse failure rather than
    bubbling the raw rdflib exception through ``_translate_errors``
    (which would surface as a 500).

    When *analyzer_bundle* is supplied, the discovered physical mapping is
    merged into the inline ontology (see :func:`_resolver_from_request`).
    """
    try:
        return _resolver_from_request(req, analyzer_bundle=analyzer_bundle)
    except Exception as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "error": _sanitize_error(f"ontology_ttl parse failed: {exc}"),
                "code": "E_SCHEMA_RESOLVE",
            },
        ) from exc


def _analyzer_bundle_for_session(session: _Session | None) -> Any | None:
    """Best-effort: the analyzer-discovered mapping bundle for *session*'s DB.

    Cache-only (never blocks on the analyzer): serves the bundle the UI
    already fetched via ``/schema/introspect``; on a miss a background
    analysis starts (``service/schema_warm.py``) and this returns ``None``
    — "no enrichment" — instead of making a translate wait minutes. Also
    ``None`` when there is no session or the read fails for any reason. A translate/execute must never fail
    merely because this optional enrichment was unavailable (e.g. the
    analyzer extra is not installed, or the DB is unreachable for schema
    discovery).
    """
    if session is None:
        return None
    try:
        # Lazy import: both route modules register on the same ``app`` at
        # import time; importing at call time avoids any load-order cycle.
        from .schema import _session_schema

        bundle, _cache_hit, _warming = _session_schema(
            session,
            strategy="auto",
        )
        return bundle
    except Exception as exc:  # noqa: BLE001 — enrichment is strictly optional
        logger.debug("analyzer enrichment skipped: %s", exc)
        return None


@app.post("/translate", response_model=TranslateResponse)
def translate_endpoint(
    req: TranslateRequest,
    request: Request,
    _: None = Depends(_check_compute_rate_limit),
    session: _Session | None = Depends(_get_optional_session),
) -> TranslateResponse:
    """Translate SPARQL to AQL (parse + visit + emit, no DB access).

    Honours the per-request ``X-Tenant-Id`` header (PRD §6.5.1) so a
    multi-tenant ontology emits the correct ``FILTER doc.<tenant_field>
    == @tenant`` predicate even when the route does no DB access.

    Works without a session (offline translation). When the caller *is*
    connected, the analyzer-discovered schema for that database is merged
    into the inline ontology so a class the user declared but did not
    annotate still resolves to its discovered ``phys:collectionName``.
    """
    # Sizes only: query text never reaches a log (PRD §17.3). The error lines
    # below log the error's code and class, not its message, which quotes the
    # query (a parse error names the offending token, a schema error the IRI).
    logger.info(
        "translate request: sparql_len=%s, ontology_ttl_len=%s",
        len(req.sparql) if req.sparql else 0,
        len(req.ontology_ttl) if req.ontology_ttl else 0,
    )
    resolver = _resolver_or_422(req, analyzer_bundle=_analyzer_bundle_for_session(session))
    tenant_id = resolve_tenant_id(request)
    t0 = time.perf_counter()
    try:
        result = _translate(
            req.sparql,
            resolver=resolver,
            params=req.params,
            tenant_id=tenant_id,
        )
    except CrossTenantJoinError as exc:
        logger.warning("translate %s (%s)", exc.code, type(exc).__name__)
        log_endpoint_timing(
            "/translate",
            round((time.perf_counter() - t0) * 1000, 1),
            status="error",
            code=exc.code,
            sparql_len=len(req.sparql or ""),
        )
        raise HTTPException(
            status_code=422,
            detail={"error": _sanitize_error(str(exc)), "code": exc.code},
        ) from exc
    except SparqlError as exc:
        logger.warning("translate %s (%s)", exc.code, type(exc).__name__)
        log_endpoint_timing(
            "/translate",
            round((time.perf_counter() - t0) * 1000, 1),
            status="error",
            code=exc.code,
            sparql_len=len(req.sparql or ""),
        )
        raise HTTPException(
            status_code=422,
            detail={"error": _sanitize_error(str(exc)), "code": exc.code},
        ) from exc
    elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)

    log_endpoint_timing(
        "/translate",
        elapsed_ms,
        sparql_len=len(req.sparql or ""),
        aql_len=len(result.aql or ""),
        warnings=len(result.warnings or []),
    )
    return TranslateResponse(
        aql=result.aql,
        bind_vars=result.bind_vars,
        warnings=result.warnings,
        schema_warnings=result.schema_warnings,
        elapsed_ms=elapsed_ms,
    )


_RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"


def _with_kind(pattern: dict[str, Any], resolver: Any) -> dict[str, Any]:
    """*pattern* with ``kind``, so the graph view need not guess from values: ``type`` (an
    ``a :Class``), ``relationship`` (an object property: an edge between entities) or
    ``property`` (a data property: a value on its subject). Left out when the schema does not
    say."""
    iri = pattern["predicate"].get("iri")
    if iri is None:
        return pattern
    if iri == _RDF_TYPE:
        return {**pattern, "kind": "type"}
    try:
        resolved = resolver.resolve_property(iri)
    except Exception:  # noqa: BLE001 — an unknown property leaves the view to its default
        return pattern
    return {**pattern, "kind": "relationship" if resolved.is_object_property else "property"}


@app.post("/execute", response_model=SparqlExecuteResponse)
def execute_endpoint(
    req: SparqlExecuteRequest,
    request: Request,
    _: None = Depends(_check_compute_rate_limit),
    session: _Session = Depends(_get_session),
) -> SparqlExecuteResponse:
    """Translate SPARQL → AQL and execute against the connected ArangoDB.

    Honours the per-request ``X-Tenant-Id`` header (PRD §6.5.1) — every
    PG/LPG ``FOR doc IN @@coll`` loop the visitor emits will carry a
    ``FILTER doc.<tenant_field> == @tenant_id`` predicate when the
    underlying ontology declares ``phys:tenantField``.

    The analyzer-discovered schema for the connected database is merged
    into the inline ontology (inline annotations win) so an unannotated
    class resolves to its discovered collection rather than guessing.
    """
    resolver = _resolver_or_422(req, analyzer_bundle=_analyzer_bundle_for_session(session))
    tenant_id = resolve_tenant_id(request)
    sparql, graph = req.sparql, None
    if req.graph:
        widened = widen_projection(req.sparql)
        sparql = widened or req.sparql
        try:
            patterns = [_with_kind(p, resolver) for p in query_patterns(req.sparql)]
        except SparqlError:
            patterns = []  # the translation below reports the query's own error
        graph = GraphShape(patterns=patterns, widened=widened is not None)
    t_translate = time.perf_counter()
    try:
        transpiled = _translate(
            sparql,
            resolver=resolver,
            params=req.params,
            tenant_id=tenant_id,
        )
        translate_ms = round((time.perf_counter() - t_translate) * 1000, 1)
    except CrossTenantJoinError as exc:
        logger.warning("execute %s (%s)", exc.code, type(exc).__name__)
        log_endpoint_timing(
            "/execute",
            round((time.perf_counter() - t_translate) * 1000, 1),
            status="error",
            code=exc.code,
            sparql_len=len(req.sparql or ""),
        )
        raise HTTPException(
            status_code=422,
            detail={"error": _sanitize_error(str(exc)), "code": exc.code},
        ) from exc
    except SparqlError as exc:
        logger.warning("execute translate %s (%s)", exc.code, type(exc).__name__)
        log_endpoint_timing(
            "/execute",
            round((time.perf_counter() - t_translate) * 1000, 1),
            status="error",
            code=exc.code,
            sparql_len=len(req.sparql or ""),
        )
        raise HTTPException(
            status_code=422,
            detail={"error": _sanitize_error(str(exc)), "code": exc.code},
        ) from exc

    with _translate_errors("AQL execution failed"):
        t_exec = time.perf_counter()
        cursor = session.db.aql.execute(transpiled.aql, bind_vars=transpiled.bind_vars)
        bindings, truncated = _materialise_cursor(cursor, _MAX_RESULT_DOCS)
        exec_ms = round((time.perf_counter() - t_exec) * 1000, 1)

    elapsed_ms = round(translate_ms + exec_ms, 1)
    warnings = list(transpiled.warnings or [])
    if truncated:
        warnings.append(
            {
                "message": (
                    f"Result set truncated to {_MAX_RESULT_DOCS} rows. "
                    "Refine the query or run against the database directly for the full set."
                ),
                "code": "W_RESULT_TRUNCATED",
            }
        )

    # The graph view's run is the question just asked again (recording.py); when it is
    # recorded, the event carries the widened SPARQL that produced the rows.
    record_question(
        request,
        session,
        query=req.sparql,
        question=req.question,
        again=req.graph,
        client_words=True,
        ran=sparql,
    )
    log_endpoint_timing(
        "/execute",
        elapsed_ms,
        translate_ms=translate_ms,
        exec_ms=exec_ms,
        rows=len(bindings),
        sparql_len=len(req.sparql or ""),
        aql_len=len(transpiled.aql or ""),
        truncated=truncated,
    )
    return SparqlExecuteResponse(
        bindings=bindings,
        aql=transpiled.aql,
        bind_vars=transpiled.bind_vars,
        warnings=warnings,
        elapsed_ms=elapsed_ms,
        translate_ms=translate_ms,
        exec_ms=exec_ms,
        truncated=truncated,
        graph=graph,
    )


@app.post("/execute-aql", response_model=RawAqlResponse)
def execute_aql_endpoint(
    req: RawAqlRequest,
    _: None = Depends(_check_compute_rate_limit),
    session: _Session = Depends(_get_session),
) -> RawAqlResponse:
    """Execute a raw AQL query directly.

    Same security guards as ``/execute`` — rate-limit + session — but
    skips the SPARQL parse and translate steps. Used by the UI's
    "rerun without re-translating" affordance and by power users who
    hand-author AQL against the same session.
    """
    with _translate_errors("AQL execution failed"):
        t_exec = time.perf_counter()
        cursor = session.db.aql.execute(req.aql, bind_vars=req.bind_vars)
        results, truncated = _materialise_cursor(cursor, _MAX_RESULT_DOCS)
        exec_ms = round((time.perf_counter() - t_exec) * 1000, 1)

    warnings: list[dict[str, Any]] = []
    if truncated:
        warnings.append(
            {
                "message": (
                    f"Result set truncated to {_MAX_RESULT_DOCS} rows. "
                    "Refine the query or run against the database directly for the full set."
                ),
                "code": "W_RESULT_TRUNCATED",
            }
        )

    log_endpoint_timing(
        "/execute-aql",
        exec_ms,
        rows=len(results),
        aql_len=len(req.aql or ""),
        truncated=truncated,
    )
    return RawAqlResponse(
        results=results,
        aql=req.aql,
        bind_vars=req.bind_vars,
        warnings=warnings,
        exec_ms=exec_ms,
        truncated=truncated,
    )


@app.post("/explain", response_model=SparqlExplainResponse)
def explain_endpoint(
    req: SparqlExecuteRequest,
    request: Request,
    _: None = Depends(_check_compute_rate_limit),
    session: _Session = Depends(_get_session),
) -> SparqlExplainResponse:
    """Translate SPARQL → AQL, then ask ArangoDB for the AQL execution plan.

    Same payload as ``/execute`` (ontology + SPARQL + optional bind
    params); same auth + rate-limit guards. Differs from ``/execute`` in
    that no rows are materialised — the route only calls
    ``db.aql.explain(query, bind_vars=..., all_plans=False)`` and
    surfaces the planner output. Useful for the "why is my query slow?"
    affordance in the UI without paying the actual execution cost.
    Honours the ``X-Tenant-Id`` header (PRD §6.5.1) so the explained
    plan reflects the tenant-filtered AQL the operator would actually
    run.
    """
    resolver = _resolver_or_422(req, analyzer_bundle=_analyzer_bundle_for_session(session))
    tenant_id = resolve_tenant_id(request)
    t_translate = time.perf_counter()
    try:
        transpiled = _translate(
            req.sparql,
            resolver=resolver,
            params=req.params,
            tenant_id=tenant_id,
        )
        translate_ms = round((time.perf_counter() - t_translate) * 1000, 1)
    except CrossTenantJoinError as exc:
        logger.warning("explain %s (%s)", exc.code, type(exc).__name__)
        log_endpoint_timing(
            "/explain",
            round((time.perf_counter() - t_translate) * 1000, 1),
            status="error",
            code=exc.code,
            sparql_len=len(req.sparql or ""),
        )
        raise HTTPException(
            status_code=422,
            detail={"error": _sanitize_error(str(exc)), "code": exc.code},
        ) from exc
    except SparqlError as exc:
        logger.warning("explain translate %s (%s)", exc.code, type(exc).__name__)
        log_endpoint_timing(
            "/explain",
            round((time.perf_counter() - t_translate) * 1000, 1),
            status="error",
            code=exc.code,
            sparql_len=len(req.sparql or ""),
        )
        raise HTTPException(
            status_code=422,
            detail={"error": _sanitize_error(str(exc)), "code": exc.code},
        ) from exc

    with _translate_errors("AQL EXPLAIN failed"):
        t_explain = time.perf_counter()
        plan = session.db.aql.explain(transpiled.aql, bind_vars=transpiled.bind_vars)
        explain_ms = round((time.perf_counter() - t_explain) * 1000, 1)

    # ``db.aql.explain`` historically returns a dict on success but some
    # driver versions return a Plan object — coerce to a plain dict so
    # the Pydantic shape stays str-keyed JSON for the UI.
    plan_dict: dict[str, Any] = dict(plan) if not isinstance(plan, dict) else plan

    log_endpoint_timing(
        "/explain",
        round(translate_ms + explain_ms, 1),
        translate_ms=translate_ms,
        explain_ms=explain_ms,
        sparql_len=len(req.sparql or ""),
        aql_len=len(transpiled.aql or ""),
        warnings=len(transpiled.warnings or []),
    )
    return SparqlExplainResponse(
        sparql=req.sparql,
        aql=transpiled.aql,
        bind_vars=transpiled.bind_vars,
        plan=plan_dict,
        warnings=list(transpiled.warnings or []),
        translate_ms=translate_ms,
    )


@app.post("/profile", response_model=SparqlProfileResponse)
def profile_endpoint(
    req: SparqlExecuteRequest,
    request: Request,
    _: None = Depends(_check_compute_rate_limit),
    session: _Session = Depends(_get_session),
) -> SparqlProfileResponse:
    """Translate SPARQL → AQL and execute with full per-stage profiling.

    ``profile=2`` asks ArangoDB to attach per-node timings + per-stage
    statistics to the cursor, which the route surfaces verbatim in the
    response under ``profile``. Result rows are still materialised (and
    capped at :data:`_MAX_RESULT_DOCS`) so the UI can show the slow
    stage **and** the rows it actually produced side-by-side. Honours
    the ``X-Tenant-Id`` header (PRD §6.5.1) so the profiled AQL is the
    tenant-scoped query the operator would actually run.
    """
    resolver = _resolver_or_422(req, analyzer_bundle=_analyzer_bundle_for_session(session))
    tenant_id = resolve_tenant_id(request)
    t_translate = time.perf_counter()
    try:
        transpiled = _translate(
            req.sparql,
            resolver=resolver,
            params=req.params,
            tenant_id=tenant_id,
        )
        translate_ms = round((time.perf_counter() - t_translate) * 1000, 1)
    except CrossTenantJoinError as exc:
        logger.warning("profile %s (%s)", exc.code, type(exc).__name__)
        log_endpoint_timing(
            "/profile",
            round((time.perf_counter() - t_translate) * 1000, 1),
            status="error",
            code=exc.code,
            sparql_len=len(req.sparql or ""),
        )
        raise HTTPException(
            status_code=422,
            detail={"error": _sanitize_error(str(exc)), "code": exc.code},
        ) from exc
    except SparqlError as exc:
        logger.warning("profile translate %s (%s)", exc.code, type(exc).__name__)
        log_endpoint_timing(
            "/profile",
            round((time.perf_counter() - t_translate) * 1000, 1),
            status="error",
            code=exc.code,
            sparql_len=len(req.sparql or ""),
        )
        raise HTTPException(
            status_code=422,
            detail={"error": _sanitize_error(str(exc)), "code": exc.code},
        ) from exc

    with _translate_errors("AQL profiled execution failed"):
        t_exec = time.perf_counter()
        cursor = session.db.aql.execute(
            transpiled.aql,
            bind_vars=transpiled.bind_vars,
            profile=2,
        )
        bindings, truncated = _materialise_cursor(cursor, _MAX_RESULT_DOCS)
        # python-arango exposes the profile blob via ``cursor.profile()``
        # on recent drivers and via a ``profile`` attribute on older
        # ones. Probe both so a slightly older driver doesn't 500 here.
        profile_data: Any = None
        getter = getattr(cursor, "profile", None)
        if callable(getter):
            profile_data = getter()
        elif getter is not None:
            profile_data = getter
        exec_ms = round((time.perf_counter() - t_exec) * 1000, 1)

    record_question(request, session, query=req.sparql, question=req.question, client_words=True)
    profile_dict: dict[str, Any] = (
        dict(profile_data)
        if profile_data is not None and not isinstance(profile_data, dict)
        else (profile_data or {})
    )

    warnings = list(transpiled.warnings or [])
    if truncated:
        warnings.append(
            {
                "message": (
                    f"Result set truncated to {_MAX_RESULT_DOCS} rows. "
                    "Refine the query or run against the database directly for the full set."
                ),
                "code": "W_RESULT_TRUNCATED",
            }
        )

    log_endpoint_timing(
        "/profile",
        round(translate_ms + exec_ms, 1),
        translate_ms=translate_ms,
        exec_ms=exec_ms,
        rows=len(bindings),
        sparql_len=len(req.sparql or ""),
        aql_len=len(transpiled.aql or ""),
        truncated=truncated,
    )
    return SparqlProfileResponse(
        sparql=req.sparql,
        aql=transpiled.aql,
        bind_vars=transpiled.bind_vars,
        bindings=bindings,
        truncated=truncated,
        profile=profile_dict,
        warnings=warnings,
        translate_ms=translate_ms,
        exec_ms=exec_ms,
    )


@app.post("/validate", response_model=ValidateResponse)
def validate_endpoint(
    req: ValidateRequest,
    _: None = Depends(_check_compute_rate_limit),
) -> ValidateResponse:
    """Parse-only SPARQL validation (no DB access, no AQL emission).

    Mirrors the Cypher project's ``/validate`` shape: returns a
    ``ValidateResponse`` with a boolean ``valid`` slot and a list of
    ``{code, message}`` error records. ``warnings`` carries the
    transpiler's non-fatal advisories so a fully-passing
    ``valid=true`` can still surface guidance to the operator (e.g.
    "predicate IRI not found in ontology — degrading to local-name
    attribute access").
    """
    t0 = time.perf_counter()
    errors: list[dict[str, str]] = []
    warnings: list[dict[str, Any]] = []
    valid = False
    try:
        parse_sparql(req.sparql)
        valid = True
    except SparqlError as exc:
        errors.append({"code": exc.code, "message": _sanitize_error(str(exc))})
        valid = False

    elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)
    log_endpoint_timing(
        "/validate",
        elapsed_ms,
        valid=valid,
        error_count=len(errors),
        sparql_len=len(req.sparql or ""),
    )
    return ValidateResponse(valid=valid, errors=errors, warnings=warnings)
