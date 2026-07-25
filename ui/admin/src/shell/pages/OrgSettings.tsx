/**
 * OrgSettings — the v3 admin shell "Organization settings" page.
 *
 * Reached from the TopBar scope actions menu ("Organization settings" /
 * "Members and roles"). It renders ONLY the page content inside the shell's
 * <main> (ApplicationShell.tsx already draws the frame/sidebar/topbar).
 *
 * ⚠️ SPEC-DERIVED, NO MOCKUP. There is no validated visual mockup for the
 * organization-settings screen. It is derived FAITHFULLY from the Epic 21
 * org-tenancy spec + the REAL backend (server/core/admin_api.py), NOT
 * free-invented. Every field and endpoint below maps to a real one; no field
 * is added that the API does not carry.
 *
 * Real backend contract (server/core/admin_api.py):
 *   GET   /api/organizations/{org_id}
 *           -> { id, name, slug, status, billing_ref, created_at, updated_at,
 *                archived_at, brand_primary, brand_secondary, brand_accent,
 *                logo_url }
 *   PATCH /api/organizations/{org_id}
 *           accepts: name (1..100), billing_ref (nullable string),
 *                    brand_primary/secondary/accent (#RRGGBB or null),
 *                    logo_url (nullable string).
 *           slug is IMMUTABLE: any PATCH carrying "slug" returns
 *           422 { code: "slug_immutable" } — the slug names the org's
 *           warehouse datasets org_<wslug>_raw / org_<wslug>_marts (epic 24).
 *   Members & roles are owned by the org (owner/admin/member/viewer) and are
 *   managed by the REAL OrgDetailPanel (GET/POST/DELETE/PATCH
 *   /api/organizations/{org_id}/members), which also carries the auth-credential
 *   and BigQuery data-access grant sub-panels. We REUSE it here rather than
 *   reimplement the members table.
 *   Countries/markets are NOT an org column: the canonical country vocabulary
 *   is read from GET /api/vocabularies/countries and the market reporting layer
 *   is configured per PROJECT (geographic posture, epic 37). This section
 *   surfaces that faithfully and does not invent an org-level countries field.
 *
 * Fallback: with no backend reachable the page still renders finished — the
 * fetch failures degrade to a safe read-only notice and the forms stay usable;
 * no dead ends.
 *
 * Styling: application.css (global, via the shell) for base classes/tokens
 * (.page-header, .section-header, .primary-button, .secondary-button, .mono,
 * .signal-label / .signal-mark) + org-settings.css for this page's specifics.
 * Colors come exclusively from the application.css CSS variables — no hex.
 * JetBrains Mono (.mono) is used for immutable identifiers (id + slug).
 */
import { useCallback, useEffect, useMemo, useState } from "react";
import type { Org } from "../../orgs/types";
import OrgDetailPanel from "../../orgs/OrgDetailPanel";
import "../application.css";
import "./org-settings.css";

// ---------------------------------------------------------------------------
// Types — mirror the REAL GET /api/organizations/{org_id} response shape.
// ---------------------------------------------------------------------------

interface OrgSettingsRecord {
  id: string;
  name: string;
  slug: string;
  status?: string;
  billing_ref?: string | null;
  created_at?: string | null;
  updated_at?: string | null;
  archived_at?: string | null;
  brand_primary?: string | null;
  brand_secondary?: string | null;
  brand_accent?: string | null;
  logo_url?: string | null;
}

interface CountryVocabularyEntry {
  code: string;
  display_name: string;
}

export interface OrgSettingsProps {
  /** The organization to configure. When absent, the page renders its
   *  spec-derived shell with a "select an organization" empty state so it is
   *  never broken. */
  orgId?: string;
  /** Test/preview seam — a pre-loaded record avoids a fetch. */
  initialOrg?: OrgSettingsRecord;
  apiBase?: string;
  apiToken?: string;
}

// #RRGGBB — mirrors admin_api._HEX_COLOUR_RE so the client rejects exactly
// what the server would reject on PATCH.
const HEX_COLOUR_RE = /^#[0-9a-fA-F]{6}$/;

// Warehouse-safe slug form — mirrors warehouse_tenancy.sanitize_warehouse_slug
// ('-' -> '_'); the datasets are org_<wslug>_raw / org_<wslug>_marts.
function warehouseSlug(slug: string): string {
  return slug.replace(/-/g, "_");
}

type SaveState =
  | { status: "idle" }
  | { status: "saving" }
  | { status: "saved" }
  | { status: "error"; message: string };

// ---------------------------------------------------------------------------

