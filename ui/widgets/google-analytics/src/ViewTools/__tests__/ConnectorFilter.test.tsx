/**
 * Unit tests for ConnectorFilter (Story 1.7, T3.5).
 *
 * Covers:
 *  - Renders one button per entry in connectors
 *  - Toggling deselects a connector (calls onChange with updated selection)
 *  - Cannot deselect the last selected connector
 */

import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import ConnectorFilter from "../ConnectorFilter";

const CONNECTORS = ["google-analytics", "meta-ads", "google-search-console"];

describe("ConnectorFilter", () => {
  it("renders one button per connector", () => {
    render(
      <ConnectorFilter
        connectors={CONNECTORS}
        selected={CONNECTORS}
        onChange={() => {}}
      />,
    );
    // Display labels mapped via TOOROW_LABELS
    expect(screen.getByRole("button", { name: "Google Analytics" })).toBeDefined();
    expect(screen.getByRole("button", { name: "Meta Ads" })).toBeDefined();
    expect(screen.getByRole("button", { name: "Search Console" })).toBeDefined();
  });

  it("calls onChange with the new selection when a connector is toggled off", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(
      <ConnectorFilter
        connectors={CONNECTORS}
        selected={CONNECTORS}
        onChange={onChange}
      />,
    );
    // Click "Meta Ads" to deselect it
    await user.click(screen.getByRole("button", { name: "Meta Ads" }));
    expect(onChange).toHaveBeenCalled();
    const newSelected: string[] = onChange.mock.calls[0][0];
    expect(newSelected).not.toContain("meta-ads");
    expect(newSelected).toContain("google-analytics");
  });

  it("calls onChange with the new selection when a connector is toggled on", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(
      <ConnectorFilter
        connectors={CONNECTORS}
        selected={["google-analytics"]}
        onChange={onChange}
      />,
    );
    await user.click(screen.getByRole("button", { name: "Meta Ads" }));
    expect(onChange).toHaveBeenCalled();
    const newSelected: string[] = onChange.mock.calls[0][0];
    expect(newSelected).toContain("meta-ads");
  });

  it("does NOT call onChange when deselecting the last selected connector", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(
      <ConnectorFilter
        connectors={["google-analytics"]}
        selected={["google-analytics"]}
        onChange={onChange}
      />,
    );
    await user.click(screen.getByRole("button", { name: "Google Analytics" }));
    // onChange must not be called — empty selection is prevented.
    expect(onChange).not.toHaveBeenCalled();
  });

  it("renders nothing (returns null) when connectors list is empty", () => {
    const { container } = render(
      <ConnectorFilter connectors={[]} selected={[]} onChange={() => {}} />,
    );
    expect(container.firstChild).toBeNull();
  });
});
