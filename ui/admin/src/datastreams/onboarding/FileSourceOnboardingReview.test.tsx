/**
 * Vitest tests for FileSourceOnboardingReview (Story 22.19).
 *
 * Coverage:
 *   - Renders per-field mapping with confidence + the placement + gate status.
 *   - A passing gate enables Confirm; clicking locks (onConfirm).
 *   - A flagged gate (missing required / flagged field) disables Confirm and
 *     surfaces the offending fields (low confidence / missing required blocks lock).
 *   - Resolving a flagged field via the select calls onResolveField(source, id).
 *
 * English copy (project-context: all admin copy is English); WCAG: semantic table,
 * textual state, aria-live gate region.
 */
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ThemeProvider } from "@mui/material";
import { adminTheme } from "../../theme";
import FileSourceOnboardingReview, {
  type GateStatus,
  type MappedField,
  type Placement,
} from "./FileSourceOnboardingReview";

function renderWithTheme(ui: React.ReactElement) {
  return render(<ThemeProvider theme={adminTheme}>{ui}</ThemeProvider>);
}

const PLACEMENT: Placement = {
  class: "planned",
  metric: "mdm_net_cost",
  period: "mdm_media_date",
  dimension: ["mdm_channel"],
};

const CANONICAL = ["mdm_net_cost", "mdm_media_date", "mdm_channel", "mdm_impressions"];

const PASSING_FIELDS: MappedField[] = [
  { source_column: "Bruttokosten Gesamt", canonical_target: "mdm_net_cost", confidence: 0.91, status: "matched" },
  { source_column: "Datum", canonical_target: "mdm_media_date", confidence: 0.88, status: "matched" },
  { source_column: "Notiz", canonical_target: null, confidence: 0.1, status: "unmatched" },
];
const PASSING_GATE: GateStatus = { passed: true, missing_required: [], flagged: [] };

const FLAGGED_FIELDS: MappedField[] = [
  { source_column: "Bruttokosten Gesamt", canonical_target: "mdm_net_cost", confidence: 0.91, status: "matched" },
  { source_column: "???", canonical_target: null, confidence: 0.4, status: "flagged" },
];
const FLAGGED_GATE: GateStatus = {
  passed: false,
  missing_required: ["mdm_media_date"],
  flagged: [{ source_column: "???", blocking_reason: "low_confidence" }],
};

describe("FileSourceOnboardingReview — rendering", () => {
  it("shows per-field mapping with confidence and the placement", () => {
    renderWithTheme(
      <FileSourceOnboardingReview
        fields={PASSING_FIELDS}
        placement={PLACEMENT}
        gate={PASSING_GATE}
        canonicalOptions={CANONICAL}
      />,
    );
    // Placement.
    expect(screen.getByTestId("placement-class")).toHaveTextContent(/planned/i);
    expect(screen.getByTestId("placement-summary")).toHaveTextContent(/mdm_net_cost/);
    // Per-field mapping + confidence.
    expect(screen.getByTestId("map-row-Bruttokosten Gesamt")).toBeInTheDocument();
    expect(screen.getByTestId("confidence-Bruttokosten Gesamt")).toHaveTextContent("91%");
    // Extra column is shown but marked ignored/unmatched.
    expect(screen.getByTestId("status-Notiz")).toHaveTextContent(/ignored/i);
  });
});

describe("FileSourceOnboardingReview — gate + confirm", () => {
  it("enables Confirm on a passing gate and locks on click", async () => {
    const onConfirm = vi.fn();
    renderWithTheme(
      <FileSourceOnboardingReview
        fields={PASSING_FIELDS}
        placement={PLACEMENT}
        gate={PASSING_GATE}
        canonicalOptions={CANONICAL}
        onConfirm={onConfirm}
      />,
    );
    expect(screen.getByTestId("gate-passed")).toBeInTheDocument();
    const confirm = screen.getByTestId("confirm-lock");
    expect(confirm).toBeEnabled();
    await userEvent.click(confirm);
    expect(onConfirm).toHaveBeenCalledTimes(1);
  });

  it("blocks lock on a flagged gate and surfaces the offending fields", () => {
    renderWithTheme(
      <FileSourceOnboardingReview
        fields={FLAGGED_FIELDS}
        placement={PLACEMENT}
        gate={FLAGGED_GATE}
        canonicalOptions={CANONICAL}
      />,
    );
    expect(screen.getByTestId("confirm-lock")).toBeDisabled();
    expect(screen.getByTestId("confirm-blocked-hint")).toBeInTheDocument();
    expect(screen.getByTestId("gate-missing-required")).toHaveTextContent(/mdm_media_date/);
    expect(screen.getByTestId("gate-flagged-fields")).toHaveTextContent(/\?\?\?/);
  });
});

describe("FileSourceOnboardingReview — resolve a flagged field", () => {
  it("calls onResolveField when the operator picks a canonical target", async () => {
    const onResolveField = vi.fn();
    renderWithTheme(
      <FileSourceOnboardingReview
        fields={FLAGGED_FIELDS}
        placement={PLACEMENT}
        gate={FLAGGED_GATE}
        canonicalOptions={CANONICAL}
        onResolveField={onResolveField}
      />,
    );
    const select = screen.getByTestId("resolve-???");
    await userEvent.selectOptions(select, "mdm_media_date");
    expect(onResolveField).toHaveBeenCalledWith("???", "mdm_media_date");
  });
});
