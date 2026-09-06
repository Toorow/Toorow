/**
 * The Overview of a Business Domain and of a Master Data Object — the identity
 * as its owner states it, and the commands that act on THAT identity, here.
 *
 * WHY IT EXISTS. Until 2026-08-30 this Overview was the generic
 * `Object.entries(detail.summary)` dump, and the only console caller of
 * `runNodeCommand` was the Context Hub. So the workbench a person opens to
 * govern a Business Domain could not rename, archive or restore it: it named
 * the object and sent them to another screen for the gesture it claims to own.
 * `governance.md` ("Every object has stable identity, scope, owner, status and
 * an auditable lifecycle") and story 49.2 AC10 ("Overview shows stable identity,
 * type, scope, owner, ... lifecycle ... and eligible commands") both asked for
 * this; the tab answered with a record.
 *
 * ONE ENDPOINT, NEVER TWO WRITE PATHS. Every command here goes through
 * `masterDataApi.runNodeCommand` — the same door the Context Hub calls, which is
 * the same authority route. Nothing on this screen writes an identity any other
 * way, and the refusal sentences come from `masterDataRefusals.ts` so the two
 * doors say the same thing about the same code.
 *
 * WHICH COMMANDS ARE ELIGIBLE IS DERIVED, AND THAT IS NAMED. The read model's
 * `allowed_actions` is empty for both composers (`governance_read_model.py`
 * `_business_domain` / `_classification` / `_client_object_instance` pass none),
 * so eligibility is derived from the ONE state it does expose:
 * `lifecycle_status`, which `business_identity_catalogue.DOMAIN_SOURCE` derives
 * from `archived_at` itself — `archived` offers restore, anything else offers
 * rename and archive. Deriving it in the browser is second best and it is
 * written here rather than hidden: the day the composer declares the actions,
 * this reads them.
 */
import { useState } from "react";

import {
  Badge,
  Button,
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  Input,
  Panel,
  PanelHeader,
  Status,
  Textarea,
  displayValue,
} from "../ui";
import { ApiError } from "../lib/apiFetch";
import { FieldTable, type FieldRow } from "./FieldTable";
import { mintCommandKey, runNodeCommand, type NodeCommandAction } from "./masterDataApi";
import {
  EVIDENCE_UNREADABLE_SENTENCE,
  MISSING_REASON_SENTENCE,
  isImpactRefusal,
  isStaleVersionRefusal,
  masterDataRefusalSentence,
  refusedConsumers,
  type RefusedConsumer,
} from "./masterDataRefusals";
import type { GovernanceObject } from "./governanceSurface";

function summaryOf(detail: GovernanceObject): Record<string, unknown> {
  return (detail.summary ?? {}) as Record<string, unknown>;
}

function text(value: unknown): string | null {
  if (typeof value !== "string") return null;
  const trimmed = value.trim();
  return trimmed.length > 0 ? trimmed : null;
}

/** The typed rows of an identity, chosen by name.
 *
 *  Both composers are covered by ONE list because a reader does not care which
 *  one answered: a Business Domain carries a short code and a count of
 *  classifications, a client-declared object carries its kind, its collection
 *  and its aliases, and a row is rendered only where its owner sent a value.
 *  A field absent from this list is absent on purpose — the dump this replaced
 *  printed `hierarchy`, `aliases` and `sources` as raw records, three facets
 *  that have tabs of their own. */
function identityRows(detail: GovernanceObject): FieldRow[] {
  const summary = summaryOf(detail);
  const owner = (detail.owner ?? {}) as Record<string, unknown>;
  const rows: FieldRow[] = [
    ["Name", detail.object_ref.label],
    ["Identifier", detail.object_ref.id],
  ];
  const push = (name: string, value: unknown, meaning?: string) => {
    if (value === null || value === undefined || value === "") return;
    rows.push(meaning ? [name, value, meaning] : [name, value]);
  };
  push("Short code", summary.slug, "The stable word other screens address this identity by.");
  push("Description", summary.description);
  push("Type", summary.classification_type);
  push("Business domain", summary.business_domain);
  push("Object kind", summary.object_kind);
  push("Collection", summary.registry_label);
  push(
    "A version covers",
    summary.version_scope === "node"
      ? "this object alone"
      : summary.version_scope === "registry"
        ? "every object of this kind"
        : null,
  );
  // Scope, status and the active version are NOT repeated here: the workbench
  // header states all three, two panels above, on every tab. Printing them
  // again would be the same notion asked twice on one screen.
  push("Steward", owner.steward, "Who answers for this identity.");
  push("Owner", owner.kind);
  if (typeof summary.classification_count === "number") {
    push("Classifications under it", summary.classification_count);
  }
  if (typeof summary.alias_count === "number") push("Aliases", summary.alias_count);
  if (typeof summary.live_source_count === "number") {
    push("Live sources", summary.live_source_count);
  }
  push("Last recorded change", detail.evidence_as_of);
  return rows;
}

const COMMAND_TITLE: Record<NodeCommandAction, string> = {
  rename: "Rename",
  archive: "Archive",
  restore: "Restore",
};

