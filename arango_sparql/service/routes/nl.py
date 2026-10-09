"""NL → SPARQL endpoints — ``/nl-translate``, ``/nl-explain``, ``/nl-execute``.

Mirror of ``arango_cypher.service.routes.nl`` adapted to the SPARQL
output target. Same dependency-injection chain
(``_check_nl_rate_limit`` for the LLM-heavy routes,
``_check_compute_rate_limit`` for the post-translation execute leg,
``_get_session`` for the DB-bound ``/nl-execute``), same 422-with-
stable-code error mapping for domain errors.

The pipeline itself is constructed lazily inside each route so the
LLM client (which may be expensive to import — vendor SDKs etc.)
isn't pulled in at module load time. The
``_get_pipeline_factory`` indirection is the seam every test uses to
inject a :class:`ScriptedLLMClient` via dependency override.
"""

from __future__ import annotations

import logging as _log
import time
from itertools import islice
from typing import Any

from fastapi import Depends, HTTPException

from ...errors import SparqlError
from ..app import app
from ..mapping import _resolver_from_request
from ..models import (
    _MAX_RESULT_DOCS,
    NlExecuteRequest,
    NlExecuteResponse,
    NlExplainRequest,
    NlExplainResponse,
    NlSamplesRequest,
    NlSamplesResponse,
    NlTranslateRequest,
    NlTranslateResponse,
)
from ..observability import (
    current_llm_provider_and_model,
    log_endpoint_timing,
    log_llm_call,
)
from ..security import (
    _check_compute_rate_limit,
    _check_nl_rate_limit,
    _get_optional_session,
    _get_session,
    _sanitize_error,
    _Session,
    _translate_errors,
)

logger = _log.getLogger("arango_sparql.service.routes.nl")


# ---------------------------------------------------------------------------
# LLM client / pipeline factory — overridable via FastAPI dependency_overrides.
# ---------------------------------------------------------------------------


def _llm_client_factory() -> Any:
    """Return the process-default LLM client, or ``None``.

    Default factory reads env vars via
    :func:`arango_sparql.nl2sparql.get_default_client`. Tests override
    this by setting ``app.dependency_overrides[_llm_client_factory]``
    to a closure returning a :class:`ScriptedLLMClient`. Returning
    ``None`` here surfaces a 503 in the route — the rule-based Cypher
    fallback has no SPARQL analogue so we fail closed instead.

    The import is deferred until call time to break the
    ``service.__init__`` → ``routes`` → ``nl`` → ``nl2sparql`` →
    ``service.models`` cycle that arose when the pipeline grew its
    own Pydantic models.
    """
    from ...nl2sparql import get_default_client

    return get_default_client()


def _schema_summary_for(req: Any, session: _Session | None) -> str:
    """Classes + data properties from the connected database's analyzer
    mapping, for the NL prompt (``nl2sparql/schema_summary.py``).

    Cache-only: uses the bundle the Workbench already loaded via
    ``/schema/introspect`` and never waits for an analysis (a miss starts a
    background one and the prompt simply has no summary yet). Rendered only
    when the request's ontology (or the bundle's own OWL) names the default
    ``:`` namespace the terms must be written in. Never fails the request:
    the summary is an enrichment.
    """
    if session is None:
        return ""
    try:
        from ...nl2sparql.schema_summary import build_schema_summary, default_namespace
        from .sparql import _analyzer_bundle_for_session

        bundle = _analyzer_bundle_for_session(session)
        if bundle is None:
            return ""
        namespace = default_namespace(getattr(req, "ontology_ttl", None), getattr(bundle, "owl_turtle", None))
        if not namespace:
            return ""
        return build_schema_summary(bundle, namespace=namespace)
    except Exception as exc:  # noqa: BLE001 — enrichment only
        logger.warning("NL schema summary skipped: %s", exc)
        return ""


