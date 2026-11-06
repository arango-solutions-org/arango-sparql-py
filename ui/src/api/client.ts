// Typed wrappers around the `arango_sparql` service HTTP surface.
//
// Mirrors `references/arango-cypher-py/ui/src/api/client.ts` but
// adapted to the SPARQL request/response shapes defined in
// `arango_sparql/service/models.py`:
//
//   * /translate → { sparql, ontology_ttl, params }
//   * /execute   → { sparql, ontology_ttl, params, database }
//
// Contract note (client↔models audit): every function below maps to a
// live backend route EXCEPT `getSampleQueries` (`/sample-queries`), which
// is documented in PRD §A.9 but not yet implemented server-side. Its
// caller (`SampleQueries.tsx`) degrades gracefully to the built-in static
// samples, so the missing route is a swallowed 404 rather than a failure.

import type { MinedExample } from "../utils/minedExamples";
import type { GraphShape } from "../utils/graphShape";
import {
  contextHeaders,
  controlPlane,
  sessionEnded,
  type ProjectContext,
} from "./controlPlane";
import type { EditorVocab } from "../lang/sparqlEditorCore";

export interface ConnectRequest {
  url: string;
  database: string;
  username: string;
  password: string;
}

export interface ConnectResponse {
  token: string;
  databases: string[];
  // The database the session opened. /connect/platform chooses it when the
  // request names none (the mount database may not be one the user can open).
  database?: string | null;
}

export interface ConnectDefaults {
  url: string;
  database: string;
  username: string;
  password?: string;
}

export interface PlatformStatus {
  // True when the Workbench can open a session from the platform login the
  // gateway forwarded — no credentials dialog needed.
  available: boolean;
  // The database a platform session opens by default: the instance's mount.
  database: string;
  reason?: string | null;
}

export interface TranslateRequest {
  sparql: string;
  ontology_ttl?: string;
  params?: Record<string, unknown>;
}

export interface TranslateResponse {
  aql: string;
  bind_vars: Record<string, unknown>;
  warnings: Array<{ message: string }>;
  elapsed_ms?: number;
}

// SPARQL `/execute` returns an SPARQL solution-mappings list (one
// dict per row keyed by SPARQL projection variable). We surface them
// under `bindings:` to match the backend, but also expose a
// `results:`-shaped alias so the existing ResultsPanel (ported from
// the Cypher UI) can render them with no code changes — the table
// view treats each row as a record and the graph view picks up
// `_id`/`_from`/`_to` IRIs the same way it does in Cypher.
export interface SparqlExecuteRequest {
  sparql: string;
  /** Run for the graph view: every variable in the query's patterns, and the patterns. */
  graph?: boolean;
  ontology_ttl?: string;
  params?: Record<string, unknown>;
  database?: string;
}

export interface SparqlExecuteResponse {
  bindings: Array<Record<string, unknown>>;
  /** Set on a graph run (`graph: true`): the query's patterns, to draw the rows with. */
  graph?: GraphShape | null;
  warnings: Array<{ message: string }>;
  aql?: string | null;
  bind_vars?: Record<string, unknown> | null;
  elapsed_ms?: number;
}

// Mirrors `ValidateResponse` in `arango_sparql/service/models.py`: the
// parse-only result uses `valid` (not `ok`), plus non-fatal `warnings`.
export interface ValidateResponse {
  valid: boolean;
  errors: Array<{ message: string; code?: string }>;
  warnings: Array<{ message?: string; code?: string }>;
}

// Mirrors `SparqlExplainResponse` in `arango_sparql/service/models.py`.
// `plan` is ArangoDB's raw explain output ({nodes, rules, collections,
// variables, estimatedCost, ...}) surfaced verbatim.
export interface ExplainResponse {
  sparql: string;
  aql: string;
  bind_vars: Record<string, unknown>;
  plan: Record<string, unknown>;
  warnings: Array<{ message?: string; code?: string }>;
  translate_ms?: number | null;
}

// Mirrors `SparqlProfileResponse` in `arango_sparql/service/models.py`.
// `profile` is ArangoDB's `cursor.profile()` blob ({plan, stats, profile,
// warnings, ...}); `bindings` are the materialised rows (capped server-side).
export interface ProfileResponse {
  sparql: string;
  aql: string;
  bind_vars: Record<string, unknown>;
  bindings: Array<Record<string, unknown>>;
  truncated: boolean;
  profile: Record<string, unknown>;
  warnings: Array<{ message?: string; code?: string }>;
  translate_ms?: number | null;
  exec_ms?: number | null;
}

