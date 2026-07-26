/**
 * ProjectSettings — the focused "Project settings" screen for the v3 admin shell.
 *
 * Reached from the TopBar scope-actions menu ("Project settings", TopBar.tsx) for
 * the currently-scoped project. Rendered inside the shell <main> (ApplicationShell
 * renders the frame/sidebar/topbar); this component is the page body only.
 *
 * ⚠️ SPEC-DERIVED, NO MOCKUP.
 * There is no validated visual mockup for project settings. This screen is a
 * faithful v3-shell port of the REAL, already-shipped settings implementation
 * (ui/admin/src/ProjectSettingsPage.tsx, MUI) plus the Epic 21 multi-tenant
 * contract (org owns projects; a project has a display name, a default currency,
 * a verification source, and a geographic mode). Every field, endpoint and
 * behavior below is REUSED from that real implementation — nothing is invented.
 * When a mockup lands, reconcile this port against it.
 *
 * Sections (all from the real implementation):
 *   1. Project identity   — id (read-only, mono), display name, default currency
 *   2. Verification source — type (none / GA4 / Stripe / custom), datastream id,
 *                            and (GA4 only) the lead event name
 *   3. Geographic mode     — Global vs Local markets + tracked-country selection
 *
 * Backend (REAL, wired — identical to ProjectSettingsPage):
 *   GET   /api/projects/{projectId}
 *           -> { id, name, slug, currency?, timezone?,
 *                verification_source_type?, verification_source_id?,
 *                lead_event_name?, geographic_mode?, local_market_country_codes? }
 *   GET   /api/vocabularies/countries       -> { countries: [{ code, display_name }] }
 *           (verified: server/core/admin_api.py registers "/api/vocabularies/
 *            countries" -> _list_countries. There is NO /api/reference/* route
 *            anywhere in server/core — the previous /api/reference/countries URL
 *            404'd on every call, so the market picker was structurally empty and
 *            said nothing about it. OrgSettings.tsx already used the right URL.)
 *   PATCH /api/projects/{projectId}
 *           body { verification_source_type, verification_source_id,
 *                  lead_event_name }        (identity-only saves send name/currency)
 *   POST  /api/projects/{projectId}/geography/preview
 *           header Idempotency-Key
 *           body  { geographic_mode, local_market_country_codes }
 *           -> { id, impact: { affected_datastream_count, blocking_gap_count,
 *                              backfill_required, datastreams[] } }
 *   POST  /api/projects/{projectId}/geography/previews/{previewId}/confirm
 *           body  { backfill_decision: "defer" | "request" }
 *   A geography change ALWAYS goes preview -> confirm (with the backfill decision
 *   when backfill_required); identity/verification changes PATCH directly.
 *
 * Failure handling: a load or save failure is SAID. The project fetch failing
 * renders an explicit error instead of an empty form; the country list failing
 * renders an explicit error instead of an empty picker (an empty picker reads as
 * "no countries exist"); and an empty list returned by the API is rendered as
 * empty, which is a truth.
 *
 * Styling: application.css (global, via the shell) for base classes/tokens +
 * project-settings.css for this page's specifics. Colors come exclusively from
 * the application.css CSS variables — no hex. Identifiers use JetBrains Mono
 * (.mono, defined in application.css).
 */
import { useCallback, useEffect, useMemo, useState } from "react";
import "../application.css";
import "./project-settings.css";
import { apiFetch } from "../../lib/apiFetch";

/* ---- Data model (REUSED verbatim from ProjectSettingsPage.tsx) ------------- */

interface ProjectPrefs {
  id: string;
  name: string;
  slug: string;
  currency?: string;
  timezone?: string;
  verification_source_type?: string | null;
  verification_source_id?: string | null;
  lead_event_name?: string | null;
  geographic_mode?: "global" | "local_markets";
  local_market_country_codes?: string[];
}

interface CountryOption {
  code: string;
  display_name: string;
}