/** What each command does, in the words of the person doing it — never the
 *  authority's verb. A confirmation that restates the button teaches nothing. */
function consequence(action: NodeCommandAction, typeLabel: string): string {
  switch (action) {
    case "rename":
      return `Publishes a new version of this ${typeLabel} carrying the name, the description and the type below. Every screen that resolves it reads the new name; nothing that pointed at it stops pointing at it.`;
    case "archive":
      return `Stops this ${typeLabel} being offered anywhere it is not already used. It is not deleted: its versions, its evidence and its history stay readable, and it can be restored.`;
    case "restore":
      return `Returns this ${typeLabel} to active governance, so it can be edited, linked and offered again.`;
  }
}

/**
 * One eligible command, from the button to the authority's answer.
 *
 * THE CONFIRMATION IS NOT DECORATION. Archiving is consequential and the
 * dialog states, before anything is sent, exactly what this object is and what
 * the command does to it — the ScopeSummary pattern the shell's consequential
 * acts use. The reason is required by this screen because the audit trail is
 * only worth what is written into it.
 *
 * THE IMPACT REFUSAL IS THE USEFUL MOMENT. A 409 `master_data_command_refused`
 * is the only place an operator learns what would break, so the named consumers
 * are rendered in full and acknowledging is a second, deliberate click — never a
 * checkbox ticked in advance. The retry carries the SAME idempotency key, which
 * is what the authority's replay path is for.
 */
