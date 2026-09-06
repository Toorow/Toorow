/**
 * Data > Datastreams — the fleet screen.
 *
 * REBUILT 2026-08-02 on Jean's instruction ("la page actuelle sert concrètement
 * à rien"), and REBUILT AGAIN by story 58.8, which is the one that made the
 * fleet say what each stream COLLECTS rather than only that it exists.
 *
 * THE TARGET, and it is not this file's opinion:
 *   `data.md:50` — "Datastream tree/list, mode, source, grain, cadence, health,
 *   current Publication, next run and exceptions".
 *   `epic-58-datastream-read-by-day.md:266-272` — per row: the connector's mark
 *   AND its name, the source account, what the stream collects with its grain,
 *   the cadence, the issues to review, the last run (mark AND instant on one
 *   baseline), the next run. And no `Destination` column: BigQuery is the one
 *   warehouse, so a column that never varies carries nothing.
 *
 * WHAT 58.8 OPENED IN THE READ MODEL, because no front end could have drawn it:
 * `data_role`, `source_account_id` + the account's label, the plan's own
 * `grain` / metric count / dimension count, and `latest_run_at`. Four facts that
 * were in the database and stopped at the SQL.
 *
 * THIRTEEN COLUMNS, and three of them are merges rather than additions:
 *   - `Freshness` is gone as a column and lives INSIDE `Published`: both derived
 *     from the same `published_at`, so the age belongs beside the date rather
 *     than two columns apart;
 *   - the connector's NAME lives inside the `Datastream` cell, under the stream
 *     name, beside the mark that was already there;
 *   - the cadence lives inside `Next run` as a STATEMENT. It used to be a
 *     fallback shown only when no next run existed, which is how a nightly
 *     stream with a scheduled run said nothing about being nightly.
 *
 * WHAT THE `Issues` COLUMN CARRIES SINCE 59.2, and why it is four states rather
 * than a sentence: `evidence.open_issues` — the open, not-yet-reviewed issues of
 * this Datastream, their worst severity, and whether any published monitor
 * watches it at all. It is drawn by `DatastreamIssueBadge`, the same component
 * the Workbench header mounts, so the list and the object cannot report
 * different numbers for one flux. The count is INDEPENDENT of `execution_id`:
 * most issues name no run, and a per-run reading would report zero for them.
 * An absent key still means "the count could not be read" and is never a `0`.
 *
 * WHAT IS ABSENT AND IS SAID RATHER THAN FAKED:
 *   - **Governance and MDM links** — `links` carries overview/runs/outputs/
 *     mapping and nothing else, so the fleet reports how far a stream is from
 *     the semantic layer and links to the tab where that is repaired.
 *
 * FILTERING IS CONSOLE-SIDE, on the envelope already loaded. The fleet route is
 * shared by six lenses and carries no query parameters; adding one for this
 * lens alone would be a change to all six. Only the SOURCE filter ships here;
 * an issues filter is named by no line of story 59.2 and is not smuggled in
 * with its count.
 */
import { type ReactNode, useEffect, useMemo, useState } from "react";
import { Button, ConfirmDialog, ConnectorMark, connectorName, EmptyState, formatClock, formatDate, formatNumber, formatRelative, formatTimestamp, NativeSelect, PageHeader, Panel, PanelHeader, Stack, Status, Table, TableBody, TableCell, TableHead, TableHeader, TableRow, TableScroll } from "../../ui";
import { toneForState } from "../../data/DataCollectionLayout";
import { type DataSurfaceItem, useDataSurface } from "../../data/dataSurface";
import { discardDatastreamSetupDraft, listDatastreamSetupDrafts, type DatastreamSetupDraftListItem } from "../../datastreams/wizard/wizardApi";
import { resolvableHref } from "../routeHref";
import { DatastreamRunStateBadge } from "../../datastreams/workbench/DatastreamRunLive";
import DatastreamIssueBadge from "../../datastreams/workbench/DatastreamIssueBadge";
import { isRunProgressing } from "../../datastreams/workbench/datastreamProgress";
// The origin registry, mirrored from `server/core/run_origins.py`. The fleet
// reads it for the same reason the live band does: whether a moving run is a
// collection at all is a property of its ORIGIN, not of its state.
import { isKnownOrigin, originLabel, readsProviderWindows } from "../../datastreams/workbench/runOrigins";

