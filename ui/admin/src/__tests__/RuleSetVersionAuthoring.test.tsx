/**
 * Composing a Rule Set version, from the screen (2026-08-24).
 *
 * What these pin is not "a form renders". It is the properties that make the
 * difference between a governed composition and a settings page:
 *
 *   - the QUESTIONS come from the server, so this file states none of them and a
 *     family the console has never heard of renders correctly;
 *   - composing and putting in force are TWO gestures, and the first one says in
 *     so many words that nothing changed;
 *   - what a new version carries forward untouched is on the screen BEFORE the
 *     gesture, because a person changing a rounding needs to know the ladder
 *     survives it;
 *   - a family composed elsewhere names that gesture instead of offering a
 *     second editor for one fact;
 *   - a refusal is rendered in the server's own words: every one of them names
 *     the gesture that clears it.
 *
 * Contract: docs/product-architecture/governance.md, "A Rule Set version is
 * drafted, then published".
 */
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import RuleSetVersionAuthoring from "../governance/RuleSetVersionAuthoring";

function response(body: unknown, status = 200) {
  return Promise.resolve({
    ok: status < 400,
    status,
    headers: { get: () => "application/json" },
    json: () => Promise.resolve(body),
    text: () => Promise.resolve(JSON.stringify(body)),
  } as unknown as Response);
}

/** A family this file knows nothing about, on purpose. */
const FIRST_VERSION = {
  schema_version: "rule-set-authoring.v1",
  rule_set: {
    id: "grs_1",
    label: "Money policy",
    family: "money_policy",
    profile: "money_policy_v1",
    profile_label: "Money Policy",
  },
  in_force: null,
  draft: null,
  composed_by: null,
  fields: [
    {
      key: "reporting_currency",
      question: "Which currency does this Project report in?",
      kind: "text",
      options: [],
      why: "Every converted figure this Project states is stated in it.",
      required: true,
      unit: null,
      value: null,
    },
    {
      key: "rounding",
      question: "How is a converted amount rounded at the boundary?",
      kind: "choice",
      options: [
        {
          value: "half_even",
          label: "Half to even",
          consequence: "Rounding does not drift upward across a long series.",
        },
        { value: "half_up", label: "Half away from zero", consequence: "A small upward bias." },
      ],
      why: null,
      required: true,
      unit: null,
      value: null,
    },
  ],
  carries_forward: { ordered_rule_count: 0, pinned_references: [] },
};

const WITH_DRAFT = {
  ...FIRST_VERSION,
  in_force: { version_id: "grsv_1", version_number: 1, rule_count: 3 },
  draft: { version_id: "grsv_2", version_number: 2, label: "Money Policy" },
  fields: FIRST_VERSION.fields.map((field) => ({
    ...field,
    value: field.key === "reporting_currency" ? "EUR" : "half_even",
  })),
  carries_forward: {
    ordered_rule_count: 3,
    pinned_references: [
      { kind: "money_policy_version", object_id: "grs_m", version_id: "grsv_m1" },
    ],
  },
};

