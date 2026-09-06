/**
 * The Dossier read in the console -- story 74-2.
 *
 * Every response below is the shape `server/core/dossiers.py` returns
 * (`list_dossiers`, `get_dossier`) and `analyze_artifacts_api` returns for a
 * Render. What these tests hold, from the amendment of 2026-09-02:
 *
 *   * a narrative SAYS who wrote it, and a block stored without the word
 *     (console-written before that date) reads as a person's;
 *   * each figure carries the ADDRESS of the reasoning path that produced its
 *     Result, and says plainly when none was recorded;
 *   * the empty list names the gesture that fills it, in the reader's words.
 *
 * The frozen figure itself is drawn by `RenderVisual`, which is the Renders
 * page's own component and is proven there; here it is replaced by a marker so
 * these tests measure the document, not the runtime.
 */
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";

vi.mock("../analyze-artifacts/Renders", async (importOriginal) => {
  const original = await importOriginal<typeof import("../analyze-artifacts/Renders")>();
  return {
    ...original,
    RenderVisual: ({ render }: { render: { id: string } }) => (
      <div data-testid="render-visual">{render.id}</div>
    ),
  };
});

import { DossierWorkbench } from "../analyze-artifacts/Dossiers";
import { DossiersPanel } from "../analyze-artifacts/Renders";

const PROJECT = "proj_EXAMPLE";

const SUMMARY = {
  id: "dos_EXAMPLE",
  label: "August videos",
  description: null,
  current_version_id: "dosv_2",
  created_by: "owner@example.com",
  created_at: "2026-09-02T10:00:00Z",
  updated_at: "2026-09-02T11:00:00Z",
  current_version_number: 2,
  render_count: 1,
  narrative_count: 2,
};

const RENDER = {
  id: "rnd_EXAMPLE",
  result_id: "qr_EXAMPLE",
  visualization_spec_version_id: "vsv_EXAMPLE",
  renderer_build_id: "table/toorow-table@1.0.0",
  runtime_build_id: "@toorow/card-shell/viz@0.1.0+f46c5a2ca8e3",
  theme_version: "viz-theme@1",
  formatter_version: "viz-formatters@1",
  responsive_profile: "console",
  origin_kind: "explore",
  created_by: "owner@example.com",
  created_at: "2026-09-02T10:00:00Z",
  result_content_hash: "b".repeat(64),
  result_payload_retained: true,
  display_state: {},
  evidence_manifest: { result_id: "qr_EXAMPLE" },
  datum_evidence_keys: {},
  origin_report_run_id: null,
  origin_notebook_run_id: null,
  predecessor_render_id: null,
  content_hash: "c".repeat(64),
  retention_actions: [],
  sharing: { canonical_share_available: true, reason: "" },
};

const DETAIL = {
  id: "dos_EXAMPLE",
  label: "August videos",
  description: "What the month says about the channel.",
  current_version_id: "dosv_2",
  archived_at: null,
  created_by: "owner@example.com",
  created_at: "2026-09-02T10:00:00Z",
  updated_at: "2026-09-02T11:00:00Z",
  versions: [
    {
      id: "dosv_2",
      version_number: 2,
      label: "August videos",
      description: null,
      blocks: [{ kind: "narrative" }, { kind: "render" }, { kind: "narrative" }],
      content_hash: "d".repeat(64),
      predecessor_version_id: "dosv_1",
      created_by: "owner@example.com",
      created_at: "2026-09-02T11:00:00Z",
    },
    {
      id: "dosv_1",
      version_number: 1,
      label: "August videos",
      description: null,
      blocks: [{ kind: "render" }],
      content_hash: "e".repeat(64),
      predecessor_version_id: null,
      created_by: "owner@example.com",
      created_at: "2026-09-02T10:00:00Z",
    },
  ],
  current_resolved_blocks: [
    { kind: "narrative", text: "Views doubled in the second week.", authored_by: "model" },
    {
      kind: "render",
      render_id: "rnd_EXAMPLE",
      provenance: {
        result_id: "qr_EXAMPLE",
        visualization_spec_version_id: "vsv_EXAMPLE",
        runtime_build_id: "@toorow/card-shell/viz@0.1.0+f46c5a2ca8e3",
        renderer_build_id: "table/toorow-table@1.0.0",
        rendered_at: "2026-09-02T10:00:00Z",
        ai_path: "aip_EXAMPLE",
      },
      provenance_missing: false,
    },
    // Stored before 2026-09-02: the server already reads it as `human`; the
    // page must not turn a missing word into "the model".
    { kind: "narrative", text: "Reviewed before sending.", authored_by: "human" },
  ],
};

function response(body: unknown, status = 200): Response {
  return { ok: status >= 200 && status < 300, status, json: async () => body } as Response;
}

function mockApi(handlers: Array<[RegExp, () => Response]>) {
  const calls: string[] = [];
  vi.stubGlobal(
    "fetch",
    vi.fn((url: string) => {
      calls.push(url);
      for (const [pattern, produce] of handlers) {
        if (pattern.test(url)) return Promise.resolve(produce());
      }
      return Promise.resolve(response({ code: "not_found", message: "Not found" }, 404));
    }),
  );
  return calls;
}

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
});

// ---------------------------------------------------------------------------
// The list
// ---------------------------------------------------------------------------

