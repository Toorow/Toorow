/**
 * Lot A1 of the front correction plan (issue #68) — the MDM canonical
 * vocabulary becomes visible and addressable.
 *
 * WHAT THE MEASUREMENT SAID BEFORE THIS SCREEN EXISTED. `app.mdm_canonical_fields`
 * holds zero rows at both scopes, six production modules validate every mdm-bound
 * binding against it, and `grep -rn "mdm_canonical_fields" ui/admin/src` returned
 * only comments. So the tests below are ordered by what actually matters: the
 * EMPTY state first, because zero rows is what every Project has today, and an
 * empty list that does not name the gesture that fills it is the defect.
 *
 * These tests mount `ContentRouter` through the REAL router, so what is proven is
 * the address → parser → screen chain — including that a canonical field now has
 * an address at all, which is the half of lot A1 no screen could have faked.
 */
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import ContentRouter from "../shell/ContentRouter";
import { RouterProvider } from "../shell/router";

const ORG = "org_EXAMPLE";
const PROJECT = "proj_EXAMPLE";
const ROOT = `/org/${ORG}/project/${PROJECT}`;
const LENS = `${ROOT}/governance/semantic-model/lens/canonical-fields`;

function ok(body: unknown): Response {
  return {
    ok: true,
    status: 200,
    json: async () => body,
    text: async () => JSON.stringify(body),
  } as Response;
}

function fail(status: number, code: string, message = "refused"): Response {
  return {
    ok: false,
    status,
    json: async () => ({ code, message }),
    text: async () => code,
  } as Response;
}

function facet(state: string, count = 0) {
  return { state, count, refs: [], truncated: false };
}

/** One row of the GENERIC Governance envelope, which is a different answer from
 *  the `/mdm/canonical-fields` payload below and must not be confused with it. */
function govItem(id: string, label: string, scope: string) {
  return {
    object_ref: {
      type: "canonical-field",
      id,
      label,
      owner_href: { surface: "project", workspace: "governance", section: "semantic-model" },
    },
    scope,
    owner: { kind: "canonical-field-registry" },
    lifecycle_status: "active",
    active_version_ref: null,
    selected_version_ref: null,
    available_tabs: ["definition"],
    default_tab: "definition",
    allowed_actions: [],
    used_by: facet("unavailable"),
    versions: facet("unavailable"),
    evidence: facet("unavailable"),
    summary: {},
    evidence_as_of: null,
  };
}

function collection(items: unknown[]) {
  return {
    schema_version: "governance-collection.v1",
    project_ref: { object_type: "project", id: PROJECT },
    organization_ref: { object_type: "organization", id: ORG },
    section: "semantic-model",
    generated_at: "2026-08-11T09:00:00Z",
    evidence_as_of: null,
    lens: "canonical-fields",
    default_lens: "concepts",
    available_lenses: ["canonical-fields"],
    items,
    coverage: {
      state: items.length ? "available" : "empty",
      returned: items.length,
      total: items.length,
      bound: 200,
    },
    unavailable_reasons: [],
  };
}

function field(overrides: Record<string, unknown>) {
  return {
    id: "mdm_0000000000000000000000000A",
    canonical_name: "impressions",
    concept_kind: "metric",
    value_type: "integer",
    aggregation: "sum",
    object_kind: null,
    non_additive: false,
    unit: null,
    description: null,
    scope: "platform",
    ...overrides,
  };
}

function payload(fields: unknown[], emptyReason: unknown = null) {
  const platform = fields.filter((f) => (f as { scope: string }).scope === "platform").length;
  return {
    project_id: PROJECT,
    organization_id: ORG,
    fields,
    scope_counts: { platform, project: fields.length - platform },
    empty_reason: emptyReason,
  };
}

const EMPTY_REASON = {
  code: "no_canonical_field_declared",
  message:
    "No field has been declared yet, at either scope. The shared vocabulary is set up with your provider; the fields your own objects need are declared from the file source that feeds them, one object at a time.",
};

