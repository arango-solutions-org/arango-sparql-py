# Roadmap: arango-sparql-py

## Overview

This is a full v1 roadmap bootstrapped over a **mature repo**: the deterministic
SPARQL 1.1 → AQL transpiler, the W3C SPARQL 1.1 Protocol FastAPI service, and the UI
shell already ship (W3C DAWG query-eval coverage at 96.4%). Phases 1–3 are therefore
marked **Complete** (shipped pre-GSD) and are held as a **no-regression gate** rather
than re-planned. The active journey targets the user's actual goal — making the
NL→SPARQL layer's quality **measurable then improvable**: Phase 6 stands up the eval
harness + seed corpus + checked-in `baseline.json`, Phase 06.2 hardens that corpus and
captures a genuine live-model baseline, and Phase 7 adds **dense few-shot retrieval**
and proves an accuracy lift against that baseline. Phases 4, 5, and 8 close out
interop/perf verification, UI parity, and public release. Throughout, W3C query-eval
coverage must never drop below 96.4%.

## Phases

**Phase Numbering:**

- Integer phases (1, 2, 3): Planned milestone work
- Decimal phases (2.1, 2.2): Urgent insertions (marked with INSERTED)

Decimal phases appear between their surrounding integers in numeric order.

- [x] **Phase 1: Deterministic transpiler core** - SPARQL 1.1 → AQL across all physical models; W3C 96.4% (COMPLETE, pre-GSD)
- [x] **Phase 2: SPARQL 1.1 Protocol service + schema HTTP surface** - Conformant `/sparql` endpoint + 9 schema routes (COMPLETE, pre-GSD)
- [x] **Phase 3: Operational, security & privacy parity** - Session/CORS/SSRF/redaction/STRIDE/log-envelope/config-gate (COMPLETE, pre-GSD)
- [x] **Phase 4: Interoperability & performance verification** - Foxx roundtrip, third-party tools, ontoextract, perf SLOs (completed 2026-07-28)
- [ ] **Phase 5: UI workbench parity completion** - Playwright/a11y CI harness + 3 backend-blocked WPs
- [x] **Phase 6: NL→SPARQL eval harness + seed corpus** - Make NL quality measurable; check in `baseline.json` gate (FIRST ACTIVE) (completed 2026-07-15)
- [ ] **Phase 06.1: Re-point nl2sparql onto arango-query-core shared engine** - Behavior-preserving refactor onto the shared `NLQueryEngine` via a 5-seam `SparqlAdapter` (INSERTED)
- [x] **Phase 06.2: NL→SPARQL harder corpus + genuine live-model baseline** - Grow corpus to real difficulty + capture a live-model baseline so a few-shot lift is measurable (INSERTED) (NEXT ACTIVE) (completed 2026-07-21)
- [x] **Phase 7: NL→SPARQL dense few-shot retrieval** - Dense/embedding ≤3-shot index via the shared engine's few-shot seam; prove pass-rate lift over the live baseline (completed 2026-07-21)
- [x] **Phase 07.1: NL→SPARQL eval via public benchmarks** - Adopt public NL→SPARQL benchmark test sets (QALD-9-plus = powered capability gate; CK25 = corporate-domain anchor) + a small refusal supplement, reaching ~5–8pt MDE with real vetted questions; synthetic generation retired (INSERTED; pivoted via grill-me, former 07.2 folded in) (completed 2026-07-22)
- [x] **Phase 07.5: NL→SPARQL query-first synthetic few-shot bank** - Build-time per-ontology few-shot bank generated query-first from the TBox (compositional templates + data-binding + paraphrase), loaded via the BM25 few-shot seam so NL→SPARQL adapts to any new ontology; re-opens 07.1's synthetic fence in a query-first/few-shot-pool form; measured mechanistically + directionally on held-out CK25+QALD (INSERTED)
- [ ] **Phase 8: Public release readiness** - Public repo, CI matrix, license/docs/runbook, SBOM on v1.0 tag

## Phase Details

### Phase 1: Deterministic transpiler core

**Goal**: A correct, layout-agnostic SPARQL 1.1 → AQL transpiler covering every physical schema shape.
**Depends on**: Nothing (first phase)
**Requirements**: REQ-w3c-coverage, REQ-physical-model-coverage, REQ-hybrid-bgp-translation, REQ-schema-detection
**Success Criteria** (what must be TRUE):

  1. W3C DAWG query-eval coverage ≥ 96.4% (244/253 pass), regenerable via `analyze_coverage.py --write`
  2. Correct AQL emitted for PG / LPG / RPT / DOCUMENT + PG-LPG and PG-RPT hybrids + both edge styles
  3. One BGP spanning ≥ 2 physical models produces a single AQL query joined on subject URI (not split, not rejected)
  4. Both schema detectors ship; analyzer wins on `strategy="auto"` with zero false negatives on the fixture corpus

**Plans**: Shipped pre-GSD (no plans authored)
**Status**: COMPLETE — held as no-regression gate

### Phase 2: SPARQL 1.1 Protocol service + schema HTTP surface

**Goal**: A conformant W3C SPARQL 1.1 Protocol HTTP service with the full schema/mapping route surface.
**Depends on**: Phase 1
**Requirements**: REQ-sparql-protocol-endpoint, REQ-schema-http-parity
**Success Criteria** (what must be TRUE):

  1. `GET/POST /sparql` honours `Accept` for JSON/XML/CSV/TSV with RFC 9110 q-value parsing
  2. Empty `GET /sparql` returns the Service Description as `text/turtle`
  3. Documented error contract in force (405 on Update forms; 400/422/406/503/504/429/401 per §5.2)
  4. All 9 schema/mapping routes exist with documented response shapes matching `arango-cypher-py`

