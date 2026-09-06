/**
 * Analyze > Notebooks — the collection, and the Notebook Workbench (Story 50.3).
 *
 * WHAT THIS REPLACES. `NotebooksPanel.tsx` lists `app.notebooks`, whose row holds
 * ONE `report_ref`, one `window_rule` and one prompt, and which is UPDATED in
 * place when edited — so every previous Run refers to a definition that no longer
 * exists. This screen reads the canonical model instead: a stable head, immutable
 * composition versions with ordered version-pinned blocks, and Runs that pin the
 * exact version they ran plus, per block, the exact Result and the exact Render.
 *
 * The three tabs are `analyze-and-test.md:97` exactly — Content, Runs, Versions.
 *
 * COMPOSING HAPPENS ON BOTH SCREENS, THROUGH ONE COMPONENT. `BlockComposer` is
 * mounted by the collection (create) and by the Content tab (append a version),
 * because they are the same act on the same object. The Content tab used to be
 * read-only while describing a save — "Saving content appends a version; the
 * head advances" — that no control on it could perform, although
 * `POST {base}/notebooks/{id}/versions` was mounted server-side
 * (`server/core/analyze_artifacts_api.py:592-596`) and typed in `client.ts`.
 * Blocks are ordered by direct manipulation, with the keyboard keeping parity
 * on the same elements (`visualization-and-rendering.md:488`).
 *
 * Two things this screen refuses to do, both because the legacy one did them:
 *
 *   * it never falls back to a Project id of `default`. Without a Project it
 *     says so; it does not guess one and show another workspace's notebooks;
 *   * it never follows a "latest run". The collection shows a LAST RUN badge,
 *     which is display state, and every link addresses a Run by identity.
 *
 * Composed only from `ui/admin/src/ui/index.ts`.
 */
import { useCallback, useEffect, useState } from "react";

import { ApiError } from "../lib/apiFetch";
import { wireWord,
  ObjectId,
  Badge,
  Button,
  Cluster,
  ConfirmDialog,
  EmptyState,
  Failure,
  Field,
  FieldComposer,
  FieldWell,
  Input,
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
  Textarea,
  Timestamp,
  type WellBindings,
  type WellField,
  type WellSpec,
  StatusLegend,
  NO_VALUE,
  stateLabel,
} from "../ui";
import {
  archiveNotebook,
  createNotebook,
  createNotebookVersion,
  fetchNotebook,
  fetchNotebookRun,
  listLegacyNotebooks,
  listNotebooks,
  listReports,
  refusalsOf,
  runNotebook,
  type LegacyNotebook,
  type NotebookBlock,
  type NotebookDetail,
  type NotebookRun,
  type NotebookSummary,
  type Refusal,
  type ReportSummary,
} from "./client";
import { outcomeLegend, outcomeTone, RefusalList } from "./Shared";

/** `analyze-and-test.md:97`, in that order. Declared, never inferred. */
export const NOTEBOOK_TABS = [
  { key: "content", label: "Content" },
  { key: "runs", label: "Runs" },
  { key: "versions", label: "Versions" },
] as const;

export const NOTEBOOK_DEFAULT_TAB = "content";

/** The maximum a composition version holds, mirrored from `_MAX_BLOCKS`
 *  (`server/core/analyze_artifacts.py`). The server refuses past it; this is
 *  what stops the screen from letting someone build a refusal. */
const MAX_BLOCKS = 100;

/**
 * One block of a composition WHILE IT IS BEING EDITED.
 *
 * `uid` is not `block_key`, and the difference is the whole reason this type
 * exists. `block_key` is rebuilt from position on every move of a NEW
 * composition — the server reads it and the collection has always sent
 * `report-1..N` — so it cannot also be the identity a drag holds onto: a field
 * whose id changes as it moves loses its place mid-gesture. `uid` is minted
 * once, never sent, and never reused.
 */
type DraftBlock = {
  uid: string;
  block_key: string;
  block_type: NotebookBlock["block_type"];
  report_version_id: string | null;
  query_spec_version_id: string | null;
  as_of_rule: string;
  narrative: Record<string, unknown>;
};

let uidSeed = 0;
const mintUid = () => `draft-${(uidSeed += 1)}`;

/** The pin a block carries, or nothing — a narrative block pins no version. */
function pinOf(block: DraftBlock): string {
  return block.report_version_id ?? block.query_spec_version_id ?? "";
}

/**
 * Composition is direct manipulation, and the keyboard keeps parity
 * (`visualization-and-rendering.md:488`). One well, one role: every block is a
 * block, so the only refusal this well can produce is its own bound. The
 * gesture — pick up with Enter, place with a real button, drag with the pointer
 * onto the same target, insertion line while it is in the air — comes from
 * `ui/FieldWells.tsx` rather than being written a second time here.
 */
const BLOCK_WELL: WellSpec = {
  name: "composition",
  label: "Composition",
  accepts: ["block"],
  max: MAX_BLOCKS,
  hint: "Blocks run in this order. Drag one to move it, or pick it up and use the arrows beside it.",
};

/** Blocks as the well lists them. The report LABEL is shown when this Project
 *  still publishes that exact version; otherwise the block's own key is the
 *  honest name, because inventing one for a pin nobody can resolve would name
 *  a Report this composition does not point at. */
function blockField(block: DraftBlock, index: number, reports: ReportSummary[]): WellField {
  const report = block.report_version_id
    ? reports.find((entry) => entry.current_version_id === block.report_version_id)
    : undefined;
  const pin = pinOf(block);
  return {
    id: block.uid,
    label: report?.label ?? block.block_key,
    role: "block",
    hint: `Block ${index + 1} · ${block.block_type}${pin ? ` · ${pin}` : " · no pinned version"}`,
  };
}

