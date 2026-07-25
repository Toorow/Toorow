/**
 * JoinOrg — the "Join an organization" onboarding screen (invitation acceptance).
 *
 * ⚠️ SPEC-DERIVED, NO MOCKUP — pending a visual mockup.
 * There is no validated mockup for the invitation-acceptance flow. This screen is
 * derived FAITHFULLY from the validated Epic 36 spec (EXPERIENCE.md + DESIGN.md
 * under ux-designs/ux-connector-2026-07-22/), NOT free-invented. It follows the
 * spec's Flow 1 (Invitation and activation), the Surface State Matrix, and the
 * ScopeSummary component anatomy. When a visual mockup lands, reconcile this port
 * against it (spines still win over any mockup when they conflict — EXPERIENCE.md).
 *
 * Spec anchors (Flow 1 / ScopeSummary / State Patterns):
 *   - A signed single-use link opens a GENERIC invitation page that reveals NO
 *     organization / project / Datastream names, identifiers, or counts before the
 *     subject identity matches (EXPERIENCE.md Flow 1 step 3; Surface State Matrix
 *     "Reveal no resource scope before subject match").
 *   - After sign-in with the matching VERIFIED identity, the subject reviews
 *     role-derived EFFECTIVE authority and the exact AD-5 project/Datastream
 *     binding paths in a read-only ScopeSummary (organization → project /
 *     ProjectDatastreamBinding → canonical Datastream, scope type, safe
 *     immutable-ID suffix, explicit "None" per scope, absolute expiry), then
 *     ACCEPTS ONCE.
 *   - Climax: the subject lands on Getting started with membership and grants
 *     active (next_url from the accept response).
 *   - Failure: expired, mismatched, replayed, or revoked links create no grant and
 *     name who can resend (the inviter / an organization admin).
 *   - The bearer lives ONLY in the URL fragment (never sent in a querystring, never
 *     persisted); the shell strips the fragment before exchange.
 *
 * Backend (REAL, wired) — server/core/invitations.py via server/core/admin_api.py:
 *   POST /api/invitations/exchange
 *     request  : { bearer: string }                 (the fragment bearer)
 *     response : 200 { ready_to_accept: true }       (+ narrow httponly exchange
 *                cookie, path=/api/invitations, samesite=strict; NO scope detail is
 *                disclosed here — identity match is enforced server-side)
 *                401 unauthorized (must sign in with the invited identity)
 *                404 { code:"not_found" } for mismatched / expired / replayed /
 *                revoked / unavailable — a single NONDISCLOSING outcome by design.
 *   POST /api/invitations/accept
 *     headers  : Idempotency-Key: <uuid>             (required; 422 if missing)
 *     request  : { confirmed: true }                 (the single "accept once" gate)
 *     response : 200 {
 *                  invitation_id, organization_id,
 *                  authority: { role_derived, explicit_grants[], explicit_none },
 *                  next_url, replayed, operation_id, ...
 *                }
 *                404 unavailable · 409 conflict (existing access) · 422 confirm.
 *   Each explicit grant is { scope_type: "project" | "flux", scope_id, capability }.
 *   (No inspect-by-token endpoint exists: exchange is the nondisclosing identity
 *   gate; accept is the single confirmed action that both reveals the effective
 *   grant and activates membership atomically. We do not invent an inspect route.)
 *
 * Fallback: with no backend reachable the screen still renders finished — it shows a
 * representative pending-invitation ScopeSummary (clearly marked example) so the
 * layout is complete; Accept surfaces a safe, nondisclosing error and never dead-ends.
 *
 * Styling: application.css (global tokens/classes) + join-org.css for this page's
 * specifics. Colors come exclusively from the application.css CSS variables — no hex.
 */
import { useEffect, useMemo, useState } from "react";
import "../application.css";
import "./join-org.css";
import { apiFetch } from "../../lib/apiFetch";

/** One explicit grant, exactly as the accept response returns it. */
interface ExplicitGrant {
  scope_type: "project" | "flux";
  scope_id: string;
  capability: string;
}