function serve(routes: Array<[RegExp, Response | (() => Response)]>) {
  const calls: string[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn((url: string) => {
      calls.push(url);
      for (const [pattern, answer] of routes) {
        if (pattern.test(url)) {
          return Promise.resolve(typeof answer === "function" ? answer() : answer);
        }
      }
      if (url.includes("/mdm/common-keys")) {
        return Promise.resolve(ok({ common_keys: [], empty_reason: null }));
      }
      return Promise.resolve(fail(404, "not_found"));
    }),
  );
  return calls;
}

function open(address: string) {
  window.history.replaceState({}, "", address);
  return render(
    <RouterProvider>
      <ContentRouter />
    </RouterProvider>,
  );
}

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  window.history.replaceState({}, "", "/");
});

const MDM = /\/mdm\/canonical-fields/;
const GOVERNANCE = /\/governance\//;

describe("the empty state, which is the state every Project is in today", () => {
  it("says why the list is empty and names the gesture that fills it", async () => {
    serve([
      [MDM, ok(payload([], EMPTY_REASON))],
      [GOVERNANCE, ok(collection([]))],
    ]);
    open(LENS);

    const empty = await screen.findByTestId("canonical-fields-empty");
    // WHY — the server's own sentence, in the reader's vocabulary.
    expect(empty).toHaveTextContent(/No field has been declared yet, at either scope/);
    // THE GESTURE — named, and reachable, not merely described.
    const door = within(empty).getByRole("link", { name: /Data › Datastreams/ });
    expect(door).toHaveAttribute("href", `${ROOT}/data/datastreams`);
  });

  it("never names a table, a deployment state or a status code", async () => {
    serve([
      [MDM, ok(payload([], EMPTY_REASON))],
      [GOVERNANCE, ok(collection([]))],
    ]);
    open(LENS);

    const empty = await screen.findByTestId("canonical-fields-empty");
    for (const forbidden of ["mdm_canonical_fields", "project_id", "NULL", "404", "200", "row"]) {
      expect(empty.textContent ?? "").not.toContain(forbidden);
    }
  });

  it("shows ONE empty state, not the generic one underneath its own", async () => {
    serve([
      [MDM, ok(payload([], EMPTY_REASON))],
      [GOVERNANCE, ok(collection([]))],
    ]);
    open(LENS);

    await screen.findByTestId("canonical-fields-empty");
    // The generic collection sentence names no gesture. Two empty states stacked
    // means a person reads the last one they see.
    expect(screen.queryByText("Nothing governed here yet")).not.toBeInTheDocument();
  });
});

