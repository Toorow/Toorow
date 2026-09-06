/**
 * Asking a Datastream to collect NOW, and saying what that costs first.
 *
 * THE HOLE THIS FILLS, MEASURED 2026-08-18. `SchedulePanel` offers the cadence
 * `manual` — "On demand only", whose failure copy reads "Nothing is retried.
 * This Datastream only runs when someone asks it to" — and nothing in the whole
 * console asked. `POST /api/datastreams/{id}/run` had existed since story 8.2;
 * the Workbench simply never mounted a door to it. Measured on the live base and
 * recorded in that route's own docstring: of 89 Datastreams exactly one is
 * active, enabled and mapped, and its cadence is `manual`. No clock would ever
 * pick it up. The one Datastream that could collect had no way to be asked.
 *
 * WHY THE WINDOW IS FETCHED AND NOT COMPUTED. The surface requires the
 * confirmation to name the connector, the account and the window BEFORE anything
 * is spent. The window is not a property this screen can read — it is
 * `pull_window.resolve_window` applied to four columns against the PROJECT's
 * last complete day in the PROJECT's timezone, none of which is on the header.
 * Composing it here would have produced a second arbitration, free to drift from
 * the one that actually runs, and a person would confirm one window and pay for
 * another. So `GET …/run/preview` answers with the same call the POST makes, and
 * this file draws what it says. That route was added for this sentence.
 *
 * IT IS THE HEAVIER SIBLING OF `repullDay`. That gesture re-asks for days
 * already collected and is deliberately light. This one starts a RUN — it opens
 * an execution, it is the same object the clock would have made — so its refusal
 * is shown before the click rather than after, and the spend line carries the
 * same honest `Not measured` with the same reason, imported rather than retyped.
 */
import { useState } from "react";
import { ConfirmDialog } from "../../ui";
import { ApiError, apiJson } from "../../lib/apiFetch";
import { NOT_MEASURED } from "./DatastreamRunLive";
import { NOT_REPORTED, SPEND_NOT_MEASURED_REASON } from "./repullDay";

/** What `GET /api/datastreams/{id}/run/preview` answers. */
export interface RunPreview {
  date_from: string;
  date_to: string;
  window_days: number;
  /** `date_window_days` | `refetch_days` | `defensive_default`. */
  window_source: string;
  /** The cadence whose floor widened the window, or `null`. */
  window_widened_for: string | null;
  cadence: string | null;
  /** A named reason this Datastream will not run, and the gesture that frees it. */
  refusal: { code: string; message: string } | null;
}

/** What `POST /api/datastreams/{id}/run` answered. */
export interface RunStarted {
  job_id?: string;
  pull_id?: string;
  state?: string;
  deduplicated?: boolean;
  execution_id?: string;
}

export async function loadRunPreview(
  projectId: string,
  datastreamId: string,
  signal?: AbortSignal,
): Promise<RunPreview> {
  return apiJson<RunPreview>(
    `/api/datastreams/${encodeURIComponent(datastreamId)}/run/preview` +
      `?project_id=${encodeURIComponent(projectId)}`,
    { signal },
  );
}

export async function startRun(
  projectId: string,
  datastreamId: string,
): Promise<RunStarted> {
  return apiJson<RunStarted>(`/api/datastreams/${encodeURIComponent(datastreamId)}/run`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    // The window is DELIBERATELY not pinned from here. Sending back the dates
    // the preview showed would freeze a window that the project's own clock may
    // have moved between the read and the click, and would make this screen the
    // author of a window it only reported. The route resolves it again, from the
    // same row, with the same module.
    body: JSON.stringify({ project_id: projectId }),
  });
}

/** Where the window came from, when that changes what the number means.
 *
 *  `defensive_default` is a fallback and not a setting — three days because the
 *  row declares nothing, which a person about to spend should not read as "this
 *  Datastream's window is three days". A declared window says nothing extra. */
export function windowProvenance(preview: RunPreview): string | null {
  if (preview.window_source === "defensive_default") {
    return "this Datastream declares no retrieval window, so the collection falls back to " +
      `${preview.window_days} days`;
  }
  if (preview.window_widened_for) {
    return `widened to ${preview.window_days} days because the cadence is ` +
      `${preview.window_widened_for}`;
  }
  return null;
}

/** What the run answered, in words that never call a refusal a queued run. */
export function runStartedSentence(started: RunStarted): string {
  if (started.deduplicated) {
    return "This window was already in flight, so no second collection was created. " +
      "Watch it on the band above.";
  }
  return "The collection is queued. Watch it on the band above the tabs — it is the " +
    "same run the clock would have started.";
}

/** Where the gesture has got to. Four states, and no two of them render alike. */
export type CollectPhase = "idle" | "checking" | "confirming" | "refused";

