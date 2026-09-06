/** Story 57.5 — the design pass of the wizard, in the three things jsdom can prove.
 *
 *  jsdom computes no layout, so "nothing overflows at 1280px" is not assertable
 *  here and this file does not pretend otherwise. What it does hold are the three
 *  decisions the pass rests on, each of which was measurably wrong before it:
 *
 *  a. THE MODE IS A CARD, AND ITS NAME IS ITS LABEL. `ChoiceGroup variant="card"`
 *     declares in its own header that it IS `.source-choice` of the validated
 *     mockup, and it had no caller at all — the mockup had been ported nowhere,
 *     and the wizard's first decision was three bare radios with no sentence.
 *     The label stays the accessible NAME (27 `getByRole("radio", { name })`
 *     calls across 11 files depend on it) and the sentence is a DESCRIPTION.
 *  b. NO ADDRESS IS COMPOSED. `:2277` wrote `/data/datastreams/o/{id}/overview`
 *     into an `<a href>`, and `parsePath` refuses every path whose first segment
 *     is not `/org/`: the last gesture of the whole wizard opened nothing. The
 *     class check is on the whole rendered tree, at the created state included,
 *     rather than on that one anchor — story 57.4 already checked one prefix and
 *     that is exactly why this one survived.
 *  c. ONE STEP, ONE TITLE. The step wrote its own `<h2>` beside a `<p>` while the
 *     panel under it repeated the same word in a second `<h2>`.
 */
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import DatastreamSetupWizard from "../datastreams/preconfiguration/DatastreamSetupWizard";
import { parsePath } from "../shell/router";
import { WIZARD_ASIDE_STICKY } from "../ui";
import * as wizardApi from "../datastreams/wizard/wizardApi";

vi.mock("../datastreams/wizard/wizardApi", async () => {
  const actual = await vi.importActual<typeof import("../datastreams/wizard/wizardApi")>("../datastreams/wizard/wizardApi");
  return {
    ...actual,
    createDatastreamSetupDraft: vi.fn(),
    readDatastreamSetupDraft: vi.fn(),
    readDatastreamSetupSourceOptions: vi.fn(),
    readDatastreamSetupObservation: vi.fn(),
    readDatastreamPreconfigurationProposal: vi.fn(),
    updateDatastreamSetupDraft: vi.fn(),
    readDatastreamMaterialization: vi.fn(),
    listDatastreamSetupTemplates: vi.fn(),
  };
});

const draft: wizardApi.DatastreamSetupDraft = {
  draft_ref: "dsd_1", project_ref: "proj_1", state: "draft",
  current_revision_ref: "dsdr_4", current_revision: 4, current_proposal_ref: null,
  first_incomplete_section: "source", invalidation_causes: [], resume_href: "",
  idempotent_replay: false, operator_input: {},
};

const options: wizardApi.DatastreamSetupSourceOptions = {
  draft_ref: "dsd_1", project_ref: "proj_1",
  source_accounts: [{ object_ref: { id: "sacct_1" }, connector_ref: { id: "generic" }, label: "Acme account", states: { availability: "available" } }],
  connectors: [{
    connector_ref: "generic", contract_version_ref: "ccv_1", contract_fingerprint: "a".repeat(64),
    display_name: "Generic Daily", observed_at: "2026-08-05T10:00:00Z",
    reports: [{ report_ref: "daily", display_name: "Daily report", availability: { status: "selectable" }, metrics: ["spend"], dimensions: ["date"], supported_grains: [["date"]], history: "90 days", cadence: { supported_modes: ["daily"] }, quota_cost: { read_points: 1 } }],
    fields: [{ field_id: "date", kind: "dimension", physical_type: "date", description: "Reporting date" }, { field_id: "spend", kind: "metric", description: "Spend" }],
  }],
  managed_channels: [], external_access: [],
};

/** The three sentences of the validated mockup, with the ratified vocabulary:
 *  `Connector report` is `Connector pull` everywhere else in this product, and
 *  the mockup does not outrank the glossary. */
const MODE_SENTENCE: Record<string, string> = {
  "Connector pull": "Select a provider report, fields, grain, history and supported schedule.",
  "External BigQuery": "Reference a read-only table or view while its external writer stays authoritative.",
  "Managed feed": "Import a file, a Google Sheet, an inbound email or a webhook delivery into a governed toorow landing.",
};

