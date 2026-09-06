import { useEffect, useState } from "react";
import { Button, Metric, NavTabs, ObjectHeader, PageHeader, Panel, PanelHeader, Stack, Status, formatTimestamp,
  Retry,
  stateLabel,
  stateTone,
} from "../../ui";
import { datastreamTabs, type Tab } from "../../shell/pages/datastreamTabs";
import { registerCapabilityTabs } from "../../shell/capabilityTabs";
import WorkbenchCostPage from "./pages/WorkbenchCostPage";
import WorkbenchDataPage from "./pages/WorkbenchDataPage";
import WorkbenchMappingPage from "./pages/WorkbenchMappingPage";
import WorkbenchOutputsPage from "./pages/WorkbenchOutputsPage";
import WorkbenchOverviewPage from "./pages/WorkbenchOverviewPage";
import WorkbenchPlacementsPage from "./pages/WorkbenchPlacementsPage";
import WorkbenchProcessingPage from "./pages/WorkbenchProcessingPage";
import WorkbenchRunsPage from "./pages/WorkbenchRunsPage";
import DatastreamRunLive from "./DatastreamRunLive";
import DatastreamIssueBadge from "./DatastreamIssueBadge";
import DatastreamLifecycleMenu, { isArchived, type LifecycleOutcome } from "./lifecycleActions";
import { useDatastreamProgress } from "./datastreamProgress";
import { isDeniedError, loadWorkbench } from "./workbenchApi";
import { resolvableHrefs } from "../../shell/routeHref";
import type { WorkbenchHeader, WorkbenchLoadState } from "./workbenchTypes";
import type { OwnerReference } from "../../shell/pages/ProjectSettings";
import type { DatastreamMatch } from "../../analyze/explorer/explorerClient";

interface Props {
  projectId: string;
  datastreamId: string;
  tab: Tab;
  onNavigateTab: (tab: Tab) => void;
  /** Builds the canonical address of a tab (story 57.5). Only the shell knows
   *  the Organization segment `parsePath` requires, so the address is built
   *  there and handed down; without one the tabs stay visible and disabled
   *  rather than carrying a path the router refuses. */
  tabHref?: (tab: Tab) => string;
  /** Resolves a capability's semantic owner reference into a canonical route. */
  onOpenOwner?: (owner: OwnerReference) => void;
  onOpenAnalytics?: (match: DatastreamMatch) => void;
}

/*
 * THE ELEVEN-WORD PRIVATE MAP IS GONE (76-2). It drew `blocked` red -- the same
 * disagreement four other screens had -- and it quoted six union words beside
 * five the union had never been told about. All eleven are declared now,
 * `change pending` and `rollback available` included: `normalize()` folds a
 * space the way it folds an underscore, so the server's display spelling and
 * `change_pending` are one word. The axis VALUE is rendered through `stateLabel`
 * as well, because it reached the screen raw.
 */

/** The evidence behind one axis, in the axis's own terms.
 *
 *  The four axes rendered with `hint={`${axis} evidence`}` — the literal words
 *  "lifecycle evidence", "operations evidence". Four state words and four
 *  placeholder strings. Meanwhile the server sends, for every one of them, the
 *  thing that makes it actionable: `versions` (active AND proposed plan and
 *  mapping), `operations_evidence` (next run, missed runs, why it is late) and
 *  `publications` (the three pointers). None of it was drawn.
 *
 *  This matters more here than anywhere else on the surface: the header is the
 *  one place the target forbids a summary — the `Incomplete if` list of
 *  `datastream-workbench-and-wizard.md` names "lifecycle, configuration,
 *  operations, run and publication states are mixed into one status". Four
 *  separate words with no evidence under them are not four axes; they are one
 *  badge printed four times.
 *
 *  CITED BY NAME, NOT BY LINE. This read `…md:182` and story 58.2 inserted seven
 *  lines above it: `:182` now lands in a paragraph about parsing a CSV header,
 *  so the citation pointed a reader at the wrong clause while looking exact. A
 *  line number in a document that grows is a citation with an expiry date. */
