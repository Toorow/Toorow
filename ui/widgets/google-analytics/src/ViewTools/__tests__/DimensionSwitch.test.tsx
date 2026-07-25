/**
 * Unit tests for DimensionSwitch (Story 8.11, R5).
 *
 * Covers:
 *  - One button per dimension with French labels
 *  - Composite dimension label « Pays > Appareil »
 *  - onChange fires the raw dimension id (incl. composite 'country>device')
 *  - Empty list renders nothing
 *  - dimensionLabel helper: single, composite, and unknown fallbacks
 */

import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import DimensionSwitch, { dimensionLabel } from "../DimensionSwitch";

const DIMENSIONS = ["country", "device_category", "country>device"];

describe("DimensionSwitch", () => {
  it("renders one button per dimension with French labels incl. composite", () => {
    render(
      <DimensionSwitch dimensions={DIMENSIONS} value="country" onChange={() => {}} />,
    );
    expect(screen.getByRole("button", { name: "Pays" })).toBeDefined();
    expect(screen.getByRole("button", { name: "Appareil" })).toBeDefined();
    expect(screen.getByRole("button", { name: "Pays > Appareil" })).toBeDefined();
  });

  it("calls onChange with the composite dimension id when clicked", async () => {
    const user = userEvent.setup();
    const onChange = vi.fn();
    render(
      <DimensionSwitch dimensions={DIMENSIONS} value="country" onChange={onChange} />,
    );
    await user.click(screen.getByRole("button", { name: "Pays > Appareil" }));
    expect(onChange).toHaveBeenCalledWith("country>device");
  });

  it("renders nothing when the dimensions list is empty", () => {
    const { container } = render(
      <DimensionSwitch dimensions={[]} value="" onChange={() => {}} />,
    );
    expect(container.firstChild).toBeNull();
  });
});

describe("dimensionLabel", () => {
  it("maps known single dimensions to French labels", () => {
    expect(dimensionLabel("country")).toBe("Pays");
    expect(dimensionLabel("device_category")).toBe("Appareil");
    expect(dimensionLabel("device")).toBe("Appareil");
    expect(dimensionLabel("date")).toBe("Date");
  });

  it("joins composite dimensions with ' > ' using French parts", () => {
    expect(dimensionLabel("country>device")).toBe("Pays > Appareil");
  });

  it("falls back to Title Case for unknown single dimensions", () => {
    expect(dimensionLabel("some_future_dim")).toBe("Some Future Dim");
  });
});
