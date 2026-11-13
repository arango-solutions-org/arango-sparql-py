"""Structured observability for ``arango_sparql.service``.

Mirror of ``arango_cypher.service.observability``, trimmed to the three
helpers the SPARQL service needs today:

1. **Per-request correlation ID.** :class:`CorrelationIdMiddleware` mints
   a UUID4 on absent ``X-Request-Id``, echoes the inbound value when
   present (sanitised to ``[A-Za-z0-9-]{1,128}`` so a hostile caller
   can't poison the log stream with newlines / shell escapes), stores
   it in :data:`correlation_id_var` (a :class:`contextvars.ContextVar`)
   so every ``logging.*`` call inside the request handler picks it up
   via :class:`CorrelationIdLogFilter` without a single signature
   change. The header is echoed back on the response so callers can
   correlate their client-side logs with the server trail.

2. **:func:`log_endpoint_timing`.** Single helper called by every
   endpoint on the success path. Emits one INFO record with
   ``endpoint``, ``elapsed_ms``, ``status`` (default ``"ok"``), and
   any caller-supplied ``extras`` (e.g. ``rows`` for ``/execute``,
   ``sparql_len`` for ``/translate``). Failures are still surfaced via
   the existing ``HTTPException`` path; a caller that wants an explicit
   error line can pass ``status="error"`` and the relevant ``extras``.

3. **:func:`time_endpoint`.** Convenience context manager that times a
   block and emits one log line on exit. Switches ``status`` to
   ``"error"`` automatically on exception.

Output format defaults to plain ``key=value`` pairs; set
``ARANGO_SPARQL_LOG_JSON=1`` to flip to single-line JSON for
log-aggregation pipelines (Datadog / Loki / Splunk). Either way the lines go
to **stdout** (PRD §9.5), and uvicorn's access and error loggers are routed
through the same handler so the stream has one shape.

**Privacy (PRD §17.2).** Inside a request every line carries ``session_id``
when the caller sent a session token — the first 12 hex characters of
SHA-256(token) (:func:`hash_session_token`), never the token — and
``tenant`` when tenant logging is on. Tenant identifiers (the tenant, and
database / graph / collection names) are logged only when
``ARANGO_SPARQL_LOG_INCLUDE_TENANT`` allows it: default off in public mode,
on otherwise. This deviates from ``arango_cypher.service.observability``,
which has neither field; the PRD's envelope is the contract here (#178).

LLM cost telemetry (:func:`log_llm_call`, :func:`current_llm_provider_and_model`,
:func:`estimate_llm_cost_usd`) was added in the NL→SPARQL bootstrap —
mirrors the Cypher project's ``arango_cypher.service.observability``
shape so cross-repo log scans group cleanly. ``LLM_PROVIDER`` /
``OPENAI_MODEL`` env vars from the Cypher world are honoured as
fallbacks, but the canonical NL→SPARQL pair is
``NL2SPARQL_PROVIDER`` / ``NL2SPARQL_MODEL`` (see
:mod:`arango_sparql.nl2sparql.client` for the rule-300 contract).
"""

from __future__ import annotations

import contextvars
import hashlib
import json
import logging
import os
import re
import sys
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import parse_qs, unquote_plus

from .. import __version__
from .._privacy import REDACTED as REDACTED_TENANT
from .._privacy import TenantName

# ---------------------------------------------------------------------------
# Correlation ID — contextvar + ASGI middleware + logging filter
# ---------------------------------------------------------------------------

# ``ContextVar`` rather than threadlocal because FastAPI runs handlers in an
# asyncio event loop where threadlocals collapse work across tasks. The
# default value is ``"-"`` so log lines emitted *outside* a request still
# render cleanly rather than blowing up with a ``LookupError`` from an unset
# contextvar.
correlation_id_var: contextvars.ContextVar[str] = contextvars.ContextVar("correlation_id", default="-")

# Inbound ``X-Request-Id`` is sanitised against this character class before
# being stored — without it, a hostile caller could send a newline-laden
# value and inject fake records into the log stream.
_INBOUND_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9-]{1,128}$")


