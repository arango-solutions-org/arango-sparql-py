import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { EditorView } from "@codemirror/view";
import ConnectionDialog from "./components/ConnectionDialog";
import SparqlEditor from "./components/SparqlEditor";
import AqlEditor from "./components/AqlEditor";
import MappingPanel from "./components/MappingPanel";
import ParameterPanel from "./components/ParameterPanel";
import QueryHistory from "./components/QueryHistory";
import SampleQueries from "./components/SampleQueries";
import ClauseOutline from "./components/ClauseOutline";
import ResultsPanel from "./components/ResultsPanel";
import SchemaWarningBanner from "./components/SchemaWarningBanner";
import GraphSelector from "./components/GraphSelector";
import ChatComposer from "./components/ChatComposer";
import Transcript from "./components/Transcript";
import QueryInspector from "./components/QueryInspector";
import SettingsMenu from "./components/SettingsMenu";
import ReportProblem from "./components/ReportProblem";
import ResultAffordances from "./components/ResultAffordances";
import CommandPalette from "./components/CommandPalette";
import PrefixManager from "./components/PrefixManager";
import type { Command } from "./utils/commandPalette";
import { formatSparql, setSparqlSchemaContext, vocabForOntology } from "./lang/sparqlComplete";
import ThemeToggle from "./components/ThemeToggle";
import { t } from "./i18n";
import { useAppState } from "./api/store";
import {
  controlPlane,
  onSessionEnded,
  startControlPlane,
  type ControlPlaneStart,
} from "./api/controlPlane";
import {
  translateSparql,
  executeSparql,
  executeAql,
  explainSparql,
  profileSparql,
  nl2Sparql,
  suggestNlQueries,
  listGraphs,
  bindGraph,
  getMinedExamples,
  isAuthError,
  apiBaseFor,
  schemaIntrospectUntilReady,
  SchemaPollSuperseded,
  type GraphInfo,
  type SparqlExecuteRequest,
} from "./api/client";
import {
  planSend,
  currentStage,
  stageLabel,
  isBusy as pipelineBusy,
} from "./utils/pipeline";
import { resultAffordances, type AffordanceId } from "./utils/affordances";
import { examplesForGraph, mergeSuggestions, type MinedExample } from "./utils/minedExamples";

// App.tsx implements the chat-first "Query Workbench Shell" (PRD §10.0),
// mirroring `references/arango-cypher-py/ui/src/App.tsx`:
//
//   * L0 — ChatComposer (NL "Ask") + ResultsPanel are always visible.
//   * L1 — QueryInspector (SPARQL | AQL editors + power actions) is a
//     collapsible bottom drawer, closed by default.
//   * L2 — Ontology, outline, samples, history live behind the gear
//     (SettingsMenu); the ontology panel is closed by default.
//
// Backend surface used today:
//   * /nl-translate — Send's generate step (NL → SPARQL → AQL in one call)
//   * /translate    — inspector "Translate" for hand-written SPARQL
//   * /execute      — Send's run step + inspector "Run" (needs a session)
//   * /connect, /graphs, /session/graph, /schema/force-reacquire, /nl-samples
//   * /explain, /profile — inspector "Explain"/"Profile" + result chips
//     (WP-UI-EXPLAIN); both need a live session and render as conditional
//     tabs in the results panel.

function loadBool(key: string, fallback: boolean): boolean {
  try {
    const raw = localStorage.getItem(key);
    return raw == null ? fallback : raw === "true";
  } catch {
    return fallback;
  }
}

/** The page: under a control plane it first adopts this tab's context, and shows why it
 * cannot open (no context, or the session ended) instead of the workbench. */
export default function App() {
  const [start, setStart] = useState<ControlPlaneStart | "loading">("loading");
  const [ended, setEnded] = useState(false);
  useEffect(() => {
    let live = true;
    onSessionEnded(() => live && setEnded(true));
    void startControlPlane(apiBaseFor(window.location.pathname)).then((s) => live && setStart(s));
    return () => {
      live = false;
      onSessionEnded(null);
    };
  }, []);
  if (start === "loading") {
    return (
      <div className="h-screen flex items-center justify-center bg-gray-950 text-gray-400 text-sm">
        {t("controlPlane.loading")}
      </div>
    );
  }
  if (start === "no-context") {
    return <ControlPlaneGate title={t("controlPlane.noContext.title")} text={t("controlPlane.noContext.text")} />;
  }
  if (ended) {
    return <ControlPlaneGate title={t("controlPlane.ended.title")} text={t("controlPlane.ended.text")} />;
  }
  return <Workbench />;
}

function ControlPlaneGate({ title, text }: { title: string; text: string }) {
  return (
    <main className="h-screen flex items-center justify-center bg-gray-950 text-gray-100 p-4">
      <div role="alert" className="max-w-md flex flex-col gap-3">
        <h1 className="text-lg font-semibold">{title}</h1>
        <p className="text-sm text-gray-400">{text}</p>
        <a
          href={controlPlane.home}
          target="_top"
          className="w-fit px-3 py-1.5 text-sm rounded bg-indigo-600 hover:bg-indigo-500 text-white"
        >
          {t("controlPlane.home")}
        </a>
      </div>
    </main>
  );
}

