# arango-sparql-py

[![CI](https://img.shields.io/github/actions/workflow/status/arango-solutions/arango-sparql-py/ci.yml?branch=main&label=CI&logo=github)](https://github.com/arango-solutions/arango-sparql-py/actions/workflows/ci.yml)
[![Python](https://img.shields.io/badge/python-3.11%20%7C%203.12-blue?logo=python&logoColor=white)](pyproject.toml)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)
[![W3C DAWG](https://img.shields.io/badge/W3C%20DAWG%20query%20eval-95.7%25-brightgreen)](tests/w3c/COVERAGE_REPORT.md)

> **Status: v0.1 — active development.** The translator and HTTP service
> are working; a W3C-conformant `/sparql` Protocol endpoint is on the v1.0
> roadmap. See [`docs/architecture/PRD.md`](docs/architecture/PRD.md) for
> the full v1 spec, scope, and release milestones.

Python-native **SPARQL 1.1 → ArangoDB AQL** transpiler and FastAPI
microservice. Modernizes the legacy JavaScript Foxx
[`arango-sparql`](https://github.com/ArthurKeen/arango-sparql) service and
deliberately mirrors the architecture of its sister project
[`arango-cypher-py`](https://github.com/ArthurKeen/arango-cypher-py), so a
developer fluent in one repo can read the other immediately.

## Why this exists

ArangoDB is a multi-model database with a powerful native query language
(AQL), but lots of teams have invested in **SPARQL** — for ontology-driven
data, federated knowledge graphs, or simply because their data already
lives as RDF/Turtle. `arango-sparql-py` lets those teams point a
SPARQL 1.1 query at ArangoDB without first re-modeling their data:

1. The query is parsed by [`rdflib`](https://rdflib.readthedocs.io/)
   (W3C-grade SPARQL 1.1 parser).
2. An [OWL ontology](https://github.com/ArthurKeen/arango-schema-analyzer)
   describing the physical schema is loaded once at startup; IRIs in the
   query are resolved to ArangoDB collection / property names.
3. The algebra walker emits parameterized AQL with bind variables (no
   string interpolation, no injection vector).
4. Optionally: the AQL is executed against ArangoDB and results are
   surfaced back as bindings.

Cross-validation against [`pyoxigraph`](https://pyoxigraph.readthedocs.io/)
(W3C-conformant Rust triplestore via Python bindings) keeps every
translation honest.

## Example: SPARQL in → AQL out

```sparql
PREFIX : <http://ex.org/>
SELECT ?n ?title WHERE {
  { ?p   a :Person  ; :name  ?n }
  { ?prj a :Project ; :title ?title ; :owner ?p }
}
ORDER BY ?n LIMIT 10
```

translates to:

```aql
FOR doc1 IN @@c1_Person
FOR doc2 IN @@c2_Project
FILTER doc2.owner == doc1._uri
SORT doc1.name ASC
LIMIT 10
RETURN { n: doc1.name, title: doc2.title }
```

with `bind_vars = {"@c1_Person": "Person", "@c2_Project": "Project"}`.

## Architecture at a glance

| Concern               | Implementation                                                       |
| --------------------- | -------------------------------------------------------------------- |
| SPARQL parsing        | `rdflib.plugins.sparql.parser.parseQuery` + Algebra translation      |
| AQL emission          | Parameterized AQL builder (port of legacy `aql-query-builder.js`)    |
| Schema mapping        | `MappingBundle` / CSI v1 from [`arango-schema-analyzer`](https://github.com/ArthurKeen/arango-schema-analyzer) is the contract of record; its OWL/Turtle export is one serialisation of it, loaded once into `rdflib.Graph` |
| HTTP service          | FastAPI (`arango_sparql.service`) — mirror of [`arango_cypher.service`](https://github.com/ArthurKeen/arango-cypher-py) |
| NL → SPARQL           | LLM-backed pipeline (`arango_sparql.nl2sparql`) with cost accounting + repair loop |
| Reference triplestore | [`pyoxigraph`](https://pyoxigraph.readthedocs.io/), embedded, W3C-compliant — used as cross-validation gold |
| Test harnesses        | `pytest` + W3C SPARQL 1.1 DAWG runner (`tests/w3c/`) + cross-validation suite |
| Frontend              | Vite + React + TypeScript, CodeMirror SPARQL mode, Cytoscape.js graph view |

## Quickstart

```bash
git clone https://github.com/arango-solutions/arango-sparql-py.git
cd arango-sparql-py

# 1. Install (works with `uv` or plain `pip install -e ".[dev]"`)
uv sync --all-extras

# 2. Run the smoke tests (no DB needed)
uv run pytest -q -m "not integration and not w3c and not eval"

# 3. Boot the service (defaults to http://localhost:8000)
uv run python main.py

# 4. (optional) Stand up ArangoDB for /execute round-trips
docker compose up -d

# 5. (optional) Integration tests, against that local ArangoDB only: a
#    non-local ARANGO_URL (e.g. from a .env) is refused unless
#    ARANGO_TEST_ALLOW_REMOTE=1
RUN_INTEGRATION=1 uv run pytest -q -m integration

# 5. (optional) Translate a query from the CLI
uv run arango-sparql-py translate \
  --sparql 'PREFIX : <http://ex.org/> SELECT ?s WHERE { ?s a :Person }' \
  --ontology-file my-schema.ttl
```

> **Dedicated database (no manual setup).** Point `ARANGO_DB` at a
> database other than `_system` (e.g. `ARANGO_DB=sparql-to-aql` in
> `.env`) and the service **auto-creates it on first boot** when it is
> missing — `main.py` runs a best-effort provisioning step outside
> public mode. ArangoDB never auto-creates databases, so this saves a
> manual step before `/connect` works. To provision out-of-band instead,
> run `uv run python scripts/ensure_database.py`; to disable the boot
> step, set `ARANGO_SPARQL_SKIP_DB_BOOTSTRAP=1`.

> **Under a governance control plane.** Set `CONTROL_PLANE_URL` and the
> service becomes a project's query tool: the project's signed context is
> the only credential, a session opens on the project's database with the
> deployment's own credential, and routes that connect elsewhere or run
> caller-written AQL are refused. Build the image with
> `control-plane.Dockerfile` (it needs the control plane's Python SDK as a
> named build context). See [PRD §8.7](docs/architecture/PRD.md#87-under-a-governance-control-plane).

> Sibling-repo work (porting from the legacy Foxx service or mirroring
> patterns from `arango-cypher-py`)? Run `./scripts/setup_references.sh`
> to symlink them under `references/`. Symlinks are gitignored — they
> only matter for AI agents and porting work.

## HTTP surface (current)

| Method | Path | Purpose |
| --- | --- | --- |
| `GET`  | `/health` | Liveness probe |
| `POST` | `/connect` / `/disconnect` | Open / close an ArangoDB session |
| `POST` | `/translate` | SPARQL → AQL (no DB access) |
| `POST` | `/validate` | SPARQL parse-only validation |
| `POST` | `/execute` | SPARQL → AQL → ArangoDB → bindings |
| `POST` | `/execute-aql` | Pass-through AQL with the same session |
| `POST` | `/explain` / `/profile` | AQL execution plan / per-stage profile |
| `POST` | `/nl-translate`, `/nl-explain`, `/nl-execute` | LLM-backed NL → SPARQL → AQL |

The W3C-conformant `GET/POST /sparql` Protocol endpoint
(content-negotiated SPARQL Results JSON / XML / CSV / TSV, plus
Service Description) is the headline v1.0 deliverable — see PRD §5.2.

## SPARQL conformance

W3C SPARQL 1.1 DAWG translation-only coverage at `main`:

| Category | Coverage |
| --- | --- |
| Syntax (positive) | **100.0 %** (63/63) |
| Syntax (negative) | **67.4 %** (29/43, the 14 xfails are rdflib parser-permissiveness gaps, not translator gaps) |
| Query evaluation | **95.7 %** (242/253) — see [`COVERAGE_REPORT.md`](tests/w3c/COVERAGE_REPORT.md) for the full XFAIL ledger driving the visitor priority queue |

Visitors shipped today: `BGP`, `Filter`, `Project`, `Distinct`, `Slice`,
`OrderBy`, `AskQuery`, `Extend` (BIND), `LeftJoin` (OPTIONAL),
`AggregateJoin` (`COUNT` / `SUM` / `AVG` / `MIN` / `MAX` /
`GROUP_CONCAT` + `GROUP BY` + `HAVING`), `Join` (multi-subject BGPs
lowered to AQL equality FILTERs), and property-path expansion
(`SequencePath`, `InvPath`, `AlternativePath`, bounded `:p+` /
`:p*` / `:p?` via UNION desugaring with nested-modifier collapsing
e.g. `((:p)*)*` → `:p*`, and forward-only `NegatedPath` via
ATTRIBUTES fan-out with NOT IN guard). FILTER expressions also
cover `IN` / `NOT IN` (including the empty-set form) and XSD
constructor casts (`xsd:double` / `xsd:integer` / `xsd:string` / …).

## Repository layout

```
arango_sparql/
  api.py                # public translate() entry point
  errors.py             # typed SparqlError hierarchy (E_SPARQL_*, E_SCHEMA_RESOLVE, …)
  cli.py                # typer CLI
  _env.py               # central env-var resolver (ARANGO_PASSWORD, …)
  translate/
    parser.py           # rdflib parser wrapper
    visitor.py          # one visit_<NodeType> per Algebra op
    builder.py          # parameterized AQL query builder
    resolver.py         # OWL → ArangoDB collection / property resolver
  service/
    app.py              # FastAPI app + CORS + public-mode guardrails
    models.py           # pydantic request/response models + _MAX_* limits
    security.py         # sessions, rate limit, SSRF guard, error redaction
    routes/
      health.py
      connect.py        # /connect, /disconnect, /connect/defaults
      sparql.py         # /translate, /validate, /execute*, /explain, /profile
      nl.py             # /nl-translate, /nl-explain, /nl-execute
  nl2sparql/
    pipeline.py         # NL → SPARQL pipeline (LLM + repair loop + cost)
    prompt.py, client.py, repair.py, cost.py, models.py
tests/
  translate/            # parser unit tests + YAML-driven goldens
  cross/                # pyoxigraph cross-validation (PG + multi-model PG/LPG/hybrid/RPT + edge-collection traversal + MINUS/EXISTS + MINUS-with-OPTIONAL + RPT cross-subject OPTIONAL)
  schema/               # MappingBundle fixtures + §13.3 per-model contracts
  w3c/                  # W3C SPARQL 1.1 DAWG runner + COVERAGE_REPORT.md
  nl2sparql/eval/       # NL eval harness (slow, gated on RUN_EVAL=1)
  helpers/oxi.py        # pyoxigraph fixtures + binding comparison
  helpers/aql_interp.py # shared in-memory AQL-subset interpreter
ui/                     # Vite + React + TS frontend
references/             # symlinks to sibling repos (gitignored, recreated locally)
docs/architecture/      # PRD.md = single source of truth (spec + ADRs + vision); vision.md & decisions/ are stubs
.cursor/
  rules/                # scoped Cursor rules (000-context, 100-backend, 200-testing, …)
  skills/sparql-to-aql/ # SPARQL→AQL porting recipe (read this first)
```

## Documentation

- **[`docs/architecture/PRD.md`](docs/architecture/PRD.md)** — the
  **single source of truth**: HTTP surface, supported physical schema
  shapes (document, hybrid multi-class, edge traversal, named graphs),
  NL pipeline, conformance targets, release milestones, plus the folded-in
  decision records (Appendix B) and the inception narrative (Appendix C).
- **[`docs/architecture/implementation_plan.md`](docs/architecture/implementation_plan.md)**
  — the living work-tracking plan (WP status), including the chat-first
  UI shell migration (WP-UI-SHELL). The PRD is the spec; this is status.
- **[`docs/architecture/vision.md`](docs/architecture/vision.md)** and
  **[`docs/architecture/decisions/`](docs/architecture/decisions/)** —
  redirect stubs that point into the PRD appendices (kept so old links
  resolve); do not add content there.
- **[`CONTRIBUTING.md`](CONTRIBUTING.md)** — dev setup, test gates,
  porting recipe pointer.
- **[`SECURITY.md`](SECURITY.md)** — vulnerability reporting flow.
- **[`AGENTS.md`](AGENTS.md)** — shared contract for AI coding agents
  working on this repo (Cursor, Claude Code, Codex CLI, Copilot
  Workspace).

## License

[MIT](LICENSE). Copyright (c) 2026 Arthur Keen.