/** The wire shape. `presentation` is deliberately NOT echoed back: the server
 *  resolves it from the presentation contract, and returning the absent literal
 *  it gave us would ask it to accept its own refusal as an input. */
function blockPayload(blocks: DraftBlock[]): Record<string, unknown>[] {
  return blocks.map((block) => ({
    block_key: block.block_key,
    block_type: block.block_type,
    report_version_id: block.report_version_id,
    query_spec_version_id: block.query_spec_version_id,
    as_of_rule: block.as_of_rule,
    narrative: block.narrative,
  }));
}

/** A saved composition version, opened for editing. Keys are PRESERVED: a
 *  block that already exists keeps the identity its earlier Runs name, and a
 *  narrative block renamed `report-2` would be a lie about what it is. */
/**
 * The two closed vocabularies a notebook block carries, declared beside the
 * `DraftBlock` shape this file reads them off.
 *
 * `BLOCK_TYPE_LABEL` mirrors `_BLOCK_TYPES` (`server/core/analyze_artifacts.py:193`,
 * `frozenset({"query", "report", "narrative"})`). The three words are written
 * out rather than de-tokenised so a fourth type added on the server is a word
 * this file does not know, not an unexplained one on a screen.
 *
 * `PRESENTATION_KIND_LABEL` takes the SERVER's own product noun:
 * `_PRESENTATION_KIND_REGISTRY` (`server/core/analyze_artifacts.py:127`) carries
 * it as the fourth element of each tuple, with its own comment saying *"The
 * product noun, never the wire token"*. Copying that column is not a second
 * decision; inventing a different word here would be.
 */
const BLOCK_TYPE_LABEL: Record<string, string> = {
  query: "Query",
  report: "Report",
  narrative: "Narrative",
};

const PRESENTATION_KIND_LABEL: Record<string, string> = {
  visualization_spec_version: "Visualization Spec version",
  visualization_template_version: "Chart Template version",
};

const blockTypeLabel = (token: string) => BLOCK_TYPE_LABEL[token] ?? wireWord(token);
const presentationKindLabel = (token: string | null | undefined) => (token ? PRESENTATION_KIND_LABEL[token] : undefined) ?? wireWord(token);

function seedBlocks(blocks: NotebookBlock[]): DraftBlock[] {
  return blocks.map((block) => ({
    uid: mintUid(),
    block_key: block.block_key,
    block_type: block.block_type,
    report_version_id: block.report_version_id,
    query_spec_version_id: block.query_spec_version_id,
    as_of_rule: block.as_of_rule,
    narrative: block.narrative ?? {},
  }));
}

/** Positional keys, for a composition being created. Unchanged from the
 *  buttons this replaced: the POST has always carried `report-1..N` in order. */
function rekeyed(blocks: DraftBlock[]): DraftBlock[] {
  return blocks.map((block, position) => ({ ...block, block_key: `report-${position + 1}` }));
}

/** A key nothing in this draft uses yet, for a block added to an existing
 *  composition whose other keys are kept. */
function freshKey(blocks: DraftBlock[]): string {
  const used = new Set(blocks.map((block) => block.block_key));
  let index = blocks.length + 1;
  while (used.has(`report-${index}`)) index += 1;
  return `report-${index}`;
}

/**
 * The ordered block list, and the one control that adds to it.
 *
 * Mounted twice — the collection's create panel and the Content tab of an
 * existing Notebook — because a person composing and a person editing perform
 * the same act. `rekey` is the only thing that differs, and it differs for a
 * measured reason: a new composition names its blocks by position, an existing
 * one keeps the keys its Runs already pin.
 */
function BlockComposer({
  blocks,
  onBlocks,
  reports,
  reportsLoading,
  onRetryReports,
  reportsError,
  rekey,
  instance,
}: {
  blocks: DraftBlock[];
  onBlocks: (next: DraftBlock[]) => void;
  reports: ReportSummary[];
  reportsLoading: boolean;
  /** Re-reads the Report versions this composer pins (76-4). */
  onRetryReports: () => void;
  reportsError: string | null;
  rekey: boolean;
  instance: string;
}) {
  const [selectedReportId, setSelectedReportId] = useState("");
  const settle = (next: DraftBlock[]) => onBlocks(rekey ? rekeyed(next) : next);

  const addBlock = () => {
    const report = reports.find((entry) => entry.id === selectedReportId);
    if (
      blocks.length >= MAX_BLOCKS ||
      !report?.current_version_id ||
      blocks.some((entry) => entry.report_version_id === report.current_version_id)
    ) return;
    settle([
      ...blocks,
      {
        uid: mintUid(),
        block_key: rekey ? `report-${blocks.length + 1}` : freshKey(blocks),
        block_type: "report",
        report_version_id: report.current_version_id,
        query_spec_version_id: null,
        as_of_rule: "current",
        narrative: {},
      },
    ]);
    setSelectedReportId("");
  };

  // A move gives back the new ORDER of ids; the blocks themselves are looked up
  // from it. A field the well no longer holds was removed, and dropping it here
  // is what makes the well's own remove button real.
  const applyBindings = (next: WellBindings) => {
    const byUid = new Map(blocks.map((block) => [block.uid, block]));
    settle(
      (next[BLOCK_WELL.name] ?? [])
        .map((uid) => byUid.get(uid))
        .filter((block): block is DraftBlock => Boolean(block)),
    );
  };

  return (
    <Stack>
      <div className="grid gap-3 md:grid-cols-[minmax(0,1fr)_auto] md:items-end">
        <Field label="Add a current Report version">
          {(field) => (
            <NativeSelect {...field} value={selectedReportId} onChange={(event) => setSelectedReportId(event.target.value)}>
              <option value="">Choose a Report</option>
              {reports.map((report) => (
                <option key={report.id} value={report.id}>
                  {report.label} · v{report.current_version_number ?? "?"}
                </option>
              ))}
            </NativeSelect>
          )}
        </Field>
        <Button type="button" variant="secondary" disabled={!selectedReportId || blocks.length >= MAX_BLOCKS} onClick={addBlock}>Add block</Button>
      </div>
      {reportsLoading ? (
        <Status as="block" active title="Loading Report versions">
          Reading the server-owned version pins available to this Project.
        </Status>
      ) : reportsError ? (
        <Status
          as="block"
          tone="error"
          title="Report versions are unavailable"
          action={<Retry onClick={onRetryReports} />}
        >
          {reportsError}
        </Status>
      ) : reports.length === 0 ? (
        <Status as="block" tone="info" title="No Report version is available">
          Save a Result as a Report first; the Notebook composer only accepts server-owned version pins.
        </Status>
      ) : null}
      <FieldComposer
        fields={blocks.map((block, index) => blockField(block, index, reports))}
        wells={[BLOCK_WELL]}
        bindings={{ [BLOCK_WELL.name]: blocks.map((block) => block.uid) }}
        onChange={applyBindings}
      >
        <FieldWell
          well={BLOCK_WELL}
          instance={instance}
          emptyHint="Add a current Report version above. Blocks run in the order they are listed here."
          consequence={`${blocks.length}/${MAX_BLOCKS} blocks. Order is content: saving it pins this sequence into an immutable version.`}
        />
      </FieldComposer>
    </Stack>
  );
}

