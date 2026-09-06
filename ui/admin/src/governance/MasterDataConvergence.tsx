/**
 * The operator's convergence gesture, on the Master Data screen.
 *
 * WHY IT IS HERE AND NOWHERE ELSE. `docs/product-architecture/governance.md`,
 * amendment of 2026-08-25, acceptance schedule ratified the same day: *"An
 * operator runs the convergence per organization from the Master Data screen —
 * controlled, observable, reversible by supersession."* The command and its plan
 * shipped with `core/master_data_convergence.py` and
 * `governance_surface_api.py`; no screen called either, so the ratified gesture
 * existed for `curl` and for nobody else.
 *
 * THE PLAN IS READ BEFORE THE GESTURE IS OFFERED, and that is the same sentence
 * the server module writes about itself: *"a command whose only way to be
 * understood is to run it is a command nobody can consent to."* So this panel
 * opens on what WOULD move — how many Business Domains, how many
 * Classifications, what has already moved, what refuses and why — and the button
 * appears under it, never above it and never alone.
 *
 * THREE STATES ARE TOLD APART, because collapsing any two of them is how a
 * governance screen starts reporting something it never measured:
 *
 *   1. `unreadable` is not `converged`. If the plan could not be read, the panel
 *      says so and offers a retry. Drawing the reassuring green line there would
 *      claim an organization is governed when nobody asked the store.
 *   2. `converged` is not `nothing to say`. An organization whose taxonomy has
 *      moved is told it has, with the counts — that is the answer to "where did
 *      my Business Domains go".
 *   3. An organization that never held a legacy taxonomy at all (nothing pending
 *      AND nothing converged) gets no panel. There is no gesture to name and no
 *      state to report; a banner there would be furniture.
 *
 * WHY IT REFUSES BEFORE THE CLICK. `plan_convergence` reports the classifications
 * whose parent it cannot reach, and `converge_org_taxonomy` raises 409 rather
 * than dropping their branch in silence. The plan carries that list, so the panel
 * states the refusal and names the repair INSTEAD of offering a button whose only
 * outcome is a refusal.
 *
 * WHY THE REASON IS ASKED HERE AND NOT IN THE CONFIRMATION. The amendment
 * describes the act as carrying *"an actor, a reason, an audit row, an outbox
 * event and an idempotency key"*. The reason is the only one of the five a person
 * supplies. It is asked in the panel — one question, answered before the next one
 * appears — so the confirmation can restate the whole act (the organization, the
 * counts, the reason) instead of asking a second question inside a dialog whose
 * job is to confirm.
 *
 * THE IDEMPOTENCY KEY IS MINTED ONCE AND REUSED FOR EVERY RETRY. That is what
 * the server's replay path is for: a retry after a client timeout is the SAME
 * command carried through, and a fresh key on each attempt would be the exact
 * shape `master_data_convergence.py` names — "a retry after a client timeout
 * mints a second identity". It is dropped only after a success.
 */
import { useCallback, useEffect, useMemo, useState } from "react";

import { ApiError, apiGet, apiPost } from "../lib/apiFetch";
import { useOptionalScope } from "../shell/scope";
import {
  Button,
  ConfirmDialog,
  Input,
  Label,
  Panel,
  PanelBody,
  PanelHeader,
  Status,
  notify,
} from "../ui";

/** What `GET .../governance/master-data/convergence` answers (`ConvergencePlan.as_dict`). */
export interface ConvergencePlan {
  org_id: string;
  pending_domains: number;
  pending_classifications: number;
  already_converged_domains: number;
  already_converged_classifications: number;
  unreachable_parents: string[];
}

export type ConvergenceRead =
  | { status: "loading" }
  | { status: "ready"; plan: ConvergencePlan }
  | { status: "unreadable"; message: string };

interface ConvergenceResult {
  operation_id?: string;
  outcome?: string;
  idempotent_replay?: boolean;
  result?: {
    converged_domains?: number;
    converged_classifications?: number;
  } | null;
}

function convergencePath(projectId: string): string {
  return `/api/projects/${encodeURIComponent(projectId)}/governance/master-data/convergence`;
}

function whole(value: unknown): number {
  const parsed = Number(value);
  return Number.isFinite(parsed) && parsed > 0 ? Math.trunc(parsed) : 0;
}

/** The wire payload, read field by field. A missing count is 0 and never NaN:
 *  "N business domains" printed as "NaN business domains" is worse than silence. */
function readPlan(payload: unknown): ConvergencePlan {
  const raw = (payload ?? {}) as Record<string, unknown>;
  return {
    org_id: typeof raw.org_id === "string" ? raw.org_id : "",
    pending_domains: whole(raw.pending_domains),
    pending_classifications: whole(raw.pending_classifications),
    already_converged_domains: whole(raw.already_converged_domains),
    already_converged_classifications: whole(raw.already_converged_classifications),
    unreachable_parents: Array.isArray(raw.unreachable_parents)
      ? raw.unreachable_parents.map(String)
      : [],
  };
}

/** True when nothing is left to move. An organization that never held a legacy
 *  taxonomy is converged by this measure, and correctly so: it has nothing to
 *  converge and its writes belong to the Master Data authority already. */
