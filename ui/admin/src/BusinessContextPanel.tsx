import { useMemo } from "react";
import { DataCollectionLayout, EvidenceTime, StateValue, type DataColumn } from "./data/DataCollectionLayout";
import { useDataSurface, usePageCursor } from "./data/dataSurface";
import ManualEventAnnotations from "./data/ManualEventAnnotations";
import { buildPath, useRoute } from "./shell/router";
import { resolvableHref } from "./shell/routeHref";
import { Button, ObjectId } from "./ui";

export default function BusinessContextPanel({
  projectId,
  onOpenEventConfiguration,
}: {
  projectId?: string;
  onOpenEventConfiguration?: (id: string) => void;
}) {
  const { route, navigate } = useRoute();
  // Same reading as Sources: the pager is wired to the envelope, and the
  // envelope decides whether there is anything to page. `_compose_collection`
  // sends the page metadata for `imports` and `connectors` only
  // (`server/core/data_surface.py:972`), so nothing is drawn here yet.
  const { cursor, canGoBack, goToFirstPage, goToNextPage, goToPreviousPage } = usePageCursor();
  const { state, reload } = useDataSurface(projectId, "events", undefined, { cursor: cursor || undefined });
  // THE EMPTY LIST NAMES ITS GESTURE, AND THE GESTURE IS REACHABLE. The sentence
  // below sends a person to a Datastream and the screen offered no way to reach
  // one: the reader was told what to do and left to find it. The address is
  // built by the router that will have to resolve it — never composed here — and
  // it is asked whether it resolves before it is offered, so an unresolved scope
  // (the sandbox mounts this panel outside a Project) yields no button rather
  // than a live-looking control landing on the unknown-route screen.
  const datastreamsHref = useMemo(() => {
    if (route.scope !== "project" || route.globalSurface !== null) return null;
    return resolvableHref(buildPath({
      ...route,
      workspace: "data",
      section: "datastreams",
      lens: null,
      objectType: null,
      objectId: null,
      tab: null,
      versionId: null,
      evidenceId: null,
      action: null,
      query: {},
    }));
  }, [route]);
  const columns = useMemo<readonly DataColumn[]>(() => [
    {
      key: "configuration",
      label: "Event Configuration",
      render: (item) => (
        <div>
          <strong className="block text-text">{item.name ?? item.object_ref.id}</strong>
          <ObjectId value={item.object_ref.id} title="Object" />
        </div>
      ),
    },
    { key: "datastream", label: "Datastream", render: (item) => <span className="font-mono text-ui">{item.datastream_ref?.id ?? "Unavailable"}</span> },
    { key: "lifecycle", label: "Lifecycle", render: (item) => <StateValue value={item.states.lifecycle} /> },
    { key: "review", label: "Review", render: (item) => <StateValue value={item.states.review} /> },
    { key: "binding", label: "Version binding", render: (item) => <StateValue value={item.states.binding} /> },
    { key: "evidence", label: "Evidence as of", render: (item) => <EvidenceTime value={item.evidence_as_of} /> },
  ], []);

  return (
    <>
    <DataCollectionLayout
      title="Events"
      description="Datastream-owned Event Configurations. Business Timeline observations remain a linked downstream projection."
      emptyTitle="No Datastream of this Project emits an event"
      emptyDescription="Events come from a Datastream that was armed to emit them. Open a Datastream, go to its Events tab, and arm the event stream it declares."
      emptyAction={datastreamsHref ? (
        <Button asChild variant="secondary">
          <a
            href={datastreamsHref}
            onClick={(event) => {
              // The address is real, so a middle click and a copied link both
              // work; the plain click stays inside the shell instead of
              // reloading the whole console.
              if (event.metaKey || event.ctrlKey || event.shiftKey || event.button !== 0) return;
              event.preventDefault();
              navigate({ workspace: "data", section: "datastreams", objectType: null, objectId: null, tab: null, action: null });
            }}
          >
            Open Datastreams
          </a>
        </Button>
      ) : undefined}
      state={state}
      reload={reload}
      columns={columns}
      onOpen={onOpenEventConfiguration ? (item) => onOpenEventConfiguration(item.object_ref.id) : undefined}
      onNextPage={() => { if (state.status === "ready" && state.envelope.next_cursor) goToNextPage(state.envelope.next_cursor); }}
      onPreviousPage={canGoBack ? goToPreviousPage : undefined}
      onFirstPage={cursor ? goToFirstPage : undefined}
    />
    {/* THE OTHER HALF OF THIS TABLE'S STORE, on the surface where events are
        read. `app.context_events` holds Connector observations — listed above,
        owned by their Datastream — AND manual annotations, which had no screen
        at all: two HTTP routes reachable and called by nobody, creation only
        through an MCP tool. They are read as causes when this Project's results
        are narrated, so they carry a human path of correction (context-hub.md,
        amendment of 2026-08-17). Kept as its own section rather than mixed into
        the table above: the two halves have different owners, and merging them
        would make Context annotations look Datastream-owned. */}
    <ManualEventAnnotations projectId={projectId} />
    </>
  );
}