#: The hashed session token (:func:`hash_session_token`) of the current request,
#: or ``None`` outside a request or when the request carries no token.
session_id_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("session_id", default=None)
#: The current request's tenant (``X-Tenant-Id``, else the default tenant), or ``None``.
tenant_var: contextvars.ContextVar[str | None] = contextvars.ContextVar("tenant", default=None)

#: Hex characters of SHA-256(token) kept as the log's ``session_id`` (PRD §17.2).
SESSION_ID_HEX_CHARS = 12


def hash_session_token(token: str) -> str:
    """The loggable stand-in for a session token: the first
    :data:`SESSION_ID_HEX_CHARS` hex characters of its SHA-256 (PRD §17.2).

    Correlates lines of one session without putting any part of the token —
    a credential — in a log or a stored document."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:SESSION_ID_HEX_CHARS]


def _session_token_from_request(headers: dict[bytes, bytes], query_string: bytes) -> str | None:
    """The session token as the routes read it: ``X-Arango-Session`` first, else
    an ``Authorization: Bearer`` value (:func:`.security._get_session`), else
    ``/sparql``'s ``?session=`` (``routes.protocol``)."""
    raw = headers.get(b"x-arango-session", b"").decode("latin-1").strip()
    if raw:
        return raw
    auth = headers.get(b"authorization", b"").decode("latin-1")
    if auth.startswith("Bearer ") and auth[7:].strip():
        return auth[7:].strip()
    if query_string:
        values = parse_qs(query_string.decode("latin-1")).get("session")
        if values and values[0].strip():
            return values[0].strip()
    return None


def _normalise_request_id(raw: str | None) -> str:
    """Return ``raw`` if it matches the safe character class, else mint a UUID4."""
    if raw and _INBOUND_REQUEST_ID_RE.fullmatch(raw):
        return raw
    return str(uuid.uuid4())