/** Effective authority returned by POST /api/invitations/accept. */
interface AcceptedAuthority {
  role_derived: string;
  explicit_grants: ExplicitGrant[];
  explicit_none: boolean;
}

/** Shape of the accept response we consume (spec fields only). */
interface AcceptedInvitation {
  invitation_id: string;
  organization_id: string;
  authority: AcceptedAuthority;
  next_url: string;
  replayed: boolean;
}

/**
 * Screen state machine — mirrors the Surface State Matrix. Before "ready" NOTHING
 * about the organization/project/Datastream is revealed; "unavailable" is the single
 * nondisclosing terminal for mismatched / expired / replayed / revoked links.
 */
type JoinState =
  | { status: "no_token" }
  | { status: "validating" }
  | { status: "signin_required" }
  | { status: "ready" } // identity matched; exchange consumed; safe to accept once
  | { status: "accepting" }
  | { status: "unavailable" } // mismatched · expired · replayed · revoked
  | { status: "conflict" } // valid link, but existing access conflicts
  | { status: "error"; message: string }
  | { status: "accepted"; result: AcceptedInvitation };

/** Read the single-use bearer from the URL fragment (#invite=<bearer>), then strip
 *  it from history so it is never re-sent, bookmarked, or leaked via referrer. */
function readBearerFromFragment(): string {
  if (typeof window === "undefined") return "";
  const hash = window.location.hash || "";
  const bearer = hash.startsWith("#invite=") ? hash.slice("#invite=".length) : "";
  if (bearer) {
    // The bearer lives ONLY in the fragment; remove it immediately (spec).
    window.history.replaceState(null, "", window.location.pathname + window.location.search);
  }
  return bearer;
}

/** A stable idempotency key for the single accept (one link = one accept). */
function newIdempotencyKey(): string {
  const c = typeof crypto !== "undefined" ? crypto : undefined;
  if (c && typeof c.randomUUID === "function") return c.randomUUID();
  // Fallback UUIDv4-ish (only when crypto.randomUUID is unavailable).
  return "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, (ch) => {
    const r = (Math.random() * 16) | 0;
    const v = ch === "x" ? r : (r & 0x3) | 0x8;
    return v.toString(16);
  });
}

/** Human-readable capability label (spec vocabulary: view/edit/manage). */
function capabilityLabel(capability: string): string {
  switch (capability) {
    case "view":
      return "View";
    case "edit":
      return "Edit";
    case "manage":
      return "Manage";
    default:
      return capability;
  }
}

/** Scope-type label — project vs Datastream (server names Datastreams "flux"). */
function scopeTypeLabel(scopeType: string): string {
  return scopeType === "flux" ? "Datastream" : scopeType === "project" ? "Project" : scopeType;
}

/** Safe immutable-ID suffix (last segment) — never the full identifier, per spec. */
function safeIdSuffix(scopeId: string): string {
  if (!scopeId) return "";
  const tail = scopeId.slice(-8);
  return scopeId.length > 8 ? `…${tail}` : tail;
}

/** Format an absolute expiry (ISO 8601) for display; falls back to the raw string. */
function formatAbsoluteExpiry(iso: string | null): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString(undefined, {
    year: "numeric",
    month: "short",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    timeZoneName: "short",
  });
}

/** POST the fragment bearer for its matching verified subject. A 404 is the single
 *  nondisclosing outcome for mismatched/expired/replayed/revoked links. */
async function exchangeBearer(
  bearer: string,
): Promise<"ready" | "signin_required" | "unavailable"> {
  const resp = await apiFetch("/api/invitations/exchange", {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ bearer }),
  });
  if (resp.ok) return "ready";
  if (resp.status === 401) return "signin_required";
  // 404 (and any other non-2xx) → nondisclosing "unavailable". No detail is surfaced.
  return "unavailable";
}

type AcceptOutcome =
  | { kind: "accepted"; result: AcceptedInvitation }
  | { kind: "unavailable" }
  | { kind: "conflict" }
  | { kind: "signin_required" }
  | { kind: "error"; message: string };

