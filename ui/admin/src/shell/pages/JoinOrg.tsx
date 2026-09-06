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
 *   POST /api/invitations/exchange returns an exact secret-free preview only after
 *   verified subject matching, plus the narrow HttpOnly acceptance cookie.
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
 * Network failure is explicit and never fabricates pending authority.
 *
 * Styling: migrated off `join-org.css` (waves 2-4 of `docs/ui-css-strategy.md`).
 * The centered public/email column is Tailwind utilities over the `@theme`
 * tokens plus the `Button` / `Input` / `Status` / `Alert` / `Skeleton`
 * primitives. Colors come exclusively from theme tokens — no hex.
 */
import { type ReactElement, useEffect, useMemo, useRef, useState } from "react";
import { apiFetch } from "../../lib/apiFetch";
import { Alert, Button, Input, Skeleton, Status, TONE_TEXT, formatTimestamp } from "../../ui";

/* ---- Centered public/email column (Surface State Matrix: no product nav) ----
   Ported 1:1 from the retired `join-org.css`. Geometry (not prose) keeps its px
   values; prose measures stay in `ch`. The identity fieldset and the field
   classes are kept identical to CreateOrg's on purpose: two screens asking the
   same thing must ask it the same way. */
const STAGE = "relative min-h-screen bg-background-light";
const SCRIM = "relative inset-0 grid items-start justify-items-center px-7 py-12";
const DIALOG =
  "w-[min(600px,calc(100%-56px))] overflow-hidden rounded-[18px] border border-divider-base bg-surface-light shadow-overlay max-[1180px]:w-[calc(100%-36px)]";
const HEADER = "flex items-start justify-between gap-4.5 border-b border-divider-base px-7 pb-5 pt-6";
const TITLE = "m-0 font-display text-[22px] font-semibold tracking-[-0.01em]";
const SUBTITLE = "m-0 mt-2 max-w-[46ch] text-label leading-normal text-text-secondary";
const BODY = "grid gap-5.5 px-7 py-6";
const LEAD = "m-0 text-ui leading-[1.6]";
const NOTE = "m-0 text-caption leading-[1.55] text-text-secondary";
const FOOTER =
  "flex items-center justify-between gap-4.5 border-t border-divider-base px-7 py-4.5 max-[1180px]:flex-wrap";
const FOOTER_NOTE = "text-caption leading-snug text-text-secondary";
const ACTIONS = "flex flex-none items-center gap-2.5";

/* Field group — same recipe as CreateOrg; change them together. */
const FIELD = "grid gap-2";
const FIELD_LABEL = "text-label font-semibold";
const FIELD_HINT = "m-0 text-caption leading-snug text-text-secondary";
const FIELD_ERROR = `m-0 text-caption font-semibold leading-snug ${TONE_TEXT.error}`;

/* ScopeSummary (DESIGN.md: page surface, read-only). The first row after the
   title takes SCOPE_ROW; every following row takes SCOPE_ROW_RULED. */
const SCOPE_SUMMARY = "rounded-lg border border-divider-base bg-background-light px-4.5 py-4";
const SCOPE_TITLE = "mb-3 flex items-center gap-2.5 font-display text-label font-semibold";
const SCOPE_ROW =
  "grid grid-cols-[140px_minmax(0,1fr)] items-baseline gap-3.5 py-[9px] max-[1180px]:grid-cols-1 max-[1180px]:gap-[3px]";
const SCOPE_ROW_RULED = `${SCOPE_ROW} border-t border-divider-base`;
const SCOPE_KEY = "text-caption font-semibold text-text-secondary";
const SCOPE_VAL = "text-label leading-normal [overflow-wrap:anywhere]";
const SCOPE_VAL_MONO = `${SCOPE_VAL} font-mono`;
const SCOPE_SUB = "text-text-secondary";
const SCOPE_NONE = "font-bold text-text-secondary";

/* Grant bindings, grouped by canonical AD-5 path. */
const BINDINGS = "m-0 grid list-none gap-2.5 p-0";
const BINDING = "grid gap-1.5 rounded-sm border border-divider-base bg-surface-light px-3 py-2.5";
const BINDING_PATH = "text-caption font-semibold text-text-secondary";
const BINDING_META = "flex flex-wrap items-center gap-2";
const CHIP =
  "rounded-pill border border-divider-base bg-background-light px-2 py-0.5 text-[11px] font-bold";
