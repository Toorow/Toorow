/**
 * AI-294, last remnant — from a published daily insight to a share.
 *
 * WHAT THE SERVER ALREADY DOES (commit 790db3b3, migration 281, deployed):
 * publishing derives a governed Query Spec from the card contract and executes
 * it, so every insight row carries `query_spec_version_id` + `result_id`, OR a
 * `result_unavailable_reason` naming the missing link — never both. Nothing in
 * `ui/` read those columns, so the person had no way from an insight to a share.
 *
 * WHAT IS ASSERTED HERE is the clause `proactive-assertions.md` writes as a
 * DEFECT: "a share of an insight is created by any door other than the Render
 * Share mechanism". So the tests check the three states of a row AND, above all,
 * that this surface opens the existing door rather than a new one — no POST, no
 * share route, no render route, and the destination resolves through the
 * console's own navigation registry (`resolveOwnerReference`), which is what
 * makes it the existing path and not a URL this screen invented.
 */

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";

import {
  fetchRunInsights,
  insightShareState,
  NOT_SHAREABLE_GESTURE,
  OPEN_RESULT_LABEL,
  UNRECORDED_SENTENCE,
} from "../daily-insights/InsightShare";
import { resolveOwnerReference } from "../shell/ownerResolution";
import ProjectSettings from "../shell/pages/ProjectSettings";

afterEach(() => vi.restoreAllMocks());

function response(body: unknown, status = 200) {
  return Promise.resolve({
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
    text: async () => JSON.stringify(body),
  });
}

function ownerRef(workspace: string, section: string) {
  return {
    surface: "project",
    workspace,
    section,
    global_surface: null,
    global_section: null,
    object_type: null,
    object_id: null,
    tab: null,
    action: null,
    version_id: null,
    evidence_id: null,
  };
}

const ENVELOPE = {
  project: {
    id: "p1",
    name: "Acme",
    description: "Governed analytics",
    organization: { id: "o1", name: "Acme Group" },
    can_edit: true,
    business_domains: [],
    external_sharing: {
      state: "forbidden",
      decided_by: null,
      decided_at: null,
      is_platform_default: true,
      can_change: true,
    },
    defaults: {
      reporting_currency: { active: "EUR", pending: null, origin: "unset", confirmation_status: "confirmed", owner_reference: ownerRef("governance", "semantic-model") },
      reporting_timezone: { active: "Europe/Paris", pending: null, origin: "unset", confirmation_status: "confirmed", owner_reference: ownerRef("governance", "controls-quality") },
      verification_source: { active: null, pending: null, origin: "unset", confirmation_status: "unconfirmed", owner_reference: ownerRef("governance", "controls-quality") },
    },
  },
  capabilities: [],
  changes: [],
};

const RECIPE = { recipe: { recipeVersion: "task-recipe.v1" }, text: "Every day at 07:00..." };

const RUNS = {
  runs: [
    { state: "published", insightDate: "2026-08-16", itemCount: 3, retractedCount: 1 },
  ],
};

/** The reason is the SERVER's sentence, stored on the row. It already names the
 *  missing link and the gesture that removes it; the console renders it rather
 *  than translating a code it was never given. */
const REFUSED_REASON =
  "These card metrics are not governed semantic concepts of this project: " +
  "blended_roas. Declare them in the Semantic Model, then publish again.";

const INSIGHTS = {
  run: { state: "published", insightDate: "2026-08-16", itemCount: 3 },
  insights: [
    {
      id: "din_SHAREABLE",
      slot: 0,
      payload: { insight: { title: "Spend concentrated on two campaigns" } },
      query_spec_version_id: "qsv_EXAMPLE",
      result_id: "res_EXAMPLE",
      result_unavailable_reason: null,
    },
    {
      id: "din_REFUSED",
      slot: 1,
      payload: { insight: { title: "Blended ROAS slipped" } },
      query_spec_version_id: null,
      result_id: null,
      result_unavailable_reason: REFUSED_REASON,
    },
    {
      id: "din_OLD",
      slot: 2,
      payload: { insight: { title: "Impressions steady week over week" } },
      query_spec_version_id: null,
      result_id: null,
      result_unavailable_reason: null,
    },
  ],
};

