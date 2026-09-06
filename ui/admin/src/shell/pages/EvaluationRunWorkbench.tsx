/**
 * The Evaluation Run Workbench — Level 3, five tabs (Story 51.2).
 *
 * `docs/product-architecture/analyze-and-test.md:291` contracts exactly these
 * five, in this order: Overview, Cases, Comparisons, Environment, Gate Decision.
 * They are the tabs declared on the `evaluation-run` contract in
 * `shell/navigation.ts`, and each one is a separate server address — so a tab is
 * something a person can send to a colleague, not a piece of component state.
 *
 * The rule this screen exists to hold: **the six dimensions keep six separate
 * verdicts.** There is no aggregate anywhere on it. `verdict_counts` is rendered
 * as a grid of counts with every verdict column present, which is what states
 * its own denominator; a percentage over it would be the compensating figure
 * `analyze-and-test.md:336-337` forbids, and no function in this file computes
 * one. An overall score cannot hide a critical failure here because there is
 * nowhere to put it.
 *
 * The second rule: **an absence is never a verdict.** `Unverifiable` takes the
 * neutral mark — an open dotted ring — with its reason code and the story that
 * owns the missing evidence. `mcp_app_behavior` reads `Unverifiable` on every
 * case written today because no rendered artifact exists to judge (Stories
 * 50.4 / 50.5 / 50.7), and the Environment tab shows the `render` pin family as
 * an explicitly unresolved pin rather than an empty cell.
 *
 * Composed only from `ui/admin/src/ui/index.ts`.
 */
import { useCallback, useEffect, useMemo, useState } from "react";

import { ApiError } from "../../lib/apiFetch";
import { Badge, Button, Cluster, displayValue, EmptyState, Metric, NavTabs, ObjectHeader, ObjectId, Panel, PanelHeader, SectionHeader, Stack, Status, Table, TableBody, TableCell, TableHead, TableHeader, TableRow, TableScroll, type NavTab, wireWord } from "../../ui";
import { EVALUATION_DIMENSIONS, EVALUATION_VERDICTS, fetchRunCases, fetchRunComparisons, fetchRunEnvironment, fetchRunGateDecisions, fetchRunOverview, type GateDecision, type RunCase, type RunComparison, type RunEnvironment, type RunOverview, type UnresolvedPin } from "../../test/evaluationRunClient";
import FeedbackRegressionCaseActions from "../../test/FeedbackRegressionCaseActions";
import type { OwnerLink } from "../../test/feedbackReviewClient";
import { dimensionLabel, evidenceModeLabel, verdictLabel, verdictTone } from "../../test/testEvidence";
import { buildPath, useRoute } from "../router";

/** The five contracted tabs. The slug is the address; the label is the copy. */
export const EVALUATION_RUN_TABS = [
  { key: "overview", label: "Overview" },
  { key: "cases", label: "Cases" },
  { key: "comparisons", label: "Comparisons" },
  { key: "environment", label: "Environment" },
  { key: "gate-decision", label: "Gate Decision" },
] as const;

type TabPayload = RunOverview | { cases: RunCase[] } | { comparisons: RunComparison[] } | RunEnvironment | { gate_decisions: GateDecision[] };

type Phase<T> =
  | { status: "loading" }
  | { status: "ready"; value: T }
  | { status: "not-found" }
  | { status: "error"; message: string };

function toPhase<T>(error: unknown): Phase<T> {
  if (error instanceof ApiError && (error.status === 404 || error.unauthenticated)) {
    return { status: "not-found" };
  }
  return { status: "error", message: error instanceof Error ? error.message : String(error) };
}

function NotOpened({ what }: { what: string }) {
  return (
    <Status as="block" tone="warning" title={`${what} was not opened`}>
      No such object exists in this Project, or it is not available to you. The two answer
      identically on purpose, and nothing else has been opened in its place.
    </Status>
  );
}

function ReadFailure({ what, message, retry }: { what: string; message: string; retry: () => void }) {
  return (
    <Status
      as="block"
      tone="error"
      title={`${what} could not be read`}
      action={
        <Button variant="secondary" onClick={retry}>
          Retry
        </Button>
      }
    >
      {message}. Nothing has been shown in its place.
    </Status>
  );
}

