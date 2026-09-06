import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import DatastreamChangeDialog from "../datastreams/workbench/DatastreamChangeDialog";

function response(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

describe("DatastreamChangeDialog", () => {
  it("prepares an exact review then confirms one non-live candidate", async () => {
    const fetchMock = vi.fn().mockImplementation((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith("/mapping/changes")) return Promise.resolve(response({
        preparation_id: "dscp_1",
        confirmation_secret: "one-time",
        review_hash: "a".repeat(64),
        expires_in_seconds: 900,
        review: {
          diff: [{ path: "$.fields", before_hash: "b".repeat(64), after_hash: "c".repeat(64) }],
          consequence: "Append immutable non-live versions and dispatch one candidate",
          expected_plan_version_id: "dsp_1",
          expected_mapping_version_id: "dmap_1",
        },
      }, 201));
      if (url.endsWith("/changes/dscp_1/confirm")) return Promise.resolve(response({
        candidate_execution_id: "dse_1",
        active_versions_unchanged: true,
      }));
      throw new Error(`Unexpected ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    const confirmed = vi.fn();
    render(
      <DatastreamChangeDialog
        projectId="proj_1"
        datastreamId="ds_1"
        kind="mapping"
        initialPayload={{ fields: [{ field_id: "date" }] }}
        rawImportId="inbraw_01EXAMPLE0000000000000000"
        onConfirmed={confirmed}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Prepare mapping change" }));
    const editor = await screen.findByLabelText("Proposed mapping contract");
    fireEvent.change(editor, { target: { value: JSON.stringify({ fields: [{ field_id: "date", included: false }] }) } });
    fireEvent.click(screen.getByRole("button", { name: "Prepare exact review" }));
    expect(await screen.findByText("Expected active plan")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Confirm and dispatch candidate" }));
    expect(await screen.findByText("Candidate dispatched")).toBeInTheDocument();
    expect(confirmed).toHaveBeenCalledOnce();
    expect(fetchMock.mock.calls.map(([url]) => String(url))).toEqual([
      "/api/projects/proj_1/datastreams/ds_1/workbench/mapping/changes",
      "/api/projects/proj_1/datastreams/ds_1/workbench/changes/dscp_1/confirm",
    ]);
    expect(JSON.parse(String(fetchMock.mock.calls[0][1]?.body))).toEqual({
      proposed_payload: { fields: [{ field_id: "date", included: false }] },
      raw_import_id: "inbraw_01EXAMPLE0000000000000000",
    });
  });

  it("does not offer a change without an exact active contract", () => {
    render(
      <DatastreamChangeDialog
        projectId="proj_1"
        datastreamId="ds_1"
        kind="processing"
        initialPayload={null}
        onConfirmed={vi.fn()}
      />,
    );
    expect(screen.getByRole("button", { name: "Prepare processing change" })).toBeDisabled();
  });

  it("shows the contract diff path by path, never as a JSON dump", async () => {
    // Finding H-6: the four governed confirmation reviews rendered
    // `<pre>{JSON.stringify(...)}`. An operator confirming an irreversible
    // change has to read what changes, path by path.
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response({
      preparation_id: "dscp_2",
      confirmation_secret: "one-time",
      review_hash: "a".repeat(64),
      expires_in_seconds: 900,
      review: {
        diff: [
          { path: "$.selection.metrics", before_hash: "b".repeat(64), after_hash: "c".repeat(64) },
          { path: "$.schedule.interval_minutes", before_hash: "d".repeat(64), after_hash: "" },
        ],
        consequence: "Append immutable non-live versions and dispatch one candidate",
        expected_plan_version_id: "dsp_1",
        expected_mapping_version_id: "dmap_1",
      },
    }, 201)));
    render(
      <DatastreamChangeDialog
        projectId="proj_1"
        datastreamId="ds_1"
        kind="processing"
        initialPayload={{ schedule: { interval_minutes: 1440 } }}
        onConfirmed={vi.fn()}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Prepare processing change" }));
    fireEvent.click(await screen.findByRole("button", { name: "Prepare exact review" }));

    expect(await screen.findByRole("columnheader", { name: "Contract path" })).toBeInTheDocument();
    // `Base`, not `Active` — amendment 4 of the 2026-08-11 review, seam half. On
    // a Datastream with no pointer the two are different things: the base is the
    // most recent recorded version, and nothing is active. The column shows the
    // first, so it is named after the first. Measured the same day: 6 of the 8
    // live Datastreams have no mapping pointer, so `Active` was the wrong word
    // on the majority of the fleet.
    expect(screen.getByRole("columnheader", { name: "Base" })).toBeInTheDocument();
    expect(screen.getByRole("columnheader", { name: "Proposed" })).toBeInTheDocument();
    expect(screen.getByText("$.selection.metrics")).toBeInTheDocument();
    // A path with no `after` reads as removed, not as an empty string.
    expect(screen.getByText("Removed")).toBeInTheDocument();
    expect(document.querySelector("pre")).toBeNull();
  });

  it("names the base HONESTLY when nothing is in force, and says what that costs", async () => {
    // Amendment 4 of the 2026-08-11 review, seam half. The two labels read
    // « Expected active plan » / « Expected active mapping » on every Datastream,
    // and on the 6 of 8 live ones with no pointer NOTHING is active — the label
    // lied about the very object the person was being asked to approve, at the
    // one moment the product asks for an exact confirmation.
    //
    // The consequence sentence is the SERVER's and is rendered whole: confirming
    // against a head appends a version and still makes nothing live, which no
    // label can carry on its own.
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response({
      preparation_id: "dscp_4",
      confirmation_secret: "one-time",
      review_hash: "a".repeat(64),
      expires_in_seconds: 900,
      review: {
        diff: [{ path: "$.fields", before_hash: "b".repeat(64), after_hash: "c".repeat(64) }],
        consequence: "Append immutable non-live versions and dispatch one candidate",
        expected_plan_version_id: "dsp_1",
        expected_mapping_version_id: "dmap_1",
        base_versions: {
          plan: { state: "in_force", version_id: "dsp_1" },
          mapping: {
            state: "head_of_ledger",
            version_id: "dmap_1",
            reason: "no version is in force; the most recent recorded version is the base, "
              + "and confirming this change does not make it live",
          },
        },
      },
    }, 201)));
    render(
      <DatastreamChangeDialog
        projectId="proj_1"
        datastreamId="ds_1"
        kind="mapping"
        initialPayload={{ fields: [{ field_id: "date" }] }}
        onConfirmed={vi.fn()}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Prepare mapping change" }));
    fireEvent.click(await screen.findByRole("button", { name: "Prepare exact review" }));

    // The axis that IS in force keeps its word; the one that is not says so.
    expect(await screen.findByText("Expected active plan")).toBeInTheDocument();
    expect(screen.getByText("Most recent mapping (nothing in force)")).toBeInTheDocument();
    expect(screen.queryByText("Expected active mapping")).toBeNull();
    // The server's sentence, whole, and not composed here.
    expect(screen.getByTestId("base-not-in-force").textContent).toContain(
      "confirming this change does not make it live",
    );
  });

  it("shows WHAT CHANGES in values, not two hashes — 2026-08-18", async () => {
    // Measured defect: `_top_level_diff` composes one row per top-level contract
    // path with a `before_hash` and an `after_hash`, so excluding one column of
    // forty-six was confirmed against `$.fields  9f2c…  4b70…` and nothing else.
    // The server now composes the same difference in values
    // (`core/mapping_value_diff.py`); the dialog renders it ABOVE the hashes,
    // because the values are what is being approved and the hashes are the
    // identity of what was frozen.
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response({
      preparation_id: "dscp_5",
      confirmation_secret: "one-time",
      review_hash: "a".repeat(64),
      expires_in_seconds: 900,
      review: {
        diff: [{ path: "$.fields", before_hash: "b".repeat(64), after_hash: "c".repeat(64) }],
        value_diff: {
          state: "composed",
          entries: [
            {
              subject: "spend",
              subject_kind: "field",
              reading: "Landing",
              before: "Lands",
              after: "Does not land (excluded)",
            },
            {
              subject: "day",
              subject_kind: "field",
              reading: "Governed target",
              before: "No governed target",
              // The CONCEPT is named. `mdm_6D13WZ…` alone is not what a person
              // reads on the row they are confirming.
              after: "event_date (mdm_6D13WZEXAMPLE)",
            },
          ],
        },
        consequence: "Append immutable non-live versions and dispatch one candidate",
        expected_plan_version_id: "dsp_1",
        expected_mapping_version_id: "dmap_1",
      },
    }, 201)));
    render(
      <DatastreamChangeDialog
        projectId="proj_1"
        datastreamId="ds_1"
        kind="mapping"
        initialPayload={{ fields: [{ field_id: "spend" }] }}
        onConfirmed={vi.fn()}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Prepare mapping change" }));
    fireEvent.click(await screen.findByRole("button", { name: "Prepare exact review" }));

    const diff = await screen.findByTestId("change-value-diff");
    expect(diff).toHaveTextContent("spend");
    expect(diff).toHaveTextContent("Does not land (excluded)");
    expect(diff).toHaveTextContent("event_date (mdm_6D13WZEXAMPLE)");
    // The hashed reading is still there — it is what the confirmation compares.
    expect(screen.getByRole("columnheader", { name: "Contract path" })).toBeInTheDocument();
  });

  it("does not pretend a server that sends no values agrees on every reading", async () => {
    // Absent and empty are two sentences. A server that predates the composition
    // sends nothing, and the dialog must not answer "no reading changes".
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response({
      preparation_id: "dscp_6",
      confirmation_secret: "one-time",
      review_hash: "a".repeat(64),
      expires_in_seconds: 900,
      review: {
        diff: [{ path: "$.fields", before_hash: "b".repeat(64), after_hash: "c".repeat(64) }],
        consequence: "Append immutable non-live versions and dispatch one candidate",
        expected_plan_version_id: "dsp_1",
        expected_mapping_version_id: "dmap_1",
      },
    }, 201)));
    render(
      <DatastreamChangeDialog
        projectId="proj_1"
        datastreamId="ds_1"
        kind="mapping"
        initialPayload={{ fields: [{ field_id: "spend" }] }}
        onConfirmed={vi.fn()}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Prepare mapping change" }));
    fireEvent.click(await screen.findByRole("button", { name: "Prepare exact review" }));

    expect(await screen.findByText("$.fields")).toBeInTheDocument();
    expect(screen.queryByTestId("change-value-diff")).toBeNull();
    expect(screen.queryByText("No reading of this contract changes")).toBeNull();
  });

  it("names what the raw contract door costs, rather than calling it Advanced", async () => {
    // The door is KEPT: the row controls write three paths of the contract, and
    // a Datastream with no version has no row to press at all. What it lacked
    // was the risk, said in the words of the refusals it skips.
    render(
      <DatastreamChangeDialog
        projectId="proj_1"
        datastreamId="ds_1"
        kind="mapping"
        initialPayload={{ fields: [] }}
        onConfirmed={vi.fn()}
        triggerLabel="Edit raw contract…"
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Edit raw contract…" }));

    const risk = await screen.findByTestId("raw-contract-risk");
    expect(risk).toHaveTextContent("refusals arrive only at the append");
    expect(screen.getByLabelText("Proposed mapping contract")).toBeInTheDocument();
  });

  it("says so when the proposed contract changes no governed path", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response({
      preparation_id: "dscp_3",
      confirmation_secret: "one-time",
      review_hash: "a".repeat(64),
      expires_in_seconds: 900,
      review: {
        diff: [],
        consequence: "Nothing would change; confirming appends an identical version",
        expected_plan_version_id: "dsp_1",
        expected_mapping_version_id: "dmap_1",
      },
    }, 201)));
    render(
      <DatastreamChangeDialog
        projectId="proj_1"
        datastreamId="ds_1"
        kind="mapping"
        initialPayload={{ fields: [] }}
        onConfirmed={vi.fn()}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Prepare mapping change" }));
    fireEvent.click(await screen.findByRole("button", { name: "Prepare exact review" }));
    expect(await screen.findByText("No contract path changes")).toBeInTheDocument();
  });
});
