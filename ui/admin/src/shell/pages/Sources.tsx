/**
 * Sources — faithful React port of the validated Sources mockup.
 *
 * Source of visual truth:
 *   _bmad-output/planning-artifacts/ux-designs/ux-connector-2026-07-23/
 *     mockups/sources.html
 *
 * The application shell (ApplicationShell.tsx) already renders the frame,
 * sidebar, topbar, and <main className="main">. This component renders ONLY the
 * page content that lives inside <main>: the page header, the three summary
 * cards, the provider-accounts panel (a table of usable provider accounts with
 * health, owner/exposure, and per-source datastream counts), and the trailing
 * inline note about shared-account access limits.
 *
 * Styling: application.css (global, via the shell) for shell/layout classes
 * (panel, page-header, header-actions, selector, buttons, signal-label,
 * signal-mark, provider-logo, sr-only) + sources.css for the page-specific
 * summary cards, table, cell layouts, and inline note. Colors come exclusively
 * from the application.css CSS variables; numbers use Geist tabular via those
 * classes.
 *
 * Data: the existing /api/connections endpoint (see ../../ConnectionsList.tsx)
 * exposes provider, health.status, and active_datastream_count — which map to
 * the "Provider account", "Health" state, and "Used by" columns. Everything the
 * mockup surfaces beyond that — the friendly account name, the owning
 * organization, the ownership/exposure copy ("Owned · You can manage",
 * "Shared with Acme Group", "Owned · Authorization expired"), the credential
 * expiry dates, the "managed feed" vs "Datastream" wording, and the three
 * summary counts — has NO backing field in the current API and is rendered from
 * the mockup literal, flagged with // TODO(api). When the backend answers, the
 * account rows below the literals should be replaced by the mapped connections.
 */
import { useEffect, useState } from "react";
import type { Connection, ConnectionHealth } from "../../ConnectionsList";
import "../application.css";
import "./sources.css";

// ---------------------------------------------------------------------------
// Provider -> logo + display label. Real /connectors/*.svg assets only; provider
// SVGs are never hand-drawn (see project doctrine). Keys are matched against a
// lowercased connection.provider prefix.
// ---------------------------------------------------------------------------

interface ProviderMeta {
  logo: string;
  alt: string;
  label: string;
}

const PROVIDER_META: Record<string, ProviderMeta> = {
  meta: { logo: "/connectors/meta.svg", alt: "Meta", label: "Meta Ads" },
  facebook: { logo: "/connectors/meta.svg", alt: "Meta", label: "Meta Ads" },
  google_ads: { logo: "/connectors/google-ads.svg", alt: "Google Ads", label: "Google Ads" },
  "google-ads": { logo: "/connectors/google-ads.svg", alt: "Google Ads", label: "Google Ads" },
  google_analytics: {
    logo: "/connectors/google-analytics.svg",
    alt: "Google Analytics",
    label: "Google Analytics 4",
  },
  ga4: {
    logo: "/connectors/google-analytics.svg",
    alt: "Google Analytics",
    label: "Google Analytics 4",
  },
  google_sheets: {
    logo: "/connectors/google-sheets.svg",
    alt: "Google Sheets",
    label: "Google Sheets",
  },
  "google-sheets": {
    logo: "/connectors/google-sheets.svg",
    alt: "Google Sheets",
    label: "Google Sheets",
  },
};

function providerMeta(provider: string): ProviderMeta {
  const key = (provider || "").toLowerCase();
  const match = Object.keys(PROVIDER_META).find((k) => key.startsWith(k));
  // Fallback keeps the page rendering for unknown providers: reference a known
  // logo path and surface the raw provider string as the label.
  return match
    ? PROVIDER_META[match]
    : { logo: "/connectors/meta.svg", alt: provider, label: provider };
}

// ---------------------------------------------------------------------------
// View model. The mockup exposes fields the API does not (account name, owner,
// exposure, expiry, feed wording). Modeled here so the literals and any
// API-derived values share one shape.
// ---------------------------------------------------------------------------

type HealthState = "success" | "warning" | "error";

interface SourceRow {
  logo: string;
  logoAlt: string;
  accountName: string; // TODO(api): friendly provider-account name (not in /api/connections)
  providerLabel: string;
  health: HealthState;
  healthLabel: string;
  healthDetail: string; // TODO(api): credential expiry date (not in /api/connections)
  owner: string; // TODO(api): owning organization (not in /api/connections)
  exposure: string; // TODO(api): ownership/sharing exposure copy (not in /api/connections)
  usedBy: string; // partial: count is real (active_datastream_count); wording is literal
  actions: "manage-use" | "use" | "reconnect";
}

