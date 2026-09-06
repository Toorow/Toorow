import { describe, expect, it } from "vitest";

import { RESULT_META_KEY, type McpToolResultParams } from "@toorow/shell/src/mcpApp";

import { renderInput } from "../__tests__/fixtures";
import {
  decodeInitialResultSlice,
  MAX_FROZEN_RESULT_ROWS,
  MAX_INITIAL_PROJECTION_BYTES,
  MAX_INITIAL_PROJECTION_ROWS,
  MAX_RESULT_CURSOR_BYTES,
  MAX_RESULT_HANDLE_CHARS,
  MAX_RESULT_SLICE_BYTES,
  MAX_RESULT_SLICE_COLUMNS,
  MAX_RESULT_SLICE_ROWS,
  reduceResultSlice,
  resultSliceRequest,
} from "./resultSliceDelivery";

const initialRows = [
  { channel: "organic", sessions: 1240 },
  { channel: "paid", sessions: 880 },
];

function largeMeta(overrides: Record<string, unknown> = {}) {
  return {
    [RESULT_META_KEY]: {
      projection_size: "large",
      result_id: "res_EXAMPLE_0001",
      content_hash: "sha256:examplehash0001",
      result_handle: "rh_opaque",
      allowed_columns: ["channel", "sessions"],
      row_count: 4,
      initial_projection: initialRows,
      next_cursor: "cursor_2",
      ...overrides,
    },
  };
}

