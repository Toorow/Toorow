/**
 * The shared capability impact review app (Story 48.1, AC10).
 *
 * One app serves all five Project capabilities. It renders capability intent,
 * exact per-Datastream coverage, proposal impact, blockers, exceptions and owner
 * links from the SAME bounded payload the Console impact matrix renders.
 *
 * Three properties this file keeps deliberately:
 *
 * * **It computes no coverage.** Every count and every label arrives composed.
 *   Recomputing a percentage here would create a second authority that could
 *   disagree with the server about what a human just approved.
 * * **It mutates nothing and authorizes nothing.** There is no confirm control.
 *   When trusted confirmation is unavailable — which is the normal case for a
 *   non-interactive host — it says so and points at the Console instead.
 * * **It builds no URL.** Owner links are semantic references; the Console
 *   resolves them through the canonical navigation registry. A hard-coded path
 *   here would break the first time Epic 49 renames a Governance screen.
 *
 * Styling is inline rather than imported from the admin component library: this
 * bundle must stay one self-contained file, and pulling the console's component
 * tree into a widget would defeat that. Every colour is read from the shared
 * token theme that `WidgetShell` provides — none is restated as a hex literal,
 * because a second palette is how six ways of saying amber get into a codebase.
 */

import { useMemo } from "react";

import type { CapabilityImpactPayload, CoverageState, MatrixRow } from "./types";
import { useTheme } from "@toorow/shell";
import type { WidgetTheme } from "@toorow/shell";

/** Which shared semantic tone each coverage state carries, and its English label. */
const STATE_TONE: Record<CoverageState, { tone: keyof WidgetTheme["palette"] | "neutral"; label: string }> = {
  complete: { tone: "success", label: "Complete" },
  partial: { tone: "warning", label: "Partial" },
  unavailable: { tone: "error", label: "Unavailable" },
  // An exclusion is a governed decision, not a fault: neutral, and still counted.
  excluded: { tone: "neutral", label: "Excluded" },
  pending: { tone: "info", label: "Pending" },
  not_applicable: { tone: "neutral", label: "Not applicable" },
};

function toneColours(theme: WidgetTheme, state: CoverageState) {
  const entry = STATE_TONE[state] ?? STATE_TONE.not_applicable;
  if (entry.tone === "neutral") {
    return {
      fg: theme.palette.text.secondary,
      bg: theme.palette.action.hover,
      label: entry.label,
    };
  }
  const swatch = theme.palette[entry.tone] as {
    main: string;
    light?: string;
    contrastText?: string;
  };
  return {
    fg: swatch.main,
    bg: swatch.light ?? theme.palette.action.hover,
    label: entry.label,
  };
}

const CAPABILITY_LABEL: Record<string, string> = {
  country: "Country",
  currency_fx: "Currency & FX",
  reporting_timezone: "Reporting Timezone",
  tax_fees: "Tax & Fees",
  competitors: "Competitors",
};

function capabilityLabel(key: string): string {
  return CAPABILITY_LABEL[key] ?? key;
}

function StateBadge({ state }: { state: CoverageState }) {
  const tone = toneColours(useTheme(), state);
  return (
    <span
      style={{
        background: tone.bg,
        color: tone.fg,
        borderRadius: 4,
        padding: "2px 8px",
        fontSize: 12,
        fontWeight: 600,
        whiteSpace: "nowrap",
      }}
    >
      {tone.label}
    </span>
  );
}

/** The six counts, read straight from the server aggregate. */
function CoverageSummary({ payload }: { payload: CapabilityImpactPayload }) {
  const theme = useTheme();
  const coverage = payload.coverage;
  if (!coverage) return null;
  const states: CoverageState[] = [
    "complete",
    "partial",
    "unavailable",
    "excluded",
    "pending",
  ];
  return (
    <section aria-labelledby="coverage-heading" style={{ marginBottom: 20 }}>
      <h2 id="coverage-heading" style={{ fontSize: 14, margin: "0 0 8px" }}>
        Coverage
      </h2>
      <p style={{ margin: "0 0 8px", fontSize: 20, fontWeight: 700 }}>{coverage.label}</p>
      <p style={{ margin: "0 0 10px", fontSize: 13, color: theme.palette.text.secondary }}>
        {coverage.applicable} applicable Datastream
        {coverage.applicable === 1 ? "" : "s"}
        {typeof coverage.not_applicable === "number"
          ? ` · ${coverage.not_applicable} not applicable`
          : ""}
      </p>
      <dl
        style={{
          display: "flex",
          flexWrap: "wrap",
          gap: 12,
          margin: 0,
          fontSize: 13,
        }}
      >
        {states.map((state) => (
          <div key={state} style={{ display: "flex", alignItems: "center", gap: 6 }}>
            <dt style={{ margin: 0 }}>
              <StateBadge state={state} />
            </dt>
            <dd style={{ margin: 0, fontWeight: 600 }}>{coverage[state]}</dd>
          </div>
        ))}
      </dl>
    </section>
  );
}

