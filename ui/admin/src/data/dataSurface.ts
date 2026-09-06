import { useCallback, useEffect, useState } from "react";
import { apiGet } from "../lib/apiFetch";

export type DataLens = "overview" | "datastreams" | "sources" | "imports" | "events" | "connectors";

export interface ObjectRef {
  object_type: string;
  id: string;
  href?: string;
  version?: number | null;
  fingerprint?: string | null;
}

export interface DataSurfaceItem {
  object_ref: ObjectRef;
  project_ref?: ObjectRef;
  datastream_ref?: ObjectRef;
  connector_ref?: ObjectRef;
  /** The authorization's own address, on the `sources` lens only. Every action
   *  the server offers on a connection is keyed by it, and it was absent from
   *  this envelope until 2026-08-03. */
  connection_ref?: ObjectRef;
  active_version_ref?: ObjectRef | null;
  active_version_refs?: Record<string, string | null>;
  published_execution_ref?: ObjectRef | null;
  connector_contract_ref?: ObjectRef | null;
  /** Who owns the authorization behind a Source Account, and how it was
   *  obtained. `owner_scope` is `organization` (repairable here) or `delegated`
   *  (provided by another organization, repairable only by its owner) -- the
   *  sharing half of the Sources function, computed in the query since it was
   *  written and absent from this type until 2026-08-03. */
  authorization_ref?: {
    object_type: string;
    owner_scope?: string;
    kind?: string;
    /** The organization that owns a `delegated` authorization. Composed by
     *  `project_source_account` since it was written and absent from this type,
     *  so every reader had to cast to reach it. */
    owner_org_name?: string | null;
  };
  /** The Connector whose discovery recorded this Source Account, when one did.
   *  `connector_ref` above carries the AUTHORIZATION's provider — for a Google
   *  direct grant that is the string "google", which names no tool. On the wire
   *  since migration 210 and unread until 2026-08-17. */
  discovered_for_connector?: string | null;
  name?: string;
  label?: string;
  connector_id?: string;
  environment?: string;
  source_kind?: string;
  states: Record<string, string>;
  evidence: Record<string, unknown>;
  evidence_as_of: string | null;
  links: Record<string, string>;
  lens?: string;
  object_count?: number;
}

export interface UnavailableReason {
  code: string;
  message: string;
}

export interface DataSurfaceEnvelope {
  schema_version: string;
  project_ref: ObjectRef;
  generated_at: string;
  evidence_as_of: string | null;
  items: DataSurfaceItem[];
  unavailable_reasons: UnavailableReason[];
  allowed_actions: string[];
  total?: number;
  bound?: number;
  next_cursor?: string | null;
  applied_filters?: Record<string, string>;
  filter_options?: { states?: string[]; datastreams?: string[] };
}

const ENDPOINTS: Record<DataLens, string> = {
  overview: "data-overview",
  datastreams: "datastreams",
  sources: "source-accounts",
  imports: "imports",
  events: "event-configurations",
  connectors: "connectors",
};

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === "object" && !Array.isArray(value);
}

function parseEnvelope(value: unknown, lens: DataLens, projectId: string): DataSurfaceEnvelope {
  if (!isRecord(value) || !Array.isArray(value.items) || !Array.isArray(value.unavailable_reasons) || !Array.isArray(value.allowed_actions)) {
    throw new Error("The Data response is malformed.");
  }
  const expected = `data-${lens}.v1`;
  if (value.schema_version !== expected || !isRecord(value.project_ref) || value.project_ref.id !== projectId) {
    throw new Error("The Data response does not match the requested Project or schema.");
  }
  return value as unknown as DataSurfaceEnvelope;
}