function authHeaders(token: string): Record<string, string> {
  // Under a control plane the context is the only credential, on every call.
  if (controlPlane.on) return contextHeaders();
  return { "X-Arango-Session": token };
}

// SPA mount-point detection, ported from the Cypher UI's `apiBaseFor`.
// The API lives at the directory the page was served from: the Container
// Manager opens the bare mount (`/_service/uds/_db/<db>/<instance>/`), so the
// base is that path. A trailing `index.html` and a `/frontend` or `/ui`
// sub-mount (legacy deploys) are dropped. Whole segments are compared, so an
// instance named `ui-demo` is not cut in half. At the origin root (Vite dev)
// the base is empty and `vite.config.ts`'s proxy table takes over.
//
// The previous `indexOf("/frontend" | "/ui")` version returned "" under the
// Container Manager mount, which sent every call (incl. /connect/platform) to
// the cluster root — the Workbench then fell back to the credentials button.
// The SPA has no client-side routes, so the page path is always the mount.
export function apiBaseFor(pathname: string): string {
  const segments = pathname.split("/");
  if (/\.html?$/i.test(segments[segments.length - 1] ?? "")) segments.pop();
  while (segments.length > 0 && segments[segments.length - 1] === "") segments.pop();
  const last = segments[segments.length - 1];
  if (last === "frontend" || last === "ui") segments.pop();
  return segments.join("/");
}

function apiBase(): string {
  return apiBaseFor(window.location.pathname);
}

export const AUTH_EXPIRED_MESSAGE =
  "Your session has expired. Please re-authenticate to the database.";

async function request<T>(
  path: string,
  options: RequestInit = {},
): Promise<T> {
  const { headers: extraHeaders, ...rest } = options;
  const res = await fetch(apiBase() + path, {
    ...rest,
    credentials: "include",
    headers: {
      "Content-Type": "application/json",
      ...(extraHeaders as Record<string, string>),
      // Every call carries the context under a control plane, including the ones that
      // need no session off it (translate, validate, the plain-English routes).
      ...(controlPlane.on ? contextHeaders() : {}),
    },
  });
  if (!res.ok) {
    if (res.status === 401) {
      await res.text().catch(() => "");
      if (controlPlane.on) sessionEnded();
      throw new ApiError(401, AUTH_EXPIRED_MESSAGE);
    }
    const body = await res.json().catch(() => ({ detail: res.statusText }));
    throw new ApiError(res.status, body.detail ?? body);
  }
  return res.json();
}

function formatDetail(detail: unknown): string {
  if (typeof detail === "string") return detail;
  if (detail && typeof detail === "object") {
    const obj = detail as Record<string, unknown>;
    if (typeof obj.error === "string") return obj.error;
    if (typeof obj.detail === "string") return obj.detail;
    if (typeof obj.message === "string") return obj.message;
  }
  return JSON.stringify(detail);
}

export class ApiError extends Error {
  status: number;
  detail: unknown;

  constructor(status: number, detail: unknown) {
    super(formatDetail(detail));
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
  }
}

export function isAuthError(err: unknown): boolean {
  return err instanceof ApiError && err.status === 401;
}

export interface HealthResponse {
  status: string;
  /** "arango-sparql-py" — same keys as arango-cypher-py's /health. */
  service: string;
  version: string;
  /** The installed arangodb-schema-analyzer, or null when absent. */
  analyzer_version: string | null;
}

/** Under a control plane: who this tab is for, which project, and its database. */
export async function getControlPlaneContext(): Promise<ProjectContext> {
  return request("/control-plane/context");
}

export async function getHealth(): Promise<HealthResponse> {
  return request("/health");
}

export async function getConnectDefaults(): Promise<ConnectDefaults> {
  return request("/connect/defaults");
}

export async function connect(req: ConnectRequest): Promise<ConnectResponse> {
  return request("/connect", {
    method: "POST",
    body: JSON.stringify(req),
  });
}

// Whether the Workbench can skip the connect dialog and open a session from
// the platform login the gateway forwarded (BYOC / Container Manager).
export async function getPlatformStatus(): Promise<PlatformStatus> {
  return request("/connect/platform");
}

// Open a session as the signed-in platform user. No credentials: the gateway
// forwards the platform login with the request.
export async function connectPlatform(database?: string): Promise<ConnectResponse> {
  return request("/connect/platform", {
    method: "POST",
    body: JSON.stringify(database ? { database } : {}),
  });
}