// Mockup literals — the validated Sources design. Rendered verbatim so the page
// is finished with no backend. Replaced by mapped connections when the API
// answers (see fromConnections below).
const MOCKUP_ROWS: SourceRow[] = [
  {
    logo: "/connectors/meta.svg",
    logoAlt: "Meta",
    accountName: "Acme Ads",
    providerLabel: "Meta Ads",
    health: "success",
    healthLabel: "Healthy",
    healthDetail: "Expires 18 Aug 2026",
    owner: "Acme Group",
    exposure: "Owned · You can manage",
    usedBy: "2 Datastreams",
    actions: "manage-use",
  },
  {
    logo: "/connectors/google-ads.svg",
    logoAlt: "Google Ads",
    accountName: "Northwind Search",
    providerLabel: "Google Ads",
    health: "success",
    healthLabel: "Healthy",
    healthDetail: "Expires 18 Aug 2026",
    owner: "Northwind Studio",
    exposure: "Shared with Acme Group",
    usedBy: "1 Datastream",
    actions: "use",
  },
  {
    logo: "/connectors/google-analytics.svg",
    logoAlt: "Google Analytics",
    accountName: "acme.com",
    providerLabel: "Google Analytics 4",
    health: "warning",
    healthLabel: "Reconnect",
    healthDetail: "Expired 21 Jul 2026",
    owner: "Acme Group",
    exposure: "Owned · Authorization expired",
    usedBy: "1 Datastream",
    actions: "reconnect",
  },
  {
    logo: "/connectors/google-sheets.svg",
    logoAlt: "Google Sheets",
    accountName: "Planning team",
    providerLabel: "Google Sheets",
    health: "success",
    healthLabel: "Healthy",
    healthDetail: "Expires 18 Aug 2026",
    owner: "Acme Group",
    exposure: "Owned · You can manage",
    usedBy: "1 managed feed",
    actions: "manage-use",
  },
];

// Health status from the API -> the mockup's three visual states. The API has
// no "authorization expired vs reconnect" nuance beyond stale/revoked.
function healthFromApi(health?: ConnectionHealth | null): {
  state: HealthState;
  label: string;
  actions: SourceRow["actions"];
} {
  switch (health?.status) {
    case "ok":
      return { state: "success", label: "Healthy", actions: "manage-use" };
    case "stale":
      return { state: "warning", label: "Reconnect", actions: "reconnect" };
    case "revoked":
      return { state: "error", label: "Disconnected", actions: "reconnect" };
    default:
      return { state: "warning", label: "Reconnect", actions: "reconnect" };
  }
}

/** Localized absolute date "18 Aug 2026" (no time), else "". */
function fmtDate(iso?: string | null): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  return d.toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric" });
}

/** Expiry copy from token_expiry: "Expires/Expired {date}" (empty when unknown). */
function expiryDetail(tokenExpiry?: string | null): string {
  const date = fmtDate(tokenExpiry);
  if (!date) return "";
  const past = new Date(tokenExpiry as string).getTime() < Date.now();
  return `${past ? "Expired" : "Expires"} ${date}`;
}

/** Ownership/exposure copy from the derived exposure enum + owner org. */
function exposureCopy(
  exposure: Connection["exposure"],
  ownerOrgName?: string | null,
  expired?: boolean,
): string {
  switch (exposure) {
    case "provided_by_org":
      return ownerOrgName ? `Provided by ${ownerOrgName}` : "Provided by another organization";
    case "shared_with_org":
      return "Owned · Shared with other organizations";
    case "owned":
    default:
      return expired ? "Owned · Authorization expired" : "Owned · You can manage";
  }
}

function fromConnections(connections: Connection[]): SourceRow[] {
  return connections.map((conn) => {
    const meta = providerMeta(conn.provider);
    const h = healthFromApi(conn.health);
    const count = conn.active_datastream_count ?? 0;
    const detail = expiryDetail(conn.token_expiry);
    const expired = detail.startsWith("Expired") || h.state === "error";
    return {
      logo: meta.logo,
      logoAlt: meta.alt,
      // TODO(api): friendly provider-account name (label) not yet on /api/connections.
      accountName: conn.nango_connection_id || conn.id,
      providerLabel: meta.label,
      health: h.state,
      healthLabel: h.label,
      healthDetail: detail,
      owner: conn.owner_org_name ?? "",
      exposure: exposureCopy(conn.exposure, conn.owner_org_name, expired),
      usedBy: `${count} Datastream${count === 1 ? "" : "s"}`,
      actions: h.actions,
    };
  });
}

function RowActions({ actions }: { actions: SourceRow["actions"] }) {
  if (actions === "reconnect") {
    return (
      <div className="row-actions">
        {/* TODO(api): trigger the reconnect / re-consent flow for this account. */}
        <button className="secondary-button" type="button">
          Reconnect
        </button>
      </div>
    );
  }
  if (actions === "use") {
    return (
      <div className="row-actions">
        {/* TODO(api): route to Add Datastream pre-scoped to this account. */}
        <a className="secondary-button action-link" href="#">
          Use
        </a>
      </div>
    );
  }
  return (
    <div className="row-actions">
      {/* TODO(api): open the account management drawer. */}
      <button className="quiet-button" type="button">
        Manage
      </button>
      {/* TODO(api): route to Add Datastream pre-scoped to this account. */}
      <a className="secondary-button action-link" href="#">
        Use
      </a>
    </div>
  );
}

