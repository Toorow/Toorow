/**
 * DatastreamCreate — faithful React port of the validated "Add Datastream"
 * (create-flow, step 1 of 6) mockup.
 *
 * Source of visual truth:
 *   _bmad-output/planning-artifacts/ux-designs/ux-connector-2026-07-23/
 *     mockups/datastream-create.html
 *
 * The application shell (ApplicationShell.tsx) renders the frame, sidebar,
 * topbar, and <main>. This component renders the FULL main content the mockup
 * shows: the DIMMED datastreams page behind the dialog (page header, data
 * summary, a placeholder panel) plus the centered FOCUSED DIALOG that opens the
 * six-stage create flow at "Step 1 of 6 · Choose where data comes from". That
 * step surfaces the three source choices (Connector report / Existing BigQuery /
 * Managed feed) and the organization-usable connection picker with expandable
 * connection cards (provider, account, health, owner, sharing — token material
 * kept hidden), exactly as the mockup.
 *
 * Styling: application.css (global, via the shell) supplies the base shell
 * classes; datastream-create.css adds the wizard/dialog surfaces the mockup
 * pulls from its sibling workflow-surfaces.css. Colors come exclusively from the
 * application.css CSS variables (brand accents via color-mix on --rose); numbers
 * use Geist tabular via those classes. Provider logos are the real /connectors/*.svg
 * images.
 *
 * Data & logic: this is a faithful VISUAL port of the mockup's step 1, not a
 * re-wiring of the whole wizard. The REAL 6-stage draft/versioning logic already
 * exists in ui/admin/src/datastreams/wizard/CreerFluxWizard.tsx (+ SourceStage,
 * ConfigureStage, DestinationStage, ClassifyStage, PreviewStage, ScheduleStage,
 * wizardApi.ts). Here the source choices and the connection list are the
 * mockup's literals, flagged // TODO(api) where they would call wizardApi
 * (listConnections / getSourceCapabilities / saveDatastreamDraft). Local state
 * drives only the visible selection affordances (which source choice, which
 * connection, picker expanded) so the screen renders finished with NO backend.
 */
import { useEffect, useState } from "react";
import { connectorSrc } from "../ConnectorLogo";
import "../application.css";
import "./datastream-create.css";

/** The three source kinds the mockup's step 1 offers. Maps 1:1 to the real
 *  wizard's SourceKind (connector_pull / existing_bigquery / managed_feed). */
type SourceChoiceId = "connector_report" | "existing_bigquery" | "managed_feed";

interface SourceChoice {
  id: SourceChoiceId;
  title: string;
  body: string;
  icon: JSX.Element;
}

const SOURCE_CHOICES: SourceChoice[] = [
  {
    id: "connector_report",
    title: "Connector report",
    body: "Select a provider report, fields, grain, history and supported schedule.",
    icon: (
      <svg viewBox="0 0 24 24">
        <path d="M4 7h16v10H4zM8 11h8M8 14h5" />
      </svg>
    ),
  },
  {
    id: "existing_bigquery",
    title: "Existing BigQuery",
    body: "Reference a read-only table or view while its external writer stays authoritative.",
    icon: (
      <svg viewBox="0 0 24 24">
        <ellipse cx="12" cy="5" rx="7" ry="3" />
        <path d="M5 5v7c0 2 3 3 7 3s7-1 7-3V5M5 12v7c0 2 3 3 7 3s7-1 7-3v-7" />
      </svg>
    ),
  },
  {
    id: "managed_feed",
    title: "Managed feed",
    body: "Import CSV, Excel or Google Sheets into a governed toorow landing.",
    icon: (
      <svg viewBox="0 0 24 24">
        <path d="M6 3h9l3 3v15H6zM9 11h6M9 15h6" />
      </svg>
    ),
  },
];

/** One expandable connection card: provider identity, the account it points at,
 *  live health, owner + sharing lineage. Token/credential material is
 *  deliberately NOT represented — the mockup keeps it hidden. */
interface ConnectionOption {
  id: string;
  provider: string;
  /** Real /connectors/*.svg logo path. */
  logo: string;
  account: string;
  /** Owner + optional sharing lineage, already composed for display. */
  ownership: string;
  health: "Healthy" | "Needs attention";
  /** Brand-aware sources (YouTube, Trends, social, brand SEA…) report per brand:
   *  the create flow lets you pick tracked brands and reuses their per-source
   *  binding from the competitor registry (epic 40, Story 40.2). */
  brandAware?: boolean;
  /** How this source identifies a brand, e.g. "YouTube channel" / "Trends term". */
  bindingLabel?: string;
}

