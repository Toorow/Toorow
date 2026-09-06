/**
 * Organization Settings — Data access.
 *
 * The fourth and last capability of the unmounted `orgs/OrgDetailPanel.tsx`, and
 * the only one that had no ratified target: it grants an external IAM principal
 * read access to the organization's `org_<slug>_marts` dataset, and no document
 * of `docs/product-architecture/` mentioned that act. Mounted on Jean's
 * instruction; the contract is written into `data.md` in the same commit.
 *
 * What is pinned: the section is reachable at its own URL, it reads and writes
 * the org-scoped routes that already existed, and a refused grant is reported
 * rather than swallowed.
 */
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import OrgSettings from "../shell/pages/OrgSettings";
import { parsePath } from "../shell/router";

interface Reply { ok: boolean; status: number; body: unknown }
const OK = (body: unknown): Reply => ({ ok: true, status: 200, body });

function stubApi(routes: Array<[string, RegExp, Reply]>) {
  const calls: Array<{ url: string; init: RequestInit }> = [];
  vi.stubGlobal("fetch", vi.fn(async (url: string, init: RequestInit = {}) => {
    calls.push({ url, init });
    const method = (init.method ?? "GET").toUpperCase();
    const hit = routes.find(([m, pattern]) => m === method && pattern.test(url));
    if (!hit) throw new Error(`unrouted ${method} ${url}`);
    return { ok: hit[2].ok, status: hit[2].status, json: async () => hit[2].body };
  }));
  return calls;
}

const GRANTS = {
  grants: [{
    id: "dagrant_1", org_id: "org-1",
    principal: "serviceAccount:bi@example.iam.gserviceaccount.com",
    granted_by: "owner@example.com", created_at: "2026-08-01T09:00:00Z",
    lifecycle_state: "effective", dataset_id: "org_acme_marts",
    role: "roles/bigquery.dataViewer", effective_at: "2026-08-01T09:00:01Z",
    revoked_at: null, last_provider_error: null, provider_error_at: null,
    updated_at: "2026-08-01T09:00:01Z",
  }],
};
const READ: [string, RegExp, Reply] = ["GET", /\/dataset-access$/, OK(GRANTS)];

afterEach(() => vi.unstubAllGlobals());

describe("Data access", () => {
  it("is a real address, not only a rendered panel", () => {
    // A section the router does not know navigates to a screen that cannot
    // resolve — the reason the sets in `router.tsx` are declared, not inferred.
    const route = parsePath("/org/org-1/settings/data-access");
    expect(route.kind).not.toBe("unknown");
  });

  it("lists the principals that may read the warehouse", async () => {
    stubApi([READ]);
    render(<OrgSettings orgId="org-1" section="data-access" />);

    expect(await screen.findByText("serviceAccount:bi@example.iam.gserviceaccount.com")).toBeInTheDocument();
    // It must not read as access to the SOURCES: it opens the warehouse toorow
    // builds, never a provider credential.
    expect(screen.getByText(/never exposes a provider credential/i)).toBeInTheDocument();
  });

  it("grants a principal through the org-scoped route", async () => {
    const calls = stubApi([
      READ,
      ["POST", /\/dataset-access$/, { ok: true, status: 201, body: { id: "dagrant_2" } }],
    ]);
    render(<OrgSettings orgId="org-1" section="data-access" />);

    // The `type:identifier` string is composed from a chosen kind and a typed
    // address — it is no longer dictated to the person as one free string.
    await userEvent.selectOptions(
      await screen.findByTestId("data-access-principal-kind"),
      "serviceAccount",
    );
    await userEvent.type(
      screen.getByTestId("data-access-principal-input"),
      "new@example.iam.gserviceaccount.com",
    );
    await userEvent.click(screen.getByTestId("data-access-grant-button"));

    await waitFor(() => {
      const post = calls.find((c) => (c.init.method ?? "GET") === "POST");
      expect(post).toBeDefined();
      expect(post!.url).toContain("/api/organizations/org-1/dataset-access");
      expect(JSON.parse(String(post!.init.body))).toEqual({
        principal: "serviceAccount:new@example.iam.gserviceaccount.com",
      });
    });
  });

  it("reports a refused grant instead of swallowing it", async () => {
    stubApi([
      READ,
      ["POST", /\/dataset-access$/, { ok: false, status: 403, body: { code: "forbidden", message: "manage required" } }],
    ]);
    render(<OrgSettings orgId="org-1" section="data-access" />);

    await userEvent.selectOptions(
      await screen.findByTestId("data-access-principal-kind"),
      "serviceAccount",
    );
    await userEvent.type(
      screen.getByTestId("data-access-principal-input"),
      "new@example.iam.gserviceaccount.com",
    );
    await userEvent.click(screen.getByTestId("data-access-grant-button"));

    expect(await screen.findByTestId("data-access-op-error")).toBeInTheDocument();
  });
});
