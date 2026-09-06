import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";

import CountryWorkspace from "../governance/CountryWorkspace";

const presetEnvelope = {
  state: "preset_required",
  registry: null,
  presets: [
    {
      id: "france-and-territories",
      key: "france-and-territories",
      label: "France and selectable territories",
      description: "France with optional retained territories.",
      classification: "toorow_curated",
      source_authority: "toorow",
      version: "2026.07",
      payload: {
        nodes: [{ key: "france", node_kind: "market", label: "France" }],
        members: [
          { parent_key: "france", value: "FR", optional: false },
          { parent_key: "france", value: "RE", optional: true },
        ],
      },
    },
  ],
  vocabulary: [
    { code: "FR", display_name: "France", aliases: ["French Republic"] },
    { code: "RE", display_name: "Réunion", aliases: ["Reunion"] },
  ],
  nodes: [],
  draft: null,
  current: null,
  versions: [],
  used_by: [],
};

const draftEnvelope = {
  state: "draft",
  registry: {
    id: "mdr_country",
    current_version_id: null,
    pending_version_id: "mdv_draft",
  },
  presets: [],
  vocabulary: [
    { code: "FR", display_name: "France", aliases: ["French Republic"] },
    { code: "RE", display_name: "Réunion", aliases: ["Reunion"] },
  ],
  nodes: [
    { id: "market_fr", label: "France", node_kind: "market" },
    { id: "region_emea", label: "EMEA", node_kind: "region" },
    { id: "row", label: "Rest of World", node_kind: "rest_of_world" },
  ],
  draft: {
    id: "mdv_draft",
    version_number: 1,
    status: "draft",
    content_hash: "a".repeat(64),
    memberships: [
      { parent_node_id: "market_fr", child_value: "FR", display_order: 0 },
      {
        parent_node_id: "region_emea",
        child_node_id: "market_fr",
        display_order: 1,
      },
    ],
  },
  current: null,
  versions: [],
  used_by: [],
};

/** A Project whose hierarchy is LIVE: one superseded revision behind the current
 *  one, a Rest of World policy in the version payload, and a named consumer. The
 *  field names are the ones the server really composes —
 *  `master_data.py:115` for a version row, `country_workspace.py:184` for a
 *  used-by row. */
function publishedEnvelope() {
  return {
    ...draftEnvelope,
    state: "published",
    registry: {
      id: "mdr_country",
      current_version_id: "mdv_current",
      pending_version_id: null,
    },
    draft: null,
    current: {
      ...draftEnvelope.draft,
      id: "mdv_current",
      version_number: 2,
      status: "current",
      content_hash: "c".repeat(64),
      payload: {
        rest_of_world: {
          node_id: "row",
          label: "Everywhere else",
          parent_node_id: "region_emea",
          default_drill: "aggregate",
        },
      },
    },
    versions: [
      {
        id: "mdv_current",
        version_number: 2,
        status: "current",
        content_hash: "c".repeat(64),
        created_at: "2026-08-01T09:00:00Z",
        published_at: "2026-08-02T10:30:00Z",
      },
      {
        id: "mdv_first",
        version_number: 1,
        status: "superseded",
        content_hash: "b".repeat(64),
        created_at: "2026-07-01T09:00:00Z",
        published_at: "2026-07-02T10:30:00Z",
      },
    ],
    used_by: [
      {
        node_id: "market_fr",
        consumer_kind: "saved_report",
        consumer_id: "report_1",
        consumer_label: "Board report",
        consumer_version_id: null,
        hierarchy_version_id: "mdv_current",
      },
    ],
  };
}

