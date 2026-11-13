"""Security primitives for ``arango_sparql.service`` — sessions, rate
limit, SSRF guard, error redaction, and the Pydantic 422 handler.

Mirror of ``arango_cypher.service.security`` with ``arango_cypher`` →
``arango_sparql`` substitutions throughout (module names, env-var
prefix ``ARANGO_CYPHER_PUBLIC_MODE`` → ``ARANGO_SPARQL_PUBLIC_MODE``,
``ARANGO_CYPHER_CONNECT_ALLOWED_HOSTS`` →
``ARANGO_SPARQL_CONNECT_ALLOWED_HOSTS``).

The :class:`~arango_sparql.errors.SparqlError` hierarchy carries stable
``code`` strings (``E_SPARQL_PARSE``, ``E_SPARQL_UNSUPPORTED``,
``E_SCHEMA_RESOLVE``, ``E_AQL_EMIT``) that the route layer surfaces in
the 422 body via ``{"error": _sanitize_error(str(e)), "code": e.code}``.
The codes themselves are short, alphanumeric tokens — none of the
redaction patterns below match them — so :func:`_sanitize_error` is
safe to call on the message portion without losing the structured
``code`` slot.

Re-exported through :mod:`arango_sparql.service` so the historical
``from arango_sparql.service import _Session, _TokenBucket, _check_compute_rate_limit, _sanitize_error, _check_connect_target, ...``
imports stay one-import deep across the package surface.
"""

from __future__ import annotations

import ipaddress
import os
import re
import time
from contextlib import contextmanager
from typing import Any
from urllib.parse import urlparse

from arango import ArangoClient
from arango.database import StandardDatabase
from arango.exceptions import AQLQueryExecuteError
from fastapi import HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .._env import read_arango_password, read_arango_url, read_arango_username
from .._privacy import tenant_name
from . import control_plane
from .app import _PUBLIC_MODE, _svc_logger, app
from .platform_auth import (
    PlatformTokenError,
    background_token,
    forwarded_token,
    open_minted_database,
    open_platform_database,
)

# ---------------------------------------------------------------------------
# Sessions (in-memory dict, TTL-based, LRU-evicted)
# ---------------------------------------------------------------------------

SESSION_TTL_SECONDS = int(os.getenv("SESSION_TTL_SECONDS", "1800"))
MAX_SESSIONS = int(os.getenv("MAX_SESSIONS", "100"))


def _service_pkg_candidates() -> list[Any]:
    """All live objects representing the ``arango_sparql.service`` package.

    After a reload-and-restore dance, ``sys.modules["arango_sparql.service"]``
    can diverge from the ``arango_sparql.service`` attribute on the parent
    package — fixtures restore only the ``sys.modules`` slot while the
    parent attribute keeps pointing at the reloaded module. Tests then
    ``monkeypatch.setattr(svc, ...)`` against whichever object their own
    import path resolved to. Returning both lets the consumer scan for an
    explicit override regardless of which path the test patched.

    De-duplicates by identity to avoid double-counting when the two paths
    agree (the common case outside reload-heavy test files).
    """
    import sys

    seen: list[Any] = []
    pkg_via_sys = sys.modules.get("arango_sparql.service")
    if pkg_via_sys is not None:
        seen.append(pkg_via_sys)
    parent = sys.modules.get("arango_sparql")
    if parent is not None:
        pkg_via_attr = getattr(parent, "service", None)
        if pkg_via_attr is not None and not any(pkg_via_attr is p for p in seen):
            seen.append(pkg_via_attr)
    return seen


def _ttl_seconds() -> int:
    """Look up ``SESSION_TTL_SECONDS`` via the package object so test
    monkeypatches against ``arango_sparql.service.SESSION_TTL_SECONDS``
    win over this submodule's local default. Falls back to the module-
    local binding when nothing has been overridden.
    """
    for pkg in _service_pkg_candidates():
        val = getattr(pkg, "SESSION_TTL_SECONDS", None)
        if val is not None and int(val) != SESSION_TTL_SECONDS:
            return int(val)
    return SESSION_TTL_SECONDS


