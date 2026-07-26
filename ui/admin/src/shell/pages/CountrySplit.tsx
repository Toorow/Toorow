/**
 * CountrySplit — the "Country split" module surface.
 *
 * Visual lineage:
 *   _bmad-output/planning-artifacts/ux-designs/ux-connector-2026-07-23/
 *     mockups/country-split.html
 *
 * The application shell (ApplicationShell.tsx) renders the frame, sidebar,
 * topbar, and <main className="main">. This component renders ONLY the page body.
 *
 * WHAT IS REAL, AND WHAT WAS REMOVED
 * ----------------------------------
 * Two things map to the backend, and only two:
 *   - module state + priority countries -> GET /api/projects/{id}
 *       (geographic_mode, local_market_country_codes)
 *   - country display names/flags       -> GET /api/vocabularies/countries
 *       VERIFIED against server/core/admin_api.py, which registers
 *       "/api/vocabularies/countries" -> _list_countries. There is NO
 *       /api/reference/* route in server/core; the previous
 *       /api/reference/countries call 404'd silently on every load, so country
 *       names never resolved and the page fell back to raw codes without ever
 *       saying the lookup had failed.
 *
 * Everything the mockup showed beyond that was FICTION rendered as measurement
 * and has been removed rather than kept behind a // TODO(api):
 *   - "Active" was hard-coded true; it is now derived from geographic_mode.
 *   - the FR priority-country fallback invented a tracked market for projects
 *     that track none.
 *   - "6 compatible Datastreams" (twice) and the per-Datastream exception row
 *     had no endpoint at all: no read model answers standing compatibility, only
 *     the transactional POST /api/projects/{id}/geography/preview does. The
 *     screen now points at that flow instead of inventing its result.
 *   - the "Save changes", "+ Add country" and switch controls were inert; the
 *     writable surface is Project settings, which owns the preview -> confirm
 *     contract. They are gone.
 *
 * A load failure is SAID; an empty tracked-market list is rendered as empty.
 *
 * Flags: flag-icons only (icon-only <span class="fi fi-xx">), never a hand-drawn
 * SVG. The country name is carried on aria-label.
 */
import { useCallback, useEffect, useState } from "react";
import "flag-icons/css/flag-icons.min.css";
import "../application.css";
import "./country-split.css";
import { apiFetch } from "../../lib/apiFetch";

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
  /** Back to the Data > Modules catalog (Country split is reached from there). */
  onBack?: () => void;
  /** Wired by the shell when project settings can be opened; the geography is
   *  edited there (preview -> confirm). Rendered only when provided. */
  onOpenProjectSettings?: () => void;
}

type LoadState<T> =
  | { status: "loading" }
  | { status: "error"; message: string }
  | { status: "ok"; data: T };

/**
 * CountryFlag — icon-only flag via flag-icons. The visible glyph is decorative;
 * the country name is exposed to assistive tech through aria-label/role="img".
 * ISO 3166-1 alpha-2, lowercased (e.g. "FR" -> "fr").
 */
function CountryFlag({ code, name }: { code: string; name: string }) {
  return (
    <span className={`fi fi-${code.toLowerCase()} country-flag`} role="img" aria-label={name} />
  );
}