export async function disconnect(token: string): Promise<void> {
  await request("/disconnect", {
    method: "POST",
    headers: authHeaders(token),
  });
}

// ---------------------------------------------------------------------------
// ArangoDB named-graph scoping (GET /graphs, POST /session/graph). Lets the
// UI restrict schema acquisition to one graph's collections so a shared DB's
// unrelated collections don't pollute translation / suggestions.
// ---------------------------------------------------------------------------

export interface GraphInfo {
  name: string;
  edgeCollections: string[];
  vertexCollections: string[];
  orphanCollections: string[];
  collectionCount: number;
}

// Verified examples mined from the session's database (GET /examples, PRD §7.7);
// scoped server-side to the bound named graph. Shape: utils/minedExamples.ts.
export async function getMinedExamples(
  token: string,
  limit: number = 50,
): Promise<{ examples: MinedExample[] }> {
  return request(`/examples?limit=${limit}`, { headers: authHeaders(token) });
}

export async function listGraphs(token: string): Promise<{ graphs: GraphInfo[] }> {
  return request("/graphs", { headers: authHeaders(token) });
}

export async function bindGraph(
  graphName: string | null,
  token: string,
): Promise<{ graph_name: string | null; bound: boolean }> {
  return request("/session/graph", {
    method: "POST",
    headers: authHeaders(token),
    body: JSON.stringify({ graphName }),
  });
}

export async function translateSparql(
  req: TranslateRequest,
): Promise<TranslateResponse> {
  return request("/translate", {
    method: "POST",
    body: JSON.stringify(req),
  });
}

export async function executeSparql(
  req: SparqlExecuteRequest,
  token?: string,
): Promise<SparqlExecuteResponse> {
  return request("/execute", {
    method: "POST",
    body: JSON.stringify(req),
    headers: token ? authHeaders(token) : undefined,
  });
}

export async function validateSparql(
  req: TranslateRequest,
): Promise<ValidateResponse> {
  return request("/validate", {
    method: "POST",
    body: JSON.stringify(req),
  });
}

// Run an AQL string directly, bypassing translation. Mirrors the
// Cypher UI's `executeAql` — this is what the AQL editor uses when
// the user hand-edits the translated AQL and re-runs it.
export async function executeAql(
  aql: string,
  bindVars: Record<string, unknown>,
  token: string,
): Promise<{ results: unknown[]; warnings: Array<{ message: string }>; exec_ms?: number }> {
  return request("/execute-aql", {
    method: "POST",
    body: JSON.stringify({ aql, bind_vars: bindVars }),
    headers: authHeaders(token),
  });
}

// POST /explain — translate SPARQL → AQL, then return the AQL execution
// plan from `db.aql.explain()` (no rows materialised). Requires a session
// (same payload/guards as /execute); honours X-Tenant-Id.
export async function explainSparql(
  req: SparqlExecuteRequest,
  token: string,
): Promise<ExplainResponse> {
  return request("/explain", {
    method: "POST",
    body: JSON.stringify(req),
    headers: authHeaders(token),
  });
}

// POST /profile — translate SPARQL → AQL and execute with `profile=2`,
// returning per-stage timings + the materialised rows. Requires a
// session; honours X-Tenant-Id.
export async function profileSparql(
  req: SparqlExecuteRequest,
  token: string,
): Promise<ProfileResponse> {
  return request("/profile", {
    method: "POST",
    body: JSON.stringify(req),
    headers: authHeaders(token),
  });
}

// ---------------------------------------------------------------------------
// OWL schema (from `arango-schema-mapper`)
// ---------------------------------------------------------------------------

// Returned by `GET /schema/owl` (live — `schema.py::schema_owl`, requires
// a session). The shape matches the Turtle ontology that
// `arango-schema-mapper` produces: a list of OWL classes + properties
// keyed by IRI. SchemaGraph.tsx renders this as a Cytoscape graph.
export interface OwlClass {
  iri: string;
  localName: string;
  superClasses: string[];
  comment?: string;
}

export interface OwlProperty {
  iri: string;
  localName: string;
  domain: string[];
  range: string[];
  kind: "object" | "datatype" | "annotation";
  comment?: string;
}

export interface OwlSchemaResponse {
  classes: OwlClass[];
  properties: OwlProperty[];
  // Optional source TTL — round-trip handy for the OntologyPanel.
  turtle?: string;
  status?: SchemaReadStatus;
  warming?: boolean;
}

