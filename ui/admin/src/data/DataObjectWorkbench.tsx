import { type ReactNode, useMemo } from "react";
import type { DataLens, DataSurfaceItem } from "./dataSurface";
import { useDataSurface } from "./dataSurface";
import {
  Button,
  EmptyState,
  EvidenceRows,
  NavTabs,
  ObjectHeader,
  Panel,
  PanelHeader,
  Stack,
  Status,
  evidenceRows,
  label as fieldLabel,
  type NavTab,
} from "../ui";
import { buildPath, type Route, useRoute } from "../shell/router";
import { resolvableHref } from "../shell/routeHref";
import { StateValue } from "./DataCollectionLayout";

/**
 * The NAME OF A STATE AXIS, spelled out — `installation` heads its panel as
 * "Installation".
 *
 * Not `stateLabel`: that answers what a state VALUE says, and since 76-2 a word
 * it does not declare answers `Unknown`, which is correct for a value the server
 * invented and nonsense for a column heading. The right home for these words is
 * the glossary 76-3 builds; this is the honest placeholder until it exists, and
 * it is three lines here rather than a borrowed map that hid the question.
 */
function axisLabel(axis: string): string {
  const spaced = axis.replaceAll(/[_-]+/g, " ").trim();
  return spaced ? spaced.charAt(0).toUpperCase() + spaced.slice(1) : axis;
}
import SourceBackfillPanel from "./SourceBackfillPanel";

const LABELS: Record<DataLens, string> = {
  overview: "Data",
  datastreams: "Datastream",
  sources: "Source Account",
  imports: "Import",
  events: "Event Configuration",
  connectors: "Connector",
};

/** The Level 2 screen each object came from, in the words of its own tab. */
const COLLECTIONS: Record<DataLens, string> = {
  overview: "Data Overview",
  datastreams: "Datastreams",
  sources: "Sources",
  imports: "Imports",
  events: "Events",
  connectors: "Connectors",
};

/** The route section each lens lives under. The same pairs `navigation/data.ts`
 *  declares — read from there through `buildPath`, never spelled as an address. */
const SECTIONS: Record<DataLens, string> = {
  overview: "data-overview",
  datastreams: "datastreams",
  sources: "sources",
  imports: "imports",
  events: "events",
  connectors: "connectors",
};

/**
 * One console address, built by the ONE builder, or nothing at all.
 *
 * `buildPath` validates against the navigation registry and THROWS on an address
 * the router would refuse to parse — an object type a section does not declare,
 * a tab a contract does not carry, query state a section does not own. Catching
 * that turns a refused address into an ABSENCE rather than an exception thrown
 * inside a render, which is the rule `routeHref.ts` already states for the
 * addresses the server sends: nothing on this screen is composed as a string.
 */
function consoleHref(
  route: Route,
  to: {
    section: string;
    objectType?: string | null;
    objectId?: string | null;
    tab?: string | null;
    action?: string | null;
  },
): string | null {
  if (route.scope !== "project" || !route.organizationId || !route.projectId) return null;
  try {
    return buildPath({
      scope: "project",
      organizationId: route.organizationId,
      projectId: route.projectId,
      workspace: "data",
      section: to.section,
      lens: null,
      objectType: to.objectType ?? null,
      objectId: to.objectId ?? null,
      tab: to.tab ?? null,
      versionId: null,
      evidenceId: null,
      action: to.action ?? null,
      query: {},
      globalSurface: null,
      globalSection: null,
    });
  } catch {
    return null;
  }
}