export function planIsConverged(plan: ConvergencePlan): boolean {
  return plan.pending_domains + plan.pending_classifications === 0;
}

/** True when the panel has something to say. Nothing pending and nothing moved
 *  is an organization this amendment never touched. */
export function planIsWorthSaying(plan: ConvergencePlan): boolean {
  return (
    !planIsConverged(plan) ||
    plan.already_converged_domains + plan.already_converged_classifications > 0
  );
}

function countOf(value: number, singular: string, plural: string): string {
  return `${value} ${value === 1 ? singular : plural}`;
}

function domainsAndClassifications(domains: number, classifications: number): string {
  return `${countOf(domains, "Business Domain", "Business Domains")} and ${countOf(
    classifications,
    "Classification",
    "Classifications",
  )}`;
}

/**
 * Every refusal this pair of routes can produce, said as the gesture that repairs it.
 *
 * The server's own words are a developer's — "Governance is unavailable",
 * "Governance object not found" — and none of them names what the person in
 * front of the screen does next. The 404 hides existence on purpose
 * (`governance_surface_api._not_found`), so its sentence claims neither that the
 * Project is missing nor that the right is missing: it names the one check that
 * resolves either.
 */
function repairSentence(err: unknown, act: "read" | "converge"): string {
  const nothing =
    act === "converge" ? "so nothing was converged" : "so nothing could be reported";
  if (!(err instanceof ApiError)) {
    return act === "converge"
      ? "Nothing was converged. Try again."
      : "The convergence state could not be read. Try again.";
  }
  if (err.status === 0) {
    return `The server could not be reached, ${nothing}. Check your connection and try again.`;
  }
  if (err.status === 401) {
    return "Your session has expired. Sign in again, then run this once more.";
  }
  if (err.status === 403) {
    return act === "converge"
      ? "Converging an organization is a Manage right. Ask an organization owner to run it, or to grant you Manage on this project."
      : "Your access to this Project does not include Governance. Ask an organization owner for it.";
  }
  if (err.status === 404) {
    return `This Project cannot be reached from this account, ${nothing}. Ask an organization owner for access to it, then try again.`;
  }
  if (err.status === 409) {
    // The server sends the refusal already written for a reader, and it names
    // the identities involved. Replacing it here would drop the one detail an
    // operator needs, so it is carried through and only framed.
    return err.message || "This organization's taxonomy cannot converge as it stands.";
  }
  if (err.status === 428) {
    return `The command was sent without its replay key, ${nothing}. Try again.`;
  }
  if (err.status === 503) {
    return `Master Data could not be read, ${nothing}. Wait a moment and try again.`;
  }
  return act === "converge"
    ? "Nothing was converged. Try again."
    : "The convergence state could not be read. Try again.";
}

/**
 * Reads the plan for the Project's organization.
 *
 * `enabled` rather than a conditional call: the Master Data section asks for it,
 * the other three Governance sections do not, and a hook cannot be called
 * conditionally. A disabled hook holds `loading` and issues no request.
 */
export function useMasterDataConvergence(
  projectId: string,
  enabled: boolean,
): { read: ConvergenceRead; reload: () => void } {
  const [read, setRead] = useState<ConvergenceRead>({ status: "loading" });
  const [nonce, setNonce] = useState(0);
  const reload = useCallback(() => setNonce((value) => value + 1), []);

  useEffect(() => {
    if (!enabled || !projectId) return;
    let cancelled = false;
    setRead({ status: "loading" });
    apiGet<unknown>(convergencePath(projectId))
      .then((payload) => {
        if (!cancelled) setRead({ status: "ready", plan: readPlan(payload) });
      })
      .catch((err: unknown) => {
        if (!cancelled) setRead({ status: "unreadable", message: repairSentence(err, "read") });
      });
    return () => {
      cancelled = true;
    };
  }, [projectId, enabled, nonce]);

  return { read, reload };
}

