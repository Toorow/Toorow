/**
 * The ratified operator gesture on the Master Data screen (governance.md,
 * amendment of 2026-08-25, acceptance schedule ratified the same day).
 *
 * The command and its plan shipped on the server and NO screen called either, so
 * the gesture existed for `curl` and for nobody else. What these tests pin is not
 * markup — it is the four claims the amendment makes about how a person meets it:
 *
 *   1. the PLAN is shown before the act, with the counts, so the command can be
 *      consented to instead of merely triggered;
 *   2. the confirmation NAMES the organization and the counts — never a blind
 *      button;
 *   3. a refusal is stated as itself and names the gesture that repairs it, and
 *      the plan's own refusal (an unreachable parent) is stated BEFORE the click;
 *   4. an organization that has already converged is told so, and is not offered
 *      a gesture that would move nothing.
 *
 * And one negative claim, which is the reason this screen changed at all: the
 * legacy create door is not drawn on an organization whose writes the cutover
 * refuses.
 */
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import ContentRouter from "../shell/ContentRouter";
import { RouterProvider } from "../shell/router";
import MasterDataConvergencePanel, {
  type ConvergencePlan,
  type ConvergenceRead,
} from "../governance/MasterDataConvergence";

const ORG = "org_EXAMPLE";
const PROJECT = "proj_EXAMPLE";
const ROOT = `/org/${ORG}/project/${PROJECT}`;

function plan(overrides: Partial<ConvergencePlan> = {}): ConvergencePlan {
  return {
    org_id: ORG,
    pending_domains: 6,
    pending_classifications: 12,
    already_converged_domains: 0,
    already_converged_classifications: 0,
    unreachable_parents: [],
    ...overrides,
  };
}

function ready(overrides: Partial<ConvergencePlan> = {}): ConvergenceRead {
  return { status: "ready", plan: plan(overrides) };
}

function panel(read: ConvergenceRead, handlers: Record<string, () => void> = {}) {
  return render(
    <MasterDataConvergencePanel
      projectId={PROJECT}
      read={read}
      onReload={handlers.onReload ?? (() => undefined)}
      onConverged={handlers.onConverged ?? (() => undefined)}
    />,
  );
}

function answer(status: number, body: unknown): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
    text: async () => JSON.stringify(body),
  } as Response;
}

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  window.history.replaceState({}, "", "/");
});

describe("Master Data convergence — the plan is read before the act", () => {
  it("states how much would move, and that no identity is minted or destroyed", () => {
    panel(ready());

    expect(
      screen.getByText(/6 Business Domains and 12 Classifications would move into Master Data/),
    ).toBeInTheDocument();
    expect(screen.getByText(/keeps the id it has today/)).toBeInTheDocument();
    expect(screen.getByText(/Nothing is deleted/)).toBeInTheDocument();
  });

  it("counts what already moved apart from what is left, so a resumed run is not a surprise", () => {
    panel(ready({ pending_domains: 1, pending_classifications: 0, already_converged_domains: 5 }));

    expect(screen.getByText(/1 Business Domain and 0 Classifications would move/)).toBeInTheDocument();
    expect(screen.getByText(/5 Business Domains and 0 Classifications already moved/)).toBeInTheDocument();
  });

  it("asks for the reason before the act, and refuses to open the confirmation without one", () => {
    panel(ready());

    fireEvent.click(screen.getByRole("button", { name: "Converge to Master Data" }));

    expect(screen.getByText(/Add a reason/)).toBeInTheDocument();
    expect(screen.queryByTestId("convergence-confirm")).not.toBeInTheDocument();
  });
});

