# BYOC deployment — ArangoDB Container Manager

Deploy `arango-sparql-py` (the `/sparql` + NL API **and** the SPARQL workbench
UI, one process) into the **ArangoDB Platform "Container Manager"** that runs
alongside a customer's ArangoDB cluster — "bring your own cloud". This is the
standalone deployment story; the contextual-data-fabric embeds the transpiler as
a library instead.

Modelled on `arango-ontoextract`'s BYOC toolchain (same platform protocol). Two
pieces:

- **`scripts/package_arango_manual.sh`** — builds a flat `.tar.gz` of the Python
  source + the built UI + `entrypoint`.
- **[`arango-byoc-deploy`](https://github.com/ArthurKeen/arango-byoc-deploy)** — the
  shared estate deploy tool: uploads it to the platform, (re)deploys the service,
  and verifies the right version is serving. Settings live in `[tool.arango-byoc]`
  in `pyproject.toml`; it is pinned in the `deploy` dependency group.

> **How it differs from a Docker deploy:** BYOC ships a **source tarball**, not
> an image. The platform runs it on a base image (`py12base`) that installs deps
> and launches uvicorn via the root `entrypoint` file. (The repo's `Dockerfile`
> is the *separate* image/registry path.)

## Prerequisites

1. **`.env` at the repo root** with the platform coordinator + credentials. The
   deploy script reads these to log into the Container Manager (and they double
   as the service's ArangoDB connection):

   | Var | Meaning |
   |---|---|
   | `ARANGO_URL` | platform coordinator URL, e.g. `https://prod.demo.pilot.arango.ai:8529` |
   | `ARANGO_USER` | platform user |
   | `ARANGO_PASSWORD` | platform password |
   | `ARANGO_DB` | database to scope the mount to (empty ⇒ global) |

   `.env` is gitignored — never commit it. (`ARANGO_ENDPOINT` / `ARANGO_USERNAME`
   are also accepted as cross-estate fallbacks.)
2. **`npm`** on PATH (the UI is built into the tarball) — or set `PACKAGE_NO_UI=1`.
3. `pip install requests` (only dependency the deploy script needs).

## Build the tarball

```bash
# UI bundled by default (Vite base:"./" → works under any mount path, no rebuild):
bash scripts/package_arango_manual.sh            # -> ./arango-sparql-py.tar.gz

# Bare API, no UI:
PACKAGE_NO_UI=1 bash scripts/package_arango_manual.sh

# Bake your .env's service keys into the tarball (so the service has creds
# without pasting them into the Container Manager UI). OFF by default — the .env
# holds secrets. (Every bundle still carries a .env with the packager's own
# deployment settings: ARANGO_SPARQL_LOG_JSON=1, and ROOT_PATH when set.)
PACKAGE_INCLUDE_ENV=1 bash scripts/package_arango_manual.sh
```

If you do **not** bake `.env`, supply `ARANGO_URL` / `ARANGO_USER` /
`ARANGO_PASSWORD` (and `ARANGO_DB`) as **Container Manager env vars** on the
service instead — that's the more secure path for shared clusters.

## Deploy

```bash
uv run --group deploy arango-byoc-deploy list                  # what's uploaded / deployed
uv run --group deploy arango-byoc-deploy update                # the usual: preflight → upload → swap → verify
uv run --group deploy arango-byoc-deploy verify --expect-version 0.2.0
uv run --group deploy arango-byoc-deploy rollback --to 0.2.0-1 # redeploy an uploaded build; verifies its release
uv run --group deploy arango-byoc-deploy delete                # remove the instance
```

`update` derives the release from `[project].version` in `pyproject.toml`,
uploads under `<version>-<n>`, then swaps the live service.

> ⚠️ **There is no in-place update.** A POST against a live instance fails with a
> Helm ownership error, so `update` **deletes then recreates** — the URL is down
> for ~60s (cold start on `py12base`). The upload happens *before* the delete, so
> a bad artifact fails while the old service still serves.

Defaults live in `[tool.arango-byoc]`; override per run with `--instance`,
`--db` (`''` for global) and `--no-ui` (placed before the command, e.g.
`uv run --group deploy arango-byoc-deploy --no-ui update`). Scope defaults to `ARANGO_DB`. A wrong `--base-image` fails fast and lists the cluster's valid ones.

## The mount path + `ROOT_PATH` (important for the UI+API to align)

The platform serves the instance under a path prefix:

```
/_service/uds/_db/<db>/<instance>/        (or /_global/<instance>/ when --db '')
```

- The **UI** works there automatically (relative asset URLs).
- For the **API** (`/openapi.json`, `/sparql`, …) to answer under the same
  prefix, set the service env **`ROOT_PATH`** to that mount path (the deploy
  prints it). Add it to the baked `.env` or the Container Manager env vars, e.g.
  `ROOT_PATH=/_service/uds/_db/mydb/arango-sparql-py`.

## Verify

`update` finishes by polling `<mount>/` (or `<mount>/health` for `--no-ui`) until
HTTP 200, then requires: the root to return 200 when the UI is bundled;
`openapi.json`'s version to equal the release; every asset the page references
to load from under the mount; and `<mount>/health` to answer. Run it standalone
any time:

```bash
uv run --group deploy arango-byoc-deploy verify --expect-version 0.2.0
```

## Health endpoints

- `GET <mount>/health` — liveness (`{"status":"ok","version":…}`)
- `GET <mount>/health/ready` — readiness; pings the configured ArangoDB (503 when unreachable)

## Environment variables the service reads

Beyond the `ARANGO_*` connection vars above: `ARANGO_SPARQL_PUBLIC_MODE`,
`CORS_ALLOWED_ORIGINS`, `ARANGO_SPARQL_CORS_CREDENTIALS`, `SESSION_TTL_SECONDS`,
`MAX_SESSIONS`, `OPENAI_API_KEY`/`ANTHROPIC_API_KEY` (NL), `ROOT_PATH` (above),
and `ARANGO_SPARQL_UI_DIR` (set automatically by `entrypoint` to the bundled
`ui/dist`; the service serves the SPA only when this points at a real bundle).

**Logging.** The packager bakes `ARANGO_SPARQL_LOG_JSON=1` into every bundle,
so a deployed service writes one JSON object per log line (PRD §9.5 requires
JSON in production). The service itself defaults to `key=value` lines for
local runs. To ship a bundle with `key=value` logs, set
`ARANGO_SPARQL_LOG_JSON=0` in the env file you bake (`PACKAGE_ENV_FILE`); an
explicit value there is kept. Write it at the start of a line with no spaces
around `=`: the packager refuses an env file that sets the variable in a form
it cannot bake, rather than override it.
