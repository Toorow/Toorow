import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { DatastreamPublicationDialog, DatastreamRollbackDialog } from "../datastreams/workbench/DatastreamPublicationDialog";
import DatastreamRecoveryDialog from "../datastreams/workbench/DatastreamRecoveryDialog";
// The registry and the join on it, read rather than restated: the whole point of
// `unbuiltRecoveryVerbs` is that a verb whose engine lands leaves the refusal
// list with no edit, and a test naming the three verbs made that untrue.
import { unbuiltRecoveryVerbs } from "../datastreams/workbench/boundedRecovery";
import { RUN_ORIGINS } from "../datastreams/workbench/runOrigins";

function response(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

beforeEach(() => {
  vi.restoreAllMocks();
  vi.stubGlobal("crypto", { randomUUID: () => "idem-1" });
});

describe("Datastream contextual operations", () => {
  it("offers only server-proven recovery and reconciles unknown work without retrying", async () => {
    const fetchMock = vi.fn().mockResolvedValue(response({ outcome: "reconciled" }));
    vi.stubGlobal("fetch", fetchMock);
    const confirmed = vi.fn();
    render(
      <DatastreamRecoveryDialog
        projectId="proj-1"
        datastreamId="ds-1"
        run={{
          id: "dse-1",
          recovery: {
            kinds: ["reconcile"],
            interval: { from: "2026-07-01", to_exclusive: "2026-07-02" },
          },
        }}
        onConfirmed={confirmed}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Prepare recovery" }));
    fireEvent.click(screen.getByRole("button", { name: "Reconcile" }));
    await waitFor(() => expect(confirmed).toHaveBeenCalledOnce());
    expect(fetchMock).toHaveBeenCalledOnce();
    expect(String(fetchMock.mock.calls[0][0])).toBe("/api/datastreams/ds-1/executions/dse-1/reconcile");
  });

  it("says WHY a failed run cannot be repaired, verb by verb, instead of blaming the run", async () => {
    // The server offers only verbs its own preflight proved, and it proves none
    // of the three bounded ones — so the list is empty on EVERY failed run and
    // the button used to read `Recovery ineligible`. That blames the run for a
    // gap in the build, names no verb, and leaves nowhere to go.
    vi.stubGlobal("fetch", vi.fn());
    render(
      <DatastreamRecoveryDialog
        projectId="proj-1"
        datastreamId="ds-1"
        run={{
          id: "dse-1",
          recovery: {
            kinds: [],
            refusals: { synchronize: "no engine", reload: "no engine", reprocess: "no engine" },
            interval: { from: "2026-07-01", to_exclusive: "2026-07-02" },
          },
        }}
        onConfirmed={vi.fn()}
      />,
    );

    const opener = screen.getByTestId("recovery-open");
    expect(opener.textContent).toBe("Why this cannot be repaired");
    expect(opener.hasAttribute("disabled")).toBe(false);
    fireEvent.click(opener);

    // THE SAME STANDINGS THE RUNS TAB STATES, FROM THE SAME COMPONENT — and the
    // list is the REGISTRY'S, read on `has_engine`, never three names typed
    // here. AMENDED 2026-08-18: this loop named `bounded_reprocess`, which left
    // the unbuilt list on 2026-08-17 (chantier 67-15b) when its engine landed.
    // A test that pins the CONTENT of a registry-driven list has to be edited
    // every time the build changes, which is the opposite of why the panel reads
    // the registry — so it asserts the JOIN instead, and cannot go stale again.
    const unbuilt = unbuiltRecoveryVerbs().map((verb) => verb.origin.key);
    expect(unbuilt).toContain("bounded_synchronize");
    for (const key of unbuilt) {
      expect(screen.getByTestId(`recovery-verb-${key}`)).toBeTruthy();
    }
    // A verb whose engine landed is NOT offered here as a refusal.
    for (const entry of RUN_ORIGINS.filter((origin) => origin.has_engine)) {
      expect(screen.queryByTestId(`recovery-verb-${entry.key}`)).toBeNull();
    }
    expect(screen.getByTestId("recovery-verb-bounded_reload").textContent).toContain("Day-by-day coverage");
  });

  it("says `Nothing to repair` for a run that ended well, which is a different fact", async () => {
    // No preflight ran, so there are no per-verb refusals to explain: the run is
    // fine. A word about missing engines here would be noise on a healthy run.
    vi.stubGlobal("fetch", vi.fn());
    render(
      <DatastreamRecoveryDialog
        projectId="proj-1"
        datastreamId="ds-1"
        run={{ id: "dse-2", recovery: { kinds: [], interval: null } }}
        onConfirmed={vi.fn()}
      />,
    );

    const opener = screen.getByTestId("recovery-open");
    expect(opener.textContent).toBe("Nothing to repair");
    expect(opener.hasAttribute("disabled")).toBe(true);
    expect(screen.queryByTestId("recovery-verb-bounded_reprocess")).toBeNull();
  });

  it("reviews and atomically publishes one unchanged later candidate", async () => {
    const hash = "a".repeat(64);
    const fetchMock = vi.fn().mockImplementation((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith("/candidate-review")) return Promise.resolve(response({
        execution_id: "dse-later",
        plan_version_id: "dsp-2",
        mapping_version_id: "dmap-2",
        artifact_ref: "artifact:dse-later",
        expected_current_execution_id: "dse-current",
        review_hash: hash,
        dq: { state: "passed" },
        output_plan: [{ kind: "full_grain" }],
        schedule: { cadence: "daily" },
      }));
      if (url.endsWith("/publish-confirmations")) return Promise.resolve(response({
        confirmation_ref: "dsc-1",
        confirmation_secret: "secret",
        review_hash: hash,
      }, 201));
      if (url.endsWith("/publish-activate")) return Promise.resolve(response({ outcome: "succeeded" }));
      throw new Error(`Unexpected ${url}`);
    });
    vi.stubGlobal("fetch", fetchMock);
    const confirmed = vi.fn();
    render(
      <DatastreamPublicationDialog
        projectId="proj-1"
        datastreamId="ds-1"
        candidateId="dse-later"
        onConfirmed={confirmed}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Review candidate" }));
    fireEvent.click(await screen.findByRole("button", { name: "Prepare publication confirmation" }));
    fireEvent.click(await screen.findByRole("button", { name: "Confirm publish atomically" }));
    await waitFor(() => expect(confirmed).toHaveBeenCalledOnce());
    expect(fetchMock.mock.calls.map(([url]) => String(url))).toEqual([
      "/api/projects/proj-1/datastreams/ds-1/executions/dse-later/candidate-review",
      "/api/projects/proj-1/datastreams/ds-1/executions/dse-later/publish-confirmations",
      "/api/projects/proj-1/datastreams/ds-1/executions/dse-later/publish-activate",
    ]);
  });

  it("prepares and confirms an exact compatible rollback set", async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(response({
        preparation_id: "dsrp-1",
        confirmation_secret: "secret",
        review: {
          target_execution_id: "dse-lkg",
          target_plan_version_id: "dsp-1",
          target_mapping_version_id: "dmap-1",
          target_schedule: { cadence: "daily" },
        },
      }, 201))
      .mockResolvedValueOnce(response({ rolled_back_to: "dse-lkg" }));
    vi.stubGlobal("fetch", fetchMock);
    const confirmed = vi.fn();
    render(
      <DatastreamRollbackDialog
        projectId="proj-1"
        datastreamId="ds-1"
        targetExecutionId="dse-lkg"
        onConfirmed={confirmed}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Prepare rollback" }));
    fireEvent.click(await screen.findByRole("button", { name: "Confirm exact rollback" }));
    await waitFor(() => expect(confirmed).toHaveBeenCalledOnce());
    expect(fetchMock.mock.calls.map(([url]) => String(url))).toEqual([
      "/api/projects/proj-1/datastreams/ds-1/workbench/outputs/rollback-preparations",
      "/api/projects/proj-1/datastreams/ds-1/workbench/outputs/rollback-preparations/dsrp-1/confirm",
    ]);
  });

  it("reads the candidate, rollback and recovery evidence as fields, never as a JSON dump", async () => {
    // Finding H-6: three of the four governed confirmation reviews rendered
    // `<pre>{JSON.stringify(...)}` — publication, rollback and recovery.
    const hash = "a".repeat(64);
    vi.stubGlobal("fetch", vi.fn().mockImplementation((input: RequestInfo | URL) => {
      const url = String(input);
      if (url.endsWith("/candidate-review")) return Promise.resolve(response({
        execution_id: "dse-later",
        plan_version_id: "dsp-2",
        mapping_version_id: "dmap-2",
        artifact_ref: "artifact:dse-later",
        expected_current_execution_id: "dse-current",
        review_hash: hash,
        dq: { state: "passed", blocking_rules: 0 },
        output_plan: { kind: "full_grain", relation: "marts.orders" },
        schedule: { cadence: "daily", timezone: "Europe/Paris" },
      }));
      throw new Error(`Unexpected ${url}`);
    }));
    const { unmount } = render(
      <DatastreamPublicationDialog
        projectId="proj-1"
        datastreamId="ds-1"
        candidateId="dse-later"
        onConfirmed={vi.fn()}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Review candidate" }));
    expect(await screen.findByText("Data quality outcome")).toBeInTheDocument();
    expect(screen.getByText("Output plan")).toBeInTheDocument();
    expect(screen.getByText("Cadence after publication")).toBeInTheDocument();
    // Each governed record reads field by field.
    expect(screen.getByText("State")).toBeInTheDocument();
    expect(screen.getByText("Blocking rules")).toBeInTheDocument();
    expect(screen.getByText("marts.orders")).toBeInTheDocument();
    expect(screen.getByText("Europe/Paris")).toBeInTheDocument();
    expect(document.querySelector("pre")).toBeNull();
    unmount();

    // Rollback: the exact consequence, read before it is confirmed.
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response({
      preparation_id: "dsrb-1",
      confirmation_secret: "one-time",
      review: {
        target_execution_id: "dse-previous",
        restores: { plan_version_id: "dsp-1", mapping_version_id: "dmap-1" },
        lifecycle: "Active",
      },
    })));
    render(
      <DatastreamRollbackDialog
        projectId="proj-1"
        datastreamId="ds-1"
        targetExecutionId="dse-previous"
        onConfirmed={vi.fn()}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Prepare rollback" }));
    expect(await screen.findByText("Target execution id")).toBeInTheDocument();
    expect(screen.getByText("Restores · Plan version id")).toBeInTheDocument();
    expect(screen.getByText("dsp-1")).toBeInTheDocument();
    expect(document.querySelector("pre")).toBeNull();
    expect(screen.getByRole("button", { name: "Confirm exact rollback" })).toBeInTheDocument();
  });

  it("refuses to offer a rollback whose exact consequence was not returned", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response({
      preparation_id: "dsrb-2",
      confirmation_secret: "one-time",
    })));
    render(
      <DatastreamRollbackDialog
        projectId="proj-1"
        datastreamId="ds-1"
        targetExecutionId="dse-previous"
        onConfirmed={vi.fn()}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Prepare rollback" }));
    expect(await screen.findByText("Rollback evidence unavailable")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Confirm exact rollback" })).not.toBeInTheDocument();
  });

  it("freezes the recovery scope as readable fields before one durable operation", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response({
      preparation_id: "dsrp-1",
      operation: "reload",
      scope: { from: "2026-07-01", to_exclusive: "2026-07-02" },
      expected_current_execution_id: "dse-current",
      idempotency_key: "idem-1",
    })));
    render(
      <DatastreamRecoveryDialog
        projectId="proj-1"
        datastreamId="ds-1"
        run={{ id: "dse-1", recovery: { kinds: ["reload"], interval: { from: "2026-07-01", to_exclusive: "2026-07-02" } } }}
        onConfirmed={vi.fn()}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Prepare recovery" }));
    fireEvent.click(screen.getByRole("button", { name: "Reload" }));
    expect(await screen.findByText("Scope · From")).toBeInTheDocument();
    expect(screen.getByText("Expected current execution id")).toBeInTheDocument();
    expect(screen.getByText("Idempotency key")).toBeInTheDocument();
    expect(document.querySelector("pre")).toBeNull();
  });
});