export async function getDataSurface(
  projectId: string,
  lens: DataLens,
  objectId?: string,
  options: { q?: string; state?: string; datastream?: string; from?: string; to?: string; limit?: number; cursor?: string } = {},
): Promise<DataSurfaceEnvelope> {
  const scope = projectId.trim();
  if (!scope) throw new Error("Select a Project before opening Data.");
  const suffix = objectId ? `/${encodeURIComponent(objectId)}` : "";
  const query = new URLSearchParams();
  if (options.q) query.set("q", options.q);
  if (options.state) query.set("state", options.state);
  if (options.datastream) query.set("datastream", options.datastream);
  if (options.from) query.set("from", options.from);
  if (options.to) query.set("to", options.to);
  if (options.limit) query.set("limit", String(options.limit));
  if (options.cursor) query.set("cursor", options.cursor);
  const querySuffix = query.size ? `?${query}` : "";
  const value = await apiGet<unknown>(
    `/api/projects/${encodeURIComponent(scope)}/${ENDPOINTS[lens]}${suffix}${querySuffix}`,
    { cache: "no-store" },
  );
  return parseEnvelope(value, lens, scope);
}
export type DataSurfaceState =
  | { status: "loading" }
  | { status: "error"; message: string }
  /** `refreshing` is a SECOND read over an envelope already on screen — a filter
   *  was narrowed, a page was turned. It is not `loading`, because `loading`
   *  means "there is nothing to read yet" and every collection answers it by
   *  replacing the table with a sentence. Typing five characters into the search
   *  box therefore made the table disappear five times, and the row a person was
   *  reading went with it. The table stays, and the screen says it is catching
   *  up. */
  | { status: "ready"; envelope: DataSurfaceEnvelope; refreshing?: boolean };

export function useDataSurface(
  projectId: string | undefined,
  lens: DataLens,
  objectId?: string,
  options: { q?: string; state?: string; datastream?: string; from?: string; to?: string; limit?: number; cursor?: string } = {},
) {
  const scope = projectId?.trim() ?? "";
  const [state, setState] = useState<DataSurfaceState>({ status: "loading" });
  const [attempt, setAttempt] = useState(0);
  const reload = useCallback(() => setAttempt((value) => value + 1), []);

  useEffect(() => {
    let alive = true;
    setState((current) => (current.status === "ready" ? { ...current, refreshing: true } : { status: "loading" }));
    if (!scope) {
      setState({ status: "error", message: "Select a Project before opening Data." });
      return;
    }
    void getDataSurface(scope, lens, objectId, options)
      .then((envelope) => { if (alive) setState({ status: "ready", envelope }); })
      .catch((reason: unknown) => {
        if (alive) setState({ status: "error", message: reason instanceof Error ? reason.message : "Request failed" });
      });
    return () => { alive = false; };
    // `options.datastream` is in this list because it is in the QUERY: it was
    // sent by `getDataSurface` and absent here, so picking a Datastream in the
    // Imports filter changed the request that would be sent and never sent it.
    // A filter the effect does not watch is a filter that does not exist.
  }, [scope, lens, objectId, options.q, options.state, options.datastream, options.from, options.to, options.limit, options.cursor, attempt]);

  return { state, reload };
}

/** The value, held back until typing stops.
 *
 *  Every keystroke used to be a request, and a request is a refetch of the whole
 *  collection. Held for `delay`, "shopify" is one read instead of seven. */
export function useDebouncedValue<T>(value: T, delay = 300): T {
  const [settled, setSettled] = useState(value);
  useEffect(() => {
    if (settled === value) return;
    const timer = setTimeout(() => setSettled(value), delay);
    return () => clearTimeout(timer);
  }, [value, delay, settled]);
  return settled;
}

/** Where the reader is in a collection, and how to walk back out.
 *
 *  A cursor alone can only go forward: the screen knows the address of the NEXT
 *  page and nothing else, so `Previous` could not be offered and the only way
 *  back was `First page` — which loses every page turned in between. The stack
 *  is the history: turning a page pushes the cursor being left, going back pops
 *  it. Every collection paginates the same way, so this lives beside the fetch
 *  rather than being re-derived on each screen. */
export function usePageCursor() {
  const [page, setPage] = useState<{ cursor: string; history: readonly string[] }>({ cursor: "", history: [] });
  const goToNextPage = useCallback((nextCursor: string) => {
    setPage((current) => ({ cursor: nextCursor, history: [...current.history, current.cursor] }));
  }, []);
  const goToPreviousPage = useCallback(() => {
    setPage((current) => (
      current.history.length === 0
        ? current
        : { cursor: current.history[current.history.length - 1], history: current.history.slice(0, -1) }
    ));
  }, []);
  const goToFirstPage = useCallback(() => setPage({ cursor: "", history: [] }), []);
  return {
    cursor: page.cursor,
    /** True once a page has been turned — nothing to go back to on page one. */
    canGoBack: page.history.length > 0,
    goToNextPage,
    goToPreviousPage,
    /** Also the reset a changed filter owes the reader: page 4 of the previous
     *  question is not page 4 of the new one. */
    goToFirstPage,
  };
}
