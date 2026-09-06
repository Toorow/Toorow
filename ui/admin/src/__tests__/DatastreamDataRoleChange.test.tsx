/**
 * Changing what kind of data a Datastream carries — amendment 9 of the
 * 2026-08-11 review, delivered 2026-08-31.
 *
 * « Le rôle et le mode s'éditent depuis le Workbench, par le même changement
 * gouverné que le reste : un changement préparé, une confirmation qui nomme ce
 * qui bouge en aval, une version. Ce que la ré-écriture entraîne […] un rôle qui
 * change la cascade de frais — se dit dans la confirmation avant d'être écrit,
 * jamais après. »
 *
 * WHAT WAS RED. The Workbench printed the role as description text under the
 * title and offered nothing that touched it, and the object menu held rename,
 * archive and restore only. Meanwhile `PATCH /api/datastreams/{id}` accepted
 * `data_role` straight into `update_datastream` with no base and no sentence —
 * so the door was open and ungoverned, which is worse than the closed door the
 * review described.
 *
 * SO THIS FILE HOLDS THE GOVERNANCE, not the control's existence:
 *   * the base the server published travels back with the change, unaltered;
 *   * the downstream is NAMED before the write, in the server's words, per role;
 *   * a stale base comes back as the server's own refusal and nothing is retried;
 *   * the mode and the connector are stated as NOT editable, with the gesture —
 *     never a selector no writer executes, and never a silence.
 */
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import DatastreamWorkbenchRoute from "../datastreams/workbench/DatastreamWorkbenchRoute";

/** Radix opens on POINTERDOWN and puts `pointer-events: none` on the body — the
 *  two facts `DatastreamLifecycleActions.test.tsx` records, learnt the same way. */
const user = userEvent.setup({ pointerEventsCheck: 0 });

/**
 * The review the server publishes on the header.
 *
 * `paid_media` × `Spend` is one of the five pairs `fee_tax_source_types._AGREEMENT`
 * recognises, so leaving `Spend` takes this Datastream OUT of the cost cascade —
 * which is exactly the consequence the amendment says must be read before the
 * click, and the reason the effect is per role rather than one warning.
 */
const ROLE_CHANGE = {
  current: "Spend",
  expected_data_role: "Spend",
  category: "paid_media",
  category_reason: null,
  source_type: "PAID_MEDIA",
  in_cost_cascade: true,
  options: [
    {
      value: "Spend", source_type: "PAID_MEDIA", in_cost_cascade: true, is_current: true,
      effect: "This is the pairing in force; the fee ladder reads this Datastream as it does today.",
    },
    {
      value: "Context", source_type: "UNKNOWN", in_cost_cascade: false, is_current: false,
      effect:
        "« paid_media » and this role are not one of the pairs the fee ladder recognises, "
        + "so this Datastream would leave the cost cascade.",
    },
    {
      value: "Performance", source_type: "PAID_MEDIA", in_cost_cascade: true, is_current: false,
      effect: "This is the pairing in force; the fee ladder reads this Datastream as it does today.",
    },
  ],
  mode_and_connector: {
    changeable: false,
    mode: "connector_pull",
    connector: "meta-ads",
    reason:
      "The mode and the connector are not changed here. Re-sourcing a Datastream rewrites "
      + "what every collected day, plan version and mart key describes.",
    gesture:
      "Create the Datastream under the mode and connector it should have, and archive this "
      + "one — its history stays readable.",
  },
};

const HEADER = {
  schema: "datastream_workbench.header.v1",
  identity: {
    datastream_id: "ds_EXAMPLE",
    project_id: "proj_EXAMPLE",
    name: "Meta Ads — daily",
    mode: "connector_pull",
    data_role: "Spend",
    owner: "owner@example.com",
    module: "meta-ads",
    connector: "meta-ads",
    source_account_ref: "sacc_EXAMPLE",
    declared_writer: null,
    business_domains: [],
  },
  data_role_change: ROLE_CHANGE,
  axes: { lifecycle: "Active", configuration: "Ready", operations: "Healthy", publication: "Current" },
  versions: { active_plan: "plan_1", active_mapping: "map_1", proposed_plan: null, proposed_mapping: null },
  operations_evidence: {
    next_run_at: null, missed_run_count: 0, schedule_state_known: true, late_reasons: [],
  },
  runs: { latest: "run_EXAMPLE", latest_state: "published" },
  publications: { candidate: null, current: "run_EXAMPLE", last_known_good: null },
  links: { source: "", project_settings: "", governance: "" },
  primary_action: { kind: "prepare_change", label: "Prepare change", reason: "Settled.", tab: "processing" },
};

function json(body: unknown, status = 200) {
  return Promise.resolve(
    new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } }),
  );
}