const SCOPE_ID = "font-mono text-caption text-text-secondary";

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
/** Exact secret-free authority returned by POST /api/invitations/exchange. */
interface InvitationPreview {
  organization_id: string | null;
  organization_label: string | null;
  authority: AcceptedAuthority;
  expires_at: string;
}

interface AcceptedInvitation {
  invitation_id: string;
  /**
   * NULL for an ENTRY invitation (migration 106): the person was invited to the
   * platform, not to somebody's organization, so there is no membership and they
   * create their own next. This was typed `string` while the server could only
   * ever send one; it can now send null.
   */
  organization_id: string | null;
  authority: AcceptedAuthority;
  next_url: string;
  replayed: boolean;
  /**
   * Who this person is. `needs_name` is true when neither the identity token nor
   * a previous visit gave us one — the console then asks, here, once. Optional so
   * an older server (or a replayed cached response) simply does not ask.
   */
  profile?: { display_name: string | null; needs_name: boolean };
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
  | { status: "ready"; preview: InvitationPreview }
  | { status: "accepting"; preview: InvitationPreview }
  | { status: "unavailable" } // mismatched · expired · replayed · revoked
  | { status: "conflict" } // valid link, but existing access conflicts
  | { status: "error"; message: string; retry: "exchange" | "accept"; preview?: InvitationPreview }
  | { status: "accepted"; result: AcceptedInvitation };

let invitationFragmentCapture: { locationKey: string; bearer: string } | null = null;

/** Read the single-use bearer from the URL fragment (#invite=<bearer>), then strip
 *  it from history so it is never re-sent, bookmarked, or leaked via referrer. */
function readBearerFromFragment(): string {
  if (typeof window === "undefined") return "";
  const hash = window.location.hash || "";
  const locationKey = window.location.pathname + window.location.search;
  const bearer = hash.startsWith("#invite=") ? hash.slice("#invite=".length) : "";
  if (bearer) {
    // The bearer lives ONLY in the fragment; remove it immediately (spec).
    window.history.replaceState(null, "", window.location.pathname + window.location.search);
    invitationFragmentCapture = { locationKey, bearer };
    return bearer;
  }
  if (invitationFragmentCapture?.locationKey === locationKey) {
    return invitationFragmentCapture.bearer;
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

/** Format an absolute expiry (ISO 8601) for display; falls back to the raw
 *  string. The locale was the READER'S until 76-1, so the same invitation read
 *  differently on two machines; it is the console's now, and the zone is still
 *  named — an expiry a person cannot place in time is not an expiry. */
function formatAbsoluteExpiry(iso: string | null): string {
  return iso ? formatTimestamp(iso) : "";
}

/** POST the fragment bearer for its matching verified subject. A 404 is the single
 *  nondisclosing outcome for mismatched/expired/replayed/revoked links. */
type ExchangeOutcome =
  | { kind: "ready"; preview: InvitationPreview }
  | { kind: "signin_required" }
  | { kind: "unavailable" }
  | { kind: "error"; message: string };

function isInvitationPreview(value: unknown): value is InvitationPreview {
  if (typeof value !== "object" || value === null) return false;
  const candidate = value as Record<string, unknown>;
  if (typeof candidate.authority !== "object" || candidate.authority === null) return false;
  const authority = candidate.authority as Record<string, unknown>;
  const grants = Array.isArray(authority.explicit_grants) ? authority.explicit_grants : null;
  const validGrants =
    grants !== null &&
    grants.every(
      (grant) =>
        typeof grant === "object" &&
        grant !== null &&
        ["project", "flux"].includes(String((grant as Record<string, unknown>).scope_type)) &&
        typeof (grant as Record<string, unknown>).scope_id === "string" &&
        ["view", "edit", "manage"].includes(
          String((grant as Record<string, unknown>).capability),
        ),
    );
  return (
    (typeof candidate.organization_id === "string" || candidate.organization_id === null) &&
    (typeof candidate.organization_label === "string" || candidate.organization_label === null) &&
    typeof candidate.expires_at === "string" &&
    typeof authority.role_derived === "string" &&
    typeof authority.explicit_none === "boolean" &&
    validGrants &&
    grants !== null &&
    authority.explicit_none === (grants.length === 0)
  );
}

async function exchangeBearer(bearer: string): Promise<ExchangeOutcome> {
  const resp = await apiFetch("/api/invitations/exchange", {
    method: "POST",
    credentials: "same-origin",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ bearer }),
  });
  if (resp.ok) {
    const body = (await resp.json().catch(() => null)) as { preview?: unknown } | null;
    if (body && isInvitationPreview(body.preview)) return { kind: "ready", preview: body.preview };
    return { kind: "unavailable" };
  }
  if (resp.status === 401) return { kind: "signin_required" };
  if (resp.status >= 500) return { kind: "error", message: "The invitation service is unavailable." };
  // 404 (and any other non-2xx) → nondisclosing "unavailable". No detail is surfaced.
  return { kind: "unavailable" };
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


export interface JoinOrgProps {
  /** Bearer from the URL fragment. When omitted, the screen reads it itself. */
  token?: string;
  /** Called after a successful accept — the subject lands with membership active. */
  onAccepted?: (nextUrl: string) => void;
}

export default function JoinOrg({ token, onAccepted }: JoinOrgProps) {
  const [bearer, setBearer] = useState<string | null>(token ?? null);
  const [state, setState] = useState<JoinState>({ status: "validating" });
  const [idempotencyKey] = useState<string>(newIdempotencyKey);
  const [exchangeAttempt, setExchangeAttempt] = useState(0);
  const exchangeRequest = useRef<{ attempt: number; bearer: string; promise: Promise<ExchangeOutcome> } | null>(null);

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
    let request = exchangeRequest.current;
    if (!request || request.attempt !== exchangeAttempt || request.bearer !== bearer) {
      if (invitationFragmentCapture?.bearer === bearer) {
        invitationFragmentCapture = null;
      }
      request = { attempt: exchangeAttempt, bearer, promise: exchangeBearer(bearer) };
      exchangeRequest.current = request;
    }
    let cancelled = false;
    setState({ status: "validating" });
    request.promise
      .then((outcome) => {
        if (cancelled) return;
        if (outcome.kind === "ready") setState({ status: "ready", preview: outcome.preview });
        else if (outcome.kind === "signin_required") setState({ status: "signin_required" });
        else if (outcome.kind === "error") {
          setState({ status: "error", message: outcome.message, retry: "exchange" });
        } else setState({ status: "unavailable" });
      })
      .catch(() => {
        if (!cancelled) setState({ status: "error", message: "The invitation service is unavailable.", retry: "exchange" });
      });
    return () => {
      cancelled = true;
    };
  }, [bearer, exchangeAttempt]);