def _max_sessions() -> int:
    """See :func:`_ttl_seconds` — same indirection for ``MAX_SESSIONS``."""
    for pkg in _service_pkg_candidates():
        val = getattr(pkg, "MAX_SESSIONS", None)
        if val is not None and int(val) != MAX_SESSIONS:
            return int(val)
    return MAX_SESSIONS


class _Session:
    __slots__ = (
        "token",
        "db",
        "client",
        "created_at",
        "last_used",
        "graph_name",
        "platform_token",
        "platform_user",
        "project_mapping",
        "project_mapping_at",
        "recorded_questions",
        "generated_questions",
    )

    def __init__(
        self,
        token: str,
        db: StandardDatabase,
        client: ArangoClient,
        graph_name: str | None = None,
        *,
        platform_token: str | None = None,
        platform_user: str | None = None,
    ):
        self.token = token
        self.db = db
        self.client = client
        self.created_at = time.time()
        self.last_used = time.time()
        # Set on sessions opened by ``/connect/platform``: the platform JWT the
        # ``db`` handle currently authenticates with. Such a session follows
        # the caller's forwarded JWT on every request (see
        # :func:`_follow_platform_identity`) and holds no password.
        self.platform_token = platform_token
        # The caller as the platform's integration sidecar names them, or
        # ``None``; background work runs as this user (see
        # :func:`background_database`).
        self.platform_user = platform_user
        # Active ArangoDB named-graph scope. ``None`` means "all
        # collections" (the default). When set, schema acquisition is
        # down-selected to the graph's vertex/edge collections so the
        # resolver, OWL view, and NL suggestions only see the relevant
        # slice of a database other programs may share. See ADR — named
        # graph scoping.
        self.graph_name = graph_name
        # Under a governance control plane: the project's ontology joined to where its
        # data lives, as the control plane assembles it (PRD §8.7), and when it was read.
        # ``None``: the project has none, and the schema is introspected as without one.
        self.project_mapping: Any = None
        self.project_mapping_at: float | None = None
        # Under a control plane: when each question (its words and query) was last recorded,
        # so the page's graph run of a question just asked is not recorded twice.
        self.recorded_questions: dict[str, float] = {}
        # Under a control plane: the questions ``/nl-translate`` generated a query from, newest
        # last, so a run of that query unedited may carry the words (recording.py).
        self.generated_questions: dict[str, None] = {}

    def touch(self) -> None:
        self.last_used = time.time()

    @property
    def expired(self) -> bool:
        return (time.time() - self.last_used) > _ttl_seconds()


_sessions: dict[str, _Session] = {}


def _prune_expired() -> None:
    expired = [k for k, v in _sessions.items() if v.expired]
    for k in expired:
        s = _sessions.pop(k, None)
        if s:
            s.client.close()


def _evict_lru() -> None:
    """If session count meets MAX_SESSIONS, evict the least-recently-used."""
    _prune_expired()
    while len(_sessions) >= _max_sessions():
        oldest_key = min(_sessions, key=lambda k: _sessions[k].last_used)
        s = _sessions.pop(oldest_key, None)
        if s:
            s.client.close()


#: Why a platform session's request was refused: it arrived without the
#: platform JWT the session authenticates with.
PLATFORM_TOKEN_MISSING = (
    "This session uses your platform login, but the request did not carry it. "
    "Reload the page to sign in again."
)


def background_database(session: _Session) -> StandardDatabase:
    """The database handle for work that outlives this request.

    A platform session's forwarded JWT expires, and a background schema warm
    can run for minutes. When the integration sidecar knows the session's
    user, the work gets a token minted for that same user; otherwise, or off
    the platform, it keeps the session's own handle.
    """
    if session.platform_token is None:
        return session.db
    token = background_token(session.platform_user)
    if token is None:
        return session.db
    return open_minted_database(session.client, session.db.name, token)


