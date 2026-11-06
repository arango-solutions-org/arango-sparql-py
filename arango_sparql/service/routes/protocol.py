"""W3C SPARQL 1.1 Protocol endpoint — ``GET`` / ``POST`` ``/sparql``.

Implements the contract documented in PRD §5.2:

* ``GET /sparql`` (no query) → Service Description as ``text/turtle``.
* ``GET /sparql?query=…`` → Translate + execute; respond per ``Accept``.
* ``POST /sparql`` body ``application/sparql-query`` → same.
* ``POST /sparql`` body ``application/x-www-form-urlencoded`` with
  ``query=…`` → same.
* ``POST /sparql`` body ``application/sparql-update`` (or any body
  whose leading keyword is a SPARQL Update operation) → ``405``
  with the typed code ``E_UPDATE_UNSUPPORTED`` and an ``Allow``
  response header.

The route layer composes the four helpers in
:mod:`arango_sparql.service.protocol`:

* :func:`negotiate.negotiate_media_type` for ``Accept`` resolution
  and the per-form priority list.
* :func:`update_detect.is_sparql_update` for body-level Update
  detection (the ``Content-Type: application/sparql-update`` case
  is decided directly from the header).
* :func:`results.render_select` / :func:`results.render_ask` for
  W3C result-format serialisation.
* :func:`service_description.render_service_description` for the
  no-query GET response.

The route does *not* re-implement schema acquisition — it delegates
to :func:`arango_sparql.service.routes.schema._read_or_warm`, which
serves the cached schema and never blocks on the analyzer: a database
not yet analyzed answers ``503 E_SCHEMA_UNAVAILABLE`` + ``Retry-After``
while a background analysis runs (``service/schema_warm.py``).

Headers (PRD §5.2 + §9.1):

* ``Vary: Accept`` — always, so caches don't conflate the four
  result-format variants.
* ``Content-Type`` — the negotiated media type (or
  ``application/json`` for error responses).
* ``Allow: GET, POST, OPTIONS`` — only on the 405 branch.
* ``Retry-After: 30`` — on the 503 ``E_SCHEMA_UNAVAILABLE`` branch.
* ``Warning: 299 - "W_RESULT_TRUNCATED"`` — when the row cap fires.
* ``X-Response-Time``, ``X-Schema-Warnings-Count``,
  ``X-Aql-Bindings-Count`` — observability headers mirroring the
  legacy Foxx service so a browser-side debug panel can read them.
* ``X-Aql-Query-B64`` — only when the request includes
  ``?showAQL=true``; carries the emitted AQL base64-encoded so it
  doesn't break HTTP header parsing.
* ``Access-Control-Expose-Headers`` — declares the X-headers above
  for cross-origin browsers (YASGUI / Microsoft Ontology
  Playground / etc.).
"""

from __future__ import annotations

import base64
import logging as _log
import os
import time
from typing import Any
from urllib.parse import parse_qs

from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response

from ..._env import (
    read_arango_database,
    read_arango_password,
    read_arango_url,
    read_arango_username,
)
from ...api import translate as _translate
from ...errors import (
    CrossTenantJoinError,
    SchemaResolutionError,
    SparqlError,
    SparqlParseError,
    UnsupportedSparqlError,
)
from ...translate.parser import parse_sparql
from ...translate.resolver import SchemaResolver
from .. import control_plane
from ..app import _PUBLIC_MODE, app
from ..models import _MAX_RESULT_DOCS
from ..observability import log_endpoint_timing
from ..protocol.negotiate import (
    QueryForm,
    negotiate_media_type,
    supported_types_for_form,
)
from ..protocol.results import render_ask, render_select
from ..protocol.results_rdf import render_construct
from ..protocol.service_description import render_service_description
from ..protocol.update_detect import is_sparql_update
from ..security import (
    _check_compute_rate_limit,
    _control_plane_session,
    _follow_platform_identity,
    _sanitize_error,
    _service_pkg_candidates,
    _Session,
    _sessions,
    _translate_errors,
)
from ..tenant import resolve_tenant_id
from .schema import _session_schema

logger = _log.getLogger("arango_sparql.service.routes.protocol")


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# The five X-headers we expose for browser-resident SPARQL clients
# (PRD §5.2 CORS posture paragraph). Listed in one place so the
# ``Access-Control-Expose-Headers`` value stays in sync with the
# headers we actually emit.
_EXPOSED_X_HEADERS = (
    "X-Response-Time",
    "X-Schema-Warnings-Count",
    "X-Aql-Bindings-Count",
    "X-Aql-Query-B64",
    "Warning",
)

_ACCESS_CONTROL_EXPOSE = ", ".join(_EXPOSED_X_HEADERS)