  async function handleAccept(retryPreview?: InvitationPreview) {
    const preview = retryPreview ?? (state.status === "ready" ? state.preview : undefined);
    if (!preview) return;
    setState({ status: "accepting", preview });
    try {
      const outcome = await acceptOnce(idempotencyKey);
      if (outcome.kind === "accepted") {
        setState({ status: "accepted", result: outcome.result });
        // Notify the parent NOW only when there is nothing left to ask. When a
        // name is still needed the parent must not route away yet, or the
        // question would be asked on a screen nobody ever sees — AcceptedView
        // calls onAccepted itself once the name is saved.
      } else if (outcome.kind === "conflict") {
        setState({ status: "conflict" });
      } else if (outcome.kind === "signin_required") {
        setState({ status: "signin_required" });
      } else if (outcome.kind === "unavailable") {
        setState({ status: "unavailable" });
      } else {
        setState({ status: "error", message: outcome.message, retry: "accept", preview });
      }
    } catch {
      // error rather than fabricating a membership.
      setState({
        status: "error",
        message: "The invitation service is unavailable. Please try again shortly.",
        retry: "accept",
        preview,
      });
    }
  }


  function retryExchange() {
    setExchangeAttempt((attempt) => attempt + 1);
  }
  return (
    <div className={STAGE}>
      <div className={SCRIM}>
        <section
          className={DIALOG}
          role="dialog"
          aria-modal="true"
          aria-labelledby="joinorg-title"
        >
          {renderBody(state, handleAccept, retryExchange, onAccepted)}
        </section>
      </div>
    </div>
  );
}

/** Renders the surface for the current state. Kept as a function (not nested
 *  components) so each state is a flat, reviewable block against the spec. */
