/**
 * Conversions card entrypoint (AD-11). Mirrors ui/cards/kpi/src/main.tsx exactly
 * (Story 9.10: shared SDK-first reader from @toorow/card-shell, legacy
 * channels kept as fallback inside the helper — G-11 origin validation preserved).
 */

import React from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
import { ErrorBoundary } from "./ErrorBoundary";
import { connectMcpApp, readInjectedEnvelope, NoEnvelope } from "@toorow/card-shell";
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

// AI-271 — NO ENVELOPE MEANS NO ANSWER TO SHOW, and that is what the card says.
// This line used to read `?? FIXTURE_ENVELOPE`, so a card opened outside its
// answer rendered a full set of figures the server had never founded — the
// `conversions` card kept showing a 50 EUR target after story 53.5 removed it.
// The fixture stays: it is what the tests render. The RENDER PATH no longer
// reaches it, and `test_cards_never_render_a_fixture` keeps it that way.
const injected = readInjectedEnvelope<CardEnvelope>();
if (injected) {
  render(injected);
} else {
  root.render(
    <React.StrictMode>
      <NoEnvelope title="Conversions" />
    </React.StrictMode>,
  );
}