def _follow_platform_identity(session: _Session, request: Request, *, via_session_header: bool) -> str | None:
    """Keep a platform session authenticated as the caller's current JWT.

    The platform rotates the JWT it forwards, so the one the session opened
    with expires long before the session does. Each request re-binds the
    ``db`` handle to the JWT that request carries. Returns why the request is
    refused, or ``None``: a platform session's request without a JWT is
    refused, never served on the stored token — that would let a request that
    bypassed the gateway act as the user who opened the session.

    When the session token itself arrived as ``Authorization: Bearer`` there
    is no header left for a JWT, so such a request is refused too.
    """
    if session.platform_token is None:
        return None
    token = forwarded_token(request) if via_session_header else None
    if token is None:
        return PLATFORM_TOKEN_MISSING
    if token != session.platform_token:
        try:
            session.db = open_platform_database(session.client, session.db.name, token)
        except PlatformTokenError as exc:
            return f"{exc}. Reload the page to sign in again."
        session.platform_token = token
    return None


def _arango_client_class() -> Any:
    """The service-level ``ArangoClient``, or the override a test patched onto the package
    (the same lookup the ``/connect`` route makes)."""
    for pkg in _service_pkg_candidates():
        client_cls = getattr(pkg, "ArangoClient", None)
        if client_cls is not None and client_cls is not ArangoClient:
            return client_cls
    return ArangoClient


def control_plane_identity(request: Request) -> control_plane.Identity:
    """The verified context a request carries, as ``HTTPException`` when it has none.

    The middleware in :mod:`.routes.control_plane` verifies it first and keeps it on the
    request; this verifies again only when called outside that middleware."""
    identity = getattr(request.state, "control_plane_identity", None)
    if isinstance(identity, control_plane.Identity):
        return identity
    try:
        return control_plane.verify(request.headers)
    except control_plane.ContextUnavailable as exc:
        raise HTTPException(status_code=503, detail=f"Cannot verify the project's context: {exc}") from exc
    except control_plane.ContextRejected as exc:
        raise HTTPException(status_code=401, detail=f"Invalid project context: {exc}") from exc


def _control_plane_session(request: Request) -> _Session:
    """The session for the user and project a context names, on the project's database.

    Opened with the deployment's own credential on first use and kept, like any session,
    until idle for the TTL. Keyed by who and where, never by the token: the page renews its
    token every few minutes and keeps its session. The key is not a credential: in this
    mode a request's token is always verified as a context, never looked up as a session.
    """
    identity = control_plane_identity(request)
    try:
        _, database = control_plane.database_for(identity)
    except control_plane.NoDatabase as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    key = "control-plane:" + "\x1f".join((identity.tenant, identity.project, identity.principal, database))
    _prune_expired()
    session = _sessions.get(key)
    if session is None:
        url = (read_arango_url(caller="control_plane") or "").rstrip("/")
        try:
            client = _arango_client_class()(hosts=url)
            db = client.db(
                database,
                # No default user: the deployment names one (configuration_problem), and a
                # read-only one is enough.
                username=read_arango_username(caller="control_plane") or "",
                password=read_arango_password(caller="control_plane") or "",
            )
            db.version()  # python-arango's .db() is lazy: check the credential now
        except Exception as exc:
            reason = _describe_connect_error(exc)
            _svc_logger.warning("control plane: cannot open database %r: %s", tenant_name(database), reason)
            raise HTTPException(
                status_code=503, detail=f"Cannot open the project's database: {_sanitize_error(reason)}"
            ) from exc
        _evict_lru()
        session = _Session(token=key, db=db, client=client)
        _sessions[key] = session
    _refresh_project_mapping(session, request)
    session.touch()
    return session


