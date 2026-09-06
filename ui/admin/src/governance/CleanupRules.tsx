/**
 * Cleanup rules — Story 60.3.
 *
 * A cleanup rule is a NAME, a collected field, a regular expression and what to
 * do with the rows that match it: drop them, keep only them, or strip the match
 * out of the field. It is applied AT READ, which is why editing one costs
 * nothing — no refetch, no backfill — and why what it removes is COUNTABLE.
 *
 * THE WORD IS NOT `FILTER`, AND THAT IS MEASURED RATHER THAN PREFERRED.
 * `query_specs.py:57-58` holds `filter` for a READ filter over a Semantic View
 * and `datastream_intents.py:539` for a COLLECTION filter pushed to the
 * provider. A third `filter` would be a third object under one word — the
 * `Channel` collision of story 59.6, replayed. `glossary.md` carries the entry
 * and names the two senses already taken beside it.
 *
 * IT LIVES IN GOVERNANCE, AND ONLY IN GOVERNANCE. The `Processing` tab of the
 * Datastream Workbench SHOWS the same rules and offers no control of any kind:
 * `datastream-workbench-and-wizard.md:989` — "Governed rules are referenced, not
 * edited" — and `data.md:82-85` — "It applies published Governance policy; it
 * does not redefine it".
 *
 * THREE STATES, AND TWO OF THEM ARE DISTINCT SENTENCES.
 *
 *   * populated — one row per rule with its condition IN WORDS, its state and
 *     its measured effect;
 *   * EMPTY — "No cleanup rule on this Project yet.", and the sentence names who
 *     writes one: this lens;
 *   * BROKEN — "The transformation library could not be read." No list of any
 *     kind is rendered underneath, because a screen that could not read must not
 *     look like a screen that read and found nothing.
 *
 * AND THE EFFECT IS NEVER A DEFAULT ZERO. `effect_state` is `measured` or
 * `unmeasured`; the second renders the server's sentence and no number. A `0`
 * would tell a person this rule removes nothing, which is the single thing they
 * must not be told wrongly about a rule that removes rows.
 *
 * THE CONFIRMATION NAMES THE COUNT BEFORE THE ACT. A deletion says how many
 * Datastreams the rule reaches — the number read before, not after — and an
 * unknown reach really disables the deletion: "I could not check" and "nothing
 * depends on this" are different facts and only one of them is safe to act on.
 *
 * EDITING A PATTERN IS A VERSION — Story 60.5. `epic-60:101` asked for "add,
 * edit, disable" and only two of the three had a control; the edit is here now
 * because it is the gesture the version ledger exists for, and a ledger nothing
 * reaches would be a fifth unreachable object. The confirmation names the version
 * number that would be created, the content hash of the proposed body, the
 * Datastreams the rule reaches BY NAME — a Project-wide rule reaches every
 * Datastream of the Project — and the sentence about already collected days.
 *
 * ENABLING AND DISABLING IS NOT. It is the rule's lifecycle rather than its
 * content, so it writes no version and is not previewed as if it did. Migration
 * 242 states the same exclusion in its schema.
 */
import { useCallback, useEffect, useState } from "react";
import {
  Button,
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
} from "../ui";
import { ApiError, apiDelete, apiGet, apiPatch, apiPost } from "../lib/apiFetch";
import {
  RuleChangeConfirmation,
  RuleVersionHistory,
  fetchChangePreview,
  type ChangePreview,
} from "./RuleVersionHistory";

type RuleKind = "exclude_row" | "keep_row" | "strip_match";

const KIND_LABEL: Record<RuleKind, string> = {
  exclude_row: "Remove matching rows",
  keep_row: "Keep only matching rows",
  strip_match: "Strip the match out of the field",
};

export interface CleanupRule {
  id: string;
  name: string;
  source_field: string;
  rule_kind: RuleKind;
  pattern: string;
  enabled: boolean;
  condition: string;
  datastream_id: string | null;
  datastream_count: number | null;
  dry_run_state: "passed" | "not_attempted";
  dry_run_detail: string | null;
}

interface Effect {
  effect_state: "measured" | "unmeasured";
  affected_rows: number | null;
  window_days: number;
  message?: string;
}

const ROOT = (projectId: string) =>
  `/api/projects/${encodeURIComponent(projectId)}/cleanup-rules`;

