/**
 * CreateOrg — the "Create organization" onboarding screen.
 *
 * ⚠️ SPEC-DERIVED, NO MOCKUP — pending a visual mockup.
 * There is no validated mockup for the create-organization flow. This screen is
 * derived FAITHFULLY from the validated Epic 36 spec (EXPERIENCE.md + DESIGN.md
 * under ux-designs/ux-connector-2026-07-22/) and the org-tenancy contract, NOT
 * free-invented. It follows the spec's ScopeSummary confirmation pattern, its
 * "name each thing separately / show exact impact" voice, and the DESIGN.md
 * tokens/components. When a visual mockup lands, reconcile this port against it
 * (spines still win over any mockup when they conflict — EXPERIENCE.md).
 *
 * Spec anchors:
 *   - Org creation provisions the organization's warehouse (org owns members,
 *     roles, countries, billing; ScopeSummary precedes a consequential action).
 *   - Org-tenancy: an organization has a name, a slug that names the warehouse
 *     datasets org_<wslug>_raw / org_<wslug>_marts, and is IMMUTABLE after
 *     creation (server returns 422 slug_immutable on later change), plus an owner
 *     (the creator is auto-enrolled as owner — server side).
 *   - Voice: "Keep one primary rose action per decision surface", "Show exact
 *     actor, scope, expiry, interval, and next action", "Show sanitized
 *     identifiers in JetBrains Mono" (DESIGN.md Do's).
 *
 * Backend (REAL, wired):
 *   POST /api/organizations
 *     request  : { name: string; slug?: string }   (billing_ref/branding exist
 *                 server-side but are NOT part of THIS spec-derived screen, so we
 *                 do not send or invent them here)
 *     response : 201 { id, name, slug, status, created_at, ... }
 *   On success the server provisions the warehouse datasets (non-blocking) and
 *   auto-enrolls the creator as owner. The 201 body does NOT echo the schema
 *   names, so the confirmed schema names are DERIVED client-side by the same
 *   rule the server uses (warehouse_tenancy.sanitize_warehouse_slug: '-' -> '_',
 *   datasets org_<wslug>_raw / org_<wslug>_marts). Flagged // derived below.
 *
 * Fallback: with no backend reachable the screen still renders finished — the
 * slug preview and ScopeSummary are pure client derivations; submit surfaces a
 * safe error and the form stays usable (no dead end).
 *
 * Styling: application.css (global, via the shell) for base classes/tokens +
 * create-org.css for the focused-dialog surface and this page's specifics.
 * Colors come exclusively from the application.css CSS variables — no hex.
 */
import { useMemo, useState } from "react";
import "../application.css";
import "./create-org.css";

/** Kebab-case slugify — mirrors the server's admin_api._slugify exactly so the
 *  preview matches what the API will store when no slug is supplied. */
function slugify(name: string): string {
  return name
    .trim()
    .toLowerCase()
    .replace(/[\s_]+/g, "-")
    .replace(/[^a-z0-9-]/g, "")
    .replace(/-+/g, "-")
    .replace(/^-+|-+$/g, "");
}

/** Server slug charset (admin_api._SLUG_RE): kebab-case, starts alphanumeric. */
const SLUG_RE = /^[a-z0-9][a-z0-9-]*$/;

/** Warehouse-safe form — mirrors warehouse_tenancy.sanitize_warehouse_slug
 *  ('-' -> '_'); the datasets are org_<wslug>_raw / org_<wslug>_marts. */
function warehouseSlug(slug: string): string {
  return slug.replace(/-/g, "_");
}

interface CreatedOrg {
  id: string;
  name: string;
  slug: string;
  status?: string;
  created_at?: string;
  [key: string]: unknown;
}

type SubmitState =
  | { status: "idle" }
  | { status: "submitting" }
  | { status: "error"; message: string }
  | { status: "created"; org: CreatedOrg };

async function createOrganization(name: string, slug: string): Promise<CreatedOrg> {
  const resp = await fetch("/api/organizations", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    // Only the two spec fields. slug is sent explicitly so the warehouse
    // datasets match the previewed, immutable name the operator confirmed.
    body: JSON.stringify({ name, slug }),
  });
  if (!resp.ok) {
    const body = (await resp.json().catch(() => ({}))) as { message?: string; code?: string };
    throw new Error(body.message ?? `HTTP ${resp.status}`);
  }
  return (await resp.json()) as CreatedOrg;
}

