/**
 * Sources — the provider-accounts surface.
 *
 * Visual lineage:
 *   _bmad-output/planning-artifacts/ux-designs/ux-connector-2026-07-23/
 *     mockups/sources.html
 *
 * The application shell (ApplicationShell.tsx) already renders the frame,
 * sidebar, topbar, and <main className="main">. This component renders ONLY the
 * page content: the page header, the three summary cards, and the
 * provider-accounts panel.
 *
 * Data: GET /api/connections (see ../../ConnectionsList.tsx) — provider,
 * health.status, token_expiry, exposure, owner_org_name and
 * active_datastream_count are all real fields on that read-model, and every cell
 * below is derived from them.
 *
 * WHAT WAS REMOVED
 * ----------------
 * This page used to open on four invented provider accounts — "Acme Ads",
 * "Northwind Search", "acme.com", "Planning team" — complete with credential
 * expiry dates, owning organizations and per-source Datastream counts, and it
 * KEPT them in two cases that matter:
 *   - `if (!resp.ok) return;  // keep mockup literals on any failure`
 *   - `if (connections.length > 0)` — an account with genuinely NO connections
 *     was shown four, which is the worst possible reading for the case it is
 *     most likely to occur in.
 * The three summary counts were then computed FROM those literals, so a broken
 * or empty console reported "4 usable provider accounts, 3 healthy".
 *
 * Now: a failed load is said and no rows are rendered; an empty list is rendered
 * as empty; the summary cards read from the same real rows or say nothing.
 * Controls that had no wiring (Add Datastream, Add connection, the provider
 * filter, Manage/Use/Reconnect) are rendered only when the shell passes a
 * handler for them.
 *
 * Styling: application.css (global, via the shell) + sources.css.
 */
import { useCallback, useEffect, useState } from "react";
import type { Connection, ConnectionHealth } from "../../ConnectionsList";
import "../application.css";
import "./sources.css";
import { apiFetch } from "../../lib/apiFetch";

// ---------------------------------------------------------------------------
import ConnectButton from "../../ConnectButton";
import GoogleConnectPanel from "../../GoogleConnectPanel";
// Provider -> logo + display label. Real /connectors/* assets only; provider
// logos are never hand-drawn (see project doctrine). Keys are matched against a
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
  google_ads: { logo: "/connectors/google-ads.png", alt: "Google Ads", label: "Google Ads" },
  "google-ads": { logo: "/connectors/google-ads.png", alt: "Google Ads", label: "Google Ads" },
  google_analytics: {
    logo: "/connectors/google-analytics.png",
    alt: "Google Analytics",
    label: "Google Analytics 4",
  },
  ga4: {
    logo: "/connectors/google-analytics.png",
    alt: "Google Analytics",
    label: "Google Analytics 4",
  },
  google_sheets: {
    logo: "/connectors/google-sheets.png",
    alt: "Google Sheets",
    label: "Google Sheets",
  },
  "google-sheets": {
    logo: "/connectors/google-sheets.png",
    alt: "Google Sheets",
    label: "Google Sheets",
  },
};

function providerMeta(provider: string): ProviderMeta {
  const key = (provider || "").toLowerCase();
  const match = Object.keys(PROVIDER_META).find((k) => key.startsWith(k));
  // Unknown provider: surface the raw provider string rather than borrow another
  // vendor's logo, which would misattribute the account.
  return match ? PROVIDER_META[match] : { logo: "", alt: provider, label: provider };
}

// ---------------------------------------------------------------------------
// View model — one row per connection, every field derived from the API.
// ---------------------------------------------------------------------------

type HealthState = "success" | "warning" | "error";

interface SourceRow {
  id: string;
  logo: string;
  logoAlt: string;
  accountName: string;
  providerLabel: string;
  health: HealthState;
  healthLabel: string;
  healthDetail: string;
  owner: string;
  exposure: string;
  usedBy: string;
  connection: Connection;
  canManage: boolean;
}

type LoadState =
  | { status: "loading" }
  | { status: "denied" }
  | { status: "error"; message: string }
  | { status: "ok"; rows: SourceRow[] };

// Health status from the API -> the three visual states.
function healthFromApi(health?: ConnectionHealth | null): {
  state: HealthState;
  label: string;
} {
  switch (health?.status) {
    case "ok":
      return { state: "success", label: "Healthy" };
    case "stale":
      return { state: "warning", label: "Reconnect" };
    case "revoked":
      return { state: "error", label: "Disconnected" };
    default:
      return { state: "warning", label: "Unknown" };
  }
}

