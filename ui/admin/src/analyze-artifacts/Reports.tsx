/**
 * Analyze > Reports — the collection, and the Report Workbench (Story 50.3).
 *
 * WHAT THIS REPLACES, and why the replacement is not cosmetic. `ReportsPanel.tsx`
 * lists connector seed rows and toggles `app.project_reports.enabled`. That
 * toggle is availability, not a Report: it holds no Query Spec version, no
 * presentation intent, no version history and no run. A user who edited a
 * "Report" there was editing a boolean on a connector pack.
 *
 * So this screen shows TWO lists that never merge: the Project's configured
 * Reports (stable head, immutable versions, immutable runs) and the connector
 * seeds available to draw from. `analyze-and-test.md:50` is the contract:
 * "the Report itself is not a cached answer".
 *
 * The five tabs are `analyze-and-test.md:96` exactly — Overview, Query,
 * Presentation, Runs, Versions — and Presentation says plainly that no accepted
 * presentation contract exists rather than showing an empty chart picker.
 *
 * Composed only from `ui/admin/src/ui/index.ts`.
 */
import { useCallback, useEffect, useState } from "react";

import { ApiError } from "../lib/apiFetch";
import {
  ObjectId,
  Badge,
  Button,
  Cluster,
  ConfirmDialog,
  EmptyState,
  Failure,
  Field,
  Loading,
  Metric,
  NativeSelect,
  NavTabs,
  NoScope,
  ObjectHeader,
  ObjectNotFound,
  ProjectNotFound,
  Retry,
  PageHeader,
  Panel,
  PanelHeader,
  Stack,
  Status,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
  TableScroll,
  Timestamp,
  StatusLegend,
  stateLabel,
} from "../ui";
import {
  archiveReport,
  fetchReport,
  createReportVersion,
  listReports,
  refusalsOf,
  runReportVersion,
  type ReportCollection,
  type ReportDetail,
  type Refusal,
} from "./client";
import { ContractUnavailable, outcomeLegend, outcomeTone, RefusalList } from "./Shared";

/** `analyze-and-test.md:96`, in that order. Declared, never inferred. */
export const REPORT_TABS = [
  { key: "overview", label: "Overview" },
  { key: "query", label: "Query" },
  { key: "presentation", label: "Presentation" },
  { key: "runs", label: "Runs" },
  { key: "versions", label: "Versions" },
] as const;

export const REPORT_DEFAULT_TAB = "overview";

type Phase<T> =
  | { status: "loading" }
  | { status: "ready"; data: T }
  | { status: "no-scope" }
  | { status: "not-found" }
  | { status: "error"; message: string };