def _pipeline_for(
    *,
    client: Any,
    req: Any,
    max_repairs: int,
    session: _Session | None = None,
) -> Any:
    """Construct an :class:`NlPipeline` for the given request.

    Encapsulates the resolver-from-request adapter (which can raise on
    malformed Turtle) and the client-not-configured guard so each
    route handler stays linear. Raises ``HTTPException(503)`` when no
    LLM client is configured — the operator's signal to set
    ``NL2SPARQL_API_KEY`` (or to mount a dependency override in
    tests).
    """
    if client is None:
        raise HTTPException(
            status_code=503,
            detail={
                "error": (
                    "No NL2SPARQL provider configured. Set NL2SPARQL_PROVIDER + "
                    "NL2SPARQL_API_KEY (and optionally NL2SPARQL_MODEL / "
                    "NL2SPARQL_BASE_URL) to enable the NL pipeline."
                ),
                "code": "E_NL_PROVIDER_UNAVAILABLE",
            },
        )
    from ...nl2sparql import NlPipeline

    resolver = _resolver_or_422(req)
    return NlPipeline(
        client=client,
        resolver=resolver,
        ontology_ttl=getattr(req, "ontology_ttl", None) or "",
        max_repairs=max_repairs,
        schema_summary=_schema_summary_for(req, session),
    )


def _resolver_or_422(req: Any) -> Any:
    """Build a resolver from ``req``; map malformed-Turtle errors to 422.

    Same posture as the SPARQL routes' helper of the same name; copied
    here rather than imported because both files own the conversion of
    a request shape into a 422 envelope and we don't want a cross-
    module import to drag the ``/translate`` route's helpers into this
    module's import graph.
    """
    try:
        return _resolver_from_request(req)
    except Exception as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "error": _sanitize_error(f"ontology_ttl parse failed: {exc}"),
                "code": "E_SCHEMA_RESOLVE",
            },
        ) from exc


def _materialise_cursor(cursor: Any, cap: int) -> tuple[list[Any], bool]:
    """Drain a python-arango cursor up to ``cap`` rows.

    Lifted from :mod:`..routes.sparql` rather than imported because that
    cluster is the canonical owner of the helper and we don't want a
    cross-cluster import to grow over time. Behaviour is identical:
    ``(rows, truncated)`` where ``truncated=True`` means there were
    more rows after the cap.
    """
    rows: list[Any] = list(islice(cursor, cap))
    truncated = False
    try:
        next(iter(cursor))
        truncated = True
    except StopIteration:
        truncated = False
    except Exception:
        truncated = False
    return rows, truncated


def _outcome_warnings_to_translation_error(outcome: Any) -> tuple[str, str] | None:
    """Inspect ``outcome.warnings`` for a translation-failure marker.

    Returns ``(code, message)`` if the pipeline returned a failure
    outcome, ``None`` otherwise. Used to map a pipeline failure (which
    never raises — see :class:`NlPipeline._failure_outcome`) into a 422
    HTTP response so the route layer's error envelope stays uniform
    with the deterministic ``/translate`` route.
    """
    if outcome.aql:
        return None
    for w in outcome.warnings:
        code = w.get("code")
        if code in (
            "W_NL_TRANSLATION_FAILED",
            "E_SPARQL_PARSE",
            "E_SPARQL_UNSUPPORTED",
            "E_SCHEMA_RESOLVE",
            "E_AQL_EMIT",
        ):
            return code, str(w.get("message") or "translation failed")
    return None


def _http_422_from_outcome(outcome: Any) -> HTTPException:
    """Build a 422 HTTPException whose body matches ``/translate``'s shape."""
    pair = _outcome_warnings_to_translation_error(outcome)
    code, message = pair or ("W_NL_TRANSLATION_FAILED", "translation failed")
    return HTTPException(
        status_code=422,
        detail={
            "error": _sanitize_error(message),
            "code": code,
            "nl": outcome.nl,
            "sparql": outcome.sparql,
            "llm_calls": outcome.llm_calls,
            "cost_usd": outcome.cost_usd,
            "latency_ms": outcome.latency_ms,
            "repaired": outcome.repaired,
        },
    )


def _log_outcome(endpoint: str, outcome: Any) -> None:
    """Emit one ``log_llm_call`` per audit record + an endpoint-timing line."""
    provider, model = current_llm_provider_and_model()
    for record in outcome.llm_call_records:
        log_llm_call(
            endpoint=endpoint,
            provider=record.provider or provider,
            model=record.model or model,
            prompt_tokens=record.prompt_tokens,
            completion_tokens=record.completion_tokens,
            cached_tokens=record.cached_tokens,
            latency_ms=record.latency_ms,
            cost_usd=record.cost_usd,
            method="llm" if not record.error else "llm_error",
            repaired=outcome.repaired,
        )
    log_endpoint_timing(
        endpoint,
        float(outcome.latency_ms),
        nl_len=len(outcome.nl or ""),
        sparql_len=len(outcome.sparql or ""),
        aql_len=len(outcome.aql or ""),
        warnings=len(outcome.warnings or []),
        llm_calls=outcome.llm_calls,
        repaired=outcome.repaired,
        cost_usd=outcome.cost_usd,
    )


