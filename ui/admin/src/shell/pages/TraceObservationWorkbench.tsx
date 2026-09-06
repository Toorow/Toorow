/**
 * The Trace Observation Workbench — Level 3, five lenses (Story 51.4).
 *
 * `docs/product-architecture/analyze-and-test.md:293` contracts exactly these
 * five, in this order: Timeline, Context & Skills, Tools, Result & Render,
 * Linked Feedback. They are the tabs declared on the `trace-observation`
 * contract in `shell/navigation.ts`, and they replace the honest
 * `RouteState kind="unavailable"` this address used to fall through to.
 *
 * This screen reads one authorized execution and asserts nothing about it.
 * Three things it deliberately does not do:
 *
 *   - **It does not fill the rendered half.** The `Result & Render` lens shows
 *     the Result its execution produced and, beside it, an EMPTY Render panel
 *     that names its owner: Stories 50.4 / 50.5 / 50.7, none delivered, the
 *     rendering stack not even installed. No thumbnail, no placeholder frame, no
 *     minted render identifier. `ck_observed_cohort_members_render_unpinned`
 *     holds that pin NULL in the schema, so `Unverifiable` is the only reachable
 *     state and the screen says so rather than leaving a blank cell.
 *   - **It does not block.** An observed record detects drift and can propose a
 *     Golden Question; it cannot be approved as a baseline and cannot gate
 *     anything until reproduced offline (`analyze-and-test.md:357-358`). Every
 *     verdict on this screen carries that statement.
 *   - **It does not reorder anything.** The timeline renders the STORED ordinal.
 *     Two steps can share a timestamp, and sorting by time would present an
 *     order that never happened.
 *
 * Composed only from `ui/admin/src/ui/index.ts`.
 */
import { useCallback, useEffect, useMemo, useState } from "react";

import { ApiError } from "../../lib/apiFetch";
import { Badge, Button, Cluster, EmptyState, Metric, NavTabs, ObjectHeader, ObjectId, Panel, PanelHeader, SectionHeader, Stack, Status, Table, TableBody, TableCell, TableHead, TableHeader, TableRow, TableScroll, type NavTab } from "../../ui";
import { fetchTraceObservation, TRACE_OBSERVATION_LENSES, type Absence, type TraceObservation } from "../../test/observedEvidenceClient";
import { verdictLabel, verdictTone } from "../../test/testEvidence";
import { buildPath, useRoute } from "../router";

type Phase =
  | { status: "loading" }
  | { status: "ready"; observation: TraceObservation }
  | { status: "not-found" }
  | { status: "error"; message: string };

/**
 * A declared absence, rendered as an absence.
 *
 * The reason code and the owning story are both shown because a gap with no
 * named owner is indistinguishable from a gap nobody noticed — the defect that
 * had Story 49.6 rejected.
 */
export function AbsencePanel({
  title,
  description,
  absence,
  testId,
}: {
  title: string;
  description: string;
  absence: Absence;
  testId: string;
}) {
  const owner = absence.owner ?? (absence.owner_stories ?? []).map((s) => `Story ${s}`).join(", ");
  return (
    <Panel flush data-testid={testId}>
      <PanelHeader
        title={title}
        description={description}
        actions={<Status tone="neutral">{verdictLabel(absence.state)}</Status>}
      />
      <div className="grid gap-2 p-5">
        <span className="text-technical">{absence.reason ?? "reason_not_stated"}</span>
        {absence.detail && (
          <p className="m-0 max-w-[70ch] text-ui text-text-secondary">{absence.detail}</p>
        )}
        <span className="text-caption text-text-secondary">
          Owner: {owner || "not stated by the server"}
        </span>
      </div>
    </Panel>
  );
}

// ---------------------------------------------------------------------------
// The five lenses.
// ---------------------------------------------------------------------------

