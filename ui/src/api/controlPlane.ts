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

export const controlPlane = {
  /** True once the server said it runs under a control plane. */
  on: false,
  /** Where a user opens the project from (the control plane's own page). */
  home: "/",
};

export type ControlPlaneStart = "off" | "no-context" | "ready";

let renewal: ReturnType<typeof setTimeout> | undefined;
let ended: (() => void) | null = null;

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
    return storage()?.getItem(KEY) ?? null;
  } catch {
    return null;
  }
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
    try {
      storage()?.setItem(KEY, arrived);
    } catch {
      /* storage refused: the context lives only in this page load */
    }
    url.searchParams.delete("ctx");
    window.history.replaceState(window.history.state, "", url.pathname + url.search + url.hash);
    if (contextToken() === null) return arrived;
  }
  return contextToken();
}

/** Renew the context two minutes before it expires, through the control plane's own
 * endpoint on this address (its session cookie is sent there, never to this tool). */
export function scheduleRenewal(now: () => number = Date.now): void {
  const claims = tokenClaims(contextToken());
  if (!claims?.exp || !claims.ctx?.project) return;
  clearTimeout(renewal);
  const project = claims.ctx.project;
  const delay = Math.max(0, claims.exp * 1000 - RENEW_BEFORE_MS - now());
  renewal = setTimeout(async () => {
    try {
      // Root-absolute on purpose: renewal is the control plane's endpoint, not this tool's.
      const res = await fetch("/api/context", {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json", "X-Control-Plane": "1" },
        body: JSON.stringify({ project }),
      });
      if (!res.ok) return; // the current token stays until it expires
      const body = (await res.json()) as { token?: string };
      if (body.token) {
        storage()?.setItem(KEY, body.token);
        scheduleRenewal(now);
      }
    } catch {
      /* the current token stays until it expires */
    }
  }, delay);
}

/** Ask the server whether it runs under a control plane; if so, adopt this tab's context
 * and keep it renewed. `apiBase` is where this tool's API is mounted. */
export async function startControlPlane(apiBase: string): Promise<ControlPlaneStart> {
  try {
    const res = await fetch(apiBase + "/control-plane/mode");
    const body = res.ok ? ((await res.json()) as { control_plane?: boolean; home?: string | null }) : {};
    controlPlane.on = !!body.control_plane;
    controlPlane.home = body.home || "/";
  } catch {
    controlPlane.on = false;
  }
  if (!controlPlane.on) return "off";
  if (!adoptContext()) return "no-context";
  scheduleRenewal();
  return "ready";
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