export default function MasterDataConvergencePanel({
  projectId,
  read,
  onReload,
  onConverged,
}: {
  projectId: string;
  read: ConvergenceRead;
  /** Re-read the plan — after a run, and from the retry of an unreadable state. */
  onReload: () => void;
  /** The object list below holds the same identities; it is re-read too. */
  onConverged: () => void;
}) {
  const scope = useOptionalScope();
  const [reason, setReason] = useState("");
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  /** Minted on the first attempt, kept across retries, dropped after a success. */
  const [idempotencyKey, setIdempotencyKey] = useState<string | null>(null);

  const plan = read.status === "ready" ? read.plan : null;
  const orgName = useMemo(() => {
    const routed = scope?.org;
    if (routed && (!plan?.org_id || routed.id === plan.org_id)) return routed.name;
    return null;
  }, [scope?.org, plan?.org_id]);
  const orgLabel = orgName ?? plan?.org_id ?? "this organization";

  if (read.status === "loading") return null;

  if (read.status === "unreadable") {
    return (
      <Status
        as="block"
        tone="warning"
        title="Whether this organization has converged could not be read"
        data-testid="convergence-unreadable"
        action={
          <Button variant="secondary" onClick={onReload}>
            Retry
          </Button>
        }
      >
        {read.message} This is not a statement that it has converged, and nothing should be read
        as one.
      </Status>
    );
  }

  const current = read.plan;
  if (!planIsWorthSaying(current)) return null;

  if (planIsConverged(current)) {
    return (
      <Status
        as="block"
        tone="success"
        title="Governed by Master Data"
        data-testid="convergence-converged"
      >
        {domainsAndClassifications(
          current.already_converged_domains,
          current.already_converged_classifications,
        )}{" "}
        are governed by Master Data. Nothing is left to converge, and running it again would move
        nothing.
      </Status>
    );
  }

  const blocked = current.unreachable_parents.length > 0;

  async function run() {
    const key = idempotencyKey ?? `md-converge-${current.org_id}-${Date.now()}`;
    setIdempotencyKey(key);
    setBusy(true);
    setError(null);
    try {
      const result = await apiPost<ConvergenceResult>(
        convergencePath(projectId),
        { reason: reason.trim() },
        { headers: { "Content-Type": "application/json", "Idempotency-Key": key } },
      );
      const moved = result?.result ?? {};
      notify(
        result?.idempotent_replay
          ? "This convergence had already been recorded. Nothing was converged twice."
          : `Converged: ${domainsAndClassifications(
              whole(moved.converged_domains),
              whole(moved.converged_classifications),
            )} are now governed by Master Data.`,
      );
      setConfirming(false);
      setIdempotencyKey(null);
      setReason("");
      onReload();
      onConverged();
    } catch (err: unknown) {
      setError(repairSentence(err, "converge"));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Panel flush data-testid="convergence-panel">
      <PanelHeader
        title="This organization has not converged into Master Data"
        description="Business Domains and Classifications are still held by the older store. Until they converge, writing one is refused."
      />
      <PanelBody className="flex flex-col gap-4">
        <p className="m-0 text-ui text-text-secondary" data-testid="convergence-plan">
          {domainsAndClassifications(current.pending_domains, current.pending_classifications)}{" "}
          would move into Master Data.
          {current.already_converged_domains + current.already_converged_classifications > 0 ? (
            <>
              {" "}
              {domainsAndClassifications(
                current.already_converged_domains,
                current.already_converged_classifications,
              )}{" "}
              already moved and are left alone.
            </>
          ) : null}
        </p>

        <p className="m-0 text-caption text-text-secondary">
          Every identity keeps the id it has today, so nothing that points at a Business Domain
          stops resolving. Nothing is deleted: each row stays readable and names what now holds its
          authority.
        </p>

        {blocked ? (
          <Status
            as="block"
            tone="error"
            title="It cannot converge as it stands"
            data-testid="convergence-blocked"
          >
            {countOf(current.unreachable_parents.length, "Classification", "Classifications")}{" "}
            {current.unreachable_parents.length === 1 ? "names" : "name"} a parent this
            organization no longer holds, so converging would drop that branch:{" "}
            {current.unreachable_parents.join(", ")}. Give each one a parent that exists — in
            Context Hub, on the Classification itself — then run this again.
          </Status>
        ) : (
          <>
            <div className="space-y-1.5">
              <Label htmlFor="convergence-reason">Reason for this convergence</Label>
              <Input
                id="convergence-reason"
                placeholder="Why this organization is converging now"
                value={reason}
                onChange={(event) => setReason(event.target.value)}
                aria-required="true"
                className="max-w-lg"
              />
            </div>

            {error && !confirming && (
              <Status as="block" tone="error" title="Nothing was converged">
                {error}
              </Status>
            )}

            <div>
              <Button
                variant="default"
                onClick={() => {
                  if (!reason.trim()) {
                    setError(
                      "Add a reason — it is recorded with the convergence so the change stays auditable.",
                    );
                    return;
                  }
                  setError(null);
                  setConfirming(true);
                }}
              >
                Converge to Master Data
              </Button>
            </div>
          </>
        )}
      </PanelBody>

      {/* The confirmation restates the whole act: WHICH organization, HOW MANY
          objects, and what the run does not do. A confirmation that says only
          "are you sure?" is a blind button with an extra click in front of it. */}
      <ConfirmDialog
        open={confirming}
        onOpenChange={(open) => {
          if (!open) setConfirming(false);
        }}
        title={`Converge ${orgLabel}?`}
        description={
          <>
            {domainsAndClassifications(current.pending_domains, current.pending_classifications)}{" "}
            of {orgLabel}
            {orgName && current.org_id ? ` (${current.org_id})` : ""} move into Master Data. They
            keep their ids and stay readable where they are today; nothing is deleted, and running
            this again converges nothing. Recorded reason: “{reason.trim()}”.
          </>
        }
        confirmLabel="Converge"
        cancelLabel="Not now"
        busy={busy}
        error={error}
        onConfirm={() => void run()}
        data-testid="convergence-confirm"
        confirmTestId="convergence-confirm-run"
        cancelTestId="convergence-confirm-cancel"
      />
    </Panel>
  );
}
