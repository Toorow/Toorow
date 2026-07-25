/**
 * Tests for the two irreversible surfaces of the console:
 *   - OrgDangerZone (shell/pages/OrgSettings.tsx) — delete an organization,
 *     which also drops its warehouse datasets;
 *   - AccountSettings (shell/pages/AccountSettings.tsx) — erase your own
 *     account.
 *
 * What is guarded here is not the happy path, it is the honesty of the screen:
 *   - the preview is fetched when the zone is OPENED, and everything shown is
 *     what the preview returned (projects, counts, warehouse datasets);
 *   - the destructive button stays disabled until the confirmation string
 *     matches EXACTLY;
 *   - a blocked preview offers no deletion at all and says what blocks it;
 *   - a FAILED preview is reported as a failure — never an empty list, which
 *     would read as "there is nothing to destroy" (finding F-010);
 *   - the sole-owner case is explained before the attempt, and the server's 409
 *     is still handled.
 *
 * The transport is the global fetch, because src/lib/apiFetch.ts is the single
 * seam every call goes through — stubbing fetch also proves the calls carry the
 * bearer token and the X-Confirm-Delete header.
 */
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { OrgDangerZone, type OrgDeletionPreview } from "../shell/pages/OrgSettings";
import AccountSettings, {
  type AccountDeletionPreview,
} from "../shell/pages/AccountSettings";

// ---------------------------------------------------------------------------
// Transport doubles
// ---------------------------------------------------------------------------

function resp(status: number, body: unknown) {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  } as unknown as Response;
}

interface Call {
  url: string;
  init: RequestInit;
}

/** Stub fetch with a router over (url, method) -> Response. Records the calls. */
function stubFetch(handler: (url: string, init: RequestInit) => Response | Promise<Response>) {
  const calls: Call[] = [];
  const mock = vi.fn((url: string, init: RequestInit = {}) => {
    calls.push({ url: String(url), init });
    return Promise.resolve(handler(String(url), init));
  });
  vi.stubGlobal("fetch", mock);
  return calls;
}

const ORG_PREVIEW: OrgDeletionPreview = {
  org_id: "org_acme",
  name: "Acme Media",
  slug: "acme-media",
  projects: [
    { id: "p1", name: "Acme Growth", status: "active" },
    { id: "p2", name: "Acme Brand", status: "paused" },
  ],
  counts: {
    datastreams: 7,
    connections: 3,
    invitations: 2,
    members: 5,
    operations: 128,
  },
  warehouse_datasets: ["org_acme_media_raw", "org_acme_media_marts"],
  blockers: [],
};

const ACCOUNT_PREVIEW: AccountDeletionPreview = {
  identity: "user_42",
  email: "jean@example.com",
  memberships: [
    { org_id: "org_acme", org_name: "Acme Media", role: "owner", other_active_members: 0 },
  ],
  sole_owner_of: [],
  blockers: [],
};

beforeEach(() => {
  localStorage.setItem("api_token", "tok-danger");
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  localStorage.clear();
});

/** Opens the disclosure and waits for the preview to land. */
async function openZone(name: RegExp) {
  await userEvent.click(screen.getByRole("button", { name }));
}

// ===========================================================================
// Organization danger zone
// ===========================================================================

