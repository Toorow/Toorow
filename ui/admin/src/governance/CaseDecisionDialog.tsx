/**
 * CaseDecisionDialog — take a decision on a Control Case (Story 49.4, AC7).
 *
 * WHY THIS EXISTS. `governance.md:111` says Governance "owns the consolidated
 * policy, case and decision". The command family that performs one has been
 * served since 49.4 (`controls_quality_api.py`), and `control_case_decisions`
 * has held every column a decision needs since migration 145 — but no screen
 * ever called it. Measured 2026-08-04: `ControlsQualityTabs.tsx` contained zero
 * POST and zero buttons, so Controls & Quality could show decisions and never
 * take one. That is the whole defect this file closes (AI-192).
 *
 * TWO PHASES, AND THE REASON IS NOT CEREMONY. `prepare` is what derives the
 * diff, impact and dependency fingerprint SERVER-side (`controls_change_sets.py:268`
 * — "an impact a client supplied describes what the client claimed"), and it
 * mints a single-use token. A decision is append-only: `control_case_decisions`
 * has no update path and none is wanted. So the impact is shown, and only then
 * is confirm reachable. Collapsing the two steps would put an irreversible act
 * behind one click on an impact nobody read.
 *
 * WHAT IS DELIBERATELY NOT ENUMERATED HERE. `decision_kind` is a slug the
 * database constrains (`145_controls_and_quality.sql:240`,
 * `^[a-z][a-z0-9_]{1,47}$`) and nothing else does — there is no ratified list of
 * kinds, and inventing one in the browser would be a design decision presented
 * as a repair. So exactly one kind is named: `approve_mapping`, because it is
 * the only one the server treats specially — it hands off to the Data owner
 * command (`controls_owner_commands.py:226`). Every other kind is typed, against
 * the database's own rule, shown to the operator.
 *
 * A failed owner hand-off is NOT a failed decision. `confirm` records the
 * decision with `owner_outcome='failed'` and returns 200 — raising would roll the
 * confirmation back and erase the trace that the owner was ever asked.
 *
 * WHICH MEANS THE DIALOG HAS TO SAY SO, and until 2026-08-16 it did not. The
 * confirm response carried `result_version_id` and nothing else, so this dialog
 * closed on "Decision recorded" whether Data had published or refused, and
 * pointed at a history tab. Someone who had just taken an APPEND-ONLY act had to
 * go read a table to learn it had failed. `confirm` now returns `owner_outcome`,
 * and a failed hand-off keeps the dialog OPEN with the owner's own sentence —
 * the decision stands, recorded, and what did not happen is named where the act
 * was taken.
 */
import { useState } from "react";
import { ApiError, apiPost } from "../lib/apiFetch";
import {
  Button,
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  Input,
  Label,
  NativeSelect,
  Status,
  Textarea,
  notify,
} from "../ui";

/** The database rule, not a rule of our own. */
const KIND_PATTERN = /^[a-z][a-z0-9_]{1,47}$/;
const REASON_MAX = 600;

/** The only kind with an owner adapter — `controls_owner_commands.py:226`. */
const APPROVE_MAPPING = "approve_mapping";

export interface CaseCandidate {
  id?: unknown;
  candidate_kind?: unknown;
  owner_object_type?: unknown;
  owner_object_id?: unknown;
}

interface PreparedChangeSet {
  id: string;
  confirmation_token?: string;
  impact?: Record<string, unknown> | null;
  dependency_fingerprint?: string | null;
  expires_at?: string | null;
}

/** What `confirm` answers. `owner_outcome` is absent when the decision called no
 *  owner command at all — which is NOT the same as `not_applicable`, a value the
 *  server stores and returns.
 *
 *  The three words are `control_cases.OWNER_OUTCOMES` and there is no fourth:
 *  `_hand_off_to_data` used to answer `"applied"`, which `decide` refused, so a
 *  publication Data had performed came back to this dialog as a 422 and rendered
 *  "Nothing was recorded" (both repaired 2026-08-31). */