export default function CountrySplit({
  projectId,
  onBack,
  onOpenProjectSettings,
}: CountrySplitProps) {
  const [prefs, setPrefs] = useState<LoadState<ProjectPrefs>>({ status: "loading" });
  const [countries, setCountries] = useState<LoadState<CountryOption[]>>({ status: "loading" });

  const load = useCallback(async () => {
    if (!projectId) {
      setPrefs({ status: "error", message: "no project is scoped" });
      setCountries({ status: "error", message: "no project is scoped" });
      return;
    }
    setPrefs({ status: "loading" });
    setCountries({ status: "loading" });

    try {
      const resp = await apiFetch(`/api/projects/${encodeURIComponent(projectId)}`, {
        cache: "no-store",
      });
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      setPrefs({ status: "ok", data: (await resp.json()) as ProjectPrefs });
    } catch (err) {
      setPrefs({ status: "error", message: err instanceof Error ? err.message : String(err) });
    }

    try {
      const resp = await apiFetch(`/api/vocabularies/countries`, { cache: "no-store" });
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      const data = (await resp.json()) as { countries?: CountryOption[] };
      setCountries({ status: "ok", data: data.countries ?? [] });
    } catch (err) {
      setCountries({
        status: "error",
        message: err instanceof Error ? err.message : String(err),
      });
    }
  }, [projectId]);

  useEffect(() => {
    void load();
  }, [load]);

  const project = prefs.status === "ok" ? prefs.data : null;
  const moduleActive = project?.geographic_mode === "local_markets";
  const priorityCodes = project?.local_market_country_codes ?? [];

  /** Display name for a code — falls back to the code itself, and says so when
   *  the vocabulary could not be loaded. */
  const nameFor = (code: string) =>
    (countries.status === "ok"
      ? countries.data.find((c) => c.code === code)?.display_name
      : undefined) ?? code;

  return (
    <div className="country-split-page">
      <nav className="module-breadcrumb" aria-label="Breadcrumb">
        <button type="button" className="crumb-link" onClick={() => onBack?.()}>
          Modules
        </button>
        <span className="crumb-sep" aria-hidden="true">
          ›
        </span>
        <span className="crumb-current">Country split</span>
      </nav>

      <div className="page-header">
        <div>
          <h1>Country split</h1>
          <p>
            Publish selected countries as their own reporting groups, with everything else
            grouped as Other.
          </p>
        </div>
        <div className="header-actions">
          {prefs.status === "ok" && (
            <span className={`signal-label ${moduleActive ? "success" : "info"}`}>
              <span className="signal-mark" />
              {moduleActive ? "Active" : "Inactive"}
            </span>
          )}
          {onOpenProjectSettings && (
            <button
              className="secondary-button"
              type="button"
              onClick={() => onOpenProjectSettings()}
            >
              Edit in project settings
            </button>
          )}
        </div>
      </div>

      {prefs.status === "loading" && (
        <p className="cs-status" role="status">
          Loading the module state…
        </p>
      )}

      {prefs.status === "error" && (
        <div className="cs-load-error" role="alert">
          <span className="signal-label error">
            <span className="signal-mark" />
            Could not load the module state
          </span>
          <p>
            {prefs.message}. Neither the module state nor the tracked markets are known —
            nothing below is being reported.
          </p>
          <button className="secondary-button" type="button" onClick={() => void load()}>
            Retry
          </button>
        </div>
      )}

      {prefs.status === "ok" && (
        <section className="country-settings">
          <div className="setting-row">
            <div className="setting-copy">
              <h2>Module</h2>
              <p>
                The split applies when the project reports on local markets rather than a
                single global view.
              </p>
            </div>
            <div className="setting-control">
              <span className={`signal-label ${moduleActive ? "success" : "info"}`}>
                <span className="signal-mark" />
                {moduleActive
                  ? "Country split is active (reporting mode: local markets)"
                  : "Country split is inactive (reporting mode: global)"}
              </span>
            </div>
          </div>

          <div className="setting-row">
            <div className="setting-copy">
              <h2>Priority countries</h2>
              <p>
                Selected countries are published as their own reporting groups. Everything
                else is grouped as Other.
              </p>
            </div>
            <div className="setting-control">
              {countries.status === "error" && (
                <p className="cs-inline-error" role="alert">
                  Country names could not be loaded ({countries.message}) — the codes below
                  are shown unresolved.
                </p>
              )}
              {priorityCodes.length === 0 ? (
                <p className="cs-empty">
                  No country is tracked for this project. Until a market is selected,
                  everything is published as a single group.
                </p>
              ) : (
                <div className="country-picker">
                  {priorityCodes.map((code) => (
                    <span key={code} className="country-flag-chip" title={nameFor(code)}>
                      <CountryFlag code={code} name={nameFor(code)} />
                      <span className="country-chip-name">{nameFor(code)}</span>
                    </span>
                  ))}
                  <span className="signal-label info">
                    <span className="signal-mark" />
                    {priorityCodes.length} selected
                  </span>
                </div>
              )}
            </div>
          </div>

          <div className="setting-row">
            <div className="setting-copy">
              <h2>Coverage</h2>
              <p>
                Compatibility is evaluated per Datastream from its report structure and the
                provider capability.
              </p>
            </div>
            <div className="setting-control">
              {/* No read model answers standing compatibility. The only endpoint
                  that evaluates it is the transactional geography preview in
                  Project settings, so that is what is stated — not a count. */}
              <p className="cs-empty">
                Datastream compatibility is not evaluated on this screen. It is computed
                when a geography change is previewed in Project settings, which reports the
                affected Datastreams and any blocking gaps before the change is applied.
              </p>
            </div>
          </div>
        </section>
      )}
    </div>
  );
}
