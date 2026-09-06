/**
 * mcpApp tests (Story 9.10, AC 4/6) — shared SDK-first channel with legacy fallback.
 *
 * AI-54 (fixture honesty): the mock App mirrors the REAL ext-apps 1.7.4 surface,
 * verified against the installed package's dist/src/app.d.ts + spec.types.d.ts:
 *   - `addEventListener("toolresult", handler)` registers a handler that receives
 *     the `ui/notifications/tool-result` params — the standard MCP CallToolResult
 *     shape { content, structuredContent?, isError? }. (The `ontoolresult` setter
 *     is @deprecated in 1.7.4; we track the non-deprecated multi-listener API.)
 *   - `callServerTool` takes CallToolRequest params { name, arguments } and
 *     resolves a CallToolResult (tool-level failures come back as isError
 *     results, transport failures reject — per the SDK's documented contract).
 *
 * Covers:
 *   - SDK connected path: ready="sdk", toolresult listener -> subscribers, awaitable
 *     callServerTool with CallToolRequest params, error propagation (thrown
 *     transport errors AND isError results normalized to rejections).
 *   - Fallback path: connect failure -> ready="legacy", legacy inbound
 *     (mcp:structuredContent with G-11 strict shape check), legacy outbound
 *     (raw postMessage incl. graceful no-op when postMessage throws).
 *   - readInjectedEnvelope global reader; singleton behavior.
 */

import { describe, it, expect, vi, afterEach } from "vitest";
import {
  connectMcpApp,
  callServerTool,
  APP_PAYLOAD_META_KEY,
  rehydrateEnvelope,
  readInjectedEnvelope,
  __resetMcpAppForTests,
  type McpToolResultParams,
} from "../mcpApp";

// ---------------------------------------------------------------------------
// Mock SDK App (real ext-apps message shapes — AI-54)
// ---------------------------------------------------------------------------

interface MockAppOptions {
  connectError?: Error;
  callError?: Error;
  callResult?: McpToolResultParams;
}

function makeMockApp(options: MockAppOptions = {}) {
  // Mirrors ext-apps 1.7.4 `addEventListener("toolresult", …)` — the mock stores
  // the registered handler so tests can drive a tool-result notification via the
  // `emitToolResult` helper (the host would fire it over the wire).
  let toolResultHandler:
    | ((params: McpToolResultParams) => void)
    | undefined;
  const app = {
    addEventListener: vi.fn(
      (
        event: "toolresult",
        handler: (params: McpToolResultParams) => void,
      ) => {
        if (event === "toolresult") toolResultHandler = handler;
      },
    ),
    /** Test-only: drive the registered toolresult handler as the host would. */
    emitToolResult(params: McpToolResultParams) {
      toolResultHandler?.(params);
    },
    connect: vi.fn(async () => {
      if (options.connectError) throw options.connectError;
    }),
    callServerTool: vi.fn(
      async (_params: {
        name: string;
        arguments?: Record<string, unknown>;
      }): Promise<McpToolResultParams> => {
        if (options.callError) throw options.callError;
        return options.callResult ?? { content: [] };
      },
    ),
  };
  return app;
}

/** A well-formed AD-1 envelope (schema_version marker). */
const ENVELOPE = {
  schema_version: "1.0",
  meta: { project_id: "proj_test", trace_id: "abc123" },
  data: { rows: [] },
};

const ORIGINAL_PARENT = window.parent;

function mockWindowParent(postMessage: (...args: unknown[]) => void) {
  Object.defineProperty(window, "parent", {
    value: { postMessage },
    writable: true,
    configurable: true,
  });
}

afterEach(() => {
  __resetMcpAppForTests();
  Object.defineProperty(window, "parent", {
    value: ORIGINAL_PARENT,
    writable: true,
    configurable: true,
  });
  delete window.__MCP_STRUCTURED_CONTENT__;
  vi.restoreAllMocks();
});

// ---------------------------------------------------------------------------
// SDK connected path
// ---------------------------------------------------------------------------