function renderBody(
  state: JoinState,
  onAccept: (preview?: InvitationPreview) => void,
  onRetryExchange: () => void,
  onAccepted?: (nextUrl: string) => void,
): ReactElement {
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
      return (
        <ErrorView
          message={state.message}
          onRetry={
            state.retry === "accept" && state.preview
              ? () => onAccept(state.preview)
              : onRetryExchange
          }
        />
      );
    case "ready":
    case "accepting":
      return <ReviewAndAcceptView preview={state.preview} accepting={state.status === "accepting"} onAccept={() => onAccept()} />;
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
    <header className={HEADER}>
      <div>
        <h1 id="joinorg-title" className={TITLE}>{title}</h1>
        <p className={SUBTITLE}>{subtitle}</p>
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
      <div className={BODY}>
        <div className="grid gap-3.5" role="status" aria-live="polite">
          <Status tone="info">Validating the invitation…</Status>
          <div className="grid gap-2.5" aria-hidden="true">
            <Skeleton className="h-3.5 w-full rounded-sm" />
            <Skeleton className="h-3.5 w-[82%] rounded-sm" />
            <Skeleton className="h-3.5 w-[64%] rounded-sm" />
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
      <div className={BODY}>
        <p className={LEAD}>
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
      <div className={BODY}>
        <p className={LEAD}>
          To keep invitations private, nothing about the organization, projects, or Datastreams is
          shown until you sign in with the invited identity. Your invitation details appear only
          after your verified identity matches.
        </p>
        <div className="grid gap-3.5" role="status">
          <Status tone="info">Sign in with the invited address, then reopen this link.</Status>
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
      <div className={BODY}>
        <Alert tone="error" title="No access was granted">
          This invitation link is no longer usable. For your privacy, we don’t reveal whether it
          expired, was already accepted, was revoked, or was meant for a different address — the
          outcome is the same: nothing was changed and no access was granted.
        </Alert>
        <p className={NOTE}>
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
      <div className={BODY}>
        {/* The legacy notice carried role="alert"; `Status` gives a warning
            role="status", so the role is restated to keep the interruption. */}
        <div role="alert">
          <Alert tone="warning" title="Nothing was changed">
            This invitation conflicts with access you already have in this organization, so it was
            not applied. Your existing membership and grants are unchanged.
          </Alert>
        </div>
        <p className={NOTE}>
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
      <div className={BODY}>
        <Alert tone="error" title="Invitation not accepted">
          {message}
        </Alert>
      </div>
      <footer className={FOOTER}>
        <span className={FOOTER_NOTE}>No changes were made. You can retry the acceptance.</span>
        <div className={ACTIONS}>
          <Button type="button" onClick={onRetry}>
            Try again
          </Button>
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
  preview,
  accepting,
  onAccept,
}: {
  preview: InvitationPreview;
  accepting: boolean;
  onAccept: () => void;
}) {
  const grants = preview.authority.explicit_grants;
  const explicitNone = preview.authority.explicit_none;

  return (
    <>
      <GenericHeader
        title="Review and accept your invitation"
        subtitle="Your identity matched. Review the exact access below, then accept once."
      />
      <div className={BODY}>
        <p className={LEAD}>
          Accepting activates your membership and grants immediately. This is a single, one-time
          action — the invitation link cannot be used again.
        </p>

        {/* THE ORGANIZATION'S NAME, OR THE PLATFORM. `app.organizations.name`
            is `NOT NULL` (migration 035) and `exchange_invitation` joins it
            beside `org_id`, so a label is served whenever an organization is
            scoped; `organization_label` is null exactly when `org_id` is, which
            IS "Platform access". The `?? preview.organization_id` this line
            carried was unreachable, and could only ever have shown `org_<ULID>`
            to a person deciding whether to join. */}
        <ScopeSummary
          organizationLabel={preview.organization_label ?? "Platform access"}
          roleDerived={preview.authority.role_derived}
          grants={grants}
          explicitNone={explicitNone}
          expiryIso={preview.expires_at}
        />
      </div>
      <footer className={FOOTER}>
        <span className={FOOTER_NOTE}>
          Accepting can’t be undone from here — it grants exactly the access shown above.
        </span>
        <div className={ACTIONS}>
          <Button type="button" onClick={onAccept} disabled={accepting}>
            {accepting ? "Accepting…" : "Accept invitation"}
          </Button>
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
  onAccepted?: (nextUrl: string) => void;
}) {
  const grants = result.authority.explicit_grants ?? [];
  const explicitNone = result.authority.explicit_none;

  // An ENTRY invitation grants no membership: nothing to summarise, and the next
  // step is creating an organization, not entering one.
  const isEntry = !result.organization_id;

  // The name, asked HERE and only when nobody could give it to us. Acceptance is
  // the one moment both kinds of arrival pass through, which is why the question
  // lives on this screen rather than on create-organization.
  // Two inputs, same labels and autocomplete as CreateOrg: two screens asking the
  // same thing must ask it the same way.
  const [firstName, setFirstName] = useState("");
  const [lastName, setLastName] = useState("");
  const [nameSaved, setNameSaved] = useState(false);
  const [savingName, setSavingName] = useState(false);
  const [nameError, setNameError] = useState<string | null>(null);

  const mustAskName = (result.profile?.needs_name ?? false) && !nameSaved;
  const displayName = `${firstName.trim()} ${lastName.trim()}`.trim();
  const nameReady = !!firstName.trim() && !!lastName.trim() && displayName.length <= 255;

  async function proceed() {
    // The name goes first. If it fails we say so and stay put rather than routing
    // on silently — arriving named is the point of asking.
    if (mustAskName) {
      if (!nameReady) return;
      setSavingName(true);
      setNameError(null);
      try {
        const resp = await apiFetch("/api/me/profile", {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ display_name: displayName }),
        });
        if (!resp.ok) {
          const body = (await resp.json().catch(() => ({}))) as { message?: string };
          throw new Error(body.message ?? `Could not save your name (HTTP ${resp.status}).`);
        }
        setNameSaved(true);
      } catch (err) {
        setNameError(err instanceof Error ? err.message : String(err));
        return;
      } finally {
        setSavingName(false);
      }
    }
    if (onAccepted) onAccepted(result.next_url);
    else if (result.next_url) window.location.assign(result.next_url);
  }

  return (
    <>
      <GenericHeader
        title="You’re in"
        subtitle={
          isEntry
            ? "Your invitation is accepted. Next, create the organization your data will live in."
            : "Your membership and grants are active. Here’s exactly what you can access."
        }
      />
      <div className={BODY}>
        <div className="grid gap-3.5" role="status" aria-live="polite">
          <Status tone="success">
            Invitation accepted{result.replayed ? " (already active)" : ""}
          </Status>
        </div>

        {isEntry ? (
          <p className={LEAD}>
            This invitation is to toorow itself, not to an existing organization — so there is
            no membership to show yet. You will create your organization next, and you will be
            its owner.
          </p>
        ) : (
          <ScopeSummary
            organizationLabel={result.organization_id ?? ""}
            organizationMono
            roleDerived={result.authority.role_derived}
            grants={grants}
            explicitNone={explicitNone}
            expiryIso={null}
            invitationId={result.invitation_id}
          />
        )}

        {mustAskName && (
          <fieldset className="m-0 grid gap-3 rounded-lg border border-divider-base px-4.5 py-4">
            <legend className="px-1.5 text-label font-semibold">Your name</legend>
            <p className={FIELD_HINT}>
              We only know your email address. Your name identifies you to your colleagues and to
              anyone you invite.
            </p>
            <div className="grid grid-cols-2 gap-3.5 max-[1180px]:grid-cols-1">
              <div className={FIELD}>
                <label htmlFor="joinorg-firstname" className={FIELD_LABEL}>First name</label>
                <Input
                  id="joinorg-firstname"
                  type="text"
                  value={firstName}
                  maxLength={120}
                  autoComplete="given-name"
                  placeholder="Jean"
                  onChange={(e) => {
                    setFirstName(e.target.value);
                    setNameError(null);
                  }}
                />
              </div>
              <div className={FIELD}>
                <label htmlFor="joinorg-lastname" className={FIELD_LABEL}>Last name</label>
                <Input
                  id="joinorg-lastname"
                  type="text"
                  value={lastName}
                  maxLength={120}
                  autoComplete="family-name"
                  placeholder="Albany"
                  onChange={(e) => {
                    setLastName(e.target.value);
                    setNameError(null);
                  }}
                />
              </div>
            </div>
            {nameError && (
              <p className={FIELD_ERROR} role="alert">
                {nameError}
              </p>
            )}
          </fieldset>
        )}
      </div>
      <footer className={FOOTER}>
        <span className={FOOTER_NOTE}>
          {isEntry ? "Next: create your organization." : "Next: get started in this organization."}
        </span>
        <div className={ACTIONS}>
          <Button
            type="button"
            disabled={savingName || (mustAskName && !nameReady)}
            onClick={() => {
              void proceed();
            }}
          >
            {savingName
              ? "Saving…"
              : isEntry
                ? "Create your organization"
                : "Continue to getting started"}
          </Button>
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
}

