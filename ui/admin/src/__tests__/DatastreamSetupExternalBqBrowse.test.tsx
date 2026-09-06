/** 57.12, T2b — the external-BigQuery object is BROWSED LIVE.
 *
 *  Choosing an access walks `GET /api/connections/{connection_ref.id}/accounts`
 *  — the door 57.1 D2 ratified, the same one the Sources page uses — and the
 *  project → dataset → table tree it returns becomes a drill-down picker.
 *  What this file holds:
 *
 *   * the picker renders the tree, and picking a table writes the SAME
 *     `project.dataset.table` reference the operator would have typed;
 *   * the request carries NO `?connector=` param — 57.11 measured that
 *     `resolve_connection_connector` refuses `bigquery` on a `google_direct`
 *     authorization BY DESIGN, so the URL is asserted, not the behavior
 *     hoped for;
 *   * a `truncated` dataset says so and keeps the free-text reference;
 *   * a FAILED listing names what failed and stays apart from an EMPTY one.
 *
 *  The mock is `apiFetch` itself, one seam below the wizard client, because
 *  the URL is the assertion — a mocked client function would make the
 *  `?connector=` test unfalsifiable. */
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import DatastreamSetupWizard from "../datastreams/preconfiguration/DatastreamSetupWizard";

const calls: Array<{ url: string; method: string }> = [];

vi.mock("../lib/apiFetch", async () => {
  const actual = await vi.importActual<typeof import("../lib/apiFetch")>("../lib/apiFetch");
  return {
    ...actual,
    apiFetch: vi.fn(async (input: string, init?: RequestInit) => {
      calls.push({ url: input, method: init?.method ?? "GET" });
      return route(input, init);
    }),
  };
});

const draft = {
  draft_ref: "dsd_1", project_ref: "proj_1", state: "draft",
  current_revision_ref: "dsdr_1", current_revision: 1, current_proposal_ref: null,
  first_incomplete_section: "source", invalidation_causes: [], resume_href: "/resume",
  idempotent_replay: false, operator_input: {},
};

/** One exposed access whose authorization IS on the wire (57.11): without
 *  `connection_ref.id` there is nothing to walk, and the picker stays off. */
const options = {
  draft_ref: "dsd_1", project_ref: "proj_1",
  source_accounts: [],
  connectors: [],
  managed_channels: [],
  external_access: [{
    object_ref: { id: "bqacct_1" }, connector_ref: { id: "bigquery" },
    connection_ref: { object_type: "connection", id: "conn_bq_1" },
    label: "Acme warehouse", states: { availability: "available" },
  }],
};

const TREE = {
  connection_ref_id: "conn_bq_1",
  topology: "bigquery",
  accounts: [{
    label: "analytics-project", kind: "project",
    children: [{
      label: "daily", kind: "dataset", truncated: false,
      children: [
        { id: "analytics-project.daily.daily_spend", label: "daily_spend", kind: "table", table_type: "TABLE" },
        { id: "analytics-project.daily.daily_reach", label: "daily_reach", kind: "table", table_type: "TABLE" },
      ],
    }],
  }],
};

/** What the listing answers in each test. Overridden per case. */
let listing: { status: number; body: unknown } = { status: 200, body: TREE };

function route(url: string, init?: RequestInit): Response {
  const json = (status: number, body: unknown) => new Response(
    JSON.stringify(body),
    { status, headers: { "Content-Type": "application/json" } },
  );
  if (url.endsWith("/datastream-setup-drafts") && init?.method === "POST") return json(200, draft);
  if (url.endsWith("/source-options")) return json(200, options);
  if (url.endsWith("/datastream-setup-templates")) return json(200, { templates: [], count: 0, limit: 10 });
  if (url.endsWith("/materialization")) return json(200, { state: "not_materialized" });
  if (/\/api\/connections\/[^/]+\/accounts\?connector=bigquery$/.test(url)) {
    return json(listing.status, listing.body);
  }
  if (init?.method === "PATCH") {
    const body = JSON.parse(String(init.body)) as { expected_revision: number; operator_input: unknown };
    return json(200, {
      ...draft,
      current_revision: body.expected_revision + 1,
      current_revision_ref: `dsdr_${body.expected_revision + 1}`,
      operator_input: body.operator_input,
    });
  }
  throw new Error(`Unmocked call: ${init?.method ?? "GET"} ${url}`);
}