describe("large Result slice delivery", () => {
  it("hydrates the bounded initial projection while keeping transport capabilities outside RenderInput", () => {
    const input = renderInput({
      result: { ...renderInput().result, rows: initialRows, row_count: 4, truncated: false },
    });
    const window = decodeInitialResultSlice(
      input,
      {
        deep_link: {
          project_id: "project_1",
          owner_reference: { workspace: "analyze", object_id: "res_EXAMPLE_0001" },
        },
      },
      largeMeta(),
    );

    expect(window?.input.result.rows).toEqual(initialRows);
    expect(window?.input.result.truncated).toBe(false);
    expect(window?.ownerReference).toEqual({
      workspace: "analyze",
      object_id: "res_EXAMPLE_0001",
    });
    expect(window?.input).not.toHaveProperty("handle");
    expect(window?.input).not.toHaveProperty("cursor");
    expect(resultSliceRequest(window!)).toEqual({
      project_id: "project_1",
      handle: "rh_opaque",
      columns: ["channel", "sessions"],
      cursor: "cursor_2",
      limit: 100,
    });
  });

  it("returns null for small/legacy metadata and rejects malformed large metadata", () => {
    expect(
      decodeInitialResultSlice(renderInput(), {}, { [RESULT_META_KEY]: { projection_size: "small" } }),
    ).toBeNull();
    expect(() =>
      decodeInitialResultSlice(
        renderInput(),
        {},
        { [RESULT_META_KEY]: { projection_size: "large-v2" } },
      ),
    ).toThrow(/projection size/i);
    expect(() =>
      decodeInitialResultSlice(renderInput(), {}, largeMeta({ result_handle: "" })),
    ).toThrow(/malformed large Result/i);
    expect(() =>
      decodeInitialResultSlice(
        renderInput({ result: { ...renderInput().result, rows: initialRows, row_count: 4 } }),
        {
          deep_link: {
            project_id: "project_1",
            owner_reference: { workspace: "analyze" },
          },
        },
        largeMeta({ content_hash: "other" }),
      ),
    ).toThrow(/does not match/i);
  });

  it("pins the client bounds and refuses oversized initial metadata", () => {
    expect({
      initialRows: MAX_INITIAL_PROJECTION_ROWS,
      initialBytes: MAX_INITIAL_PROJECTION_BYTES,
      columns: MAX_RESULT_SLICE_COLUMNS,
      pageRows: MAX_RESULT_SLICE_ROWS,
      pageBytes: MAX_RESULT_SLICE_BYTES,
      cursorBytes: MAX_RESULT_CURSOR_BYTES,
      handleChars: MAX_RESULT_HANDLE_CHARS,
      frozenRows: MAX_FROZEN_RESULT_ROWS,
    }).toEqual({
      initialRows: 200,
      initialBytes: 65_536,
      columns: 100,
      pageRows: 500,
      pageBytes: 262_144,
      cursorBytes: 512,
      handleChars: 29,
      frozenRows: 1_000,
    });
    const oversizedRows = Array.from({ length: 201 }, (_, index) => ({
      channel: `channel-${index}`,
      sessions: index,
    }));
    const input = renderInput({
      result: { ...renderInput().result, rows: oversizedRows, row_count: 201 },
    });
    expect(() =>
      decodeInitialResultSlice(
        input,
        {
          deep_link: {
            project_id: "project_1",
            owner_reference: { workspace: "analyze" },
          },
        },
        largeMeta({
          row_count: 201,
          initial_projection: oversizedRows,
          next_cursor: null,
        }),
      ),
    ).toThrow(/bounded projection/i);
    expect(() =>
      decodeInitialResultSlice(
        renderInput({ result: { ...renderInput().result, rows: initialRows, row_count: 4 } }),
        {
          deep_link: {
            project_id: "project_1",
            owner_reference: { workspace: "analyze" },
          },
        },
        largeMeta({ result_handle: "h".repeat(MAX_RESULT_HANDLE_CHARS + 1) }),
      ),
    ).toThrow(/bounded projection/i);
    expect(() =>
      decodeInitialResultSlice(
        renderInput({ result: { ...renderInput().result, rows: initialRows, row_count: 4 } }),
        {
          deep_link: {
            project_id: "project_1",
            owner_reference: { workspace: "analyze" },
          },
        },
        largeMeta({ next_cursor: "c".repeat(MAX_RESULT_CURSOR_BYTES + 1) }),
      ),
    ).toThrow(/bounded projection/i);
    const byteLargeRows = [
      { channel: "x".repeat(MAX_INITIAL_PROJECTION_BYTES), sessions: 1 },
      { channel: "paid", sessions: 2 },
    ];
    expect(() =>
      decodeInitialResultSlice(
        renderInput({ result: { ...renderInput().result, rows: byteLargeRows, row_count: 2 } }),
        {
          deep_link: {
            project_id: "project_1",
            owner_reference: { workspace: "analyze" },
          },
        },
        largeMeta({
          row_count: 2,
          initial_projection: byteLargeRows,
          next_cursor: null,
        }),
      ),
    ).toThrow(/bounded projection/i);
  });

  it("accepts an honestly empty bounded window without inventing a row range", () => {
    const input = renderInput({
      result: { ...renderInput().result, rows: [], row_count: 0, truncated: false },
    });
    const window = decodeInitialResultSlice(
      input,
      {
        deep_link: {
          project_id: "project_1",
          owner_reference: { workspace: "analyze" },
        },
      },
      largeMeta({ row_count: 0, initial_projection: [], next_cursor: null }),
    );
    expect(window?.input.result.rows).toEqual([]);
    expect(window?.hasMore).toBe(false);
  });

  it("replaces one immutable window with the next and rejects inconsistent pages", () => {
    const input = renderInput({
      result: { ...renderInput().result, rows: initialRows, row_count: 4, truncated: false },
    });
    const initial = decodeInitialResultSlice(
      input,
      {
        deep_link: {
          project_id: "project_1",
          owner_reference: { workspace: "analyze" },
        },
      },
      largeMeta(),
    )!;
    const result = {
      structuredContent: {
        result_id: input.result.result_id,
        content_hash: input.result.content_hash,
        columns: ["channel", "sessions"],
        rows: [{ channel: "email", sessions: 410 }],
        offset: 2,
        returned_rows: 1,
        total_rows: 4,
        has_more: true,
        next_cursor: "cursor_3",
      },
    } as unknown as McpToolResultParams;

    const next = reduceResultSlice(initial, result);
    expect(next.input.result.rows).toEqual([{ channel: "email", sessions: 410 }]);
    expect(next.offset).toBe(2);
    expect(next.nextCursor).toBe("cursor_3");
    expect(() =>
      reduceResultSlice(initial, {
        ...result,
        structuredContent: { ...result.structuredContent, total_rows: 5 },
      } as unknown as McpToolResultParams),
    ).toThrow(/malformed large Result delivery/i);
    expect(() =>
      reduceResultSlice(initial, {
        structuredContent: {
          ...result.structuredContent,
          rows: Array.from({ length: 101 }, (_, index) => ({
            channel: `channel-${index}`,
            sessions: index,
          })),
          returned_rows: 101,
          total_rows: 103,
        },
      } as unknown as McpToolResultParams),
    ).toThrow(/bounded page/i);
    expect(() =>
      reduceResultSlice(initial, {
        structuredContent: {
          ...result.structuredContent,
          rows: [{ channel: "x".repeat(MAX_RESULT_SLICE_BYTES), sessions: 1 }],
          returned_rows: 1,
        },
      } as unknown as McpToolResultParams),
    ).toThrow(/bounded page/i);
    expect(() =>
      reduceResultSlice(
        { ...initial, offset: 2, totalRows: 3 },
        {
          structuredContent: {
            ...result.structuredContent,
            offset: 4,
            total_rows: 3,
            has_more: false,
            next_cursor: null,
          },
        } as unknown as McpToolResultParams,
      ),
    ).toThrow(/frozen Result/i);
  });
});
