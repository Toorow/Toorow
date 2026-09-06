/**
 * What can be DONE to a Datastream as an object — rename, archive, restore.
 *
 * THE GAP THIS CLOSES, MEASURED 2026-08-18. `grep -rn "archive\|rename\|duplicate"
 * ui/admin/src/datastreams/**` returned comments and an unrelated `duplicate_target`
 * observation code, and nothing else: there was no rename, no archive, no
 * restore and no delete anywhere in the Datastream surface. Meanwhile
 * `SchedulePanel` tells a person, in two of its four run states, that an
 * archived Datastream "collects nothing and cannot be started from here.
 * Restoring it is what makes it runnable again" — naming a gesture the console
 * did not offer, for a state the console could not leave. A Fleet could only
 * grow, and every screen described the archive as reversible.
 *
 * WHAT THE SERVER SERVES, AND WHAT IT DOES NOT — the order this was built in.
 *   * rename   → `PATCH /api/datastreams/{id}` has accepted `name` since story
 *                8.2, and answers `409` on a collision. Nothing was missing.
 *   * archive  → `DELETE /api/datastreams/{id}` has soft-archived since 21.5.
 *   * delete   → the SAME route. It is one gesture in the console because it is
 *                one route on the server, and which of the two happens is not a
 *                choice anybody makes — see `DISPOSITION_IS_NOT_A_CHOICE`.
 *   * restore  → served by nothing. Added in the same commit as this file
 *                (`POST /api/datastreams/{id}/restore`), because a console that
 *                archives and cannot restore is the trap the surface warns about.
 *   * duplicate → NOT OFFERED, and deliberately not stubbed. A Datastream is a
 *                plan version, a mapping version, a schedule state, a connection
 *                binding and a name; copying it is a decision about which of
 *                those five travel, and no route makes it. Rendering a control
 *                with no executor is the defect this repository keeps finding,
 *                so there is no `Duplicate` item here at all.
 *
 * AND THE DATA ROLE, added 2026-08-31 — amendment 9 of the 2026-08-11 review:
 * « Le rôle et le mode s'éditent depuis le Workbench, par le même changement
 * gouverné que le reste : un changement préparé, une confirmation qui nomme ce
 * qui bouge en aval, une version. »
 *
 * The Workbench rendered the role as DESCRIPTION TEXT under the title
 * (« Performance data · owner ») and offered nothing that touched it, while the
 * role decides whether the fee ladder reads this Datastream at all. The gesture
 * is here rather than on a tab for the same reason rename and archive are: the
 * role is a property of the Datastream as an object, and it is visible from all
 * eight readings.
 *
 * THE MODE AND THE CONNECTOR ARE THE OTHER HALF, AND THEY ARE NOT A CONTROL. The
 * server says so on the same block (`mode_and_connector`), with the gesture that
 * replaces it — re-sourcing a Datastream rewrites what every collected day, plan
 * version and mart key describes, so it is a governed change of its own and the
 * change seam refuses it by name today. The menu STATES that, once, where a
 * person looks for it; it does not render a selector nothing executes.
 */
import { useState } from "react";
import { MoreHorizontalIcon } from "lucide-react";
import {
  Button, ConfirmDialog, DropdownMenu, DropdownMenuContent, DropdownMenuItem,
  DropdownMenuLabel, DropdownMenuSeparator, DropdownMenuTrigger, Field, Input, NativeSelect,
  formatTimestamp,
} from "../../ui";
import { ApiError, apiJson } from "../../lib/apiFetch";
import type { WorkbenchHeader } from "./workbenchTypes";

/** A fact the console does not have, said as such. Same word `repullDay` uses. */
const NOT_REPORTED = "Not reported";

/**
 * WHY THE CONSOLE DOES NOT OFFER "ARCHIVE" AND "DELETE" AS TWO ITEMS.
 *
 * `core.datastreams.delete_datastream` asks the catalogue which of the 34 tables
 * carrying a restricting foreign key hold a row for this Datastream, plus the
 * pull history that would be orphaned, and archives when anything does. It is
 * not a preference — it is what the append-only rule (AD-7) permits for THIS
 * row, today. Two menu items would ask a person to pick an outcome they do not
 * control, and one of the two would then silently do the other.
 *
 * So one item, and the dialog states BOTH outcomes and the rule that picks. The
 * answer comes back on the response (`{"status": "archived"|"deleted"}`) —
 * story AI-200 put it there precisely because "a caller cannot re-derive a
 * decision it did not make".
 */