# ---------------------------------------------------------------------------
# /nl-translate — NL → SPARQL → AQL, no DB access
# ---------------------------------------------------------------------------


@app.post("/nl-translate", response_model=NlTranslateResponse)
def nl_translate_endpoint(
    req: NlTranslateRequest,
    _: None = Depends(_check_nl_rate_limit),
    client: Any = Depends(_llm_client_factory),
    session: _Session | None = Depends(_get_optional_session),
) -> NlTranslateResponse:
    """Translate a natural language question into SPARQL 1.1, then AQL.

    No database access — the pipeline runs the LLM, parses the
    response, and feeds the SPARQL through the deterministic
    transpiler. A repair loop fires up to ``max_repairs`` times if
    the transpiler rejects the LLM's output.
    """
    pipeline = _pipeline_for(client=client, req=req, max_repairs=req.max_repairs, session=session)
    t0 = time.perf_counter()
    outcome = pipeline.run(req.nl)
    # Pipeline already records its own latency; ``t0`` is for tests
    # that monkeypatch ``time.perf_counter`` and want to assert this
    # route's wall-clock cost specifically.
    _log.getLogger("arango_sparql.service.routes.nl.timing").debug(
        "nl_translate route returned in %.2fms (pipeline %.2fms)",
        (time.perf_counter() - t0) * 1000,
        outcome.latency_ms,
    )
    if not outcome.aql:
        _log_outcome("/nl-translate", outcome)
        raise _http_422_from_outcome(outcome)
    _log_outcome("/nl-translate", outcome)
    return NlTranslateResponse(
        nl=outcome.nl,
        sparql=outcome.sparql,
        aql=outcome.aql,
        bind_vars=outcome.bind_vars,
        warnings=outcome.warnings,
        llm_calls=outcome.llm_calls,
        cost_usd=outcome.cost_usd,
        latency_ms=outcome.latency_ms,
        repaired=outcome.repaired,
    )


# ---------------------------------------------------------------------------
# /nl-explain — NL → SPARQL → AQL + LLM-generated plain-English summary
# ---------------------------------------------------------------------------


@app.post("/nl-explain", response_model=NlExplainResponse)
def nl_explain_endpoint(
    req: NlExplainRequest,
    _: None = Depends(_check_nl_rate_limit),
    client: Any = Depends(_llm_client_factory),
    session: _Session | None = Depends(_get_optional_session),
) -> NlExplainResponse:
    """Translate (if NL provided) and ask the LLM to explain the resulting SPARQL.

    Either ``nl`` or ``sparql`` (or both) must be set; if neither is
    present the route returns 422 to match the ``min_length=1`` rule
    on ``/nl-translate``'s NL field.
    """
    if not (req.nl or req.sparql):
        raise HTTPException(
            status_code=422,
            detail={
                "error": "Either nl or sparql must be provided",
                "code": "E_NL_EXPLAIN_INPUT",
            },
        )

    pipeline = _pipeline_for(client=client, req=req, max_repairs=req.max_repairs, session=session)
    try:
        outcome = pipeline.explain(nl=req.nl, sparql=req.sparql)
    except SparqlError as exc:
        # The SPARQL-only path uses the deterministic translator
        # directly and may raise — surface as a 422 the same way the
        # ``/translate`` route does.
        log_endpoint_timing(
            "/nl-explain",
            0.0,
            status="error",
            code=exc.code,
            sparql_len=len(req.sparql or ""),
        )
        raise HTTPException(
            status_code=422,
            detail={"error": _sanitize_error(str(exc)), "code": exc.code},
        ) from exc

    _log_outcome("/nl-explain", outcome)
    if req.nl and not outcome.aql:
        # Translation failed AND the user wanted a translation —
        # surface as 422 like /nl-translate does. SPARQL-only callers
        # (``req.nl`` is None) get the partial response with an empty
        # AQL slot and a warning so they can still see the explanation.
        raise _http_422_from_outcome(outcome)
    return NlExplainResponse(
        nl=outcome.nl,
        sparql=outcome.sparql,
        aql=outcome.aql,
        bind_vars=outcome.bind_vars,
        warnings=outcome.warnings,
        llm_calls=outcome.llm_calls,
        cost_usd=outcome.cost_usd,
        latency_ms=outcome.latency_ms,
        repaired=outcome.repaired,
        explanation=outcome.explanation,
    )