/**
 * Where an `allowed_actions` value is really performed — and nothing when
 * nowhere.
 *
 * The five values `data_surface.py` can send (`_ACTIONS`) are COLLECTION
 * gestures, and an object read carries the one belonging to its own lens. They
 * were declared, validated by `parseEnvelope` and read by no Data screen. Four
 * of the five have a surface that performs them; one does not, and the
 * difference is the whole point of this table:
 *
 *   `datastream.create`          the Add Datastream wizard, which the
 *                                `datastreams` section declares as its `create`
 *                                action (`navigation/data.ts`);
 *   `source-account.connect`     the two consent gestures at the top of Sources
 *                                (`ConnectGoogleButton`, `ConnectButton`);
 *   `connector.inspect`          the Connector catalog;
 *   `event-configuration.create` arming an event stream, which lives on the
 *                                OWNING Datastream's `Outputs` tab
 *                                (`EventStreamPanel`) — so it is offered only
 *                                when this object names its Datastream, and is
 *                                absent rather than dead when it does not;
 *   `import.create`              NOTHING. An Import is a file arriving through
 *                                an inbound channel; measured 2026-08-17, the
 *                                only console file that posts anywhere near it
 *                                posts to `/imports/preview`, which by
 *                                construction does not import. A button here
 *                                would be a control that exists to do nothing,
 *                                so this returns null and none is drawn.
 */
function actionDestination(
  action: string,
  route: Route,
  item: DataSurfaceItem,
): { key: string; text: string; href: string } | null {
  const at = (text: string, href: string | null) => (href ? { key: action, text, href } : null);
  if (action === "datastream.create") {
    return at("Add a Datastream", consoleHref(route, { section: "datastreams", action: "create" }));
  }
  if (action === "source-account.connect") {
    return at("Connect a source account", consoleHref(route, { section: "sources" }));
  }
  if (action === "connector.inspect") {
    return at("Open the Connector catalog", consoleHref(route, { section: "connectors" }));
  }
  if (action === "event-configuration.create") {
    const datastreamId = item.datastream_ref?.id;
    if (!datastreamId) return null;
    return at(
      "Arm an event stream",
      consoleHref(route, {
        section: "datastreams",
        objectType: "datastream",
        objectId: datastreamId,
        tab: "outputs",
      }),
    );
  }
  return null;
}

/**
 * Everything this object names that can be opened from here.
 *
 * Three sources, one rule — an address is offered only when this console's own
 * router resolves it:
 *
 *   * the object's OWN references. `datastream_ref` and `connector_ref` reached
 *     the screen as opaque identifiers in an evidence row and led nowhere,
 *     which is the dead end `data.md` forbids of the fleet and which this
 *     workbench had on every lens;
 *   * the `links` record the server composes. Every key it sends today is one of
 *     THIS object's own tabs (`_console_link`, one per tab of the contract), and
 *     those are dropped: the tab band above already opens them, and a second
 *     copy of the same four links is navigation dressed as evidence. A key that
 *     is not a tab of this object — a Governance or Analyze owner, the day the
 *     server sends one — appears here without this file changing;
 *   * the `allowed_actions` the envelope declares, through the table above.
 */
function destinations(
  item: DataSurfaceItem,
  lens: DataLens,
  route: Route,
  allowedActions: readonly string[],
  tabKeys: ReadonlySet<string>,
): Array<{ key: string; text: string; href: string }> {
  const found: Array<{ key: string; text: string; href: string }> = [];
  const push = (key: string, text: string, href: string | null) => {
    if (href) found.push({ key, text, href });
  };

  const datastreamId = item.datastream_ref?.id;
  if (datastreamId) {
    push(
      "datastream",
      "Open the Datastream",
      consoleHref(route, {
        section: "datastreams",
        objectType: "datastream",
        objectId: datastreamId,
        tab: "overview",
      }),
    );
  }
  // On the `connectors` lens the Connector IS the object being read, so a link
  // to itself would be a tab band with one tab.
  const connectorId = lens === "connectors" ? undefined : item.connector_ref?.id;
  if (connectorId) {
    push(
      "connector",
      "Open the Connector",
      consoleHref(route, {
        section: "connectors",
        objectType: "connector",
        objectId: connectorId,
        tab: "overview",
      }),
    );
  }

  for (const [key, value] of Object.entries(item.links ?? {})) {
    // `raw_evidence` the key, `raw-evidence` the tab: the server writes the
    // record in Python's spelling and the contract in the route's.
    if (tabKeys.has(key.replaceAll("_", "-"))) continue;
    push(`link:${key}`, fieldLabel(key), resolvableHref(value));
  }

  for (const action of allowedActions) {
    const destination = actionDestination(action, route, item);
    if (destination) found.push(destination);
  }
  return found;
}