def _refresh_project_mapping(session: _Session, request: Request) -> None:
    """Read the project's mapping for *session* when it has none yet or it is older than
    :data:`control_plane.MAPPING_REFRESH_SECONDS`, with this request's context.

    A project without one keeps introspection. When the control plane cannot be read, a
    session that has a mapping keeps it; one that has none yet gets 503 rather than an
    introspected schema, whose terms would differ from the project's."""
    now = time.time()
    if (
        session.project_mapping_at is not None
        and now - session.project_mapping_at < control_plane.MAPPING_REFRESH_SECONDS
    ):
        return
    token = control_plane.token_from(request.headers) or ""
    try:
        turtle = control_plane.get().project_mapping(token)
        bundle = None
        if turtle is not None and turtle.strip():  # an empty answer maps nothing
            from ..translate.owl import turtle_to_mapping

            bundle = turtle_to_mapping(
                turtle, source_notes="the project's mapping, from its control plane", derive_conceptual=True
            )
            if not bundle.conceptual_schema.get("entities"):
                bundle = None  # maps no class: nothing to ask in, so introspect as without one
    except control_plane.ContextRejected as exc:
        raise HTTPException(status_code=401, detail=f"Invalid project context: {exc}") from exc
    except Exception as exc:  # unavailable, or an answer that does not parse
        if session.project_mapping_at is not None:
            _svc_logger.warning("control plane: keeping the project's mapping: %s", _sanitize_error(str(exc)))
            session.project_mapping_at = now
            return
        raise HTTPException(
            status_code=503, detail=f"Cannot read the project's mapping: {_sanitize_error(str(exc))}"
        ) from exc
    session.project_mapping = bundle
    session.project_mapping_at = now


def _get_session(request: Request) -> _Session:
    if control_plane.mode():
        return _control_plane_session(request)
    _prune_expired()
    # Prefer X-Arango-Session: the ArangoDB platform proxy replaces the standard
    # Authorization header with its own platform JWT before forwarding to the
    # BYOC container, making Bearer tokens unusable for app-level session auth.
    token = request.headers.get("X-Arango-Session", "")
    via_session_header = bool(token)
    if not token:
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            token = auth[7:]
    if not token:
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")
    session = _sessions.get(token)
    if session is None or session.expired:
        if session and session.expired:
            _sessions.pop(token, None)
            session.client.close()
        raise HTTPException(status_code=401, detail="Session expired or invalid")
    refusal = _follow_platform_identity(session, request, via_session_header=via_session_header)
    if refusal is not None:
        raise HTTPException(status_code=401, detail=refusal)
    session.touch()
    return session


def _get_optional_session(request: Request) -> _Session | None:
    """Like :func:`_get_session` but returns ``None`` instead of raising
    when no valid session is present.

    Used by stateless endpoints (e.g. ``/translate``) that work without a
    DB connection but can *enrich* their behaviour when the caller happens
    to be connected — for translate, that means merging the analyzer-
    discovered schema into the inline ontology. Never raises on a missing
    or expired token; an expired session is pruned exactly as
    :func:`_get_session` would, then ``None`` is returned.

    Under a control plane the context is the session: a project with no database here
    has none, and an invalid context is refused as by :func:`_get_session`.
    """
    if control_plane.mode():
        try:
            return _control_plane_session(request)
        except HTTPException as exc:
            if exc.status_code == 409:
                return None
            raise
    _prune_expired()
    token = request.headers.get("X-Arango-Session", "")
    via_session_header = bool(token)
    if not token:
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            token = auth[7:]
    if not token:
        return None
    session = _sessions.get(token)
    if session is None:
        return None
    if session.expired:
        _sessions.pop(token, None)
        session.client.close()
        return None
    if _follow_platform_identity(session, request, via_session_header=via_session_header) is not None:
        return None
    session.touch()
    return session


def _require_session_in_public_mode(request: Request) -> _Session | None:
    """Dependency: enforce session auth iff ``ARANGO_SPARQL_PUBLIC_MODE``.

    Returns the resolved session in public mode so callers that want to
    *use* the session don't need a second dependency lookup; returns
    ``None`` in default mode so callers that don't need the session
    aren't forced to thread it.
    """
    if not _PUBLIC_MODE:
        return None
    return _get_session(request)


# ---------------------------------------------------------------------------
# Rate limiting (per-client token bucket; two pools — LLM-heavy vs CPU-heavy)
# ---------------------------------------------------------------------------

NL_RATE_LIMIT_PER_MINUTE = int(os.getenv("NL_RATE_LIMIT_PER_MINUTE", "10"))

