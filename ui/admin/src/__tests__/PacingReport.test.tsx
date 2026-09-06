/**
 * Analyze > Reports > Pacing — the console half of story 67.26.
 *
 * WHAT THESE ASSERT, and why each is a defect rather than a nicety:
 *
 *   1. THE SCREEN ASKS FOR THE CARD, WITH A PLAN. `mediaplan_pacing` refuses
 *      without a `plan_id`; a screen that omitted it would render a 422 forever.
 *      This is the exact call the MCP App makes, which is what "surface parity"
 *      means — the same Result, not a second computation of it.
 *   2. IT COMPUTES NOTHING. The columns, their labels, their order and every
 *      value come from the envelope. A screen that derived a percentage would be
 *      the second answer to one question that the `Placements` tab already
 *      refuses to become.
 *   3. TWO CURRENCIES ARE SAID, NOT HIDDEN (amendment 61.4). The mart suppresses
 *      the composed figures; without the sentence, a suppressed pace reads as
 *      missing data rather than as a deliberate refusal — the same lie one step
 *      later.
 *   4. NULL RENDERS `—`, NEVER `0`. AD-9, and the firing 61.4 was ratified to
 *      stop: a zero pace on a campaign that had spent normally.
 *   5. AN UNREACHABLE WAREHOUSE IS A STATE, NOT AN EMPTY TABLE.
 *   6. AN EMPTY LIST SAYS WHY AND NAMES THE GESTURE.
 */
import { render, screen, waitFor } from "@testing-library/react";
import PacingReport from "../analyze-artifacts/PacingReport";

const PROJECT = "proj_EXAMPLE";
const PLAN = "plan_EXAMPLE";

function ok(body: unknown): Response {
  return {
    ok: true,
    status: 200,
    json: async () => body,
    text: async () => JSON.stringify(body),
  } as Response;
}

function fail(status: number, body: unknown): Response {
  return {
    ok: false,
    status,
    json: async () => body,
    text: async () => JSON.stringify(body),
  } as Response;
}

const PLANS = { plans: [{ id: PLAN, name: "Q1 Brand", currency: "EUR" }] };

/** The envelope shape `get_card` returns for `mediaplan_pacing`. */
function envelope(opts: { mixed?: boolean } = {}) {
  const mixed = opts.mixed === true;
  return {
    envelope: {
      data: {
        plan_id: PLAN,
        plan_name: "Q1 Brand",
        plan_version_id: "mpv_EXAMPLE",
        as_of_day: "2026-03-10",
        money: {
          reporting_currency: "EUR",
          money_policy_version_id: "mp_v1",
          plan_currency: mixed ? ["USD"] : ["EUR"],
          actual_currency: ["EUR"],
          comparable: !mixed,
          gap_codes: mixed ? [] : [],
          withheld_line_count: 0,
          fx_evidence: {
            native_currency: ["USD"],
            as_of_start: "2026-03-01",
            as_of_end: "2026-03-10",
            source: ["ecb"],
            tier: ["reference"],
            method: ["direct"],
          },
        },
        pacing_meta: {
          pull_ids: ["pull_a", "pull_b"],
          estimate_label: "Estimation",
          pace_formula: "Pace = (actual - planned_to-date) / planned_to-date",
        },
        composition: [
          {
            type: "table",
            title: "Plan lines",
            binding: { source: "plan_lines" },
            data: {
              columns: [
                { key: "label", label: "Line", numeric: false },
                { key: "currency", label: "Currency", numeric: false },
                { key: "budget", label: "Budget", numeric: true },
                { key: "pace_pct", label: "Pace (%)", numeric: true },
                { key: "extrapolated_spend", label: "Extrapolated (Estimate)", numeric: true },
              ],
              rows: [
                {
                  line_key: "line-digital-a",
                  label: "Digital A",
                  currency: mixed ? null : "EUR",
                  plan_currency: mixed ? "USD" : "EUR",
                  actual_currency: "EUR",
                  budget: 3000,
                  pace_pct: mixed ? null : 25,
                  extrapolated_spend: 3750,
                },
              ],
              estimate_columns: ["extrapolated_spend"],
            },
          },
          {
            type: "comment",
            binding: { source: "plan_pacing" },
            data: { text: "Digital A is pacing 25% ahead of its allocation." },
          },
        ],
      },
    },
  };
}