function ScopeSummary({
  organizationLabel,
  organizationMono,
  roleDerived,
  grants,
  explicitNone,
  expiryIso,
  invitationId,
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
    <div className={SCOPE_SUMMARY} aria-label="Effective access this invitation grants">
      <div className={SCOPE_TITLE}>
        Effective access
      </div>

      <div className={SCOPE_ROW}>
        <span className={SCOPE_KEY}>Organization</span>
        <span className={organizationMono ? SCOPE_VAL_MONO : SCOPE_VAL}>
          {organizationLabel}
        </span>
      </div>

      <div className={SCOPE_ROW_RULED}>
        <span className={SCOPE_KEY}>Role authority</span>
        <span className={SCOPE_VAL}>
          {roleDerived}
          <span className={SCOPE_SUB}> (role-derived effective authority)</span>
        </span>
      </div>

      {/* Explicit grants grouped by canonical AD-5 path. "None" is explicit. */}
      <div className={SCOPE_ROW_RULED}>
        <span className={SCOPE_KEY}>Project bindings</span>
        <span className={SCOPE_VAL}>
          {projectGrants.length === 0 ? (
            <span className={SCOPE_NONE}>None</span>
          ) : (
            <ul className={BINDINGS}>
              {projectGrants.map((g) => (
                <li key={`${g.scope_type}:${g.scope_id}`} className={BINDING}>
                  <span className={BINDING_PATH}>Organization → Project</span>
                  <span className={BINDING_META}>
                    <span className={CHIP}>{scopeTypeLabel(g.scope_type)}</span>
                    <span className={CHIP}>{capabilityLabel(g.capability)}</span>
                    <span className={SCOPE_ID}>{safeIdSuffix(g.scope_id)}</span>
                  </span>
                </li>
              ))}
            </ul>
          )}
        </span>
      </div>

      <div className={SCOPE_ROW_RULED}>
        <span className={SCOPE_KEY}>Datastream bindings</span>
        <span className={SCOPE_VAL}>
          {datastreamGrants.length === 0 ? (
            <span className={SCOPE_NONE}>None</span>
          ) : (
            <ul className={BINDINGS}>
              {datastreamGrants.map((g) => (
                <li key={`${g.scope_type}:${g.scope_id}`} className={BINDING}>
                  <span className={BINDING_PATH}>
                    Organization → Project binding → Datastream
                  </span>
                  <span className={BINDING_META}>
                    <span className={CHIP}>{scopeTypeLabel(g.scope_type)}</span>
                    <span className={CHIP}>{capabilityLabel(g.capability)}</span>
                    <span className={SCOPE_ID}>{safeIdSuffix(g.scope_id)}</span>
                  </span>
                </li>
              ))}
            </ul>
          )}
        </span>
      </div>

      {explicitNone && (
        <div className={SCOPE_ROW_RULED}>
          <span className={SCOPE_KEY}>Explicit grants</span>
          <span className={SCOPE_VAL}>
            <span className={SCOPE_NONE}>None</span> — this invitation adds no project or Datastream
            access beyond the role authority above.
          </span>
        </div>
      )}

      <div className={SCOPE_ROW_RULED}>
        <span className={SCOPE_KEY}>Expires</span>
        <span className={SCOPE_VAL}>
          {expiry ? expiry : <span className={SCOPE_SUB}>At the absolute expiry on the link</span>}
        </span>
      </div>

      {invitationId && (
        <div className={SCOPE_ROW_RULED}>
          <span className={SCOPE_KEY}>Invitation</span>
          <span className={SCOPE_VAL_MONO}>{invitationId}</span>
        </div>
      )}
    </div>
  );
}
