/**
 * The two shell facts of `page-structure.md §G.3.1-2`: one appearance store
 * feeding every control that renders it, and a badge that names only a build
 * the person should be warned about.
 */
import { fireEvent, render, screen } from "@testing-library/react";

import { AppearanceControl, instanceBadge } from "../shell/StableSidebar";

describe("instanceBadge", () => {
  it("says nothing at all on the production build", () => {
    expect(instanceBadge("production")).toBeNull();
    expect(instanceBadge("")).toBeNull();
  });

  it("names any other build mode as the warning it is", () => {
    const badge = instanceBadge("development");
    expect(badge?.label).toBe("development build");
    expect(badge?.title).toContain("not the production build");
  });
});

describe("AppearanceControl — one store, two hosts", () => {
  it("moves both controls with one gesture", () => {
    render(
      <>
        <AppearanceControl />
        <AppearanceControl />
      </>,
    );
    const switches = screen.getAllByRole("switch", { name: "Dark theme" });
    expect(switches).toHaveLength(2);

    fireEvent.click(switches[0]);
    // The second host follows without being touched: the store announced.
    for (const control of screen.getAllByRole("switch", { name: "Dark theme" })) {
      expect(control).toBeChecked();
    }
    // Both offer the way back, because both left `system` together.
    expect(screen.getAllByRole("button", { name: "Follow my system again" })).toHaveLength(2);
  });
});
