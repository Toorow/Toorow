/**
 * User Account — the person-scoped global surface (Story 46.4).
 *
 * Three URL-backed sections: Profile, Authorizations and Actions. It works
 * without a Project and without an Organization.
 *
 * `Profile` also carries **Preferences** and **Session** (2026-08-18,
 * `page-structure.md §G.3`). They are panels of the Profile section and NOT
 * fourth and fifth addresses: a URL-backed section means a new `AccountSection`
 * in `router.tsx`, and `README.md:84` describes three. What Preferences holds is
 * exactly what is real without a server — the appearance switch, which is
 * browser state (`shell/themeMode.ts`). No locale, no timezone, no density:
 * `me_api.py` serves `identity`, `display_name`, `email`, `avatar_url`,
 * `avatar_source`, `updated_at` and nothing else, and a control the server
 * cannot keep is a promise the product does not hold.
 *
 * `Session` holds the two gestures a person can make on their OWN sessions, and
 * they are NOT the same act (`organization-settings.md:138-142`, ratified
 * 2026-08-17). Signing out closes the window it was clicked in and leaves my
 * other windows open — `POST /api/auth/logout`. Signing out everywhere posts a
 * bound on me: every ticket minted before that instant is refused at its next
 * call, here included — `POST /api/me/sessions/revoke`. Two buttons that read
 * "Sign out" and called the same route made the second gesture invisible, which
 * is the state 67-15's review rejected. The third ratified gesture — ending a
 * membership, which cuts the sessions of the person whose membership ended — is
 * an administrator's act and lives in Organization Settings &gt; Members.
 *
 * `Actions` holds the irreversible one. Its contract is pinned by
 * `__tests__/DangerZone.test.tsx` and is not cosmetic:
 *   - the erasure preview is fetched when the zone is OPENED, never on mount;
 *   - the manifest names the identity, the memberships removed and what is
 *     RETAINED — the promise of erasure has to be exact;
 *   - the delete button stays disabled until the email is typed EXACTLY;
 *   - a failed preview is reported as a failure and offers no erasure;
 *   - the sole-owner case is explained before the attempt, and the server's 409
 *     is still handled at confirmation time.
 */
import { useEffect, useRef, useState } from "react";
import { ApiError, apiGet, apiJson } from "../../lib/apiFetch";
import { Badge, Button, EmptyState, Input, Label, Panel, PanelHeader, Retry, Status, wireWord } from "../../ui";
import GlobalScopeLayout from "../GlobalScopeLayout";
import { AppearanceControl, signOutThisWindow } from "../StableSidebar";
import AuthorizationsPanel from "../../authorizations/AuthorizationsPanel";
import type { AccountSection } from "../router";

export interface AccountBlocker { kind: string; detail: string }
export interface AccountMembership {
  org_id: string;
  org_name: string;
  role: string;
  other_active_members: number;
}
export interface AccountDeletionPreview {
  identity: string;
  email: string;
  memberships: AccountMembership[];
  sole_owner_of: Array<{ org_id: string; org_name: string }>;
  blockers: AccountBlocker[];
}
interface AccountDeletionResult {
  deleted: boolean;
  erased?: Record<string, number>;
  retained?: { audit_entries?: number; reason?: string };
}
interface Profile { identity?: string; display_name?: string | null; email?: string | null; avatar_url?: string | null }
interface OrgRef { id: string; name: string }
export interface AccountSettingsProps {
  section?: AccountSection;
  onSectionChange?: (section: AccountSection) => void;
  onDeleted?: () => void | Promise<void>;
}

const SECTIONS = [
  { key: "profile", label: "Profile", description: "Identity, memberships and sign-out" },
  { key: "authorizations", label: "Authorizations", description: "Authorizations connected by you" },
  { key: "actions", label: "Actions", description: "Governed account erasure" },
] as const;

/** The confirmation phrase the server requires on the DELETE. */
const ACCOUNT_CONFIRM_HEADER = "erase-account";

/** Everything this browser keeps about who is signed in. Both browser auth
 *  modes, because a gesture that cleared one would strand the other. */
