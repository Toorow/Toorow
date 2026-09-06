/**
 * Taking a decision on a Control Case (Story 49.4 AC7, AI-192).
 *
 * The defect these tests are written against is not a rendering bug. Measured
 * 2026-08-04, `ControlsQualityTabs.tsx` held ZERO POST and ZERO buttons: the
 * command family had been served since 49.4 and no screen ever called it, so
 * Governance could show a decision and never take one.
 *
 * So the first assertion is that the act exists at all, and every other one is
 * about the property that makes it safe to exist: a decision is append-only, so
 * `confirm` must be unreachable until the SERVER-derived impact has been shown.
 *
 * `fetch` is stubbed, which proves the contract and not that a row lands in
 * `app.control_case_decisions` — the pg-gated suite of 49.4 owns that. What is
 * proven here is the sequence, and the sequence is where the irreversible click
 * would have been.
 */
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { CaseDecisionHistoryTab } from "../governance/ControlsQualityTabs";

const CASE_ID = "ctc_01J0000000000000000000000";
const BASE = "/governance/controls-quality/change-sets";

function detail(summary: Record<string, unknown> = {}) {
  return {
    object_ref: { type: "control-case", id: CASE_ID, label: "Mapping conflict", owner_href: "" },
    scope: "project",
    owner: {},
    lifecycle_status: "open",
    active_version_ref: null,
    selected_version_ref: null,
    available_tabs: [],
    default_tab: "decision-history",
    allowed_actions: [],
    used_by: { state: "empty", count: 0, refs: [] },
    versions: { state: "empty", count: 0, refs: [] },
    evidence: { state: "empty", count: 0, refs: [] },
    summary: { facets_state: "available", decisions: [], candidates: [], ...summary },
    evidence_as_of: null,
  } as never;
}

/** Every call the dialog makes, in order, so the SEQUENCE can be asserted. */
let calls: { url: string; body: Record<string, unknown> }[] = [];

function stubFetch(responder: (url: string) => { status?: number; json: unknown }) {
  return vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    calls.push({ url, body: init?.body ? JSON.parse(String(init.body)) : {} });
    const { status = 200, json } = responder(url);
    return new Response(JSON.stringify(json), {
      status,
      headers: { "Content-Type": "application/json" },
    });
  });
}

function renderTab(summary: Record<string, unknown> = {}) {
  return render(
    <CaseDecisionHistoryTab detail={detail(summary)} projectId="proj_EXAMPLE" onDecided={() => {}} />,
  );
}

async function openAndFill() {
  renderTab();
  const user = userEvent.setup();
  await user.click(screen.getByRole("button", { name: /take a decision/i }));
  await user.type(screen.getByLabelText(/reason/i), "The candidate matches the source grain.");
  return user;
}

beforeEach(() => {
  calls = [];
});

afterEach(() => {
  // Never leave a stub behind: a shared `fetch` makes the NEXT test pass for
  // the previous test's reason.
  vi.unstubAllGlobals();
});

describe("Control Case — the decision act exists", () => {
  it("offers to take a decision, which this screen could not do at all", () => {
    renderTab();
    expect(screen.getByRole("button", { name: /take a decision/i })).toBeInTheDocument();
  });

  it("does not offer the act when no project can be addressed", () => {
    // A button that cannot act is the read-only theatre this tab just left.
    render(<CaseDecisionHistoryTab detail={detail()} />);
    expect(screen.queryByRole("button", { name: /take a decision/i })).not.toBeInTheDocument();
  });
});