function resumedDraft(section: string): wizardApi.DatastreamSetupDraft {
  return {
    ...draft,
    operator_input: {
      mode: "connector_pull",
      name: "Daily performance",
      data_role: "Spend",
      domain_ids: [],
      schedule: { mode: "daily" },
      source: { source_account_ref: "sacct_1", connector_ref: "generic", connector_contract_version_ref: "ccv_1", report_ref: "daily" },
      configure: { date_field: "date", metrics: "spend", dimensions: "date", date_window: "", filters: "", history_intent: "", cadence_intent: "daily", grain: "date" },
      wizard_state: { active_section: 0, active_section_ref: section, first_incomplete: section },
    },
  };
}

beforeEach(() => {
  vi.mocked(wizardApi.createDatastreamSetupDraft).mockResolvedValue(draft);
  vi.mocked(wizardApi.readDatastreamSetupDraft).mockResolvedValue(draft);
  vi.mocked(wizardApi.readDatastreamSetupSourceOptions).mockResolvedValue(options);
  vi.mocked(wizardApi.readDatastreamMaterialization).mockResolvedValue({ state: "not_materialized" });
  vi.mocked(wizardApi.listDatastreamSetupTemplates).mockResolvedValue({ templates: [], count: 0, limit: 10 });
  vi.mocked(wizardApi.updateDatastreamSetupDraft).mockImplementation(async (_cfg, _id, revision, input) => ({
    ...draft, current_revision: revision + 1, current_revision_ref: `dsdr_${revision + 1}`, operator_input: input,
  }));
});

it("offers the three modes as cards that carry the mockup's sentence, named by their label", async () => {
  const user = userEvent.setup();
  render(<DatastreamSetupWizard projectId="proj_1" />);
  const group = await screen.findByRole("radiogroup", { name: "Mode" });
  const radios = within(group).getAllByRole("radio");
  expect(radios).toHaveLength(3);
  // The NAME is the label and nothing else. A hint folded into the content would
  // read "Connector pull Select a provider report…" and break 27 selectors at once.
  expect(radios.map((radio) => radio.getAttribute("aria-label")))
    .toEqual(["Connector pull", "External BigQuery", "Managed feed"]);
  // The sentence left the card for the caption under the row (57.12, UX pass):
  // three detailed cards in ~372px collapsed to one word per line. It answers
  // for the mode the operator is standing on — each pick prints its own.
  for (const [label, sentence] of Object.entries(MODE_SENTENCE)) {
    await user.click(within(group).getByRole("radio", { name: label }));
    // A mode change after a first choice is a confirmed gesture (57.12):
    // accept the dialog when it appears, then read the caption.
    const accept = document.querySelector("[data-testid='datastream-setup-confirm-accept']");
    if (accept) await user.click(accept as HTMLElement);
    const caption = group.parentElement!.querySelector("p[aria-live]");
    expect(caption).toHaveTextContent(sentence);
  }
});

it("names the step once, as the heading under the page title", async () => {
  // La première section est `Source`, et le mode est sa première question ; un
  // brouillon sans position enregistrée ouvre sur « Mode » et non « Source ».
  // L'intention du test (un seul h2 par étape) ne change pas — on reprend le
  // brouillon DIRECTEMENT sur `Source` via `active_section_ref` pour garder
  // exactement la même assertion.
  vi.mocked(wizardApi.readDatastreamSetupDraft).mockResolvedValue(resumedDraft("source"));
  render(<DatastreamSetupWizard projectId="proj_1" resumeDraftId="dsd_1" />);
  await screen.findByLabelText("Source section");
  // One `h2` per step, not two: the panel band under it used to repeat the word.
  expect(screen.getAllByRole("heading", { level: 2, name: "Source" })).toHaveLength(1);
  // The rail still names every section; it is a set of controls, not headings.
  expect(screen.queryByRole("heading", { level: 2, name: "Configure" })).not.toBeInTheDocument();
});

