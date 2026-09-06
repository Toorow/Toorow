/**
 * The Rule Set workbench, made family-aware (Story 41.7).
 *
 * `RuleSetRulesTab` was written for one family and served six. Its columns —
 * `["#","Method","Governs","Sources","Note"]` — are the keys of a
 * `metric_reconciliation` rule, and `displayValue(undefined)` returns the string
 * `"Unavailable"`. So a published Tax & Fee ladder of N rules rendered as N rows
 * reading `1 | Unavailable | Unavailable | — | Unavailable`: a screen actively
 * claiming that governed evidence could not be read, while the whole ladder sat
 * complete in the same payload.
 *
 * The class, measured from the registered `RuleSetProfile`s rather than assumed:
 *
 *   metric_reconciliation   carries rules      rendered correctly today
 *   tax_fee                 carries rules      rendered as "Unavailable"
 *   money_policy            carries NONE       content rendered nowhere
 *   fx_ingestion            carries NONE       content rendered nowhere
 *   timezone_policy         carries NONE       content rendered nowhere
 *   dq_policy               carries NONE       content rendered nowhere
 *
 * The four policy-only families are not families whose ladder is empty:
 * `governance_rule_sets.py:478-482` REFUSES to store rules for a family with no
 * rule normalizer. Their entire governed content is the version payload, which
 * the server already sends as `summary.policy` and which no tab read. Telling
 * them "its current version carries none, or nothing is published yet" was a
 * false sentence — they will never carry one.
 *
 * What this file refuses to render, and why:
 *
 *   - No waterfall, no chart. The waterfall belongs to Analyze
 *     (`alignment-register.md:65`); its materialization is an open design
 *     question (Jean, 2026-08-01) and inventing a console surface for it here
 *     would be a design decision dressed as a repair.
 *   - No per-rule status column. `tax_fee_rule_set.py:374-376`: "a rule inside a
 *     PUBLISHED version is active by virtue of the version, and a per-rule status
 *     would be a second activation authority".
 *   - No editing, reordering or "add rule" affordance ON THE MATRIX. Writes go
 *     through the Change Set lifecycle, and since 2026-08-17 one of them starts
 *     here: `LadderAdoptionDialog` opens a rule-set Change Set for a qualified
 *     preset. That is the lifecycle, not a bypass of it — what stays forbidden
 *     is a matrix that grows an edit affordance of its own, which is how the
 *     second-activation-authority defect comes back.
 *   - No re-sort. `ordered_rules` arrives sorted by
 *     `(cascade_phase, sequence_order, rule_key)` and a `(phase, order)` clash is
 *     REFUSED at validation, never tie-broken. Order is content.
 *
 * Everything below is read from `detail.summary`, composed server-side. There is
 * no browser-side join here on purpose.
 */
import { useEffect, useState } from "react";

import { apiGet } from "../lib/apiFetch";
import LadderAdoptionDialog from "./LadderAdoptionDialog";
import type { AuthoringField, AuthoringPlan } from "./RuleSetVersionAuthoring";
import {
  Accordion,
  AccordionContent,
  AccordionItem,
  AccordionTrigger,
  Badge,
  Button,
  EmptyState,
  EvidenceRows,
  Metric,
  Panel,
  PanelHeader,
  PanelBody,
  Status,
  displayValue,
} from "../ui";
import type { GovernanceObject } from "./governanceSurface";

type Row = Record<string, unknown>;

function summaryOf(detail: GovernanceObject): Record<string, unknown> {
  return (detail.summary ?? {}) as Record<string, unknown>;
}

function rulesOf(detail: GovernanceObject): Row[] {
  const value = summaryOf(detail).ordered_rules;
  return Array.isArray(value) ? (value as Row[]) : [];
}

type Proposal = Record<string, unknown>;

function proposalsOf(detail: GovernanceObject): Proposal[] {
  const value = summaryOf(detail).preset_proposals;
  return Array.isArray(value) ? (value as Proposal[]) : [];
}

/**
 * Qualified candidates for an empty ladder — completeness criterion [0].
 *
 * The unit is the QUESTION, not the rate. A reader of this panel is deciding
 * whether a shared statutory reference describes THEIR invoice, and the only
 * thing that settles that is the list of things nobody has checked yet. So the
 * rate is deliberately not the headline: the issuer, the jurisdiction and the
 * open questions are, and `confidence` is rendered as what it is — a count of
 * answered questions, never a feeling.
 *
 * NO ONE-CLICK ADOPT, and that is not the same as no adoption. Until 2026-08-17
 * this panel offered nothing at all, and the elsewhere it pointed to did not
 * exist: no screen in the product could prepare a rule-set Change Set. The
 * button below opens one — created, prepared and confirmed server-side, reached
 * only after every unproven qualification is answered. What the story forbids is
 * a click that silently converts a shared reference into Project policy, and a
 * dialog that refuses to publish while one question is open is its opposite.
 */
