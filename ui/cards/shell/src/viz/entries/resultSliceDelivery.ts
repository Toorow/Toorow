import {
  RESULT_META_KEY,
  type McpToolResultParams,
} from "@toorow/shell/src/mcpApp";

import type { CellValue, RenderInput } from "../contracts";

export const RESULT_SLICE_TOOL = "app_read_result_slice";
export const RESULT_SLICE_LIMIT = 100;
export const MAX_INITIAL_PROJECTION_ROWS = 200;
export const MAX_INITIAL_PROJECTION_BYTES = 65_536;
export const MAX_RESULT_SLICE_COLUMNS = 100;
export const MAX_RESULT_SLICE_ROWS = 500;
export const MAX_RESULT_SLICE_BYTES = 262_144;
export const MAX_RESULT_CURSOR_BYTES = 512;
export const MAX_FROZEN_RESULT_ROWS = 1_000;
export const MAX_RESULT_HANDLE_CHARS = 29;

export interface ResultSliceWindow {
  input: RenderInput;
  projectId: string;
  ownerReference: Record<string, unknown>;
  handle: string;
  allowedColumns: string[];
  offset: number;
  totalRows: number;
  hasMore: boolean;
  nextCursor: string | null;
}

type JsonRecord = Record<string, unknown>;

function record(value: unknown): JsonRecord | null {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? (value as JsonRecord)
    : null;
}

function nonEmptyString(value: unknown): value is string {
  return typeof value === "string" && value.length > 0;
}

function integer(value: unknown): value is number {
  return typeof value === "number" && Number.isSafeInteger(value) && value >= 0;
}

function sameStrings(value: unknown, expected: string[]): value is string[] {
  return (
    Array.isArray(value) &&
    value.length === expected.length &&
    value.every((item, index) => item === expected[index])
  );
}

function cell(value: unknown): value is CellValue {
  return (
    value === null ||
    typeof value === "string" ||
    typeof value === "number" ||
    typeof value === "boolean" ||
    (Array.isArray(value) && value.every((item) => typeof item === "string"))
  );
}

function rows(value: unknown, columns: string[]): value is Record<string, CellValue>[] {
  const allowed = new Set(columns);
  return (
    Array.isArray(value) &&
    value.every((item) => {
      const row = record(item);
      return (
        !!row &&
        Object.keys(row).length === columns.length &&
        Object.entries(row).every(([key, itemValue]) => allowed.has(key) && cell(itemValue))
      );
    })
  );
}

function malformed(message: string): never {
  throw new Error(`Malformed large Result delivery: ${message}`);
}

function serializedBytes(value: unknown): number {
  try {
    return new TextEncoder().encode(JSON.stringify(value)).byteLength;
  } catch {
    return Number.POSITIVE_INFINITY;
  }
}

function boundedString(value: unknown, maxBytes: number): value is string {
  return nonEmptyString(value) && new TextEncoder().encode(value).byteLength <= maxBytes;
}