export interface CreateOrgProps {
  /** Called after a successful create, with the new organization id. */
  onCreated?: (orgId: string) => void;
  /** Called when the operator cancels/closes the flow. */
  onCancel?: () => void;
}

export default function CreateOrg({ onCreated, onCancel }: CreateOrgProps) {
  const [name, setName] = useState("");
  const [submit, setSubmit] = useState<SubmitState>({ status: "idle" });

  const slug = useMemo(() => slugify(name), [name]);
  const wslug = useMemo(() => warehouseSlug(slug), [slug]);

  const nameTrimmed = name.trim();
  const nameTooLong = nameTrimmed.length > 100; // admin_api: name max 100
  const slugTooLong = slug.length > 50; //        admin_api: slug max 50
  const slugValid = slug.length > 0 && SLUG_RE.test(slug) && !slugTooLong;
  const nameValid = nameTrimmed.length > 0 && !nameTooLong;
  const canSubmit = nameValid && slugValid && submit.status !== "submitting";

  const nameError = !nameTrimmed
    ? null
    : nameTooLong
      ? "Organization name must be 100 characters or fewer."
      : !slugValid
        ? "This name has no letters or digits to build a warehouse slug from. Add at least one."
        : null;

  async function handleSubmit(event: React.FormEvent) {
    event.preventDefault();
    if (!canSubmit) return;
    setSubmit({ status: "submitting" });
    try {
      const org = await createOrganization(nameTrimmed, slug);
      setSubmit({ status: "created", org });
      onCreated?.(org.id);
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      setSubmit({ status: "error", message });
    }
  }

  // ---- Success view: the provisioned org + its warehouse schemas ----------
  if (submit.status === "created") {
    const org = submit.org;
    // derived: server 201 does not echo schema names; compute them by the same
    // rule the server uses so we display the exact provisioned datasets.
    const createdWslug = warehouseSlug(org.slug);
    return (
      <div className="createorg-stage">
        <div className="createorg-scrim">
          <section
            className="createorg-dialog"
            role="dialog"
            aria-modal="true"
            aria-labelledby="createorg-done-title"
          >
            <header className="createorg-header">
              <h1 id="createorg-done-title">Organization created</h1>
            </header>

            <div className="createorg-body">
              <p className="createorg-lead">
                <strong>{org.name}</strong> is active. Its warehouse is being provisioned and you
                are its owner. Members, roles, countries, and billing belong to this organization.
              </p>

              {/* ScopeSummary-style read-only summary of what now exists. */}
              <div className="scope-summary" aria-label="Created organization summary">
                <div className="scope-row">
                  <span className="scope-key">Organization</span>
                  <span className="scope-val">{org.name}</span>
                </div>
                <div className="scope-row">
                  <span className="scope-key">Slug</span>
                  <span className="scope-val mono">{org.slug}</span>
                </div>
                <div className="scope-row">
                  <span className="scope-key">Identifier</span>
                  <span className="scope-val mono">{org.id}</span>
                </div>
                <div className="scope-row">
                  <span className="scope-key">Status</span>
                  <span className="scope-val">
                    <span className="signal-label success">
                      <span className="signal-mark" />
                      {org.status ?? "active"}
                    </span>
                  </span>
                </div>
                <div className="scope-row">
                  <span className="scope-key">Your role</span>
                  <span className="scope-val">Owner</span>
                </div>
              </div>

              <h2 className="createorg-subhead">Provisioned warehouse datasets</h2>
              <p className="createorg-note">
                These dataset names are fixed by the slug and cannot change.
              </p>
              <ul className="schema-list">
                <li>
                  <span className="mono">org_{createdWslug}_raw</span>
                  <span className="schema-role">Raw landing</span>
                </li>
                <li>
                  <span className="mono">org_{createdWslug}_marts</span>
                  <span className="schema-role">Published marts</span>
                </li>
              </ul>
            </div>

            <footer className="createorg-footer">
              <span>Next: invite members and create a project to start a first report.</span>
              <div className="createorg-actions">
                <button
                  className="primary-button"
                  type="button"
                  onClick={() => onCreated?.(org.id)}
                >
                  Go to organization
                </button>
              </div>
            </footer>
          </section>
        </div>
      </div>
    );
  }

  // ---- Create form (idle / submitting / error) ----------------------------
  return (
    <div className="createorg-stage">
      <div className="createorg-scrim">
        <form
          className="createorg-dialog"
          role="dialog"
          aria-modal="true"
          aria-labelledby="createorg-title"
          onSubmit={handleSubmit}
          noValidate
        >
          <header className="createorg-header">
            <div>
              <h1 id="createorg-title">Create organization</h1>
              <p className="createorg-subtitle">
                An organization owns its members, roles, countries, billing, and a dedicated
                warehouse. You will be its owner.
              </p>
            </div>
            {onCancel && (
              <button
                className="icon-button action-link"
                type="button"
                aria-label="Close"
                onClick={() => onCancel()}
              >
                ×
              </button>
            )}
          </header>

          <div className="createorg-body">
            <div className="field">
              <label htmlFor="createorg-name">Organization name</label>
              <input
                id="createorg-name"
                className="text-input"
                type="text"
                value={name}
                maxLength={120}
                autoComplete="organization"
                placeholder="Acme Group"
                aria-invalid={nameError !== null}
                aria-describedby={nameError ? "createorg-name-error" : "createorg-name-hint"}
                onChange={(e) => {
                  setName(e.target.value);
                  if (submit.status === "error") setSubmit({ status: "idle" });
                }}
              />
              {nameError ? (
                <p className="field-error" id="createorg-name-error">
                  {nameError}
                </p>
              ) : (
                <p className="field-hint" id="createorg-name-hint">
                  Shown to members. You can rename the organization later.
                </p>
              )}
            </div>

            {/* Derived slug preview — the immutable warehouse identity. */}
            <div className="slug-preview" aria-live="polite">
              <div className="slug-preview-head">
                <span className="slug-preview-label">Warehouse slug</span>
                <span className="slug-preview-value mono">{slug || "—"}</span>
              </div>
              <p className="slug-preview-note">
                The slug names this organization&apos;s warehouse datasets{" "}
                <span className="mono">org_{wslug || "…"}_raw</span> and{" "}
                <span className="mono">org_{wslug || "…"}_marts</span>. It is derived from the name
                and is <strong>immutable after creation</strong> — the organization can be renamed,
                but these dataset names cannot.
              </p>
            </div>

            {/* ScopeSummary-style confirmation BEFORE create. */}
            <div className="scope-summary" aria-label="What will be created">
              <div className="scope-summary-title">Before you create</div>
              <div className="scope-row">
                <span className="scope-key">Organization</span>
                <span className="scope-val">{nameTrimmed || "—"}</span>
              </div>
              <div className="scope-row">
                <span className="scope-key">Slug</span>
                <span className="scope-val mono">{slug || "—"}</span>
              </div>
              <div className="scope-row">
                <span className="scope-key">Warehouse</span>
                <span className="scope-val mono">
                  {wslug ? `org_${wslug}_raw · org_${wslug}_marts` : "—"}
                </span>
              </div>
              <div className="scope-row">
                <span className="scope-key">Owner</span>
                <span className="scope-val">You (auto-enrolled)</span>
              </div>
              <div className="scope-row">
                <span className="scope-key">Impact</span>
                <span className="scope-val">
                  Provisions the warehouse. The slug cannot be changed afterward.
                </span>
              </div>
            </div>

            {submit.status === "error" && (
              <div className="createorg-error" role="alert">
                <span className="signal-label error">
                  <span className="signal-mark" />
                  Organization not created
                </span>
                <p>{submit.message}</p>
              </div>
            )}
          </div>

          <footer className="createorg-footer">
            <span>You can invite members and add projects once the organization exists.</span>
            <div className="createorg-actions">
              {onCancel && (
                <button
                  className="secondary-button action-link"
                  type="button"
                  onClick={() => onCancel()}
                >
                  Cancel
                </button>
              )}
              <button className="primary-button" type="submit" disabled={!canSubmit}>
                {submit.status === "submitting" ? "Creating…" : "Create organization"}
              </button>
            </div>
          </footer>
        </form>
      </div>
    </div>
  );
}