**Plans**: Shipped pre-GSD (no plans authored)
**Status**: COMPLETE — held as no-regression gate
**UI hint**: yes

### Phase 3: Operational, security & privacy parity

**Goal**: Production-grade operational, security, and privacy behaviour at parity with `arango-cypher-py`.
**Depends on**: Phase 2
**Requirements**: REQ-operational-parity, REQ-threat-model-mitigations, REQ-privacy-contract, REQ-config-appendix-normative
**Success Criteria** (what must be TRUE):

  1. Session / connect / public-mode / CORS / rate-limit / SSRF / redaction / startup-guard each have a passing parity test
  2. Every §8.6 STRIDE threat-matrix row has an asserting, CI-blocking security test
  3. No request/response bodies appear in logs; `ARANGO_SPARQL_LOG_JSON=1` emits the §9.5 envelope
  4. Adding an env var without updating Appendix A fails CI

**Plans**: Shipped pre-GSD (no plans authored)
**Status**: PARTIAL — runtime surfaces exist, but the PRD-required per-surface
parity tests, STRIDE test matrix, default JSON log contract, no-body logging
test, and configuration-appendix CI guard remain open.

### Phase 4: Interoperability & performance verification

**Goal**: Preserve archived Foxx translator semantics and prove compatibility
with third-party SPARQL tools, `arango-ontoextract`, and the performance budgets.
**Depends on**: Phase 3
**Requirements**: REQ-foxx-parity, REQ-thirdparty-tool-compat, REQ-ontoextract-integration, REQ-performance-slos
**Success Criteria** (what must be TRUE):

  1. ~~≥ 90% of translatable legacy Foxx fixtures pass a golden emitting semantically equivalent AQL (`test_foxx_roundtrip.py`, Docker-gated)~~ **STRUCK — REQ-foxx-parity retired via ADR-0003 (D-01/D-02); Foxx is deprecated.**
  2. Each §11.1 verified-compatible tool (Protégé, YASGUI, SPARQLWrapper, MS Ontology Playground) passes a smoke test (SELECT + ASK + Service Description)
  3. `arango-ontoextract` completes the Q7 roundtrip via `/mapping/export-owl` + `/mapping/import-owl` (Docker-gated, both services live)
  4. Every §9.4 performance budget row passes within ≤ 25% of its stated p95

> **Scope narrowed by 04-CONTEXT (D-01..D-09):** SC1 (Foxx parity) is retired via ADR-0003 — see plan 04-03; SC3 is reframed as our own-half `/mapping` OWL-roundtrip contract test (no live AOE); SC4 enforcement is tiered (3 CI-gated in-process rows + 8 report-only rows).

**Plans**: 8 plans (2 waves)

- Wave 1:
  - [x] 04-01-PLAN.md — Wave-0 foundation: perf marker + SPARQLWrapper dep, tests/perf scaffolding, vendored cosmic_coffee.rdf fixture, docs/howto anchor
  - [x] 04-02-PLAN.md — RDF/XML (+JSON-LD/N-Triples) format-dispatch production-code fix in owl.py + mapping.py (unblocks RDF/XML test rows)
  - [x] 04-03-PLAN.md — REQ-foxx-parity retirement: ADR-0003 + PRD/ROADMAP/REQUIREMENTS amendments
- Wave 2:
  - [x] 04-04-PLAN.md — AOE own-half roundtrip contract test (import/export isomorphism + ASK/SELECT via /sparql)
  - [x] 04-05-PLAN.md — Automated third-party smoke: SPARQLWrapper + Ontology Playground roundtrip
  - [x] 04-06-PLAN.md — CI-gated perf tier: /translate cold+warm, /execute overhead p95 gate + baseline.json
  - [x] 04-07-PLAN.md — Report-only perf tier: 8 advisory rows → LATENCY_REPORT.md (human-run)
  - [x] 04-08-PLAN.md — Documented-manual recipes: Protégé/YASGUI/rsparql/SPARQLWrapper/Playground + recorded transcript

**Status**: Planned

### Phase 5: UI workbench parity completion

**Goal**: Close the remaining UI parity gap so every workbench capability row is verified and the backend-blocked WPs unblock.
**Depends on**: Phase 4
**Requirements**: REQ-ui-parity
**Success Criteria** (what must be TRUE):

  1. Every §10.2/§10.3 capability-table row has a passing Playwright test (`ui/tests/playwright/parity.spec.ts`, CI-blocking)
  2. Playwright/axe/Lighthouse CI harness exists and runs (WP-UI-A11Y completed)
  3. Backend slices land to unblock WP-UI-CAT (async schema introspect + status), WP-UI-TENANT (tenant catalogue / `/session/tenant`), WP-UI-CORR (translator source-map metadata)

**Plans**: TBD
**Status**: Not started
**UI hint**: yes

### Phase 6: NL→SPARQL eval harness + seed corpus

**Goal**: Make NL→SPARQL translation quality measurable — implement the stubbed eval harness, author a seed corpus, and check in a baseline as the regression gate. This is the first active phase and the user's immediate goal.
**Depends on**: Phase 3 (transpiler + NL pipeline already ship; interop/UI phases are independent and can run in parallel)
**Requirements**: NL-EVAL-01, NL-EVAL-02
**Success Criteria** (what must be TRUE):

  1. `tests/nl2sparql/eval/runner.py::run()` and `write_report()` are implemented (no `NotImplementedError`) and run each corpus entry against each configured provider
  2. `corpus.yml` + `configs.yml` are authored; the eval marker runs green in CI with a `ScriptedProvider`
  3. The harness reports a numeric **NL→SPARQL pass-rate** (JSON + Markdown) — the PRIMARY quality metric now exists
  4. `baseline.json` is checked in and enforced as the regression gate
  5. W3C DAWG query-eval coverage remains ≥ 96.4% (no transpiler regression)

