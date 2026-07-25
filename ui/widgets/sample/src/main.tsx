import React from "react";
import { createRoot } from "react-dom/client";
import App from "./App";

// Single-file widget entrypoint (AD-11). No external imports — everything is
// bundled and inlined into dist/index.html by vite-plugin-singlefile.
const container = document.getElementById("root");
if (!container) {
  throw new Error("Root element #root not found");
}
createRoot(container).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>
);