/**
 * The gesture as one hook: read what it would cost, THEN confirm, start, report.
 *
 * A REFUSAL IS NOT A CONFIRMATION, so the window is read BEFORE the dialog
 * opens, not inside it. Asking "are you sure?" and then answering "this cannot
 * run anyway" puts two ways out on a dialog with no act in it, and dresses a
 * state a person can repair as a decision they have to make. When the server
 * names a gate, the phase is `refused` and the caller draws that sentence where
 * the person is already looking — no dialog is opened at all.
 *
 * AND IT IS READ ON THE CLICK, NOT ON MOUNT. A tab that probed this route for
 * every Datastream it drew would add one query per Workbench load, on a service
 * that scales to zero, to answer a question nobody asked.
 */
export function useCollectNow(
  projectId: string,
  datastreamId: string,
  onStarted: (started: RunStarted) => void,
) {
  const [phase, setPhase] = useState<CollectPhase>("idle");
  const [preview, setPreview] = useState<RunPreview | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  function reset() {
    setPhase("idle");
    setPreview(null);
    setError(null);
  }

  return {
    phase,
    preview,
    busy,
    error,
    /** The refusal the server named, or null. Only meaningful in `refused`. */
    refusal: phase === "refused" ? (preview?.refusal ?? null) : null,
    async ask() {
      if (busy || phase === "checking") return;
      setError(null);
      setPreview(null);
      setPhase("checking");
      try {
        const answer = await loadRunPreview(projectId, datastreamId);
        setPreview(answer);
        setPhase(answer.refusal ? "refused" : "confirming");
      } catch (reason) {
        setPhase("idle");
        setError(
          "What this would collect could not be read, so nothing was asked of the " +
            "provider. " +
            (reason instanceof ApiError || reason instanceof Error ? reason.message : ""),
        );
      }
    },
    dismiss() { if (!busy) reset(); },
    async confirm() {
      // Nothing is ever spent on a scope nobody was shown: no window read, no
      // run. The second lock on the same rule the phases already carry.
      if (busy || !preview || preview.refusal) return;
      setBusy(true);
      setError(null);
      try {
        const started = await startRun(projectId, datastreamId);
        reset();
        onStarted(started);
      } catch (reason) {
        // The dialog STAYS OPEN on a refusal, carrying the server's sentence —
        // closing it would leave a person believing the run started (63.6).
        setError(
          "The collection could not be started. " +
            (reason instanceof ApiError || reason instanceof Error
              ? reason.message
              : "The request failed."),
        );
      } finally {
        setBusy(false);
      }
    },
  };
}

/**
 * The confirmation — opened only once the window is KNOWN.
 *
 * It never renders a refusal and never renders "reading…": by the time it is
 * open the scope is read, so every line in it is a fact. That is what lets the
 * act carry its real name instead of being offered and then rejected.
 */
export function CollectNowDialog({
  open,
  onOpenChange,
  preview,
  datastreamId,
  datastreamName,
  connector,
  sourceAccountRef,
  busy,
  error,
  onConfirm,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** Always a preview WITHOUT a refusal — the hook opens on nothing else. */
  preview: RunPreview | null;
  datastreamId: string;
  datastreamName?: string | null;
  connector?: string | null;
  sourceAccountRef?: string | null;
  busy: boolean;
  error: string | null;
  onConfirm: () => void;
}) {
  if (!open || !preview) return null;
  const provenance = windowProvenance(preview);

  return (
    <ConfirmDialog
      open
      onOpenChange={(next) => { if (busy) return; onOpenChange(next); }}
      title="Collect now"
      description={
        "This starts the same run the clock would have started — it appears on the live " +
        "band and in Runs. Nothing already collected is removed: a day that comes back " +
        "with rows replaces its own reading, and a day that comes back empty stays empty."
      }
      evidenceLabel="What this asks for"
      evidence={{
        Datastream: datastreamName?.trim() || datastreamId,
        Connector: connector || NOT_REPORTED,
        // The account the run spends on. Blank would read as an account whose
        // name is too small to see, which is why the word exists.
        "Source account": sourceAccountRef || NOT_REPORTED,
        Window:
          `${preview.date_from} → ${preview.date_to} (${preview.window_days} ` +
          `${preview.window_days === 1 ? "day" : "days"})` +
          (provenance ? ` — ${provenance}` : ""),
        Cadence: preview.cadence ?? NOT_REPORTED,
        "Estimated spend": `${NOT_MEASURED} — ${SPEND_NOT_MEASURED_REASON}`,
      }}
      confirmLabel="Collect now"
      cancelLabel="Do not collect"
      busy={busy}
      error={error}
      onConfirm={onConfirm}
      data-testid="collect-now-confirm"
      cancelTestId="collect-now-cancel"
      confirmTestId="collect-now-go"
    />
  );
}
