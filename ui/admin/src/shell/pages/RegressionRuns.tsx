/**
 * Regression Runs — the Level 2 collection of the Test workspace's two
 * reproducible-evidence objects (Stories 51.2 and 51.4).
 *
 * This screen replaces an Epic 14 vestige that read `/api/eval/runs` and showed
 * `Latest score`, `Pass rate` and `Regressions to review` over an
 * undifferentiated list. Each of those three is the on-screen form of the merge
 * `analyze-and-test.md:210` forbids: `app.eval_runs` carries one compensating
 * `precision_pct`, and a pass rate over a mixed list of offline runs and
 * observed traces is a single trust score built from three evidence modes.
 *
 * What replaces it is the separation itself. **Offline** and **Observed Cohort**
 * are two sections, loaded by two independent requests, rendered from two
 * independent states. Nothing is computed across them — there is no variable in
 * this file that both sections contribute to, which is what makes "never merged"
 * a property of the code rather than a promise in a comment.
 *
 * An Observed Cohort has no object type in `shell/navigation.ts` (the section
 * declares `evaluation-run` and `trace-observation`, and only those). It is
 * therefore not addressable, and this screen does NOT invent an address for it:
 * a cohort is expanded in place and its members deep-link to the Trace
 * Observation workbench, which is a declared object route.
 *
 * Composed only from `ui/admin/src/ui/index.ts` — no stylesheet, no hex colour,
 * no literal spacing, no per-screen class prefix, no page width clamp.
 */
import { useCallback, useEffect, useState } from "react";

import { ApiError } from "../../lib/apiFetch";
import { stateLabel,
  ObjectId,
  Badge,
  Button,
  Cluster,
  EmptyState,
  Failure,
  Retry,
  Metric,
  PageHeader,
  StatusLegend,
  Panel,
  PanelHeader,
  SectionHeader,
  Stack,
  Status,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
  TableScroll,
  formatPercent,
} from "../../ui";
import {
  fetchActiveBaseline,
  fetchContextAdherence,
  listEvaluationRuns,
  listRunProfiles,
  type AdherenceBasis,
  type AdherenceOverview,
  type Baseline,
  type EvaluationRunSummary,
  type RunProfile,
} from "../../test/evaluationRunClient";
import {
  fetchObservedCohort,
  listObservedCohorts,
  type CohortDetail,
  type CohortSummary,
} from "../../test/observedEvidenceClient";
import { verdictLabel, verdictTone, evidenceModeLabel } from "../../test/testEvidence";

/** What a read did NOT return. Renamed from `Failure` in 76-4: the console's
 *  error BLOCK is now `Failure` from `ui/`, and one file cannot hold both. */
type ReadFailure = { kind: "not-found" } | { kind: "error"; message: string };

type OfflineState =
  | { status: "loading" }
  | { status: "ready"; runs: EvaluationRunSummary[]; profiles: RunProfile[]; baselines: Record<string, Baseline | null> }
  | ({ status: "failed" } & ReadFailure);

type ObservedState =
  | { status: "loading" }
  | { status: "ready"; cohorts: CohortSummary[] }
  | ({ status: "failed" } & ReadFailure);

type AdherenceState =
  | { status: "loading" }
  | { status: "ready"; overview: AdherenceOverview }
  | ({ status: "failed" } & ReadFailure);

function asFailure(error: unknown): ReadFailure {
  // Foreign, denied and absent answer identically by design; the screen repeats
  // that ambiguity rather than guessing which one it was.
  if (error instanceof ApiError && (error.status === 404 || error.unauthenticated)) {
    return { kind: "not-found" };
  }
  return { kind: "error", message: error instanceof Error ? error.message : String(error) };
}

