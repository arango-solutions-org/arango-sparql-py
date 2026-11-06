# syntax=docker/dockerfile:1.7
# ============================================================================
# The service and its UI as a project's query tool under a governance control
# plane (see arango_sparql/service/control_plane.py). The proxy in front strips
# the mount prefix (e.g. /query); the page derives its API base from its own
# address.
#
# Needs the control plane's Python SDK as a named build context:
#
#   docker build -f control-plane.Dockerfile \
#     --build-context control_plane_sdk=<control plane repo>/sdk/python .
#
# Runtime: CONTROL_PLANE_URL (the control plane's API), ARANGO_SPARQL_TENANT
# (the tenant this deployment serves), ARANGO_SPARQL_SOURCE (the control
# plane's id for the ArangoDB deployment below), and ARANGO_URL, ARANGO_USER,
# ARANGO_PASSWORD (a credential that can read the projects' databases; a
# read-only user is enough, and ARANGO_USER has no default). CONTROL_PLANE_HOME is where a user without a
# context is sent. The language model is the deployment's (OPENAI_API_KEY or
# ANTHROPIC_API_KEY, see README).
# ============================================================================

# Replaced by --build-context control_plane_sdk=...; empty if not given, and the
# install step below then fails with a message rather than building an image
# that cannot verify anyone.
FROM scratch AS control_plane_sdk

# ---- the UI ----------------------------------------------------------
FROM node:22-slim AS ui
WORKDIR /build/ui
COPY ui/package.json ui/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY ui/ ./
# The editor's grammar core is shared with the service and imported from outside ui/.
COPY arango_sparql/editor /build/arango_sparql/editor
RUN npm run build

# ---- the service, with the SDK ---------------------------------------
FROM python:3.12-slim AS build
WORKDIR /src
COPY pyproject.toml README.md LICENSE ./
COPY arango_sparql ./arango_sparql
RUN python -m venv /venv \
    && /venv/bin/pip install --no-cache-dir --upgrade pip \
    && /venv/bin/pip install --no-cache-dir ".[service,nl,cli]"
RUN --mount=type=bind,from=control_plane_sdk,target=/sdk \
    if [ -f /sdk/pyproject.toml ]; then /venv/bin/pip install --no-cache-dir /sdk; \
    else echo "missing --build-context control_plane_sdk=<control plane repo>/sdk/python" >&2; exit 1; fi
# The service would refuse to start without the SDK, and to answer any question with one that
# lacks a name the adapter uses (`Asked` records questions): fail here instead.
RUN /venv/bin/python -c "from control_plane_sdk import Action, Asked, ControlPlane, ControlPlaneError, Decision, Target, Unavailable"

# ---- runtime ---------------------------------------------------------
FROM python:3.12-slim
RUN useradd --system --no-create-home appuser
COPY --from=build /venv /venv
COPY --from=ui /build/ui/dist /app/ui
ENV PATH="/venv/bin:$PATH" \
    ARANGO_SPARQL_UI_DIR=/app/ui
USER appuser
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=3s --start-period=10s --retries=4 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2).status == 200 else 1)" || exit 1
CMD ["arango-sparql-py", "serve", "--host", "0.0.0.0", "--port", "8000"]
