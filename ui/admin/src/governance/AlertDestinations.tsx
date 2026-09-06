/**
 * Alert destinations — where a project's alerts go once they have been written.
 *
 * Story 59.6. The word is `Alert destination` and it is posed once: the table
 * `app.alert_destinations`, the route `/api/alert-destinations`, this component
 * and its test all carry it. `Channel` and `Webhook` are already ratified for
 * the INPUT side of a Datastream (glossary, "One word, one meaning"), so an
 * outgoing Slack target could not be called a channel without breaking, on the
 * day it shipped, the rule story 59.5 spent itself enforcing.
 *
 * IT LIVES IN GOVERNANCE because `alignment-register.md:98` is ratified and says
 * "alert rules in Governance". It is rendered inside Controls & Quality › Data
 * Quality, which is where the monitors that write these firings are governed —
 * not as a fifth lens, because Governance has exactly four Level 2 screens and
 * "object types and optional capabilities appear inside them, never as
 * additional permanent navigation".
 *
 * THREE STATES, AND TWO OF THEM ARE DISTINCT SENTENCES.
 *
 *   * populated — one row per destination, its target MASKED, its routing rule,
 *     and when it last received something;
 *   * EMPTY — "No destination configured…", plus the REAL number of firings the
 *     project wrote in the last 24 hours. Never a zero standing in for an
 *     absence: the number is measured on the project asked for;
 *   * BROKEN — "Destinations could not be read". The add button is disabled and
 *     NO empty list is rendered: a screen that cannot read must not look like a
 *     screen that read and found nothing.
 *
 * AND IT NAMES WHAT IT DOES NOT ROUTE. The four infrastructure signals carry no
 * project (`AlertSignal` has no `project_id`), so they stay Console-only. A
 * surface that listed destinations without saying so would lie by omission.
 */
import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Button,
  Checkbox,
  ConfirmDialog,
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  EmptyState,
  Field,
  Input,
  NativeSelect,
  Panel,
  PanelBody,
  PanelHeader,
  Status,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
  TableScroll,
  formatTimestamp,
  wireWord,
} from "../ui";
import { ApiError, apiDelete, apiGet, apiPost } from "../lib/apiFetch";

type DestinationKind = "email" | "slack_webhook" | "webhook";

type LastDelivery = {
  delivered_at: string | null;
  state: string | null;
  last_error_class: string | null;
  delivered_count: number;
  failed_count: number;
};

type Destination = {
  id: string;
  project_id: string;
  kind: DestinationKind;
  label: string;
  target_masked: string;
  alert_types: string[];
  enabled: boolean;
  created_at: string | null;
  has_secret?: boolean;
  last_delivery: LastDelivery | null;
};

type DestinationsEnvelope = {
  destinations: Destination[];
  firings_last_24h: number;
  routable_alert_types: string[];
  console_only_signals: string[];
  kinds: DestinationKind[];
};

type TestVerdict = {
  code: string;
  delivered: boolean;
  detail: string;
  destination_id: string;
};

const KIND_LABEL: Record<DestinationKind, string> = {
  email: "Email",
  slack_webhook: "Slack webhook",
  webhook: "Webhook",
};

/** The sentence a person reads for each refusal the transport can answer. */
const VERDICT_SENTENCE: Record<string, string> = {
  delivered: "Delivered.",
  transport_unavailable: "Not sent — this deployment carries no email transport.",
  alerts_disabled: "Not sent — alerts are switched off on this deployment.",
  http_error: "Not sent — the destination refused the request.",
  unreachable: "Not sent — the destination could not be reached.",
  send_failed: "Not sent — the transport failed.",
};

function verdictSentence(verdict: TestVerdict): string {
  const head = VERDICT_SENTENCE[verdict.code] ?? "Not sent.";
  return verdict.detail ? `${head} ${verdict.detail}` : head;
}

function ruleSentence(destination: Destination): string {
  return destination.alert_types.length === 0
    ? "Every alert of this project"
    : destination.alert_types.join(", ");
}

