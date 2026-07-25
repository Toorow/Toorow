/**
 * Widget entrypoint (T7.2 / AD-11).
 *
 * Story 9.10: data delivery goes through the shared @toorow/shell reader
 * (this widget was the original G-11 implementation — the pattern now lives in
 * ui/shell/src/mcpApp.ts and is consumed here like the cards do):
 *   - SDK-first: official @modelcontextprotocol/ext-apps App; the host delivers
 *     the tool's structuredContent via ui/notifications/tool-result.
 *   - Legacy fallback (kept, nothing deleted):
 *       1. window.__MCP_STRUCTURED_CONTENT__  (global injection)
 *       2. a `message` event: { type: 'mcp:structuredContent', data: <envelope> }
 *          with G-11 origin validation — accept when origin matches
 *          window.location.ancestorOrigins[0]; when ancestorOrigins is
 *          unavailable (sandboxed iframe, Firefox, tests), fall back to a
 *          strict shape check on schema_version.
 *   For standalone dev/test (no host), it falls back to a local fixture so the
 *   widget always renders (never a blank screen).
 *
 * The envelope shape is the AD-1 structuredContent: { schema_version, meta, data }.
 */

import React from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
import { ErrorBoundary } from "./ErrorBoundary";
import { FIXTURE_ENVELOPE } from "./fixture";
import { connectMcpApp, readInjectedEnvelope } from "@toorow/shell";
import type { DailyReportEnvelope } from "./types";

const container = document.getElementById("root");
if (!container) {
  throw new Error("Root element #root not found");
}

const root = createRoot(container);

function render(envelope: DailyReportEnvelope) {
  root.render(
    <React.StrictMode>
      <ErrorBoundary>
        <App envelope={envelope} />
      </ErrorBoundary>
    </React.StrictMode>,
  );
}

// Shared reader (Story 9.10): SDK ontoolresult and the legacy channels feed
// the same callback. Subscribed before the initial render so no late host
// delivery is missed.
const mcpApp = connectMcpApp();
mcpApp.onToolResult<DailyReportEnvelope>((envelope) => render(envelope));

render(readInjectedEnvelope<DailyReportEnvelope>() ?? FIXTURE_ENVELOPE);
