/**
 * Provenance — the lineage read-out for the Governance workspace.
 *
 * PURPOSE
 * Provenance answers one question: "where did this published number come from?".
 * It is the verification apparatus itself, which is exactly why it must never
 * show anything it did not read.
 *
 * ── Data ─────────────────────────────────────────────────────────────────────
 * There is no single /api/provenance endpoint. The real publication lineage is
 * the per-datastream publication log, composed here from two reads:
 *   1. GET /api/datastreams?project_id=…                    → the fleet
 *   2. GET /api/datastreams/{id}/publications?project_id=…  → {publications: [...]}
 * Each publication row carries `execution_id`, `plan_version_id`,
 * `mapping_version_id`, `content_hash`, `row_count`, `prior_execution_id`,
 * `published_at` and `published_by` — every column below is one of those fields.
 *
 * What is NOT shown, and why: the pull ids that fed a figure and the FX as-of-day
 * rate live in the published figure's AD-1 envelope (`meta.provenance`), which no
 * HTTP read exposes today. They are therefore ABSENT, not approximated.
 *
 * History (audit 2026-07-25): this page made no network call at all. It invented
 * pull ids ("pull_9f2ac1"), mapping versions ("mapping_v17"), exchange rates with
 * an as-of date ("1 USD = 0.9184 EUR", "2026-07-22") and client names ("Acme",
 * "Northwind") — a fabricated audit trail on the one surface whose entire job is
 * to prove that figures are traceable. Removed in full.
 *
 * Styling: application.css (global, via the shell) + provenance.css. Colors come
 * exclusively from the application.css CSS variables (dark-safe).
 */
import { useCallback, useEffect, useMemo, useState, type ReactElement } from "react";
import ConnectorLogo from "../ConnectorLogo";
import { apiGet } from "../../lib/apiFetch";
import "../application.css";
import "./provenance.css";

// ---------------------------------------------------------------------------
// API shapes (only the fields this page reads are declared)
// ---------------------------------------------------------------------------

interface DatastreamSummary {
  id: string;
  name: string;
  module_name?: string | null;
  source_kind?: string | null;
  [k: string]: unknown;
}

interface PublicationRow {
  id?: string;
  execution_id?: string | null;
  mapping_version_id?: string | null;
  plan_version_id?: string | null;
  content_hash?: string | null;
  row_count?: number | null;
  published_at?: string | null;
  published_by?: string | null;
  [k: string]: unknown;
}

interface PublicationsResponse {
  publications?: PublicationRow[];
}

/** A publication joined to the datastream it belongs to. */
interface LineageRow {
  key: string;
  datastreamId: string;
  datastreamName: string;
  provider: string;
  executionId: string | null;
  mappingVersionId: string | null;
  contentHash: string | null;
  rowCount: number | null;
  publishedAt: string | null;
  publishedBy: string | null;
}

type LoadState = "loading" | "error" | "ready";

/** Newest publications first; the page shows the most recent slice. */
const MAX_ROWS = 50;

// ---------------------------------------------------------------------------
// Formatting — "—" whenever the field is absent. Never a substitute value.
// ---------------------------------------------------------------------------

function fmtDate(iso: string | null): string {
  if (!iso) return "—";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "—";
  return d.toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric" });
}

/** Long opaque ids are truncated for the cell; the full value stays in `title`. */
function shortId(value: string | null): string {
  if (!value) return "—";
  return value.length > 18 ? `${value.slice(0, 8)}…${value.slice(-6)}` : value;
}

function providerOf(ds: DatastreamSummary): string {
  return (ds.module_name || ds.source_kind || "").toLowerCase();
}

// ---------------------------------------------------------------------------
// What the publication log records. Descriptive copy about the CONTRACT, each
// line naming the field the column above it is read from — no data claimed.
// ---------------------------------------------------------------------------

interface Guarantee {
  title: string;
  copy: string;
  field: string;
  icon: ReactElement;
}