interface GeographicPreviewImpactDatastream {
  datastream_id: string;
  datastream_name?: string;
  compatibility: "compatible" | "blocked";
  coverage_state: string;
  earliest_available_country_date?: string | null;
  max_provider_backfill_days?: number | null;
  estimated_volume?: { upper_bound_rows?: number };
  estimated_cost?: Record<string, unknown>;
  blocking_gaps?: Array<{ code: string; message: string }>;
}

interface GeographicPreviewImpact {
  affected_datastream_count: number;
  blocking_gap_count: number;
  backfill_required: boolean;
  datastreams: GeographicPreviewImpactDatastream[];
}

interface GeographicPreviewResponse {
  id: string;
  impact: GeographicPreviewImpact;
}

/** Verification-source types — REUSED from ProjectSettingsPage (English copy). */
const VS_TYPE_OPTIONS = [
  { value: "", label: "No verification source" },
  { value: "ga4", label: "Google Analytics 4 (GA4)" },
  { value: "stripe", label: "Stripe (Billing & Subscriptions)" },
  { value: "custom", label: "Custom connection" },
] as const;

type GeoMode = "global" | "local_markets";
type BackfillDecision = "defer" | "request";

export interface ProjectSettingsProps {
  /** The scoped project whose settings are edited. Falls back to a placeholder
   *  so the screen renders finished when the shell hasn't wired a scope yet. */
  projectId?: string;
}