describe("mcpApp -- SDK connected path", () => {
  it("ready resolves 'sdk' when the handshake succeeds", async () => {
    const app = makeMockApp();
    const handle = connectMcpApp({ createApp: () => app });
    expect(handle.mode).toBe("pending");
    await expect(handle.ready).resolves.toBe("sdk");
    expect(handle.mode).toBe("sdk");
    expect(app.connect).toHaveBeenCalledTimes(1);
  });

  it("registers the toolresult listener BEFORE connect (no missed notification)", async () => {
    const app = makeMockApp();
    let listenerRegisteredAtConnectTime = false;
    app.connect.mockImplementation(async () => {
      listenerRegisteredAtConnectTime = app.addEventListener.mock.calls.some(
        ([event]) => event === "toolresult",
      );
    });
    const handle = connectMcpApp({ createApp: () => app });
    await handle.ready;
    expect(listenerRegisteredAtConnectTime).toBe(true);
  });

  it("replays a result emitted synchronously during connect to the first subscriber", async () => {
    const app = makeMockApp();
    app.connect.mockImplementation(async () => {
      app.emitToolResult({ content: [], structuredContent: ENVELOPE });
    });
    const handle = connectMcpApp({ createApp: () => app });
    const received = vi.fn();
    handle.onToolResult(received);
    await handle.ready;
    expect(received).toHaveBeenCalledWith(ENVELOPE, undefined);
  });

  it("toolresult structuredContent feeds onToolResult subscribers", async () => {
    const app = makeMockApp();
    const handle = connectMcpApp({ createApp: () => app });
    const received: unknown[] = [];
    handle.onToolResult((envelope) => received.push(envelope));
    await handle.ready;

    // Real ui/notifications/tool-result params: a CallToolResult.
    app.emitToolResult({
      content: [{ type: "text", text: "3 KPIs sur la période." }],
      structuredContent: ENVELOPE,
    });

    expect(received).toEqual([ENVELOPE]);
  });

  it("tool results without a schema_version envelope are ignored", async () => {
    const app = makeMockApp();
    const handle = connectMcpApp({ createApp: () => app });
    const cb = vi.fn();
    handle.onToolResult(cb);
    await handle.ready;

    app.emitToolResult({ content: [], structuredContent: { ok: true } });
    app.emitToolResult({ content: [{ type: "text", text: "sans envelope" }] });

    expect(cb).not.toHaveBeenCalled();
  });

  it("delivers inbound isError without structuredContent on the same transport", async () => {
    const app = makeMockApp();
    const handle = connectMcpApp({ createApp: () => app });
    const normal = vi.fn();
    const failed = vi.fn();
    handle.onToolResult(normal);
    handle.onToolResultError(failed);
    await handle.ready;

    app.emitToolResult({
      isError: true,
      content: [{ type: "text", text: "The pinned runtime is unavailable." }],
    });

    expect(failed).toHaveBeenCalledOnce();
    expect(failed).toHaveBeenCalledWith({ message: "The pinned runtime is unavailable." });
    expect(normal).not.toHaveBeenCalled();
  });

  it("onToolResultError returns an unsubscribe function", async () => {
    const app = makeMockApp();
    const handle = connectMcpApp({ createApp: () => app });
    const failed = vi.fn();
    handle.onToolResultError(failed)();
    await handle.ready;
    app.emitToolResult({ isError: true, content: [] });
    expect(failed).not.toHaveBeenCalled();
  });

  it("onToolResult returns an unsubscribe function", async () => {
    const app = makeMockApp();
    const handle = connectMcpApp({ createApp: () => app });
    const cb = vi.fn();
    const unsubscribe = handle.onToolResult(cb);
    await handle.ready;
    unsubscribe();

    app.emitToolResult({ content: [], structuredContent: ENVELOPE });
    expect(cb).not.toHaveBeenCalled();
  });

  it("callServerTool routes through the SDK with CallToolRequest params", async () => {
    const toolResult = { content: [], structuredContent: { ok: true } } as McpToolResultParams;
    const app = makeMockApp({ callResult: toolResult });
    const handle = connectMcpApp({ createApp: () => app });
    await handle.ready;

    await expect(
      handle.callServerTool("submit_feedback", { rating: 1 }),
    ).resolves.toBe(toolResult);

    expect(app.callServerTool).toHaveBeenCalledTimes(1);
    expect(app.callServerTool).toHaveBeenCalledWith({
      name: "submit_feedback",
      arguments: { rating: 1 },
    });
  });

  it("transport failures propagate (rejected callServerTool)", async () => {
    const app = makeMockApp({ callError: new Error("host refused") });
    const handle = connectMcpApp({ createApp: () => app });
    await handle.ready;

    await expect(
      handle.callServerTool("submit_feedback", { rating: 1 }),
    ).rejects.toThrow("host refused");
  });

  it("isError tool results are normalized to rejections (F-11)", async () => {
    const app = makeMockApp({
      callResult: {
        isError: true,
        content: [{ type: "text", text: "quota exceeded" }],
      },
    });
    const handle = connectMcpApp({ createApp: () => app });
    await handle.ready;

    await expect(
      handle.callServerTool("submit_feedback", { rating: -1 }),
    ).rejects.toThrow("quota exceeded");
  });

  it("module-level callServerTool uses the established SDK connection", async () => {
    const app = makeMockApp();
    const handle = connectMcpApp({ createApp: () => app });
    await handle.ready;

    await callServerTool("submit_feedback", { rating: 1 });
    expect(app.callServerTool).toHaveBeenCalledWith({
      name: "submit_feedback",
      arguments: { rating: 1 },
    });
  });
});

