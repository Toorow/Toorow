/**
 * Golden Questions — the Level 2 collection of the product object (Story 51.1).
 *
 * This screen replaces an Epic 14 vestige that rendered
 * `id / question / topic / expected_citations / last_result`. Not one of those
 * is a field of the ratified contract: they belonged to the private benchmark
 * record, which migration 153 renamed `app.eval_benchmark_questions` so that one
 * noun has one owner (`glossary.md`, AI-81 point 1). The old screen also
 * computed a pass rate from `last_result` — a verdict with no run behind it.
 * There is no Evaluation Run owner yet (Story 51.2), so this collection reports
 * no verdict at all rather than a number that reads like one.
 *
 * What it shows is what the server owns: the stable heads, their lifecycle and
 * steward, and the governed pins of their current version. Every governed choice
 * in the create form comes from `/golden-questions/options`; nothing here is a
 * fixture, and a failure names its cause instead of falling back to sample rows.
 *
 * Composed only from `ui/admin/src/ui/index.ts` — no stylesheet, no hex colour,
 * no literal spacing, no per-screen class prefix, no page width clamp.
 */
import { useCallback, useEffect, useState } from "react";

import { ApiError } from "../../lib/apiFetch";
import { Badge, Button, Cluster, EmptyState, Field, Input, Metric, NativeSelect, ObjectId, PageHeader, Panel, PanelHeader, Stack, Status, Table, TableBody, TableCell, TableHead, TableHeader, TableRow, TableScroll } from "../../ui";
import { createGoldenQuestion, definitionPayload, emptyDefinitionDraft, fetchGoldenQuestionOptions, listGoldenQuestions, refusalsOf, type DefinitionDraft, type GoldenQuestionOptions, type GoldenQuestionSummary, type Refusal } from "../../test/goldenQuestionClient";
import { DefinitionTab, ExpectedResultTab, RefusalList } from "./GoldenQuestionWorkbench";

type Phase =
  | { status: "loading" }
  | {
      status: "ready";
      questions: GoldenQuestionSummary[];
      options: GoldenQuestionOptions;
      total: number | null;
      bound: number | null;
      nextCursor: string | null;
    }
  | { status: "no-scope" }
  | { status: "not-found" }
  | { status: "error"; message: string };

function pinLabel(question: GoldenQuestionSummary): string {
  const version = question.current_version;
  if (!version) return "No current version";
  return `${version.business_domain_id} v${version.business_domain_version_number}`;
}