function IdentityCommand({
  projectId,
  detail,
  action,
  typeLabel,
  onDone,
}: {
  projectId: string;
  detail: GovernanceObject;
  action: NodeCommandAction;
  typeLabel: string;
  onDone: () => void;
}) {
  const summary = summaryOf(detail);
  const [open, setOpen] = useState(false);
  const [name, setName] = useState(detail.object_ref.label);
  const [description, setDescription] = useState(text(summary.description) ?? "");
  const [classificationType, setClassificationType] = useState(
    text(summary.classification_type) ?? "",
  );
  const [reason, setReason] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [consumers, setConsumers] = useState<RefusedConsumer[]>([]);
  const [acknowledgeable, setAcknowledgeable] = useState(false);
  // A stale-version refusal ENDS this submission: the sentence tells the person
  // to reload and read the current version, and a button that still posts would
  // send the same base back to be refused again.
  const [stale, setStale] = useState(false);
  const [busy, setBusy] = useState(false);
  // ONE key per submission, reused by every retry of it — including the
  // acknowledged retry. A key minted per attempt would let a client timeout
  // apply the same command twice.
  const [commandKey, setCommandKey] = useState<string | null>(null);

  const renamesClassification = Boolean(text(summary.classification_type));
  const currentVersionId = text(summary.current_version_id);

  function start() {
    setName(detail.object_ref.label);
    setDescription(text(summary.description) ?? "");
    setClassificationType(text(summary.classification_type) ?? "");
    setReason("");
    setError(null);
    setConsumers([]);
    setAcknowledgeable(false);
    setStale(false);
    setCommandKey(null);
    setOpen(true);
  }

  async function submit(acknowledge: boolean) {
    if (!reason.trim()) {
      setError(MISSING_REASON_SENTENCE);
      return;
    }
    if (action === "rename" && !name.trim()) {
      setError("Give this object a name. A rename that clears it publishes a version with none.");
      return;
    }
    const key = commandKey ?? mintCommandKey(`md-${action}`);
    setCommandKey(key);
    setBusy(true);
    setError(null);
    try {
      await runNodeCommand(
        projectId,
        detail.object_ref.id,
        action === "rename"
          ? {
              action,
              reason,
              label: name.trim(),
              description,
              // THE BASE THIS DOOR READ (`governance.md`, 2026-08-30): the
              // authority's current revision, by id, exactly as the composer
              // gave it — never the `version` of the active version ref, which
              // is the union counter this surface only SHOWS. `null` states
              // that the object read here had no published revision.
              expected_version: currentVersionId,
              // The three fields live in ONE published version: sending only
              // the label would publish a version that erased the other two.
              ...(renamesClassification ? { classification_type: classificationType } : {}),
            }
          : { action, reason, acknowledge_impact: acknowledge },
        key,
      );
      setOpen(false);
      onDone();
    } catch (caught) {
      if (isImpactRefusal(caught)) {
        setConsumers(refusedConsumers(caught));
        setAcknowledgeable(action === "archive");
        setError(masterDataRefusalSentence(caught));
      } else if (isStaleVersionRefusal(caught)) {
        setStale(true);
        setError(masterDataRefusalSentence(caught));
      } else if (caught instanceof ApiError && caught.status === 503) {
        // Fail closed, and say so: "could not check" must never read as "done".
        setError(EVIDENCE_UNREADABLE_SENTENCE);
      } else {
        setError(masterDataRefusalSentence(caught, "Nothing was changed."));
      }
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <Button variant="secondary" onClick={start} data-testid={`master-data-${action}`}>
        {COMMAND_TITLE[action]}
      </Button>

      <Dialog open={open} onOpenChange={(next) => { if (!next) setOpen(false); }}>
        <DialogContent
          className="max-h-[90vh] overflow-y-auto"
          aria-describedby="master-data-command-consequence"
        >
          <DialogHeader>
            <DialogTitle>{`${COMMAND_TITLE[action]} ${detail.object_ref.label}`}</DialogTitle>
            <DialogDescription id="master-data-command-consequence">
              {consequence(action, typeLabel)}
            </DialogDescription>
          </DialogHeader>

          <div className="flex flex-col gap-4">
            {/* The ScopeSummary of this act: what is being changed, read-only,
                before it is changed. */}
            <dl
              className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-ui"
              data-testid="master-data-command-scope"
            >
              <dt className="text-text-secondary">{typeLabel}</dt>
              <dd className="text-text">{detail.object_ref.label}</dd>
              <dt className="text-text-secondary">Scope</dt>
              <dd className="text-text">{displayValue(detail.scope)}</dd>
              <dt className="text-text-secondary">Status today</dt>
              <dd className="text-text">{detail.lifecycle_status.replaceAll("_", " ")}</dd>
            </dl>

            {action === "rename" && (
              <>
                <label className="flex flex-col gap-1.5 text-label font-label text-text">
                  Name
                  <Input value={name} onChange={(event) => setName(event.target.value)} />
                </label>
                {renamesClassification && (
                  <label className="flex flex-col gap-1.5 text-label font-label text-text">
                    Type
                    <Input
                      value={classificationType}
                      onChange={(event) => setClassificationType(event.target.value)}
                    />
                  </label>
                )}
                <label className="flex flex-col gap-1.5 text-label font-label text-text">
                  Description
                  <Textarea
                    value={description}
                    rows={3}
                    onChange={(event) => setDescription(event.target.value)}
                  />
                </label>
              </>
            )}

            <label className="flex flex-col gap-1.5 text-label font-label text-text">
              Reason
              <Textarea
                value={reason}
                rows={2}
                onChange={(event) => setReason(event.target.value)}
                placeholder="Why this governed change is needed"
              />
            </label>

            {error && (
              <Status
                as="block"
                tone="error"
                title={acknowledgeable ? `Archiving this ${typeLabel} would break its consumers` : "Nothing was changed"}
              >
                {error}
              </Status>
            )}
            {consumers.length > 0 && (
              <ul className="text-ui text-text-secondary" data-testid="master-data-refused-consumers">
                {consumers.map((consumer, index) => (
                  <li key={`${consumer.consumer_id ?? index}`}>
                    {/* The same word as `MasterDataTabs` and `CountryWorkspace`
                        for the same state: a consumer the used-by store holds no
                        label for. It never reads as an identifier here. */}
                    {consumer.consumer_label ?? "Unnamed consumer"}
                    <Badge outline className="ml-2">{displayValue(consumer.consumer_kind)}</Badge>
                  </li>
                ))}
              </ul>
            )}
          </div>

          <DialogFooter>
            <Button type="button" variant="secondary" onClick={() => setOpen(false)}>
              Cancel
            </Button>
            <Button
              type="button"
              variant={acknowledgeable ? "destructive" : "default"}
              disabled={busy || stale}
              onClick={() => void submit(acknowledgeable)}
            >
              {busy
                ? "Working…"
                : acknowledgeable
                  ? "Archive anyway"
                  : COMMAND_TITLE[action]}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}

/**
 * The Overview of a `business-domain` or a `master-data-object`.
 *
 * `projectId` is what makes the commands addressable, and its absence is not a
 * reason to draw a button that would go nowhere — the identity still reads.
 */
export function MasterDataIdentityOverview({
  detail,
  projectId,
  typeLabel,
  onChanged,
}: {
  detail: GovernanceObject;
  projectId?: string;
  typeLabel: string;
  onChanged?: () => void;
}) {
  const archived = detail.lifecycle_status === "archived";
  const actions: NodeCommandAction[] = archived ? ["restore"] : ["rename", "archive"];
  const done = onChanged ?? (() => undefined);

  return (
    <>
      <Panel flush>
        <PanelHeader
          title="Identity"
          description={`What this ${typeLabel} is, as its owner states it.`}
        />
        <FieldTable caption={`${typeLabel} identity`} rows={identityRows(detail)} />
      </Panel>

      <Panel data-testid="master-data-identity-commands">
        <PanelHeader
          title={`Change this ${typeLabel}`}
          description={
            archived
              ? "It is archived: it is not offered anywhere, and nothing about it was deleted."
              : "Each command publishes a new governed version and keeps the reason you give with it."
          }
        />
        {projectId ? (
          <div className="flex flex-wrap gap-2">
            {actions.map((action) => (
              <IdentityCommand
                key={action}
                action={action}
                projectId={projectId}
                detail={detail}
                typeLabel={typeLabel}
                onDone={done}
              />
            ))}
          </div>
        ) : (
          <Status as="block" tone="info" title="No Project scope to command in">
            This {typeLabel} is being read outside a Project, and a governed command is addressed
            through one. Open it from Governance ▸ Master Data to change it.
          </Status>
        )}
      </Panel>
    </>
  );
}

export default MasterDataIdentityOverview;
