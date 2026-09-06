/**
 * mcpApp — shared iframe↔host channel for all widgets/cards (Story 9.10, AC 4).
 *
 * SDK-first: the official `@modelcontextprotocol/ext-apps` `App` (spec
 * 2026-01-26) over `PostMessageTransport` is the primary channel.
 *   - Inbound:  `app.addEventListener("toolresult", …)` (`ui/notifications/tool-result`)
 *     delivers the tool's `structuredContent` (the AD-1 envelope) to `onToolResult`
 *     subscribers. (The `ontoolresult` setter is @deprecated in ext-apps 1.7.4.)
 *   - Outbound: `app.callServerTool({ name, arguments })` — a REAL awaitable RPC.
 *     Transport failures reject; tool-level failures come back as
 *     `isError: true` results and are normalized to rejections here, so
 *     FeedbackBar/CardFeedbackBar's designed error state (F-11) is reachable.
 *
 * Legacy fallback (NO channel deleted in this story — removal is deferred until
 * the SDK path is verified live in Claude, see docs/deferred-work.md):
 *   - Inbound:  `window.__MCP_STRUCTURED_CONTENT__` global (read via
 *     `readInjectedEnvelope`) + `mcp:structuredContent` message listener with
 *     the G-11 origin validation preserved AS-IS (ancestorOrigins match, else
 *     strict payload shape check on `schema_version`).
 *   - Outbound: raw `window.parent.postMessage({ type: "callServerTool", ... })`
 *     fire-and-forget with graceful no-op (Vitest, Storybook, older hosts).
 *
 * The legacy inbound listener stays wired even while the SDK handshake is
 * pending, so an envelope injected by an older host is never missed. The SDK
 * `toolresult` listener is registered (via `addEventListener`) BEFORE `connect()`
 * (the host may fire the notification immediately after the handshake).
 *
 * AD-2: this helper carries no card/module-specific logic — it only moves the
 * envelope and tool calls between the iframe and the host.
 */

import { App, PostMessageTransport } from "@modelcontextprotocol/ext-apps";
import type { McpUiToolResultNotification } from "@modelcontextprotocol/ext-apps";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

/**
 * Params of the `ui/notifications/tool-result` notification — the standard MCP
 * `CallToolResult` shape: { content, structuredContent?, isError?, _meta? }.
 * (Verified against the installed ext-apps 1.7.4 dist/src/spec.types.d.ts.)
 */
export type McpToolResultParams = McpUiToolResultNotification["params"];

/** Which channel won: the official SDK handshake, or the legacy postMessage. */
export type McpChannelMode = "sdk" | "legacy";

/**
 * Subscriber callback — receives the structuredContent envelope object, and
 * (Story 50.6) the result `_meta` that arrived with it.
 *
 * WHY THE SECOND ARGUMENT EXISTS. Until Story 50.6 this channel read
 * `params.structuredContent` and dropped `_meta` on the floor, even though the
 * `CallToolResult` type has always named the field. That single fact is why every
 * dataset lived in `structuredContent`: there was no other channel. The MCP Apps
 * contract requires the opposite — `structuredContent` is model-visible and stays
 * concise, widget-only data belongs in `_meta` — so the server now routes row
 * arrays into `_meta` and the widget has to be able to see them.
 *
 * It is a SECOND argument, not a replacement, so every existing subscriber keeps
 * compiling and keeps working unchanged.
 */
export type EnvelopeCallback = (
  envelope: Record<string, unknown>,
  meta?: Record<string, unknown>,
) => void;

export interface McpToolResultFailure {
  message: string;
}

/** The single `_meta` key under which the server routes a legacy tool's app data. */
export const APP_PAYLOAD_META_KEY = "toorow.app_payload";

/** The single `_meta` key under which an Analyze Result's app data travels. */
export const RESULT_META_KEY = "toorow.result";

/**
 * Put back what the model-channel split moved out, so a widget sees one envelope.
 *
 * The server splits ONE envelope into two channels: `structuredContent` keeps the
 * concise summary and a stated descriptor where a dataset used to be, and `_meta`
 * carries the dataset whole. A widget does not care about that split — it wants
 * the envelope. Rehydrating here, once, in the transport, means no card component
 * has to learn about `_meta`, and a future tool that splits differently does not
 * become a per-widget migration.
 *
 * Only keys the split actually moved are restored, and only over a `withheld`
 * descriptor: this never invents data and never overwrites a real value.
 */
