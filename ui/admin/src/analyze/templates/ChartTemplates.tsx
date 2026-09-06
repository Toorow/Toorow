/**
 * Analyze > Reports > Chart Templates — the list of POSSIBLE ANSWERS.
 *
 * `visualization-and-rendering.md`, amendment 2026-08-31, *The list screen*: "A
 * list of templates is a list of possible answers, never a catalogue of chart
 * types. Each row states the question the template answers, its visual family,
 * what it requires in the words of the roles, its provenance and its version
 * count."
 *
 * THE THREE EMPTINESSES ARE THREE SCREENS, NOT ONE (AC18).
 *   1. no Chart Template in this Project — the gesture is to draw one from a
 *      seed, or to save a Visualization as a template;
 *   2. no seed available — the gesture names the connector that would bring one
 *      and how to connect it;
 *   3. no template compatible with the OPEN RESULT — and the incompatible ones
 *      do not disappear: each says which predicate is missing, in the server's
 *      own words.
 * None of them names a deployment state and none names a table.
 *
 * IT DRAWS NOTHING. There is no thumbnail, no miniature and no canvas in this
 * file: the single non-negotiable prohibition of this surface is a second
 * drawing path, and a list that previewed its rows would be one. The preview
 * lives in the workbench, behind `VisualizationMount`, and it is the only pixel
 * a template ever produces.
 *
 * Composed only from `ui/admin/src/ui/index.ts`.
 */
import { useCallback, useEffect, useState } from "react";

import { ApiError } from "../../lib/apiFetch";
import {
  Badge,
  Button,
  Cluster,
  EmptyState,
  Failure,
  Retry,
  Field,
  Loading,
  NativeSelect,
  NoScope,
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
} from "../../ui";
import {
  listChartTemplates,
  seedConnectorChartTemplates,
  type ChartTemplateCollection,
  type ChartTemplateRow,
} from "./chartTemplateClient";

type Phase =
  | { status: "loading" }
  | { status: "ready"; data: ChartTemplateCollection }
  | { status: "no-scope" }
  | { status: "error"; message: string };

/** The three sentences AC18 asks for, spelled once so no branch paraphrases one. */
export const NO_PROJECT_TEMPLATE = {
  title: "No Chart Template in this Project yet",
  description:
    "A Chart Template is a validated starting point: a question, a visual family and what it " +
    "asks a Result for. Draw one from an available seed below, or open a Result you are happy " +
    "with and save its presentation as a template.",
} as const;

export const NO_SEED_AVAILABLE = {
  title: "No connector in this Project brings a Chart Template",
  description:
    "A connector can ship starting points with it. None of the connectors this Project uses " +
    "declares one. Connect a source that does in Data > Sources, or build your own template " +
    "from a Result.",
} as const;

export const NO_COMPATIBLE_TEMPLATE = {
  title: "No Chart Template fits the Result you opened",
  description:
    "Every template below is still listed, with the predicate it asks for that this Result does " +
    "not carry. Ask the question again in Explore with what a template needs, or pick a " +
    "different Result.",
} as const;

function VerdictBadge({ row }: { row: ChartTemplateRow }) {
  const verdict = row.verdict;
  if (!verdict) return null;
  if (verdict.state === "compatible") return <Badge tone="success">Fits this Result</Badge>;
  if (verdict.state === "unavailable") return <Badge tone="neutral">Could not be judged</Badge>;
  return <Badge tone="warning">Does not fit</Badge>;
}

/** The server's own sentences. This component chooses none of the words. */
function VerdictReasons({ row }: { row: ChartTemplateRow }) {
  const verdict = row.verdict;
  if (!verdict || verdict.state === "compatible") return null;
  const reasons = verdict.state === "unavailable"
    ? (verdict.unreadable ? [verdict.unreadable] : [])
    : (verdict.unmet ?? []);
  if (reasons.length === 0) return null;
  return (
    <ul className="m-0 list-none space-y-1 p-0 text-ui text-text-secondary">
      {reasons.map((reason, index) => (
        <li key={`${reason.code}:${index}`}>
          {reason.message}
          {reason.remedy ? <span className="text-text-tertiary"> {reason.remedy}</span> : null}
        </li>
      ))}
    </ul>
  );
}