# ---------------------------------------------------------------------------
# /nl-execute — full NL → SPARQL → AQL → execute pipeline
# ---------------------------------------------------------------------------


@app.post("/nl-execute", response_model=NlExecuteResponse)
def nl_execute_endpoint(
    req: NlExecuteRequest,
    _: None = Depends(_check_nl_rate_limit),
    __: None = Depends(_check_compute_rate_limit),
    session: _Session = Depends(_get_session),
    client: Any = Depends(_llm_client_factory),
) -> NlExecuteResponse:
    """Translate NL → SPARQL → AQL and execute against the connected ArangoDB.

    Both rate-limit buckets fire — the LLM-bucket bounds the
    expensive translate leg and the compute bucket bounds the
    cursor-materialisation cost. Same auth posture as ``/execute``.
    """
    pipeline = _pipeline_for(client=client, req=req, max_repairs=req.max_repairs, session=session)
    outcome = pipeline.run(req.nl)
    if not outcome.aql:
        _log_outcome("/nl-execute", outcome)
        raise _http_422_from_outcome(outcome)

    with _translate_errors("AQL execution failed"):
        t_exec = time.perf_counter()
        cursor = session.db.aql.execute(outcome.aql, bind_vars=outcome.bind_vars)
        bindings, truncated = _materialise_cursor(cursor, _MAX_RESULT_DOCS)
        exec_ms = int((time.perf_counter() - t_exec) * 1000)

    warnings = list(outcome.warnings or [])
    if truncated:
        warnings.append(
            {
                "code": "W_RESULT_TRUNCATED",
                "message": (
                    f"Result set truncated to {_MAX_RESULT_DOCS} rows. "
                    "Refine the question or run the AQL against the database directly."
                ),
            }
        )

    _log_outcome("/nl-execute", outcome)
    log_endpoint_timing(
        "/nl-execute",
        float(outcome.latency_ms + exec_ms),
        rows=len(bindings),
        truncated=truncated,
        repaired=outcome.repaired,
    )

    return NlExecuteResponse(
        nl=outcome.nl,
        sparql=outcome.sparql,
        aql=outcome.aql,
        bind_vars=outcome.bind_vars,
        warnings=warnings,
        llm_calls=outcome.llm_calls,
        cost_usd=outcome.cost_usd,
        latency_ms=outcome.latency_ms,
        repaired=outcome.repaired,
        bindings=bindings,
        truncated=truncated,
        exec_ms=exec_ms,
    )


# ---------------------------------------------------------------------------
# /nl-samples — schema-derived NL question suggestions (no DB access)
# ---------------------------------------------------------------------------


@app.post("/nl-samples", response_model=NlSamplesResponse)
def nl_samples_endpoint(
    req: NlSamplesRequest,
    _: None = Depends(_check_nl_rate_limit),
    client: Any = Depends(_llm_client_factory),
) -> NlSamplesResponse:
    """Return representative NL questions for the request's OWL/Turtle schema.

    Seeds the UI "Ask" suggestions dropdown. Unlike ``/nl-translate``
    this route does **not** require a configured LLM provider: when none
    is available (or ``use_llm`` is false) it falls back to deterministic
    rule-based generation from the ontology's classes and object
    properties, so suggestions are available the moment a schema is
    imported or introspected. No database access.
    """
    from ...nl2sparql import suggest_nl_queries

    t0 = time.perf_counter()
    queries = suggest_nl_queries(
        req.ontology_ttl,
        count=req.count,
        use_llm=req.use_llm,
        client=client if req.use_llm else None,
    )
    elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)
    log_endpoint_timing(
        "/nl-samples",
        elapsed_ms,
        count=len(queries),
        use_llm=bool(req.use_llm),
    )
    return NlSamplesResponse(queries=queries, elapsed_ms=elapsed_ms)
