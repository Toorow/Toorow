/**
 * The 51st tab of 51.
 *
 * `python scripts/screens.py pages` reports that 51 addresses resolve through a
 * blanket workbench — the address answers, which is not the same as the tab
 * being drawn. Fifty are drawn by a named module. `tracked-entity/
 * representations` was not: it fell to the chassis' generic field dump, so the
 * one subject the tab exists for appeared as a raw record.
 *
 * `capabilities/competitors.md:12` contracts it — an entity "binds
 * source-specific representations without remapping" — and the payload was
 * already shipping (`governance_read_model.py:1947`).
 *
 * The case worth its own test is the EMPTY one. "No source is bound yet" and
 * "we cannot say" are different sentences, and `README.md` invariant 8 forbids
 * the second from reading as the first.
 */
import { render, screen, within } from "@testing-library/react";
import { RepresentationsTab } from "../governance/TrackedEntityTabs";
import type { GovernanceObject } from "../governance/governanceSurface";

function object(representations: unknown): GovernanceObject {
  return { summary: { representations } } as unknown as GovernanceObject;
}

it("shows every source name bound to the entity", () => {
  render(
    <RepresentationsTab
      detail={object([
        {
          connector: "google-ads",
          account_scope: "acc_1",
          external_id: "cmp_42",
          external_label: "Competitor Ltd",
          version: 3,
        },
      ])}
    />,
  );
  const row = screen.getByText("Competitor Ltd").closest("tr")!;
  expect(within(row).getByText("google-ads")).toBeInTheDocument();
  expect(within(row).getByText("acc_1")).toBeInTheDocument();
  expect(within(row).getByText("cmp_42")).toBeInTheDocument();
  expect(within(row).getByText("3")).toBeInTheDocument();
});

it("writes an absent account scope as what it means, not as unknown", () => {
  // No scope is "every account of this connector" — a different statement from
  // "we do not know", and the table must not blur the two into a dash.
  render(
    <RepresentationsTab
      detail={object([{ connector: "meta-ads", account_scope: null, external_id: "x", external_label: "X", version: 1 }])}
    />,
  );
  expect(screen.getByText("All accounts")).toBeInTheDocument();
});

it("says nothing is bound rather than rendering an empty table", () => {
  render(<RepresentationsTab detail={object([])} />);
  expect(screen.getByText(/no source representation is bound/i)).toBeInTheDocument();
  expect(screen.queryByRole("table")).not.toBeInTheDocument();
});

it("treats a missing payload as none bound, never as a broken screen", () => {
  render(<RepresentationsTab detail={object(undefined)} />);
  expect(screen.getByText(/no source representation is bound/i)).toBeInTheDocument();
});