function forgetBrowserIdentity(): void {
  localStorage.removeItem("api_token");
  sessionStorage.removeItem("toorow_browser_identity");
}

export async function clearDeletedAccountSession(
  navigate: () => void = () => window.location.assign("/"),
): Promise<void> {
  forgetBrowserIdentity();
  try {
    await fetch("/api/auth/logout", { method: "POST", credentials: "same-origin" });
  } catch {
    /* Local erasure still wins. */
  } finally {
    navigate();
  }
}

/**
 * Sign out EVERYWHERE — the second gesture, and the only one that reaches a
 * window this browser does not hold.
 *
 * It is a different request from `signOutThisWindow`, not a louder one: the
 * route posts a "not before" bound on the person, so every ticket minted before
 * this instant is refused at its next call. This window's own ticket is one of
 * them, which is why the browser leaves afterwards — staying on a screen whose
 * next read will be refused is the same lie as a logout that does not log out.
 *
 * It throws on refusal instead of leaving quietly: a person who is told "signed
 * out everywhere" while the laptop they no longer hold keeps reading has been
 * told the one thing that must be true.
 */
export async function signOutEverywhere(
  navigate: () => void = () => window.location.assign("/"),
): Promise<number> {
  const { revoked } = await apiJson<{ revoked: number }>("/api/me/sessions/revoke", {
    method: "POST",
    credentials: "same-origin",
  });
  // The route clears the session cookie itself; what is left is this browser's
  // own copy of who was signed in.
  forgetBrowserIdentity();
  navigate();
  return revoked;
}

