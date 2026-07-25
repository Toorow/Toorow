/**
 * Pacing Médiaplan card entrypoint (AD-11).
 * Pattern identique à ui/cards/dedup/src/main.tsx (Story 9.10 : shared SDK-first
 * reader depuis @toorow/card-shell, canaux legacy conservés en fallback).
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

// Shared reader (Story 9.10) : SDK ontoolresult et les canaux legacy alimentent
// le même callback. Souscrit avant le rendu initial pour ne pas manquer
// une livraison host tardive.
const mcpApp = connectMcpApp();
mcpApp.onToolResult<CardEnvelope>((envelope) => render(envelope));

render(readInjectedEnvelope<CardEnvelope>() ?? FIXTURE_ENVELOPE);
