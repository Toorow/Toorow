/**
 * Story 49.5 — the Evidence index, through the REAL router.
 *
 * `ContentRouter` is mounted inside `RouterProvider` with the browser address
 * set to the route under test, so what is proven is the chain a person
 * exercises: address → parser → registry → screen → the next address.
 *
 * This file also CARRIES FORWARD the behaviour worth keeping from the two
 * retired Evidence pages, which is why it exists before they are deleted:
 *
 *   from `Provenance.tsx`  a bounded read that reports itself degraded rather
 *                          than empty, a refusal of a response belonging to
 *                          another Project, and an honest empty state that is
 *                          reached only after a complete valid read;
 *   from `Activity.tsx`    `no-store`, Project scope proven by the response
 *                          rather than assumed, a stale response discarded when
 *                          an older request lands last, and no request at all
 *                          without a real Project.
 *
 * What is NOT carried forward, deliberately: the browser fan-out across up to
 * twenty publications, and the display of `provider_account` and
 * `connection_ref`. One was authoritative-looking and wrong; the other was a
 * directory of provider account identifiers on an audit screen.
 */
import { cleanup, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import ContentRouter from "../shell/ContentRouter";
import { RouterProvider } from "../shell/router";

const ORG = "org_EXAMPLE";
const PROJECT = "proj_EXAMPLE";
const ROOT = `/org/${ORG}/project/${PROJECT}`;
const EVIDENCE = `${ROOT}/governance/evidence`;

function ok(body: unknown): Response {
  return { ok: true, status: 200, json: async () => body, text: async () => JSON.stringify(body) } as Response;
}

function fail(status: number, code: string): Response {
  return {
    ok: false,
    status,
    json: async () => ({ code, message: "refused" }),
    text: async () => code,
  } as Response;
}

function facet(state: string, refs: unknown[] = []) {
  return { state, count: refs.length, refs, truncated: false };
}

function evidenceItem(
  type: string,
  id: string,
  label: string,
  summary: Record<string, unknown>,
  overrides: Record<string, unknown> = {},
) {
  return {
    object_ref: {
      type,
      id,
      label,
      owner_href: { surface: "project", workspace: "governance", section: "evidence" },
    },
    scope: "project",
    owner: { kind: "data_execution", workspace: "data", object_type: "datastream-execution", object_id: "dse_1" },
    lifecycle_status: "available",
    active_version_ref: null,
    selected_version_ref: null,
    available_tabs:
      type === "evidence-trace"
        ? ["overview", "lineage", "provenance"]
        : type === "object-version"
          ? ["overview", "diff", "approvals", "used-by"]
          : ["overview"],
    default_tab: "overview",
    allowed_actions: [],
    used_by: facet("empty"),
    versions: facet("unavailable"),
    evidence: facet("available", [
      { evidence_id: id, kind: "evidence_trace", state: "available", recorded_at: "2026-07-30T08:00:00Z" },
    ]),
    summary: {
      record_kind: "evidence_trace",
      producer: "data_execution",
      owner_workspace: "data",
      owner_object_type: "datastream-execution",
      owner_object_id: "dse_1",
      owner_version_id: null,
      is_anchor: true,
      occurred_at: "2026-07-30T08:00:00Z",
      availability: "available",
      correlations: [],
      ...summary,
    },
    evidence_as_of: "2026-07-30T08:00:00Z",
    ...overrides,
  };
}

function collection(lens: string, overrides: Record<string, unknown> = {}) {
  return {
    schema_version: "governance-collection.v1",
    project_ref: { object_type: "project", id: PROJECT },
    organization_ref: { object_type: "organization", id: ORG },
    section: "evidence",
    generated_at: "2026-07-30T09:00:00Z",
    evidence_as_of: "2026-07-30T08:00:00Z",
    lens,
    default_lens: "lineage-provenance",
    available_lenses: ["lineage-provenance", "versions-approvals", "audit-activity"],
    items: [],
    coverage: { state: "empty", returned: 0, total: 0, bound: 200, index_state: "complete", adapters: [] },
    unavailable_reasons: [],
    next_cursor: null,
    applied_filters: {},
    ...overrides,
  };
}

function objectEnvelope(type: string, detail: unknown, overrides: Record<string, unknown> = {}) {
  return {
    schema_version: "governance-object.v1",
    project_ref: { object_type: "project", id: PROJECT },
    organization_ref: { object_type: "organization", id: ORG },
    section: "evidence",
    object_type: type,
    generated_at: "2026-07-30T09:00:00Z",
    evidence_as_of: "2026-07-30T08:00:00Z",
    state: detail ? "available" : "unavailable",
    object: detail,
    unavailable_reasons: [],
    ...overrides,
  };
}

function serve(routes: Array<[RegExp, Response | (() => Response)]>) {
  const calls: Array<{ url: string; init?: RequestInit }> = [];
  vi.stubGlobal(
    "fetch",
    vi.fn((url: string, init?: RequestInit) => {
      calls.push({ url, init });
      for (const [pattern, answer] of routes) {
        if (pattern.test(url)) return Promise.resolve(typeof answer === "function" ? answer() : answer);
      }
      return Promise.resolve(fail(404, "not_found"));
    }),
  );
  return calls;
}

function open(pathname: string) {
  window.history.replaceState({}, "", pathname);
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

describe("the three Evidence lenses", () => {
  it("gives each lens its own typed columns instead of one generic table", async () => {
    serve([
      [/lens=lineage-provenance/, ok(collection("lineage-provenance", {
        items: [evidenceItem("evidence-trace", "evr_1", "datastream execution dse_1", {
          linked_record_count: 3,
          owner_reference_count: 2,
          completeness: "linked",
          horizon_start: "2026-07-30T07:00:00Z",
          horizon_end: "2026-07-30T08:00:00Z",
        })],
        coverage: { state: "available", returned: 1, total: 1, bound: 200, index_state: "complete", adapters: [] },
      }))],
      [/lens=audit-activity/, ok(collection("audit-activity", {
        items: [evidenceItem("audit-event", "evr_a1", "audit event audit_1", {
          record_kind: "audit_event",
          actor: "person@example.com",
          action: "datastream.published",
          outcome: "success",
          governed_target: "datastream:ds_1",
        })],
        coverage: { state: "available", returned: 1, total: 1, bound: 200, index_state: "complete", adapters: [] },
      }))],
      [/governance\/evidence/, ok(collection("lineage-provenance"))],
    ]);

    open(`${EVIDENCE}/lens/lineage-provenance`);
    await waitFor(() => expect(screen.getByRole("columnheader", { name: "Horizon" })).toBeInTheDocument());
    expect(screen.getByRole("columnheader", { name: "Completeness" })).toBeInTheDocument();
    expect(screen.getByText("Linked chain")).toBeInTheDocument();
    // The generic seven-column table is gone: "Used by" was one of its headers.
    expect(screen.queryByRole("columnheader", { name: "Used by" })).toBeNull();

    cleanup();
    open(`${EVIDENCE}/lens/audit-activity`);
    await waitFor(() => expect(screen.getByRole("columnheader", { name: "Outcome" })).toBeInTheDocument());
    expect(screen.getByRole("columnheader", { name: "Actor" })).toBeInTheDocument();
    expect(screen.queryByRole("columnheader", { name: "Horizon" })).toBeNull();
  });

  it("never shows a provider account or connection reference on Audit Activity", async () => {
    serve([
      [/governance\/evidence/, ok(collection("audit-activity", {
        items: [evidenceItem("audit-event", "evr_a1", "audit event audit_1", {
          record_kind: "audit_event",
          actor: "person@example.com",
          action: "datastream.published",
          outcome: "success",
        })],
        coverage: { state: "available", returned: 1, total: 1, bound: 200, index_state: "complete", adapters: [] },
      }))],
    ]);
    open(`${EVIDENCE}/lens/audit-activity`);
    await waitFor(() => expect(screen.getByText("datastream.published")).toBeInTheDocument());
    // The shape a leak would actually take: the owner's own column names.
    // Asserting on the prose would only prove the copy never says the words.
    expect(document.body.innerHTML).not.toMatch(/provider_account|connection_ref/);
    expect(document.body.innerHTML).not.toMatch(/idempotency_key_hash/);
  });

  it("reads with no-store and refuses a response belonging to another Project", async () => {
    const calls = serve([
      [/governance\/evidence/, ok({ ...collection("lineage-provenance"), project_ref: { object_type: "project", id: "proj_OTHER" } })],
    ]);
    open(`${EVIDENCE}/lens/lineage-provenance`);
    // Refused, not rendered: a screen that displays whatever arrived is how a
    // neighbouring Project's evidence appears under this Project's address.
    await waitFor(() => expect(screen.getByText(/Evidence is unavailable/i)).toBeInTheDocument());
    expect(calls[0].init?.cache).toBe("no-store");
  });

  it("separates an empty lens from an index that has not finished reading", async () => {
    serve([
      [/governance\/evidence/, ok(collection("lineage-provenance", {
        coverage: { state: "empty", returned: 0, total: null, bound: 200, index_state: "backfilling", adapters: [] },
        unavailable_reasons: [{ code: "index_backfilling", message: "The Evidence index has not finished registering every eligible owner artifact for this Project." }],
      }))],
    ]);
    open(`${EVIDENCE}/lens/lineage-provenance`);
    await waitFor(() =>
      expect(screen.getByText(/has not finished registering this Project/i)).toBeInTheDocument(),
    );
    // A total nobody could prove is not printed as a number.
    expect(screen.getByText(/The total could not be proven for this caller/i)).toBeInTheDocument();
  });

  it("names a future owner as unavailable instead of counting it as zero", async () => {
    serve([
      [/governance\/evidence/, ok(collection("lineage-provenance", {
        coverage: {
          state: "empty",
          returned: 0,
          total: 0,
          bound: 200,
          index_state: "complete",
          adapters: [
            { producer: "analyze_render", label: "Analyze Render", state: "unavailable", owner_workspace: "analyze", reason_code: "render_owner_not_delivered" },
            { producer: "data_execution", label: "Datastream execution", state: "idle", owner_workspace: "data", indexed_count: 0 },
          ],
        },
      }))],
    ]);
    open(`${EVIDENCE}/lens/lineage-provenance`);
    await waitFor(() => expect(screen.getByText("Analyze Render")).toBeInTheDocument());
    const row = screen.getByText("Analyze Render").closest("tr") as HTMLElement;
    expect(within(row).getByText("unavailable")).toBeInTheDocument();
    expect(within(row).getByText("render_owner_not_delivered")).toBeInTheDocument();
    // "—", not "0": nobody indexed it, so nobody counted it.
    expect(within(row).getByText("—")).toBeInTheDocument();
  });
});

describe("Evidence filters are the address", () => {
  it("puts a chosen filter in the URL and sends it to the server", async () => {
    const calls = serve([[/governance\/evidence/, () => ok(collection("audit-activity"))]]);
    open(`${EVIDENCE}/lens/audit-activity`);
    await waitFor(() => expect(screen.getByTestId("evidence-filters")).toBeInTheDocument());

    // Committed on Enter, not per keystroke: the address never holds a
    // half-typed word, and the server is asked once.
    await userEvent.type(screen.getByLabelText("Outcome"), "success{Enter}");

    await waitFor(() => expect(window.location.search).toContain("outcome=success"));
    await waitFor(() =>
      expect(calls.some((call) => call.url.includes("outcome=success"))).toBe(true),
    );
  });

  it("drops a cursor when a filter changes, because the two are minted together", async () => {
    serve([[/governance\/evidence/, () => ok(collection("audit-activity", { next_cursor: "Y3Vyc29y" }))]]);
    window.history.replaceState({}, "", `${EVIDENCE}/lens/audit-activity?cursor=Y3Vyc29y`);
    render(
      <RouterProvider>
        <ContentRouter />
      </RouterProvider>,
    );
    await waitFor(() => expect(screen.getByTestId("evidence-filters")).toBeInTheDocument());
    expect(window.location.search).toContain("cursor=");

    await userEvent.type(screen.getByLabelText("Outcome"), "s{Enter}");

    await waitFor(() => expect(window.location.search).not.toContain("cursor="));
  });

  it("offers the record kind the route declares, and only the one this lens indexes", async () => {
    // `EVIDENCE_QUERY` declares `record_kind` and no control offered it. The
    // server accepts exactly one value per lens (`LENS_RECORD_KIND`) and raises
    // for the other two, so a three-option menu would have offered two 400s.
    const calls = serve([[/governance\/evidence/, () => ok(collection("lineage-provenance"))]]);
    open(`${EVIDENCE}/lens/lineage-provenance`);
    await waitFor(() => expect(screen.getByTestId("evidence-filters")).toBeInTheDocument());

    const select = screen.getByLabelText("Record kind");
    expect(within(select).getAllByRole("option").map((option) => (option as HTMLOptionElement).value))
      .toEqual(["", "evidence_trace"]);

    await userEvent.selectOptions(select, "evidence_trace");

    await waitFor(() => expect(window.location.search).toContain("record_kind=evidence_trace"));
    await waitFor(() =>
      expect(calls.some((call) => call.url.includes("record_kind=evidence_trace"))).toBe(true),
    );
  });

  it("offers the vocabulary the ENVELOPE declares, not the one this file remembers", async () => {
    // `evidence_index.py` validates every filter against its own tuples and now
    // declares them (`filter_options`). The browser copy was the defect: it went
    // stale the moment the server's list moved. Here the server declares a
    // workspace this build has never heard of and drops two correlation kinds it
    // has — both readings must follow the server, or the menu is offering 400s.
    serve([[/governance\/evidence/, () => ok(collection("lineage-provenance", {
      filter_options: {
        owner_workspaces: ["data", "governance", "workshop"],
        correlation_kinds: ["operation", "pull"],
        record_kinds: ["evidence_trace"],
      },
    }))]]);
    open(`${EVIDENCE}/lens/lineage-provenance`);
    await waitFor(() => expect(screen.getByTestId("evidence-filters")).toBeInTheDocument());

    const workspaces = within(screen.getByLabelText("Owner workspace")).getAllByRole("option");
    expect(workspaces.map((option) => (option as HTMLOptionElement).value))
      .toEqual(["", "data", "governance", "workshop"]);
    // A slug the console has not named yet is still OFFERED, under its own word:
    // hiding it would narrow the vocabulary the server just declared. What it is
    // NOT offered under is the base's spelling -- `wireWord` takes the
    // punctuation off and changes nothing else, which is the third of the three
    // honest answers `ui/glossary.ts` enumerates. The option's VALUE, asserted
    // above, is the slug untouched: the address does not move with the word.
    expect(workspaces.find((option) => (option as HTMLOptionElement).value === "workshop")!.textContent)
      .toBe("Workshop");
    // ...and a served slug the console HAS named still reads as English.
    expect(workspaces.find((option) => (option as HTMLOptionElement).value === "governance")!.textContent)
      .toBe("Governance");

    const kinds = within(screen.getByLabelText("Correlation kind")).getAllByRole("option");
    expect(kinds.map((option) => (option as HTMLOptionElement).value)).toEqual(["", "operation", "pull"]);
    // `virtual_pull` is in this file's fallback and NOT in the served list. A
    // menu that merged the two would keep proposing a refused narrowing.
    expect(kinds.some((option) => (option as HTMLOptionElement).value === "virtual_pull")).toBe(false);
  });

  it("takes the lens's record kind from the served list", async () => {
    serve([[/lens=versions-approvals/, () => ok(collection("versions-approvals", {
      filter_options: { owner_workspaces: ["data"], correlation_kinds: ["operation"], record_kinds: ["object_version"] },
    }))], [/governance\/evidence/, () => ok(collection("versions-approvals"))]]);
    open(`${EVIDENCE}/lens/versions-approvals`);
    await waitFor(() => expect(screen.getByTestId("evidence-filters")).toBeInTheDocument());

    const select = screen.getByLabelText("Record kind");
    const options = within(select).getAllByRole("option");
    expect(options.map((option) => (option as HTMLOptionElement).value)).toEqual(["", "object_version"]);
    expect(options[1].textContent).toBe("Object version only");
  });

  it("still draws a usable bar against an envelope that declares no vocabulary", async () => {
    // An older envelope — a cached page, a deployment mid-way through. The
    // remembered lists stand in; three empty menus would be worse than a stale
    // one, because a person could then undo nothing.
    serve([[/governance\/evidence/, () => ok(collection("lineage-provenance"))]]);
    open(`${EVIDENCE}/lens/lineage-provenance`);
    await waitFor(() => expect(screen.getByTestId("evidence-filters")).toBeInTheDocument());

    expect(within(screen.getByLabelText("Owner workspace")).getAllByRole("option").length).toBe(6);
    expect(within(screen.getByLabelText("Correlation kind")).getAllByRole("option").length).toBe(13);
    expect(within(screen.getByLabelText("Record kind")).getAllByRole("option").map((option) => (option as HTMLOptionElement).value))
      .toEqual(["", "evidence_trace"]);
  });

  it("names every filter entry in the person's words, with the wire slug still reachable", async () => {
    serve([[/governance\/evidence/, () => ok(collection("lineage-provenance"))]]);
    open(`${EVIDENCE}/lens/lineage-provenance`);
    await waitFor(() => expect(screen.getByTestId("evidence-filters")).toBeInTheDocument());

    // `virtual_pull` and `ai_path` were offered raw. The menu now says what they
    // are; the slug survives as the option's title, because a person debugging
    // a shared address still needs to map one to the other.
    const kinds = within(screen.getByLabelText("Correlation kind")).getAllByRole("option");
    const virtualPull = kinds.find((option) => (option as HTMLOptionElement).value === "virtual_pull")!;
    expect(virtualPull.textContent).toBe("Virtual Run");
    expect(virtualPull.getAttribute("title")).toBe("virtual_pull");

    const workspaces = within(screen.getByLabelText("Owner workspace")).getAllByRole("option");
    const contextHub = workspaces.find((option) => (option as HTMLOptionElement).value === "context-hub")!;
    expect(contextHub.textContent).toBe("Context Hub");
  });
});

/** jsdom's Blob has no `.text()`; FileReader is what it does have. */
function blobText(blob: Blob): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result));
    reader.onerror = () => reject(reader.error);
    reader.readAsText(blob);
  });
}