it("composes no address: every href the wizard renders is one the router resolves", async () => {
  const { container } = render(<DatastreamSetupWizard projectId="proj_1" />);
  await screen.findByRole("radiogroup", { name: "Mode" });
  for (const anchor of Array.from(container.querySelectorAll("a[href]"))) {
    const url = new URL(anchor.getAttribute("href")!, "http://localhost");
    expect(parsePath(url.pathname, url.search).kind).toBe("resolved");
  }
});

it("hands the created Datastream to the shell instead of writing its address", async () => {
  const onCreated = vi.fn();
  vi.mocked(wizardApi.readDatastreamSetupDraft).mockResolvedValue(resumedDraft("schedule_activate"));
  vi.mocked(wizardApi.readDatastreamMaterialization).mockResolvedValue({
    state: "materialized",
    datastream_ref: "ds_created",
    candidate_execution_ref: "dse_1",
    candidate_state: "queued",
    current_published_execution_ref: null,
  });
  const { container } = render(
    <DatastreamSetupWizard projectId="proj_1" resumeDraftId="dsd_1" onCreated={onCreated} />,
  );
  expect(await screen.findByText(/Datastream Draft created/)).toBeInTheDocument();
  // The created state is exactly where the composed address lived. Nothing on
  // this screen may carry one, at any state.
  for (const anchor of Array.from(container.querySelectorAll("a[href]"))) {
    const url = new URL(anchor.getAttribute("href")!, "http://localhost");
    expect(parsePath(url.pathname, url.search).kind).toBe("resolved");
  }
  const open = screen.getByRole("button", { name: "Open the Datastream" });
  open.click();
  expect(onCreated).toHaveBeenCalledWith("ds_created");
});

/**
 * ----------------------------------------------------------------- story 76-6
 *
 * WHAT STAYS ON SCREEN WHEN THE STEP IS LONGER THAN THE VIEWPORT.
 *
 * `datastream-workbench-and-wizard.md:31` calls the left stepper
 * **persistent**. Measured at 1280px on 2026-09-06, four of the five stops are
 * taller than the viewport — `Classify and map` is 2707px — and the rail was an
 * ordinary grid item, so "which step am I on, and how many are left" scrolled
 * away exactly where the step is long enough for a person to need asking. The
 * `Configuration summary` next door has been sticky since it became a zone of
 * its own; the rail, which is the navigation, never was.
 *
 * jsdom lays nothing out, so the pixel proof is the capture
 * (`scratch/screens/76-6/`). What is assertable here is the DECLARATION, and
 * one thing better than a class string: that the two asides read the SAME
 * declaration, so neither can be made to scroll while the other stays.
 */
it("keeps the rail on screen, from the same declaration the summary reads", async () => {
  render(<DatastreamSetupWizard projectId="proj_1" />);
  const rail = await screen.findByLabelText("Datastream setup sections");
  const summary = screen.getByLabelText("Configuration summary");
  expect(rail.className).toContain(WIZARD_ASIDE_STICKY);
  expect(summary.className).toContain(WIZARD_ASIDE_STICKY);
  // And the declaration is a sticky one — an assertion that two files agree
  // proves nothing if they agree on the wrong thing.
  expect(WIZARD_ASIDE_STICKY).toContain("sticky");
  expect(WIZARD_ASIDE_STICKY).toContain("self-start");
  // `self-start` is not decoration: a stretched grid item is as tall as its row
  // and a sticky box that tall never moves.
  expect(rail.className).toContain("self-start");
});

/** THE FOOTER WRAPS RATHER THAN OVERLAPPING (76-6). Six controls sat in one
 *  `justify-between` row with nothing allowed to give, and at 1280px
 *  `Draft saved` broke onto two lines and ran into `Set up source access` on
 *  every step. Again the pixel proof is the capture; the declaration is that
 *  the row may wrap and that the save state may not break mid-phrase. */
it("lets the footer wrap, and keeps the save state on one line", async () => {
  render(<DatastreamSetupWizard projectId="proj_1" />);
  const saved = await screen.findByText("Draft saved");
  const footer = saved.closest("footer") as HTMLElement;
  expect(footer.className).toContain("flex-wrap");
  const line = saved.closest("span.whitespace-nowrap");
  expect(line, "the save state must not break mid-phrase").not.toBeNull();
  expect(line!.className).toContain("shrink-0");
});
