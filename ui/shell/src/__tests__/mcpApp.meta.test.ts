/**
 * Story 50.6 (AC11) — `_meta` actually reaches the widget, on BOTH transports.
 *
 * This is the missing half of the MCP Apps contract. Before this story the
 * `toolresult` listener read `params.structuredContent` and discarded `_meta`
 * entirely, even though the `CallToolResult` type has always named the field.
 * That single fact is why every dataset lived in `structuredContent`: there was
 * no other channel. Moving the rows to `_meta` without this change would have
 * broken every card — the two are one change, and these tests are what proves it.
 *
 * What is asserted here, and why each one earns its place:
 *   - the SDK transport delivers the pair;
 *   - the LEGACY transport delivers the same pair (a widget must not behave
 *     differently by transport — that is how one channel quietly keeps the old
 *     contract while the other moves on);
 *   - a result with `structuredContent` and NO `_meta` still notifies, with
 *     `meta: undefined`, rather than being dropped;
 *   - the strict `schema_version` shape check still gates the `structuredContent`
 *     half, so `_meta` did not become a way past it;
 *   - `rehydrateEnvelope` puts the routed dataset back where the widget expects
 *     it, restoring ONLY over a `withheld` descriptor.
 */

import { describe, it, expect, vi, afterEach } from "vitest";
import {
  connectMcpApp,
  rehydrateEnvelope,
  APP_PAYLOAD_META_KEY,
  __resetMcpAppForTests,
  type McpToolResultParams,
} from "../mcpApp";

function makeMockApp() {
  let handler: ((params: McpToolResultParams) => void) | undefined;
  return {
    addEventListener: vi.fn(
      (event: "toolresult", h: (params: McpToolResultParams) => void) => {
        if (event === "toolresult") handler = h;
      },
    ),
    emitToolResult(params: McpToolResultParams) {
      handler?.(params);
    },
    connect: vi.fn(async () => {}),
    callServerTool: vi.fn(async (): Promise<McpToolResultParams> => ({ content: [] })),
  };
}

/** The model-visible half AFTER the server-side split: a stated descriptor, no rows. */
const SPLIT_ENVELOPE = {
  schema_version: "1",
  meta: { freshness: { last_pull: null }, provenance: [], alerts: [] },
  data: {
    report_profile: "standard_daily",
    rows: { withheld: "moved_to_app_channel", row_count: 2, columns: ["day", "clicks"] },
  },
};

/** The app half: the dataset, whole, under the one namespaced key. */
const APP_META = {
  [APP_PAYLOAD_META_KEY]: {
    __tool__: "get_daily_report",
    rows: [
      { day: "2026-07-01", clicks: 10 },
      { day: "2026-07-02", clicks: 12 },
    ],
  },
};

afterEach(() => {
  __resetMcpAppForTests();
  vi.restoreAllMocks();
});

describe("Story 50.6 — `_meta` delivery", () => {
  it("delivers (envelope, meta) over the SDK transport", async () => {
    const app = makeMockApp();
    const handle = connectMcpApp({ createApp: () => app });
    await handle.ready;
    const cb = vi.fn();
    handle.onToolResult(cb);

    app.emitToolResult({
      content: [],
      structuredContent: SPLIT_ENVELOPE,
      _meta: APP_META,
    } as unknown as McpToolResultParams);

    expect(cb).toHaveBeenCalledTimes(1);
    const [envelope, meta] = cb.mock.calls[0];
    expect(meta).toEqual(APP_META);
    // Rehydrated: the widget sees the dataset where it has always seen it.
    expect((envelope as Record<string, any>).data.rows).toEqual(
      APP_META[APP_PAYLOAD_META_KEY].rows,
    );
  });

  it("delivers the SAME pair over the legacy postMessage transport", async () => {
    const app = makeMockApp();
    app.connect = vi.fn(async () => {
      throw new Error("no SDK host");
    });
    const handle = connectMcpApp({ createApp: () => app });
    await handle.ready;
    const cb = vi.fn();
    handle.onToolResult(cb);

    window.dispatchEvent(
      new MessageEvent("message", {
        data: {
          type: "mcp:structuredContent",
          data: SPLIT_ENVELOPE,
          meta: APP_META,
        },
      }),
    );

    expect(cb).toHaveBeenCalledTimes(1);
    const [envelope, meta] = cb.mock.calls[0];
    expect(meta).toEqual(APP_META);
    expect((envelope as Record<string, any>).data.rows).toEqual(
      APP_META[APP_PAYLOAD_META_KEY].rows,
    );
  });

  it("still notifies when a result carries no `_meta` at all", async () => {
    const app = makeMockApp();
    const handle = connectMcpApp({ createApp: () => app });
    await handle.ready;
    const cb = vi.fn();
    handle.onToolResult(cb);

    const plain = { schema_version: "1", meta: {}, data: { rows: [] } };
    app.emitToolResult({
      content: [],
      structuredContent: plain,
    } as unknown as McpToolResultParams);

    // Dropping it would have been the easy regression: `_meta` is optional, and a
    // subscriber that only ever read the envelope must keep working untouched.
    expect(cb).toHaveBeenCalledWith(plain, undefined);
  });

  it("keeps the strict schema_version gate on the structuredContent half", async () => {
    const app = makeMockApp();
    const handle = connectMcpApp({ createApp: () => app });
    await handle.ready;
    const cb = vi.fn();
    handle.onToolResult(cb);

    // `_meta` present, envelope marker absent: still refused. `_meta` did not
    // become a way past the shape check.
    app.emitToolResult({
      content: [],
      structuredContent: { data: { rows: [] } },
      _meta: APP_META,
    } as unknown as McpToolResultParams);

    expect(cb).not.toHaveBeenCalled();
  });
});

describe("Story 50.6 — rehydrateEnvelope", () => {
  it("restores only where the server said it withheld something", () => {
    const envelope = {
      schema_version: "1",
      data: {
        rows: { withheld: "moved_to_app_channel", row_count: 1 },
        kept: "untouched",
      },
    };
    const out = rehydrateEnvelope(envelope, {
      [APP_PAYLOAD_META_KEY]: { rows: [{ a: 1 }], kept: "SHOULD NOT WIN" },
    });
    expect((out.data as Record<string, unknown>).rows).toEqual([{ a: 1 }]);
    // `kept` was never withheld, so the app channel does not get to overwrite it.
    expect((out.data as Record<string, unknown>).kept).toBe("untouched");
  });

  it("is a no-op without the app payload key", () => {
    const envelope = { schema_version: "1", data: { rows: [{ a: 1 }] } };
    expect(rehydrateEnvelope(envelope, { "toorow.result": {} })).toBe(envelope);
    expect(rehydrateEnvelope(envelope, undefined)).toBe(envelope);
  });
});
