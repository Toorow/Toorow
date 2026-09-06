/**
 * Story 50.7 repair (review finding F1) -- the runtime half of the proof.
 *
 * WHAT WENT WRONG, and why a test in this language was the only thing that could
 * have caught it. `server/core/render_shares_api.py` assigned its own metadata
 * dict to `window.__TOOROW_FROZEN_RENDER__` -- `content_hash`,
 * `datum_evidence_keys`, `display_state`, `render_id`, ... -- and `share.tsx`
 * casts that global to `RenderInput`. TypeScript cannot see across an HTTP
 * boundary and a Python test cannot mount React, so both sides were green while
 * every recipient met the validator's field-by-field refusal panel: not a chart,
 * not the accessible table fallback, not an honest unavailable state.
 *
 * WHAT THIS FILE MOUNTS. `fixtures/analyzeFeedbackTargets.json` contains the
 * response `GET /api/render-shares/session/render` actually produced, with
 * only the per-run identifiers and timestamps normalized. The server-side half,
 * `server/tests/core/test_render_shares_api.py::
 * test_the_session_render_envelope_is_the_runtimes_own_contract`, drives the real
 * endpoint against a real PostgreSQL and asserts the live response still equals
 * this file. Change the composition and one of the two goes red; regenerate the
 * fixture without fixing the runtime and the other does.
 */

import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import ShareVisualization, {
  readFrozenAiPathEvidence,
  readFrozenRender,
  readFrozenFeedbackContext,
} from "../entries/share";
import { installStandardRenderers } from "../renderers";
import { RUNTIME_BUILD, THEME_VERSION, FORMATTER_VERSION } from "../buildInfo";
import type { RenderInput } from "../contracts";
import analyzeFeedbackTargets from "./fixtures/analyzeFeedbackTargets.json";

installStandardRenderers();
afterEach(() => {
  cleanup();
  delete window.__TOOROW_FROZEN_RENDER__;
  delete window.__TOOROW_FROZEN_AI_PATH_EVIDENCE__;
  delete window.__TOOROW_FROZEN_FEEDBACK_CONTEXT__;
  delete window.__TOOROW_FROZEN_DOSSIER__;
});

/** Exactly what the served page assigns, and nothing added on the way in. */
const feedbackSurface = analyzeFeedbackTargets.surfaces.share;
const frozen = feedbackSurface.delivery.render as unknown as RenderInput;
const aiPathEvidence = feedbackSurface.delivery.ai_path_evidence as unknown;
const feedbackContext = feedbackSurface.context;