describe("OrgDangerZone — preview", () => {
  it("fetches the preview on opening and shows exactly what will be destroyed", async () => {
    const calls = stubFetch(() => resp(200, ORG_PREVIEW));
    render(<OrgDangerZone orgId="org_acme" onDeleted={vi.fn()} />);

    // Nothing is fetched, and no destructive control exists, until asked for.
    expect(calls).toHaveLength(0);
    expect(
      screen.queryByRole("button", { name: "Delete organization permanently" }),
    ).not.toBeInTheDocument();

    await openZone(/Delete this organization/);

    await screen.findByRole("heading", { name: "Delete this organization", level: 3 });

    // The preview call went through the seam, with the bearer attached.
    expect(calls).toHaveLength(1);
    expect(calls[0].url).toBe("/api/organizations/org_acme/deletion-preview");
    expect(new Headers(calls[0].init.headers).get("Authorization")).toBe("Bearer tok-danger");

    // Named projects.
    expect(await screen.findByText("Acme Growth")).toBeInTheDocument();
    expect(screen.getByText("Acme Brand")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Projects deleted (2)" })).toBeInTheDocument();

    // Counts, each labelled.
    expect(screen.getByText("Datastreams")).toBeInTheDocument();
    expect(screen.getByText("7")).toBeInTheDocument();
    expect(screen.getByText("128")).toBeInTheDocument();

    // Warehouse datasets — named before any confirmation is possible.
    const datasets = screen.getByRole("list", {
      name: "Warehouse datasets that will be dropped",
    });
    expect(datasets).toHaveTextContent("org_acme_media_raw");
    expect(datasets).toHaveTextContent("org_acme_media_marts");
    expect(
      screen.getByRole("heading", { name: "Warehouse datasets dropped (2)" }),
    ).toBeInTheDocument();
  });
});

describe("OrgDangerZone — confirmation", () => {
  it("keeps the delete button disabled until the name matches exactly", async () => {
    stubFetch(() => resp(200, ORG_PREVIEW));
    render(<OrgDangerZone orgId="org_acme" onDeleted={vi.fn()} />);
    await openZone(/Delete this organization/);

    const button = await screen.findByRole("button", {
      name: "Delete organization permanently",
    });
    expect(button).toBeDisabled();

    const input = screen.getByLabelText("Type the organization name to confirm");
    await userEvent.type(input, "Acme");
    expect(button).toBeDisabled();

    // Case and whitespace count: a near-miss is still a miss.
    await userEvent.clear(input);
    await userEvent.type(input, "acme media");
    expect(button).toBeDisabled();

    await userEvent.clear(input);
    await userEvent.type(input, "Acme Media ");
    expect(button).toBeDisabled();

    await userEvent.clear(input);
    await userEvent.type(input, "Acme Media");
    expect(button).toBeEnabled();
  });

  it("sends DELETE with the confirmation header and leaves the deleted org", async () => {
    const calls = stubFetch((_url, init) =>
      init.method === "DELETE"
        ? resp(200, { deleted: true, org_id: "org_acme", removed: {} })
        : resp(200, ORG_PREVIEW),
    );
    const onDeleted = vi.fn();
    render(<OrgDangerZone orgId="org_acme" onDeleted={onDeleted} />);
    await openZone(/Delete this organization/);

    const input = await screen.findByLabelText("Type the organization name to confirm");
    await userEvent.type(input, "Acme Media");
    await userEvent.click(
      screen.getByRole("button", { name: "Delete organization permanently" }),
    );

    await waitFor(() => expect(onDeleted).toHaveBeenCalledTimes(1));
    const del = calls.find((c) => c.init.method === "DELETE");
    expect(del?.url).toBe("/api/organizations/org_acme");
    expect(new Headers(del?.init.headers).get("X-Confirm-Delete")).toBe(
      "drop-warehouse-data",
    );
  });

  it("reports a failed deletion and does not claim success", async () => {
    stubFetch((_url, init) =>
      init.method === "DELETE"
        ? resp(500, { code: "internal", message: "Warehouse drop failed" })
        : resp(200, ORG_PREVIEW),
    );
    const onDeleted = vi.fn();
    render(<OrgDangerZone orgId="org_acme" onDeleted={onDeleted} />);
    await openZone(/Delete this organization/);

    await userEvent.type(
      await screen.findByLabelText("Type the organization name to confirm"),
      "Acme Media",
    );
    await userEvent.click(
      screen.getByRole("button", { name: "Delete organization permanently" }),
    );

    expect(await screen.findByText(/Organization not deleted/)).toBeInTheDocument();
    expect(screen.getByText(/Warehouse drop failed/)).toBeInTheDocument();
    expect(onDeleted).not.toHaveBeenCalled();
  });
});

describe("OrgDangerZone — blocked", () => {
  it("explains what blocks the deletion and offers no destructive control", async () => {
    stubFetch(() =>
      resp(200, {
        ...ORG_PREVIEW,
        blockers: [
          { kind: "active_datastream", detail: "2 datastreams are still running." },
          { kind: "billing", detail: "An invoice is still open for this organization." },
        ],
      }),
    );
    render(<OrgDangerZone orgId="org_acme" onDeleted={vi.fn()} />);
    await openZone(/Delete this organization/);

    expect(
      await screen.findByText("This organization cannot be deleted yet"),
    ).toBeInTheDocument();
    expect(screen.getByText("2 datastreams are still running.")).toBeInTheDocument();
    expect(screen.getByText("active_datastream")).toBeInTheDocument();
    expect(
      screen.getByText("An invoice is still open for this organization."),
    ).toBeInTheDocument();

    // No confirmation, no delete button at all.
    expect(
      screen.queryByLabelText("Type the organization name to confirm"),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Delete organization permanently" }),
    ).not.toBeInTheDocument();
  });
});

describe("OrgDangerZone — preview failure", () => {
  it("says the check failed instead of showing an empty destruction list", async () => {
    stubFetch(() => resp(500, { code: "internal", message: "Preview unavailable" }));
    render(<OrgDangerZone orgId="org_acme" onDeleted={vi.fn()} />);
    await openZone(/Delete this organization/);

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("We could not check what would be deleted");
    expect(alert).toHaveTextContent("Preview unavailable");
    expect(alert).toHaveTextContent("Nothing has been deleted");

    // Crucially: no manifest is rendered, and no deletion is offered.
    expect(screen.queryByText(/Projects deleted/)).not.toBeInTheDocument();
    expect(screen.queryByText(/Warehouse datasets dropped/)).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Delete organization permanently" }),
    ).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Try again" })).toBeInTheDocument();
  });

  it("retries the preview and recovers", async () => {
    let attempts = 0;
    stubFetch(() => {
      attempts += 1;
      return attempts === 1 ? resp(503, { code: "unavailable" }) : resp(200, ORG_PREVIEW);
    });
    render(<OrgDangerZone orgId="org_acme" onDeleted={vi.fn()} />);
    await openZone(/Delete this organization/);

    await screen.findByRole("button", { name: "Try again" });
    await userEvent.click(screen.getByRole("button", { name: "Try again" }));

    expect(await screen.findByText("Acme Growth")).toBeInTheDocument();
  });
});

