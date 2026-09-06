import { useCallback, useEffect, useMemo, useState } from "react";
import { AlertCircle, ChevronRight, Database, Search } from "lucide-react";
import { getDataSurface, type DataSurfaceItem } from "../data/dataSurface";
// `ConnectorMark` carries its own geometry. The shell's `ConnectorLogo` had
// none but the legacy `.provider-logo` class, so its mark had NO size wherever
// `application.css` is not loaded — which is every sandbox capture, and the
// reason the rail's logos were invisible the first time it was looked at. That
// component had exactly two callers, this was the second, and it is now gone.
import { Button, ConnectorMark, NavBranch, NavSubItem, Status, TONE_TEXT } from "../ui";
import { useRoute } from "./router";

type LoadState = "loading" | "error" | "ready";
interface Group { key: string; label: string; streams: DataSurfaceItem[] }

const KIND_LABELS: Record<string, string> = {
  connector_pull: "Connector pull",
  managed_feed: "Managed feed",
  external_bq: "External BigQuery",
  unavailable: "Unclassified",
};

function groupsOf(items: DataSurfaceItem[]): Group[] {
  const groups = new Map<string, DataSurfaceItem[]>();
  for (const item of items) {
    const key = item.source_kind || "unavailable";
    groups.set(key, [...(groups.get(key) ?? []), item]);
  }
  return [...groups.entries()].map(([key, streams]) => ({ key, label: KIND_LABELS[key] ?? key, streams }));
}

function needsAttention(item: DataSurfaceItem): boolean {
  return Object.values(item.states).some((state) =>
    ["blocked", "failed", "degraded", "rejected", "revoked", "provider_denied", "populate_failed"].includes(
      state.toLowerCase(),
    ),
  );
}

export default function DataTree() {
  const { route, navigate } = useRoute();
  const [query, setQuery] = useState("");
  const [state, setState] = useState<LoadState>("loading");
  const [error, setError] = useState("");
  const [streams, setStreams] = useState<DataSurfaceItem[]>([]);
  const [attempt, setAttempt] = useState(0);
  const retry = useCallback(() => setAttempt((value) => value + 1), []);

  useEffect(() => {
    let alive = true;
    setState("loading");
    setError("");
    if (!route.projectId) {
      setState("error");
      setError("No Project in scope.");
      return;
    }
    void getDataSurface(route.projectId, "datastreams")
      .then((envelope) => {
        if (!alive) return;
        setStreams(envelope.items);
        setState("ready");
      })
      .catch((reason: unknown) => {
        if (!alive) return;
        setStreams([]);
        setError(reason instanceof Error ? reason.message : "Request failed");
        setState("error");
      });
    return () => { alive = false; };
  }, [route.projectId, attempt]);

  const groups = useMemo(() => {
    const normalized = query.trim().toLowerCase();
    return groupsOf(streams)
      .map((group) => ({
        ...group,
        streams: group.streams.filter((stream) =>
          !normalized || (stream.name ?? stream.object_ref.id).toLowerCase().includes(normalized),
        ),
      }))
      .filter((group) => group.streams.length > 0);
  }, [streams, query]);

  const openDatastream = (id: string) => navigate({
    workspace: "data",
    section: "datastreams",
    objectType: "datastream",
    objectId: id,
    tab: "overview",
    versionId: null,
    action: null,
  });

  if (state === "loading") return <p className="px-2 py-3 text-xs text-text-secondary" role="status" data-testid="tree-loading">Loading Datastreams...</p>;
  if (state === "error") {
    return (
      <div className={`space-y-2 px-2 py-3 text-xs ${TONE_TEXT.error}`} data-testid="tree-error">
        <p className="flex gap-2"><AlertCircle className="size-4 shrink-0" />Could not load Datastreams. {error}</p>
        <Button type="button" variant="ghost" size="sm" onClick={retry}>Retry</Button>
      </div>
    );
  }
  if (streams.length === 0) return <p className="px-2 py-3 text-xs text-text-secondary" data-testid="tree-empty">No Datastream yet.</p>;

  return (
    <div className="space-y-2 py-2" data-testid="datastream-tree">
      <label className="flex h-8 items-center gap-2 rounded-control border border-divider-base bg-surface-light px-2 text-text-secondary dark:border-divider-medium dark:bg-surface-dark-elevated">
        <Search className="size-3.5" aria-hidden="true" />
        <span className="sr-only">Find Datastreams</span>
        <input className="min-w-0 flex-1 bg-transparent text-xs text-text outline-none" type="search" placeholder="Find Datastreams" aria-label="Find Datastreams" value={query} onChange={(event) => setQuery(event.target.value)} />
      </label>
      {groups.length === 0 ? <p className="px-2 py-2 text-xs text-text-secondary" data-testid="tree-no-match">No Datastream matches "{query.trim()}".</p> : null}
      {groups.map((group) => (
        <details key={group.key} open className="group">
          <summary className="flex min-h-8 cursor-pointer list-none items-center gap-2 rounded-control px-2 text-xs font-semibold text-text-secondary hover:bg-divider-subtle">
            <Database className="size-3.5" aria-hidden="true" />
            <span className="min-w-0 flex-1 truncate">{group.label}</span>
            <span className="font-numeric">{group.streams.length}</span>
            <ChevronRight className="size-3.5 transition-transform group-open:rotate-90" aria-hidden="true" />
          </summary>
          <NavBranch tight>
            {group.streams.map((stream) => (
              <NavSubItem
                key={stream.object_ref.id}
                active={route.objectId === stream.object_ref.id}
                icon={<ConnectorMark provider={stream.connector_ref?.id ?? stream.source_kind} size={20} />}
                // A STATE, SO IT IS A `Status` (76-2). It was a bare amber dot: no shape, no
                // word, and nothing a reader who cannot see the colour could reach. The
                // warning mark is a diamond and carries its meaning in the title.
                trailing={
                  needsAttention(stream) ? (
                    <span title="Needs attention">
                      <Status tone="warning">
                        <span className="sr-only">Needs attention</span>
                      </Status>
                    </span>
                  ) : null
                }
                wrap
                className="h-auto min-h-9 py-1.5 text-caption"
                title={stream.name ?? stream.object_ref.id}
                data-testid={`stream-${stream.object_ref.id}`}
                onClick={() => openDatastream(stream.object_ref.id)}
              >
                {/* A generated Datastream name carries its connector and its
                    selection ("Google Search Console - Custom selection
                    [sc-domain:example.com]"): unwrapped it took four lines of a
                    190px rail and read as a paragraph. Two lines is the cap, the
                    full name stays reachable through the tooltip. */}
                <span className="line-clamp-2 break-words">{stream.name ?? stream.object_ref.id}</span>
              </NavSubItem>
            ))}
          </NavBranch>
        </details>
      ))}
    </div>
  );
}