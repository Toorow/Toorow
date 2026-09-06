/**
 * Story 49.2 — the two Master Data workbench tabs.
 *
 * These pin the claims the tabs make, not their markup. The claim that matters
 * most is negative: an object type with no alias store must say so, because an
 * empty table there reads as "this object has no aliases" and nobody knows that.
 */
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { RouterProvider } from "../shell/router";

import {
  ClientObjectSourcesPanel,
  MasterDataHierarchyTab,
  MasterDataMappingsAliasesTab,
} from "../governance/MasterDataTabs";
import { MasterDataIdentityOverview } from "../governance/MasterDataIdentityOverview";
import type { GovernanceObject } from "../governance/governanceSurface";

function objectWith(summary: Record<string, unknown>): GovernanceObject {
  return {
    object_ref: { type: "business-domain", id: "bd_1", label: "Commerce", owner_href: "" },
    scope: "organization",
    owner: {},
    lifecycle_status: "active",
    active_version_ref: null,
    selected_version_ref: null,
    available_tabs: ["overview", "hierarchy", "mappings-aliases", "used-by", "versions"],
    default_tab: "overview",
    allowed_actions: [],
    used_by: { state: "empty", count: 0, refs: [], truncated: false },
    versions: { state: "empty", count: 0, refs: [], truncated: false },
    evidence: { state: "empty", count: 0, refs: [], truncated: false },
    summary,
    evidence_as_of: null,
  } as unknown as GovernanceObject;
}

describe("Master Data — Hierarchy", () => {
  it("states rootedness as an answer instead of an empty breadcrumb", () => {
    render(
      <MasterDataHierarchyTab
        detail={objectWith({
          hierarchy: { state: "available", is_root: true, ancestors: [], children: [], reason: null },
        })}
      />,
    );

    expect(screen.getByText(/This object is a root/)).toBeInTheDocument();
    expect(screen.getByText(/that is an answer rather than a missing one/)).toBeInTheDocument();
  });

  it("renders the chain innermost first and marks which link is the Domain", () => {
    render(
      <MasterDataHierarchyTab
        detail={objectWith({
          hierarchy: {
            state: "available",
            is_root: false,
            ancestors: [
              { kind: "master-data-object", id: "cls_p", label: "Apparel" },
              { kind: "business-domain", id: "dom_1", label: "Retail" },
            ],
            children: [],
            reason: null,
          },
        })}
      />,
    );

    expect(screen.getByText("Apparel")).toBeInTheDocument();
    expect(screen.getByText("Retail")).toBeInTheDocument();
    expect(screen.getByText("Domain")).toBeInTheDocument();
  });

  it("tells an unreadable hierarchy apart from an object with no children", () => {
    const { unmount } = render(
      <MasterDataHierarchyTab
        detail={objectWith({
          hierarchy: {
            state: "unavailable",
            is_root: false,
            ancestors: [],
            children: [],
            reason: { code: "master_data_hierarchy_unreadable", message: "The store is offline." },
          },
        })}
      />,
    );
    expect(screen.getByText("The hierarchy could not be read")).toBeInTheDocument();
    expect(screen.getByText("The store is offline.")).toBeInTheDocument();
    unmount();

    render(
      <MasterDataHierarchyTab
        detail={objectWith({
          hierarchy: { state: "available", is_root: true, ancestors: [], children: [], reason: null },
        })}
      />,
    );
    expect(screen.getByText("Nothing sits under this object")).toBeInTheDocument();
    expect(screen.getByText(/the answer is none/)).toBeInTheDocument();
  });

  it("treats a missing facet as unreadable, never as a delivered empty tab", () => {
    render(<MasterDataHierarchyTab detail={objectWith({})} />);

    expect(screen.getByText("The hierarchy could not be read")).toBeInTheDocument();
  });
});

