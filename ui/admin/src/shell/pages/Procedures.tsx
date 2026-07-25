/**
 * Procedures — v3 Context surface for documented metric calculation methods.
 *
 * Mounted in the Context workspace as a sibling of Knowledge and Events. The
 * application shell already renders the frame, sidebar, topbar, and <main>; this
 * component renders ONLY the page content inside <main>: the page header, an
 * "Add procedure" action, and the grid of procedure cards.
 *
 * Purpose: a procedure is the documented CALCULATION method that analysis must
 * follow for a canonical metric — how it is computed and, when several sources
 * report it, how those readings are reconciled. The reconciliation method is
 * defined per metric by the client (there are ~5 methods in the marts, to be
 * made project-configurable). This is business knowledge AI agents read during
 * analysis, so it lives in Context.
 *
 * Styling: application.css (global, via the shell) for shell/layout classes
 * (page-header, header-actions, panel, primary-button, secondary-button) +
 * procedures.css for the page-specific card grid, procedure cards, method pill,
 * and card meta lines. Colors come exclusively from the application.css CSS
 * variables (dark-theme safe); dates use Geist tabular via .procedure-meta.
 *
 * Data: the literal cards below are the validated design and render finished
 * while the fetch is in flight and offline. On mount this reads
 * GET /api/procedures?project_id=<id> and, on success, replaces the literals
 * with the mapped procedures — which is honestly empty until any are defined.
 * The actions remain placeholders, flagged with // TODO(api).
 */
import { useEffect, useState } from "react";
import "../application.css";
import "./procedures.css";

// The reconciliation method a procedure applies. These mirror the ~5 methods
// already present in the marts, surfaced here as the pill label.
type ReconciliationMethod =
  | "Sum then divide"
  | "Priority source"
  | "Weighted blend"
  | "De-duplicated union"
  | "Manual override";

interface Procedure {
  metric: string;
  method: ReconciliationMethod;
  description: string;
  owner: string;
  updated: string;
}

// Mockup literals — the validated Procedures design. Rendered verbatim so the
// page is finished with no backend. Each card spans a distinct reconciliation
// method. TODO(api): replace with procedures returned by the Context API.
const PROCEDURES: Procedure[] = [
  {
    metric: "ROAS",
    method: "Sum then divide",
    description:
      "Aggregate revenue and cost across every reporting source first, then divide the totals. Never average per-source ratios — the blended figure is computed once at the canonical grain.",
    owner: "Winston (Architect)",
    updated: "2 days ago",
  },
  {
    metric: "Conversions",
    method: "Priority source",
    description:
      "Take GA4 as the authoritative source of truth. Platform-reported conversions are kept for context but excluded from the canonical count to avoid post-click double counting.",
    owner: "Mary (Analyst)",
    updated: "3 days ago",
  },
  {
    metric: "Impressions",
    method: "De-duplicated union",
    description:
      "Union impressions across sources, then collapse rows that share the same campaign, creative, and date so a placement reported by two connectors is counted a single time.",
    owner: "Paige (Tech Writer)",
    updated: "5 days ago",
  },
  {
    metric: "Blended CPA",
    method: "Weighted blend",
    description:
      "Combine per-source cost per acquisition weighted by each source's spend share. Sources below the minimum-spend threshold are folded into the dominant source rather than skewing the blend.",
    owner: "Winston (Architect)",
    updated: "1 week ago",
  },
  {
    metric: "Media revenue",
    method: "Manual override",
    description:
      "Use the finance-reconciled monthly figure supplied by the client in place of any connector reading. The override carries an effective date and supersedes automated collection for closed periods.",
    owner: "Mary (Analyst)",
    updated: "1 week ago",
  },
  {
    metric: "Sessions",
    method: "Priority source",
    description:
      "GA4 is the single source for sessions. Other analytics feeds are surfaced as comparison only and do not contribute to the canonical value read during analysis.",
    owner: "Paige (Tech Writer)",
    updated: "2 weeks ago",
  },
];