// ---------------------------------------------------------------------------
// Legacy fallback path
// ---------------------------------------------------------------------------

describe("mcpApp -- legacy fallback path", () => {
  it("ready resolves 'legacy' when the SDK handshake fails", async () => {
    const app = makeMockApp({ connectError: new Error("no host") });
    const handle = connectMcpApp({ createApp: () => app });
    await expect(handle.ready).resolves.toBe("legacy");
    expect(handle.mode).toBe("legacy");
  });

  it("mcp:structuredContent messages feed subscribers (G-11 strict shape check)", async () => {
    const app = makeMockApp({ connectError: new Error("no host") });
    const handle = connectMcpApp({ createApp: () => app });
    const received: unknown[] = [];
    handle.onToolResult((envelope) => received.push(envelope));
    await handle.ready;

    // jsdom has no ancestorOrigins -> the G-11 strict shape branch applies.
    window.dispatchEvent(
      new MessageEvent("message", {
        data: { type: "mcp:structuredContent", data: ENVELOPE },
      }),
    );

    expect(received).toEqual([ENVELOPE]);
  });

  it("legacy inbound is wired even BEFORE the handshake settles", () => {
    const app = makeMockApp();
    // connect never resolves during this test — mode stays "pending".
    app.connect.mockImplementation(() => new Promise(() => {}));
    const handle = connectMcpApp({ createApp: () => app });
    const cb = vi.fn();
    handle.onToolResult(cb);

    window.dispatchEvent(
      new MessageEvent("message", {
        data: { type: "mcp:structuredContent", data: ENVELOPE },
      }),
    );

    expect(handle.mode).toBe("pending");
    // Story 50.6: subscribers now receive the (envelope, meta) PAIR. A result
    // with no `_meta` still notifies -- with `meta` undefined -- rather than
    // being dropped, which is what keeps every pre-50.6 widget working.
    expect(cb).toHaveBeenCalledWith(ENVELOPE, undefined);
  });

  it("ignores malformed payloads (no schema_version)", async () => {
    const app = makeMockApp({ connectError: new Error("no host") });
    const handle = connectMcpApp({ createApp: () => app });
    const cb = vi.fn();
    handle.onToolResult(cb);
    await handle.ready;

    window.dispatchEvent(
      new MessageEvent("message", {
        data: { type: "mcp:structuredContent", data: { rows: [] } },
      }),
    );
    window.dispatchEvent(
      new MessageEvent("message", { data: { type: "autre", data: ENVELOPE } }),
    );
    window.dispatchEvent(new MessageEvent("message", { data: "junk" }));

    expect(cb).not.toHaveBeenCalled();
  });

  it("callServerTool falls back to raw postMessage {type:'callServerTool'}", async () => {
    const postMessageMock = vi.fn();
    mockWindowParent(postMessageMock);
    const app = makeMockApp({ connectError: new Error("no host") });
    const handle = connectMcpApp({ createApp: () => app });
    await handle.ready;

    await expect(
      handle.callServerTool("submit_feedback", { rating: 1 }),
    ).resolves.toBeNull();

    expect(postMessageMock).toHaveBeenCalledTimes(1);
    const [payload] = postMessageMock.mock.calls[0];
    expect(payload).toEqual({
      type: "callServerTool",
      tool: "submit_feedback",
      arguments: { rating: 1 },
    });
  });

  it("legacy callServerTool is a graceful no-op when postMessage throws", async () => {
    mockWindowParent(() => {
      throw new Error("No host available");
    });
    const app = makeMockApp({ connectError: new Error("no host") });
    const handle = connectMcpApp({ createApp: () => app });
    await handle.ready;

    await expect(
      handle.callServerTool("submit_feedback", { rating: 1 }),
    ).resolves.toBeNull();
  });

  it("module-level callServerTool without any connection uses the legacy channel", async () => {
    const postMessageMock = vi.fn();
    mockWindowParent(postMessageMock);

    // No connectMcpApp() call: Vitest/Storybook component-test situation.
    await expect(
      callServerTool("submit_feedback", { rating: -1 }),
    ).resolves.toBeNull();

    expect(postMessageMock).toHaveBeenCalledTimes(1);
    const [payload] = postMessageMock.mock.calls[0];
    expect(payload.type).toBe("callServerTool");
    expect(payload.arguments).toEqual({ rating: -1 });
  });
});