describe("the frozen envelope the share page serves", () => {
  it("uses one byte-identical server projection for MCP, Workbench and Share", () => {
    const mcpInput = analyzeFeedbackTargets.surfaces.mcp.delivery._meta["toorow.app_payload"].render_input;
    expect(frozen.result).toEqual(mcpInput.result);
    expect(frozen.spec).toEqual(mcpInput.spec);
    expect(frozen.pins).toEqual(mcpInput.pins);
  });

  it("pins the build this bundle is, so the fixture cannot silently rot", () => {
    // Not decoration. A Render pins the build that drew it and the runtime refuses
    // a pin it does not recognize -- correctly. If the runtime moves and this
    // fixture does not, the failure must say THAT, rather than surfacing as a
    // build-mismatch panel that looks like a product defect.
    expect(frozen.pins.runtime_build).toBe(RUNTIME_BUILD);
    expect(frozen.pins.theme_version).toBe(THEME_VERSION);
    expect(frozen.pins.formatter_version).toBe(FORMATTER_VERSION);
    expect(frozen.pins.renderer_build).toBe("table/toorow-table@1.0.0");
  });

  it("is the five-field envelope the runtime accepts, and no sixth", () => {
    expect(Object.keys(frozen).sort()).toEqual(
      ["display", "pins", "profile", "result", "spec"],
    );
  });

  it("RENDERS A FROZEN VALUE INTO THE DOM", () => {
    // The whole point of the finding: a recipient must see the number. The strings
    // asserted are the FORMATTED ones a reader sees, produced by the formatter
    // version the Render pinned -- not the raw JSON, which nobody looks at.
    render(<ShareVisualization input={frozen} />);

    expect(screen.getByText("2,043")).toBeTruthy();
    expect(screen.getByText("1,877")).toBeTruthy();
    expect(screen.getByText("1,412")).toBeTruthy();
    expect(screen.getByText("Jul 28, 2026")).toBeTruthy();

    // And it is a real visualization mount, not an error surface wearing values.
    expect(document.querySelector('[data-viz-entry="share"]')).toBeTruthy();
    expect(document.querySelector('[data-viz-chart-drawn="true"]')).toBeTruthy();
    expect(screen.queryByText(/is required/i)).toBeNull();
    expect(screen.queryByText(/not a field of the runtime envelope/i)).toBeNull();
  });

  it("does not invent a truncation disclosure over the complete frozen answer", () => {
    render(<ShareVisualization input={frozen} />);
    // The Result declared no truncation, so the page must not claim one. A fixture
    // that said `"truncation": "none"` produced exactly that false banner, because
    // the runtime reads the KEY's presence, not its value.
    expect(screen.queryByText("The server returned part of the rows")).toBeNull();
    // Share-page disclosure remains beside the five-field runtime input. It is
    // server chrome, not a sixth RenderInput field synthesized by this mount.
    expect(feedbackSurface.delivery.disclosure).toEqual({
      shared_on: "2026-01-01T00:00:00Z",
      expires_at: "2026-01-01T00:00:00Z",
    });
    expect(Object.keys(frozen).sort()).toEqual(["display", "pins", "profile", "result", "spec"]);
  });

  it("mounts under the share profile, whatever profile the Render was made for", () => {
    render(<ShareVisualization input={frozen} />);
    expect(frozen.profile).toBe("share");
    expect(screen.getByText("2,043")).toBeTruthy();
  });

  it("is what `readFrozenRender` reads off the global the served page assigns", () => {
    window.__TOOROW_FROZEN_RENDER__ = feedbackSurface.delivery.render;
    expect(readFrozenRender()).toEqual(frozen);
  });

  it("reads and renders frozen AI Path evidence beside the five-field RenderInput", async () => {
    window.__TOOROW_FROZEN_RENDER__ = feedbackSurface.delivery.render;
    window.__TOOROW_FROZEN_AI_PATH_EVIDENCE__ = aiPathEvidence;
    expect(readFrozenAiPathEvidence()).toEqual(aiPathEvidence);

    const { container } = render(<ShareVisualization />);
    expect(await screen.findByTestId("ai-path-timeline")).toHaveTextContent(
      feedbackSurface.delivery.ai_path_evidence.steps[0]!.tool_name,
    );
    expect(screen.queryByTestId("ai-path-branch-toggle")).toBeNull();
    expect(screen.getByText(new RegExp(`Path ${feedbackSurface.delivery.ai_path_evidence.path_id}`))).toHaveTextContent(
      feedbackSurface.delivery.ai_path_evidence.outcome,
    );
    const details = container.querySelector("details[data-ai-path-capability]");
    expect(details).toBeTruthy();
    expect(details).not.toHaveAttribute("open");
    expect(Object.keys(frozen).sort()).toEqual(["display", "pins", "profile", "result", "spec"]);
  });

  it("uses the frozen Share sidecar with the same exact target composer", async () => {
    const surface = analyzeFeedbackTargets.surfaces.share;
    const submit = vi.fn(async () => surface.receipt);
    window.__TOOROW_FROZEN_FEEDBACK_CONTEXT__ = feedbackContext;
    expect(readFrozenFeedbackContext()).toEqual(feedbackContext);
    render(
      <ShareVisualization
        input={frozen}
        aiPathEvidence={aiPathEvidence}
        feedbackContext={feedbackContext}
        onSubmitFeedback={submit}
      />,
    );
    fireEvent.click(screen.getAllByRole("button", { name: "Target this step" })[0]);
    fireEvent.click(screen.getByRole("button", { name: "Not helpful" }));
    fireEvent.change(screen.getByLabelText("Optional comment"), { target: { value: "Check this value" } });
    fireEvent.click(screen.getByRole("button", { name: "Submit feedback" }));
    await waitFor(() => expect(submit).toHaveBeenCalledOnce());
    expect(submit).toHaveBeenCalledWith(expect.objectContaining({
      context: feedbackContext,
      target: surface.target,
      polarity: "negative",
      comment: "Check this value",
    }));
    expect(surface.authority.result_id).toBe(analyzeFeedbackTargets.result.result_id);
    expect(surface.authority.visualization_spec_version_id)
      .toBe(analyzeFeedbackTargets.pins.visualization_spec_version_id);
    expect(analyzeFeedbackTargets.stored.find((row) => row.surface === "share")?.target)
      .toEqual(surface.target);
    expect(await screen.findByText("Feedback recorded.")).toBeTruthy();
  });

  it("turns a legacy Share with no frozen evidence into the closed safe state", async () => {
    expect(readFrozenAiPathEvidence()).toEqual({
      schema_version: "observed-ai-path.v1",
      state: "unavailable",
      reason: "evidence_not_frozen",
    });
    window.__TOOROW_FROZEN_RENDER__ = {
      ...frozen,
      pins: { ...frozen.pins, runtime_build: RUNTIME_BUILD },
    };
    render(<ShareVisualization />);
    expect(
      await screen.findByText("AI Path evidence was not frozen with this shared Result."),
    ).toBeTruthy();
  });

  it("keeps the chart independent from malformed path evidence", () => {
    const currentFrozen = {
      ...frozen,
      pins: { ...frozen.pins, runtime_build: RUNTIME_BUILD },
    } as RenderInput;
    render(<ShareVisualization input={currentFrozen} aiPathEvidence={{ reasoning: "unsafe" }} />);
    expect(document.querySelector('[data-viz-chart-drawn="true"]')).toBeTruthy();
    expect(
      screen.getByText("This AI Path is unavailable here.").closest('[role="status"]'),
    ).toBeTruthy();
    expect(screen.queryByTestId("ai-path-timeline")).toBeNull();
  });

  it("still refuses an envelope that is NOT the runtime contract", () => {
    // The regression itself, kept as a test: the metadata dict the endpoint used
    // to send must never render as if it were data. This is the panel a recipient
    // saw, and it is correct behaviour for a wrong envelope -- the defect was
    // sending the wrong envelope, not refusing it.
    const metadataDict = {
      render_id: "rnd_FIXTURE",
      result_id: "qr_FIXTURE",
      content_hash: "b".repeat(64),
      display_state: {},
      evidence_manifest: {},
    } as unknown as RenderInput;
    render(<ShareVisualization input={metadataDict} />);
    expect(screen.queryByText("2043")).toBeNull();
  });
});