describe("Master Data — Mappings & Aliases", () => {
  it("reports a missing alias store rather than an empty table", () => {
    render(
      <RouterProvider>
        <MasterDataMappingsAliasesTab
          detail={objectWith({
            aliases: {
              state: "unavailable",
              rows: [],
              reason: {
                code: "master_data_alias_store_absent",
                message: "No alias store exists for this object type.",
              },
            },
          })}
        />
      </RouterProvider>,
    );

    expect(screen.getByText("The alias store could not be read")).toBeInTheDocument();
    expect(screen.getByText(/No alias store exists/)).toBeInTheDocument();
    // The distinction: this must NOT read as "answered, and there are none".
    expect(screen.queryByText("No alias recorded")).not.toBeInTheDocument();
  });

  it("says none when the store answered and there is none", () => {
    render(
      <RouterProvider>
        <MasterDataMappingsAliasesTab
          detail={objectWith({ aliases: { state: "available", rows: [], reason: null } })}
        />
      </RouterProvider>,
    );

    expect(screen.getByText("No alias recorded")).toBeInTheDocument();
    expect(screen.getByText(/there is none yet/)).toBeInTheDocument();
  });

  it("shows the recorded confidence and never invents one when it is absent", () => {
    render(
      <RouterProvider>
        <MasterDataMappingsAliasesTab
          detail={objectWith({
            aliases: {
              state: "available",
              reason: null,
              rows: [
                {
                  id: "a1", namespace: "iso", locale: "fr-FR", raw_value: "Allemagne",
                  normalized_value: "DE", relation: "exact", confidence: 0.98,
                  provenance: "operator", conflict_state: "none", node_label: "DACH",
                },
                {
                  id: "a2", namespace: "iso", raw_value: "Deutschland", normalized_value: "DE",
                  relation: "exact", confidence: null, provenance: "connector",
                  conflict_state: "ambiguous", node_label: "DACH",
                },
              ],
            },
          })}
        />
      </RouterProvider>,
    );

    // `98.0 %` and not `98%`: since 76-1 a confidence goes through
    // `formatPercent`, which holds one decimal and puts a thin no-break
    // space before the sign (`console-presentation.md` §2). The regex
    // matches that space without pinning its codepoint here.
    expect(screen.getByText(/^98\.0\s*%$/)).toBeInTheDocument();
    // An absent confidence is not 0 %.
    expect(screen.queryByText(/^0(\.0)?\s*%$/)).not.toBeInTheDocument();
    expect(screen.getByText("Ambiguous")).toBeInTheDocument();
    expect(screen.getByText("fr-FR")).toBeInTheDocument();
  });
});

