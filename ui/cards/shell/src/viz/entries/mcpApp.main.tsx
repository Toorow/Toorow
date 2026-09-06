/**
 * Story 50.5 -- the MCP App bundle's mount point.
 *
 * One self-contained resource (AD-11). No external font, no CDN, no runtime
 * fetch: `ui/scripts/bundle-check.mjs` fails the build on a load-bearing
 * `http(s):` reference, which is exactly the guarantee an MCP host needs before
 * it will render a widget under its own CSP.
 */

import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import McpAppVisualization from "./mcpApp";

const host = document.getElementById("root");
if (host) {
  const displayMode =
    document.documentElement.getAttribute("data-display-mode") ?? "inline";
  createRoot(host).render(
    <StrictMode>
      <McpAppVisualization displayMode={displayMode} />
    </StrictMode>,
  );
}