function record(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};
}

function tabEvidence(item: DataSurfaceItem, lens: DataLens, tab: string): Record<string, unknown> {
  const evidence = record(item.evidence);
  if (tab === "overview") {
    return {
      identity: item.object_ref.id,
      owner_datastream: item.datastream_ref?.id,
      connector: item.connector_ref?.id ?? item.connector_id,
      active_version: item.active_version_ref?.id ?? item.active_version_refs,
      evidence_as_of: item.evidence_as_of,
    };
  }
  if (lens === "sources") {
    if (tab === "accounts") {
      // THIS ACCOUNT AND THE CONSENT BEHIND IT. The tab used to return the
      // object id and the Connector — the two fields `overview` already
      // returns — so two tabs of four showed one reading. What the contract
      // calls `Accounts` is the set of accounts ONE authorization exposes
      // (`data.md`, *One authorization, many accounts, many Connectors*), and
      // that set is not in this envelope: an object read carries one account.
      // So the tab shows what it does hold — this account's identity and the
      // authorization it was discovered through — and the note above it names
      // the surface that lists the others.
      return {
        account_label: item.label,
        account_identifier: item.object_ref.id,
        authorization: item.connection_ref?.id,
        authorization_owner: item.authorization_ref?.owner_scope,
        authorization_kind: item.authorization_ref?.kind,
        provided_by: item.authorization_ref?.owner_org_name,
        discovered_for_connector: item.discovered_for_connector,
        discovered_at: evidence.discovered_at,
      };
    }
    if (tab === "health") return { availability: item.states.availability, freshness: item.states.freshness, last_seen_at: evidence.last_seen_at };
    if (tab === "used-by") return { usage: item.states.usage, datastream_count: evidence.used_by_count };
  }
  if (lens === "imports") {
    if (tab === "raw-evidence") return { source_metadata: evidence.source_metadata, landing_relation: evidence.landing_relation, content_hash: evidence.content_hash, row_count: evidence.row_count };
    if (tab === "validation") return { validation: item.states.validation, rejected_row_count: evidence.rejected_row_count };
    if (tab === "publication") return { publication: item.states.publication, versions: item.active_version_refs, datastream: item.datastream_ref?.id };
  }
  if (lens === "events") {
    if (tab === "source-mapping") return record(evidence.source_mapping);
    if (tab === "collection") return record(evidence.collection_policy);
    if (tab === "usage") return { datastream: item.datastream_ref?.id, lifecycle: item.states.lifecycle, binding: item.states.binding };
  }
  if (lens === "connectors") {
    if (tab === "capabilities") return record(evidence.contract);
    if (tab === "coverage") return { coverage: item.states.coverage, datastream_count: evidence.datastream_count, activation: item.states.activation };
    if (tab === "versions") return { active_version: item.active_version_ref, validation: evidence.validation, verified_at: evidence.last_verified_at };
  }
  return evidence;
}

/**
 * What an empty tab says, in the words of THAT tab.
 *
 * Sixteen tabs shared one sentence — « This tab has no owned evidence in the
 * current version » — which names no subject, no absence and no gesture, and
 * reads identically whether a file was never parsed or a Connector was never
 * verified. Each entry below says what THIS tab would show and what would put it
 * there. The keys are `${lens}:${tab}`, one per tab of the contracts declared in
 * `shell/objectSurfaces.tsx`, so a tab added there without a sentence here is
 * visible as the fallback rather than silently generic.
 */