**Plans**: 3 plansPlans:
**Wave 1**

- [x] 06-01-PLAN.md — Author corpus.yml + configs.yml seed data (incl. deliberate near-miss) [NL-EVAL-02]

**Wave 2** *(blocked on Wave 1 completion)*

- [x] 06-02-PLAN.md — Implement runner.py (run/write_report/judge) + test_eval.py gate + baseline.json [NL-EVAL-01, NL-EVAL-02]

**Wave 3** *(blocked on Wave 2 completion)*

- [x] 06-03-PLAN.md — Add CI eval job + verify W3C coverage ≥ 96.4% no-regression guard [NL-EVAL-01]

**Status**: Planned — FIRST ACTIVE

### Phase 06.1: Re-point nl2sparql onto arango-query-core shared engine (INSERTED)

**Goal:** Re-point the `nl2sparql` adapter off its private generate→validate→repair loop and onto the shared `arango_query_core.nl.NLQueryEngine`, implemented as a `SparqlAdapter` satisfying the 5-seam `QueryLanguageAdapter` protocol. **Behavior-preserving refactor** — the prerequisite that makes engine-side SOTA (few-shot/dense retrieval, etc.) reachable from SPARQL and inherited by Cypher.
**Requirements**: None new — behavior-preserving; gated by the Phase 6 baseline (no NL-EVAL-01/02 regression).
**Depends on:** Phase 6 (needs the eval harness + baseline.json to prove behavior is preserved).
**Success Criteria** (what must be TRUE):

  1. `nl2sparql` exposes a `SparqlAdapter` implementing `arango_query_core.nl.seams.QueryLanguageAdapter` (all 5 seams: grammar prompt, few-shot index [None for now], validate=deterministic transpile, repair_hint, guardrails); `NlPipeline.run()` drives `NLQueryEngine` instead of its own `PromptBuilder`→`LLMClient`→`RepairLoop` loop
  2. `arango-query-core` is a real dependency (the `nl` extra pin in `pyproject.toml` resolves it; editable/PyPI per the 0.2.0 plan) and imports cleanly
  3. Scripted eval pass-rate is **UNCHANGED at 0.833 (5/6)** with identical per-case verdicts vs `baseline.json` — `RUN_EVAL=1 pytest -m eval` stays green
  4. `NLResult` is mapped back to `PipelineOutcome` preserving the public shape (`sparql`, `aql`, `bind_vars`, `warnings`, `latency_ms`, `repaired`) — re-translate the final query once to recover `aql`/`bind_vars` the `validate()` seam discards
  5. W3C DAWG query-eval coverage remains ≥ 96.4% (deterministic transpiler untouched); existing non-eval suite stays green
  6. The `/nl-explain` path and cost/audit (`LLMCallRecord`) behavior are preserved, or any deviation is explicitly documented

**Plans**: 3 plans
**Status**: Planned

Plans:
**Wave 1** *(parallel — no file overlap)*

- [ ] 06.1-01-PLAN.md — Formalize arango-query-core as a real dependency in the pyproject `nl` extra + clean-import guard
- [ ] 06.1-02-PLAN.md — Provider bridge (LLMClient→LLMProvider, per-call LLMCallRecord) + SparqlAdapter (5 seams); reproduces baseline verdicts

**Wave 2** *(blocked on Wave 1)*

- [ ] 06.1-03-PLAN.md — Re-point NlPipeline.run() onto NLQueryEngine + NLResult→PipelineOutcome mapping (re-translate for aql/bind_vars) + cost/audit doc + behavior-preservation gate

### Phase 06.2: NL→SPARQL harder corpus + genuine live-model baseline (INSERTED)

**Goal**: Make a few-shot lift *measurable* before building few-shot. Replace the 6 toy single-BGP scripted cases with a real-difficulty NL→SPARQL corpus (OPTIONAL, aggregation, property paths, multi-hop, and negative/unsupported cases, each with a gold SPARQL judged by canonical algebra), then capture a **genuine live-model baseline** by running the `openai` real-provider config against it. Today the corpus is scripted-only with 5/6 passing and a deliberate near-miss — there is no headroom for few-shot to demonstrably move, so Phase 7's "prove a lift" is unmeasurable without this. (This is the "real baseline" half of BRIEF §5.1; the re-point half already shipped in Phase 06.1.)
**Depends on**: Phase 06.1 (nl2sparql on the shared engine) + Phase 6 (the eval harness + judge)
**Requirements**: NL-EVAL-03, NL-EVAL-04
**Success Criteria** (what must be TRUE):

  1. `corpus.yml` grows beyond single-BGP toys to include OPTIONAL, aggregation, property-path, multi-hop, and negative/unsupported cases — each with a gold SPARQL and canonical-algebra judging (never string match)
  2. The corpus has genuine headroom: the scripted baseline pass-rate is meaningfully < 1.0 with room for a measurable few-shot lift (not a near-ceiling toy set)
  3. A **live-model baseline** is captured by running the `openai` config (`RUN_EVAL=1` + provider key) and checked in as a credentials-gated companion to `baseline.json`; **no secrets committed**; scripted stays the no-network CI default
  4. The harder corpus + live baseline are reproducible from documented steps; CI still runs key-free on the scripted config
  5. W3C DAWG query-eval coverage remains ≥ 96.4% (deterministic transpiler untouched)

