/**
 * Story 50.2 AC4/AC5 — the Query Spec workbench.
 *
 * One property carries this screen: an address without its version pins nothing,
 * and pinning nothing must open nothing. Falling back to the current version
 * would make a link shared to show ONE question show a different one after the
 * next revision — silently, and with no way for the reader to tell.
 */
import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../analyze/workbenchClient", async () => {
  const actual = await vi.importActual<typeof import("../analyze/workbenchClient")>(
    "../analyze/workbenchClient",
  );
  return { ...actual, fetchQuerySpecVersion: vi.fn() };
});

vi.mock("../analyze/analyzeTargets", async () => {
  const actual = await vi.importActual<typeof import("../analyze/analyzeTargets")>(
    "../analyze/analyzeTargets",
  );
  return {
    ...actual,
    resultTarget: (_scope: unknown, resultId: string) => `/result/${resultId}/view`,
    ownerTarget: () => "/owner-target",
  };
});

import QuerySpecWorkbench from "../analyze/QuerySpecWorkbench";
import { ApiError } from "../lib/apiFetch";
import { fetchQuerySpecVersion } from "../analyze/workbenchClient";

const SCOPE = { organizationId: "org_EXAMPLE", projectId: "proj_EXAMPLE" };

const DETAIL = {
  schema_version: "analyze-result-lens.v1",
  query_spec_id: "qs_EXAMPLE",
  query_spec_version_id: "qsv_EXAMPLE",
  version_number: 2,
  semantic_view_id: "sv_EXAMPLE",
  semantic_view_version_id: "svv_EXAMPLE",
  spec: { measures: [{ id: "sc_clicks", version_id: "scv_clicks" }], grain: "day" },
  content_hash: "s".repeat(64),
  predecessor_version_id: "qsv_PREVIOUS",
  created_at: "2026-07-31T09:00:00+00:00",
  created_by: "person-1",
  name: "Clicks by day",
  is_current_version: false,
  results: [
    {
      result_id: "qr_EXAMPLE",
      outcome: "unavailable",
      row_count: 0,
      truncated: false,
      started_at: null,
      ended_at: "2026-07-31T09:00:01+00:00",
      content_hash: "h".repeat(64),
      owner_ref: { workspace: "analyze", section: "explore" },
    },
  ],
  semantic_view_owner_ref: { workspace: "governance", section: "semantic-model" },
};

function mount(versionId: string | null) {
  return render(
    <QuerySpecWorkbench
      scope={SCOPE}
      querySpecId="qs_EXAMPLE"
      querySpecVersionId={versionId}
    />,
  );
}

describe("the Query Spec workbench", () => {
  // A BLOCK body, deliberately. `mockReset()` returns the mock itself, and
  // Vitest treats a value returned from `beforeEach` as a teardown callback --
  // so the concise arrow form made Vitest CALL the mock after each test. With a
  // rejecting implementation that produced a rejected promise nobody awaited,
  // and the suite failed with the error under test as an unhandled rejection.
  beforeEach(() => {
    vi.mocked(fetchQuerySpecVersion).mockReset();
  });

  it("opens nothing when the address pins no version", () => {
    mount(null);
    expect(screen.getByText("No Query Spec version is pinned")).toBeInTheDocument();
    // The read is never attempted: there is nothing to read, and reading the
    // head instead would be the substitution this screen exists to refuse.
    expect(fetchQuerySpecVersion).not.toHaveBeenCalled();
  });

  it("shows the immutable intent, its hash, and the Results it produced", async () => {
    vi.mocked(fetchQuerySpecVersion).mockResolvedValue(DETAIL as never);
    mount("qsv_EXAMPLE");
    expect(await screen.findByText("v2")).toBeInTheDocument();
    expect(screen.getByText("s".repeat(64))).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "qr_EXAMPLE" })).toHaveAttribute(
      "href",
      "/result/qr_EXAMPLE/view",
    );
    expect(screen.getByText("Unavailable")).toBeInTheDocument();
  });

  it("says plainly that a version is superseded rather than hiding it", async () => {
    vi.mocked(fetchQuerySpecVersion).mockResolvedValue(DETAIL as never);
    mount("qsv_EXAMPLE");
    expect(await screen.findByText("A later version exists.")).toBeInTheDocument();
  });

  it("does not fabricate a Result for a version that was never executed", async () => {
    vi.mocked(fetchQuerySpecVersion).mockResolvedValue({ ...DETAIL, results: [] } as never);
    mount("qsv_EXAMPLE");
    expect(await screen.findByText(/No Result has been fabricated for it/)).toBeInTheDocument();
  });

  it("answers a denied and an absent version identically", async () => {
    // `mockImplementation`, not `mockRejectedValue`: the latter builds the
    // rejected promise before anything awaits it, and Vitest reports it as an
    // unhandled rejection rather than as the case under test.
    vi.mocked(fetchQuerySpecVersion).mockImplementation(() =>
      Promise.reject(new ApiError(404, "not_found", "Not found")),
    );
    mount("qsv_FOREIGN");
    expect(
      await screen.findByText("This Query Spec version is not available to you"),
    ).toBeInTheDocument();
    expect(screen.getByText("No other version has been opened in its place.")).toBeInTheDocument();
  });
});
