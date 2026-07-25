/**
 * CountrySplit — faithful React port of the validated Country split mockup.
 *
 * Source of visual truth:
 *   _bmad-output/planning-artifacts/ux-designs/ux-connector-2026-07-23/
 *     mockups/country-split.html
 *
 * The application shell (ApplicationShell.tsx) already renders the frame,
 * sidebar, topbar, and <main className="main">. This component renders ONLY the
 * page body that lives inside <main>: the page header (with the Active signal +
 * Save changes), and the country-settings card with its four setting rows —
 * Module activation, Priority countries, Automatic result, and Coverage.
 *
 * Styling: application.css (global, via the shell) for shell/layout classes
 * (page-header, header-actions, signal-label, signal-mark, provider-logo,
 * primary/secondary button) + country-split.css for the page-specific surfaces.
 * Colors come exclusively from the application.css CSS variables.
 *
 * Flags: rendered with the flag-icons library ONLY (icon-only <span class="fi
 * fi-xx">), never a hand-drawn SVG. The country name is carried on aria-label.
 *
 * Data: the mockup surfaces a project-wide module that (a) is active, (b) has a
 * set of priority countries published separately from "Other", (c) claims to
 * add a country dimension automatically to every compatible Datastream, and (d)
 * lists per-Datastream compatibility exceptions.
 *
 * Only two of those map to today's backend:
 *   - Priority countries  -> project.local_market_country_codes (GET /api/projects/:id)
 *   - Country display names/flags -> GET /api/reference/countries
 * Everything else (project-wide activation switch, automatic cross-Datastream
 * country derivation, the "6 compatible Datastreams" coverage count, and the
 * per-Datastream exception list) has NO backing state/endpoint and is rendered
 * from the mockup's literals, each flagged with // TODO(api). See the GAP LIST
 * returned with this port. The page renders finished with no backend.
 */
import { useEffect, useState } from "react";
import "flag-icons/css/flag-icons.min.css";
import "../application.css";
import "./country-split.css";

interface ProjectPrefs {
  id: string;
  name: string;
  geographic_mode?: "global" | "local_markets";
  local_market_country_codes?: string[];
}

interface CountryOption {
  code: string;
  display_name: string;
}

interface CountrySplitProps {
  projectId?: string;
  apiBase?: string;
  apiToken?: string;
  /** Back to the Data > Modules catalog (Country split is reached from there). */
  onBack?: () => void;
}

/**
 * CountryFlag — icon-only flag via flag-icons. The visible glyph is decorative;
 * the country name is exposed to assistive tech through aria-label/role="img".
 * ISO 3166-1 alpha-2, lowercased (e.g. "FR" -> "fr").
 */
function CountryFlag({ code, name }: { code: string; name: string }) {
  return (
    <span
      className={`fi fi-${code.toLowerCase()} country-flag`}
      role="img"
      aria-label={name}
    />
  );
}

