/**
 * Single-file entry point for the shared capability impact app (AD-11).
 *
 * Host detection is a feature check, never a brand check: the app asks the shared
 * `@toorow/shell` channel whether an MCP Apps host answered, and degrades to the
 * Console fallback if none did. No host name appears anywhere in this bundle.
 */

import React, { useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import { WidgetShell, connectMcpApp, readInjectedEnvelope } from "@toorow/shell";
import App from "./App";
import type { CapabilityImpactPayload } from "./types";

function Root() {
  const [payload, setPayload] = useState<CapabilityImpactPayload | null>(
    () => readInjectedEnvelope<{ data?: unknown }>() as CapabilityImpactPayload | null,
  );
  const [hostConnected, setHostConnected] = useState<boolean>(false);

  useEffect(() => {
    const handle = connectMcpApp();
    const unsubscribe = handle.onToolResult<CapabilityImpactPayload>((envelope) => {
      setPayload(envelope);
    });
    let cancelled = false;
    handle.ready
      .then(() => {
        if (!cancelled) setHostConnected(true);
      })
      .catch(() => {
        // A host that never answers is not an error: it is the fallback path.
        if (!cancelled) setHostConnected(false);
      });
    return () => {
      cancelled = true;
      unsubscribe();
    };
  }, []);

  return (
    <WidgetShell>
      <App payload={payload} hostConnected={hostConnected} />
    </WidgetShell>
  );
}

const container = document.getElementById("root");
if (!container) {
  throw new Error("Root element #root not found");
}
createRoot(container).render(
  <React.StrictMode>
    <Root />
  </React.StrictMode>,
);