interface DataWorkspaceProps {
  projectId: string;
  onOpenDatastream?: (id: string) => void;
  /** The section declares `actions: ["create"]` (ui/admin/src/shell/navigation.ts#WORKSPACES). Absent when
   *  the caller does not own the create route — the button is then not drawn,
   *  rather than drawn and inert. */
  onAddDatastream?: () => void;
}

function evidence(item: DataSurfaceItem, key: string): unknown {
  return (item.evidence ?? {})[key];
}

function text(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value : null;
}

function count(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function strings(value: unknown): string[] {
  return Array.isArray(value) ? value.map((entry) => String(entry)).filter(Boolean) : [];
}

/**
 * An absence, named — never a dash, never a zero, never a blank cell.
 *
 * The visible words say WHAT is missing; the title says where it would come
 * from. A blank cell reads as a column nobody filled in, and a `0` reads as a
 * measurement that was taken.
 */
function Absent({ children, why }: { children: ReactNode; why: string }) {
  return (
    <span className="text-text-secondary" title={why}>
      {children}
    </span>
  );
}

/**
 * How old the published data is.
 *
 * This held `<1h` / `26h` / `1d` — a SECOND relative-time vocabulary beside
 * `formatRelative`, so the same age read two ways on two screens. It is the
 * console's wording now (`console-presentation.md` §2, "Relative time").
 *
 * Beyond seven days `formatRelative` answers the instant itself, and the date
 * in the same cell already says that. The freshness note then adds nothing and
 * says nothing, rather than repeating its neighbour.
 */
function age(iso: string | null): string | null {
  if (!iso) return null;
  const relative = formatRelative(iso);
  return relative === formatTimestamp(iso) ? null : relative;
}

/** The dense-cell date grain: no year, because the column it sits in is a
 *  publication window a person is already reading a year into. */
function shortDate(iso: string | null): string | null {
  if (!iso) return null;
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return null;
  return formatDate(date, { year: false });
}

function clockTime(iso: string | null): string | null {
  if (!iso) return null;
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return null;
  const sameDay = date.toDateString() === new Date().toDateString();
  return sameDay ? formatClock(date) : formatDate(date, { year: false });
}

/** The cadence as a sentence, not as a fallback for a missing next run.
 *
 *  `manual` is the one value whose wire name is not the word a person uses:
 *  "Runs manual" is not English. Everything else is derived, so a cadence added
 *  to `app.datastreams.schedule_mode` tomorrow reads correctly here without an
 *  edit — a hand-kept table of cadences is how `weekly` went missing from four
 *  of them at once (AI-217). */
function cadenceSentence(cadence: string | null): string | null {
  if (!cadence) return null;
  return cadence === "manual" ? "Runs on demand" : `Runs ${cadence.replaceAll("_", " ")}`;
}

/** The exception a person has to act on, named — never a colour alone.
 *
 *  Precedence is written down because it is a decision: the run that just failed
 *  outranks the plan that cannot run, which outranks a plan that is blocked with
 *  no issue recorded. A blocked validation with an empty `validation_issues` is
 *  a real state and must still be visible — the version of this screen that only
 *  read the issue list showed such a row as merely `unavailable`. */
function exceptionOf(item: DataSurfaceItem): string | null {
  const code = text(evidence(item, "latest_exception"));
  if (code) return code.replaceAll("_", " ");
  const issues = evidence(item, "validation_issues");
  if (Array.isArray(issues) && issues.length > 0) {
    const first = issues[0];
    const named = typeof first === "string" ? first : text((first as Record<string, unknown>)?.code);
    if (named) return `${named.replaceAll("_", " ")}${issues.length > 1 ? ` +${issues.length - 1}` : ""}`;
  }
  if ((item.states.validation ?? "").toLowerCase() === "blocked") return "blocked";
  return null;
}

/** How far this Datastream is from the semantic layer.
 *
 *  Three answers, never two. `mdm_business_links` cannot bind a Domain to a
 *  Datastream (its `target_type` CHECK excludes it, migration 130), so the real
 *  chain is Datastream → mapping version → canonical target fields → Domain.
 *  What the fleet can say truthfully is therefore whether the stream reaches
 *  that chain at all — and a stream with NO mapping is a different problem from
 *  one whose mapping is blocked. Collapsing them makes "nothing was configured"
 *  read the same as "nothing arrives". */
function mappingReach(item: DataSurfaceItem): {
  tone: "success" | "warning" | "neutral";
  label: string;
  why: string;
} {
  const versionId = text(evidence(item, "mapping_version_id"));
  if (!versionId) {
    return {
      tone: "neutral",
      label: "Not mapped",
      why: "No mapping version is current, so no canonical field is bound and nothing reaches Governance.",
    };
  }
  const blocking = evidence(item, "mapping_blocking_count");
  const version = evidence(item, "active_mapping_version");
  if (typeof blocking === "number" && blocking > 0) {
    return {
      tone: "warning",
      label: `${blocking} blocking`,
      why: "The mapping exists but carries blocking bindings, so it cannot be published as it stands.",
    };
  }
  return {
    tone: "success",
    label: typeof version === "number" ? `v${version}` : "Mapped",
    why: "A mapping version is current and carries no blocking binding.",
  };
}

function needsAttention(item: DataSurfaceItem): boolean {
  const health = (item.states.health ?? "").toLowerCase();
  const validation = (item.states.validation ?? "").toLowerCase();
  return Boolean(exceptionOf(item)) || health === "degraded" || health === "failed" || validation === "blocked";
}

/** The source a row is filtered BY: the Connector when there is one, the
 *  ownership mode otherwise. A `managed_feed` has no Connector and must still be
 *  selectable, or the filter silently hides a third of the fleet. */
function sourceKey(item: DataSurfaceItem): string {
  return item.connector_ref?.id ?? item.source_kind ?? "unknown";
}

function sourceLabel(key: string): string {
  return connectorName(key).replaceAll("_", " ");
}

/** What the current plan selects, in the three shapes the data actually takes.
 *
 *  Measured on the disposable cluster: 202 of 246 current plans carry a grain,
 *  44 carry a plan with none, and 1012 Datastreams of 1258 have no current plan
 *  at all. Those are three different statements about a stream and the cell owes
 *  three different sentences — a single `—` for all three is the shape of a
 *  screen that knows nothing. */
function CollectsCell({ item }: { item: DataSurfaceItem }) {
  const grain = strings(evidence(item, "plan_grain"));
  const metrics = count(evidence(item, "plan_metric_count"));
  const dimensions = count(evidence(item, "plan_dimension_count"));
  const hasPlan =
    count(evidence(item, "active_plan_version")) !== null
    || metrics !== null
    || dimensions !== null
    || grain.length > 0;

  if (!hasPlan) {
    return (
      <Absent
        why={"No plan version is current for this Datastream, so nothing is selected yet. The selection is "
          + "written when a plan is activated from the Datastream's Processing tab."}
      >
        No current plan
      </Absent>
    );
  }

  const parts: string[] = [];
  if (metrics) parts.push(`${metrics} metric${metrics > 1 ? "s" : ""}`);
  if (dimensions) parts.push(`${dimensions} dimension${dimensions > 1 ? "s" : ""}`);

  return (
    <div className="min-w-0">
      {grain.length > 0 ? (
        <span className="text-text" title="The grain of the current plan: one row per combination of these fields.">
          {grain.join(" · ")}
        </span>
      ) : (
        <Absent
          why={"The current plan selects no grain, so the rows it collects are not keyed by any field. "
            + "The grain is chosen with the report in the Datastream's Processing tab."}
        >
          No grain selected
        </Absent>
      )}
      <div className="text-caption text-text-secondary">
        {parts.length > 0 ? (
          parts.join(" · ")
        ) : (
          <span
            title={"The current plan selects neither a metric nor a dimension, so a run of it would collect "
              + "no field."}
          >
            No field selected
          </span>
        )}
      </div>
    </div>
  );
}

/** Which source scope this stream pulls from, and TWO absences that are not one.
 *
 *  A Datastream naming no account (`source_account_id` NULL) is a different
 *  statement from an account whose authorization exposed no name: the first is
 *  repaired on the Datastream, the second on the Source. Reporting both as one
 *  blank sends people to the wrong screen. */
function SourceAccountCell({ item }: { item: DataSurfaceItem }) {
  const accountId = text(evidence(item, "source_account_id"));
  const label = text(evidence(item, "source_account_label"));
  if (!accountId) {
    return (
      <Absent
        why={"This Datastream names no source account. It was created before the Datastream carried one, "
          + "or its plan selects the authorization as a whole; the scope is chosen in the Datastream's setup."}
      >
        No source account
      </Absent>
    );
  }
  if (!label) {
    return (
      <Absent
        why={"The authorization exposes no name for this account: neither a provider label nor an external "
          + "account id was recorded by discovery. Re-running account discovery on the Source fills it."}
      >
        Account not named
      </Absent>
    );
  }
  return (
    <span className="min-w-0 truncate text-text" title={`Source account ${accountId}`}>
      {label}
    </span>
  );
}

export default function DataWorkspace({ projectId, onOpenDatastream, onAddDatastream }: DataWorkspaceProps) {
  const { state, reload } = useDataSurface(projectId, "datastreams");
  const [source, setSource] = useState("");

  /** THE WAY BACK INTO A SETUP DRAFT (57.12, T7). Until now the only door was
   *  the sessionStorage key the "Set up source access" handoff writes — leave
   *  the wizard by any other path and the work was invisible. The server lists
   *  the drafts; the screen offers the resume. ONE line, at most one draft:
   *  the partial unique index (migration 134) allows one resumable draft per
   *  Project.
   *
   *  A FAILED LIST READ RENDERS NOTHING, and that is deliberate: the Add
   *  button is the primary gesture, a ghost "resume" line over a route that
   *  did not answer would offer a door that opens nothing, and an error banner
   *  for an auxiliary read would warn about a screen that works. */
  const [resumableDraft, setResumableDraft] = useState<DatastreamSetupDraftListItem | null>(null);
  useEffect(() => {
    let disposed = false;
    setResumableDraft(null);
    listDatastreamSetupDrafts({ apiBase: "", projectId })
      .then((drafts) => {
        if (disposed) return;
        // Migration 284 retired `exited`: nothing ever wrote it, and this list
        // spelled the resumable set as `draft OR exited` with the second half
        // permanently false.
        setResumableDraft(drafts.find((draft) => draft.state === "draft") ?? null);
      })
      .catch(() => { if (!disposed) setResumableDraft(null); });
    return () => { disposed = true; };
  }, [projectId]);

  /** The resume writes the SAME sessionStorage key the source-access handoff
   *  writes and takes the SAME create route: `DatastreamCreate` reads that key
   *  into `resumeDraftId`, and the wizard restores the section by name. One
   *  channel — a second one would be two answers to "which draft is open". */
  const resumeSetup = onAddDatastream && resumableDraft
    ? () => {
      sessionStorage.setItem(`datastream-setup-return:${projectId}`, resumableDraft.draft_ref);
      onAddDatastream();
    }
    : null;

  /** THE OTHER END OF THE SAME LINE (AI-336, ratified by Jean 2026-08-31).
   *
   *  A draft in progress offered exactly one gesture — resume — so the only way
   *  to be rid of one was to finish it. It ends here too, because this is one of
   *  the two places a draft is visible at all, and a person who does not want it
   *  is more likely to meet it here than inside it.
   *
   *  The server writes `archived`; the line then disappears because it reads
   *  `state === "draft"`, and the resume key is forgotten so no stale bridge
   *  survives. A failure keeps the line and says why — never a line that
   *  silently stays after a gesture that claimed to remove it. */
  const [discardOpen, setDiscardOpen] = useState(false);
  const [discardBusy, setDiscardBusy] = useState(false);
  const [discardError, setDiscardError] = useState<string | null>(null);
  const discardDraft = async () => {
    if (!resumableDraft) return;
    setDiscardBusy(true);
    setDiscardError(null);
    try {
      await discardDatastreamSetupDraft({ apiBase: "", projectId }, resumableDraft.draft_ref);
      sessionStorage.removeItem(`datastream-setup-return:${projectId}`);
      setResumableDraft(null);
      setDiscardOpen(false);
    } catch (reason) {
      setDiscardError(reason instanceof Error ? reason.message : "This draft could not be discarded");
    } finally {
      setDiscardBusy(false);
    }
  };

  const items = state.status === "ready" ? state.envelope.items : [];

  const attention = useMemo(() => items.filter(needsAttention), [items]);

  /** The sources present in the loaded fleet, in the order they appear. A filter
   *  offering a source no row carries would be a choice with no object. */
  const sources = useMemo(() => {
    const seen = new Map<string, string>();
    for (const item of items) {
      const key = sourceKey(item);
      if (!seen.has(key)) seen.set(key, sourceLabel(key));
    }
    return [...seen].map(([id, label]) => ({ id, label }));
  }, [items]);

  // A filter kept across a Project change would hide the new Project's fleet
  // behind a source it does not have.
  const activeSource = sources.some((entry) => entry.id === source) ? source : "";
  const rows = activeSource ? items.filter((item) => sourceKey(item) === activeSource) : items;

  return (
    <Stack data-owner="data/datastreams">
      <PageHeader
        title="Datastreams"
        description={"The Project fleet, from source to publication. Each row carries its own lifecycle, "
          + "cadence, freshness and exceptions."}
        actions={onAddDatastream ? (
          <span className="flex items-center gap-3">
            {resumeSetup && (
              <>
                <span className="text-caption text-text-secondary">
                  A Datastream setup is in progress
                </span>
                <Button variant="secondary" onClick={resumeSetup}>Resume</Button>
                <Button variant="ghost" onClick={() => setDiscardOpen(true)}>Discard</Button>
              </>
            )}
            <Button onClick={onAddDatastream}>+ Add Datastream</Button>
          </span>
        ) : undefined}
      />

      {state.status === "loading" && (
        <p role="status" className="text-body text-text-secondary">Loading Datastreams…</p>
      )}

      {state.status === "error" && (
        <Status
          as="block"
          tone="error"
          title="Datastreams unavailable"
          action={<Button variant="secondary" onClick={reload}>Retry</Button>}
        >
          {state.message}. No fleet has been substituted — nothing below is stale data shown as current.
        </Status>
      )}

      {state.status === "ready" && (
        <>
          {/* Accompaniment, not decoration: the fleet's own posture, before the
              list. A person who opens this screen is asking "is anything wrong",
              and a table of 40 rows does not answer it. */}
          {attention.length > 0 && (
            <Status
              as="block"
              tone="warning"
              title={`${attention.length} Datastream${attention.length > 1 ? "s need" : " needs"} attention`}
              data-testid="fleet-attention"
            >
              {attention.map((item) => item.name ?? item.object_ref.id).join(" · ")}
            </Status>
          )}

          <Panel flush>
            <PanelHeader
              title="Fleet"
              description={
                state.envelope.evidence_as_of
                  ? `Evidence as of ${formatTimestamp(state.envelope.evidence_as_of)}.`
                  : "No evidence timestamp is available."
              }
              actions={
                items.length > 0 ? (
                  <NativeSelect
                    aria-label="Filter by source"
                    className="w-56"
                    value={activeSource}
                    onChange={(event) => setSource(event.target.value)}
                  >
                    <option value="">All sources</option>
                    {sources.map((entry) => (
                      <option key={entry.id} value={entry.id}>{entry.label}</option>
                    ))}
                  </NativeSelect>
                ) : undefined
              }
            />
            {items.length === 0 ? (
              <EmptyState
                title="No Datastreams"
                description={
                  state.envelope.unavailable_reasons[0]?.message
                  ?? "Create a Datastream to establish an owned ingestion and publication path."
                }
                action={onAddDatastream ? <Button onClick={onAddDatastream}>+ Add Datastream</Button> : undefined}
              />
            ) : rows.length === 0 ? (
              // A third statement, and not the empty fleet: the Project HAS
              // Datastreams and this filter matches none of them.
              <EmptyState
                title="No Datastream on this source"
                description={
                  `This Project has ${items.length} Datastream${items.length > 1 ? "s" : ""}, none of them on `
                  + `${sourceLabel(activeSource)}. Choose All sources to see them.`
                }
                action={<Button variant="secondary" onClick={() => setSource("")}>Show all sources</Button>}
              />
            ) : (
              <TableScroll label="Datastream fleet">
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead>Datastream</TableHead>
                      <TableHead>Source account</TableHead>
                      <TableHead>Mode</TableHead>
                      <TableHead>Data type</TableHead>
                      {/* What the current plan pulls, and at what grain. Absent
                          from the read model until 58.8 opened
                          `source.selection` on the plan version. */}
                      <TableHead>Collects</TableHead>
                      {/* Lifecycle stays a column of its own. The five axes must
                          not be collapsed into one word -- that is an
                          `Incomplete if` of the workbench contract, and the
                          screen this replaced at least got that right. What it
                          got wrong was showing ONLY axes. */}
                      <TableHead>Lifecycle</TableHead>
                      {/* The Governance/MDM reach. Not in `data.md:50`'s list
                          and not in the mockup — added because Jean's own
                          requirement for this screen is that it "fasse le lien
                          avec la gouvernance et le MDM", and because a fleet
                          that cannot say which streams reach the semantic layer
                          is the raw list being replaced. */}
                      <TableHead>Mapping</TableHead>
                      {/* Cadence lives in this cell, as a statement. */}
                      <TableHead>Next run</TableHead>
                      {/* Story 63.5 — is this Datastream collecting RIGHT NOW.
                          `latest_candidate_state` has been on the wire for every
                          row since this envelope existed (`data_surface.py`) and
                          nothing read it, so the fleet could not say which of
                          its forty streams were moving.
                          IT IS THE STATE, NOT THE PROGRESS, and that is a
                          decision: one poll per row would be 40 x 17_280 wake-ups
                          a night on a service that scales to zero, and a day
                          count that only moved when the whole fleet is reloaded
                          would be a number that lies between reloads. The list
                          says a collection is running; the Workbench says where
                          it has got to. */}
                      <TableHead>Collecting</TableHead>
                      {/* The latest execution: its mark AND its instant, on one
                          baseline. A state with no instant is five minutes or
                          five months old and reads the same. */}
                      <TableHead>Last run</TableHead>
                      {/* Freshness merged in here: the age sits beside the date
                          it is derived from. */}
                      <TableHead>Published</TableHead>
                      <TableHead>Issues</TableHead>
                      <TableHead>Status</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {rows.map((item) => {
                      const open = onOpenDatastream ? () => onOpenDatastream(item.object_ref.id) : undefined;
                      const publishedAt = text(evidence(item, "published_at"));
                      const nextRunAt = text(evidence(item, "next_run_at"));
                      const cadence = cadenceSentence(text(evidence(item, "cadence")));
                      const publishedRows = evidence(item, "published_row_count");
                      const dataRole = text(evidence(item, "data_role"));
                      const lastRunAt = text(evidence(item, "latest_run_at"));
                      const exception = exceptionOf(item);
                      // The state of the latest execution, as the fleet read
                      // measured it. The one-non-terminal-execution-per-flux
                      // lock (story 63.1) makes the latest one the active one
                      // whenever one is active, so this answers "is it
                      // collecting" with no request of its own.
                      const runState = evidence(item, "latest_candidate_state");
                      // WHY that run exists — story 63.7's key, now on the fleet
                      // payload. Without it this column called every moving run a
                      // collection, and four of the declared origins read no
                      // provider window at all, so a `mapping_change` said
                      // `Collecting` here while the live band said « This update
                      // reads no provider window » over the same execution.
                      const runOrigin = evidence(item, "latest_run_origin");
                      const provider = item.connector_ref?.id ?? item.source_kind;
                      return (
                        <TableRow
                          key={item.object_ref.id}
                          className={open ? "cursor-pointer" : undefined}
                          tabIndex={open ? 0 : undefined}
                          onClick={open}
                          onKeyDown={(event) => {
                            if (open && (event.key === "Enter" || event.key === " ")) open();
                          }}
                        >
                          <TableCell>
                            <div className="flex items-center gap-3">
                              <ConnectorMark provider={provider} />
                              <div className="min-w-0">
                                <strong className="block min-w-0 truncate text-text">
                                  {item.name ?? item.object_ref.id}
                                </strong>
                                {/* The mark alone identifies a vendor only to
                                    someone who already knows the logo. The name
                                    comes from the same managed registry as the
                                    asset, so a screen cannot show one vendor's
                                    mark beside another's label. */}
                                {item.connector_ref?.id ? (
                                  <span className="block text-caption text-text-secondary">
                                    {connectorName(item.connector_ref.id)}
                                  </span>
                                ) : (
                                  <Absent
                                    why={"No Connector is bound to this Datastream. The Mode column names how "
                                      + "it is fed instead."}
                                  >
                                    <span className="block text-caption">No connector</span>
                                  </Absent>
                                )}
                              </div>
                            </div>
                          </TableCell>
                          <TableCell>
                            <SourceAccountCell item={item} />
                          </TableCell>
                          <TableCell className="text-text-secondary">
                            {item.source_kind?.replaceAll("_", " ")
                              ?? (
                                <Absent why={"The row carries no source kind, so the ownership mode of this "
                                  + "Datastream is unknown to the read model."}>
                                  No mode
                                </Absent>
                              )}
                          </TableCell>
                          <TableCell>
                            {/* `app.datastreams.data_role`, one of the seven
                                values `core.datastreams.DATA_ROLES` declares. A
                                NULL is "nobody has classified this stream" —
                                which is what the cell says, rather than the
                                claim this screen printed until 58.8 that the
                                read model had no classification at all. */}
                            {dataRole ?? (
                              <Absent
                                why={"No data role is set on this Datastream. It is one of the seven values the "
                                  + "Datastream settings offer, and it is what groups the fleet by what a Datastream "
                                  + "is for."}
                              >
                                Not classified
                              </Absent>
                            )}
                          </TableCell>
                          <TableCell>
                            <CollectsCell item={item} />
                          </TableCell>
                          <TableCell>
                            <Status tone={toneForState(item.states.lifecycle)}>
                              {(item.states.lifecycle ?? "unavailable").replaceAll("_", " ")}
                            </Status>
                          </TableCell>
                          <TableCell>
                            {(() => {
                              const reach = mappingReach(item);
                              // Asked of the router, not trusted: the address is
                              // composed server-side (`data_surface.py:409`) and
                              // every link that function produced was refused by
                              // `parsePath` until 2026-08-03. A read model can
                              // regress the same way tomorrow, and the screen
                              // must degrade to "no link" rather than to a link
                              // that opens the unknown-route screen.
                              const href = resolvableHref(item.links?.mapping);
                              const badge = <Status tone={reach.tone}>{reach.label}</Status>;
                              // The link is what turns a reported gap into
                              // something a person can act on. Absent when the
                              // server did not send one — never a dead anchor.
                              return href ? (
                                <a
                                  href={href}
                                  title={reach.why}
                                  className="underline-offset-2 hover:underline"
                                  onClick={(event) => event.stopPropagation()}
                                >
                                  {badge}
                                </a>
                              ) : (
                                <span title={reach.why}>{badge}</span>
                              );
                            })()}
                          </TableCell>
                          <TableCell className="font-numeric text-text-secondary">
                            <div className="min-w-0">
                              <span className="block text-text">
                                {clockTime(nextRunAt) ?? (
                                  <Absent
                                    why={"No schedule state exists for the current plan version — a Datastream "
                                      + "without a current plan never carries a next run, whatever its cadence."}
                                  >
                                    Not scheduled
                                  </Absent>
                                )}
                              </span>
                              <span className="block text-caption text-text-secondary">
                                {cadence ?? (
                                  <span
                                    title={"No cadence is recorded on this Datastream, so nothing says how "
                                      + "often it is meant to run."}
                                  >
                                    No cadence recorded
                                  </span>
                                )}
                              </span>
                            </div>
                          </TableCell>
                          <TableCell>
                            {isRunProgressing(runState) ? (
                              <div className="min-w-0">
                                <DatastreamRunStateBadge state={runState} />
                                {/* WHAT is moving, when it is not a collection.
                                    The registry decides — `readsProviderWindows`
                                    is mirrored from `run_origins.py`, so this
                                    column and the live band cannot drift. An
                                    origin this build does not know says nothing
                                    either way: silence, never a guessed word. */}
                                {isKnownOrigin(runOrigin) && !readsProviderWindows(runOrigin) ? (
                                  <span
                                    className="mt-1 block truncate text-caption text-text-secondary"
                                    title={`${originLabel(runOrigin) ?? String(runOrigin)} — this update reads no provider window, so nothing is being collected.`}
                                  >
                                    {originLabel(runOrigin) ?? String(runOrigin)} — not a collection
                                  </span>
                                ) : null}
                              </div>
                            ) : (
                              <span
                                className="text-text-secondary"
                                title={"No run held the active lock when this fleet list was read. Open the "
                                  + "Datastream to watch a collection as it moves."}
                              >
                                Not collecting
                              </span>
                            )}
                          </TableCell>
                          <TableCell>
                            {/* The mark and the instant share one cell and one
                                baseline: read apart they are two facts, read
                                together they are the answer to "when did this
                                last do anything". The label is the registry's
                                (`executionStateLabel`), never a word typed
                                here. */}
                            {lastRunAt ? (
                              <span className="flex items-baseline gap-2">
                                <DatastreamRunStateBadge state={runState} />
                                <span className="font-numeric text-caption text-text-secondary">
                                  {clockTime(lastRunAt)}
                                </span>
                              </span>
                            ) : (
                              <Absent
                                why={"No execution of this Datastream carries an instant in the read model, so "
                                  + "there is no last run to date. A run writes one the moment its state changes."}
                              >
                                No run recorded
                              </Absent>
                            )}
                          </TableCell>
                          <TableCell className="font-numeric text-text-secondary">
                            {shortDate(publishedAt) ? (
                              <span className="flex items-baseline gap-2">
                                <span className="text-text">{shortDate(publishedAt)}</span>
                                {/* Freshness, in the cell it is derived from. */}
                                <span className="text-caption" title="How long ago this publication landed.">
                                  {age(publishedAt)}
                                </span>
                                {typeof publishedRows === "number" && (
                                  <span className="text-caption">{formatNumber(publishedRows)} rows</span>
                                )}
                              </span>
                            ) : (
                              <Absent
                                why={"No publication pointer is set for this Datastream, so nothing has been "
                                  + "published and there is no age to report."}
                              >
                                Never published
                              </Absent>
                            )}
                          </TableCell>
                          <TableCell>
                            {/* STORY 59.2 — the count, and the three sentences
                                it is not. This cell said "No monitor has run"
                                for every row, which was true on 2026-08-07 when
                                `app.dq_monitors` was empty everywhere and became
                                a measurably FALSE claim the day 59.3 and 59.4
                                published two monitors on a real preprod
                                Datastream. What replaces it is not one sentence
                                but four states, and the component owns them so
                                the header cannot disagree with the list. The
                                address is asked of the router, never composed
                                here (`data.md:289-295`). */}
                            <DatastreamIssueBadge
                              summary={evidence(item, "open_issues")}
                              href={resolvableHref(item.links?.runs)}
                            />
                          </TableCell>
                          <TableCell>
                            <Status tone={exception ? "warning" : toneForState(item.states.health)}>
                              {exception ?? (item.states.health ?? "unavailable").replaceAll("_", " ")}
                            </Status>
                          </TableCell>
                        </TableRow>
                      );
                    })}
                  </TableBody>
                </Table>
              </TableScroll>
            )}
          </Panel>
        </>
      )}

      {/* Discarding the setup in progress (AI-336, ratified 2026-08-31). NO
          evidence rows here, deliberately: the drafts list carries no
          `operator_input`, so this screen has never read what the draft holds,
          and printing a section id from the payload would be the database's
          vocabulary standing in for the person's. The wizard's own confirmation
          shows those three facts, because the wizard has them. */}
      <ConfirmDialog
        open={discardOpen}
        onOpenChange={(open) => { setDiscardOpen(open); if (!open) setDiscardError(null); }}
        title="Discard this setup?"
        description={
          "The answers already given are no longer offered back, and the next Add Datastream "
          + "starts from the first question. No Datastream was created from this setup, so "
          + "nothing that collects data changes."
        }
        confirmLabel="Discard this setup"
        cancelLabel="Keep the setup"
        destructive
        busy={discardBusy}
        error={discardError}
        data-testid="datastream-setup-discard"
        confirmTestId="datastream-setup-discard-confirm"
        cancelTestId="datastream-setup-discard-cancel"
        onConfirm={() => { void discardDraft(); }}
      />
    </Stack>
  );
}
