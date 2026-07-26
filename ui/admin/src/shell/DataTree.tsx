/**
 * DataTree — the expandable Data navigation in the sidebar: Data overview /
 * Datastreams (grouped by source family, with search) / Sources / Imports /
 * Modules. Native <details> drive expand/collapse; a search box filters the
 * streams.
 *
 * ── Data ─────────────────────────────────────────────────────────────────────
 * Every stream, every count is READ FROM THE API. Nothing here is a literal.
 *   - GET /api/datastreams?project_id=…      → the streams + the Datastreams count
 *   - GET /api/connections?project_id=…      → the Sources count
 *   - GET /api/modules/available?project_id= → the "N active" Modules count
 *
 * History (audit 2026-07-25): this component used to render seven invented
 * datastreams ("Campaign performance", "Search performance", …) with the REAL
 * connector logos, one of them carrying a rose "Needs attention" light — a fully
 * fictional alert on a stream that did not exist, clicking through to an
 * objectId the backend had never heard of. The counts (7 / 4 / 2 / 1) were hard
 * coded next to them. There was no fetch at all, so there was no failure mode
 * either: the fiction could not correct itself. It is now three states and no
 * fallback — loading, an explicit error with a retry, or the real (possibly
 * empty) fleet.
 *
 * "Imports" carries NO count: there is no endpoint that answers how many imports
 * a project holds, and an invented number is exactly the defect above.
 */
import { useCallback, useEffect, useMemo, useState, type KeyboardEvent } from "react";
import { useRoute } from "./router";
import ConnectorLogo from "./ConnectorLogo";
import { apiGet } from "../lib/apiFetch";
import "./data-tree.css";

/** The subset of GET /api/datastreams this tree reads. Other fields permitted. */
interface DatastreamSummary {
  id: string;
  name: string;
  module_name?: string | null;
  source_kind?: string | null;
  published_state?: string | null;
  last_pull_state?: string | null;
  [k: string]: unknown;
}

interface ConnectionsResponse {
  connections?: unknown[];
}

interface AvailableModule {
  module_name: string;
  enabled?: boolean;
  [k: string]: unknown;
}

interface Stream {
  id: string;
  name: string;
  provider: string;
  /** True when the published/pull state of the stream is not healthy. */
  attention: boolean;
}

interface Group {
  key: string;
  label: string;
  streams: Stream[];
}

type LoadState = "loading" | "error" | "ready";

// ---------------------------------------------------------------------------
// Derivations from the API rows
// ---------------------------------------------------------------------------

/** Connector slug for the logo registry, from the connector module (never the
 *  ingestion mechanism, which `source_kind` carries). */
function providerOf(ds: DatastreamSummary): string {
  return (ds.module_name || ds.source_kind || "").toLowerCase();
}

/** Source family used as the tree grouping. */
function familyOf(provider: string): { key: string; label: string } {
  if (provider.includes("meta") || provider.includes("facebook") || provider.includes("ads")) {
    return { key: "paid-media", label: "Paid media" };
  }
  if (provider.includes("analytics") || provider.includes("ga4")) {
    return { key: "analytics", label: "Analytics" };
  }
  if (provider.includes("sheet") || provider.includes("csv") || provider.includes("feed")) {
    return { key: "files-plans", label: "Files & plans" };
  }
  return { key: "other", label: "Other" };
}

const FAMILY_ORDER = ["paid-media", "analytics", "files-plans", "other"];

/** A stream needs attention when its published (else last pull) state is not healthy. */
function needsAttention(ds: DatastreamSummary): boolean {
  const s = (ds.published_state || ds.last_pull_state || "").toLowerCase();
  return ["failed", "invalid", "error", "blocked", "degraded", "partial"].includes(s);
}

function toGroups(rows: DatastreamSummary[]): Group[] {
  const byKey = new Map<string, Group>();
  for (const ds of rows) {
    const provider = providerOf(ds);
    const fam = familyOf(provider);
    const group = byKey.get(fam.key) ?? { key: fam.key, label: fam.label, streams: [] };
    group.streams.push({
      id: ds.id,
      name: ds.name,
      provider,
      attention: needsAttention(ds),
    });
    byKey.set(fam.key, group);
  }
  return [...byKey.values()].sort(
    (a, b) => FAMILY_ORDER.indexOf(a.key) - FAMILY_ORDER.indexOf(b.key),
  );
}