function stub(handler: (url: string) => Response) {
  const mock = vi.fn((url: string) => Promise.resolve(handler(String(url))));
  vi.stubGlobal("fetch", mock);
  return mock;
}

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

it("asks the card mirror for the pacing Result, with the plan it is about", async () => {
  const mock = stub((url) => (url.includes("/api/cards") ? ok(envelope()) : ok(PLANS)));
  render(<PacingReport projectId={PROJECT} />);

  await waitFor(() => {
    const cardCall = mock.mock.calls.map(([u]) => String(u)).find((u) => u.includes("/api/cards"));
    expect(cardCall).toBeDefined();
    // The three arguments that make this the SAME Result the MCP App carries.
    expect(cardCall).toContain("template=mediaplan_pacing");
    expect(cardCall).toContain(`plan_id=${PLAN}`);
    expect(cardCall).toContain(`project_id=${PROJECT}`);
  });
});

it("renders the server's columns and values, and computes nothing", async () => {
  stub((url) => (url.includes("/api/cards") ? ok(envelope()) : ok(PLANS)));
  render(<PacingReport projectId={PROJECT} />);

  // Labels are the server's — including the currency COLUMN that replaced the
  // hard-coded euro in the header.
  expect(await screen.findByText("Currency")).toBeInTheDocument();
  expect(screen.getByText("Budget")).toBeInTheDocument();
  expect(screen.getByText("Digital A")).toBeInTheDocument();
  expect(screen.getByText("EUR")).toBeInTheDocument();
  // No glyph the server did not send.
  expect(screen.queryByText(/Budget \(€\)/)).not.toBeInTheDocument();
  // The extrapolation is labelled an estimate, never a measure (AD-9).
  expect(screen.getByText("Estimate")).toBeInTheDocument();
  // The comment travels with the Result.
  expect(screen.getByTestId("pacing-comment")).toHaveTextContent("25% ahead");
});

it("says the plan version and the as-of day, so the figure is reproducible", async () => {
  stub((url) => (url.includes("/api/cards") ? ok(envelope()) : ok(PLANS)));
  render(<PacingReport projectId={PROJECT} />);

  const provenance = await screen.findByTestId("pacing-provenance");
  expect(provenance).toHaveTextContent("mpv_EXAMPLE");
  expect(provenance).toHaveTextContent("2026-03-10");
  expect(provenance).toHaveTextContent("pull_a");
});

it("states one currency when there is one, with its FX provenance", async () => {
  stub((url) => (url.includes("/api/cards") ? ok(envelope()) : ok(PLANS)));
  render(<PacingReport projectId={PROJECT} />);

  const money = await screen.findByTestId("pacing-money");
  expect(money).toHaveTextContent("One currency");
  expect(money).toHaveTextContent("EUR");
  expect(screen.getByTestId("pacing-fx")).toHaveTextContent("2026-03-01");
  expect(screen.getByTestId("pacing-fx")).toHaveTextContent("ecb");
});

it("names two currencies as a refusal, and draws no pace at all", async () => {
  stub((url) => (url.includes("/api/cards") ? ok(envelope({ mixed: true })) : ok(PLANS)));
  render(<PacingReport projectId={PROJECT} />);

  const money = await screen.findByTestId("pacing-money");
  expect(money).toHaveTextContent("not in the same currency");
  expect(money).toHaveTextContent("USD");
  expect(money).toHaveTextContent("EUR");

  // The suppressed pace reads as an em dash, NEVER as 0 — the whole point of
  // 61.4. Both amounts still show, each under its own currency.
  const cells = screen.getAllByTestId("pacing-cell-pace_pct");
  expect(cells[0]).toHaveTextContent("—");
  expect(cells[0]).not.toHaveTextContent("0");
  expect(screen.getByText("3000")).toBeInTheDocument();
});

it("an unreachable warehouse is a stated condition, not an empty pacing", async () => {
  stub((url) =>
    url.includes("/api/cards")
      ? fail(503, { code: "warehouse_unavailable", message: "unavailable" })
      : ok(PLANS),
  );
  render(<PacingReport projectId={PROJECT} />);

  const status = await screen.findByTestId("pacing-unavailable");
  expect(status).toHaveTextContent(/unreachable/i);
  // The distinction that matters: unknown, not zero.
  expect(status).toHaveTextContent(/is zero/i);
  expect(status).toHaveTextContent(/unknown/i);
});