function axisEvidence(axis: string, header: WorkbenchHeader): string {
  const versions = header.versions ?? {};
  const has = (id: unknown) => typeof id === "string" && Boolean(id);

  if (axis === "configuration") {
    // The target asks for the ACTIVE and PROPOSED version NUMBERS here (`:88`),
    // because `Change pending` without them says a change exists and refuses to
    // say which.
    //
    // The header payload does not carry those numbers — only the version ids.
    // This line used to print `id.slice(-6)`, which produced `plan …TW5FW`: the
    // tail of a ULID, the least distinguishing part of it, presented where a
    // person expects `v3`. It read as a version number and was not one, and two
    // different plans could render the same six characters.
    //
    // So this says what it can prove — WHICH artefacts are pinned, and whether a
    // change is waiting — and stops implying a number it was never given. The
    // exact ids are one panel below, in full, in mono, under "Exact
    // configuration references". Surfacing the real version numbers needs them
    // added to the header payload in `server/core/datastream_workbench.py`.
    const planLabel = (id: unknown, num: unknown) => has(id) ? (typeof num === "number" ? `plan v${num}` : "plan") : false;
    const mappingLabel = (id: unknown, num: unknown) => has(id) ? (typeof num === "number" ? `mapping v${num}` : "mapping") : false;

    const active = [
      planLabel(versions.active_plan, versions.active_plan_number),
      mappingLabel(versions.active_mapping, versions.active_mapping_number)
    ].filter(Boolean).join(" and ");

    const proposed = [
      planLabel(versions.proposed_plan, versions.proposed_plan_number),
      mappingLabel(versions.proposed_mapping, versions.proposed_mapping_number)
    ].filter(Boolean).join(" and ");

    if (proposed) return `Active ${active || "none"} · a proposed ${proposed} is waiting`;
    return active ? `Active ${active} pinned` : "No active plan or mapping version";
  }

  if (axis === "operations") {
    const ops = header.operations_evidence ?? {};
    if (ops.late_reasons?.length) {
      const missed = typeof ops.missed_run_count === "number" && ops.missed_run_count > 0
        ? ` (${ops.missed_run_count} missed)` : "";
      return `${ops.late_reasons.join(", ").replaceAll("_", " ")}${missed}`;
    }
    if (!ops.schedule_state_known) return "No schedule state — the clock was never armed";
    return ops.next_run_at ? `Next run ${formatTimestamp(ops.next_run_at)}` : "Scheduled";
  }

  if (axis === "publication") {
    const pubs = header.publications ?? {};
    // Named separately because they are three different promises, and the whole
    // safety model rests on their being distinguishable.
    const parts = [
      pubs.current ? "current served" : "nothing served",
      pubs.candidate ? "candidate waiting" : null,
      pubs.last_known_good ? "last-known-good retained" : null,
    ].filter(Boolean);
    return parts.join(" · ");
  }

  return header.identity?.owner ? `Owner ${header.identity.owner}` : "Lifecycle state of this Datastream";
}

/** One owner of this Datastream, named — and reached when it can be reached.
 *
 *  The three owner addresses arrive on the header payload, so this screen did
 *  not build them and cannot vouch for them. Rendered blind, an address the
 *  router refuses is a link that looks live and opens the unknown-route screen;
 *  the sandbox fixture shipped exactly that (`/p/{project}/data/sources`, three
 *  dead links on `/debug/screen`), and the tab band next door was fixed for the
 *  same reason in story 57.5.
 *
 *  Without a resolvable address the owner keeps its NAME. Hiding it would erase
 *  the fact that the Datastream has that owner, which is the evidence, and this
 *  screen's whole job is evidence. */
function OwnerLink({ href, label }: { href: string | null; label: string }) {
  if (!href) {
    return (
      <span
        className="text-text-secondary"
        aria-disabled="true"
        title="No address this console resolves was sent for this owner, so there is nothing to open."
      >
        {label}
      </span>
    );
  }
  return <a className="text-primary underline" href={href}>{label}</a>;
}

