/**
 * ReconciliationMethods — Governance surface for per-metric calculation and
 * reconciliation methods.
 *
 * Story 44.1: this panel is the former Context ▸ Procedures surface, moved.
 * It is backed by `app.metric_procedures` (GET /api/procedures), which is
 * NOT the agent-procedure corpus (`app.procedures` / `/api/context/procedures`
 * — see shell/pages/Procedures.tsx). A metric_procedures row documents HOW a
 * canonical metric is computed and, when several sources report it, how those
 * readings are reconciled; it is read-only here (no write endpoint exists for
 * this table) and belongs under Governance next to the semantic model and
 * data quality, not in the governed, versioned knowledge/procedure corpus the
 * agent reads.
 *
 * Mounted as a sibling of Semantic model / Mapping / Data quality. The
 * application shell already renders the frame, sidebar, topbar, and <main>;
 * this component renders ONLY the page content inside <main>.
 *
 * Styling: application.css (global) for shell/layout classes (page-header,
 * panel) + reconciliation-methods.css for the card grid, method pill, and
 * meta lines. Colors come exclusively from application.css CSS variables.
 *
 * Data: GET /api/procedures?project_id=<id> (server/core/admin_api.py,
 * migration 095 `app.metric_procedures`) — unchanged endpoint, read-only page.
 */
import { useEffect, useState } from "react";
import "../application.css";
import "./reconciliation-methods.css";
import { apiFetch } from "../../lib/apiFetch";

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

/** Shape of one row returned by GET /api/procedures. */
interface ApiProcedure {
  id: string;
  metric: string;
  method: string;
  description: string;
  owner: string;
  updated_at: string;
}

const METHOD_CLASS: Record<ReconciliationMethod, string> = {
  "Sum then divide": "method-sum",
  "Priority source": "method-priority",
  "Weighted blend": "method-blend",
  "De-duplicated union": "method-dedup",
  "Manual override": "method-override",
};

const METHOD_LABEL: Record<string, ReconciliationMethod> = {
  sum_then_divide: "Sum then divide",
  priority_source: "Priority source",
  dedup_union: "De-duplicated union",
  weighted_blend: "Weighted blend",
  manual_override: "Manual override",
};

function formatUpdated(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric" });
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

export default function ReconciliationMethods({ projectId = "default" }: { projectId?: string }) {
  const [rows, setRows] = useState<Procedure[]>([]);
  const [loading, setLoading] = useState(true);
  const [loaded, setLoaded] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    setLoading(true);
    setError(null);
    (async () => {
      try {
        const res = await apiFetch(
          `/api/procedures?project_id=${encodeURIComponent(projectId)}`,
        );
        if (!res.ok) {
          if (alive) setError(`Could not load reconciliation methods (HTTP ${res.status}).`);
          return;
        }
        const body = (await res.json()) as { procedures?: ApiProcedure[] };
        if (alive) {
          setRows((body.procedures ?? []).map(toProcedure));
          setLoaded(true);
        }
      } catch (err) {
        if (alive) {
          setError(err instanceof Error ? err.message : "Network error");
        }
      } finally {
        if (alive) setLoading(false);
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
          <h1>Reconciliation methods</h1>
          <p>
            The calculation and cross-source reconciliation method documented for each canonical
            metric in this project. Read-only — defined per project by the client's data model.
          </p>
        </div>
      </div>

      {loading && (
        <p className="recon-status" role="status" data-testid="reconciliation-loading">
          Loading reconciliation methods…
        </p>
      )}

      {error && (
        <div className="panel recon-error" role="alert" data-testid="reconciliation-error">
          <p>{error}</p>
        </div>
      )}

      {!loading && !error && loaded && rows.length === 0 ? (
        <section className="panel recon-empty" data-testid="reconciliation-empty">
          <p>
            No reconciliation methods defined yet for this project's metrics.
          </p>
        </section>
      ) : (
        !loading &&
        !error && (
          <section className="recon-grid">
            {rows.map((procedure) => (
              <article key={procedure.metric} className="panel recon-card">
                <div className="recon-head">
                  <h2>{procedure.metric}</h2>
                  <span className={`recon-method ${METHOD_CLASS[procedure.method]}`}>
                    {procedure.method}
                  </span>
                </div>
                <p className="recon-desc">{procedure.description}</p>
                <div className="recon-meta">
                  <span>
                    Owner: <b>{procedure.owner}</b>
                  </span>
                  <span>
                    Last updated: <time>{procedure.updated}</time>
                  </span>
                </div>
              </article>
            ))}
          </section>
        )
      )}
    </>
  );
}
