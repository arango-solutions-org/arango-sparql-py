import { useCallback, useReducer } from "react";
import { projectStore } from "./controlPlane";
import { appendTurn, type TranscriptTurn } from "../utils/transcript";
import type { EditorVocab } from "../lang/sparqlEditorCore";

// `arango-sparql-py` UI store. Mirrors
// `references/arango-cypher-py/ui/src/api/store.ts` but trimmed to the
// subset of state the SPARQL backend currently supports:
//
//   * `sparql` (the editor's query text) instead of `cypher`
//   * `ontologyTtl` (raw Turtle text passed to /translate as
//     `ontology_ttl`) instead of `mapping` (the Cypher schema mapping
//     JSON object).
//
// The reducer surface intentionally keeps SET_SPARQL/SET_ONTOLOGY/...
// names symmetrical with the Cypher store so a developer who knows
// one repo reads the other in seconds.

export interface ConnectionState {
  status: "disconnected" | "connecting" | "connected";
  token: string | null;
  url: string;
  database: string;
  username: string;
  password: string;
  databases: string[];
  error: string | null;
  // True when the session was opened via the platform login (no credentials):
  // database switches reconnect through /connect/platform, not /connect.
  platform?: boolean;
}

export type ResultTab = "table" | "json" | "graph" | "explain" | "profile";

export interface HistoryEntry {
  sparql: string;
  timestamp: number;
  aqlPreview: string;
}

// Provenance of the text currently in the SPARQL editor. Drives
// affordances like "this query came from NL" status lines. Mirrors the
// Cypher UI's `editorCypherSource`.
export type SparqlSource = "user" | "nl_pipeline" | "sample";

// Telemetry surfaced in the NL "Ask" bar status line after a successful
// /nl-translate. Mirrors arango-cypher-py's `nlInfo`.
export interface NlInfo {
  latencyMs: number;
  llmCalls: number;
  costUsd: number;
  repaired: boolean;
}

// Schema-acquisition surface (PRD §6.4 — backed by
// `arango_sparql.schema.acquire`). Populated by ConnectionDialog
// after a successful /connect call (auto-introspect) and by the
// MappingPanel's Import / refresh affordances.
export interface SchemaWarning {
  code: string;
  message: string;
  install_hint?: string;
  /** "info" notes describe normal operation (e.g. the analyzer's no-LLM
   * baseline) and stay out of the warning banner. */
  severity?: string;
}

export type SchemaCacheStatus =
  | "idle"
  | "no_cache"
  | "unchanged"
  | "stats_only"
  | "shape_changed";

export interface SchemaState {
  /** Canonical mapping wire dict from /schema/introspect. */
  mapping: Record<string, unknown> | null;
  /** Server-side summary block (entity count, RPT collections, …). */
  summary: Record<string, unknown> | null;
  /** Editor vocabulary v1 the SPARQL completion runs on — the same
   * per-class list the NL prompt is grounded on (issue #68). */
  editorVocab: EditorVocab | null;
  /** Schema warnings (e.g. ANALYZER_NOT_INSTALLED, W_SCHEMA_*). */
  warnings: SchemaWarning[];
  /** Drift status from the most recent /schema/status call. */
  cacheStatus: SchemaCacheStatus;
  /** True when the latest introspect was served from L1 cache. */
  cacheHit: boolean;
  /** Last successful introspect timestamp (Date.now()). */
  lastFetchedAt: number | null;
  /** True while a /schema/{introspect,force-reacquire} call is in flight. */
  refreshing: boolean;
  /** True while the server analyzes the database in the background (the
   * schema read answered "pending", or a forced refresh is still `warming`).
   * The first analysis of a large database can take minutes. */
  analyzing: boolean;
  /** Last error from a schema-API call, if any. Cleared on success. */
  error: string | null;
  /** Triple count reported by the most recent OWL import (UI badge). */
  lastImportTripleCount: number | null;
}

export const initialSchemaState: SchemaState = {
  mapping: null,
  summary: null,
  editorVocab: null,
  warnings: [],
  cacheStatus: "idle",
  cacheHit: false,
  lastFetchedAt: null,
  refreshing: false,
  analyzing: false,
  error: null,
  lastImportTripleCount: null,
};