# Maximum body size for a POST. Pulled from env so deployments can
# tighten / relax it. Default = 1 MB which is huge for a SPARQL
# query but small enough to refuse a runaway curl-loop.
_DEFAULT_MAX_BODY_BYTES = 1 * 1024 * 1024


def _max_body_bytes() -> int:
    raw = os.getenv("SPARQL_PROTOCOL_MAX_BODY_BYTES", "")
    if not raw.strip():
        return _DEFAULT_MAX_BODY_BYTES
    try:
        return max(1, int(raw))
    except ValueError:
        return _DEFAULT_MAX_BODY_BYTES


# Hard query-runtime cap (server-side). The python-arango driver
# accepts ``max_runtime`` on ``aql.execute``; ArangoDB returns the
# query-killed error code 1500 if it fires.
_DEFAULT_QUERY_TIMEOUT_S = 30.0


def _query_timeout_seconds() -> float:
    raw = os.getenv("SPARQL_PROTOCOL_TIMEOUT_SECONDS", "")
    if not raw.strip():
        return _DEFAULT_QUERY_TIMEOUT_S
    try:
        v = float(raw)
        return v if v > 0 else _DEFAULT_QUERY_TIMEOUT_S
    except ValueError:
        return _DEFAULT_QUERY_TIMEOUT_S


# ---------------------------------------------------------------------------
# Error envelope helpers
# ---------------------------------------------------------------------------


def _error_response(
    *,
    status_code: int,
    code: str,
    message: str,
    extra: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    """Uniform JSON error envelope. Every protocol error response
    travels through here so the body shape ``{error, code, ...}``
    is identical across the eight documented error rows in PRD
    §5.2.
    """

    body: dict[str, Any] = {"error": _sanitize_error(message), "code": code}
    if extra:
        body.update(extra)

    response_headers = {"Vary": "Accept"}
    if headers:
        response_headers.update(headers)
    return JSONResponse(status_code=status_code, content=body, headers=response_headers)


# ---------------------------------------------------------------------------
# Body extraction
# ---------------------------------------------------------------------------


_SPARQL_QUERY_CT = "application/sparql-query"
_SPARQL_UPDATE_CT = "application/sparql-update"
_FORM_CT = "application/x-www-form-urlencoded"


def _content_type_root(request: Request) -> str:
    """Return the ``type/subtype`` portion of ``Content-Type``,
    lower-cased, with ``charset=`` and friends stripped. Returns
    the empty string when no header is present.
    """

    raw = request.headers.get("Content-Type", "")
    if not raw:
        return ""
    return raw.split(";", 1)[0].strip().lower()


async def _extract_query_from_post(request: Request) -> tuple[str, bytes | None]:
    """Pull the SPARQL query string out of a POST request.

    Returns ``(query, raw_body)`` — ``raw_body`` is surfaced so the
    caller can sanity-check the body size (the route enforces a
    byte cap before we even try to parse).

    Raises :class:`HTTPException` with the typed protocol-error
    body when the request is malformed (empty body, missing
    ``query`` form field, oversized body, etc.). Those are 400 /
    413 errors per PRD §5.2.
    """

    body = await request.body()
    if len(body) > _max_body_bytes():
        raise HTTPException(
            status_code=413,
            detail={
                "error": (
                    f"Request body exceeds {_max_body_bytes()} bytes (SPARQL_PROTOCOL_MAX_BODY_BYTES)."
                ),
                "code": "E_REQUEST_TOO_LARGE",
            },
        )

    ct = _content_type_root(request)
    if ct == _SPARQL_QUERY_CT:
        try:
            return body.decode("utf-8"), body
        except UnicodeDecodeError as exc:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "Body must be UTF-8 for application/sparql-query.",
                    "code": "E_SPARQL_PARSE",
                },
            ) from exc
    if ct == _FORM_CT:
        try:
            decoded = body.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "Form body must be UTF-8.",
                    "code": "E_SPARQL_PARSE",
                },
            ) from exc
        # ``parse_qs`` honours ``+`` and ``%XX`` decoding per the
        # ``application/x-www-form-urlencoded`` standard. ``query``
        # is the documented form field name (W3C SPARQL Protocol
        # §2.1.3).
        parsed = parse_qs(decoded, keep_blank_values=False)
        if "query" not in parsed or not parsed["query"]:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": ("POST application/x-www-form-urlencoded requires a non-empty 'query' field."),
                    "code": "E_SPARQL_PARSE",
                },
            )
        return parsed["query"][0], body
    if ct == _SPARQL_UPDATE_CT:
        # Caller should have already short-circuited on this CT.
        # Surface a non-empty query so the calling code can take
        # the Update-rejection branch unambiguously.
        try:
            return body.decode("utf-8", errors="replace"), body
        except Exception:
            return "", body
    # Unknown / missing CT — fall back to treating the body as
    # raw SPARQL. SPARQL clients in the wild (Apache Jena ``arq``
    # with ``--data``) sometimes omit Content-Type altogether.
    if not body:
        raise HTTPException(
            status_code=400,
            detail={
                "error": (
                    "POST /sparql requires a body. Set Content-Type to "
                    "'application/sparql-query' or post a "
                    "'application/x-www-form-urlencoded' form with a "
                    "'query=' field."
                ),
                "code": "E_SPARQL_PARSE",
            },
        )
    try:
        return body.decode("utf-8"), body
    except UnicodeDecodeError as exc:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "Body must be UTF-8.",
                "code": "E_SPARQL_PARSE",
            },
        ) from exc


