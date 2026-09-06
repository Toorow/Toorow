/**
 * The formula-parity verdict reaches the screen — repair of 2026-08-31.
 *
 * WHAT THE MEASUREMENT SAID. `scripts/check_metric_formula_parity.py --gate`
 * exits 0 on 3 ratios with 0 divergence, and it had been doing so since
 * 2026-08-16 — for whoever ran it. `governance_read_model._semantic_concept`
 * composed no parity flag and this tab rendered none, so a Project that governs
 * `roas` on its own components could read the Semantic Model all day without
 * ever being told that `semantic_roas`, the table its numbers come from, does
 * not implement that formula.
 *
 * The verdict is composed SERVER-SIDE by `core.platform_semantic_concepts.
 * formula_parity` — one rule, imported by the script and by the read model.
 * These tests therefore prove the RENDERING and the alert language: what an
 * override looks like versus a divergence, and that a Concept with nothing to
 * compare shows nothing at all.
 */
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";
import { RouterProvider } from "../shell/router";
import { SemanticsTab } from "../governance/SemanticModelTabs";
import type { GovernanceObject } from "../governance/governanceSurface";

const ORG = "org_EXAMPLE";
const PROJECT = "proj_EXAMPLE";

function facet<T>(state: string, refs: T[] = []) {
  return { state, count: refs.length, refs, truncated: false };
}

function concept(summary: Record<string, unknown>): GovernanceObject {
  return {
    object_ref: {
      type: "semantic-concept",
      id: "sc_01EXAMPLE0000000000000000",
      label: "Roas",
      owner_href: { section: "governance" } as never,
    },
    scope: "project",
    owner: {},
    lifecycle_status: "published",
    active_version_ref: null,
    selected_version_ref: null,
    available_tabs: ["semantics"],
    default_tab: "semantics",
    allowed_actions: [],
    used_by: facet("empty"),
    versions: facet("empty"),
    evidence: facet("empty"),
    summary: {
      name: "roas",
      concept_kind: "metric",
      value_type: "ratio",
      additivity_class: "non_additive",
      expression: {
        op: "ratio",
        numerator: { op: "concept_name", name: "revenue" },
        denominator: { op: "concept_name", name: "cost" },
      },
      ...summary,
    },
    evidence_as_of: null,
  } as unknown as GovernanceObject;
}

function mount(detail: GovernanceObject) {
  window.history.replaceState({}, "", `/org/${ORG}/project/${PROJECT}/governance/semantic-model`);
  return render(
    <RouterProvider>
      <SemanticsTab detail={detail} />
    </RouterProvider>,
  );
}

afterEach(cleanup);

describe("the Semantic Model tab renders the formula-parity verdict", () => {
  it("says nothing at all when there is nothing to compare", () => {
    // A "nothing to report" badge on every Concept is how a reader learns to
    // stop looking at the one that matters.
    mount(concept({ formula_parity: null }));
    expect(screen.queryByTestId(/^formula-parity/)).toBeNull();
  });

  it("names a project override as the client's right, and names what the table serves", () => {
    mount(
      concept({
        formula_parity: {
          verdict: "project_override",
          declared: "revenue / cost",
          governed: "revenue / spend",
          message:
            "This Project governs `revenue / spend` where the delivered catalogue and the " +
            "served table compute `revenue / cost`. That is your definition to make -- but " +
            "`semantic_roas` does not implement it, so read this metric from a Semantic View " +
            "built on this Concept rather than from the delivered one.",
        },
      }),
    );
    const banner = screen.getByTestId("formula-parity-project_override");
    expect(banner.textContent).toContain("semantic_roas");
    expect(banner.textContent).toContain("revenue / spend");
    // An override is NOT a fault: the product's alert language keeps error for
    // the case where the same object says two things.
    expect(screen.queryByTestId("formula-parity-platform_divergence")).toBeNull();
  });

  it("calls a platform divergence what it is: one metric saying two things", () => {
    mount(
      concept({
        formula_parity: {
          verdict: "platform_divergence",
          declared: "revenue / cost",
          governed: "revenue / clicks",
          message:
            "The platform Concept governs `revenue / clicks` and the delivered catalogue " +
            "declares `revenue / cost`: the same metric says two things. Publish a new " +
            "version of this Concept on the declared components, or change the catalogue " +
            "-- one of the two has to move.",
        },
      }),
    );
    const banner = screen.getByTestId("formula-parity-platform_divergence");
    expect(banner.textContent).toContain("says two things");
    // The refusal names the gesture, never only the cause.
    expect(banner.textContent).toContain("Publish a new version");
  });

  it("admits when the comparison did not happen instead of implying it passed", () => {
    mount(
      concept({
        formula_parity: {
          verdict: "unreadable",
          declared: "revenue / cost",
          governed: null,
          message:
            "This formula is a ratio named `roas`, and its operands cannot be read by name " +
            "here, so it was not compared with the delivered catalogue (revenue / cost). " +
            "Open the formula and pin each operand to an exact Concept version.",
        },
      }),
    );
    const banner = screen.getByTestId("formula-parity-unreadable");
    expect(banner.textContent).toContain("was not compared");
  });

  it("confirms the aligned case, which is the one the gate reports green", () => {
    mount(
      concept({
        formula_parity: {
          verdict: "aligned",
          declared: "revenue / cost",
          governed: "revenue / cost",
          message:
            "This formula computes on the components the delivered catalogue declares " +
            "(revenue / cost), which is what `semantic_roas` serves.",
        },
      }),
    );
    expect(screen.getByTestId("formula-parity-aligned")).toBeTruthy();
  });

  it("ignores a payload whose verdict is not a verdict", () => {
    mount(concept({ formula_parity: { declared: "revenue / cost" } }));
    expect(screen.queryByTestId(/^formula-parity/)).toBeNull();
  });
});
