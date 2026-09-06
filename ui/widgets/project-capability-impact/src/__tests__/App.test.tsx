/**
 * What the shared capability impact app must do (Story 48.1, AC10).
 *
 * The properties under test are the ones that make it safe to show a reviewer:
 * it renders server-composed coverage without recomputing it, it never offers a
 * confirmation, it says what a payload bound withheld, and its wide table stays
 * reachable from the keyboard.
 */

import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import App from "../App";
import type { CapabilityImpactPayload } from "../types";

function payload(overrides: Partial<CapabilityImpactPayload> = {}): CapabilityImpactPayload {
  return {
    schema: "project_change_set_impact.v1",
    project_id: "proj_EXAMPLE",
    change_set_id: "pcset_EXAMPLE",
    state: "prepared",
    coverage: {
      applicable: 2,
      complete: 1,
      partial: 0,
      unavailable: 0,
      excluded: 1,
      pending: 0,
      not_applicable: 3,
      label: "50% complete",
      percentage: 50,
    },
    matrix: [
      {
        capability_key: "country",
        datastream_id: "ds_EXAMPLE",
        applicability: "applicable",
        coverage_state: "complete",
        reason: "Country is compiled into the active plan.",
        changes_grain: true,
        backfill_required: true,
        downstream_consumers: 2,
      },
      {
        capability_key: "country",
        datastream_id: "ds_OTHER",
        applicability: "applicable",
        coverage_state: "excluded",
        reason: "Excluded by the data owner.",
      },
    ],
    blockers: [],
    exceptions: [],
    review_reference: "a".repeat(64),
    authorizing: false,
    ...overrides,
  };
}

describe("capability impact app", () => {
  it("renders the server-composed coverage label verbatim", () => {
    render(<App payload={payload()} />);
    // The label arrives composed. A percentage recomputed here could disagree
    // with the server about what a human is approving.
    expect(screen.getByText("50% complete")).toBeTruthy();
    expect(screen.getByText(/2 applicable Datastreams/)).toBeTruthy();
    expect(screen.getByText(/3 not applicable/)).toBeTruthy();
  });

  it("shows Not applicable rather than a full bar when nothing is applicable", () => {
    const empty = payload({
      coverage: {
        applicable: 0,
        complete: 0,
        partial: 0,
        unavailable: 0,
        excluded: 0,
        pending: 0,
        not_applicable: 4,
        label: "Not applicable",
        percentage: null,
      },
      matrix: [],
    });
    render(<App payload={empty} />);
    expect(screen.getByText("Not applicable")).toBeTruthy();
    expect(screen.queryByText("100% complete")).toBeNull();
  });

  it("renders one row per capability and Datastream with its reason", () => {
    render(<App payload={payload()} />);
    expect(screen.getByText("ds_EXAMPLE")).toBeTruthy();
    expect(screen.getByText("ds_OTHER")).toBeTruthy();
    expect(screen.getByText("Excluded by the data owner.")).toBeTruthy();
    expect(screen.getByText("Required")).toBeTruthy();
  });

  it("keeps the wide table in a keyboard-reachable scroll region", () => {
    render(<App payload={payload()} />);
    const region = screen.getByRole("region", { name: /Impact table/ });
    // A wide table must scroll inside its own focusable region, never push the
    // page sideways and never trap a keyboard user.
    expect(region.getAttribute("tabindex")).toBe("0");
  });

  it("offers no confirmation control and names the Console instead", () => {
    render(<App payload={payload({ confirmation_available: false })} />);
    expect(screen.getByText(/Confirm in the Console/)).toBeTruthy();
    // Server mutation and authorization stay outside the visualization.
    expect(screen.queryByRole("button", { name: /confirm/i })).toBeNull();
  });

  it("states that a frozen review authorizes nothing when confirmation is available", () => {
    render(<App payload={payload({ confirmation_available: true })} />);
    expect(screen.getByText(/Ready for confirmation/)).toBeTruthy();
    expect(screen.getByText(/not an approval/)).toBeTruthy();
  });

  it("says what the payload bound withheld rather than truncating silently", () => {
    render(<App payload={payload({ matrix_rows_withheld: 12 })} />);
    expect(screen.getByText(/12 further rows withheld/)).toBeTruthy();
  });

  it("falls back to the Console when no compatible host answered", () => {
    render(<App payload={null} hostConnected={false} />);
    expect(screen.getByText(/No compatible MCP host detected/)).toBeTruthy();
  });

  it("reports blockers and exceptions from the payload, deriving none", () => {
    render(
      <App
        payload={payload({
          blockers: [
            {
              code: "country_grain_unavailable",
              message: "Select a country-compatible report.",
              datastream_id: "ds_OTHER",
            },
          ],
          exceptions: [
            {
              severity: "degrading",
              reason: "Coverage begins at the first publication that includes country.",
              owner_kind: "data",
            },
          ],
        })}
      />,
    );
    expect(screen.getByText(/country_grain_unavailable/)).toBeTruthy();
    expect(screen.getByText(/Coverage begins at the first publication/)).toBeTruthy();
  });
});
