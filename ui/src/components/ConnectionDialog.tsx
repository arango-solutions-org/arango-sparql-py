import { useEffect, useRef, useState } from "react";
import {
  cancelSchemaPolls,
  connect,
  connectPlatform,
  disconnect,
  getConnectDefaults,
  getControlPlaneContext,
  getPlatformStatus,
  schemaIntrospectUntilReady,
  SchemaPollSuperseded,
  type ConnectDefaults,
  type PlatformStatus,
  type SchemaIntrospectResponse,
} from "../api/client";
import type { Action, ConnectionState, SchemaWarning } from "../api/store";
import { contextToken, controlPlane, type ProjectContext } from "../api/controlPlane";
import { t } from "../i18n";

// `ConnectionDialog` for the SPARQL UI. After a successful
// `/connect` it kicks off a `/schema/introspect` round-trip so the
// MappingPanel and AQL editor can render the analyzer-acquired
// MappingBundle without the user having to author / upload a
// Turtle ontology by hand. Mirrors the Cypher project's
// "auto-introspect after connect" flow (see PRD §6.4 and §10.2).
//
// The introspect call is best-effort: a 503 (analyzer not
// installed and heuristic disabled, per the §6.3.4 four-cell
// table) is reported via the schema warnings banner rather than
// failing the connection itself — a user with a SPARQL workload
// against a DB they cannot introspect can still author their
// own ontology in the MappingPanel and run queries.

interface Props {
  connection: ConnectionState;
  dispatch: (action: Action) => void;
  /** Optional callback so App.tsx can prefill the MappingPanel
   * editor with the OWL/Turtle that auto-introspect returns. We
   * accept it as a prop rather than dispatching SET_ONTOLOGY_TTL
   * directly so the dialog keeps its single-responsibility shape
   * — App.tsx owns the editor / Turtle coupling. */
  onSchemaLoaded?: (turtle: string | null) => void;
  /** True while a schema read is in flight (auto-introspect on connect or a
   * refresh) — drives the "Loading schema…" cue in the connection bar. */
  introspecting?: boolean;
  /** True while the server analyzes the database in the background — the
   * cue then says so, since a first analysis can take minutes. */
  analyzing?: boolean;
  /** "Refresh schema": bypass the schema cache and re-read the database.
   * Owned by App.tsx, which also keeps the ontology editor in sync. */
  onRefreshSchema?: () => void;
}

/**
 * Project the introspect response into the SchemaWarning shape the
 * store + banner expect. Tolerates partial / typed-but-loose
 * payloads so an analyzer-version mismatch doesn't crash the UI.
 */
function _normaliseWarnings(
  resp: SchemaIntrospectResponse | null,
): SchemaWarning[] {
  if (!resp || !Array.isArray(resp.warnings)) return [];
  return resp.warnings
    .filter((w): w is SchemaWarning =>
      typeof w === "object" && w !== null && typeof w.code === "string"
        ? typeof w.message === "string"
        : false,
    )
    .map((w) => ({
      code: w.code,
      message: w.message,
      install_hint: w.install_hint,
      severity: w.severity,
    }));
}

/**
 * Pull the inline OWL/Turtle out of an introspect response. The
 * mapping wire dict accepts both `owlTurtle` (canonical camelCase)
 * and `owl_turtle` (Python-side snake_case) so we check both.
 * Returns null when the analyzer didn't emit OWL.
 */
function _extractTurtle(resp: SchemaIntrospectResponse | null): string | null {
  if (!resp || !resp.mapping || typeof resp.mapping !== "object") return null;
  const mapping = resp.mapping as Record<string, unknown>;
  const owl =
    (mapping.owlTurtle as string | undefined) ??
    (mapping.owl_turtle as string | undefined);
  return typeof owl === "string" && owl.trim().length > 0 ? owl : null;
}