describe("Control Case — a decision is never one click", () => {
  it("prepares and shows the server-derived impact WITHOUT confirming", async () => {
    vi.stubGlobal(
      "fetch",
      stubFetch((url) =>
        url.endsWith("/prepare")
          ? {
              json: {
                id: "ccs_1",
                confirmation_token: "tok_ABC",
                impact: { candidates: 3 },
                dependency_fingerprint: "abcdef0123456789",
                expires_at: "2026-08-04T23:00:00Z",
              },
            }
          : { status: 201, json: { id: "ccs_1" } },
      ),
    );
    const user = await openAndFill();
    await user.click(screen.getByRole("button", { name: /review impact/i }));

    await screen.findByText(/review before deciding/i);
    // The impact came from the server and is on screen before anything commits.
    expect(screen.getByText("3")).toBeInTheDocument();
    expect(screen.getByText(/abcdef012345…/)).toBeInTheDocument();

    // The whole point: two calls, and neither of them is confirm.
    expect(calls.map((call) => call.url.replace(/^.*\/api\/projects\/[^/]+/, ""))).toEqual([
      BASE,
      `${BASE}/ccs_1/prepare`,
    ]);
    expect(calls.some((call) => call.url.endsWith("/confirm"))).toBe(false);
  });

  it("sends the case, the kind and the verbatim reason on create", async () => {
    vi.stubGlobal(
      "fetch",
      stubFetch((url) =>
        url.endsWith("/prepare")
          ? { json: { id: "ccs_1", confirmation_token: "tok_ABC", impact: {} } }
          : { status: 201, json: { id: "ccs_1" } },
      ),
    );
    const user = await openAndFill();
    await user.click(screen.getByRole("button", { name: /review impact/i }));
    await screen.findByText(/review before deciding/i);

    const created = calls[0].body as { object_type: string; object_id: string; intent: Record<string, unknown> };
    expect(created.object_type).toBe("control-case");
    expect(created.object_id).toBe(CASE_ID);
    expect(created.intent.decision_kind).toBe("approve_mapping");
    expect(created.intent.reason).toBe("The candidate matches the source grain.");
    expect(created).toHaveProperty("idempotency_key");
  });

  it("confirms with the single-use token, and only after the review step", async () => {
    vi.stubGlobal(
      "fetch",
      stubFetch((url) => {
        if (url.endsWith("/prepare"))
          return { json: { id: "ccs_1", confirmation_token: "tok_ABC", impact: { candidates: 0 } } };
        if (url.endsWith("/confirm")) return { json: { result_version_id: "ctcd_1" } };
        return { status: 201, json: { id: "ccs_1" } };
      }),
    );
    const user = await openAndFill();
    await user.click(screen.getByRole("button", { name: /review impact/i }));
    await screen.findByText(/review before deciding/i);
    await user.click(screen.getByRole("button", { name: /confirm decision/i }));

    await waitFor(() => expect(calls.some((call) => call.url.endsWith("/confirm"))).toBe(true));
    const confirm = calls.find((call) => call.url.endsWith("/confirm"));
    expect(confirm?.body).toEqual({ confirmation_token: "tok_ABC" });
  });
});

describe("Control Case — nothing is decided on a refusal", () => {
  it("refuses to go on when prepare hands out no confirmation", async () => {
    vi.stubGlobal(
      "fetch",
      stubFetch((url) =>
        url.endsWith("/prepare")
          ? { json: { id: "ccs_1", impact: {} } } // no token
          : { status: 201, json: { id: "ccs_1" } },
      ),
    );
    const user = await openAndFill();
    await user.click(screen.getByRole("button", { name: /review impact/i }));

    // Title and message both say it; either one appearing is the point.
    expect((await screen.findAllByText(/nothing was decided/i)).length).toBeGreaterThan(0);
    // A decision nobody can commit must not look taken.
    expect(screen.queryByRole("button", { name: /confirm decision/i })).not.toBeInTheDocument();
    expect(calls.some((call) => call.url.endsWith("/confirm"))).toBe(false);
  });

  it("sends the operator back to preparing when confirm is refused", async () => {
    vi.stubGlobal(
      "fetch",
      stubFetch((url) => {
        if (url.endsWith("/prepare"))
          return { json: { id: "ccs_1", confirmation_token: "tok_ABC", impact: {} } };
        if (url.endsWith("/confirm"))
          return { status: 409, json: { code: "stale", message: "Dependencies moved." } };
        return { status: 201, json: { id: "ccs_1" } };
      }),
    );
    const user = await openAndFill();
    await user.click(screen.getByRole("button", { name: /review impact/i }));
    await screen.findByText(/review before deciding/i);
    await user.click(screen.getByRole("button", { name: /confirm decision/i }));

    // The token is single-use: the review step must not stay clickable with a
    // token the server already consumed.
    expect((await screen.findAllByText(/nothing was decided/i)).length).toBeGreaterThan(0);
    expect(screen.getByRole("button", { name: /review impact/i })).toBeInTheDocument();
  });
});