// ===========================================================================
// Account deletion
// ===========================================================================

describe("AccountSettings — preview", () => {
  it("shows the identity, the memberships and what is retained", async () => {
    const calls = stubFetch(() => resp(200, ACCOUNT_PREVIEW));
    render(<AccountSettings onDeleted={vi.fn()} />);

    expect(
      screen.getByRole("heading", { name: "Your account", level: 1 }),
    ).toBeInTheDocument();
    expect(calls).toHaveLength(0);

    await openZone(/Delete my account/);

    expect(calls[0].url).toBe("/api/me/deletion-preview");
    const identity = await screen.findByRole("list", { name: "Account identity" });
    expect(identity).toHaveTextContent("jean@example.com");
    expect(identity).toHaveTextContent("user_42");
    expect(screen.getByRole("heading", { name: "Memberships removed (1)" })).toBeInTheDocument();
    expect(screen.getByText(/Acme Media — 0 other active members/)).toBeInTheDocument();

    // The promise of erasure has to be exact: audit entries are kept.
    expect(screen.getByRole("heading", { name: "Kept after erasure" })).toBeInTheDocument();
    expect(screen.getByText(/Audit entries are retained/)).toBeInTheDocument();
  });

  it("keeps the delete button disabled until the email matches exactly", async () => {
    stubFetch(() => resp(200, ACCOUNT_PREVIEW));
    render(<AccountSettings onDeleted={vi.fn()} />);
    await openZone(/Delete my account/);

    const button = await screen.findByRole("button", {
      name: "Delete my account permanently",
    });
    expect(button).toBeDisabled();

    const input = screen.getByLabelText("Type your email address to confirm");
    await userEvent.type(input, "jean@example.co");
    expect(button).toBeDisabled();
    await userEvent.type(input, "m");
    expect(button).toBeEnabled();
  });

  it("reports the failure instead of an empty account when the preview fails", async () => {
    stubFetch(() => resp(401, { code: "unauthenticated", message: "Missing bearer token" }));
    render(<AccountSettings onDeleted={vi.fn()} />);
    await openZone(/Delete my account/);

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("We could not check what would be erased");
    expect(alert).toHaveTextContent("Nothing has been erased");
    expect(screen.queryByText(/Memberships removed/)).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Delete my account permanently" }),
    ).not.toBeInTheDocument();
  });
});

