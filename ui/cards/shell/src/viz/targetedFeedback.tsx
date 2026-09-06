import { useEffect, useRef, useState } from "react";

export const EXACT_FEEDBACK_SCHEMA = "exact-feedback.v1";
export const EXACT_FEEDBACK_RECEIPT_SCHEMA = "exact-feedback-receipt.v1";
export const MAX_FEEDBACK_COMMENT_CHARS = 2_000;

export type FeedbackTarget =
  | { kind: "answer" }
  | { kind: "datum"; row_index: number; field: string }
  | { kind: "path_step"; ordinal: number };

export interface FeedbackSelection {
  target: FeedbackTarget;
  label: string;
}

export interface ExactFeedbackContext {
  schema_version: typeof EXACT_FEEDBACK_SCHEMA;
  token: string;
  interaction_ref: string;
  expires_at: string;
}

export interface ExactFeedbackRequest {
  context: ExactFeedbackContext;
  target: FeedbackTarget;
  polarity: "positive" | "negative";
  comment: string | null;
  retry_key: string;
}

export type ExactFeedbackSubmitter = (request: ExactFeedbackRequest) => Promise<unknown>;

export interface TargetedFeedbackProps {
  context: unknown;
  selection: FeedbackSelection;
  onClear: () => void;
  onSubmit: ExactFeedbackSubmitter;
  onContextChange?: (context: ExactFeedbackContext) => void;
  /** Result/Render/path identity. Changing it clears every stale draft and request. */
  resetKey?: string;
}

type JsonRecord = Record<string, unknown>;
type Snapshot = ExactFeedbackRequest;

function record(value: unknown): JsonRecord | null {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? (value as JsonRecord)
    : null;
}

function exactKeys(value: JsonRecord, keys: string[]): boolean {
  const actual = Object.keys(value).sort();
  return actual.length === keys.length && actual.every((key, index) => key === [...keys].sort()[index]);
}

function boundedString(value: unknown, max = 4_096): value is string {
  return typeof value === "string" && value.length > 0 && value.length <= max;
}

const RFC3339 = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$/;

export function decodeExactFeedbackContext(value: unknown): ExactFeedbackContext | null {
  const input = record(value);
  if (
    !input ||
    !exactKeys(input, ["schema_version", "token", "interaction_ref", "expires_at"]) ||
    input.schema_version !== EXACT_FEEDBACK_SCHEMA ||
    !boundedString(input.token) ||
    !boundedString(input.interaction_ref, 200) ||
    !boundedString(input.expires_at, 64) ||
    !RFC3339.test(input.expires_at) ||
    Number.isNaN(Date.parse(input.expires_at))
  ) {
    return null;
  }
  return input as unknown as ExactFeedbackContext;
}

function sameTarget(left: FeedbackTarget, right: unknown): boolean {
  const candidate = record(right);
  if (!candidate || candidate.kind !== left.kind) return false;
  if (left.kind === "answer") return exactKeys(candidate, ["kind"]);
  if (left.kind === "datum") {
    return (
      exactKeys(candidate, ["kind", "row_index", "field"]) &&
      candidate.row_index === left.row_index &&
      candidate.field === left.field
    );
  }
  return exactKeys(candidate, ["kind", "ordinal"]) && candidate.ordinal === left.ordinal;
}

type Receipt =
  | { kind: "ack" }
  | { kind: "refresh"; context: ExactFeedbackContext };

function decodeReceipt(value: unknown, snapshot: Snapshot): Receipt | null {
  const input = record(value);
  if (!input || input.schema_version !== EXACT_FEEDBACK_RECEIPT_SCHEMA) return null;
  if (input.status === "recorded" || input.status === "replayed") {
    if (
      !exactKeys(input, ["schema_version", "status", "feedback_id", "interaction_ref", "target"]) ||
      !boundedString(input.feedback_id, 200) ||
      input.interaction_ref !== snapshot.context.interaction_ref ||
      !sameTarget(snapshot.target, input.target)
    ) {
      return null;
    }
    return { kind: "ack" };
  }
  if (input.status === "refresh_required") {
    if (
      !exactKeys(input, ["schema_version", "status", "code", "message", "feedback_context"]) ||
      input.code !== "feedback_context_expired" ||
      !boundedString(input.message, 500)
    ) {
      return null;
    }
    const context = decodeExactFeedbackContext(input.feedback_context);
    return context ? { kind: "refresh", context } : null;
  }
  return null;
}

