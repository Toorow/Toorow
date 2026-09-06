/**
 * Authorizations — the surface must say WHOSE access a credential is, and must
 * only offer an action the caller can actually carry out (Story 42.9).
 *
 * What these pin, in order of how badly each would hurt:
 *
 *   1. A credential reaching this org through a grant must not name the person
 *      behind it, and must offer no action. The beneficiary is entitled to the
 *      owning ORGANIZATION, never the human.
 *   2. `can_revoke: false` must render no Revoke button at all — the defect this
 *      whole story exists to undo is a page telling you to do something it does
 *      not let you do.
 *   3. A failed read shows no list. Nothing is substituted for it.
 */
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import AuthorizationsPanel from "../authorizations/AuthorizationsPanel";

function stub(result: { ok: boolean; body?: unknown; status?: number }) {
  const fetchMock = vi.fn().mockImplementation(() =>
    Promise.resolve({
      ok: result.ok,
      status: result.status ?? (result.ok ? 200 : 500),
      json: async () => result.body ?? {},
      text: async () => "{}",
    }),
  );
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

const MINE = {
  id: "conn_mine",
  nango_connection_id: "n1",
  provider: "google-search-console",
  project_id: "p1",
  created_at: "2026-07-01T00:00:00Z",
  status: "active" as const,
  owner_org_id: "org_1",
  owner_org_name: "Toorow",
  owner_identity: "jean",
  owner_display_name: "Jean-Ludovic Albany",
  is_mine: true,
  can_update: true,
  can_revoke: true,
  exposure: "owned" as const,
  health: { status: "ok" as const },
};

/** Reached through an account grant: owned by someone else, in another org. */
const PROVIDED = {
  id: "conn_provided",
  nango_connection_id: "n2",
  provider: "meta",
  project_id: "p1",
  created_at: "2026-07-02T00:00:00Z",
  status: "active" as const,
  owner_org_id: "org_2",
  owner_org_name: "Partner Agency",
  owner_identity: null,
  owner_display_name: null,
  is_mine: false,
  can_update: false,
  can_revoke: false,
  exposure: "provided_by_org" as const,
  health: { status: "ok" as const },
};

beforeEach(() => {
  // The empty state offers the door only for the Project the person was last in.
  sessionStorage.clear();
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("Authorizations — organization view", () => {
  it("names who connected an owned credential", async () => {
    stub({ ok: true, body: { authorizations: [MINE] } });
    render(<AuthorizationsPanel scope={{ kind: "org", orgId: "org_1" }} />);

    await waitFor(() => {
      expect(screen.getByText("Jean-Ludovic Albany")).toBeInTheDocument();
    });
    expect(screen.getByRole("columnheader", { name: /connected by/i })).toBeInTheDocument();
  });

  it("never names the person behind a credential another org provides", async () => {
    stub({ ok: true, body: { authorizations: [PROVIDED] } });
    render(<AuthorizationsPanel scope={{ kind: "org", orgId: "org_1" }} />);

    await waitFor(() => {
      expect(screen.getByText(/Provided by Partner Agency/i)).toBeInTheDocument();
    });
    // No person, and no action on someone else's credential.
    expect(screen.queryByRole("button", { name: /^revoke$/i })).not.toBeInTheDocument();
  });

  it("offers no Revoke when the caller may not revoke", async () => {
    stub({ ok: true, body: { authorizations: [{ ...MINE, can_revoke: false, is_mine: false }] } });
    render(<AuthorizationsPanel scope={{ kind: "org", orgId: "org_1" }} />);

    await waitFor(() => {
      expect(screen.getByText("Jean-Ludovic Albany")).toBeInTheDocument();
    });
    expect(screen.queryByRole("button", { name: /^revoke$/i })).not.toBeInTheDocument();
  });
});

describe("Authorizations — my view", () => {
  it("reads the user-scoped endpoint and hides the owner column", async () => {
    const fetchMock = stub({ ok: true, body: { authorizations: [MINE] } });
    render(<AuthorizationsPanel scope={{ kind: "mine" }} />);

    await waitFor(() => {
      expect(screen.getByRole("button", { name: /^revoke$/i })).toBeInTheDocument();
    });
    expect(String(fetchMock.mock.calls[0][0])).toContain("/api/me/authorizations");
    expect(
      screen.queryByRole("columnheader", { name: /connected by/i }),
    ).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^reconnect$/i })).toBeInTheDocument();
  });

  it("shows the provider refusal as its own red state, never as Unknown or Disconnected", async () => {
    // AI-341: the authorization is alive but the provider refuses the data --
    // the one red whose gesture is not "reconnect". Until this sweep the
    // mapping fell through to "Unknown" for any status it did not know, which
    // is how six days of 403s stayed invisible on this very panel.
    stub({
      ok: true,
      body: {
        authorizations: [{ ...MINE, health: { status: "provider_denied" as const } }],
      },
    });
    render(<AuthorizationsPanel scope={{ kind: "mine" }} />);

    await waitFor(() => expect(screen.getByRole("table")).toBeInTheDocument());
    expect(screen.getByText(/access refused by provider/i)).toBeInTheDocument();
    expect(screen.queryByText(/^unknown$/i)).not.toBeInTheDocument();
  });

  it("never offers reconnect for an authorization the signed-in person did not create", async () => {
    stub({ ok: true, body: { authorizations: [{ ...MINE, is_mine: false, can_update: false }] } });
    render(<AuthorizationsPanel scope={{ kind: "mine" }} />);

    await waitFor(() => expect(screen.getByRole("table")).toBeInTheDocument());
    expect(screen.queryByRole("button", { name: /^reconnect$/i })).not.toBeInTheDocument();
  });

  it("keeps Google re-consent on the existing governed connection reference", async () => {
    const fetchMock = stub({ ok: true, body: { authorizations: [{ ...MINE, auth_path: "google_direct" }] } });
    render(<AuthorizationsPanel scope={{ kind: "mine" }} />);

    fireEvent.click(await screen.findByRole("button", { name: /^reconnect$/i }));
    await waitFor(() => {
      const authorizeCall = fetchMock.mock.calls.find((call) =>
        String(call[0]).includes("/api/google/oauth/authorize?"),
      );
      expect(authorizeCall).toBeDefined();
      expect(String(authorizeCall?.[0])).toContain("project_id=p1");
      expect(String(authorizeCall?.[0])).toContain("connection_ref_id=conn_mine");
    });
  });

  it("shows no list when the read fails", async () => {
    stub({ ok: false });
    render(<AuthorizationsPanel scope={{ kind: "mine" }} />);

    await waitFor(() => {
      expect(screen.getByRole("alert")).toHaveTextContent(/No list is shown in their place/i);
    });
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
  });

  it("says so plainly when nothing has been connected yet", async () => {
    stub({ ok: true, body: { authorizations: [] } });
    render(<AuthorizationsPanel scope={{ kind: "mine" }} />);

    // Queried by its WORDS, not by `role="status"`. The panel was ported onto the
    // shared `EmptyState` primitive (`ui/Data.tsx`), which renders a plain <div>,
    // so the live region the hand-rolled version carried is gone. That loss is
    // real and is NOT decided here: `EmptyState` is mounted by 52 screens, and
    // making it a live region announces on every navigation that happens to land
    // on an empty one. Opened as its own line rather than settled inside a test
    // repair. What this case owes the reader is that the sentence is SAID.
    await waitFor(() => {
      expect(screen.getByText("No provider account connected")).toBeInTheDocument();
    });
    // The sentence names the GESTURE that fills the list, and it now names both
    // ways in — `Data > Sources` and the wizard. It used to name only the wizard
    // ("Authorizations are created while adding a Datastream."), which sent a
    // person to the long road for a thing the Sources screen does in one click.
    // What this case owes the reader is unchanged: an empty list says why, and
    // says what to do about it. Pinned as a regex so the route survives a
    // rewording of the clause around it, and never as a substring of one word.
    expect(screen.getByText(/Connect one from Data > Sources/)).toBeInTheDocument();
    expect(screen.getByText(/while adding a Datastream/)).toBeInTheDocument();
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
  });

  /* ------------------------------------------------------------------
   * AI-112 -- ce qu'ajouter un scope au produit laisse derriere lui.
   *
   * Une autorisation deja emise ne porte pas les scopes ajoutes depuis. Les
   * quatre scopes d'AI-94 ne sont dans aucune : trois Connecteurs installes
   * etaient invisibles sur toute connexion anterieure, sans que rien ne le dise.
   * ------------------------------------------------------------------ */

  function stubRoutes(byUrl: Record<string, { ok: boolean; status?: number; body?: unknown }>) {
    const fetchMock = vi.fn().mockImplementation((input: unknown) => {
      const url = String(input);
      const key = Object.keys(byUrl).find((k) => url.includes(k));
      const hit = key ? byUrl[key] : { ok: false, status: 404 };
      return Promise.resolve({
        ok: hit.ok,
        status: hit.status ?? (hit.ok ? 200 : 500),
        json: async () => hit.body ?? {},
        text: async () => "{}",
      });
    });
    vi.stubGlobal("fetch", fetchMock);
    return fetchMock;
  }

  const GOOGLE = { ...MINE, id: "conn_g", auth_path: "google_direct" as const, provider: "google" };

  it("names the connectors a reconnect would add, as an offer and not a reproach", async () => {
    stubRoutes({
      "/api/me/authorizations": { ok: true, body: { authorizations: [GOOGLE] } },
      "/connectors": {
        ok: true,
        body: {
          unlocked_by_reconsent: [
            { connector_name: "google-ad-manager", display_name: "Google Ad Manager" },
            { connector_name: "google-business-profile", display_name: "Google Business Profile" },
          ],
        },
      },
    });
    render(<AuthorizationsPanel scope={{ kind: "mine" }} />);

    await waitFor(() => {
      expect(screen.getByText(/2 more connectors are available/i)).toBeInTheDocument();
    });
    expect(screen.getByText(/Google Ad Manager/)).toBeInTheDocument();
    expect(screen.getByText(/Reconnect to authorize/i)).toBeInTheDocument();
    // On ne peut pas distinguer << decoche >> de << n'existait pas encore >> :
    // `connection_ref` ne garde pas les scopes demandes. La copie ne doit donc
    // accuser personne.
    expect(screen.queryByText(/declined|refused|you did not/i)).not.toBeInTheDocument();
  });

  it("says nothing at all when the authorization already opens everything", async () => {
    stubRoutes({
      "/api/me/authorizations": { ok: true, body: { authorizations: [GOOGLE] } },
      "/connectors": { ok: true, body: { unlocked_by_reconsent: [] } },
    });
    render(<AuthorizationsPanel scope={{ kind: "mine" }} />);

    await waitFor(() => expect(screen.getByRole("table")).toBeInTheDocument());
    expect(screen.queryByText(/more connector/i)).not.toBeInTheDocument();
  });

  it("stays silent -- and keeps the table -- when the extra read is refused", async () => {
    // 404 est LEGITIME ici : l'endpoint verifie que l'appelant peut lire le
    // projet, et un admin d'organisation n'a pas forcement ce droit. Afficher une
    // erreur ferait passer une frontiere d'acces normale pour une panne, et
    // perdre le tableau pour un supplement serait pire encore.
    stubRoutes({
      "/api/me/authorizations": { ok: true, body: { authorizations: [GOOGLE] } },
      "/connectors": { ok: false, status: 404 },
    });
    render(<AuthorizationsPanel scope={{ kind: "mine" }} />);

    await waitFor(() => expect(screen.getByRole("table")).toBeInTheDocument());
    expect(screen.queryByText(/more connector/i)).not.toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("does not ask at all for a Nango authorization", async () => {
    const fetchMock = stubRoutes({
      "/api/me/authorizations": { ok: true, body: { authorizations: [MINE] } },
    });
    render(<AuthorizationsPanel scope={{ kind: "mine" }} />);

    await waitFor(() => expect(screen.getByRole("table")).toBeInTheDocument());
    // Une connexion Nango n'ouvre qu'un Connecteur : il n'y a rien a comparer,
    // et poser la question couterait un appel par ligne pour rien.
    expect(
      fetchMock.mock.calls.filter((c) => String(c[0]).includes("/connectors")),
    ).toHaveLength(0);
  });
});

/* ==========================================================================
 * REVOKING WITHOUT KNOWING WHAT IT BREAKS.
 *
 * The gesture was an inline pair of buttons that asked "Revoke your access?"
 * and showed nothing else — so the one fact the answer turns on, how many
 * Datastreams stop pulling, was not on the screen where the decision is made.
 * Every other destructive gesture of this console goes through `ConfirmDialog`
 * with its evidence (`orgs/CredentialGrantsPanel.tsx:404-423`); this one did
 * not.
 *
 * Neither authorization endpoint carries the count. `/api/connections` does,
 * per Project — so the panel reads it ON THE GESTURE, for the one row, and
 * says plainly when it cannot.
 * ========================================================================== */
describe("Authorizations — revoking says what it stops", () => {
  function routes(byUrl: Record<string, { ok: boolean; status?: number; body?: unknown }>) {
    const fetchMock = vi.fn().mockImplementation((input: unknown) => {
      const url = String(input);
      const key = Object.keys(byUrl).find((candidate) => url.includes(candidate));
      const hit = key ? byUrl[key] : { ok: false, status: 404 };
      return Promise.resolve({
        ok: hit.ok,
        status: hit.status ?? (hit.ok ? 200 : 500),
        json: async () => hit.body ?? {},
        text: async () => "{}",
      });
    });
    vi.stubGlobal("fetch", fetchMock);
    return fetchMock;
  }

  const CONNECTIONS = {
    ok: true,
    body: { connections: [{ id: "conn_mine", active_datastream_count: 3 }] },
  };

  it("shows the impact as evidence, and revokes nothing until it is confirmed", async () => {
    const fetchMock = routes({
      "/api/me/authorizations": { ok: true, body: { authorizations: [MINE] } },
      "/api/connections?": CONNECTIONS,
    });
    render(<AuthorizationsPanel scope={{ kind: "mine" }} />);

    fireEvent.click(await screen.findByRole("button", { name: /^revoke$/i }));

    expect(await screen.findByText("3 Datastreams stop pulling")).toBeInTheDocument();
    const evidence = screen.getByRole("region", { name: /Authorization that will be revoked/i });
    // The evidence names the credential, not only its consequence.
    expect(evidence).toHaveTextContent("google-search-console");
    expect(evidence).toHaveTextContent("Toorow");
    // Nothing has been revoked by OPENING the question.
    expect(
      fetchMock.mock.calls.filter((call) => String(call[0]).includes("/revoke")),
    ).toHaveLength(0);
  });

  it("says the impact could not be read rather than reporting none", async () => {
    // A legitimate boundary: an organization admin need not be able to read the
    // Project the authorization lives in. "0 Datastreams" there would say
    // revoking is free when it may stop every pull the Project has.
    routes({
      "/api/organizations/org_1/authorizations": { ok: true, body: { authorizations: [MINE] } },
      "/api/connections?": { ok: false, status: 404 },
    });
    render(<AuthorizationsPanel scope={{ kind: "org", orgId: "org_1" }} />);

    fireEvent.click(await screen.findByRole("button", { name: /^revoke$/i }));

    expect(await screen.findByText(/could not be read from here/i)).toBeInTheDocument();
    expect(screen.queryByText(/^None/)).not.toBeInTheDocument();
  });

  it("revokes on the confirmation, and only then", async () => {
    const fetchMock = routes({
      "/api/me/authorizations": { ok: true, body: { authorizations: [MINE] } },
      "/api/connections?": CONNECTIONS,
      "/revoke": { ok: true, body: {} },
    });
    render(<AuthorizationsPanel scope={{ kind: "mine" }} />);

    fireEvent.click(await screen.findByRole("button", { name: /^revoke$/i }));
    await screen.findByText("3 Datastreams stop pulling");
    fireEvent.click(screen.getByTestId("revoke-authorization-confirm"));

    await waitFor(() => {
      const call = fetchMock.mock.calls.find((entry) => String(entry[0]).includes("/revoke"));
      expect(call).toBeDefined();
      expect(String(call?.[0])).toContain("/api/authorizations/conn_mine/revoke");
      expect((call?.[1] as { method?: string })?.method).toBe("POST");
    });
  });

  it("leaves the credential alone when the question is dismissed", async () => {
    const fetchMock = routes({
      "/api/me/authorizations": { ok: true, body: { authorizations: [MINE] } },
      "/api/connections?": CONNECTIONS,
    });
    render(<AuthorizationsPanel scope={{ kind: "mine" }} />);

    fireEvent.click(await screen.findByRole("button", { name: /^revoke$/i }));
    await screen.findByText("3 Datastreams stop pulling");
    fireEvent.click(screen.getByTestId("revoke-authorization-cancel"));

    await waitFor(() =>
      expect(screen.queryByText("3 Datastreams stop pulling")).not.toBeInTheDocument(),
    );
    expect(
      fetchMock.mock.calls.filter((call) => String(call[0]).includes("/revoke")),
    ).toHaveLength(0);
  });
});

/* ==========================================================================
 * AN EMPTY STATE THAT DESCRIBES THE GESTURE AND DOES NOT OFFER IT.
 *
 * Both empty states named the act that fills the list — "connect from Data >
 * Sources" — and passed no `action`, on the one panel that already imports the
 * door (`ConnectGoogleButton`). A consent is granted FOR A PROJECT, and this
 * panel has none in its address: the Project the person was last in is the
 * only one it may offer without guessing.
 * ========================================================================== */
describe("Authorizations — the empty state offers the gesture it names", () => {
  function remember(organizationId: string, projectId: string) {
    sessionStorage.setItem(
      "toorow_last_project_scope",
      JSON.stringify({ organizationId, projectId }),
    );
  }

  it("offers the connect door for the Project the person was last in", async () => {
    stub({ ok: true, body: { authorizations: [] } });
    remember("org_1", "p1");
    render(<AuthorizationsPanel scope={{ kind: "mine" }} />);

    await screen.findByText("No provider account connected");
    expect(screen.getByTestId("connect-google-button")).toBeInTheDocument();
  });

  it("offers nothing to click rather than a door into another organization", async () => {
    stub({ ok: true, body: { authorizations: [] } });
    remember("org_OTHER", "p_other");
    render(<AuthorizationsPanel scope={{ kind: "org", orgId: "org_1" }} />);

    await screen.findByText("No provider authorization");
    // Starting a consent in a Project of a different organization would be worse
    // than no button at all; the sentence still names where the gesture lives.
    expect(screen.queryByTestId("connect-google-button")).not.toBeInTheDocument();
    expect(screen.getByText(/open the project and connect from Data > Sources/)).toBeInTheDocument();
  });

  it("offers the door on the organization view when the Project is its own", async () => {
    stub({ ok: true, body: { authorizations: [] } });
    remember("org_1", "p1");
    render(<AuthorizationsPanel scope={{ kind: "org", orgId: "org_1" }} />);

    await screen.findByText("No provider authorization");
    expect(screen.getByTestId("connect-google-button")).toBeInTheDocument();
  });
});

/* ==========================================================================
 * WHAT DID I ACTUALLY AUTHORIZE?
 *
 * `GET /api/google/oauth/status/{id}` (`google_oauth_api.py:398`, routed :834)
 * is the only read in the product that answers it. Until 2026-08-17 the only
 * surface calling it was `GoogleConnectPanel`, mounted only by `ConnectionsList`,
 * which nothing mounted: the answer existed and reached nobody. Both files are
 * deleted and the capability lives here.
 *
 * The cost is why it is a disclosure and not a column: the endpoint runs a query
 * and a project-access check per call, so a list of twenty rows must not spend
 * twenty of them before anyone has asked.
 * ========================================================================== */
describe("Authorizations — what a Google consent grants", () => {
  function routes(byUrl: Record<string, { ok: boolean; status?: number; body?: unknown }>) {
    const fetchMock = vi.fn().mockImplementation((input: unknown) => {
      const url = String(input);
      const key = Object.keys(byUrl).find((candidate) => url.includes(candidate));
      const hit = key ? byUrl[key] : { ok: false, status: 404 };
      return Promise.resolve({
        ok: hit.ok,
        status: hit.status ?? (hit.ok ? 200 : 500),
        json: async () => hit.body ?? {},
        text: async () => "{}",
      });
    });
    vi.stubGlobal("fetch", fetchMock);
    return fetchMock;
  }

  const GOOGLE_ROW = {
    ...MINE,
    id: "conn_g",
    provider: "google-analytics",
    auth_path: "google_direct" as const,
    health: { status: "ok" as const, last_checked_at: "2026-08-14T09:00:00Z" },
  };

  const SCOPES = {
    ok: true,
    body: {
      granted_scopes: [
        { scope: "https://www.googleapis.com/auth/analytics.readonly", label: "Read Analytics data" },
        { scope: "https://www.googleapis.com/auth/webmasters.readonly", label: "Read Search Console data" },
      ],
    },
  };

  const NO_UNOPENED = { ok: true, body: { unlocked_by_reconsent: [] } };

  function statusCalls(fetchMock: ReturnType<typeof routes>) {
    return fetchMock.mock.calls.filter((call) =>
      String(call[0]).includes("/api/google/oauth/status/"),
    );
  }

  it("asks nothing until the row is opened, then asks once", async () => {
    const fetchMock = routes({
      "/api/me/authorizations": { ok: true, body: { authorizations: [GOOGLE_ROW] } },
      "/connectors": NO_UNOPENED,
      "/api/google/oauth/status/": SCOPES,
    });
    render(<AuthorizationsPanel scope={{ kind: "mine" }} />);

    const trigger = await screen.findByTestId("authorization-scopes-trigger-conn_g");
    // The whole point of a disclosure: nothing was spent on a question nobody asked.
    expect(statusCalls(fetchMock)).toHaveLength(0);
    expect(trigger).toHaveAttribute("aria-expanded", "false");

    fireEvent.click(trigger);

    expect(await screen.findByText("Read Analytics data")).toBeInTheDocument();
    expect(
      screen.getByText("https://www.googleapis.com/auth/analytics.readonly"),
    ).toBeInTheDocument();
    expect(screen.getByText("Read Search Console data")).toBeInTheDocument();
    expect(statusCalls(fetchMock)).toHaveLength(1);
    expect(String(statusCalls(fetchMock)[0][0])).toContain("/api/google/oauth/status/conn_g");
    expect(trigger).toHaveAttribute("aria-expanded", "true");
  });

  it("says when the consent was last verified, beside what it opens", async () => {
    routes({
      "/api/me/authorizations": { ok: true, body: { authorizations: [GOOGLE_ROW] } },
      "/connectors": NO_UNOPENED,
      "/api/google/oauth/status/": SCOPES,
    });
    render(<AuthorizationsPanel scope={{ kind: "mine" }} />);

    fireEvent.click(await screen.findByTestId("authorization-scopes-trigger-conn_g"));
    // A scope list read from a consent nobody has checked is a claim about the past.
    expect(await screen.findByText(/Last verified 14 Aug 2026/)).toBeInTheDocument();
  });

  it("never verified is said as such, and never as a date", async () => {
    routes({
      "/api/me/authorizations": {
        ok: true,
        body: { authorizations: [{ ...GOOGLE_ROW, health: { status: "ok" } }] },
      },
      "/connectors": NO_UNOPENED,
      "/api/google/oauth/status/": SCOPES,
    });
    render(<AuthorizationsPanel scope={{ kind: "mine" }} />);

    fireEvent.click(await screen.findByTestId("authorization-scopes-trigger-conn_g"));
    expect(await screen.findByText(/Never verified/)).toBeInTheDocument();
  });

  it("reads only the row that was opened, never its neighbours", async () => {
    const other = { ...GOOGLE_ROW, id: "conn_g2" };
    const fetchMock = routes({
      "/api/me/authorizations": { ok: true, body: { authorizations: [GOOGLE_ROW, other] } },
      "/connectors": NO_UNOPENED,
      "/api/google/oauth/status/": SCOPES,
    });
    render(<AuthorizationsPanel scope={{ kind: "mine" }} />);

    fireEvent.click(await screen.findByTestId("authorization-scopes-trigger-conn_g"));
    await screen.findByText("Read Analytics data");

    expect(statusCalls(fetchMock)).toHaveLength(1);
    expect(screen.queryByTestId("authorization-scopes-conn_g2")).not.toBeInTheDocument();
  });

  it("offers no disclosure at all on a Nango credential", async () => {
    routes({ "/api/me/authorizations": { ok: true, body: { authorizations: [MINE] } } });
    render(<AuthorizationsPanel scope={{ kind: "mine" }} />);

    await waitFor(() => expect(screen.getByRole("table")).toBeInTheDocument());
    // A Nango credential has no scope catalogue to show. `auth_path` decides it,
    // never the provider string.
    expect(screen.queryByTestId("authorization-scopes-trigger-conn_mine")).not.toBeInTheDocument();
  });

  it("says the permissions could not be read rather than showing none", async () => {
    // A legitimate boundary: the endpoint gates on project READ access, which an
    // organization admin need not hold. An empty list here would read as
    // "this consent opens nothing".
    routes({
      "/api/organizations/org_1/authorizations": { ok: true, body: { authorizations: [GOOGLE_ROW] } },
      "/connectors": NO_UNOPENED,
      "/api/google/oauth/status/": { ok: false, status: 403 },
    });
    render(<AuthorizationsPanel scope={{ kind: "org", orgId: "org_1" }} />);

    fireEvent.click(await screen.findByTestId("authorization-scopes-trigger-conn_g"));

    expect(
      await screen.findByText(/could not be read from here/i),
    ).toBeInTheDocument();
    expect(screen.queryByTestId("granted-scopes-conn_g")).not.toBeInTheDocument();
  });
});

/* ==========================================================================
 * REVOKING A GOOGLE CREDENTIAL WITHOUT WITHDRAWING THE CONSENT.
 *
 * `/api/authorizations/{id}/revoke` runs `_apply_credential_revocation`
 * (`server/core/connection_revocation.py:28`): delete at Nango, purge the health
 * cache, `status='revoked'`. It never calls Google's revoke endpoint and never
 * purges the encrypted blob — so on a `google_direct` credential the grant stayed
 * live in the consenting person's Google Account. Only
 * `/api/google/oauth/revoke/{id}` (`google_oauth_api.py:496`) undoes both, and
 * its only caller was the deleted `GoogleConnectPanel`.
 * ========================================================================== */
describe("Authorizations — revoking a Google credential withdraws the consent", () => {
  function routes(byUrl: Record<string, { ok: boolean; status?: number; body?: unknown }>) {
    const fetchMock = vi.fn().mockImplementation((input: unknown) => {
      const url = String(input);
      const key = Object.keys(byUrl).find((candidate) => url.includes(candidate));
      const hit = key ? byUrl[key] : { ok: false, status: 404 };
      return Promise.resolve({
        ok: hit.ok,
        status: hit.status ?? (hit.ok ? 200 : 500),
        json: async () => hit.body ?? {},
        text: async () => "{}",
      });
    });
    vi.stubGlobal("fetch", fetchMock);
    return fetchMock;
  }

  const GOOGLE_ROW = { ...MINE, id: "conn_g", auth_path: "google_direct" as const };
  const CONNECTIONS = {
    ok: true,
    body: { connections: [{ id: "conn_g", active_datastream_count: 2 }] },
  };

  it("names the withdrawal in the confirmation before anything is revoked", async () => {
    const fetchMock = routes({
      "/api/me/authorizations": { ok: true, body: { authorizations: [GOOGLE_ROW] } },
      "/connectors": { ok: true, body: { unlocked_by_reconsent: [] } },
      "/api/connections?": CONNECTIONS,
    });
    render(<AuthorizationsPanel scope={{ kind: "mine" }} />);

    fireEvent.click(await screen.findByRole("button", { name: /^revoke$/i }));

    const evidence = await screen.findByRole("region", { name: /Authorization that will be revoked/i });
    expect(evidence).toHaveTextContent("Withdrawn by this action");
    expect(
      fetchMock.mock.calls.filter((call) => String(call[0]).includes("/revoke")),
    ).toHaveLength(0);
  });

  it("withdraws at Google and only then revokes the credential", async () => {
    const fetchMock = routes({
      "/api/me/authorizations": { ok: true, body: { authorizations: [GOOGLE_ROW] } },
      "/connectors": { ok: true, body: { unlocked_by_reconsent: [] } },
      "/api/connections?": CONNECTIONS,
      "/api/google/oauth/revoke/": { ok: true, body: { revoked: true } },
      "/api/authorizations/": { ok: true, body: {} },
    });
    render(<AuthorizationsPanel scope={{ kind: "mine" }} />);

    fireEvent.click(await screen.findByRole("button", { name: /^revoke$/i }));
    await screen.findByText("2 Datastreams stop pulling");
    fireEvent.click(screen.getByTestId("revoke-authorization-confirm"));

    await waitFor(() => {
      const urls = fetchMock.mock.calls.map((call) => String(call[0]));
      const google = urls.findIndex((url) => url.includes("/api/google/oauth/revoke/conn_g"));
      const generic = urls.findIndex((url) => url.includes("/api/authorizations/conn_g/revoke"));
      expect(google).toBeGreaterThanOrEqual(0);
      expect(generic).toBeGreaterThanOrEqual(0);
      // Order matters: the token must be dead at Google before the row that
      // names it stops being actionable from this screen.
      expect(google).toBeLessThan(generic);
    });
    expect(screen.queryByTestId("consent-not-withdrawn")).not.toBeInTheDocument();
  });

  it("still revokes when the withdrawal is refused, and says what is left standing", async () => {
    // The Google route gates on `identity_can_manage_org`; the generic one also
    // accepts the author. A legitimate 403 must not abandon a revocation the
    // person asked for — nor be reported as if the consent were gone.
    const fetchMock = routes({
      "/api/me/authorizations": { ok: true, body: { authorizations: [GOOGLE_ROW] } },
      "/connectors": { ok: true, body: { unlocked_by_reconsent: [] } },
      "/api/connections?": CONNECTIONS,
      "/api/google/oauth/revoke/": { ok: false, status: 403 },
      "/api/authorizations/": { ok: true, body: {} },
    });
    render(<AuthorizationsPanel scope={{ kind: "mine" }} />);

    fireEvent.click(await screen.findByRole("button", { name: /^revoke$/i }));
    await screen.findByText("2 Datastreams stop pulling");
    fireEvent.click(screen.getByTestId("revoke-authorization-confirm"));

    expect(await screen.findByTestId("consent-not-withdrawn")).toHaveTextContent(
      /stays listed in the Google Account/i,
    );
    expect(
      fetchMock.mock.calls.filter((call) =>
        String(call[0]).includes("/api/authorizations/conn_g/revoke"),
      ),
    ).toHaveLength(1);
  });

  it("asks Google nothing when the credential is a Nango one", async () => {
    const fetchMock = routes({
      "/api/me/authorizations": { ok: true, body: { authorizations: [MINE] } },
      "/api/connections?": {
        ok: true,
        body: { connections: [{ id: "conn_mine", active_datastream_count: 0 }] },
      },
      "/api/authorizations/": { ok: true, body: {} },
    });
    render(<AuthorizationsPanel scope={{ kind: "mine" }} />);

    fireEvent.click(await screen.findByRole("button", { name: /^revoke$/i }));
    await screen.findByText(/None — no Datastream pulls with it/);
    fireEvent.click(screen.getByTestId("revoke-authorization-confirm"));

    await waitFor(() =>
      expect(
        fetchMock.mock.calls.filter((call) =>
          String(call[0]).includes("/api/authorizations/conn_mine/revoke"),
        ),
      ).toHaveLength(1),
    );
    expect(
      fetchMock.mock.calls.filter((call) => String(call[0]).includes("/api/google/oauth/revoke/")),
    ).toHaveLength(0);
  });
});