const GUARANTEES: Guarantee[] = [
  {
    title: "Published execution",
    copy: "Each publication pins the execution that produced it, so the run behind a live figure is always recoverable.",
    field: "execution_id",
    icon: (
      <svg viewBox="0 0 24 24">
        <path d="M12 7v5l3 2M20 12a8 8 0 11-16 0 8 8 0 0116 0z" />
      </svg>
    ),
  },
  {
    title: "Mapping version",
    copy: "The mapping version that shaped the rows is pinned, so you can see which canonical rules were in force at publication.",
    field: "mapping_version_id",
    icon: (
      <svg viewBox="0 0 24 24">
        <path d="M4 6h16M4 12h12M4 18h8" />
      </svg>
    ),
  },
  {
    title: "Content hash",
    copy: "A hash of the published content makes the publication verifiable: the same inputs reproduce the same hash.",
    field: "content_hash",
    icon: (
      <svg viewBox="0 0 24 24">
        <path d="M7 7h10v10H7zM7 12H3M21 12h-4M12 7V3M12 21v-4" />
      </svg>
    ),
  },
  {
    title: "Publisher and moment",
    copy: "Who published, and when. Together with the prior execution pointer this closes the chain between two published states.",
    field: "published_by · published_at",
    icon: (
      <svg viewBox="0 0 24 24">
        <path d="M12 12a4 4 0 100-8 4 4 0 000 8zM5 20a7 7 0 0114 0" />
      </svg>
    ),
  },
];

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

interface ProvenanceProps {
  projectId?: string;
}

