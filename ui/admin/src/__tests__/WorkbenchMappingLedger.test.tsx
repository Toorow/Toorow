/**
 * The Mapping tab, read as a history and worked as a table — 2026-08-18.
 *
 * FOUR MEASURED DEFECTS ARE PINNED HERE, and each of them was a reading the
 * screen had the data for and did not show:
 *
 *  1. the ledger listed a ULID, a state, two counts, two hashes and a date. The
 *     server's own SELECT carries `version_number` and `created_by`
 *     (`datastream_workbench.py`, the mapping branch) — a history with no number
 *     and no author;
 *  2. no two versions could be compared, so what a version DECIDED was
 *     unreadable; and no way back to one existed at all;
 *  3. a hundred bindings arrived as a wall with no search and no order;
 *  4. excluding forty columns meant forty menus, though ONE prepared change
 *     carries every binding it touches.
 *
 * What must NOT appear: a rewrite. Going back is a PROPOSAL — the old contract
 * offered as the proposed payload of a normal governed change — and every
 * version stays exactly as recorded.
 */
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import WorkbenchMappingPage from "../datastreams/workbench/pages/WorkbenchMappingPage";
import type { WorkbenchHeader, WorkbenchTabPayload } from "../datastreams/workbench/workbenchTypes";

function field(id: string, status = "confirmed", canonical: string | null = null) {
  return {
    field_id: id,
    source_identity: id,
    physical_type: "string",
    binding: { canonical_target: canonical, mdm_target: null, status, blocking_reason: null },
  };
}

function columns(ids: string[]) {
  return ids.map((id) => ({
    field_id: id,
    treatment: "Direct",
    canonical_target: null,
    mdm_target: null,
    binding_status: "confirmed",
    confidence: 0.9,
    sample_value: `value of ${id}`,
    contributes_to: [],
    joined_with: [],
  }));
}

const IDS = ["campaign_name", "cost_micros", "impressions", "clicks"];

function payload(): WorkbenchTabPayload {
  return {
    evidence: {
      // Nothing is in force: the head is what the tab opens on and edits, which
      // is the state 4 of the 6 live Datastreams are in.
      active_version: null,
      versions: [
        {
          id: "mv_02",
          version_number: 2,
          created_by: "owner@example.com",
          created_at: "2026-08-17T09:00:00Z",
          content_hash: "b".repeat(64),
          blocking_count: 0,
          executable: true,
          mapping_payload: { fields: IDS.map((id) => field(id)), grain: ["campaign_name"] },
          columns: columns(IDS),
        },
        {
          id: "mv_01",
          version_number: 1,
          created_by: "analyst@example.com",
          created_at: "2026-08-16T09:00:00Z",
          content_hash: "a".repeat(64),
          blocking_count: 3,
          executable: false,
          mapping_payload: { fields: IDS.slice(0, 2).map((id) => field(id)) },
          columns: columns(IDS.slice(0, 2)),
        },
      ],
      capabilities: [],
    },
  } as unknown as WorkbenchTabPayload;
}

const header = {
  identity: { mode: "connector_pull" },
  versions: {
    active_plan: "pv_1",
    active_mapping: null,
    proposed_plan: null,
    proposed_mapping: null,
    ready_mapping_proposal: null,
  },
} as unknown as WorkbenchHeader;

function mount() {
  return render(
    <WorkbenchMappingPage onRetry={() => {}}
      header={header}
      payload={payload()}
      projectId="proj_EXAMPLE"
      datastreamId="ds_EXAMPLE"
      onConfirmed={() => undefined}
    />,
  );
}

/** The vocabulary read and the comparison read, both answered honestly. */
function stubFetch(compare?: unknown) {
  const calls: string[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url: string) => {
      calls.push(String(url));
      if (String(url).includes("/mapping/versions/compare")) {
        return { ok: true, status: 200, json: async () => compare ?? {} };
      }
      return { ok: true, status: 200, json: async () => ({ fields: [] }) };
    }),
  );
  return calls;
}

afterEach(() => vi.unstubAllGlobals());

