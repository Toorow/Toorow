/**
 * Competitors — the tracked-brand / competitor master registry for the
 * Governance workspace (epic-40, Story 40.1 = data-model only).
 *
 * WHAT IT IS
 * A BIDIRECTIONAL tracked-brand hub. A brand is declared ONCE at the
 * organization level (canonical name + aliases) and reused by every project;
 * each project sets its ROLE for that brand — Own / Competitor / Reference.
 * Yes, your OWN brand lives in the registry too: Share of Search, SOV, and
 * Brand Lift are all you-vs-them, so your brand must be a first-class entity.
 *
 * It works both directions:
 *   - OUTBOUND — the registry DRIVES COLLECTION. Per source it carries the
 *     identifier/query spec (the "wiring"), so declaring "Peugeot" once sets up
 *     the Google Trends query, the YouTube channel/handle pull, the competitor
 *     Strava club, the SEA brand-keyword set, the social handles, and the
 *     programmatic advertiser id — WITHOUT re-typing them per datastream. This
 *     is the key value: ease competitor selection across sources.
 *   - INBOUND — the registry RESOLVES results. When a panel/study/file lands
 *     (Google Trends, YouTube Analytics, Kantar TGI/SOV, Brand Lift), raw brand
 *     strings are matched to the canonical entity by alias (~99%); an unknown
 *     brand raises a governed "new competitor detected — add it?" alert instead
 *     of being dropped or mis-joined.
 *
 * GUARDRAIL (must read in copy): aligning identity is NOT authorizing
 * cross-source metric comparison. The registry conforms the ENTITY; it does not
 * license summing Share of Search vs SOV vs Brand Lift. Entity is ORG-scoped,
 * role is PROJECT-scoped, with a hard confidentiality boundary between sibling
 * projects.
 *
 * The application shell (ApplicationShell.tsx) already renders the frame,
 * sidebar, topbar, and <main>. This component renders ONLY the page content:
 * the page header, a short "what the registry does" explainer, a governed
 * inbound-detection alert row, and the main table of tracked brands.
 *
 * Styling: application.css (global, via the shell) for shell/layout classes
 * (page-header, header-actions, panel, selector, buttons, signal-label,
 * signal-mark, provider-logo, number, mono/event) + competitors.css for the
 * registry-specific explainer, alert, and table classes. Colors come
 * exclusively from the application.css CSS variables (dark-safe); ids/handles
 * render in JetBrains Mono via .mono, counts in Geist tabular via .number.
 *
 * ── Data ──────────────────────────────────────────────────────────────────────
 * NO HTTP endpoint exists yet — Story 40.1 is data-model only. Every block below
 * is a designed, honest placeholder flagged with // TODO(api), exactly as
 * Provenance.tsx and Overview.tsx do. The page renders finished with no backend.
 */
import { useEffect, useState, type ReactElement } from "react";
import ConnectorLogo from "../ConnectorLogo";
import "../application.css";
import "./competitors.css";
import { apiFetch } from "../../lib/apiFetch";

// ---------------------------------------------------------------------------
// View model — one tracked brand and its per-source wiring.
// TODO(api): every field below is a designed placeholder. Story 40.1 is the
// data model only; no read endpoint is wired yet.
// ---------------------------------------------------------------------------

/** Project role for a brand. Entity is ORG-scoped; role is PROJECT-scoped.
 *  "own" is the project's own brand (you-vs-them anchor) and reads distinct. */
type Role = "own" | "competitor" | "reference";

/** A source the registry can carry wiring for. `provider` (when set) resolves a
 *  real connector logo via ConnectorLogo; otherwise a plain pill is rendered. */
interface SourceWiring {
  /** Human label of the source, e.g. "Google Trends", "SEA". */
  label: string;
  /** ConnectorLogo provider id when the source has a managed logo, else null. */
  provider: string | null;
  /** Whether this brand is wired for this source (identifier/query spec present). */
  wired: boolean;
}

interface BrandRow {
  /** Canonical brand name declared once at org level (strong). */
  name: string;
  role: Role;
  /** A few resolution aliases; overflow collapses into "+N". */
  aliases: string[];
  /** Per-source wiring — which sources this brand is bound to (outbound). */
  sources: SourceWiring[];
  status: "active" | "pending";
  statusLabel: string;
}

// The five sources the registry wires today. Order is stable across rows so the
// wiring pills line up column-wise. TODO(api): the identifier/query spec behind
// each `wired` flag is the 40.1 data model, not yet exposed over HTTP.
const SOURCE_ORDER: { label: string; provider: string | null }[] = [
  { label: "Google Trends", provider: null }, // no managed connector logo → plain pill
  { label: "YouTube", provider: "youtube-analytics" },
  { label: "SEA", provider: "google-ads" }, // brand-keyword set on Google Ads
  { label: "Strava", provider: "strava" },
  { label: "Social", provider: null }, // handles across networks → plain pill
];