it("lists the Dossiers of the Project beside the Renders they compose, and opens one by address", async () => {
  mockApi([[/\/analyze\/dossiers$/, () => response({ dossiers: [SUMMARY] })]]);
  const onOpenDossier = vi.fn();
  render(<DossiersPanel projectId={PROJECT} onOpenDossier={onOpenDossier} />);
  const open = await screen.findByRole("button", { name: "August videos" });
  expect(screen.getByText("2 blocks")).toBeTruthy();
  fireEvent.click(open);
  expect(onOpenDossier).toHaveBeenCalledWith("dos_EXAMPLE");
});

it("says why the list is empty and names the gesture that fills it", async () => {
  mockApi([[/\/analyze\/dossiers$/, () => response({ dossiers: [] })]]);
  render(<DossiersPanel projectId={PROJECT} />);
  expect(await screen.findByText("No Dossier yet")).toBeTruthy();
  expect(screen.getByText(/compose_dossier/)).toBeTruthy();
});

it("reads nothing without an exact Project scope", () => {
  const calls = mockApi([]);
  render(<DossiersPanel />);
  expect(calls).toEqual([]);
});

// ---------------------------------------------------------------------------
// The document
// ---------------------------------------------------------------------------

it("shows the document in order: who wrote each narrative, and each figure with its path address", async () => {
  mockApi([
    [/\/analyze\/dossiers\/dos_EXAMPLE$/, () => response(DETAIL)],
    [/\/analyze\/renders\/rnd_EXAMPLE$/, () => response(RENDER)],
  ]);
  const onOpenReasoningPath = vi.fn();
  const onOpenResult = vi.fn();
  const { container } = render(
    <DossierWorkbench collectionHref="/analyze/renders"
      projectId={PROJECT}
      dossierId="dos_EXAMPLE"
      tab="document"
      onOpenResult={onOpenResult}
      onOpenReasoningPath={onOpenReasoningPath}
    />,
  );

  await screen.findByText("Views doubled in the second week.");
  // The order IS the document.
  const authors = Array.from(container.querySelectorAll("[data-dossier-narrative-author]")).map(
    (node) => node.getAttribute("data-dossier-narrative-author"),
  );
  expect(authors).toEqual(["model", "human"]);
  expect(screen.getByText("Written by the model")).toBeTruthy();
  expect(screen.getByText("Written by a person")).toBeTruthy();

  // The figure is drawn by the Renders page's own component, from the Render it pins...
  await waitFor(() => expect(screen.getByTestId("render-visual").textContent).toBe("rnd_EXAMPLE"));
  // ...and carries the address of the reasoning path that produced its Result.
  const path = screen.getByRole("button", { name: "Open the path that produced this figure" });
  expect(path.getAttribute("data-dossier-reasoning-path")).toBe("aip_EXAMPLE");
  fireEvent.click(path);
  expect(onOpenReasoningPath).toHaveBeenCalledWith("qr_EXAMPLE");

  fireEvent.click(screen.getByRole("button", { name: "qr_EXAMPLE" }));
  expect(onOpenResult).toHaveBeenCalledWith("qr_EXAMPLE");
});

it("says plainly when a figure's Result recorded no reasoning path", async () => {
  const detail = {
    ...DETAIL,
    current_resolved_blocks: [
      {
        ...DETAIL.current_resolved_blocks[1],
        provenance: { ...(DETAIL.current_resolved_blocks[1] as { provenance: object }).provenance, ai_path: null },
      },
    ],
  };
  mockApi([
    [/\/analyze\/dossiers\/dos_EXAMPLE$/, () => response(detail)],
    [/\/analyze\/renders\/rnd_EXAMPLE$/, () => response(RENDER)],
  ]);
  const { container } = render(
    <DossierWorkbench collectionHref="/analyze/renders" projectId={PROJECT} dossierId="dos_EXAMPLE" tab="document" />,
  );
  expect(await screen.findByText("None was recorded for this Result")).toBeTruthy();
  expect(container.querySelector("[data-dossier-reasoning-path='']")).toBeTruthy();
  expect(screen.queryByRole("button", { name: "Open the path that produced this figure" })).toBeNull();
});

it("lists the versions, newest first, and marks the current one", async () => {
  mockApi([[/\/analyze\/dossiers\/dos_EXAMPLE$/, () => response(DETAIL)]]);
  render(<DossierWorkbench collectionHref="/analyze/renders" projectId={PROJECT} dossierId="dos_EXAMPLE" tab="versions" />);
  expect(await screen.findByText("2 (current)")).toBeTruthy();
  const rows = screen.getAllByRole("row").map((row) => row.textContent ?? "");
  // Newest first, and only the head's current version carries the word.
  expect(rows.findIndex((text) => text.startsWith("2 (current)"))).toBeLessThan(
    rows.findIndex((text) => /^1[^0-9]/.test(text)),
  );
  expect(rows.filter((text) => text.includes("(current)")).length).toBe(1);
});

it("names an unknown tab and opens no other in its place", async () => {
  mockApi([[/\/analyze\/dossiers\/dos_EXAMPLE$/, () => response(DETAIL)]]);
  render(<DossierWorkbench collectionHref="/analyze/renders" projectId={PROJECT} dossierId="dos_EXAMPLE" tab="pdf" />);
  expect(await screen.findByText("Unknown tab")).toBeTruthy();
});

it("says a Dossier is not found rather than opening another", async () => {
  mockApi([]);
  render(<DossierWorkbench collectionHref="/analyze/renders" projectId={PROJECT} dossierId="dos_MISSING" tab="document" />);
  expect(await screen.findByText("Dossier not found")).toBeTruthy();
});