describe("AccountSettings — sole owner", () => {
  it("explains the sole-owner case BEFORE any attempt and blocks the erasure", async () => {
    stubFetch(() =>
      resp(200, {
        ...ACCOUNT_PREVIEW,
        memberships: [
          {
            org_id: "org_acme",
            org_name: "Acme Media",
            role: "owner",
            other_active_members: 4,
          },
        ],
        sole_owner_of: [{ org_id: "org_acme", org_name: "Acme Media" }],
      }),
    );
    render(<AccountSettings onDeleted={vi.fn()} />);
    await openZone(/Delete my account/);

    expect(
      await screen.findByText(
        "You are the only owner of an organization that still has members",
      ),
    ).toBeInTheDocument();
    // The action to take is stated, not just the refusal.
    expect(screen.getByText(/promote another member to owner/i)).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Delete my account permanently" }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByLabelText("Type your email address to confirm"),
    ).not.toBeInTheDocument();
  });

  it("handles a 409 raised at confirmation time without claiming the account is gone", async () => {
    // The preview is clean (nobody else in the org yet), so the form is offered;
    // the 409 arrives from the server on the DELETE.
    const onDeleted = vi.fn();
    stubFetch((_url, init) => {
      if (init.method === "DELETE") {
        return resp(409, {
          code: "sole_owner",
          message: "You are the sole owner of Acme Media.",
        });
      }
      return resp(200, ACCOUNT_PREVIEW);
    });
    render(<AccountSettings onDeleted={onDeleted} />);
    await openZone(/Delete my account/);

    await userEvent.type(
      await screen.findByLabelText("Type your email address to confirm"),
      "jean@example.com",
    );
    await userEvent.click(
      screen.getByRole("button", { name: "Delete my account permanently" }),
    );

    expect(await screen.findByText("Your account was not erased")).toBeInTheDocument();
    expect(
      screen.getByText(/You are the sole owner of Acme Media\./),
    ).toBeInTheDocument();
    expect(onDeleted).not.toHaveBeenCalled();
  });
});

describe("AccountSettings — erasure", () => {
  it("sends DELETE with the confirmation header and states what was retained", async () => {
    const calls = stubFetch((_url, init) =>
      init.method === "DELETE"
        ? resp(200, {
            deleted: true,
            erased: { memberships: 1 },
            retained: { audit_entries: 12, reason: "Audit history belongs to the org." },
          })
        : resp(200, ACCOUNT_PREVIEW),
    );
    const onDeleted = vi.fn();
    render(<AccountSettings onDeleted={onDeleted} />);
    await openZone(/Delete my account/);

    await userEvent.type(
      await screen.findByLabelText("Type your email address to confirm"),
      "jean@example.com",
    );
    await userEvent.click(
      screen.getByRole("button", { name: "Delete my account permanently" }),
    );

    await waitFor(() => expect(onDeleted).toHaveBeenCalledTimes(1));
    const del = calls.find((c) => c.init.method === "DELETE");
    expect(del?.url).toBe("/api/me");
    expect(new Headers(del?.init.headers).get("X-Confirm-Delete")).toBe("erase-account");
    expect(await screen.findByText(/12 audit entries were retained/)).toBeInTheDocument();
  });
});