describe("the two scopes, separated because the difference decides what may be done", () => {
  const FIELDS = [
    field({ id: "mdm_PLATFORM", canonical_name: "cost", value_type: "money", aggregation: "sum" }),
    field({
      id: "mdm_PROJECT_DIM",
      canonical_name: "recipe",
      concept_kind: "dimension",
      value_type: "string",
      aggregation: null,
      object_kind: "video",
      scope: "project",
    }),
    field({
      id: "mdm_PROJECT_NA",
      canonical_name: "confidence",
      concept_kind: "metric",
      value_type: "ratio",
      aggregation: null,
      non_additive: true,
      object_kind: "video",
      scope: "project",
    }),
  ];

  it("renders the shared vocabulary and the Project's own in separate sections", async () => {
    serve([
      [MDM, ok(payload(FIELDS))],
      [GOVERNANCE, ok(collection([govItem("mdm_PLATFORM", "cost", "platform")]))],
    ]);
    open(LENS);

    const platform = await screen.findByTestId("canonical-fields-platform");
    const project = screen.getByTestId("canonical-fields-project");

    expect(within(platform).getByRole("link", { name: "cost" })).toBeInTheDocument();
    expect(within(platform).queryByText("recipe")).not.toBeInTheDocument();
    expect(within(project).getByRole("link", { name: "recipe" })).toBeInTheDocument();
    expect(within(project).getByRole("link", { name: "confidence" })).toBeInTheDocument();
  });

  it("offers no control at all over a shared field", async () => {
    serve([
      [MDM, ok(payload(FIELDS))],
      [GOVERNANCE, ok(collection([]))],
    ]);
    open(LENS);

    const platform = await screen.findByTestId("canonical-fields-platform");
    // Not a DISABLED control, which reads as "you lack a permission": none.
    expect(within(platform).queryAllByRole("button")).toHaveLength(0);
    expect(platform).toHaveTextContent(/is not changed from here/);
  });

  it("names the kind, the value type and how each field is summed", async () => {
    serve([
      [MDM, ok(payload(FIELDS))],
      [GOVERNANCE, ok(collection([]))],
    ]);
    open(LENS);

    const platform = await screen.findByTestId("canonical-fields-platform");
    const costRow = within(platform).getByRole("link", { name: "cost" }).closest("tr")!;
    expect(costRow).toHaveTextContent("Metric");
    expect(costRow).toHaveTextContent("Money");
    expect(costRow).toHaveTextContent("Sum");

    const project = screen.getByTestId("canonical-fields-project");
    // A dimension is not a measure and carries no aggregation. The cell says so
    // rather than being blank, which would read as "somebody forgot to finish".
    const recipeRow = within(project).getByRole("link", { name: "recipe" }).closest("tr")!;
    expect(recipeRow).toHaveTextContent("Dimension");
    expect(recipeRow).toHaveTextContent("Not a measure");
    expect(recipeRow).toHaveTextContent("video");

    // A metric with no aggregation is non-additive — the database refuses one
    // that declares neither. Never a blank.
    const confidenceRow = within(project).getByRole("link", { name: "confidence" }).closest("tr")!;
    expect(confidenceRow).toHaveTextContent("Never summed");
  });
});

