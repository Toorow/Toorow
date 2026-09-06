/**
 * Saving a question as a Report (Story 50.3 AC4).
 *
 * THE DEFECT. `POST /api/projects/{id}/analyze/reports` has been served since
 * 50.3 and `ui/admin/src` contained ZERO call sites — three review lenses had
 * grepped it independently, and the Reports empty state was rewritten to admit
 * it: *"Creating one is not yet reachable from this console — the server accepts
 * it, no screen offers it."* No Report of any kind could be created, which is
 * also why the pacing marts had nowhere to surface (AI-190).
 *
 * WHAT IS ASSERTED. That the act exists, and that what it sends is the QUERY
 * SPEC VERSION rather than the Result. `analyze-and-test.md:50` — *"the Report
 * itself is not a cached answer"* — is the whole distinction: a Report that
 * pinned a Result would replay a stored answer instead of re-asking the
 * question, and no amount of later presentation work would fix that.
 */
import { describe, expect, it, vi } from "vitest";
import { createReportFromResult } from "../analyze/saveAsReport";

const PROJECT = "proj_EXAMPLE";
const SPEC_VERSION = "qsv_01J0000000000000000000000";

/**
 * The stub declares the arguments `fetch` is actually called with. A zero-arg
 * stub records its calls as an empty tuple, so every assertion below had to cast
 * the recorded call back into existence -- and a cast is exactly the thing that
 * would keep passing the day the call site stops sending a body.
 */
function stubFetch(status = 201, json: unknown = { report_id: "rep_1" }) {
  const spy = vi.fn(
    async (_input: RequestInfo | URL, _init?: RequestInit) =>
      new Response(JSON.stringify(json), {
        status,
        headers: { "Content-Type": "application/json" },
      }),
  );
  vi.stubGlobal("fetch", spy);
  return spy;
}

describe("createReportFromResult", () => {
  it("posts to the route the server has been serving all along", async () => {
    const spy = stubFetch();
    try {
      await createReportFromResult(PROJECT, SPEC_VERSION, "Plan pacing by channel");
    } finally {
      vi.unstubAllGlobals();
    }
    const [url, init] = spy.mock.calls[0];
    expect(String(url)).toContain(`/api/projects/${PROJECT}/analyze/reports`);
    expect(init?.method).toBe("POST");
  });

  it("pins the Query Spec VERSION, and never the Result", async () => {
    const spy = stubFetch();
    try {
      await createReportFromResult(PROJECT, SPEC_VERSION, "Plan pacing by channel");
    } finally {
      vi.unstubAllGlobals();
    }
    const body = JSON.parse(String(spy.mock.calls[0][1]?.body));
    expect(body.query_spec_version_id).toBe(SPEC_VERSION);
    expect(body).not.toHaveProperty("result_id");
    // No presentation is manufactured: the Reports workbench states plainly when
    // none has been accepted rather than showing an empty chart picker.
    expect(body).not.toHaveProperty("presentation");
    // And this Report comes from the Project's question, not a connector pack —
    // the server default `project` is left to apply.
    expect(body).not.toHaveProperty("seed_origin");
  });

  it("trims the name, and omits an empty description rather than sending blank", async () => {
    const spy = stubFetch();
    try {
      await createReportFromResult(PROJECT, SPEC_VERSION, "  Pacing  ", "   ");
    } finally {
      vi.unstubAllGlobals();
    }
    const body = JSON.parse(String(spy.mock.calls[0][1]?.body));
    expect(body.label).toBe("Pacing");
    expect(body).not.toHaveProperty("description");
  });

  it("refuses an empty name here rather than round-tripping for a 422", async () => {
    const spy = stubFetch();
    try {
      await expect(createReportFromResult(PROJECT, SPEC_VERSION, "   ")).rejects.toThrow(
        /needs a name/i,
      );
      expect(spy).not.toHaveBeenCalled();
    } finally {
      vi.unstubAllGlobals();
    }
  });

  it("lets a server refusal travel out instead of retrying with a quieter body", async () => {
    const spy = stubFetch(422, { code: "invalid_report", message: "Query Spec version unknown." });
    try {
      await expect(
        createReportFromResult(PROJECT, SPEC_VERSION, "Pacing"),
      ).rejects.toBeTruthy();
      // One attempt. A Report the server refused is not one to smuggle past it.
      expect(spy).toHaveBeenCalledTimes(1);
    } finally {
      vi.unstubAllGlobals();
    }
  });
});
