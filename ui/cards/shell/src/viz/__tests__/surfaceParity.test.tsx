import { act, cleanup, fireEvent, render, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import {
  __resetMcpAppForTests,
  connectMcpApp,
  type McpToolResultParams,
} from "@toorow/shell/src/mcpApp";

import type { RenderInput } from "../contracts";
import ConsoleVisualization, { serializeForConsole } from "../entries/console";
import McpAppVisualization, { serializeForMcpApp } from "../entries/mcpApp";
import ShareVisualization, { serializeForShare } from "../entries/share";
import { installStandardRenderers } from "../renderers";
import analyzeRenderToolResult from "./fixtures/analyzeRenderToolResult.json";

installStandardRenderers();
afterEach(() => {
  cleanup();
  __resetMcpAppForTests();
  delete window.__TOOROW_FROZEN_RENDER__;
});

type Surface = "console" | "mcp" | "share";

const golden = analyzeRenderToolResult as unknown as {
  _meta: {
    "toorow.app_payload": { render_input: RenderInput };
    "toorow.result": { content_hash: string; result_handle: string | null };
    "toorow.visualization": { visualization_spec_version_id: string };
  };
};

function goldenInput(): RenderInput {
  return structuredClone(golden._meta["toorow.app_payload"].render_input);
}

/**
 * THE EXPECTATIONS BELOW ARE READ FROM THE CAPTURE, NEVER TYPED (AI-337).
 *
 * This fixture used to be written by its generator -- two literal rows under a
 * `qr_FIXTURE` id -- so a test could pin `"organic1,240"` and mean something. It
 * is now the CallToolResult of one real execution against the local warehouse, so
 * a typed literal would only pin the day it was captured. What is asserted
 * instead is what must hold of ANY capture: the identities are minted ones, the
 * three surfaces agree, and the drawn rows are the Result's rows.
 */
const EXECUTED_ID = /^[a-z]{2,5}_[0-9A-HJKMNP-TV-Z]{26}$/;

/** The Result column each bound member resolves to, from the schema the server ships. */
function columnOfMember(input: RenderInput, member: string): string {
  const field = (input.result.schema?.fields ?? []).find((f) => f.id === member);
  return field?.name ?? member;
}

function boundColumn(input: RenderInput, well: "dimension" | "measure"): string {
  const member = (input.spec.document.bindings?.[well] ?? [])[0]!;
  return columnOfMember(input, member);
}

async function mount(surface: Surface, input: RenderInput) {
  if (surface === "console") return render(<ConsoleVisualization input={input} />);
  if (surface === "mcp") {
    let deliver: ((params: McpToolResultParams) => void) | null = null;
    const app = {
      addEventListener: (
        _event: "toolresult",
        handler: (params: McpToolResultParams) => void,
      ) => {
        deliver = handler;
      },
      connect: async () => {},
      callServerTool: async () => ({ content: [] }),
    };
    const handle = connectMcpApp({ createApp: () => app });
    const mounted = render(<McpAppVisualization displayMode="inline" />);
    await handle.ready;
    const wire = structuredClone(analyzeRenderToolResult) as unknown as McpToolResultParams;
    (wire._meta!["toorow.app_payload"] as { render_input: RenderInput }).render_input = input;
    act(() => deliver!(wire));
    await waitFor(() =>
      expect(mounted.container.querySelector("[data-viz-runtime]")).not.toBeNull(),
    );
    return mounted;
  }
  window.__TOOROW_FROZEN_RENDER__ = input;
  const mounted = render(<ShareVisualization />);
  await waitFor(() =>
    expect(mounted.container.querySelector("[data-viz-runtime]")).not.toBeNull(),
  );
  return mounted;
}

function semanticModel(surface: Surface, input: RenderInput) {
  const model =
    surface === "console"
      ? serializeForConsole(input)
      : surface === "mcp"
        ? serializeForMcpApp(input, "inline")
        : serializeForShare(input);
  return {
    dataset: model.dataset,
    formats: model.formats,
    colors: model.colors,
    disclosures: model.disclosures,
    tableColumns: model.tableColumns,
    datumKeys: model.datumKeys,
    datumFields: model.datumFields,
  };
}

async function domMeaning(surface: Surface, input: RenderInput) {
  const { container } = await mount(surface, input);
  const table = container.querySelector("[data-viz-table-fallback]");
  expect(table).not.toBeNull();
  const columns = [...table!.querySelectorAll("th")].map((cell) => cell.textContent);
  const rows = [...table!.querySelectorAll("[data-viz-row-key]")].map((row) => row.textContent);
  const disclosures = container.querySelector("dl")?.textContent ?? "";

  fireEvent.click(container.querySelector("[data-viz-mark]")!);
  const evidence = [...container.querySelectorAll('[data-viz-evidence-path] [role="treeitem"]')]
    .map((node) => node.getAttribute("aria-label"));
  cleanup();
  return { columns, rows, disclosures, evidence };
}

describe("Story 65.2 -- one server Result across three real surfaces", () => {
  it("carries the identities one warehouse execution minted", () => {
    const input = goldenInput();
    // A Result the product EXECUTED, not a name someone typed: the id is minted
    // by `query_execution.accept_execution`, and the two meta channels of the one
    // answer must agree on the frozen payload they describe.
    expect(input.result.result_id).toMatch(EXECUTED_ID);
    expect(input.result.result_id.startsWith("qr_")).toBe(true);
    expect(input.result.outcome).toBe("success");
    expect(input.result.content_hash).toMatch(/^[0-9a-f]{64}$/);
    expect(input.result.content_hash).toBe(golden._meta["toorow.result"].content_hash);
    expect(input.spec.visualization_spec_version_id).toMatch(EXECUTED_ID);
    expect(input.spec.visualization_spec_version_id).toBe(
      golden._meta["toorow.visualization"].visualization_spec_version_id,
    );
    expect(input.result.rows.length).toBe(input.result.row_count);
  });

  it("resolves each bound member to the Result column the server declared", () => {
    // The defect this pins (AI-337, 2026-08-31): a Visualization Spec binds
    // CONCEPT IDS and a Result row is keyed by the member's COLUMN NAME. Until
    // the compiler resolved one to the other through `result.schema.fields`,
    // every executed Result compiled to nulls -- invisible while the only
    // envelope in the repository was a fixture whose ids were its column names.
    const input = goldenInput();
    const dimension = (input.spec.document.bindings?.dimension ?? [])[0]!;
    const measure = (input.spec.document.bindings?.measure ?? [])[0]!;
    expect(dimension).toMatch(EXECUTED_ID);
    expect(measure).toMatch(EXECUTED_ID);
    const dimensionColumn = columnOfMember(input, dimension);
    const measureColumn = columnOfMember(input, measure);
    expect(dimensionColumn).not.toBe(dimension);
    expect(measureColumn).not.toBe(measure);

    const model = serializeForConsole(input);
    expect(model.dataset.dimensions).toContain(dimensionColumn);
    // Every drawn cell is the value the server put in the row, never a null the
    // lookup produced by missing the column.
    const drawn = model.dataset.source.map((line) => line[line.length - 1]);
    expect(drawn).toEqual(input.result.rows.map((row) => row[measureColumn]));
    expect(drawn.some((value) => value === null)).toBe(false);
    // And the label a reader sees is the canonical name, never the identifier.
    expect(model.tableColumns.map((column) => column.label)).toEqual([
      dimensionColumn,
      measureColumn,
    ]);
    expect(
      model.tableColumns.find((column) => column.key === measureColumn)?.role,
    ).toBe("measure");
  });

  it("keeps the same pinned identity and compiled analytical meaning", () => {
    const input = goldenInput();
    expect(Object.keys(input.pins).sort()).toEqual([
      "formatter_version",
      "renderer_build",
      "runtime_build",
      "theme_version",
    ]);

    const models = (["console", "mcp", "share"] as const).map((surface) =>
      semanticModel(surface, input),
    );
    expect(models[1]).toEqual(models[0]);
    expect(models[2]).toEqual(models[0]);
  });

  it("mounts the three actual delivery adapters with the same table and evidence", async () => {
    const input = goldenInput();
    const meanings: Awaited<ReturnType<typeof domMeaning>>[] = [];
    for (const surface of ["console", "mcp", "share"] as const) {
      meanings.push(await domMeaning(surface, goldenInput()));
    }
    expect(meanings[1]).toEqual(meanings[0]);
    expect(meanings[2]).toEqual(meanings[0]);
    // One drawn row per executed row, each carrying the category the warehouse
    // returned, in the order the server ranked them.
    const dimensionColumn = boundColumn(input, "dimension");
    expect(meanings[0].rows).toHaveLength(input.result.rows.length);
    input.result.rows.forEach((row, index) => {
      expect(meanings[0].rows[index]).toContain(String(row[dimensionColumn]));
    });
    const evidence = meanings[0].evidence.join(" ");
    expect(evidence).toContain(boundColumn(input, "measure"));
    // The pull the Result recorded, read from the capture: evidence names the
    // collection the figure came from, and this proves it reached the drawer.
    const provenance = (input.result.manifest ?? {})["provenance"] as
      | { pull_id?: string }
      | undefined;
    const pull = provenance?.pull_id ?? "";
    expect(pull).toMatch(EXECUTED_ID);
    expect(evidence).toContain(pull);
  });

  it.each(["degraded", "refused", "unavailable"] as const)(
    "keeps returned rows inspectable beside the %s state",
    async (outcome) => {
      for (const surface of ["console", "mcp", "share"] as const) {
        const input = goldenInput();
        input.result.outcome = outcome;
        const { container } = await mount(surface, input);
        expect(container.querySelector(`[data-viz-state="${outcome}"]`)).not.toBeNull();
        expect(container.querySelectorAll("[data-viz-row-key]")).toHaveLength(
          input.result.rows.length,
        );
        cleanup();
      }
    },
  );

  it("does not promise a table for an empty Result", async () => {
    for (const surface of ["console", "mcp", "share"] as const) {
      const input = goldenInput();
      input.result = { ...input.result, outcome: "empty", rows: [], row_count: 0 };
      const { container } = await mount(surface, input);
      expect(container.querySelector('[data-viz-state="empty"]')).not.toBeNull();
      expect(container.querySelector("[data-viz-table-fallback]")).toBeNull();
      cleanup();
    }
  });

  it("does not promise a fallback when an empty Result also lacks a renderer", async () => {
    const input = goldenInput();
    input.result = { ...input.result, outcome: "empty", rows: [], row_count: 0 };
    input.spec.document.family = "heatmap" as never;
    const { container } = await mount("console", input);
    expect(container.querySelector("[data-viz-table-fallback]")).toBeNull();
    expect(container.textContent).not.toContain("table below");
    expect(container.textContent).toContain("returned no rows");
  });

  it("discloses a server-declared stale state without calculating it", async () => {
    for (const surface of ["console", "mcp", "share"] as const) {
      const input = goldenInput();
      input.result.manifest = { ...input.result.manifest, freshness: { state: "stale" } };
      const { container } = await mount(surface, input);
      expect(container.querySelector("dl")?.textContent).toContain("stale");
      cleanup();
    }
  });

  it("refuses the same unavailable frozen runtime build on every surface", async () => {
    const messages = [];
    for (const surface of ["console", "mcp", "share"] as const) {
      const input = goldenInput();
      input.pins.runtime_build = "@toorow/card-shell/viz@0.0.0+deadbee";
      const { container } = await mount(surface, input);
      const message = container.querySelector('[data-viz-state="refused"]')?.textContent ?? "";
      cleanup();
      messages.push(message);
    }
    expect(messages[1]).toBe(messages[0]);
    expect(messages[2]).toBe(messages[0]);
    expect(messages[0]).toContain("deadbee");
    expect(messages[0]).toContain("deploy the build whose hash matches");
  });
});
