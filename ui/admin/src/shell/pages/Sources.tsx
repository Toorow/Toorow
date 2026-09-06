import { useMemo } from "react";
import ConnectButton from "../../ConnectButton";
import ConnectGoogleButton from "../../authorizations/ConnectGoogleButton";
import { DataCollectionLayout, EvidenceTime, StateValue, type DataColumn } from "../../data/DataCollectionLayout";
import { useDataSurface, usePageCursor } from "../../data/dataSurface";
import { ConnectorMark, ObjectId, Status } from "../../ui";

/** `organization` / `delegated` — the SHARING half of this page's function. */
function ownership(scope: unknown, ownerOrgName?: string): { label: string; tone: "success" | "warning" | "neutral"; hint: string } {
  if (scope === "organization") {
    return {
      label: "This organization",
      tone: "success",
      hint: "Owned here — this Project can reconnect it itself.",
    };
  }
  if (scope === "delegated") {
    return {
      label: ownerOrgName ? `Provided by ${ownerOrgName}` : "Shared with this Project",
      tone: "warning",
      hint: ownerOrgName ? `Provided by ${ownerOrgName}. Repairing it needs its owner, not this Project.` : "Provided by another organization. Repairing it needs its owner, not this Project.",
    };
  }
  return { label: "Unavailable", tone: "neutral", hint: "No ownership evidence was returned." };
}

export default function Sources({
  projectId,
  onOpenSourceAccount,
  onOpenConnector,
}: {
  projectId?: string;
  onOpenSourceAccount?: (id: string) => void;
  /** Opens the Connector Workbench for the Connector serving a Source Account.
   *
   *  A callback, not an address. Only the mount owns the Organization segment
   *  `parsePath` requires, and the cell that used to compose
   *  `/data/connectors?id={id}` proved what happens without one: the address was
   *  refused on its first segment and the only gesture the column offered opened
   *  the unknown-route screen. Absent, the Connector is still NAMED — which one
   *  serves an account is the reading of the column — it just offers nothing. */
  onOpenConnector?: (id: string) => void;
}) {
  // Half the Data collections carried a pager and half carried none, on the same
  // layout and against the same envelope. Since 2026-08-18 `_compose_collection`
  // composes the page metadata for `sources` too, so the footer and the pager
  // below are live — the wiring never changed, only the envelope did.
  const { cursor, canGoBack, goToFirstPage, goToNextPage, goToPreviousPage } = usePageCursor();
  const { state, reload } = useDataSurface(projectId, "sources", undefined, { cursor: cursor || undefined });
  const columns = useMemo<readonly DataColumn[]>(() => [
    {
      key: "account",
      label: "Source Account",
      render: (item) => (
        <div className="flex items-center gap-3">
          <ConnectorMark provider={item.connector_ref?.id} />
          <div className="min-w-0">
            <strong className="block truncate text-text">{item.label ?? item.object_ref.id}</strong>
            <ObjectId value={item.object_ref.id} title="Source Account" />
          </div>
        </div>
      ),
    },
    {
      key: "ownership",
      label: "Ownership",
      render: (item) => {
        const scope = item.authorization_ref?.owner_scope;
        const ownerOrgName = (item.authorization_ref as { owner_org_name?: string } | undefined)?.owner_org_name;
        const read = ownership(scope, ownerOrgName);
        return <span title={read.hint}><Status tone={read.tone}>{read.label}</Status></span>;
      },
    },
    {
      key: "authorization",
      label: "Authorization",
      render: (item) => <StateValue value={item.states.authorization ?? "unavailable"} />,
    },
    { key: "availability", label: "Availability", render: (item) => <StateValue value={item.states.availability} /> },
    { key: "freshness", label: "Freshness", render: (item) => <StateValue value={item.states.freshness} /> },
    {
      key: "usage",
      label: "Used by",
      render: (item) => {
        const count = (item.evidence as { used_by_count?: unknown } | undefined)?.used_by_count;
        if (typeof count !== "number") return <StateValue value={item.states.usage} />;
        return (
          <span className="text-ui text-text">
            {count === 0 ? "Nothing yet" : `${count} Datastream${count === 1 ? "" : "s"}`}
          </span>
        );
      },
    },
    {
      key: "connector",
      label: "Connector",
      // The cell used to render `<a href="/data/connectors?id={id}">`, an address
      // this console's own router refuses on its first segment
      // (`router.tsx:130`, "Expected an organization route"): the only gesture
      // the column offered opened the unknown-route screen. Same defect as the
      // wizard's owner links (57.4) and the Workbench tab band (57.5), on the
      // Sources collection.
      //
      // It is not repaired into a second canonical address either: the cell now
      // asks the mount to navigate, exactly as the row already does through
      // `onOpenSourceAccount`. `ContentRouter` answers with
      // `openDataObject("connectors", "connector", id)`, the one resolver every
      // other Data object goes through — the remedy story 57.5 used for the
      // wizard's success link.
      //
      // `ObjectNav`'s rule, applied to a cell: with a callback and no address it
      // is a `<button>`, so it is reachable by keyboard and announced as a
      // control; with neither it is inert text that still names the Connector.
      render: (item) => {
        const connectorId = item.connector_ref?.id;
        if (!connectorId) return <span className="font-mono text-ui">Unavailable</span>;
        if (!onOpenConnector) return <span className="font-mono text-ui">{connectorId}</span>;
        return (
          <button
            type="button"
            className="font-mono text-ui text-text-link underline"
            onClick={(event) => {
              // The row opens the Source Account; this cell opens the Connector.
              // Without this the click would do both, and land on the other one.
              event.stopPropagation();
              onOpenConnector(connectorId);
            }}
          >
            {connectorId}
          </button>
        );
      },
    },
    { key: "evidence", label: "Last observed", render: (item) => <EvidenceTime value={item.evidence_as_of} /> },
  ], []);

  // THE GOOGLE DOOR IS ITS OWN GESTURE, and it comes first because it is the one
  // this screen is reached FOR: `Add Datastream` -> `Set up source access` lands
  // here, and until 2026-08-11 the only control on the page sent every provider
  // to Nango — which cannot start a `google_direct` consent, the authorization
  // every Google Connector needs. The wizard told a person to create the
  // authorization here; here offered no gesture that could.
  const connectActions = (
    <div className="flex gap-3">
      <ConnectGoogleButton projectId={projectId} />
      <ConnectButton projectId={projectId} onSuccess={reload} label="Connect another source" />
    </div>
  );

  return (
    <DataCollectionLayout
      title="Sources"
      description="Provider accounts this Project can use: who owns them, whether they are shared, and what depends on them."
      emptyTitle="No source account reaches this Project"
      emptyDescription="Nobody has connected a provider account to this Project yet. Connect Google, or connect another source, with the buttons above — that consent is what puts an account here."
      state={state}
      reload={reload}
      columns={columns}
      actions={connectActions}
      onOpen={onOpenSourceAccount ? (item) => onOpenSourceAccount(item.object_ref.id) : undefined}
      onNextPage={() => { if (state.status === "ready" && state.envelope.next_cursor) goToNextPage(state.envelope.next_cursor); }}
      onPreviousPage={canGoBack ? goToPreviousPage : undefined}
      onFirstPage={cursor ? goToFirstPage : undefined}
    />
  );
}