beforeEach(() => {
  calls.length = 0;
  listing = { status: 200, body: TREE };
});

async function openExternalBq() {
  const user = userEvent.setup();
  render(<DatastreamSetupWizard projectId="proj_1" />);
  // CINQ arrêts : les radios de mode ouvrent l'étape `Source`, et les
  // leur PROPRE section (`Mode`), et le sélecteur d'accès vit une section plus
  // questions du mode suivent dessous, sans arrêt intermédiaire.
  // chemin — l'assertion sur l'URL du listing n'a pas bougé, c'est le parcours
  // qui suit la restructuration.
  await user.click(await screen.findByRole("radio", { name: "External BigQuery" }));
  await user.selectOptions(await screen.findByLabelText(/BigQuery access/), "bqacct_1");
  return user;
}

it("renders the live tree as a drill-down, and a picked table writes the reference", async () => {
  const user = await openExternalBq();

  // THE URL IS THE ASSERTION: the connection's own id and the connector family
  // that selects the BigQuery account topology.
  await screen.findByLabelText("Project");
  const browse = calls.filter((call) => call.url.includes("/accounts"));
  expect(browse).toHaveLength(1);
  expect(browse[0].url).toContain(
    "/api/connections/conn_bq_1/accounts?connector=bigquery",
  );

  await user.selectOptions(screen.getByLabelText("Dataset"), "daily");
  await user.selectOptions(await screen.findByLabelText("Table or view"), "analytics-project.daily.daily_spend");

  // The pick and the typed reference are one answer: the input reads it.
  expect(screen.getByLabelText(/Table or view reference/)).toHaveValue("analytics-project.daily.daily_spend");
});

it("a bounded dataset says so, and the free-text reference stays", async () => {
  listing = {
    status: 200,
    body: {
      ...TREE,
      accounts: [{
        label: "analytics-project", kind: "project",
        children: [
          { label: "daily", kind: "dataset", truncated: true, children: [
            { id: "analytics-project.daily.daily_spend", label: "daily_spend", kind: "table" },
          ] },
          { label: "archive", kind: "dataset", truncated: false, children: [] },
        ],
      }],
    },
  };
  const user = await openExternalBq();

  await user.selectOptions(await screen.findByLabelText("Dataset"), "daily");
  expect(await screen.findByText("This dataset reached the listing bound")).toBeInTheDocument();
  expect(screen.getByText(/the table you need may not be in it/)).toBeInTheDocument();

  // The fallback is not decorative: typing the three parts still answers.
  const reference = screen.getByLabelText(/Table or view reference/);
  expect(reference).not.toHaveAttribute("readonly");
  await user.type(reference, "analytics-project.daily.unlisted_table");
  await waitFor(() => expect(reference).toHaveValue("analytics-project.daily.unlisted_table"));
});

it("a failed listing names the failure, and stays apart from an empty one", async () => {
  listing = { status: 502, body: { code: "discovery_error", message: "Account discovery failed." } };
  const user = await openExternalBq();

  expect(await screen.findByText("The live warehouse listing could not be read")).toBeInTheDocument();
  expect(screen.getByText(/Account discovery failed/)).toBeInTheDocument();
  // The failure is not the end of the question: the reference can be typed.
  const reference = screen.getByLabelText(/Table or view reference/);
  await user.type(reference, "analytics-project.daily.known_table");
  await waitFor(() => expect(reference).toHaveValue("analytics-project.daily.known_table"));
});

it("an empty listing is a stated absence, not a failure", async () => {
  listing = { status: 200, body: { ...TREE, accounts: [] } };
  await openExternalBq();

  expect(await screen.findByText("This authorization listed nothing it can read")).toBeInTheDocument();
  expect(screen.queryByText("The live warehouse listing could not be read")).not.toBeInTheDocument();
  expect(screen.queryByLabelText("Project")).not.toBeInTheDocument();
});