// TODO(api): replace with wizardApi.listConnections(cfg) — connections usable by
// the current organization, with health + ownership/sharing lineage resolved.
const CONNECTIONS: ConnectionOption[] = [
  {
    id: "yt-apple",
    provider: "YouTube Analytics",
    logo: "/connectors/youtube-analytics.svg",
    account: "Apple",
    ownership: "Owned by Acme Group",
    health: "Healthy",
    brandAware: true,
    bindingLabel: "YouTube channel",
  },
  {
    id: "meta-acme-ads",
    provider: "Meta Ads",
    logo: "/connectors/meta.svg",
    account: "Acme Ads",
    ownership: "Owned by Acme Group",
    health: "Healthy",
  },
  {
    id: "gads-northwind-search",
    provider: "Google Ads",
    logo: "/connectors/google-ads.svg",
    account: "Northwind Search",
    ownership: "Owned by Northwind Studio · Shared with Acme Group",
    health: "Healthy",
  },
];

/** A tracked brand as it applies to THIS source: the canonical entity + its
 *  per-source binding (the YouTube channel here). Roles are project-scoped; the
 *  binding is declared once in the registry and reused (epic 40). */
interface TrackedBrandBinding {
  id: string;
  name: string;
  role: "own" | "competitor" | "reference";
  /** The source-specific identifier — e.g. a YouTube handle. */
  binding: string;
}

// TODO(api): wizardApi.listTrackedBrandsForSource(projectId, source) — the org's
// registry brands with this project's role + this source's binding. Unbound
// brands surface an "add binding" affordance; a pasted handle not in the registry
// raises the governed new-entity flow (Governance > Competitors).
const REGISTRY_BRANDS: TrackedBrandBinding[] = [
  { id: "apple", name: "Apple", role: "own", binding: "@Apple" },
  { id: "google", name: "Google", role: "competitor", binding: "@Google" },
  { id: "samsung", name: "Samsung", role: "competitor", binding: "@Samsung" },
  { id: "microsoft", name: "Microsoft", role: "competitor", binding: "@Microsoft" },
];

// --- Real-data mappers (GET /api/connections + GET /api/tracked-entities) -----

/** Providers whose sources report per brand — the create flow then offers the
 *  tracked-brand selection (epic 40, Story 40.2). */
const BRAND_AWARE_PROVIDERS = new Set([
  "youtube-analytics",
  "youtube",
  "google-trends",
  "trends",
]);

interface ApiConnection {
  id: string;
  provider: string;
  nango_connection_id?: string;
  health?: { status?: string } | null;
  owner_org_name?: string | null;
  exposure?: string | null;
}

interface ApiBrandEntity {
  id: string;
  name: string;
  role: string;
  aliases?: string[];
}

/** Title-case a kebab provider id for display ("youtube-analytics" -> "Youtube analytics"). */
function providerLabel(provider: string): string {
  const s = provider.replace(/[-_]+/g, " ").trim();
  return s ? s.charAt(0).toUpperCase() + s.slice(1) : provider;
}

function toConnectionOption(c: ApiConnection): ConnectionOption {
  const provider = c.provider || "";
  const brandAware = BRAND_AWARE_PROVIDERS.has(provider.toLowerCase());
  const owner = c.owner_org_name ? `Owned by ${c.owner_org_name}` : "";
  const shared = c.exposure === "provided_by_org" ? " · Shared with your org" : "";
  return {
    id: c.id,
    provider: providerLabel(provider),
    logo: connectorSrc(provider),
    account: c.nango_connection_id || provider,
    ownership: `${owner}${shared}`.trim() || "—",
    health: c.health?.status === "ok" ? "Healthy" : "Needs attention",
    brandAware,
    bindingLabel: brandAware ? "YouTube channel" : undefined,
  };
}

function toBrandBinding(e: ApiBrandEntity): TrackedBrandBinding {
  const role: TrackedBrandBinding["role"] = (
    ["own", "competitor", "reference"] as string[]
  ).includes(e.role)
    ? (e.role as TrackedBrandBinding["role"])
    : "reference";
  const handle = (e.aliases ?? []).find((a) => a.startsWith("@")) ?? `@${e.name}`;
  return { id: e.id, name: e.name, role, binding: handle };
}

interface DatastreamCreateProps {
  projectId?: string;
  /** Called when the operator cancels/closes the create dialog. */
  onCancel?: () => void;
  /** Called when the operator advances past step 1 (Source → Configure). */
  onContinue?: (selection: {
    sourceKind: SourceChoiceId;
    connectionId: string | null;
  }) => void;
}