/** Absolute date "18 Aug 2026" (no time), else "". */
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
    const h = healthFromApi(
      conn.status === "revoked" ? { status: "revoked", last_checked_at: null, last_fetched_at: null } : conn.health
    );
    const count = conn.active_datastream_count ?? 0;
    const detail = expiryDetail(conn.token_expiry);
    const expired = detail.startsWith("Expired") || h.state === "error";
    return {
      id: conn.id,
      connection: conn,
      canManage: conn.can_manage === true,
      logo: meta.logo,
      logoAlt: meta.alt,
      accountName: conn.account_label || conn.nango_connection_id || conn.id,
      providerLabel: meta.label,
      health: h.state,
      healthLabel: h.label,
      healthDetail: detail,
      owner: conn.owner_org_name ?? "",
      exposure: exposureCopy(conn.exposure, conn.owner_org_name, expired),
      usedBy: `${count} Datastream${count === 1 ? "" : "s"}`,
    };
  });
}

interface SourcesProps {
  projectId?: string;
  /** Header/row actions. Each control is rendered ONLY when the shell wires it —
   *  a button that does nothing is worse than no button. */
  onAddDatastream?: () => void;
  onAddConnection?: () => void;
  onManageConnection?: (connectionId: string) => void;
  onReconnect?: (connectionId: string) => void;
}

function RowActions({
  row,
  onManageConnection,
  onReconnect,
  onOpenManage,
}: {
  row: SourceRow;
  onManageConnection?: (id: string) => void;
  onReconnect?: (id: string) => void;
  onOpenManage: (row: SourceRow) => void;
}) {
  if (!row.canManage) return null;
  const needsReconnect = row.health !== "success";
  return (
    <div className="row-actions">
      <button
        className="quiet-button"
        type="button"
        onClick={() => {
          onManageConnection?.(row.id);
          onOpenManage(row);
        }}
      >
        Manage
      </button>
      {needsReconnect && (
        <button
          className="secondary-button"
          type="button"
          onClick={() => {
            onReconnect?.(row.id);
            onOpenManage(row);
          }}
        >
          Reconnect
        </button>
      )}
    </div>
  );
}