describe("a canonical field is addressable, which it had never been", () => {
  it("links each row at the object route the navigation contract now declares", async () => {
    serve([
      [MDM, ok(payload([field({ id: "mdm_ONE", canonical_name: "clicks" })]))],
      [GOVERNANCE, ok(collection([]))],
    ]);
    open(LENS);

    const link = await screen.findByRole("link", { name: "clicks" });
    expect(link).toHaveAttribute(
      "href",
      `${ROOT}/governance/semantic-model/object/canonical-field/mdm_ONE/tab/definition`,
    );
  });

  it("opens the Definition workbench at that address, and draws the field's own properties", async () => {
    const detail = {
      ...govItem("mdm_ONE", "clicks", "platform"),
      summary: {
        concept_kind: "metric",
        value_type: "integer",
        aggregation: "sum",
        non_additive: false,
        unit: null,
        object_kind: null,
        description: null,
        dictionary_field_name: null,
      },
    };
    serve([
      [
        /objects\//,
        ok({
          schema_version: "governance-object.v1",
          project_ref: { object_type: "project", id: PROJECT },
          organization_ref: { object_type: "organization", id: ORG },
          section: "semantic-model",
          object_type: "canonical-field",
          generated_at: "2026-08-11T09:00:00Z",
          evidence_as_of: null,
          state: "available",
          object: detail,
          unavailable_reasons: [],
        }),
      ],
    ]);
    open(`${ROOT}/governance/semantic-model/object/canonical-field/mdm_ONE/tab/definition`);

    // The workbench draws, rather than the Unknown or the "registered but no
    // workbench" screen — the two answers a contracted-but-unserved object type
    // would have produced.
    expect(await screen.findByRole("heading", { level: 1, name: "clicks" })).toBeInTheDocument();
    expect(
      screen.queryByText("This address is not registered"),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByText("This route is registered, but its workbench does not exist yet"),
    ).not.toBeInTheDocument();
    expect(await screen.findByText("mdm_ONE")).toBeInTheDocument();
  });

  it("navigates there from the list, and the address is the one that was linked", async () => {
    serve([
      [MDM, ok(payload([field({ id: "mdm_ONE", canonical_name: "clicks" })]))],
      [/objects\//, fail(404, "not_found")],
      [GOVERNANCE, ok(collection([]))],
    ]);
    open(LENS);

    await userEvent.click(await screen.findByRole("link", { name: "clicks" }));
    await waitFor(() =>
      expect(window.location.pathname).toBe(
        `${ROOT}/governance/semantic-model/object/canonical-field/mdm_ONE/tab/definition`,
      ),
    );
  });
});

describe("the Project says what its flows share, before any key is declared (2026-09-04)", () => {
  it("lists each shared identity with its carriers, its pins and the gesture", async () => {
    const day = field({
      id: "mdm_DAY",
      canonical_name: "date",
      concept_kind: "dimension",
      value_type: "date",
      aggregation: null,
    });
    vi.stubGlobal("fetch", vi.fn(async (url: string) => {
      if (url.includes("/mdm/common-keys/proposals")) {
        return ok({
          proposals: [
            {
              identity: "date",
              kind: "column",
              carrier_count: 3,
              carriers: [
                { datastream_id: "ds_A", datastream_name: "Audience by age", column: "date", pinned_to: null },
                { datastream_id: "ds_B", datastream_name: "Channel performance", column: "date", pinned_to: null },
                { datastream_id: "ds_C", datastream_name: "Traffic sources", column: "date", pinned_to: null },
              ],
              already_pinned: [],
              pending_publication: ["ds_A", "ds_B"],
              to_pin: ["ds_C"],
              canonical_field_id: "mdm_DAY",
              canonical_name: "date",
              gesture:
                "2 flow(s) already pin `date` to `date` in a newer mapping version that is not published: publish those versions; pin `date` to `date` on the Mapping of 1 flow(s), then declare a common key over it.",
            },
          ],
          unmapped_datastreams: [{ id: "ds_D", name: "Audience snapshot" }],
          empty_reason: null,
        });
      }
      if (url.includes("/mdm/common-keys")) {
        return ok({ common_keys: [], empty_reason: null });
      }
      if (MDM.test(url)) return ok(payload([day]));
      if (GOVERNANCE.test(url)) return ok(collection([]));
      return fail(404, "not_found");
    }));
    open(LENS);

    const section = await screen.findByTestId("mdm-shared-identities");
    const row = await within(section).findByTestId("shared-identity-date");
    expect(within(row).getByText("3 Datastreams")).toBeInTheDocument();
    expect(within(row).getByText(/Audience by age, Channel performance, Traffic sources/)).toBeInTheDocument();
    expect(within(row).getByText("0 of 3")).toBeInTheDocument();
    expect(within(row).getByText(/2 waiting in an unpublished mapping version/)).toBeInTheDocument();
    expect(within(row).getByText(/publish those versions; pin `date`/)).toBeInTheDocument();
    // The flow that publishes no mapping is named, and never counted.
    expect(within(section).getByText(/Not counted, because they publish no mapping yet: Audience snapshot\./)).toBeInTheDocument();
  });

  it("says why nothing is shared, in the server's words, rather than showing an empty table", async () => {
    // One canonical field, or the whole Master Data page shows its own empty
    // state and the common-key panel is never mounted (see the first describe).
    const day = field({ id: "mdm_DAY", canonical_name: "date", concept_kind: "dimension", value_type: "date", aggregation: null });
    vi.stubGlobal("fetch", vi.fn(async (url: string) => {
      if (url.includes("/mdm/common-keys/proposals")) {
        return ok({
          proposals: [],
          unmapped_datastreams: [],
          empty_reason: { code: "no_shared_identity", message: "No column is carried by two published flows yet. Publish a second flow that shares a dimension with the first, then come back." },
        });
      }
      if (url.includes("/mdm/common-keys")) return ok({ common_keys: [], empty_reason: null });
      if (MDM.test(url)) return ok(payload([day]));
      if (GOVERNANCE.test(url)) return ok(collection([]));
      return fail(404, "not_found");
    }));
    open(LENS);

    const section = await screen.findByTestId("mdm-shared-identities");
    expect(await within(section).findByText(/No column is carried by two published flows yet/)).toBeInTheDocument();
    expect(within(section).queryByRole("table")).not.toBeInTheDocument();
  });
});

describe("the panel performs the pin it names (2026-09-05)", () => {
  it("pins the flows still to pin through the console door, then reads the proposal again", async () => {
    const posted: Array<{ url: string; body: unknown }> = [];
    let reads = 0;
    const views = field({ id: "mdm_VIEWS", canonical_name: "views", concept_kind: "metric", value_type: "integer", aggregation: "sum" });
    const proposal = (toPin: string[]) => ({
      identity: "views",
      kind: "column",
      role: "metric",
      carrier_count: 2,
      carriers: [
        { datastream_id: "ds_A", datastream_name: "Channel performance", column: "views", pinned_to: toPin.includes("ds_A") ? null : "mdm_VIEWS" },
        { datastream_id: "ds_B", datastream_name: "Views by video", column: "views", pinned_to: null },
      ],
      already_pinned: toPin.includes("ds_A") ? [] : ["ds_A"],
      pending_publication: [],
      to_pin: toPin,
      canonical_field_id: "mdm_VIEWS",
      canonical_name: "views",
      gesture: "Pin `views` to `views` on the Mapping of 2 flow(s), so the measure is selectable in every crossing.",
    });
    vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
      if (url.includes("/mdm/common-keys/proposals/pin") && init?.method === "POST") {
        posted.push({ url, body: JSON.parse(String(init.body)) });
        return ok({
          canonical_field_id: "mdm_VIEWS", canonical_name: "views", concept_kind: "metric",
          pinned: 1, already_pinned: 0, refused: 1,
          flows: [
            { datastream_id: "ds_A", datastream_name: "Channel performance", column: "views", outcome: "pinned", no_candidate_reason: "binding-only change: published as an overlay" },
            { datastream_id: "ds_B", datastream_name: "Views by video", column: "views", outcome: "refused", code: "unknown_column", message: "`Views by video` carries no column `views` in its mapping in force." },
          ],
        });
      }
      if (url.includes("/mdm/common-keys/proposals")) {
        reads += 1;
        return ok({ proposals: [proposal(reads === 1 ? ["ds_A", "ds_B"] : ["ds_B"])], unmapped_datastreams: [], empty_reason: null });
      }
      if (url.includes("/mdm/common-keys")) return ok({ common_keys: [], empty_reason: null });
      if (MDM.test(url)) return ok(payload([views]));
      if (GOVERNANCE.test(url)) return ok(collection([]));
      return fail(404, "not_found");
    }));
    open(LENS);

    const section = await screen.findByTestId("mdm-shared-identities");
    const row = await within(section).findByTestId("shared-identity-views");
    expect(within(row).getByText("Measure")).toBeInTheDocument();
    const button = within(row).getByTestId("pin-shared-identity-views");
    expect(button).toHaveTextContent("Pin 2 Datastreams");
    button.click();

    const outcome = await within(section).findByTestId("pin-outcome-views");
    expect(outcome).toHaveTextContent("1 pinned, 0 already pinned, 1 refused.");
    expect(outcome).toHaveTextContent("Views by video: `Views by video` carries no column `views`");
    expect(posted).toEqual([{
      url: expect.stringContaining("/mdm/common-keys/proposals/pin"),
      body: { canonical_field_id: "mdm_VIEWS", carriers: [
        { datastream_id: "ds_A", column: "views" },
        { datastream_id: "ds_B", column: "views" },
      ] },
    }]);
    // The proposal was read again, and the server's answer drives the row.
    expect(reads).toBe(2);
    expect(await within(section).findByText("1 of 2")).toBeInTheDocument();
    expect(within(section).getByTestId("pin-shared-identity-views")).toHaveTextContent("Pin 1 Datastream");
  });
});