// ---------------------------------------------------------------------------
// Icons
// ---------------------------------------------------------------------------

function DbIcon() {
  return (
    <svg viewBox="0 0 24 24">
      <ellipse cx="12" cy="5" rx="7" ry="3" />
      <path d="M5 5v6c0 2 3 3 7 3s7-1 7-3V5M5 11v6c0 2 3 3 7 3s7-1 7-3v-6" />
    </svg>
  );
}
function BarsIcon() {
  return (
    <svg viewBox="0 0 24 24">
      <path d="M4 18V9m5 9V5m5 13v-7m5 7V7" />
    </svg>
  );
}
function DocIcon() {
  return (
    <svg viewBox="0 0 24 24">
      <path d="M6 3h9l3 3v15H6zM15 3v4h4M9 12h6M9 16h6" />
    </svg>
  );
}
const GROUP_ICON: Record<string, React.ReactNode> = {
  "paid-media": <BarsIcon />,
  analytics: <BarsIcon />,
  "files-plans": <DocIcon />,
  other: <DbIcon />,
};

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export default function DataTree() {
  const { route, navigate } = useRoute();
  const projectId = route.projectId;
  const [query, setQuery] = useState("");

  const [state, setState] = useState<LoadState>("loading");
  const [error, setError] = useState<string>("");
  const [streams, setStreams] = useState<DatastreamSummary[]>([]);
  /** null = not known (request failed or still in flight) — never a stand-in number. */
  const [sourceCount, setSourceCount] = useState<number | null>(null);
  const [activeModules, setActiveModules] = useState<number | null>(null);
  const [attempt, setAttempt] = useState(0);

  const retry = useCallback(() => setAttempt((n) => n + 1), []);

  useEffect(() => {
    let alive = true;
    setState("loading");
    setError("");
    setSourceCount(null);
    setActiveModules(null);

    if (!projectId) {
      setState("error");
      setError("No project in scope.");
      return;
    }
    const scope = `project_id=${encodeURIComponent(projectId)}`;

    (async () => {
      try {
        const rows = await apiGet<DatastreamSummary[]>(`/api/datastreams?${scope}`);
        if (!alive) return;
        setStreams(Array.isArray(rows) ? rows : []);
        setState("ready");
      } catch (err) {
        if (!alive) return;
        setStreams([]);
        setError(err instanceof Error ? err.message : "Request failed");
        setState("error");
      }
    })();

    // The two side counts are independent: a failure leaves the badge ABSENT
    // rather than substituting a number.
    (async () => {
      try {
        const body = await apiGet<ConnectionsResponse>(`/api/connections?${scope}`);
        if (alive) setSourceCount(body.connections?.length ?? 0);
      } catch {
        if (alive) setSourceCount(null);
      }
    })();
    (async () => {
      try {
        const mods = await apiGet<AvailableModule[]>(`/api/modules/available?${scope}`);
        if (alive) {
          setActiveModules(Array.isArray(mods) ? mods.filter((m) => m.enabled).length : 0);
        }
      } catch {
        if (alive) setActiveModules(null);
      }
    })();

    return () => {
      alive = false;
    };
  }, [projectId, attempt]);

  const onKey = (e: KeyboardEvent, fn: () => void) => {
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      fn();
    }
  };

  const tool = (label: string, section: string, count?: string, modules?: boolean) => {
    const on = route.section === section;
    return (
      <div
        className={`data-tool${on ? " active" : ""}${modules ? " modules" : ""}`}
        role="button"
        tabIndex={0}
        aria-current={on ? "page" : undefined}
        data-testid={`sec-data-${section}`}
        onClick={() => navigate({ workspace: "data", section })}
        onKeyDown={(e) => onKey(e, () => navigate({ workspace: "data", section }))}
      >
        {label}
        {count ? <b>{count}</b> : null}
      </div>
    );
  };

  const q = query.trim().toLowerCase();
  const groups = useMemo(() => toGroups(streams), [streams]);
  const filtered = useMemo(
    () =>
      groups.map((g) => ({
        ...g,
        matches: g.streams.filter((s) => !q || s.name.toLowerCase().includes(q)),
      })),
    [groups, q],
  );
  const matchCount = filtered.reduce((n, g) => n + g.matches.length, 0);

  const openDatastream = (id: string) =>
    navigate({
      workspace: "data",
      section: "datastreams",
      objectType: "datastream",
      objectId: id,
      tab: "overview",
    });

  const renderStreams = () => {
    if (state === "loading") {
      return (
        <p className="tree-state" data-testid="tree-loading">
          Loading datastreams…
        </p>
      );
    }
    if (state === "error") {
      return (
        <div className="tree-state error" data-testid="tree-error">
          <span>Couldn&apos;t load datastreams. {error}</span>
          <button type="button" className="tree-retry" onClick={retry}>
            Retry
          </button>
        </div>
      );
    }
    if (streams.length === 0) {
      return (
        <div className="tree-state" data-testid="tree-empty">
          <span>No datastream yet.</span>
          <button
            type="button"
            className="tree-retry"
            onClick={() => navigate({ workspace: "data", section: "add" })}
          >
            Add a Datastream
          </button>
        </div>
      );
    }
    if (matchCount === 0) {
      return (
        <p className="tree-state" data-testid="tree-no-match">
          No datastream matches “{query.trim()}”.
        </p>
      );
    }
    return filtered.map((g) => {
      if (g.matches.length === 0) return null;
      return (
        <details className="stream-group" key={g.key} open>
          <summary className="stream-group-row">
            <span className="group-icon" aria-hidden="true">
              {GROUP_ICON[g.key] ?? <DbIcon />}
            </span>
            <span>{g.label}</span>
            <b>{g.matches.length}</b>
            <span className="tree-chevron" aria-hidden="true">
              ›
            </span>
          </summary>
          <div className="stream-group-items">
            {g.matches.map((s) => {
              const on = route.objectId === s.id;
              return (
                <div
                  key={s.id}
                  className={`stream-link${on ? " active" : ""}`}
                  role="button"
                  tabIndex={0}
                  data-testid={`stream-${s.id}`}
                  onClick={() => openDatastream(s.id)}
                  onKeyDown={(e) => onKey(e, () => openDatastream(s.id))}
                >
                  <ConnectorLogo provider={s.provider} />
                  <span>{s.name}</span>
                  {s.attention ? <span className="attention-light" title="Needs attention" /> : null}
                </div>
              );
            })}
          </div>
        </details>
      );
    });
  };

  return (
    <div className="data-tree">
      {tool("Data overview", "data-overview")}

      <details className="tree-section datastream-tree" open>
        <summary className="tree-section-row">
          <span className="tree-section-icon" aria-hidden="true">
            <DbIcon />
          </span>
          <span>Datastreams</span>
          {state === "ready" ? <b data-testid="count-datastreams">{streams.length}</b> : null}
          <span className="tree-chevron" aria-hidden="true">
            ›
          </span>
        </summary>
        <div className="tree-section-panel">
          {state === "ready" && streams.length > 0 ? (
            <label className="tree-search">
              <svg viewBox="0 0 24 24" aria-hidden="true">
                <circle cx="11" cy="11" r="6" />
                <path d="m16 16 4 4" />
              </svg>
              <input
                type="search"
                placeholder="Find Datastreams"
                aria-label="Find Datastreams"
                autoComplete="off"
                value={query}
                onChange={(e) => setQuery(e.target.value)}
              />
            </label>
          ) : null}

          {renderStreams()}
        </div>
      </details>

      {tool("Sources", "sources", sourceCount === null ? undefined : String(sourceCount))}
      {/* No count: no endpoint answers how many imports a project holds. */}
      {tool("Imports", "imports")}
      {tool(
        "Modules",
        "modules",
        activeModules === null ? undefined : `${activeModules} active`,
        true,
      )}
    </div>
  );
}
