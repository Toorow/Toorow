/**
 * Adopting a preset into a ladder, from the screen (2026-08-17).
 *
 * What these pin is not "a dialog renders". It is the four properties that make
 * the difference between a governed adoption and a click that publishes policy:
 *
 *   - the plan is READ before anything is written, and the Money Policy version
 *     it would compose under is shown, not implied;
 *   - one question at a time — the next appears only once the previous is
 *     answered;
 *   - "Not decided yet" is an ANSWER the question offers, with what it costs
 *     stated where it is chosen, never a default that slid through;
 *   - a refusal is rendered in the server's own words, because every one of them
 *     names the gesture that clears it.
 *
 * Contract: docs/product-architecture/governance.md, "A ladder is adopted where
 * it is read".
 */
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import LadderAdoptionDialog from "../governance/LadderAdoptionDialog";

function response(body: unknown, status = 200) {
  return Promise.resolve({
    ok: status < 400,
    status,
    headers: { get: () => "application/json" },
    json: () => Promise.resolve(body),
    text: () => Promise.resolve(JSON.stringify(body)),
  } as unknown as Response);
}

const PLAN = {
  schema_version: "rule-set-adoption.v1",
  rule_set: { id: "grs_1", label: "Tax and fee ladder", published_rule_count: 0 },
  proposal: {},
  money_policy: { version_id: "grsv_money_1", reporting_currency: "EUR" },
  decisions_required: [
    {
      kind: "posture",
      key: "rest_of_world_posture",
      question: "Every country this rule does not name — does it apply there?",
      why: "Rest of World is a known place outside the named set.",
      options: [
        { value: "include", label: "Yes, it applies everywhere else too" },
        { value: "exclude", label: "No, only in the countries it names" },
        {
          value: "unresolved",
          label: "Not decided yet",
          consequence: "The ladder states no complete headline total until this is answered.",
        },
      ],
    },
    {
      kind: "qualification",
      key: "qualification:0",
      question: "Does the platform pass this through on its invoice?",
      options: [
        { value: "confirmed", label: "Yes, this holds for this Project" },
        { value: "not_confirmed", label: "No, or not yet" },
      ],
    },
  ],
  resulting_rules: [{ rule_key: "fr_dst", category: "REGULATORY_TAX" }],
};

function mount() {
  return render(
    <LadderAdoptionDialog
      open
      projectId="p1"
      ruleSetId="grs_1"
      presetVersionId="tfp_1"
      presetLabel="France DST pass-through"
      onClose={() => {}}
    />,
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("adopting a preset through a Change Set", () => {
  it("shows the Money Policy version the arithmetic would be composed under", async () => {
    vi.stubGlobal("fetch", vi.fn(() => response(PLAN)));
    mount();
    // A ladder whose money policy is "the latest" has no defined arithmetic, so
    // the exact version is on the screen before anyone agrees to publish.
    expect(await screen.findByText(/grsv_money_1/)).toBeTruthy();
  });

  it("asks one question at a time", async () => {
    vi.stubGlobal("fetch", vi.fn(() => response(PLAN)));
    mount();

    await screen.findByText(/does it apply there\?/i);
    // The qualification is NOT on screen yet: a form that shows six questions at
    // once invites someone to pick the shape of the page over the answer.
    expect(screen.queryByText(/pass this through on its invoice/i)).toBeNull();

    fireEvent.click(screen.getByLabelText(/only in the countries it names/i));
    expect(await screen.findByText(/pass this through on its invoice/i)).toBeTruthy();
  });

  it("offers 'not decided yet' as an answer, with what it costs beside it", async () => {
    vi.stubGlobal("fetch", vi.fn(() => response(PLAN)));
    mount();
    await screen.findByText(/does it apply there\?/i);

    expect(screen.getByLabelText(/not decided yet/i)).toBeTruthy();
    // Stated where it is chosen, not in a note under the form that nobody reads
    // after deciding.
    expect(screen.getByText(/no complete headline total/i)).toBeTruthy();
  });

  it("cannot publish while one question is unanswered", async () => {
    vi.stubGlobal("fetch", vi.fn(() => response(PLAN)));
    mount();
    await screen.findByText(/does it apply there\?/i);

    const publish = screen.getByRole("button", { name: /Publish this version/i });
    expect(publish).toBeDisabled();

    fireEvent.click(screen.getByLabelText(/only in the countries it names/i));
    expect(publish).toBeDisabled();

    fireEvent.click(await screen.findByLabelText(/this holds for this Project/i));
    await waitFor(() => expect(publish).not.toBeDisabled());
  });

  it("posts the answers under a key stable across retries", async () => {
    const fetchMock = vi.fn((_url: string, init?: RequestInit) =>
      init?.method === "POST" ? response({ change_set: {}, replayed: false }, 201) : response(PLAN),
    );
    vi.stubGlobal("fetch", fetchMock);
    mount();
    await screen.findByText(/does it apply there\?/i);
    fireEvent.click(screen.getByLabelText(/only in the countries it names/i));
    fireEvent.click(await screen.findByLabelText(/this holds for this Project/i));
    fireEvent.click(screen.getByRole("button", { name: /Publish this version/i }));

    await waitFor(() => {
      const post = fetchMock.mock.calls.find(([, init]) => (init as RequestInit)?.method === "POST");
      expect(post).toBeTruthy();
      const init = post![1] as RequestInit;
      expect(JSON.parse(String(init.body))).toEqual({
        preset_version_id: "tfp_1",
        answers: { rest_of_world_posture: "exclude", "qualification:0": "confirmed" },
      });
      // A double click, or a lost response, replays the same change set instead
      // of minting a rival version of the ladder.
      expect((init.headers as Record<string, string>)["Idempotency-Key"]).toBe(
        "adopt-grs_1-tfp_1",
      );
    });
  });

  it("renders a refusal in the server's own words", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() =>
        response(
          {
            code: "money_policy_not_confirmed",
            message:
              "This Project has no confirmed Money Policy. Confirm the Currency & FX policy in Controls & Quality, then adopt.",
          },
          422,
        ),
      ),
    );
    mount();

    // Every refusal names the gesture that clears it. Replacing it with a
    // generic sentence throws away the only useful part.
    expect(await screen.findByTestId("adoption-refused")).toBeTruthy();
    expect(screen.getByText(/Confirm the Currency & FX policy/i)).toBeTruthy();
    expect(screen.getByRole("button", { name: /Publish this version/i })).toBeDisabled();
  });
});