describe("common matching keys are declared from the MDM vocabulary", () => {
  it("lists exact versions and declares a key from canonical dimensions only", async () => {
    const day = field({
      id: "mdm_DAY",
      canonical_name: "day",
      concept_kind: "dimension",
      value_type: "date",
      aggregation: null,
    });
    const campaign = field({
      id: "mdm_CAMPAIGN",
      canonical_name: "campaign_id",
      concept_kind: "dimension",
      value_type: "string",
      aggregation: null,
      scope: "project",
    });
    const metric = field({ id: "mdm_REVENUE", canonical_name: "revenue" });
    const posted: Array<{ url: string; body: unknown }> = [];
    vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
      if (url.includes("/mdm/common-keys") && init?.method === "POST") {
        posted.push({ url, body: JSON.parse(String(init.body)) });
        return ok({
          common_key: {
            id: "mck_CAMPAIGN_DAY",
            name: "Campaign + day",
            description: null,
            status: "active",
            current_version: {
              id: "mckv_CAMPAIGN_DAY_1",
              version_number: 1,
              components: [
                { canonical_field_id: "mdm_DAY", canonical_name: "day" },
                { canonical_field_id: "mdm_CAMPAIGN", canonical_name: "campaign_id" },
              ],
            },
          },
        });
      }
      if (url.includes("/mdm/common-keys")) {
        return ok({ common_keys: [], empty_reason: null });
      }
      if (MDM.test(url)) return ok(payload([day, campaign, metric]));
      if (GOVERNANCE.test(url)) return ok(collection([]));
      return fail(404, "not_found");
    }));
    open(LENS);

    const panel = await screen.findByTestId("mdm-common-keys");
    expect(await within(panel).findByText(/No common key is declared yet/)).toBeInTheDocument();
    expect(within(panel).queryByRole("checkbox", { name: /revenue/ })).not.toBeInTheDocument();
    await userEvent.type(within(panel).getByRole("textbox", { name: "Business name" }), "Campaign + day");
    await userEvent.click(within(panel).getByRole("checkbox", { name: /day/ }));
    await userEvent.click(within(panel).getByRole("checkbox", { name: /campaign_id/ }));
    await userEvent.click(within(panel).getByRole("button", { name: "Declare common key" }));

    expect(await within(panel).findByText(/mckv_CAMPAIGN_DAY_1/)).toBeInTheDocument();
    expect(within(panel).getByText("day + campaign_id")).toBeInTheDocument();
    expect(posted).toEqual([{
      url: `/api/projects/${encodeURIComponent(PROJECT)}/mdm/common-keys`,
      body: {
        name: "Campaign + day",
        components: ["mdm_DAY", "mdm_CAMPAIGN"],
      },
    }]);
  });
});