describe("Master Data — the governed node command reaches the route", () => {
  const REG = {
    hierarchy: {
      state: "available",
      is_root: true,
      ancestors: [],
      reason: null,
      children: [
        { kind: "registry-node", id: "mdn_1", label: "France", node_kind: "market", archived: false, command: "archive" },
        { kind: "registry-node", id: "mdn_2", label: "DACH", node_kind: "market", archived: true, command: "restore" },
      ],
    },
  };

  function fetchReturning(...responses: Array<{ status: number; body: unknown }>) {
    const calls: Array<{ url: string; init: RequestInit }> = [];
    const queue = [...responses];
    vi.stubGlobal("fetch", vi.fn((url: string, init: RequestInit = {}) => {
      calls.push({ url, init });
      const next = queue.shift() ?? { status: 200, body: {} };
      return Promise.resolve({
        ok: next.status >= 200 && next.status < 300,
        status: next.status,
        json: () => Promise.resolve(next.body),
      } as Response);
    }));
    return calls;
  }

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("posts the action the read model decided, with an idempotency key", async () => {
    const calls = fetchReturning({ status: 200, body: { operation_id: "op_1" } });
    const onChanged = vi.fn();
    render(<MasterDataHierarchyTab detail={objectWith(REG)} projectId="p1" onChanged={onChanged} />);

    await userEvent.click(screen.getByRole("button", { name: "Archive" }));

    await waitFor(() => expect(onChanged).toHaveBeenCalled());
    const call = calls[0];
    expect(call.url).toContain("/governance/master-data/nodes/mdn_1/commands");
    expect(call.init.method).toBe("POST");
    // Required by the route: a server-minted key would make every retry a new command.
    expect((call.init.headers as Record<string, string>)["Idempotency-Key"]).toBeTruthy();
    expect(JSON.parse(String(call.init.body))).toEqual({ action: "archive", acknowledge_impact: false });
  });

  it("offers Restore on an archived node and keeps it listed as archived", async () => {
    fetchReturning({ status: 200, body: {} });
    render(<MasterDataHierarchyTab detail={objectWith(REG)} projectId="p1" onChanged={vi.fn()} />);

    // Archiving must not look like deletion: the node stays visible and says so.
    expect(screen.getByText("Archived")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Restore" })).toBeInTheDocument();
  });

  it("renders the 409 refusal with the named consumers before anything is acknowledged", async () => {
    fetchReturning({
      status: 409,
      body: {
        message: "node mdn_1 is still used by datastream_mapping (1).",
        impact: { consumers: [{ consumer_kind: "datastream_mapping", consumer_id: "ds_1", consumer_label: "Meta Ads" }] },
      },
    });
    const onChanged = vi.fn();
    render(<MasterDataHierarchyTab detail={objectWith(REG)} projectId="p1" onChanged={onChanged} />);

    await userEvent.click(screen.getByRole("button", { name: "Archive" }));

    expect(await screen.findByText(/still used by datastream_mapping/)).toBeInTheDocument();
    expect(screen.getByText("Meta Ads")).toBeInTheDocument();
    // Nothing happened: the refusal is a stop, not a warning shown after the act.
    expect(onChanged).not.toHaveBeenCalled();
  });

  it("acknowledges on a second deliberate click, reusing the same command key", async () => {
    const calls = fetchReturning(
      { status: 409, body: { message: "still used", impact: { consumers: [] } } },
      { status: 200, body: { operation_id: "op_1" } },
    );
    const onChanged = vi.fn();
    render(<MasterDataHierarchyTab detail={objectWith(REG)} projectId="p1" onChanged={onChanged} />);

    await userEvent.click(screen.getByRole("button", { name: "Archive" }));
    await userEvent.click(await screen.findByRole("button", { name: "Archive anyway" }));

    await waitFor(() => expect(onChanged).toHaveBeenCalled());
    expect(JSON.parse(String(calls[1].init.body))).toEqual({ action: "archive", acknowledge_impact: true });
    // The retry is the SAME command carried through, not a new one.
    const keyOf = (i: number) => (calls[i].init.headers as Record<string, string>)["Idempotency-Key"];
    expect(keyOf(1)).toBe(keyOf(0));
  });

  it("says a 503 changed nothing instead of reporting success", async () => {
    fetchReturning({ status: 503, body: { code: "master_data_evidence_unavailable" } });
    const onChanged = vi.fn();
    render(<MasterDataHierarchyTab detail={objectWith(REG)} projectId="p1" onChanged={onChanged} />);

    await userEvent.click(screen.getByRole("button", { name: "Archive" }));

    expect(await screen.findByText(/could not be read, so nothing was changed/)).toBeInTheDocument();
    expect(onChanged).not.toHaveBeenCalled();
  });

  it("offers no command where there is no project scope to command in", () => {
    render(<MasterDataHierarchyTab detail={objectWith(REG)} />);

    expect(screen.queryByRole("button", { name: "Archive" })).not.toBeInTheDocument();
  });
});

describe("Master Data — a client-declared object kind (AI-232)", () => {
  it("names the Datastream that feeds it, and shows the grain only where there is one", () => {
    render(
      <ClientObjectSourcesPanel
        detail={objectWith({
          object_kind: "video",
          version_scope: "node",
          instance_count: 527,
          sources: {
            state: "available",
            reason: null,
            rows: [
              {
                namespace: "client_workbook",
                datastream_id: "ds_1",
                datastream_label: "Annotation workbook",
                identity_mode: "source_key",
                identity_fields: ["video_id"],
                label_field: null,
              },
              {
                namespace: "pos_export",
                datastream_id: "ds_2",
                datastream_label: "Point of sale export",
                identity_mode: "governed_label",
                identity_fields: [],
                label_field: "restaurant_name",
              },
            ],
          },
        })}
      />,
    );

    expect(screen.getByText("Annotation workbook")).toBeInTheDocument();
    expect(screen.getByText("Identified by its key")).toBeInTheDocument();
    expect(screen.getByText("video_id")).toBeInTheDocument();
    // The label-only source must NOT be shown carrying a key: that is the whole
    // difference between the two modes.
    expect(screen.getByText("Resolved by its label")).toBeInTheDocument();
    expect(screen.getByText("restaurant_name")).toBeInTheDocument();
    // A version covers one instance, not the kind — `version_scope = node`.
    expect(screen.getByText("One instance")).toBeInTheDocument();
    expect(screen.getByText("527")).toBeInTheDocument();
  });

  it("says a kind is no longer fed instead of showing it as never declared", () => {
    render(
      <ClientObjectSourcesPanel
        detail={objectWith({
          object_kind: "video",
          version_scope: "node",
          instance_count: 527,
          sources: { state: "empty", rows: [], reason: null },
        })}
      />,
    );

    expect(screen.getByText("No source feeds this object")).toBeInTheDocument();
    expect(screen.getByText(/Every binding has been released/)).toBeInTheDocument();
    // The instances it already holds are still reported — they did not vanish.
    expect(screen.getByText("527")).toBeInTheDocument();
  });

  it("tells a registry with no binding store apart from one that lost its bindings", () => {
    render(
      <ClientObjectSourcesPanel
        detail={objectWith({
          sources: {
            state: "unavailable",
            rows: [],
            reason: {
              code: "master_data_source_bindings_absent",
              message: "No source binding store exists for this object.",
            },
          },
        })}
      />,
    );

    expect(screen.getByText(/The source bindings could not be read/)).toBeInTheDocument();
    expect(screen.queryByText("No source feeds this object")).not.toBeInTheDocument();
  });
});

describe("Master Data — the alias table narrows", () => {
  const ROWS = [
    { id: "a1", namespace: "iso", raw_value: "Allemagne", normalized_value: "DE",
      relation: "exact", confidence: 0.98, provenance: "operator",
      conflict_state: "none", node_label: "DACH" },
    { id: "a2", namespace: "iso", raw_value: "Espagne", normalized_value: "ES",
      relation: "exact", confidence: 0.9, provenance: "connector",
      conflict_state: "ambiguous", node_label: "Iberia" },
  ];

  function mount() {
    // A real project address: the arbitration door is built with the router's
    // own helpers and is an absence, not a guess, on an unscoped address.
    window.history.replaceState({}, "", "/org/org_1/project/proj_1/governance/master-data");
    render(
      <RouterProvider>
        <MasterDataMappingsAliasesTab
          detail={objectWith({ aliases: { state: "available", reason: null, rows: ROWS } })}
        />
      </RouterProvider>,
    );
  }

  it("filters by incoming value, and the empty narrowing names the term", async () => {
    mount();
    const box = screen.getByRole("searchbox", { name: "Filter aliases" });
    await userEvent.type(box, "allema");
    expect(screen.getByText("Allemagne")).toBeInTheDocument();
    expect(screen.queryByText("Espagne")).toBeNull();

    await userEvent.clear(box);
    await userEvent.type(box, "shopify");
    expect(screen.queryByText("Allemagne")).toBeNull();
    // Distinct from "no alias recorded": the store answered, the filter hides.
    expect(screen.getByText(/shopify/)).toBeInTheDocument();
  });

  it("links a conflicted row to its arbitration in Controls & Quality", () => {
    mount();
    const door = screen.getByRole("link", {
      name: /arbitrate the ambiguous conflict on Espagne/i,
    });
    expect(door).toHaveAttribute("href", expect.stringContaining("controls-quality"));
  });
});

/**
 * The defect this closes (review of 2026-08-30): the Overview of a Business
 * Domain was the generic `Object.entries(summary)` dump, and the ONLY console
 * caller of `runNodeCommand` was the Context Hub — so the workbench a person
 * opens to govern an identity named it and sent them to another screen to
 * rename, archive or restore it.
 *
 * These pin the three claims that make it a workbench again: the eligible
 * commands are the ones the object's state allows, the command reaches the ONE
 * authority door with the object's own id, and a refusal is read in the
 * authority's sentence rather than a generic failure.
 */
describe("Master Data — the identity Overview acts on the object it opened", () => {
  function identity(overrides: Record<string, unknown> = {}): GovernanceObject {
    return {
      ...objectWith({
        slug: "commerce",
        description: "Everything sold.",
        // The base this Overview read -- the authority's current revision, by
        // id (`governance.md`, 2026-08-30).
        current_version_id: "mdver_commerce_c",
      }),
      ...overrides,
    } as GovernanceObject;
  }

  function fetchReturning(...responses: Array<{ status: number; body: unknown }>) {
    const calls: Array<{ url: string; init: RequestInit }> = [];
    const queue = [...responses];
    vi.stubGlobal("fetch", vi.fn((url: string, init: RequestInit = {}) => {
      calls.push({ url, init });
      const next = queue.shift() ?? { status: 200, body: {} };
      return Promise.resolve({
        ok: next.status >= 200 && next.status < 300,
        status: next.status,
        json: () => Promise.resolve(next.body),
      } as Response);
    }));
    return calls;
  }

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("offers rename and archive on an active identity, and never restore", () => {
    render(
      <MasterDataIdentityOverview
        detail={identity()}
        projectId="p1"
        typeLabel="Business Domain"
        onChanged={vi.fn()}
      />,
    );

    expect(screen.getByTestId("master-data-rename")).toBeInTheDocument();
    expect(screen.getByTestId("master-data-archive")).toBeInTheDocument();
    expect(screen.queryByTestId("master-data-restore")).toBeNull();
    // The identity is typed, not dumped: the short code is named in words.
    expect(screen.getByText("Short code")).toBeInTheDocument();
    expect(screen.getByText("commerce")).toBeInTheDocument();
  });

  it("offers only restore on an archived identity, and says it was not deleted", () => {
    render(
      <MasterDataIdentityOverview
        detail={identity({ lifecycle_status: "archived" })}
        projectId="p1"
        typeLabel="Business Domain"
        onChanged={vi.fn()}
      />,
    );

    expect(screen.getByTestId("master-data-restore")).toBeInTheDocument();
    expect(screen.queryByTestId("master-data-rename")).toBeNull();
    expect(screen.queryByTestId("master-data-archive")).toBeNull();
    expect(screen.getByText(/nothing about it was deleted/i)).toBeInTheDocument();
  });

  it("renames through the one authority door, with this object's id, then refreshes", async () => {
    const calls = fetchReturning({ status: 200, body: { operation_id: "op_1" } });
    const onChanged = vi.fn();
    render(
      <MasterDataIdentityOverview
        detail={identity()}
        projectId="p1"
        typeLabel="Business Domain"
        onChanged={onChanged}
      />,
    );

    await userEvent.click(screen.getByTestId("master-data-rename"));
    const name = await screen.findByLabelText("Name");
    await userEvent.clear(name);
    await userEvent.type(name, "Retail");
    await userEvent.type(screen.getByLabelText("Reason"), "The board renamed the division");
    await userEvent.click(within(screen.getByRole("dialog")).getByRole("button", { name: "Rename" }));

    await waitFor(() => expect(onChanged).toHaveBeenCalled());
    const call = calls[calls.length - 1];
    expect(call.url).toContain("/governance/master-data/nodes/bd_1/commands");
    expect(call.init.method).toBe("POST");
    expect((call.init.headers as Record<string, string>)["Idempotency-Key"]).toBeTruthy();
    const body = JSON.parse(String(call.init.body));
    expect(body.action).toBe("rename");
    expect(body.label).toBe("Retail");
    // The three fields live in ONE published version: a rename that sent only
    // the label would publish a version that erased the other two.
    expect(body.description).toBe("Everything sold.");
    expect(body.reason).toBe("The board renamed the division");
    // AND THE BASE IT RENAMES FROM. Without it the authority answers 428 and the
    // rename does not happen at all; with the wrong one it answers 409.
    expect(body.expected_version).toBe("mdver_commerce_c");
  });

  it("renames on the base it read, and states null when the object has none", async () => {
    const calls = fetchReturning({ status: 200, body: { operation_id: "op_1" } });
    render(
      <MasterDataIdentityOverview
        detail={identity({ summary: { slug: "commerce", description: "Everything sold." } })}
        projectId="p1"
        typeLabel="Business Domain"
        onChanged={vi.fn()}
      />,
    );

    await userEvent.click(screen.getByTestId("master-data-rename"));
    await userEvent.type(screen.getByLabelText("Reason"), "The board renamed the division");
    await userEvent.click(
      within(screen.getByRole("dialog")).getByRole("button", { name: "Rename" }),
    );

    await waitFor(() => expect(calls).toHaveLength(1));
    // `null` is a STATED base, not a missing one: the composer sent no current
    // revision, this door says so, and the authority holds the rename to that.
    // Omitting the field would be refused instead — which is the point.
    const body = JSON.parse(String(calls[0].init.body));
    expect(body).toHaveProperty("expected_version", null);
  });

  it("says the object was revised since it was read, and stops posting", async () => {
    // The SHARED sentence, from `masterDataRefusals.ts` — the same words the
    // Context Hub says about the same code. A second wording for one refusal is
    // the defect that module exists to prevent.
    const calls = fetchReturning({
      status: 409,
      body: {
        code: "version_conflict",
        message: "This identity has been revised since you read it.",
      },
    });
    render(
      <MasterDataIdentityOverview
        detail={identity()}
        projectId="p1"
        typeLabel="Business Domain"
        onChanged={vi.fn()}
      />,
    );

    await userEvent.click(screen.getByTestId("master-data-rename"));
    await userEvent.type(screen.getByLabelText("Reason"), "The board renamed the division");
    const dialog = await screen.findByRole("dialog");
    await userEvent.click(within(dialog).getByRole("button", { name: "Rename" }));

    expect(await screen.findByText(/nothing was overwritten/i)).toBeInTheDocument();
    expect(screen.getByText(/reopen the entry/i)).toBeInTheDocument();
    expect(screen.queryByText(/could not be saved/i)).toBeNull();

    // And the command stops here: pressing Rename again would send the same
    // stale base, which is exactly what the sentence tells them not to do.
    expect(within(dialog).getByRole("button", { name: "Rename" })).toBeDisabled();
    await userEvent.click(within(dialog).getByRole("button", { name: "Rename" }));
    expect(calls).toHaveLength(1);
  });

  it("refuses to send a governed change with no reason, and sends nothing", async () => {
    const calls = fetchReturning({ status: 200, body: {} });
    render(
      <MasterDataIdentityOverview
        detail={identity()}
        projectId="p1"
        typeLabel="Business Domain"
        onChanged={vi.fn()}
      />,
    );

    await userEvent.click(screen.getByTestId("master-data-archive"));
    const dialog = await screen.findByRole("dialog");
    await userEvent.click(within(dialog).getByRole("button", { name: "Archive" }));

    expect(await screen.findByText(/remains auditable/i)).toBeInTheDocument();
    expect(calls).toHaveLength(0);
  });

  it("reads the impact refusal in the authority's own sentence, names the consumers, and stops", async () => {
    const calls = fetchReturning({
      status: 409,
      body: {
        code: "master_data_command_refused",
        message:
          "node bd_1 is still used by datastream_mapping (1). Release them, or confirm the archive with the impact acknowledged.",
        impact: {
          consumers: [
            { consumer_kind: "datastream_mapping", consumer_id: "ds_1", consumer_label: "Meta Ads" },
          ],
        },
      },
    });
    const onChanged = vi.fn();
    render(
      <MasterDataIdentityOverview
        detail={identity()}
        projectId="p1"
        typeLabel="Business Domain"
        onChanged={onChanged}
      />,
    );

    await userEvent.click(screen.getByTestId("master-data-archive"));
    const dialog = await screen.findByRole("dialog");
    await userEvent.type(within(dialog).getByLabelText("Reason"), "The division closed");
    await userEvent.click(within(dialog).getByRole("button", { name: "Archive" }));

    expect(await screen.findByText(/still used by datastream_mapping/)).toBeInTheDocument();
    expect(screen.getByText("Meta Ads")).toBeInTheDocument();
    // NOT the generic sentence, and nothing happened: a refusal is a stop.
    expect(screen.queryByText(/could not be saved/i)).toBeNull();
    expect(onChanged).not.toHaveBeenCalled();
    expect(calls).toHaveLength(1);
  });

  it("acknowledges the impact on a second deliberate click, reusing the same command key", async () => {
    const calls = fetchReturning(
      {
        status: 409,
        body: {
          code: "master_data_command_refused",
          message: "node bd_1 is still used by datastream_mapping (1).",
          impact: { consumers: [] },
        },
      },
      { status: 200, body: { operation_id: "op_1" } },
    );
    const onChanged = vi.fn();
    render(
      <MasterDataIdentityOverview
        detail={identity()}
        projectId="p1"
        typeLabel="Business Domain"
        onChanged={onChanged}
      />,
    );

    await userEvent.click(screen.getByTestId("master-data-archive"));
    const dialog = await screen.findByRole("dialog");
    await userEvent.type(within(dialog).getByLabelText("Reason"), "The division closed");
    await userEvent.click(within(dialog).getByRole("button", { name: "Archive" }));
    await userEvent.click(await screen.findByRole("button", { name: "Archive anyway" }));

    await waitFor(() => expect(onChanged).toHaveBeenCalled());
    expect(JSON.parse(String(calls[1].init.body)).acknowledge_impact).toBe(true);
    const keyOf = (i: number) =>
      (calls[i].init.headers as Record<string, string>)["Idempotency-Key"];
    expect(keyOf(1)).toBe(keyOf(0));
  });

  it("says a 503 changed nothing rather than reporting the command done", async () => {
    fetchReturning({ status: 503, body: { code: "master_data_evidence_unavailable" } });
    const onChanged = vi.fn();
    render(
      <MasterDataIdentityOverview
        detail={identity()}
        projectId="p1"
        typeLabel="Business Domain"
        onChanged={onChanged}
      />,
    );

    await userEvent.click(screen.getByTestId("master-data-archive"));
    const dialog = await screen.findByRole("dialog");
    await userEvent.type(within(dialog).getByLabelText("Reason"), "The division closed");
    await userEvent.click(within(dialog).getByRole("button", { name: "Archive" }));

    expect(await screen.findByText(/could not be read, so nothing was changed/)).toBeInTheDocument();
    expect(onChanged).not.toHaveBeenCalled();
  });
});

describe("MasterDataIdentityOverview — a Product carries its base too", () => {
  // Review of d1fbdbb6 (2026-08-30): the composer of products / activities did
  // not put `current_version_id` on its summary, so every Product rename sent
  // `null` and was refused `version_conflict` with a reload that could never
  // help. The read model gives the base for all three kinds; this door sends it.
  it("renames a Product on the base the read model gave it", async () => {
    const calls: Array<{ url: string; init: RequestInit }> = [];
    vi.stubGlobal("fetch", vi.fn((url: string, init: RequestInit = {}) => {
      calls.push({ url, init });
      return Promise.resolve({
        ok: true,
        status: 200,
        json: () => Promise.resolve({ operation_id: "op_p1" }),
      } as Response);
    }));
    render(
      <MasterDataIdentityOverview
        detail={{
          ...objectWith({
            object_kind: "product",
            registry_id: "mdreg_1",
            registry_label: "Products",
            current_version_id: "mdver_product_p",
          }),
          object_ref: { type: "master-data-object", id: "mdnode_p1", label: "Signature Blend", owner_href: "" },
        } as unknown as GovernanceObject}
        projectId="p1"
        typeLabel="Product"
        onChanged={vi.fn()}
      />,
    );

    await userEvent.click(screen.getByTestId("master-data-rename"));
    await userEvent.type(screen.getByLabelText("Reason"), "The catalogue renamed the blend");
    await userEvent.click(
      within(screen.getByRole("dialog")).getByRole("button", { name: "Rename" }),
    );

    await waitFor(() => expect(calls).toHaveLength(1));
    const body = JSON.parse(String(calls[0].init.body));
    expect(body).toHaveProperty("expected_version", "mdver_product_p");
  });
});
