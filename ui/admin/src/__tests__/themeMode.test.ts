/**
 * The two dark mechanisms must always describe the same scheme.
 *
 * The console has two, and neither is wrong: `[data-color-scheme]` plus the
 * media queries for the CSS custom properties, and the `.dark` class for every
 * Tailwind `dark:` utility — which is the entire chrome (StableSidebar, TopBar,
 * DataTree, GlobalScopeLayout, ScreenSandbox: 26 utilities across 5 files).
 *
 * `theme.css:7` declares `@custom-variant dark (&:where(.dark, .dark *))`, so
 * Tailwind follows the CLASS and nothing else. It does not see
 * `prefers-color-scheme`. The default preference is `system`, and `system` used
 * to remove the class — so on a dark OS, with nobody having touched a setting,
 * the page went dark and the chrome stayed light. Measured before the fix:
 * `--page #141416`, `body rgb(20,20,22)`, `.dark class false`.
 *
 * These tests hold the agreement, not the implementation.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  getThemeMode,
  initThemeMode,
  resolvedScheme,
  setThemeMode,
} from "../shell/themeMode";

/** A controllable `prefers-color-scheme`, with the listener jsdom does not give. */
function stubOs(dark: boolean) {
  const listeners: Array<(e: { matches: boolean }) => void> = [];
  vi.stubGlobal(
    "matchMedia",
    vi.fn().mockImplementation((q: string) => ({
      matches: dark && q.includes("dark"),
      media: q,
      addEventListener: (_: string, fn: (e: { matches: boolean }) => void) =>
        listeners.push(fn),
      removeEventListener: vi.fn(),
      addListener: vi.fn(),
      removeListener: vi.fn(),
      dispatchEvent: vi.fn(),
      onchange: null,
    })),
  );
  return { flipTo: (matches: boolean) => listeners.forEach((fn) => fn({ matches })) };
}

const html = () => document.documentElement;

beforeEach(() => {
  localStorage.clear();
  html().classList.remove("dark");
  html().removeAttribute("data-color-scheme");
  html().removeAttribute("data-mui-color-scheme");
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("theme mode — the two mechanisms agree", () => {
  it("follows a dark OS in system mode, which is the default", () => {
    stubOs(true);
    initThemeMode();
    expect(getThemeMode()).toBe("system");
    expect(resolvedScheme()).toBe("dark");
    // The defect: this was false, so 26 `dark:` utilities stayed light on a
    // page whose tokens had already gone dark.
    expect(html().classList.contains("dark")).toBe(true);
    // The attribute stays OFF in system mode — the sheets' media query decides,
    // and forcing the attribute would defeat a later OS change.
    expect(html().getAttribute("data-color-scheme")).toBeNull();
  });

  it("follows a light OS in system mode", () => {
    stubOs(false);
    initThemeMode();
    expect(resolvedScheme()).toBe("light");
    expect(html().classList.contains("dark")).toBe(false);
  });

  it("sets BOTH the class and the attributes when a mode is forced", () => {
    stubOs(false);
    setThemeMode("dark");
    expect(html().classList.contains("dark")).toBe(true);
    expect(html().getAttribute("data-color-scheme")).toBe("dark");
    expect(html().getAttribute("data-mui-color-scheme")).toBe("dark");

    setThemeMode("light");
    expect(html().classList.contains("dark")).toBe(false);
    expect(html().getAttribute("data-color-scheme")).toBe("light");
  });

  it("a forced light choice outranks a dark OS", () => {
    stubOs(true);
    setThemeMode("light");
    expect(resolvedScheme()).toBe("light");
    expect(html().classList.contains("dark")).toBe(false);
  });

  it("returning to system re-reads the OS instead of going light", () => {
    stubOs(true);
    setThemeMode("light");
    expect(html().classList.contains("dark")).toBe(false);
    setThemeMode("system");
    expect(getThemeMode()).toBe("system");
    expect(html().classList.contains("dark")).toBe(true);
    expect(html().getAttribute("data-color-scheme")).toBeNull();
  });

  // The subscription is guarded by a module-level flag so `initThemeMode()` can
  // be called twice without stacking listeners. That guard is correct in the
  // app — boot happens once — but it means a test needing a FRESH subscription
  // has to reload the module rather than call init again on a new stub.
  it("keeps following the OS when it flips while in system mode", async () => {
    vi.resetModules();
    const os = stubOs(false);
    const mod = await import("../shell/themeMode");
    mod.initThemeMode();
    expect(html().classList.contains("dark")).toBe(false);
    os.flipTo(true);
    expect(html().classList.contains("dark")).toBe(true);
  });

  it("ignores an OS flip once a mode is forced", async () => {
    vi.resetModules();
    const os = stubOs(false);
    const mod = await import("../shell/themeMode");
    mod.initThemeMode();
    mod.setThemeMode("light");
    os.flipTo(true);
    expect(html().classList.contains("dark")).toBe(false);
  });
});