describe("Control Case — a refused hand-off is not a silent success", () => {
  /**
   * MEASURED 2026-08-16. `_hand_off_to_data` records a refusal from Data as
   * `owner_outcome = 'failed'` and returns 200 — raising would roll the
   * confirmation back and erase the trace that the owner was ever asked. But the
   * `confirm` response carried `result_version_id` and nothing else, so this
   * dialog closed on "Decision recorded" whether Data had published OR refused,
   * and pointed at a history tab. Someone who had just taken an APPEND-ONLY act
   * had to go read a table to learn that it had failed.
   *
   * AND THE INSTRUMENT MEASURED ITS OWN COPY, until 2026-08-31. Every stub below
   * used to hand the dialog `{ message: … }` and `owner_outcome: "applied"` —
   * shapes the server could not produce. `controls_owner_commands.py` stored
   * `{"refusal": <code>}` with NO message, so the "quotes Data's own refusal"
   * test passed while every real refusal rendered *"gave no reason"*; and
   * `"applied"` is not in `control_cases.OWNER_OUTCOMES`, so the success path it
   * claimed to cover ended in a 422. The two server shapes are now written once,
   * below, in the words the server writes them.
   */

  /** What `_hand_off_to_data` stores on a refusal: the routing code AND the
   *  owner's sentence. Both keys, always — a stub that dropped either one would
   *  be back to proving the dialog against a payload nobody sends. */
  const SERVER_REFUSAL = {
    refusal: "stale_versions",
    message: "The mapping version is no longer the published one.",
  };

  /** What it stores on a success: no `message`, the two public pointers. */
  const SERVER_PUBLISHED = {
    operation_id: "op_EXAMPLE",
    outcome: "succeeded",
    replayed: false,
    current_mapping_version_id: "dmv_EXAMPLE_new",
    prior_mapping_version_id: "dmv_EXAMPLE_old",
  };

  /** The three words `control_cases.OWNER_OUTCOMES` admits, and no fourth. */
  const OWNER_OUTCOMES = ["succeeded", "failed", "not_applicable"] as const;

  function stubWithOutcome(
    outcome: (typeof OWNER_OUTCOMES)[number],
    ownerResult?: Record<string, unknown>,
  ) {
    vi.stubGlobal(
      "fetch",
      stubFetch((url) => {
        if (url.endsWith("/prepare"))
          return { json: { id: "ccs_1", confirmation_token: "tok_ABC", impact: {} } };
        if (url.endsWith("/confirm"))
          return {
            json: {
              result_version_id: "ctcd_1",
              owner_outcome: outcome,
              ...(ownerResult ? { owner_result: ownerResult } : {}),
            },
          };
        return { status: 201, json: { id: "ccs_1" } };
      }),
    );
  }

  async function decide() {
    const user = await openAndFill();
    await user.click(screen.getByRole("button", { name: /review impact/i }));
    await screen.findByText(/review before deciding/i);
    await user.click(screen.getByRole("button", { name: /confirm decision/i }));
  }

  it("keeps the dialog open and quotes Data's own refusal", async () => {
    stubWithOutcome("failed", SERVER_REFUSAL);
    await decide();

    await screen.findByTestId("owner-handoff-failed");
    // The owner's sentence, whole: a paraphrase would lose the only actionable
    // thing on the screen.
    expect(
      screen.getByText(/the mapping version is no longer the published one/i),
    ).toBeInTheDocument();
    // And the decision is NOT presented as undone -- it is append-only.
    expect(screen.getByText(/decided — the data owner refused to publish/i)).toBeInTheDocument();
  });

  it("says so even when Data refused without a reason", async () => {
    // The one shape that still reaches the fallback: a decision recorded
    // `failed` with no owner result at all — an older row, or an owner that
    // refused before it had a sentence to give.
    stubWithOutcome("failed");
    await decide();

    await screen.findByTestId("owner-handoff-failed");
    expect(screen.getByText(/gave no reason/i)).toBeInTheDocument();
  });

  it("does not fall back to 'gave no reason' on the code alone", async () => {
    // A refusal stored with its code and its sentence must render the SENTENCE.
    // This is the assertion the old stub could never make: it fed a `message`
    // the server never wrote, so the fallback it was meant to exclude was the
    // one every real refusal took.
    stubWithOutcome("failed", SERVER_REFUSAL);
    await decide();

    await screen.findByTestId("owner-handoff-failed");
    expect(screen.queryByText(/gave no reason/i)).not.toBeInTheDocument();
  });

  it("offers nothing to confirm again — the token is spent and the act is done", async () => {
    stubWithOutcome("failed");
    await decide();

    await screen.findByTestId("owner-handoff-failed");
    expect(screen.queryByRole("button", { name: /confirm decision/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /review impact/i })).not.toBeInTheDocument();
    expect(screen.getByTestId("case-decision-dismiss")).toHaveTextContent("Close");
  });

  it("closes on a successful hand-off, which is the only case that may", async () => {
    stubWithOutcome("succeeded", SERVER_PUBLISHED);
    await decide();

    await waitFor(() =>
      expect(screen.queryByTestId("owner-handoff-failed")).not.toBeInTheDocument(),
    );
    expect(screen.queryByText(/review before deciding/i)).not.toBeInTheDocument();
  });
});