describe("the mapping ledger reads as a history", () => {
  it("names the version by its NUMBER and says who proposed it", () => {
    stubFetch();
    mount();

    expect(screen.getByRole("cell", { name: /Version 2/ })).toBeInTheDocument();
    expect(screen.getByTestId("version-author-mv_02")).toHaveTextContent("owner@example.com");
    expect(screen.getByTestId("version-author-mv_01")).toHaveTextContent("analyst@example.com");
  });

  it("carries an order, announced to a screen reader", async () => {
    stubFetch();
    mount();

    const header = screen.getByRole("columnheader", { name: /Blocking fields/ });
    expect(header).toHaveAttribute("aria-sort", "none");
    await userEvent.click(within(header).getByRole("button"));
    expect(header).toHaveAttribute("aria-sort", "ascending");
  });

  it("compares two versions through the server, and renders the values", async () => {
    const calls = stubFetch({
      base: { id: "mv_02", version_number: 2 },
      against: { id: "mv_01", version_number: 1 },
      value_diff: {
        state: "composed",
        entries: [
          {
            subject: "impressions",
            subject_kind: "field",
            reading: "Presence in the contract",
            before: "Declared",
            after: "Absent",
          },
        ],
      },
    });
    mount();

    await userEvent.selectOptions(screen.getByTestId("compare-against"), "mv_01");
    await userEvent.click(screen.getByTestId("compare-versions"));

    expect(await screen.findByTestId("version-value-diff")).toBeInTheDocument();
    expect(screen.getByText("Presence in the contract")).toBeInTheDocument();
    // The COMPOSITION is the server's: the console asks, it does not derive.
    expect(calls.some((url) => url.includes("base=mv_02&against=mv_01"))).toBe(true);
  });

  it("offers a way back that PROPOSES, and never rewrites the ledger", async () => {
    stubFetch();
    mount();

    await userEvent.click(screen.getByTestId("propose-from-mv_01"));
    const panel = screen.getByTestId("propose-from-version");
    expect(panel).toHaveTextContent("stays in the ledger");
    expect(panel).toHaveTextContent("nothing is rewritten");
    // The act is the ordinary governed change, so the append stays the only
    // writer. No "restore" endpoint, no pointer move.
    expect(screen.getByTestId("prepare-restore-change")).toBeInTheDocument();
  });
});

describe("the bindings table can be searched, ordered and acted on in bulk", () => {
  it("finds one column among the wall, and says how many are shown", async () => {
    stubFetch();
    mount();

    expect(screen.getByTestId("binding-shown")).toHaveTextContent("All 4 columns shown");
    await userEvent.type(screen.getByTestId("binding-search"), "cost");
    expect(screen.getByTestId("binding-shown")).toHaveTextContent("1 of 4 columns shown");
    expect(screen.getByRole("cell", { name: "cost_micros" })).toBeInTheDocument();
    expect(screen.queryByRole("cell", { name: "clicks" })).toBeNull();
  });

  it("orders the columns by the reading a person can see", async () => {
    stubFetch();
    mount();

    const identity = screen.getByRole("columnheader", { name: /Source identity/ });
    await userEvent.click(within(identity).getByRole("button"));
    expect(identity).toHaveAttribute("aria-sort", "ascending");
  });

  it("excludes every column the search left, in ONE prepared change", async () => {
    stubFetch();
    mount();

    await userEvent.type(screen.getByTestId("binding-search"), "s");
    const shown = screen.getByTestId("binding-shown").textContent ?? "";
    await userEvent.click(screen.getByTestId("bulk-exclude"));

    // The scope sentence counts what stops landing BEFORE the act — the rule
    // every destructive confirmation on this console follows.
    const scope = screen.getByTestId("exclusion-review");
    expect(scope).toHaveTextContent(/columns? stop landing/);
    expect(shown).toContain("of 4 columns shown");
    // ONE door, one confirmation, however many bindings it carries.
    expect(screen.getAllByTestId("prepare-exclusion-change")).toHaveLength(1);
  });
});