# ---------------------------------------------------------------------------
# Session resolution (with env-default fallback)
# ---------------------------------------------------------------------------


def _resolve_arango_client():
    """Return the patched ``ArangoClient`` from the service package
    if a test has injected one, else the real driver class. Same
    indirection the ``connect`` route module uses so the env-default
    code path here is reachable in tests without spinning a real
    ArangoDB.
    """

    for pkg in _service_pkg_candidates():
        cls = getattr(pkg, "ArangoClient", None)
        if cls is not None:
            return cls
    # Imported lazily so the protocol module itself doesn't hard-
    # depend on the driver — keeps the import graph clean for
    # unit-testing the negotiate / results helpers.
    from arango import ArangoClient as _Real

    return _Real


_env_default_session: _Session | None = None


def _build_env_default_session() -> _Session:
    """Construct (and cache) a session that points at the env-
    default ArangoDB. Used in non-public mode when the caller
    didn't supply a session token — PRD §5.2 says
    ``curl /sparql`` Just Works against a developer's
    ``localhost:8529``.

    The cached session lives in this module's
    :data:`_env_default_session` rather than in ``_sessions`` so
    the LRU eviction pool isn't polluted by a session that's
    deliberately not user-facing.
    """

    global _env_default_session
    if _env_default_session is not None and not _env_default_session.expired:
        _env_default_session.touch()
        return _env_default_session

    url = read_arango_url(caller="protocol_env_default") or "http://localhost:8529"
    db_name = read_arango_database(default="_system", caller="protocol_env_default") or "_system"
    username = read_arango_username(default="root", caller="protocol_env_default") or "root"
    password = read_arango_password(caller="protocol_env_default") or ""

    cls = _resolve_arango_client()
    client = cls(hosts=url)
    db = client.db(db_name, username=username, password=password)
    _env_default_session = _Session(token="<env-default>", db=db, client=client)
    return _env_default_session