const DISPOSITION_IS_NOT_A_CHOICE =
  "If any history references this Datastream — a collected day, a plan version, " +
  "a file receipt — it is ARCHIVED: it stops collecting and stops holding its " +
  "name, and everything it has already produced stays readable. If nothing " +
  "references it, there is no history to keep and it is DELETED outright. " +
  "Which of the two happens is decided from what is actually stored, not here.";

export type LifecycleVerb = "rename" | "archive" | "restore" | "data_role";

/** What one gesture answered, in the words the header will show. */
export interface LifecycleOutcome {
  verb: LifecycleVerb;
  tone: "success" | "neutral";
  message: string;
}

/**
 * Is this Datastream archived, read from the header the route already loaded?
 *
 * THE AXIS IS THE EVIDENCE, and it is the only thing on this payload that says
 * so. `WorkbenchHeader` carries no `archived` flag; `axes.lifecycle` is composed
 * by `datastream_workbench.py` from the same row, and `SchedulePanel` prints the
 * same word from `schedule.run_state`. Compared case-insensitively because this
 * screen must not depend on the server's capitalisation of a display word.
 */
export function isArchived(header: WorkbenchHeader): boolean {
  return (header.axes?.lifecycle ?? "").trim().toLowerCase() === "archived";
}

/** The evidence an archive confirmation shows: what stops, named from the header.
 *
 *  Every row is a fact the header already carries. Nothing here is a count this
 *  screen invented, and nothing is blank: a fact that was not sent says so, or a
 *  reader takes an empty cell for a zero. */
function whatStops(header: WorkbenchHeader): Record<string, unknown> {
  const ops = header.operations_evidence ?? {};
  const pubs = header.publications ?? {};
  return {
    Datastream: header.identity.name?.trim() || header.identity.datastream_id,
    Connector: header.identity.connector ?? header.identity.module ?? NOT_REPORTED,
    "Source account": header.identity.source_account_ref || NOT_REPORTED,
    // The run that will not happen. `schedule_state_known: false` is a third
    // answer and never renders as "no next run".
    "Next collection": !ops.schedule_state_known
      ? "No schedule state was ever written for this Datastream"
      : ops.next_run_at
        ? `${formatTimestamp(ops.next_run_at)} — this run will not happen`
        : "Nothing is placed on the clock",
    // What downstream is reading TODAY. This is the consequence a person most
    // often has not thought about, so it is stated rather than left to Outputs.
    "Currently served": pubs.current
      ? `Publication ${pubs.current} — it stays readable, and no new one replaces it`
      : "Nothing is being served",
    "Latest run": header.runs?.latest
      ? `${header.runs.latest_state ?? "Unknown state"} (${header.runs.latest})`
      : "None",
  };
}