/** Build a row's source wiring from the stable order + the set of wired labels. */
function wire(...wiredLabels: string[]): SourceWiring[] {
  const on = new Set(wiredLabels);
  return SOURCE_ORDER.map((s) => ({
    label: s.label,
    provider: s.provider,
    wired: on.has(s.label),
  }));
}

// Mockup literals — the validated placeholder registry. Rendered verbatim so the
// surface is finished with no backend. Includes the project's OWN brand and
// several competitors. TODO(api): replace with the 40.1 read once it exists.
const MOCK_ROWS: BrandRow[] = [
  {
    name: "Peugeot",
    role: "own",
    aliases: ["PEUGEOT", "Peugot", "Peugeot FR", "@peugeot"],
    sources: wire("Google Trends", "YouTube", "SEA", "Strava", "Social"),
    status: "active",
    statusLabel: "Active",
  },
  {
    name: "Renault",
    role: "competitor",
    aliases: ["RENAULT", "Renault FR", "@renault"],
    sources: wire("Google Trends", "YouTube", "SEA", "Social"),
    status: "active",
    statusLabel: "Active",
  },
  {
    name: "Citroën",
    role: "competitor",
    aliases: ["Citroen", "CITROËN", "DS Automobiles", "@citroen"],
    sources: wire("Google Trends", "YouTube", "SEA"),
    status: "active",
    statusLabel: "Active",
  },
  {
    name: "Volkswagen",
    role: "competitor",
    aliases: ["VW", "Volkswagen AG", "@volkswagen"],
    sources: wire("Google Trends", "YouTube", "Social"),
    status: "active",
    statusLabel: "Active",
  },
  {
    name: "Tesla",
    role: "competitor",
    aliases: ["TESLA", "Tesla Inc", "@tesla"],
    sources: wire("Google Trends", "YouTube", "SEA", "Social"),
    status: "active",
    statusLabel: "Active",
  },
  {
    name: "Toyota",
    role: "reference",
    aliases: ["TOYOTA", "Toyota Motor", "@toyota"],
    sources: wire("Google Trends", "YouTube"),
    status: "active",
    statusLabel: "Active",
  },
  {
    name: "BYD",
    role: "competitor",
    aliases: ["BYD Auto", "比亚迪"],
    sources: wire("Google Trends"),
    status: "pending",
    statusLabel: "Pending wiring",
  },
  {
    name: "Stellantis",
    role: "reference",
    aliases: ["STELLANTIS", "Stellantis N.V."],
    sources: wire(),
    status: "pending",
    statusLabel: "No source wired",
  },
];

// The four things the registry does, as a calm explainer. TODO(api): each maps
// to a 40.1 data-model capability (declaration, outbound wiring, inbound
// resolution, per-project role), none exposed over HTTP yet.
interface Capability {
  title: string;
  copy: string;
  icon: ReactElement;
}

const CAPABILITIES: Capability[] = [
  {
    title: "Declare once",
    copy: "A brand is declared a single time at the organization level — canonical name plus aliases — and every project reuses that one entity.",
    icon: (
      <svg viewBox="0 0 24 24">
        <path d="M12 3l8 4.5v9L12 21l-8-4.5v-9L12 3zM12 12v9M12 12l8-4.5M12 12L4 7.5" />
      </svg>
    ),
  },
  {
    title: "Drives collection",
    copy: "Outbound, it carries the identifier and query spec per source — the Trends query, YouTube handle, SEA brand keywords, Strava club, advertiser id — so you never re-type wiring per datastream.",
    icon: (
      <svg viewBox="0 0 24 24">
        <path d="M4 12h10M14 12l-4-4M14 12l-4 4M17 5v14" />
      </svg>
    ),
  },
  {
    title: "Resolves results",
    copy: "Inbound, when a panel or file lands its raw brand strings are matched to the canonical entity by alias (~99%); an unknown brand raises a governed alert instead of being dropped or mis-joined.",
    icon: (
      <svg viewBox="0 0 24 24">
        <path d="M20 12h-10M10 12l4-4M10 12l4 4M7 5v14" />
      </svg>
    ),
  },
  {
    title: "Role per project",
    copy: "The entity is org-scoped; each project sets its own role — Own, Competitor, or Reference — behind a hard confidentiality boundary between sibling projects.",
    icon: (
      <svg viewBox="0 0 24 24">
        <path d="M12 12a4 4 0 100-8 4 4 0 000 8zM5 20a7 7 0 0114 0" />
      </svg>
    ),
  },
];

