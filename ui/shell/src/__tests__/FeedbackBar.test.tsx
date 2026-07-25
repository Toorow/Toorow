/**
 * Vitest tests for FeedbackBar (Story 5.5, AC5, AC8; review-global-gaps G-09).
 *
 * UX change (G-09): thumb clicks now SELECT the rating (no auto-submit).
 *   Envoyer submits; Envoyer is disabled while selectedRating === null.
 *
 * No-network discipline: with no MCP connection established (component tests),
 * the shared callServerTool (Story 9.10, ../mcpApp) uses the legacy raw
 * postMessage to window.parent. Tests mock window.parent.postMessage and
 * verify the correct payload is sent.
 *
 * Story 9.10 additions: SDK path via connectMcpApp({ createApp }) with a mock
 * ext-apps App (AI-54: real CallToolRequest params / CallToolResult shapes) —
 * a rejected callServerTool shows the designed error state (F-11).
 *
 * Covers:
 *   - Renders 👍 and 👎 buttons.
 *   - Click 👍 → selects rating (does NOT submit yet; Envoyer becomes enabled).
 *   - Click 👎 → selects rating (does NOT submit yet; Envoyer becomes enabled).
 *   - Envoyer is disabled when no rating selected.
 *   - Envoyer click → postMessage called with selectedRating.
 *   - Clicking 👍 then Envoyer → postMessage with rating=1.
 *   - Clicking 👎 then Envoyer → postMessage with rating=-1.
 *   - Submit with comment → comment in postMessage arguments.
 *   - After submit → shows "Retour envoyé. Merci !".
 *   - No MCP host (postMessage throws) → no uncaught exception (graceful no-op).
 *   - trace_id null when TRACING_ENABLED=false.
 *   - SDK path: success → confirmation; rejection → error state + Réessayer.
 */

import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { ThemeProvider } from "@mui/material";
import { createTheme } from "@mui/material/styles";
import FeedbackBar from "../FeedbackBar";
import {
  connectMcpApp,
  __resetMcpAppForTests,
  type McpToolResultParams,
} from "../mcpApp";

// ---------------------------------------------------------------------------
// Setup: minimal MUI theme wrapper + postMessage mock
// ---------------------------------------------------------------------------

const theme = createTheme();

const ORIGINAL_PARENT = window.parent;

afterEach(() => {
  // Story 9.10: drop the shared MCP connection (and its window listener)
  // between tests, and restore the real window.parent.
  __resetMcpAppForTests();
  Object.defineProperty(window, "parent", {
    value: ORIGINAL_PARENT,
    writable: true,
    configurable: true,
  });
});

function renderFeedbackBar(props?: Partial<Parameters<typeof FeedbackBar>[0]>) {
  const defaultProps = {
    projectId: "proj_test",
    traceId: "abc123",
    reportRef: "get_daily_report:2026-07-11",
    module: "test-module",
    ...props,
  };
  return render(
    <ThemeProvider theme={theme}>
      <FeedbackBar {...defaultProps} />
    </ThemeProvider>,
  );
}

// ---------------------------------------------------------------------------
// Renders 👍 and 👎 buttons
// ---------------------------------------------------------------------------

describe("FeedbackBar -- renders", () => {
  it("renders 👍 Utile button", () => {
    renderFeedbackBar();
    expect(screen.getByTestId("feedback-thumbs-up")).toBeInTheDocument();
  });

  it("renders 👎 Pas utile button", () => {
    renderFeedbackBar();
    expect(screen.getByTestId("feedback-thumbs-down")).toBeInTheDocument();
  });

  it("renders comment field with French placeholder", () => {
    renderFeedbackBar();
    expect(screen.getByPlaceholderText(/Commentaire optionnel/i)).toBeInTheDocument();
  });

  it("renders Envoyer submit button", () => {
    renderFeedbackBar();
    expect(screen.getByTestId("feedback-submit")).toBeInTheDocument();
    expect(screen.getByTestId("feedback-submit")).toHaveTextContent("Envoyer");
  });

  it("Envoyer is disabled when no rating selected", () => {
    renderFeedbackBar();
    expect(screen.getByTestId("feedback-submit")).toBeDisabled();
  });
});