describe("Master Data convergence — the confirmation names the organization and the counts", () => {
  function openConfirmation() {
    fireEvent.change(screen.getByLabelText("Reason for this convergence"), {
      target: { value: "Single Master Data authority" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Converge to Master Data" }));
  }

  it("is never a blind button: the organization and both counts are in the confirmation", () => {
    panel(ready());
    openConfirmation();

    expect(screen.getByTestId("convergence-confirm")).toBeInTheDocument();
    expect(screen.getByText(`Converge ${ORG}?`)).toBeInTheDocument();
    expect(
      screen.getByText(/6 Business Domains and 12 Classifications of org_EXAMPLE move into Master Data/),
    ).toBeInTheDocument();
    expect(screen.getByText(/Single Master Data authority/)).toBeInTheDocument();
  });

  it("sends the reason and one replay key, and re-reads both the plan and the list", async () => {
    const calls: Array<[string, RequestInit | undefined]> = [];
    vi.stubGlobal(
      "fetch",
      vi.fn((url: string, init?: RequestInit) => {
        calls.push([url, init]);
        return Promise.resolve(
          answer(200, {
            operation_id: "op_1",
            outcome: "succeeded",
            idempotent_replay: false,
            result: { converged_domains: 6, converged_classifications: 12 },
          }),
        );
      }),
    );
    const reloaded = vi.fn();
    const converged = vi.fn();
    panel(ready(), { onReload: reloaded, onConverged: converged });
    openConfirmation();

    fireEvent.click(screen.getByTestId("convergence-confirm-run"));

    await waitFor(() => expect(reloaded).toHaveBeenCalled());
    expect(converged).toHaveBeenCalled();
    const [url, init] = calls[0];
    expect(url).toBe(
      `/api/projects/${PROJECT}/governance/master-data/convergence`,
    );
    expect(init?.method).toBe("POST");
    expect(String(init?.body)).toContain("Single Master Data authority");
    const headers = init?.headers as Record<string, string>;
    expect(headers["Idempotency-Key"]).toBeTruthy();
  });

  it("carries the SAME replay key into a retry, so a client timeout cannot act twice", async () => {
    const keys: string[] = [];
    let attempt = 0;
    vi.stubGlobal(
      "fetch",
      vi.fn((_url: string, init?: RequestInit) => {
        keys.push((init?.headers as Record<string, string>)["Idempotency-Key"]);
        attempt += 1;
        return Promise.resolve(
          attempt === 1
            ? answer(503, { code: "governance_unavailable", message: "Governance is unavailable" })
            : answer(200, { operation_id: "op_1", outcome: "succeeded", result: {} }),
        );
      }),
    );
    panel(ready());
    openConfirmation();

    fireEvent.click(screen.getByTestId("convergence-confirm-run"));
    await screen.findByText(/Master Data could not be read, so nothing was converged/);

    fireEvent.click(screen.getByTestId("convergence-confirm-run"));
    await waitFor(() => expect(keys).toHaveLength(2));
    expect(keys[0]).toBe(keys[1]);
  });
});

describe("Master Data convergence — a refusal is stated as itself", () => {
  it("carries the server's 409 through, because it names the identities involved", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() =>
        Promise.resolve(
          answer(409, {
            code: "master_data_convergence_refused",
            message:
              "these classifications name a parent that is neither converged nor in this batch, so converging them would drop their branch: bcl_9",
            detail: { unreachable_parents: ["bcl_9"] },
          }),
        ),
      ),
    );
    const converged = vi.fn();
    panel(ready(), { onConverged: converged });

    fireEvent.change(screen.getByLabelText("Reason for this convergence"), {
      target: { value: "Single authority" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Converge to Master Data" }));
    fireEvent.click(screen.getByTestId("convergence-confirm-run"));

    expect(await screen.findByText(/would drop their branch: bcl_9/)).toBeInTheDocument();
    expect(converged).not.toHaveBeenCalled();
  });

  it("names the Manage right rather than the HTTP status when the account cannot run it", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() => Promise.resolve(answer(403, { code: "denied", message: "denied" }))),
    );
    panel(ready());

    fireEvent.change(screen.getByLabelText("Reason for this convergence"), {
      target: { value: "Single authority" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Converge to Master Data" }));
    fireEvent.click(screen.getByTestId("convergence-confirm-run"));

    expect(
      await screen.findByText(/Converging an organization is a Manage right/),
    ).toBeInTheDocument();
  });

  it("refuses BEFORE the click when the plan already knows a branch would be dropped", () => {
    panel(ready({ unreachable_parents: ["bcl_9", "bcl_10"] }));

    expect(screen.getByTestId("convergence-blocked")).toBeInTheDocument();
    expect(screen.getByText(/bcl_9, bcl_10/)).toBeInTheDocument();
    expect(screen.getByText(/Give each one a parent that exists/)).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Converge to Master Data" }),
    ).not.toBeInTheDocument();
  });
});

describe("Master Data convergence — the states that are not a gesture", () => {
  it("says an organization has converged, and offers nothing that would move nothing", () => {
    panel(
      ready({
        pending_domains: 0,
        pending_classifications: 0,
        already_converged_domains: 6,
        already_converged_classifications: 12,
      }),
    );

    expect(screen.getByTestId("convergence-converged")).toBeInTheDocument();
    expect(
      screen.getByText(/6 Business Domains and 12 Classifications are governed by Master Data/),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "Converge to Master Data" }),
    ).not.toBeInTheDocument();
  });

  it("draws nothing at all for an organization that never held a legacy taxonomy", () => {
    const { container } = panel(
      ready({ pending_domains: 0, pending_classifications: 0 }),
    );
    expect(container).toBeEmptyDOMElement();
  });

  it("tells an unreadable plan apart from a converged one", () => {
    const reloaded = vi.fn();
    panel({ status: "unreadable", message: "Master Data could not be read." }, { onReload: reloaded });

    expect(screen.getByTestId("convergence-unreadable")).toBeInTheDocument();
    expect(screen.getByText(/This is not a statement that it has converged/)).toBeInTheDocument();
    expect(screen.queryByTestId("convergence-converged")).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(reloaded).toHaveBeenCalled();
  });

  it("draws nothing while the plan is still being read", () => {
    const { container } = panel({ status: "loading" });
    expect(container).toBeEmptyDOMElement();
  });
});