export interface AppState {
  connection: ConnectionState;
  sparql: string;
  // Raw Turtle/OWL ontology text. Sent to /translate as
  // `ontology_ttl` (see `arango_sparql.service.models`). Stored as a
  // string rather than a parsed graph because the backend re-parses
  // it anyway and round-tripping JSON would lose whitespace /
  // comments the user sees in the editor.
  ontologyTtl: string;
  params: Record<string, unknown>;
  aql: string;
  bindVars: Record<string, unknown>;
  results: unknown[] | null;
  warnings: Array<{ message: string }>;
  activeResultTab: ResultTab;
  error: string | null;
  translating: boolean;
  executing: boolean;
  /** True while a /explain call is in flight. */
  explaining: boolean;
  /** True while a /profile call is in flight. */
  profiling: boolean;
  /** Raw ArangoDB explain plan from the last /explain (null until run). */
  explainPlan: Record<string, unknown> | null;
  /** Raw ArangoDB profile blob from the last /profile (null until run). */
  profileData: Record<string, unknown> | null;
  history: HistoryEntry[];
  translateMs: number | null;
  execMs: number | null;
  /** Schema-acquisition slice (PRD §6.4 wire shapes). */
  schema: SchemaState;
  // ---- Natural-language ("Ask") slice ----------------------------------
  /** True while a /nl-translate call is in flight. */
  generating: boolean;
  /** NL-specific error banner (kept separate from the deterministic
   *  translate/execute `error` so an NL failure doesn't blow away a
   *  good editor preview, mirroring the Cypher UI's `nlError`). */
  nlError: string | null;
  /** Telemetry for the last successful NL translation (status line). */
  nlInfo: NlInfo | null;
  /** Where the current editor SPARQL came from (status hinting). */
  sparqlSource: SparqlSource;
  /** Most recent NL question (for re-ask / status). */
  lastNlQuestion: string | null;
  /** De-duplicated history of NL questions (suggestions + recall). */
  nlHistory: string[];
  /** Session-scoped multi-turn transcript (WP-UI-SHELL Phase 4). */
  transcript: TranscriptTurn[];
}

const STORAGE_KEY = "sparql-workbench";


const MAX_HISTORY = 50;
const MAX_NL_HISTORY = 50;

function loadSavedState(): Partial<AppState> {
  try {
    // Under a control plane the editor, ontology and history belong to a project: they stay
    // in this tab, under its project (every tab on the origin shares localStorage).
    const { storage, key } = projectStore(STORAGE_KEY);
    const raw = storage.getItem(key);
    if (!raw) return {};
    const saved = JSON.parse(raw);
    return {
      sparql: saved.sparql ?? "",
      ontologyTtl: saved.ontologyTtl ?? "",
      params: saved.params ?? {},
      history: Array.isArray(saved.history) ? saved.history.slice(0, MAX_HISTORY) : [],
      nlHistory: Array.isArray(saved.nlHistory)
        ? saved.nlHistory.slice(0, MAX_NL_HISTORY)
        : [],
    };
  } catch {
    return {};
  }
}

function saveState(state: AppState) {
  try {
    const { storage, key } = projectStore(STORAGE_KEY);
    storage.setItem(
      key,
      JSON.stringify({
        sparql: state.sparql,
        ontologyTtl: state.ontologyTtl,
        params: state.params,
        history: state.history.slice(0, MAX_HISTORY),
        nlHistory: state.nlHistory.slice(0, MAX_NL_HISTORY),
      }),
    );
  } catch {
    // localStorage may be unavailable
  }
}

const DEFAULT_SPARQL = `PREFIX rdf:  <http://www.w3.org/1999/02/22-rdf-syntax-ns#>
PREFIX rdfs: <http://www.w3.org/2000/01/rdf-schema#>
PREFIX ex:   <http://example.org/>

SELECT ?person ?name
WHERE {
  ?person a ex:Person ;
          ex:name ?name .
}
LIMIT 100
`;