export async function getOwlSchema(
  token?: string,
): Promise<OwlSchemaResponse> {
  return request("/schema/owl", {
    headers: token ? authHeaders(token) : undefined,
  });
}

// Mirrors `SchemaStatisticsResponse` in models.py. `statistics` is the
// analyzer's free-form `metadata.statistics` block (cardinality / degree /
// selectivity per relationship); `available` is false for heuristic-only
// bundles. Used by the schema graph to weight arcs by instance volume.
export interface SchemaStatisticsResponse {
  statistics: Record<string, unknown>;
  available: boolean;
  last_acquired_at?: string | null;
  status?: SchemaReadStatus;
  warming?: boolean;
}

export async function getSchemaStatistics(
  token?: string,
): Promise<SchemaStatisticsResponse> {
  return request("/schema/statistics", {
    headers: token ? authHeaders(token) : undefined,
  });
}

// ---------------------------------------------------------------------------
// Schema acquisition (PRD §6.4 — backed by `arango_sparql.schema.acquire`)
// ---------------------------------------------------------------------------
//
// These wrappers are a 1:1 mapping of the FastAPI routes added in
// service slice 6 (`arango_sparql/service/routes/schema.py`). The
// shapes intentionally mirror the Pydantic response models; we
// keep them as `Record<string, unknown>` rather than full typed
// interfaces because the analyzer's wire-dict shape evolves
// version-to-version and a permissive type lets the UI render any
// future fields without a frontend rebuild.

export type SchemaStrategy = "auto" | "analyzer" | "heuristic";

export interface SchemaIntrospectQuery {
  database?: string;
  strategy?: SchemaStrategy;
  force?: boolean;
  /** Include the OWL/Turtle ontology in the response. Default true. */
  include_owl?: boolean;
  /** Include statistics. Default true. */
  include_statistics?: boolean;
}

// Catalog model (arango_sparql/service/schema_warm.py): schema analysis never
// blocks a request. "pending" = not analyzed yet, a background analysis is
// running — retry. `warming` = a (re)analysis is in flight; with "ready" the
// payload is the current cached schema and a fresher one is on its way.
export type SchemaReadStatus = "ready" | "pending";

export interface SchemaIntrospectResponse {
  mapping: Record<string, unknown>;
  summary: Record<string, unknown>;
  warnings: Array<{ code: string; message: string; install_hint?: string; severity?: string }>;
  source: Record<string, unknown> | null;
  cache_hit: boolean;
  elapsed_ms: number;
  status?: SchemaReadStatus;
  warming?: boolean;
  /** Editor vocabulary v1 for SPARQL completion (issue #68); null while
   * pending or when the ontology names no default `:` namespace. */
  editor_vocab?: EditorVocab | null;
}

function qs(params: Record<string, string | number | boolean | undefined>): string {
  const parts: string[] = [];
  for (const [k, v] of Object.entries(params)) {
    if (v === undefined || v === null) continue;
    parts.push(`${encodeURIComponent(k)}=${encodeURIComponent(String(v))}`);
  }
  return parts.length ? `?${parts.join("&")}` : "";
}

export async function schemaIntrospect(
  token: string,
  query: SchemaIntrospectQuery = {},
): Promise<SchemaIntrospectResponse> {
  return request(`/schema/introspect${qs(query as Record<string, string | number | boolean | undefined>)}`, {
    headers: authHeaders(token),
  });
}

const defaultSleep = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms));

// Latest-wins guard: only the most recent schema poll may deliver a result.
// A DB switch, disconnect or second refresh starts (or cancels) a poll, and
// every older one stops with SchemaPollSuperseded instead of overwriting the
// new session's schema state.
let schemaPollGeneration = 0;

export class SchemaPollSuperseded extends Error {
  constructor() {
    super("superseded by a newer schema request");
    this.name = "SchemaPollSuperseded";
  }
}

/** Stop every in-flight schema poll (e.g. on disconnect). */
export function cancelSchemaPolls(): void {
  schemaPollGeneration += 1;
}

export interface SchemaPollOptions {
  /** Called on every wait so the UI can show "Analyzing schema…". */
  onAnalyzing?: (resp: SchemaIntrospectResponse) => void;
  /** Give up (returning the last "pending" answer) after this long. */
  maxWaitMs?: number;
  /** Injectable for tests. */
  sleep?: (ms: number) => Promise<void>;
  now?: () => number;
}