export function rehydrateEnvelope(
  envelope: Record<string, unknown>,
  meta?: Record<string, unknown>,
): Record<string, unknown> {
  const payload = meta?.[APP_PAYLOAD_META_KEY];
  if (!payload || typeof payload !== "object") return envelope;
  const payloadRecord = payload as Record<string, unknown>;
  const moved =
    payloadRecord.kind === "render" && payloadRecord.schema_version === 1
      ? payloadRecord.moved
      : payloadRecord;
  if (!moved || typeof moved !== "object") return envelope;
  const data = envelope.data;
  if (!data || typeof data !== "object") return envelope;
  const merged: Record<string, unknown> = { ...(data as Record<string, unknown>) };
  for (const [path, value] of Object.entries(moved as Record<string, unknown>)) {
    if (path === "__tool__") continue;
    const segments = path.split(".");
    let cursor: Record<string, unknown> = merged;
    for (let i = 0; i < segments.length - 1; i += 1) {
      const next = cursor[segments[i]];
      if (!next || typeof next !== "object") break;
      cursor[segments[i]] = { ...(next as Record<string, unknown>) };
      cursor = cursor[segments[i]] as Record<string, unknown>;
    }
    const leaf = segments[segments.length - 1];
    const current = cursor[leaf] as Record<string, unknown> | undefined;
    // Restore only where the server said it withheld something. A key the split
    // did not touch keeps whatever the model channel carried.
    if (current && typeof current === "object" && "withheld" in current) {
      cursor[leaf] = value;
    }
  }
  return { ...envelope, data: merged };
}

/**
 * Structural subset of the ext-apps `App` used by this helper. The real `App`
 * satisfies it; Vitest mocks implement it with the REAL SDK message shapes
 * (AI-54): `addEventListener("toolresult", …)` receives CallToolResult-shaped
 * params, and `callServerTool` takes CallToolRequest params `{ name, arguments }`.
 *
 * The `on*` setters (e.g. `ontoolresult`) are @deprecated in ext-apps 1.7.4 in
 * favour of `addEventListener` (composes with other listeners, supports
 * `removeEventListener` cleanup). We track the official convention (Story 9.10)
 * and use only the non-deprecated multi-listener API. The signature mirrors the
 * real `App.addEventListener<K extends keyof AppEventMap>` narrowed to the one
 * event we consume — `AppEventMap["toolresult"] === McpUiToolResultNotification["params"]`.
 */
export interface McpSdkApp {
  addEventListener(
    event: "toolresult",
    handler: (params: McpToolResultParams) => void,
  ): void;
  connect(
    transport?: PostMessageTransport,
    options?: { timeout?: number },
  ): Promise<void>;
  callServerTool(
    params: { name: string; arguments?: Record<string, unknown> },
    options?: { timeout?: number },
  ): Promise<McpToolResultParams>;
}

export interface ConnectMcpAppOptions {
  /** App identification sent in the `ui/initialize` handshake. */
  appInfo?: { name: string; version: string };
  /**
   * Budget for the `ui/initialize` handshake. When it elapses without a host
   * response (older host, Vitest, Storybook, standalone dev) the handle
   * settles on the legacy channel.
   */
  connectTimeoutMs?: number;
  /**
   * Test seam: factory for the SDK App. Mocks MUST mirror the real ext-apps
   * surface (see {@link McpSdkApp}). When provided, no transport is created —
   * the mock owns the (absence of a) wire.
   */
  createApp?: () => McpSdkApp;
}

export interface McpAppHandle {
  /** Settles once the SDK handshake attempt resolves: "sdk" or "legacy". */
  readonly ready: Promise<McpChannelMode>;
  /** Current channel; "pending" until `ready` settles. */
  readonly mode: McpChannelMode | "pending";
  /**
   * Subscribe to envelope deliveries (both channels). Returns unsubscribe.
   * `T` is the caller's envelope type (e.g. CardEnvelope) — the wire carries
   * the AD-1 structuredContent object, validated for a `schema_version` marker.
   */
  onToolResult<T = Record<string, unknown>>(
    cb: (envelope: T, meta?: Record<string, unknown>) => void,
  ): () => void;
  /** Subscribe to inbound `isError` results, including those with no envelope. */
  onToolResultError(cb: (failure: McpToolResultFailure) => void): () => void;
  /**
   * Call a server tool. SDK path: awaitable RPC — rejects on transport failure
   * AND on `isError: true` tool results (F-11). Legacy path: fire-and-forget
   * postMessage that resolves immediately (graceful no-op outside a host).
   */
  callServerTool(
    tool: string,
    args: Record<string, unknown>,
  ): Promise<McpToolResultParams | null>;
}