type Phase<T> =
  | { status: "loading" }
  | { status: "ready"; data: T }
  | { status: "no-scope" }
  | { status: "not-found" }
  | { status: "error"; message: string };

export function NotebooksCollection({
  projectId,
  onOpenNotebook,
}: {
  projectId?: string;
  onOpenNotebook?: (notebookId: string) => void;
}) {
  const [phase, setPhase] = useState<Phase<NotebookSummary[]>>({ status: "loading" });
  const [legacy, setLegacy] = useState<Phase<LegacyNotebook[]>>({ status: "loading" });
  const [reports, setReports] = useState<ReportSummary[]>([]);
  const [reportsLoading, setReportsLoading] = useState(true);
  const [reportsError, setReportsError] = useState<string | null>(null);
  const [reloadToken, setReloadToken] = useState(0);
  //: THE WAY BACK THE CONFIRMATION PROMISES. The archive dialog says "an
  //: archived Notebook can be listed again", the route has served
  //: `?include_archived=true` since the archive landed, and nothing in
  //: `ui/admin/src` ever sent it -- so the sentence was true of the server and
  //: false of the product. A screen that names a gesture and offers no control
  //: for it is the same defect as a button that does nothing, read backwards.
  const [includeArchived, setIncludeArchived] = useState(false);
  const [composing, setComposing] = useState(false);
  const [label, setLabel] = useState("");
  const [description, setDescription] = useState("");
  const [draftBlocks, setDraftBlocks] = useState<DraftBlock[]>([]);
  const [createError, setCreateError] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  //: 67-19. THE SAME WRITER THE REPORTS LIST HAS CARRIED SINCE 09f9d5be, and
  //: the same reason it is a confirmation: archiving is destructive enough to
  //: ask (it empties the list, and a Notebook's schedule stops with it) and
  //: reversible enough not to demand typing a name — nothing is deleted.
  //: `POST .../notebooks/{id}/archive` was served and `archiveNotebook` existed
  //: in the client with zero call sites; a verb nobody can reach is a verb the
  //: product does not have.
  const [pendingArchive, setPendingArchive] = useState<
    { id: string; label: string } | null
  >(null);
  const [archiveBusy, setArchiveBusy] = useState(false);
  const [archiveError, setArchiveError] = useState<string | null>(null);

  useEffect(() => {
    if (!projectId) {
      setPhase({ status: "no-scope" });
      return;
    }
    const controller = new AbortController();
    setPhase({ status: "loading" });
    listNotebooks(projectId, { signal: controller.signal }, { includeArchived })
      .then((body) => setPhase({ status: "ready", data: body.notebooks }))
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

  useEffect(() => {
    if (!projectId) return;
    const controller = new AbortController();
    setReportsLoading(true);
    setReportsError(null);
    listReports(projectId, { signal: controller.signal })
      .then((body) => setReports(body.reports.filter((report) => report.current_version_id)))
      .catch((error: unknown) => {
        if (!controller.signal.aborted) setReportsError((error as Error).message);
      })
      .finally(() => {
        if (!controller.signal.aborted) setReportsLoading(false);
      });
    return () => controller.abort();
  }, [projectId, reloadToken]);

  // A SECOND request, and a second state, on purpose. The legacy read failing
  // must not empty the canonical list, and the canonical list must never absorb
  // legacy rows -- a legacy definition has no version history and its Runs pin no
  // Result, so one merged table would be a lie about half its rows.
  useEffect(() => {
    if (!projectId) {
      setLegacy({ status: "no-scope" });
      return;
    }
    const controller = new AbortController();
    setLegacy({ status: "loading" });
    listLegacyNotebooks(projectId, { signal: controller.signal })
      .then((body) => setLegacy({ status: "ready", data: body.notebooks }))
      .catch((error: unknown) => {
        if (controller.signal.aborted) return;
        setLegacy({ status: "error", message: (error as Error).message });
      });
    return () => controller.abort();
  }, [projectId]);

  const confirmArchive = async () => {
    if (!projectId || !pendingArchive) return;
    setArchiveBusy(true);
    setArchiveError(null);
    try {
      await archiveNotebook(projectId, pendingArchive.id);
      setPendingArchive(null);
      // The row is NOT spliced out client-side: the server decides what is
      // live, and a screen that hides a row it did not confirm gone is how a
      // failed write comes to look like a success.
      setReloadToken((token) => token + 1);
    } catch (error: unknown) {
      // The dialog stays open and says what happened. Closing it on failure
      // would leave the row in place with no explanation, which reads as a
      // button that does nothing.
      setArchiveError((error as Error).message);
    } finally {
      setArchiveBusy(false);
    }
  };

  if (phase.status === "no-scope") return <NoScope what="notebooks" />;
  if (phase.status === "loading") return <Loading label="notebooks" />;
  if (phase.status === "not-found") return <ProjectNotFound />;
  if (phase.status === "error") {
    return <Failure what="Notebooks" message={phase.message} action={<Retry onClick={() => setReloadToken((token) => token + 1)} />} />;
  }

  const submitNotebook = async () => {
    if (!projectId || !label.trim() || draftBlocks.length === 0) return;
    setCreating(true);
    setCreateError(null);
    try {
      const created = await createNotebook(projectId, {
        label: label.trim(),
        description: description.trim() || undefined,
        blocks: blockPayload(draftBlocks),
      });
      setLabel("");
      setDescription("");
      setDraftBlocks([]);
      setComposing(false);
      setReloadToken((token) => token + 1);
      onOpenNotebook?.(created.notebook_id);
    } catch (error) {
      setCreateError((error as Error).message);
    } finally {
      setCreating(false);
    }
  };

  return (
    <Stack>
      <PageHeader
        title="Notebooks"
        description="Re-runnable what-and-why compositions. Every run pins the exact composition version it ran."
        legend={
          <StatusLegend
            label="What a run outcome means"
            entries={outcomeLegend(phase.data.map((notebook) => notebook.last_run_outcome))}
          />
        }
      />
      <Cluster>
        <Button type="button" onClick={() => setComposing((open) => !open)}>
          {composing ? "Close composer" : "Compose a Notebook"}
        </Button>
      </Cluster>
      {composing ? (
        <Panel>
          <PanelHeader
            title="New Notebook"
            description="Build an ordered composition from the current immutable versions of Reports in this Project."
          />
          <Stack>
            {createError ? (
              <Status as="block" tone="error" title="The Notebook was not created">
                {createError}
              </Status>
            ) : null}
            <div className="grid gap-3 md:grid-cols-2">
              <Field label="Notebook name">
                {(field) => <Input {...field} value={label} onChange={(event) => setLabel(event.target.value)} />}
              </Field>
              <Field label="Description">
                {(field) => <Textarea {...field} value={description} onChange={(event) => setDescription(event.target.value)} rows={2} />}
              </Field>
            </div>
            <BlockComposer
              blocks={draftBlocks}
              onBlocks={setDraftBlocks}
              reports={reports}
              reportsLoading={reportsLoading}
              reportsError={reportsError}
              onRetryReports={() => setReloadToken((token) => token + 1)}
              rekey
              instance="new-notebook"
            />
            <Cluster className="justify-end">
              <Button type="button" disabled={creating || !label.trim() || draftBlocks.length === 0} onClick={() => void submitNotebook()}>
                {creating ? "Creating…" : "Create Notebook"}
              </Button>
            </Cluster>
          </Stack>
        </Panel>
      ) : null}
      <Panel>
        <PanelHeader
          title="Notebooks"
          actions={
            /* THE WAY BACK, NAMED AS A STATE AND NOT AS A FILTER. "Show
               archived" is what the confirmation promised; the count is what
               makes it worth pressing, and its absence is why the promise read
               as decoration. */
            <Button
              variant={includeArchived ? "secondary" : "ghost"}
              onClick={() => setIncludeArchived((shown) => !shown)}
              aria-pressed={includeArchived}
              data-testid="notebooks-show-archived"
            >
              {includeArchived ? "Hide archived" : "Show archived"}
            </Button>
          }
        />
        {phase.data.length === 0 ? (
          <EmptyState
            title="No Notebook yet"
            description="Compose one from the current immutable versions of Reports in this Project."
          />
        ) : (
          <TableScroll label="Notebooks">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Notebook</TableHead>
                  <TableHead>Version</TableHead>
                  <TableHead>Runs</TableHead>
                  <TableHead>Last run</TableHead>
                  <TableHead>Schedule</TableHead>
                  <TableHead>Origin</TableHead>
                  <TableHead>Retire</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {phase.data.map((notebook) => (
                  <TableRow key={notebook.id}>
                    <TableCell>
                      {onOpenNotebook ? (
                        <Button variant="ghost" onClick={() => onOpenNotebook(notebook.id)}>
                          {notebook.label}
                        </Button>
                      ) : (
                        notebook.label
                      )}
                    </TableCell>
                    <TableCell>
                      {notebook.current_version_number
                        ? `v${notebook.current_version_number}`
                        : "—"}
                    </TableCell>
                    <TableCell>{notebook.run_count}</TableCell>
                    <TableCell>
                      {notebook.last_run_at ? (
                        <Cluster>
                          {/* NO INVENTED WORD. `?? "accepted"` claimed the last run had been
                              accepted whenever the server sent no outcome at all —
                              a verdict nobody reached, in the colour of one. The
                              row is only rendered when `last_run_at` exists, so an
                              absent outcome is a gap in the record, and it reads as
                              the dash the console uses for one. */}
                          {notebook.last_run_outcome ? (
                            <Badge tone={outcomeTone(notebook.last_run_outcome)}>
                              {stateLabel(notebook.last_run_outcome)}
                            </Badge>
                          ) : (
                            <span className="text-text-secondary">{NO_VALUE}</span>
                          )}
                          <span><Timestamp value={notebook.last_run_at} absentMeaning="Never run" /></span>
                        </Cluster>
                      ) : (
                        "Never run"
                      )}
                    </TableCell>
                    <TableCell>
                      {notebook.schedule ? (
                        <Badge tone={notebook.schedule.enabled ? "success" : "neutral"}>
                          {notebook.schedule.recurrence}
                        </Badge>
                      ) : (
                        "None"
                      )}
                    </TableCell>
                    <TableCell>
                      {notebook.legacy_notebook_id ? (
                        <Badge tone="warning">Migrated from legacy</Badge>
                      ) : (
                        "Project"
                      )}
                    </TableCell>
                    <TableCell>
                      <Button
                        variant="ghost"
                        onClick={() =>
                          setPendingArchive({ id: notebook.id, label: notebook.label })
                        }
                        data-testid={`archive-notebook-${notebook.id}`}
                        disabled={notebook.archived}
                      >
                        {notebook.archived ? "Archived" : "Archive"}
                      </Button>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </TableScroll>
        )}
      </Panel>

      <LegacyNotebooksPanel phase={legacy} retry={() => setReloadToken((token) => token + 1)} />

      {/* THE SENTENCE SAYS WHAT ACTUALLY HAPPENS, and it is not the Report's
          sentence with a word swapped: a Notebook carries a SCHEDULE, and
          archiving stops it. Leaving that out would make the reader discover
          it from a dispatch that never came. */}
      <ConfirmDialog
        open={pendingArchive !== null}
        onOpenChange={(open) => {
          if (!open) {
            setPendingArchive(null);
            setArchiveError(null);
          }
        }}
        title="Archive this Notebook?"
        description="It leaves this list, accepts no new version, and its schedule stops dispatching. Its versions and runs are kept as evidence — nothing is deleted, and an archived Notebook can be listed again."
        evidenceLabel="Notebook"
        evidence={pendingArchive ? { Notebook: pendingArchive.label } : undefined}
        confirmLabel="Archive it"
        cancelLabel="Keep it active"
        destructive
        busy={archiveBusy}
        error={archiveError ?? undefined}
        onConfirm={() => void confirmArchive()}
        data-testid="archive-notebook-confirm"
      />
    </Stack>
  );
}

/**
 * The legacy `app.notebooks` definitions and their Runs — read-only, labelled,
 * and never promoted (AC12).
 *
 * WHY THIS EXISTS. `NotebooksPanel.tsx` was the only surface where these rows
 * were readable, and it is no longer mounted anywhere: the canonical screens took
 * the Analyze sections. AC12's "legacy Notebook definitions and Runs remain
 * readable with their actual evidence and limitations" quietly became false. This
 * panel makes it true again without pretending the rows are canonical.
 *
 * Every row states what it LACKS, per row, because a reader looking at one Run
 * needs to know what that Run is missing — not a footnote about the family.
 */
function LegacyNotebooksPanel({ phase, retry }: { phase: Phase<LegacyNotebook[]>; retry: () => void }) {
  if (phase.status === "no-scope") return null;
  if (phase.status === "loading") return <Loading label="legacy Notebooks" />;
  if (phase.status === "error") {
    return <Failure what="The legacy Notebooks" message={phase.message} action={<Retry onClick={retry} />} />;
  }
  if (phase.status === "not-found") return null;
  if (phase.data.length === 0) return null;

  return (
    <Panel>
      <PanelHeader
        title="Legacy Notebooks"
        description="Preserved and read-only. One mutable definition per row, with no version history, and Runs that pin no Result and no Render. They are shown as they are; nothing is reconstructed or promoted."
      />
      <Stack>
        {phase.data.map((notebook) => (
          <Stack key={notebook.id}>
            <Cluster>
              <strong>{notebook.title}</strong>
              <Badge tone="warning">Legacy, not versioned</Badge>
              {notebook.is_shared ? <Badge tone="warning">Legacy share exists</Badge> : null}
            </Cluster>
            <Cluster>
              <Metric label="Report reference" value={notebook.report_ref} />
              <Metric label="Window rule" value={notebook.window_rule} />
              <Metric
                label="Legacy schedule"
                value={notebook.scheduled ? (notebook.schedule_rule ?? "on") : "off"}
              />
              <Metric label="Last edited" value={<Timestamp value={notebook.updated_at} />} />
            </Cluster>
            <Status as="block" tone="info" title="What this definition cannot say">
              {notebook.limitation}
            </Status>
            {notebook.runs.length === 0 ? (
              <EmptyState title="This legacy Notebook was never run" />
            ) : (
              <TableScroll label={`Legacy runs of ${notebook.title}`}>
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead>Run</TableHead>
                      <TableHead>Executed</TableHead>
                      <TableHead>Status</TableHead>
                      <TableHead>Evidence</TableHead>
                      <TableHead>Run ids</TableHead>
                      <TableHead>Limitation</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {notebook.runs.map((run) => (
                      <TableRow key={run.id}>
                        <TableCell>
                          <ObjectId value={run.id} title="Run" />
                        </TableCell>
                        <TableCell><Timestamp value={run.executed_at} /></TableCell>
                        <TableCell>
                          <Badge tone={outcomeTone(run.status)}>{stateLabel(run.status)}</Badge>
                        </TableCell>
                        <TableCell>
                          <Badge tone={run.evidence === "inline_envelope" ? "info" : "warning"}>
                            {LEGACY_EVIDENCE_LABEL[run.evidence]}
                          </Badge>
                        </TableCell>
                        <TableCell>{run.pull_id_count}</TableCell>
                        <TableCell>{run.limitation}</TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              </TableScroll>
            )}
          </Stack>
        ))}
      </Stack>
    </Panel>
  );
}

/** The three evidence states a legacy Run can be in, named for a reader. */
const LEGACY_EVIDENCE_LABEL: Record<LegacyNotebook["runs"][number]["evidence"], string> = {
  inline_envelope: "Whole-notebook envelope",
  deferred: "Deferred — never written",
  absent: "No evidence at all",
};

export function NotebookWorkbench({
  projectId,
  notebookId,
  tab,
  onNavigateTab,
  tabHref,
  collectionHref,
  onOpenResult,
}: {
  projectId?: string;
  notebookId: string;
  tab: string;
  onNavigateTab?: (tab: string) => void;
  /** The canonical address of each tab, supplied by the shell router.
   *  A workbench tab is route navigation, not local state: without an
   *  address the tab renders disabled rather than pretending to be a link. */
  tabHref?: (tab: string) => string;
  /** The way back when this address names no Notebook (76-4). REQUIRED for the
   *  reason `ObjectNotFound` gives: this state draws no rail and no tabs. */
  collectionHref: string;
  onOpenResult?: (resultId: string) => void;
}) {
  const [phase, setPhase] = useState<Phase<NotebookDetail>>({ status: "loading" });
  const [reloadToken, setReloadToken] = useState(0);
  const [selectedVersionId, setSelectedVersionId] = useState<string | null>(null);
  const [openRun, setOpenRun] = useState<NotebookRun | null>(null);
  const [running, setRunning] = useState(false);
  const [failure, setFailure] = useState<{ message: string; refusals: Refusal[] } | null>(
    null,
  );
  const [editing, setEditing] = useState(false);
  const [draftBlocks, setDraftBlocks] = useState<DraftBlock[]>([]);
  const [saving, setSaving] = useState(false);
  const [reports, setReports] = useState<ReportSummary[]>([]);
  const [reportsLoading, setReportsLoading] = useState(false);
  const [reportsError, setReportsError] = useState<string | null>(null);

  useEffect(() => {
    if (!projectId) {
      setPhase({ status: "no-scope" });
      return;
    }
    const controller = new AbortController();
    setPhase({ status: "loading" });
    fetchNotebook(projectId, notebookId, { signal: controller.signal })
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
  }, [projectId, notebookId, reloadToken]);

  // Report versions are read only when the editor is opened. A tab that is
  // being READ asks nothing of the Reports route: a request nobody's gesture
  // caused is a request whose failure nobody can act on.
  useEffect(() => {
    if (!editing || !projectId) return;
    const controller = new AbortController();
    setReportsLoading(true);
    setReportsError(null);
    listReports(projectId, { signal: controller.signal })
      .then((body) => setReports(body.reports.filter((report) => report.current_version_id)))
      .catch((error: unknown) => {
        if (!controller.signal.aborted) setReportsError((error as Error).message);
      })
      .finally(() => {
        if (!controller.signal.aborted) setReportsLoading(false);
      });
    return () => controller.abort();
  }, [editing, projectId]);

  const saveVersion = useCallback(async () => {
    if (!projectId || draftBlocks.length === 0) return;
    setSaving(true);
    setFailure(null);
    try {
      await createNotebookVersion(projectId, notebookId, { blocks: blockPayload(draftBlocks) });
      setEditing(false);
      setDraftBlocks([]);
      setReloadToken((token) => token + 1);
    } catch (error) {
      setFailure({ message: (error as Error).message, refusals: refusalsOf(error) });
    } finally {
      setSaving(false);
    }
  }, [projectId, notebookId, draftBlocks]);

  const start = useCallback(async () => {
    if (!projectId) return;
    setRunning(true);
    setFailure(null);
    try {
      // One key per user action. A double click therefore completes the run that
      // was already accepted instead of starting a second one over the same
      // evidence -- the server returns the original Run for a repeated key.
      const key = `manual-${Date.now()}`;
      await runNotebook(projectId, notebookId, key);
      setReloadToken((token) => token + 1);
    } catch (error) {
      setFailure({ message: (error as Error).message, refusals: refusalsOf(error) });
    } finally {
      setRunning(false);
    }
  }, [projectId, notebookId]);

  const inspect = useCallback(
    async (runId: string) => {
      if (!projectId) return;
      setOpenRun(null);
      try {
        setOpenRun(await fetchNotebookRun(projectId, runId));
      } catch (error) {
        setFailure({ message: (error as Error).message, refusals: refusalsOf(error) });
      }
    },
    [projectId],
  );

  if (phase.status === "no-scope") return <NoScope what="this Notebook" />;
  if (phase.status === "loading") return <Loading label="this Notebook" />;
  if (phase.status === "not-found") {
    return <ObjectNotFound what="Notebook" collection="Notebooks" collectionHref={collectionHref} />;
  }
  if (phase.status === "error") {
    return <Failure what="This Notebook" message={phase.message} action={<Retry onClick={() => setReloadToken((token) => token + 1)} />} />;
  }

  const notebook = phase.data;
  const current =
    notebook.versions.find((version) => version.id === selectedVersionId) ??
    notebook.versions.find((version) => version.id === notebook.current_version_id) ??
    notebook.versions[0];
  const known = NOTEBOOK_TABS.some((entry) => entry.key === tab);
  /** The head, and only the head, is editable. An immutable version opened from
   *  the Versions tab offers no composer at all — offering one and refusing the
   *  save afterwards would be the same promise broken later. */
  const viewingCurrent = Boolean(current) && current.id === notebook.current_version_id;

  return (
    <Stack>
      <ObjectHeader name={notebook.label} source="Notebook" />
      <NavTabs
        label="Notebook"
        tabs={NOTEBOOK_TABS.map((entry) => ({ ...entry, href: tabHref?.(entry.key) }))}
        current={tab}
        onNavigate={onNavigateTab}
      />
      {selectedVersionId && selectedVersionId !== notebook.current_version_id ? (
        <Status as="block" tone="info" title={`Viewing immutable version v${current.version_number}`}>
          This historical composition is read-only. <Button size="xs" variant="ghost" onClick={() => setSelectedVersionId(null)}>Return to current</Button>
        </Status>
      ) : null}

      {!known ? (
        <EmptyState
          title="Unknown tab"
          description="This Notebook does not have that tab. No other tab is opened in its place."
        />
      ) : null}

      {failure ? (
        <Status as="block" tone="error" title="The request was refused">
          <Stack>
            <p>{failure.message}</p>
            <RefusalList refusals={failure.refusals} />
          </Stack>
        </Status>
      ) : null}

      {known && tab === "content" ? (
        <Panel>
          <PanelHeader
            title="Content"
            description={
              current
                ? `Composition v${current.version_number} — ordered blocks, each pinned to an exact version.`
                : "This Notebook has no composition version."
            }
          />
          {current && current.blocks.length > 0 ? (
            <TableScroll label="Composition blocks">
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>#</TableHead>
                    <TableHead>Block</TableHead>
                    <TableHead>Type</TableHead>
                    <TableHead>Pinned intent</TableHead>
                    <TableHead>Presentation</TableHead>
                    <TableHead>As-of rule</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {current.blocks.map((block) => (
                    <TableRow key={block.block_key}>
                      <TableCell>{block.position}</TableCell>
                      <TableCell>
                        <code>{block.block_key}</code>
                      </TableCell>
                      <TableCell>{blockTypeLabel(block.block_type)}</TableCell>
                      <TableCell>
                        <code>
                          {block.query_spec_version_id ?? block.report_version_id ?? "—"}
                        </code>
                      </TableCell>
                      <TableCell>
                        {block.presentation.absent_literal ? (
                          <Badge tone="info">{block.presentation.absent_literal}</Badge>
                        ) : (
                          <Badge tone="success">{presentationKindLabel(block.presentation.kind)}</Badge>
                        )}
                      </TableCell>
                      <TableCell>{block.as_of_rule}</TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </TableScroll>
          ) : (
            <EmptyState title="This composition version has no block" />
          )}
          <Cluster>
            <Button onClick={start} disabled={running || !current}>
              {running ? "Running…" : "Run this Notebook"}
            </Button>
            {/* Composing lives HERE now, not only on the collection screen.
                `analyze-and-test.md` gives this tab the content, and the
                version-append route it writes to
                (`analyze_artifacts_api.py:592-596`) was mounted server-side
                with no caller in the console — so the tab described a save
                nothing on it could perform. A historical version stays
                read-only: the control is drawn only on the head. */}
            {viewingCurrent ? (
              <Button
                variant="secondary"
                onClick={() => {
                  setEditing((open) => {
                    if (!open) setDraftBlocks(seedBlocks(current?.blocks ?? []));
                    return !open;
                  });
                }}
              >
                {editing ? "Close editor" : "Edit content"}
              </Button>
            ) : null}
          </Cluster>
        </Panel>
      ) : null}

      {known && tab === "content" && editing && viewingCurrent ? (
        <Panel>
          <PanelHeader
            title="Edit content"
            description="Seeded from the current composition. Saving appends an immutable version and advances the head; no earlier composition and no earlier Run changes."
          />
          <Stack>
            <BlockComposer
              blocks={draftBlocks}
              onBlocks={setDraftBlocks}
              reports={reports}
              reportsLoading={reportsLoading}
              reportsError={reportsError}
              onRetryReports={() => setReloadToken((token) => token + 1)}
              rekey={false}
              instance="notebook-content"
            />
            <Cluster className="justify-end">
              <Button disabled={saving || draftBlocks.length === 0} onClick={() => void saveVersion()}>
                {saving ? "Saving…" : "Save as a new version"}
              </Button>
            </Cluster>
            {draftBlocks.length === 0 ? (
              <Status as="block" tone="info" title="A composition needs at least one block">
                The server refuses an empty composition. Add a Report version above, or close the
                editor to leave v{current?.version_number} as it is.
              </Status>
            ) : null}
          </Stack>
        </Panel>
      ) : null}

      {known && tab === "runs" ? (
        <Stack>
          <Panel>
            <PanelHeader
              title="Runs"
              description="Immutable. Each Run names the exact composition version it ran; editing the Notebook never rewrites one."
            />
            {notebook.runs.length === 0 ? (
              <EmptyState title="This Notebook has not been run yet" />
            ) : (
              <TableScroll label="Notebook runs">
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead>Run</TableHead>
                      <TableHead>Composition version</TableHead>
                      <TableHead>Dispatch</TableHead>
                      <TableHead>Outcome</TableHead>
                      <TableHead>Accepted</TableHead>
                      <TableHead>Blocks</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {notebook.runs.map((entry) => (
                      <TableRow key={entry.id}>
                        <TableCell>
                          <ObjectId value={entry.id} title="Entry" />
                        </TableCell>
                        <TableCell>
                          <ObjectId value={entry.notebook_version_id} title="Notebook version" />
                        </TableCell>
                        <TableCell>{entry.dispatch_source}</TableCell>
                        <TableCell>
                          <Badge tone={outcomeTone(entry.outcome)}>
                            {entry.outcome ?? entry.state}
                          </Badge>
                        </TableCell>
                        <TableCell><Timestamp value={entry.accepted_at} /></TableCell>
                        <TableCell>
                          <Button variant="ghost" onClick={() => inspect(entry.id)}>
                            Inspect
                          </Button>
                        </TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              </TableScroll>
            )}
          </Panel>

          {openRun ? (
            <Panel>
              <PanelHeader
                title={`Run ${openRun.id}`}
                description="Per-block evidence. A block that failed remains inspectable, and one that is intentionally not rendered says so in exact words."
              />
              <TableScroll label="Run block outcomes">
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead>#</TableHead>
                      <TableHead>Block</TableHead>
                      <TableHead>Result</TableHead>
                      <TableHead>Render</TableHead>
                      <TableHead>Status</TableHead>
                      <TableHead>Limitation</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {openRun.blocks.map((block) => (
                      <TableRow key={block.block_key}>
                        <TableCell>{block.position}</TableCell>
                        <TableCell>
                          <code>{block.block_key}</code>
                        </TableCell>
                        <TableCell>
                          {block.result_id ? (
                            onOpenResult ? (
                              <Button
                                variant="ghost"
                                onClick={() => onOpenResult(block.result_id as string)}
                              >
                                <ObjectId value={block.result_id} title="Result" />
                              </Button>
                            ) : (
                              <ObjectId value={block.result_id} title="Result" />
                            )
                          ) : (
                            "—"
                          )}
                        </TableCell>
                        <TableCell>
                          {block.render_id ? (
                            <ObjectId value={block.render_id} title="Render" />
                          ) : (
                            <Badge tone="info">{block.render_absent_literal}</Badge>
                          )}
                        </TableCell>
                        <TableCell>
                          <Badge tone={outcomeTone(block.status)}>{stateLabel(block.status)}</Badge>
                        </TableCell>
                        <TableCell>{block.limitation ?? "—"}</TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              </TableScroll>
            </Panel>
          ) : null}
        </Stack>
      ) : null}

      {known && tab === "versions" ? (
        <Panel>
          <PanelHeader
            title="Versions"
            description="Immutable and ordered. Saving content appends a version; the head advances, no prior composition changes."
          />
          <TableScroll label="Notebook versions">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Version</TableHead>
                  <TableHead>Blocks</TableHead>
                  <TableHead>Content hash</TableHead>
                  <TableHead>Predecessor</TableHead>
                  <TableHead>Created</TableHead>
                  <TableHead>Open</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {notebook.versions.map((version) => (
                  <TableRow key={version.id}>
                    <TableCell>
                      v{version.version_number}
                      {version.id === notebook.current_version_id ? (
                        <Badge tone="success">Current</Badge>
                      ) : null}
                    </TableCell>
                    <TableCell>{version.blocks.length}</TableCell>
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

      {known && tab === "content" && notebook.schedule ? (
        <Panel>
          <PanelHeader
            title="Schedule"
            description="Operational configuration, not composition content. Editing it creates no version and rewrites no Run."
          />
          <Cluster>
            <Metric label="Recurrence" value={notebook.schedule.recurrence} />
            <Metric label="Enabled" value={notebook.schedule.enabled ? "Yes" : "No"} />
            <Metric label="Next due" value={<Timestamp value={notebook.schedule.next_due_at} absentMeaning="Not scheduled" />} />
            <Metric
              label="Last dispatched"
              value={<Timestamp value={notebook.schedule.last_dispatched_at} absentMeaning="Never dispatched" />}
            />
          </Cluster>
          {/*
            An enabled schedule that has never dispatched, and a dispatch that
            failed, are two different states and neither may look like success.
            The first version of this panel showed "Enabled: Yes / Next due: —"
            while nothing in the repository read the schedule at all.
          */}
          {notebook.schedule.last_run_id ? (
            <Status as="block" tone="success" title="Dispatch is connected">
              The last scheduled Run was <ObjectId value={notebook.schedule.last_run_id} title="Run" />, dispatched
              by <code>{notebook.schedule.dispatcher}</code>. It is an ordinary immutable Run:
              open it under Runs.
            </Status>
          ) : notebook.schedule.last_dispatch_note ? (
            <Status as="block" tone="error" title="The last dispatch was refused">
              <Stack>
                <p>{notebook.schedule.last_dispatch_note}</p>
                <p>
                  The schedule stays enabled and the next attempt is due above. No Run was
                  created, and no previous Run was changed.
                </p>
              </Stack>
            </Status>
          ) : notebook.schedule.enabled ? (
            <Status as="block" tone="info" title="No scheduled Run yet">
              This schedule is enabled and due at the time above. Dispatch runs from{" "}
              <code>{notebook.schedule.dispatcher}</code>; nothing has fired yet, so there is no
              scheduled Run to show.
            </Status>
          ) : (
            <Status as="block" tone="neutral" title="This schedule is off">
              Nothing is dispatched while a schedule is disabled. Manual runs are unaffected.
            </Status>
          )}
        </Panel>
      ) : null}
    </Stack>
  );
}

export default NotebooksCollection;