/**
 * GET /schema/introspect, waiting out a background analysis.
 *
 * Ported from arango-cypher-py's `introspectSchemaUntilReady`, with a longer
 * budget: cypher relies on its catalog sidecar pre-warming, while this service
 * analyzes on first use (the first read of a large database such as
 * prod.demo `IAM` takes minutes). Waits while the answer is "pending" — and,
 * for a forced refresh, while it is still `warming` (the server keeps serving
 * the previous schema until the new one lands). Backs off 2s → 10s; later
 * polls never re-force. Returns the last answer when `maxWaitMs` elapses
 * (default 20 min) so the caller can say "still analyzing".
 */
export async function schemaIntrospectUntilReady(
  token: string,
  query: SchemaIntrospectQuery = {},
  opts: SchemaPollOptions = {},
): Promise<SchemaIntrospectResponse> {
  const generation = ++schemaPollGeneration;
  const { onAnalyzing, maxWaitMs = 20 * 60_000, sleep = defaultSleep, now = Date.now } = opts;
  const forced = query.force === true;
  const started = now();
  const waiting = (r: SchemaIntrospectResponse) =>
    r.status === "pending" || (forced && r.warming === true);
  let resp = await schemaIntrospect(token, query);
  let delay = 2000;
  while (waiting(resp)) {
    if (generation !== schemaPollGeneration) throw new SchemaPollSuperseded();
    if (now() - started >= maxWaitMs) return resp;
    onAnalyzing?.(resp);
    await sleep(delay);
    if (generation !== schemaPollGeneration) throw new SchemaPollSuperseded();
    delay = Math.min(Math.round(delay * 1.5), 10_000);
    resp = await schemaIntrospect(token, { ...query, force: false });
  }
  if (generation !== schemaPollGeneration) throw new SchemaPollSuperseded();
  return resp;
}

export type SchemaDriftStatus =
  | "no_cache"
  | "unchanged"
  | "stats_only"
  | "shape_changed";

export interface SchemaStatusResponse {
  status: SchemaDriftStatus;
  cached_at?: string | null;
  cached_fingerprints?: { shape?: string; statistics?: string };
  live_fingerprints?: { shape?: string; statistics?: string };
  warnings: Array<{ code: string; message: string; install_hint?: string }>;
}

export async function schemaStatus(
  token: string,
  database?: string,
): Promise<SchemaStatusResponse> {
  return request(`/schema/status${qs({ database })}`, {
    headers: authHeaders(token),
  });
}

// Mirrors `SchemaInvalidateCacheResponse`: `db_name` (not `database`) and
// the L2 `persistent_dropped` stub flag.
export interface SchemaInvalidateResponse {
  invalidated: boolean;
  db_name?: string;
  persistent_dropped?: boolean;
}

export async function schemaInvalidateCache(
  token: string,
  database?: string,
): Promise<SchemaInvalidateResponse> {
  return request("/schema/invalidate-cache", {
    method: "POST",
    headers: authHeaders(token),
    body: JSON.stringify({ database }),
  });
}

export async function schemaForceReacquire(
  token: string,
  query: SchemaIntrospectQuery = {},
): Promise<SchemaIntrospectResponse> {
  return request("/schema/force-reacquire", {
    method: "POST",
    headers: authHeaders(token),
    body: JSON.stringify(query),
  });
}

// ---------------------------------------------------------------------------
// OWL Import / Export (PRD §6.4 rows 8 & 9)
// ---------------------------------------------------------------------------
//
// Two shapes per route to match the backend's content-negotiated
// behaviour:
//
// * Import — JSON envelope `{turtle, source_notes?}` is the default
//   path because the UI's File API hands us a string. Raw text/turtle
//   is supported for symmetry but not used by the panel today.
// * Export — JSON envelope returns `{turtle, mime_type, triple_count}`;
//   the `Accept: text/turtle` path is used by the "Download" button
//   so the browser saves the raw bytes verbatim.

export interface OwlImportResponse {
  accepted: boolean;
  mapping: Record<string, unknown>;
  triple_count: number;
  warnings: Array<{ code: string; message: string }>;
  source: Record<string, unknown> | null;
  elapsed_ms: number;
}

export async function importOwl(
  turtle: string,
  token: string,
  sourceNotes?: string,
): Promise<OwlImportResponse> {
  return request("/mapping/import-owl", {
    method: "POST",
    headers: authHeaders(token),
    body: JSON.stringify({
      turtle,
      ...(sourceNotes ? { source_notes: sourceNotes } : {}),
    }),
  });
}

