/**
 * Theme-mode preference — the user-preference switch the design system reserved
 * (see application.css: the `[data-mui-color-scheme]` attribute rules win over
 * the `@media (prefers-color-scheme)` default). Setting the attribute on <html>
 * forces light or dark against the OS; removing it falls back to the OS.
 *
 * Persisted in localStorage so the choice survives reloads. initThemeMode() runs
 * before React renders (main.tsx) to avoid a flash of the wrong scheme.
 */
export type ThemeMode = "light" | "dark" | "system";

const KEY = "toorow_theme_mode";
const ATTR = "data-mui-color-scheme";

export function getThemeMode(): ThemeMode {
  const v = localStorage.getItem(KEY);
  return v === "light" || v === "dark" ? v : "system";
}

/** Apply and persist a mode. "system" clears the override so the OS decides. */
export function setThemeMode(mode: ThemeMode): void {
  const root = document.documentElement;
  if (mode === "system") {
    root.removeAttribute(ATTR);
    localStorage.removeItem(KEY);
  } else {
    root.setAttribute(ATTR, mode);
    localStorage.setItem(KEY, mode);
  }
}

/** Reapply the stored preference at boot (no-op when following the OS). */
export function initThemeMode(): void {
  const v = localStorage.getItem(KEY);
  if (v === "light" || v === "dark") {
    document.documentElement.setAttribute(ATTR, v);
  }
}