**Plans**: 4 plans

Plans:
**Wave 1** *(parallel — no file overlap)*

- [x] 06.2-01-PLAN.md — Harness capabilities: CorpusCase/BaselineConfig load-time gate + inverted expect_refusal judge branch [NL-EVAL-03, NL-EVAL-04]
- [x] 06.2-02-PLAN.md — Author positive difficulty classes (OPTIONAL, aggregation, property-path, multi-hop) + ontology extension + transpilability guard [NL-EVAL-03]

**Wave 2** *(blocked on Wave 1)*

- [x] 06.2-03-PLAN.md — Author negative/unsupported expect_refusal cases + retain near-miss + regenerate scripted baseline.json + headroom-invariant test [NL-EVAL-03]

**Wave 3** *(blocked on Wave 2)*

- [x] 06.2-04-PLAN.md — Live-model baseline: reproducibility runbook + credentials-gated openai-gpt4o-mini companion (human-run sweep) + no-network structural test [NL-EVAL-04]

**Status**: Planned — NEXT ACTIVE

### Phase 7: NL→SPARQL dense few-shot retrieval

**Goal**: Wire the shared engine's few-shot seam (`arango_query_core.nl.FewShotIndex`, reached via the re-pointed `SparqlAdapter.few_shot_index()`) with **dense/embedding retrieval** (sentence-transformer index) over the curated corpus, and prove it lifts NL→SPARQL pass-rate against the Phase 06.2 **live-model** baseline. Dense retrieval is the SOTA-survey's #1 win (up to +21 F1, highest evidence-per-cost); BM25 is the fallback/ablation. (Engine-side change — Cypher inherits the retrieval upgrade.)
**Depends on**: Phase 06.2 (needs the harder corpus + genuine live-model baseline to prove a lift against) + Phase 06.1 (nl2sparql running on the shared engine so few-shot lands engine-side)
**Requirements**: NL-FEW-01, NL-FEW-02
**Success Criteria** (what must be TRUE):

  1. A dense/embedding few-shot retriever is loaded via `arango_query_core.nl.FewShotIndex` and returned by `SparqlAdapter.few_shot_index()` (≤ 3 shots per query, rule-300 budget); BM25 available as an ablation baseline
  2. Retrieved examples appear in the engine-built prompt's `## Examples` section (the `NLQueryEngine` few-shot path), not the standalone `PromptBuilder`
  3. A dense few-shot eval run shows a **positive pass-rate delta over the Phase 06.2 live-model baseline** via the Phase 6 harness — **ACTUAL RESULT**: the pre-registered gpt-4o-mini dense-vs-zero paired McNemar test returned a documented NULL (p=0.453), closed via the plan's own human-accepted-documented-null completion path, NOT a passed confirmatory test. A SECONDARY bm25-vs-zero comparison IS significant (p=0.031), and lexical BM25 outperformed dense embeddings in all 3 model tiers — see 07-04-SUMMARY.md for the full, honest result.
  4. W3C DAWG query-eval coverage remains ≥ 96.4% (no transpiler regression)

**Plans**: 4 plans
**Status**: Complete (2026-07-21) — NL-FEW-02 closed via the documented-null completion path (see 07-04-SUMMARY.md)

Plans:
**Wave 1** *(parallel — no file overlap)*

- [x] 07-01-PLAN.md — Engine-side DenseRetriever + mode= dispatch + .retriever property + memoized index factory [NL-FEW-01]
- [x] 07-02-PLAN.md — Curated disjoint few-shot bank (fewshot_bank.yml) + D-02 two-way disjointness gate [NL-FEW-01]

**Wave 2** *(blocked on Wave 1)*

- [x] 07-03-PLAN.md — Flip SparqlAdapter.few_shot_index() -> populated index (mode=auto) + NlPipeline few_shot_k=3 + SC2 engine-prompt gate [NL-FEW-01]

**Wave 3** *(blocked on Wave 2)*

- [x] 07-04-PLAN.md — 3-arm x 3-model lift sweep: temperature fix + configs/runner extension + D-06 guard + D-04 provenance + W3C non-regression [NL-FEW-02] — complete (7ce312e, b2aa008, f1c327e, 3136c17, ac19edc); the credentialed human's live sweep returned a documented null on the pre-registered confirmatory test, closed per the plan's human-accepted-documented-null path — see 07-04-SUMMARY.md

### Phase 07.6: NL→SPARQL relationship-path grounding (INSERTED)

