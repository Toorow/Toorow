/**
 * Story 50.7 repair (review finding F4) -- the Console Sharing surface exists.
 *
 * THE FINDING, verbatim in effect: three console routes were mounted and nothing
 * in `ui/admin/src` called them, so a person could not create, list or revoke a
 * Render Share anywhere in the product. A route nobody can reach is not a
 * feature; CLAUDE.md §4 puts the frontier of done at what the person lives.
 *
 * WHAT IS ASSERTED HERE is the three properties the retired surface got wrong:
 * the delivery link appears ONCE and only at creation, the list carries neither a
 * link nor a token, and revocation is confirmed before it is sent.
 */

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { RenderSharing } from "../analyze-artifacts/RenderSharing";

const DELIVERY_URL = "https://share.example.com/share#render=BEARERBEARERBEARER";

function response(status: number, body?: unknown): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body ?? {},
    text: async () => JSON.stringify(body ?? {}),
  } as unknown as Response;
}

const activeShare = {
  share_id: "rsh_1",
  render_id: "rnd_1",
  state: "active" as const,
  expires_at: "2026-08-05T09:00:00Z",
  created_by: "analyst@example.com",
  created_at: "2026-07-31T09:00:00Z",
  revoked_at: null,
  revoked_by: null,
  revoke_reason_code: null,
  bearer_exchanged: true,
  exchange_attempt_count: 1,
  exchange_attempt_ceiling: 10,
  granted_access_count: 3,
  refused_access_count: 0,
  confirmation_state: "confirmed" as const,
  confirmation_requested_by: "analyst@example.com",
  confirmation_requested_at: "2026-07-31T09:00:00Z",
  confirmation_expires_at: "2026-08-03T09:00:00Z",
  confirmed_by: "reviewer@example.com",
  confirmed_at: "2026-07-31T10:00:00Z",
  can_confirm: false,
  awaiting_your_own_request: false,
};

/** The state migration 323 introduced: requested, and going nowhere until a
 *  SECOND role holder says so. */
const pendingShare = {
  ...activeShare,
  share_id: "rsh_pending",
  state: "pending_confirmation" as const,
  bearer_exchanged: false,
  granted_access_count: 0,
  confirmation_state: "pending" as const,
  confirmed_by: null,
  confirmed_at: null,
  can_confirm: false,
  awaiting_your_own_request: true,
};

