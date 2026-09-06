/**
 * The two daily-insight states Overview could not say, and now says.
 *
 * 1. THE FIFTH STATE (`execution-substrate.md`, `Incomplete if` 11). Four states
 *    are RECORDED by a run — published, no_insight, blocked, failed — and the
 *    fifth is the ABSENT row: the task never ran, in a host toorow does not own.
 *    The server answered it with `empty`, the same word it uses for `no_insight`,
 *    so a Project whose scheduled task was never installed read exactly like a
 *    Project whose task ran and found the day quiet. The screen has to SAY the
 *    difference, and name the one gesture that repairs it — the recipe and the
 *    host's own schedule, never a deployment.
 *
 * 2. A RETRACTED CLAIM (`proactive-assertions.md`, decision 4; migration 321). A
 *    withdrawn insight stops being volunteered as a business signal AND does not
 *    vanish: the reader must be able to tell "never said" from "said, and later
 *    withdrawn", which is the whole reason the retraction is a state transition
 *    rather than a delete.
 */

import { render, screen } from "@testing-library/react";
import { expect, test, vi } from "vitest";

import ProjectOverview, { type OwnerReference } from "../shell/pages/ProjectOverview";

function response(status: number, body: unknown): Response {
  return { ok: status >= 200 && status < 300, status, json: async () => body } as Response;
}

const OWNER: OwnerReference = {
  surface: "project",
  workspace: "analyze",
  section: "explore",
  global_surface: null,
  global_section: null,
  object_type: null,
  object_id: null,
  tab: null,
  action: null,
  version_id: null,
  evidence_id: null,
};

const DIMENSION = {
  state: "unknown" as const,
  explanation: "No persisted business signal is available.",
  evidence_horizon: null,
  owner: OWNER,
};

const ENVELOPE = {
  schema_version: "project-overview.v1",
  project: {
    id: "project-1",
    name: "Acme",
    organization: { id: "org-1", name: "Acme Org" },
    business_domains: [],
    active_configuration_version_id: null,
    as_of: "2026-08-30T09:00:00Z",
  },
  posture: {
    operational_health: DIMENSION,
    trust_readiness: DIMENSION,
    business_signals: DIMENSION,
    limiting_dimension: "business_signals",
  },
  next_action: null,
  attention: { items: [], total: 0, has_more: false },
  coverage: [],
  outcomes: { status: "empty", items: [] },
  changes: { status: "empty", items: [] },
};

const NEVER_RAN = {
  state: "never_ran",
  insight_date: null,
  explanation:
    "No daily insight run has ever been recorded for this project, so the scheduled "
    + "task has not reported once. Copy the daily insight task recipe from Project "
    + "settings, General, into your LLM host's scheduled task -- or run it there once "
    + "to see the first day appear.",
  reason: null,
  gesture:
    "Copy the daily insight task recipe from Project settings, General, into your "
    + "LLM host's scheduled task -- or run it there once to see the first day appear.",
};

test("an absent run is titled after the absence and names the gesture that repairs it", async () => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response(200, {
    ...ENVELOPE,
    // `status` is what project_overview.py really emits for an absent run -- "empty" --
    // never "never_ran": the front keys off insight_silence.state alone, and a
    // fixture teaching a status the server cannot emit misleads the next reader.
    outcomes: { status: "empty", items: [], insight_silence: NEVER_RAN },
  })));

  render(<ProjectOverview projectId="project-1" />);

  // Not "No recent outcomes": that is the sentence for a run that reported.
  expect(await screen.findByText(/The daily insight task has never run/)).toBeInTheDocument();
  expect(screen.getByText(/scheduled task/)).toBeInTheDocument();
  expect(screen.getByText(/task recipe/)).toBeInTheDocument();
  expect(screen.queryByText("No recent outcomes")).toBeNull();
});

test("an absent run and a quiet day do not read the same on this screen", async () => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response(200, {
    ...ENVELOPE,
    outcomes: {
      status: "empty",
      items: [],
      insight_silence: {
        state: "no_insight",
        insight_date: "2026-08-29",
        explanation: "The daily insight run found nothing worth reporting that day.",
        reason: null,
      },
    },
  })));

  render(<ProjectOverview projectId="project-1" />);

  expect(await screen.findByText("No recent outcomes")).toBeInTheDocument();
  expect(screen.getByText(/nothing worth reporting/)).toBeInTheDocument();
  // The gesture belongs to the absent run alone; a quiet day has nothing to repair.
  expect(screen.queryByText(/task recipe/)).toBeNull();
});

test("a withdrawn insight is shown as withdrawn, with who, when and why", async () => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response(200, {
    ...ENVELOPE,
    outcomes: {
      status: "empty",
      items: [],
      insight_silence: {
        state: "retracted",
        insight_date: "2026-08-29",
        explanation: "The insight published for 2026-08-29 was retracted by owner@example.com. read on the wrong window",
        reason: "read on the wrong window",
      },
      insight_retractions: [
        {
          id: "din_EXAMPLE",
          title: "Spend spiked on Search",
          insight_date: "2026-08-29",
          retracted_at: "2026-08-30T08:00:00Z",
          retracted_by: "owner@example.com",
          reason: "read on the wrong window",
        },
      ],
    },
  })));

  render(<ProjectOverview projectId="project-1" />);

  const block = await screen.findByTestId("insight-retractions");
  expect(block).toHaveTextContent("Spend spiked on Search");
  expect(block).toHaveTextContent("owner@example.com");
  expect(block).toHaveTextContent("2026-08-30");
  expect(block).toHaveTextContent("read on the wrong window");
  // It is NOT counted among the signals, and the screen says it cannot come back.
  expect(block).toHaveTextContent(/no longer counted as a business signal/i);
  expect(block).toHaveTextContent(/cannot be reinstated/i);
});

test("a project with no withdrawal draws no withdrawal block", async () => {
  vi.stubGlobal("fetch", vi.fn().mockResolvedValue(response(200, ENVELOPE)));

  render(<ProjectOverview projectId="project-1" />);

  await screen.findByText("No recent outcomes");
  expect(screen.queryByTestId("insight-retractions")).toBeNull();
});