class CorrelationIdMiddleware:
    """ASGI middleware: mint or accept ``X-Request-Id``, propagate via contextvar.

    Implemented as a raw ASGI callable rather than a Starlette
    :class:`BaseHTTPMiddleware` subclass so the response body isn't
    eagerly buffered (breaks streaming endpoints) and the contextvar's
    :meth:`set` token can be released exactly when the response is fully
    sent.
    """

    def __init__(self, app: Callable[..., Awaitable[None]]):
        self.app = app

    async def __call__(self, scope: dict[str, Any], receive: Callable, send: Callable) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # First occurrence of each header wins, as Starlette's ``Headers.get`` does.
        headers: dict[bytes, bytes] = {}
        for name, value in scope.get("headers", []):
            headers.setdefault(name, value)
        inbound = headers.get(b"x-request-id")
        request_id = _normalise_request_id(inbound.decode("latin-1") if inbound is not None else None)
        token = correlation_id_var.set(request_id)
        session_token = _session_token_from_request(headers, scope.get("query_string", b""))
        session_reset = session_id_var.set(hash_session_token(session_token) if session_token else None)
        tenant_reset = tenant_var.set(_tenant_from_headers(headers))

        async def _send_with_header(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers.append((b"x-request-id", request_id.encode("latin-1")))
                message["headers"] = headers
            await send(message)

        try:
            await self.app(scope, receive, _send_with_header)
        finally:
            tenant_var.reset(tenant_reset)
            session_id_var.reset(session_reset)
            correlation_id_var.reset(token)


def _tenant_from_headers(headers: dict[bytes, bytes]) -> str | None:
    """The tenant :func:`.tenant.resolve_tenant_id` resolves for these headers."""
    from .tenant import TENANT_DEFAULT_ENV, TENANT_HEADER

    raw = headers.get(TENANT_HEADER.lower().encode("latin-1"))
    if raw is not None:
        stripped = raw.decode("latin-1").strip()
        if stripped:
            return stripped
    env_value = (os.environ.get(TENANT_DEFAULT_ENV) or "").strip()
    return env_value or None


class CorrelationIdLogFilter(logging.Filter):
    """Inject :data:`correlation_id_var` into every :class:`LogRecord`."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.correlation_id = correlation_id_var.get()
        return True


#: Record attributes that name a tenant or its data (PRD §17.1 class B). Dropped
#: from every line unless tenant logging is on (§17.2).
TENANT_IDENTIFIER_KEYS = frozenset(
    {"tenant", "tenant_id", "database", "db", "db_name", "graph", "collection"}
)


class RequestContextLogFilter(logging.Filter):
    """Add the request's ``session_id`` and, when allowed, its ``tenant``; and
    strip tenant identifiers from records when tenant logging is off.

    ``include_tenant`` is fixed when the filter is built
    (:func:`configure_observability`), so one process logs one way."""

    def __init__(self, *, include_tenant: bool) -> None:
        super().__init__()
        self.include_tenant = include_tenant

    def filter(self, record: logging.LogRecord) -> bool:
        session_id = session_id_var.get()
        if session_id is not None and "session_id" not in record.__dict__:
            record.session_id = session_id
        if self.include_tenant:
            tenant = tenant_var.get()
            if tenant is not None and "tenant" not in record.__dict__:
                record.tenant = tenant
        else:
            for key in TENANT_IDENTIFIER_KEYS:
                record.__dict__.pop(key, None)
            record.args = _redact_tenant_args(record.args)
        return True


def _redact_tenant_args(args: Any) -> Any:
    """``args`` with every :class:`~arango_sparql._privacy.TenantName` replaced."""
    if isinstance(args, tuple):
        return tuple(REDACTED_TENANT if isinstance(a, TenantName) else a for a in args)
    if isinstance(args, dict):
        return {k: REDACTED_TENANT if isinstance(v, TenantName) else v for k, v in args.items()}
    return args


_TRUE = frozenset({"1", "true", "yes", "on"})
_FALSE = frozenset({"0", "false", "no", "off"})


def tenant_logging_enabled() -> bool:
    """``ARANGO_SPARQL_LOG_INCLUDE_TENANT``, defaulting to *not* public mode (PRD §17.2).

    A value that is neither true nor false fails closed: tenants stay out of
    the logs, and a warning says why."""
    raw = os.getenv("ARANGO_SPARQL_LOG_INCLUDE_TENANT")
    if raw is None or not raw.strip():
        return os.getenv("ARANGO_SPARQL_PUBLIC_MODE", "").strip().lower() not in ("1", "true", "yes")
    value = raw.strip().lower()
    if value in _TRUE:
        return True
    if value not in _FALSE:
        logging.getLogger("arango_sparql.service.observability").warning(
            "ARANGO_SPARQL_LOG_INCLUDE_TENANT=%r is not a boolean; tenant identifiers stay out of the logs",
            raw,
        )
    return False


# ---------------------------------------------------------------------------
# Formatters — key=value (default) and JSON (env-gated)
# ---------------------------------------------------------------------------


class _KeyValueFormatter(logging.Formatter):
    """Emit ``ts level logger correlation_id=… msg=… k=v k=v …`` lines."""

    _RESERVED = frozenset(
        {
            "args",
            "asctime",
            "created",
            "exc_info",
            "exc_text",
            "filename",
            "funcName",
            "levelname",
            "levelno",
            "lineno",
            "message",
            "module",
            "msecs",
            "msg",
            "name",
            "pathname",
            "process",
            "processName",
            "relativeCreated",
            "stack_info",
            "thread",
            "threadName",
            "taskName",
            "correlation_id",
        }
    )

    def format(self, record: logging.LogRecord) -> str:
        ts = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created))
        ts = f"{ts}.{int(record.msecs):03d}Z"
        cid = getattr(record, "correlation_id", "-")
        msg = record.getMessage()
        prefix = f"{ts} {record.levelname} {record.name} correlation_id={cid} msg={msg!r}"
        extras = {
            k: v for k, v in record.__dict__.items() if k not in self._RESERVED and not k.startswith("_")
        }
        if not extras:
            return prefix
        kv = " ".join(f"{k}={_format_kv_value(v)}" for k, v in sorted(extras.items()))
        return f"{prefix} {kv}"


def _format_kv_value(v: Any) -> str:
    """Render a structured value safely for the ``key=value`` formatter."""
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, str):
        return json.dumps(v, ensure_ascii=False)
    return json.dumps(repr(v), ensure_ascii=False)


#: The ``service`` field of every JSON line (PRD §9.5).
SERVICE_NAME = "arango-sparql-py"


class _JsonFormatter(logging.Formatter):
    """Emit one JSON object per record, stable key order.

    Envelope (PRD §9.5): ``ts``, ``level``, ``logger``, ``correlation_id``,
    ``msg``, ``service``, ``version``, then the record's own fields
    (``session_id``, ``tenant``, ``endpoint``, ``elapsed_ms``, …). A record
    field never overwrites an envelope field."""

    _RESERVED = _KeyValueFormatter._RESERVED

    def format(self, record: logging.LogRecord) -> str:
        ts = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created))
        ts = f"{ts}.{int(record.msecs):03d}Z"
        payload: dict[str, Any] = {
            "ts": ts,
            "level": record.levelname,
            "logger": record.name,
            "correlation_id": getattr(record, "correlation_id", "-"),
            "msg": record.getMessage(),
            "service": SERVICE_NAME,
            "version": __version__,
        }
        for k, v in record.__dict__.items():
            if k in self._RESERVED or k.startswith("_"):
                continue
            payload.setdefault(k, _json_safe(v))
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def _json_safe(v: Any) -> Any:
    """Coerce a value into something :func:`json.dumps` accepts losslessly."""
    if isinstance(v, (str, int, float, bool)) or v is None:
        return v
    if isinstance(v, (list, tuple)):
        return [_json_safe(item) for item in v]
    if isinstance(v, dict):
        return {str(k): _json_safe(item) for k, item in v.items()}
    return repr(v)


# ---------------------------------------------------------------------------
# configure_observability — idempotent setup hook called from service.app
# ---------------------------------------------------------------------------

# Module-level guard: installing two filters / two handlers on the
# ``arango_sparql`` root logger would duplicate every record.
_CONFIGURED = False

#: uvicorn's loggers routed through the service's handler (PRD §9.5: one stream).
_UVICORN_LOGGERS = ("uvicorn.access", "uvicorn.error")


#: Query parameters that carry a credential: a control plane's context (``ctx``, on a tool's
#: first link), a session token (``session``, accepted by ``/sparql`` off a control plane),
#: a ``token``. Their values never reach the access log (PRD §8.6 T1, T18).
_SECRET_QUERY_KEYS = frozenset({"ctx", "session", "token"})
#: Query parameters that carry query content: the SPARQL Protocol's ``query`` and
#: ``update`` (a ``GET /sparql?query=…`` puts the whole query in the address). Query
#: text never reaches a log (PRD §17.3).
_CONTENT_QUERY_KEYS = frozenset({"query", "update"})


_REDACTED_QUERY_KEYS = _SECRET_QUERY_KEYS | _CONTENT_QUERY_KEYS


def redact_query(path: str) -> str:
    """*path* with the values of credential- and query-carrying parameters replaced. Keys
    are compared decoded and case-folded, so ``%63tx=`` or ``CTX=`` are caught too."""
    base, sep, query = path.partition("?")
    if not sep:
        return path
    parts = []
    for part in query.split("&"):
        key, eq, _ = part.partition("=")
        if eq and unquote_plus(key).lower() in _REDACTED_QUERY_KEYS:
            part = f"{key}=[redacted]"
        parts.append(part)
    return f"{base}?{'&'.join(parts)}"


class AccessLogRedactFilter(logging.Filter):
    """Redact credentials in uvicorn's access log. Its records carry
    ``(client, method, path, http_version, status)`` as ``args``."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) >= 3 and isinstance(args[2], str):
            record.args = (*args[:2], redact_query(args[2]), *args[3:])
        return True


def _install_access_log_redaction() -> None:
    access = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, AccessLogRedactFilter) for f in access.filters):
        access.addFilter(AccessLogRedactFilter())