**Goal:** Close the dominant CK25 NL→SPARQL failure mode — *right-entity, wrong-path*. Root-caused from the 07.5 composed-lever evaluation (entity grounding + few-shot ceiling = 16/49; every failure is valid, executable SPARQL — 0 malformed): **16 of 31 failures ground the named entity correctly but navigate the wrong predicate-path and execute to empty** (e.g. "manager of Data Services" — the model used `responsibleFor` off the Department instead of the inverse `memberOf`←Person→`hasManager`). Add an engine **seam-8** that, given the question's anchor class (from seam-6 entity grounding) and its target (from seam-7's token scorer), retrieves the *specific* shortest predicate path connecting them from a mechanically-built, **subclass-aware, inverse-edged** TBox class-connectivity graph, and injects it as a compact navigation hint — surfacing *one path between two anchored classes*, NOT a schema dump (the distraction that sank 07.4's full predicate grounding). Mechanical / TBox-only → transfers to CDF; Cypher inherits. Full design + Step-0 offline recall spike (GREEN: 12/16 → ~16/16 with D-9 subclass-aware nodes + D-10 self-revisit + inverse edges): `.planning/research/relationship-path-grounding-scope.md`.
**Requirements**: NL-ACC-03
**Depends on:** Phase 07.3 (seam-6 entity grounding → anchor class), Phase 07.4 (seam-7 `PredicateIndex` → domain/range substrate + token scorer), Phase 07.5 (composed-lever sweep + candidate-dump diagnostics harness)
**Plans:** 3/3 plans complete

Plans:
**Wave 1** *(tracer — mechanical lever proven offline, engine-first)*

- [x] 07.6-01-PLAN.md — TRACER: ClassPathIndex engine seam (subclass-aware+inverse+self-revisit+≤5 bounded shortest-path) + execution-diff 16-case gold fixture + R4 offline recall gate ≥14/16 [NL-ACC-03]

**Wave 2** *(blocked on 07.6-01 — the prompt seam)*

- [x] 07.6-02-PLAN.md — Engine Protocol seam + seam-8 composition (after entity/predicate blocks) + both SPARQL adapters (byte-identical, anchor=seam-6/target=seam-7 per D-02) + NlPipeline threading + 3rd parity test [NL-ACC-03]

**Wave 3** *(blocked on 07.6-02 — eval arm + promotion + credentialed sweep; human checkpoints)*

- [x] 07.6-03-PLAN.md — Additive `path_grounding:` config arm + scripted twin + `--dry-run` 49/49; D-01 engine promotion (two-remote push + pin bump, recall-green-gated); human-run credentialed CK25 paired sweep + baseline.json adopt/kill fold-in [NL-ACC-03]

### Phase 07.5: NL→SPARQL query-first synthetic few-shot bank (INSERTED)

**Goal:** At build time for any new ontology, auto-generate a **query-first** `(question, gold-SPARQL)` few-shot bank from the TBox, and load it through the existing BM25 `FewShotIndex` seam so NL→SPARQL specializes to any new ontology without hand-curation. Query-first construction (hand-written ontology-agnostic compositional SPARQL templates — lookup / value-object / category-filter / COUNT / top-N / OFFSET / negation / 2-hop — whose predicate/class slots fill from the 07.4 TBox walker; data-bound and execution-filtered to be non-empty; paired question templates + K LLM paraphrases for faithful, natural questions). This **consciously re-opens Phase 07.1's "synthetic generator out-of-scope" fence** in a narrower, safer form — query-first for a few-shot *pool*, not question-first for *eval* (dodging 07.1's oracle/external-validity reasons) — and follows 07.4's documented-null follow-up ("try selective/targeted surfacing, not a full dump"). Measured **mechanistically + directionally, not significance-gated** (the CK25 anchor is underpowered at n=49 and QALD's floor is an orthogonal entity-grounding bottleneck): shape-coverage of held-out failures + CK25 directional lift + QALD non-regression, overlap-audited, with the **same generator run across both CK25 and QALD** as the generalization signal. Full design: `.planning/research/query-first-synthetic-fewshot-bank-design.md`.
**Requirements**: NL-GEN-01 (query-first synthetic few-shot bank generation; per-ontology adaptation, directionally-graded).
**Depends on:** Phase 07.4 (TBox walker + predicate shape classification — reused for query construction), Phase 7 (few-shot seam / BM25 `FewShotIndex`).
**Plans:** 5/6 plans executed

Plans:
**Wave 1**

- [x] 07.5-01-PLAN.md — Walker signals (orderable + optional-relation) + ShapeTemplate catalog scaffold + promote verify_bank.py -> verify_generated_bank.py [NL-GEN-01]

**Wave 2**

- [x] 07.5-02-PLAN.md — Generator core (9 name-anchored templates + slot-fill + data-bind + execution non-empty filter + strict-extremum) -> CK25 bank + per-shape yield report (REQ-1/REQ-2) [NL-GEN-01]

**Wave 3**

- [x] 07.5-03-PLAN.md — K=3 paraphrases (OpenAICompatibleClient) + offline primary slot-preservation faithfulness guard (REQ-3) [NL-GEN-01]

**Wave 4**

- [x] 07.5-04-PLAN.md — Same generator on QALD TBox (structural-only, D-04) + generator_no_ontology_branch proof (REQ-5) [NL-GEN-01]

**Wave 5** *(human-run, credentialed)*

- [x] 07.5-05-PLAN.md — Shape+entity overlap audit + additive config arms + run_generated_sweep.py + human-run CK25/QALD adopt/kill sweep + baseline.json fold-in (REQ-4/REQ-6) [NL-GEN-01]

**Wave 6** *(CONDITIONAL — only if the SPEC adopt bar clears)*

- [x] 07.5-06-PLAN.md — Stage 2 engine promotion: pure construction core -> arango_query_core seam + pyproject.toml git-pin bump (D-01, OQ-2) [NL-GEN-01]

**Status**: Planned

### Phase 07.4: NL→SPARQL predicate/schema-convention grounding (INSERTED)

**Goal:** Extend the grounding seam (seam 6) from instance-entity grounding to predicate/schema-convention grounding: walk the OWL/RDFS TBox to surface schema predicates with label, domain, range, and a shape classification (value-object range → emit the join pattern; class-typed range → filter by a category-instance IRI), and inject them so the model binds to real predicates and follows schema conventions instead of inventing nonexistent classes or flat predicates. Language-agnostic seam so the Cypher sister repo inherits it. Prove a statistically-graded CK25 accuracy lift over the 07.3 entity-grounded baseline, targeting the 17 convention-bound still-failing cases (dominated by the price value-object and hasCategory product-typing patterns, both mechanically derivable from the TBox).
**Requirements**: NL-ACC-02 (predicate/schema-convention grounding lift, execution-graded).
**Depends on:** Phase 07.3 (entity grounding — seam 6, LabelIndex, grounded eval configs)
**Plans:** 6/5 plans complete