// ---------------------------------------------------------------------------
// Legacy inbound — window global + mcp:structuredContent message listener
// (G-11 origin validation preserved AS-IS from the pre-9.10 entrypoints)
// ---------------------------------------------------------------------------

declare global {
  interface Window {
    __MCP_STRUCTURED_CONTENT__?: unknown;
  }
}

/**
 * Read the host-injected global envelope (legacy channel 1). Returns null when
 * absent or when it does not carry a `data` payload — callers fall back to
 * their local fixture for standalone dev/test.
 */
export function readInjectedEnvelope<T extends { data?: unknown }>(): T | null {
  const injected = window.__MCP_STRUCTURED_CONTENT__ as T | undefined | null;
  if (injected && typeof injected === "object" && injected.data) return injected;
  return null;
}

/**
 * G-11: strict shape check — used when ancestorOrigins is not available
 * (sandboxed iframe, Firefox, tests). Returns true only for well-formed
 * mcp:structuredContent payloads.
 */
function isValidStructuredContentPayload(payload: unknown): payload is {
  type: "mcp:structuredContent";
  data: Record<string, unknown>;
} {
  if (payload === null || typeof payload !== "object") return false;
  const p = payload as Record<string, unknown>;
  return (
    p.type === "mcp:structuredContent" &&
    p.data !== null &&
    p.data !== undefined &&
    typeof p.data === "object" &&
    !!(p.data as Record<string, unknown>).schema_version
  );
}

function makeLegacyMessageListener(
  notify: EnvelopeCallback,
): (event: MessageEvent) => void {
  return (event: MessageEvent) => {
    const payload = event?.data;

    // G-11: origin validation.
    const expectedOrigin =
      window.location.ancestorOrigins && window.location.ancestorOrigins.length > 0
        ? window.location.ancestorOrigins[0]
        : null;

    // Story 50.6: the legacy transport carries the SAME pair as the SDK one. A
    // widget must not behave differently by transport — that is how one channel
    // quietly keeps the old contract while the other moves on.
    const meta =
      payload && typeof payload === "object" && payload.meta && typeof payload.meta === "object"
        ? (payload.meta as Record<string, unknown>)
        : undefined;

    if (expectedOrigin !== null) {
      // Non-sandboxed iframe: enforce origin match.
      if (event.origin !== expectedOrigin) return;
      // Origin matches — accept any structuredContent message from this origin.
      if (
        payload &&
        typeof payload === "object" &&
        payload.type === "mcp:structuredContent" &&
        payload.data
      ) {
        notify(payload.data as Record<string, unknown>, meta);
      }
    } else {
      // Sandboxed iframe / tests / Firefox: ancestorOrigins unavailable.
      // Fall back to strict payload shape validation.
      if (isValidStructuredContentPayload(payload)) {
        notify(payload.data, meta);
      }
    }
  };
}

// ---------------------------------------------------------------------------
// Legacy outbound — raw postMessage fire-and-forget (pre-9.10 FeedbackBar
// helper, preserved AS-IS including the F-4 targetOrigin least-privilege)
// ---------------------------------------------------------------------------

async function legacyCallServerTool(
  tool: string,
  args: Record<string, unknown>,
): Promise<null> {
  try {
    // review-epic-5 F-4: target the embedding host's origin when the browser
    // exposes it (least privilege) — "*" only as the sandboxed-iframe fallback
    // where ancestorOrigins is unavailable. The payload carries no secrets.
    const targetOrigin =
      (window.location.ancestorOrigins && window.location.ancestorOrigins[0]) || "*";
    window.parent.postMessage(
      {
        type: "callServerTool",
        tool,
        arguments: args,
      },
      targetOrigin,
    );
  } catch {
    // Not in MCP Apps host — graceful no-op (Vitest, Storybook, etc.)
  }
  return null;
}