function stubApi(options: { header?: unknown; patch?: { status: number; body: unknown } } = {}) {
  const calls: Array<{ url: string; method: string; body?: string }> = [];
  vi.stubGlobal(
    "fetch",
    vi.fn().mockImplementation((input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      const method = (init?.method ?? "GET").toUpperCase();
      calls.push({ url, method, body: init?.body as string | undefined });

      if (method === "PATCH") {
        const answer = options.patch ?? {
          status: 200,
          body: {
            id: "ds_EXAMPLE",
            data_role: "Context",
            data_role_change: {
              data_role: "Context",
              data_role_before: "Spend",
              source_type_before: "PAID_MEDIA",
              source_type_after: "UNKNOWN",
              effect:
                "« paid_media » and this role are not one of the pairs the fee ladder "
                + "recognises, so this Datastream would leave the cost cascade.",
            },
          },
        };
        return json(answer.body, answer.status);
      }
      if (url.endsWith("/progress")) {
        return json({
          schema: "datastream_progress.v1", project_id: "proj_EXAMPLE", datastream_id: "ds_EXAMPLE",
          progress: null,
          idle: { reason: "never_ran", execution_id: null, state: null, ended_at: null, error_code: null },
        });
      }
      if (url.endsWith("/processing")) {
        return json({
          schema: "datastream_workbench.processing.v1", tab: "processing",
          project_id: "proj_EXAMPLE", datastream_id: "ds_EXAMPLE",
          evidence: { plans: [], active_version: "plan_1" },
        });
      }
      return json(options.header ?? HEADER);
    }),
  );
  return calls;
}

function mount() {
  return render(
    <DatastreamWorkbenchRoute
      projectId="proj_EXAMPLE"
      datastreamId="ds_EXAMPLE"
      tab="processing"
      onNavigateTab={vi.fn()}
    />,
  );
}

async function openRoleDialog() {
  await user.click(await screen.findByTestId("datastream-lifecycle-menu"));
  await user.click(await screen.findByTestId("lifecycle-data-role"));
  return screen.findByTestId("lifecycle-data-role-select");
}

describe("Changing a Datastream's data role", () => {
  beforeEach(() => vi.restoreAllMocks());

  it("offers the gesture when the header carries its review", async () => {
    stubApi();
    mount();
    await user.click(await screen.findByTestId("datastream-lifecycle-menu"));

    expect(await screen.findByTestId("lifecycle-data-role")).toBeInTheDocument();
  });

  it("offers no role control at all when the header carries no review", async () => {
    // NO REVIEW, NO CONTROL. A confirmation that cannot name what moves
    // downstream is the ungoverned door this change closed on the seam, and
    // rendering the item anyway would put that door back on the screen. An
    // older payload carries no review either, so this is the compatibility case
    // as much as the governance one.
    stubApi({ header: { ...HEADER, data_role_change: null } });
    mount();
    await user.click(await screen.findByTestId("datastream-lifecycle-menu"));

    expect(await screen.findByTestId("lifecycle-rename")).toBeInTheDocument();
    expect(screen.queryByTestId("lifecycle-data-role")).not.toBeInTheDocument();
  });

  it("names what the chosen role does downstream BEFORE the write", async () => {
    stubApi();
    mount();
    const select = await openRoleDialog();

    // Nothing is pre-chosen: a default here would be an answer nobody gave —
    // the same rule the creation wizard's own role selector holds.
    expect((select as HTMLSelectElement).value).toBe("");
    // And the role in force is not offered as a change to itself.
    expect(select.querySelector('option[value="Spend"]')).toBeNull();

    await user.selectOptions(select, "Context");

    expect(await screen.findByTestId("lifecycle-data-role-effect")).toHaveTextContent(
      /would leave the cost cascade/,
    );
  });

  it("sends the base the server published, so a stale screen cannot win silently", async () => {
    const calls = stubApi();
    mount();
    const select = await openRoleDialog();
    await user.selectOptions(select, "Context");
    await user.click(screen.getByTestId("lifecycle-data-role-go"));

    await waitFor(() => expect(calls.some((call) => call.method === "PATCH")).toBe(true));
    const patch = calls.find((call) => call.method === "PATCH");
    const body = JSON.parse(patch?.body ?? "{}");
    expect(body.data_role).toBe("Context");
    // THE SERVER'S value, not one recomposed from `identity.data_role`: a base a
    // screen composed itself is not a claim about what was read.
    expect(body.expected_data_role).toBe(ROLE_CHANGE.expected_data_role);
  });

  it("shows the server's refusal whole when the base has moved", async () => {
    stubApi({
      patch: {
        status: 409,
        body: {
          code: "stale_data_role",
          message:
            "This Datastream's role has been changed since you read it — it now reads "
            + "« Performance ». Reopen it and make the change again against what is there now.",
        },
      },
    });
    mount();
    const select = await openRoleDialog();
    await user.selectOptions(select, "Context");
    await user.click(screen.getByTestId("lifecycle-data-role-go"));

    // The whole sentence, including what it reads NOW — « HTTP 409 » tells a
    // person nothing they can act on.
    expect(await screen.findByText(/it now reads « Performance »/)).toBeInTheDocument();
  });

  it("states that the mode and the connector are not changed here, with the gesture", async () => {
    stubApi();
    mount();
    await openRoleDialog();

    // NOT A SILENCE. The other half of amendment 9 is not delivered, and a
    // person looking for it must find the reason and the gesture rather than an
    // absence they read as a missing feature.
    expect(screen.getByText(/Re-sourcing a Datastream rewrites/)).toBeInTheDocument();
    expect(screen.getByText(/archive this one — its history stays readable/)).toBeInTheDocument();
    // And no control for it: a selector no writer executes is the defect this
    // repository keeps finding.
    expect(screen.queryByTestId("lifecycle-mode-select")).toBeNull();
  });
});
