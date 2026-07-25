/**
 * CardFeedbackBar tests (Story 9.2b) — thumb click, no-op safe, confirmation.
 *
 * Story 9.10 : callServerTool est le helper partagé (@toorow/shell).
 *   - Sans connexion MCP (tests composant) : fallback legacy raw postMessage —
 *     les tests existants restent valides tels quels.
 *   - Chemin SDK : connectMcpApp({ createApp }) avec un mock ext-apps App
 *     (AI-54 : shapes réels CallToolRequest/CallToolResult) — un rejet affiche
 *     l'état d'erreur designé (F-11, enfin atteignable).
 */

import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import CardFeedbackBar from "../CardFeedbackBar";
import {
  connectMcpApp,
  __resetMcpAppForTests,
  type McpToolResultParams,
} from "@toorow/shell";

const DEFAULT_PROPS = {
  projectId: "proj_test",
  traceId: "trace_abc",
  reportRef: "card:kpi:2026-07-13",
  module: "card-kpi",
};

const ORIGINAL_PARENT = window.parent;

describe("CardFeedbackBar", () => {
  beforeEach(() => {
    // Rétablir les mocks entre tests
    vi.restoreAllMocks();
  });

  afterEach(() => {
    // Story 9.10 : détacher la connexion MCP partagée et restaurer window.parent.
    __resetMcpAppForTests();
    Object.defineProperty(window, "parent", {
      value: ORIGINAL_PARENT,
      writable: true,
      configurable: true,
    });
  });

  it("renders thumbs-up and thumbs-down buttons", () => {
    render(<CardFeedbackBar {...DEFAULT_PROPS} />);
    expect(screen.getByTestId("card-feedback-thumbs-up")).toBeInTheDocument();
    expect(screen.getByTestId("card-feedback-thumbs-down")).toBeInTheDocument();
  });

  it("Envoyer button is disabled until a rating is selected", () => {
    render(<CardFeedbackBar {...DEFAULT_PROPS} />);
    expect(screen.getByTestId("card-feedback-submit")).toBeDisabled();
  });

  it("clicking thumbs-up enables the Envoyer button", () => {
    render(<CardFeedbackBar {...DEFAULT_PROPS} />);
    fireEvent.click(screen.getByTestId("card-feedback-thumbs-up"));
    expect(screen.getByTestId("card-feedback-submit")).not.toBeDisabled();
  });

  it("clicking thumbs-down enables the Envoyer button", () => {
    render(<CardFeedbackBar {...DEFAULT_PROPS} />);
    fireEvent.click(screen.getByTestId("card-feedback-thumbs-down"));
    expect(screen.getByTestId("card-feedback-submit")).not.toBeDisabled();
  });

  it("shows confirmation after submit — graceful no-op hors host", async () => {
    // window.parent.postMessage peut lever hors host — on mocke pour test unitaire
    const postMessageMock = vi.fn();
    Object.defineProperty(window, "parent", {
      value: { postMessage: postMessageMock },
      writable: true,
    });

    render(<CardFeedbackBar {...DEFAULT_PROPS} />);
    fireEvent.click(screen.getByTestId("card-feedback-thumbs-up"));
    fireEvent.click(screen.getByTestId("card-feedback-submit"));

    // Attendre la confirmation (async submit)
    await screen.findByTestId("card-feedback-confirmation");
    expect(screen.getByTestId("card-feedback-confirmation")).toHaveTextContent(
      "Merci pour votre retour !",
    );
  });

  it("callServerTool is a no-op when postMessage throws (hors host)", async () => {
    // Simuler le cas où postMessage throw (pas de host MCP Apps)
    const postMessageMock = vi.fn(() => {
      throw new Error("No host");
    });
    Object.defineProperty(window, "parent", {
      value: { postMessage: postMessageMock },
      writable: true,
    });

    render(<CardFeedbackBar {...DEFAULT_PROPS} />);
    fireEvent.click(screen.getByTestId("card-feedback-thumbs-up"));
    // Ne doit pas crasher
    fireEvent.click(screen.getByTestId("card-feedback-submit"));

    // La confirmation doit quand même apparaître (le finally s'exécute)
    await screen.findByTestId("card-feedback-confirmation");
    expect(screen.getByTestId("card-feedback-confirmation")).toBeInTheDocument();
  });

  it("renders French copy (Cette carte vous a été utile ?)", () => {
    render(<CardFeedbackBar {...DEFAULT_PROPS} />);
    expect(screen.getByText(/Cette carte vous a été utile/)).toBeInTheDocument();
  });

  // F-11 (9-2b review, câblé pour de vrai en 9.10) : sur le chemin legacy,
  // callServerTool ne throw jamais (fire-and-forget postMessage avec try/catch
  // interne) — la confirmation reste optimiste et l'état d'erreur n'apparaît pas.
  it("shows confirmation after submit (setSubmitted in try — F-11 structural fix)", async () => {
    const postMessageMock = vi.fn();
    Object.defineProperty(window, "parent", {
      value: { postMessage: postMessageMock },
      writable: true,
    });

    render(<CardFeedbackBar {...DEFAULT_PROPS} />);
    fireEvent.click(screen.getByTestId("card-feedback-thumbs-down"));
    fireEvent.click(screen.getByTestId("card-feedback-submit"));

    await screen.findByTestId("card-feedback-confirmation");
    expect(screen.getByTestId("card-feedback-confirmation")).toHaveTextContent("Merci");
    // Error state must NOT appear (legacy callServerTool doesn't throw).
    expect(screen.queryByTestId("card-feedback-error")).not.toBeInTheDocument();
  });

  // -------------------------------------------------------------------------
  // Story 9.10 — chemin SDK (helper partagé + mock ext-apps App)
  // -------------------------------------------------------------------------

  function connectWithMockApp(
    callServerTool: (params: {
      name: string;
      arguments?: Record<string, unknown>;
    }) => Promise<McpToolResultParams>,
  ) {
    // AI-54 : le mock reflète la surface réelle du App ext-apps — l'API
    // non dépréciée `addEventListener("toolresult", …)` (le setter
    // `ontoolresult` est @deprecated en 1.7.4), connect/callServerTool
    // awaitables, params CallToolRequest.
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

  it("SDK path: resolved callServerTool → confirmation avec params CallToolRequest", async () => {
    const { app, handle } = connectWithMockApp(async () => ({ content: [] }));
    await handle.ready;

    render(<CardFeedbackBar {...DEFAULT_PROPS} />);
    fireEvent.click(screen.getByTestId("card-feedback-thumbs-up"));
    fireEvent.click(screen.getByTestId("card-feedback-submit"));

    await screen.findByTestId("card-feedback-confirmation");
    expect(app.callServerTool).toHaveBeenCalledTimes(1);
    const [params] = app.callServerTool.mock.calls[0];
    expect(params.name).toBe("submit_feedback");
    expect(params.arguments).toMatchObject({
      project_id: "proj_test",
      rating: 1,
      trace_id: "trace_abc",
      report_ref: "card:kpi:2026-07-13",
      module: "card-kpi",
    });
  });

  it("SDK path: rejected callServerTool → état d'erreur designé (F-11 atteint)", async () => {
    const { handle } = connectWithMockApp(async () => {
      throw new Error("host refused");
    });
    await handle.ready;

    render(<CardFeedbackBar {...DEFAULT_PROPS} />);
    fireEvent.click(screen.getByTestId("card-feedback-thumbs-down"));
    fireEvent.click(screen.getByTestId("card-feedback-submit"));

    await waitFor(() =>
      expect(screen.getByTestId("card-feedback-error")).toBeInTheDocument(),
    );
    expect(
      screen.queryByTestId("card-feedback-confirmation"),
    ).not.toBeInTheDocument();

    // Réessayer ramène au formulaire (rating toujours sélectionné).
    fireEvent.click(screen.getByTestId("card-feedback-retry"));
    expect(screen.getByTestId("card-feedback-bar")).toBeInTheDocument();
    expect(screen.getByTestId("card-feedback-submit")).not.toBeDisabled();
  });

  it("SDK path: résultat isError → état d'erreur (échec au niveau outil)", async () => {
    const { handle } = connectWithMockApp(async () => ({
      isError: true,
      content: [{ type: "text", text: "rate limited" }],
    }));
    await handle.ready;

    render(<CardFeedbackBar {...DEFAULT_PROPS} />);
    fireEvent.click(screen.getByTestId("card-feedback-thumbs-up"));
    fireEvent.click(screen.getByTestId("card-feedback-submit"));

    await waitFor(() =>
      expect(screen.getByTestId("card-feedback-error")).toBeInTheDocument(),
    );
  });
});
