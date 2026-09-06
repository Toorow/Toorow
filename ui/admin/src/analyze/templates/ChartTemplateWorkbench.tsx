/**
 * Level 3 — the Chart Template workbench, four tabs.
 *
 * The tabs and what each one shows are `visualization-and-rendering.md`'s own
 * table (amendment 2026-08-31, *The Chart Template workbench*), in its order:
 * Overview, Presentation, Compatibility, Versions.
 *
 * WHAT A PERSON COMES HERE TO DO IS DONE HERE. The same document says it: "from
 * the workbench the template is applied to a Result and yields a Visualization
 * Spec version of the Project, without leaving the screen." So the Compatibility
 * tab carries the act, not a link to somewhere that carries it — a screen that
 * sends a person elsewhere for the gesture it names is unfinished.
 *
 * ONE VOCABULARY, AND IT COMES FROM THE SERVER. The Presentation tab renders the
 * wells and roles of `GET /chart-template-vocabulary`, which is
 * `visualization_templates.template_vocabulary()` — the same wells the Builder
 * binds and the same roles the verdict judges. This file holds no list of wells,
 * no list of roles and no list of families: "a Chart Template declares a well, a
 * role, a visual family or a responsive profile instead of importing it from the
 * authority that defines it" is a ratified failure condition.
 *
 * IT DRAWS NOTHING ITSELF. Every pixel of a template comes from
 * `TemplatePreview`, which mounts the shared runtime and nothing else.
 *
 * Composed only from `ui/admin/src/ui/index.ts`.
 */
import { useCallback, useEffect, useState } from "react";

import { ApiError } from "../../lib/apiFetch";
import {
  ObjectId,
  Badge,
  Button,
  Cluster,
  ConfirmDialog,
  EmptyState,
  Failure,
  ObjectNotFound,
  Retry,
  Field,
  Loading,
  Metric,
  NativeSelect,
  NavTabs,
  NoScope,
  ObjectHeader,
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
} from "../../ui";
import {
  applyChartTemplate,
  archiveChartTemplate,
  fetchChartTemplate,
  fetchTemplateVocabulary,
  restoreChartTemplate,
  type ChartTemplateDetail,
  type CompatibilityVerdict,
  type MaterializedSpec,
  type TemplateVocabulary,
} from "./chartTemplateClient";
import { TemplatePreview } from "./TemplatePreview";
import TemplateDocumentEditor from "./TemplateDocumentEditor";

/** The ratified table, in its order. Declared, never inferred. */
export const CHART_TEMPLATE_TABS = [
  { key: "overview", label: "Overview" },
  { key: "presentation", label: "Presentation" },
  { key: "compatibility", label: "Compatibility" },
  { key: "versions", label: "Versions" },
] as const;

export const CHART_TEMPLATE_DEFAULT_TAB = "overview";

/**
 * What archiving this template will cost, counted from what the server said uses it.
 *
 * ASKED BEFORE THE GESTURE, NOT REPORTED AFTER IT. `load_chart_template` reads
 * `used_by` — the Report versions that pin a version of this head, and the
 * Visualizations materialised from one — and both are already on this screen's
 * Overview. A confirmation that did not carry them would be asking a person to
 * decide with less than the screen behind it already knows.
 *
 * IT NAMES WHAT SURVIVES, because that is the half that decides the click: an
 * archived template keeps every version, and every pin already made goes on
 * resolving. Nothing here is a deletion, and the sentence must not let it read
 * as one.
 */
export function archiveConsequence(template: ChartTemplateDetail): string {
  const reports = template.used_by.reports.length;
  const visualizations = template.used_by.visualizations.length;
  const consumers: string[] = [];
  if (reports > 0) consumers.push(`${reports} Report version${reports === 1 ? "" : "s"}`);
  if (visualizations > 0) {
    consumers.push(`${visualizations} Visualization${visualizations === 1 ? "" : "s"}`);
  }
  if (consumers.length === 0) {
    return (
      "Nothing uses this template yet. It leaves the list and accepts no new version; " +
      "its versions are kept and it can be restored here."
    );
  }
  return (
    `${consumers.join(" and ")} already use this template, and they keep working: ` +
    "an archived template keeps every version, and a pin already made goes on resolving. " +
    "What stops is new use — it leaves the list, accepts no new version, and is no longer " +
    "offered when a Report pins a presentation. It can be restored here."
  );
}