describe("the shared dossier sequence (73-2)", () => {
  it("mounts one runtime per frozen figure, prints its facts, and says an absent one", () => {
    window.__TOOROW_FROZEN_DOSSIER__ = {
      label: "Q3 channel review",
      version_number: 1,
      blocks: [
        { kind: "narrative", text: "What the quarter says." },
        {
          kind: "render",
          render_id: "rnd_EXAMPLE",
          render: frozen,
          ai_path_evidence: aiPathEvidence,
          facts: [{ label: "Shared on", value: "2026-09-02" }],
        },
        {
          kind: "render",
          render_id: "rnd_EMPTY",
          render: null,
          unavailable: { title: "This shared figure carries no frozen values" },
        },
      ],
    };
    const { container } = render(<ShareVisualization />);
    expect(screen.getByRole("heading", { name: "Q3 channel review" })).toBeTruthy();
    expect(screen.getByText("What the quarter says.")).toBeTruthy();
    expect(screen.getByText("Shared on")).toBeTruthy();
    // The frozen figure draws through the SAME runtime as a single share...
    expect(container.querySelectorAll("[data-viz-entry='share-dossier']").length).toBe(1);
    // ...and the absent one is said, never substituted.
    expect(screen.getByText("This shared figure carries no frozen values")).toBeTruthy();
  });

  it("says who wrote each narrative, and reads a block stored without an author as a person's (74-2)", () => {
    window.__TOOROW_FROZEN_DOSSIER__ = {
      label: "August videos",
      version_number: 2,
      blocks: [
        { kind: "narrative", text: "Views doubled in the second week.", authored_by: "model" },
        { kind: "render", render_id: "rnd_EXAMPLE", render: frozen, ai_path_evidence: aiPathEvidence },
        { kind: "narrative", text: "Reviewed before sending." },
      ],
    };
    const { container } = render(<ShareVisualization />);
    const narratives = container.querySelectorAll("[data-share-narrative-author]");
    expect(narratives.length).toBe(2);
    expect(narratives[0].getAttribute("data-share-narrative-author")).toBe("model");
    expect(narratives[1].getAttribute("data-share-narrative-author")).toBe("human");
    expect(screen.getByText("Written by the model")).toBeTruthy();
    expect(screen.getByText("Written by a person")).toBeTruthy();
    // The figure carries the reasoning path's address through the same
    // capability the single-render page mounts -- one per frozen figure.
    expect(container.querySelectorAll("[data-ai-path-capability]").length).toBe(1);
  });

  it("keeps the single-render page exactly as it was when no dossier is frozen", () => {
    window.__TOOROW_FROZEN_RENDER__ = frozen;
    window.__TOOROW_FROZEN_AI_PATH_EVIDENCE__ = aiPathEvidence;
    const { container } = render(<ShareVisualization />);
    expect(container.querySelector("[data-viz-entry='share']")).toBeTruthy();
    expect(container.querySelector("[data-viz-entry='share-dossier']")).toBeNull();
  });
});

describe("per-figure feedback on the shared dossier (73-2, last half)", () => {
  it("offers feedback only on figures whose minted context rode inline", () => {
    window.__TOOROW_FROZEN_DOSSIER__ = {
      label: "Q3 channel review",
      version_number: 1,
      blocks: [
        {
          kind: "render",
          render_id: "rnd_WITH",
          render: frozen,
          feedback_context: feedbackContext,
        },
        { kind: "render", render_id: "rnd_WITHOUT", render: frozen },
      ],
    };
    render(<ShareVisualization />);
    expect(screen.getAllByLabelText("Feedback on this analysis").length).toBe(1);
  });
});

