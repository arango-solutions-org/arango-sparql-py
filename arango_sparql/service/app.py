"""FastAPI app factory + startup-time guards.

Mirrors ``arango_cypher.service.app``:

1. The ``app = FastAPI(...)`` instance every route module decorates.
2. CORS middleware with the same credentialed-wildcard guardrail.
3. The ``ARANGO_SPARQL_PUBLIC_MODE`` flag readout that flips the
   service from local-dev defaults to public-internet defaults.
"""

from __future__ import annotations

import logging as _logging
import os

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .. import __version__

app = FastAPI(
    title="Arango SPARQL",
    description="SPARQL 1.1 → AQL translation service for ArangoDB",
    # Read from the package's single source of truth so openapi.json reports the
    # real release — the BYOC deploy verifier asserts against exactly this.
    version=__version__,
    root_path=os.getenv("ROOT_PATH", ""),
)

# Public-mode flag: matches arango-cypher-py's ARANGO_CYPHER_PUBLIC_MODE.
# Single switch that flips the service from "single-user / local-dev /
# inside-trusted-network" defaults to "shared / multi-user / public-internet"
# defaults. Read once at import time so the running config stays
# deterministic for an operator.
_PUBLIC_MODE = os.getenv("ARANGO_SPARQL_PUBLIC_MODE", "").lower() in ("true", "1", "yes")

_svc_logger = _logging.getLogger("arango_sparql.service")

_cors_origins_raw = os.getenv("CORS_ALLOWED_ORIGINS", "*")
_cors_origins = (
    ["*"]
    if _cors_origins_raw.strip() == "*"
    else [o.strip() for o in _cors_origins_raw.split(",") if o.strip()]
)

# CORS credentialed-wildcard guardrail — see arango-cypher-py/service/app.py
# for the full rationale; the matrix is identical here.
_cors_credentials_raw = os.getenv("ARANGO_SPARQL_CORS_CREDENTIALS")
_cors_is_wildcard = _cors_origins == ["*"]
if _cors_is_wildcard and _cors_credentials_raw and _cors_credentials_raw.lower() in ("1", "true", "yes"):
    raise RuntimeError(
        "Refusing to start: CORS_ALLOWED_ORIGINS='*' combined with "
        "ARANGO_SPARQL_CORS_CREDENTIALS=true is unsafe. Pin an explicit "
        "origin list or unset ARANGO_SPARQL_CORS_CREDENTIALS."
    )
if _cors_is_wildcard:
    _cors_credentials = False
    if _cors_credentials_raw is None:
        _svc_logger.warning(
            "CORS_ALLOWED_ORIGINS='*' detected; allow_credentials forced off. "
            "Pin an explicit origin list to enable credentialed CORS."
        )
else:
    _cors_credentials = True
    if _cors_credentials_raw is not None:
        _cors_credentials = _cors_credentials_raw.lower() in ("1", "true", "yes")

