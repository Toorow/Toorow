import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import TargetedFeedback, {
  type ExactFeedbackRequest,
  type FeedbackSelection,
} from "../targetedFeedback";

const CONTEXT = {
  schema_version: "exact-feedback.v1",
  token: "signed-token",
  interaction_ref: "interaction-1",
  expires_at: "2026-08-10T12:00:00Z",
};

function selection(target: FeedbackSelection["target"], label: string): FeedbackSelection {
  return { target, label };
}

describe("TargetedFeedback", () => {
  it("defaults to Answer, names a selected datum, and Clear restores Answer", () => {
    const clear = vi.fn();
    const { rerender } = render(
      <TargetedFeedback context={CONTEXT} selection={selection({ kind: "answer" }, "Answer")} onClear={clear} onSubmit={vi.fn()} />,
    );
    expect(screen.getByText("Answer", { selector: "strong" })).toBeInTheDocument();

    rerender(
      <TargetedFeedback
        context={CONTEXT}
        selection={selection({ kind: "datum", row_index: 4, field: "running_total_micros" }, "Row 5 · running_total_micros")}
        onClear={clear}
        onSubmit={vi.fn()}
      />,
    );
    expect(screen.getByText("Row 5 · running_total_micros", { selector: "strong" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Clear target" }));
    expect(clear).toHaveBeenCalledOnce();
  });

  it("submits one exact snapshot and reports success only after an acknowledged receipt", async () => {
    let resolve!: (value: unknown) => void;
    const submit = vi.fn<(request: ExactFeedbackRequest) => Promise<unknown>>(
      () => new Promise((done) => { resolve = done; }),
    );
    render(
      <TargetedFeedback context={CONTEXT} selection={selection({ kind: "path_step", ordinal: 7 }, "AI Path step 7")} onClear={vi.fn()} onSubmit={submit} />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Helpful" }));
    fireEvent.change(screen.getByLabelText("Optional comment"), { target: { value: "This step resolved it." } });
    fireEvent.click(screen.getByRole("button", { name: "Submit feedback" }));
    expect(screen.getByRole("button", { name: "Helpful" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Not helpful" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "Not helpful" }));
    expect(screen.getByRole("button", { name: "Helpful" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByRole("button", { name: "Not helpful" })).toHaveAttribute("aria-pressed", "false");
    expect(screen.getByRole("button", { name: "Submitting…" })).toBeDisabled();
    expect(submit).toHaveBeenCalledTimes(1);
    const request = submit.mock.calls[0]![0] as ExactFeedbackRequest;
    expect(request).toMatchObject({
      context: CONTEXT,
      target: { kind: "path_step", ordinal: 7 },
      polarity: "positive",
      comment: "This step resolved it.",
    });
    expect(request.retry_key).toEqual(expect.any(String));
    expect(screen.queryByText("Feedback recorded.")).not.toBeInTheDocument();

    resolve({
      schema_version: "exact-feedback-receipt.v1",
      status: "recorded",
      feedback_id: "fba_1",
      interaction_ref: "interaction-1",
      target: { kind: "path_step", ordinal: 7 },
    });
    expect(await screen.findByText("Feedback recorded.")).toBeInTheDocument();
  });

  it("retries the exact failed snapshot; editing creates a new retry key", async () => {
    const submit = vi.fn()
      .mockRejectedValueOnce(new Error("offline"))
      .mockRejectedValueOnce(new Error("offline"))
      .mockResolvedValueOnce({
        schema_version: "exact-feedback-receipt.v1",
        status: "recorded",
        feedback_id: "fba_2",
        interaction_ref: "interaction-1",
        target: { kind: "answer" },
      });
    render(
      <TargetedFeedback context={CONTEXT} selection={selection({ kind: "answer" }, "Answer")} onClear={vi.fn()} onSubmit={submit} />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Not helpful" }));
    fireEvent.change(screen.getByLabelText("Optional comment"), { target: { value: "First draft" } });
    fireEvent.click(screen.getByRole("button", { name: "Submit feedback" }));
    await screen.findByRole("button", { name: "Retry" });
    const first = submit.mock.calls[0]![0] as ExactFeedbackRequest;

    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    await waitFor(() => expect(submit).toHaveBeenCalledTimes(2));
    expect(submit.mock.calls[1]![0]).toEqual(first);

    await screen.findByRole("button", { name: "Retry" });
    fireEvent.change(screen.getByLabelText("Optional comment"), { target: { value: "Edited draft" } });
    fireEvent.click(screen.getByRole("button", { name: "Submit feedback" }));
    await waitFor(() => expect(submit).toHaveBeenCalledTimes(3));
    const edited = submit.mock.calls[2]![0] as ExactFeedbackRequest;
    expect(edited.retry_key).not.toBe(first.retry_key);
    expect(edited.comment).toBe("Edited draft");
  });

  it("refreshes an expired context and resends the unchanged snapshot", async () => {
    const refreshed = { ...CONTEXT, token: "new-token", expires_at: "2026-08-10T12:15:00Z" };
    const submit = vi.fn()
      .mockResolvedValueOnce({
        schema_version: "exact-feedback-receipt.v1",
        status: "refresh_required",
        code: "feedback_context_expired",
        message: "expired",
        feedback_context: refreshed,
      })
      .mockResolvedValueOnce({
        schema_version: "exact-feedback-receipt.v1",
        status: "replayed",
        feedback_id: "fba_3",
        interaction_ref: "interaction-1",
        target: { kind: "answer" },
      });
    const replaced = vi.fn();
    render(
      <TargetedFeedback context={CONTEXT} selection={selection({ kind: "answer" }, "Answer")} onClear={vi.fn()} onContextChange={replaced} onSubmit={submit} />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Helpful" }));
    fireEvent.change(screen.getByLabelText("Optional comment"), { target: { value: "Keep this" } });
    fireEvent.click(screen.getByRole("button", { name: "Submit feedback" }));

    await waitFor(() => expect(submit).toHaveBeenCalledTimes(2));
    const first = submit.mock.calls[0]![0] as ExactFeedbackRequest;
    expect(submit.mock.calls[1]![0]).toEqual({ ...first, context: refreshed });
    expect(replaced).toHaveBeenCalledWith(refreshed);
    expect(screen.getByLabelText("Optional comment")).toHaveValue("Keep this");
    expect(await screen.findByText("Feedback recorded.")).toBeInTheDocument();
  });

  it("keeps the draft on malformed, null, or refused acknowledgements and exposes an accessible status", async () => {
    const submit = vi.fn().mockResolvedValue(null);
    render(
      <TargetedFeedback context={CONTEXT} selection={selection({ kind: "answer" }, "Answer")} onClear={vi.fn()} onSubmit={submit} />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Not helpful" }));
    fireEvent.change(screen.getByLabelText("Optional comment"), { target: { value: "Do not lose me" } });
    fireEvent.click(screen.getByRole("button", { name: "Submit feedback" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Feedback was not recorded");
    expect(screen.getByLabelText("Optional comment")).toHaveValue("Do not lose me");
    expect(screen.queryByText("Feedback recorded.")).not.toBeInTheDocument();
  });

  it("refuses malformed sidecars and enforces the 2,000-character comment bound", () => {
    render(
      <TargetedFeedback context={{ ...CONTEXT, extra: true }} selection={selection({ kind: "answer" }, "Answer")} onClear={vi.fn()} onSubmit={vi.fn()} />,
    );
    expect(screen.getByRole("status")).toHaveTextContent("Feedback is unavailable for this delivery");
    expect(screen.getByLabelText("Optional comment")).toHaveAttribute("maxlength", "2000");
    expect(screen.getByRole("button", { name: "Submit feedback" })).toBeDisabled();
  });

  it("does not erase a draft when a parent recreates the same semantic selection", () => {
    const submit = vi.fn();
    const { rerender } = render(
      <TargetedFeedback context={CONTEXT} selection={{ target: { kind: "answer" }, label: "Answer" }} onClear={vi.fn()} onSubmit={submit} />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Helpful" }));
    fireEvent.change(screen.getByLabelText("Optional comment"), { target: { value: "Stable draft" } });
    rerender(
      <TargetedFeedback context={CONTEXT} selection={{ target: { kind: "answer" }, label: "Answer" }} onClear={vi.fn()} onSubmit={submit} />,
    );
    expect(screen.getByLabelText("Optional comment")).toHaveValue("Stable draft");
    expect(screen.getByRole("button", { name: "Helpful" })).toHaveAttribute("aria-pressed", "true");
  });
});