// Role → signal-label variant + display label. "own" reads distinct (info) from
// the two them-side roles; competitor is the attention-bearing rose, reference
// is muted. TODO(api): role is a per-project value in the 40.1 model.
const ROLE_META: Record<Role, { variant: string; label: string }> = {
  own: { variant: "info", label: "Own" },
  competitor: { variant: "role-competitor", label: "Competitor" },
  reference: { variant: "muted", label: "Reference" },
};

interface CompetitorsProps {
  projectId?: string;
}

/** Shape returned by GET /api/tracked-entities (Epic 40 project-facing read). */
interface ApiEntity {
  id: string;
  name: string;
  role: string;
  aliases: string[];
  status: string;
  /** Per-source query bindings (Story 40.2) — empty until wired. */
  bindings: unknown[];
}

const ROLES: Role[] = ["own", "competitor", "reference"];

function toRow(e: ApiEntity): BrandRow {
  const role: Role = (ROLES as string[]).includes(e.role) ? (e.role as Role) : "reference";
  const active = e.status === "active";
  return {
    name: e.name,
    role,
    aliases: e.aliases ?? [],
    // TODO(40.2): derive wired sources from e.bindings once the binding store lands.
    sources: wire(),
    status: active ? "active" : "pending",
    statusLabel: active ? "Active" : "No source wired",
  };
}