def configure_observability(*, force: bool = False) -> None:
    """Install :class:`CorrelationIdLogFilter` + the chosen formatter.

    Idempotent — second and subsequent calls are no-ops unless
    ``force=True``. Tests pass ``force=True`` to exercise the setup path
    against a freshly-cleared root logger.

    Reads three env vars:

    * ``ARANGO_SPARQL_LOG_INCLUDE_TENANT`` — see :func:`tenant_logging_enabled`.
    * ``ARANGO_SPARQL_LOG_LEVEL`` — log level for the ``arango_sparql``
      root logger (default ``INFO``). Endpoint timing lines are emitted
      at ``INFO``; flip to ``WARNING`` to silence them while keeping
      CORS / SSRF / 422 warnings.
    * ``ARANGO_SPARQL_LOG_JSON`` — when ``"1" / "true" / "yes"`` (case
      insensitive), uses :class:`_JsonFormatter`. Otherwise the
      key=value formatter.
    """
    global _CONFIGURED
    _install_access_log_redaction()
    if _CONFIGURED and not force:
        return

    root = logging.getLogger("arango_sparql")
    if force:
        for h in list(root.handlers):
            root.removeHandler(h)
        for f in list(root.filters):
            root.removeFilter(f)

    # The filter is attached to the handler rather than the logger because
    # Python's logging machinery only runs a logger's filters on records
    # *originating* from that logger — records propagated up from child
    # loggers skip the parent's filter list and go straight to its handlers.
    correlation_filter = CorrelationIdLogFilter()
    root.addFilter(correlation_filter)

    json_mode = os.getenv("ARANGO_SPARQL_LOG_JSON", "").lower() in ("1", "true", "yes")
    formatter: logging.Formatter = _JsonFormatter() if json_mode else _KeyValueFormatter()

    # stdout, not StreamHandler's default stderr (PRD §9.5).
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)
    handler.addFilter(correlation_filter)
    root.addHandler(handler)
    # Read after the handler is attached, so a warning about the value is written.
    handler.addFilter(RequestContextLogFilter(include_tenant=tenant_logging_enabled()))

    # uvicorn configures its loggers before it imports the app, so replacing
    # their handlers here takes effect for the process. The access log keeps
    # its logger-level redaction filter (_install_access_log_redaction). Its
    # lines carry correlation_id "-": uvicorn writes them after the request's
    # context has been reset.
    for name in _UVICORN_LOGGERS:
        uv_logger = logging.getLogger(name)
        for h in list(uv_logger.handlers):
            uv_logger.removeHandler(h)
        uv_logger.addHandler(handler)
        uv_logger.propagate = False

    level_name = os.getenv("ARANGO_SPARQL_LOG_LEVEL", "INFO").upper()
    root.setLevel(getattr(logging, level_name, logging.INFO))

    _CONFIGURED = True