// ---------------------------------------------------------------------------
// The declared absences, rendered as what they are.
// ---------------------------------------------------------------------------

export function UnresolvedPins({ pins }: { pins: UnresolvedPin[] }) {
  if (pins.length === 0) {
    return (
      <EmptyState
        title="No unresolved pin recorded"
        description="Every pin family this run declares resolved to an exact identity."
      />
    );
  }
  return (
    <TableScroll label="Unresolved pins">
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>Pin family</TableHead>
            <TableHead>State</TableHead>
            <TableHead>Reason</TableHead>
            <TableHead>Owner</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {pins.map((pin) => (
            <TableRow key={`${pin.pin_family}-${pin.reason_code}`}>
              <TableCell className="font-semibold text-text">{pin.pin_family}</TableCell>
              <TableCell>
                <Status tone="neutral" data-testid={`pin-${pin.pin_family}`}>
                  Unverifiable
                </Status>
              </TableCell>
              <TableCell>
                <span className="text-technical">{pin.reason_code}</span>
                <span className="block text-caption text-text-secondary">{pin.detail}</span>
              </TableCell>
              <TableCell>{pin.owner}</TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </TableScroll>
  );
}

// ---------------------------------------------------------------------------
// Overview.
// ---------------------------------------------------------------------------

function OverviewTab({ overview }: { overview: RunOverview }) {
  return (
    <Stack>
      <Panel className="grid gap-2 p-2 md:grid-cols-2 xl:grid-cols-4">
        <Metric label="Evidence mode" value={overview.evidence_mode} hint="Never merged with another" />
        <Metric label="Lifecycle" value={overview.lifecycle} hint="A finalized run is immutable" />
        <Metric label="Cases" value={overview.case_count} hint="One per pinned Golden Question version" />
        <Metric label="As of" value={overview.as_of ?? "Unavailable"} hint="A date, never an hour" />
      </Panel>

      <Panel flush>
        <PanelHeader
          title="Verdicts by dimension"
          description="Six dimensions, four verdicts, one row each. There is no total column and no rate: an overall figure could hide a critical failure or pay for a correctness regression with a better one elsewhere."
        />
        <TableScroll label="Verdicts by dimension">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Dimension</TableHead>
                {EVALUATION_VERDICTS.map((verdict) => (
                  <TableHead key={verdict}>{verdictLabel(verdict)}</TableHead>
                ))}
              </TableRow>
            </TableHeader>
            <TableBody>
              {EVALUATION_DIMENSIONS.map((dimension) => {
                const counts = overview.verdict_counts?.[dimension] ?? {};
                return (
                  <TableRow key={dimension}>
                    <TableCell className="font-semibold text-text">
                      {dimensionLabel(dimension)}
                    </TableCell>
                    {EVALUATION_VERDICTS.map((verdict) => (
                      <TableCell key={verdict} data-testid={`count-${dimension}-${verdict}`}>
                        <span className="font-numeric [font-variant-numeric:lining-nums_tabular-nums]">
                          {counts[verdict] ?? 0}
                        </span>
                        <span className="block text-caption text-text-secondary">
                          of {overview.case_count} case(s)
                        </span>
                      </TableCell>
                    ))}
                  </TableRow>
                );
              })}
            </TableBody>
          </Table>
        </TableScroll>
      </Panel>

      <Panel flush>
        <PanelHeader
          title="Unresolved pins"
          description="Each absence names its reason and the story that owns it. A run may not be finalized while an unresolvable pin has no recorded reason."
        />
        <UnresolvedPins pins={overview.unresolved_pins ?? []} />
      </Panel>

      <Panel flush>
        <PanelHeader title="Identity" description="What makes this run comparable with another." />
        <TableScroll label="Run identity">
          <Table>
            <TableBody>
              <TableRow>
                <TableCell className="font-semibold text-text">Run</TableCell>
                <TableCell><ObjectId value={overview.id} title="Evaluation Run" /></TableCell>
              </TableRow>
              <TableRow>
                <TableCell className="font-semibold text-text">Run profile</TableCell>
                <TableCell><ObjectId value={overview.run_profile_id} title="Run profile" /></TableCell>
              </TableRow>
              <TableRow>
                <TableCell className="font-semibold text-text">Question set fingerprint</TableCell>
                <TableCell className="text-technical break-all">
                  {overview.question_set_fingerprint ?? "Not fingerprinted until finalized"}
                </TableCell>
              </TableRow>
              <TableRow>
                <TableCell className="font-semibold text-text">Content hash</TableCell>
                <TableCell className="text-technical break-all">
                  {overview.content_hash ?? "Not frozen until finalized"}
                </TableCell>
              </TableRow>
            </TableBody>
          </Table>
        </TableScroll>
      </Panel>
    </Stack>
  );
}

// ---------------------------------------------------------------------------
// Cases — six independent verdicts per case.
// ---------------------------------------------------------------------------

function CasesTab({
  projectId,
  cases,
  focusId,
  onOpenOwner,
}: {
  projectId: string;
  cases: RunCase[];
  focusId?: string | null;
  onOpenOwner?: (link: OwnerLink) => void;
}) {
  if (cases.length === 0) {
    return (
      <EmptyState
        title="This run pins no case"
        description="A run with no case pins no subject and cannot be finalized. Nothing has been shown in place of the cases it does not have."
      />
    );
  }
  const focusedCase = focusId ? cases.find((runCase) => runCase.id === focusId) : undefined;
  const focusedVerdict = focusId
    ? cases.flatMap((runCase) => runCase.verdicts.map((verdict) => ({ runCase, verdict })))
      .find(({ verdict }) => verdict.verdict_id === focusId)
    : undefined;
  return (
    <Stack>
      <Status as="block" tone="neutral" title="Six verdicts per case, and none of them compensates another">
        A case is judged on semantic correctness, provenance, context adherence, path quality, DQ
        handling and MCP App behaviour independently. `Unverifiable` means nothing was measured — it
        is not a soft failure, and `Not applicable` is not a silent pass.
      </Status>
      {focusId && (
        <Status
          as="block"
          tone={focusedCase || focusedVerdict ? "neutral" : "error"}
          title={focusedCase
            ? "Focused Evaluation Case"
            : focusedVerdict
              ? "Focused evaluation Verdict"
              : "Focused evaluation evidence is unavailable"}
          data-testid={`evaluation-focus-${focusId}`}
        >
          {focusedCase
            ? `Evaluation Case ${focusedCase.id}`
            : focusedVerdict
              ? `${dimensionLabel(focusedVerdict.verdict.dimension)} Verdict ${focusedVerdict.verdict.verdict_id}`
              : `No Case or Verdict in this Run has identity ${focusId}.`}
        </Status>
      )}
      {cases.map((runCase) => (
        <Panel key={runCase.id} flush>
          <PanelHeader
            title={runCase.golden_question_version_id}
            description={`Case ${runCase.id}`}
            actions={
              <Cluster>
                {runCase.result_type && <Badge tone="neutral">{wireWord(runCase.result_type)}</Badge>}
                {runCase.capability_key && <Badge tone="neutral">{runCase.capability_key}</Badge>}
              </Cluster>
            }
          />
          <FeedbackRegressionCaseActions
            projectId={projectId}
            runCase={runCase}
            onOpenOwner={onOpenOwner}
          />
          <div className="grid gap-3 p-5">
            <SectionHeader
              title="What this case pins"
              description="The classification axes are read from the governed Golden Question version, never chosen by the run."
            />
            <TableScroll label={`Pins of case ${runCase.id}`}>
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Result</TableHead>
                    <TableHead>AI Path</TableHead>
                    <TableHead>Business Domain</TableHead>
                    <TableHead>Render</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  <TableRow>
                    <TableCell className="text-technical break-all">
                      {runCase.result_id ?? "No immutable Result pinned"}
                    </TableCell>
                    <TableCell className="text-technical break-all">
                      {runCase.ai_path ?? "Path evidence expected and missing"}
                    </TableCell>
                    <TableCell>
                      {runCase.business_domain_id
                        ? `${runCase.business_domain_id} v${runCase.business_domain_version_number}`
                        : "Unavailable"}
                    </TableCell>
                    <TableCell>
                      <Status tone="neutral" data-testid={`case-render-${runCase.id}`}>
                        Unverifiable
                      </Status>
                    </TableCell>
                  </TableRow>
                </TableBody>
              </Table>
            </TableScroll>
          </div>
          <TableScroll label={`Verdicts of case ${runCase.id}`}>
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Dimension</TableHead>
                  <TableHead>Verdict</TableHead>
                  <TableHead>Reason</TableHead>
                  <TableHead>Evidence</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {runCase.verdicts.map((entry) => (
                  <TableRow key={entry.dimension}>
                    <TableCell className="font-semibold text-text">
                      {dimensionLabel(entry.dimension)}
                    </TableCell>
                    <TableCell>
                      <Status
                        tone={verdictTone(entry.verdict)}
                        data-testid={`verdict-${runCase.id}-${entry.dimension}`}
                      >
                        {verdictLabel(entry.verdict)}
                      </Status>
                    </TableCell>
                    <TableCell className="text-technical">{entry.reason_code}</TableCell>
                    <TableCell className="text-caption text-text-secondary">
                      {typeof entry.evidence_refs?.owner === "string"
                        ? String(entry.evidence_refs.owner)
                        : "—"}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </TableScroll>
          {runCase.unresolved_pins.length > 0 && (
            <div className="border-t border-divider-base">
              <UnresolvedPins pins={runCase.unresolved_pins} />
            </div>
          )}
        </Panel>
      ))}
    </Stack>
  );
}

// ---------------------------------------------------------------------------
// Comparisons — a mixed or missing pin is Unverifiable, never "unchanged".
// ---------------------------------------------------------------------------

function ComparisonsTab({ comparisons }: { comparisons: RunComparison[] }) {
  if (comparisons.length === 0) {
    return (
      <EmptyState
        title="This run is in no comparison"
        description="A comparison holds everything constant but the families it declares changed. None has been built against this run."
      />
    );
  }
  return (
    <Panel flush>
      <PanelHeader
        title="Comparisons"
        description="A model, host or tool-catalog qualification is a separate comparison from a semantic or context one; their results are never pooled."
      />
      <TableScroll label="Comparisons">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Comparison</TableHead>
              <TableHead>Kind</TableHead>
              <TableHead>Baseline run</TableHead>
              <TableHead>Candidate run</TableHead>
              <TableHead>Declared changed</TableHead>
              <TableHead>Unverifiable families</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {comparisons.map((comparison) => (
              <TableRow key={comparison.id}>
                <TableCell><ObjectId value={comparison.id} title="Comparison" /></TableCell>
                <TableCell>
                  <Badge tone="neutral">{wireWord(comparison.comparison_kind)}</Badge>
                </TableCell>
                <TableCell><ObjectId value={comparison.baseline_run_id} title="Baseline Run" /></TableCell>
                <TableCell><ObjectId value={comparison.candidate_run_id} title="Candidate Run" /></TableCell>
                <TableCell>{comparison.changed_pin_families.join(", ") || "None"}</TableCell>
                <TableCell>
                  {comparison.unverifiable_families.length === 0 ? (
                    "None"
                  ) : (
                    <Status tone="neutral" data-testid={`unverifiable-${comparison.id}`}>
                      Unverifiable: {comparison.unverifiable_families.join(", ")}
                    </Status>
                  )}
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </TableScroll>
    </Panel>
  );
}

// ---------------------------------------------------------------------------
// Environment — the pinned families of `analyze-and-test.md:223-231`.
// ---------------------------------------------------------------------------

function EnvironmentTab({ environment }: { environment: RunEnvironment }) {
  return (
    <Stack>
      <Panel flush>
        <PanelHeader
          title="Pinned families"
          description="Each family resolves to an exact identity or to a declared, owned absence. There is no third state and no default."
        />
        <TableScroll label="Pinned families">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Family</TableHead>
                <TableHead>State</TableHead>
                <TableHead>Pinned</TableHead>
                <TableHead>Fingerprint</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {environment.families.map((family) => (
                <TableRow key={family.family}>
                  <TableCell className="font-semibold text-text">{family.family}</TableCell>
                  <TableCell>
                    {family.state === "pinned" ? (
                      <Status tone="success" data-testid={`family-${family.family}`}>
                        Pinned
                      </Status>
                    ) : (
                      <Status tone="neutral" data-testid={`family-${family.family}`}>
                        Unverifiable
                      </Status>
                    )}
                  </TableCell>
                  <TableCell className="break-all">
                    {family.state === "pinned" ? (
                      // `displayValue` rather than a local formatter: a pin read
                      // through `JSON.stringify` is the defect `Evidence.tsx`
                      // exists to have removed once, not once per screen.
                      displayValue(family.pinned)
                    ) : (
                      <>
                        <span className="text-technical">{family.absence?.reason_code}</span>
                        <span className="block text-caption text-text-secondary">
                          {family.absence?.detail} — {family.absence?.owner}
                        </span>
                      </>
                    )}
                  </TableCell>
                  <TableCell className="text-technical break-all">
                    {environment.pin_fingerprints?.[family.family] ?? "Not fingerprinted"}
                  </TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </TableScroll>
      </Panel>

      <Panel flush>
        <PanelHeader
          title="Unresolved pins"
          description="The same absences, listed once with their owners, so the run's reproducibility gap is readable without reading every family."
        />
        <UnresolvedPins pins={environment.unresolved_pins ?? []} />
      </Panel>
    </Stack>
  );
}

// ---------------------------------------------------------------------------
// Gate Decision — evidence the owning workflow reads. Never a transition.
// ---------------------------------------------------------------------------

function gateTone(decision: string): "success" | "error" | "neutral" {
  if (decision === "pass") return "success";
  if (decision === "block") return "error";
  return "neutral";
}

function GateDecisionTab({ decisions }: { decisions: GateDecision[] }) {
  return (
    <Stack>
      <Status as="block" tone="neutral" title="Test writes evidence; the owner decides">
        A Gate Decision is read by the owning Governance or Context Hub workflow before its own
        publish or activate step. Nothing on this screen edits a candidate or changes its lifecycle,
        and missing required coverage is `Unverifiable`, never green.
      </Status>
      {decisions.length === 0 ? (
        <EmptyState
          title="No Gate Decision references this run"
          description="No comparison built on this run has been submitted for a decision. An absent decision is not a pass."
        />
      ) : (
        decisions.map((decision) => (
          <Panel key={decision.id} flush>
            <PanelHeader
              title={`${decision.candidate.object_type} ${decision.candidate.object_id}`}
              description={`Candidate version ${decision.candidate.version_id} · owned by ${decision.candidate.owner_workspace}`}
              actions={
                <Status tone={gateTone(decision.decision)} data-testid={`gate-${decision.id}`}>
                  {decision.decision === "unverifiable"
                    ? "Unverifiable"
                    : decision.decision === "block"
                      ? "Block"
                      : "Pass"}
                </Status>
              }
            />
            <div className="grid gap-2 p-2 md:grid-cols-3">
              <Metric
                label="Eligible questions"
                value={decision.coverage.eligible}
                hint="The denominator of this decision"
              />
              <Metric
                label="Evaluated"
                value={decision.coverage.evaluated}
                hint="All six dimensions recorded"
              />
              <Metric
                label="Missing"
                value={decision.coverage.missing}
                hint="Any missing coverage makes the decision Unverifiable"
              />
            </div>
            <div className="grid gap-2 p-5 pt-0">
              <SectionHeader title="Reason" />
              <p className="m-0 max-w-[70ch] text-ui text-text">{decision.decision_reason}</p>
              <SectionHeader title="Failing dimensions" />
              <p className="m-0 text-ui text-text">
                {decision.failing_dimensions.length === 0
                  ? "None recorded"
                  : decision.failing_dimensions.map(dimensionLabel).join(", ")}
              </p>
              <span className="text-caption text-text-secondary">
                Decided by {decision.decided_by} · {decision.decided_at ?? "Unavailable"} ·
                comparison {decision.comparison_id}
              </span>
            </div>
          </Panel>
        ))
      )}
    </Stack>
  );
}

// ---------------------------------------------------------------------------
// The workbench.
// ---------------------------------------------------------------------------

export default function EvaluationRunWorkbench({
  projectId,
  runId,
  tab,
  focusId,
  onNavigateTab,
  onOpenOwner,
}: {
  projectId: string;
  runId: string;
  tab: string;
  focusId?: string | null;
  onNavigateTab: (tab: string) => void;
  onOpenOwner?: (link: OwnerLink) => void;
}) {
  const { route } = useRoute();
  const [overview, setOverview] = useState<Phase<RunOverview>>({ status: "loading" });
  const [detail, setDetail] = useState<Phase<TabPayload>>({ status: "loading" });
  const [reloadToken, setReloadToken] = useState(0);

  // The header reads the Overview address on every tab: the object's identity
  // and its lifecycle belong to the object, not to one of its five views.
  useEffect(() => {
    const controller = new AbortController();
    let live = true;
    setOverview({ status: "loading" });
    fetchRunOverview(projectId, runId, { signal: controller.signal })
      .then((value) => {
        if (live) setOverview({ status: "ready", value });
      })
      .catch((error: unknown) => {
        if (!live || controller.signal.aborted) return;
        setOverview(toPhase<RunOverview>(error));
      });
    return () => {
      live = false;
      controller.abort();
    };
  }, [projectId, runId, reloadToken]);

  useEffect(() => {
    if (tab === "overview") return;
    const controller = new AbortController();
    let live = true;
    setDetail({ status: "loading" });
    const read =
      tab === "cases"
        ? fetchRunCases(projectId, runId, { signal: controller.signal })
        : tab === "comparisons"
          ? fetchRunComparisons(projectId, runId, { signal: controller.signal })
          : tab === "environment"
            ? fetchRunEnvironment(projectId, runId, { signal: controller.signal })
            : fetchRunGateDecisions(projectId, runId, { signal: controller.signal });
    read
      .then((value) => {
        if (live) setDetail({ status: "ready", value: value as TabPayload });
      })
      .catch((error: unknown) => {
        if (!live || controller.signal.aborted) return;
        setDetail(toPhase<TabPayload>(error));
      });
    return () => {
      live = false;
      controller.abort();
    };
  }, [projectId, runId, tab, reloadToken]);

  // The tab IS the address, so each one is a real anchor a person can copy or
  // middle-click. When the current address cannot produce one — the workbench
  // mounted outside its object route — the tab renders disabled rather than
  // pointing at something that would open a different screen.
  const tabs = useMemo<NavTab[]>(
    () =>
      EVALUATION_RUN_TABS.map((candidate) => {
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

  if (overview.status === "loading") {
    return (
      <p role="status" className="text-body text-text-secondary">
        Loading this Evaluation Run…
      </p>
    );
  }
  if (overview.status === "not-found") return <NotOpened what="This Evaluation Run" />;
  if (overview.status === "error") {
    return <ReadFailure what="This Evaluation Run" message={overview.message} retry={retry} />;
  }

  const run = overview.value;

  const body = () => {
    if (tab === "overview") return <OverviewTab overview={run} />;
    if (detail.status === "loading") {
      return (
        <p role="status" className="text-body text-text-secondary">
          Reading this tab…
        </p>
      );
    }
    if (detail.status === "not-found") return <NotOpened what="This tab" />;
    if (detail.status === "error") {
      return <ReadFailure what="This tab" message={detail.message} retry={retry} />;
    }
    if (tab === "cases") {
      return (
        <CasesTab
          projectId={projectId}
          cases={(detail.value as { cases: RunCase[] }).cases ?? []}
          focusId={focusId}
          onOpenOwner={onOpenOwner}
        />
      );
    }
    if (tab === "comparisons") {
      return (
        <ComparisonsTab
          comparisons={(detail.value as { comparisons: RunComparison[] }).comparisons ?? []}
        />
      );
    }
    if (tab === "environment") return <EnvironmentTab environment={detail.value as RunEnvironment} />;
    return (
      <GateDecisionTab
        decisions={(detail.value as { gate_decisions: GateDecision[] }).gate_decisions ?? []}
      />
    );
  };

  return (
    <Stack data-owner={`test/regression-runs/${runId}`}>
      <ObjectHeader name={run.id} source={`Evaluation Run · ${run.run_profile_id}`} />
      <Cluster>
        <Badge tone="neutral">{evidenceModeLabel(run.evidence_mode)}</Badge>
        <Badge tone="neutral">{run.lifecycle}</Badge>
        <span className="text-caption text-text-secondary">
          {run.lifecycle === "finalized"
            ? "Frozen: this run is immutable evidence and accepts no further case."
            : "Recording: cases may still be added, and no comparison can read it yet."}
        </span>
      </Cluster>
      <NavTabs label="Evaluation Run" tabs={tabs} current={tab} onNavigate={onNavigateTab} />
      {body()}
    </Stack>
  );
}