function ProposalOffer({
  proposals,
  projectId,
  ruleSetId,
  onAdopted,
}: {
  proposals: Proposal[];
  projectId?: string;
  ruleSetId?: string;
  onAdopted?: () => void;
}) {
  const [adopting, setAdopting] = useState<{ id: string; label: string } | null>(null);
  return (
    <Panel flush>
      <PanelHeader
        title={`${proposals.length} qualified proposal${proposals.length === 1 ? "" : "s"} for this ladder`}
        description="Published presets whose jurisdiction this Project governs. Narrowing by Country does not prove that it applies: each candidate carries what is still unproven, and adopting one opens a Change Set that asks every open question before it publishes — never a click that turns a shared reference into Project policy."
      />
      <PanelBody className="grid gap-3">
        {proposals.map((proposal, index) => {
          const preset = (proposal.preset ?? {}) as Record<string, unknown>;
          const questions = Array.isArray(proposal.operator_must_confirm)
            ? (proposal.operator_must_confirm as unknown[])
            : [];
          return (
            <div
              key={String(preset.preset_version_id ?? index)}
              className="grid gap-2 rounded-medium border border-divider-base p-4"
            >
              <div className="flex flex-wrap items-center gap-2">
                <span className="text-ui font-medium">{displayValue(preset.issuer)}</span>
                {preset.jurisdiction_code ? (
                  <Badge outline>{String(preset.jurisdiction_code)}</Badge>
                ) : (
                  <Badge outline>No jurisdiction — applies on contract, not on geography</Badge>
                )}
                <Badge outline>{`confidence ${displayValue(proposal.confidence)}`}</Badge>
              </div>
              <p className="m-0 text-caption text-text-secondary">
                {displayValue(proposal.why_it_may_apply)}
              </p>
              {questions.length > 0 ? (
                <div>
                  <p className="m-0 text-caption text-text-secondary">
                    What you must confirm before this can apply
                  </p>
                  <ul className="m-0 mt-1 list-disc space-y-1 pl-5 text-caption">
                    {questions.map((question, position) => (
                      <li key={`${String(question)}:${position}`}>{String(question)}</li>
                    ))}
                  </ul>
                </div>
              ) : (
                <Status
                  as="block"
                  tone="info"
                  title="Nothing left to confirm on the preset itself"
                >
                  Every qualification its issuer attached has been answered. That still
                  does not prove that it applies to this Project — it means the shared
                  reference is complete, not that your invoice carries this charge.
                </Status>
              )}
              {projectId && ruleSetId ? (
                <div>
                  {/* Not an adopt button: it opens the Change Set, which reads
                      back what would be published and asks every open question
                      before anything is written. */}
                  <Button
                    type="button"
                    variant="secondary"
                    onClick={() =>
                      setAdopting({
                        id: String(preset.preset_version_id ?? ""),
                        label: String(preset.label ?? preset.issuer ?? "this preset"),
                      })
                    }
                  >
                    Adopt through a Change Set…
                  </Button>
                </div>
              ) : null}
            </div>
          );
        })}
      </PanelBody>
      {projectId && ruleSetId && adopting ? (
        <LadderAdoptionDialog
          open
          projectId={projectId}
          ruleSetId={ruleSetId}
          presetVersionId={adopting.id}
          presetLabel={adopting.label}
          onClose={() => setAdopting(null)}
          onAdopted={onAdopted}
        />
      ) : null}
    </Panel>
  );
}

function policyOf(detail: GovernanceObject): Record<string, unknown> {
  const value = summaryOf(detail).policy;
  return value && typeof value === "object" ? (value as Record<string, unknown>) : {};
}

/**
 * The family, read from the summary with the owner as fallback. Both are stamped
 * server-side by `_governed_rule_set` (`governance_read_model.py:906`, `:938`),
 * and a capability owner object that reaches this lens carries neither — which
 * is why the fallback is `null` and not a guess.
 */
export function ruleSetFamily(detail: GovernanceObject): string | null {
  const fromSummary = summaryOf(detail).family;
  if (typeof fromSummary === "string" && fromSummary) return fromSummary;
  const owner = (detail.owner ?? {}) as Record<string, unknown>;
  const fromOwner = owner.family;
  return typeof fromOwner === "string" && fromOwner ? fromOwner : null;
}

