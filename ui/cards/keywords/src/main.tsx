/**
 * Keywords card entrypoint (AD-11). Reads the AD-1 structuredContent envelope
 * delivered by the MCP Apps host, falling back to a local fixture for standalone dev/test.
 *
 * Story 9.10: delivery mirrors the KPI card (ui/cards/kpi/src/main.tsx) — the
 * shared @toorow/card-shell reader is SDK-first (@modelcontextprotocol/ext-apps)
 * with the legacy channels (global + mcp:structuredContent listener, G-11 origin
 * validation) kept as fallback inside the helper.
 */

import React from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
import { ErrorBoundary } from "./ErrorBoundary";
import { FIXTURE_ENVELOPE } from "./fixture";
import { connectMcpApp, readInjectedEnvelope } from "@toorow/card-shell";
import type { CardEnvelope } from "@toorow/card-shell";

const container = document.getElementById("root");
if (!container) {
  throw new Error("Root element #root not found");
}

const root = createRoot(container);

function render(envelope: CardEnvelope) {
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
mcpApp.onToolResult<CardEnvelope>((envelope) => render(envelope));

render(readInjectedEnvelope<CardEnvelope>() ?? FIXTURE_ENVELOPE);
