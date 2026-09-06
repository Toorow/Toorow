import type { ReactNode } from "react";
import BusinessContextPanel from "../BusinessContextPanel";
import DataObjectWorkbench from "../data/DataObjectWorkbench";
import DataOverview from "./pages/DataOverview";
import DataWorkspace from "./pages/DataWorkspace";
import Imports from "./pages/Imports";
import ConnectorsCatalog from "./pages/ConnectorsCatalog";
import Sources from "./pages/Sources";
import { RouterProvider } from "./router";
import { installSandboxApi } from "./sandboxApi";

const PROJECT_ID = "proj_EXAMPLE";
const OBJECT_ID = "object_EXAMPLE";

const WORKBENCHES = {
  SourceAccountWorkbench: {
    section: "sources",
    objectType: "source-account",
    lens: "sources",
    tabs: [
      { key: "overview", label: "Overview" },
      { key: "accounts", label: "Accounts" },
      { key: "health", label: "Health" },
      { key: "used-by", label: "Used by" },
    ],
  },
  ImportWorkbench: {
    section: "imports",
    objectType: "import",
    lens: "imports",
    tabs: [
      { key: "overview", label: "Overview" },
      { key: "raw-evidence", label: "Raw evidence" },
      { key: "validation", label: "Validation" },
      { key: "publication", label: "Publication" },
    ],
  },
  EventConfigurationWorkbench: {
    section: "events",
    objectType: "event-configuration",
    lens: "events",
    tabs: [
      { key: "overview", label: "Overview" },
      { key: "source-mapping", label: "Source mapping" },
      { key: "collection", label: "Collection" },
      { key: "usage", label: "Usage" },
    ],
  },
  ConnectorWorkbench: {
    section: "connectors",
    objectType: "connector",
    lens: "connectors",
    tabs: [
      { key: "overview", label: "Overview" },
      { key: "capabilities", label: "Capabilities" },
      { key: "coverage", label: "Coverage" },
      { key: "versions", label: "Versions" },
    ],
  },
} as const;

const COLLECTIONS = [
  "DataOverview",
  "DataWorkspace",
  "BusinessContextPanel",
  "Sources",
  "Imports",
  "ConnectorsCatalog",
] as const;

export const DATA_SANDBOX_SCREENS = [...COLLECTIONS, ...Object.keys(WORKBENCHES)];

function envelope(lens: string, items: readonly Record<string, unknown>[]) {
  return {
    schema_version: `data-${lens}.v1`,
    project_ref: { object_type: "project", id: PROJECT_ID },
    generated_at: "2026-07-29T10:00:00Z",
    evidence_as_of: "2026-07-29T09:45:00Z",
    items,
    unavailable_reasons: [],
    allowed_actions: [],
  };
}