app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=_cors_credentials,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Observability spine. Imported here (rather than from the package
# init) so the middleware install happens at app-construction time,
# alongside CORS, and the logging filter / handler attachment runs
# before any route module's import-time logger.* calls.
#
# Middleware order matters: ``CorrelationIdMiddleware`` is added *after*
# ``CORSMiddleware`` above, which means it runs *first* on the inbound
# path (Starlette wraps middlewares LIFO). That's deliberate — we want
# the correlation ID minted before the CORS preflight handler emits its
# log line, not after, so even rejected preflights carry an X-Request-Id
# in the log trail for cross-referencing client / server traces.
from .observability import CorrelationIdMiddleware, configure_observability  # noqa: E402

configure_observability()
app.add_middleware(CorrelationIdMiddleware)


# ---------------------------------------------------------------------------
# Startup guard — PRD §6.3.4 ``_require_analyzer_unless_opted_out``
# ---------------------------------------------------------------------------
#
# This guard runs at app-import time so a misconfigured deployment
# fails *before* the first request lands rather than mid-flight on
# the first ``/schema/introspect`` call. Two env vars govern the
# four reachable cells (PRD §6.3.4):
#
# 1. ``SCHEMA_ANALYZER_REQUIRED=true`` (default) + analyzer importable
#    → boot.
# 2. ``SCHEMA_ANALYZER_REQUIRED=true`` + analyzer **not** importable
#    → boot refused with a clear install hint.
# 3. ``SCHEMA_ANALYZER_REQUIRED=false`` (operator opt-out) → boot
#    regardless. The route layer's per-request fallback gate
#    (``ARANGO_SPARQL_ALLOW_HEURISTIC``) takes over from here.
# 4. ``ARANGO_SPARQL_PUBLIC_MODE=true`` forces both opt-outs off
#    so a public deployment cannot accidentally degrade to the
#    heuristic path. The boot guard already runs *after* the
#    ``_PUBLIC_MODE`` readout above so that constraint is honoured.

# Pinned analyzer version range — single source of truth for the
# install hint. Must match the pin in :mod:`arango_sparql.schema.acquire`
# (``ANALYZER_VERSION_RANGE``). When the range bumps, the test in
# ``tests/test_service_startup_guard.py::test_install_hint_matches_acquire``
# will fail and force the operator to update both sites in lockstep.
ANALYZER_VERSION_RANGE: str = ">=0.14.1,<0.23.0"
ANALYZER_INSTALL_HINT: str = (
    f"pip install 'arangodb-schema-analyzer{ANALYZER_VERSION_RANGE}' "
    "(or set SCHEMA_ANALYZER_REQUIRED=false for a heuristic-only deployment)"
)


class AnalyzerStartupGuardError(RuntimeError):
    """Raised by :func:`_require_analyzer_unless_opted_out` when the
    analyzer extra is missing and the operator has not opted into a
    heuristic-only deployment.

    Carries the install hint so the surrounding process supervisor
    (systemd, Docker, Kubernetes pod log) surfaces an actionable
    error message rather than a bare ``ImportError`` traceback.
    """

    def __init__(self, *, install_hint: str = ANALYZER_INSTALL_HINT) -> None:
        super().__init__(
            f"SCHEMA_ANALYZER_REQUIRED=true but arangodb-schema-analyzer is not installed. {install_hint}"
        )
        self.install_hint = install_hint


def _require_analyzer_unless_opted_out() -> None:
    """PRD §6.3.4 startup guard.

    Reads :envvar:`SCHEMA_ANALYZER_REQUIRED` (default ``true``) and
    refuses to start when the analyzer extra cannot be imported.
    The opt-out gate is deliberately verbose so a heuristic-only or
    schema-less deployment is a conscious operator decision, not a
    silent default (PRD §6.3.4 last paragraph).

    Idempotent: safe to call from tests that need to re-run the
    guard under different env-var states. The function never
    mutates module-level state — every input it reads is from
    ``os.environ`` or from the (immutable) installed-package set.
    """

    # Opt-out is deliberately verbose (PRD §6.3.4 last paragraph): only
    # an explicit known-false value disables the requirement. An unset,
    # empty, or unrecognised value falls through to "required=True" so
    # a typo in deployment YAML cannot silently degrade the service.
    raw = (os.getenv("SCHEMA_ANALYZER_REQUIRED") or "").strip().lower()
    explicit_opt_out = raw in ("false", "0", "no")
    if explicit_opt_out:
        _svc_logger.info(
            "Startup guard: SCHEMA_ANALYZER_REQUIRED=%r — analyzer extra is optional for this deployment.",
            raw,
        )
        return

    try:
        import schema_analyzer  # noqa: F401
    except ImportError as exc:
        _svc_logger.error(
            "Startup guard: arangodb-schema-analyzer is not importable and SCHEMA_ANALYZER_REQUIRED=%r. %s",
            raw,
            ANALYZER_INSTALL_HINT,
        )
        raise AnalyzerStartupGuardError() from exc

    _svc_logger.info(
        "Startup guard: arangodb-schema-analyzer importable; analyzer "
        "path is the canonical mapping source for this deployment."
    )


# Run the guard at import time. Tests can opt-out via
# ``ARANGO_SPARQL_SKIP_STARTUP_GUARD=1`` so they can exercise
# different env-var combinations without crashing pytest's own
# import phase. The default (env var unset) runs the guard
# unconditionally, matching production semantics.
if os.getenv("ARANGO_SPARQL_SKIP_STARTUP_GUARD", "").lower() not in (
    "1",
    "true",
    "yes",
):
    _require_analyzer_unless_opted_out()