// ---------------------------------------------------------------------------
// Connection
// ---------------------------------------------------------------------------

const DEFAULT_APP_INFO = { name: "connector-widget", version: "1.0.0" };
const DEFAULT_CONNECT_TIMEOUT_MS = 2000;

class McpAppConnection implements McpAppHandle {
  readonly ready: Promise<McpChannelMode>;

  private subscribers = new Set<EnvelopeCallback>();
  private errorSubscribers = new Set<(failure: McpToolResultFailure) => void>();
  private pendingResult: {
    envelope: Record<string, unknown>;
    meta?: Record<string, unknown>;
  } | null = null;
  private pendingFailure: McpToolResultFailure | null = null;
  private sdkApp: McpSdkApp | null = null;
  private modeInternal: McpChannelMode | "pending" = "pending";
  private legacyListener: (event: MessageEvent) => void;

  constructor(options: ConnectMcpAppOptions = {}) {
    // Legacy inbound stays wired from t0 — an older host may inject the
    // envelope before (or without) any SDK handshake.
    this.legacyListener = makeLegacyMessageListener((envelope, meta) =>
      this.notify(envelope, meta),
    );
    window.addEventListener("message", this.legacyListener);
    this.ready = this.connectSdk(options);
  }

  get mode(): McpChannelMode | "pending" {
    return this.modeInternal;
  }

  onToolResult<T = Record<string, unknown>>(
    cb: (envelope: T, meta?: Record<string, unknown>) => void,
  ): () => void {
    // The wire delivers a schema_version-validated envelope object; the
    // caller's T narrows it (AD-1 contract — server and fixture share it).
    const subscriber = cb as unknown as EnvelopeCallback;
    this.subscribers.add(subscriber);
    if (this.pendingResult) {
      const pending = this.pendingResult;
      this.pendingResult = null;
      subscriber(rehydrateEnvelope(pending.envelope, pending.meta), pending.meta);
    }
    return () => {
      this.subscribers.delete(subscriber);
    };
  }

  onToolResultError(cb: (failure: McpToolResultFailure) => void): () => void {
    this.errorSubscribers.add(cb);
    if (this.pendingFailure) {
      const pending = this.pendingFailure;
      this.pendingFailure = null;
      cb(pending);
    }
    return () => {
      this.errorSubscribers.delete(cb);
    };
  }

  async callServerTool(
    tool: string,
    args: Record<string, unknown>,
  ): Promise<McpToolResultParams | null> {
    const mode = await this.ready;
    if (mode === "sdk" && this.sdkApp) {
      // Real acknowledgement: transport failures throw; tool-level failures
      // return isError results — normalize both to a rejection so the caller's
      // designed error state (F-11) fires in either case.
      const result = await this.sdkApp.callServerTool({
        name: tool,
        arguments: args,
      });
      if (result && result.isError) {
        throw new Error(extractErrorText(result, tool));
      }
      return result;
    }
    return legacyCallServerTool(tool, args);
  }

  /** Test-only: detach the legacy listener and drop subscribers. */
  dispose(): void {
    window.removeEventListener("message", this.legacyListener);
    this.subscribers.clear();
    this.errorSubscribers.clear();
    this.pendingResult = null;
    this.pendingFailure = null;
    this.sdkApp = null;
  }

  private notify(
    envelope: Record<string, unknown>,
    meta?: Record<string, unknown>,
  ): void {
    // Rehydrate first, then hand BOTH halves over: a subscriber that only ever
    // read the envelope sees exactly what it saw before the model-channel split,
    // and a subscriber that wants the Result handle reads `meta`.
    const rehydrated = rehydrateEnvelope(envelope, meta);
    if (this.subscribers.size === 0) {
      this.pendingResult = { envelope, meta };
      return;
    }
    for (const cb of this.subscribers) cb(rehydrated, meta);
  }

