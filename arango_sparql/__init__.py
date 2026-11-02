"""Top-level package for ``arango-sparql-py``.

Public entry points:

- :func:`arango_sparql.api.translate` — SPARQL → AQL translation.
- :mod:`arango_sparql.service` — the FastAPI application.
- :mod:`arango_sparql.nl2sparql` — natural-language → SPARQL pipeline.
"""

from __future__ import annotations

# Single source of truth for the package version. Kept in lockstep with
# pyproject.toml [project].version (tests/test_version_parity.py enforces it);
# the BYOC deploy tool (arango-byoc-deploy, [tool.arango-byoc] in pyproject) tags
# releases with it and verifies that openapi.json reports it after a deploy.
__version__ = "0.6.0"