# ---------------------------------------------------------------------------
# log_endpoint_timing — single line per endpoint success
# ---------------------------------------------------------------------------

_endpoint_logger = logging.getLogger("arango_sparql.service.endpoint")


def log_endpoint_timing(
    endpoint: str,
    elapsed_ms: float,
    *,
    status: str = "ok",
    **extras: Any,
) -> None:
    """Emit one INFO line for an endpoint round-trip.

    Caller contract:

    * ``endpoint`` — the route path (``"/translate"``, ``"/execute"``,
      …). Stable across the call site so log aggregation can group by
      endpoint without parsing the access log.
    * ``elapsed_ms`` — wall-clock milliseconds, rounded to 1 decimal.
    * ``status`` — ``"ok"`` (default), ``"error"``, or any other
      caller-defined token.
    * ``extras`` — endpoint-specific structured fields. Strings are
      sanitised through :func:`arango_sparql.service.security._sanitize_error`
      before emit so a stray URL / credential can't leak via this
      surface; non-string values pass through unchanged.

    Reserved keys (``correlation_id``, ``msg``, ``level``, …) are
    silently dropped to keep the formatter contract clean.
    """
    safe_extras = {
        k: _sanitize_extra_value(v) for k, v in extras.items() if k not in _KeyValueFormatter._RESERVED
    }
    _endpoint_logger.info(
        "endpoint_timing",
        extra={
            "endpoint": endpoint,
            "elapsed_ms": elapsed_ms,
            "status": status,
            **safe_extras,
        },
    )


