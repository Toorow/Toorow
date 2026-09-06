/**
 * Tax & Fees inside the Datastream Workbench (Story 41.7, AC10/AC11).
 *
 * Five situations reach this panel and four of them look like "nothing here".
 * They are not the same fact and they do not have the same repair, so the whole
 * point of these tests is that each produces its OWN sentence:
 *
 *   pending                       no Change Set has compiled this yet
 *   tax_evidence_unobserved       no publication recorded the evidence → a Run
 *   tax_applicability_unresolved  a required input is unresolved → Mapping
 *   NOT_APPLICABLE                a VERDICT, naming the version and the type
 *   partial + posture unresolved  the ladder cannot decide, so no total
 *
 * And the fifth thing that must never happen: rendering "0 rules apply" for a
 * Datastream whose source type is UNKNOWN. The compiler already refuses to; the
 * screen has to carry the refusal through.
 *
 * Fixtures are hand-written from `TaxFeesCompiler.assess`'s own return shapes.
 * No database carries a `datastream_tax_evidence` row (measured 2026-08-04: 0),
 * so nothing here proves a browser met a live verdict.
 */
import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import WorkbenchCapabilityPanel from "../datastreams/workbench/WorkbenchCapabilityPanel";

function projection(capability: Record<string, unknown>) {
  return { capabilities: [capability as never], primary_action: null };
}

const SUPPORT = {
  state: "detected",
  declared_source_type: "PAID_MEDIA",
  source_type_origin: "contracted",
  source_type_confidence: "high",
  tax_posture: "TAX_EXCLUSIVE",
  matched_rule_keys: ["gb_dsp_platform", "fr_dst"],
  refused_rules: [
    {
      rule_key: "us_sales_tax",
      code: "source_type_out_of_scope",
      reason: "Scoped to ECOMMERCE, and this Datastream is PAID_MEDIA.",
    },
    {
      rule_key: "agency_fee_q1",
      code: "scoped_to_another_plan_version",
      reason: "Scoped to plan version pv_EXAMPLE.",
    },
  ],
  cascade_phases: [2, 3],
  rule_set_version_id: "grsv_EXAMPLE",
  tax_evidence_version_id: "dte_EXAMPLE",
  selected: true,
};

describe("the verdict is readable, and so is every refusal", () => {
  it("shows the observed type with its origin and confidence side by side", () => {
    render(
      <WorkbenchCapabilityPanel
        projection={projection({
          capability_key: "tax_fees",
          applicability: "applicable",
          coverage_state: "complete",
          impact: { detected_support_selection: SUPPORT },
        })}
      />,
    );
    expect(screen.getByText("PAID_MEDIA")).toBeTruthy();
    expect(screen.getByText("contracted · confidence high")).toBeTruthy();
    expect(screen.getByText("TAX_EXCLUSIVE")).toBeTruthy();
    expect(screen.getByText("2, 3")).toBeTruthy();
    expect(screen.getByText("2 · gb_dsp_platform, fr_dst")).toBeTruthy();
    // The verdict is only readable against BOTH versions.
    expect(screen.getByText(/ladder grsv_EXAMPLE · evidence dte_EXAMPLE/)).toBeTruthy();
  });

  it("lists every refused rule with its code and its reason, never a count", () => {
    render(
      <WorkbenchCapabilityPanel
        projection={projection({
          capability_key: "tax_fees",
          applicability: "applicable",
          coverage_state: "complete",
          impact: { detected_support_selection: SUPPORT },
        })}
      />,
    );
    expect(screen.getByText("us_sales_tax")).toBeTruthy();
    expect(screen.getByText("source_type_out_of_scope")).toBeTruthy();
    expect(
      screen.getByText(/Scoped to ECOMMERCE, and this Datastream is PAID_MEDIA\./),
    ).toBeTruthy();
    expect(screen.getByText("scoped_to_another_plan_version")).toBeTruthy();
    // "2 rules did not apply" would be a regression against what the server
    // already computed.
    expect(screen.queryByText(/2 rules did not apply/)).toBeNull();
  });
});

describe("UNKNOWN is a typed gap, never a source type", () => {
  it("refuses to present it as a value, and refuses the 'no fee applies' reading", () => {
    render(
      <WorkbenchCapabilityPanel
        projection={projection({
          capability_key: "tax_fees",
          applicability: "applicable",
          coverage_state: "unavailable",
          impact: {
            detected_support_selection: {
              ...SUPPORT,
              declared_source_type: "UNKNOWN",
              source_type_origin: "unresolved",
              source_type_confidence: "low",
              matched_rule_keys: [],
              selected: false,
            },
          },
        })}
      />,
    );
    expect(screen.getByText("Unresolved — no type is proved")).toBeTruthy();
    expect(screen.getByText("unresolved · confidence low")).toBeTruthy();
    expect(screen.getByText("The source type is a typed gap, not a value")).toBeTruthy();
    expect(screen.getByText(/Only a contracted or overridden/)).toBeTruthy();
    // The collapsed one-liner says it too, rather than "Detected, not selected".
    expect(screen.getByText("source type unresolved · no rule matched")).toBeTruthy();
  });
});

