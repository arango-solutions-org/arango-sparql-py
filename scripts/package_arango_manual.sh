#!/usr/bin/env bash
# Build the BYOC tarball for the ArangoDB Container Manager (manual packaging).
#
# Companion to the arango-byoc-deploy tool, which uploads + deploys what this builds.
# See docs/BYOC_DEPLOYMENT.md.
#
# Layout: FLAT archive — `entrypoint` and `pyproject.toml` at the ROOT of the
# tar. `entrypoint` must be Python whose line-1 first token is literally
# `entrypoint` (the platform runs `python /project/$(awk '{print $1}' entrypoint)`).
# A nested layout fails inside the platform with "No entrypoint found".
#
# UI: bundled by default (the SPARQL workbench). `ui/vite.config.ts` uses
# base:"./" (relative asset URLs), so NO build-time mount prefix is needed —
# the bundle works under any Container-Manager path. Set PACKAGE_NO_UI=1 to
# ship a bare API (deploy with `arango-byoc-deploy --no-ui release`).
#
# .env: NOT bundled unless PACKAGE_INCLUDE_ENV=1 — a local .env typically holds
# OPENAI_API_KEY / ARANGO_PASSWORD etc. that must not leak into a shared tarball.
# Prefer Container Manager UI env vars for secrets.
#
# macOS: strip Apple xattrs / AppleDouble so GNU tar on the Linux cluster
# extracts cleanly.
set -euo pipefail
export COPYFILE_DISABLE=1

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="${1:-${REPO_ROOT}/arango-sparql-py.tar.gz}"
STAGE="$(mktemp -d)"
cleanup() { rm -rf "${STAGE}"; }
trap cleanup EXIT

NAME="${PACKAGE_DIR_NAME:-arango-sparql-py}"
ROOTDIR="${STAGE}/${NAME}"
mkdir -p "${ROOTDIR}"

# --- source the editable install needs (pyproject + package + metadata files) --
cp -R "${REPO_ROOT}/arango_sparql" "${ROOTDIR}/"
cp "${REPO_ROOT}/pyproject.toml" "${ROOTDIR}/"
cp "${REPO_ROOT}/README.md" "${ROOTDIR}/"      # pyproject readme= (build metadata)
cp "${REPO_ROOT}/LICENSE" "${ROOTDIR}/"        # pyproject license=
[[ -f "${REPO_ROOT}/uv.lock" ]] && cp "${REPO_ROOT}/uv.lock" "${ROOTDIR}/"
cp "${REPO_ROOT}/entrypoint" "${ROOTDIR}/entrypoint"
chmod +x "${ROOTDIR}/entrypoint"

# Drop compiled caches so the archive is lean + reproducible.
find "${ROOTDIR}/arango_sparql" -name "__pycache__" -type d -prune -exec rm -rf {} + 2>/dev/null || true

# --- UI bundle (default on) ----------------------------------------------------
if [[ "${PACKAGE_NO_UI:-0}" != "1" ]]; then
    if [[ ! -d "${REPO_ROOT}/ui" ]]; then
        echo "error: no ui/ directory; set PACKAGE_NO_UI=1 to ship a bare API" >&2
        exit 1
    fi
    if ! command -v npm >/dev/null 2>&1; then
        echo "error: npm not on PATH (needed to build the UI); set PACKAGE_NO_UI=1 to skip" >&2
        exit 1
    fi
    echo "==> Building SPARQL workbench UI (relative base — no prefix needed)..."
    (
        cd "${REPO_ROOT}/ui"
        if [[ -f package-lock.json ]]; then npm ci; else npm install; fi
        rm -rf dist
        npm run build
    )
    if [[ ! -f "${REPO_ROOT}/ui/dist/index.html" ]]; then
        echo "error: ui build produced no ui/dist/index.html" >&2
        exit 1
    fi
    mkdir -p "${ROOTDIR}/ui"
    cp -R "${REPO_ROOT}/ui/dist" "${ROOTDIR}/ui/dist"
    echo "==> Bundled ui/dist (served by the app at the mount root)"
else
    echo "==> PACKAGE_NO_UI=1 — shipping bare API (deploy with --no-ui)"
fi