async function renameDatastream(projectId: string, datastreamId: string, name: string) {
  return apiJson<{ name?: string }>(`/api/datastreams/${encodeURIComponent(datastreamId)}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ project_id: projectId, name }),
  });
}

async function archiveDatastream(projectId: string, datastreamId: string) {
  return apiJson<{ status?: string }>(
    `/api/datastreams/${encodeURIComponent(datastreamId)}` +
      `?project_id=${encodeURIComponent(projectId)}`,
    { method: "DELETE" },
  );
}

/**
 * Change the role, stating the base — amendment 9.
 *
 * `expected_data_role` is the value the SERVER published beside the options, not
 * one this file read off `identity.data_role`: the base of a governed change is
 * a claim about what was on screen, and a base the screen composed itself is not
 * that claim. A `409 stale_data_role` comes back whole and is shown as the
 * server wrote it, naming what the role reads now.
 */
async function changeDataRole(
  projectId: string,
  datastreamId: string,
  role: string,
  expected: string,
) {
  return apiJson<{ data_role_change?: { effect?: string; source_type_after?: string } }>(
    `/api/datastreams/${encodeURIComponent(datastreamId)}`,
    {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        project_id: projectId,
        data_role: role,
        expected_data_role: expected,
      }),
    },
  );
}

async function restoreDatastream(projectId: string, datastreamId: string) {
  return apiJson<{ name?: string }>(
    `/api/datastreams/${encodeURIComponent(datastreamId)}/restore`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ project_id: projectId }),
    },
  );
}

function reasonOf(error: unknown, fallback: string): string {
  // The SERVER'S sentence, whole. `409 name_taken` on a restore names which
  // Datastream took the name and what to do about it; `HTTP 409` alone tells a
  // person nothing they can act on. Same rule as `repullDay.requestRepull`.
  return error instanceof ApiError || error instanceof Error ? error.message : fallback;
}

/**
 * The three gestures, on the header, behind one overflow trigger.
 *
 * BESIDE `ObjectHeader`, NEVER INSIDE IT — the rule story 59.2 settled one
 * component over for `DatastreamIssueBadge`: `ObjectHeader` is mounted by twelve
 * files, and a prop added there exposes eleven other screens to a layout change
 * for a need that concerns one.
 */
export default function DatastreamLifecycleMenu({
  header,
  projectId,
  datastreamId,
  onDone,
  onGone,
}: {
  header: WorkbenchHeader;
  projectId: string;
  datastreamId: string;
  /** The Datastream is still here and its header must be re-read. */
  onDone: (outcome: LifecycleOutcome) => void;
  /** It was DELETED outright — there is no header to re-read. The route decides
   *  where a person lands, because only the shell can build that address. */
  onGone?: (message: string) => void;
}) {
  const archived = isArchived(header);
  const currentName = header.identity.name?.trim() || "";

  const roleChange = header.data_role_change ?? null;
  const currentRole = roleChange?.current ?? header.identity.data_role ?? null;

  const [open, setOpen] = useState<LifecycleVerb | null>(null);
  const [draftName, setDraftName] = useState(currentName);
  /** The role picked in the dialog. Empty until one is, so nothing is pre-chosen:
   *  a default here would be an answer nobody gave, which is the rule the wizard's
   *  own role `<select>` already holds. */
  const [draftRole, setDraftRole] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  function close() {
    if (busy) return;
    setOpen(null);
    setError(null);
  }

  function ask(verb: LifecycleVerb) {
    setError(null);
    if (verb === "rename") setDraftName(currentName);
    if (verb === "data_role") setDraftRole("");
    setOpen(verb);
  }

  async function confirm() {
    if (busy || !open) return;
    setBusy(true);
    setError(null);
    try {
      if (open === "rename") {
        const next = draftName.trim();
        // Refused HERE because the server would accept it as "no change" and
        // answer 200 — a success for a gesture that did nothing.
        if (!next) throw new Error("A Datastream needs a name. Type the one it should carry.");
        if (next === currentName) throw new Error(`This Datastream is already called « ${next} ».`);
        await renameDatastream(projectId, datastreamId, next);
        setOpen(null);
        onDone({ verb: "rename", tone: "success", message: `This Datastream is now called « ${next} ».` });
      } else if (open === "data_role") {
        if (!roleChange) throw new Error("This Datastream's role cannot be changed from here.");
        if (!draftRole) throw new Error("Choose the role this Datastream should carry.");
        const answer = await changeDataRole(
          projectId,
          datastreamId,
          draftRole,
          roleChange.expected_data_role,
        );
        setOpen(null);
        onDone({
          verb: "data_role",
          tone: "success",
          message:
            `This Datastream now carries « ${draftRole} ». ` +
            // THE SERVER'S SENTENCE, because it computed the pairing. A message
            // written here would be this screen's second opinion on which
            // Datastreams the cost cascade contains.
            (answer.data_role_change?.effect ?? ""),
        });
      } else if (open === "archive") {
        const answer = await archiveDatastream(projectId, datastreamId);
        setOpen(null);
        // THE ROUTE SAYS WHICH OF THE TWO IT DID, so the console reports what
        // happened instead of the outcome it hoped for.
        if (answer.status === "deleted") {
          const gone =
            `« ${currentName || datastreamId} » held no history, so it was deleted outright. ` +
            "There is nothing left to restore.";
          if (onGone) onGone(gone);
          else onDone({ verb: "archive", tone: "neutral", message: gone });
        } else {
          onDone({
            verb: "archive",
            tone: "neutral",
            message:
              `« ${currentName || datastreamId} » is archived. It collects nothing and no ` +
              "longer holds its name, everything it produced stays readable, and Restore " +
              "brings it back.",
          });
        }
      } else {
        await restoreDatastream(projectId, datastreamId);
        setOpen(null);
        onDone({
          verb: "restore",
          tone: "success",
          message:
            "This Datastream is restored, and it is NOT collecting yet — restoring brings " +
            "it back, starting it is the Schedule panel's own gesture on the Collect stage.",
        });
      }
    } catch (reason) {
      setError(reasonOf(reason, "The request failed."));
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <Button
            variant="secondary"
            size="icon-sm"
            aria-label={`What to do with ${currentName || datastreamId}`}
            data-testid="datastream-lifecycle-menu"
          >
            <MoreHorizontalIcon />
          </Button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="end" className="w-80">
          <DropdownMenuLabel>This Datastream</DropdownMenuLabel>
          <DropdownMenuItem data-testid="lifecycle-rename" onSelect={() => ask("rename")}>
            Rename it
          </DropdownMenuItem>
          {/* THE ROLE, and only when the server sent the review that governs it.
              No review, no item: a control whose confirmation cannot name what
              moves downstream is exactly the ungoverned door this change closed
              on the seam. */}
          {roleChange && !archived ? (
            <DropdownMenuItem data-testid="lifecycle-data-role" onSelect={() => ask("data_role")}>
              Change what kind of data it carries
            </DropdownMenuItem>
          ) : null}
          <DropdownMenuSeparator />
          {archived ? (
            <DropdownMenuItem data-testid="lifecycle-restore" onSelect={() => ask("restore")}>
              Restore it
            </DropdownMenuItem>
          ) : (
            <DropdownMenuItem
              variant="destructive"
              data-testid="lifecycle-archive"
              onSelect={() => ask("archive")}
            >
              Stop it and archive it
            </DropdownMenuItem>
          )}
        </DropdownMenuContent>
      </DropdownMenu>

      <ConfirmDialog
        open={open === "rename"}
        onOpenChange={(next) => { if (!next) close(); }}
        title="Rename this Datastream"
        description={
          <>
            <span className="block">
              The name is what everyone who reads this Datastream sees. Nothing it has
              collected, mapped or published changes, and no address changes.
            </span>
            <Field label="Name">
              {(field) => (
                <Input
                  {...field}
                  value={draftName}
                  autoFocus
                  onChange={(event) => setDraftName(event.target.value)}
                  data-testid="lifecycle-rename-input"
                />
              )}
            </Field>
          </>
        }
        evidenceLabel="What this renames"
        evidence={{
          "Current name": currentName || NOT_REPORTED,
          Connector: header.identity.connector ?? header.identity.module ?? NOT_REPORTED,
          Identifier: `${datastreamId} — unchanged`,
        }}
        confirmLabel="Rename it"
        busy={busy}
        error={error}
        onConfirm={() => void confirm()}
        data-testid="lifecycle-rename-confirm"
        cancelTestId="lifecycle-rename-cancel"
        confirmTestId="lifecycle-rename-go"
      />

      <ConfirmDialog
        open={open === "data_role"}
        onOpenChange={(next) => { if (!next) close(); }}
        title="Change what kind of data this Datastream carries"
        description={
          <>
            <span className="block">
              {/* THE DOWNSTREAM, NAMED BEFORE THE WRITE — the amendment's own
                  requirement. It is the server's pairing sentence, per role,
                  because the consequence is decided by the pair (the connector's
                  manifest category and the role) and not by the role alone. */}
              The role is half of the pair the fee ladder reads; the other half is the
              connector's own category
              {roleChange?.category ? ` (« ${roleChange.category} »)` : ""}. Each role below
              says what it would do to that reading.
            </span>
            <Field label="Role">
              {(field) => (
                <NativeSelect
                  {...field}
                  value={draftRole}
                  onChange={(event) => setDraftRole(event.target.value)}
                  data-testid="lifecycle-data-role-select"
                >
                  {/* NEVER PRE-FILLED — the same rule the creation wizard's role
                      selector holds: a default is an answer nobody gave. */}
                  <option value="">Choose a role…</option>
                  {(roleChange?.options ?? [])
                    .filter((option) => !option.is_current)
                    .map((option) => (
                      <option key={option.value} value={option.value}>
                        {option.value}
                      </option>
                    ))}
                </NativeSelect>
              )}
            </Field>
            {draftRole ? (
              <span className="block text-caption text-text-secondary" data-testid="lifecycle-data-role-effect">
                {roleChange?.options.find((option) => option.value === draftRole)?.effect}
              </span>
            ) : null}
          </>
        }
        evidenceLabel="What this changes"
        evidence={{
          "Current role": currentRole || NOT_REPORTED,
          "Connector category": roleChange?.category || NOT_REPORTED,
          "Fee ladder reads it as": roleChange?.source_type ?? NOT_REPORTED,
          // THE HALF THAT IS NOT A CONTROL, said where a person would look for
          // it rather than left as a silence they read as a missing feature.
          Mode: roleChange
            ? `${roleChange.mode_and_connector.mode} — ${roleChange.mode_and_connector.reason}`
            : NOT_REPORTED,
          "To change the mode or the connector":
            roleChange?.mode_and_connector.gesture ?? NOT_REPORTED,
        }}
        confirmLabel="Change the role"
        busy={busy}
        error={error}
        onConfirm={() => void confirm()}
        data-testid="lifecycle-data-role-confirm"
        cancelTestId="lifecycle-data-role-cancel"
        confirmTestId="lifecycle-data-role-go"
      />

      <ConfirmDialog
        open={open === "archive"}
        onOpenChange={(next) => { if (!next) close(); }}
        title="Stop this Datastream and archive it"
        description={DISPOSITION_IS_NOT_A_CHOICE}
        evidenceLabel="What stops"
        evidence={whatStops(header)}
        confirmLabel="Stop it and archive it"
        cancelLabel="Keep it collecting"
        destructive
        busy={busy}
        error={error}
        onConfirm={() => void confirm()}
        data-testid="lifecycle-archive-confirm"
        cancelTestId="lifecycle-archive-cancel"
        confirmTestId="lifecycle-archive-go"
      />

      <ConfirmDialog
        open={open === "restore"}
        onOpenChange={(next) => { if (!next) close(); }}
        title="Restore this Datastream"
        description={
          "It becomes a Datastream of this project again, with everything it had. It " +
          "does NOT start collecting: restoring brings it back, and starting it is a " +
          "separate consent on the Collect stage — nothing is asked of the provider " +
          "until you give it."
        }
        evidenceLabel="What comes back"
        evidence={{
          Datastream: currentName || datastreamId,
          Connector: header.identity.connector ?? header.identity.module ?? NOT_REPORTED,
          "Source account": header.identity.source_account_ref || NOT_REPORTED,
          // NAMED BEFORE THE CLICK, because it is the one refusal a person can
          // do something about — and migration 256 makes it a real case, not a
          // theoretical one: an archived Datastream stops reserving its name
          // precisely so the same feed can be rebuilt under it.
          "If the name was reused":
            "the restore is refused and says which Datastream holds it — rename one of " +
            "the two, then restore again",
          "After restoring": "Configured, not collecting",
        }}
        confirmLabel="Restore it"
        busy={busy}
        error={error}
        onConfirm={() => void confirm()}
        data-testid="lifecycle-restore-confirm"
        cancelTestId="lifecycle-restore-cancel"
        confirmTestId="lifecycle-restore-go"
      />
    </>
  );
}
