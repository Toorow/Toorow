/**
 * Unit tests for GranularityToggle (Story 1.7, T2.4).
 *
 * Covers:
 *  - Renders three buttons with French labels (Jour / Semaine / Mois)
 *  - Calls onChange with the correct Granularity value on click
 *  - Default value is "day"
 */

import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import GranularityToggle from "../GranularityToggle";

describe("GranularityToggle", () => {
  it("renders three buttons with French labels", () => {
    render(<GranularityToggle value="day" onChange={() => {}} />);
    expect(screen.getByRole("button", { name: "Jour" })).toBeDefined();
    expect(screen.getByRole("button", { name: "Semaine" })).toBeDefined();
    expect(screen.getByRole("button", { name: "Mois" })).toBeDefined();
  });

  it("calls onChange with 'week' when Semaine is clicked", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(<GranularityToggle value="day" onChange={onChange} />);
    await user.click(screen.getByRole("button", { name: "Semaine" }));
    expect(onChange).toHaveBeenCalledWith("week");
  });

  it("calls onChange with 'month' when Mois is clicked", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(<GranularityToggle value="day" onChange={onChange} />);
    await user.click(screen.getByRole("button", { name: "Mois" }));
    expect(onChange).toHaveBeenCalledWith("month");
  });

  it("calls onChange with 'day' when Jour is clicked", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(<GranularityToggle value="week" onChange={onChange} />);
    await user.click(screen.getByRole("button", { name: "Jour" }));
    expect(onChange).toHaveBeenCalledWith("day");
  });

  it("does not call onChange when the already-selected button is clicked", async () => {
    // MUI ToggleButtonGroup passes null on deselection of exclusive value.
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(<GranularityToggle value="day" onChange={onChange} />);
    await user.click(screen.getByRole("button", { name: "Jour" }));
    // onChange should not be called (MUI sends null, we guard against it).
    expect(onChange).not.toHaveBeenCalled();
  });
});