export default function GoldenQuestions({
  projectId,
  onOpenGoldenQuestion,
}: {
  projectId?: string;
  /** Supplied by the shell. Without it the collection stays readable and the
   *  rows are not dressed as links to a screen nothing would open. */
  onOpenGoldenQuestion?: (goldenQuestionId: string) => void;
}) {
  const [phase, setPhase] = useState<Phase>({ status: "loading" });
  const [reloadToken, setReloadToken] = useState(0);
  const [draft, setDraft] = useState<DefinitionDraft | null>(null);
  const [title, setTitle] = useState("");
  const [owner, setOwner] = useState("");
  const [saving, setSaving] = useState(false);
  const [failure, setFailure] = useState<{ message: string; refusals: Refusal[] } | null>(null);
  const [query, setQuery] = useState("");
  const [lifecycleFilter, setLifecycleFilter] = useState("");
  const [cursor, setCursor] = useState("");

  useEffect(() => {
    if (!projectId) {
      setPhase({ status: "no-scope" });
      return;
    }
    const controller = new AbortController();
    let live = true;
    setPhase({ status: "loading" });
    Promise.all([
      listGoldenQuestions(
        projectId,
        { q: query.trim() || undefined, lifecycle: lifecycleFilter || undefined, cursor: cursor || undefined },
        { signal: controller.signal },
      ),
      fetchGoldenQuestionOptions(projectId, { signal: controller.signal }),
    ])
      .then(([collection, options]) => {
        if (!live) return;
        setPhase({
          status: "ready",
          questions: collection.golden_questions ?? [],
          options,
          total: Number.isInteger(collection.total) ? collection.total : null,
          bound: Number.isInteger(collection.bound) ? collection.bound : null,
          nextCursor: collection.next_cursor ?? null,
        });
      })
      .catch((error: unknown) => {
        if (!live || controller.signal.aborted) return;
        // Foreign, denied and absent answer identically by design; the screen
        // repeats that ambiguity instead of inventing which one it was.
        if (error instanceof ApiError && (error.status === 404 || error.unauthenticated)) {
          setPhase({ status: "not-found" });
          return;
        }
        setPhase({
          status: "error",
          message: error instanceof Error ? error.message : String(error),
        });
      });
    return () => {
      live = false;
      controller.abort();
    };
  }, [projectId, reloadToken, query, lifecycleFilter, cursor]);

  const startDraft = useCallback((options: GoldenQuestionOptions) => {
    setFailure(null);
    setTitle("");
    setOwner("");
    setDraft(emptyDefinitionDraft(options.provenance_link_kinds));
  }, []);

  const update = useCallback((patch: Partial<DefinitionDraft>) => {
    setDraft((current) => (current ? { ...current, ...patch } : current));
  }, []);

  const create = useCallback(async () => {
    if (!projectId || !draft) return;
    setSaving(true);
    setFailure(null);
    try {
      const created = await createGoldenQuestion(projectId, {
        title,
        owner,
        definition: definitionPayload(draft),
      });
      setDraft(null);
      setReloadToken((token) => token + 1);
      onOpenGoldenQuestion?.(created.golden_question_id);
    } catch (error: unknown) {
      if (error instanceof ApiError) {
        setFailure({ message: error.message, refusals: refusalsOf(error.body) });
      } else {
        setFailure({
          message: error instanceof Error ? error.message : String(error),
          refusals: [],
        });
      }
    } finally {
      setSaving(false);
    }
  }, [projectId, draft, title, owner, onOpenGoldenQuestion]);

  const header = (
    <PageHeader
      title="Golden Questions"
      description="Versioned product evaluation specifications: what a trustworthy answer means for this Project, pinned to a Business Domain version and a Semantic View version."
      actions={
        phase.status === "ready" && !draft ? (
          <Button onClick={() => startDraft(phase.options)}>New Golden Question</Button>
        ) : undefined
      }
    />
  );

  if (phase.status === "no-scope") {
    return (
      <Stack>
        {header}
        <Status as="block" tone="warning" title="Select a Project">
          Golden Questions are Project-scoped. No collection has been read, and none from another
          Project has been shown in its place.
        </Status>
      </Stack>
    );
  }
  if (phase.status === "loading") {
    return (
      <Stack>
        {header}
        <p role="status" className="text-body text-text-secondary">
          Loading the Golden Question collection…
        </p>
      </Stack>
    );
  }
  if (phase.status === "not-found") {
    return (
      <Stack>
        {header}
        <Status as="block" tone="warning" title="This collection was not opened">
          This Project has no Golden Question capability available to you, or the Project does not
          exist. The two answer identically on purpose.
        </Status>
      </Stack>
    );
  }
  if (phase.status === "error") {
    return (
      <Stack>
        {header}
        <Status
          as="block"
          tone="error"
          title="The Golden Question collection could not be read"
          action={
            <Button variant="secondary" onClick={() => setReloadToken((token) => token + 1)}>
              Retry
            </Button>
          }
        >
          {phase.message}. No question has been fabricated to fill the screen.
        </Status>
      </Stack>
    );
  }

  const { questions, options } = phase;
  const active = questions.filter((question) => question.lifecycle === "active").length;
  const critical = questions.filter(
    (question) => question.current_version?.severity === "critical",
  ).length;

  return (
    <Stack>
      {header}

      <Status as="block" tone="neutral" title="No run coverage is reported here">
        The Evaluation Run does not exist yet, so this collection shows no pass rate, no
        last result and no verdict. An absence is reported as Unverifiable on each question's
        Coverage tab, never as a green or red number.
      </Status>

      <Panel className="grid gap-2 p-2 md:grid-cols-3">
        <Metric label="Matching questions" value={phase.total ?? "Unavailable"} hint="Server-owned total" />
        <Metric label="Active on this page" value={active} hint={`Bound: ${phase.bound ?? "unavailable"}`} />
        <Metric
          label="Critical on this page"
          value={critical}
          hint="Non-compensating for a future gate"
        />
      </Panel>

      <Panel className="p-4">
        <div className="grid w-full gap-3 md:grid-cols-[minmax(16rem,2fr)_minmax(12rem,1fr)_auto_auto]">
          <Input
            aria-label="Search Golden Questions"
            placeholder="Search title, owner or identifier"
            value={query}
            onChange={(event) => { setQuery(event.target.value); setCursor(""); }}
          />
          <NativeSelect
            aria-label="Filter Golden Questions by lifecycle"
            value={lifecycleFilter}
            onChange={(event) => { setLifecycleFilter(event.target.value); setCursor(""); }}
          >
            <option value="">All lifecycle states</option>
            {options.lifecycles.map((lifecycle) => (
              <option key={lifecycle} value={lifecycle}>{lifecycle}</option>
            ))}
          </NativeSelect>
          {cursor ? <Button className="md:self-center" variant="secondary" onClick={() => setCursor("")}>First page</Button> : null}
          <Button className="md:self-center" variant="secondary" disabled={!phase.nextCursor} onClick={() => { if (phase.nextCursor) setCursor(phase.nextCursor); }}>Next page</Button>
        </div>
      </Panel>

      {failure && (
        <RefusalList
          title="The Golden Question was refused"
          message={failure.message}
          refusals={failure.refusals}
        />
      )}

      {draft && (
        <Panel flush data-testid="golden-question-create">
          <PanelHeader
            title="New Golden Question"
            description="The seven mandatory fields are validated by the server against the live governed rows. Nothing is accepted here that it would refuse."
            actions={
              <Cluster>
                <Button variant="ghost" onClick={() => setDraft(null)}>
                  Cancel
                </Button>
                <Button onClick={() => void create()} disabled={saving} data-testid="golden-question-create-save">
                  {saving ? "Creating…" : "Create version 1"}
                </Button>
              </Cluster>
            }
          />
          <div className="grid gap-4 p-5 md:grid-cols-2">
            <Field label="Title" required>
              {(field) => (
                <Input {...field} value={title} onChange={(event) => setTitle(event.target.value)} />
              )}
            </Field>
            <Field label="Owner" required hint="Stewardship. It lives on the head and does not mint a version.">
              {(field) => (
                <Input {...field} value={owner} onChange={(event) => setOwner(event.target.value)} />
              )}
            </Field>
          </div>
          <div className="grid gap-6 p-5 pt-0">
            <DefinitionTab draft={draft} options={options} update={update} />
            <ExpectedResultTab draft={draft} options={options} update={update} />
          </div>
        </Panel>
      )}

      <Panel flush>
        <PanelHeader
          title="Collection"
          description="One stable identity per question; the pins below are those of its current version."
        />
        {questions.length === 0 ? (
          <EmptyState
            title={query || lifecycleFilter || cursor ? "No Golden Question matches these filters" : "No Golden Question in this Project"}
            description={query || lifecycleFilter || cursor
              ? "Clear the filters or return to the first page; this is not evidence that the Project has no Golden Questions."
              : "Nothing has been imported from the repository evaluation corpus: that record is test code, not product knowledge, and reading it here would make the instrument part of the result."}
            action={!draft ? <Button onClick={() => startDraft(options)}>New Golden Question</Button> : undefined}
          />
        ) : (
          <TableScroll label="Golden Questions">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Question</TableHead>
                  <TableHead>Lifecycle</TableHead>
                  <TableHead>Owner</TableHead>
                  <TableHead>Business Domain</TableHead>
                  <TableHead>Semantic View version</TableHead>
                  <TableHead>Result type</TableHead>
                  <TableHead>Severity</TableHead>
                  <TableHead>Capabilities</TableHead>
                  <TableHead>Version</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {questions.map((question) => (
                  <TableRow key={question.id}>
                    <TableCell className="font-semibold text-text">
                      {onOpenGoldenQuestion ? (
                        <Button
                          variant="link"
                          size="sm"
                          onClick={() => onOpenGoldenQuestion(question.id)}
                        >
                          {question.title}
                        </Button>
                      ) : (
                        question.title
                      )}
                      <span className="block text-caption text-text-secondary"><ObjectId value={question.id} title="Golden Question" /></span>
                    </TableCell>
                    <TableCell>
                      <Badge tone="neutral">{question.lifecycle}</Badge>
                    </TableCell>
                    <TableCell>{question.owner}</TableCell>
                    <TableCell>{pinLabel(question)}</TableCell>
                    <TableCell className="text-technical break-all">
                      {question.current_version?.semantic_view_version_id ?? "Unavailable"}
                    </TableCell>
                    <TableCell>{question.current_version?.result_type ?? "Unavailable"}</TableCell>
                    <TableCell>{question.current_version?.severity ?? "Unavailable"}</TableCell>
                    <TableCell>
                      {question.current_version?.capability_tags.join(", ") || "Unavailable"}
                    </TableCell>
                    <TableCell>
                      {question.current_version ? `v${question.current_version.version_number}` : "None"}
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