/**
 * The screen-level claim, through the real router: an unconverged organization
 * is not offered the legacy create door, and IS offered the convergence.
 */
describe("Master Data screen — the door that would be refused is not drawn", () => {
  function serveCollection(planBody: Record<string, unknown>) {
    vi.stubGlobal(
      "fetch",
      vi.fn((url: string) => {
        if (url.includes("/governance/master-data/convergence")) {
          return Promise.resolve(answer(200, planBody));
        }
        if (url.includes("/governance/")) {
          return Promise.resolve(
            answer(200, {
              schema_version: "governance-collection.v1",
              project_ref: { object_type: "project", id: PROJECT },
              organization_ref: { object_type: "organization", id: ORG },
              section: "master-data",
              generated_at: "2026-08-25T09:00:00Z",
              evidence_as_of: "2026-08-25T08:00:00Z",
              lens: "business-domains",
              default_lens: "business-domains",
              available_lenses: ["business-domains"],
              items: [],
              coverage: {
                state: "empty",
                returned: 0,
                total: 0,
                bound: 200,
                index_state: "complete",
                adapters: [],
              },
              unavailable_reasons: [],
              next_cursor: null,
              applied_filters: {},
            }),
          );
        }
        return Promise.resolve(answer(404, { code: "not_found", message: "not found" }));
      }),
    );
    window.history.replaceState({}, "", `${ROOT}/governance/master-data/lens/business-domains`);
    return render(
      <RouterProvider>
        <ContentRouter />
      </RouterProvider>,
    );
  }

  it("withholds the create action and names the convergence instead", async () => {
    serveCollection({
      org_id: ORG,
      pending_domains: 6,
      pending_classifications: 12,
      already_converged_domains: 0,
      already_converged_classifications: 0,
      unreachable_parents: [],
    });

    expect(await screen.findByTestId("convergence-panel")).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "+ New Domain / Taxonomy" }),
    ).not.toBeInTheDocument();
    // An empty list names the gesture that unblocks it, not the one that 409s.
    expect(
      await screen.findByText(/has not converged into Master Data yet, so nothing can be declared here/),
    ).toBeInTheDocument();
  });

  it("keeps the create action once the organization has converged", async () => {
    serveCollection({
      org_id: ORG,
      pending_domains: 0,
      pending_classifications: 0,
      already_converged_domains: 6,
      already_converged_classifications: 12,
      unreachable_parents: [],
    });

    expect(await screen.findByTestId("convergence-converged")).toBeInTheDocument();
    expect(
      await screen.findByRole("button", { name: "+ New Domain / Taxonomy" }),
    ).toBeInTheDocument();
  });
});
