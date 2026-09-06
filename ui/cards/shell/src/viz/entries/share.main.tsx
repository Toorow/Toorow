/**
 * Story 50.5 -- the share bundle's mount point.
 *
 * It mounts into `#widget-mount`, the id the public share page already serves
 * (`server/core/rendus_api.py`, an EMPTY div inside a `.widget-frame` DIV -- not
 * a frame, and no runtime mounts into it today). Serving this bundle from that
 * page is Story 50.7; this file is the half that has to exist first.
 *
 * It falls back to `#root` so the bundle is testable standalone.
 */

import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import ShareVisualization from "./share";
import { followColorScheme } from "./shareTheme";

// Before the first paint: the runtime reads `.dark` once per render, and a
// recipient on a dark OS must not see a light flash (G14-T05, 2026-09-04).
followColorScheme();

const host = document.getElementById("widget-mount") ?? document.getElementById("root");
if (host) {
  createRoot(host).render(
    <StrictMode>
      <ShareVisualization />
    </StrictMode>,
  );
}