export default function ProjectSettings({ projectId }: ProjectSettingsProps) {
  // A stable, non-empty id so the screen renders finished with no scope wired.
  const resolvedProjectId = projectId?.trim() || "project-current";

  const [prefs, setPrefs] = useState<ProjectPrefs | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // Editable field state (mirrors the real page's local state).
  const [name, setName] = useState<string>("");
  const [currency, setCurrency] = useState<string>("");
  const [vsType, setVsType] = useState<string>("");
  const [vsId, setVsId] = useState<string>("");
  const [leadEventName, setLeadEventName] = useState<string>("");
  const [geoMode, setGeoMode] = useState<GeoMode>("global");
  const [countryCodes, setCountryCodes] = useState<string[]>([]);

  const [countryOptions, setCountryOptions] = useState<CountryOption[]>([]);
  const [countriesLoading, setCountriesLoading] = useState(false);
  const [countriesError, setCountriesError] = useState<string | null>(null);
  /** True once a country-list request has been ISSUED — so a failed load is not
   *  retried in a loop by the lazy-load effect, and so "no options" can be told
   *  apart from "not asked yet". */
  const [countriesRequested, setCountriesRequested] = useState(false);
  const [countryQuery, setCountryQuery] = useState<string>("");

  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [saveSuccess, setSaveSuccess] = useState(false);

  const [geographicPreview, setGeographicPreview] = useState<GeographicPreviewResponse | null>(null);
  const [backfillDecision, setBackfillDecision] = useState<BackfillDecision>("defer");

  const clearSaveState = useCallback(() => {
    setSaveSuccess(false);
    setSaveError(null);
  }, []);

  const applyUpdatedPrefs = useCallback((updated: ProjectPrefs) => {
    setPrefs(updated);
    setName(updated.name ?? "");
    setCurrency(updated.currency ?? "");
    setVsType(updated.verification_source_type ?? "");
    setVsId(updated.verification_source_id ?? "");
    setLeadEventName(updated.lead_event_name ?? "");
    setGeoMode(updated.geographic_mode ?? "global");
    setCountryCodes(updated.local_market_country_codes ?? []);
  }, []);

  const loadPrefs = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const resp = await apiFetch(`/api/projects/${encodeURIComponent(resolvedProjectId)}`, {
        headers: { "Content-Type": "application/json" },
        cache: "no-store",
      });
      if (!resp.ok) {
        const errData = (await resp.json().catch(() => null)) as { message?: string } | null;
        throw new Error(errData?.message ?? `HTTP ${resp.status}`);
      }
      const data = (await resp.json()) as ProjectPrefs;
      applyUpdatedPrefs(data);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Could not load project settings.");
    } finally {
      setLoading(false);
    }
  }, [resolvedProjectId, applyUpdatedPrefs]);

  const loadCountries = useCallback(async () => {
    setCountriesRequested(true);
    setCountriesLoading(true);
    setCountriesError(null);
    try {
      const resp = await apiFetch(`/api/vocabularies/countries`, {
        headers: { "Content-Type": "application/json" },
        cache: "no-store",
      });
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      const data = (await resp.json()) as { countries?: CountryOption[] };
      setCountryOptions(data.countries ?? []);
    } catch (err) {
      setCountriesError(err instanceof Error ? err.message : "Could not load the country list.");
      setCountryOptions([]);
    } finally {
      setCountriesLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadPrefs();
  }, [loadPrefs]);

  // Load the country list lazily once local markets is selected.
  useEffect(() => {
    if (geoMode === "local_markets" && !countriesRequested) {
      void loadCountries();
    }
  }, [geoMode, countriesRequested, loadCountries]);

  const countryLabel = useCallback(
    (option: CountryOption) => `${option.display_name} (${option.code})`,
    [],
  );

  /** Selected countries resolved to their option (so a code with no loaded
   *  option still renders as a removable chip). */
  const selectedCountries = useMemo<CountryOption[]>(
    () =>
      countryCodes.map((code) => countryOptions.find((c) => c.code === code) ?? { code, display_name: code }),
    [countryCodes, countryOptions],
  );

  /** Options offered by the country autocomplete: not-yet-selected, matching the
   *  typed query on code or name. */
  const countrySuggestions = useMemo<CountryOption[]>(() => {
    const q = countryQuery.trim().toLowerCase();
    return countryOptions
      .filter((o) => !countryCodes.includes(o.code))
      .filter((o) => q === "" || o.code.toLowerCase().includes(q) || o.display_name.toLowerCase().includes(q))
      .slice(0, 8);
  }, [countryOptions, countryCodes, countryQuery]);

  const addCountry = useCallback((code: string) => {
    setCountryCodes((prev) => (prev.includes(code) ? prev : [...prev, code].sort()));
    setCountryQuery("");
    clearSaveState();
  }, [clearSaveState]);

  const removeCountry = useCallback((code: string) => {
    setCountryCodes((prev) => prev.filter((c) => c !== code));
    clearSaveState();
  }, [clearSaveState]);

  /** Did the geography (mode or set of countries) change vs. what's persisted?
   *  A geography change routes through preview -> confirm, everything else PATCHes. */
  const geographyChanged = useMemo(() => {
    const persisted = [...(prefs?.local_market_country_codes ?? [])].sort();
    const selected = [...countryCodes].sort();
    return (
      geoMode !== (prefs?.geographic_mode ?? "global") ||
      selected.join(",") !== persisted.join(",")
    );
  }, [prefs, geoMode, countryCodes]);

  const handleSave = useCallback(async () => {
    if (geoMode === "local_markets" && countryCodes.length === 0) {
      setSaveSuccess(false);
      setSaveError("Select at least one local market.");
      return;
    }

    setSaving(true);
    setSaveError(null);
    setSaveSuccess(false);

    try {
      if (geographyChanged) {
        const resp = await apiFetch(
          `/api/projects/${encodeURIComponent(resolvedProjectId)}/geography/preview`,
          {
            method: "POST",
            headers: {
              "Content-Type": "application/json",
              "Idempotency-Key": globalThis.crypto?.randomUUID?.() ?? `geography-${Date.now()}`,
            },
            body: JSON.stringify({
              geographic_mode: geoMode,
              local_market_country_codes: countryCodes,
            }),
          },
        );
        if (!resp.ok) {
          const errData = (await resp.json().catch(() => null)) as { message?: string } | null;
          throw new Error(errData?.message ?? `HTTP ${resp.status}`);
        }
        const preview = (await resp.json()) as GeographicPreviewResponse;
        setBackfillDecision("defer");
        setGeographicPreview(preview);
        return; // confirmation continues in confirmGeographicChange
      }

      const resp = await apiFetch(`/api/projects/${encodeURIComponent(resolvedProjectId)}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          name: name.trim() || null,
          currency: currency.trim() || null,
          verification_source_type: vsType || null,
          verification_source_id: vsId.trim() || null,
          lead_event_name: vsType === "ga4" ? leadEventName.trim() || null : null,
        }),
      });
      if (!resp.ok) {
        const errData = (await resp.json().catch(() => null)) as { message?: string } | null;
        throw new Error(errData?.message ?? `HTTP ${resp.status}`);
      }
      const updated = (await resp.json()) as ProjectPrefs;
      applyUpdatedPrefs(updated);
      setSaveSuccess(true);
    } catch (err) {
      setSaveError(err instanceof Error ? err.message : "Could not save the project settings.");
    } finally {
      setSaving(false);
    }
  }, [
    geoMode,
    countryCodes,
    geographyChanged,
    resolvedProjectId,
    name,
    currency,
    vsType,
    vsId,
    leadEventName,
    applyUpdatedPrefs,
  ]);

  const confirmGeographicChange = useCallback(async () => {
    if (!geographicPreview) return;
    setSaving(true);
    setSaveError(null);
    try {
      const resp = await apiFetch(
        `/api/projects/${encodeURIComponent(resolvedProjectId)}/geography/previews/${encodeURIComponent(geographicPreview.id)}/confirm`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ backfill_decision: backfillDecision }),
        },
      );
      if (!resp.ok) {
        const errData = (await resp.json().catch(() => null)) as { message?: string } | null;
        throw new Error(errData?.message ?? `HTTP ${resp.status}`);
      }
      const updated = (await resp.json()) as ProjectPrefs;
      applyUpdatedPrefs(updated);
      setGeographicPreview(null);
      setSaveSuccess(true);
    } catch (err) {
      setSaveError(err instanceof Error ? err.message : "Could not confirm the geography change.");
    } finally {
      setSaving(false);
    }
  }, [geographicPreview, resolvedProjectId, backfillDecision, applyUpdatedPrefs]);

  const blockingGaps = geographicPreview?.impact.blocking_gap_count ?? 0;

  return (
    <div className="full-main projectsettings">
      <header className="page-header">
        <div>
          <h1>Project settings</h1>
          <p>
            Preferences for the scoped project: its identity, the source of truth used to verify
            conversions, and the geographic dimension of its reporting.
          </p>
        </div>
      </header>

      {loading ? (
        <div className="ps-loading" role="status">
          <span className="signal running" aria-hidden="true" />
          <span>Loading project settings…</span>
        </div>
      ) : error && !prefs ? (
        <div className="ps-inline-error" role="alert">
          <span className="signal-label error">
            <span className="signal-mark" />
            Could not load project settings
          </span>
          <p>{error}</p>
        </div>
      ) : (
        <div className="ps-sections">
          {/* ---- 1. Project identity ------------------------------------- */}
          <section className="panel ps-panel" aria-labelledby="ps-identity-title">
            <div className="section-header">
              <div>
                <h2 id="ps-identity-title">Project identity</h2>
                <p>The project&apos;s display name and the currency reports default to.</p>
              </div>
            </div>
            <div className="ps-panel-body">
              <div className="ps-field">
                <label htmlFor="ps-id">Project ID</label>
                <div className="ps-readonly mono" id="ps-id">
                  {prefs?.id ?? resolvedProjectId}
                </div>
                <p className="field-hint">Immutable identifier. Used in API calls and warehouse scoping.</p>
              </div>

              <div className="ps-field">
                <label htmlFor="ps-name">Display name</label>
                <input
                  id="ps-name"
                  className="text-input"
                  type="text"
                  value={name}
                  placeholder="Project name"
                  maxLength={120}
                  onChange={(e) => {
                    setName(e.target.value);
                    clearSaveState();
                  }}
                />
                <p className="field-hint">Shown to members across the workspace. You can rename it later.</p>
              </div>

              <div className="ps-field">
                <label htmlFor="ps-currency">Default currency</label>
                <input
                  id="ps-currency"
                  className="text-input ps-currency-input mono"
                  type="text"
                  value={currency}
                  placeholder="EUR"
                  maxLength={3}
                  autoComplete="off"
                  spellCheck={false}
                  onChange={(e) => {
                    setCurrency(e.target.value.toUpperCase().replace(/[^A-Z]/g, ""));
                    clearSaveState();
                  }}
                />
                <p className="field-hint">ISO 4217 code (e.g. EUR, USD). Reports convert to this currency.</p>
              </div>
            </div>
          </section>

          {/* ---- 2. Verification source ---------------------------------- */}
          <section className="panel ps-panel" aria-labelledby="ps-verification-title">
            <div className="section-header">
              <div>
                <h2 id="ps-verification-title">Verification source</h2>
                <p>The source of truth used to deduplicate and verify estimated conversions.</p>
              </div>
            </div>
            <div className="ps-panel-body">
              <div className="ps-field">
                <label htmlFor="ps-vs-type">Source type</label>
                <select
                  id="ps-vs-type"
                  className="ps-select"
                  value={vsType}
                  onChange={(e) => {
                    setVsType(e.target.value);
                    if (e.target.value !== "ga4") setLeadEventName("");
                    clearSaveState();
                  }}
                >
                  {VS_TYPE_OPTIONS.map((opt) => (
                    <option key={opt.value} value={opt.value}>
                      {opt.label}
                    </option>
                  ))}
                </select>
                {vsType === "stripe" && (
                  <p className="field-hint">Note: Stripe becomes available after story 15.7.</p>
                )}
              </div>

              {vsType && (
                <div className="ps-field">
                  <label htmlFor="ps-vs-id">Datastream identifier</label>
                  <input
                    id="ps-vs-id"
                    className="text-input mono"
                    type="text"
                    value={vsId}
                    placeholder="ds_xxxxxxxxxxxxxxxx"
                    autoComplete="off"
                    spellCheck={false}
                    onChange={(e) => {
                      setVsId(e.target.value);
                      clearSaveState();
                    }}
                  />
                  <p className="field-hint">The datastream that carries this verification source.</p>
                </div>
              )}

              {vsType === "ga4" && (
                <div className="ps-field">
                  <label htmlFor="ps-lead-event">Lead event name (GA4)</label>
                  <input
                    id="ps-lead-event"
                    className="text-input mono"
                    type="text"
                    value={leadEventName}
                    placeholder="generate_lead"
                    autoComplete="off"
                    spellCheck={false}
                    onChange={(e) => {
                      setLeadEventName(e.target.value);
                      clearSaveState();
                    }}
                  />
                  <p className="field-hint">The GA4 event that counts as a verified lead.</p>
                </div>
              )}
            </div>
          </section>

          {/* ---- 3. Geographic mode -------------------------------------- */}
          <section className="panel ps-panel" aria-labelledby="ps-geo-title">
            <div className="section-header">
              <div>
                <h2 id="ps-geo-title">Geographic dimension</h2>
                <p>Global consolidates reporting with no per-country view. Local markets prepares
                  adding the country to future datastream plans.</p>
              </div>
            </div>
            <div className="ps-panel-body">
              <fieldset className="ps-radio-group">
                <legend className="ps-legend">Reporting mode</legend>
                <label className={`ps-radio${geoMode === "global" ? " selected" : ""}`}>
                  <input
                    type="radio"
                    name="ps-geo-mode"
                    value="global"
                    checked={geoMode === "global"}
                    onChange={() => {
                      setGeoMode("global");
                      setCountryCodes([]);
                      clearSaveState();
                    }}
                  />
                  <span className="ps-radio-body">
                    <span className="ps-radio-title">Global</span>
                    <span className="ps-radio-note">One consolidated view. No per-country breakdown.</span>
                  </span>
                </label>
                <label className={`ps-radio${geoMode === "local_markets" ? " selected" : ""}`}>
                  <input
                    type="radio"
                    name="ps-geo-mode"
                    value="local_markets"
                    checked={geoMode === "local_markets"}
                    onChange={() => {
                      setGeoMode("local_markets");
                      clearSaveState();
                    }}
                  />
                  <span className="ps-radio-body">
                    <span className="ps-radio-title">Local markets</span>
                    <span className="ps-radio-note">Track a set of countries. Adds the country dimension to future plans.</span>
                  </span>
                </label>
              </fieldset>

              {geoMode === "local_markets" && (
                <div className="ps-field ps-countries">
                  <label htmlFor="ps-country-input">Tracked markets</label>

                  {selectedCountries.length > 0 && (
                    <ul className="ps-chips" aria-label="Selected markets">
                      {selectedCountries.map((c) => (
                        <li key={c.code} className="ps-chip">
                          <span>{countryLabel(c)}</span>
                          <button
                            type="button"
                            className="ps-chip-remove"
                            aria-label={`Remove ${c.display_name}`}
                            onClick={() => removeCountry(c.code)}
                          >
                            ×
                          </button>
                        </li>
                      ))}
                    </ul>
                  )}

                  <div className="ps-autocomplete">
                    <input
                      id="ps-country-input"
                      className="text-input"
                      type="text"
                      value={countryQuery}
                      placeholder={
                        countriesLoading
                          ? "Loading countries…"
                          : countriesError
                            ? "Country list unavailable"
                            : countryOptions.length === 0
                              ? "No country available"
                              : "Add a country"
                      }
                      autoComplete="off"
                      disabled={countriesLoading || !!countriesError || countryOptions.length === 0}
                      onChange={(e) => setCountryQuery(e.target.value)}
                    />
                    {countryQuery.trim() !== "" && countrySuggestions.length > 0 && (
                      <ul className="ps-suggestions" role="listbox" aria-label="Country suggestions">
                        {countrySuggestions.map((o) => (
                          <li key={o.code}>
                            <button type="button" role="option" aria-selected="false" onClick={() => addCountry(o.code)}>
                              <span>{o.display_name}</span>
                              <span className="mono ps-suggestion-code">{o.code}</span>
                            </button>
                          </li>
                        ))}
                      </ul>
                    )}
                  </div>

                  {/* A country list that failed to load must never look like a
                      country list that is empty. */}
                  {countriesError ? (
                    <p className="field-error" role="alert">
                      Could not load the country list ({countriesError}). The picker below is
                      unavailable — this is a loading failure, not an empty reference list.{" "}
                      <button
                        type="button"
                        className="quiet-button"
                        onClick={() => void loadCountries()}
                      >
                        Retry
                      </button>
                    </p>
                  ) : !countriesLoading && countryOptions.length === 0 ? (
                    <p className="field-hint">
                      The reference country list came back empty. No market can be selected
                      until it is populated.
                    </p>
                  ) : (
                    <p className="field-hint">
                      Type to search the reference country list. At least one market is required.
                    </p>
                  )}
                </div>
              )}
            </div>
          </section>

          {/* ---- Save bar ------------------------------------------------ */}
          <div className="ps-savebar">
            <div className="ps-savebar-status" aria-live="polite">
              {saveError && (
                <span className="signal-label error">
                  <span className="signal-mark" />
                  {saveError}
                </span>
              )}
              {saveSuccess && !saveError && (
                <span className="signal-label success">
                  <span className="signal-mark" />
                  Settings saved.
                </span>
              )}
              {!saveError && !saveSuccess && geographyChanged && (
                <span className="ps-savebar-hint">
                  A geography change is previewed before it is applied.
                </span>
              )}
            </div>
            <button
              className="primary-button"
              type="button"
              onClick={() => void handleSave()}
              disabled={saving || loading}
            >
              {saving ? "Saving…" : geographyChanged ? "Preview change" : "Save changes"}
            </button>
          </div>
        </div>
      )}

      {/* ---- Geography preview -> confirm dialog ---------------------------- */}
      {geographicPreview && (
        <div className="ps-scrim" role="presentation">
          <section
            className="ps-dialog"
            role="dialog"
            aria-modal="true"
            aria-labelledby="ps-geo-preview-title"
          >
            <header className="ps-dialog-header">
              <h2 id="ps-geo-preview-title">Preview geography change</h2>
              <p>Review the impact before applying the new geographic dimension.</p>
            </header>

            <div className="ps-dialog-body">
              <div className="scope-summary" aria-label="Change impact">
                <div className="scope-row">
                  <span className="scope-key">Affected datastreams</span>
                  <span className="scope-val">{geographicPreview.impact.affected_datastream_count}</span>
                </div>
                <div className="scope-row">
                  <span className="scope-key">Blocking gaps</span>
                  <span className="scope-val">
                    {blockingGaps > 0 ? (
                      <span className="signal-label error">
                        <span className="signal-mark" />
                        {blockingGaps}
                      </span>
                    ) : (
                      <span className="signal-label success">
                        <span className="signal-mark" />
                        None
                      </span>
                    )}
                  </span>
                </div>
                <div className="scope-row">
                  <span className="scope-key">Backfill</span>
                  <span className="scope-val">
                    {geographicPreview.impact.backfill_required ? "Required" : "Not required"}
                  </span>
                </div>
              </div>

              {blockingGaps > 0 && (
                <div className="ps-inline-error" role="alert">
                  <span className="signal-label error">
                    <span className="signal-mark" />
                    {blockingGaps} incompatibilit{blockingGaps === 1 ? "y" : "ies"} block this change
                  </span>
                  <p>Resolve the blocking gaps on the affected datastreams before confirming.</p>
                </div>
              )}

              {geographicPreview.impact.datastreams.length > 0 && (
                <ul className="ps-impact-list" aria-label="Affected datastreams">
                  {geographicPreview.impact.datastreams.map((ds) => (
                    <li key={ds.datastream_id} className="ps-impact-row">
                      <span className="ps-impact-name">
                        {ds.datastream_name ?? <span className="mono">{ds.datastream_id}</span>}
                      </span>
                      <span
                        className={`signal-label ${ds.compatibility === "blocked" ? "error" : "success"}`}
                      >
                        <span className="signal-mark" />
                        {ds.compatibility === "blocked" ? "Blocked" : "Compatible"}
                      </span>
                    </li>
                  ))}
                </ul>
              )}

              {geographicPreview.impact.backfill_required && (
                <fieldset className="ps-radio-group">
                  <legend className="ps-legend">History handling</legend>
                  <label className={`ps-radio${backfillDecision === "request" ? " selected" : ""}`}>
                    <input
                      type="radio"
                      name="ps-backfill"
                      value="request"
                      checked={backfillDecision === "request"}
                      onChange={() => setBackfillDecision("request")}
                    />
                    <span className="ps-radio-body">
                      <span className="ps-radio-title">Request backfill</span>
                      <span className="ps-radio-note">Backfill history through the governed workflow.</span>
                    </span>
                  </label>
                  <label className={`ps-radio${backfillDecision === "defer" ? " selected" : ""}`}>
                    <input
                      type="radio"
                      name="ps-backfill"
                      value="defer"
                      checked={backfillDecision === "defer"}
                      onChange={() => setBackfillDecision("defer")}
                    />
                    <span className="ps-radio-body">
                      <span className="ps-radio-title">Defer backfill</span>
                      <span className="ps-radio-note">Apply now and show partial coverage until backfilled.</span>
                    </span>
                  </label>
                </fieldset>
              )}
            </div>

            <footer className="ps-dialog-footer">
              <button
                className="secondary-button action-link"
                type="button"
                onClick={() => setGeographicPreview(null)}
                disabled={saving}
              >
                Cancel
              </button>
              <button
                className="primary-button"
                type="button"
                onClick={() => void confirmGeographicChange()}
                disabled={saving || blockingGaps > 0}
              >
                {saving ? "Confirming…" : "Confirm change"}
              </button>
            </footer>
          </section>
        </div>
      )}
    </div>
  );
}
