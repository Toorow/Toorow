/**
 * Connectors card entrypoint (AD-11). Mirrors ui/cards/journey/src/main.tsx exactly
 * (Story 9.10: shared SDK-first reader from @toorow/card-shell, legacy
 * channels kept as fallback inside the helper — G-11 origin validation preserved).
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