function ImpactMatrix({ rows, withheld }: { rows: MatrixRow[]; withheld: number }) {
  const theme = useTheme();
  if (rows.length === 0) {
    return (
      <p style={{ fontSize: 13, color: theme.palette.text.secondary }}>
        No Datastream proposal was compiled for this change.
      </p>
    );
  }
  return (
    <section aria-labelledby="matrix-heading" style={{ marginBottom: 20 }}>
      <h2 id="matrix-heading" style={{ fontSize: 14, margin: "0 0 8px" }}>
        Per-Datastream impact
      </h2>
      {/* A wide table scrolls inside its own named region rather than pushing the
          page sideways, and stays reachable from the keyboard. */}
      <div
        role="region"
        aria-label="Impact table, scrollable"
        tabIndex={0}
        style={{ overflowX: "auto", border: `1px solid ${theme.palette.divider}`, borderRadius: 6 }}
      >
        <table style={{ borderCollapse: "collapse", width: "100%", fontSize: 13 }}>
          <caption style={{ captionSide: "bottom", padding: 8, fontSize: 12, color: theme.palette.text.secondary }}>
            One row per capability and Datastream, exactly as compiled.
          </caption>
          <thead>
            <tr style={{ background: theme.palette.action.hover, textAlign: "left" }}>
              <th scope="col" style={cell(theme)}>Capability</th>
              <th scope="col" style={cell(theme)}>Datastream</th>
              <th scope="col" style={cell(theme)}>Coverage</th>
              <th scope="col" style={cell(theme)}>Reason</th>
              <th scope="col" style={cell(theme)}>Grain</th>
              <th scope="col" style={cell(theme)}>Backfill</th>
              <th scope="col" style={cell(theme)}>Downstream</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={`${row.capability_key}:${row.datastream_id}`}>
                <th scope="row" style={{ ...cell(theme), fontWeight: 600 }}>
                  {capabilityLabel(row.capability_key)}
                </th>
                <td style={cell(theme)}>{row.datastream_id}</td>
                <td style={cell(theme)}>
                  <StateBadge state={row.coverage_state} />
                </td>
                <td style={cell(theme)}>{row.reason ?? "—"}</td>
                <td style={cell(theme)}>{row.changes_grain ? "Changes" : "Unchanged"}</td>
                <td style={cell(theme)}>{row.backfill_required ? "Required" : "Not required"}</td>
                <td style={cell(theme)}>{row.downstream_consumers ?? 0}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {withheld > 0 ? (
        <p style={{ fontSize: 12, color: theme.palette.error.main, margin: "8px 0 0" }}>
          {withheld} further row{withheld === 1 ? "" : "s"} withheld by the payload bound.
          Open Project settings in the Console to read the complete matrix.
        </p>
      ) : null}
    </section>
  );
}

function cell(theme: WidgetTheme) {
  return {
    padding: "8px 10px",
    borderBottom: `1px solid ${theme.palette.divider}`,
    verticalAlign: "top",
  } as const;
}

function Blockers({ payload }: { payload: CapabilityImpactPayload }) {
  const blockers = payload.blockers ?? [];
  if (blockers.length === 0) return null;
  return (
    <section aria-labelledby="blockers-heading" style={{ marginBottom: 20 }}>
      <h2 id="blockers-heading" style={{ fontSize: 14, margin: "0 0 8px" }}>
        Blocking issues
      </h2>
      <ul style={{ margin: 0, paddingLeft: 18, fontSize: 13 }}>
        {blockers.map((blocker, index) => (
          <li key={`${blocker.code}:${index}`} style={{ marginBottom: 4 }}>
            <strong>{blocker.code}</strong> — {blocker.message}
            {blocker.datastream_id ? ` (${blocker.datastream_id})` : ""}
          </li>
        ))}
      </ul>
    </section>
  );
}

function Exceptions({ payload }: { payload: CapabilityImpactPayload }) {
  const exceptions = payload.exceptions ?? [];
  if (exceptions.length === 0) return null;
  return (
    <section aria-labelledby="exceptions-heading" style={{ marginBottom: 20 }}>
      <h2 id="exceptions-heading" style={{ fontSize: 14, margin: "0 0 8px" }}>
        Exceptions
      </h2>
      <ul style={{ margin: 0, paddingLeft: 18, fontSize: 13 }}>
        {exceptions.map((exception, index) => (
          <li key={`${exception.reason_code ?? index}`} style={{ marginBottom: 4 }}>
            <strong>{exception.severity ?? "exception"}</strong> — {exception.reason}
            {exception.owner_kind ? ` · owned by ${exception.owner_kind}` : ""}
          </li>
        ))}
      </ul>
    </section>
  );
}