# CPU-bound (non-LLM) bucket — translate, validate, execute*, explain,
# schema/*, etc. Defaults an order of magnitude higher than the LLM bucket
# so a normal interactive workflow doesn't trip it. Override per-deployment
# via env.
COMPUTE_RATE_LIMIT_PER_MINUTE = int(os.getenv("COMPUTE_RATE_LIMIT_PER_MINUTE", "100"))


class _TokenBucket:
    """Simple per-key in-memory token bucket for rate limiting."""

    __slots__ = ("_capacity", "_tokens", "_last_refill")

    def __init__(self, capacity: int):
        self._capacity = capacity
        self._tokens: dict[str, float] = {}
        self._last_refill: dict[str, float] = {}

    def allow(self, key: str) -> bool:
        now = time.time()
        last = self._last_refill.get(key, now)
        elapsed = now - last
        current = self._tokens.get(key, float(self._capacity))
        current = min(self._capacity, current + elapsed * (self._capacity / 60.0))
        self._last_refill[key] = now
        if current >= 1.0:
            self._tokens[key] = current - 1.0
            return True
        self._tokens[key] = current
        return False

    def reset(self) -> None:
        """Drop all per-key state so every key starts full again.

        Equivalent to administratively clearing the limiter (e.g. after
        raising the cap). The test harness calls this between cases so the
        process-wide bucket can't accumulate across an entire suite run and
        spuriously 429 unrelated tests.
        """
        self._tokens.clear()
        self._last_refill.clear()


_nl_bucket = _TokenBucket(NL_RATE_LIMIT_PER_MINUTE)
_compute_bucket = _TokenBucket(COMPUTE_RATE_LIMIT_PER_MINUTE)


def _client_key(request: Request) -> str:
    """Per-client rate-limit key.

    Authorization header (a session token in our case) when available,
    else the remote IP, else the literal string ``"anon"``. Under a control
    plane, the verified context's user and project: the token is renewed
    every few minutes, and a renewal must not reset the user's budget.
    """
    identity = getattr(request.state, "control_plane_identity", None)
    if isinstance(identity, control_plane.Identity):
        return "control-plane:" + "\x1f".join((identity.tenant, identity.project, identity.principal))
    return request.headers.get("Authorization") or (request.client.host if request.client else "anon")


def _resolve_bucket(name: str) -> _TokenBucket:
    """Resolve a token bucket via the ``arango_sparql.service`` package
    so tests can ``monkeypatch.setattr(svc, "_nl_bucket", new_bucket)``
    without forking which package object the test imported.
    """
    local = globals()[name]
    for pkg in _service_pkg_candidates():
        bucket = getattr(pkg, name, None)
        if isinstance(bucket, _TokenBucket) and bucket is not local:
            return bucket
    return local


def _check_nl_rate_limit(request: Request) -> None:
    if not _resolve_bucket("_nl_bucket").allow(_client_key(request)):
        raise HTTPException(status_code=429, detail="Rate limit exceeded for NL endpoints")


def _check_compute_rate_limit(request: Request) -> None:
    """Cheaper rate-limit bucket for the CPU-bound non-LLM endpoints
    (``/translate``, ``/validate``, ``/execute*``, ``/explain``, …).
    """
    if not _resolve_bucket("_compute_bucket").allow(_client_key(request)):
        raise HTTPException(status_code=429, detail="Rate limit exceeded for compute endpoints")


# ---------------------------------------------------------------------------
# Error / log redaction (URLs, IPs, credential-shaped tokens)
# ---------------------------------------------------------------------------
#
# The SparqlError hierarchy carries stable, alphanumeric ``code`` strings
# (``E_SPARQL_PARSE``, ``E_SPARQL_UNSUPPORTED``, ``E_SCHEMA_RESOLVE``,
# ``E_AQL_EMIT``). None of the regex patterns below match these tokens,
# so the route layer can safely call ``_sanitize_error(str(exc))`` on
# the message slot while preserving ``exc.code`` verbatim in the JSON
# body's ``code`` field.