const ITEMS: Record<string, readonly Record<string, unknown>[]> = {
  datastreams: [{
    object_ref: { object_type: "datastream", id: "ds_paid_media" },
    name: "Paid media daily",
    source_kind: "connector_pull",
    connector_ref: { object_type: "connector", id: "google-analytics" },
    states: { lifecycle: "active", validation: "executable", publication: "published", health: "ready", freshness: "observed" },
    evidence: { next_run_at: "2026-07-30T02:00:00Z" },
    evidence_as_of: "2026-07-29T09:45:00Z",
    links: {},
  }],
  sources: [{
    object_ref: { object_type: "source-account", id: OBJECT_ID },
    label: "North America analytics",
    connector_ref: { object_type: "connector", id: "google-analytics" },
    // Shaped like the projector really emits it: the ownership scope and the
    // ok|stale|revoked health, both of which the page now draws.
    authorization_ref: { object_type: "source-authorization", owner_scope: "delegated", kind: "oauth2" },
    states: { availability: "available", authorization: "stale", freshness: "observed", usage: "used" },
    evidence: { used_by_count: 2, last_seen_at: "2026-07-29T09:45:00Z" },
    evidence_as_of: "2026-07-29T09:45:00Z",
    links: {},
  }],
  imports: [{
    object_ref: { object_type: "import", id: OBJECT_ID },
    datastream_ref: { object_type: "datastream", id: "ds_managed_feed" },
    name: "Weekly spend import",
    source_kind: "managed_feed",
    states: { lifecycle: "failed", validation: "rejected_rows", candidate: "failed", publication: "not_current", receipt: "LANDED" },
    evidence: { feed_format: "csv", row_count: 1842, rejected_row_count: 7, receipt: { channel: "email", state: "LANDED" } },
    evidence_as_of: "2026-07-29T09:30:00Z",
    links: {},
  }],
  events: [{
    object_ref: { object_type: "event-configuration", id: OBJECT_ID },
    datastream_ref: { object_type: "datastream", id: "ds_paid_media" },
    name: "Campaign launches",
    active_version_ref: { object_type: "event-configuration-version", id: "ecv_7", version: 7 },
    states: { lifecycle: "active", review: "active", binding: "pinned", collection: "observed", usage: "linked" },
    evidence: { source_mapping: { event: "campaign_launch" }, collection_policy: { cadence: "daily" }, observation_count: 14 },
    evidence_as_of: "2026-07-29T09:40:00Z",
    links: {},
  }],
  connectors: [{
    object_ref: { object_type: "connector", id: OBJECT_ID },
    connector_id: "google-analytics",
    environment: "production",
    active_version_ref: { object_type: "connector-contract-version", id: "ccv_4", version: 4, fingerprint: "a1b2c3" },
    states: { installation: "ready", activation: "active", contract: "versioned", coverage: "used" },
    evidence: { contract: { auth_kind: "oauth2", reports: 12, grains: ["date", "campaign"] }, datastream_count: 2, last_verified_at: "2026-07-29T09:00:00Z" },
    evidence_as_of: "2026-07-29T09:00:00Z",
    links: {},
  }],
};

function fixtureFor(url: string): unknown {
  const lens = url.includes("/source-accounts")
    ? "sources"
    : url.includes("/event-configurations")
      ? "events"
      : url.includes("/connectors")
        ? "connectors"
        : url.includes("/imports")
          ? "imports"
          : url.includes("/datastreams")
            ? "datastreams"
            : "overview";
  if (lens === "overview") {
    const summaries = Object.entries(ITEMS).map(([key, items]) => ({
      object_ref: { object_type: "data-lens", id: key },
      lens: key,
      object_count: items.length,
      states: { evidence: "available" },
      evidence: {},
      evidence_as_of: "2026-07-29T09:45:00Z",
      links: {},
    }));
    return envelope("overview", summaries);
  }
  return envelope(lens, ITEMS[lens] ?? []);
}

function Collection({ name }: { name: string }): ReactNode {
  switch (name) {
    case "DataOverview": return <DataOverview projectId={PROJECT_ID} />;
    case "DataWorkspace": return <DataWorkspace projectId={PROJECT_ID} />;
    case "BusinessContextPanel": return <BusinessContextPanel projectId={PROJECT_ID} />;
    case "Sources": return <Sources projectId={PROJECT_ID} />;
    case "Imports": return <Imports projectId={PROJECT_ID} />;
    case "ConnectorsCatalog": return <ConnectorsCatalog projectId={PROJECT_ID} />;
    default: return null;
  }
}

export default function DataScreenSandbox({ name }: { name: string }) {
  const workbench = WORKBENCHES[name as keyof typeof WORKBENCHES];
  installSandboxApi((url) => ({ body: fixtureFor(url), status: 200 }));
  if (workbench && window.location.pathname === "/debug/screen") {
    window.history.replaceState(
      {},
      "",
      `/org/org_EXAMPLE/project/${PROJECT_ID}/data/${workbench.section}/object/${workbench.objectType}/${OBJECT_ID}/tab/overview`,
    );
  }
  return (
    <RouterProvider>
      {workbench ? (
        <DataObjectWorkbench
          projectId={PROJECT_ID}
          lens={workbench.lens}
          objectId={OBJECT_ID}
          tab="overview"
          tabs={workbench.tabs}
          onNavigateTab={() => undefined}
        />
      ) : <Collection name={name} />}
    </RouterProvider>
  );
}