export default function DatastreamWorkbenchRoute({
  projectId,
  datastreamId,
  tab,
  onNavigateTab,
  onOpenOwner,
  onOpenAnalytics,
  tabHref,
}: Props) {
  const [reload, setReload] = useState(0);
  /**
   * The run a day of the `Data` grid was opened for — story 58.2, arbitrage 7.
   *
   * It is held HERE because the two tabs never exist at the same time: the grid
   * that knows the `execution_id` unmounts the moment the person lands on
   * `Runs`. Dropping the id and navigating to the tab was the first shape of
   * this gesture, and on a 60-day window it delivers an unfiltered list and the
   * work of finding the run again — the appearance of the feature.
   */
  const [openedRunId, setOpenedRunId] = useState<string | null>(null);
  const [mappingRawImportId, setMappingRawImportId] = useState<string | null>(null);
  /**
   * What the last lifecycle gesture did, in the words a person can check.
   *
   * Held HERE and not inside the menu because the sentence outlives the dialog:
   * a rename closes its dialog and the header re-reads, and without this the
   * only evidence the gesture landed would be a name that changed while nobody
   * was looking at it. `gone` is the one that has no header to return to — a
   * hard delete leaves no object, so the screen says so and stops re-reading
   * rather than showing the 404 of a Datastream it just removed.
   */
  const [lifecycleNotice, setLifecycleNotice] = useState<LifecycleOutcome | null>(null);
  const [gone, setGone] = useState<string | null>(null);
  const key = `${projectId}\u0000${datastreamId}\u0000${tab}\u0000${reload}`;
  const [state, setState] = useState<WorkbenchLoadState>({ status: "loading", key });
  const visibleState: WorkbenchLoadState = state.key === key ? state : { status: "loading", key };
  /**
   * The conditional tabs this Project has OPEN — story 58.6.
   *
   * Read from the header the route already loads, never derived here and never
   * defaulted to open: a band that draws `Cost` while the answer is unknown has
   * shown a tab that must not exist. The same answer is published to the router,
   * so a typed `…/tab/cost` on a Project whose `tax_fees` is off resolves to
   * nothing rather than to a panel explaining the extinction — which is a panel.
   */
  const openCapabilityTabs = (visibleState.status === "ok"
    ? visibleState.header.capability_tabs ?? []
    : []
  )
    // `entry.tab` is `null` on the four capabilities that open no tab (story
    // 58.9). An active capability with no tab is not a tab to draw, and letting
    // a `null` through would put an unnamed entry in the band's allow-list.
    .filter((entry) => entry.open && Boolean(entry.tab))
    .map((entry) => entry.tab as string);
  const tabs = datastreamTabs(tabHref, openCapabilityTabs);
  // Story 63.5. Called BEFORE the loading/denied/error returns, because a hook
  // that only runs on the happy path is a hook that unmounts and remounts on
  // every reload of the tab -- and the reading of this address is shared, so
  // that would restart it for the `Runs` tab too. The registry in
  // `lib/polledRead.ts` makes both mounts one poll, one backoff and one
  // `measuredAt`; before it, the header and the tab showed two numbers taken up
  // to fifteen seconds apart, each of them exact.
  const runPoll = useDatastreamProgress(projectId, datastreamId);

  useEffect(() => {
    const controller = new AbortController();
    let current = true;
    setState({ status: "loading", key });
    void loadWorkbench(projectId, datastreamId, tab, controller.signal)
      .then(({ header, payload }) => {
        if (!current) return;
        // PUBLISHED WHERE THE ANSWER LANDS, so the router learns which
        // conditional addresses this Project may open from the same request that
        // draws the band. The registry is module state read synchronously by
        // `parsePath`, which no hook can be read from — the shape `scopeAliases`
        // already uses for the same reason.
        registerCapabilityTabs("datastream", projectId, header.capability_tabs ?? []);
        setState({ status: "ok", key, header, payload });
      })
      .catch((error: unknown) => {
        if (!current || controller.signal.aborted) return;
        const message = error instanceof Error ? error.message : "Datastream Workbench could not be loaded.";
        setState({ status: isDeniedError(error) ? "denied" : "error", key, message });
      });
    return () => {
      current = false;
      controller.abort();
    };
  }, [datastreamId, key, projectId, tab]);

  // A HARD DELETE LEAVES NO OBJECT, and this screen is that object's screen.
  // Re-reading the header would answer 404 and land a person on "Workbench
  // evidence unavailable" — an error, for something that worked exactly as they
  // asked. It is checked before the loading branch because the header read that
  // follows the deletion is precisely the one that must not be believed.
  if (gone) {
    return (
      <Status as="block" tone="neutral" title="This Datastream no longer exists">
        {gone}
      </Status>
    );
  }
  if (visibleState.status === "loading") {
    return <Panel role="status"><PanelHeader title="Loading Datastream Workbench…" description="No state is inferred until both scoped contracts are available." /></Panel>;
  }
  if (visibleState.status === "denied") {
    return <Status as="block" tone="error" title="Workbench access unavailable"
          action={<Retry onClick={() => setReload((value) => value + 1)} />}
        >{visibleState.message}</Status>;
  }
  if (visibleState.status === "error") {
    return (
      <Status as="block" tone="error" title="Workbench evidence unavailable" action={<Button variant="secondary" size="sm" onClick={() => setReload((value) => value + 1)}>Retry</Button>}>
        {visibleState.message}
      </Status>
    );
  }

  const { header, payload } = visibleState;
  // Judged once, here, and handed down already judged: Processing renders the
  // same three addresses in its Owner column, and two screens asking the
  // question two ways is how one of them ends up not asking it.
  const links = resolvableHrefs(header.links);
  const page = {
    // Story 58.9: the Overview IS the flow, so it mounts the same components the
    // other tabs do — `CoverageStrip` for the collected days and `SchedulePanel`
    // for the `Collect` stage. Both need the scope this route already holds, and
    // `Runs` has been handed exactly these four props since 58.4 (the `runs:`
    // entry below). A second, read-only strip on the same endpoint would have
    // been the orphan-component defect the `CoverageStrip` mount in
    // `WorkbenchRunsPage` names (AI-144).
    //
    // CITED BY NAME, NOT BY LINE. Both pointers here were line numbers into
    // `WorkbenchRunsPage.tsx`, and story 58.10 rewrote that file: one landed on
    // an unrelated `className`, the other on a local variable. A citation that
    // rots when a neighbour is edited teaches a reader to distrust every
    // comment in the file.
    overview: (
      <WorkbenchOverviewPage
        header={header}
        payload={payload}
        projectId={projectId}
        datastreamId={datastreamId}
        connector={header.identity.connector ?? header.identity.module}
        sourceAccountRef={header.identity.source_account_ref}
        onOpenOwner={onOpenOwner}
        onNavigateTab={onNavigateTab}
        onRetryCapabilities={() => setReload((value) => value + 1)}
        onRepairMapping={(rawImportId) => {
          setMappingRawImportId(rawImportId);
          onNavigateTab("mapping");
        }}
      />
    ),
    data: (
      <WorkbenchDataPage
        payload={payload}
        projectId={projectId}
        datastreamId={datastreamId}
        // `mode` is `source_kind`, and it is the ONLY field that says
        // `managed_feed`: `connector` is composed `config.connector_name or
        // module_name`, both NULL on 4 of the 4 live managed feeds. The guard
        // that reads `connector` for a mode has never fired.
        mode={header.identity.mode}
        // Story 58.4: the account a re-collection would spend on. It is on the
        // header this route already read, and a tab payload carries only its own
        // evidence — so it travels rather than being read a second time.
        sourceAccountRef={header.identity.source_account_ref}
        onOpenRun={(executionId) => {
          setOpenedRunId(executionId);
          onNavigateTab("runs");
        }}
        onOpenAnalytics={onOpenAnalytics}
      />
    ),
    // The tab the `tax_fees` capability opens. It is in this table because the
    // table is keyed by the tab the ROUTER resolved, and the router refuses
    // `cost` when the capability is off — so this entry is unreachable exactly
    // when the amendment says the tab does not exist.
    cost: <WorkbenchCostPage payload={payload} onOpenOwner={onOpenOwner} />,
    // The tab the `placement_mapping` capability opens (story 61.1), on the same
    // terms as `cost` above: the router refuses `placements` when the capability
    // is off, so this entry is unreachable exactly when the amendment says the
    // tab does not exist.
    //
    // `connector` HERE IS A LABEL AND NOTHING ELSE — the connector's display name
    // when it has one. The identity that scopes the reading and both writes is
    // `module_name`, and the SERVER reads it from `app.datastreams` on every one
    // of the three calls; a name travelling from this screen could be made to
    // point at another connector's slice.
    placements: (
      <WorkbenchPlacementsPage
        payload={payload}
        projectId={projectId}
        datastreamId={datastreamId}
        connector={header.identity.connector ?? header.identity.module}
        onOpenOwner={onOpenOwner}
      />
    ),
    // `onOpenOwner` — amendment 10 of the 2026-08-11 review. A bound field names
    // the concept that governs it and has to LEAD there; only the shell's
    // resolver may build that address, so it travels rather than being composed.
    mapping: <WorkbenchMappingPage header={header} payload={payload} projectId={projectId} datastreamId={datastreamId} rawImportId={mappingRawImportId} onRetry={() => setReload((value) => value + 1)} onConfirmed={() => { setMappingRawImportId(null); setReload((value) => value + 1); }} onOpenOwner={onOpenOwner} />,
    processing: <WorkbenchProcessingPage payload={payload} projectId={projectId} datastreamId={datastreamId} mode={header.identity.mode} deliveryChannels={header.identity.delivery_channels} onConfirmed={() => setReload((value) => value + 1)} onOpenOwner={onOpenOwner} links={links} onNavigateTab={onNavigateTab} />,
    runs: <WorkbenchRunsPage payload={payload} projectId={projectId} datastreamId={datastreamId} connector={header.identity.connector ?? header.identity.module} sourceAccountRef={header.identity.source_account_ref} onConfirmed={() => setReload((value) => value + 1)} onOpenOwner={onOpenOwner} onNavigateTab={onNavigateTab} selectedRunId={openedRunId} />,
    outputs: <WorkbenchOutputsPage header={header} payload={payload} projectId={projectId} datastreamId={datastreamId} onConfirmed={() => setReload((value) => value + 1)} onOpenOwner={onOpenOwner} onOpenRun={(executionId) => { setOpenedRunId(executionId); onNavigateTab("runs"); }} />,
  }[tab];

  return (
    <Stack>
      <div className="flex flex-wrap items-center gap-4">
        <ObjectHeader
          name={header.identity.name?.trim() || datastreamId}
          source={header.identity.module || header.identity.mode}
          provider={header.identity.module || header.identity.mode}
        />
        {/* BESIDE THE HEADER, NOT INSIDE IT — story 59.2, arbitrage 3.
            `ObjectHeader` takes `name`/`source`/`provider` and is mounted by
            twelve files; a prop added there would expose seven other screens to
            a layout defect for a need that concerns one. The precedent is
            fourteen lines below: `DatastreamRunLive` (story 63.5) is a band of
            its own for the same reason, and neither is a fifth axis tile —
            "lifecycle, configuration, operations, run and publication states
            mixed into one status" is an `Incomplete if` of this surface.
            It sits above `NavTabs`, so it is read from all six tabs and is
            loaded once with the header rather than per tab. */}
        <DatastreamIssueBadge
          summary={header.open_issues}
          onOpen={(executionId) => {
            if (executionId) setOpenedRunId(executionId);
            onNavigateTab("runs");
          }}
        />
        {/* WHAT CAN BE DONE TO THIS DATASTREAM AS AN OBJECT — 2026-08-18.
            Beside `ObjectHeader` for the same reason `DatastreamIssueBadge`
            is: that component is mounted by twelve files and takes three
            props, so a menu added inside it would expose eleven other screens
            to a layout change for a need that concerns one.

            It is on the HEADER and not on a tab because rename, archive and
            restore act on the Datastream itself rather than on any one of its
            eight readings — and because the state the archive produces is
            visible from all eight. `ml-auto` puts it on the trailing edge, the
            same edge the acts of `BindingsTable` sit on. */}
        <div className="ml-auto">
          <DatastreamLifecycleMenu
            header={header}
            projectId={projectId}
            datastreamId={datastreamId}
            onDone={(outcome) => {
              setLifecycleNotice(outcome);
              // The header is re-read rather than patched here: the axes, the
              // schedule evidence and the primary action are all composed by the
              // server from the row that just changed, and recombining them in
              // the console would be a second authority on the same state.
              setReload((value) => value + 1);
            }}
            onGone={(message) => setGone(message)}
          />
        </div>
      </div>
      {/* AN ARCHIVED DATASTREAM SAYS SO ONCE, AND NAMES THE WAY BACK.
          The `Lifecycle` axis already carries the word, but a state word in a
          tile of four is not the same as knowing what to do — and until this
          commit there was nothing to do, which is why the axis was the whole of
          it. The banner sits above the tabs so the same sentence is read from
          all eight readings, and it names the gesture that is now two clicks
          away instead of describing a state a person cannot leave. */}
      {isArchived(header) && !lifecycleNotice && (
        <Status as="block" tone="warning" title="This Datastream is archived">
          It collects nothing, it holds no name, and everything it produced stays readable.
          Restore it from the menu beside its name to make it a Datastream of this project
          again — restoring does not start it collecting.
        </Status>
      )}
      {lifecycleNotice && (
        <Status
          as="block"
          tone={lifecycleNotice.tone}
          title={
            lifecycleNotice.verb === "rename"
              ? "Renamed"
              : lifecycleNotice.verb === "restore"
                ? "Restored"
                : "Archived"
          }
          action={
            <Button variant="secondary" size="sm" onClick={() => setLifecycleNotice(null)}>
              Dismiss
            </Button>
          }
        >
          {lifecycleNotice.message}
        </Status>
      )}
      {/* A BAND OF ITS OWN, BEFORE THE TABS — story 63.5.
          Not a fifth tile in the axes panel below: "lifecycle, configuration,
          operations, run and publication states mixed into one status" is an
          `Incomplete if` of this surface, and where a collection has got to is
          not an axis of the Datastream. Here it is visible from every tab,
          which is the point — a person watching a run must not have to pick the
          right tab to see it move. */}
      <DatastreamRunLive poll={runPoll} projectId={projectId} datastreamId={datastreamId} />
      <NavTabs label="Datastream" current={tab} tabs={tabs} onNavigate={(next) => onNavigateTab(next as Tab)} />
      <PageHeader
        eyebrow={`Datastream · ${tab[0].toUpperCase()}${tab.slice(1)}`}
        title={header.identity.name?.trim() || datastreamId}
        /* THE ROLE STOPS BEING DEAD TEXT — amendment 9 of the 2026-08-11 review.
           It read « Performance data · owner » and nothing on this screen touched
           it, while the role decides whether the fee ladder reads this Datastream
           at all. The gesture that changes it now exists (the object menu above),
           so the line names it: a description of a decision nobody can revisit is
           the shape this review calls out, and « Unclassified » was the worst of
           them — a state with no way out of it. */
        description={
          `${header.identity.data_role || "No data role — nothing has classified this Datastream"}`
          + ` · ${header.identity.owner || "Owner unavailable"}`
          + (header.data_role_change ? " · change it from the object menu above" : "")
        }
        actions={<Button title={header.primary_action.reason} onClick={() => onNavigateTab(header.primary_action.tab)}>{header.primary_action.label}</Button>}
      />
      {/* A named region: the four axes are one object — the Datastream's state —
          and a screen reader needs to be able to reach it as such. It was an
          unlabelled grid, so the only way in was to walk the page. */}
      <Panel
        flush
        role="group"
        aria-label="Datastream state axes"
        className="grid grid-cols-4 divide-x divide-divider-base max-lg:grid-cols-2"
      >
        {Object.entries(header.axes).map(([axis, value]) => (
          <Metric
            key={axis}
            label={axis[0].toUpperCase() + axis.slice(1)}
            value={<Status tone={stateTone(value)}>{stateLabel(value)}</Status>}
            hint={axisEvidence(axis, header)}
          />
        ))}
      </Panel>
      <nav aria-label="Datastream owners" className="flex flex-wrap gap-4 text-ui">
        <OwnerLink href={links.source} label="Source owner" />
        <OwnerLink href={links.governance} label="Governance" />
        <OwnerLink href={links.project_settings} label="Project settings" />
      </nav>
      {page}
    </Stack>
  );
}
