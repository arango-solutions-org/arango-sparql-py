"""ASGI entry point for the ``arango_sparql.service`` FastAPI app.

Mirrors ``arango-cypher-py/main.py`` so deployment scripts (ServiceMaker,
Container Manager, plain ``uvicorn``) work the same across both repos.
"""

from __future__ import annotations

import os

import uvicorn

if __name__ == "__main__":
    port = int(os.getenv("PORT", "8000"))
    host = os.getenv("HOST", "0.0.0.0")

    # Best-effort: create the configured ``ARANGO_DB`` if it doesn't
    # exist yet (local-dev / demo convenience). Gated off in public mode
    # and via ``ARANGO_SPARQL_SKIP_DB_BOOTSTRAP``; never raises, so a DB
    # outage can't stop the translation-only service from booting.
    # ``.env`` is loaded as a side effect of importing the service app.
    from arango_sparql.arango_admin import maybe_bootstrap_configured_database
    from arango_sparql.service import app as _app  # noqa: F401  (triggers load_dotenv)

    maybe_bootstrap_configured_database()

    # ``log_config=None``: importing the app above already routed uvicorn's
    # loggers through the service's handler (stdout, the configured format;
    # PRD §9.5). uvicorn's default config would put its own formatter back;
    # with none it only sets the levels.
    uvicorn.run(
        "arango_sparql.service:app",
        host=host,
        port=port,
        reload=False,
        proxy_headers=True,
        forwarded_allow_ips="*",
        log_config=None,
        log_level="info",
    )
