/**
 * Story 75-2 — the review queue of the promotion rail, and the four states it
 * has to keep apart.
 *
 * An empty queue SAYS WHY and names the gesture that fills it (CLAUDE.md,
 * "L'écran"); a machine's proposal is never rendered as a person's; an
 * acceptance names the change-set it PREPARED and what that change-set still
 * refuses, because a promotion is not a publication; and a second verdict on a
 * proposal somebody else already decided is a CONFLICT, not an absence — the
 * row leaves and the reason is said, instead of leaving a row whose buttons do
 * nothing.
 */
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import CalculatedFieldProposalsQueue from "../governance/CalculatedFieldProposalsQueue";
import { expressionInWords } from "../governance/calculatedFieldProposalsClient";

const PROJECT = "proj_EXAMPLE";
const CHANGE_SET = "scs_01EXAMPLE0000000000000000";
const SPEND = { concept_id: "sc_spend_EXAMPLE", version_id: "scv_spend_EXAMPLE", name: "spend" };
const CLICKS = { concept_id: "sc_clicks_EXAMPLE", version_id: "scv_clicks_EXAMPLE", name: "clicks" };

function proposal(overrides: Record<string, unknown> = {}) {
  return {
    id: "cfp_01HUMAN00000000000000000",
    org_id: "org_EXAMPLE",
    project_id: PROJECT,
    status: "open",
    origin: "human",
    name: "cost_per_click",
    description: "Spend divided by clicks.",
    expression: {
      op: "ratio",
      numerator: { op: "concept_ref", concept_id: SPEND.concept_id, version_id: SPEND.version_id },
      denominator: { op: "concept_ref", concept_id: CLICKS.concept_id, version_id: CLICKS.version_id },
      zero_denominator: "null",
      as_percent: false,
    },
    value_type: "ratio",
    unit: null,
    currency: null,
    dependencies: [SPEND, CLICKS],
    provenance: { origin: "exploration", result_id: "qr_01EXAMPLE0000000000000000" },
    requested_by: "owner@example.com",
    created_at: "2026-09-05T09:00:00Z",
    resolved_by: null,
    resolved_at: null,
    applied_ref: null,
    ...overrides,
  };
}

interface Resolved {
  url: string;
  body: Record<string, unknown>;
}

function stubFetch(options: {
  proposals?: unknown[];
  listFails?: boolean;
  resolveConflict?: boolean;
  appliedRef?: string | null;
  prepareRefusals?: Array<{ code: string; message: string }>;
}) {
  const resolved: Resolved[] = [];
  const fetchMock = vi.fn().mockImplementation((url: string, init?: RequestInit) => {
    const address = String(url);
    if (address.includes("/resolve")) {
      resolved.push({ url: address, body: JSON.parse(String(init?.body ?? "{}")) });
      if (options.resolveConflict) {
        return Promise.resolve({
          ok: false,
          status: 409,
          json: async () => ({
            code: "proposal_already_resolved",
            message: "Proposal 'cfp_…' has already been resolved (current status: 'accepted').",
          }),
        });
      }
      const body = JSON.parse(String(init?.body ?? "{}")) as { status: string };
      return Promise.resolve({
        ok: true,
        status: 200,
        json: async () => ({
          proposal: proposal({
            status: body.status,
            resolved_by: "reviewer@example.com",
            resolved_at: "2026-09-05T10:00:00Z",
            applied_ref: body.status === "accepted"
              ? (options.appliedRef === undefined ? CHANGE_SET : options.appliedRef)
              : null,
            prepare_refusals: body.status === "accepted"
              ? (options.prepareRefusals ?? [
                { code: "undeclared_aggregation", message: "Declare how this metric aggregates." },
              ])
              : [],
          }),
        }),
      });
    }
    if (address.includes("/calculated-field-proposals")) {
      if (options.listFails) {
        return Promise.resolve({
          ok: false,
          status: 503,
          json: async () => ({ code: "db_error", message: "The promotion queue is unavailable." }),
        });
      }
      const proposals = options.proposals ?? [];
      return Promise.resolve({
        ok: true,
        status: 200,
        json: async () => ({
          project_id: PROJECT,
          status: "open",
          proposals,
          proposals_total: proposals.length,
        }),
      });
    }
    return Promise.resolve({ ok: true, status: 200, json: async () => ({}) });
  });
  vi.stubGlobal("fetch", fetchMock);
  return resolved;
}

afterEach(() => vi.unstubAllGlobals());

test("an empty queue says why it is empty and names the gesture that fills it", async () => {
  stubFetch({ proposals: [] });
  render(<CalculatedFieldProposalsQueue projectId={PROJECT} />);

  const empty = await screen.findByText("Nothing is waiting to be decided");
  expect(empty).toBeInTheDocument();
  const description = screen.getByText(/Propose as governed field/);
  expect(description.textContent).toContain("open a Result in Analyze");
});