function response(body: unknown, status = 200) {
  return Promise.resolve({
    ok: status >= 200 && status < 300,
    status,
    json: () => Promise.resolve(body),
  } as Response);
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("Country Governance workspace", () => {
  it("starts from a qualified versioned preset instead of a blank form", async () => {
    vi.stubGlobal("fetch", vi.fn(() => response(presetEnvelope)));

    render(<CountryWorkspace projectId="p1" />);

    expect(
      await screen.findByRole("heading", {
        name: "Choose a qualified starting point",
      }),
    ).toBeInTheDocument();
    expect(screen.getByText("toorow")).toBeInTheDocument();
    expect(screen.getByText("Version 2026.07")).toBeInTheDocument();
    expect(
      screen.getByRole("button", {
        name: "Use France and selectable territories",
      }),
    ).toBeInTheDocument();
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
  });

  it("materializes a selected preset through the audited Country route", async () => {
    const fetchMock = vi
      .fn()
      .mockImplementationOnce(() => response(presetEnvelope))
      .mockImplementationOnce(() =>
        response({
          operation_id: "op_1",
          outcome: "succeeded",
          result: { draft_version_id: "mdv_draft" },
        }),
      )
      .mockImplementationOnce(() => response(draftEnvelope));
    vi.stubGlobal("fetch", fetchMock);

    render(<CountryWorkspace projectId="p1" />);

    await userEvent.click(
      await screen.findByRole("button", {
        name: "Use France and selectable territories",
      }),
    );

    expect(
      await screen.findByRole("heading", { name: "Country hierarchy draft" }),
    ).toBeInTheDocument();
    const post = fetchMock.mock.calls[1];
    expect(post[0]).toContain("/governance/master-data/country");
    expect(post[1].method).toBe("POST");
    expect(post[1].headers["Idempotency-Key"]).toBeTruthy();
    expect(JSON.parse(post[1].body)).toEqual({
      action: "apply_preset",
      payload: {
        preset_id: "france-and-territories",
        selected_optional_values: [],
      },
    });
  });

  it("opens a fresh editable draft from the immutable published version", async () => {
    const publishedEnvelope = {
      ...draftEnvelope,
      state: "published",
      registry: {
        id: "mdr_country",
        current_version_id: "mdv_current",
        pending_version_id: null,
      },
      draft: null,
      current: {
        ...draftEnvelope.draft,
        id: "mdv_current",
        status: "current",
        content_hash: "c".repeat(64),
      },
    };
    const fetchMock = vi
      .fn()
      .mockImplementationOnce(() => response(publishedEnvelope))
      .mockImplementationOnce(() =>
        response({
          operation_id: "op_draft",
          outcome: "succeeded",
          result: { draft_version: { id: "mdv_draft" } },
        }),
      )
      .mockImplementationOnce(() => response(draftEnvelope));
    vi.stubGlobal("fetch", fetchMock);

    render(<CountryWorkspace projectId="p1" />);

    await userEvent.click(
      await screen.findByRole("button", { name: "Create editable draft" }),
    );

    expect(
      await screen.findByRole("heading", { name: "Country hierarchy draft" }),
    ).toBeInTheDocument();
    expect(JSON.parse(fetchMock.mock.calls[1][1].body)).toEqual({
      action: "start_draft",
      payload: {
        current_version_id: "mdv_current",
        expected_content_hash: "c".repeat(64),
      },
    });
  });

  // -------------------------------------------------------------------------
  // The published screen is a READ, not a dead end.
  //
  // It used to render one green box carrying the version's raw UUID and a
  // button. The Markets, the Regions and the country assignments — the whole
  // answer to "what is live right now" — were absent, so the only way to see
  // what every report is grouped by was to open a DRAFT of it: an edit
  // performed to satisfy a read, leaving a pending version on a Project nobody
  // meant to change.
  // -------------------------------------------------------------------------

  it("shows the live hierarchy without asking anyone to open a draft", async () => {
    const fetchMock = vi.fn(() => response(publishedEnvelope()));
    vi.stubGlobal("fetch", fetchMock);

    render(<CountryWorkspace projectId="p1" />);

    await screen.findByRole("heading", { name: "Published Country hierarchy" });
    // Region, nested Market and the assigned country, all read-only.
    const tree = within(
      await screen.findByRole("list", { name: "Country hierarchy" }),
    );
    expect(tree.getByText("EMEA")).toBeInTheDocument();
    expect(tree.getByText("region")).toBeInTheDocument();
    expect(tree.getByText("market")).toBeInTheDocument();
    // "France" twice on purpose: the Market, and the canonical country assigned
    // to it. The tree shows the assignment, not just the container.
    expect(tree.getAllByText("France")).toHaveLength(2);
    const franceFlag = tree.getByRole("img", { name: "France" });
    expect(franceFlag).toHaveTextContent("🇫🇷FR");
    expect(franceFlag).toHaveAttribute("title", "France (FR)");
    // The raw UUID is no longer the whole answer, and nothing was fetched twice.
    expect(fetchMock).toHaveBeenCalledTimes(1);
    // Read-only means read-only: no control edits this version.
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
    expect(screen.queryByRole("combobox")).not.toBeInTheDocument();
    expect(screen.getAllByRole("button")).toHaveLength(1);
    expect(
      screen.getByRole("button", { name: "Create editable draft" }),
    ).toBeInTheDocument();
  });

  it("states where Rest of World sits in the published version", async () => {
    vi.stubGlobal("fetch", vi.fn(() => response(publishedEnvelope())));

    render(<CountryWorkspace projectId="p1" />);

    expect(
      await screen.findByText(/reported as Everywhere else under EMEA/i),
    ).toBeInTheDocument();
    expect(screen.getByText(/kept aggregated by default/i)).toBeInTheDocument();
  });

  it("renders the version history the envelope has always carried", async () => {
    vi.stubGlobal("fetch", vi.fn(() => response(publishedEnvelope())));

    render(<CountryWorkspace projectId="p1" />);

    const history = within(
      await screen.findByRole("region", { name: "Country hierarchy versions" }),
    );
    expect(history.getByText(/Version 2/)).toBeInTheDocument();
    expect(history.getByText(/Version 1/)).toBeInTheDocument();
    // Status and date, from the fields the payload really carries.
    expect(history.getByText("Current")).toBeInTheDocument();
    expect(history.getByText("Superseded")).toBeInTheDocument();
    expect(history.getByText("Live")).toBeInTheDocument();
  });

  it("names the consumers of the published hierarchy", async () => {
    vi.stubGlobal("fetch", vi.fn(() => response(publishedEnvelope())));

    render(<CountryWorkspace projectId="p1" />);

    const consumers = within(
      await screen.findByRole("region", { name: "Country hierarchy consumers" }),
    );
    expect(consumers.getByText("Board report")).toBeInTheDocument();
    expect(consumers.getByText("Saved report")).toBeInTheDocument();
    // Named by the node it reads, not by an opaque id.
    expect(consumers.getByText("France")).toBeInTheDocument();
  });

  it("says a consumer is unnamed rather than printing its id", async () => {
    // THE OTHER HALF OF THE TEST ABOVE. The used-by store's `consumer_label` is
    // nullable, and this list read `consumer_label ?? consumer_id ?? "Unnamed
    // consumer"` — a chain whose middle term put `svv_<ULID>` in a name's
    // position while the honest word sat one term to its right, unreachable.
    // `visualization-and-rendering.md` refuses exactly that: the name is
    // resolved on the server, and its absence is a state, not an address.
    const unnamed = publishedEnvelope();
    vi.stubGlobal(
      "fetch",
      vi.fn(() =>
        response({
          ...unnamed,
          used_by: [
            {
              ...unnamed.used_by[0],
              consumer_id: "svv_01KZTESTCONSUMER0001",
              consumer_label: null,
            },
          ],
        }),
      ),
    );

    const { container } = render(<CountryWorkspace projectId="p1" />);

    const consumers = within(
      await screen.findByRole("region", { name: "Country hierarchy consumers" }),
    );
    expect(consumers.getByText("Unnamed consumer")).toBeInTheDocument();
    expect(container.textContent).not.toContain("svv_01KZTESTCONSUMER0001");
  });

  it("names an unnamed node of an impact list without falling back to its id", async () => {
    // The same defect one panel over: `UsedBy` resolves the node's word from the
    // envelope's `nodes`, and a consumer pointing at a node the envelope does
    // not carry used to be labelled `mdnode_<ULID>` instead of "Unnamed node".
    const orphaned = publishedEnvelope();
    vi.stubGlobal(
      "fetch",
      vi.fn(() =>
        response({
          ...orphaned,
          used_by: [{ ...orphaned.used_by[0], node_id: "mdnode_01KZTESTNODE000001" }],
        }),
      ),
    );

    const { container } = render(<CountryWorkspace projectId="p1" />);

    const consumers = within(
      await screen.findByRole("region", { name: "Country hierarchy consumers" }),
    );
    expect(consumers.getByText("Unnamed node")).toBeInTheDocument();
    expect(container.textContent).not.toContain("mdnode_01KZTESTNODE000001");
  });

  it("says why each list is empty instead of showing nothing", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(() => response({ ...publishedEnvelope(), versions: [], used_by: [] })),
    );

    render(<CountryWorkspace projectId="p1" />);

    expect(await screen.findByText("No version has been recorded")).toBeInTheDocument();
    expect(screen.getByText("Nothing reads this hierarchy yet")).toBeInTheDocument();
    // The sentence names the gesture that fills the list, never a deployment state.
    expect(screen.getByText(/Bind one to a geographic dimension/i)).toBeInTheDocument();
  });

  it("edits stable nodes and saves the complete nested hierarchy", async () => {
    const fetchMock = vi
      .fn()
      .mockImplementationOnce(() => response(draftEnvelope))
      .mockImplementationOnce(() =>
        response({
          operation_id: "op_2",
          outcome: "succeeded",
          result: { draft_version: { id: "mdv_draft" } },
        }),
      )
      .mockImplementationOnce(() => response(draftEnvelope));
    vi.stubGlobal("fetch", fetchMock);

    render(<CountryWorkspace projectId="p1" />);

    const name = await screen.findByRole("textbox", {
      name: "Name for France",
    });
    fireEvent.change(name, { target: { value: "France and territories" } });
    expect(
      screen.getByRole("combobox", {
        name: "Parent region for France and territories",
      }),
    ).toHaveValue("region_emea");
    await userEvent.click(screen.getByRole("button", { name: "Save draft" }));

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(3));
    const request = JSON.parse(fetchMock.mock.calls[1][1].body);
    expect(request.action).toBe("save_hierarchy");
    expect(request.payload.version_id).toBe("mdv_draft");
    expect(request.payload.expected_content_hash).toBe("a".repeat(64));
    expect(request.payload.nodes).toContainEqual({
      ref: "market_fr",
      id: "market_fr",
      kind: "market",
      label: "France and territories",
    });
    expect(request.payload.memberships).toContainEqual({
      parent_ref: "region_emea",
      child_ref: "market_fr",
    });
  });

  it("renders named used-by impacts before an acknowledged retry", async () => {
    const impacted = {
      ...draftEnvelope,
      used_by: [
        {
          node_id: "market_fr",
          consumer_kind: "saved_report",
          consumer_id: "report_1",
          consumer_label: "Board report",
        },
      ],
    };
    const fetchMock = vi
      .fn()
      .mockImplementationOnce(() => response(impacted))
      .mockImplementationOnce(() =>
        response(
          {
            code: "country_workspace_impact_acknowledgement_required",
            message: "Meaning would change.",
            impacts: [
              {
                node_id: "market_fr",
                consumers: impacted.used_by,
              },
            ],
          },
          409,
        ),
      );
    vi.stubGlobal("fetch", fetchMock);

    render(<CountryWorkspace projectId="p1" />);

    await screen.findByRole("heading", { name: "Country hierarchy draft" });
    await userEvent.click(screen.getByRole("button", { name: "Save draft" }));

    // Scoped to the refusal block itself. The consumer is now also named in the
    // permanent "What reads this hierarchy" panel below the editor, so an
    // unscoped query would pass on either one; what this test owns is that the
    // ACKNOWLEDGEMENT names who is affected.
    const impact = within(await screen.findByTestId("country-impact"));
    expect(impact.getByText("Board report")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Save and acknowledge impact" }),
    ).toBeInTheDocument();
  });
  it("publishes only through a same-key prepare and consume confirmation", async () => {
    const fetchMock = vi
      .fn()
      .mockImplementationOnce(() => response(draftEnvelope))
      .mockImplementationOnce(() =>
        response({
          confirmation_id: "econf_1",
          confirmation_secret: "single-use-secret",
        }),
      )
      .mockImplementationOnce(() =>
        response({ operation_id: "op_publish", outcome: "succeeded" }),
      )
      .mockImplementationOnce(() => response(draftEnvelope));
    vi.stubGlobal("fetch", fetchMock);

    render(<CountryWorkspace projectId="p1" />);
    await userEvent.click(
      await screen.findByRole("button", { name: "Publish version" }),
    );

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(4));
    const prepare = fetchMock.mock.calls[1][1];
    const publish = fetchMock.mock.calls[2][1];
    expect(prepare.headers["Idempotency-Key"]).toBe(
      publish.headers["Idempotency-Key"],
    );
    expect(JSON.parse(prepare.body)).toEqual({
      action: "prepare_publish",
      payload: {
        version_id: "mdv_draft",
        expected_content_hash: "a".repeat(64),
      },
    });
    expect(JSON.parse(publish.body)).toEqual({
      action: "publish",
      payload: {
        version_id: "mdv_draft",
        expected_content_hash: "a".repeat(64),
        confirmation_id: "econf_1",
        confirmation_secret: "single-use-secret",
      },
    });
  });

  // -------------------------------------------------------------------------
  // Removing a node takes its contents with it, and the contents were not on
  // the button. The draft is only recoverable by reloading, which discards
  // every other unsaved edit too.
  // -------------------------------------------------------------------------

  it("names what leaves with a Market before removing it", async () => {
    vi.stubGlobal("fetch", vi.fn(() => response(draftEnvelope)));

    render(<CountryWorkspace projectId="p1" />);
    await screen.findByRole("heading", { name: "Country hierarchy draft" });
    await userEvent.click(await screen.findByRole("button", { name: "Remove market" }));

    // The count, in the reader's words, before anything is mutated.
    expect(await screen.findByText("Remove France?")).toBeInTheDocument();
    expect(screen.getByText(/This removes 1 country from the draft/)).toBeInTheDocument();

    await userEvent.click(screen.getByTestId("country-remove-confirm"));
    await waitFor(() =>
      expect(screen.queryByRole("textbox", { name: "Name for France" })).not.toBeInTheDocument(),
    );
    // Confirming edits the DRAFT and nothing else: no request was sent.
    expect(screen.getByText("Unsaved hierarchy changes")).toBeInTheDocument();
  });

  it("counts the nested Markets a Region takes with it, and cancelling changes nothing", async () => {
    vi.stubGlobal("fetch", vi.fn(() => response(draftEnvelope)));

    render(<CountryWorkspace projectId="p1" />);
    await screen.findByRole("heading", { name: "Country hierarchy draft" });
    await userEvent.click(await screen.findByRole("button", { name: "Remove region" }));

    // Only the part that is at stake is stated: the Region holds no country of
    // its own, so no zero is put in front of the reader.
    expect(
      await screen.findByText(/This removes 1 nested Market from the draft/),
    ).toBeInTheDocument();
    expect(screen.queryByText(/0 countries/)).not.toBeInTheDocument();

    await userEvent.click(screen.getByTestId("country-remove-cancel"));
    await waitFor(() =>
      expect(screen.queryByTestId("country-remove-confirm")).not.toBeInTheDocument(),
    );
    expect(
      await screen.findByRole("textbox", { name: "Name for EMEA" }),
    ).toBeInTheDocument();
    // Cancelling is not an edit: the draft is still clean and publishable.
    expect(screen.queryByText("Unsaved hierarchy changes")).not.toBeInTheDocument();
  });

  it("removes an empty node in one click", async () => {
    // A confirmation that fires when nothing is at stake teaches people to
    // dismiss it without reading, which is how the one that matters gets
    // dismissed.
    vi.stubGlobal("fetch", vi.fn(() => response(draftEnvelope)));

    render(<CountryWorkspace projectId="p1" />);
    await screen.findByRole("heading", { name: "Country hierarchy draft" });
    await userEvent.click(screen.getByRole("button", { name: "Add Region" }));
    const added = await screen.findByRole("textbox", { name: "Name for New Region" });
    expect(added).toBeInTheDocument();

    const removals = screen.getAllByRole("button", { name: "Remove region" });
    await userEvent.click(removals[removals.length - 1]);

    expect(screen.queryByTestId("country-remove-confirm")).not.toBeInTheDocument();
    await waitFor(() =>
      expect(
        screen.queryByRole("textbox", { name: "Name for New Region" }),
      ).not.toBeInTheDocument(),
    );
  });

  it("saves removals, moves, and the Rest of World policy in one draft", async () => {
    const fetchMock = vi
      .fn()
      .mockImplementationOnce(() => response(draftEnvelope))
      .mockImplementationOnce(() =>
        response({ operation_id: "op_save", outcome: "succeeded" }),
      )
      .mockImplementationOnce(() => response(draftEnvelope));
    vi.stubGlobal("fetch", fetchMock);

    render(<CountryWorkspace projectId="p1" />);
    await screen.findByRole("heading", { name: "Country hierarchy draft" });
    await userEvent.click(
      await screen.findByRole("button", { name: "Remove France from France" }),
    );
    await userEvent.selectOptions(
      screen.getByRole("combobox", { name: "Parent region for France" }),
      "",
    );
    await userEvent.clear(screen.getByRole("textbox", { name: "Rest of World label" }));
    await userEvent.type(
      screen.getByRole("textbox", { name: "Rest of World label" }),
      "Everywhere else",
    );
    await userEvent.selectOptions(
      screen.getByRole("combobox", { name: "Rest of World parent Region" }),
      "region_emea",
    );
    await userEvent.selectOptions(
      screen.getByRole("combobox", { name: "Rest of World default exploration" }),
      "aggregate",
    );
    expect(screen.getByRole("button", { name: "Publish version" })).toBeDisabled();
    await userEvent.click(screen.getByRole("button", { name: "Save draft" }));

    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(3));
    const request = JSON.parse(fetchMock.mock.calls[1][1].body);
    expect(request.payload.memberships).toEqual([]);
    expect(request.payload.rest_of_world_label).toBe("Everywhere else");
    expect(request.payload.rest_of_world_parent_ref).toBe("region_emea");
    expect(request.payload.rest_of_world_drill).toBe("aggregate");
  });
});
