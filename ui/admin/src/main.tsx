/**
 * Admin console entry point (Story 2.4, T2.3).
 * React 19 createRoot — standard SPA bootstrap.
 */
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { initThemeMode } from "./shell/themeMode";

// Apply the stored light/dark preference before the first paint (no FOUC).
initThemeMode();

const rootEl = document.getElementById("root");
if (!rootEl) {
  throw new Error("Root element #root not found");
}

const root = createRoot(rootEl);

/**
 * Gate G1 — the component sheet, at `/debug/components`.
 *
 * Mounted here rather than inside `App`, and through a dynamic import, so that
 * `App.tsx` is never evaluated on this route and none of the legacy
 * stylesheets it pulls in are loaded. That is not tidiness: `application.css`
 * is unlayered, and an unlayered rule beats every Tailwind utility, so its
 * `button, input { font: inherit }` (l. 163) silently overrides the type of
 * every component in the library. Judging the components with it loaded would
 * be judging the legacy sheet.
 *
 * Development builds only — the branch is dropped from the bundle.
 */
const isSheet =
  import.meta.env.DEV &&
  (window.location.pathname === "/debug/components" ||
    window.location.pathname === "/debug/components/");

/**
 * The screen sandbox — one migrated screen, rendered on its own so it can be
 * looked at. Same reasoning as the sheet: `App.tsx` is never evaluated, so no
 * legacy stylesheet is loaded and the screen is judged on the component
 * library alone.
 */
const isSandbox =
  import.meta.env.DEV && window.location.pathname.replace(/\/$/, "") === "/debug/screen";

if (isSheet) {
  void import("./shell/ComponentSheet").then(({ default: ComponentSheet }) => {
    root.render(
      <StrictMode>
        <ComponentSheet />
      </StrictMode>,
    );
  });
} else if (isSandbox) {
  void import("./shell/ScreenSandbox").then(({ default: ScreenSandbox }) => {
    root.render(
      <StrictMode>
        <ScreenSandbox />
      </StrictMode>,
    );
  });
} else {
  void import("./App").then(({ default: App }) => {
    root.render(
      <StrictMode>
        <App />
      </StrictMode>,
    );
  });
}
