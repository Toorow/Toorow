/** ConnectGoogleButton — the door to a Google consent, from a Project that has none.
 *
 *  THERE WAS NO SUCH DOOR, and that is the whole of "I cannot connect YouTube".
 *  Measured 2026-08-11 on the served console:
 *
 *    - `Sources` offered `ConnectButton`, which sends EVERY provider to Nango
 *      (`${VITE_NANGO_BASE_URL}/oauth/connect/<provider>`). The Google stack is
 *      `auth_type: google_direct` — GA4, GSC, Ads, Sheets, YouTube Analytics all
 *      of them — so that button could not start the consent they need, whatever
 *      it was clicked on.
 *    - `organization-settings/credentials` renders `AuthorizationsPanel`, which
 *      reads, revokes and retries. It creates nothing.
 *    - `GoogleConnectPanel`, the one component that did start the consent, was
 *      mounted ONLY inside `ConnectionsList` — and `ConnectionsList` was imported
 *      by nothing but a type import. It was unreachable in the shell. Worse, the
 *      panel was gated on a Google connection ALREADY existing, so even mounted it
 *      could never be the first one. Both files were deleted on 2026-08-17; the
 *      one capability that only they carried — reading the scopes a consent
 *      actually granted, and withdrawing it at Google — moved into
 *      `AuthorizationsPanel`.
 *
 *  A first consent therefore had no gesture anywhere in the product. This button
 *  is that gesture, and it is deliberately the same one for every Google
 *  Connector: `data.md:443` — *"one authorization can open several Connectors
 *  (one Google consent covers GA4, GSC, Google Ads, Sheets)"*. Asking which
 *  Google product to connect would be asking a question the authorization does
 *  not have an answer to.
 *
 *  It POSTs the project-scoped endpoint, which mints (or reuses an abandoned)
 *  `connection_ref` and returns the authorize URL, then redirects the console
 *  tab — AD-15: the OAuth flow lives in the console, never in the chat iframe.
 */
import { useState } from "react";
import { Button, Spinner, notify } from "../ui";
import { apiFetch } from "../lib/apiFetch";

export type ConnectGoogleButtonProps = {
  /** Optional like every other control of this page: a screen reached
   *  without a Project shows the door disabled rather than not at all. */
  projectId?: string;
  /** Existing authorization to re-consent. When present, the server keeps the
   *  governed connection reference and only starts a fresh provider consent. */
  connectionRefId?: string;
  /** Label override. The default names the ACT, not the vendor's product list. */
  label?: string;
  variant?: "default" | "secondary";
};

export default function ConnectGoogleButton({
  projectId,
  connectionRefId,
  label = "Connect Google",
  variant = "default",
}: ConnectGoogleButtonProps) {
  const [loading, setLoading] = useState(false);

  async function start() {
    if (!projectId) {
      // The message names the gesture that repairs, not the missing parameter.
      notify("Open a Project before connecting a source. Nothing was connected.", {
        tone: "error",
      });
      return;
    }
    setLoading(true);
    try {
      const resp = connectionRefId
        ? await apiFetch(`/api/google/oauth/authorize?${new URLSearchParams({
            project_id: projectId,
            connection_ref_id: connectionRefId,
          }).toString()}`)
        : await apiFetch(
            `/api/projects/${encodeURIComponent(projectId)}/connections/google_direct`,
            { method: "POST" },
          );
      if (!resp.ok) {
        const body = (await resp.json().catch(() => ({}))) as { message?: string };
        notify(body.message ?? `Could not start the Google consent (HTTP ${resp.status}).`, {
          tone: "error",
        });
        return;
      }
      const data = (await resp.json()) as { authorize_url?: string };
      if (!data.authorize_url) {
        // A 200 with no URL is a server that agreed and gave nothing: say so
        // rather than leaving a button that appears to do nothing at all.
        notify("The Google consent could not be started: the server returned no address.", {
          tone: "error",
        });
        return;
      }
      window.location.href = data.authorize_url;
    } catch (err) {
      notify(
        `Could not reach the server to start the Google consent: ${
          err instanceof Error ? err.message : String(err)
        }`,
        { tone: "error" },
      );
    } finally {
      setLoading(false);
    }
  }

  return (
    <Button
      type="button"
      variant={variant}
      disabled={loading}
      onClick={() => void start()}
      data-testid="connect-google-button"
    >
      {loading && <Spinner size="inline" label="Starting the Google consent" />}
      {loading ? "Starting..." : label}
    </Button>
  );
}
