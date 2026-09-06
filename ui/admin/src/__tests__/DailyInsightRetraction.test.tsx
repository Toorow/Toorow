/**
 * Withdrawing a published daily insight, from the screen it is shown on.
 *
 * `proactive-assertions.md` decision 4, and its `Incomplete if`: "a retraction is
 * delivered as a delete rather than an audited state transition". Migration 321
 * makes the withdrawal three columns that move together and a trigger that never
 * unmakes them; this file is the console half of the same rule:
 *
 *   - a withdrawn insight is STILL THERE, marked withdrawn, with who / when / why.
 *     Hiding it would be the delete the decision refuses, one layer up;
 *   - the gesture appears only where the SERVER said this caller may make it
 *     (`canRetract`), so the screen never draws a control that answers 404;
 *   - a reason is required before anything is sent, because a withdrawal nobody
 *     can judge later is the same object as an anonymous erasure;
 *   - the door is a POST to `/retract`. NO request this component can make is a
 *     DELETE, and that is asserted rather than described.
 */

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";

import {
  RETRACT_LABEL,
  RETRACT_REASON_REQUIRED,
  RunInsights,
  retractionSentence,
} from "../daily-insights/InsightShare";

afterEach(() => vi.restoreAllMocks());

function response(body: unknown, status = 200) {
  return Promise.resolve({
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
    text: async () => JSON.stringify(body),
  });
}

const STANDING = {
  id: "din_STANDING",
  slot: 0,
  payload: { insight: { title: "Spend spiked on Search" } },
  result_id: null,
  result_unavailable_reason: null,
};

const WITHDRAWN = {
  id: "din_WITHDRAWN",
  slot: 1,
  payload: { insight: { title: "Revenue collapsed" } },
  result_id: null,
  result_unavailable_reason: null,
  retracted_at: "2026-08-30T08:00:00Z",
  retracted_by: "owner@example.com",
  retracted_reason: "read on the wrong window",
};

function stub(body: unknown, status = 200) {
  const fetchMock = vi.fn(() => response(body, status));
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

async function openTheDay() {
  fireEvent.click(await screen.findByTestId("daily-insight-open-2026-08-29"));
  return screen.findByTestId("daily-insight-items-2026-08-29");
}

// ---------------------------------------------------------------------------
// A withdrawal is displayed, never swallowed
// ---------------------------------------------------------------------------

it("shows a withdrawn insight as withdrawn instead of dropping it", async () => {
  stub({ insights: [WITHDRAWN], canRetract: true });
  render(<RunInsights projectId="p1" insightDate="2026-08-29" />);

  await openTheDay();
  const row = screen.getByTestId("insight-retracted-din_WITHDRAWN");
  expect(row).toHaveTextContent("Withdrawn");
  expect(row).toHaveTextContent("owner@example.com");
  expect(row).toHaveTextContent("2026-08-30");
  expect(row).toHaveTextContent("read on the wrong window");
  // The claim itself is still readable: the row is the evidence it was made.
  expect(screen.getByTestId("insight-share-din_WITHDRAWN")).toHaveTextContent(
    "Revenue collapsed",
  );
  // And it offers no second withdrawal -- that door answers 409.
  expect(screen.queryByTestId("insight-retract-din_WITHDRAWN")).toBeNull();
});

it("keeps who, when and why together, or says nothing", () => {
  expect(retractionSentence(STANDING)).toBeNull();
  expect(retractionSentence(WITHDRAWN)).toBe(
    "Withdrawn by owner@example.com on 2026-08-30: read on the wrong window",
  );
});

// ---------------------------------------------------------------------------
// The gesture, and the permission it waits on
// ---------------------------------------------------------------------------

it("draws no withdrawal control where the server did not grant it", async () => {
  stub({ insights: [STANDING], canRetract: false });
  render(<RunInsights projectId="p1" insightDate="2026-08-29" />);

  await openTheDay();
  expect(screen.queryByTestId("insight-retract-din_STANDING")).toBeNull();
});

it("refuses to send a withdrawal with no reason, and says why the reason is needed", async () => {
  const fetchMock = stub({ insights: [STANDING], canRetract: true });
  render(<RunInsights projectId="p1" insightDate="2026-08-29" />);

  await openTheDay();
  fireEvent.click(screen.getByTestId("insight-retract-din_STANDING"));
  fireEvent.click(screen.getByTestId("insight-retract-confirm-din_STANDING"));

  expect(screen.getByTestId("insight-retract-refused-din_STANDING")).toHaveTextContent(
    RETRACT_REASON_REQUIRED,
  );
  // Nothing left the browser.
  expect(fetchMock.mock.calls).toHaveLength(1);
});

it("withdraws through a POST to /retract, and the row becomes the withdrawal", async () => {
  const fetchMock = vi.fn((url: string, _init?: RequestInit) =>
    String(url).includes("/retract")
      ? response({ ...STANDING, ...WITHDRAWN, id: STANDING.id })
      : response({ insights: [STANDING], canRetract: true }),
  );
  vi.stubGlobal("fetch", fetchMock);
  render(<RunInsights projectId="p1" insightDate="2026-08-29" />);

  await openTheDay();
  fireEvent.click(screen.getByTestId("insight-retract-din_STANDING"));
  fireEvent.change(screen.getByTestId("insight-retract-reason-din_STANDING"), {
    target: { value: "read on the wrong window" },
  });
  fireEvent.click(screen.getByTestId("insight-retract-confirm-din_STANDING"));

  await waitFor(() => expect(screen.getByTestId("insight-retracted-din_STANDING")).toBeTruthy());

  const calls = fetchMock.mock.calls.map(([url, init]) => ({
    url: String(url),
    method: String((init as RequestInit | undefined)?.method ?? "GET").toUpperCase(),
    body: String((init as RequestInit | undefined)?.body ?? ""),
  }));
  const write = calls.find((call) => call.url.includes("/retract"));
  expect(write?.method).toBe("POST");
  expect(write?.url).toContain("/api/daily-insights/insights/din_STANDING/retract");
  expect(write?.url).toContain("project_id=p1");
  expect(JSON.parse(write?.body ?? "{}").reason).toBe("read on the wrong window");
  // THE CLAUSE: nothing this surface can do is a delete.
  expect(calls.every((call) => call.method !== "DELETE")).toBe(true);
  // The gesture is gone once the claim is withdrawn; the withdrawal is not.
  expect(screen.queryByRole("button", { name: RETRACT_LABEL })).toBeNull();
});

it("a refused withdrawal leaves the insight standing and says what the server said", async () => {
  const fetchMock = vi.fn((url: string, _init?: RequestInit) =>
    String(url).includes("/retract")
      ? response({ code: "already_retracted", message: "This insight was already retracted." }, 409)
      : response({ insights: [STANDING], canRetract: true }),
  );
  vi.stubGlobal("fetch", fetchMock);
  render(<RunInsights projectId="p1" insightDate="2026-08-29" />);

  await openTheDay();
  fireEvent.click(screen.getByTestId("insight-retract-din_STANDING"));
  fireEvent.change(screen.getByTestId("insight-retract-reason-din_STANDING"), {
    target: { value: "wrong window" },
  });
  fireEvent.click(screen.getByTestId("insight-retract-confirm-din_STANDING"));

  await waitFor(() =>
    expect(screen.getByTestId("insight-retract-refused-din_STANDING")).toBeTruthy(),
  );
  expect(screen.queryByTestId("insight-retracted-din_STANDING")).toBeNull();
});
