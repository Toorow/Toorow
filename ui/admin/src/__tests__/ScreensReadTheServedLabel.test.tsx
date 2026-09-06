/**
 * A LABEL IS SERVED, NEVER COMPOSED HERE — AND ITS ABSENCE IS A STATE, NOT AN ID.
 *
 * `docs/product-architecture/visualization-and-rendering.md` says where the word
 * comes from — *"The name is resolved on the server, where the vocabulary lives.
 * A console that resolved names itself would be a second authority on the
 * vocabulary, and two authorities eventually disagree"* — and its `Incomplete if`
 * closes on the console's half of the bargain: incomplete *"if a label is
 * composed in the browser"*.
 *
 * `scripts/browser_name_fallback_census.py` COUNTS the class; it cannot say what
 * a screen does instead. That is this file. Each case below renders a repaired
 * surface twice — once with the word the server serves, once with the word
 * missing — and asserts both halves:
 *
 *   * the served word reaches the screen, so the repair did not simply delete a
 *     name; and
 *   * its absence reads as a NAMED STATE, and the product identifier that used
 *     to stand there appears nowhere in the rendered text.
 *
 * The second assertion is the one that has to bite. Asserting only "the state is
 * named" would pass on a screen that printed the state AND the identifier beside
 * it, which is the defect wearing a label.
 *
 * WHY THE IDENTIFIERS IN THE FIXTURES LOOK REAL. They are minted family prefixes
 * (`ds_`, `sc_`, `dqm_`) followed by an opaque part, exactly the shape the
 * ratified rule recognises. A fixture that used `x` would prove nothing about a
 * screen that prints a ULID.
 */
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";

import { ClientObjectSourcesPanel } from "../governance/MasterDataTabs";
import { SourceBindingsTab } from "../governance/SemanticModelTabs";
import RunAnomalies from "../datastreams/workbench/RunAnomalies";
import type { GovernanceObject } from "../governance/governanceSurface";

/** Every identifier a repaired screen used to be able to print. */
const DATASTREAM_ID = "ds_01KZTESTDATASTREAM0001";
const CONCEPT_ID = "sc_01KZTESTCONCEPT000001";
const MONITOR_ID = "dqm_01KZTESTMONITOR00001";

function objectWith(summary: Record<string, unknown>): GovernanceObject {
  return {
    object_ref: { type: "client-object", id: "cok_1", label: "Campaign", owner_href: "" },
    scope: "project",
    owner: {},
    lifecycle_status: "active",
    active_version_ref: null,
    selected_version_ref: null,
    available_tabs: ["overview"],
    default_tab: "overview",
    allowed_actions: [],
    used_by: { state: "empty", count: 0, refs: [], truncated: false },
    versions: { state: "empty", count: 0, refs: [], truncated: false },
    evidence: { state: "empty", count: 0, refs: [], truncated: false },
    summary,
    evidence_as_of: null,
  } as unknown as GovernanceObject;
}

function sourceBindings(row: Record<string, unknown>) {
  return {
    state: "available",
    coverage: { total: 1, by_state: { bound: 1 } },
    unavailable_reason: null,
    rows: [
      {
        concept_id: CONCEPT_ID,
        concept_version_id: "scv_01KZTESTVERSION00001",
        concept_name: "Ad spend",
        datastream_id: DATASTREAM_ID,
        datastream_name: "Google Ads — daily",
        mapping_version_id: "dmv_01KZTESTMAPPING00001",
        mapping_version_number: 3,
        source_field_id: "cost_micros",
        source_field_path: "metrics.cost_micros",
        state: "bound",
        confidence: null,
        publication_ref: null,
        fingerprints: {},
        freshness: {},
        blocking_refs: [],
        provenance: {},
        owner_href: null,
        ...row,
      },
    ],
  };
}