export default function DatastreamCreate({
  projectId = "default",
  onCancel,
  onContinue,
}: DatastreamCreateProps) {
  // Visible selection affordances the mockup renders (.selected / aria-checked /
  // aria-expanded). No draft/versioning here — that lives in CreerFluxWizard.
  const [sourceKind, setSourceKind] = useState<SourceChoiceId>("connector_report");
  // Connections + tracked brands are fetched; the mock stays as the offline/
  // loading fallback so the create dialog renders finished.
  const [connections, setConnections] = useState<ConnectionOption[]>(CONNECTIONS);
  const [brands, setBrands] = useState<TrackedBrandBinding[]>(REGISTRY_BRANDS);
  const [connectionId, setConnectionId] = useState<string | null>(CONNECTIONS[0]?.id ?? null);
  const [pickerOpen, setPickerOpen] = useState(true);
  // Brand-aware sources: which registry brands to pull (own is pre-selected).
  const [selectedBrands, setSelectedBrands] = useState<Set<string>>(
    () => new Set(REGISTRY_BRANDS.filter((b) => b.role === "own").map((b) => b.id)),
  );

  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const res = await fetch(`/api/connections?project_id=${encodeURIComponent(projectId)}`);
        if (!res.ok || !alive) return;
        const body = (await res.json()) as { connections?: ApiConnection[] };
        const mapped = (body.connections ?? []).map(toConnectionOption);
        setConnections(mapped);
        setConnectionId(mapped[0]?.id ?? null);
      } catch {
        /* keep the mock connections offline */
      }
    })();
    return () => {
      alive = false;
    };
  }, [projectId]);

  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const res = await fetch(`/api/tracked-entities?project_id=${encodeURIComponent(projectId)}`);
        if (!res.ok || !alive) return;
        const body = (await res.json()) as { entities?: ApiBrandEntity[] };
        const mapped = (body.entities ?? []).map(toBrandBinding);
        setBrands(mapped);
        setSelectedBrands(new Set(mapped.filter((b) => b.role === "own").map((b) => b.id)));
      } catch {
        /* keep the mock brands offline */
      }
    })();
    return () => {
      alive = false;
    };
  }, [projectId]);

  const activeConnection = connections.find((c) => c.id === connectionId) ?? null;
  const brandAware = sourceKind === "connector_report" && !!activeConnection?.brandAware;
  const toggleBrand = (id: string) =>
    setSelectedBrands((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });

  return (
    // Rendered as a fragment inside the shell's <main className="main"> (like
    // the sibling .full-main pages). .dialog-stage is a full-bleed surface: the
    // datastream-create.css neutralizes the shell padding around it so the
    // scrim (inset:0) covers the whole stage, faithful to the mockup.
    <div className="dialog-stage">
      {/* Dimmed datastreams page behind the dialog. */}
      <div className="dialog-stage-background">
        <div className="page-header">
          <div>
            <h1>Datastreams</h1>
            <p>Collection contracts that produce governed datasets for this project.</p>
          </div>
          <button className="primary-button">+ Add Datastream</button>
        </div>
        <section className="data-summary">
          <article className="panel summary-card">
            <span>Active</span>
            {/* TODO(api): active datastream count for this project. */}
            <strong>7</strong>
            <p>Across four providers</p>
          </article>
          <article className="panel summary-card">
            <span>Healthy</span>
            {/* TODO(api): healthy / needs-attention breakdown. */}
            <strong>6</strong>
            <p>One needs attention</p>
          </article>
          <article className="panel summary-card">
            <span>Latest publication</span>
            {/* TODO(api): freshest published-datastream age + name. */}
            <strong>3h</strong>
            <p>Campaign performance</p>
          </article>
        </section>
        <section className="panel" style={{ height: 400 }} />
      </div>

      {/* Focused create dialog: step 1 of 6 (Source). */}
      <div className="dialog-scrim">
        <section
          className="focused-dialog"
          role="dialog"
          aria-modal="true"
          aria-labelledby="add-title"
        >
          <header className="dialog-header">
            <h1 id="add-title">Add Datastream</h1>
            <button
              className="icon-button action-link"
              type="button"
              aria-label="Close"
              onClick={() => onCancel?.()}
            >
              ×
            </button>
          </header>

          <div className="dialog-body">
            {/* The six-stage flow: Source · Configure · Destination ·
                Classification & mapping · Preview & validation ·
                Schedule & activation. Step 1 is shown here; the real per-stage
                logic lives in CreerFluxWizard's stages. */}
            <div className="step-line">
              <strong>Step 1 of 6 · Choose where data comes from</strong>
              <span>Source</span>
            </div>

            <div className="choice-grid" role="radiogroup" aria-label="Source">
              {SOURCE_CHOICES.map((choice) => {
                const selected = choice.id === sourceKind;
                return (
                  <button
                    key={choice.id}
                    type="button"
                    className={`source-choice${selected ? " selected" : ""}`}
                    role="radio"
                    aria-checked={selected}
                    onClick={() => setSourceKind(choice.id)}
                  >
                    <div className="choice-icon">{choice.icon}</div>
                    <strong>{choice.title}</strong>
                    <p>{choice.body}</p>
                  </button>
                );
              })}
            </div>

            {/* Organization-usable connection picker. Credentials stay hidden. */}
            <div className="connection-picker">
              <button
                type="button"
                className="connection-picker-toggle"
                aria-expanded={pickerOpen}
                onClick={() => setPickerOpen((open) => !open)}
              >
                <span>
                  <strong>Connection</strong>
                  <small>Choose a connection usable by Acme Group. Credentials stay hidden.</small>
                </span>
                <span className="connection-picker-count">
                  {/* TODO(api): count of connections usable by this organization. */}
                  <span className="signal-label info">
                    <span className="signal-mark" />
                    {connections.length} available
                  </span>
                  <span aria-hidden="true">{pickerOpen ? "⌃" : "⌄"}</span>
                </span>
              </button>

              {pickerOpen && (
                <div className="connection-grid" role="radiogroup" aria-label="Available connections">
                  {connections.map((conn) => {
                    const selected = conn.id === connectionId;
                    return (
                      <button
                        key={conn.id}
                        type="button"
                        className={`connection-card${selected ? " selected" : ""}`}
                        role="radio"
                        aria-checked={selected}
                        onClick={() => setConnectionId(conn.id)}
                      >
                        <span className="provider-logo">
                          <img src={conn.logo} alt={conn.provider} />
                        </span>
                        <span className="connection-copy">
                          <strong>{conn.provider}</strong>
                          <span>{conn.account}</span>
                          <small>{conn.ownership}</small>
                        </span>
                        <span className="connection-state">
                          <span
                            className={`signal-label ${
                              conn.health === "Healthy" ? "success" : "warning"
                            }`}
                          >
                            <span className="signal-mark" />
                            {conn.health}
                          </span>
                          <b>{selected ? "Selected" : "Select"}</b>
                        </span>
                      </button>
                    );
                  })}
                </div>
              )}
            </div>

            {/* Brand-aware source (YouTube, Trends, …): pick the tracked brands
                to pull. Handles come from the competitor registry — declared
                once, reused across sources (epic 40, Story 40.2). */}
            {brandAware && (
              <div className="brand-scope">
                <div className="brand-scope-head">
                  <span>
                    <strong>Tracked brands</strong>
                    <small>
                      {activeConnection?.provider} reports per brand. Pick the brands
                      to pull — your own and competitors. Each {activeConnection?.bindingLabel}
                      {" "}comes from the registry, so you declare it once and reuse it.
                    </small>
                  </span>
                  <span className="signal-label info">
                    <span className="signal-mark" />
                    {selectedBrands.size} selected
                  </span>
                </div>

                <div className="brand-list" role="group" aria-label="Tracked brands">
                  {brands.map((b) => {
                    const on = selectedBrands.has(b.id);
                    const roleLabel =
                      b.role === "own" ? "Your brand" : b.role === "competitor" ? "Competitor" : "Reference";
                    return (
                      <button
                        key={b.id}
                        type="button"
                        className={`brand-row${on ? " selected" : ""}`}
                        role="checkbox"
                        aria-checked={on}
                        onClick={() => toggleBrand(b.id)}
                      >
                        <span className={`brand-check${on ? " on" : ""}`} aria-hidden="true" />
                        <span className="brand-identity">
                          <strong>{b.name}</strong>
                          <span className={`signal-label ${b.role === "own" ? "info" : b.role === "competitor" ? "rose" : "muted"}`}>
                            <span className="signal-mark" />
                            {roleLabel}
                          </span>
                        </span>
                        <code className="brand-binding">{b.binding}</code>
                      </button>
                    );
                  })}
                </div>

                {/* TODO(api): paste a channel URL/handle → resolve against the
                    registry; unknown raises the governed new-entity flow. */}
                <div className="brand-add">
                  <input
                    type="text"
                    className="brand-add-input"
                    placeholder="Add a competitor by YouTube URL or @handle — e.g. https://youtube.com/@google"
                    aria-label="Add a competitor by channel"
                  />
                  <button type="button" className="secondary-button">+ Add competitor</button>
                </div>
                <p className="brand-note">
                  New handles are added to the registry (Governance › Competitors) and
                  wired for every future {activeConnection?.provider} stream. Inbound results
                  are matched back to these brands by alias.
                </p>
              </div>
            )}
          </div>

          <footer className="dialog-footer">
            <span>The next step selects a compatible provider report.</span>
            <div className="dialog-actions">
              <button
                className="secondary-button action-link"
                type="button"
                onClick={() => onCancel?.()}
              >
                Cancel
              </button>
              <button
                className="primary-button"
                type="button"
                // TODO(api): saveDatastreamDraft + getSourceCapabilities, then
                // advance to the Configure stage (CreerFluxWizard owns this).
                onClick={() => onContinue?.({ sourceKind, connectionId })}
              >
                Continue
              </button>
            </div>
          </footer>
        </section>
      </div>
    </div>
  );
}