function stubConsole(insights: unknown = INSIGHTS, insightsStatus = 200) {
  const fetchMock = vi.fn((url: string, _init?: RequestInit) => {
    const u = String(url);
    if (u.includes("/api/daily-insights/recipe")) return response(RECIPE);
    if (u.includes("/api/daily-insights/runs/")) return response(insights, insightsStatus);
    if (u.includes("/api/daily-insights/runs")) return response(RUNS);
    return response(ENVELOPE);
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

async function openTheDay() {
  const opener = await screen.findByTestId("daily-insight-open-2026-08-16");
  fireEvent.click(opener);
  return screen.findByTestId("daily-insight-items-2026-08-16");
}

// ---------------------------------------------------------------------------
// State 1 -- the insight names a Result: the ONE door opens, at that Result.
// ---------------------------------------------------------------------------

it("opens the existing Share path at the insight's Result, and mints nothing here", async () => {
  const fetchMock = stubConsole();
  const onOpenOwner = vi.fn();
  render(<ProjectSettings projectId="p1" section="general" onOpenOwner={onOpenOwner} />);

  const list = await openTheDay();
  expect(list).toHaveTextContent("Spend concentrated on two campaigns");

  fireEvent.click(screen.getByTestId("insight-open-result-din_SHAREABLE"));

  // A SEMANTIC reference, not a path this screen assembled. The shell resolves
  // it; nothing here learns that URLs exist.
  expect(onOpenOwner).toHaveBeenCalledWith(
    expect.objectContaining({
      surface: "project",
      workspace: "analyze",
      section: "explore",
      object_type: "result",
      object_id: "res_EXAMPLE",
      tab: "view",
    }),
  );

  // And the reference is one the console's OWN registry resolves. A destination
  // this screen invented would be refused here, which is what makes the door
  // "the existing one" rather than a claim in a comment.
  const resolved = resolveOwnerReference(onOpenOwner.mock.calls[0][0]);
  expect(resolved.kind).toBe("workspace");
  if (resolved.kind === "workspace") {
    expect(resolved.workspace).toBe("analyze");
    expect(resolved.section).toBe("explore");
    expect(resolved.objectType).toBe("result");
    expect(resolved.objectId).toBe("res_EXAMPLE");
  }

  // THE CLAUSE ITSELF: no second Share door. `proactive-assertions.md` calls a
  // share created by any other door a defect, so this surface writes nothing at
  // all -- no share route, no render route, and no POST of any kind.
  const calls = fetchMock.mock.calls.map(([url, init]) => ({
    url: String(url),
    method: String((init as RequestInit | undefined)?.method ?? "GET").toUpperCase(),
  }));
  expect(calls.every((call) => call.method === "GET")).toBe(true);
  expect(calls.some((call) => call.url.includes("/shares"))).toBe(false);
  expect(calls.some((call) => call.url.includes("/renders"))).toBe(false);
  // What it DID read is the day the server serves, through the seam.
  expect(calls.some((call) => call.url.startsWith("/api/daily-insights/runs/2026-08-16?project_id=p1"))).toBe(true);
});

// ---------------------------------------------------------------------------
// State 2 -- the insight names the missing link: said, never a dead control.
// ---------------------------------------------------------------------------

it("names why an insight is not shareable, and offers no control that would fail", async () => {
  stubConsole();
  render(<ProjectSettings projectId="p1" section="general" onOpenOwner={vi.fn()} />);

  await openTheDay();
  const refused = screen.getByTestId("insight-not-shareable-din_REFUSED");
  expect(refused).toHaveTextContent("Not shareable");
  expect(refused).toHaveTextContent(/not governed semantic concepts/i);
  expect(refused).toHaveTextContent(/Declare them in the Semantic Model/i);
  // The one thing the server cannot say: publication did not wait for this.
  expect(refused).toHaveTextContent(/Publishing never waited for this/i);

  // No door on this row. A Share button that refused on click would teach the
  // reader that the control lies, and the reason is already on screen.
  expect(screen.queryByTestId("insight-open-result-din_REFUSED")).toBeNull();
  expect(screen.getAllByRole("button", { name: OPEN_RESULT_LABEL })).toHaveLength(1);
});

// ---------------------------------------------------------------------------
// State 3 -- older than migration 281: nothing invented, in either direction.
// ---------------------------------------------------------------------------

it("says nothing it does not know about an insight published before the lineage existed", async () => {
  stubConsole();
  render(<ProjectSettings projectId="p1" section="general" onOpenOwner={vi.fn()} />);

  await openTheDay();
  const old = screen.getByTestId("insight-share-unrecorded-din_OLD");
  expect(old).toHaveTextContent(UNRECORDED_SENTENCE);

  // Not a refusal (the row names none) and not an offer (it names no Result).
  expect(screen.queryByTestId("insight-not-shareable-din_OLD")).toBeNull();
  expect(screen.queryByTestId("insight-open-result-din_OLD")).toBeNull();
});

// ---------------------------------------------------------------------------
// The pure classifier, probed by collapsing the states it must keep apart.
// ---------------------------------------------------------------------------

it("keeps the three states apart", () => {
  expect(insightShareState({ id: "a", result_id: "res_1" })).toEqual({
    kind: "shareable",
    resultId: "res_1",
    sentence: expect.stringContaining("frozen view"),
  });
  expect(insightShareState({ id: "b", result_unavailable_reason: "no view proves it" })).toEqual({
    kind: "not_shareable",
    reason: "no view proves it",
    gesture: NOT_SHAREABLE_GESTURE,
  });
  // A blank column is an ABSENCE, and an absence is not a refusal.
  expect(insightShareState({ id: "c", result_id: "", result_unavailable_reason: "  " }).kind).toBe(
    "unrecorded",
  );
});

// ---------------------------------------------------------------------------
// A day that could not be read is not a day that published nothing.
// ---------------------------------------------------------------------------

it("a day that failed to load is not an empty one", async () => {
  stubConsole({ code: "db_error", message: "Erreur base de donnees" }, 500);
  render(<ProjectSettings projectId="p1" section="general" onOpenOwner={vi.fn()} />);

  const opener = await screen.findByTestId("daily-insight-open-2026-08-16");
  fireEvent.click(opener);

  const failure = await screen.findByTestId("daily-insight-items-error-2026-08-16");
  expect(failure.textContent).toBeTruthy();
  expect(screen.queryByTestId("daily-insight-items-2026-08-16")).toBeNull();
});

it("refuses a 200 that carries no insights instead of reading it as an empty day", async () => {
  vi.stubGlobal("fetch", vi.fn(() => response({ run: {} })));
  await expect(fetchRunInsights("p1", "2026-08-16")).rejects.toThrow(/carries no insights/i);
});

// ---------------------------------------------------------------------------
// Nothing is read until the day is opened.
// ---------------------------------------------------------------------------

it("reads a day only when it is opened", async () => {
  const fetchMock = stubConsole();
  render(<ProjectSettings projectId="p1" section="general" onOpenOwner={vi.fn()} />);

  await screen.findByTestId("daily-insight-open-2026-08-16");
  await waitFor(() =>
    expect(
      fetchMock.mock.calls.some(([url]) => String(url).includes("/api/daily-insights/runs?")),
    ).toBe(true),
  );
  expect(
    fetchMock.mock.calls.some(([url]) => String(url).includes("/api/daily-insights/runs/")),
  ).toBe(false);
});

// ---------------------------------------------------------------------------
// `proactive-assertions.md` ("Incomplete if"): model-authored prose is not
// visibly distinguishable from cited server data.
//
// Story 53.4 derived `authorship.modelAuthored` and NOTHING drew it — four of the
// five prose fields reached no reader at all, and the fifth was drawn in the
// typography of a server-composed label. These two tests are the pair that keeps
// it honest: the marker on every one of the five, and its ABSENCE on the server's
// own words. Only the first would pass on a component that marks everything.
// ---------------------------------------------------------------------------

const FIVE_FIELDS = [
  "insight.title",
  "insight.summary",
  "insight.whyItMatters",
  "insight.recommendedAction",
  "insight.limitations",
];

const AUTHORED_INSIGHTS = {
  run: { state: "published", insightDate: "2026-08-16", itemCount: 1 },
  insights: [
    {
      id: "din_AUTHORED",
      slot: 0,
      payload: {
        insight: {
          title: "Spend concentrated on two campaigns",
          summary: "Two campaigns took most of the spend.",
          whyItMatters: "Concentration raises exposure to one auction.",
          recommendedAction: "Rebalance before the weekend.",
          limitations: ["One connector only."],
          confidence: "high",
        },
        authorship: {
          modelAuthored: FIVE_FIELDS,
          confidence: "derived",
          declaredConfidence: "high",
          derivedConfidence: {
            reading: "medium",
            terms: { completeness: 1, freshness: 0.7, provenance: 1 },
            limitingTerm: "freshness",
            unknownTerms: [],
            citedMembers: ["metric:spend"],
            unresolvedRefs: [],
          },
          evidenceRefs: ["metric:spend"],
        },
      },
      query_spec_version_id: "qsv_EXAMPLE",
      result_id: "res_EXAMPLE",
      result_unavailable_reason: null,
    },
  ],
};

it("marks every model-authored field, and only those", async () => {
  stubConsole(AUTHORED_INSIGHTS);
  render(<ProjectSettings projectId="p1" section="general" onOpenOwner={vi.fn()} />);

  const list = await openTheDay();
  const marked = [...list.querySelectorAll('[data-slot="model-authored"]')].map((node) =>
    node.getAttribute("data-model-authored"),
  );

  for (const field of FIVE_FIELDS) {
    expect(marked, `${field} is model prose and must carry the marker`).toContain(field);
  }
});

it("leaves the server's own words unmarked", async () => {
  stubConsole(AUTHORED_INSIGHTS);
  render(<ProjectSettings projectId="p1" section="general" onOpenOwner={vi.fn()} />);

  const list = await openTheDay();

  // The derived confidence is the SERVER's measurement. Marking it would say the
  // model wrote its own confidence, which is the very claim this lot removed.
  const derived = list.querySelector('[data-testid="insight-confidence-derived-din_AUTHORED"]');
  expect(derived?.textContent).toMatch(/medium — derived by the server/);
  expect(derived?.querySelector('[data-slot="model-authored"]')).toBeNull();

  // The share sentence is the console's own text, and stays bare.
  const row = list.querySelector('[data-testid="insight-share-din_AUTHORED"]');
  const shareSentence = [...(row?.querySelectorAll("p") ?? [])].find((node) =>
    node.textContent?.includes("A share link is created over a frozen view"),
  );
  expect(shareSentence).toBeTruthy();
  expect(shareSentence?.querySelector('[data-slot="model-authored"]')).toBeNull();

  // And the model's own confidence word IS marked -- it is the model's estimate,
  // reported beside the measurement it does not decide.
  const declared = list.querySelector('[data-testid="insight-confidence-declared-din_AUTHORED"]');
  expect(declared?.querySelector('[data-slot="model-authored"]')).not.toBeNull();
});

it("a day whose claim was withdrawn does not read like a day whose claim stands", async () => {
  // Review of ae60c22a, R2: the server counts retractions for exactly this row
  // (daily_insights_recipe.run_journal), and the journal dropped the count.
  stubConsole();
  render(<ProjectSettings projectId="p1" section="general" onOpenOwner={vi.fn()} />);

  const withdrawn = await screen.findByTestId("daily-insight-retracted-count-2026-08-16");
  expect(withdrawn.textContent).toBe("1 claim was withdrawn.");
});

it("more than one withdrawal is counted in the plural", async () => {
  stubConsole();
  vi.stubGlobal("fetch", vi.fn((u: string) => {
    if (u.includes("/api/daily-insights/runs/")) return response(INSIGHTS);
    if (u.includes("/api/daily-insights/runs"))
      return response({
        runs: [{ state: "published", insightDate: "2026-08-16", itemCount: 3, retractedCount: 2 }],
      });
    return response(ENVELOPE);
  }));
  render(<ProjectSettings projectId="p1" section="general" onOpenOwner={vi.fn()} />);

  const withdrawn = await screen.findByTestId("daily-insight-retracted-count-2026-08-16");
  expect(withdrawn.textContent).toBe("2 claims were withdrawn.");
});