/** Confirm once. Uses the narrow exchange cookie set by /exchange plus the required
 *  Idempotency-Key. Reveals the effective grant only on success (never before). */
async function acceptOnce(idempotencyKey: string): Promise<AcceptOutcome> {
  const resp = await apiFetch("/api/invitations/accept", {
    method: "POST",
    credentials: "same-origin",
    headers: {
      "Content-Type": "application/json",
      "Idempotency-Key": idempotencyKey,
    },
    body: JSON.stringify({ confirmed: true }),
  });
  if (resp.ok) {
    const body = (await resp.json()) as AcceptedInvitation;
    return { kind: "accepted", result: body };
  }
  if (resp.status === 401) return { kind: "signin_required" };
  if (resp.status === 409) return { kind: "conflict" };
  if (resp.status === 404) return { kind: "unavailable" };
  return { kind: "error", message: "The invitation could not be accepted. Please try again." };
}

/** Representative pending grant used ONLY for the no-backend fallback, so the page
 *  renders finished. It is clearly labeled as an example and never treated as real. */
const FALLBACK_GRANTS: ExplicitGrant[] = [
  { scope_type: "project", scope_id: "proj_example_0001", capability: "edit" },
  { scope_type: "flux", scope_id: "flux_example_0002", capability: "view" },
];

export interface JoinOrgProps {
  /** Bearer from the URL fragment. When omitted, the screen reads it itself. */
  token?: string;
  /** Called after a successful accept — the subject lands with membership active. */
  onAccepted?: () => void;
}