it("an empty list says why it is empty and names the gesture that fills it", async () => {
  stub(() => ok({ plans: [], carriers: [] }));
  render(<PacingReport projectId={PROJECT} />);

  expect(await screen.findByText("No media plan in this Project")).toBeInTheDocument();
  // It names the gesture AND what the console does not carry, rather than
  // offering a button that goes nowhere.
  expect(screen.getByText(/import/i)).toBeInTheDocument();
});

/**
 * LA PORTE — arbitrage ratifié le 2026-08-24 (`analyze-and-test.md`, « the media
 * plan is created and imported in the carrier Datastream's Workbench »), dont
 * l'`Incomplete if` dit : « the Pacing empty state still names the gesture
 * without offering the door to the carrier Datastream's Workbench ».
 *
 * Ces trois cas sont les trois situations, et la deuxième n'est pas un échec de
 * la première : sans porteur, la porte serait une adresse inventée.
 */
it("offers the carrier Datastream's Workbench when one exists", async () => {
  stub(() =>
    ok({
      plans: [],
      carriers: [
        { datastream_id: "ds_EXAMPLE_PLAN", name: "Agency plan file", plan_id: null, plan_name: null },
      ],
    }),
  );
  const onOpenDatastream = vi.fn();
  render(<PacingReport projectId={PROJECT} onOpenDatastream={onOpenDatastream} />);

  const door = await screen.findByTestId("pacing-open-carrier");
  expect(door).toHaveTextContent("Open Agency plan file");
  // La phrase nomme le porteur ET ce qui s'y fait, pas un terme de base.
  expect(screen.getByText(/reads files as plan lines and carries no plan yet/)).toBeInTheDocument();

  door.click();
  // L'onglet `data` : le cycle du fichier, où le plan et ses versions datées
  // vivent. Une porte vers `overview` laisserait la personne chercher.
  expect(onOpenDatastream).toHaveBeenCalledWith("ds_EXAMPLE_PLAN", "data");
});

it("names every carrier when the Project has several, because a plan is one per Datastream", async () => {
  stub(() =>
    ok({
      plans: [],
      carriers: [
        { datastream_id: "ds_A", name: "Agency A plan", plan_id: null, plan_name: null },
        { datastream_id: "ds_B", name: "Agency B plan", plan_id: "plan_B", plan_name: "Q2 Retail" },
      ],
    }),
  );
  const onOpenDatastream = vi.fn();
  render(<PacingReport projectId={PROJECT} onOpenDatastream={onOpenDatastream} />);

  expect(await screen.findByText("Datastreams that carry a media plan")).toBeInTheDocument();
  // Le second porteur porte DÉJÀ un plan, et la phrase le dit : sa prochaine
  // révision datée s'importe là, elle ne s'y crée pas.
  expect(screen.getByText(/carries the media plan “Q2 Retail”/)).toBeInTheDocument();
  screen.getByRole("button", { name: "Open Agency B plan" }).click();
  expect(onOpenDatastream).toHaveBeenCalledWith("ds_B", "data");
});

it("with no carrier at all, names the gesture that makes one and offers THAT door", async () => {
  stub(() => ok({ plans: [], carriers: [] }));
  const onOpenDatastream = vi.fn();
  const onAddDatastream = vi.fn();
  render(
    <PacingReport
      projectId={PROJECT}
      onOpenDatastream={onOpenDatastream}
      onAddDatastream={onAddDatastream}
    />,
  );

  expect(
    await screen.findByText(/No Datastream of this Project reads a file as plan lines yet/),
  ).toBeInTheDocument();
  // Aucune porte vers un Workbench : il n'y en a pas. Celle qui est offerte est
  // celle du geste qui répare vraiment.
  expect(screen.queryByTestId("pacing-open-carrier")).not.toBeInTheDocument();
  screen.getByTestId("pacing-add-carrier").click();
  expect(onAddDatastream).toHaveBeenCalled();
  expect(onOpenDatastream).not.toHaveBeenCalled();
});
