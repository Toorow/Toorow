import { useCallback, useEffect, useRef, useState } from "react";

import { ApiError } from "../lib/apiFetch";
import { Button, Cluster, ObjectId, PanelBody, Stack, Status } from "../ui";
import type { RunCase } from "./evaluationRunClient";
import { evaluateFeedbackRegressionCase, resolveFeedbackRegression, type EvaluationResultReceipt, type FeedbackRegressionResolutionReceipt, type OwnerLink } from "./feedbackReviewClient";
import { refusalsOf, type Refusal } from "./goldenQuestionClient";

function mintRetryKey(): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return crypto.randomUUID();
  }
  return `feedback-regression-evaluate-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

function OwnerButtons({
  links,
  onOpenOwner,
  labelForLink,
}: {
  links: OwnerLink[];
  onOpenOwner?: (link: OwnerLink) => void;
  labelForLink?: (link: OwnerLink) => string;
}) {
  if (links.length === 0) return null;
  return (
    <Cluster>
      {links.map((link) => onOpenOwner ? (
        <Button
          key={`${link.section}-${link.object_type}-${link.object_id}-${link.version_id ?? "run"}`}
          variant="link"
          size="sm"
          onClick={() => onOpenOwner(link)}
        >
          {labelForLink?.(link) ?? `Open ${link.object_type} ${link.object_id}`}
        </Button>
      ) : (
        <code key={`${link.section}-${link.object_type}-${link.object_id}-${link.version_id ?? "run"}`} className="text-technical">
          {link.object_type} {link.object_id}
        </code>
      ))}
    </Cluster>
  );
}

export function FeedbackRegressionResolutionStatus({
  runCaseId,
  receipt,
  onOpenOwner,
}: {
  runCaseId: string;
  receipt: FeedbackRegressionResolutionReceipt;
  onOpenOwner?: (link: OwnerLink) => void;
}) {
  const labelForLink = (link: OwnerLink) => {
    if (link.version_id === receipt.evaluation_case_id) return "Review Evaluation Case";
    if (receipt.verdict_id && link.version_id === receipt.verdict_id) return "Review trusted Verdict";
    return "Open Evaluation Run";
  };
  return (
    <Status
      as="block"
      tone={receipt.status === "resolved" ? "success" : "neutral"}
      title={receipt.status === "resolved" ? "Feedback regression resolved" : "Feedback remains unresolved"}
      data-testid={`feedback-regression-resolution-${runCaseId}`}
    >
      <Stack>
        <span>{receipt.reason ?? "The current trusted pass was linked without rewriting prior evidence."}</span>
        {receipt.status === "unresolved" && (
          <span>
            This trusted evaluation is immutable. Review the incompatible Evaluation Case and
            Verdict below. To test corrected pins or expectations, create a new offline Evaluation
            Case; this case cannot be evaluated again.
          </span>
        )}
        <OwnerButtons links={receipt.owner_links} onOpenOwner={onOpenOwner} labelForLink={labelForLink} />
      </Stack>
    </Status>
  );
}

export default function FeedbackRegressionCaseActions({
  projectId,
  runCase,
  onOpenOwner,
}: {
  projectId: string;
  runCase: RunCase;
  onOpenOwner?: (link: OwnerLink) => void;
}) {
  const promotion = runCase.feedback_regression;
  const retryKeyRef = useRef(mintRetryKey());
  const [evaluating, setEvaluating] = useState(false);
  const [resolving, setResolving] = useState(false);
  const [evaluation, setEvaluation] = useState<EvaluationResultReceipt | null>(null);
  const [resolution, setResolution] = useState<FeedbackRegressionResolutionReceipt | null>(null);
  const [failure, setFailure] = useState<{ message: string; refusals: Refusal[] } | null>(null);
  const generationRef = useRef(0);
  const requestAbortRef = useRef<AbortController | null>(null);

  useEffect(() => {
    generationRef.current += 1;
    requestAbortRef.current?.abort();
    retryKeyRef.current = mintRetryKey();
    setEvaluating(false);
    setResolving(false);
    setEvaluation(null);
    setResolution(null);
    setFailure(null);
  }, [projectId, runCase.id, promotion?.feedback_id, promotion?.regression_case_id]);

  useEffect(() => () => {
    generationRef.current += 1;
    requestAbortRef.current?.abort();
  }, []);

  const evaluate = useCallback(async () => {
    if (!promotion) return;
    requestAbortRef.current?.abort();
    const controller = new AbortController();
    requestAbortRef.current = controller;
    const generation = generationRef.current;
    const current = () => !controller.signal.aborted && generationRef.current === generation;
    setEvaluating(true);
    setFailure(null);
    setResolution(null);
    setEvaluation(null);
    try {
      const receipt = await evaluateFeedbackRegressionCase(projectId, runCase.id, retryKeyRef.current, {
        signal: controller.signal,
      });
      if (!current()) return;
      setEvaluation(receipt);
      retryKeyRef.current = mintRetryKey();
    } catch (error: unknown) {
      if (!current()) return;
      setFailure({
        message: error instanceof Error ? error.message : String(error),
        refusals: error instanceof ApiError ? refusalsOf(error.body) : [],
      });
      // A lost acknowledgement must replay the byte-identical request. The key
      // rotates only after the immutable evaluation receipt is received.
    } finally {
      if (current()) setEvaluating(false);
      if (requestAbortRef.current === controller) requestAbortRef.current = null;
    }
  }, [promotion, projectId, runCase.id]);

  const resolve = useCallback(async () => {
    if (!promotion || !evaluation) return;
    requestAbortRef.current?.abort();
    const controller = new AbortController();
    requestAbortRef.current = controller;
    const generation = generationRef.current;
    const current = () => !controller.signal.aborted && generationRef.current === generation;
    setResolving(true);
    setFailure(null);
    try {
      const receipt = await resolveFeedbackRegression(
        projectId,
        promotion.feedback_id,
        promotion.regression_case_id,
        evaluation.evaluation_case_id,
        { signal: controller.signal },
      );
      if (!current()) return;
      setResolution(receipt);
    } catch (error: unknown) {
      if (!current()) return;
      setFailure({
        message: error instanceof Error ? error.message : String(error),
        refusals: error instanceof ApiError ? refusalsOf(error.body) : [],
      });
    } finally {
      if (current()) setResolving(false);
      if (requestAbortRef.current === controller) requestAbortRef.current = null;
    }
  }, [promotion, evaluation, projectId]);

  if (!promotion) return null;

  return (
    <PanelBody className="border-b border-divider-base">
      <Stack>
      <Status as="block" tone="neutral" title="Feedback regression case">
        This action is available because the server joined this exact Evaluation Case to feedback
        <ObjectId value={promotion.feedback_id} title="Feedback" /> and promotion
        <ObjectId value={promotion.regression_case_id} title="Regression case" />. No identity is
        inferred from the Result or Golden Question.
      </Status>

      {failure && (
        <Status as="block" tone="error" title="The feedback regression action was refused" data-testid={`feedback-regression-case-refusal-${runCase.id}`}>
          <p className="m-0">{failure.message}</p>
          {failure.refusals.length > 0 && (
            <ul className="mt-2 list-disc pl-5">
              {failure.refusals.map((refusal, index) => (
                <li key={`${refusal.code}-${index}`}>
                  <code className="text-technical">{refusal.subject ?? refusal.code}</code> — {refusal.message}
                </li>
              ))}
            </ul>
          )}
        </Status>
      )}

      {evaluation ? (
        <Status
          as="block"
          tone="success"
          title={evaluation.status === "replayed" ? "Trusted evaluation acknowledgement replayed" : "Trusted evaluation recorded"}
        >
          <Stack>
            <span>
              Producer <code className="text-technical">{evaluation.producer}</code> returned {Object.keys(evaluation.verdicts).length} separate dimension verdicts.
            </span>
            <OwnerButtons links={evaluation.owner_links} onOpenOwner={onOpenOwner} />
          </Stack>
        </Status>
      ) : (
        <Button
          data-testid={`feedback-regression-evaluate-${runCase.id}`}
          onClick={() => void evaluate()}
          disabled={evaluating}
        >
          {evaluating ? "Evaluating…" : "Evaluate regression case"}
        </Button>
      )}

      {evaluation && !resolution && (
        <Button
          data-testid={`feedback-regression-resolve-${runCase.id}`}
          onClick={() => void resolve()}
          disabled={resolving}
        >
          {resolving ? "Resolving…" : "Resolve feedback from trusted pass"}
        </Button>
      )}

      {resolution && (
        <FeedbackRegressionResolutionStatus
          runCaseId={runCase.id}
          receipt={resolution}
          onOpenOwner={onOpenOwner}
        />
      )}
      </Stack>
    </PanelBody>
  );
}