export function isTaxFeeRuleSet(detail: GovernanceObject): boolean {
  return ruleSetFamily(detail) === "tax_fee";
}

/**
 * The families whose profile declares no `validate_rules`, so their governed
 * content is a policy payload and never an ordered ladder.
 *
 * IT ROUTES, AND THAT IS ALL IT DOES. Until 2026-08-31 this was a map from each
 * family to a hand-written LABEL and a hand-written sentence naming what it
 * governs — a second statement of what a version contains, free to disagree with
 * the profile that declares it, and it did: the Money Policy sentence named five
 * things where `money_policy.py` declares SEVEN form fields, so the console had
 * been silently under-describing the policy since the day the sixth was added.
 * `governance.md` ("A Rule Set version is drafted, then published") already ruled
 * it out — *"the console decides what a family's version contains … a label
 * written in the front end rather than declared by the profile"* is one of its
 * Incomplete-if lines. The label and the governed fields are read from the
 * profile now (`PolicyOnlyRulesTab`); what stays here is the one fact this file
 * needs before any request is made: which READ to draw.
 */
export const POLICY_ONLY_FAMILIES: ReadonlySet<string> = new Set([
  "money_policy",
  "fx_ingestion",
  "timezone_policy",
  "dq_policy",
]);

export function isPolicyOnlyRuleSet(detail: GovernanceObject): boolean {
  const family = ruleSetFamily(detail);
  return family !== null && POLICY_ONLY_FAMILIES.has(family);
}

// ---------------------------------------------------------------------------
// Exact decimal formatting. The whole epic is a story about exact money; the
// surface must not be where the exactness is lost.
// ---------------------------------------------------------------------------

/**
 * Move a decimal point right by `places`, on the STRING.
 *
 * `rate` is stored as `str(normalized["rate"])` over a `NUMERIC(12,6)`, so
 * `"0.030000"` is exact. `Number("0.030000") * 100` is `3.0000000000000004`.
 * Returns `null` for anything that is not a plain decimal, so a malformed value
 * is reported as itself rather than silently becoming a number.
 */
export function shiftDecimal(text: string, places: number): string | null {
  const match = /^(-?)(\d+)(?:\.(\d*))?$/.exec(text.trim());
  if (!match) return null;
  const [, sign, whole, fraction = ""] = match;
  const digits = whole + fraction;
  const pointAt = whole.length + places;
  let shifted: string;
  if (pointAt <= 0) {
    shifted = `0.${"0".repeat(-pointAt)}${digits}`;
  } else if (pointAt >= digits.length) {
    shifted = digits + "0".repeat(pointAt - digits.length);
  } else {
    shifted = `${digits.slice(0, pointAt)}.${digits.slice(pointAt)}`;
  }
  // A leading zero run is noise, but `0.5` must keep its zero.
  shifted = shifted.replace(/^0+(?=\d)/, "");
  return sign + shifted;
}

/** `"0.030000"` -> `"3.0000 %"`, exactly, or the value verbatim if it is not a decimal. */
export function ratePercent(value: unknown): string {
  if (typeof value !== "string" || !value) return displayValue(value);
  return `${shiftDecimal(value, 2) ?? value} %`;
}

/** Micros are integers. Format for display; never compute. */
export function fromMicros(value: unknown): string | null {
  if (typeof value === "number" && Number.isInteger(value)) {
    return shiftDecimal(String(value), -6);
  }
  if (typeof value === "string" && value) return shiftDecimal(value, -6);
  return null;
}

function money(value: unknown, currency: unknown): string {
  const amount = fromMicros(value);
  if (amount === null) return displayValue(value);
  return typeof currency === "string" && currency ? `${amount} ${currency}` : amount;
}

/**
 * The value column, in the shape its `form` uses. A single "Value" column that
 * printed `rate` for every form would show a blank for the three forms that do
 * not carry one.
 */
export function ruleValue(rule: Row): string {
  switch (rule.form) {
    case "PERCENTAGE":
    case "GROSS_UP":
      return ratePercent(rule.rate);
    case "FLAT":
      return money(rule.amount_micros, rule.currency);
    case "CPM":
      return `${money(rule.cpm_micros, rule.currency)} per 1000`;
    case "PER_TRANSACTION":
      return `${money(rule.amount_micros, rule.currency)} per transaction`;
    case "SPEND_TIERS": {
      const tiers = (rule.tiers ?? {}) as Record<string, unknown>;
      const bands = Array.isArray(tiers.bands) ? tiers.bands.length : 0;
      return `${bands} band${bands === 1 ? "" : "s"}, ${displayValue(tiers.mode)}`;
    }
    default:
      return displayValue(rule.form);
  }
}