export default function OrgSettings({
  orgId,
  initialOrg,
  apiBase = "",
  apiToken = "",
}: OrgSettingsProps) {
  const [org, setOrg] = useState<OrgSettingsRecord | null>(initialOrg ?? null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [loading, setLoading] = useState<boolean>(Boolean(orgId) && !initialOrg);

  // Editable working copies (identity + billing + branding).
  const [name, setName] = useState(initialOrg?.name ?? "");
  const [billingRef, setBillingRef] = useState(initialOrg?.billing_ref ?? "");
  const [brandPrimary, setBrandPrimary] = useState(initialOrg?.brand_primary ?? "");
  const [brandSecondary, setBrandSecondary] = useState(initialOrg?.brand_secondary ?? "");
  const [brandAccent, setBrandAccent] = useState(initialOrg?.brand_accent ?? "");
  const [logoUrl, setLogoUrl] = useState(initialOrg?.logo_url ?? "");

  const [identitySave, setIdentitySave] = useState<SaveState>({ status: "idle" });
  const [billingSave, setBillingSave] = useState<SaveState>({ status: "idle" });
  const [brandingSave, setBrandingSave] = useState<SaveState>({ status: "idle" });

  // Countries vocabulary (read-only reference; not an org field).
  const [countries, setCountries] = useState<CountryVocabularyEntry[]>([]);
  const [countriesError, setCountriesError] = useState<string | null>(null);

  const headers = useMemo<HeadersInit>(
    () => ({
      "Content-Type": "application/json",
      ...(apiToken ? { Authorization: `Bearer ${apiToken}` } : {}),
    }),
    [apiToken],
  );

  const hydrate = useCallback((record: OrgSettingsRecord) => {
    setOrg(record);
    setName(record.name ?? "");
    setBillingRef(record.billing_ref ?? "");
    setBrandPrimary(record.brand_primary ?? "");
    setBrandSecondary(record.brand_secondary ?? "");
    setBrandAccent(record.brand_accent ?? "");
    setLogoUrl(record.logo_url ?? "");
  }, []);

  // ---- Load the org ------------------------------------------------------
  useEffect(() => {
    if (!orgId || initialOrg) return;
    let cancelled = false;
    setLoading(true);
    setLoadError(null);
    void (async () => {
      try {
        const resp = await fetch(
          `${apiBase}/api/organizations/${encodeURIComponent(orgId)}`,
          { headers },
        );
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const data = (await resp.json()) as OrgSettingsRecord;
        if (!cancelled) hydrate(data);
      } catch (err) {
        if (!cancelled) {
          setLoadError(err instanceof Error ? err.message : String(err));
        }
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [orgId, initialOrg, apiBase, headers, hydrate]);

  // ---- Load canonical country vocabulary (read-only) ---------------------
  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const resp = await fetch(`${apiBase}/api/vocabularies/countries`, { headers });
        if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
        const data = (await resp.json()) as { countries?: CountryVocabularyEntry[] };
        if (!cancelled) setCountries(data.countries ?? []);
      } catch (err) {
        if (!cancelled) {
          setCountriesError(err instanceof Error ? err.message : String(err));
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [apiBase, headers]);

  // ---- PATCH helper ------------------------------------------------------
  const patchOrg = useCallback(
    async (body: Record<string, unknown>): Promise<OrgSettingsRecord> => {
      if (!org) throw new Error("No organization loaded.");
      const resp = await fetch(
        `${apiBase}/api/organizations/${encodeURIComponent(org.id)}`,
        { method: "PATCH", headers, body: JSON.stringify(body) },
      );
      if (!resp.ok) {
        const parsed = (await resp.json().catch(() => ({}))) as {
          message?: string;
          code?: string;
        };
        throw new Error(parsed.message ?? `HTTP ${resp.status}`);
      }
      return (await resp.json()) as OrgSettingsRecord;
    },
    [org, apiBase, headers],
  );

  // ---- Section savers ----------------------------------------------------
  const nameTrimmed = name.trim();
  const nameValid = nameTrimmed.length > 0 && nameTrimmed.length <= 100;
  const nameDirty = org != null && nameTrimmed !== (org.name ?? "");

  async function saveIdentity() {
    if (!nameValid || !nameDirty) return;
    setIdentitySave({ status: "saving" });
    try {
      const updated = await patchOrg({ name: nameTrimmed });
      hydrate(updated);
      setIdentitySave({ status: "saved" });
    } catch (err) {
      setIdentitySave({
        status: "error",
        message: err instanceof Error ? err.message : String(err),
      });
    }
  }

  const billingDirty =
    org != null && billingRef.trim() !== (org.billing_ref ?? "");

  async function saveBilling() {
    if (!billingDirty) return;
    setBillingSave({ status: "saving" });
    try {
      const trimmed = billingRef.trim();
      const updated = await patchOrg({ billing_ref: trimmed === "" ? null : trimmed });
      hydrate(updated);
      setBillingSave({ status: "saved" });
    } catch (err) {
      setBillingSave({
        status: "error",
        message: err instanceof Error ? err.message : String(err),
      });
    }
  }

  const brandFields: Array<{
    key: "brand_primary" | "brand_secondary" | "brand_accent";
    label: string;
    value: string;
    set: (v: string) => void;
  }> = [
    { key: "brand_primary", label: "Primary", value: brandPrimary, set: setBrandPrimary },
    { key: "brand_secondary", label: "Secondary", value: brandSecondary, set: setBrandSecondary },
    { key: "brand_accent", label: "Accent", value: brandAccent, set: setBrandAccent },
  ];

  const brandColourErrors = brandFields
    .filter((f) => f.value.trim() !== "" && !HEX_COLOUR_RE.test(f.value.trim()))
    .map((f) => f.label);
  const brandingValid = brandColourErrors.length === 0;
  const brandingDirty =
    org != null &&
    ((brandPrimary.trim() || "") !== (org.brand_primary ?? "") ||
      (brandSecondary.trim() || "") !== (org.brand_secondary ?? "") ||
      (brandAccent.trim() || "") !== (org.brand_accent ?? "") ||
      logoUrl.trim() !== (org.logo_url ?? ""));

  async function saveBranding() {
    if (!brandingValid || !brandingDirty) return;
    setBrandingSave({ status: "saving" });
    try {
      const norm = (v: string): string | null => (v.trim() === "" ? null : v.trim());
      const updated = await patchOrg({
        brand_primary: norm(brandPrimary),
        brand_secondary: norm(brandSecondary),
        brand_accent: norm(brandAccent),
        logo_url: norm(logoUrl),
      });
      hydrate(updated);
      setBrandingSave({ status: "saved" });
    } catch (err) {
      setBrandingSave({
        status: "error",
        message: err instanceof Error ? err.message : String(err),
      });
    }
  }

  // ---- Empty / loading states -------------------------------------------
  if (!orgId && !initialOrg) {
    return (
      <div className="orgsettings">
        <header className="page-header">
          <div>
            <h1>Organization settings</h1>
            <p>Select an organization from the scope switcher to manage it.</p>
          </div>
        </header>
        <div className="orgsettings-empty" role="status">
          No organization is in scope. Pick one from the top bar to edit its
          identity, members and roles, markets, billing, and branding.
        </div>
      </div>
    );
  }

  if (loading) {
    return (
      <div className="orgsettings">
        <header className="page-header">
          <div>
            <h1>Organization settings</h1>
            <p>Loading organization…</p>
          </div>
        </header>
        <div className="orgsettings-empty" role="status">
          Loading organization details…
        </div>
      </div>
    );
  }

  const wslug = org ? warehouseSlug(org.slug) : "";
  const orgForPanel: Org | null = org
    ? ({
        id: org.id,
        name: org.name,
        slug: org.slug,
        status: org.status,
        billing_ref: org.billing_ref ?? null,
      } as Org)
    : null;

  return (
    <div className="orgsettings">
      <header className="page-header">
        <div>
          <h1>Organization settings</h1>
          <p>
            {org ? org.name : "Organization"} — identity, members and roles,
            markets, billing, and branding.
          </p>
        </div>
        {org && (
          <div className="orgsettings-headmeta">
            <span className="signal-label success" aria-label={`Status ${org.status ?? "active"}`}>
              <span className="signal-mark" />
              {org.status ?? "active"}
            </span>
            <span className="mono orgsettings-id" title="Organization identifier">
              {org.id}
            </span>
          </div>
        )}
      </header>

      {loadError && (
        <div className="orgsettings-notice error" role="alert">
          <span className="signal-label error">
            <span className="signal-mark" />
            Could not load this organization
          </span>
          <p>{loadError}</p>
        </div>
      )}

      {/* ============================ IDENTITY ============================ */}
      <section className="orgsettings-section" aria-labelledby="orgsettings-identity">
        <div className="section-header">
          <h2 id="orgsettings-identity">Organization identity</h2>
          <p>The organization can be renamed. Its slug cannot change.</p>
        </div>

        <div className="orgsettings-card">
          <div className="orgsettings-field">
            <label htmlFor="orgsettings-name">Organization name</label>
            <input
              id="orgsettings-name"
              className="orgsettings-input"
              type="text"
              value={name}
              maxLength={120}
              placeholder="Acme Group"
              autoComplete="organization"
              aria-invalid={name.trim() !== "" && !nameValid}
              disabled={!org}
              onChange={(e) => {
                setName(e.target.value);
                if (identitySave.status !== "idle") setIdentitySave({ status: "idle" });
              }}
            />
            {name.trim() !== "" && !nameValid ? (
              <p className="orgsettings-field-error">
                Name must be 1–100 characters.
              </p>
            ) : (
              <p className="orgsettings-field-hint">
                Shown to members and used across reports.
              </p>
            )}
          </div>

          {/* Slug — IMMUTABLE. Named warehouse datasets note. */}
          <div className="orgsettings-slug" aria-live="polite">
            <div className="orgsettings-slug-head">
              <span className="orgsettings-slug-label">Slug (immutable)</span>
              <span className="mono orgsettings-slug-value">{org?.slug ?? "—"}</span>
            </div>
            <p className="orgsettings-slug-note">
              The slug names this organization&apos;s warehouse datasets{" "}
              <span className="mono">org_{wslug || "…"}_raw</span> and{" "}
              <span className="mono">org_{wslug || "…"}_marts</span>. It is fixed
              at creation and <strong>cannot be changed</strong> — renaming it
              would orphan the client&apos;s data plane. To use a different slug,
              create a new organization.
            </p>
          </div>

          {identitySave.status === "error" && (
            <div className="orgsettings-notice error" role="alert">
              <span className="signal-label error">
                <span className="signal-mark" />
                Name not saved
              </span>
              <p>{identitySave.message}</p>
            </div>
          )}

          <div className="orgsettings-actions">
            {identitySave.status === "saved" && (
              <span className="signal-label success">
                <span className="signal-mark" />
                Saved
              </span>
            )}
            <button
              type="button"
              className="primary-button"
              disabled={!org || !nameValid || !nameDirty || identitySave.status === "saving"}
              onClick={() => void saveIdentity()}
            >
              {identitySave.status === "saving" ? "Saving…" : "Save name"}
            </button>
          </div>
        </div>
      </section>

      {/* ========================= MEMBERS & ROLES ======================= */}
      <section className="orgsettings-section" aria-labelledby="orgsettings-members">
        <div className="section-header">
          <h2 id="orgsettings-members">Members and roles</h2>
          <p>
            Owner, admin, member, and viewer roles. Invite people, change roles,
            and manage authorization and data-access grants. The last active
            owner cannot be removed.
          </p>
        </div>

        <div className="orgsettings-card orgsettings-members">
          {orgForPanel ? (
            /* Reuse the REAL members/roles + invite entry point + grants panels.
               (OrgDetailPanel owns the members table, the "add member" invite
               entry point, the auth-credential grants, and the BigQuery
               data-access grants — we do not reimplement any of it.) */
            <OrgDetailPanel org={orgForPanel} apiBase={apiBase} apiToken={apiToken} />
          ) : (
            <p className="orgsettings-field-hint">
              Members become available once the organization has loaded.
            </p>
          )}
        </div>
      </section>

      {/* ========================= COUNTRIES / MARKETS =================== */}
      <section className="orgsettings-section" aria-labelledby="orgsettings-markets">
        <div className="section-header">
          <h2 id="orgsettings-markets">Countries and markets</h2>
          <p>
            The canonical country vocabulary is shared across the organization.
            Which markets a report consolidates is chosen per project through its
            geographic posture — this page shows the reference list, not a
            per-organization override.
          </p>
        </div>

        <div className="orgsettings-card">
          {countriesError ? (
            <p className="orgsettings-field-hint">
              Country vocabulary is unavailable right now ({countriesError}).
              Market selection remains a per-project setting.
            </p>
          ) : countries.length === 0 ? (
            <p className="orgsettings-field-hint">Loading the country vocabulary…</p>
          ) : (
            <>
              <p className="orgsettings-field-hint orgsettings-markets-lead">
                {countries.length} canonical countries are available to every
                project in this organization.
              </p>
              <ul className="orgsettings-countries" aria-label="Canonical countries">
                {countries.map((c) => (
                  <li key={c.code}>
                    <span className="mono orgsettings-country-code">{c.code}</span>
                    <span className="orgsettings-country-name">{c.display_name}</span>
                  </li>
                ))}
              </ul>
            </>
          )}
        </div>
      </section>

      {/* ============================== BILLING ========================== */}
      <section className="orgsettings-section" aria-labelledby="orgsettings-billing">
        <div className="section-header">
          <h2 id="orgsettings-billing">Billing</h2>
          <p>The external billing reference this organization is invoiced under.</p>
        </div>

        <div className="orgsettings-card">
          <div className="orgsettings-field">
            <label htmlFor="orgsettings-billing-ref">Billing reference</label>
            <input
              id="orgsettings-billing-ref"
              className="orgsettings-input"
              type="text"
              value={billingRef}
              placeholder="e.g. a customer or contract identifier"
              disabled={!org}
              onChange={(e) => {
                setBillingRef(e.target.value);
                if (billingSave.status !== "idle") setBillingSave({ status: "idle" });
              }}
            />
            <p className="orgsettings-field-hint">
              Optional. Clearing this field removes the reference.
            </p>
          </div>

          {billingSave.status === "error" && (
            <div className="orgsettings-notice error" role="alert">
              <span className="signal-label error">
                <span className="signal-mark" />
                Billing reference not saved
              </span>
              <p>{billingSave.message}</p>
            </div>
          )}

          <div className="orgsettings-actions">
            {billingSave.status === "saved" && (
              <span className="signal-label success">
                <span className="signal-mark" />
                Saved
              </span>
            )}
            <button
              type="button"
              className="primary-button"
              disabled={!org || !billingDirty || billingSave.status === "saving"}
              onClick={() => void saveBilling()}
            >
              {billingSave.status === "saving" ? "Saving…" : "Save billing reference"}
            </button>
          </div>
        </div>
      </section>

      {/* ============================= BRANDING ========================== */}
      <section className="orgsettings-section" aria-labelledby="orgsettings-branding">
        <div className="section-header">
          <h2 id="orgsettings-branding">Branding</h2>
          <p>Three brand colors and a logo, applied where the organization is shown.</p>
        </div>

        <div className="orgsettings-card">
          <div className="orgsettings-colours">
            {brandFields.map((f) => {
              const val = f.value.trim();
              const invalid = val !== "" && !HEX_COLOUR_RE.test(val);
              const swatch = HEX_COLOUR_RE.test(val) ? val : undefined;
              return (
                <div className="orgsettings-colour" key={f.key}>
                  <label htmlFor={`orgsettings-${f.key}`}>{f.label}</label>
                  <div className="orgsettings-colour-row">
                    <span
                      className="orgsettings-swatch"
                      aria-hidden="true"
                      // The swatch previews the operator's own brand colour;
                      // it is data, not a theme token, so it is set inline.
                      style={swatch ? { background: swatch } : undefined}
                      data-empty={swatch ? undefined : "true"}
                    />
                    <input
                      id={`orgsettings-${f.key}`}
                      className="orgsettings-input mono"
                      type="text"
                      value={f.value}
                      placeholder="#RRGGBB"
                      maxLength={7}
                      aria-invalid={invalid}
                      disabled={!org}
                      onChange={(e) => {
                        f.set(e.target.value);
                        if (brandingSave.status !== "idle") setBrandingSave({ status: "idle" });
                      }}
                    />
                  </div>
                </div>
              );
            })}
          </div>

          <div className="orgsettings-field">
            <label htmlFor="orgsettings-logo">Logo URL</label>
            <input
              id="orgsettings-logo"
              className="orgsettings-input"
              type="text"
              value={logoUrl}
              placeholder="https://…/logo.svg"
              disabled={!org}
              onChange={(e) => {
                setLogoUrl(e.target.value);
                if (brandingSave.status !== "idle") setBrandingSave({ status: "idle" });
              }}
            />
            <p className="orgsettings-field-hint">
              Optional. Colors must be #RRGGBB (six hex digits).
            </p>
          </div>

          {brandColourErrors.length > 0 && (
            <p className="orgsettings-field-error">
              Invalid color for {brandColourErrors.join(", ")}. Use #RRGGBB.
            </p>
          )}

          {brandingSave.status === "error" && (
            <div className="orgsettings-notice error" role="alert">
              <span className="signal-label error">
                <span className="signal-mark" />
                Branding not saved
              </span>
              <p>{brandingSave.message}</p>
            </div>
          )}

          <div className="orgsettings-actions">
            {brandingSave.status === "saved" && (
              <span className="signal-label success">
                <span className="signal-mark" />
                Saved
              </span>
            )}
            <button
              type="button"
              className="primary-button"
              disabled={
                !org || !brandingValid || !brandingDirty || brandingSave.status === "saving"
              }
              onClick={() => void saveBranding()}
            >
              {brandingSave.status === "saving" ? "Saving…" : "Save branding"}
            </button>
          </div>
        </div>
      </section>
    </div>
  );
}