// ---------------------------------------------------------------------------
// Thumb click selects rating — does NOT submit
// ---------------------------------------------------------------------------

describe("FeedbackBar -- thumb click selects rating (no auto-submit)", () => {
  it("click 👍 enables Envoyer but does NOT call postMessage", async () => {
    const postMessageMock = vi.fn();
    Object.defineProperty(window, "parent", {
      value: { postMessage: postMessageMock },
      writable: true,
    });

    renderFeedbackBar();

    fireEvent.click(screen.getByTestId("feedback-thumbs-up"));

    // Envoyer should be enabled now
    await waitFor(() =>
      expect(screen.getByTestId("feedback-submit")).not.toBeDisabled()
    );

    // No postMessage yet
    expect(postMessageMock).not.toHaveBeenCalled();
  });

  it("click 👎 enables Envoyer but does NOT call postMessage", async () => {
    const postMessageMock = vi.fn();
    Object.defineProperty(window, "parent", {
      value: { postMessage: postMessageMock },
      writable: true,
    });

    renderFeedbackBar();

    fireEvent.click(screen.getByTestId("feedback-thumbs-down"));

    await waitFor(() =>
      expect(screen.getByTestId("feedback-submit")).not.toBeDisabled()
    );

    expect(postMessageMock).not.toHaveBeenCalled();
  });
});

// ---------------------------------------------------------------------------
// Click 👍 then Envoyer → postMessage called with rating=1
// ---------------------------------------------------------------------------

describe("FeedbackBar -- thumbs up + Envoyer", () => {
  it("click 👍 then Envoyer calls postMessage with rating=1", async () => {
    const postMessageMock = vi.fn();
    Object.defineProperty(window, "parent", {
      value: { postMessage: postMessageMock },
      writable: true,
    });

    renderFeedbackBar({ traceId: "trace_abc" });

    fireEvent.click(screen.getByTestId("feedback-thumbs-up"));
    await waitFor(() =>
      expect(screen.getByTestId("feedback-submit")).not.toBeDisabled()
    );
    fireEvent.click(screen.getByTestId("feedback-submit"));

    await waitFor(() => expect(postMessageMock).toHaveBeenCalledTimes(1));

    const [payload] = postMessageMock.mock.calls[0];
    expect(payload.type).toBe("callServerTool");
    expect(payload.tool).toBe("submit_feedback");
    expect(payload.arguments.rating).toBe(1);
    expect(payload.arguments.project_id).toBe("proj_test");
    expect(payload.arguments.trace_id).toBe("trace_abc");
  });
});

// ---------------------------------------------------------------------------
// Click 👎 then Envoyer → postMessage called with rating=-1
// ---------------------------------------------------------------------------

describe("FeedbackBar -- thumbs down + Envoyer", () => {
  it("click 👎 then Envoyer calls postMessage with rating=-1", async () => {
    const postMessageMock = vi.fn();
    Object.defineProperty(window, "parent", {
      value: { postMessage: postMessageMock },
      writable: true,
    });

    renderFeedbackBar();

    fireEvent.click(screen.getByTestId("feedback-thumbs-down"));
    await waitFor(() =>
      expect(screen.getByTestId("feedback-submit")).not.toBeDisabled()
    );
    fireEvent.click(screen.getByTestId("feedback-submit"));

    await waitFor(() => expect(postMessageMock).toHaveBeenCalledTimes(1));

    const [payload] = postMessageMock.mock.calls[0];
    expect(payload.arguments.rating).toBe(-1);
  });
});

// ---------------------------------------------------------------------------
// Submit with comment → comment in postMessage arguments
// ---------------------------------------------------------------------------

