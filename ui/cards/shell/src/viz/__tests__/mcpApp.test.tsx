/**
 * The MCP App hands `_meta["toorow.result"]["ai_path_walk"]` to the shared
 * observed-ai-path.v1 capability without casting or remapping it. These tests
 * pin that contract through the real subscription path (an ext-apps shaped
 * mock, AI-54), including replacement on every ToolResult.
 */

import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import {
  __resetMcpAppForTests,
  APP_PAYLOAD_META_KEY,
  connectMcpApp,
  RESULT_META_KEY,
  type McpToolResultParams,
} from "@toorow/shell/src/mcpApp";

import McpAppVisualization from "../entries/mcpApp";
import { installStandardRenderers } from "../renderers";
import { renderInput } from "./fixtures";
import analyzeFeedbackTargets from "./fixtures/analyzeFeedbackTargets.json";
import observedAiPathParity from "./fixtures/observedAiPathParity.json";

installStandardRenderers();

afterEach(() => {
  __resetMcpAppForTests();
  delete window.__MCP_STRUCTURED_CONTENT__;
});

/** Server-generated evidence shared by the MCP, Workbench and Share mount proofs. */
const WALK = observedAiPathParity.mcp;

/** A minimal ToolResult; AI Path evidence is app-only Result meta. */
function toolResult(meta?: Record<string, unknown>): McpToolResultParams {
  return {
    structuredContent: { schema_version: 1 },
    _meta: meta,
  } as unknown as McpToolResultParams;
}

function renderToolResult(): McpToolResultParams {
  return analyzeFeedbackTargets.surfaces.mcp.delivery as unknown as McpToolResultParams;
}

/**
 * Subscribe the entry through the same singleton the entry itself will get,
 * with an App mock that lets the test fire the host's notification. The mock
 * mirrors the real ext-apps surface (AI-54): `addEventListener("toolresult")`.
 */
function connectCapturingHost(
  callImpl: (request?: { name: string; arguments?: Record<string, unknown> }) => Promise<McpToolResultParams> = async () => ({ content: [] }),
  connectImpl: () => Promise<void> = async () => {},
) {
  let fire: ((params: McpToolResultParams) => void) | null = null;
  const app = {
    addEventListener: vi.fn((_event: "toolresult", handler: (params: McpToolResultParams) => void) => {
      fire = handler;
    }),
    connect: vi.fn(connectImpl),
    callServerTool: vi.fn(callImpl),
  };
  const handle = connectMcpApp({ createApp: () => app });
  return {
    app,
    handle,
    fire: (params: McpToolResultParams) => {
      const handler = fire;
      if (!handler) throw new Error("the entry did not subscribe to toolresult");
      act(() => handler(params));
    },
  };
}

const FEEDBACK_META_KEY = "toorow.feedback";
const feedbackContext = (token: string, interactionRef = "interaction-1") => ({
  schema_version: "exact-feedback.v1",
  token,
  interaction_ref: interactionRef,
  expires_at: "2026-08-10T12:00:00Z",
});

function largeToolResult({ truncated = false }: { truncated?: boolean } = {}): McpToolResultParams {
  const initial = [
    { channel: "organic", sessions: 1240 },
    { channel: "paid", sessions: 880 },
  ];
  const input = renderInput({
    result: {
      ...renderInput().result,
      rows: initial,
      row_count: 4,
      truncated,
    },
  });
  return {
    structuredContent: {
      schema_version: 1,
      deep_link: {
        project_id: "project_1",
        owner_reference: {
          workspace: "analyze",
          section: "results",
          object_type: "result",
          object_id: "res_EXAMPLE_0001",
        },
      },
    },
    _meta: {
      [APP_PAYLOAD_META_KEY]: {
        schema_version: 1,
        kind: "render",
        render_input: input,
      },
      [RESULT_META_KEY]: {
        projection_size: "large",
        result_id: input.result.result_id,
        content_hash: input.result.content_hash,
        result_handle: "rh_opaque",
        allowed_columns: ["channel", "sessions"],
        row_count: 4,
        initial_projection: initial,
        next_cursor: "cursor_2",
      },
    },
  } as unknown as McpToolResultParams;
}