/** Decode app-only capabilities, hydrating only the bounded initial row window. */
export function decodeInitialResultSlice(
  input: RenderInput,
  envelope: Record<string, unknown>,
  meta?: Record<string, unknown>,
): ResultSliceWindow | null {
  const result = record(meta?.[RESULT_META_KEY]);
  if (!result || result.projection_size === undefined || result.projection_size === "small") {
    return null;
  }
  if (result.projection_size !== "large") malformed("the projection size is not supported");

  const deepLink = record(envelope.deep_link);
  const projectId = deepLink?.project_id;
  const ownerReference = record(deepLink?.owner_reference);
  const handle = result.result_handle;
  const allowedColumns = result.allowed_columns;
  const totalRows = result.row_count;
  const initial = result.initial_projection;
  const nextCursor = result.next_cursor;
  if (
    !nonEmptyString(projectId) ||
    !ownerReference ||
    !boundedString(handle, MAX_RESULT_HANDLE_CHARS) ||
    !Array.isArray(allowedColumns) ||
    allowedColumns.length === 0 ||
    allowedColumns.length > MAX_RESULT_SLICE_COLUMNS ||
    !allowedColumns.every(nonEmptyString) ||
    new Set(allowedColumns).size !== allowedColumns.length ||
    !integer(totalRows) ||
    totalRows > MAX_FROZEN_RESULT_ROWS ||
    totalRows !== input.result.row_count ||
    !rows(initial, allowedColumns) ||
    initial.length > MAX_INITIAL_PROJECTION_ROWS ||
    serializedBytes(initial) > MAX_INITIAL_PROJECTION_BYTES ||
    initial.length > totalRows ||
    !(nextCursor === null || boundedString(nextCursor, MAX_RESULT_CURSOR_BYTES)) ||
    (initial.length < totalRows) !== (nextCursor !== null)
  ) {
    malformed("required capabilities or bounded projection are invalid");
  }
  if (
    result.result_id !== input.result.result_id ||
    result.content_hash !== input.result.content_hash ||
    JSON.stringify(input.result.rows) !== JSON.stringify(initial)
  ) {
    throw new Error("Large Result delivery does not match the immutable RenderInput");
  }

  return {
    input: { ...input, result: { ...input.result, rows: initial } },
    projectId,
    ownerReference,
    handle,
    allowedColumns: [...allowedColumns],
    offset: 0,
    totalRows,
    hasMore: nextCursor !== null,
    nextCursor: nextCursor as string | null,
  };
}

export function resultSliceRequest(
  window: ResultSliceWindow,
  feedbackContext?: Record<string, unknown> | null,
): Record<string, unknown> {
  if (!window.nextCursor) throw new Error("This Result has no next slice");
  return {
    project_id: window.projectId,
    handle: window.handle,
    columns: [...window.allowedColumns],
    cursor: window.nextCursor,
    limit: RESULT_SLICE_LIMIT,
    ...(feedbackContext ? { feedback_context: feedbackContext } : {}),
  };
}

/** Validate the frozen page and replace (never append to) the visible window. */
export function reduceResultSlice(
  current: ResultSliceWindow,
  toolResult: McpToolResultParams,
): ResultSliceWindow {
  const page = record(toolResult.structuredContent);
  if (!page) malformed("the slice response is absent");
  const pageRows = page.rows;
  const offset = page.offset;
  const returnedRows = page.returned_rows;
  const hasMore = page.has_more;
  const nextCursor = page.next_cursor;
  if (
    current.allowedColumns.length > MAX_RESULT_SLICE_COLUMNS ||
    !Array.isArray(pageRows) ||
    pageRows.length > MAX_RESULT_SLICE_ROWS ||
    pageRows.length > RESULT_SLICE_LIMIT ||
    serializedBytes(page) > MAX_RESULT_SLICE_BYTES
  ) {
    malformed("the bounded page limits were exceeded");
  }
  if (
    page.result_id !== current.input.result.result_id ||
    page.content_hash !== current.input.result.content_hash ||
    !sameStrings(page.columns, current.allowedColumns) ||
    !rows(pageRows, current.allowedColumns) ||
    !integer(offset) ||
    offset > MAX_FROZEN_RESULT_ROWS ||
    !integer(returnedRows) ||
    offset !== current.offset + current.input.result.rows.length ||
    returnedRows !== pageRows.length ||
    page.total_rows !== current.totalRows ||
    offset + pageRows.length > current.totalRows ||
    offset + pageRows.length > MAX_FROZEN_RESULT_ROWS ||
    typeof hasMore !== "boolean" ||
    hasMore !== (offset + pageRows.length < current.totalRows) ||
    (hasMore ? !boundedString(nextCursor, MAX_RESULT_CURSOR_BYTES) : nextCursor !== null) ||
    (hasMore && (pageRows.length === 0 || nextCursor === current.nextCursor))
  ) {
    malformed("the slice response contradicts the frozen Result");
  }
  return {
    ...current,
    input: { ...current.input, result: { ...current.input.result, rows: pageRows } },
    offset,
    hasMore,
    nextCursor: nextCursor as string | null,
  };
}