describe("a field declared by mistake can leave the vocabulary (AI-304)", () => {
  const PLATFORM = field({ id: "mdm_PLATFORM", canonical_name: "cost", value_type: "money" });
  const MINE = field({
    id: "mdm_MINE",
    canonical_name: "views",
    object_kind: "video",
    scope: "project",
  });

  function serveFields(onDelete: () => { status: number; json: string | Record<string, unknown> }) {
    const seen: Array<{ url: string; method: string }> = [];
    let listed = [PLATFORM, MINE];
    vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
      const method = init?.method ?? "GET";
      if (MDM.test(url) || url.includes("/mdm/canonical-fields")) {
        if (method === "DELETE") {
          seen.push({ url, method });
          const answer = onDelete();
          if (answer.status >= 400) return fail(answer.status, "not_found", String(answer.json));
          listed = [PLATFORM];
          return ok(answer.json);
        }
        return ok(payload(listed));
      }
      if (url.includes("/mdm/common-keys"))
        return ok({ common_keys: [], empty_reason: null });
      if (GOVERNANCE.test(url)) return ok(collection([]));
      return fail(404, "not_found");
    }));
    return seen;
  }

  it("offers the gesture on this Project's own field and on no shared one", async () => {
    serveFields(() => ({ status: 200, json: {} }));
    open(LENS);

    const project = await screen.findByTestId("canonical-fields-project");
    expect(within(project).getByTestId("retire-canonical-field-mdm_MINE")).toBeInTheDocument();

    // Not a DISABLED control on the shared row, which reads as "you lack a
    // permission" instead of "this is not yours to change": none at all.
    const platform = screen.getByTestId("canonical-fields-platform");
    expect(within(platform).queryAllByRole("button")).toHaveLength(0);
  });

  it("never retires on one click — the confirmation says what archiving is NOT", async () => {
    const seen = serveFields(() => ({ status: 200, json: {} }));
    open(LENS);

    const project = await screen.findByTestId("canonical-fields-project");
    await userEvent.click(within(project).getByTestId("retire-canonical-field-mdm_MINE"));

    await within(project).findByTestId("retire-canonical-field-confirm");
    // The two facts that separate this from a delete, said before the act.
    expect(within(project).getByText(/Nothing is deleted/)).toBeInTheDocument();
    expect(within(project).getByText(/becomes\s+free again/)).toBeInTheDocument();
    expect(seen).toEqual([]);
  });

  it("calls DELETE on the single-field address once confirmed", async () => {
    const seen = serveFields(() => ({
      status: 200,
      json: { canonical_field: { id: "mdm_MINE", canonical_name: "views", status: "archived" } },
    }));
    open(LENS);

    const project = await screen.findByTestId("canonical-fields-project");
    await userEvent.click(within(project).getByTestId("retire-canonical-field-mdm_MINE"));
    await userEvent.click(await within(project).findByTestId("retire-canonical-field-confirmed"));

    await waitFor(() => expect(seen.length).toBe(1));
    expect(seen[0]).toEqual({
      url: `/api/projects/${encodeURIComponent(PROJECT)}/mdm/canonical-fields/mdm_MINE`,
      method: "DELETE",
    });
    // Read the list again rather than editing it here: the retired field is gone
    // because the SERVER stopped listing it, not because the screen guessed.
    await waitFor(() =>
      expect(screen.queryByTestId("retire-canonical-field-mdm_MINE")).not.toBeInTheDocument(),
    );
  });

  it("keeps the field selected when the retirement is refused", async () => {
    serveFields(() => ({ status: 404, json: "Not found." }));
    open(LENS);

    const project = await screen.findByTestId("canonical-fields-project");
    await userEvent.click(within(project).getByTestId("retire-canonical-field-mdm_MINE"));
    await userEvent.click(await within(project).findByTestId("retire-canonical-field-confirmed"));

    // The refusal is rendered whole, under a title that names what did not happen.
    expect(await within(project).findByText("The field was not retired")).toBeInTheDocument();
    expect(within(project).getByText("Not found.")).toBeInTheDocument();
    // A refusal is not a reason to lose the gesture.
    expect(within(project).getByTestId("retire-canonical-field-confirm")).toBeInTheDocument();
  });
});