Plans:
**Wave 1**

- [x] 07.4-01-PLAN.md — Engine-side seam 7 in arango-query-core (GroundedPredicate/PredicateIndex + shared scorer + seam 7 Protocol + engine composition) + author NL-ACC-02 [NL-ACC-02]

**Wave 2** *(atomic: pin bump never precedes adapters — Pitfall 1)*

- [x] 07.4-02-PLAN.md — Seam 7 on BOTH SPARQL adapters + NlPipeline passthrough + PREDICATE_DUMP_THRESHOLD, then bump arango-query-core pin (both extras) + uv lock [NL-ACC-02]

**Wave 3**

- [x] 07.4-03-PLAN.md — Eval-only build_predicate_index() TBox walk + corrected 3-way shape rule + shape precision/purity tests (Price/ProductCategory/Manager/literal) [NL-ACC-02]

**Wave 4**

- [x] 07.4-04-PLAN.md — runner.py predicate_grounding read + build-once + D-05 query capture + configs.yml entries + predicate SC-gate/parity/recall guards [NL-ACC-02]

**Wave 5** *(human-run, credentialed)*

- [x] 07.4-05-PLAN.md — README §10 runbook + live CK25 hard-gate McNemar sweep + first live QALD directional run + baseline.json fold-in + close NL-ACC-02 [NL-ACC-02]

**Wave 6** *(gap-closure: CR-01 code-review fix + re-fold)*

- [x] 07.4-06-PLAN.md — Real PredicateIndex dump mode upstream (CR-01 fix, pin b669320) + both adapters wired + re-fold: credentialed human RE-RAN the live CK25 sweep on the fixed dump mode, overwrote the confounded 07.4-05 numbers with valid ones, re-closed NL-ACC-02 as a VALID documented-null [NL-ACC-02]