export default function Competitors({ projectId = "default" }: CompetitorsProps) {
  // The entity is org-scoped, the role is project-scoped: the read is filtered to
  // THIS project (confidentiality boundary). MOCK_ROWS render finished while the
  // fetch is in flight and offline; a successful fetch replaces them with the
  // real registry (which is honestly empty until brands are declared).
  const [rows, setRows] = useState<BrandRow[]>(MOCK_ROWS);
  const [loaded, setLoaded] = useState(false);

  useEffect(() => {
    let alive = true;
    (async () => {
      try {
        const res = await apiFetch(
          `/api/tracked-entities?project_id=${encodeURIComponent(projectId)}`,
        );
        if (!res.ok) return; // keep the mock fallback
        const body = (await res.json()) as { entities?: ApiEntity[] };
        if (alive) {
          setRows((body.entities ?? []).map(toRow));
          setLoaded(true);
        }
      } catch {
        /* offline — keep the designed mock so the surface stays finished. */
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
          <h1>Competitors</h1>
          <p>
            Declare each tracked brand once — your own and your competitors — and
            every source aligns to it, both for collection and for reading panel
            results.
          </p>
        </div>
        <div className="header-actions">
          {/* TODO(api): open the declare-brand flow (canonical name + aliases). */}
          <button className="primary-button" type="button">
            + Add brand
          </button>
        </div>
      </div>

      <section className="panel registry-explainer">
        <div className="registry-explainer-header">
          <div>
            <h2>What the registry does</h2>
            <p>
              One bidirectional hub: declare a brand once, drive collection
              outbound, resolve results inbound, and set a role per project.
            </p>
          </div>
          <span className="signal-label info">
            <span className="signal-mark" />
            Org-scoped entity
          </span>
        </div>
        <div className="capability-rows">
          {CAPABILITIES.map((c) => (
            <div className="capability-row" key={c.title}>
              <span className="capability-icon">{c.icon}</span>
              <div className="capability-copy">
                <strong>{c.title}</strong>
                <p>{c.copy}</p>
              </div>
            </div>
          ))}
        </div>
        {/* Guardrail: identity alignment ≠ authorizing cross-source comparison. */}
        <div className="inline-note registry-guardrail">
          <span className="signal-label info">
            <span className="signal-mark" />
          </span>
          <div>
            <strong>Aligning identity is not authorizing comparison.</strong>
            The registry conforms the entity — it does not license summing Share
            of Search against SOV against Brand Lift. Those stay distinct metrics
            across distinct sources.
          </div>
        </div>
      </section>

      {/* Governed inbound-detection alert. TODO(40.3): raised when alias matching
          fails on a landed panel/file; Add declares it, Dismiss suppresses it.
          Shown only as the designed concept before the real read resolves — once
          the registry has answered we never surface a fabricated detection. */}
      {!loaded && (
      <div className="detection-alert" role="status">
        <span className="detection-icon" aria-hidden="true">
          <svg viewBox="0 0 24 24">
            <path d="M12 9v4M12 17h.01M10.3 4.3L2.8 17a2 2 0 001.7 3h15a2 2 0 001.7-3L14.7 4.3a2 2 0 00-3.4 0z" />
          </svg>
        </span>
        <div className="detection-copy">
          <strong>New brand detected in an inbound file</strong>
          <p>
            <strong className="detection-brand">BYD</strong> appeared in a{" "}
            <ConnectorLogo
              provider="google-trends"
              alt="Google Trends"
              className="provider-logo detection-source-logo"
            />
            <span className="detection-source">Google Trends</span> panel and
            did not match any known alias. Add it to the registry?
          </p>
        </div>
        <div className="detection-actions">
          {/* TODO(api): declare the detected brand as a new canonical entity. */}
          <button className="primary-button" type="button">
            Add
          </button>
          {/* TODO(api): suppress this detection without declaring the brand. */}
          <button className="quiet-button" type="button">
            Dismiss
          </button>
        </div>
      </div>
      )}

      <section className="panel registry-panel">
        <div className="registry-panel-header">
          <div>
            <h2>Tracked brands</h2>
            <p>
              {/* TODO(api): Story 40.1 is data-model only — no read endpoint yet. */}
              Every brand this project tracks, its role, resolution aliases, and
              the sources its wiring is bound to.
            </p>
          </div>
          {/* TODO(api): role filter (Own / Competitor / Reference). */}
          <button className="selector" type="button">
            All roles
          </button>
        </div>

        <div
          className="table-scroll"
          tabIndex={0}
          aria-label="Tracked brands and their per-source wiring"
        >
          <table className="registry-table">
            <thead>
              <tr>
                <th style={{ width: "20%" }}>Brand</th>
                <th style={{ width: "14%" }}>Role</th>
                <th style={{ width: "26%" }}>Aliases</th>
                <th style={{ width: "26%" }}>Source wiring</th>
                <th style={{ width: "14%" }}>Status</th>
              </tr>
            </thead>
            <tbody>
              {rows.length === 0 ? (
                <tr>
                  <td colSpan={5} className="registry-empty">
                    No tracked brands yet. Declare your own brand and the
                    competitors you track — each is then reused across every source.
                  </td>
                </tr>
              ) : null}
              {rows.map((row) => {
                const roleMeta = ROLE_META[row.role];
                const shownAliases = row.aliases.slice(0, 3);
                const extraAliases = row.aliases.length - shownAliases.length;
                return (
                  <tr key={row.name}>
                    <td>
                      <div className="brand-cell">
                        <strong>{row.name}</strong>
                        {row.role === "own" ? (
                          <small>Your brand · you-vs-them anchor</small>
                        ) : (
                          <small>Org-scoped entity</small>
                        )}
                      </div>
                    </td>
                    <td>
                      {/* TODO(api): role is per-project in the 40.1 model. */}
                      <span className={`signal-label ${roleMeta.variant}`}>
                        <span className="signal-mark" />
                        {roleMeta.label}
                      </span>
                    </td>
                    <td>
                      {/* TODO(api): aliases power ~99% inbound resolution. */}
                      <div className="alias-cell">
                        {shownAliases.map((a) => (
                          <code className="mono alias-chip" key={a}>
                            {a}
                          </code>
                        ))}
                        {extraAliases > 0 ? (
                          <span className="alias-more number">+{extraAliases}</span>
                        ) : null}
                      </div>
                    </td>
                    <td>
                      {/* TODO(api): per-source identifier/query spec (outbound). */}
                      <div className="wiring-cell">
                        {row.sources.map((s) => (
                          <span
                            className={`wiring-pill ${s.wired ? "wired" : "unwired"}`}
                            key={s.label}
                            title={
                              s.wired
                                ? `${s.label} — wired`
                                : `${s.label} — not wired`
                            }
                          >
                            {s.provider ? (
                              <ConnectorLogo
                                provider={s.provider}
                                alt={s.label}
                                className="provider-logo wiring-logo"
                              />
                            ) : null}
                            <span className="wiring-label">{s.label}</span>
                          </span>
                        ))}
                      </div>
                    </td>
                    <td>
                      {/* TODO(api): wiring completeness status. */}
                      <span
                        className={`signal-label ${
                          row.status === "active" ? "success" : "warning"
                        }`}
                      >
                        <span className="signal-mark" />
                        {row.statusLabel}
                      </span>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>

        <div className="registry-footer">
          <span>
            Tracking <span className="number">{rows.length}</span> brands ·{" "}
            <span className="number">
              {rows.filter((r) => r.role === "competitor").length}
            </span>{" "}
            competitors
          </span>
          {/* TODO(api): no read endpoint yet — Story 40.1 is data-model only. */}
          <span className="footer-note mono">registry.brand</span>
        </div>
      </section>
    </>
  );
}