export default function Provenance({ projectId }: ProvenanceProps) {
  const [state, setState] = useState<LoadState>("loading");
  const [error, setError] = useState("");
  const [rows, setRows] = useState<LineageRow[]>([]);
  const [unreadable, setUnreadable] = useState(0);
  const [streamNames, setStreamNames] = useState<{ id: string; name: string }[]>([]);
  const [filter, setFilter] = useState("all");
  const [attempt, setAttempt] = useState(0);

  const retry = useCallback(() => setAttempt((n) => n + 1), []);

  useEffect(() => {
    let alive = true;
    setState("loading");
    setError("");
    setUnreadable(0);

    if (!projectId) {
      setState("error");
      setError("No project in scope.");
      return;
    }
    const scope = `project_id=${encodeURIComponent(projectId)}`;

    (async () => {
      try {
        const fleet = await apiGet<DatastreamSummary[]>(`/api/datastreams?${scope}`);
        const streams = Array.isArray(fleet) ? fleet : [];
        if (!alive) return;
        setStreamNames(streams.map((ds) => ({ id: ds.id, name: ds.name })));

        const settled = await Promise.allSettled(
          streams.map((ds) =>
            apiGet<PublicationsResponse>(
              `/api/datastreams/${encodeURIComponent(ds.id)}/publications?${scope}`,
            ),
          ),
        );
        if (!alive) return;

        const collected: LineageRow[] = [];
        let failures = 0;
        settled.forEach((result, i) => {
          const ds = streams[i];
          if (result.status === "rejected") {
            failures += 1;
            return;
          }
          const pubs = result.value.publications ?? [];
          pubs.forEach((p, j) => {
            collected.push({
              key: `${ds.id}:${p.id ?? p.execution_id ?? j}`,
              datastreamId: ds.id,
              datastreamName: ds.name,
              provider: providerOf(ds),
              executionId: p.execution_id ?? null,
              mappingVersionId: p.mapping_version_id ?? null,
              contentHash: p.content_hash ?? null,
              rowCount: typeof p.row_count === "number" ? p.row_count : null,
              publishedAt: p.published_at ?? null,
              publishedBy: p.published_by ?? null,
            });
          });
        });
        collected.sort((a, b) => {
          const ta = a.publishedAt ? Date.parse(a.publishedAt) : 0;
          const tb = b.publishedAt ? Date.parse(b.publishedAt) : 0;
          return tb - ta;
        });

        setRows(collected.slice(0, MAX_ROWS));
        setUnreadable(failures);
        setState("ready");
      } catch (err) {
        if (!alive) return;
        setRows([]);
        setStreamNames([]);
        setError(err instanceof Error ? err.message : "Request failed");
        setState("error");
      }
    })();

    return () => {
      alive = false;
    };
  }, [projectId, attempt]);

  const visible = useMemo(
    () => (filter === "all" ? rows : rows.filter((r) => r.datastreamId === filter)),
    [rows, filter],
  );

  return (
    <>
      <div className="page-header">
        <div>
          <h1>Provenance</h1>
          <p>
            Trace every published state back to the execution, mapping version and
            content hash that produced it.
          </p>
        </div>
      </div>

      <section className="panel provenance-explainer">
        <div className="provenance-explainer-header">
          <div>
            <h2>What a publication records</h2>
            <p>
              Each publication of a datastream writes an immutable log entry. These
              are the fields it records — and the columns below are read from them.
            </p>
          </div>
        </div>
        <div className="guarantee-rows">
          {GUARANTEES.map((g) => (
            <div className="guarantee-row" key={g.title}>
              <span className="guarantee-icon">{g.icon}</span>
              <div className="guarantee-copy">
                <strong>{g.title}</strong>
                <p>{g.copy}</p>
              </div>
              <code className="mono guarantee-key">{g.field}</code>
            </div>
          ))}
        </div>
        <p className="provenance-gap" data-testid="provenance-gap">
          Pull-level lineage and the as-of FX rate travel in the published figure&apos;s
          envelope (<code className="mono">meta.provenance</code>). No HTTP read exposes
          them yet, so they are not shown here.
        </p>
      </section>

      <section className="panel provenance-panel">
        <div className="provenance-panel-header">
          <div>
            <h2>Recent publications</h2>
            <p>The publication log of every datastream in this project, newest first.</p>
          </div>
          <label className="provenance-filter">
            <span className="sr-only">Filter by datastream</span>
            <select
              className="selector"
              data-testid="provenance-filter"
              value={filter}
              onChange={(e) => setFilter(e.target.value)}
              disabled={state !== "ready" || streamNames.length === 0}
            >
              <option value="all">All datastreams</option>
              {streamNames.map((s) => (
                <option key={s.id} value={s.id}>
                  {s.name}
                </option>
              ))}
            </select>
          </label>
        </div>

        {state === "loading" ? (
          <p className="provenance-state" data-testid="provenance-loading">
            Loading the publication log…
          </p>
        ) : null}

        {state === "error" ? (
          <div className="provenance-state error" data-testid="provenance-error">
            <p>Couldn&apos;t load the publication log. {error}</p>
            <button className="secondary-button" type="button" onClick={retry}>
              Retry
            </button>
          </div>
        ) : null}

        {state === "ready" && rows.length === 0 ? (
          <p className="provenance-state" data-testid="provenance-empty">
            Nothing has been published in this project yet, so there is no lineage to
            show.
          </p>
        ) : null}

        {state === "ready" && rows.length > 0 ? (
          <>
            {unreadable > 0 ? (
              <p className="provenance-state warning" data-testid="provenance-partial">
                {unreadable} datastream{unreadable === 1 ? "" : "s"} could not be read —
                their publications are missing from this list.
              </p>
            ) : null}
            <div
              className="table-scroll"
              tabIndex={0}
              aria-label="Recent publications and their provenance"
            >
              <table className="provenance-table">
                <thead>
                  <tr>
                    <th style={{ width: "24%" }}>Datastream</th>
                    <th style={{ width: "14%" }}>Published</th>
                    <th style={{ width: "14%" }}>Published by</th>
                    <th style={{ width: "16%" }}>Execution</th>
                    <th style={{ width: "16%" }}>Mapping version</th>
                    <th style={{ width: "16%" }}>Content hash</th>
                  </tr>
                </thead>
                <tbody>
                  {visible.map((row) => (
                    <tr key={row.key}>
                      <td>
                        <div className="datastream-cell">
                          <ConnectorLogo provider={row.provider} alt={row.datastreamName} />
                          <div>
                            <strong>{row.datastreamName}</strong>
                            <small>
                              {row.rowCount === null ? (
                                "row count not recorded"
                              ) : (
                                <>
                                  <span className="number">{row.rowCount}</span> rows
                                  published
                                </>
                              )}
                            </small>
                          </div>
                        </div>
                      </td>
                      <td className="number published-cell">{fmtDate(row.publishedAt)}</td>
                      <td>{row.publishedBy ?? "—"}</td>
                      <td>
                        <code className="mono" title={row.executionId ?? undefined}>
                          {shortId(row.executionId)}
                        </code>
                      </td>
                      <td>
                        <code className="mono mapping-tag" title={row.mappingVersionId ?? undefined}>
                          {shortId(row.mappingVersionId)}
                        </code>
                      </td>
                      <td>
                        <code className="mono" title={row.contentHash ?? undefined}>
                          {shortId(row.contentHash)}
                        </code>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <div className="provenance-footer">
              <span>
                Showing <span className="number">{visible.length}</span> publication
                {visible.length === 1 ? "" : "s"}
              </span>
              <span className="footer-note mono">datastream_publication_log</span>
            </div>
          </>
        ) : null}
      </section>
    </>
  );
}