describe("a common key can be retired, which the route served and no control reached", () => {
  const KEY = {
    id: "mck_DAY",
    name: "Day",
    description: null,
    status: "active",
    current_version: {
      id: "mckv_DAY_1",
      version_number: 1,
      components: [{ canonical_field_id: "mdm_DAY", canonical_name: "day" }],
    },
  };

  function serveKeys(onDelete: () => { status: number; json: string | Record<string, unknown> }) {
    const seen: Array<{ url: string; method: string }> = [];
    let listed = [KEY];
    vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
      const method = init?.method ?? "GET";
      if (url.includes("/mdm/common-keys")) {
        if (method === "DELETE") {
          seen.push({ url, method });
          const answer = onDelete();
          if (answer.status >= 400)
            return fail(answer.status, "common_key_in_use", String(answer.json));
          listed = [];
          return ok(answer.json);
        }
        return ok({ common_keys: listed, empty_reason: null });
      }
      //  Le panneau des cles vit SOUS le vocabulaire : une lentille sans champ
      //  canonique rend son etat vide et rien d autre.
      if (MDM.test(url))
        return ok(
          payload([
            field({
              id: "mdm_DAY",
              canonical_name: "day",
              concept_kind: "dimension",
              value_type: "date",
              aggregation: null,
            }),
          ]),
        );
      if (GOVERNANCE.test(url)) return ok(collection([]));
      return fail(404, "not_found");
    }));
    return seen;
  }

  it("never retires on one click — the confirmation says what archiving is NOT", async () => {
    const seen = serveKeys(() => ({ status: 200, json: {} }));
    open(LENS);

    const panel = await screen.findByTestId("mdm-common-keys");
    await userEvent.click(within(panel).getByTestId("retire-common-key-mck_DAY"));

    await within(panel).findByTestId("retire-common-key-confirm");
    // The difference between this and a delete, said before the act.
    expect(within(panel).getByText(/Nothing is deleted/)).toBeInTheDocument();
    expect(within(panel).getByText(/keeps working/)).toBeInTheDocument();
    expect(seen).toEqual([]);
  });

  it("calls DELETE on the served route once confirmed", async () => {
    const seen = serveKeys(() => ({ status: 200, json: { common_key: { id: "mck_DAY", status: "archived" } } }));
    open(LENS);

    const panel = await screen.findByTestId("mdm-common-keys");
    await userEvent.click(within(panel).getByTestId("retire-common-key-mck_DAY"));
    await userEvent.click(await within(panel).findByTestId("retire-common-key-confirmed"));

    await waitFor(() => expect(seen.length).toBe(1));
    expect(seen[0]).toEqual({
      url: `/api/projects/${encodeURIComponent(PROJECT)}/mdm/common-keys/mck_DAY`,
      method: "DELETE",
    });
  });

  it("renders the 409 whole, because its sentence names what to retire first", async () => {
    serveKeys(() => ({
      status: 409,
      json:
        "'Day' is pinned by 2 Semantic View relationships. Retire those relationships " +
        "before archiving the identity they use.",
    }));
    open(LENS);

    const panel = await screen.findByTestId("mdm-common-keys");
    await userEvent.click(within(panel).getByTestId("retire-common-key-mck_DAY"));
    await userEvent.click(await within(panel).findByTestId("retire-common-key-confirmed"));

    expect(await within(panel).findByText(/Retire those relationships before archiving/)).toBeInTheDocument();
    // The key stays selected: a refusal is not a reason to lose the gesture.
    expect(within(panel).getByTestId("retire-common-key-confirm")).toBeInTheDocument();
  });
});

describe("broken is not empty", () => {
  it("says the vocabulary could not be read, and lists nothing underneath", async () => {
    serve([
      [MDM, fail(503, "canonical_fields_unavailable", "the store did not answer")],
      [GOVERNANCE, ok(collection([]))],
    ]);
    open(LENS);

    const broken = await screen.findByTestId("canonical-fields-broken");
    expect(broken).toHaveTextContent(/this is not a Project without a vocabulary/i);
    expect(screen.queryByTestId("canonical-fields-platform")).not.toBeInTheDocument();
    expect(screen.queryByTestId("canonical-fields-project")).not.toBeInTheDocument();
    expect(screen.queryByTestId("canonical-fields-empty")).not.toBeInTheDocument();
  });
});

describe("the lens asks its own route for the vocabulary", () => {
  it("reads GET /api/projects/{id}/mdm/canonical-fields", async () => {
    const calls = serve([
      [MDM, ok(payload([]))],
      [GOVERNANCE, ok(collection([]))],
    ]);
    open(LENS);

    await waitFor(() =>
      expect(
        calls.some((url) =>
          url.includes(`/api/projects/${encodeURIComponent(PROJECT)}/mdm/canonical-fields`),
        ),
      ).toBe(true),
    );
  });
});