**Status**: Complete (2026-07-27) — NL-ACC-02 closed via the documented-null path (standalone predicate-alone 7/49 vs a fresh 12/49 entity-alone arm, p=0.1250; additive 10/49 vs the same arm, p=0.6875 — the phase's actual hard-gate test), re-confirmed valid after the 07.4-06 CR-01 dump-mode fix and live re-sweep superseded the original 07.4-05 confounded numbers; see 07.4-06-SUMMARY.md "Re-fold" section for the full before/after.

### Phase 07.3: NL to SPARQL entity/instance grounding (INSERTED)

**Goal:** Productionize **entity/instance grounding** as a language-agnostic seam in the
shared `arango-query-core` NL engine (so the sister Cypher project inherits it), and prove
a statistically significant NL→SPARQL accuracy lift on the CK25 corporate-domain anchor via
the existing execution-graded eval. The `07.2` live run confirmed the model cannot invent
opaque instance IRIs (e.g. `Ms. Brant` → `empl-Karen.Brant%40company.org`) from the
vocabulary alone; grounding retrieves candidate instance IRIs from the target data and
injects them into the prompt so the model can bind to real entities.

**Requirements**: NL-ACC-01 (NL→SPARQL entity/instance grounding lift, execution-graded).

**Depends on:** Phase 07.2 (execution judge + vendored CK25 instance graph), Phase 06.1
(NL layer on the shared `NLQueryEngine`).

**Spike evidence (2026-07-23, live gpt-4o-mini, CK25 49-case execution judge):**

- Grounding **doubled** CK25: 6/49 (12.2%) → 12/49 (24.5%); Δ +12.2pt, 95% CI [+4.1, +22.4],
  McNemar b=6/c=0, **p=0.031, zero regressions**; retrieval recall of gold IRIs = 96%.
  Prototype in `scratchpad/nl-grounding-spike/` + findings in the phase dir
  (`07.3-SPIKE-FINDINGS.md`); label index over `rdfs:label|pv:name`, top-k by token match,
  inject "use these EXACT IRIs" block.

- Execution-guided **selection** (the former v1.1 lever) is **empirically dead** for CK25
  (p=1.0): the model reaches full consensus on systematically-wrong queries, so best-of-N /
  MBR has no correct sample to select. Superseded by this phase.

**Scope:** lever #1 (grounding) ONLY. Explicitly OUT of scope (own later phases):
(2) in-domain few-shot for schema-convention failures (~15 residual cases: `subClassOf*`,
OPTIONAL, indirect-manager idioms); (3) loosening the execution judge's projection-shape
strictness (some true accuracy is deflated by the answer-set judge keying on all projected
columns). Retrieval must run against the target instance data / a schema-agnostic index
(not CK25-specific hand-curation) so it transfers to the CDF project unchanged.

**Non-regression invariants (hard):** W3C DAWG query-eval coverage ≥ 96.4%; the deterministic
SPARQL→AQL transpiler package untouched; scripted configs stay the CI default (no live calls
in CI); each eval set stays independently reported (never blended).

**Plans:** 6/6 plans complete

Plans:
**Wave 1**

- [x] 07.3-01-PLAN.md — Engine-side grounding seam in arango-query-core: grounding.py (GroundedEntity/LabelIndex, verbatim spike port + label sanitization) + seam 6 + engine _system_prompt splice + barrel export + engine unit tests [NL-ACC-01]

**Wave 2**

- [x] 07.3-02-PLAN.md — Publish engine commit to the pinned remote (dual-remote git ls-remote verify) + bump pyproject pin (both extras) + uv lock [NL-ACC-01]

**Wave 3** *(parallel — no file overlap)*

- [x] 07.3-03-PLAN.md — SparqlAdapter seam 6 (injection-only) + verbatim SPARQL wording + NlPipeline passthrough + SC-gate (block in engine prompt, not grammar section) + adapter unit tests [NL-ACC-01]
- [x] 07.3-04-PLAN.md — Eval-only pyoxigraph→LabelIndex builder + deterministic gold-IRI retrieval-recall guard (CI-visible, >=0.90) + grounding: config-default structural test [NL-ACC-01]

**Wave 4**

- [x] 07.3-05-PLAN.md — runner.py additive grounding: read + build-once + passthrough + configs.yml CK25-grounded entries (pv:name config-only) + scripted-ck25-grounded plumbing gate [NL-ACC-01]

**Wave 5**

- [x] 07.3-06-PLAN.md — Human-run live CK25 grounded-vs-fresh-zero McNemar sweep + baseline.json fold-in (reported, not gated) + README §7 runbook + W3C/transpiler non-regression re-check [NL-ACC-01]

### Phase 07.2: Execution-based eval judging for adopted benchmarks (CK25) (INSERTED)

**Goal:** Make adopted-benchmark eval meaningful by grading on ANSWERS, not query
structure. 07.1's live run proved the canonical exact-algebra judge floors real LLM
output at 0% on QALD/CK25 (the golds pre-resolve entity IRIs + fix projections the
model can't reproduce). This phase adds answer-set execution judging (pyoxigraph) with
variable-rename + IRI↔label normalization, vendors the CK25 instance graph, and records
CK25's first real end-to-end NL→SPARQL accuracy number as the corporate-domain anchor.

**Scope (locked in 07.2-CONTEXT.md):**

- CK25 now; QALD's heavier DBpedia-subset capture deferred to its own later phase.
- Execution engine = **pyoxigraph only** (the repo's W3C ground-truth engine); no
  ArangoDB/AQL execution path this phase (transpiler AQL correctness stays covered by the
  W3C suite).

- Correctness = answer-set equality up to variable renaming **+ IRI↔label normalization**
  (so a manager's name vs IRI both count); handles SELECT and ASK.

**Explicitly OUT of scope:** QALD execution grading + DBpedia answer-subset capture (own
phase); transpiler→AQL→ArangoDB grading path; partial-credit/F1 scoring (binary answer-set
match this phase). Deterministic transpiler + W3C ≥ 96.4% guard untouched.

**Requirements**: NL-EVAL-05
**Depends on:** Phase 7, Phase 07.1
**Success Criteria** (what must be TRUE):

  1. CK25 instance graph vendored under `vendored/ck25/` with CC-BY-4.0 NOTICE + source/commit provenance (passes the 07.1 `test_vendored_provenance.py` guard; no secrets)
  2. An answer-set execution judge exists — runs gold + candidate SPARQL through pyoxigraph, compares answers up to variable renaming + IRI↔label normalization, handles SELECT and ASK, and is opt-in per config via `judge: execution`
  3. `scripted-ck25` under execution judging is a green gold-vs-gold sanity gate (100%), and a live `openai-gpt4o-mini-ck25` execution-graded accuracy number is recorded in `baseline.json` (reported, NOT gated — CK25 is the directional anchor)
  4. Non-regression: `scripted` stays the no-network canonical CI default, W3C DAWG coverage ≥ 96.4% holds, transpiler untouched, no secrets/raw-prompts committed

**Plans:** 4/4 plans complete

Plans:
**Wave 1** *(parallel — no file overlap)*

- [x] 07.2-01-PLAN.md — Answer-set execution judge: oxi_query() form-aware helper + relaxed _judge_execution (value-set + IRI↔label norm + ASK + gold xsd:int fix + D-05 tagged buckets) + corpus data_path read, test-first [NL-EVAL-05]
- [x] 07.2-02-PLAN.md — Vendor full CK25 instance graph (951,747 B / 26,903 triples @ pinned commit) + extend NOTICE.md + corpus.yml data_path key [NL-EVAL-05]

**Wave 2** *(blocked on Wave 1)*

- [x] 07.2-03-PLAN.md — Flip scripted-ck25 / openai-gpt4o-mini-ck25 to judge: execution in place + gold-vs-gold sanity gate (100%) + execution-graded scripted-ck25 baseline entry [NL-EVAL-05]

**Wave 3** *(blocked on Wave 2)*

- [x] 07.2-04-PLAN.md — Human-run live openai-gpt4o-mini-ck25 execution sweep + fold reported-not-gated anchor number into baseline.json + README runbook [NL-EVAL-05]

### Phase 07.1: NL→SPARQL eval via public benchmarks (INSERTED)

> **Approach pivoted 2026-07-22 via a `/grill-me` design review.** The original plan
> (synthetic ontology-driven corpus generation) was **retired** in favor of adopting public,
> human-authored NL→SPARQL benchmark test sets. The former Phase 07.2 (CK25 anchor) is
> **folded into this phase**. See `07.1-CONTEXT.md` for the full rationale (D-01..D-09).

**Goal:** Make NL→SPARQL quality measurable with real statistical power by **adopting public
benchmark test sets** rather than generating a synthetic corpus. The eval judge already
compares SPARQL-to-SPARQL (canonical algebra — no data or transpiler needed for positive
cases), so public (question → gold-SPARQL) sets drop into the existing harness cheaply. This
reaches ~5–8pt minimum-detectable-effect (from ~16pt at 25 cases) using real, independently
vetted questions — and removes the template-correctness, external-validity, and SPARQL-fluency
risks that sank the synthetic approach.

**Scope:**

- Adopt **QALD-9-plus** (DBpedia, CC-BY-4.0, ~558 English Q w/ gold SPARQL) as the **powered
  capability gate** (~6–8pt MDE, transferable SPARQL skill).

- Adopt **CK25** (`eccenca/ck25-dataset`, CC-BY-4.0, ~50 expert-curated corporate Q) as the
  **corporate-domain relevance anchor** (directional; domain-matched to CDF).

- Author a **small `expect_refusal` supplement** (~a dozen cases) keyed to the transpiler's
  actual coverage — the one thing public sets can't provide.

- Keep the **power-analysis module** (achieved-MDE reporting) — the sole salvage from the
  retired synthetic plan.

- Wire both sets into the harness (schema subset + question filter + `configs.yml`),
  regenerate the scripted baseline; keep the W3C ≥ 96.4% guard.

**Explicitly OUT of scope:** the synthetic generator (templates/enumeration/LLM authoring/
faithfulness judge/partitioning) — **retired**, revisited only if real QALD+CK25 numbers show a
specific need for more corporate-domain power (then as an amplifier of CK25's real patterns).
v1.1 levers (SFT/LoRA, execution-guided selection, embedder/hybrid swap) and live-CDF-schema
grounding remain deferred.

**Non-regression:** deterministic SPARQL→AQL transpiler untouched; W3C DAWG query-eval coverage
≥ 96.4%; `scripted` stays the no-network CI default; adopted sets vendored with CC-BY
attribution; no secrets committed.

**Requirements**: NL-BENCH-01, NL-BENCH-02, NL-BENCH-03, NL-BENCH-04, NL-BENCH-05, NL-BENCH-06, NL-BENCH-07
**Depends on:** Phase 7 (eval harness + judge + the measurement finding)
**Plans:** 6/6 plans complete

Plans:
**Wave 1** *(parallel — no file overlap)*

- [x] 07.1-01-PLAN.md — Harness foundation: power module (D-07) + additive `corpus:` config key (test-first, Pitfall 1) + glob-driven gold-transpilability guard [NL-BENCH-04, NL-BENCH-05, NL-BENCH-07]
- [x] 07.1-02-PLAN.md — Vendored-data provenance + no-secrets static guard (T-07.1-01) [NL-BENCH-06]
- [x] 07.1-03-PLAN.md — Refusal supplement: ~10–12 expect_refusal cases keyed to real UnsupportedSparqlError sites [NL-BENCH-03]

**Wave 2** *(parallel — no file overlap; blocked on Wave 1)*

- [x] 07.1-04-PLAN.md — QALD-9-plus adoption: QALD-JSON→cases, minimal phys:-annotated DBpedia subset, D-06 filter log (combined train+test pool) [NL-BENCH-01, NL-BENCH-07]
- [x] 07.1-05-PLAN.md — CK25 adoption: questions.yml→cases, phys:-annotate the prod-inst.ttl schema, D-06 filter log [NL-BENCH-02, NL-BENCH-07]

**Wave 3** *(blocked on Waves 1–2)*

- [x] 07.1-06-PLAN.md — Wire per-set configs + regenerate scripted baseline (QALD/CK25 reported SEPARATELY) + achieved-MDE via power module + W3C guard + file NL-BENCH ids [NL-BENCH-01..07]

### Phase 8: Public release readiness

**Goal**: Ship v1.0 publicly with a green CI matrix, complete governance docs, and a signed-off SBOM.
**Depends on**: Phase 7 (and Phases 4–5 verification)
**Requirements**: REQ-public-release-readiness
**Success Criteria** (what must be TRUE):

  1. Repo is public with MIT LICENSE + CONTRIBUTING + SECURITY + operational runbook published
  2. CI is green across Python 3.11/3.12/3.13 and ArangoDB 3.11/3.12
  3. `docker compose up && curl /health/ready` succeeds as a repeatable dev loop
  4. An SBOM artefact is attached to the v1.0 release tag

**Plans**: TBD
**Status**: Not started

## Progress

**Execution Order:**
Phases execute in numeric order: 1 → 2 → 3 → 4 → 5 → 6 → 06.1 → 06.2 → 7 → 07.1 → 8.
Phases 1–3 are already Complete. The active workstream begins at **Phase 6**
(NL→SPARQL). Phases 4, 5, and 6 have no hard inter-dependency on each other and may
be sequenced by priority — the user's directive puts NL→SPARQL first. The NL→SPARQL
arc runs 6 (measurable) → 06.1 (shared engine) → 06.2 (harder corpus + live baseline)
→ 7 (dense few-shot lift).

| Phase | Plans Complete | Status | Completed |
|-------|----------------|--------|-----------|
| 1. Deterministic transpiler core | shipped | Complete | pre-GSD (mature) |
| 2. Protocol service + schema HTTP | shipped | Complete | pre-GSD (mature) |
| 3. Operational/security/privacy parity | shipped | Complete | pre-GSD (mature) |
| 4. Interop & performance verification | 8/8 | Complete    | 2026-07-28 |
| 5. UI workbench parity completion | 0/TBD | Not started | - |
| 6. NL→SPARQL eval harness + corpus | 3/3 | Complete    | 2026-07-15 |
| 06.1. Re-point nl2sparql onto shared engine | 3/3 | Executed | 2026-07-20 |
| 06.2. NL→SPARQL harder corpus + live baseline | 4/4 | Complete    | 2026-07-21 |
| 7. NL→SPARQL dense few-shot retrieval | 4/4 | Complete    | 2026-07-22 |
| 07.1. NL→SPARQL eval via public benchmarks | 6/6 | Complete    | 2026-07-22 |
| 8. Public release readiness | 0/TBD | Not started | - |