export default function ConnectionDialog({
  connection,
  dispatch,
  onSchemaLoaded,
  introspecting = false,
  analyzing = false,
  onRefreshSchema,
}: Props) {
  const [form, setForm] = useState({
    url: connection.url,
    database: connection.database,
    username: connection.username,
    password: "",
  });
  const [open, setOpen] = useState(false);
  // null until GET /connect/platform answers (or fails on an older backend).
  const [platform, setPlatform] = useState<PlatformStatus | null>(null);
  const autoConnectAttempted = useRef(false);
  // Under a control plane: the project, user and database this tab's context names.
  const [project, setProject] = useState<ProjectContext | null>(null);

  useEffect(() => {
    if (connection.status === "disconnected") {
      setForm((f) => ({
        ...f,
        url: connection.url,
        database: connection.database,
        username: connection.username,
      }));
    }
  }, [connection.status, connection.url, connection.database, connection.username]);

  // On the platform the user is already signed in: open a session with that
  // login and skip the credentials dialog entirely. Off the platform (local
  // dev, or a backend without /connect/platform) fall back to the .env
  // defaults and their optional password auto-connect.
  useEffect(() => {
    if (controlPlane.on) {
      // The context is the session: open the project's database, no dialog.
      if (!autoConnectAttempted.current) {
        autoConnectAttempted.current = true;
        void doControlPlaneConnect();
      }
      return;
    }
    getPlatformStatus()
      .then((status) => {
        setPlatform(status);
        if (status.available) {
          if (!autoConnectAttempted.current && connection.status === "disconnected") {
            autoConnectAttempted.current = true;
            // No database named: the server opens one this user can open
            // (the mount database when possible) and says which.
            doPlatformConnect();
          }
          return;
        }
        loadDefaults();
      })
      .catch((err) => {
        console.warn("Platform login check failed; using the connect dialog:", err);
        loadDefaults();
      });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  function loadDefaults() {
    getConnectDefaults()
      .then((defaults: ConnectDefaults) => {
        const newForm = {
          url: defaults.url || form.url,
          database:
            form.database && form.database !== "_system"
              ? form.database
              : defaults.database || form.database,
          username: defaults.username || form.username,
          password: defaults.password || form.password,
        };
        setForm(newForm);

        if (
          !autoConnectAttempted.current &&
          defaults.password &&
          connection.status === "disconnected"
        ) {
          autoConnectAttempted.current = true;
          doConnect(newForm);
        }
      })
      .catch((err) => {
        // /connect/defaults is 404 in public mode (and omits the password
        // unless ARANGO_SPARQL_EXPOSE_DEFAULTS_PASSWORD=1). Surface in the
        // console rather than silently dropping — the form falls back to its
        // hardcoded defaults.
        console.warn("Failed to load /connect/defaults:", err);
      });
  }

  async function doConnect(f: typeof form) {
    dispatch({
      type: "CONNECT_START",
      url: f.url,
      database: f.database,
      username: f.username,
    });
    try {
      const resp = await connect({
        url: f.url,
        database: f.database,
        username: f.username,
        password: f.password,
      });
      dispatch({
        type: "CONNECT_SUCCESS",
        token: resp.token,
        databases: resp.databases,
        url: f.url,
        database: f.database,
        username: f.username,
        password: f.password,
      });
      setOpen(false);
      // Fire-and-forget auto-introspect (PRD §6.4 + §10.2). Awaited
      // so a fast backend completes the dispatch before React commits
      // the post-connect render; error path logs and degrades to
      // "no schema loaded" rather than rolling back the connection.
      void doAutoIntrospect(resp.token, f.database);
    } catch (err) {
      dispatch({
        type: "CONNECT_ERROR",
        error: err instanceof Error ? err.message : String(err),
      });
    }
  }

  // Open a session as the signed-in platform user — no credentials. The
  // platform session's cluster is the one serving this page; its origin
  // labels the connection. The server chooses (or confirms) the database and
  // reports it in resp.database, which is what we introspect and store.
  async function doPlatformConnect(database?: string) {
    const url = window.location.origin;
    dispatch({
      type: "CONNECT_START",
      url,
      database: database ?? platform?.database ?? "",
      username: "",
    });
    try {
      const resp = await connectPlatform(database);
      const opened = resp.database || database || platform?.database || "";
      dispatch({
        type: "CONNECT_SUCCESS",
        token: resp.token,
        databases: resp.databases,
        url,
        database: opened,
        username: "",
        password: "",
        platform: true,
      });
      setOpen(false);
      void doAutoIntrospect(resp.token, opened);
    } catch (err) {
      dispatch({
        type: "CONNECT_ERROR",
        error: err instanceof Error ? err.message : String(err),
      });
    }
  }

  // Under a control plane the server opens the project's database with its own
  // credential. The token in the state only marks the session as open: every call sends
  // the tab's current context (it is renewed every few minutes), see api/client.ts.
  async function doControlPlaneConnect() {
    dispatch({ type: "CONNECT_START", url: "", database: "", username: "" });
    try {
      const ctx = await getControlPlaneContext();
      setProject(ctx);
      if (!ctx.database) {
        throw new Error(t("controlPlane.noDatabase", { problem: ctx.problem ?? "" }));
      }
      const token = contextToken() ?? "control-plane";
      dispatch({
        type: "CONNECT_SUCCESS",
        token,
        databases: [ctx.database],
        url: "",
        database: ctx.database,
        username: ctx.principal,
        password: "",
      });
      void doAutoIntrospect(token, ctx.database);
    } catch (err) {
      dispatch({
        type: "CONNECT_ERROR",
        error: err instanceof Error ? err.message : String(err),
      });
    }
  }

  async function doAutoIntrospect(token: string, database: string) {
    dispatch({ type: "SCHEMA_REFRESH_START" });
    try {
      // The schema is read from the server's cache; a database it has not
      // analyzed yet answers "pending" while a background analysis runs
      // (service/schema_warm.py), so wait it out instead of freezing on one
      // multi-minute request.
      const resp = await schemaIntrospectUntilReady(
        token,
        { database, include_owl: true, include_statistics: true },
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
        warnings: _normaliseWarnings(resp),
        cacheHit: !!resp.cache_hit,
      });
      const turtle = _extractTurtle(resp);
      if (turtle && onSchemaLoaded) onSchemaLoaded(turtle);
    } catch (err) {
      // A newer connect / refresh / disconnect took over: drop this result.
      if (err instanceof SchemaPollSuperseded) return;
      // Non-fatal — the user can still author an ontology by hand.
      // We surface the error in the schema slice so the warning
      // banner can render it; the connection itself remains active.
      const message =
        err instanceof Error ? err.message : String(err);
      dispatch({ type: "SCHEMA_REFRESH_ERROR", error: message });
      console.warn("auto-introspect failed:", err);
    }
  }

  async function handleConnect() {
    await doConnect(form);
  }

  async function handleSwitchDb(newDb: string) {
    if (newDb === connection.database) return;

    // Platform session: reconnect through /connect/platform (no credentials).
    // Open the new session first, then tear down the old one — doPlatformConnect
    // dispatches CONNECT_START, which nulls the old token so DB-keyed effects
    // bail instead of firing against the new DB with the dead token.
    if (connection.platform) {
      const oldToken = connection.token;
      await doPlatformConnect(newDb);
      if (oldToken) {
        try {
          await disconnect(oldToken);
        } catch {
          /* best-effort */
        }
      }
      return;
    }

    // Switch within the SAME cluster: reuse the live connection's credentials
    // rather than `form`. After the initial connect the dialog is closed and
    // `form.password` may be stale/empty, which would turn a database switch
    // into a 400 "Connection failed". `connection.*` is the source of truth for
    // the active session (password is retained on CONNECT_SUCCESS). Mirrors
    // arango-cypher-py 51be876.
    const f = {
      ...form,
      url: connection.url || form.url,
      username: connection.username || form.username,
      password: connection.password || form.password,
      database: newDb,
    };
    setForm(f);

    // Open the new session first, then tear down the old one: doConnect()
    // dispatches CONNECT_START, which nulls the token so the database-keyed
    // effects bail instead of firing against the new DB with the dead token.
    const oldToken = connection.token;
    await doConnect(f);
    if (oldToken) {
      try {
        await disconnect(oldToken);
      } catch {
        /* best-effort */
      }
    }
  }

  async function handleDisconnect() {
    cancelSchemaPolls();
    if (connection.token) {
      try {
        await disconnect(connection.token);
      } catch {
        /* best-effort */
      }
    }
    dispatch({ type: "DISCONNECT" });
  }

  if (controlPlane.on) {
    // The project's context decides the database: no cluster, picker or disconnect.
    return (
      <div className="flex items-center gap-3 text-sm">
        {project && (
          <>
            <span className="flex items-center gap-1.5">
              <span className="text-[10px] text-gray-500 uppercase tracking-wide">
                {t("controlPlane.project")}
              </span>
              <span className="text-gray-300">{project.project}</span>
            </span>
            <span className="flex items-center gap-1.5">
              <span className="text-[10px] text-gray-500 uppercase tracking-wide">
                {t("controlPlane.signedInAs")}
              </span>
              <span className="text-gray-300">{project.principal}</span>
            </span>
          </>
        )}
        {connection.status === "connected" && (
          <span className="flex items-center gap-1.5">
            <span className="inline-block w-2 h-2 rounded-full bg-emerald-400" />
            <span className="text-[10px] text-gray-500 uppercase tracking-wide">Database</span>
            <span className="text-gray-300">{connection.database}</span>
          </span>
        )}
        {connection.status === "connecting" && <span className="text-gray-400">Connecting...</span>}
        {connection.status === "disconnected" && connection.error && (
          <>
            <span role="alert" className="text-xs text-red-400 max-w-[360px] truncate" title={connection.error}>
              {connection.error}
            </span>
            <button
              onClick={() => void doControlPlaneConnect()}
              className="px-2.5 py-1 text-xs rounded bg-gray-700 hover:bg-gray-600 text-gray-300 transition-colors"
            >
              Try again
            </button>
          </>
        )}
        {connection.status === "connected" && introspecting && (
          <span className="flex items-center gap-1.5 text-xs text-amber-400 animate-pulse">
            {analyzing ? t("schema.analyzing") : t("schema.loading")}
          </span>
        )}
        {connection.status === "connected" && onRefreshSchema && (
          <button
            onClick={onRefreshSchema}
            disabled={introspecting}
            title="Bypass the schema cache and re-introspect the database"
            className="px-2.5 py-1 text-xs rounded bg-gray-800 hover:bg-gray-700 text-gray-300 transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
          >
            Refresh schema
          </button>
        )}
      </div>
    );
  }

  if (connection.status === "connected") {
    // Mirrors arango-cypher-py's connection bar: CLUSTER host · DATABASE
    // picker · schema status · Refresh schema · Disconnect.
    return (
      <div className="flex items-center gap-3 text-sm">
        <span
          className="flex items-center gap-1.5"
          title={
            connection.platform
              ? `${connection.url} — signed in with your platform login`
              : connection.url
          }
        >
          <span className="inline-block w-2 h-2 rounded-full bg-emerald-400" />
          <span className="text-[10px] text-gray-500 uppercase tracking-wide">Cluster</span>
          <span className="text-gray-300 text-xs truncate max-w-[220px]">
            {connection.url.replace(/^https?:\/\//, "").replace(/\/$/, "")}
          </span>
        </span>
        <span className="flex items-center gap-1.5">
          <span className="text-[10px] text-gray-500 uppercase tracking-wide">Database</span>
          {connection.databases.length > 1 ? (
            <select
              value={connection.database}
              onChange={(e) => handleSwitchDb(e.target.value)}
              aria-label="Database"
              className="bg-gray-800 border border-gray-600 text-gray-200 text-sm rounded px-1.5 py-0.5 focus:border-indigo-500 focus:outline-none cursor-pointer"
            >
              {connection.databases.map((db) => (
                <option key={db} value={db}>
                  {db}
                </option>
              ))}
            </select>
          ) : (
            <span className="text-gray-300">{connection.database}</span>
          )}
        </span>
        {introspecting && (
          <span className="flex items-center gap-1.5 text-xs text-amber-400 animate-pulse">
            <svg className="w-3 h-3 animate-spin" viewBox="0 0 16 16" fill="none" aria-hidden="true">
              <circle cx="8" cy="8" r="6" stroke="currentColor" strokeWidth="2" strokeDasharray="28" strokeDashoffset="8" strokeLinecap="round" />
            </svg>
            {analyzing ? t("schema.analyzing") : t("schema.loading")}
          </span>
        )}
        {onRefreshSchema && (
          <button
            onClick={onRefreshSchema}
            disabled={introspecting}
            title="Bypass the schema cache and re-introspect the database"
            className="px-2.5 py-1 text-xs rounded bg-gray-800 hover:bg-gray-700 text-gray-300 transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
          >
            Refresh schema
          </button>
        )}
        <button
          onClick={handleDisconnect}
          className="px-2.5 py-1 text-xs rounded bg-gray-700 hover:bg-gray-600 text-gray-300 transition-colors"
        >
          Disconnect
        </button>
      </div>
    );
  }

  if (!open && connection.status === "connecting") {
    return <span className="text-sm text-gray-400">Connecting...</span>;
  }

  if (!open && platform?.available) {
    // Signed in to the platform: reconnecting needs no credentials. The
    // dialog stays reachable for a different cluster.
    return (
      <div className="flex items-center gap-3 text-sm">
        {connection.error && (
          <span
            role="alert"
            className="text-xs text-red-400 max-w-[360px] truncate"
            title={connection.error}
          >
            {connection.error}
          </span>
        )}
        <button
          onClick={() => doPlatformConnect()}
          className="px-3 py-1.5 text-sm rounded bg-indigo-600 hover:bg-indigo-500 text-white transition-colors"
        >
          Connect
        </button>
        <button
          onClick={() => setOpen(true)}
          className="text-xs text-gray-400 hover:text-gray-200 underline-offset-2 hover:underline"
        >
          Use credentials
        </button>
      </div>
    );
  }

  if (!open) {
    return (
      <button
        onClick={() => setOpen(true)}
        className="px-3 py-1.5 text-sm rounded bg-indigo-600 hover:bg-indigo-500 text-white transition-colors"
      >
        Connect to ArangoDB
      </button>
    );
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60">
      <div className="bg-gray-800 rounded-lg shadow-2xl p-6 w-full max-w-md border border-gray-700">
        <h2 className="text-lg font-semibold mb-4 text-gray-100">
          Connect to ArangoDB
        </h2>

        {connection.error && (
          <div className="mb-4 p-3 rounded bg-red-900/50 border border-red-700 text-red-300 text-sm">
            {connection.error}
          </div>
        )}

        <div className="space-y-3">
          <label>
            <span className="text-xs text-gray-400 block mb-1">URL</span>
            <input
              value={form.url}
              onChange={(e) => setForm({ ...form, url: e.target.value })}
              placeholder="http://localhost:8529 or https://cloud.arangodb.com"
              className="w-full px-3 py-2 rounded bg-gray-900 border border-gray-600 text-sm text-gray-100 focus:border-indigo-500 focus:outline-none"
            />
          </label>
          <label>
            <span className="text-xs text-gray-400 block mb-1">Database</span>
            <input
              value={form.database}
              onChange={(e) => setForm({ ...form, database: e.target.value })}
              className="w-full px-3 py-2 rounded bg-gray-900 border border-gray-600 text-sm text-gray-100 focus:border-indigo-500 focus:outline-none"
            />
          </label>
          <label>
            <span className="text-xs text-gray-400 block mb-1">Username</span>
            <input
              value={form.username}
              onChange={(e) => setForm({ ...form, username: e.target.value })}
              className="w-full px-3 py-2 rounded bg-gray-900 border border-gray-600 text-sm text-gray-100 focus:border-indigo-500 focus:outline-none"
            />
          </label>
          <label>
            <span className="text-xs text-gray-400 block mb-1">Password</span>
            <input
              type="password"
              value={form.password}
              onChange={(e) => setForm({ ...form, password: e.target.value })}
              onKeyDown={(e) => e.key === "Enter" && handleConnect()}
              className="w-full px-3 py-2 rounded bg-gray-900 border border-gray-600 text-sm text-gray-100 focus:border-indigo-500 focus:outline-none"
            />
          </label>
        </div>

        <div className="flex justify-end gap-3 mt-6">
          <button
            onClick={() => setOpen(false)}
            className="px-4 py-2 text-sm rounded bg-gray-700 hover:bg-gray-600 text-gray-300 transition-colors"
          >
            Cancel
          </button>
          <button
            onClick={handleConnect}
            disabled={connection.status === "connecting"}
            className="px-4 py-2 text-sm rounded bg-indigo-600 hover:bg-indigo-500 text-white transition-colors disabled:opacity-50"
          >
            {connection.status === "connecting" ? "Connecting..." : "Connect"}
          </button>
        </div>
      </div>
    </div>
  );
}