  private async connectSdk(
    options: ConnectMcpAppOptions,
  ): Promise<McpChannelMode> {
    try {
      const app: McpSdkApp = options.createApp
        ? options.createApp()
        : new App(options.appInfo ?? DEFAULT_APP_INFO, {});
      // Register BEFORE connect() — the host may fire
      // ui/notifications/tool-result right after the handshake completes.
      // addEventListener (not the @deprecated `ontoolresult` setter) is the
      // official ext-apps 1.7.4 convention — Story 9.10 alignment.
      app.addEventListener("toolresult", (params) => {
        if (params.isError) {
          const failure = { message: extractErrorText(params, "render") };
          if (this.errorSubscribers.size === 0) {
            this.pendingFailure = failure;
            return;
          }
          for (const cb of this.errorSubscribers) cb(failure);
          return;
        }
        const sc = params.structuredContent;
        // Story 50.6: `_meta` is forwarded alongside `structuredContent` instead
        // of being discarded. The strict `schema_version` shape check stays on the
        // `structuredContent` half only — `_meta` is app data, not an AD-1
        // envelope, and gating it on the envelope marker would drop it whenever a
        // host delivered the two in separate notifications.
        const meta =
          params._meta && typeof params._meta === "object"
            ? (params._meta as Record<string, unknown>)
            : undefined;
        // Only AD-1 envelopes (schema_version marker) reach subscribers —
        // mirrors the G-11 strict shape check on the legacy channel.
        if (
          sc &&
          typeof sc === "object" &&
          (sc as Record<string, unknown>).schema_version
        ) {
          this.notify(sc as Record<string, unknown>, meta);
        }
      });
      const transport = options.createApp
        ? undefined
        : new PostMessageTransport(window.parent, window.parent);
      await app.connect(transport, {
        timeout: options.connectTimeoutMs ?? DEFAULT_CONNECT_TIMEOUT_MS,
      });
      this.sdkApp = app;
      this.modeInternal = "sdk";
      return "sdk";
    } catch (err) {
      // No SDK-capable host (older host, Vitest, Storybook, standalone dev):
      // the legacy channels — already wired — carry the traffic. Surface the
      // reason (warn, not error, to keep Vitest output clean) instead of
      // swallowing it silently — a real handshake regression must be diagnosable.
      if (typeof console !== "undefined") {
        console.warn(
          "[mcpApp] SDK connect failed, falling back to legacy:",
          err,
        );
      }
      this.modeInternal = "legacy";
      return "legacy";
    }
  }
}

function extractErrorText(result: McpToolResultParams, tool: string): string {
  const content = Array.isArray(result.content) ? result.content : [];
  for (const block of content) {
    if (
      block &&
      typeof block === "object" &&
      (block as { type?: unknown }).type === "text" &&
      typeof (block as { text?: unknown }).text === "string"
    ) {
      return (block as { text: string }).text;
    }
  }
  return `The server tool ${tool} failed.`;
}

// ---------------------------------------------------------------------------
// Public API — one connection per iframe (singleton)
// ---------------------------------------------------------------------------

let singleton: McpAppConnection | null = null;

/**
 * Connect (once) to the MCP Apps host. Idempotent: repeated calls return the
 * same handle; options are only honored on the first call. Entrypoints call
 * this at module scope; FeedbackBar/CardFeedbackBar reach the same connection
 * through the module-level {@link callServerTool}.
 */
export function connectMcpApp(options?: ConnectMcpAppOptions): McpAppHandle {
  if (!singleton) {
    singleton = new McpAppConnection(options);
  }
  return singleton;
}

/**
 * React-friendly accessor for the shared connection (same singleton handle,
 * stable identity across renders). The ext-apps `useApp` hook is intentionally
 * not used: our entrypoints connect at module scope, before React mounts, so
 * the host's tool-result notification is never missed by a late mount.
 */
export function useMcpApp(options?: ConnectMcpAppOptions): McpAppHandle {
  return connectMcpApp(options);
}

/**
 * Call a server tool through the shared connection. When no connection was
 * established by the entrypoint (Vitest component tests, Storybook), falls
 * back to the legacy fire-and-forget postMessage (graceful no-op).
 */
export async function callServerTool(
  tool: string,
  args: Record<string, unknown>,
): Promise<McpToolResultParams | null> {
  if (singleton) {
    return singleton.callServerTool(tool, args);
  }
  return legacyCallServerTool(tool, args);
}

/** Test-only: drop the singleton and its window listener between tests. */
export function __resetMcpAppForTests(): void {
  if (singleton) {
    singleton.dispose();
    singleton = null;
  }
}