describe("FeedbackBar -- comment field", () => {
  it("submit with comment includes comment in postMessage arguments", async () => {
    const postMessageMock = vi.fn();
    Object.defineProperty(window, "parent", {
      value: { postMessage: postMessageMock },
      writable: true,
    });

    renderFeedbackBar();

    const commentField = screen.getByPlaceholderText(/Commentaire optionnel/i);
    fireEvent.change(commentField, { target: { value: "Super rapport !" } });

    fireEvent.click(screen.getByTestId("feedback-thumbs-up"));
    await waitFor(() =>
      expect(screen.getByTestId("feedback-submit")).not.toBeDisabled()
    );
    fireEvent.click(screen.getByTestId("feedback-submit"));

    await waitFor(() => expect(postMessageMock).toHaveBeenCalledTimes(1));

    const [payload] = postMessageMock.mock.calls[0];
    expect(payload.arguments.comment).toBe("Super rapport !");
  });
});

// ---------------------------------------------------------------------------
// After submit → shows "Retour envoyé. Merci !"
// ---------------------------------------------------------------------------

describe("FeedbackBar -- confirmation", () => {
  it("shows confirmation message after submit via 👍 + Envoyer", async () => {
    const postMessageMock = vi.fn();
    Object.defineProperty(window, "parent", {
      value: { postMessage: postMessageMock },
      writable: true,
    });

    renderFeedbackBar();

    fireEvent.click(screen.getByTestId("feedback-thumbs-up"));
    await waitFor(() =>
      expect(screen.getByTestId("feedback-submit")).not.toBeDisabled()
    );
    fireEvent.click(screen.getByTestId("feedback-submit"));

    await waitFor(() =>
      expect(screen.getByText("Retour envoyé. Merci !")).toBeInTheDocument(),
    );
  });

  it("shows confirmation message after submit via 👎 + Envoyer", async () => {
    const postMessageMock = vi.fn();
    Object.defineProperty(window, "parent", {
      value: { postMessage: postMessageMock },
      writable: true,
    });

    renderFeedbackBar();

    fireEvent.click(screen.getByTestId("feedback-thumbs-down"));
    await waitFor(() =>
      expect(screen.getByTestId("feedback-submit")).not.toBeDisabled()
    );
    fireEvent.click(screen.getByTestId("feedback-submit"));

    await waitFor(() =>
      expect(screen.getByText("Retour envoyé. Merci !")).toBeInTheDocument(),
    );
  });
});

// ---------------------------------------------------------------------------
// No MCP host → postMessage throws → graceful no-op
// ---------------------------------------------------------------------------

describe("FeedbackBar -- graceful no-op outside MCP host", () => {
  it("does not throw when postMessage throws (no host)", async () => {
    // Simulate no-host environment: postMessage throws
    Object.defineProperty(window, "parent", {
      value: {
        postMessage: () => {
          throw new Error("No host available");
        },
      },
      writable: true,
    });

    renderFeedbackBar();

    // Select rating first
    fireEvent.click(screen.getByTestId("feedback-thumbs-up"));
    await waitFor(() =>
      expect(screen.getByTestId("feedback-submit")).not.toBeDisabled()
    );

    // Should NOT throw — the try/catch in callServerTool swallows the error
    expect(() => {
      fireEvent.click(screen.getByTestId("feedback-submit"));
    }).not.toThrow();

    // Confirmation should still appear (submit succeeded locally)
    await waitFor(() =>
      expect(screen.getByText("Retour envoyé. Merci !")).toBeInTheDocument(),
    );
  });

  it("does not throw when window.parent is undefined", async () => {
    // Simulate missing parent
    Object.defineProperty(window, "parent", {
      value: undefined,
      writable: true,
    });

    renderFeedbackBar();

    fireEvent.click(screen.getByTestId("feedback-thumbs-down"));
    await waitFor(() =>
      expect(screen.getByTestId("feedback-submit")).not.toBeDisabled()
    );

    expect(() => {
      fireEvent.click(screen.getByTestId("feedback-submit"));
    }).not.toThrow();
  });
});

// ---------------------------------------------------------------------------
// trace_id null when TRACING_ENABLED=false
// ---------------------------------------------------------------------------