def _sanitize_extra_value(v: Any) -> Any:
    """Run string values through the existing service redactor.

    Lazy import via ``sys.modules`` avoids the
    ``security → observability → security`` circular at package init —
    security is imported into the package namespace before observability,
    but observability is imported by app.py *before* security; reading
    via :data:`sys.modules` at call time sidesteps the ordering question
    entirely.
    """
    if not isinstance(v, str):
        return v
    import sys

    sec = sys.modules.get("arango_sparql.service.security")
    if sec is not None:
        sanitiser = getattr(sec, "_sanitize_error", None)
        if sanitiser is not None:
            try:
                return sanitiser(v)
            except Exception:
                # Sanitiser failure must not crash the log line —
                # preserve raw value and accept the (unlikely) leak risk.
                return v
    return v


# ---------------------------------------------------------------------------
# Convenience timer — used by routes that didn't previously track elapsed_ms
# ---------------------------------------------------------------------------


class _EndpointTimer:
    """Context manager that times a block and emits one log line on exit.

    Captures wall-clock at ``__enter__``, computes elapsed at
    ``__exit__``, and calls :func:`log_endpoint_timing` with the
    accumulated extras. On exception, ``status`` flips to ``"error"``
    automatically and the exception type name is added under
    ``error_type`` so log scans can group by failure mode without
    re-parsing the message.
    """

    __slots__ = ("endpoint", "extras", "_start", "status")

    def __init__(self, endpoint: str, **extras: Any):
        self.endpoint = endpoint
        self.extras: dict[str, Any] = dict(extras)
        self._start = 0.0
        self.status = "ok"

    def __enter__(self) -> _EndpointTimer:
        self._start = time.perf_counter()
        return self

    def __exit__(self, exc_type: type[BaseException] | None, *_: Any) -> None:
        elapsed_ms = round((time.perf_counter() - self._start) * 1000, 1)
        if exc_type is not None:
            self.status = "error"
            self.extras.setdefault("error_type", exc_type.__name__)
        log_endpoint_timing(self.endpoint, elapsed_ms, status=self.status, **self.extras)

    def add(self, **extras: Any) -> None:
        """Attach extras inside the ``with`` block (e.g. ``timer.add(rows=42)``)."""
        self.extras.update(extras)


def time_endpoint(endpoint: str, **extras: Any) -> _EndpointTimer:
    """Public name for :class:`_EndpointTimer` — use as a context manager.

    Pattern::

        with time_endpoint("/foo", form="json") as t:
            result = do_work()
            t.add(rows=len(result))
        return result
    """
    return _EndpointTimer(endpoint, **extras)


# ---------------------------------------------------------------------------
# LLM telemetry — log_llm_call, current_llm_provider_and_model, cost helper
# ---------------------------------------------------------------------------
#
# Mirrors :mod:`arango_cypher.service.observability` byte-for-byte where
# the semantics carry over. The pricing table itself lives in
# :mod:`arango_sparql.nl2sparql.cost` so the pipeline (which has no
# reason to import the service layer) can still compute a USD figure.

_llm_logger = logging.getLogger("arango_sparql.service.llm")


def estimate_llm_cost_usd(
    provider: str | None,
    model: str | None,
    prompt_tokens: int,
    completion_tokens: int,
) -> float:
    """Re-export of :func:`arango_sparql.nl2sparql.cost.estimate_llm_cost_usd`.

    Re-exported here so the route layer's ``log_llm_call`` callsite has
    a one-import surface; callers that don't already import the
    pipeline don't need to pull it in just for the cost arithmetic.
    """
    # Lazy import — the nl2sparql package imports models from
    # service.models, so importing it at module-load time creates a
    # circular import. Calling at use-time bypasses the cycle and the
    # cost is one cached attribute lookup per call.
    from ..nl2sparql.cost import estimate_llm_cost_usd as _estimate

    return _estimate(provider, model, prompt_tokens, completion_tokens)