describe("the page on screen can be taken away as a file", () => {
  it("writes exactly the visible rows, under the visible column headers", async () => {
    serve([
      [/governance\/evidence/, () => ok(collection("lineage-provenance", {
        items: [
          evidenceItem("evidence-trace", "evr_1", "datastream execution dse_1", {
            linked_record_count: 3,
            owner_reference_count: 2,
            completeness: "linked",
            correlations: [{ kind: "execution", id: "dse_1" }],
          }),
          evidenceItem("evidence-trace", "evr_2", "publication pub_9", {
            linked_record_count: 0,
            owner_reference_count: 0,
            completeness: "partial",
            availability: "owner_unavailable",
          }),
        ],
        coverage: { state: "available", returned: 2, total: 2, bound: 200, index_state: "complete", adapters: [] },
      }))],
    ]);
    open(`${EVIDENCE}/lens/lineage-provenance`);
    await waitFor(() => expect(screen.getByTestId("evidence-export")).toBeInTheDocument());

    const written: Blob[] = [];
    vi.stubGlobal("URL", {
      ...URL,
      createObjectURL: vi.fn((blob: Blob) => {
        written.push(blob);
        return "blob:evidence";
      }),
      revokeObjectURL: vi.fn(),
    });
    const clicked = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});

    const button = screen.getByTestId("evidence-export");
    // The count is IN the label: this file is one page, and it says so.
    expect(button.textContent).toBe("Export this page (2 rows)");
    await userEvent.click(button);

    const csv = await blobText(written[0]);
    const lines = csv.split("\r\n");
    // The header row is the table's own header row, in order.
    expect(lines[0]).toBe("Trace,Owner,Horizon,Linked steps,Correlation,Completeness,Availability");
    expect(lines).toHaveLength(3);
    // And every cell is what the row above RENDERED, not a second formatting of
    // the same summary: the export reads the exact strings the table printed.
    const firstRow = screen.getAllByRole("row")[1];
    for (const cell of within(firstRow).getAllByRole("cell")) {
      expect(lines[1]).toContain(cell.textContent?.trim());
    }
    expect(lines[1]).toContain("execution:dse_1");
    expect(lines[2]).toContain("owner unavailable");

    const anchor = clicked.mock.instances[0] as unknown as HTMLAnchorElement;
    expect(anchor.download).toBe("evidence_lineage-provenance_2-rows.csv");
  });

  it("carries the filters in the file name, so two narrowings never look alike", async () => {
    serve([
      [/governance\/evidence/, () => ok(collection("audit-activity", {
        items: [evidenceItem("audit-event", "aud_1", "publish semantic view", {
          record_kind: "audit_event",
          actor: "owner@example.com",
          action: "publish",
          outcome: "success",
          governed_target: "semantic-view svw_1",
        })],
        coverage: { state: "available", returned: 1, total: 1, bound: 200, index_state: "complete", adapters: [] },
      }))],
    ]);
    open(`${EVIDENCE}/lens/audit-activity?outcome=success&correlation_kind=audit&correlation_id=aud_1`);
    await waitFor(() => expect(screen.getByTestId("evidence-export")).toBeInTheDocument());

    vi.stubGlobal("URL", { ...URL, createObjectURL: vi.fn(() => "blob:evidence"), revokeObjectURL: vi.fn() });
    const clicked = vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});

    await userEvent.click(screen.getByTestId("evidence-export"));

    const anchor = clicked.mock.instances[0] as unknown as HTMLAnchorElement;
    expect(anchor.download).toBe(
      "evidence_audit-activity_correlation_id-aud_1_correlation_kind-audit_outcome-success_1-rows.csv",
    );
  });
});