/** The reach of a rule, or the word that is not a number.
 *  `0` is only ever printed when the server measured zero. */
function reachLabel(count: number | null | undefined): string {
  return count === null || count === undefined ? "unknown" : String(count);
}

export default function CleanupRules({ projectId }: { projectId: string }) {
  const [rules, setRules] = useState<CleanupRule[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [createOpen, setCreateOpen] = useState(false);
  const [pendingDelete, setPendingDelete] = useState<CleanupRule | null>(null);
  const [editing, setEditing] = useState<CleanupRule | null>(null);
  const [openHistoryId, setOpenHistoryId] = useState<string | null>(null);
  const [historyToken, setHistoryToken] = useState(0);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const body = await apiGet<{ rules: CleanupRule[] }>(ROOT(projectId));
      setRules(body.rules);
      setError(null);
    } catch (err) {
      // BROKEN is not EMPTY. The list is dropped so no empty table can be
      // rendered from a read that did not happen.
      setRules(null);
      setError(err instanceof ApiError ? err.message : "The request did not complete.");
    } finally {
      setLoading(false);
    }
  }, [projectId]);

  useEffect(() => {
    void load();
  }, [load]);

  const toggle = useCallback(
    async (rule: CleanupRule) => {
      setBusy(true);
      try {
        await apiPatch(`${ROOT(projectId)}/${rule.id}`, { enabled: !rule.enabled });
        await load();
      } catch (err) {
        setError(err instanceof ApiError ? err.message : "The request did not complete.");
      } finally {
        setBusy(false);
      }
    },
    [projectId, load],
  );

  const runDelete = useCallback(async () => {
    if (!pendingDelete) return;
    setBusy(true);
    try {
      await apiDelete(`${ROOT(projectId)}/${pendingDelete.id}`);
      setPendingDelete(null);
      await load();
    } catch (err) {
      setError(err instanceof ApiError ? err.message : "The request did not complete.");
      setPendingDelete(null);
    } finally {
      setBusy(false);
    }
  }, [pendingDelete, projectId, load]);

  return (
    <div className="flex flex-col gap-4" data-testid="cleanup-rules">
      <Panel flush>
        <PanelHeader
          title="Cleanup rules"
          description="A rule reads one collected field and removes rows, or strips a match out of the value. It is applied when the data is READ, so changing one costs nothing: no re-collection, and the raw rows are untouched."
        />
        <PanelBody className="flex flex-col gap-4">
          <div className="flex items-center justify-between gap-3">
            <p className="m-0 text-caption text-text-secondary">
              {rules
                ? `${rules.length} rule${rules.length === 1 ? "" : "s"} on this Project.`
                : " "}
            </p>
            <Button
              variant="default"
              disabled={!rules}
              onClick={() => setCreateOpen(true)}
              data-testid="new-cleanup-rule"
            >
              + New cleanup rule
            </Button>
          </div>

          {loading && (
            <p role="status" className="m-0 text-body text-text-secondary">
              Loading cleanup rules…
            </p>
          )}

          {/* BROKEN — its own sentence, and no list of any kind underneath. */}
          {!loading && error && (
            <Status
              as="block"
              tone="error"
              title="The transformation library could not be read."
              data-testid="cleanup-broken"
              action={
                <Button variant="secondary" onClick={() => void load()}>
                  Retry
                </Button>
              }
            >
              {error} No rule is listed, because none was read — this is not a Project without
              cleanup rules.
            </Status>
          )}

          {/* EMPTY — the other sentence, and it names who writes one. */}
          {!loading && rules && rules.length === 0 && (
            <EmptyState
              title="No cleanup rule on this Project yet."
              description={
                <span data-testid="cleanup-empty">
                  A cleanup rule is written here, in Governance › Semantic Model › Cleanup Rules.
                  Nothing has been hidden or substituted.
                </span>
              }
            />
          )}

          {!loading && rules && rules.length > 0 && (
            <TableScroll label="Cleanup rules">
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Rule</TableHead>
                    <TableHead>Field</TableHead>
                    <TableHead>Condition</TableHead>
                    <TableHead>State</TableHead>
                    <TableHead>Datastreams</TableHead>
                    <TableHead>Effect over 30 days</TableHead>
                    <TableHead>Actions</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {rules.map((rule) => (
                    <TableRow key={rule.id} data-testid={`cleanup-rule-${rule.id}`}>
                      <TableCell className="font-semibold text-text">{rule.name}</TableCell>
                      <TableCell className="font-mono text-text-secondary">
                        {rule.source_field}
                      </TableCell>
                      <TableCell className="text-text-secondary">{rule.condition}</TableCell>
                      <TableCell>
                        <Status tone={rule.enabled ? "success" : "neutral"}>
                          {rule.enabled ? "Enabled" : "Disabled"}
                        </Status>
                      </TableCell>
                      <TableCell className="font-numeric text-text-secondary">
                        {reachLabel(rule.datastream_count)}
                      </TableCell>
                      <TableCell className="text-text-secondary">
                        <RuleEffect projectId={projectId} ruleId={rule.id} />
                      </TableCell>
                      <TableCell>
                        <div className="flex gap-2">
                          <Button
                            variant="secondary"
                            disabled={busy}
                            onClick={() => void toggle(rule)}
                            data-testid={`toggle-${rule.id}`}
                          >
                            {rule.enabled ? "Disable" : "Enable"}
                          </Button>
                          <Button
                            variant="secondary"
                            disabled={busy}
                            onClick={() => setEditing(rule)}
                            data-testid={`edit-${rule.id}`}
                          >
                            Edit
                          </Button>
                          <Button
                            variant="secondary"
                            onClick={() =>
                              setOpenHistoryId((current) =>
                                current === rule.id ? null : rule.id,
                              )
                            }
                            data-testid={`history-${rule.id}`}
                          >
                            History
                          </Button>
                          {/* An unknown reach really disables the destruction.
                              Deleting a rule whose Datastreams could not be
                              counted is exactly the act "I could not check"
                              forbids. */}
                          <Button
                            variant="secondary"
                            disabled={busy || rule.datastream_count === null}
                            title={
                              rule.datastream_count === null
                                ? "The number of Datastreams this rule reaches could not be read. Nothing is deleted on an unknown reach."
                                : undefined
                            }
                            onClick={() => setPendingDelete(rule)}
                            data-testid={`delete-${rule.id}`}
                          >
                            Delete
                          </Button>
                        </div>
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </TableScroll>
          )}

          {/* Story 60.5. Its own read, because it is its own question: folding
              the history into the list payload would make an unreadable ledger
              look like a rule nobody ever edited. */}
          {openHistoryId && (
            <div className="flex flex-col gap-2" data-testid="cleanup-history-panel">
              <h3 className="m-0 text-body font-semibold text-text">Version history</h3>
              <RuleVersionHistory
                projectId={projectId}
                family="cleanup-rule"
                objectId={openHistoryId}
                reloadToken={historyToken}
              />
            </div>
          )}

          {/* What a rule does NOT do, said in the screen rather than in a
              documentation nobody opens. */}
          {!loading && rules && rules.length > 0 && (
            <p className="m-0 text-caption text-text-secondary" data-testid="cleanup-scope-note">
              A cleanup rule applies when the data is read. It never deletes anything collected,
              and it is not a collection filter: rows a provider drops before sending them are
              never returned, so they cannot be counted here and cannot be recovered without
              collecting those days again.
            </p>
          )}
        </PanelBody>
      </Panel>

      <CreateRuleDialog
        open={createOpen}
        projectId={projectId}
        onClose={() => setCreateOpen(false)}
        onCreated={() => {
          setCreateOpen(false);
          void load();
        }}
      />

      {editing && (
        <EditRuleDialog
          projectId={projectId}
          rule={editing}
          onClose={() => setEditing(null)}
          onSaved={() => {
            setEditing(null);
            setHistoryToken((token) => token + 1);
            void load();
          }}
        />
      )}

      {/* The confirmation names the COUNT BEFORE the act, never after. */}
      <ConfirmDialog
        open={pendingDelete !== null}
        onOpenChange={(open) => {
          if (!open) setPendingDelete(null);
        }}
        title={pendingDelete ? `Delete “${pendingDelete.name}”?` : "Delete cleanup rule?"}
        description={
          pendingDelete
            ? `This rule reaches ${reachLabel(pendingDelete.datastream_count)} Datastream${
                pendingDelete.datastream_count === 1 ? "" : "s"
              } today. Deleting it brings back the rows it was removing on the next reading; ` +
              "nothing collected is touched, because the rule never removed anything from the raw zone."
            : ""
        }
        confirmLabel="Delete cleanup rule"
        destructive
        busy={busy}
        onConfirm={() => void runDelete()}
        data-testid="delete-rule-confirm"
      />
    </div>
  );
}

/**
 * Editing one rule — Story 60.5, and the gesture `epic-60:101` named and 60.3
 * did not ship.
 *
 * The pattern is validated by the SERVER before the write, exactly as a new one
 * is (`cleanup_rules.update_rule` runs the same three validators), so nothing is
 * re-implemented here. What this dialog adds is the confirmation: the version the
 * change would record, its hash, the Datastreams it reaches by name and the
 * temporal-scope sentence — all four measured by the server before the act.
 */
function EditRuleDialog({
  projectId,
  rule,
  onClose,
  onSaved,
}: {
  projectId: string;
  rule: CleanupRule;
  onClose: () => void;
  onSaved: () => void;
}) {
  const [name, setName] = useState(rule.name);
  const [pattern, setPattern] = useState(rule.pattern);
  const [kind, setKind] = useState<RuleKind>(rule.rule_kind);
  const [busy, setBusy] = useState(false);
  const [failure, setFailure] = useState<string | null>(null);
  const [confirming, setConfirming] = useState(false);
  const [preview, setPreview] = useState<ChangePreview | null>(null);
  const [previewFailure, setPreviewFailure] = useState<string | null>(null);

  const change = { name, pattern, rule_kind: kind };
  const untouched =
    name === rule.name && pattern === rule.pattern && kind === rule.rule_kind;

  const askPreview = useCallback(async () => {
    setPreview(null);
    setPreviewFailure(null);
    setConfirming(true);
    try {
      setPreview(await fetchChangePreview(projectId, "cleanup-rule", rule.id, change));
    } catch (err) {
      setPreviewFailure(
        err instanceof ApiError ? err.message : "The request did not complete.",
      );
    }
    // `change` is rebuilt on every render from the three fields below it, so the
    // three fields are the dependency and the object is not.
  }, [projectId, rule.id, name, pattern, kind]); // eslint-disable-line react-hooks/exhaustive-deps

  const save = useCallback(async () => {
    setBusy(true);
    setFailure(null);
    try {
      await apiPatch(`${ROOT(projectId)}/${rule.id}`, change);
      setConfirming(false);
      onSaved();
    } catch (err) {
      setFailure(err instanceof ApiError ? err.message : "The request did not complete.");
    } finally {
      setBusy(false);
    }
  }, [projectId, rule.id, name, pattern, kind, onSaved]); // eslint-disable-line react-hooks/exhaustive-deps

  return (
    <>
      <Dialog open onOpenChange={(next) => !next && onClose()}>
        <DialogContent data-testid="edit-rule-dialog">
          <DialogHeader>
            <DialogTitle>Edit “{rule.name}”</DialogTitle>
            <DialogDescription>
              The rule is applied when the data is read, so a corrected pattern takes effect on
              the next reading. Nothing collected is rewritten, and the previous body stays
              readable in this rule&apos;s version history.
            </DialogDescription>
          </DialogHeader>
          <div className="flex flex-col gap-3">
            <Field label="Name">
              {(props) => (
                <Input
                  {...props}
                  value={name}
                  onChange={(event) => setName(event.target.value)}
                  data-testid="edit-rule-name"
                />
              )}
            </Field>
            <Field label="What the rule does">
              {(props) => (
                <NativeSelect
                  {...props}
                  value={kind}
                  onChange={(event) => setKind(event.target.value as RuleKind)}
                  data-testid="edit-rule-kind"
                >
                  {(Object.keys(KIND_LABEL) as RuleKind[]).map((value) => (
                    <option key={value} value={value}>
                      {KIND_LABEL[value]}
                    </option>
                  ))}
                </NativeSelect>
              )}
            </Field>
            <Field
              label="Pattern"
              hint="A regular expression, run by the warehouse (RE2). It is validated by the server before it is stored, exactly as a new one is."
            >
              {(props) => (
                <Input
                  {...props}
                  value={pattern}
                  onChange={(event) => setPattern(event.target.value)}
                  data-testid="edit-rule-pattern"
                />
              )}
            </Field>
            {failure && <Status tone="error">{failure}</Status>}
          </div>
          <DialogFooter>
            <Button variant="secondary" onClick={onClose}>
              Cancel
            </Button>
            <Button
              disabled={busy || untouched || !name.trim() || !pattern.trim()}
              onClick={() => void askPreview()}
              data-testid="submit-rule-edit"
            >
              Save
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <RuleChangeConfirmation
        open={confirming}
        title={`Change “${rule.name}”?`}
        onOpenChange={(next) => {
          if (!next) setConfirming(false);
        }}
        onConfirm={() => void save()}
        preview={preview}
        previewFailure={previewFailure}
        busy={busy}
        error={failure}
        confirmLabel="Record this version"
        testId="rule-version-confirm"
      />
    </>
  );
}

/**
 * The measured effect of one rule, asked of the warehouse.
 *
 * It is its own request because it is its own question: the Governance list is
 * read from Postgres and the effect is counted by the warehouse, and joining the
 * two in one payload would make an unreachable warehouse look like an empty
 * Project. `unmeasured` renders the server's SENTENCE and no number.
 */
function RuleEffect({ projectId, ruleId }: { projectId: string; ruleId: string }) {
  const [effect, setEffect] = useState<Effect | null>(null);
  const [failure, setFailure] = useState<string | null>(null);

  useEffect(() => {
    let alive = true;
    void (async () => {
      try {
        const body = await apiGet<Effect>(`${ROOT(projectId)}/${ruleId}/effect`);
        if (alive) setEffect(body);
      } catch (err) {
        if (alive) {
          setFailure(
            err instanceof ApiError ? err.message : "The request did not complete.",
          );
        }
      }
    })();
    return () => {
      alive = false;
    };
  }, [projectId, ruleId]);

  if (failure) {
    return (
      <span data-testid={`effect-${ruleId}`}>
        The effect of this rule could not be read. This is not a count of zero.
      </span>
    );
  }
  if (!effect) {
    return (
      <span role="status" data-testid={`effect-${ruleId}`}>
        Counting…
      </span>
    );
  }
  if (effect.effect_state === "unmeasured" || effect.affected_rows === null) {
    return (
      <span data-testid={`effect-${ruleId}`}>
        {effect.message ??
          "The rows this rule changes could not be counted. This is not a count of zero."}
      </span>
    );
  }
  return (
    <span className="font-numeric" data-testid={`effect-${ruleId}`}>
      {effect.affected_rows} row{effect.affected_rows === 1 ? "" : "s"} over{" "}
      {effect.window_days} days
    </span>
  );
}

/**
 * Writing one rule.
 *
 * The preview runs BEFORE the rule is saved, on real rows, and it fabricates
 * nothing: a warehouse that cannot answer says so and no row is drawn. That is
 * the same contract as the server's dry run, which submits the compiled SELECT
 * to the engine before the INSERT — the engine that will run the pattern is the
 * one that approves it.
 */
function CreateRuleDialog({
  open,
  projectId,
  onClose,
  onCreated,
}: {
  open: boolean;
  projectId: string;
  onClose: () => void;
  onCreated: () => void;
}) {
  const [name, setName] = useState("");
  const [sourceField, setSourceField] = useState("");
  const [pattern, setPattern] = useState("");
  const [kind, setKind] = useState<RuleKind>("exclude_row");
  const [busy, setBusy] = useState(false);
  const [failure, setFailure] = useState<string | null>(null);
  const [preview, setPreview] = useState<
    { rows: Array<Record<string, unknown>>; state: "available" } | { state: "unavailable"; message: string } | null
  >(null);

  const runPreview = useCallback(async () => {
    setBusy(true);
    setFailure(null);
    try {
      const body = await apiPost<{ rows: Array<Record<string, unknown>> }>(
        `${ROOT(projectId)}/preview`,
        { rule_kind: kind, source_field: sourceField, pattern, limit: 20 },
      );
      setPreview({ rows: body.rows, state: "available" });
    } catch (err) {
      setPreview({
        state: "unavailable",
        message:
          err instanceof ApiError ? err.message : "The request did not complete.",
      });
    } finally {
      setBusy(false);
    }
  }, [projectId, kind, sourceField, pattern]);

  const submit = useCallback(async () => {
    setBusy(true);
    setFailure(null);
    try {
      await apiPost(ROOT(projectId), {
        name,
        source_field: sourceField,
        rule_kind: kind,
        pattern,
      });
      setName("");
      setSourceField("");
      setPattern("");
      setPreview(null);
      onCreated();
    } catch (err) {
      setFailure(err instanceof ApiError ? err.message : "The request did not complete.");
    } finally {
      setBusy(false);
    }
  }, [projectId, name, sourceField, kind, pattern, onCreated]);

  if (!open) return null;
  return (
    <Dialog open={open} onOpenChange={(next) => !next && onClose()}>
      <DialogContent data-testid="create-rule-dialog">
        <DialogHeader>
          <DialogTitle>New cleanup rule</DialogTitle>
          <DialogDescription>
            The rule is applied when the data is read. It never rewrites what was collected, and
            the pattern is run by the warehouse itself.
          </DialogDescription>
        </DialogHeader>
        <div className="flex flex-col gap-3">
          <Field label="Name">
            {(props) => (
              <Input
                {...props}
                value={name}
                onChange={(event) => setName(event.target.value)}
                data-testid="rule-name"
              />
            )}
          </Field>
          <Field label="Field" hint="The collected column the rule reads. It does not have to be mapped to a canonical field.">
            {(props) => (
              <Input
                {...props}
                value={sourceField}
                onChange={(event) => setSourceField(event.target.value)}
                data-testid="rule-field"
              />
            )}
          </Field>
          <Field label="What the rule does">
            {(props) => (
              <NativeSelect
                {...props}
                value={kind}
                onChange={(event) => setKind(event.target.value as RuleKind)}
                data-testid="rule-kind"
              >
                {(Object.keys(KIND_LABEL) as RuleKind[]).map((value) => (
                  <option key={value} value={value}>
                    {KIND_LABEL[value]}
                  </option>
                ))}
              </NativeSelect>
            )}
          </Field>
          <Field
            label="Pattern"
            hint="A regular expression, run by the warehouse (RE2). Lookahead, lookbehind and backreferences are refused because RE2 does not implement them."
          >
            {(props) => (
              <Input
                {...props}
                value={pattern}
                onChange={(event) => setPattern(event.target.value)}
                data-testid="rule-pattern"
              />
            )}
          </Field>

          {pattern.trim() && sourceField.trim() && (
            <p className="m-0 text-caption text-text-secondary" data-testid="rule-condition">
              {kind === "exclude_row"
                ? `Keeps a row only when ${sourceField} does not match \`${pattern}\`.`
                : kind === "keep_row"
                  ? `Keeps a row only when ${sourceField} matches \`${pattern}\`.`
                  : `Removes from ${sourceField} every part that matches \`${pattern}\`.`}
            </p>
          )}

          <div>
            <Button
              variant="secondary"
              disabled={busy || !pattern.trim() || !sourceField.trim()}
              onClick={() => void runPreview()}
              data-testid="run-preview"
            >
              Preview on real rows
            </Button>
          </div>

          {preview?.state === "unavailable" && (
            <Status as="block" tone="warning" title="No preview was rendered" data-testid="preview-unavailable">
              {preview.message} No row is shown: a preview that invented values would be worse
              than none.
            </Status>
          )}
          {preview?.state === "available" && preview.rows.length === 0 && (
            <Status as="block" tone="neutral" data-testid="preview-empty">
              The warehouse answered and no row of this field was returned. That is an answer, not
              a failure.
            </Status>
          )}
          {preview?.state === "available" && preview.rows.length > 0 && (
            <TableScroll label="Cleanup rule preview">
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Value</TableHead>
                    <TableHead>Changed by this rule</TableHead>
                    {"becomes" in preview.rows[0] && <TableHead>Becomes</TableHead>}
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {preview.rows.map((row, index) => (
                    <TableRow key={`${String(row.breakdown_value)}-${index}`}>
                      <TableCell className="font-mono">{String(row.breakdown_value)}</TableCell>
                      <TableCell>{row.affected ? "Yes" : "No"}</TableCell>
                      {"becomes" in row && (
                        <TableCell className="font-mono">{String(row.becomes)}</TableCell>
                      )}
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </TableScroll>
          )}

          {failure && <Status tone="error">{failure}</Status>}
        </div>
        <DialogFooter>
          <Button variant="secondary" onClick={onClose}>
            Cancel
          </Button>
          <Button
            disabled={busy || !name.trim() || !sourceField.trim() || !pattern.trim()}
            onClick={() => void submit()}
            data-testid="submit-rule"
          >
            {busy ? "Saving…" : "Create cleanup rule"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