export interface OwlExportJsonResponse {
  turtle: string;
  mime_type: string;
  triple_count: number;
  elapsed_ms: number;
}

export async function exportOwlJson(
  mapping: Record<string, unknown> | null,
  ontologyTtl: string | undefined,
  token: string,
): Promise<OwlExportJsonResponse> {
  return request("/mapping/export-owl", {
    method: "POST",
    headers: authHeaders(token),
    body: JSON.stringify({
      ...(mapping ? { mapping } : {}),
      ...(ontologyTtl ? { ontology_ttl: ontologyTtl } : {}),
    }),
  });
}

/**
 * Fetch the export as raw `text/turtle` bytes, suitable for handing
 * to a Blob download. Bypasses `request()` because the response is
 * not JSON.
 */
export async function exportOwlAsTurtle(
  mapping: Record<string, unknown> | null,
  ontologyTtl: string | undefined,
  token: string,
): Promise<{ turtle: string; tripleCount: number }> {
  const res = await fetch(apiBase() + "/mapping/export-owl", {
    method: "POST",
    credentials: "include",
    headers: {
      "Content-Type": "application/json",
      Accept: "text/turtle",
      ...authHeaders(token),
    },
    body: JSON.stringify({
      ...(mapping ? { mapping } : {}),
      ...(ontologyTtl ? { ontology_ttl: ontologyTtl } : {}),
    }),
  });
  if (!res.ok) {
    if (res.status === 401) {
      if (controlPlane.on) sessionEnded();
      throw new ApiError(401, AUTH_EXPIRED_MESSAGE);
    }
    const body = await res.json().catch(() => ({ detail: res.statusText }));
    throw new ApiError(res.status, body.detail ?? body);
  }
  const turtle = await res.text();
  const tripleCount = Number(res.headers.get("x-triple-count") ?? "0") || 0;
  return { turtle, tripleCount };
}

// ---------------------------------------------------------------------------
// Sample queries
// ---------------------------------------------------------------------------

export interface SampleQuery {
  id: string;
  description: string;
  sparql: string;
  dataset: string;
  expected_min_count?: number;
}

export async function getSampleQueries(
  dataset?: string,
): Promise<{ queries: SampleQuery[] }> {
  const qs = dataset ? `?dataset=${encodeURIComponent(dataset)}` : "";
  return request(`/sample-queries${qs}`);
}

// ---------------------------------------------------------------------------
// NL → SPARQL pipeline (POST /nl-translate). The backend runs the LLM,
// parses the SPARQL, and feeds it through the deterministic transpiler,
// so a single call returns BOTH the SPARQL and the ready-to-run AQL.
// Mirrors arango-cypher-py's nl2Cypher but adapted to the SPARQL
// pipeline's request/response shapes (see
// arango_sparql.service.models.NlTranslate{Request,Response}).
// ---------------------------------------------------------------------------

export interface NlTranslateResponse {
  nl: string;
  sparql: string;
  aql: string;
  bind_vars: Record<string, unknown>;
  warnings: Array<Record<string, unknown>>;
  llm_calls: number;
  cost_usd: number;
  latency_ms: number;
  repaired: boolean;
}

export interface NlTranslateOptions {
  /** Inline OWL/Turtle schema the LLM should ground its query in. */
  ontologyTtl?: string;
  /** Max transpiler-driven repair iterations (backend clamps 0..5). */
  maxRepairs?: number;
}

export async function nl2Sparql(
  question: string,
  opts: NlTranslateOptions = {},
): Promise<NlTranslateResponse> {
  const body: Record<string, unknown> = { nl: question };
  if (opts.ontologyTtl) body.ontology_ttl = opts.ontologyTtl;
  if (opts.maxRepairs !== undefined) body.max_repairs = opts.maxRepairs;
  return request("/nl-translate", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export interface NlSamplesResponse {
  queries: string[];
  elapsed_ms?: number;
}

export async function suggestNlQueries(
  ontologyTtl: string | undefined,
  count: number = 8,
  useLlm: boolean = true,
): Promise<NlSamplesResponse> {
  const body: Record<string, unknown> = { count, use_llm: useLlm };
  if (ontologyTtl) body.ontology_ttl = ontologyTtl;
  return request("/nl-samples", {
    method: "POST",
    body: JSON.stringify(body),
  });
}