describe("the three Evidence workbenches", () => {
  const trace = evidenceItem("evidence-trace", "evr_1", "datastream execution dse_1", {
    trace: {
      anchor: { record_id: "evr_1", producer: "data_execution", owner_object_type: "datastream-execution", owner_object_id: "dse_1" },
      nodes: [
        {
          record_id: "evr_1",
          producer: "data_execution",
          owner_workspace: "data",
          owner_object_type: "datastream-execution",
          owner_object_id: "dse_1",
          owner_version_id: null,
          is_anchor: true,
          occurred_at: "2026-07-30T07:00:00Z",
          observed_at: "2026-07-30T07:00:00Z",
          integrity_hash: "a".repeat(64),
          availability: "available",
          correlations: [{ kind: "execution", id: "dse_1" }],
          owner_href: { surface: "project", workspace: "data", section: "datastreams", object_type: "datastream", object_id: "ds_1", tab: "runs" },
        },
        {
          record_id: "evr_2",
          producer: "data_execution_stage",
          owner_workspace: "data",
          owner_object_type: "datastream-execution-stage",
          owner_object_id: "dsse_1",
          owner_version_id: null,
          is_anchor: false,
          occurred_at: "2026-07-30T08:00:00Z",
          observed_at: null,
          integrity_hash: null,
          availability: "owner_unavailable",
          correlations: [],
          owner_href: null,
        },
      ],
      edges: [{ id: "evl_1", relation: "anchored_by", from: "evr_2", to: "evr_1", ordinal: 1, integrity_hash: null }],
      owner_references: [],
      roots: ["evr_2"],
      terminals: ["evr_1"],
      coverage: "partial",
      unavailable_reasons: [{ code: "single_node_chain", message: "Only this record proves anything here." }],
    },
  });

  it("shows Overview, Lineage and Provenance as three different projections", async () => {
    serve([
      [/objects\/evidence-trace\/evr_1/, ok(objectEnvelope("evidence-trace", trace))],
      [/governance\/evidence/, ok(collection("lineage-provenance"))],
    ]);
    open(`${EVIDENCE}/object/evidence-trace/evr_1/tab/overview`);
    await waitFor(() => expect(screen.getByText("Evidence horizon")).toBeInTheDocument());
    expect(screen.getByText("Proven steps")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("link", { name: "Lineage" }));
    await waitFor(() => expect(screen.getByText("Owner steps")).toBeInTheDocument());
    // The proven ordinal, not a row number.
    expect(screen.getByText("Step 2")).toBeInTheDocument();
    expect(screen.getByText("owner unavailable")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("link", { name: "Provenance" }));
    await waitFor(() => expect(screen.getByText("Source and integrity")).toBeInTheDocument());
    expect(screen.getByText("execution:dse_1")).toBeInTheDocument();
  });

  it("refuses to invent a Diff when no predecessor is pinned", async () => {
    const version = evidenceItem("object-version", "evr_v1", "rule set version grsv_2", {
      record_kind: "object_version",
      owner_object_type: "rule-set-version",
      owner_version_id: "grsv_2",
      version_detail: {
        diff: {
          state: "unavailable",
          reason_code: "no_pinned_predecessor",
          message: "No predecessor is pinned to this version by its owner.",
        },
        approvals: { state: "empty", items: [] },
        used_by: { state: "empty", items: [] },
      },
    });
    serve([
      [/objects\/object-version\/evr_v1/, ok(objectEnvelope("object-version", version))],
      [/governance\/evidence/, ok(collection("versions-approvals"))],
    ]);
    open(`${EVIDENCE}/object/object-version/evr_v1/tab/diff`);
    await waitFor(() => expect(screen.getByText("Diff is unavailable")).toBeInTheDocument());
    expect(screen.getByText(/has NOT been searched for/i)).toBeInTheDocument();
    expect(document.body.textContent).not.toMatch(/latest version/i);
  });

  it("compares only the two exact versions when a predecessor IS pinned", async () => {
    const version = evidenceItem("object-version", "evr_v2", "rule set version grsv_3", {
      record_kind: "object_version",
      owner_object_type: "rule-set-version",
      owner_version_id: "grsv_3",
      version_detail: {
        diff: {
          state: "available",
          selected_version_id: "grsv_3",
          predecessor_version_id: "grsv_2",
          predecessor_record_id: "evr_v1",
          selected_integrity_hash: "a".repeat(64),
          predecessor_integrity_hash: "b".repeat(64),
          changed: true,
          owner_href: null,
        },
        approvals: { state: "available", items: [{ approval_id: "rsa_1", owner_workspace: "governance", owner_object_type: "rule-set-version-approval" }] },
        used_by: { state: "empty", items: [] },
      },
    });
    serve([
      [/objects\/object-version\/evr_v2/, ok(objectEnvelope("object-version", version))],
      [/governance\/evidence/, ok(collection("versions-approvals"))],
    ]);
    open(`${EVIDENCE}/object/object-version/evr_v2/tab/diff`);
    await waitFor(() => expect(screen.getByTestId("version-diff-verdict")).toBeInTheDocument());
    expect(screen.getByText("grsv_2")).toBeInTheDocument();
    expect(screen.getByText("grsv_3")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("link", { name: "Approvals" }));
    await waitFor(() => expect(screen.getByText("rsa_1")).toBeInTheDocument());
  });

  it("opens an Audit Event on its own complete Overview", async () => {
    const audit = evidenceItem("audit-event", "evr_a1", "audit event audit_1", {
      record_kind: "audit_event",
      audit_detail: {
        state: "available",
        audit_id: "audit_1",
        actor: "person@example.com",
        action: "datastream.published",
        outcome: "success",
        occurred_at: "2026-07-30T08:00:00Z",
        resource_path: ["datastream:ds_1"],
        correlation: { trace_id: "a".repeat(32), operation_id: "op_1" },
        versions: { policy_version: "p1" },
        hashes: { after_hash: "c".repeat(64) },
      },
    });
    serve([
      [/objects\/audit-event\/evr_a1/, ok(objectEnvelope("audit-event", audit))],
      [/governance\/evidence/, ok(collection("audit-activity"))],
    ]);
    open(`${EVIDENCE}/object/audit-event/evr_a1/tab/overview`);
    await waitFor(() => expect(screen.getByText("Authorized resource")).toBeInTheDocument());
    expect(screen.getByText("datastream:ds_1")).toBeInTheDocument();
    expect(screen.getByText("a".repeat(32))).toBeInTheDocument();
    expect(document.body.innerHTML).not.toMatch(/provider_account|connection_ref/);
  });

  it("keeps an unknown Evidence Record unknown instead of opening a neighbour", async () => {
    serve([
      [/objects\/evidence-trace\/evr_missing/, fail(404, "not_found")],
      [/governance\/evidence/, ok(collection("lineage-provenance"))],
    ]);
    open(`${EVIDENCE}/object/evidence-trace/evr_missing/tab/overview`);
    await waitFor(() =>
      expect(screen.getByText(/Nothing similar has been opened in its place/i)).toBeInTheDocument(),
    );
  });

  it("no longer marks any Evidence tab as owned by a later story", async () => {
    const { PENDING_TAB_OWNER } = await import("../governance/contracts");
    const evidenceTabs = Object.keys(PENDING_TAB_OWNER).filter((key) =>
      key.startsWith("evidence-trace:") || key.startsWith("object-version:") || key.startsWith("audit-event:"),
    );
    expect(evidenceTabs).toEqual([]);
    // This used to assert that `business-domain:` markers survived, as proof
    // that removing the Evidence keys had not swept the others away. Story 49.2
    // has since delivered MasterDataTabs.tsx and removed those six legitimately,
    // so the map is now empty -- every contracted Governance tab has an owner
    // that ships it. The invariant worth keeping is the narrow one: this removal
    // took the Evidence keys and nothing was left half-removed.
    expect(Object.keys(PENDING_TAB_OWNER)).toEqual([]);
  });
});