interface ConfirmedDecision {
  result_version_id?: string | null;
  owner_outcome?: "succeeded" | "failed" | "not_applicable";
  owner_result?: Record<string, unknown> | null;
}

/** The owner's own sentence, whole, when there is one. A paraphrase would lose
 *  the reason Data refused — which is the only actionable thing on this screen.
 *
 *  `message` is what `controls_owner_commands._hand_off_to_data` stores beside
 *  the routing `refusal` code. Until 2026-08-31 it stored the code ALONE, so this
 *  function returned null for every real refusal and the screen said "gave no
 *  reason" about an owner that had given one. The code is deliberately not shown
 *  in its place: a stable identifier is for routing, not for reading. */
function ownerSentence(result: Record<string, unknown> | null | undefined): string | null {
  if (!result || typeof result !== "object") return null;
  const message = (result as { message?: unknown }).message;
  return typeof message === "string" && message.trim() ? message : null;
}

function todayIso(): string {
  return new Date().toISOString().slice(0, 10);
}

function messageOf(error: unknown, fallback: string): string {
  if (error instanceof ApiError) return error.message || fallback;
  return error instanceof Error ? error.message : fallback;
}

export default function CaseDecisionDialog({
  open,
  projectId,
  caseId,
  candidates,
  onClose,
  onDecided,
}: {
  open: boolean;
  projectId: string;
  caseId: string;
  candidates: CaseCandidate[];
  onClose: () => void;
  onDecided: () => void;
}) {
  const [kindChoice, setKindChoice] = useState<string>(APPROVE_MAPPING);
  const [customKind, setCustomKind] = useState("");
  const [candidateId, setCandidateId] = useState("");
  const [reason, setReason] = useState("");
  const [effectiveFrom, setEffectiveFrom] = useState(todayIso());
  const [prepared, setPrepared] = useState<PreparedChangeSet | null>(null);
  const [ownerFailure, setOwnerFailure] = useState<ConfirmedDecision | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const decisionKind = kindChoice === "__other" ? customKind.trim() : kindChoice;
  const trimmedReason = reason.trim();
  const kindValid = KIND_PATTERN.test(decisionKind);
  const reasonValid = trimmedReason.length >= 1 && trimmedReason.length <= REASON_MAX;
  const canPrepare = kindValid && reasonValid && Boolean(effectiveFrom) && !busy;

  function reset() {
    setKindChoice(APPROVE_MAPPING);
    setCustomKind("");
    setCandidateId("");
    setReason("");
    setEffectiveFrom(todayIso());
    setPrepared(null);
    setOwnerFailure(null);
    setError(null);
    setBusy(false);
  }

  function close() {
    reset();
    onClose();
  }

  const base = `/api/projects/${encodeURIComponent(projectId)}/governance/controls-quality/change-sets`;

  async function handlePrepare() {
    setBusy(true);
    setError(null);
    try {
      const created = await apiPost<{ id: string }>(base, {
        object_type: "control-case",
        object_id: caseId,
        intent: {
          decision_kind: decisionKind,
          reason: trimmedReason,
          effective_from: effectiveFrom,
          ...(candidateId ? { candidate_id: candidateId } : {}),
        },
        // Scoped to this case and this exact intent: a retry after a network
        // failure returns the same change set instead of opening a second one.
        idempotency_key: `case-decision:${caseId}:${decisionKind}:${effectiveFrom}:${trimmedReason}`,
      });
      const result = await apiPost<PreparedChangeSet>(`${base}/${encodeURIComponent(created.id)}/prepare`, {});
      if (!result?.confirmation_token) {
        // THE COMMENT HERE USED TO CLAIM that an absent token proved the server
        // had declined to issue one. Measured false in story 60.2, on this store
        // too: `controls_change_sets.prepare_change_set`
        // (`server/core/controls_change_sets.py:243-266`) mints the token
        // UNCONDITIONALLY and returns it beside the record. A refusal from that
        // route arrives as a raised `ControlsChangeSetError` -> a 4xx -> the
        // `catch` below, never as a missing token.
        //
        // The branch is KEPT because a token is what `confirm` consumes and a
        // response without one must not be followed by a confirm attempt. What
        // is removed is the claim that reaching it proves a validation refusal.
        // A guard described as doing something it does not do is how the same
        // belief was copied into three dialogs.
        setError("The server returned no confirmation token. Nothing was decided.");
        return;
      }
      setPrepared(result);
    } catch (err) {
      setError(messageOf(err, "The change set could not be prepared. Nothing was decided."));
    } finally {
      setBusy(false);
    }
  }

  async function handleConfirm() {
    if (!prepared?.confirmation_token) return;
    setBusy(true);
    setError(null);
    try {
      const confirmed = await apiPost<ConfirmedDecision>(
        `${base}/${encodeURIComponent(prepared.id)}/confirm`,
        { confirmation_token: prepared.confirmation_token }
      );
      // The decision IS recorded either way — that is what append-only means, and
      // the list behind this dialog must refresh before anything else is said.
      onDecided();
      if (confirmed?.owner_outcome === "failed") {
        // NOT an error banner: nothing failed to be decided. What failed is the
        // command the decision asked for, and the dialog stays open naming it
        // rather than closing on a success message.
        setOwnerFailure(confirmed);
        setPrepared(null);
        return;
      }
      notify(
        confirmed?.owner_outcome === "succeeded"
          ? `Decision recorded: ${decisionKind}. The Data owner published the mapping.`
          : `Decision recorded: ${decisionKind}.`
      );
      close();
    } catch (err) {
      // The token is single-use. A refused confirm leaves the operator on the
      // review step with the reason, and preparing again is the way forward.
      setError(messageOf(err, "The decision was refused. Nothing was recorded."));
      setPrepared(null);
    } finally {
      setBusy(false);
    }
  }

  const impact = prepared?.impact ?? {};
  const candidateCount = typeof impact.candidates === "number" ? impact.candidates : null;

  return (
    <Dialog open={open} onOpenChange={(next) => (next ? undefined : close())}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>
            {ownerFailure
              ? "Decided — the Data owner refused to publish"
              : prepared
                ? "Review before deciding"
                : "Take a decision"}
          </DialogTitle>
          <DialogDescription>
            {ownerFailure
              ? "The decision is recorded and stays recorded; it is append-only. What did not happen is the publication it asked for."
              : prepared
                ? "A decision is append-only: it is never edited and never deleted. Read the impact the server derived, then confirm."
                : "Governance owns the decision on this case. The owner command runs only if the decision calls for it."}
          </DialogDescription>
        </DialogHeader>

        {error && (
          <Status as="block" tone="error" title="Nothing was decided">
            {error}
          </Status>
        )}

        {ownerFailure ? (
          <div className="flex flex-col gap-4" data-testid="owner-handoff-failed">
            <Status as="block" tone="warning" title="The mapping was not published">
              {ownerSentence(ownerFailure.owner_result) ??
                "The Data owner refused the publication and gave no reason."}
            </Status>
            <p className="text-caption text-muted-foreground">
              Governance decided; Data refused. The two authorities are separate, so neither
              undoes the other — repair what Data named, then decide again if the case needs it.
            </p>
          </div>
        ) : prepared ? (
          <div className="flex flex-col gap-4">
            <dl className="grid grid-cols-2 gap-x-4 gap-y-2 text-body">
              <dt className="text-muted-foreground">Decision</dt>
              <dd className="font-mono">{decisionKind}</dd>
              <dt className="text-muted-foreground">Effective from</dt>
              <dd>{effectiveFrom}</dd>
              <dt className="text-muted-foreground">Candidates on this case</dt>
              {/* `null` is not zero: the server answering nothing and the server
                  answering none are different facts, per this file's siblings. */}
              <dd>{candidateCount === null ? "Not reported" : candidateCount}</dd>
              <dt className="text-muted-foreground">Dependency fingerprint</dt>
              <dd className="font-mono">
                {prepared.dependency_fingerprint
                  ? `${prepared.dependency_fingerprint.slice(0, 12)}…`
                  : "Not reported"}
              </dd>
            </dl>
            {decisionKind === APPROVE_MAPPING && (
              <Status as="block" tone="info" title="This decision calls the Data owner">
                Publishing the mapping is a Data command. Governance records what that command
                actually reported, including a failure — check the owner outcome afterwards.
              </Status>
            )}
            {prepared.expires_at && (
              <p className="text-caption text-muted-foreground">
                This confirmation expires at {prepared.expires_at}. After that, prepare again.
              </p>
            )}
          </div>
        ) : (
          <div className="flex flex-col gap-4">
            <div className="flex flex-col gap-1.5">
              <Label htmlFor="decision-kind">Decision kind</Label>
              <NativeSelect
                id="decision-kind"
                value={kindChoice}
                onChange={(event) => setKindChoice(event.target.value)}
              >
                <option value={APPROVE_MAPPING}>Approve mapping — hands off to the Data owner</option>
                <option value="__other">Other…</option>
              </NativeSelect>
              {kindChoice === "__other" && (
                <>
                  <Input
                    aria-label="Decision kind slug"
                    value={customKind}
                    onChange={(event) => setCustomKind(event.target.value)}
                    placeholder="e.g. accept_conflict"
                  />
                  <p className="text-caption text-muted-foreground">
                    Lowercase letters, digits and underscores, 2 to 48 characters. No kind other
                    than <code>approve_mapping</code> triggers an owner command.
                  </p>
                </>
              )}
            </div>

            {candidates.length > 0 && (
              <div className="flex flex-col gap-1.5">
                <Label htmlFor="decision-candidate">Candidate change (optional)</Label>
                <NativeSelect
                  id="decision-candidate"
                  value={candidateId}
                  onChange={(event) => setCandidateId(event.target.value)}
                >
                  <option value="">None</option>
                  {candidates.map((candidate) => (
                    <option key={String(candidate.id)} value={String(candidate.id ?? "")}>
                      {String(candidate.candidate_kind ?? "candidate")} —{" "}
                      {String(candidate.owner_object_type ?? "")} {String(candidate.owner_object_id ?? "")}
                    </option>
                  ))}
                </NativeSelect>
              </div>
            )}

            <div className="flex flex-col gap-1.5">
              <Label htmlFor="decision-effective">Effective from</Label>
              <Input
                id="decision-effective"
                type="date"
                value={effectiveFrom}
                onChange={(event) => setEffectiveFrom(event.target.value)}
              />
            </div>

            <div className="flex flex-col gap-1.5">
              <Label htmlFor="decision-reason">Reason</Label>
              <Textarea
                id="decision-reason"
                value={reason}
                onChange={(event) => setReason(event.target.value)}
                placeholder="Why this case is decided this way."
              />
              <p className="text-caption text-muted-foreground">
                {trimmedReason.length}/{REASON_MAX} — recorded verbatim and never edited.
              </p>
            </div>
          </div>
        )}

        <DialogFooter>
          {/* Nothing is left to cancel once the decision is recorded: the act is
              append-only and this dialog closing changes none of it. */}
          <Button
            variant="secondary"
            onClick={close}
            disabled={busy}
            data-testid="case-decision-dismiss"
          >
            {ownerFailure ? "Close" : "Cancel"}
          </Button>
          {ownerFailure ? null : prepared ? (
            <Button onClick={handleConfirm} disabled={busy}>
              {busy ? "Recording…" : "Confirm decision"}
            </Button>
          ) : (
            <Button onClick={handlePrepare} disabled={!canPrepare}>
              {busy ? "Preparing…" : "Review impact"}
            </Button>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