function mount(onChanged?: () => void) {
  return render(
    <RuleSetVersionAuthoring projectId="p1" ruleSetId="grs_1" onChanged={onChanged} />,
  );
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("composing a Rule Set version", () => {
  it("asks the questions the server declared, and none this file wrote", async () => {
    vi.stubGlobal("fetch", vi.fn(() => response(FIRST_VERSION)));
    mount();
    expect(
      await screen.findByText("Which currency does this Project report in?"),
    ).toBeInTheDocument();
  });

  it("states what an answer costs where the answer is chosen", async () => {
    // Not in a note under the form, which nobody reads after deciding. Read on a
    // policy already in force, where every question is visible at once.
    vi.stubGlobal("fetch", vi.fn(() => response(WITH_DRAFT)));
    mount();
    expect(
      await screen.findByText("Rounding does not drift upward across a long series."),
    ).toBeInTheDocument();
    expect(await screen.findByText("A small upward bias.")).toBeInTheDocument();
  });

  it("reveals the next question only once the previous is answered", async () => {
    vi.stubGlobal("fetch", vi.fn(() => response(FIRST_VERSION)));
    mount();
    const currency = await screen.findByLabelText(/Which currency does this Project report in/);
    // Composing the FIRST version: the second question is not on the screen yet.
    expect(
      screen.queryByText("How is a converted amount rounded at the boundary?"),
    ).not.toBeInTheDocument();
    fireEvent.change(currency, { target: { value: "EUR" } });
    expect(
      await screen.findByText("How is a converted amount rounded at the boundary?"),
    ).toBeInTheDocument();
  });

  it("says that nothing is in force yet, rather than implying a default", async () => {
    vi.stubGlobal("fetch", vi.fn(() => response(FIRST_VERSION)));
    mount();
    expect(
      await screen.findByText(/every read that needs one is refused rather than guessed/),
    ).toBeInTheDocument();
  });

  it("composes a draft and says it changed nothing in force", async () => {
    const fetchMock = vi.fn((_url: string, init?: RequestInit) =>
      init?.method === "POST"
        ? response({ version: { id: "grsv_9", status: "draft" } }, 201)
        : response(WITH_DRAFT),
    );
    vi.stubGlobal("fetch", fetchMock as unknown as typeof fetch);
    mount();

    fireEvent.click(await screen.findByRole("button", { name: /Compose this version/ }));
    await waitFor(() => {
      const posted = fetchMock.mock.calls.find(([, init]) => init?.method === "POST");
      expect(posted).toBeTruthy();
      // The answers travel as the ANSWERS the server asked for, keyed by the
      // payload key it declared. Nothing here invents a body shape.
      expect(JSON.parse(String((posted?.[1] as RequestInit).body))).toEqual({
        answers: { reporting_currency: "EUR", rounding: "half_even" },
      });
    });
  });

  it("keeps composing and putting in force as two gestures", async () => {
    vi.stubGlobal("fetch", vi.fn(() => response(WITH_DRAFT)));
    mount();
    // The draft is announced as NOT in force, and it names what still decides.
    expect(
      await screen.findByText(/Version 2 is composed and NOT in force/),
    ).toBeInTheDocument();
    expect(
      await screen.findByText(/this Project keeps computing with version 1/),
    ).toBeInTheDocument();
    expect(
      await screen.findByRole("button", { name: /Put this version in force/ }),
    ).toBeInTheDocument();
  });

  it("publishes the exact draft, at its own address, idempotently", async () => {
    const fetchMock = vi.fn((_url: string, init?: RequestInit) =>
      init?.method === "POST" ? response({ replayed: false }, 201) : response(WITH_DRAFT),
    );
    vi.stubGlobal("fetch", fetchMock as unknown as typeof fetch);
    mount();

    fireEvent.click(await screen.findByRole("button", { name: /Put this version in force/ }));
    await waitFor(() => {
      const posted = fetchMock.mock.calls.find(([, init]) => init?.method === "POST");
      expect(String(posted?.[0])).toContain("/versions/grsv_2/publication");
      // A double click replays the same change set rather than minting a rival.
      const headers = (posted?.[1] as RequestInit).headers as Record<string, string>;
      expect(headers["Idempotency-Key"]).toBe("publish-grs_1-grsv_2");
    });
  });

  it("states what the new version carries forward before it is composed", async () => {
    vi.stubGlobal("fetch", vi.fn(() => response(WITH_DRAFT)));
    mount();
    // A person about to change a rounding must know the ladder survives it.
    expect(await screen.findByText("3 published rules")).toBeInTheDocument();
    expect(await screen.findByText(/money_policy_version @ grsv_m1/)).toBeInTheDocument();
  });

  it("names the gesture that composes a family written elsewhere", async () => {
    vi.stubGlobal("fetch", vi.fn(() =>
      response({
        ...FIRST_VERSION,
        rule_set: { ...FIRST_VERSION.rule_set, family: "source_currency" },
        fields: [],
        composed_by: "A source currency is declared on the mapping screen.",
      }),
    ));
    mount();
    expect(
      await screen.findByText("A source currency is declared on the mapping screen."),
    ).toBeInTheDocument();
    // And offers no second editor for the same fact.
    expect(
      screen.queryByRole("button", { name: /Compose this version/ }),
    ).not.toBeInTheDocument();
  });

  it("renders a refusal in the server's own words", async () => {
    const fetchMock = vi.fn((_url: string, init?: RequestInit) =>
      init?.method === "POST"
        ? response(
            {
              code: "pin_unavailable",
              message:
                "This Project has no confirmed Money Policy. Publish its Money Policy version first.",
            },
            422,
          )
        : response(WITH_DRAFT),
    );
    vi.stubGlobal("fetch", fetchMock as unknown as typeof fetch);
    mount();

    fireEvent.click(await screen.findByRole("button", { name: /Compose this version/ }));
    expect(
      await screen.findByText(
        "This Project has no confirmed Money Policy. Publish its Money Policy version first.",
      ),
    ).toBeInTheDocument();
    // Nothing was changed, and the screen says so rather than leaving it open.
    expect(await screen.findByText("Nothing was changed")).toBeInTheDocument();
  });
});