// Map each method to a stable modifier class so the pill can tint distinctly
// while still drawing every color from the application.css CSS variables.
const METHOD_CLASS: Record<ReconciliationMethod, string> = {
  "Sum then divide": "method-sum",
  "Priority source": "method-priority",
  "Weighted blend": "method-blend",
  "De-duplicated union": "method-dedup",
  "Manual override": "method-override",
};

// ---------------------------------------------------------------------------
// API mapping — GET /api/procedures?project_id=<id> returns rows keyed by the
// raw method enum; humanize it into the ReconciliationMethod pill label.
// ---------------------------------------------------------------------------

/** Shape of one procedure row returned by GET /api/procedures. */
interface ApiProcedure {
  id: string;
  metric: string;
  method: string;
  description: string;
  owner: string;
  updated_at: string;
}

// Raw method enum → the humanized pill label. Unknown values fall back to the
// calculation-heavy default so a card never renders a raw enum token.
const METHOD_LABEL: Record<string, ReconciliationMethod> = {
  sum_then_divide: "Sum then divide",
  priority_source: "Priority source",
  dedup_union: "De-duplicated union",
  weighted_blend: "Weighted blend",
  manual_override: "Manual override",
};

/** Format an ISO updated_at into a short human date, e.g. "12 May 2026". */
function formatUpdated(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleDateString("en-GB", {
    day: "numeric",
    month: "short",
    year: "numeric",
  });
}

function toProcedure(p: ApiProcedure): Procedure {
  return {
    metric: p.metric,
    method: METHOD_LABEL[p.method] ?? "Sum then divide",
    description: p.description,
    owner: p.owner,
    updated: formatUpdated(p.updated_at),
  };
}

export default function Procedures({ projectId = "default" }: { projectId?: string }) {
  // The literal PROCEDURES render finished while the fetch is in flight and
  // offline; a successful fetch replaces them with the real project procedures
  // (which is honestly empty until any are defined for this project).
  const [rows, setRows] = useState<Procedure[]>(PROCEDURES);
  const [loaded, setLoaded] = useState(false);

  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const res = await fetch(
          `/api/procedures?project_id=${encodeURIComponent(projectId)}`,
        );
        if (!res.ok) return; // keep the literal fallback
        const body = (await res.json()) as { procedures?: ApiProcedure[] };
        if (alive) {
          setRows((body.procedures ?? []).map(toProcedure));
          setLoaded(true);
        }
      } catch {
        /* offline — keep the designed literals so the surface stays finished. */
      }
    })();
    return () => {
      alive = false;
    };
  }, [projectId]);

  return (
    <>
      <div className="page-header">
        <div>
          <h1>Procedures</h1>
          <p>
            The calculation and reconciliation methods analysis must apply for this project's
            metrics.
          </p>
        </div>
        <div className="header-actions">
          {/* TODO(api): open the Add procedure flow. */}
          <button className="primary-button" type="button">
            + Add procedure
          </button>
        </div>
      </div>

      {loaded && rows.length === 0 ? (
        <section className="panel procedure-empty">
          <p>
            No procedures defined yet. Document how each canonical metric is
            calculated — and, when several sources report it, how those readings
            are reconciled — so analysis follows one agreed method.
          </p>
        </section>
      ) : (
      <section className="procedure-grid">
        {rows.map((procedure) => (
          <article key={procedure.metric} className="panel procedure-card">
            <div className="procedure-head">
              <h2>{procedure.metric}</h2>
              <span className={`procedure-method ${METHOD_CLASS[procedure.method]}`}>
                {procedure.method}
              </span>
            </div>
            <p className="procedure-desc">{procedure.description}</p>
            <div className="procedure-meta">
              <span>
                Owner: <b>{procedure.owner}</b>
              </span>
              <span>
                Last updated: <time>{procedure.updated}</time>
              </span>
            </div>
            {/* TODO(api): open this procedure in the calculation editor. */}
            <button className="secondary-button" type="button">
              Edit procedure
            </button>
          </article>
        ))}
      </section>
      )}
    </>
  );
}