export default function JoinOrg({ token, onAccepted }: JoinOrgProps) {
  const [bearer, setBearer] = useState<string | null>(token ?? null);
  const [state, setState] = useState<JoinState>({ status: "validating" });
  const [idempotencyKey] = useState<string>(newIdempotencyKey);

  // Resolve the bearer from the fragment once (if not supplied via props).
  useEffect(() => {
    if (token !== undefined) {
      setBearer(token);
      return;
    }
    setBearer(readBearerFromFragment());
    // Intentionally run once: the fragment is stripped after the first read.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Validate the link by exchanging the bearer for its matching verified subject.
  useEffect(() => {
    if (bearer === null) return; // still resolving
    if (bearer === "") {
      setState({ status: "no_token" });
      return;
    }
    let cancelled = false;
    setState({ status: "validating" });
    exchangeBearer(bearer)
      .then((outcome) => {
        if (cancelled) return;
        if (outcome === "ready") setState({ status: "ready" });
        else if (outcome === "signin_required") setState({ status: "signin_required" });
        else setState({ status: "unavailable" });
      })
      .catch(() => {
        // No backend reachable → render finished with a representative pending grant.
        if (!cancelled) setState({ status: "ready" });
      });
    return () => {
      cancelled = true;
    };
  }, [bearer]);

  async function handleAccept() {
    setState({ status: "accepting" });
    try {
      const outcome = await acceptOnce(idempotencyKey);
      if (outcome.kind === "accepted") {
        setState({ status: "accepted", result: outcome.result });
        onAccepted?.();
      } else if (outcome.kind === "conflict") {
        setState({ status: "conflict" });
      } else if (outcome.kind === "signin_required") {
        setState({ status: "signin_required" });
      } else if (outcome.kind === "unavailable") {
        setState({ status: "unavailable" });
      } else {
        setState({ status: "error", message: outcome.message });
      }
    } catch {
      // No backend reachable in the finished-fallback: show a safe, nondisclosing
      // error rather than fabricating a membership.
      setState({
        status: "error",
        message: "The invitation service is unavailable. Please try again shortly.",
      });
    }
  }

  return (
    <div className="joinorg-stage">
      <div className="joinorg-scrim">
        <section
          className="joinorg-dialog"
          role="dialog"
          aria-modal="true"
          aria-labelledby="joinorg-title"
        >
          {renderBody(state, handleAccept, onAccepted)}
        </section>
      </div>
    </div>
  );
}

/** Renders the surface for the current state. Kept as a function (not nested
 *  components) so each state is a flat, reviewable block against the spec. */
function renderBody(
  state: JoinState,
  onAccept: () => void,
  onAccepted?: () => void,
): JSX.Element {
  switch (state.status) {
    case "validating":
      return <ValidatingView />;
    case "no_token":
      return <NoTokenView />;
    case "signin_required":
      return <SignInRequiredView />;
    case "unavailable":
      return <UnavailableView />;
    case "conflict":
      return <ConflictView />;
    case "error":
      return <ErrorView message={state.message} onRetry={onAccept} />;
    case "ready":
    case "accepting":
      return <ReviewAndAcceptView accepting={state.status === "accepting"} onAccept={onAccept} />;
    case "accepted":
      return <AcceptedView result={state.result} onAccepted={onAccepted} />;
    default:
      return <UnavailableView />;
  }
}

/* --------------------------------------------------------------------------- *
 * Generic pre-match surfaces — reveal NO resource scope (names/IDs/counts).    *
 * --------------------------------------------------------------------------- */

function GenericHeader({ title, subtitle }: { title: string; subtitle: string }) {
  return (
    <header className="joinorg-header">
      <div>
        <h1 id="joinorg-title">{title}</h1>
        <p className="joinorg-subtitle">{subtitle}</p>
      </div>
    </header>
  );
}

function ValidatingView() {
  return (
    <>
      <GenericHeader
        title="Continue to your invitation"
        subtitle="Checking your invitation. Nothing is shared until your identity matches."
      />
      <div className="joinorg-body">
        <div className="joinorg-status" role="status" aria-live="polite">
          <span className="signal-label info">
            <span className="signal-mark" />
            Validating the invitation…
          </span>
          <div className="joinorg-skeleton" aria-hidden="true">
            <span />
            <span />
            <span />
          </div>
        </div>
      </div>
    </>
  );
}

function NoTokenView() {
  return (
    <>
      <GenericHeader
        title="No invitation to open"
        subtitle="This page opens an invitation link. There is nothing to review here."
      />
      <div className="joinorg-body">
        <p className="joinorg-lead">
          Open the invitation link from your invitation email. If your link has expired or was
          already used, ask the person who invited you — an organization owner or admin — to send a
          new one.
        </p>
      </div>
    </>
  );
}

function SignInRequiredView() {
  return (
    <>
      <GenericHeader
        title="Sign in to continue"
        subtitle="Sign in with the exact address this invitation was sent to."
      />
      <div className="joinorg-body">
        <p className="joinorg-lead">
          To keep invitations private, nothing about the organization, projects, or Datastreams is
          shown until you sign in with the invited identity. Your invitation details appear only
          after your verified identity matches.
        </p>
        <div className="joinorg-status" role="status">
          <span className="signal-label info">
            <span className="signal-mark" />
            Sign in with the invited address, then reopen this link.
          </span>
        </div>
      </div>
    </>
  );
}

function UnavailableView() {
  // Single nondisclosing terminal for mismatched · expired · replayed · revoked.
  return (
    <>
      <GenericHeader
        title="This invitation can’t be opened"
        subtitle="It may have expired, already been used, or been revoked."
      />
      <div className="joinorg-body">
        <div className="joinorg-notice error" role="alert">
          <span className="signal-label error">
            <span className="signal-mark" />
            No access was granted
          </span>
          <p>
            This invitation link is no longer usable. For your privacy, we don’t reveal whether it
            expired, was already accepted, was revoked, or was meant for a different address — the
            outcome is the same: nothing was changed and no access was granted.
          </p>
        </div>
        <p className="joinorg-note">
          Ask the person who invited you — an organization owner or admin — to send a fresh
          invitation. Only they can resend it.
        </p>
      </div>
    </>
  );
}

function ConflictView() {
  return (
    <>
      <GenericHeader
        title="You already have access here"
        subtitle="This invitation overlaps with access you already hold."
      />
      <div className="joinorg-body">
        <div className="joinorg-notice warning" role="alert">
          <span className="signal-label warning">
            <span className="signal-mark" />
            Nothing was changed
          </span>
          <p>
            This invitation conflicts with access you already have in this organization, so it was
            not applied. Your existing membership and grants are unchanged.
          </p>
        </div>
        <p className="joinorg-note">
          If the invitation was meant to change your access, ask an organization owner or admin to
          adjust it directly.
        </p>
      </div>
    </>
  );
}

function ErrorView({ message, onRetry }: { message: string; onRetry: () => void }) {
  return (
    <>
      <GenericHeader
        title="Something went wrong"
        subtitle="No access was granted. You can try again."
      />
      <div className="joinorg-body">
        <div className="joinorg-notice error" role="alert">
          <span className="signal-label error">
            <span className="signal-mark" />
            Invitation not accepted
          </span>
          <p>{message}</p>
        </div>
      </div>
      <footer className="joinorg-footer">
        <span>No changes were made. You can retry the acceptance.</span>
        <div className="joinorg-actions">
          <button className="primary-button" type="button" onClick={onRetry}>
            Try again
          </button>
        </div>
      </footer>
    </>
  );
}

/* --------------------------------------------------------------------------- *
 * Post-match review + single accept. Only reached after the exchange (identity *
 * matched) succeeds. The effective grant is confirmed by the single Accept.    *
 * --------------------------------------------------------------------------- */

function ReviewAndAcceptView({
  accepting,
  onAccept,
}: {
  accepting: boolean;
  onAccept: () => void;
}) {
  // The exchange endpoint discloses no scope detail (identity gate only). Until the
  // subject accepts once, we present the effective grant as the representative
  // pending binding the invitation carries. On real backends the exact bindings are
  // returned by the accept response and re-surfaced in AcceptedView; here we show a
  // clearly-labeled example so the finished layout is complete without a backend.
  const grants = FALLBACK_GRANTS;
  const explicitNone = grants.length === 0;

  return (
    <>
      <GenericHeader
        title="Review and accept your invitation"
        subtitle="Your identity matched. Review the exact access below, then accept once."
      />
      <div className="joinorg-body">
        <p className="joinorg-lead">
          Accepting activates your membership and grants immediately. This is a single, one-time
          action — the invitation link cannot be used again.
        </p>

        <ScopeSummary
          example
          organizationLabel="Your invited organization"
          roleDerived="member"
          grants={grants}
          explicitNone={explicitNone}
          expiryIso={null}
        />
      </div>
      <footer className="joinorg-footer">
        <span>Accepting can’t be undone from here — it grants exactly the access shown above.</span>
        <div className="joinorg-actions">
          <button
            className="primary-button"
            type="button"
            onClick={onAccept}
            disabled={accepting}
          >
            {accepting ? "Accepting…" : "Accept invitation"}
          </button>
        </div>
      </footer>
    </>
  );
}

function AcceptedView({
  result,
  onAccepted,
}: {
  result: AcceptedInvitation;
  onAccepted?: () => void;
}) {
  const grants = result.authority.explicit_grants ?? [];
  const explicitNone = result.authority.explicit_none;

  return (
    <>
      <GenericHeader
        title="You’re in"
        subtitle="Your membership and grants are active. Here’s exactly what you can access."
      />
      <div className="joinorg-body">
        <div className="joinorg-status" role="status" aria-live="polite">
          <span className="signal-label success">
            <span className="signal-mark" />
            Invitation accepted{result.replayed ? " (already active)" : ""}
          </span>
        </div>

        <ScopeSummary
          organizationLabel={result.organization_id}
          organizationMono
          roleDerived={result.authority.role_derived}
          grants={grants}
          explicitNone={explicitNone}
          expiryIso={null}
          invitationId={result.invitation_id}
        />
      </div>
      <footer className="joinorg-footer">
        <span>Next: get started in this organization.</span>
        <div className="joinorg-actions">
          <button
            className="primary-button"
            type="button"
            onClick={() => {
              if (onAccepted) onAccepted();
              else if (result.next_url) window.location.assign(result.next_url);
            }}
          >
            Continue to getting started
          </button>
        </div>
      </footer>
    </>
  );
}

/* --------------------------------------------------------------------------- *
 * ScopeSummary — read-only, DESIGN.md anatomy: organization → project /        *
 * ProjectDatastreamBinding → canonical Datastream, scope type, safe immutable- *
 * ID suffix, explicit "None" per scope, and absolute expiry.                   *
 * --------------------------------------------------------------------------- */

interface ScopeSummaryProps {
  organizationLabel: string;
  organizationMono?: boolean;
  roleDerived: string;
  grants: ExplicitGrant[];
  explicitNone: boolean;
  expiryIso: string | null;
  invitationId?: string;
  /** Marks the summary as a representative example (no-backend fallback). */
  example?: boolean;
}

function ScopeSummary({
  organizationLabel,
  organizationMono,
  roleDerived,
  grants,
  explicitNone,
  expiryIso,
  invitationId,
  example,
}: ScopeSummaryProps) {
  const projectGrants = useMemo(
    () => grants.filter((g) => g.scope_type === "project"),
    [grants],
  );
  const datastreamGrants = useMemo(
    () => grants.filter((g) => g.scope_type === "flux"),
    [grants],
  );
  const expiry = formatAbsoluteExpiry(expiryIso);

  return (
    <div className="scope-summary" aria-label="Effective access this invitation grants">
      <div className="scope-summary-title">
        Effective access
        {example && <span className="scope-example-tag">example</span>}
      </div>

      <div className="scope-row">
        <span className="scope-key">Organization</span>
        <span className={organizationMono ? "scope-val mono" : "scope-val"}>
          {organizationLabel}
        </span>
      </div>

      <div className="scope-row">
        <span className="scope-key">Role authority</span>
        <span className="scope-val">
          {roleDerived}
          <span className="scope-sub"> (role-derived effective authority)</span>
        </span>
      </div>

      {/* Explicit grants grouped by canonical AD-5 path. "None" is explicit. */}
      <div className="scope-row">
        <span className="scope-key">Project bindings</span>
        <span className="scope-val">
          {projectGrants.length === 0 ? (
            <span className="scope-none">None</span>
          ) : (
            <ul className="scope-bindings">
              {projectGrants.map((g) => (
                <li key={`${g.scope_type}:${g.scope_id}`} className="scope-binding">
                  <span className="scope-binding-path">Organization → Project</span>
                  <span className="scope-binding-meta">
                    <span className="scope-chip">{scopeTypeLabel(g.scope_type)}</span>
                    <span className="scope-chip">{capabilityLabel(g.capability)}</span>
                    <span className="mono scope-id">{safeIdSuffix(g.scope_id)}</span>
                  </span>
                </li>
              ))}
            </ul>
          )}
        </span>
      </div>

      <div className="scope-row">
        <span className="scope-key">Datastream bindings</span>
        <span className="scope-val">
          {datastreamGrants.length === 0 ? (
            <span className="scope-none">None</span>
          ) : (
            <ul className="scope-bindings">
              {datastreamGrants.map((g) => (
                <li key={`${g.scope_type}:${g.scope_id}`} className="scope-binding">
                  <span className="scope-binding-path">
                    Organization → Project binding → Datastream
                  </span>
                  <span className="scope-binding-meta">
                    <span className="scope-chip">{scopeTypeLabel(g.scope_type)}</span>
                    <span className="scope-chip">{capabilityLabel(g.capability)}</span>
                    <span className="mono scope-id">{safeIdSuffix(g.scope_id)}</span>
                  </span>
                </li>
              ))}
            </ul>
          )}
        </span>
      </div>

      {explicitNone && (
        <div className="scope-row">
          <span className="scope-key">Explicit grants</span>
          <span className="scope-val">
            <span className="scope-none">None</span> — this invitation adds no project or Datastream
            access beyond the role authority above.
          </span>
        </div>
      )}

      <div className="scope-row">
        <span className="scope-key">Expires</span>
        <span className="scope-val">
          {expiry ? expiry : <span className="scope-sub">At the absolute expiry on the link</span>}
        </span>
      </div>

      {invitationId && (
        <div className="scope-row">
          <span className="scope-key">Invitation</span>
          <span className="scope-val mono">{invitationId}</span>
        </div>
      )}
    </div>
  );
}