export default function ChartTemplates({
  projectId,
  onOpenTemplate,
  /** The Result the reader arrived with, when they arrived from one. */
  resultId = null,
}: {
  projectId?: string;
  onOpenTemplate?: (templateId: string) => void;
  resultId?: string | null;
}) {
  const [phase, setPhase] = useState<Phase>({ status: "loading" });
  const [reloadToken, setReloadToken] = useState(0);
  //: Which Result the list is narrowed to. It starts at whatever the caller
  //: carried, and the picker below is the only other way it moves — the address
  //: keeps its own copy so a reload does not lose the reader's choice.
  const [againstResult, setAgainstResult] = useState<string>(resultId ?? "");
  const [seeding, setSeeding] = useState<string | null>(null);
  const [seedError, setSeedError] = useState<string | null>(null);
  //: THE WAY BACK THE ARCHIVE PROMISES. The workbench confirmation says an
  //: archived template can be listed again and restored, the route has served
  //: `?include_archived=true` since story 72.5, and nothing in `ui/admin/src`
  //: ever sent it — so the sentence was true of the server and false of the
  //: product. Same defect, same repair as the Reports list.
  const [includeArchived, setIncludeArchived] = useState(false);

  useEffect(() => {
    if (!projectId) {
      setPhase({ status: "no-scope" });
      return;
    }
    const controller = new AbortController();
    setPhase({ status: "loading" });
    listChartTemplates(
      projectId,
      { signal: controller.signal },
      { resultId: againstResult || null, includeArchived },
    )
      .then((data) => setPhase({ status: "ready", data }))
      .catch((error: unknown) => {
        if (controller.signal.aborted) return;
        setPhase({
          status: "error",
          message: error instanceof ApiError ? error.message : (error as Error).message,
        });
      });
    return () => controller.abort();
  }, [projectId, againstResult, includeArchived, reloadToken]);

  const drawSeed = useCallback(
    async (moduleName: string) => {
      if (!projectId) return;
      setSeeding(moduleName);
      setSeedError(null);
      try {
        await seedConnectorChartTemplates(projectId, moduleName);
        setReloadToken((token) => token + 1);
      } catch (error) {
        setSeedError((error as Error).message);
      } finally {
        setSeeding(null);
      }
    },
    [projectId],
  );

  if (phase.status === "no-scope") return <NoScope what="Chart Templates" />;
  if (phase.status === "loading") return <Loading label="the Chart Templates of this Project" />;
  if (phase.status === "error") {
    return <Failure what="The Chart Templates" message={phase.message} action={<Retry onClick={() => setReloadToken((token) => token + 1)} />} />;
  }

  const { templates, available_seeds: seeds, results, compatible_count: compatible } = phase.data;
  const narrowed = Boolean(againstResult);
  const noneFits = narrowed && compatible === 0 && templates.length > 0;
  const archivedShown = templates.filter((row) => row.archived).length;

  return (
    <Stack>
      <PageHeader
        title="Chart Templates"
        description="Validated starting points: each one states the question it answers and what it asks a Result for. Applying one to a Result produces a Visualization of this Project."
      />

      <Panel>
        <PanelHeader
          title="Narrow to one Result"
          description="A template is only useful against a question that was actually answered. Choose a Result and every row below says whether it fits, or which predicate it is missing."
        />
        {results.length === 0 ? (
          <Status as="block" tone="info" title="No Result has been produced in this Project yet">
            Ask a question in Explore and run it. Until a Result exists there is nothing to judge a
            template against, so every row below states only what it asks for.
          </Status>
        ) : (
          <Cluster>
            <Field label="Result">
              {(field) => (
                <NativeSelect
                  {...field}
                  value={againstResult}
                  onChange={(event) => setAgainstResult(event.target.value)}
                  data-testid="chart-template-result-filter"
                >
                  <option value="">Judge against no Result</option>
                  {results.map((result) => (
                    <option key={result.result_id} value={result.result_id}>
                      {result.question ?? "A question with no name"} · {result.row_count} rows
                    </option>
                  ))}
                </NativeSelect>
              )}
            </Field>
          </Cluster>
        )}
      </Panel>

      {noneFits ? (
        <Status as="block" tone="warning" title={NO_COMPATIBLE_TEMPLATE.title}>
          {NO_COMPATIBLE_TEMPLATE.description}
        </Status>
      ) : null}

      <Panel>
        <PanelHeader
          title="Templates of this Project"
          description="Each row is a possible answer, not a chart type."
        />
        <Cluster>
          <Button
            variant="ghost"
            size="xs"
            onClick={() => setIncludeArchived((shown) => !shown)}
            data-testid="chart-templates-show-archived"
          >
            {includeArchived ? "Hide archived" : "Show archived"}
          </Button>
          {includeArchived ? (
            //: The COUNT, not a claim that some exist. "Archived templates are
            //: listed" with none listed would be the empty state lying about
            //: itself.
            <span className="text-ui text-text-secondary">
              {archivedShown === 0
                ? "No template of this Project is archived."
                : `${archivedShown} archived template${archivedShown === 1 ? " is" : "s are"} listed below.`}
            </span>
          ) : null}
        </Cluster>
        {templates.length === 0 ? (
          <EmptyState
            title={NO_PROJECT_TEMPLATE.title}
            description={NO_PROJECT_TEMPLATE.description}
          />
        ) : (
          <TableScroll label="Chart Templates">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Answers</TableHead>
                  <TableHead>Family</TableHead>
                  <TableHead>Asks for</TableHead>
                  <TableHead>Provenance</TableHead>
                  <TableHead>Versions</TableHead>
                  {narrowed ? <TableHead>Against this Result</TableHead> : null}
                </TableRow>
              </TableHeader>
              <TableBody>
                {templates.map((row) => (
                  <TableRow key={row.id}>
                    <TableCell>
                      <Button
                        variant="ghost"
                        size="xs"
                        onClick={() => onOpenTemplate?.(row.id)}
                        disabled={!onOpenTemplate}
                      >
                        {row.answers_question || row.label}
                      </Button>
                      {/* THE STATE IS ON THE ROW, not a filter a reader has to
                          remember. A row shown only under "Show archived" and
                          marked nowhere is a row whose state is carried by the
                          control that revealed it — and it stops being carried
                          the moment somebody scrolls. */}
                      {row.archived ? (
                        <>
                          {" "}
                          <Badge tone="neutral">Archived</Badge>
                        </>
                      ) : null}
                      {row.readable ? null : (
                        <p className="m-0 text-ui text-text-secondary">
                          {row.unreadable_reason?.message}
                        </p>
                      )}
                    </TableCell>
                    <TableCell>{row.family_label ?? "—"}</TableCell>
                    <TableCell>{row.requires_sentence ?? "—"}</TableCell>
                    <TableCell>{row.origin_label}</TableCell>
                    <TableCell>{row.version_count}</TableCell>
                    {narrowed ? (
                      <TableCell>
                        <Stack>
                          <VerdictBadge row={row} />
                          <VerdictReasons row={row} />
                        </Stack>
                      </TableCell>
                    ) : null}
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </TableScroll>
        )}
      </Panel>

      <Panel>
        <PanelHeader
          title="Seeds you can draw from"
          description="A connector can ship starting points with it. Drawing one creates a template in THIS Project; the connector never owns it, and your first edit makes it yours."
        />
        {seedError ? (
          <Status as="block" tone="error" title="The seed was not drawn">
            {seedError}
          </Status>
        ) : null}
        {seeds.length === 0 ? (
          <EmptyState title={NO_SEED_AVAILABLE.title} description={NO_SEED_AVAILABLE.description} />
        ) : (
          <TableScroll label="Chart Template seeds">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Connector</TableHead>
                  <TableHead>Answers</TableHead>
                  <TableHead>Family</TableHead>
                  <TableHead>Draw it</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {seeds.map((seed) => (
                  <TableRow key={`${seed.module_name}/${seed.seed_template_id}`}>
                    <TableCell>{seed.module_name}</TableCell>
                    <TableCell>{seed.answers_question || seed.label}</TableCell>
                    <TableCell>{seed.family}</TableCell>
                    <TableCell>
                      <Button
                        size="xs"
                        onClick={() => void drawSeed(seed.module_name)}
                        disabled={seeding !== null}
                      >
                        {seeding === seed.module_name ? "Drawing…" : "Draw into this Project"}
                      </Button>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </TableScroll>
        )}
      </Panel>
    </Stack>
  );
}
