/**
 * The Rule Set workbench, family by family (Story 41.7).
 *
 * The defect these tests pin is not "a tab was missing". The tab was there, it
 * rendered rows, and every cell of every row said `Unavailable` — because
 * `RuleSetRulesTab` read the keys of a `metric_reconciliation` rule out of a
 * `tax_fee` ladder rule that carries none of them. A screen claiming governed
 * evidence could not be read, while the evidence sat complete in the same
 * payload.
 *
 * So the assertions below are mostly about DISTINCTIONS, not about the happy
 * path:
 *
 *   - a complete ladder produces the string "Unavailable" zero times
 *   - `metric_reconciliation` renders exactly as it did before
 *   - a family that carries no rules by construction does not borrow the
 *     sentence of a family whose ladder is merely empty
 *   - `unresolved` and `exclude` read differently, in words and not by colour
 *   - `facets_state: "unavailable"` and `ordered_rules: []` are two screens
 *
 * Every fixture here is hand-written. No database carries a published tax_fee
 * ladder (measured 2026-08-04: `app.governance_rule_sets` holds zero rows, all
 * families), so these are component tests over payloads shaped from the server's
 * own validators — not evidence that a browser was pointed at a live ladder.
 */
import { render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { RuleSetRulesTab } from "../governance/ControlsQualityTabs";
import {
  TaxFeeLadderOverviewTab,
  fromMicros,
  isOverlayRule,
  isTaxFeeRuleSet,
  ratePercent,
  ruleValue,
  shiftDecimal,
} from "../governance/TaxFeeLadderTabs";

function detail(summary: Record<string, unknown>, owner: Record<string, unknown> = {}) {
  return {
    object_ref: { type: "rule-set", id: "grs_EXAMPLE", label: "Project ladder", owner_href: "" },
    scope: "project",
    owner,
    lifecycle_status: "published",
    active_version_ref: null,
    selected_version_ref: null,
    available_tabs: [],
    default_tab: "overview",
    allowed_actions: [],
    used_by: { state: "empty", count: 0, refs: [] },
    versions: { state: "empty", count: 0, refs: [] },
    evidence: { state: "empty", count: 0, refs: [] },
    summary,
    evidence_as_of: null,
  } as never;
}

/** One ladder rule, shaped exactly as `validate_ladder_rule` returns it. */
function rule(overrides: Record<string, unknown> = {}) {
  return {
    rule_key: "gb_dsp_platform",
    label: "DSP platform fee",
    scope_kind: "project",
    scope_ref: null,
    category: "PLATFORM_FEE",
    form: "PERCENTAGE",
    // Stored as a string over NUMERIC(12,6). Never a JS number.
    rate: "0.030000",
    amount_micros: null,
    cpm_micros: null,
    tiers: null,
    currency: null,
    base_target: "RUNNING_SUBTOTAL",
    money_basis: "native_source",
    cascade_phase: 2,
    sequence_order: 10,
    conditions: {},
    source_type_scope: [],
    jurisdiction: { kind: "none", id: null, hierarchy_version_id: null, label: null },
    geography_dependent: false,
    rest_of_world_posture: null,
    unknown_posture: null,
    effective_from: "2026-01-01",
    effective_to: null,
    authority_kind: "provider_invoice_practice",
    source_evidence: {
      issuer: "Example DSP",
      reference: "Rate card",
      reference_version: "2026-01",
      authoritative_url: null,
      document_ref: null,
      preset_version_id: null,
      published_on: null,
      last_verified_on: null,
      assumptions: [],
    },
    origin: "operator",
    ...overrides,
  };
}

const LADDER = {
  family: "tax_fee",
  profile: "tax_fee_ladder_v1",
  facets_state: "available",
  rule_count: 3,
  version_count: 2,
  approvals: 1,
  active_exceptions: 0,
  effective_from: "2026-01-01",
  effective_to: null,
  has_pending_version: false,
  last_known_good_version_id: "grsv_EXAMPLE",
  content_hash: "sha256:example",
  policy: { rounding: "half_even", default_money_basis: "native_source" },
  ordered_rules: [
    rule(),
    rule({
      rule_key: "fr_dst",
      label: "French DST",
      category: "REGULATORY_TAX",
      cascade_phase: 3,
      base_target: "NET_MEDIA",
      authority_kind: "statutory_reference",
      geography_dependent: true,
      jurisdiction: { kind: "country", id: "FR", hierarchy_version_id: "gh_v7", label: "France" },
      rest_of_world_posture: "exclude",
      unknown_posture: "exclude",
    }),
    rule({
      rule_key: "ias_verification",
      label: "IAS verification",
      category: "VERIFICATION",
      form: "CPM",
      rate: null,
      cpm_micros: 1200000,
      currency: "EUR",
      base_target: "MEASURED_IMPRESSIONS",
      cascade_phase: 2,
    }),
  ],
};

describe("exact decimal formatting — the surface is not where exactness is lost", () => {
  it("shifts the decimal point on the string, not through a JS number", () => {
    // The whole reason this helper exists: Number("0.030000") * 100 is
    // 3.0000000000000004.
    expect(ratePercent("0.030000")).toBe("3.0000 %");
    expect(shiftDecimal("0.030000", 2)).toBe("3.0000");
    expect(shiftDecimal("1234567", -6)).toBe("1.234567");
    expect(shiftDecimal("0.5", 2)).toBe("50");
    expect(shiftDecimal("-0.020000", 2)).toBe("-2.0000");
  });

  it("reports a value it cannot parse as itself rather than as a number", () => {
    expect(shiftDecimal("not-a-decimal", 2)).toBeNull();
    expect(ratePercent("not-a-decimal")).toBe("not-a-decimal %");
  });

  it("renders each form in the shape that form actually uses", () => {
    expect(ruleValue(rule())).toBe("3.0000 %");
    expect(ruleValue(rule({ form: "FLAT", amount_micros: 2500000, currency: "EUR" }))).toBe(
      "2.500000 EUR",
    );
    expect(ruleValue(rule({ form: "CPM", cpm_micros: 1200000, currency: "EUR" }))).toBe(
      "1.200000 EUR per 1000",
    );
    expect(
      ruleValue(
        rule({ form: "SPEND_TIERS", tiers: { mode: "marginal", bands: [{}, {}] } }),
      ),
    ).toBe("2 bands, marginal");
    expect(fromMicros(1200000)).toBe("1.200000");
  });
});

describe("a tax_fee ladder renders as the Global Rule Matrix", () => {
  it("vouches for every field the payload carries", () => {
    render(<RuleSetRulesTab detail={detail(LADDER)} />);

    // The defect, pinned: a complete ladder must produce the word zero times.
    expect(screen.queryAllByText("Unavailable")).toHaveLength(0);

    expect(screen.getByText("DSP platform fee")).toBeTruthy();
    expect(screen.getAllByText("3.0000 %").length).toBeGreaterThan(0);
    expect(screen.getByText("RUNNING_SUBTOTAL")).toBeTruthy();
    // An empty source_type_scope means EVERY source type; blank would read as
    // its opposite.
    expect(screen.getAllByText("All source types").length).toBeGreaterThan(0);
    // Three rules, three windows. `null` reads as open-ended, never as today.
    expect(screen.getAllByText("2026-01-01 → open-ended")).toHaveLength(3);
  });

  it("groups the cascade by phase and keeps the overlays out of it", () => {
    render(<RuleSetRulesTab detail={detail(LADDER)} />);

    expect(screen.getByText("Phase 2 — Platform and technology fees")).toBeTruthy();
    expect(screen.getByText("Phase 3 — Regulatory taxes")).toBeTruthy();
    // Verification is an overlay whose every row is keep_separate. Rendering it
    // among the phases teaches the reader the opposite of Epic 27 invariant 4.
    expect(screen.getByText("Overlays — never part of the running total")).toBeTruthy();
    expect(isOverlayRule(rule({ category: "VERIFICATION" }))).toBe(true);
    expect(isOverlayRule(rule({ category: "PAYMENT_FEE" }))).toBe(true);
    expect(isOverlayRule(rule({ category: "PLATFORM_FEE" }))).toBe(false);
  });

  it("shows authority and origin as two separate facts", () => {
    render(<RuleSetRulesTab detail={detail(LADDER)} />);
    // The channel that created the row is not the authority behind the rate.
    expect(screen.getAllByText("provider_invoice_practice").length).toBeGreaterThan(0);
    expect(screen.getAllByText("statutory_reference").length).toBeGreaterThan(0);
    expect(screen.getAllByText(/Origin:/).length).toBeGreaterThan(0);
  });

  it("reads the family from the owner when the summary does not carry it", () => {
    expect(isTaxFeeRuleSet(detail({ family: "tax_fee" }))).toBe(true);
    expect(isTaxFeeRuleSet(detail({}, { family: "tax_fee" }))).toBe(true);
    expect(isTaxFeeRuleSet(detail({ family: "money_policy" }))).toBe(false);
    expect(isTaxFeeRuleSet(detail({}))).toBe(false);
  });
});

describe("unresolved is not a quieter exclude", () => {
  const unresolvedLadder = {
    ...LADDER,
    ordered_rules: [
      rule({
        rule_key: "es_dst",
        geography_dependent: true,
        jurisdiction: { kind: "country", id: "ES", hierarchy_version_id: "gh_v7", label: "Spain" },
        rest_of_world_posture: "unresolved",
        unknown_posture: "exclude",
      }),
    ],
  };

  it("says in words that it blocks a complete total, not by colour", () => {
    render(<RuleSetRulesTab detail={detail(unresolvedLadder)} />);
    expect(
      screen.getByText(/Undecided — this blocks a complete total\. It does not mean excluded\./),
    ).toBeTruthy();
  });

  it("separates complete-as-a-document from complete-as-an-answer on Overview", () => {
    render(<TaxFeeLadderOverviewTab detail={detail(unresolvedLadder)} />);
    expect(screen.getByText("No complete headline total can be stated")).toBeTruthy();
    expect(screen.getAllByText(/complete as a document/).length).toBeGreaterThan(0);
    expect(screen.getByText(/es_dst/)).toBeTruthy();
    // The pin exists so a membership republication is diffable.
    expect(screen.getByText("gh_v7")).toBeTruthy();
  });

  it("states the opposite verdict when every posture has decided", () => {
    render(<TaxFeeLadderOverviewTab detail={detail(LADDER)} />);
    expect(screen.getByText("Every geography-dependent rule has decided")).toBeTruthy();
    expect(screen.queryByText("No complete headline total can be stated")).toBeNull();
  });

  it("declares no posture on a rule that reads no geography", () => {
    render(<RuleSetRulesTab detail={detail(LADDER)} />);
    expect(
      screen.getAllByText(
        /This rule reads no geography, so it declares no Rest of world or Unknown posture\./,
      ).length,
    ).toBeGreaterThan(0);
  });
});

describe("nothing renders a zero it cannot vouch for", () => {
  it("refuses the table when the owner did not answer", () => {
    render(<RuleSetRulesTab detail={detail({ ...LADDER, facets_state: "unavailable" })} />);
    expect(screen.getByText("This evidence could not be read")).toBeTruthy();
    expect(screen.queryByText("Rule ladder")).toBeNull();
  });

  it("says something different when the version simply carries no rule", () => {
    render(<RuleSetRulesTab detail={detail({ ...LADDER, ordered_rules: [] })} />);
    expect(screen.getByText("The published version of this ladder carries no rule")).toBeTruthy();
    // And it says so explicitly, rather than letting the reader conclude it:
    // an empty ladder is a fact about publication, not about fees.
    expect(
      screen.getByText(/That is not the same as 'no fee applies to this Project'/),
    ).toBeTruthy();
    // AC9: no blank rule editor, ever. Rules are proposed from governed presets
    // and confirmed through a Change Set, never authored on this screen.
    expect(screen.queryByRole("button", { name: /add rule/i })).toBeNull();
    // Nothing to propose is a TRUTHFUL empty state, and it must stay reachable:
    // inventing a proposal to avoid a blank page is the worse defect.
    expect(screen.getByText(/no qualified proposal matches this Project/i)).toBeTruthy();
  });
});

/**
 * Completeness criterion [0] of tax-fees: "activation opens a blank rule editor
 * WHEN A QUALIFIED PROPOSAL IS POSSIBLE."
 *
 * This screen used to say, correctly for the tree it was written against, that
 * "the proposal path ... has no reachable caller in this build: its MCP tools are
 * not registered and no REST route exposes them". That sentence stopped being
 * true a few hours later, on 2026-08-04, when `TaxFeesCompiler` gained one and the
 * Rule Set read path started serving `summary.preset_proposals`. Two sessions met
 * the same defect from opposite sides on the same day; this is the half that lets
 * the screen offer what the server already knows.
 */
describe("an empty ladder offers its qualified proposals", () => {
  const PROPOSAL = {
    preset: {
      preset_version_id: "tfpv_EXAMPLE",
      issuer: "Direction generale des Finances publiques",
      jurisdiction_code: "FR",
      taxable_subject: "Digital advertising services supplied in France",
    },
    matched_on: ["jurisdiction:FR"],
    why_it_may_apply: "DGFiP states this for FR (Digital advertising services).",
    unproven_qualifications: [
      { code: "provider_passes_through", question: "Does the platform pass this through?" },
    ],
    confidence: "low",
    operator_must_confirm: ["Does the platform pass this through?"],
    draft_rule: { rule_key: "fr_dst", category: "REGULATORY_TAX" },
  };

  function emptyWithProposals(proposals: unknown[]) {
    return detail({ ...LADDER, ordered_rules: [], preset_proposals: proposals });
  }

  it("names what may apply, on whose authority, and for which jurisdiction", () => {
    render(<RuleSetRulesTab detail={emptyWithProposals([PROPOSAL])} />);
    expect(screen.getByText(/1 qualified proposal/i)).toBeTruthy();
    expect(
      screen.getByText(/DGFiP states this for FR \(Digital advertising services\)\./),
    ).toBeTruthy();
    expect(screen.getByText("FR")).toBeTruthy();
  });

  it("shows the questions the operator must answer, not just a confidence", () => {
    render(<RuleSetRulesTab detail={emptyWithProposals([PROPOSAL])} />);
    expect(screen.getByText("Does the platform pass this through?")).toBeTruthy();
    expect(screen.getByText(/low/i)).toBeTruthy();
  });

  it("still refuses to be an editor: the matrix authors nothing", () => {
    render(<RuleSetRulesTab detail={emptyWithProposals([PROPOSAL])} />);
    expect(screen.queryByRole("button", { name: /add rule/i })).toBeNull();
    // AC3: narrowing by Country proves nothing. The screen must say so where the
    // operator reads the proposal, not only in a document.
    expect(screen.getByText(/does not prove that it applies/i)).toBeTruthy();
  });

  // -------------------------------------------------------------------------
  // 2026-08-17. This assertion USED TO BE `queryByRole(/adopt/i) === null`, and
  // it was passing for the wrong reason: not because adopting belonged
  // elsewhere, but because the elsewhere did not exist. No screen in the product
  // could prepare a rule-set Change Set, so the panel's own sentence pointed at
  // nothing. `governance.md`, "A ladder is adopted where it is read".
  // -------------------------------------------------------------------------

  it("offers no adoption without a Project to adopt into", () => {
    // The tab renders in contexts that carry no project id. An affordance that
    // cannot complete is worse than an absent one.
    render(<RuleSetRulesTab detail={emptyWithProposals([PROPOSAL])} />);
    expect(screen.queryByRole("button", { name: /adopt/i })).toBeNull();
  });

  it("opens a Change Set rather than adopting on the click", () => {
    render(
      <RuleSetRulesTab
        detail={emptyWithProposals([PROPOSAL])}
        projectId="p1"
        onAdopted={() => {}}
      />,
    );
    const button = screen.getByRole("button", { name: /Adopt through a Change Set/i });
    // The name says what it opens. "Adopt" alone would promise that the click
    // decides, and it is the dialog that asks every open question first.
    expect(button).toBeTruthy();
  });

  it("a proposal with nothing left unproven says so instead of inventing a question", () => {
    render(
      <RuleSetRulesTab
        detail={emptyWithProposals([
          {
            ...PROPOSAL,
            unproven_qualifications: [],
            operator_must_confirm: [],
            confidence: "high",
          },
        ])}
      />,
    );
    expect(screen.getByText(/nothing left to confirm/i)).toBeTruthy();
  });
});

describe("the four families that carry no rules by construction", () => {
  // `governance_rule_sets.py:478-482` refuses to store a ladder for a family
  // with no rule normalizer. Their whole governed content is the version
  // payload, which the server has always sent and no tab read.
  //
  // AND THE NAME OF THE POLICY IS THE SERVER'S, NOT THIS FILE'S (2026-08-31).
  // These cases used to assert headings the CONSOLE composed from a map of four
  // families to hand-written labels and "governs" sentences — a second statement
  // of what a version contains, which had already fallen two fields behind
  // `money_policy.py`. The heading, the questions and the count now come from the
  // authoring plan (`plan_authoring`), so the stubs below carry the plan's real
  // shape: `rule_set.profile_label`, and `fields[]` as
  // `RuleSetFormField.as_dict()` emits them.

  function planFor(profileLabel: string, fields: { key: string; question: string }[]) {
    return {
      rule_set: {
        id: "grs_EXAMPLE",
        label: "Whatever the Project called it",
        family: "money_policy",
        profile: "profile_EXAMPLE",
        profile_label: profileLabel,
        lifecycle_status: "published",
      },
      in_force: null,
      draft: null,
      composed_by: null,
      fields: fields.map((field) => ({
        ...field,
        kind: "text",
        options: [],
        why: null,
        required: true,
        unit: null,
        value: null,
      })),
      carries_forward: { ordered_rule_count: 0, pinned_references: [] },
    };
  }

  function stubPlan(plan: unknown) {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        new Response(JSON.stringify(plan), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
      ),
    );
  }

  afterEach(() => {
    // A shared `fetch` makes the NEXT test pass for the previous test's reason.
    vi.unstubAllGlobals();
  });

  const cases = [
    [
      "money_policy",
      "Money Policy",
      [
        { key: "reporting_currency", question: "Which currency does this Project report in?" },
        { key: "rounding", question: "How is a converted amount rounded at the boundary?" },
      ],
      { reporting_currency: "EUR", rounding: "half_even" },
    ],
    [
      "fx_ingestion",
      "FX Ingestion Policy",
      [
        { key: "provider", question: "Who publishes the rates this Project converts with?" },
        { key: "cadence", question: "How often does a batch arrive?" },
      ],
      { provider: "example-fx", cadence: "daily" },
    ],
    [
      "timezone_policy",
      "Reporting Timezone Policy",
      [
        { key: "reporting_timezone", question: "Which timezone decides what day a figure falls on?" },
        { key: "tzdb_version", question: "Which timezone database version is this pinned to?" },
      ],
      { reporting_timezone: "Europe/Paris", tzdb_version: "2026a" },
    ],
    [
      "dq_policy",
      "Data Quality Policy",
      [
        { key: "severity", question: "How serious is a failure of this monitor?" },
        { key: "window_days", question: "Over how many days is it observed?" },
      ],
      { severity: "warning", window_days: 7 },
    ],
  ] as const;

  for (const [family, profileLabel, fields, policy] of cases) {
    it(`renders ${family}'s policy under the profile's own name`, async () => {
      stubPlan(planFor(profileLabel, [...fields]));
      render(
        <RuleSetRulesTab
          projectId="proj_EXAMPLE"
          detail={detail({ family, facets_state: "available", ordered_rules: [], policy })}
        />,
      );
      // The heading is the profile's `label`, arriving over the wire.
      expect(await screen.findByText(`${profileLabel} — governed settings`)).toBeTruthy();
      // The sentence it must NOT borrow: these families will never carry a rule,
      // so "nothing is published yet" is a red herring.
      expect(screen.queryByText("This rule set has no published rules")).toBeNull();
      for (const value of Object.values(policy)) {
        expect(screen.getByText(String(value))).toBeTruthy();
      }
      // Each value is labelled by the question its profile asks, not by a
      // humanized storage key. `getAllByText`, because the compose form under
      // this panel asks the same declared question — one declaration, read in
      // both places, which is the point.
      for (const field of fields) {
        expect(screen.getAllByText(field.question).length).toBeGreaterThan(0);
      }
    });
  }

  it("names as many things as the profile declares, not as many as it once knew", async () => {
    // THE MEASURED DEFECT: the console's Money Policy sentence named five things
    // where `money_policy.py` declares seven fields. The count is derived now, so
    // the two cannot disagree.
    const seven = Array.from({ length: 7 }, (_, index) => ({
      key: `field_${index}`,
      question: `Question ${index}?`,
    }));
    stubPlan(planFor("Money Policy", seven));
    render(
      <RuleSetRulesTab
        projectId="proj_EXAMPLE"
        detail={detail({
          family: "money_policy",
          facets_state: "available",
          ordered_rules: [],
          policy: { field_0: "answered" },
        })}
      />,
    );
    expect(await screen.findByText(/a version of it decides 7 things/)).toBeTruthy();
  });

  it("shows a field added server-side with no edit to this console", async () => {
    // The pin the repair exists for. `triangulation_pivot` is declared by the
    // profile and was named nowhere in the front end. It labels its own row here
    // because the label travelled with the declaration.
    stubPlan(
      planFor("Money Policy", [
        { key: "reporting_currency", question: "Which currency does this Project report in?" },
        { key: "triangulation_pivot", question: "Through which currency?" },
      ]),
    );
    render(
      <RuleSetRulesTab
        projectId="proj_EXAMPLE"
        detail={detail({
          family: "money_policy",
          facets_state: "available",
          ordered_rules: [],
          policy: { reporting_currency: "EUR", triangulation_pivot: "USD" },
        })}
      />,
    );
    expect(await screen.findByText("Through which currency?")).toBeTruthy();
    expect(screen.getByText("USD")).toBeTruthy();
  });

  it("names no family when the declaration could not be read", async () => {
    // A refusal is not a licence to invent a label. The policy still reads; the
    // name of the family does not appear, because nothing declared it.
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => new Response("{}", { status: 503 })),
    );
    render(
      <RuleSetRulesTab
        projectId="proj_EXAMPLE"
        detail={detail({
          family: "money_policy",
          facets_state: "available",
          ordered_rules: [],
          policy: { reporting_currency: "EUR" },
        })}
      />,
    );
    expect(await screen.findByText("Governed settings")).toBeTruthy();
    expect(screen.queryByText(/Money Policy/)).toBeNull();
    expect(screen.getByText("EUR")).toBeTruthy();
  });

  it("still distinguishes an unpublished policy from a policy of zero", () => {
    render(
      <RuleSetRulesTab
        detail={detail({ family: "money_policy", facets_state: "available", ordered_rules: [], policy: {} })}
      />,
    );
    expect(screen.getByText("No version is published")).toBeTruthy();
    expect(screen.getByText(/This is not a policy of zero\./)).toBeTruthy();
  });
});

describe("metric_reconciliation is untouched", () => {
  it("still renders its four original columns", () => {
    render(
      <RuleSetRulesTab
        detail={detail({
          family: "metric_reconciliation",
          facets_state: "available",
          ordered_rules: [
            {
              method: "PREFER_SOURCE",
              concept: { object_id: "cpt_EXAMPLE" },
              sources: [{ object_id: "sv_EXAMPLE", version_id: "svv_EXAMPLE" }],
              note: "Platform wins on impressions",
            },
          ],
        })}
      />,
    );
    for (const column of ["Method", "Governs", "Sources", "Note"]) {
      expect(screen.getByText(column)).toBeTruthy();
    }
    expect(screen.getByText("PREFER_SOURCE")).toBeTruthy();
    expect(screen.getByText("sv_EXAMPLE@svv_EXAMPLE")).toBeTruthy();
  });
});