function Workbench() {
  const [state, dispatch] = useAppState();
  // L2 panels — progressive disclosure: all closed by default so a hard
  // refresh returns to the clean L0 (composer + results) surface.
  const [showMapping, setShowMapping] = useState(false);
  const [mappingWidth, setMappingWidth] = useState(320);
  const [showHistory, setShowHistory] = useState(false);
  const [showSamples, setShowSamples] = useState(false);
  const [showOutline, setShowOutline] = useState(false);
  const [showPalette, setShowPalette] = useState(false);
  // "Report a problem" (PRD §10.19): which error the dialog files — the query
  // banner's, the NL banner's, or (from the settings menu) whichever is shown.
  const [reportSource, setReportSource] = useState<"query" | "nl" | "menu" | null>(null);
  const [showTranscript, setShowTranscript] = useState(() =>
    loadBool("show_transcript", false),
  );
  // L1 inspector — closed by default; per-pane visibility persists.
  const [showInspector, setShowInspector] = useState(false);
  const [sparqlPaneOpen, setSparqlPaneOpen] = useState(() =>
    loadBool("qi_sparql_open", true),
  );
  const [aqlPaneOpen, setAqlPaneOpen] = useState(() =>
    loadBool("qi_aql_open", true),
  );
  const [autoOpenOnError, setAutoOpenOnError] = useState(() =>
    loadBool("qi_auto_open_error", true),
  );
  const [nlInput, setNlInput] = useState("");
  const [nlSamples, setNlSamples] = useState<string[]>([]);
  // Verified examples mined from this database's own saved queries (PRD §7.7);
  // fetched once per connection and filtered by the selected graph on the
  // client, so a graph switch never races the server-side graph binding.
  // Tagged with the connection they were loaded for, so a reconnect or a
  // database switch shows none until its own arrive (no stale examples).
  const [mined, setMined] = useState<{ key: string; examples: MinedExample[] } | null>(null);
  const [graphs, setGraphs] = useState<GraphInfo[]>([]);
  const [graphScope, setGraphScope] = useState<string | null>(null);
  const [graphBusy, setGraphBusy] = useState(false);
  const [graphError, setGraphError] = useState<string | null>(null);
  const sparqlViewRef = useRef<EditorView | null>(null);

  useEffect(() => {
    localStorage.setItem("qi_sparql_open", String(sparqlPaneOpen));
  }, [sparqlPaneOpen]);
  useEffect(() => {
    localStorage.setItem("qi_aql_open", String(aqlPaneOpen));
  }, [aqlPaneOpen]);
  useEffect(() => {
    localStorage.setItem("qi_auto_open_error", String(autoOpenOnError));
  }, [autoOpenOnError]);
  useEffect(() => {
    localStorage.setItem("show_transcript", String(showTranscript));
  }, [showTranscript]);

  const dragRef = useRef<{ startX: number; startW: number } | null>(null);
  useEffect(() => {
    const onMouseMove = (e: MouseEvent) => {
      if (!dragRef.current) return;
      const delta = e.clientX - dragRef.current.startX;
      setMappingWidth(Math.max(240, Math.min(800, dragRef.current.startW + delta)));
    };
    const onMouseUp = () => {
      dragRef.current = null;
      document.body.style.cursor = "";
      document.body.style.userSelect = "";
    };
    window.addEventListener("mousemove", onMouseMove);
    window.addEventListener("mouseup", onMouseUp);
    return () => {
      window.removeEventListener("mousemove", onMouseMove);
      window.removeEventListener("mouseup", onMouseUp);
    };
  }, []);

  const sparqlRef = useRef(state.sparql);
  sparqlRef.current = state.sparql;
  const ontologyRef = useRef(state.ontologyTtl);
  ontologyRef.current = state.ontologyTtl;

  // Schema-derived "Ask" suggestions (POST /nl-samples). Re-fetched only
  // when the schema source actually changes — keyed on (url, db,
  // ontology) — so editing an unrelated bit of state doesn't spam the
  // endpoint. Rule-based on the backend, so this works without an LLM
  // provider; failures are swallowed (suggestions are best-effort).
  const lastSampleKeyRef = useRef<string | null>(null);
  useEffect(() => {
    const ttl = state.ontologyTtl.trim();
    if (!ttl) {
      lastSampleKeyRef.current = null;
      setNlSamples([]);
      return;
    }
    const key = `${state.connection.url}||${state.connection.database}||${ttl.length}:${ttl.slice(0, 256)}`;
    if (lastSampleKeyRef.current === key) return;
    lastSampleKeyRef.current = key;
    let cancelled = false;
    void (async () => {
      try {
        const resp = await suggestNlQueries(ttl, 8, true);
        if (!cancelled) setNlSamples(resp.queries ?? []);
      } catch {
        // Best-effort: leave whatever samples we already have.
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [state.ontologyTtl, state.connection.url, state.connection.database]);

  // Mined examples for the connected database. A database never mined, or a
  // user who cannot read the collection, simply has none; the rest of the UI
  // is unaffected.
  const minedKey = state.connection.token ? `${state.connection.token}|${state.connection.database}` : null;
  useEffect(() => {
    const token = state.connection.token;
    if (!token || !minedKey) return;
    let cancelled = false;
    getMinedExamples(token)
      .then((resp) => {
        if (!cancelled) setMined({ key: minedKey, examples: resp.examples ?? [] });
      })
      .catch((err) => {
        console.warn("Loading mined examples failed:", err);
        if (!cancelled) setMined({ key: minedKey, examples: [] });
      });
    return () => {
      cancelled = true;
    };
  }, [minedKey, state.connection.token]);
  const minedExamples = useMemo(
    () => (mined !== null && mined.key === minedKey ? mined.examples : []),
    [mined, minedKey],
  );

  // Load the connected database's named graphs so the scope picker can
  // offer them. Resets the scope on every (re)connect. Best-effort: a DB
  // with no graphs yields an empty list and the picker stays hidden.
  useEffect(() => {
    const token = state.connection.token;
    if (!token) {
      setGraphs([]);
      setGraphScope(null);
      setGraphError(null);
      return;
    }
    let cancelled = false;
    void (async () => {
      try {
        const resp = await listGraphs(token);
        if (!cancelled) {
          setGraphs(resp.graphs ?? []);
          setGraphScope(null);
          setGraphError(null);
        }
      } catch (err) {
        if (!cancelled) {
          setGraphs([]);
          setGraphError(err instanceof Error ? err.message : String(err));
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [state.connection.token, state.connection.url, state.connection.database]);
  const paramsRef = useRef(state.params);
  paramsRef.current = state.params;

  // The request of the last SPARQL run, which the graph view runs again with every variable
  // and the query's patterns (`graph: true`, utils/graphShape.ts).
  const [lastRun, setLastRun] = useState<SparqlExecuteRequest | null>(null);
  const loadGraph = useMemo(() => {
    const token = state.connection.token;
    if (lastRun === null || !token) return undefined;
    return () => executeSparql({ ...lastRun, graph: true }, token);
  }, [lastRun, state.connection.token]);

  const buildRequest = useCallback(() => {
    const p = paramsRef.current;
    return {
      sparql: sparqlRef.current,
      ontology_ttl: ontologyRef.current || undefined,
      params: Object.keys(p).length > 0 ? p : undefined,
    };
  }, []);

  const addToHistory = useCallback(
    (aqlPreview: string) => {
      const sparql = sparqlRef.current.trim();
      if (!sparql) return;
      dispatch({
        type: "ADD_HISTORY",
        entry: {
          sparql,
          timestamp: Date.now(),
          aqlPreview: aqlPreview.slice(0, 120),
        },
      });
    },
    [dispatch],
  );

  const handleMaybeAuthError = useCallback(
    (err: unknown) => {
      if (isAuthError(err)) {
        dispatch({ type: "DISCONNECT" });
      }
    },
    [dispatch],
  );

  const handleTranslate = useCallback(async () => {
    if (!sparqlRef.current.trim()) return;
    dispatch({ type: "TRANSLATE_START" });
    try {
      const resp = await translateSparql(buildRequest());
      dispatch({
        type: "TRANSLATE_SUCCESS",
        aql: resp.aql,
        bindVars: resp.bind_vars,
        warnings: resp.warnings,
        translateMs: resp.elapsed_ms ?? null,
      });
      addToHistory(resp.aql);
    } catch (err) {
      dispatch({
        type: "TRANSLATE_ERROR",
        error: err instanceof Error ? err.message : String(err),
      });
      handleMaybeAuthError(err);
    }
  }, [dispatch, buildRequest, addToHistory, handleMaybeAuthError]);

  // Re-read the schema for the session's current scope. ``force`` (the
  // "Refresh schema" button) bypasses the cache; a graph pick re-reads the
  // cached scoped bundle. Both go through GET /schema/introspect with
  // include_owl so the ontology editor — which drives translation and NL —
  // follows the new scope. (POST /schema/force-reacquire never returned OWL,
  // so graph scoping used to have no effect on translation.) Mirrors
  // arango-cypher-py: Refresh = introspect?force=true, graph pick = unforced.
  const handleRefreshSchema = useCallback(async (force: boolean = true) => {
    if (!state.connection.token) return;
    dispatch({ type: "SCHEMA_REFRESH_START" });
    try {
      const resp = await schemaIntrospectUntilReady(
        state.connection.token,
        { force, include_owl: true, include_statistics: true },
        { onAnalyzing: () => dispatch({ type: "SCHEMA_ANALYZING" }) },
      );
      if (resp.status === "pending") {
        dispatch({ type: "SCHEMA_REFRESH_ERROR", error: t("schema.stillAnalyzing") });
        return;
      }
      dispatch({
        type: "SCHEMA_LOADED",
        mapping: resp.mapping ?? null,
        summary: resp.summary ?? null,
        editorVocab: resp.editor_vocab ?? null,
        warnings: (resp.warnings ?? []).map((w) => ({
          code: w.code,
          message: w.message,
          install_hint: w.install_hint,
          severity: w.severity,
        })),
        cacheHit: !!resp.cache_hit,
      });
      // If the analyzer emitted inline OWL, prefill the editor so
      // the user sees the freshly-acquired ontology without
      // hand-import. Both spellings honoured (camelCase from the
      // wire, snake_case from a Python-emitted bundle).
      const mapping = resp.mapping as Record<string, unknown> | undefined;
      const owl =
        (mapping?.owlTurtle as string | undefined) ??
        (mapping?.owl_turtle as string | undefined);
      if (typeof owl === "string" && owl.trim().length > 0) {
        dispatch({ type: "SET_ONTOLOGY_TTL", ontologyTtl: owl });
      }
    } catch (err) {
      // A newer refresh / DB switch / disconnect took over: drop this result.
      if (err instanceof SchemaPollSuperseded) return;
      dispatch({
        type: "SCHEMA_REFRESH_ERROR",
        error: err instanceof Error ? err.message : String(err),
      });
      handleMaybeAuthError(err);
    }
  }, [dispatch, state.connection.token, handleMaybeAuthError]);

  // Bind (or clear) the named-graph scope, then re-acquire the schema so
  // the ontology / mapping / NL suggestions reflect the narrowed set of
  // collections. `null` clears the scope back to "all collections".
  const handleSelectGraph = useCallback(
    async (graphName: string | null) => {
      const token = state.connection.token;
      if (!token) return;
      setGraphBusy(true);
      setGraphError(null);
      try {
        await bindGraph(graphName, token);
        setGraphScope(graphName);
        await handleRefreshSchema(false);
      } catch (err) {
        setGraphError(err instanceof Error ? err.message : String(err));
        handleMaybeAuthError(err);
      } finally {
        setGraphBusy(false);
      }
    },
    [state.connection.token, handleRefreshSchema, handleMaybeAuthError],
  );

  const handleExecute = useCallback(async () => {
    if (!state.connection.token) return;
    if (!sparqlRef.current.trim()) return;
    dispatch({ type: "EXECUTE_START" });
    const req = buildRequest();
    try {
      const resp = await executeSparql(req, state.connection.token);
      if (resp.aql) {
        dispatch({
          type: "TRANSLATE_SUCCESS",
          aql: resp.aql,
          bindVars: resp.bind_vars ?? {},
          warnings: resp.warnings,
          translateMs: null,
        });
      }
      dispatch({
        type: "EXECUTE_SUCCESS",
        results: resp.bindings,
        warnings: resp.warnings,
        execMs: resp.elapsed_ms ?? null,
      });
      setLastRun(req); // the graph view draws this run, not whatever the editor holds now
      if (resp.aql) addToHistory(resp.aql);
    } catch (err) {
      dispatch({
        type: "EXECUTE_ERROR",
        error: err instanceof Error ? err.message : String(err),
      });
      handleMaybeAuthError(err);
    }
  }, [
    dispatch,
    buildRequest,
    addToHistory,
    state.connection.token,
    handleMaybeAuthError,
  ]);

  // Run the live AQL document directly via /execute-aql (WP-UI-AQL).
  // Bypasses the SPARQL→AQL step so the user can hand-edit the transpiled
  // AQL and re-run it. Reuses the EXECUTE_* channel; the current bind
  // vars ride along unchanged.
  const handleRunAql = useCallback(
    async (aql: string) => {
      if (!state.connection.token) return;
      if (!aql.trim()) return;
      dispatch({ type: "EXECUTE_START" });
      try {
        const resp = await executeAql(aql, state.bindVars, state.connection.token);
        setLastRun(null); // raw AQL has no patterns: the graph view draws the rows
        dispatch({
          type: "EXECUTE_SUCCESS",
          results: resp.results,
          warnings: resp.warnings,
          execMs: resp.exec_ms ?? null,
        });
      } catch (err) {
        dispatch({
          type: "EXECUTE_ERROR",
          error: err instanceof Error ? err.message : String(err),
        });
        handleMaybeAuthError(err);
      }
    },
    [dispatch, state.connection.token, state.bindVars, handleMaybeAuthError],
  );

  // Map backend warning dicts ({message?, code?}) to the UI's
  // {message} shape, dropping empty ones. Shared by explain/profile.
  const toWarnings = useCallback(
    (raw: Array<{ message?: string; code?: string }> | undefined) =>
      (raw ?? [])
        .map((w) => ({ message: String(w.message ?? "") }))
        .filter((w) => w.message.length > 0),
    [],
  );

  // WP-UI-EDITOR / issue #68: the SPARQL completion runs on the editor
  // vocabulary /schema/introspect serves — built server-side from the same
  // per-class list the NL prompt is grounded on, so completion never offers a
  // property on a class the model would not be told owns it.
  // Rebased onto the ontology NL requests carry, as the NL route reads it.
  useEffect(() => {
    setSparqlSchemaContext(vocabForOntology(state.schema.editorVocab, state.ontologyTtl));
  }, [state.schema.editorVocab, state.ontologyTtl]);

  // Pretty-print with the shared formatter (the fabric demo's Format button
  // runs the same one). It returns a query with # comments unchanged.
  const formattedSparql = useMemo(() => formatSparql(state.sparql), [state.sparql]);
  const canFormatSparql = formattedSparql !== state.sparql;
  const handleFormatSparql = useCallback(() => {
    if (canFormatSparql) dispatch({ type: "SET_SPARQL", sparql: formattedSparql });
  }, [canFormatSparql, formattedSparql, dispatch]);

  // POST /explain — transpile + fetch the AQL execution plan (no rows).
  // Surfaces as the "Explain" result tab.
  const handleExplain = useCallback(async () => {
    if (!state.connection.token) return;
    if (!sparqlRef.current.trim()) return;
    dispatch({ type: "EXPLAIN_START" });
    try {
      const resp = await explainSparql(buildRequest(), state.connection.token);
      dispatch({
        type: "EXPLAIN_SUCCESS",
        plan: resp.plan ?? {},
        aql: resp.aql,
        bindVars: resp.bind_vars ?? {},
        warnings: toWarnings(resp.warnings),
        translateMs: resp.translate_ms ?? null,
      });
      addToHistory(resp.aql);
    } catch (err) {
      dispatch({
        type: "EXPLAIN_ERROR",
        error: err instanceof Error ? err.message : String(err),
      });
      handleMaybeAuthError(err);
    }
  }, [
    dispatch,
    buildRequest,
    addToHistory,
    toWarnings,
    state.connection.token,
    handleMaybeAuthError,
  ]);

  // POST /profile — transpile + execute with profile=2, returning rows +
  // per-stage timings. Surfaces as the "Profile" result tab; the rows
  // also populate the Table/JSON/Graph tabs.
  const handleProfile = useCallback(async () => {
    if (!state.connection.token) return;
    if (!sparqlRef.current.trim()) return;
    dispatch({ type: "PROFILE_START" });
    try {
      const resp = await profileSparql(buildRequest(), state.connection.token);
      dispatch({
        type: "PROFILE_SUCCESS",
        profile: resp.profile ?? {},
        results: resp.bindings ?? [],
        aql: resp.aql,
        bindVars: resp.bind_vars ?? {},
        warnings: toWarnings(resp.warnings),
        translateMs: resp.translate_ms ?? null,
        execMs: resp.exec_ms ?? null,
      });
      addToHistory(resp.aql);
    } catch (err) {
      dispatch({
        type: "PROFILE_ERROR",
        error: err instanceof Error ? err.message : String(err),
      });
      handleMaybeAuthError(err);
    }
  }, [
    dispatch,
    buildRequest,
    addToHistory,
    toWarnings,
    state.connection.token,
    handleMaybeAuthError,
  ]);

  // Natural-language generate step. One /nl-translate call returns BOTH
  // the SPARQL (shown in the editor) and the ready-to-run AQL preview.
  // Returns the generated SPARQL on success (and updates `sparqlRef`
  // synchronously so a chained Send → Run reads the fresh query), or
  // null on empty input / failure.
  const runNL = useCallback(
    async (question: string): Promise<string | null> => {
      const q = question.trim();
      if (!q) return null;
      dispatch({ type: "NL_START", question: q });
      try {
        const resp = await nl2Sparql(q, {
          ontologyTtl: ontologyRef.current || undefined,
        });
        dispatch({
          type: "NL_SUCCESS",
          sparql: resp.sparql,
          aql: resp.aql,
          bindVars: resp.bind_vars,
          warnings: (resp.warnings ?? [])
            .map((w) => ({
              message: String((w as { message?: unknown }).message ?? ""),
            }))
            .filter((w) => w.message.length > 0),
          latencyMs: resp.latency_ms,
          llmCalls: resp.llm_calls,
          costUsd: resp.cost_usd,
          repaired: resp.repaired,
        });
        // Keep the ref in lock-step with the reducer so a chained
        // execute() in handleSend sees the just-generated query.
        sparqlRef.current = resp.sparql;
        if (resp.sparql.trim()) {
          dispatch({
            type: "ADD_HISTORY",
            entry: {
              sparql: resp.sparql,
              timestamp: Date.now(),
              aqlPreview: resp.aql.slice(0, 120),
            },
          });
        }
        return resp.sparql;
      } catch (err) {
        dispatch({
          type: "NL_ERROR",
          error: err instanceof Error ? err.message : String(err),
        });
        handleMaybeAuthError(err);
        return null;
      }
    },
    [dispatch, handleMaybeAuthError],
  );

  // The Send pipeline (PRD §10.0): always generate + transpile; run only
  // when connected. `planSend` encodes the degradation contract.
  const handleSend = useCallback(async () => {
    const question = nlInput.trim();
    if (!question) return;
    const intent = planSend(state.connection.status === "connected");
    const generated = await runNL(question);
    // Record the turn (WP-UI-SHELL Phase 4). Kept regardless of the
    // transcript panel's visibility so toggling it on reveals prior turns.
    dispatch({
      type: "ADD_TRANSCRIPT_TURN",
      turn: {
        id: `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
        question,
        sparql: generated,
        ok: !!(generated && generated.trim()),
        timestamp: Date.now(),
      },
    });
    if (generated && generated.trim() && intent.run) {
      await handleExecute();
    }
  }, [nlInput, state.connection.status, runNL, handleExecute, dispatch]);

  // Reload an earlier turn's SPARQL into the editor.
  const handleSelectTurn = useCallback(
    (sparql: string) => {
      dispatch({ type: "SET_SPARQL", sparql });
      sparqlRef.current = sparql;
    },
    [dispatch],
  );

  const handleJumpToLine = useCallback((line: number) => {
    const view = sparqlViewRef.current;
    if (!view) return;
    const lineInfo = view.state.doc.line(Math.min(line, view.state.doc.lines));
    view.dispatch({
      selection: { anchor: lineInfo.from },
      scrollIntoView: true,
    });
    view.focus();
  }, []);

  const isConnected = state.connection.status === "connected";
  const pipelineFlags = {
    nlLoading: state.generating,
    translating: state.translating,
    executing: state.executing,
  };
  const busy = pipelineBusy(pipelineFlags);
  const stage = currentStage(pipelineFlags);

  // Screen-reader status announcement (PRD §10.10 aria-live row): a single
  // polite message reflecting the current pipeline stage or the latest
  // error so non-visual users hear progress/failures without polling the UI.
  const statusAnnouncement = useMemo(() => {
    if (state.error) return t("status.error", { message: state.error });
    if (state.nlError) return t("status.error", { message: state.nlError });
    if (stage === "generating") return t("status.thinking");
    if (stage === "transpiling") return t("status.translating");
    if (stage === "running") return t("status.executing");
    if (state.explaining) return t("status.explaining");
    if (state.profiling) return t("status.profiling");
    return "";
  }, [state.error, state.nlError, stage, state.explaining, state.profiling]);

  // Reveal the inspector when a query fails so the user can see/fix the
  // offending SPARQL or AQL (toggleable via the gear, default on).
  useEffect(() => {
    if (autoOpenOnError && (state.error || state.nlError)) {
      setShowInspector(true);
    }
  }, [autoOpenOnError, state.error, state.nlError]);

  // At least one editor pane must stay open while the inspector is shown.
  const toggleSparqlPane = useCallback(() => {
    setSparqlPaneOpen((v) => (v && !aqlPaneOpen ? v : !v));
  }, [aqlPaneOpen]);
  const toggleAqlPane = useCallback(() => {
    setAqlPaneOpen((v) => (v && !sparqlPaneOpen ? v : !v));
  }, [sparqlPaneOpen]);

  // Per-result affordance chips: jump to a power surface without a
  // permanent toolbar. "View SPARQL"/"View AQL" reveal the L1 inspector
  // focused on a pane; "Graph" switches the results tab; "Explain"/
  // "Profile" run the corresponding backend call (WP-UI-EXPLAIN).
  const handleAffordance = useCallback(
    (id: AffordanceId) => {
      if (id === "graph") {
        dispatch({ type: "SET_RESULT_TAB", tab: "graph" });
        return;
      }
      if (id === "explain") {
        void handleExplain();
        return;
      }
      if (id === "profile") {
        void handleProfile();
        return;
      }
      setShowInspector(true);
      if (id === "sparql") {
        setSparqlPaneOpen(true);
        requestAnimationFrame(() => sparqlViewRef.current?.focus());
      } else {
        setAqlPaneOpen(true);
      }
    },
    [dispatch, handleExplain, handleProfile],
  );

  const affordances = resultAffordances({
    hasResults: state.results !== null,
    sparqlPresent: state.sparql.trim().length > 0,
    aqlPresent: state.aql.trim().length > 0,
    inspectorOpen: showInspector,
    activeTab: state.activeResultTab,
    connected: isConnected,
  });

  // The inspector's power actions are disabled while any of the pipeline
  // *or* explain/profile calls are in flight.
  const inspectorBusy = busy || state.explaining || state.profiling;

  // Command palette (Mod-K, §10.7). Toggling here mirrors every action
  // reachable from the gear menu — per ui-architecture.mdc rule 19 the
  // palette accelerates rather than replaces the menu.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && (e.key === "k" || e.key === "K")) {
        e.preventDefault();
        setShowPalette((v) => !v);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const commands = useMemo<Command[]>(() => {
    const hasSparql = state.sparql.trim().length > 0;
    return [
      {
        id: "translate",
        title: "Translate to AQL",
        section: "Query",
        keywords: "transpile sparql aql",
        enabled: hasSparql,
        run: () => void handleTranslate(),
      },
      {
        id: "run",
        title: "Run query",
        section: "Query",
        keywords: "execute",
        enabled: isConnected && hasSparql,
        run: () => void handleExecute(),
      },
      {
        id: "explain",
        title: "Explain query",
        section: "Query",
        keywords: "plan execution cost",
        enabled: isConnected && hasSparql,
        run: () => void handleExplain(),
      },
      {
        id: "profile",
        title: "Profile query",
        section: "Query",
        keywords: "timing performance stages",
        enabled: isConnected && hasSparql,
        run: () => void handleProfile(),
      },
      {
        id: "inspector",
        title: showInspector ? "Hide query inspector" : "Show query inspector",
        section: "Panels",
        keywords: "sparql aql editor drawer",
        enabled: true,
        run: () => setShowInspector((v) => !v),
      },
      {
        id: "ontology",
        title: showMapping ? "Hide ontology panel" : "Show ontology panel",
        section: "Panels",
        keywords: "mapping turtle owl schema",
        enabled: true,
        run: () => setShowMapping((v) => !v),
      },
      {
        id: "outline",
        title: showOutline ? "Hide clause outline" : "Show clause outline",
        section: "Panels",
        keywords: "clauses sparql",
        enabled: true,
        run: () => setShowOutline((v) => !v),
      },
      {
        id: "samples",
        title: "Open sample queries",
        section: "Panels",
        keywords: "examples",
        enabled: true,
        run: () => setShowSamples(true),
      },
      {
        id: "history",
        title: "Open query history",
        section: "Panels",
        keywords: "recent",
        enabled: state.history.length > 0,
        run: () => setShowHistory(true),
      },
      {
        id: "tab-table",
        title: "Show results: Table",
        section: "View",
        enabled: true,
        run: () => dispatch({ type: "SET_RESULT_TAB", tab: "table" }),
      },
      {
        id: "tab-json",
        title: "Show results: JSON",
        section: "View",
        enabled: true,
        run: () => dispatch({ type: "SET_RESULT_TAB", tab: "json" }),
      },
      {
        id: "tab-graph",
        title: "Show results: Graph",
        section: "View",
        enabled: true,
        run: () => dispatch({ type: "SET_RESULT_TAB", tab: "graph" }),
      },
      {
        id: "refresh",
        title: "Refresh schema",
        section: "Schema",
        keywords: "reacquire introspect mapping",
        enabled: isConnected && !state.schema.refreshing,
        run: () => void handleRefreshSchema(),
      },
    ];
  }, [
    state.sparql,
    state.history.length,
    state.schema.refreshing,
    isConnected,
    showInspector,
    showMapping,
    showOutline,
    handleTranslate,
    handleExecute,
    handleExplain,
    handleProfile,
    handleRefreshSchema,
    dispatch,
  ]);

  // Ask-bar suggestions: recent questions first, then the database's verified
  // (mined) examples for the selected graph, then schema-derived examples
  // (POST /nl-samples), de-duplicated case-insensitively.
  const scopedExamples = useMemo(
    () => examplesForGraph(minedExamples, graphScope),
    [minedExamples, graphScope],
  );
  const nlSuggestions = mergeSuggestions(state.nlHistory, scopedExamples, nlSamples);

  const sparqlPane = (
    <div className="flex flex-col h-full min-h-0">
      <div className="px-3 py-1.5 bg-gray-900/30 border-b border-gray-800 flex items-center gap-2">
        <span className="text-xs font-medium text-gray-400 uppercase tracking-wide">
          SPARQL
        </span>
        <div className="flex-1" />
        <button
          type="button"
          onClick={handleFormatSparql}
          disabled={!canFormatSparql}
          className="text-[10px] px-2 py-0.5 rounded border border-gray-700 text-gray-400 hover:text-gray-200 hover:border-gray-600 disabled:opacity-40 disabled:hover:text-gray-400 disabled:hover:border-gray-700"
          title={canFormatSparql ? t("editor.formatTitle") : t("editor.formatNothing")}
        >
          {t("action.format")}
        </button>
        <PrefixManager
          value={state.sparql}
          onChange={(v) => dispatch({ type: "SET_SPARQL", sparql: v })}
        />
      </div>
      <div className="flex-1 min-h-0 flex">
        <div className="flex-1 min-w-0">
          <SparqlEditor
            value={state.sparql}
            onChange={(v) => dispatch({ type: "SET_SPARQL", sparql: v })}
            onTranslate={handleTranslate}
            onExecute={handleExecute}
            onExplain={handleExplain}
            onProfile={handleProfile}
            onFormat={handleFormatSparql}
            viewRef={sparqlViewRef}
          />
        </div>
        {showOutline && (
          <div className="w-48 border-l border-gray-800 overflow-y-auto bg-gray-900/30 shrink-0">
            <div className="px-3 py-1.5 border-b border-gray-800">
              <span className="text-[10px] font-semibold text-gray-500 uppercase tracking-wide">
                Clause Outline
              </span>
            </div>
            <ClauseOutline sparql={state.sparql} onJumpToLine={handleJumpToLine} />
          </div>
        )}
      </div>
      <ParameterPanel
        sparql={state.sparql}
        params={state.params}
        onChange={(p) => dispatch({ type: "SET_PARAMS", params: p })}
      />
    </div>
  );

  const aqlPane = (
    <div className="flex flex-col h-full min-h-0">
      <div className="px-3 py-1.5 bg-gray-900/30 border-b border-gray-800 flex items-center gap-2">
        <span className="text-xs font-medium text-gray-400 uppercase tracking-wide">
          AQL
        </span>
        <div className="flex-1" />
        {state.translateMs != null && (
          <span className="text-[10px] text-emerald-500/70 tabular-nums">
            SPARQL&rarr;AQL {state.translateMs}ms
          </span>
        )}
        {state.execMs != null && (
          <span className="text-[10px] text-sky-400/70 tabular-nums">
            AQL exec {state.execMs}ms
          </span>
        )}
      </div>
      {state.warnings.length > 0 && (
        <div className="px-3 py-1.5 bg-amber-900/20 border-b border-amber-800/30 space-y-0.5">
          {state.warnings.map((w, i) => (
            <div key={i} className="flex items-start gap-2">
              <span className="text-amber-500 text-xs mt-0.5 shrink-0">
                &#9888;
              </span>
              <span className="text-xs text-amber-400">{w.message}</span>
            </div>
          ))}
        </div>
      )}
      <div className="flex-1 min-h-0">
        <AqlEditor
          value={state.aql}
          bindVars={state.bindVars}
          error={null}
          mapping={state.schema.mapping ?? undefined}
          onRun={handleRunAql}
          running={state.executing}
          canRun={isConnected}
        />
      </div>
    </div>
  );

  return (
    <div className="h-screen flex flex-col bg-gray-950 text-gray-100">
      <div role="status" aria-live="polite" className="sr-only">
        {statusAnnouncement}
      </div>
      <header className="flex items-center justify-between px-4 py-2 bg-gray-900 border-b border-gray-800">
        <div className="flex items-center gap-3">
          <h1 className="text-sm font-semibold text-gray-50 tracking-tight">
            {t("app.title")}
          </h1>
          <span className="text-gray-600 text-xs">|</span>
          <ConnectionDialog
            connection={state.connection}
            introspecting={state.schema.refreshing}
            analyzing={state.schema.analyzing}
            onRefreshSchema={() => void handleRefreshSchema(true)}
            dispatch={dispatch}
            onSchemaLoaded={(turtle) => {
              if (turtle) {
                dispatch({ type: "SET_ONTOLOGY_TTL", ontologyTtl: turtle });
              }
            }}
          />
          {isConnected && state.schema.cacheHit && !state.schema.refreshing && (
            <span
              className="text-[10px] text-gray-500 tabular-nums"
              title="Last /schema/introspect was served from the L1 cache"
            >
              cached
            </span>
          )}
          {state.schema.lastImportTripleCount != null && (
            <span
              className="text-[10px] text-gray-500 tabular-nums"
              title="Triples in the most recently imported OWL ontology"
            >
              {state.schema.lastImportTripleCount} triples
            </span>
          )}
        </div>
        <div className="flex items-center gap-2">
          {isConnected && (
            <GraphSelector
              graphs={graphs}
              selection={graphScope}
              loading={graphBusy}
              onSelect={handleSelectGraph}
              error={graphError}
            />
          )}
          <ThemeToggle />
          <SettingsMenu
            showMapping={showMapping}
            onToggleMapping={() => setShowMapping((v) => !v)}
            showOutline={showOutline}
            onToggleOutline={() => setShowOutline((v) => !v)}
            showTranscript={showTranscript}
            onToggleTranscript={() => setShowTranscript((v) => !v)}
            onOpenSamples={() => setShowSamples(true)}
            onOpenHistory={() => setShowHistory(true)}
            onOpenPalette={() => setShowPalette(true)}
            historyCount={state.history.length}
            autoOpenOnError={autoOpenOnError}
            onToggleAutoOpenOnError={() => setAutoOpenOnError((v) => !v)}
            onReportProblem={() => setReportSource("menu")}
          />
        </div>
      </header>

      {state.error && (
        <div className="px-4 py-2 bg-red-900/30 border-b border-red-800 flex items-center justify-between gap-3">
          <span className="text-sm text-red-300 flex-1 break-words">
            {state.error}
          </span>
          <div className="flex items-center gap-3 shrink-0">
            <button
              onClick={() => setReportSource("query")}
              title="File this as a GitHub issue (you review it first)"
              className="text-red-300 hover:text-red-100 text-xs underline-offset-2 hover:underline"
            >
              Report
            </button>
            <button
              onClick={() => dispatch({ type: "CLEAR_ERROR" })}
              className="text-red-400 hover:text-red-200 text-xs"
            >
              Dismiss
            </button>
          </div>
        </div>
      )}

      {/* Schema-acquisition warnings (PRD §10.17). Sits above the main
          surface so analyzer-not-installed and W_SCHEMA_* messages are
          visible without scrolling. */}
      <SchemaWarningBanner
        warnings={state.schema.warnings}
        url={state.connection.url}
        database={state.connection.database}
        token={state.connection.token}
        dispatch={dispatch}
      />
      {state.schema.error && (
        <div className="px-4 py-1 bg-amber-900/20 border-b border-amber-800/30 flex items-center justify-between gap-3">
          <span className="text-xs text-amber-300 flex-1 break-words">
            <strong className="font-medium">Schema:</strong> {state.schema.error}
          </span>
        </div>
      )}

      <div className="flex-1 min-h-0 flex">
        {showMapping && (
          <>
            <div
              className="border-r border-gray-800 flex-shrink-0 relative"
              style={{ width: mappingWidth }}
            >
              <MappingPanel
                ontologyTtl={state.ontologyTtl}
                onChange={(ttl) =>
                  dispatch({ type: "SET_ONTOLOGY_TTL", ontologyTtl: ttl })
                }
                onClose={() => setShowMapping(false)}
                sessionToken={state.connection.token}
                dispatch={dispatch}
              />
            </div>
            <div
              className="w-1.5 flex-shrink-0 cursor-col-resize hover:bg-indigo-500/30 active:bg-indigo-500/40 transition-colors"
              onMouseDown={(e) => {
                e.preventDefault();
                dragRef.current = { startX: e.clientX, startW: mappingWidth };
                document.body.style.cursor = "col-resize";
                document.body.style.userSelect = "none";
              }}
            />
          </>
        )}

        <div className="flex-1 min-w-0 flex flex-col">
          {/* L0 — multi-turn transcript (opt-in) + chat composer (NL "Ask") */}
          {showTranscript && (
            <div className="px-3 pt-2">
              <Transcript
                turns={state.transcript}
                onSelect={handleSelectTurn}
                onClear={() => dispatch({ type: "CLEAR_TRANSCRIPT" })}
              />
            </div>
          )}
          <ChatComposer
            value={nlInput}
            {...(controlPlane.on ? { placeholder: t("controlPlane.composerPlaceholder") } : {})}
            onChange={setNlInput}
            onSend={handleSend}
            busy={busy}
            suggestions={nlSuggestions}
            onPickSuggestion={setNlInput}
            statusSlot={
              stage !== "idle" ? (
                <span
                  role="status"
                  className="text-[10px] text-violet-300/80 tabular-nums"
                >
                  {stageLabel(stage)}
                </span>
              ) : state.nlInfo && state.sparqlSource === "nl_pipeline" ? (
                <span
                  className="text-[10px] text-emerald-500/70 tabular-nums"
                  title="Generated from your natural-language question"
                >
                  from NL &middot; {state.nlInfo.latencyMs}ms &middot;{" "}
                  {state.nlInfo.llmCalls} call
                  {state.nlInfo.llmCalls === 1 ? "" : "s"}
                  {state.nlInfo.repaired ? " \u00b7 repaired" : ""}
                </span>
              ) : !isConnected ? (
                <span className="text-[10px] text-amber-600/80 truncate">
                  {t("status.notConnected")}
                </span>
              ) : null
            }
          />

          {state.nlError && (
            <div className="px-3 py-1.5 bg-red-900/30 border-b border-red-800 flex items-center justify-between gap-3">
              <span className="text-xs text-red-300 flex-1 break-words">
                {state.nlError}
              </span>
              <div className="flex items-center gap-3 shrink-0">
                <button
                  onClick={() => setReportSource("nl")}
                  title="File this as a GitHub issue (you review it first)"
                  className="text-red-300 hover:text-red-100 text-[11px] underline-offset-2 hover:underline"
                >
                  Report
                </button>
                <button
                  onClick={() => dispatch({ type: "CLEAR_NL_ERROR" })}
                  className="text-red-400 hover:text-red-200 text-[11px]"
                >
                  Dismiss
                </button>
              </div>
            </div>
          )}

          {/* L0 — per-result affordance chips (Phase 3) */}
          <ResultAffordances affordances={affordances} onSelect={handleAffordance} />

          {/* L1 — collapsible query inspector (SPARQL | AQL) */}
          <QueryInspector
            open={showInspector}
            onToggle={() => setShowInspector((v) => !v)}
            onTranslate={handleTranslate}
            onRun={handleExecute}
            onExplain={handleExplain}
            onProfile={handleProfile}
            translating={state.translating}
            executing={state.executing}
            explaining={state.explaining}
            profiling={state.profiling}
            busy={inspectorBusy}
            isConnected={isConnected}
            sparqlEmpty={!state.sparql.trim()}
            sparqlOpen={sparqlPaneOpen}
            aqlOpen={aqlPaneOpen}
            onToggleSparql={toggleSparqlPane}
            onToggleAql={toggleAqlPane}
            sparqlPane={sparqlPane}
            aqlPane={aqlPane}
          />

          {/* L0 — results are the primary surface; fill the space the inspector leaves */}
          <div className="flex-1 min-h-0 border-t border-gray-800">
            <ResultsPanel
              results={state.results}
              warnings={state.warnings}
              activeTab={state.activeResultTab}
              dispatch={dispatch}
              execMs={state.execMs}
              explainPlan={state.explainPlan}
              profileData={state.profileData}
              loadGraph={loadGraph}
            />
          </div>
        </div>
      </div>

      {showHistory && (
        <QueryHistory
          history={state.history}
          onSelect={(sparql) => dispatch({ type: "SET_SPARQL", sparql })}
          onClear={() => dispatch({ type: "CLEAR_HISTORY" })}
          onClose={() => setShowHistory(false)}
        />
      )}

      {reportSource && (
        <ReportProblem
          error={
            reportSource === "nl"
              ? state.nlError
              : reportSource === "query"
                ? state.error
                : (state.error ?? state.nlError)
          }
          sparql={state.sparql}
          aql={state.aql}
          onClose={() => setReportSource(null)}
        />
      )}

      {showSamples && (
        <SampleQueries
          onSelect={(sparql) => dispatch({ type: "SET_SPARQL", sparql })}
          onClose={() => setShowSamples(false)}
          minedExamples={scopedExamples}
        />
      )}

      <CommandPalette
        open={showPalette}
        onClose={() => setShowPalette(false)}
        commands={commands}
      />
    </div>
  );
}