export default function CountrySplit({
  projectId = "default",
  apiBase = "",
  apiToken = "",
  onBack,
}: CountrySplitProps) {
  const [prefs, setPrefs] = useState<ProjectPrefs | null>(null);
  const [countryOptions, setCountryOptions] = useState<CountryOption[]>([]);

  useEffect(() => {
    const headers: Record<string, string> = {};
    if (apiToken) headers["Authorization"] = `Bearer ${apiToken}`;

    async function load() {
      try {
        const resp = await fetch(
          `${apiBase}/api/projects/${encodeURIComponent(projectId)}`,
          { headers }
        );
        if (resp.ok) setPrefs((await resp.json()) as ProjectPrefs);
      } catch {
        /* leave prefs null -> fall back to mockup literals */
      }
      try {
        const resp = await fetch(`${apiBase}/api/reference/countries`, { headers });
        if (resp.ok) {
          const data = await resp.json();
          setCountryOptions((data.countries ?? []) as CountryOption[]);
        }
      } catch {
        /* leave country options empty -> fall back to literal names */
      }
    }
    void load();
  }, [projectId, apiBase, apiToken]);

  // Real map: priority countries = project.local_market_country_codes. When the
  // API has not answered (or the project has none set), fall back to the
  // mockup's single priority country, France.
  const priorityCodes =
    prefs?.local_market_country_codes && prefs.local_market_country_codes.length > 0
      ? prefs.local_market_country_codes
      : ["FR"]; // TODO(api): mockup literal until a project has local markets set

  const nameFor = (code: string) =>
    countryOptions.find((c) => c.code === code)?.display_name ?? code;

  // The primary selected country drives the flow card + the "Selected" group
  // label in the mockup (single-country layout). Additional selections still
  // render as flag chips in the picker.
  const primaryCode = priorityCodes[0];
  const primaryName = nameFor(primaryCode);
  const selectedCount = priorityCodes.length;

  // TODO(api): project-wide activation has no backing field today. The mockup
  // shows the module as active; there is no `country_split_active` on the
  // project. Treated as always-active to match the validated surface.
  const moduleActive = true;

  // TODO(api): automatic cross-Datastream coverage count. There is no endpoint
  // that returns "N compatible Datastreams" for a project without POSTing a
  // geography preview. Mockup literal.
  const compatibleCount = 6;

  // TODO(api): per-Datastream compatibility exceptions. The geography preview
  // returns blocked datastreams, but only transactionally on a proposed change;
  // there is no standing exception list for an active module. Mockup literal.
  const exceptions = [
    {
      provider: "Meta",
      logo: "/connectors/meta.svg",
      name: "Account insights",
      reason: "Country breakdown is not supported by this report structure.",
    },
  ];

  return (
    <div className="country-split-page">
      <nav className="module-breadcrumb" aria-label="Breadcrumb">
        <button type="button" className="crumb-link" onClick={() => onBack?.()}>
          Modules
        </button>
        <span className="crumb-sep" aria-hidden="true">›</span>
        <span className="crumb-current">Country split</span>
      </nav>
      <div className="page-header">
        <div>
          <h1>Country split</h1>
          <p>
            Automatically add a country dimension to compatible Datastreams and
            publish selected countries separately from Other.
          </p>
        </div>
        <div className="header-actions">
          <span className="signal-label success">
            <span className="signal-mark" />
            {moduleActive ? "Active" : "Inactive"}
          </span>
          {/* TODO(api): persist module + priority-country changes */}
          <button className="primary-button">Save changes</button>
        </div>
      </div>

      <section className="country-settings">
        <div className="setting-row">
          <div className="setting-copy">
            <h2>Module</h2>
            <p>
              Apply the split automatically to every compatible Datastream in
              this project.
            </p>
          </div>
          <div className="setting-control">
            {/* TODO(api): no `country_split_active` field on the project yet */}
            <div className="switch-control" role="switch" aria-checked={moduleActive}>
              <span className="switch-track" />
              Country split is active
            </div>
          </div>
        </div>

        <div className="setting-row">
          <div className="setting-copy">
            <h2>Priority countries</h2>
            <p>
              Selected countries are published as their own reporting groups.
              Everything else is grouped as Other.
            </p>
          </div>
          <div className="setting-control">
            <div className="country-picker">
              {priorityCodes.map((code) => (
                <button
                  key={code}
                  className="country-flag-button"
                  aria-label={`${nameFor(code)} selected`}
                  title={nameFor(code)}
                >
                  <CountryFlag code={code} name={nameFor(code)} />
                </button>
              ))}
              {/* TODO(api): open country picker + persist local_market_country_codes */}
              <button className="secondary-button">+ Add country</button>
              <span className="signal-label info">
                <span className="signal-mark" />
                {selectedCount} selected
              </span>
            </div>
          </div>
        </div>

        <div className="setting-row">
          <div className="setting-copy">
            <h2>Automatic result</h2>
            <p>No country assignment is required inside individual Datastreams.</p>
          </div>
          <div className="setting-control">
            <div className="country-flow">
              <div className="flow-card">
                <span>Project coverage</span>
                {/* TODO(api): compatible-Datastream count */}
                <strong>{compatibleCount} compatible Datastreams</strong>
              </div>
              <div className="flow-arrow">→</div>
              <div className="flow-card">
                <span>Module action</span>
                <strong>Add country</strong>
              </div>
              <div className="flow-arrow">→</div>
              <div className="flow-groups">
                <div className="flow-group france">
                  <span>Selected</span>
                  <strong>{primaryName}</strong>
                </div>
                <div className="flow-group other">
                  <span>Remainder</span>
                  <strong>Other</strong>
                </div>
              </div>
            </div>
          </div>
        </div>

        <div className="setting-row">
          <div className="setting-copy">
            <h2>Coverage</h2>
            <p>
              Compatibility is evaluated from each report structure and provider
              capability.
            </p>
          </div>
          <div className="setting-control">
            {/* TODO(api): count of Datastreams that will update automatically */}
            <div className="signal-label success">
              <span className="signal-mark" />
              {compatibleCount} Datastreams will update automatically
            </div>
            {/* TODO(api): standing per-Datastream compatibility exceptions */}
            {exceptions.map((ex) => (
              <div className="exception-row" key={ex.name}>
                <div className="consumer-cell">
                  <span className="provider-logo">
                    <img src={ex.logo} alt={ex.provider} />
                  </span>
                  <div>
                    <strong>{ex.name}</strong>
                    <small>{ex.reason}</small>
                  </div>
                </div>
                <span className="signal-label warning">
                  <span className="signal-mark" />
                  {exceptions.length} exception{exceptions.length === 1 ? "" : "s"}
                </span>
              </div>
            ))}
          </div>
        </div>
      </section>
    </div>
  );
}
