/**
 * The Feedback Review Workbench — Level 3, five tabs (Story 65.8).
 *
 * `docs/product-architecture/analyze-and-test.md:294` contracts exactly these
 * five, in this order: Feedback, Result & Render, AI Path, Classification,
 * Resolution. They are the tabs declared on the `feedback-review` contract in
 * `shell/navigation.ts`, and they replace the honest
 * `RouteState kind="unavailable"` this address used to fall through to.
 *
 * Three rules this screen holds:
 *
 *   - **Polarity, human review and automated verdicts stay separate.** Each is
 *     named on its own server-owned evidence surface. Merging them would let a
 *     person's sentiment or opinion read as a correctness measurement.
 *   - **A review is appended, never edited.** Saving writes the next immutable
 *     version and advances the head; the version being read stays exactly as it
 *     was written, because a rewritten review destroys the only evidence that
 *     the conclusion was ever different.
 *   - **An absence is stated, not filled.** Retained Render evidence is shown
 *     when pinned; genuinely absent pins remain explicitly unavailable.
 *
 * Composed only from `ui/admin/src/ui/index.ts`.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { ApiError } from "../../lib/apiFetch";
import { Badge, Button, Cluster, EmptyState, Field, NativeSelect, NavTabs, ObjectHeader, ObjectId, Panel, PanelBody, PanelHeader, SectionHeader, Stack, stateLabel, Status, Table, TableBody, TableCell, TableHead, TableHeader, TableRow, TableScroll, Textarea, type NavTab, wireWord } from "../../ui";
import { AFFECTED_DIMENSIONS, appendFeedbackReview, emptyReviewDraft, FEEDBACK_REVIEW_TABS, fetchFeedbackAnnotation, HUMAN_VERDICTS, listFeedbackReviews, REVIEW_STATES, SEVERITIES, type EvidenceLens, type FeedbackAnnotation, type OwnerLink, type ReviewDraft } from "../../test/feedbackReviewClient";
import { dimensionLabel, refusalsOf, reviewStateLabel, type Refusal, verdictLabel, verdictTone } from "../../test/testEvidence";
import FeedbackRegressionPromotion from "../../test/FeedbackRegressionPromotion";
import { buildPath, useRoute } from "../router";

type Phase =
  | { status: "loading" }
  | { status: "ready"; annotation: FeedbackAnnotation }
  | { status: "not-found" }
  | { status: "error"; message: string };

/** The server's structured refusal, shown exactly as it was written. */
export function RefusalList({
  title,
  message,
  refusals,
}: {
  title: string;
  message: string;
  refusals: Refusal[];
}) {
  return (
    <Status as="block" tone="error" title={title} data-testid="feedback-review-refusal">
      <p className="m-0">{message}</p>
      {refusals.length > 0 && (
        <ul className="mt-2 list-disc pl-5">
          {refusals.map((refusal, index) => (
            <li key={`${refusal.code}-${index}`} className="text-ui">
              <code className="text-technical">{refusal.subject ?? refusal.code}</code> —{" "}
              {refusal.message}
            </li>
          ))}
        </ul>
      )}
    </Status>
  );
}

function LensStatus({ lens, testId }: { lens: EvidenceLens | undefined; testId: string }) {
  return (
    <>
      <Status tone="neutral" data-testid={testId}>
        {verdictLabel(lens?.state ?? "unverifiable")}
      </Status>
      <span className="block text-caption text-text-secondary">
        {lens?.reason ?? "owner_not_delivered"}
        {(lens?.owner_stories ?? []).length > 0
          ? ` — Story ${(lens?.owner_stories ?? []).join(", ")}`
          : ""}
      </span>
    </>
  );
}

function ownerLabel(link: OwnerLink): string {
  return `${link.object_type} ${link.object_id}${link.version_id ? ` v${link.version_id}` : ""}`;
}