export default function AccountSettings({
  section = "profile",
  onSectionChange = () => undefined,
  onDeleted,
}: AccountSettingsProps = {}) {
  const [profile, setProfile] = useState<Profile | null>(null);
  const [orgs, setOrgs] = useState<OrgRef[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  // Note what is NOT here: the deletion preview. Reading what erasure would
  // remove is the first step of the destructive flow, so it belongs to the
  // zone, not to page load.
  //
  // Nor `/api/me/authorizations`. `AuthorizationsPanel` has read it itself since
  // it was mounted here, with its own loading, denied, error and empty states;
  // the copy this page kept was a second request whose answer reached no pixel
  // and whose failure raised a vaguer banner above the panel's precise one.
  // THE READ THIS SCREEN'S ERROR BLOCK OFFERS TO REPEAT (76-4). An error
  // that names no way forward is a dead end; this token is what `Retry`
  // moves, and the effect below is the read it re-runs.
  const [reloadToken, setReloadToken] = useState(0);
  useEffect(() => {
    if (section !== "profile") return;
    let active = true;
    setError(null);
    Promise.all([
      apiGet<Profile>("/api/me/profile"),
      apiGet<{ organizations?: OrgRef[] } | OrgRef[]>("/api/organizations"),
    ])
      .then(([me, payload]) => {
        if (!active) return;
        setProfile(me);
        setOrgs(Array.isArray(payload) ? payload : (payload.organizations ?? []));
      })
      .catch(() => {
        if (active) setError("Account evidence is unavailable. No empty state was inferred.");
      });
    return () => { active = false; };
  }, [section, reloadToken]);

  return (
    <GlobalScopeLayout
      eyebrow="Personal scope"
      title="User Account"
      description="Your identity and personal authorizations work without selecting a Project."
      scopeLabel={profile?.display_name ?? profile?.email ?? "Signed-in identity"}
      sections={SECTIONS}
      activeSection={section}
      onSectionChange={onSectionChange}
    >
      {error ? <Status as="block" tone="error" title="Account unavailable"
          action={<Retry onClick={() => setReloadToken((token) => token + 1)} />}
        >{error}</Status> : null}
      {section === "profile" ? (
        <>
          <ProfileSection
            profile={profile}
            orgs={orgs}
            hasError={error !== null}
            onSaved={setProfile}
          />
          <PreferencesPanel />
          <SessionPanel />
        </>
      ) : null}
      {section === "authorizations" ? <AuthorizationsSection /> : null}
      {section === "actions" ? <AccountDangerZone onDeleted={onDeleted} /> : null}
    </GlobalScopeLayout>
  );
}

function Fact({ label, value, mono = false }: { label: string; value: string; mono?: boolean }) {
  return (
    <div>
      <dt className="text-caption text-text-secondary">{label}</dt>
      <dd className={`mt-1 text-ui ${mono ? "font-mono" : ""}`}>{value}</dd>
    </div>
  );
}

function ProfileSection({
  profile, orgs, hasError, onSaved,
}: {
  profile: Profile | null;
  orgs: OrgRef[] | null;
  hasError: boolean;
  onSaved: (profile: Profile) => void;
}) {
  const [displayName, setDisplayName] = useState(profile?.display_name ?? "");
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);

  useEffect(() => setDisplayName(profile?.display_name ?? ""), [profile?.display_name]);

  const saveProfile = async () => {
    setSaving(true);
    setSaveError(null);
    setSaved(false);
    try {
      await apiJson<Profile>("/api/me/profile", {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ display_name: displayName.trim() || null }),
      });
      const accepted = await apiGet<Profile>("/api/me/profile");
      onSaved(accepted);
      setDisplayName(accepted.display_name ?? "");
      setSaved(true);
    } catch (cause) {
      setSaveError(failureMessage(cause));
    } finally {
      setSaving(false);
    }
  };

  return (
    <Panel>
      <PanelHeader
        title="Profile and memberships"
        description="Organization administration and Project grants are owned elsewhere."
      />
      {profile ? (
        <div className="space-y-5 p-5">
          <dl className="grid gap-4 sm:grid-cols-2">
            <Fact label="Display name" value={profile.display_name ?? "Not set"} />
            <Fact label="Email" value={profile.email ?? "Not available"} />
            <Fact label="Identity" value={profile.identity ?? "Not available"} mono />
          </dl>
          <div className="max-w-md space-y-2">
            <Label htmlFor="account-display-name">Display name</Label>
            <div className="flex flex-col gap-2 sm:flex-row">
              <Input
                id="account-display-name"
                value={displayName}
                maxLength={255}
                onChange={(event) => {
                  setDisplayName(event.target.value);
                  setSaved(false);
                }}
              />
              <Button
                type="button"
                disabled={saving || displayName.trim() === (profile.display_name ?? "")}
                onClick={() => void saveProfile()}
              >
                {saving ? "Saving…" : "Save name"}
              </Button>
            </div>
            {saveError ? <Status as="block" tone="error">{saveError}</Status> : null}
            {saved ? <Status tone="success">Display name saved.</Status> : null}
          </div>
          <div>
            <h3 className="text-ui font-semibold">Active Organization memberships</h3>
            {orgs === null ? (
              <p className="mt-2 text-body text-text-secondary">Membership evidence is still loading.</p>
            ) : orgs.length === 0 ? (
              <EmptyState
                title="No active Organization membership"
                description="Membership is granted by an organization's owner or admin, from its own Settings. Nothing is shown in its place."
              />
            ) : (
              <div className="mt-2 flex flex-wrap gap-2">
                {orgs.map((org) => <Badge key={org.id} tone="neutral">{org.name}</Badge>)}
              </div>
            )}
            <p className="mt-2 text-caption text-text-secondary">
              Your role in each Organization is shown in Organization Settings &gt; Members.
            </p>
          </div>
          {/* No sign-out here. It used to sit under the memberships, labelled
              exactly like the one in Session below and calling the same route:
              the same page asked the same thing twice and named neither
              gesture. Leaving is owned by `SessionPanel`. */}
        </div>
      ) : !hasError ? (
        <Status as="block" active title="Loading profile">Verifying personal identity and memberships.</Status>
      ) : null}
    </Panel>
  );
}

