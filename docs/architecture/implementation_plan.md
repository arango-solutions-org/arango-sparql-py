# Implementation plan — `arango-sparql-py`

This is the **living work-tracking** document. It complements the PRD:

- [`PRD.md`](PRD.md) is the **spec** (what "done" means, normative
  contracts, acceptance criteria) and the milestone **roadmap** (§14).
- This file is the **plan** (discrete work packages, their status, the
  files they touch, and their test signal). When the two disagree about
  intent, the PRD wins; when they disagree about *status*, this file is
  the source of truth.

Work packages use a stable `WP-<AREA>-<N|NAME>` id so they can be
referenced from commits and the PRD. Statuses: **Done**, **In progress**,
**Planned**, **Deferred**.

Numbering mirrors the sister project `arango-cypher-py`'s
`docs/implementation_plan.md` so cross-repo navigation is trivial. The
UI shell work packages (WP-UI-SHELL Phase 0–4) are the SPARQL-side port
of that project's chat-first "Query Workbench Shell"
(`references/arango-cypher-py/docs/query_workbench_shell_spec.md`).

---

## Tracking table

| WP | Area | Summary | PRD ref | Status |
| --- | --- | --- | --- | --- |
| WP-BE-OPT | Backend | Cross-subject `OPTIONAL` (RPT-native, ADR-0002 P1-A) + OPTIONAL-rebind-in-MINUS (P2) | App. B.2 | ✅ Done |
| WP-BE-DBBOOT | Backend | Dedicated-database auto-provision on boot | §A.4 | ✅ Done |
| WP-BE-MERGE | Backend | Merge analyzer-discovered physical mapping into inline ontology | §6.3.2 | ✅ Done |
| WP-BE-GRAPHSCOPE | Backend | Named-graph down-select: `/graphs`, `/session/graph`, scoped cache key | §6.8 | ✅ Done |
| WP-BE-LLMKEY | Backend | LLM provider resolution w/ standard-key fallback | §7.6 | ✅ Done |
| WP-BE-400 | Backend | `AQLQueryExecuteError` → `400` (not `500`) | §5.1 | ✅ Done |
| WP-BE-EVALGATE | Backend | Evaluation-correctness gate (CDF M5 WP-C1): variable-predicate `?p`→IRI mapping (`SchemaResolver.attribute_uri_map`), AQL-runtime fixes (`NOT_NULL` for `COALESCE`, `STRENDS` lowering — no `ENDS_WITH` in AQL, `GROUP_CONCAT` via `PUSH` + projection-time `CONCAT_SEPARATOR`, `xsd:decimal` bind coercion), `EXPECTED_LIVE_PASSES` hard gate in the W3C live harness, per-PR `integration` CI job + nightly W3C workflow | §6.6, §13.5 | ✅ Done |
| WP-BE-PARTITION | Backend | Federation entry point (CDF M5 WP-C2): `translate_partition` — sub-SELECT partition wire shape, subject-IRI canonical-key columns via visitor `extra_projection`, seed-binding pushdown as a trailing `VALUES` clause (rdflib terms + SPARQL-JSON binding dicts, injection-safe serialization), `PartitionProvenance` per-leg stamp (`as_of` executor-owned). CDF confirmed (2026-07-16) the seed pushdown is the CC-11/FR-13 bind-join. **Not adopted (checked 2026-10-22):** CDF calls `api.translate` per leg and writes its bind-join `VALUES` itself, engine-neutrally, so `translate_partition` has no consumer today (see the proposal's status). Contract + decisions in `docs/architecture/proposals/federation-entry-point.md` | proposals/federation-entry-point.md | ✅ Done (no consumer) |
| WP-LDBC-DATA | Testing | LDBC SNB Interactive v1 Turtle fetch (SHA-256 pinned), normalisation (`directKnows`, skolemisation) and RPT/LPG/PG loader with cross-layout count parity (per class and per relationship) and live-mapping diff | §13.6 | ✅ Done (`tests/ldbc/{datasets,normalize,loader}.py`, `test_ldbc_load_live.py`; dedicated server `docker-compose.ldbc.yml`) |
| WP-LDBC-CORPUS | Testing + Backend | 21 verbatim GraphDB reads + labelled variants, three mapping fixtures, outcome ledger + per-PR ratchet, fail-closed guards (`dateTime ± duration`, nested `SERVICE`) | §13.6 | ✅ Corpus, fixtures, ledger and ratchet done; nested `SERVICE` reports `E_SPARQL_UNSUPPORTED` (rdflib's unbounded recursion, RDFLib/rdflib#3082, fixed by #3608, unreleased) |
| WP-LDBC-ORACLE | Testing | `pyoxigraph` + LDBC validation-parameter oracles; live execution tier on three layouts | §13.6 | 🟡 pyoxigraph oracle and live tier done; LDBC validation of the short reads through WP-LDBC-REPLAY |
| WP-LDBC-PARAMS | Testing | Official SF0.1 substitution parameters → deterministic bindings (+ one empty-result binding per op) | §13.6 | ✅ Done (`tests/ldbc/params.py`, `params/sf0.1.yml`) |
| WP-LDBC-EDGENODE | Backend (+ upstream analyzer) | Reified relationship node ↔ edge document mapping; symmetric `directKnows` over `knows` | §13.6 | ✅ Symmetric `directKnows` done (`c068771`); reified relationships done (D8: `phys:reifiedEdge` entry + `phys:edgeTargetOf` exits, the node is the edge; IS3, IC1, IC5, IC7, IC11 translate on PG and LPG) |
| WP-LDBC-POLY | Backend (+ upstream query-core) | Polymorphic domains / multi-typed nodes on PG (Post ∪ Comment, Place, Organisation) | §13.6 | 🟡 Single-class domains, LPG label unions, reverse and symmetric traversal done (`c068771`); PG multi-collection domain unions done (D9: `_open_class_union`; IS4, IS5, IC3, IC4 match the oracle on PG). Supertype and label-array classes: arango-query-core's `UNION`/`MULTI_LABEL` and the analyzer's `LABELS` (they used to read as a plain collection, returning wrong rows); unknown styles refused. Value joins are index-friendly and pushed into union arms (IS2, IC8 on PG and IC3 on LPG now match the oracle). Open: IC9 (PG memory cap), IC10 (timeout) |
| WP-LDBC-OPT | Backend | Cross-subject `OPTIONAL` on PG/LPG; `OPTIONAL` bodies with `BIND`/`EXISTS` | §13.6, App. B.2 | ✅ Done (`3327f74`, `6a070b9`: OPTIONAL as a correlated subquery) |
| WP-LDBC-PATHS | Backend | Property paths over RPT subjects; `*`/`+` over edge collections at LDBC depth | §13.6, §6.6 | 🟡 Paths on RPT in a pure triple store (`e65b072`), zero-hop from values, shared sigil counters (`9c6af74`); `rdf:type` with tag-class nodes in PG/LPG open |
| WP-LDBC-TEMPORAL | Backend | `xsd:dateTime ± xsd:duration` → `DATE_ADD`; dateTime differences; canonical timestamp encoding | §13.6 | 🟡 `dateTime ± duration` done (`f4bf7a2`); dateTime differences (IC7) pending |
| WP-LDBC-RPT-ROUTING | Backend | Untyped and constant subjects read the triples table in a pure triple store; paths compose on RPT; constant type-pattern subjects filter on their IRI (silent-wrong fix) | §13.6, §6.6 | ✅ Done (`e65b072`, `9448c37`) |
| WP-LDBC-REPLAY | Testing | Replay the LDBC update stream on the oracle so the published validation answers apply (they interleave 18,626 updates with the reads) | §13.6 | 🟡 Increment 1 done on the oracle (`tests/ldbc/replay.py`): inserts IU1–IU8 written as the Turtle models them; 2026-10-30, SF0.1, 50 each of IS1–IS5 and IS7 agree with LDBC (300/300). IS6 is left out by default (pyoxigraph does not answer it in 10 min). Open: complex reads, then the inserts on the ArangoDB layouts |
| WP-LDBC-BENCH | Testing | Performance runner + generated `docs/ldbc_results.md` (report-only, fair-use disclaimer) | §13.6, §9.4 | 🟡 Runner and report done (`bench.py`, `report.py`); nightly SF0.1 workflow added (`.github/workflows/ldbc-nightly.yml`). Its first run (2026-10-28) failed: LDBC's server answered the runner with HTTP 403. Archives are now cached by their pins and can come from a digest-checked mirror (`LDBC_DATASET_MIRROR`); a mirror is still to be provisioned |
| WP-LDBC-OPTIMIZE | Backend | Plan-digest-driven optimisation loop per layout | §13.6 | 🟡 Join order done (`4a0455b`); index-friendly value joins (#97); backward walks from value-bound objects and over reified relationships (IC11 LPG now matches; IC5 PG/LPG run, the oracle times out). Open: IC7, IC10 (all layouts), IC9 PG, IC5/IC8/IC12 RDF |
| WP-UI-1 | UI | NL "Ask" bar + provenance/telemetry | §10.12 | ✅ Done |
| WP-UI-2 | UI | Schema-derived / optional-LLM query suggestions (`/nl-samples`) | §7.5, §10.12 | ✅ Done |
| WP-UI-3 | UI | Graph selector (named-graph scope pill) | §10.13 | ✅ Done |
| WP-UI-4 | UI | Ontology/mapping panel (Turtle + graph) + OWL roundtrip | §10.4 | ✅ Done |
| WP-UI-5 | UI | Schema introspect on connect + warning banner + refresh | §10.6 | ✅ Done |
| WP-UI-6 | UI | Results table/JSON/graph + literal-collapse toggle | §10.5 | ✅ Done |
| WP-UI-7 | UI | Query history + sample queries | §10.7 | ✅ Done |
| WP-UI-PROXY | UI | Dev vite proxy for `/mapping` + `/sample-queries` | §A.9 | ✅ Done |
| WP-UI-SHELL | UI | Chat-first workbench shell (Phase 0–4) | §10.0 | ✅ Done (Phase 0–4) |
| WP-UI-CAT | UI | Schema-catalog readiness UX (pending/analyzing) | §10.17 | ⛔ Blocked (needs backend async introspect) |
| WP-UI-GRAPH | UI | Schema-graph scalability (bundling/search/weight/expand) | §10.18 | ✅ Done |
| WP-UI-EDITOR | UI | SPARQL completion + hover + PrefixManager + explain/profile keymap | §10.2 | ✅ Done |
| WP-UI-AQL | UI | AQL edit-and-rerun wiring + `mapping` prop for completion | §10.3 | ✅ Done |
| WP-EDITOR-CORE | UI + Backend | Shared SPARQL editor core (workbench + CDF) + editor vocabulary v1 + workbench §10.2 class-aware completion + Format (#68) | §10.2, §10.2.1 | 🟡 Built and tested here; CDF adoption waits on a release |
| WP-UI-EXPLAIN | UI | Explain/Profile result tabs + client wiring | §10.5 | ✅ Done |
| WP-UI-PALETTE | UI | Command palette (`Mod-K`) | §10.7 | ✅ Done |
| WP-UI-TENANT | UI | Mount `TenantSelector` when multitenancy detected | §10.6 | ⛔ Blocked (no tenant-catalogue / `/session/tenant` route) |
| WP-UI-CORR | UI | Cross-pane SPARQL↔AQL correspondence wired via translator source map | §10.2 | ⛔ Blocked (translator emits no source map) |
| WP-UI-A11Y | UI | a11y + i18n + Playwright + Lighthouse budgets | §10.10, §10.11 | 🟡 Partial (app-code a11y + i18n scaffold done; Playwright/Lighthouse CI deferred) |
| WP-UI-THEME | UI | Light theme | §10.8 | ✅ Done |

---

## UI shell migration — WP-UI-SHELL (the chat-first redesign)

Adopts the sister project's progressive-disclosure shell (§10.0) as the
canonical layout. Ships in phases so the workbench keeps working at every
checkpoint (per `incremental-over-atomic`). Each phase lands with vitest
coverage for the new pure logic and a green `cd ui && npm run build`.

### Phase 0 — `SettingsMenu` (gear consolidation) ✅ Done

- **Status:** shipped. Header reduced to title + connection + graph +
  refresh-schema + gear; Samples/History/Outline/Ontology + open-on-error
  moved into the gear. SPARQL deviation: NL-mode and auto-translate/
  auto-run toggles omitted (single NL path; Send covers translate+run).
- **New:** `ui/src/components/SettingsMenu.tsx` — gear popover.
- **Move into it:** Samples, History, Outline, Ontology toggle, and the
  auto-translate / auto-run / NL-mode / open-inspector-on-error toggles
  (today these are header buttons / app-local state in `App.tsx`).
- **Result:** header reduced to title + `ConnectionDialog` +
  `GraphSelector` (+ `TenantSelector` when mounted) + gear.
- **Verify:** reducer/toggle unit tests; header renders only the four
  allowed controls.

### Phase 1 — `ChatComposer` + `utils/pipeline.ts` (L0) ✅ Done

- **Status:** shipped. `handleSend` runs `planSend` (NL generate → run
  when connected); `runNL` syncs `sparqlRef` so a chained Send → Run
  reads the fresh query. Status strip + disconnected chip wired. 39 UI
  tests green (5 new in `pipeline.test.ts`).
- **New:** `ui/src/components/ChatComposer.tsx` (§10.14) and
  `ui/src/utils/pipeline.ts` exporting `planSend(connected)` →
  `{ translate, run }`, plus `currentStage` / `stageLabel` / `isBusy`
  helpers (port + adapt SPARQL stage labels:
  "Generating SPARQL…" → "Transpiling to AQL…" → "Running…").
- **Wire:** Enter = Send full pipeline; Shift+Enter = newline; cancel
  while in flight; status strip (`role="status"`); disconnected =
  translate-only + "Connect to run".
- **Verify:** `pipeline.test.ts` for `planSend` / stage helpers;
  reducer provenance already covered by `store.test.ts`.

### Phase 2 — `QueryInspector` (L1 drawer) ✅ Done

- **Status:** shipped with Translate/Run + per-pane collapse + drag
  resize/split; persists `qi_height`/`qi_split`/`qi_sparql_open`/
  `qi_aql_open`/`qi_auto_open_error`; auto-opens on error. Explain/
  Profile buttons landed later in WP-UI-EXPLAIN; AQL edit-and-rerun is
  tracked by WP-UI-AQL.
- **New:** `ui/src/components/QueryInspector.tsx` (§10.15) — collapsible
  bottom drawer hosting `SparqlEditor` + `AqlEditor` in a drag-resizable
  split; power actions (Translate / Run, plus Explain / Profile from
  WP-UI-EXPLAIN) relocated here from the header toolbar. Format + AQL
  edit-and-rerun remain in WP-UI-AQL.
- **Persist (session-only):** `qi_height`, `qi_split`, `qi_sparql_open`,
  `qi_aql_open`.
- **Auto-open on error** (toggle from Phase 0): translate error → SPARQL
  pane; execute error → AQL pane w/ line highlight.
- **Verify:** inspector open/close + persistence unit tests.

### Phase 3 — per-result affordance chips + lazy-mount ✅ Done

- **Status:** shipped. `ui/src/components/ResultAffordances.tsx` renders a
  chip bar above the results; `ui/src/utils/affordances.ts` holds the
  pure enable/active logic (`resultAffordances`) with tests (46 UI tests
  green). Chips: **View SPARQL** / **View AQL** (open the L1 inspector
  focused on a pane) and **Graph** (switch the results tab). Explain /
  Profile chips landed later in WP-UI-EXPLAIN.
- **Lazy-mount:** already satisfied — `ResultsPanel` mounts
  `CytoscapeGraph` only when the Graph tab is active (`GraphView`), and
  Table/JSON render conditionally. No eager mount of heavy tabs.

### Phase 4 — multi-turn transcript ✅ Done

- **Status:** shipped as an **opt-in** panel so the single-active-query
  default is preserved. Toggle via the gear menu (**Panels →
  Conversation**), persisted to `localStorage["show_transcript"]`.
- **Model:** pure `ui/src/utils/transcript.ts` (`TranscriptTurn`,
  `appendTurn` with a 50-turn cap, `turnStatusLabel`) — 4 tests. Store
  gains a session-scoped `transcript: TranscriptTurn[]` with
  `ADD_TRANSCRIPT_TURN` / `CLEAR_TRANSCRIPT` (not persisted across reloads,
  matching a conversation's session scope).
- **Capture:** every Send appends a turn (question → generated SPARQL +
  ok/failed status), recorded regardless of panel visibility so toggling
  it on reveals prior turns.
- **UI:** `Transcript.tsx` renders a compact scrollable list (newest at the
  bottom, auto-scrolled); clicking a turn reloads its SPARQL into the
  editor; empty state + Clear action. Failed turns are non-clickable with a
  red status dot.
- **Verify:** 122 UI tests green; build clean.

---

## Standalone UI work packages

### WP-UI-PROXY — dev proxy gap (quick win) ✅

`ui/vite.config.ts` did not proxy `/mapping` or `/sample-queries`, so
dev-mode OWL import/export and API sample queries 404'd. **Done:** both
prefixes added to `proxiedPaths`; `npm run build` green.

### WP-UI-CAT — schema-catalog readiness UX (§10.17)

> ⚠️ **Backend dependency — not a pure-UI WP.** `GET /schema/introspect`
> (`arango_sparql/service/routes/schema.py`) is **synchronous**: it blocks
> until the analyzer finishes and has no `status: "pending"` / async
> "analyzing" state. The PRD §10.17 `introspectSchemaUntilReady` poll has
> nothing to poll against yet. Doing this WP requires a **backend slice
> first** (async schema acquisition + a `pending`/`analyzing` status on
> the introspect response), then the UI poll. A lighter UI-only slice is
> possible (show a non-blocking "Analyzing schema…" banner with elapsed
> time + cancel around the synchronous call) but does not deliver the
> spec'd polling contract. **Decision needed** before starting.

Planned shape once the backend supports it: add `schemaPending` /
`schemaAnalyzing` to the store, poll `introspectSchemaUntilReady` on
connect / graph-scope change, and render an amber "Schema is being
analyzed" banner with **Check again** / **Analyze now** rather than an
indefinite spinner. **Verify:** reducer tests for the pending lifecycle.

### WP-UI-GRAPH — schema-graph scalability (§10.18) ✅ Done

- **Status:** shipped. All four §10.18 behaviours land on top of a pure,
  render-agnostic model in `ui/src/utils/schemaGraph.ts`
  (`buildSchemaModel` / `edgeWidth` / `bundleLabel` / `matchSchema` /
  `neighborsOf` / `extractRelationshipCounts`), so the scaling logic is
  unit-tested without a DOM and `CytoscapeSchemaGraph` stays thin.
- **Bundling:** object properties sharing a (domain, range) class pair
  collapse into one directed arc; the arc label is `pred +N` and clicking
  it opens a side panel listing the member predicates (with counts when
  available) — the "expand members" affordance. Datatype/annotation
  properties stay a per-class property bag (`•N` node badge), never nodes
  (RDF literal-collapse rule).
- **Weighting:** `edgeWidth` log-scales arc stroke by summed instance
  volume relative to the busiest arc. Counts come from a **best-effort**
  `GET /schema/statistics` fetch (new `getSchemaStatistics` client +
  `extractRelationshipCounts`, tolerant of the analyzer's free-form
  `dict[str, Any]` shape); heuristic bundles / 401s degrade to uniform
  width and the legend says "uniform (no stats)".
- **Search / focus:** a canvas search box highlights class + predicate
  matches (amber ring) and dims everything outside the match's local
  neighbourhood; clicking a class focuses it + its neighbours. Highlight
  is applied by painting CM… er, cytoscape classes on the **stable**
  layout — never a relayout (§10.18 / rule 14 spirit).
- **Legend:** bottom-left overlay documents node = class, `•N` = datatype
  props, arc = object property, `+N` = bundled predicates (click to
  expand), and whether thickness is instance volume or uniform.
- **Verify:** `schemaGraph.test.ts` (17 tests) pins bundling (incl.
  dangling-endpoint skip + datatype-bag routing), count summing, width
  scaling monotonicity, search match/focus/neighbour sets, and the
  defensive stats extractor. 107 UI tests green; build clean.
- **Audit follow-through:** wires `/schema/statistics`, one of the
  "unused backend surface" routes the client↔models audit flagged.

### WP-UI-EDITOR — SPARQL editor parity (§10.2) ✅ Done

- **Status:** shipped. New `ui/src/lang/sparqlComplete.ts` holds all the
  editor intelligence as **pure, CM-agnostic** helpers (type-only
  `@codemirror/autocomplete` imports) so it runs under the node test env:
  `parsePrefixes` / `usedPrefixes` / `missingPrefixes` /
  `resolvableMissingDeclarations` (well-known vs. unknown split),
  `expandCurie`, `extractVars`, `sparqlCompletionSource`, and
  `sparqlHoverInfo`. `SparqlEditor` wraps only the CM glue
  (`autocompletion({override})`, `hoverTooltip`, keymap).
- **Completion:** keywords + built-in functions in fresh position;
  `?`/`$` variables already in the doc; declared **+ well-known** prefix
  names (`p:`); and, after `prefix:`, class/predicate local names from the
  live schema. Schema is fed via `setSparqlSchemaContext` from an `App`
  effect that derives class names from `physicalMapping.entities` and
  predicate names from `relationships` + all property keys (reuses the
  camelCase-safe `physicalMappingOf` from WP-UI-AQL).
- **Hover:** CURIE under the cursor expands to its full IRI (document
  prefixes override well-known), rendered in a small monospace tooltip.
- **PrefixManager:** `PrefixManager.tsx` popover in the SPARQL pane header
  — badge counts used-but-undeclared prefixes, **Add all** inserts the
  resolvable ones (prepended so they precede the body), lists unknown
  ones, and offers a browse-and-add list of any undeclared well-known
  prefix.
- **Keymap:** `Mod-Shift-E` (explain) / `Mod-Shift-P` (profile) added to
  the editor, wired to `App.handleExplain` / `handleProfile`. `Mod-K`
  (palette) shipped in WP-UI-PALETTE.
- **Verify:** `sparqlComplete.test.ts` (18 tests) pins prefix parsing,
  used/missing detection (incl. IRI/string false-positives), CURIE
  expansion, var extraction, and hover. 90 UI tests green; build clean.

### WP-EDITOR-CORE — shared SPARQL editor core (§10.2.1, issue #68) 🟡 Built here; CDF adoption pending a release

- **Why:** the workbench and the contextual-data-fabric (CDF) demo each had
  their own SPARQL suggestion rules, and they disagreed. The workbench offered
  every class and property after any `prefix:`, which fell short of §10.2
  (WP-UI-EDITOR was marked done without it), and it built its list from the
  physical mapping while the NL prompt reads the conceptual schema. CDF's
  editor had the class-aware rules, but locked inside its textarea shell.
- **What landed:** `arango_sparql/editor/sparql_editor_core.js` (the rules, in
  one plain-script file shipped in the wheel); `arango_sparql.editor`
  (`EditorVocabV1`, `editor_vocab()`, `editor_core_js()`);
  `arango_sparql.schema.vocabulary` (the per-class derivation now shared by
  `build_schema_summary` and `editor_vocab`); `editor_vocab` on
  `/schema/introspect`; the workbench's completion re-pointed at the core
  (`sparqlComplete.ts` is CodeMirror glue now) and a Format action.
- **Moved, with these deliberate changes** (everything else is CDF's 2026-08-10
  code or the workbench's WP-UI-EDITOR helpers as they were):
  - concept prefixes are configurable: every prefix the query binds to the
    vocabulary's base, plus the editor's default (`c` for CDF, `:` for the
    workbench) unless the query binds it to another namespace;
  - a bare word after `a` filters the classes by what is typed (CDF listed
    all of them);
  - variable completion leaves out the variable being typed, and accepts `$`;
  - a bare word also offers built-in functions and prefix names, so CDF gains
    them;
  - the tokenizer only starts a prefixed concept at a name boundary (CDF
    highlighted the `c:Name` inside `abc:Name`);
  - bug fixes found while moving: `classifyConcept("constructor")` crashed on
    `Object.prototype`; the formatter collapsed spaces inside `'single'` and
    `"""long"""` strings, and joined a `#` comment to the lines after it,
    commenting them out. It now protects those strings and leaves a query with
    a comment unchanged.
  - from the #69 review: one escape-aware lexer (comments, IRIs, every string
    form, PN_LOCAL escapes such as `:O\'Brien`) now backs the formatter, the
    tokenizer, prefix/variable detection and subject lookup. So the formatter
    never touches a literal, and never joins `FILTER(?a<1)#c>` (rdflib reads
    that as less-than then a comment). A `.` in an IRI, a decimal or a string
    no longer ends the statement, and a FILTER/BIND before a triple no longer
    hides its subject. The subject's class is the last declared before the
    caret (`?s` and `$s` alike). Hyphenated names work end to end, nothing is
    offered inside a comment or a string, and `PREFIX : <` offers only the
    namespace;
  - `EditorVocabV1` rejects names the core cannot write (`prefix:name`) and a
    base that breaks out of `<…>`; a relationship whose domain has no entity
    is still offered on that class; the workbench rebases the vocabulary onto
    the ontology NL requests carry (the NL route reads it first);
  - `build_schema_summary` differs from before only on malformed bundles: a
    property listed twice is listed once, two entities with one name merge,
    and an explicit `fromEntity: null` reads "any class" instead of `:None`.
- **Verify:** `tests/editor/` (the core under node, including CDF's
  formatter tests and a regression test per review finding, plus the
  vocabulary contract and the prompt ⊆ editor invariant for classes and
  relationships); three `/schema/introspect` route tests; UI
  `sparqlEditorCore.test.ts` and new `sparqlComplete.test.ts` cases; `ruff`, `mypy` (now also over `arango_sparql/editor` and
  `schema/vocabulary.py`), `tsc -b`, `eslint` (no new warnings), `vite build`.
- **Remaining:** CDF drops its copy of the rules and embeds the core from its
  pinned install. That is a CDF pull request held as a draft until a release
  that includes this work. The design still needs PJ's review.

### WP-UI-AQL — AQL edit-and-rerun (§10.3) ✅ Done

- **Status:** shipped. `AqlEditor` gained `onRun` / `running` / `canRun`
  props and a **Run AQL** toolbar button that reads the live editor
  document and calls `executeAql` (`POST /execute-aql`), bypassing the
  SPARQL→AQL step. An **edited** badge + **Reset** appear once the doc
  diverges from the transpiled AQL, and a fresh transpile clears the
  edited state. `App.handleRunAql` reuses the `EXECUTE_*` channel.
- **Fixed a latent completion bug:** the wire mapping is camelCase
  (`physicalMapping`, per `translate/mapping.mapping_to_wire_dict`) but
  `AqlEditor` read snake_case `physical_mapping`, so schema-aware
  `var.property` completion silently got nothing. Extracted a pure
  `ui/src/utils/mappingWire.ts::physicalMappingOf` (camelCase + snake
  fallback), wired it into `AqlEditor`, and passed
  `mapping={state.schema.mapping}` from `App`.
- **Verify:** `mappingWire.test.ts` pins the camelCase read + fallback (6
  new tests; 60 UI tests green). Editor/run wiring is thin and rides the
  existing `EXECUTE_*` reducer coverage (no DOM/RTL harness in this repo).

### WP-UI-EXPLAIN — explain/profile in results (§10.5) ✅ Done

- **Status:** shipped. Fixed a latent client bug — `profileSparql` was
  POSTing to a non-existent `/aql-profile`; the real route is `/profile`
  (`arango_sparql/service/routes/sparql.py`). Both `ExplainResponse` /
  `ProfileResponse` types were realigned to the backend models
  (`SparqlExplainResponse` / `SparqlProfileResponse`) and `/profile`
  added to the vite proxy (dropping the dead `/aql-profile`,
  `/sparql-profile` entries).
- **Store:** `explaining` / `profiling` flags + `explainPlan` /
  `profileData` payloads; `EXPLAIN_*` / `PROFILE_*` reducer actions.
  `PROFILE_SUCCESS` also populates `results` (profile executes rows), and
  `DISCONNECT` drops both payloads. 54 UI tests green (8 new: 6 store +
  2 affordance).
- **Results tabs:** conditional **Explain** / **Profile** tabs in
  `ResultsPanel` — `ExplainView` renders the plan summary (est. cost/rows),
  a per-node relative-cost bar with a hotspot highlight, the optimizer
  rules, and a raw-JSON escape hatch; `ProfileView` renders per-stage
  timings (s → ms) with a slow-stage bar. Both read ArangoDB's native
  JSON defensively.
- **Actions + chips:** Explain / Profile buttons restored to the
  `QueryInspector` toolbar (gated on `isConnected`), and Explain / Profile
  affordance chips added (gated on `connected && sparqlPresent`).

### WP-UI-PALETTE — command palette (§10.7) ✅ Done

- **Status:** shipped. `Mod-K` (⌘K / Ctrl-K) opens a keyboard-first
  overlay indexing every workbench action; also reachable from the gear
  menu (`SettingsMenu` "Command palette" row) per `ui-architecture.mdc`
  rule 19 (accelerate, don't replace).
- **New:** `ui/src/components/CommandPalette.tsx` (modal + keys) and
  `ui/src/utils/commandPalette.ts` — pure `matchesQuery` / `filterCommands`
  (AND-of-terms substring match, title-prefix matches float to top) +
  `nextEnabledIndex` / `firstEnabledIndex` (enabled-aware, wrapping
  keyboard nav). 12 new tests (72 UI tests green).
- **Commands:** Query (Translate / Run / Explain / Profile, gated on
  connection + SPARQL), Panels (inspector / ontology / outline / samples /
  history), View (result tab table/json/graph), Schema (refresh). Disabled
  commands render greyed and are skipped by nav.

### WP-UI-TENANT — tenant selector (§10.6)

> ⚠️ **Backend dependency (client↔models audit).** Multitenancy today is
> purely a request-header concern (`X-Tenant-Id`, PRD §6.5.1) enforced in
> `sparql.py` / `mapping.py`. There is **no tenant-catalogue endpoint** to
> auto-detect tenancy and **no `/session/tenant`** route to bind a scope
> the way `/session/graph` does. `TenantSelector.tsx` exists but has
> nothing to populate it or bind against. Needs a backend slice (list
> tenants + bind session tenant) OR a scoped-down "type a tenant id,
> attach as header" variant that deviates from the §10.6 auto-detect
> spec. **Decision needed.**

Planned: mount `TenantSelector.tsx` when the backend reports a tenant
catalogue; bind via a session route mirroring `/session/graph`.

### WP-UI-CORR — cross-pane correspondence (§10.2)

> ⚠️ **Backend dependency (client↔models audit).** The transpiler emits
> **no source-map** metadata — `TranslateResponse` is `{ aql, bind_vars,
> warnings, schema_warnings, elapsed_ms }` with no SPARQL-span → AQL-span
> mapping. True cross-pane correspondence needs the translator to emit
> spans and `TranslateResponse` to carry them. The existing heuristic
> `utils/correspondenceMap.ts` is the only option until then. **Needs a
> backend slice first.**

Planned: replace the heuristic `utils/correspondenceMap.ts` with
translator source-map metadata and wire hover-sync between the SPARQL and
AQL panes in `App.tsx`.

### WP-UI-THEME — light theme (§10.8) ✅ Done

- **Status:** shipped. Dark stays the default; a system/dark/light toggle
  lives in the gear menu (**Appearance → Theme**).
- **Mechanism:** the app chrome uses literal Tailwind gray utilities, and
  Tailwind v4 compiles those to `var(--color-gray-NNN)`. So `index.css`
  retones the *entire* app by overriding an **inverted** gray ramp under
  `html.light` (gray-950 app-bg → light, gray-100 text → dark) — no
  per-component rewrite. CodeMirror follows the same switch: `theme.ts`
  now reads a `--cm-*` palette defined per-theme in `index.css`. Accent
  colours (indigo/emerald/…) are unchanged in both themes. The handful of
  literal `text-white` used as page text/inputs/headings were moved to
  `text-gray-100` so they invert; `text-white` on coloured buttons stays.
- **State:** `hooks/useTheme.ts` resolves mode → theme (honours
  `prefers-color-scheme` in system mode, live-updates on OS change),
  toggles `html.light`/`.dark` + `color-scheme`, persists to
  `localStorage["sparql_theme"]`. Pure `utils/theme.ts`
  (`resolveTheme` / `nextMode` / `parseThemeMode`) has 6 tests.
- **Reduced motion:** `@media (prefers-reduced-motion: reduce)` neutralises
  animations/transitions app-wide (covers §10.10's reduced-motion row).
- **Verify:** 113 UI tests green; build clean.

### WP-UI-A11Y — accessibility, i18n, and perf CI (§10.10, §10.11) 🟡 Partial

**Done (app-code a11y + i18n scaffold):**

- **i18n indirection:** `ui/src/i18n/en.ts` (message catalogue) + `t()`
  (`ui/src/i18n/index.ts`, `{name}` interpolation, key-fallback). No
  translations ship — the indirection exists so future locales need no
  refactor. Wired into the header + status announcements as a proof;
  broader migration is incremental. 5 tests in `i18n.test.ts`.
- **Advisory CI check:** `ui/scripts/check-i18n.mjs` (`npm run check:i18n`)
  reports remaining hardcoded JSX strings (81 across 22 files today).
  Advisory-only (`EXIT_ON_FIND=false`) until migration completes, then
  flip to the CI gate §10.10 specifies.
- **Screen-reader status:** a single `role="status" aria-live="polite"`
  region in `App` announces pipeline stage + errors.
- **aria-labels:** icon-only controls carry labels (audited — gear, chat
  send, palette, inspector/ontology hide, result actions already had
  them; added `Close` labels to the `×` buttons in `SampleQueries` and
  the schema-graph detail panel).
- **Reduced motion:** shipped in WP-UI-THEME
  (`@media (prefers-reduced-motion: reduce)`).

**Deferred (test/CI infra, needs a harness this repo doesn't have yet):**

- Playwright + axe-core suites (`a11y_keyboard`/`contrast`/`aria`/`forms`),
  `tests/playwright/perf_*`, `ui/lighthouse.json` + bundle-size gate. These
  require standing up Playwright + a CI perf runner; tracked as a
  follow-up rather than stubbed.

---

## LDBC SNB benchmark — WP-LDBC-* (§13.6) 🟡 In progress

The research, baseline probe, scope decisions (D1–D11), work-package detail,
sequencing, risks and open questions are in
[`ldbc_benchmark_plan.md`](ldbc_benchmark_plan.md). That file is the
authority for the WP-LDBC-* scope; the tracking table above carries status.

- **Phase 0** (harness + baseline, milestone M1): WP-LDBC-DATA,
  WP-LDBC-CORPUS, WP-LDBC-ORACLE, WP-LDBC-PARAMS. The only translator changes
  are fail-closed guards.
- **Phase 1** (correctness, milestone M2: every read except IC13/IC14 matches
  the oracle on RPT, LPG and PG): WP-LDBC-EDGENODE, WP-LDBC-POLY,
  WP-LDBC-OPT, WP-LDBC-PATHS, WP-LDBC-TEMPORAL.
- **Phase 2** (performance): WP-LDBC-BENCH (can start after M1),
  WP-LDBC-OPTIMIZE.
- **Phase 3** (optional): cross-transpiler comparison with
  `arango-cypher-py` on the same load; a hybrid layout.

The status emoji 📝 means *proposed*. Findings so far are in the plan's §3.3.

---

## client↔models audit (2026-07-05)

A pass comparing `ui/src/api/client.ts` against `service/models.py` +
every `@app` route. Findings:

- **Fixed (client bugs):** `ValidateResponse.ok` → `valid` (+ `warnings`);
  `SchemaInvalidateResponse.database` → `db_name` (+ `persistent_dropped`);
  stale "not implemented" comments for `/schema/owl` (live) and the
  top-of-file list (referenced a non-existent `/nl2sparql`). *(`/aql-profile`
  → `/profile` and the `physicalMapping` key were fixed in WP-UI-EXPLAIN /
  WP-UI-AQL.)*
- **Backend gaps (client ready, route missing):** `/sample-queries`
  (PRD §A.9; `SampleQueries.tsx` degrades to static samples — swallowed
  404); `/schema/introspect` has no async `pending` status (blocks
  WP-UI-CAT); no tenant catalogue / `/session/tenant` (blocks
  WP-UI-TENANT); transpiler emits no source map (blocks WP-UI-CORR).
- **Unused backend surface (opportunities, not bugs):** `/nl-explain`,
  `/nl-execute`, `/schema/properties`, `/schema/summary`, `/connections`
  have no client wrapper yet. *(`/schema/statistics` was wired by
  WP-UI-GRAPH for edge weighting.)*
- **Verified matching:** `/graphs`, `/session/graph`, `/translate`,
  `/execute`, `/execute-aql`, `/explain`, `/profile`, `/nl-translate`,
  `/nl-samples`, `/mapping/*`, `/schema/introspect|status|force-reacquire`,
  connect/health.

## Notes

- The object-centric `.cursor/rules/ui-architecture.mdc` (workspace
  canvas, context menus, never-collapse zones) describes a *different*
  product surface; the query workbench deliberately opts out of the
  never-collapse rule (§10.0). New UI work here follows
  `.cursor/rules/400-frontend-ui.mdc`.
- Keep parity with `references/arango-cypher-py/ui/` component-for-
  component where a SPARQL analogue exists; deviations (Turtle-centric
  ontology panel, RDF literal-collapse) are documented in PRD §10.