test("a queue that could not be read names the gesture, not the code", async () => {
  stubFetch({ listFails: true });
  render(<CalculatedFieldProposalsQueue projectId={PROJECT} />);

  const banner = await screen.findByTestId("calculated-field-proposals-error");
  const said = banner.textContent ?? "";
  // The GESTURE, not the state: "The promotion queue is unavailable." is what
  // the server wrote and it tells a person nothing to do (CLAUDE.md, L'écran).
  expect(said).toContain("Try again in a moment");
  expect(said).not.toContain("db_error");
  // And it is still not an empty queue.
  expect(said).toContain("An empty list here would read as");
  expect(screen.queryByText("Nothing is waiting to be decided")).toBeNull();
});

test("a transport that never answered an envelope still names a gesture", async () => {
  // `apiFetch` calls this one `unavailable`: no `{code, message}` came back at
  // all, so there is no server sentence to fall back to.
  vi.stubGlobal(
    "fetch",
    vi.fn().mockResolvedValue({ ok: false, status: 502, json: async () => "<html>502</html>" }),
  );
  render(<CalculatedFieldProposalsQueue projectId={PROJECT} />);

  const banner = await screen.findByTestId("calculated-field-proposals-error");
  const said = banner.textContent ?? "";
  expect(said).toContain("Reload the page and try again");
  expect(said).not.toContain("HTTP 502");
});

test("lists both origins, named, and renders the formula in words", async () => {
  stubFetch({
    proposals: [
      proposal(),
      proposal({ id: "cfp_01AGENT000000000000000000", origin: "agent", name: "roas", requested_by: "agent@example.com" }),
    ],
  });
  render(<CalculatedFieldProposalsQueue projectId={PROJECT} />);

  await screen.findByTestId("calculated-field-proposal-cfp_01HUMAN00000000000000000");
  expect(
    screen.getByTestId("calculated-field-proposal-origin-cfp_01HUMAN00000000000000000").textContent,
  ).toBe("Human");
  expect(
    screen.getByTestId("calculated-field-proposal-origin-cfp_01AGENT000000000000000000").textContent,
  ).toBe("Agent");
  expect(
    screen.getByTestId("calculated-field-proposal-words-cfp_01HUMAN00000000000000000").textContent,
  ).toBe("spend (pinned) ÷ clicks (pinned)");
});

test("accepting names the prepared change-set and what it still refuses", async () => {
  const resolved = stubFetch({ proposals: [proposal()] });
  render(<CalculatedFieldProposalsQueue projectId={PROJECT} />);

  await screen.findByTestId("calculated-field-proposal-cfp_01HUMAN00000000000000000");
  await userEvent.click(
    screen.getByTestId("calculated-field-proposal-accept-cfp_01HUMAN00000000000000000"),
  );

  await waitFor(() =>
    expect(screen.getByTestId("calculated-field-proposals-prepared")).toBeInTheDocument(),
  );
  expect(resolved).toHaveLength(1);
  expect(resolved[0].body).toEqual({ status: "accepted" });
  const banner = screen.getByTestId("calculated-field-proposals-prepared").textContent ?? "";
  expect(banner).toContain(CHANGE_SET);
  expect(banner).toContain("prepared, never");
  expect(
    screen.getByTestId("calculated-field-proposals-prepared-next").textContent,
  ).toContain("How this field aggregates is still to be declared");
  // WHAT THE CHANGE-SET STILL REFUSES IS SAID IN WORDS. The banner used to
  // print `undeclared_aggregation` on its own — a token of the server contract,
  // which names no gesture and appears nowhere else in this console.
  const refusal = screen.getByTestId(
    "calculated-field-proposals-prepared-refusal-undeclared_aggregation",
  );
  expect(refusal.textContent).toContain("Declare how this field aggregates");
  expect(refusal.textContent).toContain("before the change-set can be confirmed");
  // The queue is WHAT IS LEFT TO DECIDE: a decided proposal leaves it.
  expect(screen.queryByTestId("calculated-field-proposal-cfp_01HUMAN00000000000000000")).toBeNull();
});

test("declining removes the row and prepares nothing", async () => {
  const resolved = stubFetch({ proposals: [proposal()] });
  render(<CalculatedFieldProposalsQueue projectId={PROJECT} />);

  await screen.findByTestId("calculated-field-proposal-cfp_01HUMAN00000000000000000");
  await userEvent.click(
    screen.getByTestId("calculated-field-proposal-decline-cfp_01HUMAN00000000000000000"),
  );

  await waitFor(() =>
    expect(screen.queryByTestId("calculated-field-proposal-cfp_01HUMAN00000000000000000")).toBeNull(),
  );
  expect(resolved[0].body).toEqual({ status: "declined" });
  expect(screen.queryByTestId("calculated-field-proposals-prepared")).toBeNull();
  expect(await screen.findByText("Nothing is waiting to be decided")).toBeInTheDocument();
});

