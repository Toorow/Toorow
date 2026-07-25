/**
 * AccountSettings — "Your account", the surface where a person erases their own
 * toorow account.
 *
 * WHERE IT LIVES, AND WHY. The console's information architecture has exactly
 * six project workspaces (Overview / Analyze / Test / Data / Governance /
 * Context). Organization administration and the user's own account are NOT
 * project concerns and deliberately sit OUTSIDE that navigation: they are
 * reached from the scope control in the TopBar, next to "Organization settings"
 * and "Project settings". This page is therefore routed as the scope section
 * "account" (ContentRouter), reached from the TopBar actions menu under
 * "Account" > "Your account". No seventh workspace is invented for it.
 *
 * Contract (server, implemented in parallel — do not widen it here):
 *   GET    /api/me/deletion-preview
 *            -> { identity, email,
 *                 memberships: [{org_id, org_name, role, other_active_members}],
 *                 sole_owner_of: [{org_id, org_name}],
 *                 blockers: [{kind, detail}] }
 *   DELETE /api/me           header X-Confirm-Delete: erase-account
 *            -> { deleted: true, erased: {...},
 *                 retained: {audit_entries, reason} }
 *            409 when the caller is the sole owner of an organization that
 *            still has other members.
 *
 * Rules, identical to the organization danger zone:
 *   1. Preview first, and NOTHING is displayed that the preview did not return.
 *      A failed preview is reported as a failure — never an empty list, which
 *      would read as "you belong to nothing" (finding F-010).
 *   2. The sole-owner conflict (the 409) is explained BEFORE the attempt, with
 *      the action to take, because the preview already carries `sole_owner_of`.
 *      The 409 is still handled, because membership can change between the
 *      preview and the confirmation.
 *   3. What is RETAINED is stated as plainly as what is erased. Audit entries
 *      survive the deletion; promising otherwise would be a false promise.
 *
 * Every call goes through src/lib/apiFetch.ts (bearer token attached there).
 * Styling: application.css tokens + danger-zone.css (shared with the
 * organization danger zone) + account-settings.css for this page's frame.
 */
import { useEffect, useRef, useState } from "react";
import { ApiError, apiGet, apiJson } from "../../lib/apiFetch";
import "../application.css";
import "./danger-zone.css";
import "./account-settings.css";

// ---------------------------------------------------------------------------
// Types — mirror GET /api/me/deletion-preview exactly. Nothing is added.
// ---------------------------------------------------------------------------

export interface AccountBlocker {
  kind: string;
  detail: string;
}

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

/** DELETE /api/me */
interface AccountDeletionResult {
  deleted: boolean;
  erased?: Record<string, unknown>;
  retained?: { audit_entries?: number; reason?: string };
}

/** The confirmation phrase the server requires on the DELETE. */
const ACCOUNT_CONFIRM_HEADER = "erase-account";

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

export interface AccountSettingsProps {
  /** What to do once the account no longer exists. Default: drop the held token
   *  and go back to the sign-in gate — staying signed in as an erased user
   *  would be a scope pointing at nothing. */
  onDeleted?: () => void;
}

function defaultOnDeleted(): void {
  localStorage.removeItem("api_token");
  window.location.assign("/");
}