/**
 * Exactly what is real without a server: the appearance choice — browser state
 * owned by `shell/themeMode.ts`, the SAME control the rail's account menu
 * renders. `me_api.py` serves no locale, timezone or density, so no such control
 * is offered here (`page-structure.md §G.3`).
 *
 * Sign-out left this panel when the second gesture arrived: revoking every
 * session is a server act, and it cannot sit under a header that promises
 * "nothing here is stored on the server".
 */
function PreferencesPanel() {
  return (
    <Panel>
      <PanelHeader
        title="Preferences"
        description="Choices this browser keeps. Nothing here is stored on the server."
      />
      <div className="space-y-5 p-5" data-testid="account-preferences">
        <div>
          <h3 className="text-ui font-semibold">Appearance</h3>
          <AppearanceControl className="mt-2 max-w-xs" />
        </div>
      </div>
    </Panel>
  );
}

/**
 * The two gestures on my own sessions, told apart by what each one reaches.
 *
 * A refusal here names the gesture that still works rather than the reason the
 * other one did not: a person who cannot cut their other windows can always cut
 * this one, and saying so is more use than a status code.
 *
 * `navigate` is the same seam `clearDeletedAccountSession` carries and for the
 * same reason: the product never passes it, and a test cannot assert on a
 * gesture whose last act is to leave the document.
 */
export function SessionPanel({ navigate }: { navigate?: () => void } = {}) {
  const [revoking, setRevoking] = useState(false);
  const [failure, setFailure] = useState<{ title: string; detail: string } | null>(null);

  async function revokeEverywhere() {
    setRevoking(true);
    setFailure(null);
    try {
      await signOutEverywhere(navigate);
    } catch (err) {
      setRevoking(false);
      setFailure(
        err instanceof ApiError && err.status === 503
          ? {
              title: "Your other windows are still open",
              detail:
                "This deployment cannot cut sessions on demand. Sign out of this window with the " +
                "button above, and ask whoever runs this deployment to turn session revocation on.",
            }
          : {
              title: "Your other windows are still open",
              detail:
                `${failureMessage(err)}. Nothing was signed out. Try again — and if it keeps failing, ` +
                "sign out of this window so at least this one is closed.",
            },
      );
    }
  }

  return (
    <Panel>
      <PanelHeader
        title="Session"
        description="Where you are signed in, and how far a sign-out reaches."
      />
      <div className="space-y-5 p-5" data-testid="account-session">
        <div>
          <h3 className="text-ui font-semibold">This window</h3>
          <p className="mt-1 mb-0 text-body text-text-secondary">
            Closes the window you are in now. Any other window you left signed in stays signed in.
          </p>
          <Button type="button" variant="secondary" className="mt-2" onClick={signOutThisWindow}>
            Sign out
          </Button>
        </div>
        <div>
          <h3 className="text-ui font-semibold">Every window</h3>
          <p className="mt-1 mb-0 text-body text-text-secondary">
            Signs out every window signed in before now — this one, the phone you left at home, the
            laptop you no longer have. You can sign in again straight away.
          </p>
          <Button
            type="button"
            variant="secondary"
            className="mt-2"
            disabled={revoking}
            onClick={() => void revokeEverywhere()}
          >
            {revoking ? "Signing out everywhere…" : "Sign out everywhere"}
          </Button>
          {failure ? (
            <Status as="block" tone="error" title={failure.title} className="mt-3">
              {failure.detail}
            </Status>
          ) : null}
        </div>
      </div>
    </Panel>
  );
}

/** A frame around `AuthorizationsPanel`, which owns the read and every state it
 *  can be in. It is handed nothing because there is nothing left to hand it. */
function AuthorizationsSection() {
  return (
    <Panel>
      <PanelHeader
        title="Your authorizations"
        description="Only Authorizations connected by this identity appear here."
      />
      <div className="p-5">
        <AuthorizationsPanel scope={{ kind: "mine" }} />
      </div>
    </Panel>
  );
}

// ---------------------------------------------------------------------------
// The irreversible surface
// ---------------------------------------------------------------------------

type PreviewState =
  | { status: "idle" }
  | { status: "loading" }
  | { status: "error"; message: string }
  | { status: "ready"; preview: AccountDeletionPreview };

