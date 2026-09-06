/**
 * Appearance — one switch between day and night.
 *
 * It was three pills of equal weight — `Light` `Dark` `System` — for a setting
 * with two outcomes, and nothing pinned any of it. Jean, 2026-08-11: *"le menu
 * apparence n'est pas propre, mettre un truc jour et nuit avec des icônes et un
 * switch"*.
 *
 * `themeMode.test.ts` next door holds the STORE: that the class and the
 * attributes never describe two different schemes. This file holds the CONTROL,
 * and the one claim the pills could not make:
 *
 *   the switch shows the scheme that is ON SCREEN, not the mode that is stored.
 *
 * On a dark OS with the default preference, `System` looked selected and nothing
 * in that row said the page was dark. `resolvedScheme()` is the only honest
 * source for a two-position control, and `system` stops being a third button of
 * equal weight: it is the state you are in, named in words, and the way back is
 * offered only once there is something to come back from.
 */
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import StableSidebar from "../shell/StableSidebar";
import { RouterProvider } from "../shell/router";
import { getThemeMode } from "../shell/themeMode";

/** A controllable `prefers-color-scheme`, with the listener jsdom does not give. */
function stubOs(dark: boolean) {
  vi.stubGlobal(
    "matchMedia",
    vi.fn().mockImplementation((query: string) => ({
      matches: dark && query.includes("dark"),
      media: query,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      addListener: vi.fn(),
      removeListener: vi.fn(),
      dispatchEvent: vi.fn(),
      onchange: null,
    })),
  );
}

/** The rail asks the platform-admin route on mount; it decides one menu entry
 *  and nothing this file measures. */
function stubApi() {
  vi.stubGlobal(
    "fetch",
    vi.fn(() => Promise.resolve({
      ok: true,
      status: 200,
      json: () => Promise.resolve({ is_platform_admin: false }),
    } as Response)),
  );
}

async function openAppearance() {
  const user = userEvent.setup();
  render(
    <RouterProvider>
      <StableSidebar scope={{ organizationId: "acme", projectId: "proj_EXAMPLE" }} />
    </RouterProvider>,
  );
  await user.click(screen.getByRole("button", { name: "Account menu" }));
  return { user, menu: screen.getByRole("menu") };
}

beforeEach(() => {
  localStorage.clear();
  document.documentElement.classList.remove("dark");
  window.history.replaceState({}, "", "/o/acme/p/proj_EXAMPLE/overview/project-overview");
  stubApi();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("Appearance — the switch shows the page, the sentence shows the rule", () => {
  it("is OFF on a light system and says it is following the system", async () => {
    stubOs(false);
    const { menu } = await openAppearance();

    expect(within(menu).getByRole("switch", { name: "Dark theme" })).not.toBeChecked();
    expect(within(menu).getByText("Following your system")).toBeInTheDocument();
    // Nothing to come back FROM: the escape hatch would undo nothing, and an
    // inert control is furniture.
    expect(
      within(menu).queryByRole("button", { name: "Follow my system again" }),
    ).not.toBeInTheDocument();
  });

  it("is ON on a dark system even though the stored mode is `system`", async () => {
    // THE DEFECT THE THREE PILLS COULD NOT SHOW. Nobody has touched a setting,
    // the OS is dark, so the page is dark — and the control must say so. A
    // switch reading the STORED mode would sit off over a dark page.
    stubOs(true);
    const { menu } = await openAppearance();

    expect(within(menu).getByRole("switch", { name: "Dark theme" })).toBeChecked();
    expect(within(menu).getByText("Following your system")).toBeInTheDocument();
    expect(getThemeMode()).toBe("system");
  });

  it("forces dark when switched on, and offers the way back to the system", async () => {
    stubOs(false);
    const { user, menu } = await openAppearance();

    await user.click(within(menu).getByRole("switch", { name: "Dark theme" }));

    expect(getThemeMode()).toBe("dark");
    expect(document.documentElement.classList.contains("dark")).toBe(true);
    expect(within(menu).getByText("Always dark")).toBeInTheDocument();
    expect(
      within(menu).getByRole("button", { name: "Follow my system again" }),
    ).toBeInTheDocument();
  });

  it("forces light when switched off under a dark system, and comes back", async () => {
    stubOs(true);
    const { user, menu } = await openAppearance();

    await user.click(within(menu).getByRole("switch", { name: "Dark theme" }));
    expect(getThemeMode()).toBe("light");
    expect(document.documentElement.classList.contains("dark")).toBe(false);
    expect(within(menu).getByText("Always light")).toBeInTheDocument();

    // Back to the system: the mode is cleared, and the switch snaps to what the
    // OS actually says — ON, because this OS is dark. A control that stayed off
    // here would be the original defect, reintroduced by the escape hatch.
    await user.click(within(menu).getByRole("button", { name: "Follow my system again" }));
    expect(getThemeMode()).toBe("system");
    expect(within(menu).getByRole("switch", { name: "Dark theme" })).toBeChecked();
    expect(within(menu).getByText("Following your system")).toBeInTheDocument();
    expect(document.documentElement.classList.contains("dark")).toBe(true);
  });
});