describe("the MCP App draws the canonical AI Path projection it is handed", () => {
  it("renders the shared collapsed timeline from the app channel, even while the chart waits", async () => {
    const host = connectCapturingHost();
    render(<McpAppVisualization />);
    await host.handle.ready;

    host.fire(toolResult({ [RESULT_META_KEY]: { ai_path_walk: WALK } }));

    // The drawing is the family's own: the timeline, in walk order, with the
    // lifecycle header -- not a panel ABOUT the path.
    const timeline = await screen.findByTestId("ai-path-timeline");
    expect(screen.queryByTestId("ai-path-branch-toggle")).toBeNull();
    expect(document.querySelector("details[data-ai-path-capability]")).not.toHaveAttribute("open");
    const rows = [...timeline.querySelectorAll("[data-ai-path-step]")];
    expect(rows.map((row) => row.getAttribute("data-ai-path-ordinal"))).toEqual(
      WALK.steps.map((step) => String(step.ordinal + 1)),
    );
    expect(rows[0]?.textContent).toContain(WALK.steps[0]!.tool_name);
    // A delivery occurred, so an absent render payload is terminal rather than
    // an infinite wait. The walk remains independently useful.
    expect(screen.getByRole("alert").textContent).toContain("without a render payload");
    expect(screen.queryByText(/Waiting for the host/)).toBeNull();
  });

  it("fails closed when the result channel carries no canonical projection", async () => {
    const host = connectCapturingHost();
    render(<McpAppVisualization />);
    await host.handle.ready;
    host.fire(toolResult({ [RESULT_META_KEY]: { rows: [] } }));

    expect(await screen.findByText("This AI Path is unavailable here.")).toBeTruthy();
    expect(screen.queryByTestId("ai-path-timeline")).toBeNull();
  });

  it("keeps a denial nondisclosing", async () => {
    const host = connectCapturingHost();
    render(<McpAppVisualization />);
    await host.handle.ready;
    host.fire(
      toolResult({
        [RESULT_META_KEY]: {
          ai_path_walk: {
            schema_version: "observed-ai-path.v1",
            state: "unavailable",
            reason: "unavailable",
          },
        },
      }),
    );

    expect(await screen.findByText("This AI Path is unavailable here.")).toBeTruthy();
    expect(document.body).not.toHaveTextContent(/aip_1|ai_paths_unavailable/);
    expect(screen.queryByTestId("ai-path-timeline")).toBeNull();
  });

  it("states the exact literal for a human-only result", async () => {
    const host = connectCapturingHost();
    render(<McpAppVisualization />);
    await host.handle.ready;
    host.fire(
      toolResult({
        [RESULT_META_KEY]: {
          ai_path_walk: {
            schema_version: "observed-ai-path.v1",
            state: "human_absent",
            literal: "No AI path",
          },
        },
      }),
    );

    expect(await screen.findByText("No AI path")).toBeTruthy();
    expect(screen.getByText(/produced without AI/)).toBeTruthy();
    expect(screen.queryByTestId("ai-path-timeline")).toBeNull();
  });

  it("draws the shared capability beside the visualization when a host supplies both", async () => {
    render(
      <McpAppVisualization
        input={renderInput()}
        displayMode="fullscreen"
        aiPathEvidence={WALK}
      />,
    );

    expect(await screen.findByTestId("ai-path-timeline")).toBeTruthy();
    expect(document.querySelector("details[data-ai-path-capability]")).not.toHaveAttribute("open");
    expect(document.querySelector('[data-viz-entry="mcp-app"]')).toBeTruthy();
  });

  it("renders the exact server-owned pivot sidecar instead of recomputing a table", async () => {
    const input = renderInput();
    input.spec.document.family = "table";
    render(
      <McpAppVisualization
        input={input}
        pivotEvidence={{
          schema_version: "analyze-pivot-render.v1",
          result_id: input.result.result_id,
          content_hash: input.result.content_hash,
          matrix: {
            result_id: input.result.result_id,
            content_hash: input.result.content_hash,
            row_fields: ["campaign"],
            column_fields: ["day"],
            value_fields: [{ name: "clicks" }],
            row_keys: [["Brand"]],
            column_keys: [["2026-08-01"]],
            cells: [{
              row_key: ["Brand"],
              column_key: ["2026-08-01"],
              values: { clicks: { value: 42 } },
              contributing_rows: 2,
            }],
            bounds: {
              response_bytes: 400,
              rows_truncated: false,
              columns_truncated: false,
            },
          },
        }}
      />,
    );

    expect(screen.getByRole("region", { name: "Multi-Datastream pivot matrix" })).toBeInTheDocument();
    expect(screen.getByText("42")).toBeInTheDocument();
    expect(screen.queryByTestId("table-fallback")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Table" }));
    expect(document.querySelector("[data-viz-table-fallback]")).toBeInTheDocument();
    expect(screen.queryByRole("region", { name: "Multi-Datastream pivot matrix" })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Pivot" }));
    expect(screen.getByRole("region", { name: "Multi-Datastream pivot matrix" })).toBeInTheDocument();
  });

  it("replaces prior path evidence on every ToolResult, including malformed input", async () => {
    const host = connectCapturingHost();
    render(<McpAppVisualization />);
    await host.handle.ready;

    host.fire(toolResult({ [RESULT_META_KEY]: { ai_path_walk: WALK } }));
    expect(await screen.findByTestId("ai-path-timeline")).toBeTruthy();
    const firstDetails = document.querySelector("details[data-ai-path-capability]")!;
    fireEvent.click(firstDetails.querySelector("summary")!);
    expect(firstDetails).toHaveAttribute("open");

    host.fire(toolResult({ [RESULT_META_KEY]: { ai_path_walk: { reasoning: "unsafe" } } }));
    expect(await screen.findByText("This AI Path is unavailable here.")).toBeTruthy();
    expect(screen.queryByTestId("ai-path-timeline")).toBeNull();
    expect(document.querySelector("details[data-ai-path-capability]")).not.toHaveAttribute("open");

    host.fire(
      toolResult({
        [RESULT_META_KEY]: {
          ai_path_walk: {
            schema_version: "observed-ai-path.v1",
            state: "human_absent",
            literal: "No AI path",
          },
        },
      }),
    );
    expect(await screen.findByText("No AI path")).toBeTruthy();
    expect(screen.queryByTestId("ai-path-timeline")).toBeNull();
  });

  it("clears the prior Result path when the next ToolResult is an error", async () => {
    const host = connectCapturingHost();
    render(<McpAppVisualization />);
    await host.handle.ready;

    host.fire(toolResult({ [RESULT_META_KEY]: { ai_path_walk: WALK } }));
    expect(await screen.findByTestId("ai-path-timeline")).toBeTruthy();

    host.fire({
      isError: true,
      content: [{ type: "text", text: "The next Result could not be delivered." }],
    });

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "The next Result could not be delivered.",
    );
    expect(await screen.findByText("This AI Path is unavailable here.")).toBeTruthy();
    expect(screen.queryByTestId("ai-path-timeline")).toBeNull();
    expect(document.body).not.toHaveTextContent(WALK.path_id);
  });

  it("renders a typed server payload through the shared runtime and table fallback", async () => {
    const host = connectCapturingHost();
    const { container } = render(<McpAppVisualization />);
    await host.handle.ready;
    host.fire(renderToolResult());

    expect(container.querySelector("[data-viz-runtime]")).not.toBeNull();
    expect(container.querySelector("[data-viz-table-fallback]")).not.toBeNull();
    expect(screen.queryByText(/Waiting for the host/)).toBeNull();
    expect(await screen.findByRole("button", {
      name: "Target feedback at row 2, sessions",
    })).toHaveTextContent("1,877");
  });

  it("replaces a large Result window through one acknowledged app-only call", async () => {
    const page = {
      structuredContent: {
        result_id: "res_EXAMPLE_0001",
        content_hash: "sha256:examplehash0001",
        columns: ["channel", "sessions"],
        rows: [
          { channel: "email", sessions: 410 },
          { channel: "direct", sessions: 205 },
        ],
        offset: 2,
        returned_rows: 2,
        total_rows: 4,
        has_more: false,
        next_cursor: null,
      },
    } as unknown as McpToolResultParams;
    const host = connectCapturingHost(async () => page);
    const onOpenResult = vi.fn();
    const { container } = render(<McpAppVisualization onOpenResult={onOpenResult} />);
    await host.handle.ready;
    host.fire(largeToolResult());

    expect(await screen.findByText(/Showing rows 1–2 of 4/)).toBeTruthy();
    const status = container.querySelector('[role="status"]');
    expect(status?.getAttribute("aria-live")).toBe("polite");
    expect(status?.textContent).toContain("Bounded slice of a frozen Result");
    fireEvent.click(screen.getByRole("button", { name: "Open Result" }));
    expect(onOpenResult).toHaveBeenCalledWith({
      workspace: "analyze",
      section: "results",
      object_type: "result",
      object_id: "res_EXAMPLE_0001",
    });
    fireEvent.click(screen.getByRole("button", { name: "Next" }));
    expect(await screen.findByText(/Showing rows 3–4 of 4/)).toBeTruthy();
    expect(screen.getByText("No more rows in this Result.")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Next" })).toBeNull();
    expect(container.textContent).toContain("email");
    expect(container.textContent).not.toContain("organic");
    expect(container.innerHTML).not.toContain("rh_opaque");
    expect(container.innerHTML).not.toContain("cursor_2");
    expect(container.innerHTML).not.toContain("cursor_3");
    expect(container.querySelectorAll("[data-viz-runtime]")).toHaveLength(1);
    expect(host.app.callServerTool).toHaveBeenCalledWith({
      name: "app_read_result_slice",
      arguments: {
        project_id: "project_1",
        handle: "rh_opaque",
        columns: ["channel", "sessions"],
        cursor: "cursor_2",
        limit: 100,
      },
    });
    host.fire(largeToolResult({ truncated: true }));
    expect(await screen.findByText(/The source Result is truncated/)).toBeTruthy();
  });

  it("carries and atomically replaces the exact feedback context across result pages", async () => {
    const firstContext = feedbackContext("first-token");
    const nextContext = feedbackContext("next-token");
    const page = {
      structuredContent: {
        result_id: "res_EXAMPLE_0001",
        content_hash: "sha256:examplehash0001",
        columns: ["channel", "sessions"],
        rows: [{ channel: "email", sessions: 410 }, { channel: "direct", sessions: 205 }],
        offset: 2,
        returned_rows: 2,
        total_rows: 4,
        has_more: false,
        next_cursor: null,
      },
      _meta: { [FEEDBACK_META_KEY]: nextContext },
    } as unknown as McpToolResultParams;
    const host = connectCapturingHost(async (request) => {
      if (request?.name === "app_read_result_slice") return page;
      return {
        structuredContent: {
          schema_version: "exact-feedback-receipt.v1",
          status: "recorded",
          feedback_id: "fba_1",
          interaction_ref: "interaction-1",
          target: { kind: "answer" },
        },
      } as unknown as McpToolResultParams;
    });
    render(<McpAppVisualization />);
    await host.handle.ready;
    const initial = largeToolResult();
    (initial._meta as Record<string, unknown>)[FEEDBACK_META_KEY] = firstContext;
    host.fire(initial);

    fireEvent.click(await screen.findByRole("button", { name: "Next" }));
    expect(await screen.findByText(/Showing rows 3/)).toBeTruthy();
    expect(host.app.callServerTool).toHaveBeenNthCalledWith(1, {
      name: "app_read_result_slice",
      arguments: expect.objectContaining({ feedback_context: firstContext }),
    });
    expect(screen.getByText("Answer", { selector: "strong" })).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "Helpful" }));
    fireEvent.click(screen.getByRole("button", { name: "Submit feedback" }));
    await waitFor(() => expect(host.app.callServerTool).toHaveBeenCalledTimes(2));
    expect(host.app.callServerTool).toHaveBeenNthCalledWith(2, {
      name: "submit_analyze_feedback",
      arguments: expect.objectContaining({ context: nextContext, target: { kind: "answer" } }),
    });
  });

  it("submits feedback only for the exact Result and pins attested by the MCP delivery", async () => {
    const surface = analyzeFeedbackTargets.surfaces.mcp;
    const host = connectCapturingHost(async () => ({
      structuredContent: surface.receipt,
    } as unknown as McpToolResultParams));
    render(<McpAppVisualization />);
    await host.handle.ready;
    host.fire(surface.delivery as unknown as McpToolResultParams);

    expect(await screen.findByRole("button", {
      name: "Target feedback at row 2, sessions",
    })).toHaveTextContent("1,877");
    fireEvent.click(screen.getByRole("button", { name: "Helpful" }));
    fireEvent.click(screen.getByRole("button", { name: "Submit feedback" }));
    expect(await screen.findByText("Feedback recorded.")).toBeTruthy();
    expect(host.app.callServerTool).toHaveBeenCalledWith({
      name: "submit_analyze_feedback",
      arguments: expect.objectContaining({ context: surface.context, target: surface.target }),
    });
    const input = surface.delivery._meta[APP_PAYLOAD_META_KEY].render_input;
    expect(input.result.result_id).toBe(analyzeFeedbackTargets.result.result_id);
    expect(input.result.content_hash).toBe(analyzeFeedbackTargets.result.content_hash);
    expect(input.result.row_count).toBe(analyzeFeedbackTargets.result.row_count);
    expect(input.pins.runtime_build).toBe(analyzeFeedbackTargets.pins.runtime_build_id);
    expect(analyzeFeedbackTargets.stored.find((row) => row.surface === "mcp_app")?.target)
      .toEqual(surface.target);
  });

  it("asks the real embedding host to open the structured Result reference", async () => {
    const postMessage = vi.spyOn(window.parent, "postMessage").mockImplementation(() => {});
    const host = connectCapturingHost();
    render(<McpAppVisualization />);
    await host.handle.ready;
    host.fire(largeToolResult());

    fireEvent.click(await screen.findByRole("button", { name: "Open Result" }));
    expect(postMessage).toHaveBeenCalledWith(
      {
        type: "toorow:openOwnerReference",
        owner_reference: {
          workspace: "analyze",
          section: "results",
          object_type: "result",
          object_id: "res_EXAMPLE_0001",
        },
      },
      expect.any(String),
    );
  });

  it("keeps the initial window, permits one in-flight call, then retries", async () => {
    let rejectFirst: ((error: Error) => void) | null = null;
    const first = new Promise<McpToolResultParams>((_resolve, reject) => {
      rejectFirst = reject;
    });
    const page = {
      structuredContent: {
        result_id: "res_EXAMPLE_0001",
        content_hash: "sha256:examplehash0001",
        columns: ["channel", "sessions"],
        rows: [{ channel: "email", sessions: 410 }],
        offset: 2,
        returned_rows: 1,
        total_rows: 4,
        has_more: true,
        next_cursor: "cursor_3",
      },
    } as unknown as McpToolResultParams;
    const host = connectCapturingHost(
      vi.fn().mockImplementationOnce(() => first).mockResolvedValueOnce(page),
    );
    const { container } = render(<McpAppVisualization />);
    await host.handle.ready;
    host.fire(largeToolResult());

    const next = await screen.findByRole("button", { name: "Next" });
    fireEvent.click(next);
    fireEvent.click(next);
    await waitFor(() => expect(host.app.callServerTool).toHaveBeenCalledTimes(1));
    await act(async () => rejectFirst!(new Error("page unavailable")));
    expect(await screen.findByRole("alert")).toHaveTextContent("page unavailable");
    expect(container.textContent).toContain("organic");

    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByText(/Showing rows 3–3 of 4/)).toBeTruthy();
    expect(host.app.callServerTool).toHaveBeenCalledTimes(2);
  });

  it("lets a new Result page immediately while the old request remains deferred", async () => {
    let resolveFirst: ((result: McpToolResultParams) => void) | null = null;
    const first = new Promise<McpToolResultParams>((resolve) => {
      resolveFirst = resolve;
    });
    const page = {
      structuredContent: {
        result_id: "res_EXAMPLE_0001",
        content_hash: "sha256:examplehash0001",
        columns: ["channel", "sessions"],
        rows: [{ channel: "email", sessions: 410 }],
        offset: 2,
        returned_rows: 1,
        total_rows: 4,
        has_more: true,
        next_cursor: "cursor_3",
      },
    } as unknown as McpToolResultParams;
    const host = connectCapturingHost(
      vi.fn().mockImplementationOnce(() => first).mockResolvedValueOnce(page),
    );
    render(<McpAppVisualization />);
    await host.handle.ready;
    host.fire(largeToolResult());
    fireEvent.click(await screen.findByRole("button", { name: "Next" }));
    await waitFor(() => expect(host.app.callServerTool).toHaveBeenCalledTimes(1));

    host.fire(largeToolResult());
    const next = await screen.findByRole("button", { name: "Next" });
    expect(next).toBeEnabled();
    fireEvent.click(next);
    await waitFor(() => expect(host.app.callServerTool).toHaveBeenCalledTimes(2));
    expect(await screen.findByText(/Showing rows 3/)).toBeTruthy();

    await act(async () => resolveFirst!({ content: [] }));
    expect(screen.getByText(/Showing rows 3/)).toBeTruthy();
  });

  it("invalidates a deferred page on ToolResult error so the next delivery is not blocked", async () => {
    let resolveFirst: ((result: McpToolResultParams) => void) | null = null;
    const first = new Promise<McpToolResultParams>((resolve) => { resolveFirst = resolve; });
    const page = {
      structuredContent: {
        result_id: "res_EXAMPLE_0001",
        content_hash: "sha256:examplehash0001",
        columns: ["channel", "sessions"],
        rows: [{ channel: "email", sessions: 410 }],
        offset: 2,
        returned_rows: 1,
        total_rows: 4,
        has_more: true,
        next_cursor: "cursor_3",
      },
    } as unknown as McpToolResultParams;
    const host = connectCapturingHost(
      vi.fn().mockImplementationOnce(() => first).mockResolvedValueOnce(page),
    );
    render(<McpAppVisualization />);
    await host.handle.ready;
    host.fire(largeToolResult());
    fireEvent.click(await screen.findByRole("button", { name: "Next" }));
    await waitFor(() => expect(host.app.callServerTool).toHaveBeenCalledTimes(1));

    host.fire({ isError: true, content: [{ type: "text", text: "delivery failed" }] });
    expect(await screen.findByRole("alert")).toHaveTextContent("delivery failed");
    host.fire(largeToolResult());
    fireEvent.click(await screen.findByRole("button", { name: "Next" }));
    await waitFor(() => expect(host.app.callServerTool).toHaveBeenCalledTimes(2));
    expect(await screen.findByText(/Showing rows 3/)).toBeTruthy();

    await act(async () => resolveFirst!({ content: [] }));
    expect(screen.getByText(/Showing rows 3/)).toBeTruthy();
  });

  it("ignores an old feedback acknowledgement after a new signed interaction arrives", async () => {
    let resolveFeedback: ((result: McpToolResultParams) => void) | null = null;
    const pendingFeedback = new Promise<McpToolResultParams>((resolve) => { resolveFeedback = resolve; });
    const host = connectCapturingHost(async (request) => {
      if (request?.name === "submit_analyze_feedback") return pendingFeedback;
      return { content: [] } as unknown as McpToolResultParams;
    });
    render(<McpAppVisualization />);
    await host.handle.ready;
    const first = largeToolResult();
    (first._meta as Record<string, unknown>)[FEEDBACK_META_KEY] = feedbackContext("token-a", "interaction-a");
    host.fire(first);
    fireEvent.click(await screen.findByRole("button", { name: "Helpful" }));
    fireEvent.click(screen.getByRole("button", { name: "Submit feedback" }));
    await waitFor(() => expect(host.app.callServerTool).toHaveBeenCalledTimes(1));

    const second = largeToolResult();
    (second._meta as Record<string, unknown>)[FEEDBACK_META_KEY] = feedbackContext("token-b", "interaction-b");
    host.fire(second);
    await act(async () => resolveFeedback!({
      structuredContent: {
        schema_version: "exact-feedback-receipt.v1",
        status: "recorded",
        feedback_id: "fba_old",
        interaction_ref: "interaction-a",
        target: { kind: "answer" },
      },
    } as unknown as McpToolResultParams));

    expect(screen.queryByText("Feedback recorded.")).toBeNull();
    expect(screen.getByRole("button", { name: "Submit feedback" })).toBeDisabled();
  });

  it("keeps the initial window and points legacy hosts to the Result workbench", async () => {
    const host = connectCapturingHost(
      async () => ({ content: [] }),
      async () => {
        throw new Error("legacy host");
      },
    );
    const { container } = render(<McpAppVisualization />);
    await host.handle.ready;
    const initial = largeToolResult();
    act(() => {
      window.dispatchEvent(
        new MessageEvent("message", {
          data: {
            type: "mcp:structuredContent",
            data: initial.structuredContent,
            meta: initial._meta,
          },
        }),
      );
    });

    fireEvent.click(await screen.findByRole("button", { name: "Next" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Open the Result workbench");
    expect(container.textContent).toContain("organic");
    expect(screen.queryByRole("button", { name: "Retry" })).toBeNull();
  });

  it("states an empty initial window without inventing the range 1–0", async () => {
    const host = connectCapturingHost();
    render(<McpAppVisualization />);
    await host.handle.ready;
    const initial = largeToolResult();
    const meta = initial._meta as Record<string, Record<string, unknown>>;
    const wrapper = meta[APP_PAYLOAD_META_KEY]!;
    const input = wrapper.render_input as ReturnType<typeof renderInput>;
    wrapper.render_input = {
      ...input,
      result: { ...input.result, rows: [], row_count: 0 },
    };
    Object.assign(meta[RESULT_META_KEY]!, {
      row_count: 0,
      initial_projection: [],
      next_cursor: null,
    });
    host.fire(initial);

    expect(await screen.findByText("No rows in this window (0 total).")).toBeTruthy();
    expect(screen.queryByText(/1–0/)).toBeNull();
    expect(screen.getByText("No more rows in this Result.")).toBeTruthy();
  });

  it("turns a delivered missing or future payload into a truthful terminal error", async () => {
    const host = connectCapturingHost();
    render(<McpAppVisualization />);
    await host.handle.ready;
    host.fire(toolResult());
    expect(screen.getByRole("alert").textContent).toContain("without a render payload");

    host.fire({
      structuredContent: { schema_version: 1 },
      _meta: {
        [APP_PAYLOAD_META_KEY]: {
          schema_version: 2,
          kind: "render",
          render_input: renderInput(),
        },
      },
    } as unknown as McpToolResultParams);
    expect(screen.getByRole("alert").textContent).toContain("newer runtime");
  });

  it("keeps the legacy structuredContent.data path when meta is a flat moved map", async () => {
    const host = connectCapturingHost();
    const { container } = render(<McpAppVisualization />);
    await host.handle.ready;
    host.fire({
      structuredContent: { schema_version: 1, data: renderInput() },
      _meta: { [APP_PAYLOAD_META_KEY]: { __tool__: "legacy", rows: [] } },
    } as unknown as McpToolResultParams);
    expect(container.querySelector("[data-viz-runtime]")).not.toBeNull();
  });

  it("calls a missing render schema version malformed, not newer", async () => {
    const host = connectCapturingHost();
    render(<McpAppVisualization />);
    await host.handle.ready;
    host.fire({
      structuredContent: { schema_version: 1 },
      _meta: {
        [APP_PAYLOAD_META_KEY]: { kind: "render", render_input: renderInput() },
      },
    } as unknown as McpToolResultParams);
    expect(screen.getByRole("alert").textContent).toContain("malformed render payload");
    expect(screen.getByRole("alert").textContent).not.toContain("newer runtime");
  });

  it("surfaces an inbound tool error even when structuredContent is absent", async () => {
    const host = connectCapturingHost();
    render(<McpAppVisualization />);
    await host.handle.ready;
    host.fire({
      isError: true,
      content: [{ type: "text", text: "The Visualization Spec is not compatible." }],
    });
    expect(screen.getByRole("alert").textContent).toContain(
      "The Visualization Spec is not compatible.",
    );
    expect(screen.queryByText(/Waiting for the host/)).toBeNull();
  });
});