function mintRetryKey(): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return crypto.randomUUID();
  }
  return `review-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

function technicalValue(value: unknown, unavailable = "Unavailable"): string {
  if (value === null || value === undefined || value === "") return unavailable;
  if (typeof value === "string" || typeof value === "number" || typeof value === "boolean") {
    return String(value);
  }
  try {
    return JSON.stringify(value);
  } catch {
    return unavailable;
  }
}

function missingVisibleVersionPins(annotation: FeedbackAnnotation): string[] {
  const expected: Array<[string, unknown]> = [
    ["result_id", annotation.result.id],
    ["result_content_hash", annotation.result.content_hash],
    ["query_spec_version_id", annotation.result.query_spec_version_id],
    ["semantic_view_id", annotation.semantic_view.id],
    ["semantic_view_version_id", annotation.semantic_view.version_id],
    ["ai_path_id", annotation.ai_path],
  ];
  if (annotation.render) {
    expected.push(...Object.entries(annotation.render));
  }
  return expected
    .filter(([name, pinned]) => pinned !== null && pinned !== undefined && annotation.visible_versions[name] == null)
    .map(([name]) => name);
}

function OwnerLinks({
  annotation,
  onOpenOwner,
}: {
  annotation: FeedbackAnnotation;
  onOpenOwner?: (link: OwnerLink) => void;
}) {
  return (
    <Panel flush>
      <PanelHeader
        title="Owners"
        description="Built only from identities this annotation actually pinned. Test annotates their versions; it does not duplicate their edit surfaces."
      />
      <TableScroll label="Owner links">
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Target</TableHead>
              <TableHead>Identity</TableHead>
              <TableHead>Open</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {annotation.owner_links.map((link) => (
              <TableRow key={`${link.object_type}-${link.object_id}`}>
                <TableCell className="font-semibold text-text">
                  {link.workspace} / {link.section}
                </TableCell>
                <TableCell className="text-technical break-all">{ownerLabel(link)}</TableCell>
                <TableCell>
                  {onOpenOwner ? (
                    <Button variant="link" size="sm" onClick={() => onOpenOwner(link)}>
                      Open owner
                    </Button>
                  ) : (
                    <span className="text-caption text-text-secondary">
                      No navigation was supplied to this workbench
                    </span>
                  )}
                </TableCell>
              </TableRow>
            ))}
            {annotation.unavailable_owner_links.map((link) => (
              <TableRow key={`missing-${link.target}`}>
                <TableCell className="font-semibold text-text">{link.target}</TableCell>
                <TableCell>
                  <LensStatus lens={link} testId={`unavailable-owner-${link.target}`} />
                </TableCell>
                <TableCell className="text-caption text-text-secondary">{link.detail}</TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </TableScroll>
    </Panel>
  );
}

// ---------------------------------------------------------------------------
// Feedback.
// ---------------------------------------------------------------------------

function FeedbackTab({ annotation }: { annotation: FeedbackAnnotation }) {
  const missingPins = missingVisibleVersionPins(annotation);
  return (
    <Stack>
      <Panel flush>
        <PanelHeader
          title="The annotation"
          description="What the person said, who they were, and on which surface."
        />
        <TableScroll label="The annotation">
          <Table>
            <TableBody>
              <TableRow>
                <TableCell className="font-semibold text-text">Source</TableCell>
                <TableCell>{annotation.source ?? annotation.actor_source ?? "authenticated"}</TableCell>
              </TableRow>
              <TableRow>
                <TableCell className="font-semibold text-text">Target contract</TableCell>
                <TableCell>{annotation.target_schema_version ?? "Historical / unpinned"}</TableCell>
              </TableRow>
              <TableRow>
                <TableCell className="font-semibold text-text">Polarity</TableCell>
                <TableCell>
                  <Status
                    tone={annotation.polarity === "positive" ? "success" : "error"}
                    data-testid="annotation-polarity"
                  >
                    {annotation.polarity}
                  </Status>
                </TableCell>
              </TableRow>
              <TableRow>
                <TableCell className="font-semibold text-text">Comment</TableCell>
                <TableCell className="max-w-[70ch]">{annotation.comment ?? "No comment"}</TableCell>
              </TableRow>
              <TableRow>
                <TableCell className="font-semibold text-text">Author</TableCell>
                <TableCell>
                  {annotation.actor === null
                    ? "Anonymous Share recipient"
                    : technicalValue(annotation.actor)}
                  {annotation.actor_source ? ` (${technicalValue(annotation.actor_source)})` : ""}
                </TableCell>
              </TableRow>
              <TableRow>
                <TableCell className="font-semibold text-text">Interaction</TableCell>
                <TableCell className="text-technical break-all">
                  {technicalValue(annotation.interaction_ref)}
                </TableCell>
              </TableRow>
              <TableRow>
                <TableCell className="font-semibold text-text">Surface</TableCell>
                <TableCell>{annotation.observed_surface}</TableCell>
              </TableRow>
              <TableRow>
                <TableCell className="font-semibold text-text">Observed at</TableCell>
                <TableCell>{annotation.observed_at ?? "Unavailable"}</TableCell>
              </TableRow>
              <TableRow>
                <TableCell className="font-semibold text-text">W3C trace id</TableCell>
                <TableCell className="text-technical break-all">
                  {annotation.w3c_trace_id ?? "Unavailable"}
                  <span className="block text-caption text-text-secondary">
                    Correlation, never identity.
                  </span>
                </TableCell>
              </TableRow>
            </TableBody>
          </Table>
        </TableScroll>
      </Panel>

      <Panel flush>
        <PanelHeader
          title="Versions visible at that moment"
          description="What the person could SEE. The authority stays the pinned owner rows; when the two disagree, both are shown rather than one silently preferred."
        />
        {annotation.version_divergence.length > 0 ? (
          <TableScroll label="Version divergence">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Field</TableHead>
                  <TableHead>Visible to the person</TableHead>
                  <TableHead>Pinned by the record</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {annotation.version_divergence.map((entry) => (
                  <TableRow key={entry.field}>
                    <TableCell className="font-semibold text-text">{entry.field}</TableCell>
                    <TableCell className="text-technical break-all">{technicalValue(entry.visible)}</TableCell>
                    <TableCell className="text-technical break-all">{technicalValue(entry.pinned)}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </TableScroll>
        ) : missingPins.length > 0 || !annotation.visible_versions_hash ? (
          <PanelBody>
            <Status tone="warning" data-testid="visible-versions-incomplete">
              Visible-version snapshot incomplete
            </Status>
            <p className="mt-2 mb-0 text-caption text-text-secondary">
              No divergence was recorded, but the snapshot cannot prove a complete match. Missing: {missingPins.join(", ") || "snapshot hash"}.
            </p>
          </PanelBody>
        ) : (
          <PanelBody>
            <Status tone="success" data-testid="no-divergence">
              The versions visible on screen match the versions this annotation pins.
            </Status>
            <p className="mt-2 mb-0 text-caption text-text-secondary">
              Snapshot hash {annotation.visible_versions_hash ?? "unavailable"}.
            </p>
          </PanelBody>
        )}
      </Panel>
    </Stack>
  );
}

// ---------------------------------------------------------------------------
// Result & Render.
// ---------------------------------------------------------------------------

function ResultRenderTab({ annotation }: { annotation: FeedbackAnnotation }) {
  return (
    <Stack>
      <Panel flush>
        <PanelHeader
          title="Result"
          description="The exact immutable Result this annotation judges."
        />
        <TableScroll label="Pinned Result">
          <Table>
            <TableBody>
              <TableRow>
                <TableCell className="font-semibold text-text">Result</TableCell>
                <TableCell><ObjectId value={annotation.result.id} title="Result" /></TableCell>
              </TableRow>
              <TableRow>
                <TableCell className="font-semibold text-text">Outcome</TableCell>
                <TableCell>{annotation.result.outcome ?? "Unavailable"}</TableCell>
              </TableRow>
              <TableRow>
                <TableCell className="font-semibold text-text">Result content hash</TableCell>
                <TableCell className="text-technical break-all">
                  {annotation.result.content_hash ?? "Unavailable"}
                </TableCell>
              </TableRow>
              <TableRow>
                <TableCell className="font-semibold text-text">Query Spec version</TableCell>
                <TableCell className="text-technical break-all">
                  {annotation.result.query_spec_version_id ?? "Unavailable"}
                </TableCell>
              </TableRow>
              <TableRow>
                <TableCell className="font-semibold text-text">Semantic View version</TableCell>
                <TableCell className="text-technical break-all">
                  {annotation.semantic_view.version_id ?? "Unavailable"}
                </TableCell>
              </TableRow>
            </TableBody>
          </Table>
        </TableScroll>
      </Panel>

      <Panel flush data-testid="render-evidence">
        <PanelHeader
          title="Render"
          description="The retained Render when one exists, followed by the exact served Spec and build tuple."
          actions={<LensStatus lens={annotation.evidence_lenses?.render} testId="render-state" />}
        />
        {annotation.render ? (
          <TableScroll label="Pinned Render and build versions">
            <Table>
              <TableBody>
                {Object.entries(annotation.render).map(([name, value]) => (
                  <TableRow key={name}>
                    <TableCell className="font-semibold text-text">{name}</TableCell>
                    <TableCell className="text-technical break-all">
                      {value ?? "No retained Render"}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </TableScroll>
        ) : (
          <PanelBody>
            <Status tone="neutral">
              Unavailable · {annotation.evidence_lenses?.render?.reason ?? "render pins were not recorded"}
            </Status>
          </PanelBody>
        )}
      </Panel>

      <Panel flush>
        <PanelHeader
          title="Exact target"
          description="The exact answer, datum locator or path step the person reacted to."
          actions={<LensStatus lens={annotation.evidence_lenses?.datum_mark} testId="datum-state" />}
        />
        <PanelBody>
          {annotation.target?.kind === "datum" ? (
            <p className="m-0 text-technical">
              Datum · zero-based row {annotation.target.row_index} · field {annotation.target.field}
            </p>
          ) : annotation.target?.kind === "path_step" ? (
            <p className="m-0 text-technical">Path step · ordinal {annotation.target.ordinal}</p>
          ) : annotation.target?.kind === "answer" ? (
            <p className="m-0">The whole answer.</p>
          ) : (
            <p className="m-0 text-ui text-text-secondary">Historical / unpinned target.</p>
          )}
        </PanelBody>
      </Panel>
    </Stack>
  );
}

// ---------------------------------------------------------------------------
// AI Path.
// ---------------------------------------------------------------------------

function AiPathTab({
  annotation,
  onOpenTraceObservation,
}: {
  annotation: FeedbackAnnotation;
  onOpenTraceObservation?: (aiPathId: string) => void;
}) {
  const lens = annotation.evidence_lenses?.server_owned_path_evidence;
  const observed = lens?.state === "observed";
  return (
    <Panel flush>
      <PanelHeader
        title="AI Path"
        description="The server-owned path evidence for the execution this annotation judges."
        actions={
          observed ? (
            <Status tone="success" data-testid="path-state">
              Observed
            </Status>
          ) : (
            <LensStatus lens={lens} testId="path-state" />
          )
        }
      />
      <PanelBody className="grid gap-3">
        <SectionHeader title="Pinned path" />
        <p className="m-0 text-technical break-all">{annotation.ai_path ?? "Unavailable"}</p>
        {annotation.target?.kind === "path_step" && (
          <p className="m-0 text-technical">Targeted step ordinal {annotation.target.ordinal}</p>
        )}
        {observed && annotation.ai_path && onOpenTraceObservation && (
          <Cluster>
            <Button variant="secondary" size="sm" onClick={() => onOpenTraceObservation(annotation.ai_path as string)}>
              Open the Trace Observation
            </Button>
          </Cluster>
        )}
        {!observed && (
          <p className="m-0 max-w-[70ch] text-ui text-text-secondary">
            {lens?.detail ??
              "No server-owned path evidence exists for this execution, so nothing about the path used can be judged. A bare adherence flag would be Unverifiable, never a pass."}
          </p>
        )}
      </PanelBody>
    </Panel>
  );
}

// ---------------------------------------------------------------------------
// Classification — the append-only review history.
// ---------------------------------------------------------------------------

function ClassificationTab({
  annotation,
  onOpenOwner,
  loadMoreHistory,
  historyLoading,
}: {
  annotation: FeedbackAnnotation;
  onOpenOwner?: (link: OwnerLink) => void;
  loadMoreHistory: () => void;
  historyLoading: boolean;
}) {
  const versions = annotation.review.versions ?? [];
  const automated = annotation.automated_verdicts;
  return (
    <Stack>
      <Panel flush>
        <PanelHeader
          title="Classification axes"
          description="Read from the annotation, never invented by the review."
        />
        <TableScroll label="Classification axes">
          <Table>
            <TableBody>
              <TableRow>
                <TableCell className="font-semibold text-text">Semantic View</TableCell>
                <TableCell className="text-technical break-all">
                  {technicalValue(annotation.classification.semantic_view, "Unattributed")}
                </TableCell>
              </TableRow>
              <TableRow>
                <TableCell className="font-semibold text-text">Business Domain</TableCell>
                <TableCell className="text-technical break-all">
                  {technicalValue(annotation.classification.business_domains, "Unattributed")}
                </TableCell>
              </TableRow>
              <TableRow>
                <TableCell className="font-semibold text-text">Skills</TableCell>
                <TableCell className="text-technical break-all">
                  {technicalValue(annotation.classification.skills, "Unattributed")}
                </TableCell>
              </TableRow>
              <TableRow>
                <TableCell className="font-semibold text-text">Capability</TableCell>
                <TableCell className="text-technical break-all">
                  {technicalValue(annotation.classification.capability, "Unattributed")}
                </TableCell>
              </TableRow>
              <TableRow>
                <TableCell className="font-semibold text-text">Result type</TableCell>
                <TableCell className="text-technical break-all">
                  {technicalValue(annotation.classification.result_type, "Unattributed")}
                </TableCell>
              </TableRow>
            </TableBody>
          </Table>
        </TableScroll>
      </Panel>

      <Panel flush>
        <PanelHeader
          title="Review history"
          description="Append-only human review history. Automated verdicts remain in their own evidence table below."
        />
        {versions.length === 0 ? (
          <EmptyState
            title="This annotation has not been reviewed"
            description="Unreviewed is its own fact. It is not folded into a severity nobody chose."
          />
        ) : (
          <TableScroll label="Review history">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Version</TableHead>
                  <TableHead>Review state</TableHead>
                  <TableHead>Affected dimension</TableHead>
                  <TableHead>Human verdict</TableHead>
                  <TableHead>Severity</TableHead>
                  <TableHead>Reviewer</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {versions.map((version) => (
                  <TableRow key={version.id}>
                    <TableCell className="font-semibold text-text">
                      v{version.version_number}
                      {version.id === annotation.review.current_version_id && (
                        <Badge tone="accent" className="ml-2">
                          current
                        </Badge>
                      )}
                    </TableCell>
                    <TableCell>{reviewStateLabel(version.review_state)}</TableCell>
                    <TableCell>{dimensionLabel(version.affected_dimension)}</TableCell>
                    <TableCell data-testid={`human-verdict-${version.id}`}>
                      <Status tone={verdictTone(version.human_verdict)}>
                        {verdictLabel(version.human_verdict)}
                      </Status>
                      <span className="block text-caption text-text-secondary">
                        Human judgement
                      </span>
                    </TableCell>
                    <TableCell>{wireWord(version.severity)}</TableCell>
                    <TableCell>
                      {version.reviewer}
                      <span className="block text-caption text-text-secondary">
                        {version.created_at ?? "Unavailable"}
                      </span>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </TableScroll>
        )}
        {annotation.review.versions_truncated && annotation.review.versions_next_cursor && (
          <PanelBody>
            <Button variant="secondary" size="sm" disabled={historyLoading} onClick={loadMoreHistory}>
              {historyLoading ? "Loading review history…" : "Load earlier review versions"}
            </Button>
          </PanelBody>
        )}
        {annotation.review.versions_truncated && !annotation.review.versions_next_cursor && (
          <PanelBody>
            <Status tone="warning">Review history is truncated, but no continuation cursor was provided.</Status>
          </PanelBody>
        )}
      </Panel>

      <Panel flush>
        <PanelHeader
          title="Automated verdicts"
          description="Evaluation-owned verdicts matched to this exact evidence tuple. They do not change the user polarity or human review."
        />
        {automated.state !== "available" ? (
          <PanelBody>
            <Status tone="neutral" data-testid="automated-verdicts-unavailable">
              Automated verdicts unavailable
            </Status>
            <p className="mt-2 mb-0 text-caption text-text-secondary">
              {automated.reason ?? "No compatible evaluation verdict was recorded."}
            </p>
          </PanelBody>
        ) : automated.items.length === 0 ? (
          <EmptyState
            title="No compatible automated verdict"
            description="Exact evidence is available; no stored evaluation verdict matched this evidence tuple."
          />
        ) : (
          <TableScroll label="Automated verdicts">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Run / case</TableHead>
                  <TableHead>Objective dimension</TableHead>
                  <TableHead>Automated verdict</TableHead>
                  <TableHead>Reason</TableHead>
                  <TableHead>Evidence</TableHead>
                  <TableHead>Owner</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {automated.items.map((verdict) => (
                  <TableRow key={`${verdict.run_id}-${verdict.case_id}-${verdict.dimension}`}>
                    <TableCell className="text-technical break-all">
                      {verdict.run_id} / {verdict.case_id}
                      <span className="block text-caption text-text-secondary">
                        {verdict.created_at ?? "Unavailable"}
                      </span>
                    </TableCell>
                    <TableCell>{dimensionLabel(verdict.dimension)}</TableCell>
                    <TableCell
                      data-testid={`automated-verdict-${verdict.case_id}-${verdict.dimension}`}
                    >
                      <Status tone={verdictTone(verdict.verdict)}>
                        {verdictLabel(verdict.verdict)}
                      </Status>
                      <span className="block text-caption text-text-secondary">
                        Automated evaluation
                      </span>
                    </TableCell>
                    <TableCell>{verdict.reason_code}</TableCell>
                    <TableCell className="text-technical break-all">
                      {technicalValue(verdict.evidence_refs)}
                    </TableCell>
                    <TableCell>
                      {onOpenOwner ? (
                        <Button
                          variant="link"
                          size="sm"
                          onClick={() => onOpenOwner(verdict.owner_link)}
                        >
                          Open evaluation owner
                        </Button>
                      ) : (
                        <span className="text-technical break-all">
                          {ownerLabel(verdict.owner_link)}
                        </span>
                      )}
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </TableScroll>
        )}
        {automated.truncated && (
          <PanelBody className="border-t border-divider-base text-caption text-text-secondary">
            The server returned the first 50 exact matches; this evidence list is truncated.
          </PanelBody>
        )}
      </Panel>
    </Stack>
  );
}

// ---------------------------------------------------------------------------
// Resolution — append the next review version.
// ---------------------------------------------------------------------------

function ResolutionTab({
  draft,
  update,
  save,
  saving,
}: {
  draft: ReviewDraft;
  update: (patch: Partial<ReviewDraft>) => void;
  save: () => void;
  saving: boolean;
}) {
  return (
    <Stack>
      <Status as="block" tone="neutral" title="A review is appended, never edited">
        Saving writes the next immutable version and advances the head. The version being read stays
        exactly as it was written, because rewriting a conclusion destroys the evidence that it was
        ever different.
      </Status>

      <Panel flush>
        <PanelHeader
          title="Append a review version"
          description="Every field belongs to the closed command and is validated by the server against the ratified vocabularies. Empty or unknown values are refused; the console chooses no review decision for you."
          actions={
            <Button onClick={save} disabled={saving} data-testid="feedback-review-save">
              {saving ? "Appending…" : "Append review version"}
            </Button>
          }
        />
        <PanelBody className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
          <Field label="Review state" required>
            {(field) => (
              <NativeSelect
                {...field}
                value={draft.state}
                onChange={(event) => update({ state: event.target.value })}
              >
                <option value="">Not declared</option>
                {REVIEW_STATES.map((state) => (
                  <option key={state} value={state}>
                    {state}
                  </option>
                ))}
              </NativeSelect>
            )}
          </Field>
          <Field
            label="Affected dimension"
            required
            hint="One of the six objective dimensions, or `not_applicable`. Style, tone and layout taste are not dimensions."
          >
            {(field) => (
              <NativeSelect
                {...field}
                value={draft.affected_dimension}
                onChange={(event) => update({ affected_dimension: event.target.value })}
              >
                <option value="">Not declared</option>
                {AFFECTED_DIMENSIONS.map((dimension) => (
                  <option key={dimension} value={dimension}>
                    {dimensionLabel(dimension)}
                  </option>
                ))}
              </NativeSelect>
            )}
          </Field>
          <Field
            label="Human verdict"
            required
            hint="Yours, recorded as such. It is never merged with automated evaluation verdicts."
          >
            {(field) => (
              <NativeSelect
                {...field}
                value={draft.human_verdict}
                onChange={(event) => update({ human_verdict: event.target.value })}
              >
                <option value="">Not declared</option>
                {HUMAN_VERDICTS.map((verdict) => (
                  <option key={verdict} value={verdict}>
                    {verdictLabel(verdict)}
                  </option>
                ))}
              </NativeSelect>
            )}
          </Field>
          <Field label="Severity" required>
            {(field) => (
              <NativeSelect
                {...field}
                value={draft.severity}
                onChange={(event) => update({ severity: event.target.value })}
              >
                <option value="">Not declared</option>
                {SEVERITIES.map((severity) => (
                  <option key={severity} value={severity}>
                    {severity}
                  </option>
                ))}
              </NativeSelect>
            )}
          </Field>
        </PanelBody>
        <PanelBody className="grid gap-4 border-t border-divider-base">
          <Field label="Reason" required hint="What this conclusion rests on.">
            {(field) => (
              <Textarea
                {...field}
                value={draft.reason}
                onChange={(event) => update({ reason: event.target.value })}
              />
            )}
          </Field>
        </PanelBody>
      </Panel>

    </Stack>
  );
}

// ---------------------------------------------------------------------------
// The workbench.
// ---------------------------------------------------------------------------

export default function FeedbackReviewWorkbench({
  projectId,
  feedbackId,
  tab,
  onNavigateTab,
  onOpenOwner,
  onOpenTraceObservation,
}: {
  projectId: string;
  feedbackId: string;
  tab: string;
  onNavigateTab: (tab: string) => void;
  onOpenOwner?: (link: OwnerLink) => void;
  onOpenTraceObservation?: (aiPathId: string) => void;
}) {
  const { route } = useRoute();
  const [phase, setPhase] = useState<Phase>({ status: "loading" });
  const [draft, setDraft] = useState<ReviewDraft>(emptyReviewDraft());
  const [retryKey, setRetryKey] = useState(mintRetryKey);
  const [saving, setSaving] = useState(false);
  const [failure, setFailure] = useState<{
    title: string;
    message: string;
    refusals: Refusal[];
  } | null>(null);
  const [appended, setAppended] = useState<number | null>(null);
  const [reloadToken, setReloadToken] = useState(0);
  const [historyLoading, setHistoryLoading] = useState(false);
  const generationRef = useRef(0);
  const saveAbortRef = useRef<AbortController | null>(null);
  const historyAbortRef = useRef<AbortController | null>(null);

  useEffect(() => {
    generationRef.current += 1;
    saveAbortRef.current?.abort();
    historyAbortRef.current?.abort();
    setDraft(emptyReviewDraft());
    setRetryKey(mintRetryKey());
    setSaving(false);
    setHistoryLoading(false);
    setFailure(null);
    setAppended(null);
    setReloadToken(0);
  }, [projectId, feedbackId]);

  useEffect(() => () => {
    generationRef.current += 1;
    saveAbortRef.current?.abort();
    historyAbortRef.current?.abort();
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    const generation = generationRef.current;
    setPhase({ status: "loading" });
    fetchFeedbackAnnotation(projectId, feedbackId, { signal: controller.signal })
      .then((annotation) => {
        if (!controller.signal.aborted && generationRef.current === generation) {
          setPhase({ status: "ready", annotation });
        }
      })
      .catch((error: unknown) => {
        if (controller.signal.aborted || generationRef.current !== generation) return;
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
      controller.abort();
    };
  }, [projectId, feedbackId, reloadToken]);

  const tabs = useMemo<NavTab[]>(
    () =>
      FEEDBACK_REVIEW_TABS.map((candidate) => {
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

  const update = useCallback((patch: Partial<ReviewDraft>) => {
    setDraft((current) => ({ ...current, ...patch }));
  }, []);

  const save = useCallback(async () => {
    if (phase.status !== "ready") return;
    saveAbortRef.current?.abort();
    historyAbortRef.current?.abort();
    generationRef.current += 1;
    setHistoryLoading(false);
    const controller = new AbortController();
    saveAbortRef.current = controller;
    const generation = generationRef.current;
    const current = () => !controller.signal.aborted && generationRef.current === generation;
    setSaving(true);
    setFailure(null);
    setAppended(null);
    try {
      const receipt = await appendFeedbackReview(
        projectId,
        feedbackId,
        draft,
        phase.annotation.review.current_version_id,
        retryKey,
        { signal: controller.signal },
      );
      // The POST acknowledgement is immutable and sufficient to rotate the
      // retry key. Detail is deliberately re-read from its own GET projection;
      // a later head must never change what an idempotent POST replay means.
      if (!current()) return;
      setAppended(receipt.version_number);
      setDraft(emptyReviewDraft());
      setRetryKey(mintRetryKey());
      try {
        const annotation = await fetchFeedbackAnnotation(projectId, feedbackId, { signal: controller.signal });
        if (!current()) return;
        setPhase({ status: "ready", annotation });
      } catch (refreshError: unknown) {
        if (!current()) return;
        setFailure({
          title: "The review was appended, but the detail could not be refreshed",
          message:
            refreshError instanceof Error
              ? refreshError.message
              : "Reload this feedback record to read the new review head.",
          refusals: [],
        });
      }
    } catch (error: unknown) {
      if (!current()) return;
      // Shown as the server wrote it: a reviewer who cannot see which field was
      // refused guesses, and a guessed triage decision is not evidence.
      if (error instanceof ApiError) {
        if (error.status === 409) {
          try {
            const refreshed = await fetchFeedbackAnnotation(projectId, feedbackId, { signal: controller.signal });
            if (!current()) return;
            setPhase({ status: "ready", annotation: refreshed });
            setDraft(emptyReviewDraft());
            setRetryKey(mintRetryKey());
            setFailure({
              title: "The review was refused",
              message: `${error.message}. The current review head was refreshed; enter the judgement again before appending.`,
              refusals: refusalsOf(error.body),
            });
            return;
          } catch (refreshError: unknown) {
            if (!current()) return;
            setFailure({
              title: "The review was refused",
              message:
                refreshError instanceof Error
                  ? `${error.message}. Refresh failed: ${refreshError.message}`
                  : error.message,
              refusals: refusalsOf(error.body),
            });
            return;
          }
        }
        setFailure({
          title: "The review was refused",
          message: error.message,
          refusals: refusalsOf(error.body),
        });
      } else {
        setFailure({
          title: "The review was refused",
          message: error instanceof Error ? error.message : String(error),
          refusals: [],
        });
      }
    } finally {
      if (current()) setSaving(false);
      if (saveAbortRef.current === controller) saveAbortRef.current = null;
    }
  }, [projectId, feedbackId, draft, phase, retryKey]);

  const loadMoreHistory = useCallback(async () => {
    if (phase.status !== "ready" || !phase.annotation.review.versions_next_cursor) return;
    historyAbortRef.current?.abort();
    const controller = new AbortController();
    historyAbortRef.current = controller;
    const generation = generationRef.current;
    const cursor = phase.annotation.review.versions_next_cursor;
    setHistoryLoading(true);
    try {
      const page = await listFeedbackReviews(
        projectId,
        feedbackId,
        { cursor, limit: 50 },
        { signal: controller.signal },
      );
      if (controller.signal.aborted || generationRef.current !== generation) return;
      setPhase((current) => {
        if (
          current.status !== "ready"
          || current.annotation.review.versions_next_cursor !== cursor
        ) return current;
        const known = new Set((current.annotation.review.versions ?? []).map((version) => version.id));
        return {
          status: "ready",
          annotation: {
            ...current.annotation,
            review: {
              ...current.annotation.review,
              versions: [
                ...(current.annotation.review.versions ?? []),
                ...page.versions.filter((version) => !known.has(version.id)),
              ],
              versions_truncated: page.truncated,
              versions_next_cursor: page.next_cursor,
            },
          },
        };
      });
    } catch (error: unknown) {
      if (controller.signal.aborted || generationRef.current !== generation) return;
      setFailure({
        title: "The review history could not be continued",
        message: error instanceof Error ? error.message : String(error),
        refusals: [],
      });
    } finally {
      if (!controller.signal.aborted && generationRef.current === generation) setHistoryLoading(false);
      if (historyAbortRef.current === controller) historyAbortRef.current = null;
    }
  }, [phase, projectId, feedbackId]);

  if (phase.status === "loading") {
    return (
      <p role="status" aria-live="polite" className="text-body text-text-secondary">
        Loading this feedback record…
      </p>
    );
  }
  if (phase.status === "not-found") {
    return (
      <Status as="block" tone="warning" title="This feedback record was not opened">
        No annotation with this identifier exists in this Project, or it is not available to you. The
        two answer identically on purpose.
      </Status>
    );
  }
  if (phase.status === "error") {
    return (
      <Status
        as="block"
        tone="error"
        title="This feedback record could not be read"
        action={
          <Button variant="secondary" onClick={() => setReloadToken((token) => token + 1)}>
            Retry
          </Button>
        }
      >
        {phase.message}. Nothing has been shown in its place.
      </Status>
    );
  }

  const { annotation } = phase;

  return (
    <Stack data-owner={`test/widget-feedback/${feedbackId}`}>
      {/* `DESIGN.md:137`: name + concise source/data-role metadata, not an id.
          The `name` fallback keeps the identifier for the one case where it is
          the only thing there is — a review whose author left no comment. */}
      <ObjectHeader
        name={annotation.comment ?? annotation.id}
        source="Feedback Review"
      />
      <Cluster>
        <Status tone={annotation.polarity === "positive" ? "success" : "error"}>
          {annotation.polarity}
        </Status>
        <Badge tone="neutral">{stateLabel(annotation.review.current_state)}</Badge>
        <span className="text-caption text-text-secondary">{annotation.blocking_use}</span>
      </Cluster>
      <NavTabs label="Feedback Review" tabs={tabs} current={tab} onNavigate={onNavigateTab} />

      {appended !== null && (
        <Status as="block" tone="success" title={`Review version ${appended} appended`}>
          The previous version was not modified.
        </Status>
      )}
      {failure && (
        <RefusalList
          title={failure.title}
          message={failure.message}
          refusals={failure.refusals}
        />
      )}

      {tab === "feedback" && <FeedbackTab annotation={annotation} />}
      {tab === "result-render" && <ResultRenderTab annotation={annotation} />}
      {tab === "ai-path" && (
        <AiPathTab annotation={annotation} onOpenTraceObservation={onOpenTraceObservation} />
      )}
      {tab === "classification" && (
        <ClassificationTab
          annotation={annotation}
          onOpenOwner={onOpenOwner}
          loadMoreHistory={() => void loadMoreHistory()}
          historyLoading={historyLoading}
        />
      )}
      {tab === "resolution" && (
        <Stack>
          <ResolutionTab
            draft={draft}
            update={update}
            save={() => void save()}
            saving={saving}
          />
          <FeedbackRegressionPromotion
            projectId={projectId}
            feedbackId={feedbackId}
            reviewVersionId={annotation.review.current_version_id}
            onOpenOwner={onOpenOwner}
          />
        </Stack>
      )}

      <OwnerLinks annotation={annotation} onOpenOwner={onOpenOwner} />
    </Stack>
  );
}