_URL_RE = re.compile(r"https?://[^\s,;'\")\]}>]+", re.IGNORECASE)
_HOST_PORT_RE = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}(?::\d+)?\b")
# Key-value credential forms (``password=hunter2``, ``api_key: sk-live-…``).
# ``\S+`` stops at whitespace so we don't eat the rest of the log line.
_CRED_RE = re.compile(
    r"(?:password|passwd|token|secret|api[_-]?key)\s*[:=]\s*\S+",
    re.IGNORECASE,
)
# ``Authorization: Bearer <token>`` (and ``Basic`` / ``Digest``) is handled
# separately because the value follows the *scheme* after a whitespace, and
# ``_CRED_RE`` would only consume "Bearer" and let the actual token leak.
_AUTH_HEADER_RE = re.compile(
    r"authorization\s*[:=]\s*(?:bearer|basic|digest|token)\s+\S+",
    re.IGNORECASE,
)

# ArangoDB collection name grammar — used to validate caller-supplied
# identifiers before any backtick-interpolation into AQL. Backtick
# interpolation is not a parameterisation boundary; a stray backtick or
# newline would break out of the quote.
_COLLECTION_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]{0,255}$")


def _sanitize_error(msg: str) -> str:
    """Strip URLs, IP addresses, and credential-like patterns from an error message."""
    msg = _URL_RE.sub("<redacted-url>", msg)
    msg = _HOST_PORT_RE.sub("<redacted-host>", msg)
    # ``_AUTH_HEADER_RE`` first so its multi-token match wins over
    # ``_CRED_RE``'s single-token "authorization: Bearer" prefix match.
    msg = _AUTH_HEADER_RE.sub("<redacted-credential>", msg)
    msg = _CRED_RE.sub("<redacted-credential>", msg)
    return msg


def _redact_value(val: Any) -> Any:
    """Recursive credential-pattern redaction on arbitrary JSON-shaped data."""
    if isinstance(val, str):
        return _sanitize_error(val)
    if isinstance(val, dict):
        return {k: _redact_value(v) for k, v in val.items()}
    if isinstance(val, list):
        return [_redact_value(v) for v in val]
    return val