const EMPTY_TAB: Record<string, { title: string; description: string }> = {
  "sources:overview": {
    title: "This Source Account has reported nothing yet",
    description:
      "Its label, the authorization behind it and the Connector it was discovered for arrive the first time the provider answers. Reconnect it from Sources and this reading fills.",
  },
  "sources:accounts": {
    title: "This account carries no identity yet",
    description:
      "The label, the owner of the consent and the date the account was discovered all come from the authorization that exposed it. Sources is where that consent is given.",
  },
  "sources:health": {
    title: "No health check has answered for this authorization",
    description:
      "Availability, freshness and the last time the provider answered are written by a health check. Reconnect the authorization from Sources, or ask for earlier history below.",
  },
  "sources:used-by": {
    title: "Nothing reads this Source Account yet",
    description:
      "A Datastream names exactly one Source Account. Add a Datastream and choose this one, and it is counted here.",
  },
  "imports:overview": {
    title: "This Import carries no identity yet",
    description:
      "A receipt is written when a file lands through an inbound channel or is uploaded from the owning Datastream. Nothing has landed for this one.",
  },
  "imports:raw-evidence": {
    title: "No file was kept for this Import",
    description:
      "The landing relation, the content hash and the row count are written while the file is read. This Import was not read.",
  },
  "imports:validation": {
    title: "This Import was not validated",
    description:
      "Accepted and rejected rows are counted while the file is parsed, and parsing has not run for this one.",
  },
  "imports:publication": {
    title: "This Import published nothing",
    description:
      "A publication makes one validated candidate current, and it is done from the owning Datastream's Outputs tab.",
  },
  "events:overview": {
    title: "This Event Configuration carries no identity yet",
    description:
      "An event stream is armed from the owning Datastream's Outputs tab, and arming it writes the identity, the version and the confirmation this tab reads.",
  },
  "events:source-mapping": {
    title: "No source mapping is pinned here",
    description:
      "The mapping names the Connector report and the field the events are read from. Arming the event stream on the owning Datastream's Outputs tab is what writes it.",
  },
  "events:collection": {
    title: "No collection policy is pinned here",
    description:
      "The cadence and the timezone are copied from the owning Datastream's schedule when the Event Configuration is armed. Nothing has been armed here.",
  },
  "events:usage": {
    title: "Nothing has used this Event Configuration yet",
    description:
      "Observations are counted as the Datastream collects, and linked from Context Hub once they are published. Neither has happened.",
  },
  "connectors:overview": {
    title: "This Connector reports no installation",
    description:
      "The installation, the activation and the contract version are written when the Connector is installed into this deployment. None of the three has been.",
  },
  "connectors:capabilities": {
    title: "This Connector published no contract",
    description:
      "The authorization kind, the reports, the fields and the grains all come from the installed Connector itself. Nothing has been verified for this one.",
  },
  "connectors:coverage": {
    title: "No Datastream is built on this Connector",
    description:
      "Coverage counts the Datastreams that read through it. Add a Datastream and choose this Connector, and it is counted here.",
  },
  "connectors:versions": {
    title: "No contract version is recorded for this Connector",
    description:
      "A version is written each time the Connector's contract is verified against the provider. It has not been verified.",
  },
};

function emptyTab(lens: DataLens, tab: string): { title: string; description: string } {
  return EMPTY_TAB[`${lens}:${tab}`] ?? {
    title: "This tab has nothing to show yet",
    description:
      "No reading of this object answers this question. The tab is part of its contract and stays here, so what is missing can be seen.",
  };
}

/**
 * What a tab has to say beyond its own rows.
 *
 * Two of the sixteen need it, and both for the same reason: the Source Account
 * contract asks a question one account's envelope cannot answer alone. Both
 * notes name the surface that owns the answer and hand over its address — never
 * a number standing on its own.
 */