export default function Sources({
  projectId,
  onAddDatastream,
  onAddConnection,
  onManageConnection,
  onReconnect,
}: SourcesProps) {
  const [state, setState] = useState<LoadState>({ status: "loading" });
  const [managedRow, setManagedRow] = useState<SourceRow | null>(null);
  const [confirmRevoke, setConfirmRevoke] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setState({ status: "loading" });
    try {
    if (!projectId) {
      setState({ status: "denied" });
      return;
    }
      const url = `/api/connections?project_id=${encodeURIComponent(projectId)}`;
      const resp = await apiFetch(url);
      if ([401, 403, 404].includes(resp.status)) {
        setState({ status: "denied" });
        return;
      }
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      const data: { connections?: Connection[] } = await resp.json();
      // An empty list is the answer, not a reason to render accounts.
      setState({ status: "ok", rows: fromConnections(data.connections ?? []) });
    } catch (err) {
      setState({ status: "error", message: err instanceof Error ? err.message : String(err) });
    }
  }, [projectId]);

  useEffect(() => {
    void load();
  }, [load]);

  const rows = state.status === "ok" ? state.rows : [];
  const healthyCount = rows.filter((r) => r.health === "success").length;
  const attentionCount = rows.length - healthyCount;
  /** Summary figures exist only when the list was actually read. */
  const figure = (n: number) => (state.status === "ok" ? String(n) : "—");
  const figureNote = (note: string) =>
    state.status === "loading" ? "Loading…" : state.status === "error" ? "Not available" : note;

  const showActions = rows.some((row) => row.canManage);

  async function revokeManagedConnection() {
    if (!projectId || !managedRow?.canManage) return;
    setActionError(null);
    try {
      const resp = await apiFetch(
        `/api/projects/${encodeURIComponent(projectId)}/connections/${encodeURIComponent(managedRow.id)}/revoke`,
        { method: "POST" }
      );
      if (!resp.ok) {
        const body = (await resp.json().catch(() => ({}))) as { message?: string };
        throw new Error(body.message ?? `HTTP ${resp.status}`);
      }
      setConfirmRevoke(false);
      setManagedRow(null);
      await load();
    } catch (err) {
      setActionError(err instanceof Error ? err.message : String(err));
    }
  }

  return (
    <>

      <div className="page-header">
        <div>
          <h1>Sources</h1>
          <p>Authorizations and provider accounts available to this project.</p>
        </div>
        <div className="header-actions">
          {onAddDatastream && (
            <button className="secondary-button" type="button" onClick={() => onAddDatastream()}>
              Add Datastream
            </button>
          )}
          <ConnectButton
            projectId={projectId}
            excludeProviders={["google"]}
            label="Add connection"
            onSuccess={() => {
              onAddConnection?.();
              void load();
            }}
          />
        </div>
      </div>

      <section className="source-summary">
        <article className="panel source-card">
          <span>Usable provider accounts</span>
          <strong>{figure(rows.length)}</strong>
          <p>{figureNote("Owned by or shared with your organization")}</p>
        </article>
        <article className="panel source-card">
          <span>Healthy</span>
          <strong>{figure(healthyCount)}</strong>
          <p>{figureNote("Ready for new Datastreams")}</p>
        </article>
        <article className="panel source-card">
          <span>Needs attention</span>
          <strong>{figure(attentionCount)}</strong>
          <p>{figureNote("Reconnect before the next run")}</p>
        </article>
      </section>

      <section className="panel source-panel">
        <div className="source-panel-header">
          <div>
            <h2>Provider accounts</h2>
            <p>Access is scoped to the organization. Credential material is never displayed.</p>
          </div>
        </div>

        {state.status === "loading" && (
          <p className="source-status" role="status">

            Loading provider accounts…
          </p>
        )}

        {state.status === "denied" && (
          <div className="source-load-error" role="alert">
            <span className="signal-label error">
              <span className="signal-mark" /> Access denied
            </span>
            <p>This project is unavailable or your account cannot view its provider accounts.</p>
          </div>
        )}
        {state.status === "error" && (
          <div className="source-load-error" role="alert">
            <span className="signal-label error">
              <span className="signal-mark" />
              Could not load the provider accounts
            </span>
            <p>
              {state.message}. No account is being listed — this is a loading failure, not an
              empty account list.
            </p>
            <button className="secondary-button" type="button" onClick={() => void load()}>
              Retry
            </button>
          </div>
        )}

        {state.status === "ok" && rows.length === 0 && (
          <p className="source-status">
            No provider account is connected to this project yet. Connect a source to start
            building Datastreams.
          </p>
        )}

        {state.status === "ok" && rows.length > 0 && (
          <div className="table-scroll" tabIndex={0} aria-label="Usable provider accounts">
            <table className="source-table">
              <thead>
                <tr>
                  <th>Provider account</th>
                  <th>Health</th>
                  <th>Owner and exposure</th>
                  <th>Used by</th>
                  {showActions && (
                    <th>
                      <span className="sr-only">Actions</span>
                    </th>
                  )}
                </tr>
              </thead>
              <tbody>
                {rows.map((row) => (
                  <tr key={row.id}>
                    <td>
                      <div className="provider-cell">
                        {row.logo ? (
                          <span className="provider-logo">
                            <img src={row.logo} alt={row.logoAlt} />
                          </span>
                        ) : null}
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

                        <strong>{row.owner || "—"}</strong>
                        {row.exposure ? <small>{row.exposure}</small> : null}
                      </div>
                    </td>
                    <td>{row.usedBy}</td>
                    {showActions && (
                      <td>
                        <RowActions
                          row={row}
                          onManageConnection={onManageConnection}
                          onReconnect={onReconnect}
                          onOpenManage={(selected) => {
                            setManagedRow(selected);
                            setConfirmRevoke(false);
                          }}
                        />
                      </td>
                    )}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        {managedRow && projectId && (
          <div className="source-management" role="region" aria-label={`Manage ${managedRow.accountName}`}>
            <div className="source-panel-header">
              <div>
                <h3>{managedRow.accountName}</h3>
                <p>{managedRow.providerLabel} authorization</p>
              </div>
              <button className="quiet-button" type="button" onClick={() => setManagedRow(null)}>
                Close
              </button>
            </div>
            {actionError && <p className="source-load-error" role="alert">{actionError}</p>}
            {managedRow.connection.auth_path === "google_direct" ? (
              <GoogleConnectPanel
                connectionRefId={managedRow.id}
                projectId={projectId}
                onStatusChange={() => void load()}
              />
            ) : (
              <div className="row-actions">
                <ConnectButton
                  projectId={projectId}
                  fixedProvider={managedRow.connection.provider}
                  nangoConnectionId={managedRow.connection.nango_connection_id}
                  label="Reconnect"
                  onSuccess={() => void load()}
                />
                {!confirmRevoke ? (
                  <button className="quiet-button" type="button" onClick={() => setConfirmRevoke(true)}>
                    Revoke
                  </button>
                ) : (
                  <>
                    <span>Revoke this authorization?</span>
                    <button className="quiet-button" type="button" onClick={() => setConfirmRevoke(false)}>
                      Cancel
                    </button>
                    <button className="secondary-button" type="button" onClick={() => void revokeManagedConnection()}>
                      Confirm revoke
                    </button>
                  </>
                )}
              </div>
            )}
          </div>
        )}
      </section>
    </>
  );
}
