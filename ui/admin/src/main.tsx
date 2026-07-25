/**
 * Admin console entry point (Story 2.4, T2.3).
 * React 19 createRoot — standard SPA bootstrap.
 */
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import App from "./App";

const rootEl = document.getElementById("root");
if (!rootEl) {
  throw new Error("Root element #root not found");
}

createRoot(rootEl).render(
  <StrictMode>
    <App />
  </StrictMode>
);