// ---------------------------------------------------------------------------
// readInjectedEnvelope + singleton
// ---------------------------------------------------------------------------

describe("mcpApp -- readInjectedEnvelope", () => {
  it("returns the injected global when it carries data", () => {
    window.__MCP_STRUCTURED_CONTENT__ = ENVELOPE;
    expect(readInjectedEnvelope()).toEqual(ENVELOPE);
  });

  it("returns null when the global is absent", () => {
    expect(readInjectedEnvelope()).toBeNull();
  });

  it("returns null when the global has no data payload", () => {
    window.__MCP_STRUCTURED_CONTENT__ = { schema_version: "1.0" };
    expect(readInjectedEnvelope()).toBeNull();
  });
});

describe("mcpApp -- typed render payload coexistence", () => {
  it("rehydrates only the nested moved map and leaves render_input untouched", () => {
    const renderInput = { result: { id: "qr_EXAMPLE" } };
    const envelope = {
      schema_version: 1,
      data: { rows: { withheld: "moved_to_app_channel", bytes: 10 } },
    };
    const rehydrated = rehydrateEnvelope(envelope, {
      [APP_PAYLOAD_META_KEY]: {
        schema_version: 1,
        kind: "render",
        render_input: renderInput,
        moved: { rows: [{ value: 7 }] },
      },
    });
    expect(rehydrated.data).toEqual({ rows: [{ value: 7 }] });
  });
});

describe("mcpApp -- singleton", () => {
  it("connectMcpApp returns the same handle on repeated calls", () => {
    const app = makeMockApp();
    const first = connectMcpApp({ createApp: () => app });
    const second = connectMcpApp();
    expect(second).toBe(first);
    // Only one SDK App was created for the iframe.
    expect(app.connect).toHaveBeenCalledTimes(1);
  });
});