function tabNote(
  item: DataSurfaceItem,
  lens: DataLens,
  tab: string,
  route: Route,
): ReactNode | null {
  if (lens !== "sources") return null;

  if (tab === "accounts") {
    const href = consoleHref(route, { section: "sources" });
    return (
      <Status
        as="block"
        tone="info"
        title="One account, and its siblings are on Sources"
        data-testid="data-tab-note"
        action={href ? <Button asChild variant="secondary" size="sm"><a href={href}>Open Sources</a></Button> : undefined}
      >
        What is read here is the single account this address opens. One consent can expose as
        many accounts as it reaches, and Sources lists every one this Project can use — one row
        each, with the consent that exposed it.
      </Status>
    );
  }

  if (tab === "used-by") {
    const count = record(item.evidence).used_by_count;
    if (typeof count !== "number") {
      return (
        <Status as="block" tone="warning" title="How many Datastreams read this account was not measured" data-testid="data-tab-note">
          The count is absent from this reading, which is not the same statement as none. Datastreams
          names the Source Account each Datastream pulls from, and is where the answer can be counted by hand.
        </Status>
      );
    }
    const fleetHref = consoleHref(route, { section: "datastreams" });
    const addHref = consoleHref(route, { section: "datastreams", action: "create" });
    if (count === 0) {
      return (
        <Status
          as="block"
          tone="neutral"
          title="No Datastream reads this Source Account"
          data-testid="data-tab-note"
          action={addHref ? <Button asChild variant="secondary" size="sm"><a href={addHref}>Add a Datastream</a></Button> : undefined}
        >
          A Datastream names exactly one Source Account, and none has named this one. Adding a
          Datastream and choosing this account is what puts it to use.
        </Status>
      );
    }
    return (
      <Status
        as="block"
        tone="success"
        title={`${count} Datastream${count === 1 ? "" : "s"} read this Source Account`}
        data-testid="data-tab-note"
        action={fleetHref ? <Button asChild variant="secondary" size="sm"><a href={fleetHref}>Open Datastreams</a></Button> : undefined}
      >
        Which ones is answered on Datastreams: every row there names the Source Account it pulls
        from, so the list is read by looking for this account's name in that column.
      </Status>
    );
  }
  return null;
}

/* `displayValue`, `summarizeObject`, `label` and `flatten` used to live here.
 * They are `ui/Evidence.tsx` now, shared with the Datastream Workbench
 * confirmation reviews, which had the same `JSON.stringify` defect. */