def _sanitize_pydantic_errors(errors: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Strip credential-shaped tokens from a Pydantic ``errors()`` list.

    Pydantic v2 echoes the offending ``input`` value back in every
    validation error, which is invaluable for client debugging *and*
    catastrophic for any payload that happened to embed a password
    (saved-correction notes, NL questions that quote a connection
    string, etc.). We can't drop ``input`` wholesale — the UI's
    "tell me what you sent that broke" affordance depends on it —
    so we walk the structure and run the same scalar-level
    :func:`_sanitize_error` redaction on every string value found.
    """
    cleaned: list[dict[str, Any]] = []
    for err in errors:
        new = dict(err)
        if "input" in new:
            new["input"] = _redact_value(new["input"])
        if "msg" in new and isinstance(new["msg"], str):
            new["msg"] = _sanitize_error(new["msg"])
        # Pydantic v2 attaches the raw ``Exception`` instance under
        # ``ctx['error']`` whenever a ``@field_validator`` raises. The
        # bare instance is not JSON-serialisable; stringify so the
        # response encoder doesn't crash before the 422 reaches the
        # client.
        if "ctx" in new and isinstance(new["ctx"], dict):
            new["ctx"] = {k: (str(v) if isinstance(v, BaseException) else v) for k, v in new["ctx"].items()}
        cleaned.append(new)
    return cleaned


@contextmanager
def _translate_errors(prefix: str, status_code: int = 500):
    """Convert any ``Exception`` raised inside the block into an ``HTTPException``.

    The detail is ``f"{prefix}: {_sanitize_error(str(exc))}"``. Existing
    ``HTTPException``\\ s are re-raised unchanged, so nested validators that
    already produced a richer status code (e.g. 400 / 422) are not masked
    to 500. Use this at every endpoint boundary that runs AQL or otherwise
    touches the DB, to keep the error-surface uniform and sanitised.

    A failed *AQL execution* (``AQLQueryExecuteError``) — missing
    collection, bad bind variable, a query that cannot run against the
    current data — is a client/data condition, not a server fault, so it
    is surfaced as ``400`` with the structured ``{error, code}`` shape the
    SPARQL-error paths use. The ``code`` carries the ArangoDB numeric
    error code (e.g. ``E_AQL_1203`` for "collection or view not found") so
    the HTTP status and the UI banner agree. Genuine infrastructure
    failures (connection refused, server 5xx) fall through to
    ``status_code`` (default 500).
    """
    try:
        yield
    except HTTPException:
        raise
    except AQLQueryExecuteError as e:
        arango_code = getattr(e, "error_code", None)
        code = f"E_AQL_{arango_code}" if arango_code else "E_AQL_EXECUTION"
        raise HTTPException(
            status_code=400,
            detail={
                "error": f"{prefix}: {_sanitize_error(str(e))}",
                "code": code,
            },
        ) from e
    except Exception as e:
        raise HTTPException(
            status_code=status_code,
            detail=f"{prefix}: {_sanitize_error(str(e))}",
        ) from e


@app.exception_handler(RequestValidationError)
async def _validation_error_handler(request: Request, exc: RequestValidationError):
    # The log names which fields failed and how — enough to diagnose UI ↔
    # service contract drift — and nothing the caller sent: pydantic's
    # ``input`` (for a missing field, the whole body) and the body itself
    # carry query text, questions and credentials (PRD §17.3; #181). The
    # caller still gets the full errors in the response.
    safe_errors = _sanitize_pydantic_errors(exc.errors())
    _svc_logger.warning(
        "Pydantic 422 on %s %s: %s",
        request.method,
        request.url.path,
        _loggable_validation_errors(exc.errors()),
    )
    return JSONResponse(status_code=422, content={"detail": safe_errors})


def _loggable_validation_errors(errors: Any) -> list[dict[str, Any]]:
    """Each validation error's field path and error type — no input, message or context."""
    loggable = []
    for err in errors or []:
        if isinstance(err, dict):
            loc = [part if isinstance(part, int) else str(part) for part in err.get("loc", ())]
            loggable.append({"loc": loc, "type": str(err.get("type", "?"))})
    return loggable


# ---------------------------------------------------------------------------
# /connect SSRF guard
# ---------------------------------------------------------------------------
#
# The /connect endpoint takes a caller-supplied URL and opens a TCP
# connection to it. That's intentional — the UI's connect dialog is the
# whole point of this product — but it also means an unauthenticated POST
# can probe arbitrary internal infrastructure from the service host (the
# class textbook SSRF). We can't fully block private targets without
# breaking the legitimate dev workflow ("connect to my localhost ArangoDB"),
# so the policy splits along trust:
#
#   * Always reject cloud-metadata literals (AWS / Azure / OpenStack /
#     GCP / Alibaba). There is no plausible reason to point this service
#     at 169.254.169.254, and the cost of a misclick is full IAM
#     credential exfiltration.
#   * In ``ARANGO_SPARQL_PUBLIC_MODE``, additionally reject any
#     literal-IP host inside RFC1918 / loopback / link-local / ULA so a
#     public deployment can't be coerced into reaching internal services.
#     Operators who deliberately allow private targets opt in via
#     ``ARANGO_SPARQL_CONNECT_ALLOWED_HOSTS``.
#
# We intentionally do *not* perform DNS resolution here. Resolving the
# host opens a second SSRF vector (DNS rebinding, slow targets used as a
# blocking-IO probe).

_PROXY_ENV_VARS = (
    "HTTPS_PROXY",
    "HTTP_PROXY",
    "https_proxy",
    "http_proxy",
    "ALL_PROXY",
    "all_proxy",
)

_BLOCK_METADATA_HOSTS = frozenset(
    {
        "169.254.169.254",  # AWS / Azure / OpenStack / DigitalOcean
        "100.100.100.200",  # Alibaba Cloud
        "metadata.google.internal",
        "metadata.goog",
        "metadata",  # GCP shorthand
    }
)
_BLOCK_METADATA_IPS = frozenset(
    {
        ipaddress.ip_address("169.254.169.254"),
        ipaddress.ip_address("100.100.100.200"),
        ipaddress.ip_address("fd00:ec2::254"),  # AWS IPv6 metadata
    }
)
_PRIVATE_NETWORKS = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
)