export default function AccountSettings({ onDeleted }: AccountSettingsProps = {}) {
  const [open, setOpen] = useState(false);
  const [attempt, setAttempt] = useState(0);
  const [preview, setPreview] = useState<PreviewState>({ status: "idle" });
  const [confirmText, setConfirmText] = useState("");
  const [del, setDel] = useState<DeleteState>({ status: "idle" });
  const panelRef = useRef<HTMLDivElement>(null);

  // Preview on OPENING the zone (and on retry) — reading what would be erased
  // is the first step of the destructive flow, not a page-load side effect.
  useEffect(() => {
    if (!open) return;
    let alive = true;
    setPreview({ status: "loading" });
    void (async () => {
      try {
        const data = await apiGet<AccountDeletionPreview>("/api/me/deletion-preview");
        if (alive) setPreview({ status: "ready", preview: data });
      } catch (err) {
        if (alive) {
          setPreview({
            status: "error",
            message: err instanceof Error ? err.message : String(err),
          });
        }
      }
    })();
    return () => {
      alive = false;
    };
  }, [open, attempt]);

  useEffect(() => {
    if (open) panelRef.current?.focus();
  }, [open]);

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
      (onDeleted ?? defaultOnDeleted)();
    } catch (err) {
      if (err instanceof ApiError && err.status === 409) {
        // Sole owner of an organization that still has members. Membership can
        // change between the preview and the confirmation, so this is handled
        // even though the preview already warns about it.
        setDel({ status: "conflict", message: err.message });
        setAttempt((n) => n + 1); // re-read the preview so the list is current
        return;
      }
      const message =
        err instanceof ApiError
          ? `${err.message} (HTTP ${err.status})`
          : err instanceof Error
            ? err.message
            : String(err);
      setDel({ status: "error", message });
    }
  }

  const ready = preview.status === "ready" ? preview.preview : null;

  // The organizations that BLOCK the erasure: those where this account is the
  // sole owner AND other active members remain. `sole_owner_of` alone does not
  // block — an organization with no other member is not a 409.
  const soleOwnerBlocking = ready
    ? ready.sole_owner_of.filter((o) => {
        const m = ready.memberships.find((mm) => mm.org_id === o.org_id);
        return (m?.other_active_members ?? 0) > 0;
      })
    : [];
  const soleOwnerAlone = ready
    ? ready.sole_owner_of.filter(
        (o) => !soleOwnerBlocking.some((b) => b.org_id === o.org_id),
      )
    : [];
  const blockers = ready?.blockers ?? [];
  const blocked = blockers.length > 0 || soleOwnerBlocking.length > 0;
  const emailMatches = ready != null && confirmText === ready.email;

  return (
    <div className="accountsettings">
      <header className="page-header">
        <div>
          <h1>Your account</h1>
          <p>
            Your identity in toorow, the organizations you belong to, and the
            permanent erasure of this account.
          </p>
        </div>
      </header>

      <section className="dangerzone" aria-labelledby="account-danger">
        <div className="section-header">
          <h2 id="account-danger">Delete your account</h2>
          <p>Erasing your account is permanent and cannot be undone.</p>
        </div>

        <div className="dangerzone-card">
          <p className="dangerzone-lead">
            Erasing your account removes your identity, your memberships and your
            invitations. <strong>Audit entries are kept</strong> — they record who
            did what inside an organization, and the organizations you belonged to
            keep that history. Organizations, projects and their data are not
            deleted with your account.
          </p>

          {!open ? (
            <div className="dangerzone-disclosure">
              <button
                type="button"
                className="dangerzone-open-button"
                aria-expanded={false}
                aria-controls="account-danger-panel"
                onClick={() => setOpen(true)}
              >
                Delete my account…
              </button>
            </div>
          ) : (
            <div
              className="dangerzone-panel"
              id="account-danger-panel"
              ref={panelRef}
              tabIndex={-1}
              aria-labelledby="account-danger-panel-title"
            >
              <h3 className="dangerzone-panel-title" id="account-danger-panel-title">
                Delete my account
              </h3>

              {preview.status === "loading" && (
                <div className="dangerzone-status" role="status" aria-live="polite">
                  <span className="signal-label info">
                    <span className="signal-mark" />
                    Checking
                  </span>
                  <p>Checking exactly what erasing your account would remove…</p>
                </div>
              )}

              {preview.status === "error" && (
                <div className="dangerzone-status error" role="alert">
                  <span className="signal-label error">
                    <span className="signal-mark" />
                    We could not check what would be erased
                  </span>
                  <p>
                    {preview.message}. Nothing has been erased. We will not offer a
                    deletion we cannot describe — this is not a sign that you belong
                    to nothing. Try again, and if it keeps failing, do not assume
                    your account is empty.
                  </p>
                  <button
                    type="button"
                    className="dangerzone-retry"
                    onClick={() => setAttempt((n) => n + 1)}
                  >
                    Try again
                  </button>
                </div>
              )}

              {/* The 409 the server raises at confirmation time. It is rendered
                  outside the preview-dependent blocks on purpose: re-reading the
                  preview must not make the refusal disappear. */}
              {del.status === "conflict" && (
                <div className="dangerzone-status blocked" role="alert">
                  <span className="signal-label warning">
                    <span className="signal-mark" />
                    Your account was not erased
                  </span>
                  <p>
                    {del.message} You are the only owner of an organization that
                    still has other members. Promote another member to owner, or
                    delete that organization, then try again. Nothing has been
                    erased.
                  </p>
                </div>
              )}

              {ready && (
                <div className="dangerzone-manifest">
                  <div className="dangerzone-group">
                    <h4>Account</h4>
                    <ul className="dangerzone-chips" aria-label="Account identity">
                      <li>
                        <span className="mono">{ready.email}</span>
                        <span className="dangerzone-chip-note">email</span>
                      </li>
                      <li>
                        <span className="mono">{ready.identity}</span>
                        <span className="dangerzone-chip-note">identity</span>
                      </li>
                    </ul>
                  </div>

                  <div className="dangerzone-group">
                    <h4>Memberships removed ({ready.memberships.length})</h4>
                    {ready.memberships.length === 0 ? (
                      <p>You are not a member of any organization.</p>
                    ) : (
                      <ul className="dangerzone-list" aria-label="Memberships that will be removed">
                        {ready.memberships.map((m) => (
                          <li key={m.org_id}>
                            <span className="dangerzone-list-kind">{m.role}</span>
                            <span className="dangerzone-list-detail">
                              {m.org_name} — {m.other_active_members} other active{" "}
                              {m.other_active_members === 1 ? "member" : "members"}
                            </span>
                          </li>
                        ))}
                      </ul>
                    )}
                  </div>

                  <div className="dangerzone-group">
                    <h4>Kept after erasure</h4>
                    <p>
                      Audit entries are retained. They record actions taken inside an
                      organization and belong to that organization&apos;s history, so they
                      survive your account. Everything else tied to your identity —
                      your profile, your memberships and your invitations — is erased.
                    </p>
                  </div>
                </div>
              )}

              {ready && soleOwnerBlocking.length > 0 && (
                <div className="dangerzone-status blocked" role="alert">
                  <span className="signal-label warning">
                    <span className="signal-mark" />
                    You are the only owner of an organization that still has members
                  </span>
                  <p>
                    Your account cannot be erased while these organizations would be
                    left without an owner. Open each one, promote another member to
                    owner (Organization settings &gt; Members and roles) — or delete
                    the organization from its danger zone — then come back here.
                  </p>
                  <ul className="dangerzone-list" aria-label="Organizations blocking the erasure">
                    {soleOwnerBlocking.map((o) => (
                      <li key={o.org_id}>
                        <span className="dangerzone-list-kind">sole owner</span>
                        <span className="dangerzone-list-detail">{o.org_name}</span>
                      </li>
                    ))}
                  </ul>
                </div>
              )}

              {ready && blockers.length > 0 && (
                <div className="dangerzone-status blocked" role="alert">
                  <span className="signal-label warning">
                    <span className="signal-mark" />
                    Your account cannot be erased yet
                  </span>
                  <p>
                    Erasure is unavailable until the following is resolved. Nothing
                    has been erased.
                  </p>
                  <ul className="dangerzone-list">
                    {blockers.map((b, i) => (
                      <li key={`${b.kind}-${i}`}>
                        <span className="dangerzone-list-kind">{b.kind}</span>
                        <span className="dangerzone-list-detail">{b.detail}</span>
                      </li>
                    ))}
                  </ul>
                </div>
              )}

              {ready && !blocked && soleOwnerAlone.length > 0 && (
                <div className="dangerzone-status info" role="status">
                  <span className="signal-label info">
                    <span className="signal-mark" />
                    These organizations lose their last member
                  </span>
                  <p>
                    You are the only member of the organizations below. Erasing your
                    account does not delete them or their data; it leaves them
                    without anyone able to sign in. Delete them from their danger
                    zone first if their data should go too.
                  </p>
                  <ul className="dangerzone-list" aria-label="Organizations left without a member">
                    {soleOwnerAlone.map((o) => (
                      <li key={o.org_id}>
                        <span className="dangerzone-list-detail">{o.org_name}</span>
                      </li>
                    ))}
                  </ul>
                </div>
              )}

              {ready && !blocked && (
                <>
                  <div className="dangerzone-status warning" role="alert">
                    <span className="signal-label error">
                      <span className="signal-mark" />
                      This is permanent
                    </span>
                    <p>
                      Your account is erased when you confirm. It cannot be undone,
                      and signing in again creates a new, empty account.
                    </p>
                  </div>

                  {del.status === "error" && (
                    <div className="dangerzone-status error" role="alert">
                      <span className="signal-label error">
                        <span className="signal-mark" />
                        Account not erased
                      </span>
                      <p>{del.message}</p>
                    </div>
                  )}

                  {del.status === "deleted" && (
                    <div className="dangerzone-status success" role="status">
                      <span className="signal-label success">
                        <span className="signal-mark" />
                        Account erased
                      </span>
                      <p>
                        Your account is gone.{" "}
                        {typeof del.result.retained?.audit_entries === "number"
                          ? `${del.result.retained.audit_entries} audit entries were retained.`
                          : "Audit entries were retained."}{" "}
                        {del.result.retained?.reason ?? ""} Signing you out…
                      </p>
                    </div>
                  )}

                  <div className="dangerzone-confirm">
                    <label htmlFor="account-danger-confirm">
                      Type your email address to confirm
                    </label>
                    <input
                      id="account-danger-confirm"
                      className="dangerzone-input"
                      type="text"
                      autoComplete="off"
                      spellCheck={false}
                      value={confirmText}
                      placeholder={ready.email}
                      aria-describedby="account-danger-confirm-hint"
                      disabled={del.status === "deleting" || del.status === "deleted"}
                      onChange={(e) => {
                        setConfirmText(e.target.value);
                        if (del.status === "error" || del.status === "conflict") {
                          setDel({ status: "idle" });
                        }
                      }}
                    />
                    <p className="dangerzone-hint" id="account-danger-confirm-hint">
                      Enter <span className="mono">{ready.email}</span> exactly. The
                      delete button stays disabled until it matches.
                    </p>
                  </div>

                  <div className="dangerzone-actions">
                    <button type="button" className="dangerzone-cancel" onClick={closeZone}>
                      Cancel
                    </button>
                    <button
                      type="button"
                      className="dangerzone-button"
                      disabled={
                        !emailMatches ||
                        del.status === "deleting" ||
                        del.status === "deleted"
                      }
                      onClick={() => void runDelete()}
                    >
                      {del.status === "deleting" ? "Erasing…" : "Delete my account permanently"}
                    </button>
                  </div>
                </>
              )}

              {(preview.status === "error" || blocked) && (
                <div className="dangerzone-actions">
                  <button type="button" className="dangerzone-cancel" onClick={closeZone}>
                    Close
                  </button>
                </div>
              )}
            </div>
          )}
        </div>
      </section>
    </div>
  );
}