function OwnerLinks({ payload }: { payload: CapabilityImpactPayload }) {
  const references = payload.referenced_versions ?? [];
  if (references.length === 0) return null;
  return (
    <section aria-labelledby="owners-heading" style={{ marginBottom: 20 }}>
      <h2 id="owners-heading" style={{ fontSize: 14, margin: "0 0 8px" }}>
        Owners this version references
      </h2>
      <ul style={{ margin: 0, paddingLeft: 18, fontSize: 13 }}>
        {references.map((reference) => (
          <li
            key={`${reference.owner_kind}:${reference.object_type}:${reference.object_id}:${reference.version_id}`}
            style={{ marginBottom: 4 }}
          >
            {reference.owner_kind === "governance" ? "Governance" : "Data"} ·{" "}
            {reference.object_type} <code>{reference.object_id}</code>
            {reference.capability_key ? ` · ${capabilityLabel(reference.capability_key)}` : ""}
          </li>
        ))}
      </ul>
    </section>
  );
}

/**
 * The Console fallback. Shown whenever trusted confirmation is unavailable, which
 * is the honest state for a host with no verified human-presence gesture: this app
 * cannot authorize, so it names where the operator can.
 */
function ConsoleFallback({ payload }: { payload: CapabilityImpactPayload }) {
  const theme = useTheme();
  const canConfirm = payload.confirmation_available === true;
  return (
    <section
      aria-labelledby="next-heading"
      style={{
        border: `1px solid ${theme.palette.divider}`,
        borderRadius: 6,
        padding: 12,
        background: theme.palette.action.hover,
      }}
    >
      <h2 id="next-heading" style={{ fontSize: 14, margin: "0 0 6px" }}>
        {canConfirm ? "Ready for confirmation" : "Confirm in the Console"}
      </h2>
      <p style={{ margin: 0, fontSize: 13, color: theme.palette.text.secondary }}>
        {canConfirm
          ? "This review is frozen and nothing is active yet. Confirmation is a separate, single-use step and is never performed by this view."
          : "This host cannot provide a trusted confirmation. Open Project settings → Changes in the Console to review and confirm."}
      </p>
      {payload.review_reference ? (
        <p style={{ margin: "8px 0 0", fontSize: 12, color: theme.palette.text.disabled }}>
          Review reference <code>{payload.review_reference.slice(0, 12)}…</code> — an
          identifier for this frozen payload, not an approval.
        </p>
      ) : null}
    </section>
  );
}

export interface AppProps {
  payload: CapabilityImpactPayload | null;
  /** True when the app is running inside a compatible MCP Apps host. */
  hostConnected?: boolean;
}

export default function App({ payload, hostConnected = true }: AppProps) {
  const theme = useTheme();
  const rows = useMemo(() => payload?.matrix ?? [], [payload]);

  if (!payload) {
    return (
      <main style={shell(theme)}>
        <p role="status" style={{ fontSize: 13, color: theme.palette.text.secondary }}>
          {hostConnected
            ? "Waiting for the capability impact payload…"
            : "No compatible MCP host detected. Open Project settings in the Console to review this change."}
        </p>
      </main>
    );
  }

  const title = payload.capability_key
    ? `${capabilityLabel(payload.capability_key)} coverage`
    : "Capability change impact";

  return (
    <main style={shell(theme)}>
      <header style={{ marginBottom: 16 }}>
        <h1 style={{ fontSize: 18, margin: "0 0 4px" }}>{title}</h1>
        <p style={{ margin: 0, fontSize: 13, color: theme.palette.text.secondary }}>
          {payload.state ? `Change Set is ${payload.state}. ` : ""}
          This view reads server-composed truth and changes nothing.
        </p>
      </header>
      <CoverageSummary payload={payload} />
      <ImpactMatrix rows={rows} withheld={payload.matrix_rows_withheld ?? 0} />
      <Blockers payload={payload} />
      <Exceptions payload={payload} />
      <OwnerLinks payload={payload} />
      <ConsoleFallback payload={payload} />
    </main>
  );
}

function shell(theme: WidgetTheme) {
  return {
    padding: 16,
    maxWidth: 960,
    margin: "0 auto",
    // The type family comes from the shared theme too: @toorow/shell inlines the
    // faces, so naming them again here could drift from what is actually loaded.
    fontFamily: theme.typography.fontFamily,
    color: theme.palette.text.primary,
  } as const;
}
