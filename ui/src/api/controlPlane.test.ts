import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  adoptContext,
  contextHeaders,
  contextToken,
  controlPlane,
  onSessionEnded,
  scheduleRenewal,
  startControlPlane,
} from "./controlPlane";
import { listGraphs, translateSparql } from "./client";

// The page under a governance control plane: the context is kept per tab, out of the
// address bar, sent as the only credential, and renewed. Vitest runs in node here, so
// the browser globals are small stand-ins.

class MemoryStorage {
  private items = new Map<string, string>();
  getItem(key: string): string | null {
    return this.items.get(key) ?? null;
  }
  setItem(key: string, value: string): void {
    this.items.set(key, value);
  }
  removeItem(key: string): void {
    this.items.delete(key);
  }
}

function token(project: string, exp: number): string {
  const claims = btoa(JSON.stringify({ exp, ctx: { project } }))
    .replace(/\+/g, "-")
    .replace(/\//g, "_")
    .replace(/=+$/, "");
  return `header.${claims}.signature`;
}

let address: string;
let session: MemoryStorage;
let local: MemoryStorage;
const replaceState = vi.fn((_state: unknown, _title: string, url: string) => {
  address = new URL(url, address).href;
});

beforeEach(() => {
  address = "http://localhost/query/?ctx=CTX-1&tab=x#top";
  session = new MemoryStorage();
  local = new MemoryStorage();
  replaceState.mockClear();
  vi.stubGlobal("window", {
    get location() {
      return { href: address, pathname: new URL(address).pathname };
    },
    history: { state: null, replaceState },
    get sessionStorage() {
      return session;
    },
    get localStorage() {
      return local;
    },
  });
  controlPlane.on = false;
  controlPlane.home = "/";
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
  onSessionEnded(null);
});

describe("adoptContext", () => {
  it("keeps the context for this tab and takes it out of the address bar", () => {
    expect(adoptContext()).toBe("CTX-1");
    expect(session.getItem("arango-sparql.context")).toBe("CTX-1");
    expect(address).toBe("http://localhost/query/?tab=x#top");
    expect(local.getItem("arango-sparql.context")).toBeNull();
  });

  it("keeps the tab's context on reload, when the address no longer has one", () => {
    adoptContext();
    address = "http://localhost/query/";
    expect(adoptContext()).toBe("CTX-1");
    expect(replaceState).toHaveBeenCalledTimes(1);
  });

  it("has nothing when the tab never got a context", () => {
    address = "http://localhost/query/";
    expect(adoptContext()).toBeNull();
  });
});

describe("startControlPlane", () => {
  function mode(body: unknown) {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify(body))));
  }

  it("does nothing off a control plane", async () => {
    mode({ control_plane: false, home: null });
    expect(await startControlPlane("/query")).toBe("off");
    expect(controlPlane.on).toBe(false);
    expect(contextToken()).toBeNull();
  });

  it("is ready with a context, and says where to open it from without one", async () => {
    mode({ control_plane: true, home: "/projects" });
    expect(await startControlPlane("/query")).toBe("ready");
    expect(controlPlane).toEqual({ on: true, home: "/projects" });
    session = new MemoryStorage();
    address = "http://localhost/query/";
    expect(await startControlPlane("/query")).toBe("no-context");
  });
});

describe("renewal", () => {
  it("asks the control plane for a new context two minutes before expiry and keeps it", async () => {
    vi.useFakeTimers();
    const now = 1_000_000_000_000;
    session.setItem("arango-sparql.context", token("library", now / 1000 + 600));
    const renewed = token("library", now / 1000 + 1200);
    const fetchMock = vi.fn(async () => new Response(JSON.stringify({ token: renewed })));
    vi.stubGlobal("fetch", fetchMock);
    scheduleRenewal(() => now);
    await vi.advanceTimersByTimeAsync(479_000);
    expect(fetchMock).not.toHaveBeenCalled();
    await vi.advanceTimersByTimeAsync(1_000);
    expect(fetchMock).toHaveBeenCalledWith(
      "/api/context",
      expect.objectContaining({
        method: "POST",
        credentials: "same-origin",
        body: JSON.stringify({ project: "library" }),
        headers: expect.objectContaining({ "X-Control-Plane": "1" }),
      }),
    );
    expect(contextToken()).toBe(renewed);
    expect(contextHeaders()).toEqual({ Authorization: `Bearer ${renewed}` });
  });
});

describe("calls under a control plane", () => {
  function capture(status = 200, body: unknown = { graphs: [] }) {
    const fetchMock = vi.fn(async (_url: string, _init: RequestInit) => new Response(JSON.stringify(body), { status }));
    vi.stubGlobal("fetch", fetchMock);
    return fetchMock;
  }

  it("send the tab's current context as the only credential, on every call", async () => {
    controlPlane.on = true;
    session.setItem("arango-sparql.context", "CTX-NOW");
    const fetchMock = capture();
    await listGraphs("stale-session-token");
    await translateSparql({ sparql: "ASK {}" });
    for (const [, init] of fetchMock.mock.calls) {
      const headers = init.headers as Record<string, string>;
      expect(headers.Authorization).toBe("Bearer CTX-NOW");
      expect(headers["X-Arango-Session"]).toBeUndefined();
    }
  });

  it("say the session ended when the server refuses the context", async () => {
    controlPlane.on = true;
    session.setItem("arango-sparql.context", "CTX-OLD");
    capture(401, { detail: "Invalid project context" });
    const ended = vi.fn();
    onSessionEnded(ended);
    await expect(listGraphs("x")).rejects.toThrow();
    expect(ended).toHaveBeenCalledTimes(1);
  });

  it("are unchanged off a control plane", async () => {
    const fetchMock = capture();
    await listGraphs("session-token");
    const headers = fetchMock.mock.calls[0][1].headers as Record<string, string>;
    expect(headers["X-Arango-Session"]).toBe("session-token");
    expect(headers.Authorization).toBeUndefined();
  });
});