export default function DataObjectWorkbench({
  projectId,
  lens,
  objectId,
  tab,
  tabs,
  onNavigateTab,
}: {
  projectId: string;
  lens: Exclude<DataLens, "overview" | "datastreams">;
  objectId: string;
  tab: string;
  tabs: readonly { key: string; label: string }[];
  onNavigateTab: (tab: string) => void;
}) {
  const { route } = useRoute();
  const { state, reload } = useDataSurface(projectId, lens, objectId);
  const navTabs = useMemo<NavTab[]>(() => tabs.map((candidate) => ({
    ...candidate,
    href: buildPath({ ...route, tab: candidate.key, action: null, versionId: null }),
  })), [route, tabs]);
  const item = state.status === "ready" ? state.envelope.items[0] : undefined;
  const rows = item ? evidenceRows(tabEvidence(item, lens, tab)) : [];
  const name = item?.name ?? item?.label ?? item?.connector_id ?? objectId;
  const provider = item?.connector_ref?.id ?? item?.connector_id;
  const tabKeys = useMemo(() => new Set(tabs.map((candidate) => candidate.key)), [tabs]);
  const collectionHref = consoleHref(route, { section: SECTIONS[lens] });
  const opens = item
    ? destinations(item, lens, route, state.status === "ready" ? state.envelope.allowed_actions : [], tabKeys)
    : [];
  const absence = emptyTab(lens, tab);
  const note = item ? tabNote(item, lens, tab, route) : null;

  return (
    <Stack data-owner={`data/${lens}/${objectId}`}>
      {/* `DESIGN.md:137` contracts the ObjectHeader as "provider logo, name and
          concise source/data-role metadata". An opaque identifier is neither a
          source nor a data role, and pasting one here put `sacc_01KYJ0NP…` in
          the subtitle of every Data workbench — the Datastream workbench, which
          follows the same contract, shows its module instead. The identifier is
          not lost: it is in the address, and in the object's own evidence. */}
      <ObjectHeader name={name} source={LABELS[lens]} provider={provider} />
      <NavTabs label={LABELS[lens]} tabs={navTabs} current={tab} onNavigate={onNavigateTab} />
      {state.status === "loading" && <p role="status" className="text-body text-text-secondary">Loading {LABELS[lens]}…</p>}
      {state.status === "error" && (
        <Status
          as="block"
          tone="error"
          title={`${LABELS[lens]} unavailable`}
          action={
            // RETRY WAS THE ONLY WAY OUT, and it is the one that does not work
            // when the object itself is the problem — a deleted identity, an
            // address typed by hand, a Project the reader has left. The screen
            // the object came from is always readable, so it is offered beside
            // the retry rather than left to the browser's back button.
            <div className="flex flex-wrap gap-2">
              <Button variant="secondary" onClick={reload}>Retry</Button>
              {collectionHref && (
                <Button asChild variant="secondary"><a href={collectionHref}>Back to {COLLECTIONS[lens]}</a></Button>
              )}
            </div>
          }
        >
          {state.message}. No object from another Project has been substituted.
        </Status>
      )}
      {item && (
        <>
          <Panel className="grid gap-4 md:grid-cols-3" data-testid="data-object-states">
            {Object.entries(item.states).map(([axis, value]) => (
              <div key={axis}>
                {/* THE AXIS IS READ, NOT SPELLED — and it is read by `axisLabel`,
                    not by `labelForState`. The two were the same call until 76-2:
                    the state vocabulary's fallback capitalized any token it was
                    handed, so `installation` came back "Installation" and the
                    borrowing was invisible. Now an undeclared word answers
                    `Unknown` (README invariant 8), which is the right answer for a
                    STATE and a wrong one for a COLUMN NAME — the axis is not a
                    state and never was. A glossary of axis names belongs to 76-3;
                    until then this spells the token and says so. */}
                <p className="mb-2 text-caption font-semibold tracking-wide text-text-secondary">{axisLabel(axis)}</p>
                <StateValue value={value} />
              </div>
            ))}
          </Panel>
          {opens.length > 0 && (
            <Panel flush>
              <PanelHeader
                title="Opens from here"
                description={`What this ${LABELS[lens]} names, and the surfaces that act on it. An address this console's router refuses is not offered.`}
              />
              <div className="flex flex-wrap gap-2 p-5" data-testid="data-object-destinations">
                {opens.map((destination) => (
                  <Button key={destination.key} asChild variant="secondary" size="sm">
                    <a href={destination.href}>{destination.text}</a>
                  </Button>
                ))}
              </div>
            </Panel>
          )}
          <Panel flush>
            <PanelHeader title={tabs.find((candidate) => candidate.key === tab)?.label ?? "Overview"} description={`Pinned evidence for ${LABELS[lens]}.`} />
            {note && <div className="border-b border-divider-base p-4">{note}</div>}
            {rows.length === 0 ? (
              <EmptyState title={absence.title} description={absence.description} />
            ) : (
              <EvidenceRows source={tabEvidence(item, lens, tab)} label={`${LABELS[lens]} ${tab} evidence`} />
            )}
          </Panel>
          {/* The one action this surface owns and never offered.
              `execution-substrate.md:88-97` describes a person waiting in the
              console for exactly this, and `/api/connections/{}/backfill` has
              answered it since Story 3.2 with no caller anywhere.
              It sits on `health` because that is the tab whose subject is the
              authorization itself rather than the accounts behind it or the
              Datastreams in front of it. Rendered only when the connection id
              is really there: a panel that posts to `/api/connections//backfill`
              is worse than no panel. */}
          {lens === "sources" && tab === "health" && item.connection_ref?.id && (
            <SourceBackfillPanel
              connectionId={item.connection_ref.id}
              label={name}
              usedByHref={consoleHref(route, {
                section: "sources",
                objectType: "source-account",
                objectId,
                tab: "used-by",
              })}
            />
          )}
        </>
      )}
    </Stack>
  );
}