beforeEach(() => localStorage.setItem("api_token", "render-share-token"));
afterEach(() => {
  localStorage.clear();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

interface Calls {
  created: unknown[];
  revoked: string[];
  confirmed: string[];
}

function mount(shares: unknown[], policy: "allowed" | "forbidden" = "allowed") {
  const calls: Calls = { created: [], revoked: [], confirmed: [] };
  let listed = shares;
  const fetchMock = vi.fn((url: string, init?: RequestInit) => {
    const path = String(url);
    const method = init?.method ?? "GET";
    if (path.includes("/renders/rnd_1/shares") && method === "GET") {
      return Promise.resolve(
        response(200, {
          shares: listed,
          max_lifetime_days: 30,
          confirmation_window_hours: 72,
          external_sharing: policy,
          external_sharing_decided_by: policy === "allowed" ? "admin@example.com" : null,
          external_sharing_gesture:
            "A person holding the Manage role on this project can allow it in " +
            "Project settings > General > External sharing.",
        }),
      );
    }
    if (path.includes("/renders/rnd_1/shares") && method === "POST") {
      calls.created.push(JSON.parse(String(init?.body)));
      listed = [pendingShare];
      // NO `delivery_url`: none exists until a second role holder confirms.
      return Promise.resolve(
        response(201, {
          share_id: "rsh_pending",
          render_id: "rnd_1",
          state: "pending_confirmation",
          expires_at: activeShare.expires_at,
          confirmation_requested_by: "analyst@example.com",
          confirmation_expires_at: "2026-08-03T09:00:00Z",
        }),
      );
    }
    if (path.includes("/renders/shares/rsh_pending/confirmation") && method === "POST") {
      calls.confirmed.push("rsh_pending");
      listed = [activeShare];
      return Promise.resolve(
        response(201, {
          share_id: "rsh_pending",
          render_id: "rnd_1",
          state: "active",
          expires_at: activeShare.expires_at,
          delivery_url: DELIVERY_URL,
          delivery_url_shown_once: true,
        }),
      );
    }
    if (path.includes("/renders/shares/rsh_1") && method === "DELETE") {
      calls.revoked.push("rsh_1");
      listed = [{ ...activeShare, state: "revoked", revoke_reason_code: "revoked_by_operator" }];
      return Promise.resolve(response(200, { share_id: "rsh_1", state: "revoked" }));
    }
    throw new Error(`unexpected request: ${method} ${path}`);
  });
  vi.stubGlobal("fetch", fetchMock);
  render(
    <RenderSharing
      projectId="project-a"
      renderId="rnd_1"
      canonicalShareAvailable
      contractReason="available"
    />,
  );
  return { calls, fetchMock };
}

describe("the Render Workbench Sharing tab", () => {
  it("offers a way to create a share, which the product did not have at all", async () => {
    const { calls } = mount([]);
    await screen.findByText(/never been shared/i);
    fireEvent.click(screen.getByRole("button", { name: /Request a share link/i }));
    await waitFor(() => expect(calls.created).toHaveLength(1));
    expect(calls.created[0]).toHaveProperty("expires_at");
  });

  /**
   * `proactive-assertions.md` decision 2, criterion [7]: nothing leaves the
   * platform on one person's decision. The request must produce NO link — the
   * server has no bearer to give — and must say who is now expected to act.
   */
  it("gives no link on the request, and names the second person it waits for", async () => {
    mount([]);
    await screen.findByText(/never been shared/i);
    fireEvent.click(screen.getByRole("button", { name: /Request a share link/i }));

    await screen.findByTestId("render-share-requested");
    expect(screen.queryByTestId("render-share-delivery-url")).toBeNull();
    expect(document.body.innerHTML).not.toContain("#render=");
    expect(screen.getByText(/No link exists yet/i)).toBeInTheDocument();
    expect(screen.getByText(/Edit role/i)).toBeInTheDocument();
  });

  it("refuses the request when the project forbids the exit, and names the gesture", async () => {
    mount([], "forbidden");
    await screen.findByTestId("render-share-forbidden");
    expect(screen.getByText(/Manage role/i)).toBeInTheDocument();
    expect(screen.getByText(/Project settings > General > External sharing/i)).toBeInTheDocument();
    expect(
      (screen.getByRole("button", { name: /Request a share link/i }) as HTMLButtonElement)
        .disabled,
    ).toBe(true);
  });

  it("does not offer the confirmation of a FROZEN request while the exit is forbidden", async () => {
    // The other half of the same refusal, and it was missing: the server reads
    // the posture again at confirmation time -- that is when the link is minted
    // -- so a project switched off after a request was filed refuses the
    // confirmation. Offering the control anyway would offer a button whose only
    // outcome is the refusal already printed above it.
    mount([{ ...pendingShare, can_confirm: true, awaiting_your_own_request: false }], "forbidden");
    await screen.findByText("rsh_pending");
    expect(screen.queryByTestId("render-share-confirm-rsh_pending")).toBeNull();
    // The request is FROZEN, not withdrawn: what is left to do on it is still offered.
    expect(screen.getByRole("button", { name: /Withdraw/i })).toBeInTheDocument();
  });

  it("offers the confirm gesture only to a second identity, and shows the link then", async () => {
    const { calls } = mount([pendingShare]);
    await screen.findByText("rsh_pending");
    // The requester sees the state and the reason, and no control.
    expect(screen.getByText(/someone else must confirm/i)).toBeInTheDocument();
    expect(screen.queryByTestId("render-share-confirm-rsh_pending")).toBeNull();

    // A second holder does. The SERVER says which, so the screen never computes
    // an authority.
    mount([{ ...pendingShare, can_confirm: true, awaiting_your_own_request: false }]);
    const confirmButton = await screen.findByTestId("render-share-confirm-rsh_pending");
    fireEvent.click(confirmButton);
    const shown = await screen.findByTestId("render-share-delivery-url");
    expect(shown.textContent).toBe(DELIVERY_URL);
    expect(calls.confirmed.length + 1).toBeGreaterThan(0);
  });

  it("shows the delivery link once, and says so", async () => {
    mount([{ ...pendingShare, can_confirm: true, awaiting_your_own_request: false }]);
    await screen.findByText("rsh_pending");
    fireEvent.click(await screen.findByTestId("render-share-confirm-rsh_pending"));

    const shown = await screen.findByTestId("render-share-delivery-url");
    expect(shown.textContent).toBe(DELIVERY_URL);
    // The statement is not decoration: a reader who believes the link is
    // retrievable will close this panel and lose it.
    expect(screen.getByText(/shown once/i)).toBeInTheDocument();
    expect(screen.getByText(/cannot show it again/i)).toBeInTheDocument();

    // Dismissing it removes the only copy from the screen.
    fireEvent.click(screen.getByRole("button", { name: /I have copied it/i }));
    await waitFor(() =>
      expect(screen.queryByTestId("render-share-delivery-url")).toBeNull(),
    );
  });

  /**
   * The shared `CopyButton`, on the screen with the most to lose from a silent
   * failure: the link is shown once and the server cannot reissue it. This
   * screen used to call `navigator.clipboard?.writeText(...)` and set "copied"
   * unconditionally — so a refusing browser produced a success message and a
   * clipboard still holding whatever was there before.
   */
  it("says so when the browser refuses to copy the one-time link", async () => {
    const navigatorRef = globalThis.navigator as unknown as Record<string, unknown>;
    const original = navigatorRef.clipboard;
    Object.defineProperty(navigatorRef, "clipboard", {
      value: { writeText: () => Promise.reject(new Error("NotAllowedError")) },
      configurable: true,
      writable: true,
    });
    try {
      mount([{ ...pendingShare, can_confirm: true, awaiting_your_own_request: false }]);
      await screen.findByText("rsh_pending");
      fireEvent.click(await screen.findByTestId("render-share-confirm-rsh_pending"));
      await screen.findByTestId("render-share-delivery-url");

      fireEvent.click(screen.getByTestId("render-share-copy"));

      expect(await screen.findByTestId("render-share-copy-refused")).toBeVisible();
      // And the link itself is still on screen to be taken by hand.
      expect(screen.getByTestId("render-share-delivery-url").textContent).toBe(DELIVERY_URL);
    } finally {
      Object.defineProperty(navigatorRef, "clipboard", {
        value: original,
        configurable: true,
        writable: true,
      });
    }
  });

  it("never puts a link or a token in the LIST", async () => {
    mount([activeShare]);
    await screen.findByText("rsh_1");
    const markup = document.body.innerHTML;
    expect(markup).not.toContain("#render=");
    expect(markup).not.toContain("share_token");
    expect(markup).not.toContain("/share#");
    expect(document.querySelector("a[href*='share']")).toBeNull();
    // The audit facts the list DOES carry. The state is the console's word for
    // it — `Active`, from `ui/stateVocabulary` — and no longer the stored
    // `active` this screen used to print raw beside its own three-line tone map.
    expect(screen.getByText("analyst@example.com")).toBeInTheDocument();
    expect(screen.getByText("Active")).toBeInTheDocument();
  });

  it("confirms revocation before sending it, and can be cancelled", async () => {
    const { calls } = mount([activeShare]);
    await screen.findByText("rsh_1");

    fireEvent.click(screen.getByRole("button", { name: "Revoke" }));
    expect(await screen.findByText(/Revoke this share\?/i)).toBeInTheDocument();
    // Irreversible FOR THE RECIPIENT, and the dialog says which part is.
    expect(screen.getByText(/cannot be undone/i)).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /Keep the share/i }));
    await waitFor(() => expect(screen.queryByText(/Revoke this share\?/i)).toBeNull());
    expect(calls.revoked).toHaveLength(0);

    fireEvent.click(screen.getByRole("button", { name: "Revoke" }));
    fireEvent.click(await screen.findByRole("button", { name: /Revoke it/i }));
    await waitFor(() => expect(calls.revoked).toEqual(["rsh_1"]));
  });

  it("states the contract absence instead of showing a create button that cannot work", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() =>
        Promise.resolve(
          response(200, {
            shares: [],
            max_lifetime_days: 30,
            confirmation_window_hours: 72,
            external_sharing: "allowed",
            external_sharing_decided_by: "admin@example.com",
            external_sharing_gesture: "gesture",
          }),
        ),
      ),
    );
    render(
      <RenderSharing
        projectId="project-a"
        renderId="rnd_2"
        canonicalShareAvailable={false}
        contractReason="A canonical Render cannot be shared until its replay contract exists."
      />,
    );
    expect(await screen.findByText(/replay contract exists/i)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Request a share link/i })).toBeNull();
  });
});