function TimelineLens({ observation }: { observation: TraceObservation }) {
  const lens = observation.lenses.timeline;
  if (!lens || lens.steps.length === 0) {
    return (
      <EmptyState
        title="No step was recorded for this execution"
        description="A step-less path counts in the denominator of every aggregate and never in the numerator. Dropping it would inflate coverage, which is the same defect as a percentage without a denominator."
      />
    );
  }
  return (
    <Panel flush>
      <PanelHeader
        title="Timeline"
        description={`Ordered by ${lens.ordering}. Two steps can share a timestamp, so the recorded order is the one shown.`}
      />
      <TableScroll label="Observed steps">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Ordinal</TableHead>
              <TableHead>Observed at</TableHead>
              <TableHead>Step kind</TableHead>
              <TableHead>Outcome</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {lens.steps.map((step) => (
              <TableRow key={`${step.ordinal}-${step.step_kind}`}>
                <TableCell className="font-semibold text-text">{step.ordinal}</TableCell>
                <TableCell>{step.observed_at ?? "Unavailable"}</TableCell>
                <TableCell>{step.step_kind ?? "Unavailable"}</TableCell>
                <TableCell>{step.outcome ?? "Unavailable"}</TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </TableScroll>
    </Panel>
  );
}

function ContextSkillsLens({ observation }: { observation: TraceObservation }) {
  const steps = observation.lenses["context-skills"]?.steps ?? [];
  if (steps.length === 0) {
    return (
      <EmptyState
        title="No governed context was consulted in this execution"
        description="That is a fact about the execution, not a gap in the record."
      />
    );
  }
  return (
    <Panel flush>
      <PanelHeader
        title="Context & Skills"
        description="A Skill pin is shown as a pair or not at all: half a pin resolves to the wrong step the first time a Skill is revised."
      />
      <TableScroll label="Governed context and Skills">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Ordinal</TableHead>
              <TableHead>Owner workspace</TableHead>
              <TableHead>Object</TableHead>
              <TableHead>Version</TableHead>
              <TableHead>Skill</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {steps.map((step) => (
              <TableRow key={`${step.ordinal}-${step.owner_object_id}`}>
                <TableCell className="font-semibold text-text">{step.ordinal}</TableCell>
                <TableCell>{step.owner_workspace ?? "—"}</TableCell>
                <TableCell>
                  {step.owner_object_type ?? "—"}
                  <span className="block text-caption text-text-secondary break-all">
                    {step.owner_object_id ?? "—"}
                  </span>
                </TableCell>
                <TableCell className="text-technical break-all">
                  {step.owner_version_id ?? "Not pinned"}
                </TableCell>
                <TableCell className="text-technical break-all">
                  {step.skill
                    ? `${step.skill.skill_version_id} · ${step.skill.skill_step_id}`
                    : "No Skill pinned on this step"}
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </TableScroll>
    </Panel>
  );
}

function ToolsLens({ observation }: { observation: TraceObservation }) {
  const lens = observation.lenses.tools;
  const steps = lens?.steps ?? [];
  return (
    <Panel flush>
      <PanelHeader
        title="Tools"
        description={
          lens?.tool_name_is_a_label
            ? "A tool name is a recorded label. Nothing joins on it, so it must never be read as a governed identity."
            : "Tool calls observed during this execution."
        }
      />
      {steps.length === 0 ? (
        <EmptyState
          title="No tool call was observed"
          description="The execution used no tool, or none was recorded. Both are stated as an absence rather than as a zero that looks measured."
        />
      ) : (
        <TableScroll label="Observed tool calls">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Ordinal</TableHead>
                <TableHead>Tool name (label)</TableHead>
                <TableHead>Outcome</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {steps.map((step) => (
                <TableRow key={`${step.ordinal}-${step.tool_name}`}>
                  <TableCell className="font-semibold text-text">{step.ordinal}</TableCell>
                  <TableCell>{step.tool_name ?? "Unavailable"}</TableCell>
                  <TableCell>{step.outcome ?? "Unavailable"}</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </TableScroll>
      )}
    </Panel>
  );
}

function ResultRenderLens({ observation }: { observation: TraceObservation }) {
  const lens = observation.lenses["result-render"];
  const result = lens?.result;
  return (
    <Stack>
      <Panel flush>
        <PanelHeader
          title="Result"
          description="The immutable Result this execution produced. Test annotates its version; it never duplicates the owning workbench."
        />
        {!result || result.state === "absent" ? (
          <EmptyState
            title="No Result is pinned to this AI Path"
            description={
              result && result.state === "absent"
                ? result.reason
                : "The execution recorded no immutable Result."
            }
          />
        ) : (
          <TableScroll label="Pinned Result">
            <Table>
              <TableBody>
                <TableRow>
                  <TableCell className="font-semibold text-text">Result</TableCell>
                  <TableCell><ObjectId value={result.id} title="Result" /></TableCell>
                </TableRow>
                <TableRow>
                  <TableCell className="font-semibold text-text">Outcome</TableCell>
                  <TableCell>{result.outcome ?? "Unavailable"}</TableCell>
                </TableRow>
                <TableRow>
                  <TableCell className="font-semibold text-text">Rows</TableCell>
                  <TableCell>
                    {result.row_count ?? "Unavailable"}
                    {result.truncated ? " (truncated)" : ""}
                  </TableCell>
                </TableRow>
                <TableRow>
                  <TableCell className="font-semibold text-text">Content hash</TableCell>
                  <TableCell className="text-technical break-all">
                    {result.content_hash ?? "Unavailable"}
                  </TableCell>
                </TableRow>
              </TableBody>
            </Table>
          </TableScroll>
        )}
      </Panel>

      {/* The empty half, said out loud. This panel is intentionally without
          content: there is no rendered artifact object in this repository. */}
      <AbsencePanel
        testId="render-absence"
        title="Render"
        description="This panel is empty because the object it would show does not exist — not because this execution produced nothing."
        absence={lens?.render ?? { state: "unverifiable", reason: "render_owner_not_delivered" }}
      />
    </Stack>
  );
}

function LinkedFeedbackLens({ observation }: { observation: TraceObservation }) {
  const lens = observation.lenses["linked-feedback"];
  const records = lens?.records ?? [];
  return (
    <Stack>
      <AbsencePanel
        testId="linked-feedback-absence"
        title="Linked Feedback"
        description="Feedback pinned to this exact execution."
        absence={lens?.feedback ?? { state: "unverifiable", reason: "pinned_feedback_owner_not_delivered" }}
      />
      {records.length === 0 && (
        <EmptyState
          title="No annotation is pinned to this execution"
          description="Matching a trace id against a feedback row would present correlation as identity, which the schema refuses. Nothing has been matched in its place."
        />
      )}
    </Stack>
  );
}

// ---------------------------------------------------------------------------
// The workbench.
// ---------------------------------------------------------------------------

export default function TraceObservationWorkbench({
  projectId,
  aiPathId,
  tab,
  onNavigateTab,
}: {
  projectId: string;
  aiPathId: string;
  tab: string;
  onNavigateTab: (tab: string) => void;
}) {
  const { route } = useRoute();
  const [phase, setPhase] = useState<Phase>({ status: "loading" });
  const [reloadToken, setReloadToken] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    let live = true;
    setPhase({ status: "loading" });
    fetchTraceObservation(projectId, aiPathId, { signal: controller.signal })
      .then((observation) => {
        if (live) setPhase({ status: "ready", observation });
      })
      .catch((error: unknown) => {
        if (!live || controller.signal.aborted) return;
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
  }, [projectId, aiPathId, reloadToken]);

  const tabs = useMemo<NavTab[]>(
    () =>
      TRACE_OBSERVATION_LENSES.map((candidate) => {
        let href: string | undefined;
        try {
          href = buildPath({ ...route, tab: candidate.key, action: null, versionId: null });
        } catch {
          href = undefined;
        }
        return { key: candidate.key, label: candidate.label, href };
      }),
    [route],
  );

  const retry = useCallback(() => setReloadToken((token) => token + 1), []);

  if (phase.status === "loading") {
    return (
      <p role="status" className="text-body text-text-secondary">
        Loading this Trace Observation…
      </p>
    );
  }
  if (phase.status === "not-found") {
    return (
      <Status as="block" tone="warning" title="This Trace Observation was not opened">
        No observed AI Path with this identifier exists in this Project, or it is not available to
        you. The two answer identically on purpose.
      </Status>
    );
  }
  if (phase.status === "error") {
    return (
      <Status
        as="block"
        tone="error"
        title="This Trace Observation could not be read"
        action={
          <Button variant="secondary" onClick={retry}>
            Retry
          </Button>
        }
      >
        {phase.message}. Nothing has been shown in its place.
      </Status>
    );
  }

  const { observation } = phase;
  const dimensions = observation.dimensions;

  return (
    <Stack data-owner={`test/regression-runs/${observation.ai_path_id}`}>
      <ObjectHeader
        name={observation.ai_path_id}
        source={`Trace Observation · ${observation.actor ?? "actor unavailable"}`}
      />
      <Cluster>
        <Badge tone="neutral">{dimensions?.evidence_mode ?? "observed_cohort"}</Badge>
        <Badge tone="neutral">{observation.lifecycle ?? "lifecycle unavailable"}</Badge>
        <Status tone="neutral" data-testid="observed-non-blocking">
          {dimensions?.label ?? "Observed Cohort - reference window, non-blocking"}
        </Status>
      </Cluster>
      <NavTabs label="Trace Observation" tabs={tabs} current={tab} onNavigate={onNavigateTab} />

      <Panel className="grid gap-2 p-2 md:grid-cols-2 xl:grid-cols-4">
        <Metric
          label="Path evidence"
          value={observation.path_evidence_state}
          hint="`unverifiable` counts in the denominator, never in the numerator"
        />
        <Metric label="Outcome" value={observation.outcome ?? "Unavailable"} hint="As recorded" />
        <Metric
          label="Model"
          value={observation.model_ref ?? "Unavailable"}
          hint={`Tool catalog ${observation.tool_catalog_version ?? "unavailable"}`}
        />
        <Metric
          label="W3C trace id"
          value={observation.w3c_trace_id ?? "Unavailable"}
          hint={
            observation.w3c_trace_id_is_correlation_not_identity
              ? "Correlation, never identity"
              : "As recorded"
          }
        />
      </Panel>

      <Panel flush>
        <PanelHeader
          title="Dimensions of this observation"
          description="Each dimension keeps its own verdict, and an observed record can never block on any of them."
        />
        <TableScroll label="Dimensions of this observation">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Dimension</TableHead>
                <TableHead>Verdict</TableHead>
                <TableHead>Reason / findings</TableHead>
                <TableHead>Owner</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              <TableRow>
                <TableCell className="font-semibold text-text">Path quality</TableCell>
                <TableCell>
                  <Status
                    tone={verdictTone(dimensions?.path_quality?.verdict ?? "unverifiable")}
                    data-testid="dimension-path_quality"
                  >
                    {verdictLabel(dimensions?.path_quality?.verdict ?? "unverifiable")}
                  </Status>
                </TableCell>
                <TableCell>
                  {(dimensions?.path_quality?.findings ?? []).length === 0
                    ? "No finding recorded"
                    : (dimensions?.path_quality?.findings ?? [])
                        .map((finding) => `${finding.finding}${finding.detail ? ` — ${finding.detail}` : ""}`)
                        .join("; ")}
                </TableCell>
                {/* The owner of this dimension, in the same column and the same words as
                    the two rows below, which read theirs from the record. A story
                    number is not an owner a reader can go to (76-4). */}
                <TableCell>Observed evidence</TableCell>
              </TableRow>
              {([
                ["Render behaviour", "render_behavior", dimensions?.render_behavior],
                ["MCP App behaviour", "mcp_app_behavior", dimensions?.mcp_app_behavior],
              ] as const).map(([label, key, absence]) => (
                <TableRow key={key}>
                  <TableCell className="font-semibold text-text">{label}</TableCell>
                  <TableCell>
                    <Status tone="neutral" data-testid={`dimension-${key}`}>
                      {verdictLabel(absence?.state ?? "unverifiable")}
                    </Status>
                  </TableCell>
                  <TableCell>
                    <span className="text-technical">{absence?.reason ?? "owner_not_delivered"}</span>
                    <span className="block text-caption text-text-secondary">{absence?.detail}</span>
                  </TableCell>
                  <TableCell>
                    {absence?.owner ?? ((absence?.owner_stories ?? []).join(", ") || "Not stated")}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </TableScroll>
      </Panel>

      {tab === "timeline" && <TimelineLens observation={observation} />}
      {tab === "context-skills" && <ContextSkillsLens observation={observation} />}
      {tab === "tools" && <ToolsLens observation={observation} />}
      {tab === "result-render" && <ResultRenderLens observation={observation} />}
      {tab === "linked-feedback" && <LinkedFeedbackLens observation={observation} />}

      {observation.proposals_suggested.length > 0 && (
        <Panel flush>
          <PanelHeader
            title="Golden Questions this observation suggests"
            description="Suggestions only. Nothing on this screen creates, edits or versions a Golden Question — that is done from the Golden Question workbench."
          />
          <TableScroll label="Suggested Golden Questions">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Reason</TableHead>
                  <TableHead>Severity hint</TableHead>
                  <TableHead>Blocking</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {observation.proposals_suggested.map((proposal) => (
                  <TableRow key={proposal.reason_code}>
                    <TableCell className="text-technical">{proposal.reason_code}</TableCell>
                    <TableCell>{proposal.severity_hint}</TableCell>
                    <TableCell>
                      <Status tone="neutral">Never, until reproduced offline</Status>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </TableScroll>
        </Panel>
      )}

      <SectionHeader
        title="Owners"
        description={Object.entries(observation.owner_links ?? {})
          .map(([target, owner]) => `${target}: ${owner}`)
          .join(" · ")}
      />
    </Stack>
  );
}