def _connect_allowed_hosts() -> frozenset[str]:
    """Operator-supplied allowlist of host strings that bypass the SSRF guard.

    Re-read on every call (not cached) so tests can monkeypatch the env
    var per case. The cost is one ``os.environ.get`` plus a string split
    per ``/connect`` request, which is negligible next to the TCP
    handshake the endpoint is about to perform.
    """
    raw = os.environ.get("ARANGO_SPARQL_CONNECT_ALLOWED_HOSTS", "")
    return frozenset(h.strip().lower() for h in raw.split(",") if h.strip())


def _check_connect_target(url: str) -> None:
    """Raise ``HTTPException(400)`` if ``url`` points at a forbidden target.

    See the module-level comment block above for the policy. Best-effort
    by design — the intent is to refuse the obvious foot-guns (literal
    cloud-metadata IPs, hard-coded RFC1918 hostnames in a public
    deployment) without pretending to provide complete network isolation;
    that's the job of the surrounding network/SG configuration.
    """
    try:
        parsed = urlparse(url if "://" in url else f"http://{url}")
    except (ValueError, TypeError) as exc:
        raise HTTPException(status_code=400, detail="Invalid connection URL") from exc
    host = (parsed.hostname or "").lower()
    if not host:
        raise HTTPException(status_code=400, detail="Connection URL is missing a host")

    allowlist = _connect_allowed_hosts()
    if host in allowlist:
        return

    bare = host.strip("[]")

    if host in _BLOCK_METADATA_HOSTS:
        raise HTTPException(
            status_code=400,
            detail=(
                "Refusing to connect to cloud metadata service host. Set "
                "ARANGO_SPARQL_CONNECT_ALLOWED_HOSTS if this is intentional."
            ),
        )

    try:
        ip = ipaddress.ip_address(bare)
    except ValueError:
        return

    if ip in _BLOCK_METADATA_IPS:
        raise HTTPException(
            status_code=400,
            detail=(
                "Refusing to connect to cloud metadata IP. Set "
                "ARANGO_SPARQL_CONNECT_ALLOWED_HOSTS if this is intentional."
            ),
        )

    if not _PUBLIC_MODE:
        return

    for net in _PRIVATE_NETWORKS:
        if ip in net:
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Refusing to connect to private/loopback address {ip} in public mode. "
                    "Pin the literal via ARANGO_SPARQL_CONNECT_ALLOWED_HOSTS to override."
                ),
            )


def _walk_cause_chain(exc: BaseException) -> list[BaseException]:
    """Return the chain of exceptions from outer to root via __cause__/__context__."""
    seen: list[BaseException] = []
    cur: BaseException | None = exc
    while cur is not None and cur not in seen:
        seen.append(cur)
        cur = cur.__cause__ or cur.__context__
    return seen


def _describe_connect_error(exc: BaseException) -> str:
    """Build a diagnostic message for a /connect failure.

    The python-arango client wraps low-level transport failures in a
    generic ``ClientConnectionError``. Walk the ``__cause__`` /
    ``__context__`` chain to surface the most specific root cause and,
    on a proxy-tunnel failure, hint at any proxy env vars set in this
    process.
    """
    chain = _walk_cause_chain(exc)
    root_msg = str(chain[-1]) if chain else str(exc)
    top_msg = str(chain[0]) if chain else str(exc)

    parts: list[str] = [top_msg]
    if len(chain) > 1 and root_msg and root_msg != top_msg:
        parts.append(f"root cause: {root_msg}")

    joined = " | ".join(parts)
    lowered = joined.lower()

    if "tunnel connection failed" in lowered or "proxy" in lowered:
        proxies_set = [v for v in _PROXY_ENV_VARS if os.environ.get(v)]
        if proxies_set:
            parts.append(
                "hint: this process has proxy env vars set ("
                + ", ".join(proxies_set)
                + "). Unset them or add the ArangoDB host to NO_PROXY and restart."
            )
        else:
            parts.append(
                "hint: proxy tunnel rejected the connection but no proxy env vars "
                "are set in this process; check system / IDE sandbox proxy config."
            )

    return _sanitize_error(" | ".join(parts))
