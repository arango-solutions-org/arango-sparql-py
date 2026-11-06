// Under a governance control plane (the server's `CONTROL_PLANE_URL`), the page is opened
// at `<mount>?ctx=<token>`: a short-lived signed context for one user in one project. It is
// kept for THIS TAB only (sessionStorage), taken out of the address bar, sent on every call
// as `Authorization: Bearer`, and renewed before it expires. Never localStorage or a cookie:
// every tab shares those, so a second tab opened on another project would move this one
// into it. Off the control plane none of this runs and the page is unchanged.
//
// upstream: arango-solutions-org/arango-sparql-py#152 — remove when the control plane's TypeScript SDK ships its browser module

const KEY = "arango-sparql.context";
/** Renew this long before the token expires. */
const RENEW_BEFORE_MS = 120_000;
/** After a failed renewal, try again this soon (while the token is still valid). */
const RETRY_RENEWAL_MS = 15_000;
/** How long the page waits to learn whether it runs under a control plane. */
const MODE_TIMEOUT_MS = 10_000;

export const controlPlane = {
  /** True once the server said it runs under a control plane. */
  on: false,
  /** Where a user opens the project from (the control plane's own page). */
  home: "/",
  /** This tool's id as the server verifies contexts for it (its `TOOL_ID`), read from
   * `/control-plane/mode`: a context is issued for one tool, so renewal asks for this one. */
  tool: null as string | null,
};

export type ControlPlaneStart = "off" | "no-context" | "ready";

let renewal: ReturnType<typeof setTimeout> | undefined;
let ended: (() => void) | null = null;
/** The context, also held here: a browser that refuses storage keeps it for this page load. */
let held: string | null = null;

function storage(): Storage | null {
  try {
    return window.sessionStorage;
  } catch {
    return null;
  }
}

/** The context this tab holds, or null. */
export function contextToken(): string | null {
  try {
    return storage()?.getItem(KEY) ?? held;
  } catch {
    return held;
  }
}

function keep(token: string): void {
  held = token;
  try {
    storage()?.setItem(KEY, token);
  } catch {
    /* storage refused: the context lives only in this page load */
  }
}

/** Where the page keeps state a project owns, and under which key. Off a control plane:
 * localStorage under `key`, as always. Under one: this tab's sessionStorage, under `key` and
 * the context's project, so a tab given a context for another project starts clean. */
export function projectStore(key: string): { storage: Storage; key: string } {
  if (!controlPlane.on) return { storage: window.localStorage, key };
  const project = tokenClaims(contextToken())?.ctx?.project ?? "";
  return { storage: window.sessionStorage, key: `${key}:${project}` };
}

/** The headers that carry the context: the only credential under a control plane. */
export function contextHeaders(): Record<string, string> {
  const token = contextToken();
  return token ? { Authorization: `Bearer ${token}` } : {};
}

interface Claims {
  exp?: number;
  ctx?: { project?: string };
}

/** The token's claims, read without verifying (the server verifies). Null if unreadable. */
export function tokenClaims(token: string | null): Claims | null {
  try {
    const part = (token ?? "").split(".")[1].replace(/-/g, "+").replace(/_/g, "/");
    return JSON.parse(atob(part)) as Claims;
  } catch {
    return null;
  }
}

/** Take `?ctx=` from the address into this tab's storage and out of the address bar. */
export function adoptContext(): string | null {
  const url = new URL(window.location.href);
  const arrived = url.searchParams.get("ctx");
  if (arrived) {
    keep(arrived);
    url.searchParams.delete("ctx");
    window.history.replaceState(window.history.state, "", url.pathname + url.search + url.hash);
  }
  return contextToken();
}

/** Renew the context two minutes before it expires, through the control plane's own
 * endpoint on this address (its session cookie is sent there, never to this tool). It asks
 * for this tool again: a control plane that issues a context per tool names it in the
 * token, and refuses one for a tool the user's role may not use. */
export function scheduleRenewal(now: () => number = Date.now): void {
  const claims = tokenClaims(contextToken());
  if (!claims?.exp || !claims.ctx?.project) return;
  const project = claims.ctx.project;
  const expiresAt = claims.exp * 1000;
  const attempt = (delay: number) => {
    clearTimeout(renewal);
    renewal = setTimeout(async () => {
      try {
        // Root-absolute on purpose: renewal is the control plane's endpoint, not this tool's.
        const res = await fetch("/api/context", {
          method: "POST",
          credentials: "same-origin",
          headers: { "Content-Type": "application/json", "X-Control-Plane": "1" },
          body: JSON.stringify({ project, tool: controlPlane.tool }),
        });
        const body = res.ok ? ((await res.json()) as { token?: string }) : {};
        if (body.token) {
          keep(body.token);
          scheduleRenewal(now);
          return;
        }
      } catch {
        /* retried below */
      }
      // A failed renewal is tried again while the current token is still valid: one
      // transient failure must not end an active user's session.
      if (now() + RETRY_RENEWAL_MS < expiresAt) attempt(RETRY_RENEWAL_MS);
    }, delay);
  };
  attempt(Math.max(0, expiresAt - RENEW_BEFORE_MS - now()));
}

/** Ask the server whether it runs under a control plane; if so, adopt this tab's context
 * and keep it renewed. `apiBase` is where this tool's API is mounted. */
export async function startControlPlane(apiBase: string): Promise<ControlPlaneStart> {
  try {
    // Bounded: a request that hangs must not leave the page on its loading screen.
    const res = await fetch(apiBase + "/control-plane/mode", { signal: AbortSignal.timeout(MODE_TIMEOUT_MS) });
    const body = res.ok
      ? ((await res.json()) as { control_plane?: boolean; home?: string | null; tool?: string | null })
      : {};
    controlPlane.on = !!body.control_plane;
    controlPlane.home = body.home || "/";
    controlPlane.tool = body.tool ?? null;
  } catch {
    controlPlane.on = false;
  }
  if (!controlPlane.on) return "off";
  if (!adoptContext()) return "no-context";
  scheduleRenewal();
  return "ready";
}

/** Tests: forget the page's state between cases. */
export function __resetForTest(): void {
  clearTimeout(renewal);
  held = null;
  ended = null;
}

/** Called when the server refuses this tab's context: signed out, or expired while idle. */
export function onSessionEnded(callback: (() => void) | null): void {
  ended = callback;
}

export function sessionEnded(): void {
  clearTimeout(renewal);
  ended?.();
}

/** The context this tab is in, as the server reads it. */
export interface ProjectContext {
  principal: string;
  roles: string[];
  tenant: string;
  project: string;
  binding: string | null;
  database: string | null;
  /** Why the project has no database here, when it has none. */
  problem: string | null;
  expires_at: number;
}
