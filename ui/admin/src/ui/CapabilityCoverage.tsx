/**
 * Capability coverage and impact — the recurring pair (Story 48.1).
 *
 * No mockup covers this surface: Project Settings shipped a `<pre>` of raw JSON,
 * and the Datastream Workbench had no capability projection at all. What is
 * measured here is the ratified contract rather than a composition — the five
 * coverage states, the applicable denominator, and `Not applicable` when that
 * denominator is zero.
 *
 * It lives in `ui/admin/src/ui/` because the same two shapes appear on three
 * surfaces (Settings Capabilities, Settings Changes, Workbench Overview/Mapping/
 * Processing) and the console already carries 170 locally re-implemented
 * components. A `settings-coverage-bar` class would have been the 171st.
 *
 * Both components render server-composed values only. Neither recomputes a count
 * or a percentage: a second calculation in the browser could disagree with the
 * server about what a human approved, which is the failure the whole story is
 * about.
 */

import type { ReactNode } from "react";
import { Badge } from "../components/ui/badge";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "../components/ui/table";
import { ObjectId, TableScroll } from "./Data";
import type { Tone } from "./tone";
import { wireWord } from "./glossary";

/** The five states that partition the denominator, plus the one that stays out. */
export const COVERAGE_STATES = [
  "complete",
  "partial",
  "unavailable",
  "excluded",
  "pending",
] as const;

export type CoverageState = (typeof COVERAGE_STATES)[number] | "not_applicable";

export interface CoverageCounts {
  applicable: number;
  complete: number;
  partial: number;
  unavailable: number;
  excluded: number;
  pending: number;
  not_applicable?: number;
  /** Server-composed. Never rebuilt here. */
  label: string;
  percentage: number | null;
}

const STATE_TONE: Record<CoverageState, Tone> = {
  complete: "success",
  partial: "warning",
  unavailable: "error",
  // An exclusion is a governed decision, not a fault: neutral, and still counted.
  excluded: "neutral",
  pending: "info",
  not_applicable: "neutral",
};

const STATE_LABEL: Record<CoverageState, string> = {
  complete: "Complete",
  partial: "Partial",
  unavailable: "Unavailable",
  excluded: "Excluded",
  pending: "Pending",
  not_applicable: "Not applicable",
};

export function CoverageStateBadge({ state }: { state: CoverageState }) {
  // `Badge` already speaks the console's five-tone scale; there is no second
  // vocabulary to translate into here.
  return <Badge tone={STATE_TONE[state] ?? "neutral"}>{STATE_LABEL[state] ?? wireWord(state)}</Badge>;
}

export interface CapabilityCoverageProps {
  coverage: CoverageCounts;
  /** Optional trailing content, e.g. the owner links for this capability. */
  children?: ReactNode;
}

/**
 * The six counts in one line, with the denominator stated rather than implied.
 */
export function CapabilityCoverage({ coverage, children }: CapabilityCoverageProps) {
  const notApplicable = coverage.not_applicable ?? 0;
  return (
    <div className="space-y-2">
      {/* The coverage label is a VALUE, not a title. At `text-h3 font-h3` it
          carried the same weight as the card title above it — "Not applicable"
          shouting as loud as "Country" — and on the Changes tab it was LOUDER
          than the capability name it belonged to. It is subordinate to whatever
          titles it, on all three surfaces. */}
      <p className="m-0 text-ui font-semibold text-text">{coverage.label}</p>
      <p className="m-0 text-caption text-text-secondary">
        {coverage.applicable} applicable Datastream{coverage.applicable === 1 ? "" : "s"}
        {notApplicable > 0 ? ` · ${notApplicable} not applicable` : ""}
      </p>
      <ul className="m-0 flex list-none flex-wrap gap-3 p-0">
        {COVERAGE_STATES.map((state) => (
          <li key={state} className="flex items-center gap-2">
            <CoverageStateBadge state={state} />
            <span className="text-ui text-text">{coverage[state]}</span>
          </li>
        ))}
      </ul>
      {children}
    </div>
  );
}

export interface ImpactMatrixRow {
  capability_key: string;
  datastream_id: string;
  applicability: string;
  coverage_state: CoverageState;
  reason?: string;
  changes_grain?: boolean;
  backfill_required?: boolean;
  downstream_consumers?: number;
  blocker_count?: number;
  exception_count?: number;
}

export const CAPABILITY_LABELS: Record<string, string> = {
  country: "Country",
  currency_fx: "Currency & FX",
  // The casing is the ratified one, not a taste: `project-settings.md`,
  // `governance.md` and `page-structure.md` write `Tax & Fees` and
  // `Reporting Timezone`. This map is the single source for all six labels, so
  // a lowercase variant here renames the capability on every screen at once.
  reporting_timezone: "Reporting Timezone",
  tax_fees: "Tax & Fees",
  competitors: "Competitors",
  // The sixth, story 61.5. Without this entry `capabilityLabel` falls back to
  // `?? key` and prints `placement_mapping` at a person — a machine key on a
  // reviewed impact matrix.
  placement_mapping: "Placement Mapping",
  // The seventh, story 70.3. Same rule as the sixth: without the entry a person
  // reads `analytics_alignment` on the impact matrix.
  analytics_alignment: "Analytics Alignment",
};

export function capabilityLabel(key: string): string {
  return CAPABILITY_LABELS[key] ?? key;
}

/**
 * One row per (capability, Datastream): what a reviewer must read before
 * confirming, in English, replacing the raw JSON dump this surface used to show.
 */
export function CapabilityImpactMatrix({ rows }: { rows: ImpactMatrixRow[] }) {
  if (rows.length === 0) {
    return (
      <p className="m-0 text-ui text-text-secondary">
        No Datastream proposal was compiled for this change.
      </p>
    );
  }
  return (
    <TableScroll label="Per-Datastream capability impact">
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>Capability</TableHead>
            <TableHead>Datastream</TableHead>
            <TableHead>Coverage</TableHead>
            <TableHead>Reason</TableHead>
            <TableHead>Grain</TableHead>
            <TableHead>Backfill</TableHead>
            <TableHead>Downstream</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {rows.map((row) => (
            <TableRow key={`${row.capability_key}:${row.datastream_id}`}>
              <TableCell className="font-medium">{capabilityLabel(row.capability_key)}</TableCell>
              <TableCell><ObjectId value={row.datastream_id} title="Datastream" /></TableCell>
              <TableCell>
                <CoverageStateBadge state={row.coverage_state} />
              </TableCell>
              <TableCell>{row.reason ?? "—"}</TableCell>
              <TableCell>{row.changes_grain ? "Changes" : "Unchanged"}</TableCell>
              <TableCell>{row.backfill_required ? "Required" : "Not required"}</TableCell>
              <TableCell>{row.downstream_consumers ?? 0}</TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </TableScroll>
  );
}