// ---------------------------------------------------------------------------
// The cascade
// ---------------------------------------------------------------------------

/**
 * Phase 1 is Net Media, the base. The five that follow are the categories'
 * declared phases (`tax_fee_presets.py:453-459`).
 *
 * `VERIFICATION` and `PAYMENT_FEE` are deliberately absent: they are overlays,
 * every row of theirs carries `keep_separate = TRUE`, and grouping them among
 * the phases would teach the reader that verification is a cascade step — the
 * exact merge Epic 27 invariant 4 and E41-AD4 forbid.
 */
const PHASE_LABELS: Record<number, string> = {
  1: "Phase 1 — Net media (the base)",
  2: "Phase 2 — Platform and technology fees",
  3: "Phase 3 — Regulatory taxes",
  4: "Phase 4 — Withholding tax gross-up",
  5: "Phase 5 — Agency fees",
  6: "Phase 6 — Sales tax",
};

const OVERLAY_CATEGORIES = new Set(["VERIFICATION", "PAYMENT_FEE"]);

export function isOverlayRule(rule: Row): boolean {
  return typeof rule.category === "string" && OVERLAY_CATEGORIES.has(rule.category);
}

/** `unresolved` on either posture. The same predicate as `TaxFeeLadder.unresolved_geography_rules`. */
export function hasUnresolvedPosture(rule: Row): boolean {
  return rule.rest_of_world_posture === "unresolved" || rule.unknown_posture === "unresolved";
}