# --- optional baked .env (SANITIZED allowlist — opt-in) -----------------------
# PACKAGE_INCLUDE_ENV=1 bakes ONLY the keys the service actually reads (the
# allowlist below), so publish tokens (PYPI_*), test flags (RUN_*) or anything
# else in a developer .env never ships inside a deployment artifact. This is the
# REAL guard: the arango-byoc-deploy preflight only refuses baked *_API_KEY
# names, so it would happily let PYPI_TOKEN through. PACKAGE_ENV_FILE overrides
# the source (default: repo-root .env) so you can bake a deploy-specific file.
ENV_SRC="${PACKAGE_ENV_FILE:-${REPO_ROOT}/.env}"
# ARANGO_* = connection + ARANGO_SPARQL_* service config; the rest are
# CORS / session / LLM / uvicorn. (ROOT_PATH is appended below, not from here.)
BAKE_ALLOW='^(export +)?(ARANGO_[A-Z0-9_]*|CORS_ALLOWED_ORIGINS|SESSION_TTL_SECONDS|MAX_SESSIONS|OPENAI_API_KEY|ANTHROPIC_API_KEY|OPENROUTER_API_KEY|LLM_PROVIDER|PORT|HOST)='
if [[ "${PACKAGE_INCLUDE_ENV:-0}" == "1" ]]; then
    if [[ -f "${ENV_SRC}" ]]; then
        grep -E "${BAKE_ALLOW}" "${ENV_SRC}" > "${ROOTDIR}/.env" || true
        echo "==> Baked a SANITIZED .env from ${ENV_SRC} (service keys only; PYPI_*/RUN_* excluded)."
        echo "    Keys baked: $(cut -d= -f1 "${ROOTDIR}/.env" | sed -E 's/^export +//' | tr '\n' ' ')"
    else
        echo "==> PACKAGE_INCLUDE_ENV=1 but no env file at ${ENV_SRC} — nothing bundled." >&2
    fi
elif [[ -f "${ENV_SRC}" ]]; then
    echo "==> Skipping .env (PACKAGE_INCLUDE_ENV=1 bakes a sanitized subset; or set env in the Container Manager UI)." >&2
fi

# --- bake ROOT_PATH (REQUIRED for a path-prefixed Container Manager mount) -----
# The platform does NOT forward app env at deploy time, so the service's mount
# prefix must be baked into a bundled .env (the FastAPI app reads it via
# load_dotenv() → root_path). Without it, /openapi.json, /docs and the API
# resolve at the CLUSTER root instead of under /_service/uds/_db/<db>/<instance>/
# (the UI still works — its asset URLs are relative). Mirrors arango-cypher-py's
# scripts/package-byoc.sh. Append, so it coexists with a baked full .env above.
if [[ -n "${SERVICE_ROOT_PATH:-}" ]]; then
    printf 'ROOT_PATH=%s\n' "${SERVICE_ROOT_PATH%/}" >> "${ROOTDIR}/.env"
    echo "==> Baked ROOT_PATH=${SERVICE_ROOT_PATH%/} into the bundle"
else
    echo "==> WARNING: no SERVICE_ROOT_PATH set — /openapi.json, /docs and the API" >&2
    echo "    will resolve at the cluster root, not under the service mount. Pass" >&2
    echo "    SERVICE_ROOT_PATH=/_service/uds/_db/<db>/<instance> to fix it." >&2
fi

# --- bake JSON logging (PRD §9.5: production deployments MUST log JSON) --------
# The service defaults to key=value log lines, which suit a developer terminal;
# a deployed bundle logs one JSON object per line. Baked for the same reason as
# ROOT_PATH: the platform drops app env at deploy time. An env file that already
# sets ARANGO_SPARQL_LOG_JSON (either value) wins, so an operator can opt out.
if grep -qE '^(export +)?ARANGO_SPARQL_LOG_JSON=' "${ROOTDIR}/.env" 2>/dev/null; then
    echo "==> ARANGO_SPARQL_LOG_JSON already set by the baked env file; leaving it"
else
    printf 'ARANGO_SPARQL_LOG_JSON=1\n' >> "${ROOTDIR}/.env"
    echo "==> Baked ARANGO_SPARQL_LOG_JSON=1 (JSON log lines, PRD §9.5)"
fi

# Strip lingering macOS xattrs (provenance/quarantine) that break Linux tar.
if [[ "$(uname -s)" == "Darwin" ]] && command -v xattr >/dev/null 2>&1; then
    xattr -cr "${ROOTDIR}" 2>/dev/null || true
fi

# Flat archive: entrypoint + pyproject.toml at the archive root.
tar -czf "${OUT}" -C "${ROOTDIR}" .
echo "Wrote ${OUT} (flat: entrypoint + pyproject.toml at archive root)"