export function ReportsCollection({
  projectId,
  onOpenReport,
}: {
  projectId?: string;
  onOpenReport?: (reportId: string) => void;
}) {
  const [phase, setPhase] = useState<Phase<ReportCollection>>({ status: "loading" });
  //: Which Report the confirmation is about — null when the dialog is closed.
  //: The archive is destructive enough to confirm (it empties the list) and
  //: reversible enough not to demand typing a name: nothing is deleted, and
  //: `include_archived` brings the artifact back.
  const [pendingArchive, setPendingArchive] = useState<
    { id: string; label: string } | null
  >(null);
  const [archiveBusy, setArchiveBusy] = useState(false);
  const [archiveError, setArchiveError] = useState<string | null>(null);
  //: Bumped after a successful archive so the list is re-read from the server.
  //: The row is not spliced out client-side: the server decides what is live,
  //: and a screen that hides a row it did not confirm gone is how a failed
  //: write comes to look like a success.
  const [reloadToken, setReloadToken] = useState(0);
  //: THE WAY BACK THE CONFIRMATION PROMISES. The archive dialog says "an
  //: archived Report can be listed again", the route has served
  //: `?include_archived=true` since the archive landed, and nothing in
  //: `ui/admin/src` ever sent it -- so the sentence was true of the server and
  //: false of the product. A screen that names a gesture and offers no control
  //: for it is the same defect as a button that does nothing, read backwards.
  const [includeArchived, setIncludeArchived] = useState(false);

  useEffect(() => {
    if (!projectId) {
      setPhase({ status: "no-scope" });
      return;
    }
    // Symmetric cleanup: the abort is what stops an earlier Project's response
    // from overwriting a later one after a fast switch.
    const controller = new AbortController();
    setPhase({ status: "loading" });
    listReports(projectId, { signal: controller.signal }, { includeArchived })
      .then((data) => setPhase({ status: "ready", data }))
      .catch((error: unknown) => {
        if (controller.signal.aborted) return;
        if (error instanceof ApiError && error.status === 404) {
          setPhase({ status: "not-found" });
          return;
        }
        setPhase({ status: "error", message: (error as Error).message });
      });
    return () => controller.abort();
  }, [projectId, reloadToken, includeArchived]);

  const confirmArchive = useCallback(async () => {
    if (!projectId || !pendingArchive) return;
    setArchiveBusy(true);
    setArchiveError(null);
    try {
      await archiveReport(projectId, pendingArchive.id);
      setPendingArchive(null);
      setReloadToken((token) => token + 1);
    } catch (error: unknown) {
      // The dialog stays open and says what happened. Closing it on failure
      // would leave the row in place with no explanation, which reads as a
      // button that does nothing.
      setArchiveError((error as Error).message);
    } finally {
      setArchiveBusy(false);
    }
  }, [projectId, pendingArchive]);

  if (phase.status === "no-scope") return <NoScope what="reports" />;
  if (phase.status === "loading") return <Loading label="reports" />;
  if (phase.status === "not-found") return <ProjectNotFound />;
  if (phase.status === "error") {
    return <Failure what="Reports" message={phase.message} action={<Retry onClick={() => setReloadToken((token) => token + 1)} />} />;
  }

  // The payload is CHECKED, not destructured on faith. A response without
  // `reports` threw `Cannot read properties of undefined (reading 'length')`
  // and took the whole screen down -- a blank page, not even the honest refusal
  // this file otherwise renders well. Third instance of this class today
  // (`RegressionRuns` leaked the same TypeError into its own error surface),
  // and the one that finally showed how far it goes: an unguarded destructure
  // has no error surface at all.
  const { reports, seeds, presentation_contract: contract } = phase.data ?? {};
  if (!Array.isArray(reports) || !Array.isArray(seeds)) {
    return (
      <Failure
        what="Reports"
        message="The Reports response did not carry a Report and seed list, so nothing can be shown."
        action={<Retry onClick={() => setReloadToken((token) => token + 1)} />}
      />
    );
  }

  return (
    <Stack>
      <PageHeader
        title="Reports"
        description="Reusable governed question intent. Opening or refreshing a Report runs it; the Report is not a cached answer."
      />

      <ContractUnavailable
        contract={contract}
        what="A default presentation"
        presentationKinds
      />

      <Panel>
        <PanelHeader
          title="Configured Reports"
          description="Project-owned, versioned, and each run recorded as its own immutable evidence."
          actions={
            /* THE WAY BACK, NAMED AS A STATE AND NOT AS A FILTER. "Show
               archived" is what the confirmation promised; the count is what
               makes it worth pressing, and its absence is why the promise read
               as decoration. */
            <Button
              variant={includeArchived ? "secondary" : "ghost"}
              onClick={() => setIncludeArchived((shown) => !shown)}
              aria-pressed={includeArchived}
              data-testid="reports-show-archived"
            >
              {includeArchived ? "Hide archived" : "Show archived"}
            </Button>
          }
        />
        {reports.length === 0 ? (
          /* An empty state must not INSTRUCT a path that does not exist.
             This one said "Save a question from Explore, or create one from a
             connector seed below" -- and neither existed: `createReport` was
             mounted server-side (`/api/projects/{id}/analyze/reports`) with ZERO
             call sites in `ui/admin/src`, grepped independently by three review
             lenses. It was then rewritten to say plainly that creating was
             unreachable.
             2026-08-04: the Explore half is now TRUE and the sentence goes back
             to instructing it -- `analyze/saveAsReport.ts` is the call site, and
             `ResultWorkbench` carries "Save this question as a Report". The
             connector-seed half is still not a path and stays unsaid: a seed is
             availability, and enabling one creates nothing. */
          <EmptyState
            title="No configured Report yet"
            description="Ask a question in Explore, then save it from its Result — a Report pins that Query Spec version, not the answer. A connector seed on its own is availability, not a Report."
          />
        ) : (
          <TableScroll label="Configured Reports">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Report</TableHead>
                  <TableHead>Version</TableHead>
                  <TableHead>Query Spec version</TableHead>
                  <TableHead>Presentation</TableHead>
                  <TableHead>Runs</TableHead>
                  <TableHead>Origin</TableHead>
                  <TableHead>Actions</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {reports.map((report) => (
                  <TableRow key={report.id}>
                    <TableCell>
                      {onOpenReport ? (
                        <Button variant="ghost" onClick={() => onOpenReport(report.id)}>
                          {report.label}
                        </Button>
                      ) : (
                        report.label
                      )}
                    </TableCell>
                    <TableCell>
                      {report.current_version_number
                        ? `v${report.current_version_number}`
                        : "—"}
                    </TableCell>
                    <TableCell>
                      <code>{report.query_spec_version_id ?? "—"}</code>
                    </TableCell>
                    <TableCell>
                      {report.presentation_absent ? (
                        <Badge tone="info">{report.presentation_absent}</Badge>
                      ) : (
                        <Badge tone="success">Pinned</Badge>
                      )}
                    </TableCell>
                    <TableCell>{report.run_count}</TableCell>
                    <TableCell>
                      {report.seed
                        ? `${report.seed.module_name}/${report.seed.report_id}`
                        : report.seed_origin}
                    </TableCell>
                    <TableCell>
                      <Button
                        variant="ghost"
                        onClick={() =>
                          setPendingArchive({ id: report.id, label: report.label })
                        }
                        data-testid={`archive-report-${report.id}`}
                        disabled={report.archived}
                      >
                        {/* An archived Report is shown, never re-archived: what
                            it needs is a restore, and no route serves one -- so
                            the control says what is true of it rather than
                            offering a gesture that would 200 and change
                            nothing. */}
                        {report.archived ? "Archived" : "Archive"}
                      </Button>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </TableScroll>
        )}
      </Panel>

      <Panel>
        <PanelHeader
          title="Connector report seeds"
          description="Expert packs this Project may draw from. A seed is an immutable source template — enabling one does not create a Report, and editing a Report never writes back to the pack."
        />
        {seeds.length === 0 ? (
          <EmptyState
            title="No connector seed available"
            description="No connector in this Project publishes a report pack."
          />
        ) : (
          <TableScroll label="Connector report seeds">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Connector</TableHead>
                  <TableHead>Seed</TableHead>
                  <TableHead>Availability</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {seeds.map((seed) => (
                  <TableRow key={`${seed.module_name}/${seed.report_id}`}>
                    <TableCell>{seed.module_name}</TableCell>
                    <TableCell>
                      <ObjectId value={seed.report_id} title="Report" />
                    </TableCell>
                    <TableCell>
                      <Badge tone={seed.enabled ? "success" : "neutral"}>
                        {seed.enabled ? "Available" : "Not available"}
                      </Badge>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </TableScroll>
        )}
      </Panel>

      <ConfirmDialog
        open={pendingArchive !== null}
        onOpenChange={(open) => {
          if (!open) {
            setPendingArchive(null);
            setArchiveError(null);
          }
        }}
        title="Archive this Report?"
        description="It leaves this list and accepts no new version. Its versions and runs are kept as evidence — nothing is deleted, and an archived Report can be listed again."
        evidenceLabel="Report"
        evidence={pendingArchive ? { Report: pendingArchive.label } : undefined}
        confirmLabel="Archive it"
        cancelLabel="Keep it active"
        destructive
        busy={archiveBusy}
        error={archiveError ?? undefined}
        onConfirm={() => void confirmArchive()}
        data-testid="archive-report-confirm"
      />
    </Stack>
  );
}

export function ReportWorkbench({
  projectId,
  reportId,
  tab,
  onNavigateTab,
  tabHref,
  collectionHref,
  onOpenResult,
}: {
  projectId?: string;
  reportId: string;
  tab: string;
  onNavigateTab?: (tab: string) => void;
  /** The way back when this address names no Report (76-4). REQUIRED: the
   *  workbench renders no rail and no tabs in that state, so an optional
   *  address would be an optional way out of a screen with no other. */
  collectionHref: string;
  /** The canonical address of each tab, supplied by the shell router.
   *  A workbench tab is route navigation, not local state: without an
   *  address the tab renders disabled rather than pretending to be a link. */
  tabHref?: (tab: string) => string;
  onOpenResult?: (resultId: string) => void;
}) {
  const [phase, setPhase] = useState<Phase<ReportDetail>>({ status: "loading" });
  const [reloadToken, setReloadToken] = useState(0);
  const [selectedVersionId, setSelectedVersionId] = useState<string | null>(null);
  const [running, setRunning] = useState(false);
  const [savingPresentation, setSavingPresentation] = useState(false);
  const [presentationKind, setPresentationKind] = useState("visualization_spec_version");
  const [presentationVersionId, setPresentationVersionId] = useState("");
  const [presentationFailure, setPresentationFailure] = useState<{
    message: string;
    refusals: Refusal[];
  } | null>(null);
  const [failure, setFailure] = useState<{ message: string; refusals: Refusal[] } | null>(
    null,
  );

  useEffect(() => {
    if (!projectId) {
      setPhase({ status: "no-scope" });
      return;
    }
    const controller = new AbortController();
    setPhase({ status: "loading" });
    fetchReport(projectId, reportId, { signal: controller.signal })
      .then((data) => setPhase({ status: "ready", data }))
      .catch((error: unknown) => {
        if (controller.signal.aborted) return;
        if (error instanceof ApiError && error.status === 404) {
          setPhase({ status: "not-found" });
          return;
        }
        setPhase({ status: "error", message: (error as Error).message });
      });
    return () => controller.abort();
  }, [projectId, reportId, reloadToken]);

  const run = useCallback(
    async (versionId: string) => {
      if (!projectId) return;
      setRunning(true);
      setFailure(null);
      try {
        await runReportVersion(projectId, versionId);
        setReloadToken((token) => token + 1);
      } catch (error) {
        setFailure({
          message: (error as Error).message,
          refusals: refusalsOf(error),
        });
      } finally {
        setRunning(false);
      }
    },
    [projectId],
  );

  const savePresentation = useCallback(async () => {
    if (!projectId || phase.status !== "ready") return;
    const currentVersion = phase.data.versions.find(
      (version) => version.id === phase.data.current_version_id,
    ) ?? phase.data.versions[0];
    if (!currentVersion || !presentationVersionId.trim()) return;
    setSavingPresentation(true);
    setPresentationFailure(null);
    try {
      await createReportVersion(projectId, reportId, {
        query_spec_version_id: currentVersion.query_spec_version_id,
        presentation: {
          kind: presentationKind,
          version_id: presentationVersionId.trim(),
        },
      });
      setPresentationVersionId("");
      setReloadToken((token) => token + 1);
    } catch (error) {
      setPresentationFailure({ message: (error as Error).message, refusals: refusalsOf(error) });
    } finally {
      setSavingPresentation(false);
    }
  }, [phase, presentationKind, presentationVersionId, projectId, reportId]);

  if (phase.status === "no-scope") return <NoScope what="this Report" />;
  if (phase.status === "loading") return <Loading label="this Report" />;
  if (phase.status === "not-found") {
    return <ObjectNotFound what="Report" collection="Reports" collectionHref={collectionHref} />;
  }
  if (phase.status === "error") {
    return <Failure what="This Report" message={phase.message} action={<Retry onClick={() => setReloadToken((token) => token + 1)} />} />;
  }

  const report = phase.data;
  const current =
    report.versions.find((version) => version.id === selectedVersionId) ??
    report.versions.find((version) => version.id === report.current_version_id) ??
    report.versions[0];
  const known = REPORT_TABS.some((entry) => entry.key === tab);
  //  WHAT THIS DEPLOYMENT CAN ACTUALLY PIN, read from the server and never
  //  guessed. An absent list means the server did not say, which is not the same
  //  as "everything is pinnable" -- so the picker below only ever DISABLES a kind
  //  the server has named, and offers the rest.
  const unpinnableKinds = report.presentation_contract?.unpinnable_kinds ?? [];
  const unpinnable = (kind: string) => unpinnableKinds.find((entry) => entry.kind === kind);
  const chartTemplateBlocked = unpinnable("visualization_template_version");
  //  WHAT CAN ACTUALLY BE PINNED, LISTED BY THE SERVER (AC21). An absent map is
  //  a deployment that predates the field, and it renders as "nothing of this
  //  kind" rather than as a free-text box: a pin nobody could verify is the
  //  thing this picker exists to stop.
  const pinnableVersions =
    report.pinnable_presentation_versions?.[presentationKind] ?? [];

  return (
    <Stack>
      {/* THE KEY SITS WHERE THE MARKS ARE. It was on the Reports LIST, whose
          `ReportSummary` carries no outcome at all — a legend explaining marks
          that were on another screen. The run table is here, so the key is here,
          and it lists only the outcomes these runs actually carry. */}
      <StatusLegend label="What a run outcome means" entries={outcomeLegend(report.runs.map((run) => run.outcome))} />
      <ObjectHeader
        name={report.label}
        source={`Report · ${report.seed ? `seeded by ${report.seed.module_name}` : report.seed_origin}`}
      />
      <NavTabs label="Report" tabs={REPORT_TABS.map((entry) => ({ ...entry, href: tabHref?.(entry.key) }))} current={tab} onNavigate={onNavigateTab} />
      {selectedVersionId && selectedVersionId !== report.current_version_id ? (
        <Status as="block" tone="info" title={`Viewing immutable version v${current.version_number}`}>
          This historical version is read-only. <Button size="xs" variant="ghost" onClick={() => setSelectedVersionId(null)}>Return to current</Button>
        </Status>
      ) : null}

      {!known ? (
        <EmptyState
          title="Unknown tab"
          description="This Report does not have that tab. No other tab is opened in its place."
        />
      ) : null}

      {failure ? (
        <Status as="block" tone="error" title="The run was refused">
          <Stack>
            <p>{failure.message}</p>
            <RefusalList refusals={failure.refusals} />
          </Stack>
        </Status>
      ) : null}

      {known && tab === "overview" ? (
        <Panel>
          <PanelHeader title="Overview" />
          <Cluster>
            <Metric label="Current version" value={current ? `v${current.version_number}` : "—"} />
            <Metric label="Runs" value={report.run_count} />
            <Metric label="Created by" value={report.created_by} />
            <Metric label="Last updated" value={<Timestamp value={report.updated_at} />} />
          </Cluster>
        </Panel>
      ) : null}

      {known && tab === "query" ? (
        <Panel>
          <PanelHeader
            title="Query"
            description="The exact Query Spec version this Report version pins. Changing the question creates a new Query Spec version and a new Report version — it never rewrites this one."
          />
          <Cluster>
            <Metric label="Query Spec" value={<code>{current?.query_spec_id ?? "—"}</code>} />
            <Metric
              label="Pinned version"
              value={<code>{current?.query_spec_version_id ?? "—"}</code>}
            />
          </Cluster>
          <Cluster>
            <Button
              onClick={() => current && run(current.id)}
              disabled={running || !current}
            >
              {running ? "Running…" : "Run this version"}
            </Button>
          </Cluster>
        </Panel>
      ) : null}

      {known && tab === "presentation" ? (
        <Panel>
          <PanelHeader
            title="Presentation"
            description="The default presentation intent this Report version references."
          />
          {current?.presentation.absent_literal ? (
            <Status
              as="block"
              tone="info"
              title={current.presentation.absent_literal}
            >
              {/* THE SENTENCE THAT WAS FALSE, AND THE HALF OF IT THAT WENT ON
                  BEING FALSE. It first claimed no Visualization Spec version
                  exists in this deployment — and they do. What was left said a
                  Chart Template version "cannot be pinned here yet", naming
                  story 72.1 as its owner: that story LANDED, migration 333
                  created `app.visualization_template_versions`, and the probe in
                  `render_contract_state` has answered pinnable ever since. So
                  the screen was announcing as absent an object the deployment
                  ships — the exact failure `visualization-and-rendering.md`
                  names in its Incomplete-if list.
                  Nothing is asserted here about which kinds exist. The server
                  probes its own registries and names what it cannot resolve;
                  this branch prints THAT, and nothing when there is nothing. */}
              This Report version references no presentation. It is recorded as absent
              rather than pinned to a placeholder — pin one below to create the next
              version.
              {chartTemplateBlocked ? (
                <>
                  {" "}
                  A Chart Template version cannot be pinned here yet: Story{" "}
                  {chartTemplateBlocked.owner_story} owns the registry that would resolve
                  one. A Visualization Spec version can.
                </>
              ) : null}
            </Status>
          ) : (
            <Stack>
              <Cluster>
                <Metric label="Kind" value={current?.presentation.kind ?? "—"} />
                <Metric
                  label="Version"
                  value={<code>{current?.presentation.version_id ?? "—"}</code>}
                />
              </Cluster>
              {/* A PIN THAT STILL RESOLVES, AND A TEMPLATE THAT IS RETIRED.
                  Archiving a Chart Template is a date on the head; nothing is
                  deleted, so this Report goes on resolving its presentation
                  exactly as it did the day it was pinned. What changed is that
                  the template is no longer offered below for a NEW pin — and a
                  screen that showed the pin without saying so would let a person
                  read a retired template as a live one. The server measures it
                  on the same column the picker filters on. */}
              {current?.presentation.archived ? (
                <Status
                  as="block"
                  tone="info"
                  title="The Chart Template this version pins is archived"
                  data-testid="report-presentation-archived"
                >
                  This Report resolves it exactly as before — archiving deletes nothing. It is
                  no longer offered below, so a new Report version cannot pin it until someone
                  restores it from its Chart Template workbench.
                </Status>
              ) : null}
            </Stack>
          )}
          <div className="border-t border-divider-base pt-4">
            <Stack>
              <div>
                <strong className="text-ui">Create a new presentation version</strong>
                <p className="m-0 text-ui text-text-secondary">
                  The current Report version stays immutable. The server validates the exact
                  presentation version before making the new version current.
                </p>
              </div>
              {presentationFailure ? (
                <Status as="block" tone="error" title="The presentation was not saved">
                  <Stack>
                    <p>{presentationFailure.message}</p>
                    <RefusalList refusals={presentationFailure.refusals} />
                  </Stack>
                </Status>
              ) : null}
              <div className="grid gap-3 md:grid-cols-[minmax(0,15rem)_minmax(0,1fr)_auto] md:items-end">
                <Field label="Presentation kind">
                  {(field) => (
                    <NativeSelect
                      {...field}
                      value={presentationKind}
                      onChange={(event) => {
                        setPresentationKind(event.target.value);
                        // A version id belongs to ONE kind. Carrying the previous
                        // selection across would compose a pin of kind A on an
                        // identifier of kind B — resolvable by nothing.
                        setPresentationVersionId("");
                      }}
                    >
                      <option value="visualization_spec_version">Visualization Spec version</option>
                      {/* OFFERED ONLY WHILE IT CAN BE PINNED. The server refuses
                          this kind until its registry exists (story 72.1), and a
                          control whose every use is a guaranteed refusal is a
                          promise the screen cannot keep. It is disabled and says
                          why, rather than removed: the object is ratified, and a
                          reader who came looking for it must find out where it
                          went. */}
                      <option
                        value="visualization_template_version"
                        disabled={Boolean(chartTemplateBlocked)}
                      >
                        {chartTemplateBlocked
                          ? `Chart Template version — not available yet (Story ${chartTemplateBlocked.owner_story})`
                          : "Chart Template version"}
                      </option>
                    </NativeSelect>
                  )}
                </Field>
                {/* THE PICKER THAT REPLACED A TEXT BOX (story 72.5, AC21).
                    This was an `<Input placeholder="vsv_…">`, and the commit
                    that left it there said exactly why: "aucun endpoint de liste
                    n'existe, un picker aurait invente une route" (`5576ae60`).
                    The route exists now — `GET /reports/{id}` carries
                    `pinnable_presentation_versions`, one list per kind the
                    deployment can actually resolve — so a person chooses a
                    version that EXISTS instead of typing one nobody verified.
                    `app.is_exact_pin` refuses six literal words and accepts
                    every other string, so a typed identifier was a pin the
                    database would happily store and no read could ever
                    resolve. */}
                <Field label="Version to pin">
                  {(field) =>
                    pinnableVersions.length === 0 ? (
                      <Status as="inline" tone="info" title="Nothing of this kind to pin yet">
                        {presentationKind === "visualization_template_version"
                          ? "This Project has no Chart Template version. Open Analyze > Reports > Chart Templates and draw one from a seed, or save a Visualization as a template."
                          : "This Project has no Visualization Spec version. Open a Result in Explore and build a Visualization from it."}
                      </Status>
                    ) : (
                      <NativeSelect
                        {...field}
                        value={presentationVersionId}
                        onChange={(event) => setPresentationVersionId(event.target.value)}
                        data-testid="report-presentation-version"
                      >
                        <option value="">Choose a version</option>
                        {pinnableVersions.map((entry) => (
                          <option key={entry.version_id} value={entry.version_id}>
                            {entry.label} · v{entry.version_number} · {entry.family}
                          </option>
                        ))}
                      </NativeSelect>
                    )
                  }
                </Field>
                <Button
                  type="button"
                  disabled={savingPresentation || !presentationVersionId.trim() || !current}
                  onClick={() => void savePresentation()}
                >
                  {savingPresentation ? "Saving…" : "Save as new version"}
                </Button>
              </div>
            </Stack>
          </div>
        </Panel>
      ) : null}

      {known && tab === "runs" ? (
        <Panel>
          <PanelHeader
            title="Runs"
            description="Every run is a new Result. Refresh never updates a prior Result or Render."
          />
          {report.runs.length === 0 ? (
            <EmptyState title="This Report has not been run yet" />
          ) : (
            <TableScroll label="Report runs">
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Run</TableHead>
                    <TableHead>Report version</TableHead>
                    <TableHead>Result</TableHead>
                    <TableHead>Render</TableHead>
                    <TableHead>Outcome</TableHead>
                    <TableHead>Ran at</TableHead>
                    <TableHead>Actor</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {report.runs.map((entry) => (
                    <TableRow key={entry.id}>
                      <TableCell>
                        <ObjectId value={entry.id} title="Entry" />
                      </TableCell>
                      <TableCell>
                        <ObjectId value={entry.report_version_id} title="Report version" />
                      </TableCell>
                      <TableCell>
                        {onOpenResult ? (
                          <Button variant="ghost" onClick={() => onOpenResult(entry.result_id)}>
                            <ObjectId value={entry.result_id} title="Result" />
                          </Button>
                        ) : (
                          <ObjectId value={entry.result_id} title="Result" />
                        )}
                      </TableCell>
                      <TableCell>
                        {entry.render_id ? (
                          <ObjectId value={entry.render_id} title="Render" />
                        ) : (
                          <Badge tone="info">No Render</Badge>
                        )}
                      </TableCell>
                      <TableCell>
                        <Badge tone={outcomeTone(entry.outcome)}>{stateLabel(entry.outcome)}</Badge>
                      </TableCell>
                      <TableCell><Timestamp value={entry.ended_at} /></TableCell>
                      <TableCell>{entry.actor}</TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </TableScroll>
          )}
        </Panel>
      ) : null}

      {known && tab === "versions" ? (
        <Panel>
          <PanelHeader
            title="Versions"
            description="Immutable and ordered. Editing appends; it never rewrites a prior version."
          />
          <TableScroll label="Report versions">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Version</TableHead>
                  <TableHead>Label</TableHead>
                  <TableHead>Query Spec version</TableHead>
                  <TableHead>Content hash</TableHead>
                  <TableHead>Predecessor</TableHead>
                  <TableHead>Created</TableHead>
                  <TableHead>Open</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {report.versions.map((version) => (
                  <TableRow key={version.id}>
                    <TableCell>
                      v{version.version_number}
                      {version.id === report.current_version_id ? (
                        <Badge tone="success">Current</Badge>
                      ) : null}
                    </TableCell>
                    <TableCell>{version.label}</TableCell>
                    <TableCell>
                      <ObjectId value={version.query_spec_version_id} title="Query Spec version" />
                    </TableCell>
                    <TableCell>
                      <code>{version.content_hash.slice(0, 12)}…</code>
                    </TableCell>
                    <TableCell>
                      <code>{version.predecessor_version_id ?? "—"}</code>
                    </TableCell>
                    <TableCell><Timestamp value={version.created_at} /></TableCell>
                    <TableCell>
                      <Button size="xs" variant="ghost" onClick={() => setSelectedVersionId(version.id)}>
                        {version.id === current.id ? "Opened" : "Open version"}
                      </Button>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </TableScroll>
        </Panel>
      ) : null}
    </Stack>
  );
}

export default ReportsCollection;