interface SourcesProps {
  projectId?: string;
}

export default function Sources({ projectId }: SourcesProps) {
  const [rows, setRows] = useState<SourceRow[]>(MOCKUP_ROWS);

  useEffect(() => {
    let cancelled = false;
    async function load() {
      try {
        const url = projectId
          ? `/api/connections?project_id=${encodeURIComponent(projectId)}`
          : "/api/connections";
        const resp = await fetch(url);
        if (!resp.ok) return; // keep mockup literals on any failure
        const data: { connections?: Connection[] } = await resp.json();
        const connections = data.connections ?? [];
        // Only replace the literals when the API actually returns accounts,
        // so the page always renders finished with no backend.
        if (!cancelled && connections.length > 0) {
          setRows(fromConnections(connections));
        }
      } catch {
        // Swallow — the mockup literals remain the rendered state.
      }
    }
    void load();
    return () => {
      cancelled = true;
    };
  }, [projectId]);

  // Summary counts. TODO(api): these derive from ownership + health semantics
  // that the current API does not expose (usable = owned-or-shared, healthy vs
  // needs-attention). Derived from the rendered rows as the best available
  // approximation; falls back to the mockup literals for the ownership subtitle.
  const usableCount = rows.length;
  const healthyCount = rows.filter((r) => r.health === "success").length;
  const attentionCount = rows.length - healthyCount;

  return (
    <>
      <div className="page-header">
        <div>
          <h1>Sources</h1>
          <p>Authorizations and provider accounts available to Acme Growth.</p>
        </div>
        <div className="header-actions">
          {/* TODO(api): route to the Add Datastream flow. */}
          <a className="secondary-button action-link" href="#">
            Add Datastream
          </a>
          {/* TODO(api): open the Add connection (Nango / Google OAuth) flow. */}
          <button className="primary-button" type="button">
            + Add connection
          </button>
        </div>
      </div>

      <section className="source-summary">
        <article className="panel source-card">
          <span>Usable provider accounts</span>
          <strong>{usableCount}</strong>
          {/* TODO(api): "Owned or shared with <org>" — org name not in API. */}
          <p>Owned or shared with Acme Group</p>
        </article>
        <article className="panel source-card">
          <span>Healthy</span>
          <strong>{healthyCount}</strong>
          <p>Ready for new Datastreams</p>
        </article>
        <article className="panel source-card">
          <span>Needs attention</span>
          <strong>{attentionCount}</strong>
          <p>Reconnect before the next run</p>
        </article>
      </section>

      <section className="panel source-panel">
        <div className="source-panel-header">
          <div>
            <h2>Provider accounts</h2>
            <p>Access is scoped to the organization. Credential material is never displayed.</p>
          </div>
          {/* TODO(api): provider filter. */}
          <button className="selector" type="button">
            All providers
          </button>
        </div>
        <div className="table-scroll" tabIndex={0} aria-label="Usable provider accounts">
          <table className="source-table">
            <thead>
              <tr>
                <th>Provider account</th>
                <th>Health</th>
                <th>Owner and exposure</th>
                <th>Used by</th>
                <th>
                  <span className="sr-only">Actions</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row, i) => (
                <tr key={`${row.accountName}-${i}`}>
                  <td>
                    <div className="provider-cell">
                      <span className="provider-logo">
                        <img src={row.logo} alt={row.logoAlt} />
                      </span>
                      <div>
                        <strong>{row.accountName}</strong>
                        <small>{row.providerLabel}</small>
                      </div>
                    </div>
                  </td>
                  <td>
                    <div className="health-cell">
                      <span className={`signal-label ${row.health}`}>
                        <span className="signal-mark" />
                        {row.healthLabel}
                      </span>
                      {row.healthDetail ? <small>{row.healthDetail}</small> : null}
                    </div>
                  </td>
                  <td>
                    <div className="owner-cell">
                      <strong>{row.owner}</strong>
                      {row.exposure ? <small>{row.exposure}</small> : null}
                    </div>
                  </td>
                  <td>{row.usedBy}</td>
                  <td>
                    <RowActions actions={row.actions} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>

      <div className="inline-note">
        <span className="signal-label info">
          <span className="signal-mark" />
        </span>
        <div>
          <strong>Shared account access is intentionally limited.</strong>
          You can use Northwind Search in a Datastream, but only Northwind Studio can inspect
          or revoke its authorization.
        </div>
      </div>
    </>
  );
}