export const initialState: AppState = {
  connection: {
    status: "disconnected",
    token: null,
    url: "http://localhost:8529",
    database: "_system",
    username: "root",
    password: "",
    databases: [],
    error: null,
    platform: false,
  },
  sparql: DEFAULT_SPARQL,
  ontologyTtl: "",
  params: {},
  aql: "",
  bindVars: {},
  results: null,
  warnings: [],
  activeResultTab: "table",
  error: null,
  translating: false,
  executing: false,
  explaining: false,
  profiling: false,
  explainPlan: null,
  profileData: null,
  history: [],
  translateMs: null,
  execMs: null,
  schema: initialSchemaState,
  generating: false,
  nlError: null,
  nlInfo: null,
  sparqlSource: "user",
  lastNlQuestion: null,
  nlHistory: [],
  transcript: [],
};

export type Action =
  | { type: "SET_SPARQL"; sparql: string }
  | { type: "SET_ONTOLOGY_TTL"; ontologyTtl: string }
  | {
      type: "CONNECT_START";
      url: string;
      database: string;
      username: string;
    }
  | {
      type: "CONNECT_SUCCESS";
      token: string;
      databases: string[];
      url: string;
      database: string;
      username: string;
      password: string;
      platform?: boolean;
    }
  | { type: "CONNECT_ERROR"; error: string }
  | { type: "DISCONNECT" }
  | { type: "TRANSLATE_START" }
  | {
      type: "TRANSLATE_SUCCESS";
      aql: string;
      bindVars: Record<string, unknown>;
      warnings?: Array<{ message: string }>;
      translateMs?: number | null;
    }
  | { type: "TRANSLATE_ERROR"; error: string }
  | { type: "EXECUTE_START" }
  | { type: "EXECUTE_SUCCESS"; results: unknown[]; warnings?: Array<{ message: string }>; execMs?: number | null }
  | { type: "EXECUTE_ERROR"; error: string }
  | { type: "EXPLAIN_START" }
  | {
      type: "EXPLAIN_SUCCESS";
      plan: Record<string, unknown>;
      aql: string;
      bindVars: Record<string, unknown>;
      warnings?: Array<{ message: string }>;
      translateMs?: number | null;
    }
  | { type: "EXPLAIN_ERROR"; error: string }
  | { type: "PROFILE_START" }
  | {
      type: "PROFILE_SUCCESS";
      profile: Record<string, unknown>;
      results: unknown[];
      aql: string;
      bindVars: Record<string, unknown>;
      warnings?: Array<{ message: string }>;
      translateMs?: number | null;
      execMs?: number | null;
    }
  | { type: "PROFILE_ERROR"; error: string }
  | { type: "SET_RESULT_TAB"; tab: ResultTab }
  | { type: "CLEAR_ERROR" }
  | { type: "SET_PARAMS"; params: Record<string, unknown> }
  | { type: "ADD_HISTORY"; entry: HistoryEntry }
  | { type: "CLEAR_HISTORY" }
  | { type: "ADD_TRANSCRIPT_TURN"; turn: TranscriptTurn }
  | { type: "CLEAR_TRANSCRIPT" }
  | { type: "SCHEMA_REFRESH_START" }
  | { type: "SCHEMA_ANALYZING" }
  | {
      type: "SCHEMA_LOADED";
      mapping: Record<string, unknown> | null;
      summary: Record<string, unknown> | null;
      editorVocab: EditorVocab | null;
      warnings: SchemaWarning[];
      cacheHit: boolean;
    }
  | { type: "SCHEMA_REFRESH_ERROR"; error: string }
  | { type: "SCHEMA_STATUS_UPDATED"; status: SchemaCacheStatus }
  | { type: "SCHEMA_CACHE_INVALIDATED" }
  | { type: "SCHEMA_IMPORT_SUCCESS"; tripleCount: number }
  | { type: "NL_START"; question: string }
  | {
      type: "NL_SUCCESS";
      sparql: string;
      aql: string;
      bindVars: Record<string, unknown>;
      warnings?: Array<{ message: string }>;
      latencyMs: number;
      llmCalls: number;
      costUsd: number;
      repaired: boolean;
    }
  | { type: "NL_ERROR"; error: string }
  | { type: "CLEAR_NL_ERROR" };

// Prepend `value` to `list`, drop any prior duplicate, and cap length.
function prependUnique(list: string[], value: string, cap: number): string[] {
  const trimmed = value.trim();
  if (!trimmed) return list;
  return [trimmed, ...list.filter((v) => v !== trimmed)].slice(0, cap);
}

