/**
 * Theme-mode preference — and the one place the console's TWO dark mechanisms
 * are kept in agreement.
 *
 * There are two, and neither is wrong:
 *
 *   `[data-color-scheme]` + `@media (prefers-color-scheme)`   the CSS custom
 *       properties — `shell/application.css`. Read by every legacy sheet.
 *       (This cited `styles/tokens.css` until 2026-08-04; that sheet was a
 *       duplicate reachable only through the dead `app.css` manifest, and both
 *       were deleted. `application.css` was always the loaded definition.)
 *   `.dark` class                                             Tailwind, via
 *       `styles/theme.css:7` — `@custom-variant dark (&:where(.dark, .dark *))`.
 *       Read by every `dark:` utility, i.e. the whole chrome: StableSidebar,
 *       TopBar, DataTree, GlobalScopeLayout, ScreenSandbox.
 *
 * THE DEFECT THIS FILE NOW CLOSES. The Tailwind variant keys on the class and
 * on NOTHING else — it does not follow `prefers-color-scheme`. An earlier
 * version of `setThemeMode` removed the class in `system` mode, which is the
 * DEFAULT when nothing is stored. So on a dark OS, with nobody having touched a
 * setting: the tokens went dark, the page went dark, and every `dark:` utility
 * stayed light. Measured in a real browser, OS dark, preference system:
 *
 *     --page  #141416      body  rgb(20,20,22)      .dark class  false
 *
 * The chrome rendered its light styles on a dark page. That is the mirror image
 * of the layering defect fixed in `application.css` the same day, and the two
 * have the same shape: two mechanisms, one of them silently not following.
 *
 * THE FIX. `system` no longer means "remove everything"; it means "resolve the
 * OS and keep following it". The attributes still come off — the media queries
 * in the sheets do that half correctly on their own — but the class is set from
 * `matchMedia` and kept in step when the OS flips. So both mechanisms always
 * describe the same scheme, by construction rather than by discipline.
 *
 * Persisted in localStorage so the choice survives reloads. `initThemeMode()`
 * runs before React renders (`main.tsx`) to avoid a flash of the wrong scheme.
 *
 * ONE STATE SOURCE, TWO CONTROLS (2026-08-18). The appearance switch is now
 * offered in two places — the rail's account menu and User Account >
 * Preferences — and two components each holding their own `useState(getThemeMode())`
 * would be a third mechanism to keep in step, on a file whose entire subject is
 * two mechanisms drifting. `useThemePreference()` below is a subscription to
 * THIS module: every mount reads the same store, `setThemeMode` notifies all of
 * them, and the two controls cannot sit in different positions.
 */
import { useSyncExternalStore } from "react";

export type ThemeMode = "light" | "dark" | "system";

const KEY = "toorow_theme_mode";
const ATTRS = ["data-mui-color-scheme", "data-color-scheme"];
const MEDIA = "(prefers-color-scheme: dark)";

/** Guarded so `initThemeMode()` can be called more than once without stacking. */
let watching = false;

/** Everything currently rendering the preference. Notified on every change. */
const listeners = new Set<() => void>();

function announce(): void {
  for (const listener of listeners) listener();
}

function query(): MediaQueryList | null {
  return typeof window !== "undefined" && typeof window.matchMedia === "function"
    ? window.matchMedia(MEDIA)
    : null;
}

function systemPrefersDark(): boolean {
  return query()?.matches === true;
}

/** The Tailwind side. Always called with the scheme that is actually in force. */
function applyClass(dark: boolean): void {
  document.documentElement.classList.toggle("dark", dark);
}

export function getThemeMode(): ThemeMode {
  const v = localStorage.getItem(KEY);
  return v === "light" || v === "dark" ? v : "system";
}

/** The scheme actually rendered right now, which is what a test should assert. */
export function resolvedScheme(): "light" | "dark" {
  const mode = getThemeMode();
  return mode === "system" ? (systemPrefersDark() ? "dark" : "light") : mode;
}

/** Apply and persist a mode. `system` follows the OS — and keeps following it. */
export function setThemeMode(mode: ThemeMode): void {
  const root = document.documentElement;
  if (mode === "system") {
    // The attributes come off so the sheets' media queries decide. The class
    // does NOT: nothing else would tell Tailwind what the OS chose.
    ATTRS.forEach((attr) => root.removeAttribute(attr));
    localStorage.removeItem(KEY);
    applyClass(systemPrefersDark());
  } else {
    ATTRS.forEach((attr) => root.setAttribute(attr, mode));
    localStorage.setItem(KEY, mode);
    applyClass(mode === "dark");
  }
  announce();
}

/** Reapply the stored preference at boot, and follow the OS while in `system`. */
export function initThemeMode(): void {
  const stored = localStorage.getItem(KEY);
  if (stored === "light" || stored === "dark") {
    ATTRS.forEach((attr) => document.documentElement.setAttribute(attr, stored));
    applyClass(stored === "dark");
  } else {
    applyClass(systemPrefersDark());
  }

  if (watching) return;
  const mq = query();
  // `addEventListener` on a MediaQueryList is not in every jsdom version, so the
  // subscription is optional and its absence must not break boot.
  if (!mq || typeof mq.addEventListener !== "function") return;
  mq.addEventListener("change", (event) => {
    // Only while following the OS. A forced choice outranks it, which is the
    // whole point of having a switch.
    if (getThemeMode() === "system") applyClass(event.matches);
    announce();
  });
  watching = true;
}

// ---------------------------------------------------------------------------
// The store, and the one hook every appearance control reads
// ---------------------------------------------------------------------------

/**
 * What a control has to render, as one comparable value: the stored MODE and
 * the scheme actually ON SCREEN, which `A.7.4` requires to be told apart.
 */
export function themeSnapshot(): string {
  return `${getThemeMode()}:${resolvedScheme()}`;
}

/**
 * Subscribe to the preference — to this module's own changes AND to the OS.
 *
 * The OS is listened to here rather than relying on `initThemeMode()`: that
 * runs once, in `main.tsx`, and a control mounted in a test or in the sandbox
 * would otherwise sit still while `system` moved the page underneath it. Both
 * paths end in `announce()`, so a subscriber never has to know which one fired.
 */
export function subscribeThemeMode(listener: () => void): () => void {
  listeners.add(listener);
  const mq = query();
  const relay = () => listener();
  if (mq && typeof mq.addEventListener === "function") mq.addEventListener("change", relay);
  return () => {
    listeners.delete(listener);
    if (mq && typeof mq.removeEventListener === "function") mq.removeEventListener("change", relay);
  };
}

/**
 * The preference, for a React control. `mode` is what is stored, `scheme` is
 * what is rendered — never collapsed into one, because `system` on a dark OS is
 * a dark page and a two-position switch has to show the page.
 */
export function useThemePreference(): {
  mode: ThemeMode;
  scheme: "light" | "dark";
  setMode: (mode: ThemeMode) => void;
} {
  const snapshot = useSyncExternalStore(subscribeThemeMode, themeSnapshot, themeSnapshot);
  const [mode, scheme] = snapshot.split(":") as [ThemeMode, "light" | "dark"];
  return { mode, scheme, setMode: setThemeMode };
}