describe("the five situations produce five sentences", () => {
  const cases: Array<[string, Record<string, unknown>, string]> = [
    [
      "pending",
      { applicability: "pending", coverage_state: "pending", impact: {} },
      "No Change Set has compiled this capability for this Datastream.",
    ],
    [
      "evidence unobserved",
      {
        applicability: "applicable",
        coverage_state: "unavailable",
        reason: "No publication has recorded this Datastream's source type.",
        blockers: [
          { code: "tax_evidence_unobserved", message: "No publication has recorded it; run this Datastream." },
        ],
      },
      "No publication has recorded it; run this Datastream.",
    ],
    [
      "applicability unresolved",
      {
        applicability: "applicable",
        coverage_state: "unavailable",
        blockers: [
          { code: "tax_applicability_unresolved", message: "A required input is unresolved; repair Mapping." },
        ],
      },
      "A required input is unresolved; repair Mapping.",
    ],
    [
      "a verdict",
      {
        applicability: "not_applicable",
        coverage_state: "not_applicable",
        reason: "No rule in version 4 is scoped to an ORGANIC_ANALYTICS Datastream.",
      },
      "No rule in version 4 is scoped to an ORGANIC_ANALYTICS Datastream.",
    ],
    [
      "partial because the ladder cannot decide",
      {
        applicability: "applicable",
        coverage_state: "partial",
        exceptions: [
          {
            reason:
              "Matched rules leave Rest of world or Unknown undecided, so no complete headline total can be stated.",
          },
        ],
      },
      "Matched rules leave Rest of world or Unknown undecided, so no complete headline total can be stated.",
    ],
  ];

  for (const [name, capability, sentence] of cases) {
    it(`says its own sentence for: ${name}`, () => {
      render(
        <WorkbenchCapabilityPanel
          projection={projection({ capability_key: "tax_fees", ...capability })}
        />,
      );
      expect(screen.getByText(sentence)).toBeTruthy();
      // None of the four non-verdict states may borrow the verdict's wording.
      if (name !== "a verdict") {
        expect(screen.queryByText("Not applicable to this Datastream.")).toBeNull();
      }
    });
  }
});

describe("the other capabilities are untouched", () => {
  it("keeps the original detected_support_selection wording", () => {
    // None of the other four carries `declared_source_type`, so none of them
    // reaches the additive branch.
    for (const [selected, expected] of [
      [true, "Detected and selected"],
      [false, "Detected, not selected"],
    ] as const) {
      const { unmount } = render(
        <WorkbenchCapabilityPanel
          projection={projection({
            capability_key: "country",
            applicability: "applicable",
            coverage_state: "complete",
            impact: { detected_support_selection: { state: "detected", selected } },
          })}
        />,
      );
      expect(screen.getByText(expected)).toBeTruthy();
      unmount();
    }
  });

  /**
   * Story 48.4, Task 3 second bullet. The panel could say WHY a rule was refused
   * and could not say what the publication actually landed, because the server
   * did not send it. A refusal without its evidence is a verdict you cannot check.
   */
  it("lists the physical inputs the publication landed, and marks the absent ones", () => {
    render(
      <WorkbenchCapabilityPanel
        projection={projection({
          capability_key: "tax_fees",
          applicability: "applicable",
          coverage_state: "partial",
          impact: {
            detected_support_selection: {
              ...SUPPORT,
              observed_inputs: {
                native_amount_micros: 1000000,
                measured_impressions: 25000,
                transaction_count: 0,
              },
              available_inputs: ["measured_impressions", "native_amount_micros"],
            },
          },
        })}
      />,
    );
    expect(screen.getByText("Physical inputs this publication landed")).toBeTruthy();
    expect(screen.getByText(/measured_impressions/)).toBeTruthy();
    // A key present with a falsy value is ABSENT for a rule. Printing `0` as a
    // measurement is how a verification fee becomes a confident zero.
    expect(screen.getByText(/transaction_count/)).toBeTruthy();
    expect(screen.getByText(/not landed/)).toBeTruthy();
  });

  it("says so plainly when no physical input was recorded at all", () => {
    render(
      <WorkbenchCapabilityPanel
        projection={projection({
          capability_key: "tax_fees",
          applicability: "applicable",
          coverage_state: "partial",
          impact: {
            detected_support_selection: {
              ...SUPPORT,
              observed_inputs: {},
              available_inputs: [],
            },
          },
        })}
      />,
    );
    expect(screen.getByText("No physical input was recorded by this publication")).toBeTruthy();
  });

  it("adds no Tax & Fees detail block to a capability that is not tax_fees", () => {
    render(
      <WorkbenchCapabilityPanel
        projection={projection({
          capability_key: "currency_fx",
          applicability: "applicable",
          coverage_state: "complete",
          impact: { detected_support_selection: SUPPORT },
        })}
      />,
    );
    expect(screen.queryByText("Observed source type")).toBeNull();
    expect(screen.queryByText("us_sales_tax")).toBeNull();
  });
});