type Phase =
  | { status: "loading" }
  | { status: "ready"; data: ChartTemplateDetail }
  | { status: "no-scope" }
  | { status: "not-found" }
  | { status: "error"; message: string };

function VerdictPanel({ verdict }: { verdict: CompatibilityVerdict }) {
  if (verdict.state === "compatible") {
    return (
      <Status as="block" tone="success" title="This Result satisfies every predicate">
        Applying the template below produces a Visualization of this Project, pinned to the
        question this Result already answered.
      </Status>
    );
  }
  const reasons = verdict.state === "unavailable"
    ? (verdict.unreadable ? [verdict.unreadable] : [])
    : (verdict.unmet ?? []);
  return (
    <Status
      as="block"
      tone={verdict.state === "unavailable" ? "neutral" : "warning"}
      title={
        verdict.state === "unavailable"
          ? "This pair could not be judged"
          : "This Result does not satisfy this template"
      }
      data-testid="chart-template-verdict"
    >
      <Stack>
        {reasons.map((reason, index) => (
          <p className="m-0" key={`${reason.code}:${index}`}>
            {reason.message}
            {reason.remedy ? <span className="text-text-secondary"> {reason.remedy}</span> : null}
          </p>
        ))}
      </Stack>
    </Status>
  );
}

export default function ChartTemplateWorkbench({
  projectId,
  templateId,
  tab,
  onNavigateTab,
  tabHref,
  collectionHref,
  onOpenVisualization,
  onOpenReport,
}: {
  projectId?: string;
  templateId: string;
  tab: string;
  onNavigateTab?: (tab: string) => void;
  tabHref?: (tab: string) => string;
  /** The way back when this address names no Chart Template (76-4). REQUIRED
   *  for the reason `ObjectNotFound` gives: this state draws no rail, no tabs. */
  collectionHref: string;
  onOpenVisualization?: (visualizationId: string) => void;
  onOpenReport?: (reportId: string) => void;
}) {
  const [phase, setPhase] = useState<Phase>({ status: "loading" });
  const [vocabulary, setVocabulary] = useState<TemplateVocabulary | null>(null);
  const [againstResult, setAgainstResult] = useState("");
  const [verdict, setVerdict] = useState<CompatibilityVerdict | null>(null);
  const [applying, setApplying] = useState(false);
  const [applied, setApplied] = useState<MaterializedSpec | null>(null);
  const [applyFailure, setApplyFailure] = useState<{ message: string; reasons: string[] } | null>(
    null,
  );
  //: Bumped when the Presentation tab appends a version. The head, the current
  //: version, the requirement sentence and the Versions tab all move at once,
  //: and they move because the SERVER was re-read -- never because this file
  //: patched a copy of what it thinks the write did.
  const [reloadToken, setReloadToken] = useState(0);
  const reload = useCallback(() => setReloadToken((token) => token + 1), []);
  //: Open only while the archive confirmation is showing. Restoring is NOT
  //: confirmed: it takes nothing away, and a dialog in front of a gesture that
  //: costs nothing is a question with one answer.
  const [confirmingArchive, setConfirmingArchive] = useState(false);
  const [archiveBusy, setArchiveBusy] = useState(false);
  const [archiveError, setArchiveError] = useState<string | null>(null);

  useEffect(() => {
    if (!projectId) {
      setPhase({ status: "no-scope" });
      return;
    }
    const controller = new AbortController();
    setPhase({ status: "loading" });
    fetchChartTemplate(projectId, templateId, { signal: controller.signal }, againstResult || null)
      .then((data) => {
        setPhase({ status: "ready", data });
        setVerdict(data.verdict);
      })
      .catch((error: unknown) => {
        if (controller.signal.aborted) return;
        if (error instanceof ApiError && error.status === 404) {
          setPhase({ status: "not-found" });
          return;
        }
        setPhase({ status: "error", message: (error as Error).message });
      });
    return () => controller.abort();
  }, [projectId, templateId, againstResult, reloadToken]);

  useEffect(() => {
    if (!projectId) return;
    const controller = new AbortController();
    fetchTemplateVocabulary(projectId, { signal: controller.signal })
      .then(setVocabulary)
      .catch(() => {
        // The vocabulary is the Presentation tab's whole content. Leaving it
        // null renders the honest absence below rather than a well list this
        // file would have had to invent.
        if (!controller.signal.aborted) setVocabulary(null);
      });
    return () => controller.abort();
  }, [projectId]);

  const apply = useCallback(async () => {
    if (!projectId || phase.status !== "ready") return;
    const versionId = phase.data.current_version_id;
    if (!versionId || !againstResult) return;
    setApplying(true);
    setApplyFailure(null);
    setApplied(null);
    try {
      const created = await applyChartTemplate(projectId, versionId, { result_id: againstResult });
      setApplied(created);
    } catch (error) {
      const body = error instanceof ApiError ? (error.body as Record<string, unknown> | null) : null;
      const refusals = (body?.refusals as { message: string; remedy?: string }[] | undefined) ?? [];
      setApplyFailure({
        message: (error as Error).message,
        reasons: refusals.map((r) => [r.message, r.remedy].filter(Boolean).join(" ")),
      });
    } finally {
      setApplying(false);
    }
  }, [againstResult, phase, projectId]);

  //: ONE handler for both directions, because they are one state with two
  //: values. The screen never patches its own copy of `archived`: the server is
  //: re-read, so a write that failed cannot come to look like one that worked.
  const setArchived = useCallback(
    async (archiving: boolean) => {
      if (!projectId) return;
      setArchiveBusy(true);
      setArchiveError(null);
      try {
        const call = archiving ? archiveChartTemplate : restoreChartTemplate;
        await call(projectId, templateId);
        setConfirmingArchive(false);
        reload();
      } catch (error) {
        // The dialog stays open and says what happened. Closing it on failure
        // would leave the template listed with no explanation, which reads as a
        // control that does nothing.
        setArchiveError((error as Error).message);
      } finally {
        setArchiveBusy(false);
      }
    },
    [projectId, reload, templateId],
  );

  if (phase.status === "no-scope") return <NoScope what="this Chart Template" />;
  if (phase.status === "loading") return <Loading label="this Chart Template" />;
  if (phase.status === "not-found") {
    return (
      <ObjectNotFound
        what="Chart Template"
        collection="Reports"
        collectionHref={collectionHref}
        detail="This template does not exist in this Project, or you cannot see it."
      />
    );
  }
  if (phase.status === "error") {
    return <Failure what="This Chart Template" message={phase.message} action={<Retry onClick={reload} />} />;
  }

  const template = phase.data;
  const known = CHART_TEMPLATE_TABS.some((entry) => entry.key === tab);
  const currentVersion =
    template.versions.find((version) => version.id === template.current_version_id) ??
    template.versions[0];

  return (
    <Stack>
      <ObjectHeader
        name={template.answers_question || template.label}
        source={`Chart Template · ${template.origin_label}`}
      />
      <NavTabs
        label="Chart Template"
        tabs={CHART_TEMPLATE_TABS.map((entry) => ({ ...entry, href: tabHref?.(entry.key) }))}
        current={tab}
        onNavigate={onNavigateTab}
      />

      {!known ? (
        <EmptyState
          title="Unknown tab"
          description="This Chart Template does not have that tab. No other tab is opened in its place."
        />
      ) : null}

      {/* THE STATE, WHEREVER THE READER LANDED. It is stated above the tabs and
          not inside Overview, because the tab it changes most is Presentation:
          an archived head accepts no new version, and a person who opened that
          tab directly would otherwise compose a document the server refuses.
          The way out is on the same line as the fact. */}
      {template.archived ? (
        <Status
          as="block"
          tone="info"
          title="This Chart Template is archived"
          data-testid="chart-template-archived-banner"
        >
          <Stack>
            <p className="m-0">
              It is not offered when a Report pins a presentation and it accepts no new version.
              Nothing was deleted: every version below is still readable, and every pin already
              made still resolves.
            </p>
            <Cluster>
              <Button
                size="xs"
                onClick={() => void setArchived(false)}
                disabled={archiveBusy}
                data-testid="chart-template-restore"
              >
                {archiveBusy ? "Restoring…" : "Restore it"}
              </Button>
            </Cluster>
            {archiveError ? (
              <Status as="inline" tone="error" title="It was not restored">
                {archiveError}
              </Status>
            ) : null}
          </Stack>
        </Status>
      ) : null}

      {template.readable ? null : (
        <Status as="block" tone="neutral" title="This template cannot be read here">
          {template.unreadable_reason?.message}{" "}
          <span className="text-text-secondary">{template.unreadable_reason?.remedy}</span>
        </Status>
      )}

      {known && tab === "overview" ? (
        <Stack>
          <Panel>
            <PanelHeader
              title="Overview"
              description="What this template answers, what it asks a Result for, and where it came from."
            />
            <Cluster>
              <Metric label="Visual family" value={template.family_label ?? "—"} />
              <Metric label="Provenance" value={template.origin_label} />
              <Metric
                label="Current version"
                value={currentVersion ? `v${currentVersion.version_number}` : "—"}
              />
              <Metric label="Versions" value={template.version_count} />
              <Metric label="Last updated" value={<Timestamp value={template.updated_at} />} />
            </Cluster>
            <p className="m-0 text-ui text-text-secondary">
              Asks for: {template.requires_sentence ?? "—"}
            </p>
          </Panel>
          <Panel>
            <PanelHeader
              title="What uses it"
              description="A pin from a Report, and a Visualization materialised from it. Two different facts, kept apart."
            />
            {template.used_by.reports.length === 0 && template.used_by.visualizations.length === 0 ? (
              <EmptyState
                title="Nothing uses this template yet"
                description="Open the Compatibility tab, choose a Result and apply it — that produces the first Visualization of this Project from this template."
              />
            ) : (
              <Stack>
                {template.used_by.reports.map((entry) => (
                  <Cluster key={entry.report_version_id}>
                    <Button
                      variant="ghost"
                      size="xs"
                      onClick={() => onOpenReport?.(entry.report_id)}
                      disabled={!onOpenReport}
                    >
                      {entry.label}
                    </Button>
                    <Badge tone="neutral">Pinned by a Report version</Badge>
                  </Cluster>
                ))}
                {template.used_by.visualizations.map((entry) => (
                  <Cluster key={entry.visualization_spec_version_id}>
                    <Button
                      variant="ghost"
                      size="xs"
                      onClick={() => onOpenVisualization?.(entry.visualization_id)}
                      disabled={!onOpenVisualization}
                    >
                      Visualization materialised from this template
                    </Button>
                    <Badge tone="neutral">Provenance</Badge>
                  </Cluster>
                ))}
              </Stack>
            )}
          </Panel>

          {/* THE GESTURE THIS SCREEN OWED. `append_chart_template_version` has
              refused an archived head since story 72.5, with the remedy "List
              archived templates and restore this one first" — two gestures no
              route carried and no control offered. A screen that names a gesture
              and sends the person elsewhere to perform it is unfinished
              (`CLAUDE.md`, *Ce que la personne vient faire doit pouvoir se faire
              ici*).
              IT SITS UNDER "What uses it" ON PURPOSE: the consequence of
              archiving is the list directly above, and the confirmation counts
              it rather than asking a person to remember it. */}
          <Panel>
            <PanelHeader
              title={template.archived ? "Restore this template" : "Retire this template"}
              description="Archiving is a date on the head, never a deletion. The versions stay, the pins keep resolving, and the template stops being offered for new use."
            />
            <Cluster>
              {template.archived ? (
                <Button
                  onClick={() => void setArchived(false)}
                  disabled={archiveBusy}
                  data-testid="chart-template-restore-overview"
                >
                  {archiveBusy ? "Restoring…" : "Restore it"}
                </Button>
              ) : (
                <Button
                  variant="ghost"
                  onClick={() => {
                    setArchiveError(null);
                    setConfirmingArchive(true);
                  }}
                  disabled={archiveBusy}
                  data-testid="chart-template-archive"
                >
                  Archive it
                </Button>
              )}
            </Cluster>
            <p className="m-0 text-ui text-text-secondary">{archiveConsequence(template)}</p>
          </Panel>
        </Stack>
      ) : null}

      {known && tab === "presentation" ? (
        <Stack>
          <Panel>
            <PanelHeader
              title="Presentation"
              description="The template document, in the typed wells and role vocabulary the Builder uses. A template names a well and a role; it never names a member — that is what makes it a starting point. Saving here appends a successor version; nothing already saved is rewritten."
            />
            {vocabulary === null ? (
              <Status as="block" tone="neutral" title="The document vocabulary could not be read">
                The wells and roles a template may speak are served by the same registry the Builder
                binds against. Until it answers, this tab states nothing rather than showing a list
                it composed itself, and nothing can be saved from a vocabulary nobody read.
              </Status>
            ) : (
              <Stack>
                <Cluster>
                  <Metric label="Document contract" value={vocabulary.contract_version} />
                  <Metric label="Schema version" value={vocabulary.schema_version} />
                </Cluster>
                <p className="m-0 text-ui text-text-secondary">
                  A template subtracts from a Visualization Spec what binds it to data:{" "}
                  {Object.keys(vocabulary.subtracted_from_visualization_spec).join(", ")}.
                </p>
              </Stack>
            )}
          </Panel>
          {/* AN ARCHIVED HEAD ACCEPTS NO NEW VERSION, so the editor is not
              offered — rather than offered and refused after a person has
              composed a document. The refusal `append_chart_template_version`
              raises names the same gesture this states, and the control that
              performs it is the banner above. */}
          {template.archived ? (
            <Status
              as="block"
              tone="neutral"
              title="An archived template accepts no new version"
              data-testid="chart-template-presentation-archived"
            >
              Its versions stay exactly as they are and stay readable in the Versions tab.
              Restore this template to edit it again.
            </Status>
          ) : vocabulary !== null && projectId ? (
            <TemplateDocumentEditor
              projectId={projectId}
              template={template}
              vocabulary={vocabulary}
              onVersionAppended={reload}
            />
          ) : null}
        </Stack>
      ) : null}

      {known && tab === "compatibility" ? (
        <Stack>
          <Panel>
            <PanelHeader
              title="Compatibility"
              description="Choose a Result of this Project. The answer is compatible, or it names the predicate that is missing — never a bare yes or no."
            />
            {template.results.length === 0 ? (
              <EmptyState
                title="No Result to judge this template against"
                description="Ask a question in Explore and run it. A template is judged against one exact Result, so until one exists there is nothing to compare."
              />
            ) : (
              <Cluster>
                <Field label="Result">
                  {(field) => (
                    <NativeSelect
                      {...field}
                      value={againstResult}
                      onChange={(event) => {
                        setAgainstResult(event.target.value);
                        setApplied(null);
                        setApplyFailure(null);
                      }}
                      data-testid="chart-template-compatibility-result"
                    >
                      <option value="">Choose a Result</option>
                      {template.results.map((result) => (
                        <option key={result.result_id} value={result.result_id}>
                          {result.question ?? "A question with no name"} · {result.row_count} rows
                        </option>
                      ))}
                    </NativeSelect>
                  )}
                </Field>
              </Cluster>
            )}
            {verdict ? <VerdictPanel verdict={verdict} /> : null}
          </Panel>

          <Panel>
            <PanelHeader
              title="Apply it here"
              description="Applying produces a Visualization Spec version of this Project — an ordinary one, through the same validator every hand-built presentation goes through. No question is re-executed and no Result is created."
            />
            {applyFailure ? (
              <Status as="block" tone="error" title="The template was not applied">
                <Stack>
                  <p className="m-0">{applyFailure.message}</p>
                  {applyFailure.reasons.map((reason, index) => (
                    <p className="m-0" key={index}>
                      {reason}
                    </p>
                  ))}
                </Stack>
              </Status>
            ) : null}
            <Cluster>
              <Button
                onClick={() => void apply()}
                disabled={
                  applying ||
                  !againstResult ||
                  !template.current_version_id ||
                  verdict?.state !== "compatible"
                }
                data-testid="chart-template-apply"
              >
                {applying ? "Applying…" : "Apply to this Result"}
              </Button>
              {applied && onOpenVisualization ? (
                <Button
                  variant="ghost"
                  onClick={() => onOpenVisualization(applied.visualization_id)}
                >
                  Open it in the Builder
                </Button>
              ) : null}
            </Cluster>
            {applied ? (
              <Stack>
                <Status as="block" tone="success" title="A Visualization of this Project was created">
                  Version {applied.version_number}, content hash {applied.content_hash.slice(0, 12)}
                  … — the same hash the same presentation composed by hand would carry.
                </Status>
                {projectId ? (
                  <TemplatePreview projectId={projectId} materialized={applied} />
                ) : null}
              </Stack>
            ) : null}
          </Panel>
        </Stack>
      ) : null}

      {known && tab === "versions" ? (
        <Panel>
          <PanelHeader
            title="Versions"
            description="Immutable. An edit appends a version naming its predecessor; nothing here is ever rewritten."
          />
          {template.versions.length === 0 ? (
            <EmptyState
              title="This template has no version"
              description="A template with no version cannot be read as a presentation. Save a version from the Presentation tab."
            />
          ) : (
            <TableScroll label="Chart Template versions">
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Version</TableHead>
                    <TableHead>Family</TableHead>
                    <TableHead>Content hash</TableHead>
                    <TableHead>Predecessor</TableHead>
                    <TableHead>Proposed by</TableHead>
                    <TableHead>Created</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {template.versions.map((version) => (
                    <TableRow key={version.id}>
                      <TableCell>
                        v{version.version_number}
                        {version.id === template.current_version_id ? (
                          <> <Badge tone="success">Current</Badge></>
                        ) : null}
                      </TableCell>
                      <TableCell>{version.family}</TableCell>
                      <TableCell>
                        <code>{version.content_hash.slice(0, 12)}…</code>
                      </TableCell>
                      <TableCell>
                        {version.predecessor_version_id ? (
                          <ObjectId value={version.predecessor_version_id} title="Predecessor version" />
                        ) : (
                          "None — this is the first"
                        )}
                      </TableCell>
                      <TableCell>
                        {version.proposed_by === "model" ? "A model" : "A person"}
                      </TableCell>
                      <TableCell>
                        <Timestamp value={version.created_at} />
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </TableScroll>
          )}
        </Panel>
      ) : null}

      <ConfirmDialog
        open={confirmingArchive}
        onOpenChange={(open) => {
          if (!open) {
            setConfirmingArchive(false);
            setArchiveError(null);
          }
        }}
        title="Archive this Chart Template?"
        description={archiveConsequence(template)}
        evidenceLabel="Chart Template"
        evidence={{
          "Chart Template": template.answers_question || template.label,
          "Report versions that pin it": template.used_by.reports.length,
          "Visualizations made from it": template.used_by.visualizations.length,
        }}
        confirmLabel="Archive it"
        cancelLabel="Keep it active"
        destructive
        busy={archiveBusy}
        error={archiveError ?? undefined}
        onConfirm={() => void setArchived(true)}
        data-testid="chart-template-archive-confirm"
      />
    </Stack>
  );
}