function lastDeliverySentence(destination: Destination): string {
  const last = destination.last_delivery;
  if (!last) return "Never sent";
  if (last.state === "delivered" && last.delivered_at) {
    return `${formatTimestamp(last.delivered_at)} (${last.delivered_count} delivered)`;
  }
  if (last.last_error_class) {
    return `Last attempt failed: ${last.last_error_class} (${last.failed_count} pending)`;
  }
  return "Never sent";
}

export default function AlertDestinations({ projectId }: { projectId: string }) {
  const [envelope, setEnvelope] = useState<DestinationsEnvelope | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [addOpen, setAddOpen] = useState(false);
  const [pendingDelete, setPendingDelete] = useState<Destination | null>(null);
  const [verdicts, setVerdicts] = useState<Record<string, TestVerdict>>({});
  const [busyId, setBusyId] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const body = await apiGet<DestinationsEnvelope>(
        `/api/alert-destinations?project_id=${encodeURIComponent(projectId)}`,
      );
      setEnvelope(body);
      setError(null);
    } catch (err) {
      // BROKEN is not EMPTY. The envelope is dropped so no list — empty or
      // otherwise — can be rendered from a read that did not happen.
      setEnvelope(null);
      setError(err instanceof ApiError ? err.message : "The request did not complete.");
    } finally {
      setLoading(false);
    }
  }, [projectId]);

  useEffect(() => {
    void load();
  }, [load]);

  const sendTest = useCallback(
    async (destination: Destination) => {
      setBusyId(destination.id);
      try {
        const verdict = await apiPost<TestVerdict>(
          `/api/alert-destinations/${destination.id}/test?project_id=${encodeURIComponent(projectId)}`,
        );
        setVerdicts((current) => ({ ...current, [destination.id]: verdict }));
      } catch (err) {
        setVerdicts((current) => ({
          ...current,
          [destination.id]: {
            code: "request_failed",
            delivered: false,
            detail: err instanceof ApiError ? err.message : "The request did not complete.",
            destination_id: destination.id,
          },
        }));
      } finally {
        setBusyId(null);
      }
    },
    [projectId],
  );

  const confirmDelete = useCallback(async () => {
    if (!pendingDelete) return;
    setBusyId(pendingDelete.id);
    try {
      await apiDelete(
        `/api/alert-destinations/${pendingDelete.id}?project_id=${encodeURIComponent(projectId)}`,
      );
      setPendingDelete(null);
      await load();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "The request did not complete.");
    } finally {
      setBusyId(null);
    }
  }, [pendingDelete, projectId, load]);

  const destinations = envelope?.destinations ?? [];
  const consoleOnly = envelope?.console_only_signals ?? [];

  return (
    <div className="flex flex-col gap-4" data-testid="alert-destinations">
      <Panel flush>
        <PanelHeader
          title="Alert destinations"
          description="Where this project's alerts go once a monitor has written one. Routing is on the alert type, never on its severity."
        />
        <PanelBody className="flex flex-col gap-4">
          <div className="flex items-center justify-between gap-3">
            <p className="m-0 text-caption text-text-secondary">
              {envelope
                ? `${destinations.length} destination${destinations.length === 1 ? "" : "s"} configured.`
                : " "}
            </p>
            <Button
              variant="secondary"
              disabled={!envelope}
              onClick={() => setAddOpen(true)}
              data-testid="add-destination"
            >
              Add destination
            </Button>
          </div>

          {loading && (
            <p role="status" className="m-0 text-body text-text-secondary">
              Loading alert destinations…
            </p>
          )}

          {/* BROKEN — its own sentence, and no list of any kind underneath. */}
          {!loading && error && (
            <Status
              as="block"
              tone="error"
              title="Destinations could not be read"
              data-testid="destinations-broken"
              action={
                <Button variant="secondary" onClick={() => void load()}>
                  Retry
                </Button>
              }
            >
              {error} No destination list is shown, because none was read — this is not a project
              without destinations.
            </Status>
          )}

          {/* EMPTY — the other sentence, with a measured number beside it. */}
          {!loading && envelope && destinations.length === 0 && (
            <EmptyState
              title="No destination configured"
              description={
                <span data-testid="destinations-empty">
                  Alerts stay in the console and in <code>app.alert_firings</code>. This project
                  wrote {envelope.firings_last_24h} firing
                  {envelope.firings_last_24h === 1 ? "" : "s"} in the last 24 hours, and none of
                  them left this deployment.
                </span>
              }
            />
          )}

          {!loading && envelope && destinations.length > 0 && (
            <TableScroll label="Alert destinations">
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Destination</TableHead>
                    <TableHead>Kind</TableHead>
                    <TableHead>Target</TableHead>
                    <TableHead>Receives</TableHead>
                    <TableHead>Last sent</TableHead>
                    <TableHead>Actions</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {destinations.map((destination) => (
                    <TableRow key={destination.id} data-testid={`destination-${destination.id}`}>
                      <TableCell>
                        <span className="font-semibold text-text">{destination.label}</span>
                        {!destination.enabled && (
                          <Status tone="warning" className="ml-2">
                            Disabled
                          </Status>
                        )}
                      </TableCell>
                      <TableCell className="text-text-secondary">
                        {KIND_LABEL[destination.kind] ?? wireWord(destination.kind)}
                      </TableCell>
                      <TableCell className="text-text-secondary">
                        {destination.target_masked}
                      </TableCell>
                      <TableCell className="text-text-secondary">
                        {ruleSentence(destination)}
                      </TableCell>
                      <TableCell className="text-text-secondary">
                        {lastDeliverySentence(destination)}
                      </TableCell>
                      <TableCell>
                        <div className="flex flex-col gap-1">
                          <div className="flex gap-2">
                            <Button
                              variant="secondary"
                              disabled={busyId === destination.id}
                              onClick={() => void sendTest(destination)}
                              data-testid={`test-${destination.id}`}
                            >
                              Send a test
                            </Button>
                            <Button
                              variant="secondary"
                              onClick={() => setPendingDelete(destination)}
                              data-testid={`delete-${destination.id}`}
                            >
                              Remove
                            </Button>
                          </div>
                          {verdicts[destination.id] && (
                            <Status
                              tone={verdicts[destination.id].delivered ? "success" : "warning"}
                              data-testid={`verdict-${destination.id}`}
                            >
                              {verdictSentence(verdicts[destination.id])}
                            </Status>
                          )}
                        </div>
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </TableScroll>
          )}

          {/* What this screen does NOT route, stated rather than omitted. */}
          {envelope && consoleOnly.length > 0 && (
            <Status as="block" tone="neutral" title="What no destination can receive">
              {consoleOnly.join(", ")} are measured for the whole deployment and carry no project,
              so they reach the console only. A destination is project-scoped and cannot be offered
              them.
            </Status>
          )}
        </PanelBody>
      </Panel>

      {envelope && (
        <AddDestinationDialog
          open={addOpen}
          projectId={projectId}
          routableAlertTypes={envelope.routable_alert_types}
          kinds={envelope.kinds}
          onClose={() => setAddOpen(false)}
          onCreated={() => {
            setAddOpen(false);
            void load();
          }}
        />
      )}

      {/* The confirmation names WHAT STOPS LEAVING, and counts before the act. */}
      <ConfirmDialog
        open={pendingDelete !== null}
        onOpenChange={(open) => !open && setPendingDelete(null)}
        title={pendingDelete ? `Remove “${pendingDelete.label}”?` : "Remove destination?"}
        description={
          pendingDelete
            ? `${ruleSentence(pendingDelete)} will stop leaving this deployment through ${
                KIND_LABEL[pendingDelete.kind] ?? pendingDelete.kind
              } ${pendingDelete.target_masked}. ${
                pendingDelete.last_delivery?.delivered_count ?? 0
              } alert(s) have already been delivered to it; that history goes with it.`
            : ""
        }
        confirmLabel="Remove destination"
        destructive
        busy={busyId === pendingDelete?.id}
        onConfirm={() => void confirmDelete()}
        data-testid="delete-destination-confirm"
      />
    </div>
  );
}

function AddDestinationDialog({
  open,
  projectId,
  routableAlertTypes,
  kinds,
  onClose,
  onCreated,
}: {
  open: boolean;
  projectId: string;
  routableAlertTypes: string[];
  kinds: DestinationKind[];
  onClose: () => void;
  onCreated: () => void;
}) {
  const [kind, setKind] = useState<DestinationKind>("email");
  const [label, setLabel] = useState("");
  const [target, setTarget] = useState("");
  const [secret, setSecret] = useState("");
  const [selected, setSelected] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [failure, setFailure] = useState<string | null>(null);

  const carriesSecret = kind !== "email";
  const targetLabel = useMemo(
    () => (kind === "email" ? "Address" : "HTTPS URL"),
    [kind],
  );

  const submit = useCallback(async () => {
    setBusy(true);
    setFailure(null);
    try {
      await apiPost("/api/alert-destinations", {
        project_id: projectId,
        kind,
        label,
        target,
        alert_types: selected,
        secret: carriesSecret && secret ? secret : undefined,
      });
      setLabel("");
      setTarget("");
      setSecret("");
      setSelected([]);
      onCreated();
    } catch (err) {
      setFailure(err instanceof ApiError ? err.message : "The request did not complete.");
    } finally {
      setBusy(false);
    }
  }, [projectId, kind, label, target, selected, secret, carriesSecret, onCreated]);

  if (!open) return null;
  return (
    <Dialog open={open} onOpenChange={(next) => !next && onClose()}>
      <DialogContent data-testid="add-destination-dialog">
        <DialogHeader>
          <DialogTitle>Add an alert destination</DialogTitle>
          <DialogDescription>
            A destination receives this project's alerts. A key is written once and never read
            back.
          </DialogDescription>
        </DialogHeader>
        <div className="flex flex-col gap-3">
          <Field label="Kind">
            {(props) => (
              <NativeSelect
                {...props}
                value={kind}
                onChange={(event) => setKind(event.target.value as DestinationKind)}
              >
                {kinds.map((candidate) => (
                  <option key={candidate} value={candidate}>
                    {KIND_LABEL[candidate] ?? wireWord(candidate)}
                  </option>
                ))}
              </NativeSelect>
            )}
          </Field>
          <Field label="Name">
            {(props) => (
              <Input {...props} value={label} onChange={(event) => setLabel(event.target.value)} />
            )}
          </Field>
          <Field
            label={targetLabel}
            hint={
              kind === "email"
                ? "One address. An email destination carries no key."
                : "https only — a key sent over http is a key disclosed."
            }
          >
            {(props) => (
              <Input
                {...props}
                value={target}
                onChange={(event) => setTarget(event.target.value)}
              />
            )}
          </Field>
          {carriesSecret && (
            <Field label="Key (optional)" hint="Sent as a bearer token. Written once, never read back.">
              {(props) => (
                <Input
                  {...props}
                  type="password"
                  value={secret}
                  onChange={(event) => setSecret(event.target.value)}
                />
              )}
            </Field>
          )}
          <fieldset className="flex flex-col gap-2 border-0 p-0">
            <legend className="text-label font-label text-text">Receives</legend>
            <p className="m-0 text-caption text-text-secondary">
              Select nothing to receive every alert of this project. Routing is on the alert type:
              severity is the same word for every quality monitor and cannot tell them apart.
            </p>
            {routableAlertTypes.map((alertType) => (
              <label key={alertType} className="flex items-center gap-2 text-ui text-text">
                <Checkbox
                  checked={selected.includes(alertType)}
                  onCheckedChange={(checked) =>
                    setSelected((current) =>
                      checked
                        ? [...current, alertType]
                        : current.filter((entry) => entry !== alertType),
                    )
                  }
                />
                {alertType}
              </label>
            ))}
          </fieldset>
          {failure && <Status tone="error">{failure}</Status>}
        </div>
        <DialogFooter>
          <Button variant="secondary" onClick={onClose}>
            Cancel
          </Button>
          <Button disabled={busy} onClick={() => void submit()} data-testid="submit-destination">
            {busy ? "Saving…" : "Add destination"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