type DeleteState =
  | { status: "idle" }
  | { status: "deleting" }
  | { status: "error"; message: string }
  | { status: "conflict"; message: string }
  | { status: "deleted"; result: AccountDeletionResult };

function failureMessage(err: unknown): string {
  if (err instanceof ApiError) return `${err.message} (HTTP ${err.status})`;
  return err instanceof Error ? err.message : String(err);
}

export function AccountDangerZone({ onDeleted }: { onDeleted?: () => void | Promise<void> }) {
  const [open, setOpen] = useState(false);
  const [attempt, setAttempt] = useState(0);
  const [preview, setPreview] = useState<PreviewState>({ status: "idle" });
  const [confirmText, setConfirmText] = useState("");
  const [del, setDel] = useState<DeleteState>({ status: "idle" });
  const panelRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    let alive = true;
    setPreview({ status: "loading" });
    void (async () => {
      try {
        const data = await apiGet<AccountDeletionPreview>("/api/me/deletion-preview");
        if (alive) setPreview({ status: "ready", preview: data });
      } catch (err) {
        if (alive) setPreview({ status: "error", message: err instanceof ApiError ? err.message : failureMessage(err) });
      }
    })();
    return () => { alive = false; };
  }, [open, attempt]);

  useEffect(() => { if (open) panelRef.current?.focus(); }, [open]);

  function closeZone() {
    setOpen(false);
    setConfirmText("");
    setPreview({ status: "idle" });
    setDel({ status: "idle" });
  }

  async function runDelete() {
    setDel({ status: "deleting" });
    try {
      const result = await apiJson<AccountDeletionResult>("/api/me", {
        method: "DELETE",
        headers: { "X-Confirm-Delete": ACCOUNT_CONFIRM_HEADER },
      });
      setDel({ status: "deleted", result });
      await (onDeleted ?? clearDeletedAccountSession)();
    } catch (err) {
      if (err instanceof ApiError && err.status === 409) {
        // Sole owner of an organization that still has members. Membership can
        // change between the preview and the confirmation, so this is handled
        // even though the preview already warns about it.
        setDel({ status: "conflict", message: err.message });
        setAttempt((n) => n + 1);
        return;
      }
      setDel({ status: "error", message: failureMessage(err) });
    }
  }

  const ready = preview.status === "ready" ? preview.preview : null;

  // What BLOCKS the erasure: organizations where this account is the sole owner
  // AND other active members remain. `sole_owner_of` alone is not a 409 — an
  // organization with no other member is merely left empty.
  const soleOwnerBlocking = ready
    ? ready.sole_owner_of.filter((org) => {
        const membership = ready.memberships.find((m) => m.org_id === org.org_id);
        return (membership?.other_active_members ?? 0) > 0;
      })
    : [];
  const soleOwnerAlone = ready
    ? ready.sole_owner_of.filter((org) => !soleOwnerBlocking.some((b) => b.org_id === org.org_id))
    : [];
  const blockers = ready?.blockers ?? [];
  const blocked = blockers.length > 0 || soleOwnerBlocking.length > 0;
  const emailMatches = ready != null && confirmText === ready.email;

  return (
    <Panel>
      <PanelHeader
        title="Account actions"
        description="Erasing your account is permanent and cannot be undone."
      />
      <div className="space-y-4 p-5">
        <p className="text-body text-text-secondary">
          Erasing your account removes your identity, your memberships and your invitations.{" "}
          <strong className="text-text">Audit entries are kept</strong> — they record who did what inside an
          organization, and the organizations you belonged to keep that history. Organizations, projects and
          their data are not deleted with your account.
        </p>

        {!open ? (
          <Button
            type="button"
            variant="secondary"
            aria-expanded={false}
            aria-controls="account-danger-panel"
            onClick={() => setOpen(true)}
          >
            Delete my account…
          </Button>
        ) : (
          <div
            id="account-danger-panel"
            ref={panelRef}
            tabIndex={-1}
            aria-labelledby="account-danger-panel-title"
            className="space-y-4 rounded-control border border-divider-base p-5 focus-visible:outline-3 focus-visible:outline-focus"
          >
            <h3 id="account-danger-panel-title" className="m-0 text-ui font-semibold text-text">
              Delete my account
            </h3>

            {preview.status === "loading" ? (
              <Status as="block" active title="Checking">
                Checking exactly what erasing your account would remove…
              </Status>
            ) : null}

            {/* `Status` carries role="alert" for the error tone; a wrapper would
                make findByRole("alert") ambiguous. */}
            {preview.status === "error" ? (
              <Status as="block" tone="error" title="We could not check what would be erased">
                <p className="m-0">
                  {preview.message}. Nothing has been erased. We will not offer a deletion we cannot describe —
                  this is not a sign that you belong to nothing. Try again, and if it keeps failing, do not
                  assume your account is empty.
                </p>
                <Button className="mt-3" type="button" variant="secondary" onClick={() => setAttempt((n) => n + 1)}>
                  Try again
                </Button>
              </Status>
            ) : null}

            {/* The 409 the server raises at confirmation time. Rendered outside
                the preview-dependent blocks on purpose: re-reading the preview
                must not make the refusal disappear. */}
            {del.status === "conflict" ? (
              <Status as="block" tone="warning" title="Your account was not erased">
                <p className="m-0">
                  {del.message} You are the only owner of an organization that still has other members. Promote
                  another member to owner, or delete that organization, then try again. Nothing has been erased.
                </p>
              </Status>
            ) : null}

            {ready ? (
              <section className="space-y-5">
                <div>
                  <h4 className="m-0 text-ui font-semibold text-text">Account</h4>
                  <ul className="mt-2 mb-0 grid list-none gap-1 p-0" aria-label="Account identity">
                    <li className="flex flex-wrap items-baseline gap-2">
                      <span className="font-mono text-caption text-text">{ready.email}</span>
                      <span className="text-caption text-text-secondary">email</span>
                    </li>
                    <li className="flex flex-wrap items-baseline gap-2">
                      <span className="font-mono text-caption text-text">{ready.identity}</span>
                      <span className="text-caption text-text-secondary">identity</span>
                    </li>
                  </ul>
                </div>

                <div>
                  <h4 className="m-0 text-ui font-semibold text-text">
                    Memberships removed ({ready.memberships.length})
                  </h4>
                  {ready.memberships.length === 0 ? (
                    <p className="mt-2 mb-0 text-body text-text-secondary">
                      You are not a member of any organization.
                    </p>
                  ) : (
                    <ul
                      className="mt-2 mb-0 grid list-none gap-2 p-0"
                      aria-label="Memberships that will be removed"
                    >
                      {ready.memberships.map((membership) => (
                        <li key={membership.org_id} className="flex flex-wrap items-baseline gap-2">
                          <span className="text-caption text-text-secondary">{wireWord(membership.role)}</span>
                          <span className="text-ui">
                            {membership.org_name} — {membership.other_active_members} other active{" "}
                            {membership.other_active_members === 1 ? "member" : "members"}
                          </span>
                        </li>
                      ))}
                    </ul>
                  )}
                </div>

                <div>
                  <h4 className="m-0 text-ui font-semibold text-text">Kept after erasure</h4>
                  <p className="mt-2 mb-0 text-body text-text-secondary">
                    Audit entries are retained. They record actions taken inside an organization and belong to
                    that organization&apos;s history, so they survive your account. Everything else tied to your
                    identity — your profile, your memberships and your invitations — is erased.
                  </p>
                </div>
              </section>
            ) : null}

            {ready && soleOwnerBlocking.length > 0 ? (
              <Status
                as="block"
                tone="warning"
                title="You are the only owner of an organization that still has members"
              >
                <p className="m-0">
                  Your account cannot be erased while these organizations would be left without an owner. Open
                  each one, promote another member to owner (Organization Settings &gt; Members) — or delete the
                  organization from its actions section — then come back here.
                </p>
                <ul className="mt-3 mb-0 grid list-none gap-1 p-0" aria-label="Organizations blocking the erasure">
                  {soleOwnerBlocking.map((org) => (
                    <li key={org.org_id} className="flex flex-wrap items-baseline gap-2">
                      <span className="font-mono text-caption text-text-secondary">sole owner</span>
                      <span className="text-ui">{org.org_name}</span>
                    </li>
                  ))}
                </ul>
              </Status>
            ) : null}

            {ready && blockers.length > 0 ? (
              <Status as="block" tone="warning" title="Your account cannot be erased yet">
                <p className="m-0">Erasure is unavailable until the following is resolved. Nothing has been erased.</p>
                <ul className="mt-3 mb-0 grid list-none gap-2 p-0">
                  {blockers.map((blocker, index) => (
                    <li key={`${blocker.kind}-${index}`} className="flex flex-wrap items-baseline gap-2">
                      <span className="text-caption text-text-secondary">{wireWord(blocker.kind)}</span>
                      <span className="text-ui">{blocker.detail}</span>
                    </li>
                  ))}
                </ul>
              </Status>
            ) : null}

            {ready && !blocked && soleOwnerAlone.length > 0 ? (
              <Status as="block" tone="neutral" title="These organizations lose their last member">
                <p className="m-0">
                  You are the only member of the organizations below. Erasing your account does not delete them
                  or their data; it leaves them without anyone able to sign in. Delete them from their actions
                  section first if their data should go too.
                </p>
                <ul className="mt-3 mb-0 grid list-none gap-1 p-0" aria-label="Organizations left without a member">
                  {soleOwnerAlone.map((org) => <li key={org.org_id} className="text-ui">{org.org_name}</li>)}
                </ul>
              </Status>
            ) : null}

            {ready && !blocked ? (
              <>
                <Status as="block" tone="error" title="This is permanent">
                  Your account is erased when you confirm. It cannot be undone, and signing in again creates a
                  new, empty account.
                </Status>

                {del.status === "error" ? (
                  <Status as="block" tone="error" title="Account not erased">
                    <p className="m-0">{del.message}</p>
                  </Status>
                ) : null}

                {del.status === "deleted" ? (
                  <Status as="block" tone="success" title="Account erased">
                    Your account is gone.{" "}
                    {typeof del.result.retained?.audit_entries === "number"
                      ? `${del.result.retained.audit_entries} audit entries were retained.`
                      : "Audit entries were retained."}{" "}
                    {del.result.retained?.reason ?? ""} Signing you out…
                  </Status>
                ) : null}

                <div className="space-y-2">
                  <Label htmlFor="account-danger-confirm">Type your email address to confirm</Label>
                  <Input
                    id="account-danger-confirm"
                    type="text"
                    autoComplete="off"
                    spellCheck={false}
                    value={confirmText}
                    placeholder={ready.email}
                    aria-describedby="account-danger-confirm-hint"
                    disabled={del.status === "deleting" || del.status === "deleted"}
                    onChange={(event) => {
                      setConfirmText(event.target.value);
                      if (del.status === "error" || del.status === "conflict") setDel({ status: "idle" });
                    }}
                  />
                  <p id="account-danger-confirm-hint" className="m-0 text-caption text-text-secondary">
                    Enter <span className="font-mono">{ready.email}</span> exactly. The delete button stays
                    disabled until it matches.
                  </p>
                </div>

                <div className="flex flex-wrap gap-3">
                  <Button type="button" variant="secondary" onClick={closeZone}>Cancel</Button>
                  <Button
                    type="button"
                    variant="destructive"
                    disabled={!emailMatches || del.status === "deleting" || del.status === "deleted"}
                    onClick={() => void runDelete()}
                  >
                    {del.status === "deleting" ? "Erasing…" : "Delete my account permanently"}
                  </Button>
                </div>
              </>
            ) : null}

            {preview.status === "error" || blocked ? (
              <Button type="button" variant="secondary" onClick={closeZone}>Close</Button>
            ) : null}
          </div>
        )}
      </div>
    </Panel>
  );
}