describe("Master Data — what feeds a client object", () => {
  it("names the Datastream the read model served", () => {
    render(
      <ClientObjectSourcesPanel
        detail={objectWith({
          sources: {
            state: "available",
            reason: null,
            rows: [
              {
                namespace: "campaign",
                datastream_id: DATASTREAM_ID,
                datastream_label: "Google Ads — daily",
                identity_mode: "source_key",
                identity_fields: ["campaign_id"],
                label_field: "campaign_name",
                mapping_version_id: "dmv_01KZTESTMAPPING00001",
              },
            ],
          },
        })}
      />,
    );

    expect(screen.getByText("Google Ads — daily")).toBeInTheDocument();
  });

  it("says the binding lost its Datastream instead of printing the id", () => {
    // The read model LEFT JOINs `app.datastreams`, so a null label is a binding
    // whose Datastream this Project no longer has. That is the fact; `ds_…` is
    // not, and it is what this cell used to render.
    const { container } = render(
      <ClientObjectSourcesPanel
        detail={objectWith({
          sources: {
            state: "available",
            reason: null,
            rows: [
              {
                namespace: "campaign",
                datastream_id: DATASTREAM_ID,
                datastream_label: null,
                identity_mode: "source_key",
                identity_fields: ["campaign_id"],
                label_field: null,
                mapping_version_id: null,
              },
            ],
          },
        })}
      />,
    );

    expect(screen.getByText("No longer in this Project")).toBeInTheDocument();
    expect(container.textContent).not.toContain(DATASTREAM_ID);
  });
});

describe("Semantic Model — source bindings", () => {
  it("names the Datastream and the Concept the projection served", () => {
    render(
      <SourceBindingsTab
        bindings={sourceBindings({}) as never}
        organizationId="org_1"
        projectId="proj_1"
        typeLabel="Concept"
        onRetry={() => {}}
      />,
    );

    expect(screen.getByText("Google Ads — daily")).toBeInTheDocument();
    expect(screen.getByText("Ad spend")).toBeInTheDocument();
  });

  it("names both absences, and prints neither identifier", () => {
    const { container } = render(
      <SourceBindingsTab
        bindings={sourceBindings({ datastream_name: null, concept_name: null }) as never}
        organizationId="org_1"
        projectId="proj_1"
        typeLabel="Concept"
        onRetry={() => {}}
      />,
    );

    expect(screen.getByText("Unnamed Datastream")).toBeInTheDocument();
    expect(screen.getByText("Unattributed")).toBeInTheDocument();
    expect(container.textContent).not.toContain(DATASTREAM_ID);
    expect(container.textContent).not.toContain(CONCEPT_ID);
  });
});

describe("Datastream runs — an anomaly names its monitor", () => {
  const run = (monitorLabel: string | null) => ({
    run_id: "run_01KZTESTRUN000000001",
    anomalies: {
      anomalies: 1,
      evaluations: 1,
      issues: [
        {
          id: "dqi_01KZTESTISSUE0000001",
          severity: "warn",
          status: "open",
          monitor_id: MONITOR_ID,
          monitor_label: monitorLabel,
        },
      ],
    },
  });

  it("names the monitor the registry served", async () => {
    render(<RunAnomalies run={run("Null rate — cost")} projectId="proj_1" datastreamId="ds_1" />);
    await userEvent.click(screen.getByTestId("run-anomalies-trigger"));

    expect(screen.getByTestId("run-anomaly-monitor")).toHaveTextContent("Null rate — cost");
  });

  it("says the monitor could not be read instead of printing its id", async () => {
    // `datastream_workbench._monitor_profiles` serves `monitor_label: null` on
    // purpose — "a monitor row that could not be read is NAMED, never filled
    // with a plausible label". Printing `dqm_…` here undid that decision.
    render(<RunAnomalies run={run(null)} projectId="proj_1" datastreamId="ds_1" />);
    await userEvent.click(screen.getByTestId("run-anomalies-trigger"));

    const heading = screen.getByTestId("run-anomaly-monitor");
    expect(heading).toHaveTextContent("Monitor could not be read");
    expect(heading.textContent).not.toContain(MONITOR_ID);
    // AND THE ADDRESS SURVIVES WHERE IT BELONGS. The card's "Monitor" row is a
    // monospace cell, which `visualization-and-rendering.md` names as one of the
    // two legitimate places for an identifier. The repair takes the token out of
    // the heading, not out of the card: an anomaly nobody can trace back is a
    // worse screen than one that shows an id where an id is expected.
    const identifier = screen.getByText(MONITOR_ID);
    expect(identifier.className).toContain("font-mono");
  });
});