describe("FeedbackBar -- null traceId", () => {
  it("passes trace_id=null when traceId prop is null", async () => {
    const postMessageMock = vi.fn();
    Object.defineProperty(window, "parent", {
      value: { postMessage: postMessageMock },
      writable: true,
    });

    renderFeedbackBar({ traceId: null });

    fireEvent.click(screen.getByTestId("feedback-thumbs-up"));
    await waitFor(() =>
      expect(screen.getByTestId("feedback-submit")).not.toBeDisabled()
    );
    fireEvent.click(screen.getByTestId("feedback-submit"));

    await waitFor(() => expect(postMessageMock).toHaveBeenCalledTimes(1));

    const [payload] = postMessageMock.mock.calls[0];
    expect(payload.arguments.trace_id).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// Story 9.10 — SDK path (shared mcpApp helper, mock ext-apps App)
// ---------------------------------------------------------------------------

describe("FeedbackBar -- SDK path (Story 9.10)", () => {
  function connectWithMockApp(
    callServerTool: (params: {
      name: string;
      arguments?: Record<string, unknown>;
    }) => Promise<McpToolResultParams>,
  ) {
    // AI-54: the mock mirrors the real ext-apps App surface — the
    // non-deprecated `addEventListener("toolresult", …)` listener API (the
    // `ontoolresult` setter is @deprecated in 1.7.4), awaitable
    // connect/callServerTool with CallToolRequest params.
    const app = {
      addEventListener: vi.fn(
        (
          _event: "toolresult",
          _handler: (params: McpToolResultParams) => void,
        ) => {},
      ),
      connect: vi.fn(async () => {}),
      callServerTool: vi.fn(callServerTool),
    };
    return { app, handle: connectMcpApp({ createApp: () => app }) };
  }

  it("resolved callServerTool → confirmation, CallToolRequest params sent", async () => {
    const { app, handle } = connectWithMockApp(async () => ({ content: [] }));
    await handle.ready;

    renderFeedbackBar({ traceId: "trace_abc" });
    fireEvent.click(screen.getByTestId("feedback-thumbs-up"));
    fireEvent.click(screen.getByTestId("feedback-submit"));

    await waitFor(() =>
      expect(screen.getByText("Retour envoyé. Merci !")).toBeInTheDocument(),
    );
    expect(app.callServerTool).toHaveBeenCalledTimes(1);
    const [params] = app.callServerTool.mock.calls[0];
    expect(params.name).toBe("submit_feedback");
    expect(params.arguments).toMatchObject({
      project_id: "proj_test",
      rating: 1,
      trace_id: "trace_abc",
    });
  });

  it("rejected callServerTool → designed error state (F-11 reachable)", async () => {
    const { handle } = connectWithMockApp(async () => {
      throw new Error("host refused");
    });
    await handle.ready;

    renderFeedbackBar();
    fireEvent.click(screen.getByTestId("feedback-thumbs-down"));
    fireEvent.click(screen.getByTestId("feedback-submit"));

    await waitFor(() =>
      expect(screen.getByTestId("feedback-error")).toBeInTheDocument(),
    );
    expect(screen.queryByTestId("feedback-confirmation")).not.toBeInTheDocument();

    // Réessayer returns to the form (rating still selected).
    fireEvent.click(screen.getByTestId("feedback-retry"));
    expect(screen.getByTestId("feedback-bar")).toBeInTheDocument();
    expect(screen.getByTestId("feedback-submit")).not.toBeDisabled();
  });

  it("isError tool result → designed error state (tool-level failure)", async () => {
    const { handle } = connectWithMockApp(async () => ({
      isError: true,
      content: [{ type: "text", text: "rate limited" }],
    }));
    await handle.ready;

    renderFeedbackBar();
    fireEvent.click(screen.getByTestId("feedback-thumbs-up"));
    fireEvent.click(screen.getByTestId("feedback-submit"));

    await waitFor(() =>
      expect(screen.getByTestId("feedback-error")).toBeInTheDocument(),
    );
  });
});