def _resolve_protocol_session(request: Request) -> _Session:
    """Resolve the session a protocol request runs under.

    Lookup order (PRD §5.2 "Session binding" paragraph):

    1. ``X-Arango-Session`` header (preferred — survives platform
       proxies that rewrite ``Authorization``).
    2. ``Authorization: Bearer <token>``.
    3. ``?session=<token>`` query parameter.
    4. Env-default connection — only in non-public mode.

    Raises ``HTTPException`` 401 (with ``WWW-Authenticate: Bearer``)
    when no source resolves and we're in public mode, mapping to
    PRD §5.2's ``E_AUTH_REQUIRED`` row.
    """

    if control_plane.mode():
        # The context is the only credential: no ``?session=``, and no env default.
        return _control_plane_session(request)
    bearer = _bearer_token(request.headers.get("Authorization", ""))
    token = request.headers.get("X-Arango-Session") or bearer or request.query_params.get("session", "")
    # The Authorization header is free to carry the platform login unless it
    # carried the session token itself.
    via_session_header = bool(token) and token != bearer
    if token:
        session = _sessions.get(token)
        if session is None or session.expired:
            if session and session.expired:
                _sessions.pop(token, None)
                try:
                    session.client.close()
                except Exception:
                    # Closing a stale client should not block the
                    # 401 response; the LRU evictor will mop up.
                    pass
            raise HTTPException(
                status_code=401,
                detail={
                    "error": "Session expired or invalid",
                    "code": "E_AUTH_REQUIRED",
                },
                headers={"WWW-Authenticate": "Bearer"},
            )
        # A platform session follows the caller's current login and refuses a
        # request that does not carry it, as every other route does: serving
        # it on the stored login would let anyone holding the session token
        # query as the user who opened it.
        refusal = _follow_platform_identity(session, request, via_session_header=via_session_header)
        if refusal is not None:
            raise HTTPException(
                status_code=401,
                detail={"error": refusal, "code": "E_AUTH_REQUIRED"},
                headers={"WWW-Authenticate": "Bearer"},
            )
        session.touch()
        return session

    if _PUBLIC_MODE:
        raise HTTPException(
            status_code=401,
            detail={
                "error": (
                    "Public-mode deployments require a session token. "
                    "Pass it via X-Arango-Session, Authorization: Bearer, "
                    "or ?session=<token>."
                ),
                "code": "E_AUTH_REQUIRED",
            },
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Default mode: env-default fallback. This keeps the
    # ``curl localhost:8000/sparql?query=…`` developer story working.
    return _build_env_default_session()


def _bearer_token(header_value: str) -> str:
    """Extract the token from an ``Authorization: Bearer …`` header,
    or the empty string when the header isn't a Bearer header.

    Case-sensitive on purpose: behind the platform gateway the header holds
    the user's platform login as ``bearer <jwt>``, which is not a session
    token and must not be looked up as one (that would refuse the request).
    """

    if not header_value:
        return ""
    if header_value.startswith("Bearer "):
        return header_value[7:].strip()
    return ""


# ---------------------------------------------------------------------------
# Schema acquisition
# ---------------------------------------------------------------------------


def _resolver_for_session(session: _Session) -> SchemaResolver:
    """Acquire (or pull from cache) the :class:`MappingBundle` for
    *session*'s database and wrap it in a :class:`SchemaResolver`.

    The schema advisories are the translation's (``schema_warnings`` of its
    result, #157): resolving the query's IRIs raises them. A failed acquisition
    raises ``HTTPException`` 503 with ``E_SCHEMA_UNAVAILABLE`` and
    a ``Retry-After: 30`` header — see PRD §5.2 row 5.
    """

    try:
        bundle, _cache_hit, _warming = _session_schema(
            session,
            strategy="auto",
        )
    except HTTPException:
        # The schema route already maps the
        # "no-acquisition-path-available" case to 503 with
        # ``E_SCHEMA_UNAVAILABLE`` (PRD §6.3.4 row 4); re-raise
        # with the protocol-specific Retry-After.
        raise HTTPException(
            status_code=503,
            detail={
                "error": ("Schema acquisition is currently unavailable. See /schema/status for details."),
                "code": "E_SCHEMA_UNAVAILABLE",
            },
            headers={"Retry-After": "30"},
        ) from None
    except Exception as exc:
        # Any other acquisition failure (analyzer down, etc.) is a
        # 503 — the protocol route should not surface raw analyzer
        # exceptions because spec-compliant clients can't act on
        # them.
        logger.warning("protocol schema acquisition failed: %s", exc)
        raise HTTPException(
            status_code=503,
            detail={
                "error": _sanitize_error(f"Schema acquisition failed: {exc}"),
                "code": "E_SCHEMA_UNAVAILABLE",
            },
            headers={"Retry-After": "30"},
        ) from exc

    if bundle is None:
        # Not analyzed yet: a background analysis is running
        # (service/schema_warm.py). PRD §5.2 row 5's contract for a
        # schema that is not available yet — 503 + Retry-After — is
        # exactly what a protocol client needs; it never blocks on the
        # multi-minute analyzer.
        raise HTTPException(
            status_code=503,
            detail={
                "error": (
                    "The schema for this database is being analyzed in the background. "
                    "Retry after the indicated delay."
                ),
                "code": "E_SCHEMA_UNAVAILABLE",
            },
            headers={"Retry-After": "30"},
        )

    return SchemaResolver.from_mapping_bundle(bundle)


def _bundle_for_session(session: _Session):
    """Return the cached :class:`MappingBundle` for *session*'s db,
    or ``None`` if no entry is cached yet. Used by ``GET /sparql``
    (no query) so the Service Description can advertise the named
    graphs without forcing a fresh acquisition.
    """

    try:
        # Cache-only (never blocks): a miss starts a background analysis
        # and the Service Description renders with the default graph.
        bundle, _hit, _warming = _session_schema(
            session,
            strategy="auto",
        )
        return bundle
    except Exception:
        # A schema acquisition failure on the SD path is a soft
        # error — return ``None`` and let the SD render with only
        # the default graph rather than 503.
        return None


# ---------------------------------------------------------------------------
# Algebra-form classification
# ---------------------------------------------------------------------------


_ALGEBRA_TO_FORM: dict[str, QueryForm] = {
    "SelectQuery": QueryForm.SELECT,
    "AskQuery": QueryForm.ASK,
    "ConstructQuery": QueryForm.CONSTRUCT,
    "DescribeQuery": QueryForm.DESCRIBE,
}


def _classify_form(algebra: Any) -> QueryForm:
    """Map the rdflib Algebra root node's ``.name`` to a
    :class:`QueryForm`. Defaults to SELECT — the strongest
    fallback since the negotiator's SELECT priority list overlaps
    every concrete tabular type.
    """

    name = getattr(algebra, "name", "")
    return _ALGEBRA_TO_FORM.get(name, QueryForm.SELECT)


# ---------------------------------------------------------------------------
# 405 handling — Update form
# ---------------------------------------------------------------------------


_UPDATE_405_BODY: dict[str, Any] = {
    "error": "E_UPDATE_UNSUPPORTED",
    "message": "SPARQL Update is not supported by this endpoint in v1.x.",
    "code": "E_UPDATE_UNSUPPORTED",
    "see": "https://github.com/ArthurKeen/arango-sparql-py#non-goals",
    "supported_methods": ["GET", "POST"],
    "supported_query_forms": ["SELECT", "ASK", "CONSTRUCT", "DESCRIBE"],
}


def _update_405_response() -> JSONResponse:
    """Build the canonical 405 response for an Update form. Matches
    PRD §5.2 verbatim (the JSON body is asserted by
    ``test_sparql_protocol_errors.py``).
    """

    return JSONResponse(
        status_code=405,
        content=_UPDATE_405_BODY,
        headers={
            "Allow": "GET, POST, OPTIONS",
            "Vary": "Accept",
        },
    )


# ---------------------------------------------------------------------------
# 406 handling — Accept does not match anything supported
# ---------------------------------------------------------------------------


def _not_acceptable_response(form: QueryForm, accept: str | None) -> JSONResponse:
    """PRD §5.2 rule 4: when no offered media type matches, respond
    406 with the supported list so spec-compliant clients (Jena
    ``arq``) can fall back.
    """

    return JSONResponse(
        status_code=406,
        content={
            "error": (f"No supported media type matches Accept {accept or '(unset)'!r} for {form.value}."),
            "code": "E_NOT_ACCEPTABLE",
            "supported_types": supported_types_for_form(form),
            "query_form": form.value,
        },
        headers={"Vary": "Accept"},
    )


# ---------------------------------------------------------------------------
# Response builders
# ---------------------------------------------------------------------------


def _add_observability_headers(
    response: Response,
    *,
    elapsed_ms: float,
    schema_warnings: int,
    bindings: int,
    aql: str | None,
    show_aql: bool,
    truncated: bool,
) -> None:
    """Stamp the X-headers documented in PRD §5.2 onto *response*.
    The ``Warning`` header is added only when the row cap fired
    (per RFC 9110 §12.5.5; legacy code 299 = "Miscellaneous
    persistent warning", which the legacy Foxx service used too).
    """

    response.headers["Vary"] = "Accept"
    response.headers["X-Response-Time"] = f"{elapsed_ms:.1f}ms"
    response.headers["X-Schema-Warnings-Count"] = str(schema_warnings)
    response.headers["X-Aql-Bindings-Count"] = str(bindings)
    response.headers["Access-Control-Expose-Headers"] = _ACCESS_CONTROL_EXPOSE
    if show_aql and aql:
        # Base64 keeps the AQL safe to put in a header (no CRLF,
        # no non-token characters). Clients decode with
        # ``atob(headers["X-Aql-Query-B64"])``.
        response.headers["X-Aql-Query-B64"] = base64.b64encode(aql.encode("utf-8")).decode("ascii")
    if truncated:
        # The ``Warning`` value must be quoted per RFC 9110.
        response.headers["Warning"] = f'299 - "W_RESULT_TRUNCATED row cap {_MAX_RESULT_DOCS} reached"'


def _materialise(cursor: Any, cap: int) -> tuple[list[Any], bool]:
    """Drain *cursor* up to *cap* rows; return ``(rows, truncated)``.

    Same semantics as the existing ``/execute`` path's helper but
    inlined here so the protocol module doesn't have to import a
    private symbol from ``routes.sparql``. Stays defensive against
    older python-arango versions whose cursor iteration semantics
    differ slightly (``CursorEmptyError`` on a probe past the cap).
    """

    from itertools import islice

    rows: list[Any] = list(islice(cursor, cap))
    try:
        next(iter(cursor))
        return rows, True
    except StopIteration:
        return rows, False
    except Exception:
        return rows, False


# ---------------------------------------------------------------------------
# Core execution — shared between GET and POST
# ---------------------------------------------------------------------------


def _truthy_query_param(value: str | None) -> bool:
    """``?showAQL=true`` / ``?showAQL=1`` / ``?showAQL`` (no value)
    all enable the X-Aql-Query-B64 header.
    """

    if value is None:
        return False
    return value.strip().lower() in ("", "1", "true", "yes")


def _show_aql_param(request: Request) -> bool:
    return _truthy_query_param(request.query_params.get("showAQL"))


def _http_exception_to_flat_response(exc: HTTPException) -> JSONResponse:
    """Convert an :class:`HTTPException` raised by a shared helper
    (``_resolve_protocol_session``, ``_resolver_for_session``,
    ``_check_compute_rate_limit``) into the flat ``{error, code, …}``
    envelope the protocol uses everywhere else.

    FastAPI's default exception handler wraps ``detail`` under
    ``{"detail": …}``, which would force every protocol-error
    consumer to special-case the auth/schema rows. Centralising
    the unwrap here keeps the wire shape uniform.
    """

    extra: dict[str, Any] = {}
    if isinstance(exc.detail, dict):
        message = str(exc.detail.get("error", "Request failed"))
        code = str(exc.detail.get("code", "E_INTERNAL"))
        for k, v in exc.detail.items():
            if k not in ("error", "code"):
                extra[k] = v
    else:
        message = str(exc.detail)
        # Heuristic mapping for common bare-string HTTPExceptions
        # so the body still carries a typed code rather than the
        # opaque "E_INTERNAL" placeholder.
        code = {
            401: "E_AUTH_REQUIRED",
            429: "E_RATE_LIMITED",
        }.get(exc.status_code, "E_INTERNAL")

    return _error_response(
        status_code=exc.status_code,
        code=code,
        message=message,
        extra=extra,
        headers=exc.headers,
    )


def _execute_protocol_query(request: Request, query: str) -> Response:
    """End-to-end pipeline for a SPARQL query body.

    1. Rate-limit guard.
    2. Update detection — 405 if the body is an Update.
    3. Parse — 400 ``E_SPARQL_PARSE`` on rdflib failure.
    4. Form classification + Accept negotiation — 406 if no match.
    5. Session resolution + schema acquisition.
    6. Translate — 422 ``E_TRANSLATE_UNSUPPORTED_ALGEBRA`` on the
       visitor's typed errors.
    7. Execute against ArangoDB with ``max_runtime`` — 504
       ``E_TIMEOUT`` if the cursor server-side-times-out.
    8. Render into the chosen media type and return.
    """

    t0 = time.perf_counter()

    # Rate limit — same bucket as other compute endpoints. Surfaces
    # as a flat 429 envelope rather than the wrapped-detail shape.
    try:
        _check_compute_rate_limit(request)
    except HTTPException as exc:
        return _http_exception_to_flat_response(exc)

    # 1) Update detection — body-level.
    if is_sparql_update(query):
        log_endpoint_timing(
            "/sparql",
            round((time.perf_counter() - t0) * 1000, 1),
            status="rejected_update",
            method=request.method,
        )
        return _update_405_response()

    # 2) Parse the query so we know its form for Accept negotiation.
    try:
        parsed = parse_sparql(query)
    except SparqlParseError as exc:
        log_endpoint_timing(
            "/sparql",
            round((time.perf_counter() - t0) * 1000, 1),
            status="error",
            code=exc.code,
            method=request.method,
        )
        return _error_response(
            status_code=400,
            code=exc.code,
            message=str(exc),
        )

    form = _classify_form(parsed.algebra)

    # 3) Negotiate the response media type. If nothing matches,
    # 406 immediately (no point in translating a query whose
    # result we can't return).
    accept_header = request.headers.get("Accept")
    media_type, _offers = negotiate_media_type(accept_header, form)
    if media_type is None:
        log_endpoint_timing(
            "/sparql",
            round((time.perf_counter() - t0) * 1000, 1),
            status="not_acceptable",
            form=form.value,
            method=request.method,
        )
        return _not_acceptable_response(form, accept_header)

    # 4) Session + schema. Both can raise HTTPException; convert
    # those into the flat envelope so the wire shape stays
    # uniform across every error branch.
    try:
        session = _resolve_protocol_session(request)
    except HTTPException as exc:
        return _http_exception_to_flat_response(exc)

    try:
        resolver = _resolver_for_session(session)
    except HTTPException as exc:
        return _http_exception_to_flat_response(exc)

    # 5) Translate — typed errors map to specific status codes.
    # Tenant context: prefer the request's ``X-Tenant-Id`` header, then
    # fall back to the env-default ``ARANGO_SPARQL_DEFAULT_TENANT`` for
    # dev / single-tenant deployments. Single-tenant ontologies (no
    # class declares ``phys:tenantField``) ignore this entirely; tenant-
    # scoped ontologies whose request lacks a tenant context surface
    # ``CrossTenantJoinError`` from the visitor — caught below.
    tenant_id = resolve_tenant_id(request)
    try:
        transpiled = _translate(query, resolver=resolver, tenant_id=tenant_id)
        if transpiled.schema_warnings:
            # A protocol client gets only the count header: log what they were.
            logger.info(
                "protocol query schema warnings (db=%s): %s",
                getattr(getattr(session, "db", None), "name", "?"),
                ", ".join(f"{w.get('code')} {w.get('iri')}" for w in transpiled.schema_warnings),
            )
    except CrossTenantJoinError as exc:
        log_endpoint_timing(
            "/sparql",
            round((time.perf_counter() - t0) * 1000, 1),
            status="error",
            code=exc.code,
            form=form.value,
            method=request.method,
        )
        return _error_response(
            status_code=422,
            code=exc.code,
            message=str(exc),
            extra={"query_form": form.value},
        )
    except UnsupportedSparqlError as exc:
        log_endpoint_timing(
            "/sparql",
            round((time.perf_counter() - t0) * 1000, 1),
            status="error",
            code="E_TRANSLATE_UNSUPPORTED_ALGEBRA",
            form=form.value,
            method=request.method,
        )
        return _error_response(
            status_code=422,
            code="E_TRANSLATE_UNSUPPORTED_ALGEBRA",
            message=str(exc),
            extra={"query_form": form.value},
        )
    except SchemaResolutionError as exc:
        log_endpoint_timing(
            "/sparql",
            round((time.perf_counter() - t0) * 1000, 1),
            status="error",
            code=exc.code,
            method=request.method,
        )
        return _error_response(
            status_code=422,
            code=exc.code,
            message=str(exc),
        )
    except SparqlError as exc:
        log_endpoint_timing(
            "/sparql",
            round((time.perf_counter() - t0) * 1000, 1),
            status="error",
            code=exc.code,
            method=request.method,
        )
        return _error_response(
            status_code=422,
            code=exc.code,
            message=str(exc),
        )

    # 6) Execute. ``max_runtime`` is a python-arango feature —
    # older drivers ignore the kwarg, which is fine: the cap is
    # advisory, not strictly required for correctness.
    timeout = _query_timeout_seconds()
    try:
        with _translate_errors("AQL execution failed"):
            cursor = _execute_with_timeout(
                session.db,
                transpiled.aql,
                transpiled.bind_vars,
                timeout,
            )
            bindings, truncated = _materialise(cursor, _MAX_RESULT_DOCS)
    except HTTPException as exc:
        # ``_translate_errors`` raises HTTPException(500). For
        # query-killed (timeout), we raise our own 504 from
        # ``_execute_with_timeout``; pass it through unchanged.
        if exc.status_code == 504:
            log_endpoint_timing(
                "/sparql",
                round((time.perf_counter() - t0) * 1000, 1),
                status="timeout",
                method=request.method,
            )
            return _error_response(
                status_code=504,
                code="E_TIMEOUT",
                message=str(exc.detail.get("error") if isinstance(exc.detail, dict) else exc.detail),
                extra={
                    "elapsed_ms": round((time.perf_counter() - t0) * 1000, 1),
                    "timeout_s": timeout,
                },
            )
        raise

    # 7) Render the response body in the negotiated media type.
    # SELECT/ASK go through the tabular renderer; CONSTRUCT/DESCRIBE
    # produce RDF (a set of triples) and dispatch to the rdflib-backed
    # graph serialiser. The visitor produces the same wire shape for
    # both RDF forms (a list of ``{subject, predicate, object}`` dicts
    # per cursor row); the renderer flattens and dedupes via
    # :class:`rdflib.Graph`'s set semantics.
    explicit_vars = (
        [str(v) for v in parsed.explicit_projection] if parsed.explicit_projection is not None else None
    )
    if form is QueryForm.ASK:
        ask_value = _coerce_ask_value(bindings)
        body_text = render_ask(media_type, ask_value)
    elif form in (QueryForm.CONSTRUCT, QueryForm.DESCRIBE):
        body_text = render_construct(media_type, bindings)
    else:
        body_text = render_select(
            media_type,
            (b for b in bindings if isinstance(b, dict)),
            explicit_vars=explicit_vars,
        )

    elapsed_ms = round((time.perf_counter() - t0) * 1000, 1)
    response = PlainTextResponse(
        content=body_text,
        media_type=media_type,
    )
    _add_observability_headers(
        response,
        elapsed_ms=elapsed_ms,
        schema_warnings=len(transpiled.schema_warnings),
        bindings=len(bindings),
        aql=transpiled.aql,
        show_aql=_show_aql_param(request),
        truncated=truncated,
    )

    log_endpoint_timing(
        "/sparql",
        elapsed_ms,
        method=request.method,
        form=form.value,
        media_type=media_type,
        rows=len(bindings),
        truncated=truncated,
        sparql_len=len(query),
        aql_len=len(transpiled.aql or ""),
    )
    return response


def _coerce_ask_value(rows: list[Any]) -> bool:
    """Read the boolean answer out of an ASK query's row set.

    The visitor emits ``RETURN LENGTH(<inner>) > 0`` so the cursor
    yields a single boolean row. We tolerate the absence of any row
    (treated as ``False``) and dict-wrapped rows that some drivers
    return for boolean RETURNs.
    """

    if not rows:
        return False
    head = rows[0]
    if isinstance(head, bool):
        return head
    if isinstance(head, (int, float)):
        return bool(head)
    if isinstance(head, dict) and len(head) == 1:
        return bool(next(iter(head.values())))
    # Any other shape is truthiness-coerced — defensive against
    # future visitor changes that wrap the boolean differently.
    return bool(head)


def _execute_with_timeout(db: Any, aql: str, bind_vars: dict[str, Any], timeout_s: float) -> Any:
    """Execute *aql* with a server-side ``max_runtime`` cap. Maps
    the ArangoDB query-killed error (code 1500) to an
    ``HTTPException(504, E_TIMEOUT)`` so the route layer can shape
    the response uniformly.
    """

    try:
        return db.aql.execute(
            aql,
            bind_vars=bind_vars,
            max_runtime=timeout_s,
        )
    except TypeError:
        # Older python-arango doesn't know ``max_runtime``. Fall
        # back to an unbounded execute — better to allow a slow
        # query than to refuse the request entirely.
        return db.aql.execute(aql, bind_vars=bind_vars)
    except Exception as exc:
        # python-arango exposes the server error code on
        # ``AQLQueryExecuteError.error_code`` (older drivers) or
        # ``error_code`` attribute. 1500 = "query killed".
        code = getattr(exc, "error_code", None) or getattr(exc, "code", None)
        if code == 1500 or "query killed" in str(exc).lower():
            raise HTTPException(
                status_code=504,
                detail={
                    "error": (
                        f"Query exceeded max runtime ({timeout_s}s). "
                        "Refine the query or raise SPARQL_PROTOCOL_TIMEOUT_SECONDS."
                    ),
                    "code": "E_TIMEOUT",
                },
            ) from exc
        raise


# ---------------------------------------------------------------------------
# Route handlers
# ---------------------------------------------------------------------------


@app.get("/sparql")
async def sparql_get(request: Request) -> Response:
    """``GET /sparql`` — SPARQL Protocol GET form (PRD §5.2).

    Two modes:

    * No ``query`` parameter ⇒ Service Description as
      ``text/turtle``. Sourced from the cached schema bundle when
      one is available so the named-graph list is accurate.
    * ``?query=…`` ⇒ translate + execute, respond per ``Accept``.
    """

    query = request.query_params.get("query")
    if not query:
        return await _service_description_response(request)
    return _execute_protocol_query(request, query)


@app.post("/sparql")
async def sparql_post(request: Request) -> Response:
    """``POST /sparql`` — SPARQL Protocol POST form (PRD §5.2).

    Three Content-Type cases:

    * ``application/sparql-update`` ⇒ 405 ``E_UPDATE_UNSUPPORTED``.
    * ``application/sparql-query`` ⇒ body *is* the query.
    * ``application/x-www-form-urlencoded`` ⇒ ``query=`` form
      field.
    """

    if _content_type_root(request) == _SPARQL_UPDATE_CT:
        # Authoritative on the Content-Type header — no body
        # inspection needed. PRD §5.2 row 1.
        return _update_405_response()

    try:
        query, _body = await _extract_query_from_post(request)
    except HTTPException as exc:
        return _http_exception_to_flat_response(exc)
    return _execute_protocol_query(request, query)


async def _service_description_response(request: Request) -> Response:
    """Build the ``GET /sparql`` (no query) response — Service
    Description as ``text/turtle``. Skips schema acquisition in
    public mode without a session (we don't know which database
    to introspect) and falls back to the default-graph-only
    description in that case.
    """

    bundle = None
    # Best-effort schema bundle for the named-graph list. We don't
    # 503 here — a Service Description without named graphs is
    # still useful for clients that just want format / feature
    # discovery.
    try:
        session = _resolve_protocol_session(request)
        bundle = _bundle_for_session(session)
    except HTTPException:
        bundle = None

    endpoint_url = str(request.url_for("sparql_get"))
    body = render_service_description(
        endpoint_url=endpoint_url,
        bundle=bundle,
    )
    response = PlainTextResponse(content=body, media_type="text/turtle")
    response.headers["Vary"] = "Accept"
    response.headers["Access-Control-Expose-Headers"] = _ACCESS_CONTROL_EXPOSE
    return response