test("a proposal somebody else already decided is a conflict, said and cleared", async () => {
  stubFetch({ proposals: [proposal()], resolveConflict: true });
  render(<CalculatedFieldProposalsQueue projectId={PROJECT} />);

  await screen.findByTestId("calculated-field-proposal-cfp_01HUMAN00000000000000000");
  await userEvent.click(
    screen.getByTestId("calculated-field-proposal-accept-cfp_01HUMAN00000000000000000"),
  );

  const banner = await screen.findByTestId("calculated-field-proposals-failure");
  expect(banner.textContent).toContain("was already decided by someone else");
  expect(screen.queryByTestId("calculated-field-proposal-cfp_01HUMAN00000000000000000")).toBeNull();
});

test("a verdict the server refused names the gesture, never the code", async () => {
  // 500 `db_error` on the resolve door: the row is still open, and the banner
  // has to say what to do about it.
  vi.stubGlobal(
    "fetch",
    vi.fn().mockImplementation((url: string) => {
      if (String(url).includes("/resolve")) {
        return Promise.resolve({
          ok: false,
          status: 500,
          json: async () => ({
            code: "db_error",
            message: "The promotion queue is unavailable.",
          }),
        });
      }
      return Promise.resolve({
        ok: true,
        status: 200,
        json: async () => ({
          project_id: PROJECT,
          status: "open",
          proposals: [proposal()],
          proposals_total: 1,
        }),
      });
    }),
  );
  render(<CalculatedFieldProposalsQueue projectId={PROJECT} />);

  await screen.findByTestId("calculated-field-proposal-cfp_01HUMAN00000000000000000");
  await userEvent.click(
    screen.getByTestId("calculated-field-proposal-accept-cfp_01HUMAN00000000000000000"),
  );

  const banner = await screen.findByTestId("calculated-field-proposals-failure");
  expect(banner.textContent).toContain("Try again in a moment");
  expect(banner.textContent).not.toContain("db_error");
});

test("a refusal this console has no gesture for falls back to the server's sentence", async () => {
  // NEVER the bare code: an unknown code still has the sentence the server
  // wrote, which is written for a person too.
  stubFetch({
    proposals: [proposal()],
    prepareRefusals: [
      { code: "some_future_refusal", message: "Name the grain this field is valid at." },
    ],
  });
  render(<CalculatedFieldProposalsQueue projectId={PROJECT} />);

  await screen.findByTestId("calculated-field-proposal-cfp_01HUMAN00000000000000000");
  await userEvent.click(
    screen.getByTestId("calculated-field-proposal-accept-cfp_01HUMAN00000000000000000"),
  );

  const item = await screen.findByTestId(
    "calculated-field-proposals-prepared-refusal-some_future_refusal",
  );
  expect(item.textContent).toContain("Name the grain this field is valid at.");
});

/**
 * The formula in WORDS — every operation of the governed contract.
 *
 * A queue row that renders `conditional` or `comparison` as its own token asks
 * the person deciding to know the contract's vocabulary before they can decide.
 * `ALLOWED_OPERATIONS` (`server/core/semantic_expressions.py`) is the list this
 * must cover.
 */
describe("expressionInWords", () => {
  const named = (conceptId: string) =>
    conceptId === SPEND.concept_id ? "spend" : conceptId === CLICKS.concept_id ? "clicks" : null;
  const say = (node: unknown) => expressionInWords(node, (conceptId) => named(conceptId));
  const spend = { op: "concept_ref", concept_id: SPEND.concept_id, version_id: SPEND.version_id };
  const clicks = { op: "concept_ref", concept_id: CLICKS.concept_id, version_id: CLICKS.version_id };

  test("a comparison reads as a sentence, and its operator is a word", () => {
    expect(say({ op: "comparison", operator: "gte", left: spend, right: { op: "literal", value: 100 } }))
      .toBe("spend is at least 100");
    expect(say({ op: "comparison", operator: "ne", left: spend, right: clicks }))
      .toBe("spend is not clicks");
    // A token the contract does not carry is said to be unreadable rather than
    // printed as itself.
    expect(say({ op: "comparison", operator: "approximately", left: spend, right: clicks }))
      .toBe("an unreadable comparison");
  });

  test("a conditional says every branch AND its otherwise", () => {
    const said = say({
      op: "conditional",
      when: [
        {
          condition: { op: "comparison", operator: "gt", left: clicks, right: { op: "literal", value: 0 } },
          then: { op: "ratio", numerator: spend, denominator: clicks, zero_denominator: "null" },
        },
      ],
      otherwise: { op: "literal", value: 0 },
    });
    expect(said).toBe("spend ÷ clicks when clicks is above 0; otherwise 0");
    // The default branch is never silent: an implicit blank is the exact thing
    // the server refuses as `undeclared_default_branch`.
    expect(said).toContain("otherwise");
  });

  test("a reference carried by name says it is not pinned", () => {
    expect(say({ op: "concept_name", name: "margin" })).toBe("margin (not pinned to a version)");
  });
});
