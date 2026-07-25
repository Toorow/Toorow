/**
 * Unit tests for MetricSwitch (Story 1.7, T4.4).
 *
 * Covers:
 *  - Renders one option per metric
 *  - Calls onChange on selection with the correct metric key
 *  - Returns null when metrics list is empty
 */

import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import MetricSwitch from "../MetricSwitch";

const METRICS = ["sessions", "active_users", "conversions"];

describe("MetricSwitch", () => {
  it("renders one button per metric with French labels", () => {
    render(
      <MetricSwitch metrics={METRICS} value="sessions" onChange={() => {}} />,
    );
    expect(screen.getByRole("button", { name: "Sessions" })).toBeDefined();
    expect(screen.getByRole("button", { name: "Utilisateurs actifs" })).toBeDefined();
    expect(screen.getByRole("button", { name: "Conversions" })).toBeDefined();
  });

  it("calls onChange with the correct metric key when clicked", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(
      <MetricSwitch metrics={METRICS} value="sessions" onChange={onChange} />,
    );
    await user.click(screen.getByRole("button", { name: "Utilisateurs actifs" }));
    expect(onChange).toHaveBeenCalledWith("active_users");
  });

  it("calls onChange with 'conversions' when Conversions is clicked", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(
      <MetricSwitch metrics={METRICS} value="sessions" onChange={onChange} />,
    );
    await user.click(screen.getByRole("button", { name: "Conversions" }));
    expect(onChange).toHaveBeenCalledWith("conversions");
  });

  it("renders nothing when metrics list is empty", () => {
    const { container } = render(
      <MetricSwitch metrics={[]} value="" onChange={() => {}} />,
    );
    expect(container.firstChild).toBeNull();
  });

  it("falls back to Title Case for unknown metric keys", () => {
    render(
      <MetricSwitch
        metrics={["some_future_metric"]}
        value="some_future_metric"
        onChange={() => {}}
      />,
    );
    expect(screen.getByRole("button", { name: "Some Future Metric" })).toBeDefined();
  });
});
