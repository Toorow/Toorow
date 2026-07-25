/**
 * DataTree — the expandable Data navigation (faithful to mockups/data-workspace.html
 * + navigation.js): Data overview / Datastreams (type > Datastream, with search and
 * collapse) / Sources / Imports / Modules. Native <details> drive expand/collapse
 * (déploiement/repli); a search box filters the streams. application.css classes;
 * real connector logos via /imports; a rose AttentionLight marks a stream to review.
 *
 * GAP: the tree data is currently the mockup's literal structure. It should come from
 * GET /api/datastreams grouped by data-type (Paid media / Analytics / Files & plans).
 */
import { useMemo, useState, type KeyboardEvent } from "react";
import { useRoute } from "./router";
import ConnectorLogo from "./ConnectorLogo";

interface Stream {
  id: string;
  name: string;
  provider: "meta" | "google-ads" | "google-analytics" | "google-sheets";
  attention?: boolean;
}
interface Group {
  key: string;
  label: string;
  streams: Stream[];
  defaultOpen?: boolean;
}

// TODO(api): derive from GET /api/datastreams grouped by data-type.
const GROUPS: Group[] = [
  {
    key: "paid-media",
    label: "Paid media",
    defaultOpen: true,
    streams: [
      { id: "campaign-performance", name: "Campaign performance", provider: "meta" },
      { id: "search-performance", name: "Search performance", provider: "google-ads", attention: true },
      { id: "reach-frequency", name: "Reach & frequency", provider: "meta" },
    ],
  },
  {
    key: "analytics",
    label: "Analytics",
    streams: [
      { id: "website-acquisition", name: "Website acquisition", provider: "google-analytics" },
      { id: "conversion-paths", name: "Conversion paths", provider: "google-analytics" },
    ],
  },
  {
    key: "files-plans",
    label: "Files & plans",
    streams: [
      { id: "media-plan-2026", name: "Media plan 2026", provider: "google-sheets" },
      { id: "sales-targets", name: "Sales targets", provider: "google-sheets" },
    ],
  },
];

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
};

export default function DataTree() {
  const { route, navigate } = useRoute();
  const [query, setQuery] = useState("");

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
  const filtered = useMemo(
    () =>
      GROUPS.map((g) => ({
        ...g,
        matches: g.streams.filter((s) => !q || s.name.toLowerCase().includes(q)),
      })),
    [q],
  );

  const openDatastream = (id: string) =>
    navigate({ workspace: "data", section: "datastreams", objectType: "datastream", objectId: id, tab: "overview" });

  return (
    <div className="data-tree">
      {tool("Data overview", "data-overview")}

      <details className="tree-section datastream-tree" open>
        <summary className="tree-section-row">
          <span className="tree-section-icon" aria-hidden="true">
            <DbIcon />
          </span>
          <span>Datastreams</span>
          <b>7</b>
          <span className="tree-chevron" aria-hidden="true">
            ›
          </span>
        </summary>
        <div className="tree-section-panel">
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

          {filtered.map((g) => {
            if (q && g.matches.length === 0) return null;
            return (
              <details className="stream-group" key={g.key} open={g.defaultOpen || Boolean(q)}>
                <summary className="stream-group-row">
                  <span className="group-icon" aria-hidden="true">
                    {GROUP_ICON[g.key]}
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
          })}
        </div>
      </details>

      {tool("Sources", "sources", "4")}
      {tool("Imports", "imports", "2")}
      {tool("Modules", "modules", "1 active", true)}
    </div>
  );
}