function FailureNote({ what, failure, retry }: { what: string; failure: ReadFailure; retry: () => void }) {
  if (failure.kind === "not-found") {
    return (
      <Status as="block" tone="warning" title={`${what} was not opened`}>
        This Project has no such evidence available to you, or the Project does not exist. The two
        answer identically on purpose, and nothing has been shown in its place.
      </Status>
    );
  }
  // ONE ERROR SURFACE FOR THE CONSOLE (76-4). This branch was `Failure`'s shape
  // with the two halves `Failure` did not have -- a titled subject and a way to
  // ask again -- discovered here first. Both are in `ui/AsyncStates.tsx` now, so
  // the private copy is deleted rather than kept in step by hand.
  return (
    <Failure
      what={what}
      message={`${failure.message}. No run, cohort or figure has been fabricated to fill the section.`}
      action={<Retry onClick={retry} />}
    />
  );
}

// ---------------------------------------------------------------------------
// Offline — reproducible runs, their profiles and their explicitly approved
// baselines.
// ---------------------------------------------------------------------------

function OfflineSection({
  state,
  retry,
  onOpenEvaluationRun,
}: {
  state: OfflineState;
  retry: () => void;
  onOpenEvaluationRun?: (runId: string) => void;
}) {
  return (
    <Stack>
      <SectionHeader
        title="Offline"
        description="A fixed question-set version, a pinned data snapshot and an as-of date: reproducible, and the only evidence mode eligible to block."
      />

      {state.status === "loading" && (
        <p role="status" className="text-body text-text-secondary">
          Reading the offline Evaluation Runs…
        </p>
      )}
      {state.status === "failed" && (
        <FailureNote what="The offline evidence" failure={state} retry={retry} />
      )}

      {state.status === "ready" && (
        <>
          <Panel flush>
            <PanelHeader
              title="Run profiles and approved baselines"
              description="A baseline is an explicit approval of one finalized run. It is never moved by finalizing a newer one."
            />
            {state.profiles.length === 0 ? (
              <EmptyState
                title="No run profile"
                description="A run belongs to a named profile, and a baseline is approved for one profile. Neither exists in this Project yet: a profile appears here the first time an offline Evaluation Run is finalized against this Project, which is done from your LLM host, not from this screen."
              />
            ) : (
              <TableScroll label="Run profiles">
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead>Profile</TableHead>
                      <TableHead>Evidence mode</TableHead>
                      <TableHead>Approved baseline</TableHead>
                      <TableHead>Approved by</TableHead>
                      <TableHead>Reason</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {state.profiles.map((profile) => {
                      const baseline = state.baselines[profile.id] ?? null;
                      return (
                        <TableRow key={profile.id}>
                          <TableCell className="font-semibold text-text">
                            {profile.name}
                            <span className="block text-caption text-text-secondary"><ObjectId value={profile.id} title="Run profile" /></span>
                          </TableCell>
                          <TableCell>
                            <Badge tone="neutral">{evidenceModeLabel(profile.evidence_mode)}</Badge>
                          </TableCell>
                          <TableCell className="text-technical break-all">
                            {baseline ? (
                              baseline.run_id
                            ) : (
                              <Status tone="neutral" data-testid={`baseline-${profile.id}`}>
                                None approved
                              </Status>
                            )}
                          </TableCell>
                          <TableCell>{baseline?.approved_by ?? "—"}</TableCell>
                          <TableCell>{baseline?.approval_reason ?? "No approval has been recorded"}</TableCell>
                        </TableRow>
                      );
                    })}
                  </TableBody>
                </Table>
              </TableScroll>
            )}
          </Panel>

          <Panel flush>
            <PanelHeader
              title="Offline Evaluation Runs"
              description="Every run states its own case count and its own unresolved pins. No column ranks one run against another."
            />
            {state.runs.length === 0 ? (
              <EmptyState
                title="No offline Evaluation Run"
                description="Nothing has been read from the repository evaluation corpus in its place: that record is test code, and a run built from it would not pin the environment it claims to describe."
              />
            ) : (
              <TableScroll label="Offline Evaluation Runs">
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead>Run</TableHead>
                      <TableHead>Profile</TableHead>
                      <TableHead>Lifecycle</TableHead>
                      <TableHead>As of</TableHead>
                      <TableHead>Cases</TableHead>
                      <TableHead>Unresolved pins</TableHead>
                      <TableHead>Question set</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {state.runs.map((run) => (
                      <TableRow key={run.id}>
                        <TableCell className="font-semibold text-text">
                          {onOpenEvaluationRun ? (
                            <Button variant="link" size="sm" onClick={() => onOpenEvaluationRun(run.id)}>
                              <ObjectId value={run.id} title="Run" />
                            </Button>
                          ) : (
                            <ObjectId value={run.id} title="Run" />
                          )}
                        </TableCell>
                        <TableCell>{run.run_profile}</TableCell>
                        <TableCell>
                          <Badge tone="neutral">{run.lifecycle}</Badge>
                        </TableCell>
                        <TableCell>{run.as_of ?? "Unavailable"}</TableCell>
                        <TableCell>{run.case_count}</TableCell>
                        <TableCell>
                          {run.unresolved_pin_count > 0 ? (
                            <Status tone="neutral">{run.unresolved_pin_count} declared</Status>
                          ) : (
                            "None"
                          )}
                        </TableCell>
                        <TableCell className="text-technical break-all">
                          {run.question_set_fingerprint ?? "Not fingerprinted until finalized"}
                        </TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              </TableScroll>
            )}
          </Panel>
        </>
      )}
    </Stack>
  );
}

// ---------------------------------------------------------------------------
// Observed Cohort — a reference window. Never a baseline, never blocking.
// ---------------------------------------------------------------------------

function CohortMembers({
  detail,
  onOpenTraceObservation,
}: {
  detail: CohortDetail;
  onOpenTraceObservation?: (aiPathId: string) => void;
}) {
  if (detail.members.length === 0) {
    return (
      <EmptyState
        title="This cohort resolved to no member"
        description="The frozen selection is empty. It is kept as it was resolved rather than re-run, because a cohort that refreshed itself would change the denominator of every figure already read from it."
      />
    );
  }
  // THE KEY SITS WITH THE MARKS IT EXPLAINS, and lists only the verdicts these
  // members carry. It was on the page header, three entries fixed, on a screen
  // whose verdict marks live two components down — so a cohort where everything
  // passed still explained what a failure looks like. `unverifiable` is neutral
  // rather than amber or red because NOTHING WAS JUDGED, and the open dotted
  // ring is the one shape a reader cannot mistake for a result
  // (`analyze-and-test.md:282-284`); when a cohort carries one, the key says so.
  const verdictMeaning: Record<string, { label: string; meaning: string }> = {
    success: { label: "Pass", meaning: "the case was judged and met its expectation" },
    error: { label: "Fail", meaning: "the case was judged and did not meet it" },
    neutral: { label: "Unverifiable", meaning: "nothing was judged — no score is implied in either direction" },
  };
  const verdicts = new Set(detail.members.map((member) => verdictTone(member.assessment.verdict)));
  const legend = [...verdicts]
    .filter((tone) => tone in verdictMeaning)
    .map((tone) => ({ tone, ...verdictMeaning[tone] }));

  return (
    <Stack>
    <StatusLegend label="What a verdict mark means" entries={legend} />
    <TableScroll label={`Observations in cohort ${detail.id}`}>
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>Observation</TableHead>
            <TableHead>Observed at</TableHead>
            <TableHead>Path evidence</TableHead>
            <TableHead>Path verdict</TableHead>
            <TableHead>Result</TableHead>
            <TableHead>Render</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {detail.members.map((member) => (
            <TableRow key={member.id}>
              <TableCell className="font-semibold text-text">
                {onOpenTraceObservation ? (
                  <Button
                    variant="link"
                    size="sm"
                    onClick={() => onOpenTraceObservation(member.ai_path_id)}
                  >
                    <ObjectId value={member.ai_path_id} title="AI Path" />
                  </Button>
                ) : (
                  <ObjectId value={member.ai_path_id} title="AI Path" />
                )}
              </TableCell>
              <TableCell>{member.observed_at ?? "Unavailable"}</TableCell>
              <TableCell>{stateLabel(member.path_evidence_state)}</TableCell>
              <TableCell>
                <Status tone={verdictTone(member.assessment.verdict)}>
                  {verdictLabel(member.assessment.verdict)}
                </Status>
              </TableCell>
              <TableCell className="text-technical break-all">
                {member.query_result_id ?? "No Result pinned"}
              </TableCell>
              <TableCell>
                {/* The rendered artifact has no owner. Its pin is held NULL by the
                    schema, so the only reachable state is Unverifiable. */}
                <Status tone="neutral" data-testid={`render-${member.id}`}>
                  {verdictLabel(member.render.state)}
                </Status>
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </TableScroll>
    </Stack>
  );
}

function ObservedSection({
  projectId,
  state,
  retry,
  onOpenTraceObservation,
}: {
  projectId: string;
  state: ObservedState;
  retry: () => void;
  onOpenTraceObservation?: (aiPathId: string) => void;
}) {
  const [openId, setOpenId] = useState<string | null>(null);
  const [detail, setDetail] = useState<CohortDetail | null>(null);
  const [detailFailure, setDetailFailure] = useState<ReadFailure | null>(null);

  useEffect(() => {
    if (!openId) {
      setDetail(null);
      setDetailFailure(null);
      return;
    }
    const controller = new AbortController();
    let live = true;
    setDetail(null);
    setDetailFailure(null);
    fetchObservedCohort(projectId, openId, { signal: controller.signal })
      .then((value) => {
        if (live) setDetail(value);
      })
      .catch((error: unknown) => {
        if (!live || controller.signal.aborted) return;
        setDetailFailure(asFailure(error));
      });
    return () => {
      live = false;
      controller.abort();
    };
  }, [projectId, openId]);

  return (
    <Stack>
      <SectionHeader
        title="Observed Cohort"
        description="Authorized real traces, frozen by an explicit window, scope and version selection. It detects drift and proposes Golden Questions; it can never block."
      />

      <Status as="block" tone="neutral" title="A reference window, not a baseline">
        An Observed Cohort preserves the environment that was actually observed, so it cannot be
        approved as a baseline and cannot be compared as if it were reproducible. Nothing in this
        section is counted together with the Offline section above.
      </Status>

      {state.status === "loading" && (
        <p role="status" className="text-body text-text-secondary">
          Reading the Observed Cohorts…
        </p>
      )}
      {state.status === "failed" && (
        <FailureNote what="The observed evidence" failure={state} retry={retry} />
      )}

      {state.status === "ready" && (
        <Panel flush>
          <PanelHeader
            title="Observed Cohorts"
            description="Each cohort carries its own member count. That count is the denominator of every figure read from it, and it is never added to another cohort's."
          />
          {state.cohorts.length === 0 ? (
            <EmptyState
              title="No Observed Cohort"
              description="No authorized real trace has been frozen into a cohort in this Project. An empty section is the honest state; a sample cohort would be evidence about nothing."
            />
          ) : (
            <TableScroll label="Observed Cohorts">
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Cohort</TableHead>
                    <TableHead>Window</TableHead>
                    <TableHead>Members</TableHead>
                    <TableHead>Blocking</TableHead>
                    <TableHead>Filter fingerprint</TableHead>
                    <TableHead>Observations</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {state.cohorts.map((cohort) => (
                    <TableRow key={cohort.id}>
                      <TableCell className="font-semibold text-text">
                        {cohort.label ?? cohort.id}
                        <span className="block text-caption text-text-secondary"><ObjectId value={cohort.id} title="Cohort" /></span>
                      </TableCell>
                      <TableCell>
                        {cohort.window_start ?? "Unavailable"} → {cohort.window_end ?? "Unavailable"}
                      </TableCell>
                      <TableCell>{cohort.member_count}</TableCell>
                      <TableCell>
                        <Status tone="neutral" data-testid={`blocking-${cohort.id}`}>
                          {cohort.blocking ? "Blocking" : "Non-blocking"}
                        </Status>
                      </TableCell>
                      <TableCell className="text-technical break-all">
                        {cohort.filter_hash ?? "Unavailable"}
                      </TableCell>
                      <TableCell>
                        <Button
                          variant="secondary"
                          size="sm"
                          onClick={() => setOpenId(openId === cohort.id ? null : cohort.id)}
                        >
                          {openId === cohort.id ? "Hide observations" : "Show observations"}
                        </Button>
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </TableScroll>
          )}

          {openId && detailFailure && (
            <div className="p-5">
              <FailureNote
                what="This cohort"
                failure={detailFailure}
                retry={() => setOpenId(openId)}
              />
            </div>
          )}
          {openId && !detailFailure && !detail && (
            <p role="status" className="p-5 text-body text-text-secondary">
              Reading the frozen membership…
            </p>
          )}
          {openId && detail && (
            <div className="border-t border-divider-base">
              <PanelHeader
                title={detail.label ?? detail.id}
                description={detail.evidence_label}
              />
              <CohortMembers detail={detail} onOpenTraceObservation={onOpenTraceObservation} />
            </div>
          )}
        </Panel>
      )}
    </Stack>
  );
}

// ---------------------------------------------------------------------------
// The collection.
// ---------------------------------------------------------------------------

// ---------------------------------------------------------------------------
// Context adherence — the measure that was written since migration 034 and read
// by nothing.
//
// It answers one question: when this Project was asked a data question, had the
// governed context been consulted first. It is NOT the `context_adherence`
// dimension of a run — that judges one pinned execution against one question —
// and the two are deliberately not shown as one figure.
//
// Every count is rendered WITH its denominator ("3 of 4"), never as a bare
// percentage: a share over four observations reads exactly like a share over
// four hundred, and this section is the first place in the console where that
// confusion could start.
//
// AND NO FIGURE HERE SPANS THE TWO KINDS OF EVIDENCE. A row labelled "All
// questions" stood at the top of the first table and rendered the server's
// pooled total, so the headline figure a reader met FIRST was one wall-clock
// inference added to the trace-identified measures — the exact merge
// `analyze-and-test.md:1330` forbids. It is gone, both here and in the payload;
// the per-basis table below carries the totals, apart.
// ---------------------------------------------------------------------------

/** How each basis is named on screen. One place, so two tables cannot disagree. */
const BASIS_LABEL: Record<AdherenceBasis, string> = {
  observed_session: "Observed exchange",
  inferred_window: "Inferred",
};

function BasisBadge({ basis }: { basis: AdherenceBasis }) {
  return (
    <Badge tone={basis === "observed_session" ? "neutral" : "warning"}>
      {BASIS_LABEL[basis]}
    </Badge>
  );
}

/** "3 of 4" — the denominator is not optional, and never a lone percentage. */
function OutOf({ adherent, observations }: { adherent: number; observations: number }) {
  return (
    <>
      <span className="font-semibold text-text">
        {adherent} of {observations}
      </span>
      {observations > 0 && (
        <span className="block text-caption text-text-secondary">
          {formatPercent(adherent / observations)} consulted context first
        </span>
      )}
    </>
  );
}

function AdherenceSection({ state, retry }: { state: AdherenceState; retry: () => void }) {
  return (
    <Stack>
      <SectionHeader
        title="Context adherence"
        description="When someone asked this Project a data question, had the governed context been consulted first? Measured, never enforced — a question that skipped the context is still answered."
      />

      {state.status === "loading" && (
        <p role="status" className="text-body text-text-secondary">
          Reading what the pre-query gate measured…
        </p>
      )}
      {state.status === "failed" && (
        <FailureNote what="The adherence measure" failure={state} retry={retry} />
      )}

      {state.status === "ready" && state.overview.empty_state && (
        <EmptyState
          title={state.overview.empty_state.headline}
          description={`${state.overview.empty_state.detail} ${state.overview.empty_state.next_step}`}
        />
      )}

      {state.status === "ready" && !state.overview.empty_state && (
        <Panel flush>
          <PanelHeader
            title={`Last ${state.overview.window.days} days`}
            description={`${state.overview.observations} question${state.overview.observations === 1 ? "" : "s"} measured. Two kinds of evidence, reported apart: one is known to be a single exchange, the other is an approximation. There is no combined figure — pooling them would let an inference read as a measure.`}
          />
          <TableScroll label="Context adherence by kind of evidence">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Evidence</TableHead>
                  <TableHead>Consulted context first</TableHead>
                  <TableHead>What it proves</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {state.overview.by_basis.map((bucket) => (
                  <TableRow key={bucket.basis}>
                    <TableCell>
                      <BasisBadge basis={bucket.basis} />
                    </TableCell>
                    <TableCell>
                      <OutOf adherent={bucket.adherent} observations={bucket.observations} />
                    </TableCell>
                    <TableCell className="text-text-secondary">{bucket.means}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </TableScroll>
          <TableScroll label="Context adherence by data question and kind of evidence">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Data question</TableHead>
                  <TableHead>Evidence</TableHead>
                  <TableHead>Consulted context first</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {state.overview.by_data_tool.map((bucket) => (
                  <TableRow key={`${bucket.data_tool}:${bucket.basis}`}>
                    <TableCell className="text-technical break-all">{bucket.data_tool}</TableCell>
                    <TableCell>
                      <BasisBadge basis={bucket.basis} />
                    </TableCell>
                    <TableCell>
                      <OutOf adherent={bucket.adherent} observations={bucket.observations} />
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </TableScroll>
        </Panel>
      )}
    </Stack>
  );
}

export default function RegressionRuns({
  projectId,
  onOpenEvaluationRun,
  onOpenTraceObservation,
}: {
  projectId?: string;
  /** Supplied by the shell. Without it the rows stay readable rather than being
   *  dressed as links to a screen nothing would open. */
  onOpenEvaluationRun?: (runId: string) => void;
  onOpenTraceObservation?: (aiPathId: string) => void;
}) {
  const [offline, setOffline] = useState<OfflineState>({ status: "loading" });
  const [observed, setObserved] = useState<ObservedState>({ status: "loading" });
  const [adherence, setAdherence] = useState<AdherenceState>({ status: "loading" });
  const [offlineToken, setOfflineToken] = useState(0);
  const [observedToken, setObservedToken] = useState(0);
  const [adherenceToken, setAdherenceToken] = useState(0);

  // Two loads, two states, no shared variable. A single request feeding both
  // sections is how a figure ends up computed across evidence modes.
  useEffect(() => {
    if (!projectId) return;
    const controller = new AbortController();
    let live = true;
    setOffline({ status: "loading" });
    Promise.all([
      listEvaluationRuns(projectId, "offline", { signal: controller.signal }),
      listRunProfiles(projectId, { signal: controller.signal }),
    ])
      .then(async ([collection, profiles]) => {
        // The payload is CHECKED, not trusted. Without this, a response that
        // carries no `run_profiles` threw `Cannot read properties of undefined
        // (reading 'filter')` -- and this screen's honest refusal surface then
        // printed that TypeError verbatim to the person, under "The offline
        // evidence could not be read". A stack message is not an explanation,
        // and it was only visible once the screen could be captured at all.
        //
        // Every other reader in this console already refuses a shape it does
        // not recognise rather than guessing (the `/sample` echo check, the Data
        // envelope check). This one trusted.
        if (!Array.isArray(profiles?.run_profiles)) {
          throw new Error(
            "The run-profile response did not carry a profile list, so no offline evidence can be read.",
          );
        }
        const offlineProfiles = profiles.run_profiles.filter(
          (profile) => profile.evidence_mode === "offline",
        );
        const approvals = await Promise.all(
          offlineProfiles.map((profile) =>
            fetchActiveBaseline(projectId, profile.id, { signal: controller.signal })
              .then((answer) => [profile.id, answer.baseline] as const)
              // A profile whose baseline cannot be read reports "none approved"
              // rather than borrowing another profile's approval.
              .catch(() => [profile.id, null] as const),
          ),
        );
        if (!live) return;
        setOffline({
          status: "ready",
          runs: collection.evaluation_runs ?? [],
          profiles: profiles.run_profiles ?? [],
          baselines: Object.fromEntries(approvals),
        });
      })
      .catch((error: unknown) => {
        if (!live || controller.signal.aborted) return;
        setOffline({ status: "failed", ...asFailure(error) });
      });
    return () => {
      live = false;
      controller.abort();
    };
  }, [projectId, offlineToken]);

  useEffect(() => {
    if (!projectId) return;
    const controller = new AbortController();
    let live = true;
    setObserved({ status: "loading" });
    listObservedCohorts(projectId, { signal: controller.signal })
      .then((value) => {
        if (live) setObserved({ status: "ready", cohorts: value.cohorts ?? [] });
      })
      .catch((error: unknown) => {
        if (!live || controller.signal.aborted) return;
        setObserved({ status: "failed", ...asFailure(error) });
      });
    return () => {
      live = false;
      controller.abort();
    };
  }, [projectId, observedToken]);

  // A third load, a third state. Same rule as the two above: no variable is
  // shared, so nothing can be computed across evidence modes by accident.
  useEffect(() => {
    if (!projectId) return;
    const controller = new AbortController();
    let live = true;
    setAdherence({ status: "loading" });
    fetchContextAdherence(projectId, undefined, { signal: controller.signal })
      .then((overview) => {
        if (live) setAdherence({ status: "ready", overview });
      })
      .catch((error: unknown) => {
        if (!live || controller.signal.aborted) return;
        setAdherence({ status: "failed", ...asFailure(error) });
      });
    return () => {
      live = false;
      controller.abort();
    };
  }, [projectId, adherenceToken]);

  const retryOffline = useCallback(() => setOfflineToken((token) => token + 1), []);
  const retryObserved = useCallback(() => setObservedToken((token) => token + 1), []);
  const retryAdherence = useCallback(() => setAdherenceToken((token) => token + 1), []);

  const header = (
    <PageHeader
      title="Regression Runs"
      description="Execute or observe a version-pinned cohort and compare it with an approved baseline. Offline and observed evidence stay separate, here and everywhere."
    />
  );

  if (!projectId) {
    return (
      <Stack>
        {header}
        <Status as="block" tone="warning" title="Select a Project">
          Evaluation evidence is Project-scoped. Nothing has been read, and no other Project's runs
          have been shown in its place.
        </Status>
      </Stack>
    );
  }

  return (
    <Stack>
      {header}

      <Status as="block" tone="neutral" title="Three evidence modes, never one score">
        Offline runs, observed trace cohorts and user feedback answer different questions and are
        never merged into a trust score. This screen therefore reports no pass rate, no latest score
        and no figure computed across the two sections below. Each run states its verdicts per
        dimension, each cohort states its own denominator.
      </Status>

      <Panel className="grid gap-2 p-2 md:grid-cols-2">
        <Metric
          label="Offline Evaluation Runs"
          value={offline.status === "ready" ? offline.runs.length : "Unavailable"}
          hint="Reproducible, eligible to block"
        />
        <Metric
          label="Observed Cohorts"
          value={observed.status === "ready" ? observed.cohorts.length : "Unavailable"}
          hint="Reference windows, never blocking"
        />
      </Panel>

      <OfflineSection
        state={offline}
        retry={retryOffline}
        onOpenEvaluationRun={onOpenEvaluationRun}
      />

      <ObservedSection
        projectId={projectId}
        state={observed}
        retry={retryObserved}
        onOpenTraceObservation={onOpenTraceObservation}
      />

      <AdherenceSection state={adherence} retry={retryAdherence} />

      <Cluster>
        <span className="text-caption text-text-secondary">
          An Observed Cohort has no object route in this workspace: `shell/navigation.ts` declares
          `evaluation-run` and `trace-observation`, and no address has been invented for a third.
        </span>
      </Cluster>
    </Stack>
  );
}