function reducer(state: AppState, action: Action): AppState {
  switch (action.type) {
    case "SET_SPARQL":
      // Hand-edits (and sample/history inserts) reset NL provenance so
      // the status line doesn't keep claiming the editor text came from
      // the pipeline once the user has touched it.
      return { ...state, sparql: action.sparql, sparqlSource: "user" };
    case "SET_ONTOLOGY_TTL":
      return { ...state, ontologyTtl: action.ontologyTtl };
    case "CONNECT_START":
      return {
        ...state,
        connection: {
          ...state.connection,
          status: "connecting",
          // Drop the previous token: on a DB switch the old session is about
          // to be closed, and the token+database-keyed effects (graph list,
          // auto-introspect) would otherwise re-fire against the new DB with
          // the dead token and 401 — which DISCONNECTs the new session.
          // Mirrors arango-cypher-py 51be876.
          token: null,
          url: action.url,
          database: action.database,
          username: action.username,
          error: null,
        },
      };
    case "CONNECT_SUCCESS":
      return {
        ...state,
        connection: {
          status: "connected",
          token: action.token,
          url: action.url,
          database: action.database,
          username: action.username,
          password: action.password,
          databases: action.databases,
          error: null,
          platform: action.platform ?? false,
        },
      };
    case "CONNECT_ERROR":
      return {
        ...state,
        connection: {
          ...state.connection,
          status: "disconnected",
          error: action.error,
        },
      };
    case "DISCONNECT":
      return {
        ...state,
        connection: {
          ...state.connection,
          status: "disconnected",
          token: null,
          databases: [],
          error: null,
          platform: false,
        },
        results: null,
        // Explain/profile are session-scoped (they hit a live DB); drop
        // them so a reconnect doesn't show the previous session's plan.
        explainPlan: null,
        profileData: null,
        // Clear the schema slice so a reconnect to a different DB
        // doesn't render the previous DB's mapping for one frame
        // before the new /schema/introspect lands.
        schema: initialSchemaState,
      };
    case "TRANSLATE_START":
      return { ...state, translating: true, error: null, translateMs: null };
    case "TRANSLATE_SUCCESS":
      return {
        ...state,
        translating: false,
        aql: action.aql,
        bindVars: action.bindVars,
        warnings: action.warnings ?? state.warnings,
        translateMs:
          action.translateMs !== undefined ? action.translateMs : state.translateMs,
        error: null,
      };
    case "TRANSLATE_ERROR":
      return { ...state, translating: false, error: action.error };
    case "EXECUTE_START":
      return { ...state, executing: true, error: null, execMs: null };
    case "EXECUTE_SUCCESS":
      return {
        ...state,
        executing: false,
        results: action.results,
        warnings: action.warnings ?? state.warnings,
        execMs: action.execMs ?? null,
        activeResultTab: "table",
        error: null,
      };
    case "EXECUTE_ERROR":
      return { ...state, executing: false, error: action.error };
    case "EXPLAIN_START":
      return { ...state, explaining: true, error: null, explainPlan: null };
    case "EXPLAIN_SUCCESS":
      return {
        ...state,
        explaining: false,
        explainPlan: action.plan,
        aql: action.aql,
        bindVars: action.bindVars,
        warnings: action.warnings ?? state.warnings,
        translateMs:
          action.translateMs !== undefined ? action.translateMs : state.translateMs,
        activeResultTab: "explain",
        error: null,
      };
    case "EXPLAIN_ERROR":
      return { ...state, explaining: false, error: action.error };
    case "PROFILE_START":
      return { ...state, profiling: true, error: null, profileData: null };
    case "PROFILE_SUCCESS":
      return {
        ...state,
        profiling: false,
        profileData: action.profile,
        results: action.results,
        aql: action.aql,
        bindVars: action.bindVars,
        warnings: action.warnings ?? state.warnings,
        translateMs:
          action.translateMs !== undefined ? action.translateMs : state.translateMs,
        execMs: action.execMs ?? null,
        activeResultTab: "profile",
        error: null,
      };
    case "PROFILE_ERROR":
      return { ...state, profiling: false, error: action.error };
    case "SET_RESULT_TAB":
      return { ...state, activeResultTab: action.tab };
    case "CLEAR_ERROR":
      return { ...state, error: null };
    case "SET_PARAMS":
      return { ...state, params: action.params };
    case "ADD_HISTORY": {
      const exists = state.history.some((h) => h.sparql === action.entry.sparql);
      const updated = exists
        ? [action.entry, ...state.history.filter((h) => h.sparql !== action.entry.sparql)]
        : [action.entry, ...state.history];
      return { ...state, history: updated.slice(0, MAX_HISTORY) };
    }
    case "CLEAR_HISTORY":
      return { ...state, history: [] };
    case "ADD_TRANSCRIPT_TURN":
      return { ...state, transcript: appendTurn(state.transcript, action.turn) };
    case "CLEAR_TRANSCRIPT":
      return { ...state, transcript: [] };
    case "SCHEMA_REFRESH_START":
      return {
        ...state,
        schema: { ...state.schema, refreshing: true, analyzing: false, error: null },
      };
    case "SCHEMA_ANALYZING":
      return {
        ...state,
        schema: { ...state.schema, refreshing: true, analyzing: true },
      };
    case "SCHEMA_LOADED":
      return {
        ...state,
        schema: {
          ...state.schema,
          mapping: action.mapping,
          summary: action.summary,
          editorVocab: action.editorVocab,
          warnings: action.warnings,
          cacheHit: action.cacheHit,
          // Drift status moves to "unchanged" on a fresh load — the
          // server-side cache and the just-loaded mapping share a
          // fingerprint by definition.
          cacheStatus: "unchanged",
          lastFetchedAt: Date.now(),
          refreshing: false,
          analyzing: false,
          error: null,
        },
      };
    case "SCHEMA_REFRESH_ERROR":
      return {
        ...state,
        schema: {
          ...state.schema,
          refreshing: false,
          analyzing: false,
          error: action.error,
        },
      };
    case "SCHEMA_STATUS_UPDATED":
      return {
        ...state,
        schema: { ...state.schema, cacheStatus: action.status },
      };
    case "SCHEMA_CACHE_INVALIDATED":
      return {
        ...state,
        schema: {
          ...state.schema,
          cacheStatus: "no_cache",
          cacheHit: false,
        },
      };
    case "SCHEMA_IMPORT_SUCCESS":
      return {
        ...state,
        schema: {
          ...state.schema,
          lastImportTripleCount: action.tripleCount,
        },
      };
    case "NL_START":
      return {
        ...state,
        generating: true,
        nlError: null,
        lastNlQuestion: action.question,
        nlHistory: prependUnique(state.nlHistory, action.question, MAX_NL_HISTORY),
      };
    case "NL_SUCCESS":
      return {
        ...state,
        generating: false,
        sparql: action.sparql,
        aql: action.aql,
        bindVars: action.bindVars,
        warnings: action.warnings ?? state.warnings,
        translateMs: action.latencyMs,
        sparqlSource: "nl_pipeline",
        nlError: null,
        nlInfo: {
          latencyMs: action.latencyMs,
          llmCalls: action.llmCalls,
          costUsd: action.costUsd,
          repaired: action.repaired,
        },
      };
    case "NL_ERROR":
      return { ...state, generating: false, nlError: action.error };
    case "CLEAR_NL_ERROR":
      return { ...state, nlError: null };
    default:
      return state;
  }
}

// Re-exported pure reducer so unit tests can drive state transitions
// without a React tree. Mirrors the Cypher UI's `__reducerForTest`.
export { reducer as __reducerForTest };
export { loadSavedState as __loadSavedStateForTest, saveState as __saveStateForTest };

export function useAppState() {
  // Saved state is read when the workbench mounts, not when this module loads: under a
  // control plane the page learns that first, and the saved state is then this tab's.
  const [state, dispatch] = useReducer(reducer, initialState, (s): AppState => ({
    ...s,
    ...loadSavedState(),
  }));

  const PERSIST_ACTIONS = new Set([
    "SET_SPARQL",
    "SET_ONTOLOGY_TTL",
    "SET_PARAMS",
    "ADD_HISTORY",
    "CLEAR_HISTORY",
    "NL_START",
  ]);

  const persistAndDispatch = useCallback(
    (action: Action) => {
      dispatch(action);
      if (PERSIST_ACTIONS.has(action.type)) {
        const next = reducer(state, action);
        saveState(next);
      }
    },
    [state],
  );

  return [state, persistAndDispatch] as const;
}