def log_llm_call(
    *,
    endpoint: str | None = None,
    provider: str | None,
    model: str | None,
    prompt_tokens: int,
    completion_tokens: int,
    cached_tokens: int = 0,
    latency_ms: float | None = None,
    cost_usd: float | None = None,
    method: str | None = None,
    **extras: Any,
) -> None:
    """Emit one INFO line for an LLM round-trip.

    Called from ``/nl-translate``, ``/nl-explain``, and ``/nl-execute``
    after the pipeline returns. ``provider`` / ``model`` are nullable
    so a fallback path that didn't hit an LLM (e.g. SPARQL-only
    explain) can call the helper too with zero tokens — gives a
    uniform grep target for "every NL request, regardless of LLM-or-
    not".

    ``cost_usd`` is computed via :func:`estimate_llm_cost_usd` when
    not supplied; pass it explicitly when the pipeline already has the
    aggregate (e.g. cumulative cost across a repaired multi-call run)
    so a downstream sum doesn't double-count.

    Reserved log-record keys (``correlation_id``, ``msg``, ``level``,
    …) in ``extras`` are silently dropped to keep the formatter
    contract clean.
    """
    if cost_usd is None:
        cost_usd = (
            estimate_llm_cost_usd(provider, model, prompt_tokens, completion_tokens)
            if provider and model
            else 0.0
        )
    safe_extras = {
        k: _sanitize_extra_value(v) for k, v in extras.items() if k not in _KeyValueFormatter._RESERVED
    }
    _llm_logger.info(
        "llm_call",
        extra={
            "endpoint": endpoint or "-",
            "provider": provider or "-",
            "model": model or "-",
            "prompt_tokens": int(prompt_tokens),
            "completion_tokens": int(completion_tokens),
            "cached_tokens": int(cached_tokens),
            "cost_usd": float(cost_usd),
            "latency_ms": float(latency_ms) if latency_ms is not None else 0.0,
            "method": method or "-",
            **safe_extras,
        },
    )


def current_llm_provider_and_model() -> tuple[str | None, str | None]:
    """Best-effort read of the configured NL → SPARQL provider + model.

    Reads from environment variables rather than instantiating a
    client (which would require a valid API key for nothing). Order
    of preference:

    1. ``NL2SPARQL_PROVIDER`` + ``NL2SPARQL_MODEL`` (canonical, used
       by :func:`arango_sparql.nl2sparql.client.get_default_client`).
    2. ``LLM_PROVIDER`` + the relevant ``*_MODEL`` env (Cypher-style
       fallback so a single-shell setup that already configured
       ``LLM_PROVIDER=openai`` doesn't need a duplicate var).
    3. Inference from which model env is set, in priority order
       Anthropic → OpenRouter → OpenAI.

    Returns ``(None, None)`` when none of those resolve — the caller
    logs ``-`` in that case.
    """
    provider = (os.getenv("NL2SPARQL_PROVIDER") or "").strip().lower() or None
    model = os.getenv("NL2SPARQL_MODEL") or None
    if provider and model:
        return provider, model

    legacy_provider = (os.getenv("LLM_PROVIDER") or "").strip().lower() or None
    if provider is None:
        provider = legacy_provider
    if model is None:
        if provider == "anthropic":
            model = os.getenv("ANTHROPIC_MODEL") or os.getenv("OPENAI_MODEL")
        elif provider == "openrouter":
            model = os.getenv("OPENROUTER_MODEL") or os.getenv("OPENAI_MODEL")
        elif provider == "openai":
            model = os.getenv("OPENAI_MODEL")

    if provider is None and model is None:
        if os.getenv("ANTHROPIC_MODEL"):
            provider, model = "anthropic", os.getenv("ANTHROPIC_MODEL")
        elif os.getenv("OPENROUTER_MODEL"):
            provider, model = "openrouter", os.getenv("OPENROUTER_MODEL")
        elif os.getenv("OPENAI_MODEL"):
            provider, model = "openai", os.getenv("OPENAI_MODEL")
    return provider, model