function LadderTable({ rows, offset }: { rows: Row[]; offset: number }) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full text-caption">
        <thead>
          <tr className="text-left text-muted-foreground">
            <th scope="col" className="py-1 pr-3 font-medium">#</th>
            <th scope="col" className="py-1 pr-3 font-medium">Rule</th>
            <th scope="col" className="py-1 pr-3 font-medium">Category</th>
            <th scope="col" className="py-1 pr-3 font-medium">Form</th>
            <th scope="col" className="py-1 pr-3 font-medium">Value</th>
            <th scope="col" className="py-1 pr-3 font-medium">Base</th>
            <th scope="col" className="py-1 pr-3 font-medium">Money basis</th>
            <th scope="col" className="py-1 pr-3 font-medium">Scope</th>
            <th scope="col" className="py-1 pr-3 font-medium">Source types</th>
            <th scope="col" className="py-1 pr-3 font-medium">Effective</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((rule, index) => {
            const scopeRef = rule.scope_ref ? ` ${String(rule.scope_ref)}` : "";
            const scopes = Array.isArray(rule.source_type_scope) ? rule.source_type_scope : [];
            return (
              <tr key={String(rule.rule_key ?? index)} className="border-t border-border/60">
                <td className="py-1 pr-3 tabular-nums">{offset + index + 1}</td>
                <td className="py-1 pr-3 font-mono">{displayValue(rule.label ?? rule.rule_key)}</td>
                <td className="py-1 pr-3">{displayValue(rule.category)}</td>
                <td className="py-1 pr-3">{displayValue(rule.form)}</td>
                <td className="py-1 pr-3 tabular-nums">{ruleValue(rule)}</td>
                <td className="py-1 pr-3">{displayValue(rule.base_target)}</td>
                <td className="py-1 pr-3">{displayValue(rule.money_basis)}</td>
                <td className="py-1 pr-3">{`${displayValue(rule.scope_kind)}${scopeRef}`}</td>
                {/* An empty array means every source type. Rendering it blank
                    would read as "none", which is its opposite. */}
                <td className="py-1 pr-3">
                  {scopes.length === 0 ? "All source types" : scopes.join(", ")}
                </td>
                <td className="py-1 pr-3">
                  {`${displayValue(rule.effective_from)} → ${rule.effective_to ? String(rule.effective_to) : "open-ended"}`}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

/**
 * Drop the keys whose value is absent.
 *
 * Only ever applied to OPTIONAL fields. A mandatory field must keep its row and
 * say `Unavailable`, because there its absence is the finding.
 */
function whenPresent(source: Record<string, unknown>): Record<string, unknown> {
  return Object.fromEntries(
    Object.entries(source).filter(([, value]) => value !== null && value !== undefined && value !== ""),
  );
}

/** Authority, evidence and geography, per rule. */
function RuleEvidence({ rule }: { rule: Row }) {
  const evidence = (rule.source_evidence ?? {}) as Record<string, unknown>;
  const jurisdiction = (rule.jurisdiction ?? {}) as Record<string, unknown>;
  const assumptions = Array.isArray(evidence.assumptions) ? evidence.assumptions : [];
  const geographic = rule.geography_dependent === true;
  return (
    <div className="grid gap-3">
      <div className="grid gap-1">
        {/* The channel that created the row is not the authority behind the
            rate. Two facts, two rows. */}
        <div>
          <span className="text-muted-foreground">Authority: </span>
          <span className="font-mono">{displayValue(rule.authority_kind)}</span>
        </div>
        <div>
          <span className="text-muted-foreground">Origin: </span>
          <span className="font-mono">{displayValue(rule.origin)}</span>
        </div>
      </div>
      {/* Two different absences, deliberately rendered differently.
          `issuer`, `reference` and `reference_version` are MANDATORY at
          validation (`tax_fee_rule_set.py:326-333`), so a missing one is a DATA
          DEFECT and must say `Unavailable` rather than disappear. The rest are
          optional and are "shown when present" — printing `Unavailable` for a
          field the rule never had to carry would report a defect that is not
          one, and drown the real one. `preset_version_id` reads as
          governed-preset provenance, not as a link: the preset proposed this
          rule, it did not decide it. */}
      <EvidenceRows
        label={`Source evidence for ${String(rule.rule_key ?? "this rule")}`}
        source={{
          issuer: evidence.issuer,
          reference: evidence.reference,
          reference_version: evidence.reference_version,
          ...whenPresent({
            authoritative_url: evidence.authoritative_url,
            document_ref: evidence.document_ref,
            governed_preset_version: evidence.preset_version_id,
            published_on: evidence.published_on,
            last_verified_on: evidence.last_verified_on,
          }),
        }}
      />
      {assumptions.length > 0 ? (
        <Status as="block" tone="info" title="This rule carries assumptions">
          {assumptions.map((item) => displayValue(item)).join(" · ")}
        </Status>
      ) : null}
      {geographic ? (
        <div className="grid gap-1">
          <div>
            <span className="text-muted-foreground">Jurisdiction: </span>
            <span className="font-mono">
              {`${displayValue(jurisdiction.kind)} ${displayValue(jurisdiction.id)}`}
            </span>
            {jurisdiction.label ? ` — ${String(jurisdiction.label)}` : null}
          </div>
          {/* The pin exists so a membership republication is diffable. Hiding it
              makes the rule look unpinned. */}
          <div>
            <span className="text-muted-foreground">Hierarchy version: </span>
            <span className="font-mono">{displayValue(jurisdiction.hierarchy_version_id)}</span>
          </div>
          <PostureLine name="Rest of world" value={rule.rest_of_world_posture} />
          <PostureLine name="Unknown" value={rule.unknown_posture} />
        </div>
      ) : (
        // The validator refuses to store postures on a rule that reads no
        // geography, so rendering empty ones would invent one.
        <div className="text-muted-foreground">
          This rule reads no geography, so it declares no Rest of world or Unknown posture.
        </div>
      )}
      {Object.keys((rule.conditions ?? {}) as Record<string, unknown>).length > 0 ? (
        <EvidenceRows
          label={`Conditions on ${String(rule.rule_key ?? "this rule")}`}
          source={rule.conditions as Record<string, unknown>}
        />
      ) : null}
    </div>
  );
}

/**
 * A posture. `unresolved` is not a quieter `exclude`: it means the ladder cannot
 * decide, and it blocks a complete total. The distinction is carried by the word
 * and by the sentence, never by colour alone.
 */
function PostureLine({ name, value }: { name: string; value: unknown }) {
  const unresolved = value === "unresolved";
  return (
    <div>
      <span className="text-muted-foreground">{name} posture: </span>
      <Badge tone={unresolved ? "warning" : "neutral"}>{displayValue(value)}</Badge>
      {unresolved ? (
        <span className="ml-2">
          Undecided — this blocks a complete total. It does not mean excluded.
        </span>
      ) : null}
    </div>
  );
}

// ---------------------------------------------------------------------------
// The tabs
// ---------------------------------------------------------------------------

/** The Global Rule Matrix. It IS the Rule Set workbench's Rules tab. */
export function TaxFeeLadderRulesTab({
  detail,
  projectId,
  onAdopted,
}: {
  detail: GovernanceObject;
  projectId?: string;
  onAdopted?: () => void;
}) {
  const rules = rulesOf(detail);
  const state = summaryOf(detail).facets_state;

  if (state === "unavailable") {
    return (
      <Status as="block" tone="warning" title="This evidence could not be read">
        Its owner did not answer. This is not a count of zero, and nothing below should be
        read as one.
      </Status>
    );
  }
  if (rules.length === 0) {
    /* AC9 forbids presenting an empty matrix as the end of the road, and equally
       forbids inventing a blank rule editor here — a ladder is PROPOSED from
       governed presets and CONFIRMED through a Change Set, and presets propose,
       they do not decide.

       ⚠️ THIS BLOCK USED TO SAY there was "no reachable caller in this build":
       `propose_from_presets` had exactly one, `fee_tax_mcp.py`, and the 41.6
       cutover had unregistered that module. It was true when written, on the
       morning of 2026-08-04, and false by that afternoon — `TaxFeesCompiler`
       gained one on the activation path and the Rule Set read path now serves
       `summary.preset_proposals`. Two sessions met the same defect from opposite
       sides on the same day, which is why this comment is kept rather than
       deleted: it is the record of what completeness criterion [0] was about.

       What has NOT changed: this screen still authors nothing. It shows what may
       apply and what remains unproven; adopting a candidate is a Change Set. */
    const proposals = proposalsOf(detail);
    if (proposals.length === 0) {
      return (
        <EmptyState
          title="The published version of this ladder carries no rule"
          description="That is not the same as 'no fee applies to this Project': it means nothing has been published into this ladder yet, and a ladder that cannot decide states no total at all. Rules are proposed from governed presets and confirmed through a Change Set — never authored on this screen. And no qualified proposal matches this Project today: no published preset covers a jurisdiction it governs, so there is genuinely nothing to offer. Publishing a preset, or governing the country a preset names, is what makes candidates appear here."
        />
      );
    }
    return (
      <ProposalOffer
        proposals={proposals}
        projectId={projectId}
        ruleSetId={detail.object_ref.id}
        onAdopted={onAdopted}
      />
    );
  }

  const ladder = rules.filter((rule) => !isOverlayRule(rule));
  const overlays = rules.filter(isOverlayRule);
  const phases = [...new Set(ladder.map((rule) => Number(rule.cascade_phase)))].sort(
    (a, b) => a - b,
  );
  let counted = 0;

  return (
    <div className="grid gap-4">
      <Panel flush>
        <PanelHeader
          title="Rule ladder"
          description="Order is content, not a display choice: it is stored in the immutable version, and a clash of (phase, order) is refused rather than tie-broken. Every rule inside a phase reads the subtotal as at phase entry — sequence_order is a display and override key, not a compounding order."
        />
        <PanelBody className="grid gap-4">
          {phases.map((phase) => {
            const rows = ladder.filter((rule) => Number(rule.cascade_phase) === phase);
            const offset = counted;
            counted += rows.length;
            return (
              <div key={phase} className="grid gap-1">
                <h4 className="text-caption font-medium">
                  {PHASE_LABELS[phase] ?? `Phase ${phase}`}
                </h4>
                <LadderTable rows={rows} offset={offset} />
              </div>
            );
          })}
        </PanelBody>
      </Panel>

      {overlays.length > 0 ? (
        <Panel flush>
          <PanelHeader
            title="Overlays — never part of the running total"
            description="Verification (IAS/DV) and payment fees are kept separate by construction: every row of theirs carries keep_separate. They are read alongside the ladder, they never enter it, and adding them to the total would double-count what the platform already reported."
          />
          <PanelBody>
            <LadderTable rows={overlays} offset={0} />
          </PanelBody>
        </Panel>
      ) : null}

      <Panel flush>
        <PanelHeader
          title="Authority and evidence"
          description="Who says so, and where it is written down. The authority behind a rate and the channel that created the row are two different facts."
        />
        <PanelBody>
          {/* Open by default, and collapsible rather than collapsed. Authority,
              issuer and reference are MANDATORY at validation, and both postures
              decide whether a total can be stated at all — evidence that must be
              clicked to exist is evidence a reader will not know is missing. */}
          <Accordion
            type="multiple"
            defaultValue={rules.map((rule, index) => String(rule.rule_key ?? index))}
          >
            {rules.map((rule, index) => (
              <AccordionItem key={String(rule.rule_key ?? index)} value={String(rule.rule_key ?? index)}>
                <AccordionTrigger>
                  <span className="font-mono">{displayValue(rule.rule_key)}</span>
                  <span className="ml-2 text-muted-foreground">
                    {displayValue(rule.authority_kind)}
                  </span>
                </AccordionTrigger>
                <AccordionContent>
                  <RuleEvidence rule={rule} />
                </AccordionContent>
              </AccordionItem>
            ))}
          </Accordion>
        </PanelBody>
      </Panel>
    </div>
  );
}

/**
 * The ladder's health. Its one job beyond the counts is to keep "complete as a
 * document" and "complete as an answer" apart: a ladder every one of whose rules
 * is published can still be unable to state a total, because it has not decided
 * what happens outside its named jurisdictions.
 */
export function TaxFeeLadderOverviewTab({ detail }: { detail: GovernanceObject }) {
  const summary = summaryOf(detail);
  const policy = policyOf(detail);
  const rules = rulesOf(detail);

  if (summary.facets_state === "unavailable") {
    return (
      <Status as="block" tone="warning" title="This evidence could not be read">
        Its owner did not answer. This is not a count of zero.
      </Status>
    );
  }

  const unresolved = rules.filter(hasUnresolvedPosture);
  const pinned = [
    ...new Set(
      rules
        .map((rule) => (rule.jurisdiction ?? {}) as Record<string, unknown>)
        .map((jurisdiction) => jurisdiction.hierarchy_version_id)
        .filter((value): value is string => typeof value === "string" && value !== ""),
    ),
  ].sort();

  return (
    <div className="grid gap-4">
      <Panel flush>
        <PanelHeader
          title="Ladder"
          description="The published version, and what it was compiled from. Rounding is declared once at ladder level and nowhere else — a per-rule rounding mode is how a ladder ends up with several rounding boundaries whose order changes the total."
        />
        <PanelBody className="grid gap-4 sm:grid-cols-3">
          <Metric label="Rules" value={String(rules.length)} />
          <Metric label="Versions" value={displayValue(summary.version_count)} />
          <Metric label="Approvals" value={displayValue(summary.approvals)} />
          <Metric label="Exceptions in force" value={displayValue(summary.active_exceptions)} />
          <Metric label="Rounding" value={displayValue(policy.rounding)} />
          <Metric label="Default money basis" value={displayValue(policy.default_money_basis)} />
        </PanelBody>
        <PanelBody>
          <EvidenceRows
            label="Ladder version identity"
            source={{
              content_hash: summary.content_hash,
              effective_from: summary.effective_from,
              effective_to: summary.effective_to ?? "Open-ended",
              // Distinct from the current version on purpose: a rollback reads
              // this, and collapsing the two makes a failed publication invisible.
              pending_version: summary.has_pending_version ? "Yes" : "No",
              last_known_good_version: summary.last_known_good_version_id,
              profile: summary.profile,
            }}
          />
        </PanelBody>
      </Panel>

      <Panel flush>
        <PanelHeader
          title="Can this ladder state a total?"
          description="A ladder can be complete as a document and incomplete as an answer. Those are two different states and this screen keeps them apart."
        />
        <PanelBody className="grid gap-3">
          <Metric
            label="Rules whose Rest of world or Unknown posture is unresolved"
            value={String(unresolved.length)}
            hint="Counted here with the same predicate the server uses, over the published ordered_rules."
          />
          {unresolved.length > 0 ? (
            <Status as="block" tone="warning" title="No complete headline total can be stated">
              {`This ladder is complete as a document — every rule is published — and incomplete as an answer: ${unresolved.length} rule${unresolved.length === 1 ? "" : "s"} leave${unresolved.length === 1 ? "s" : ""} Rest of world or Unknown undecided (${unresolved.map((rule) => String(rule.rule_key)).join(", ")}). Undecided is not excluded, so spend outside the named jurisdictions can be neither included nor left out.`}
            </Status>
          ) : (
            <Status as="block" tone="success" title="Every geography-dependent rule has decided">
              No rule leaves Rest of world or Unknown undecided, so the ladder can state a
              complete total for the versions it pins.
            </Status>
          )}
          <div>
            <span className="text-muted-foreground">Pinned hierarchy versions: </span>
            {pinned.length === 0 ? "None — no rule reads a geography" : (
              <span className="font-mono">{pinned.join(", ")}</span>
            )}
          </div>
        </PanelBody>
      </Panel>
    </div>
  );
}

/**
 * What the profile of THIS rule set declares: its label and its governed fields.
 *
 * Read from `GET .../rule-sets/{id}/versions` — the authoring plan, whose
 * `fields` are `RuleSetFormField.as_dict()` straight off the profile, declared
 * beside the validator that judges them. This tab used to hold that knowledge
 * itself, in a map of four families to hand-written labels and sentences, and it
 * had already fallen two fields behind the server.
 *
 * A SECOND READ OF ONE ROUTE, deliberately. `RuleSetVersionAuthoring` below asks
 * the same question to draw the form; the alternative was to lift the fetch into
 * `ControlsQualityTabs` and thread one plan through two components that each own
 * their own refusal state. It is a GET with no side effect, and one extra read is
 * a smaller price than a front end that states again what the profile declares.
 *
 * A refusal is `null` and never a guess: the panel then names no family, which is
 * honest, rather than a label the console invented for it.
 */
function useProfileDeclaration(
  projectId: string | undefined,
  ruleSetId: string,
): AuthoringPlan | null {
  const [plan, setPlan] = useState<AuthoringPlan | null>(null);

  useEffect(() => {
    setPlan(null);
    if (!projectId || !ruleSetId) return undefined;
    let live = true;
    void apiGet<AuthoringPlan>(
      `/api/projects/${encodeURIComponent(projectId)}/governance/controls-quality/` +
        `rule-sets/${encodeURIComponent(ruleSetId)}/versions`,
    )
      .then((value) => {
        if (live) setPlan(value);
      })
      .catch(() => {
        if (live) setPlan(null);
      });
    return () => {
      live = false;
    };
  }, [projectId, ruleSetId]);

  return plan;
}

/**
 * The published policy, in the profile's own order and the profile's own words.
 *
 * Declared fields first, in the order the family declares them — a policy reads
 * as a sequence of decisions and alphabetical order is not that sequence — then
 * anything else the published payload carries, so a normalized key the form does
 * not ask is still shown rather than hidden by the very declaration meant to
 * explain it.
 */
function policyRowsFromDeclaration(
  policy: Record<string, unknown>,
  fields: AuthoringField[],
): { source: Record<string, unknown>; labels: Record<string, string> } {
  const source: Record<string, unknown> = {};
  const labels: Record<string, string> = {};
  for (const field of fields) {
    labels[field.key] = field.question;
    if (field.key in policy) source[field.key] = policy[field.key];
  }
  for (const [key, value] of Object.entries(policy)) {
    if (!(key in source)) source[key] = value;
  }
  return { source, labels };
}

/**
 * The Rules tab of a family that has no rules by construction.
 *
 * The generic empty state said "its current version carries none, or nothing is
 * published yet". For these families the first half is permanent and the second
 * is a red herring: the profile declares no rule normalizer, so `publish`
 * refuses a ladder outright. What they govern is the version payload, which the
 * server has always sent and no tab read.
 *
 * NOTHING HERE KNOWS A FAMILY. The name of the policy, the questions it decides
 * and how many there are all come from the profile's declaration. A field added
 * to `money_policy.py` tomorrow labels its own row and moves the count in the
 * sentence with no edit to this file — which is the whole difference between
 * reading a declaration and keeping a copy of one.
 */
export function PolicyOnlyRulesTab({
  detail,
  projectId,
}: {
  detail: GovernanceObject;
  projectId?: string;
}) {
  const declaration = useProfileDeclaration(projectId, detail.object_ref.id);
  const policy = policyOf(detail);
  const summary = summaryOf(detail);
  const fields = declaration?.fields ?? [];
  const profileLabel = declaration?.rule_set.profile_label ?? null;

  if (summary.facets_state === "unavailable") {
    return (
      <Status as="block" tone="warning" title="This evidence could not be read">
        Its owner did not answer. This is not a count of zero.
      </Status>
    );
  }

  const { source, labels } = policyRowsFromDeclaration(policy, fields);

  return (
    <Panel flush>
      <PanelHeader
        title={profileLabel ? `${profileLabel} — governed settings` : "Governed settings"}
        description={
          fields.length > 0
            ? `This family governs by policy, not by an ordered ladder: a version of it decides ${fields.length} ${fields.length === 1 ? "thing" : "things"}, each named below in the words its own profile asks it in. It has no rule list, and an empty one here would be a fact about this screen rather than about the policy.`
            : "This family governs by policy rather than by an ordered rule list."
        }
      />
      <PanelBody>
        {Object.keys(policy).length === 0 ? (
          /* THIS EMPTY STATE USED TO NAME NO GESTURE, which is the one thing
             `CLAUDE.md` forbids an empty list to do — and for these families
             there was genuinely none to name, exactly as the Tax & Fee tab had
             none before 2026-08-17. The gesture is now on this same tab, below:
             `RuleSetVersionAuthoring` composes the first version from the
             questions this family's profile declares. */
          <EmptyState
            title="No version is published"
            description="This rule set exists but nothing has been published into it, so there is no governed setting to read — and every read that needs one is refused rather than answered with a default. This is not a policy of zero. Composing the first version is the form below; it becomes this Project's policy only once you put it in force."
          />
        ) : (
          <EvidenceRows
            label={`${profileLabel ?? "Rule set"} published policy`}
            source={source}
            labels={labels}
          />
        )}
      </PanelBody>
    </Panel>
  );
}