function retryKey(): string {
  const random = globalThis.crypto?.randomUUID?.();
  return random ?? `feedback-${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

function selectionKey(selection: FeedbackSelection): string {
  const target = selection.target;
  if (target.kind === "answer") return `answer:${selection.label}`;
  if (target.kind === "datum") {
    return `datum:${target.row_index}:${target.field}:${selection.label}`;
  }
  return `path_step:${target.ordinal}:${selection.label}`;
}

function unavailableMessage(): string {
  return "Feedback is unavailable for this delivery. The answer remains usable.";
}

export default function TargetedFeedback({
  context: contextInput,
  selection,
  onClear,
  onSubmit,
  onContextChange,
  resetKey = "",
}: TargetedFeedbackProps) {
  const [context, setContext] = useState<ExactFeedbackContext | null>(() =>
    decodeExactFeedbackContext(contextInput),
  );
  const [polarity, setPolarity] = useState<"positive" | "negative" | null>(null);
  const [comment, setComment] = useState("");
  const [pending, setPending] = useState(false);
  const [failure, setFailure] = useState<Snapshot | null>(null);
  const [status, setStatus] = useState<"idle" | "success" | "error">("idle");
  const generation = useRef(0);
  const activeRequest = useRef<symbol | null>(null);
  const semanticSelectionKey = selectionKey(selection);

  useEffect(() => {
    setContext(decodeExactFeedbackContext(contextInput));
  }, [contextInput]);

  useEffect(() => {
    generation.current += 1;
    activeRequest.current = null;
    setPolarity(null);
    setComment("");
    setPending(false);
    setFailure(null);
    setStatus("idle");
  }, [resetKey]);

  useEffect(() => {
    generation.current += 1;
    activeRequest.current = null;
    setPending(false);
    setFailure(null);
    setStatus("idle");
  }, [semanticSelectionKey]);

  const edit = () => {
    setFailure(null);
    setStatus("idle");
  };

  const send = async (snapshot: Snapshot) => {
    if (activeRequest.current) return;
    const requestToken = Symbol("exact-feedback-request");
    activeRequest.current = requestToken;
    const requestGeneration = generation.current;
    setPending(true);
    setStatus("idle");
    try {
      let receipt = decodeReceipt(await onSubmit(snapshot), snapshot);
      if (!receipt) throw new Error("missing acknowledgement");
      if (receipt.kind === "refresh") {
        setContext(receipt.context);
        onContextChange?.(receipt.context);
        const refreshed = { ...snapshot, context: receipt.context };
        receipt = decodeReceipt(await onSubmit(refreshed), refreshed);
        if (!receipt || receipt.kind !== "ack") throw new Error("missing acknowledgement");
      }
      if (generation.current !== requestGeneration) return;
      setFailure(null);
      setStatus("success");
    } catch {
      if (generation.current !== requestGeneration) return;
      setFailure(snapshot);
      setStatus("error");
    } finally {
      if (activeRequest.current === requestToken) {
        activeRequest.current = null;
        if (generation.current === requestGeneration) setPending(false);
      }
    }
  };

  const submit = () => {
    if (!context || !polarity || pending) return;
    void send({
      context,
      target: selection.target,
      polarity,
      comment: comment.trim() ? comment : null,
      retry_key: retryKey(),
    });
  };

  return (
    <section
      aria-label="Feedback on this analysis"
      data-targeted-feedback
      className="flex flex-col gap-3 rounded-md border border-[color:var(--color-divider-base,currentColor)] p-3 text-sm"
    >
      <div className="flex flex-wrap items-center gap-2">
        <span>Feedback target:</span>
        <strong>{selection.label}</strong>
        {selection.target.kind !== "answer" ? (
          <button type="button" onClick={onClear} className="underline">
            Clear target
          </button>
        ) : null}
      </div>

      <div role="group" aria-label="Feedback polarity" className="flex gap-2">
        <button
          type="button"
          disabled={pending}
          aria-pressed={polarity === "positive"}
          onClick={() => { edit(); setPolarity("positive"); }}
        >
          Helpful
        </button>
        <button
          type="button"
          disabled={pending}
          aria-pressed={polarity === "negative"}
          onClick={() => { edit(); setPolarity("negative"); }}
        >
          Not helpful
        </button>
      </div>

      <label className="flex flex-col gap-1">
        <span>Optional comment</span>
        <textarea
          aria-label="Optional comment"
          maxLength={MAX_FEEDBACK_COMMENT_CHARS}
          value={comment}
          onChange={(event) => { edit(); setComment(event.currentTarget.value); }}
          disabled={pending}
        />
      </label>

      <div className="flex flex-wrap items-center gap-2">
        <button
          type="button"
          disabled={!context || !polarity || pending}
          onClick={submit}
        >
          {pending ? "Submitting…" : "Submit feedback"}
        </button>
        {failure && !pending ? (
          <button type="button" onClick={() => void send(failure)}>
            Retry
          </button>
        ) : null}
      </div>

      {!context ? <p role="status" aria-live="polite">{unavailableMessage()}</p> : null}
      {status === "success" ? <p role="status" aria-live="polite">Feedback recorded.</p> : null}
      {status === "error" ? (
        <p role="alert">Feedback was not recorded. Your draft is still here.</p>
      ) : null}
    </section>
  );
}
