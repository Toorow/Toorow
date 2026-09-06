/**
 * The client's own value mapping tables — Story 60.1.
 *
 * A value mapping table is a NAME the client gives to a list of
 * `source value → canonical value` pairs, and the Datastreams it is applied to.
 * It is not the governed conformance store (`app.dimension_value_mappings`,
 * migration 052) and it is not read at render time: which of the two wins when
 * both answer is decided by story 60.5.
 *
 * IT LIVES IN THE `Value Tables` LENS of Governance › Semantic Model, beside the
 * Concepts, because Governance has exactly four Level 2 screens and "object
 * types and optional capabilities appear inside them, never as additional
 * permanent navigation" (`governance.md`). The `Transformations` tab that will
 * group three families is story 60.4 and is deliberately not named here.
 *
 * THREE STATES, AND TWO OF THEM ARE DISTINCT SENTENCES.
 *
 *   * populated — one row per table with its pairs, its scope and the REAL
 *     number of Datastreams it is applied to;
 *   * EMPTY — "No mapping table on this Project yet.", and the sentence says who
 *     writes one: this lens;
 *   * BROKEN — "The transformations library could not be read." No list of any
 *     kind is rendered underneath, because a screen that could not read must not
 *     look like a screen that read and found nothing.
 *
 * AND THE IMPACT IS NEVER A DEFAULT ZERO. When the server answers
 * `impact_state: "unknown"` the count reads "unknown" AND the destructive
 * controls it governs are really disabled — the Delete of a table whose count
 * is unknown, and the Remove of an assignment on a detail that could not be
 * measured. "I could not check" and "nothing depends on this" are different
 * facts, and only one of them is safe to act on. This sentence is a claim about
 * the code below it, and `TransformationsLibrary.test.tsx` holds it to it.
 *
 * THE ACKNOWLEDGEMENT IS THE PERSON'S ACT, NEVER THIS SCREEN'S. A deletion is
 * sent WITHOUT `acknowledge_impact` first. The server answers `409
 * value_mapping_impact_not_acknowledged` when the table is really applied
 * somewhere, and that refusal — with the count the server read at that instant —
 * is what the screen shows before offering a second, explicit button. Sending
 * `acknowledge_impact=true` on the first call would acknowledge on the person's
 * behalf and make the 409 unreachable from the console, which is the same thing
 * as not having a guard.
 *
 * AND EVERY EDIT NOW NAMES THE VERSION IT WOULD WRITE — Story 60.5. Editing what
 * a pair becomes passes through a confirmation carrying the version number, the
 * content hash of the proposed body, the Datastreams the table is applied to BY
 * NAME, and the sentence about already collected days. All four are the SERVER's
 * measurement, taken before the act; the table's own history is read beside its
 * pairs, and "no version yet" and "the history could not be read" are two
 * different sentences.
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
  Textarea,
} from "../ui";
import { ApiError, apiDelete, apiGet, apiPatch, apiPost } from "../lib/apiFetch";
import {
  RuleChangeConfirmation,
  RuleVersionHistory,
  fetchChangePreview,
  type ChangePreview,
} from "./RuleVersionHistory";

type Scope = "ORG" | "PROJECT";

export interface ValueMappingTable {
  id: string;
  name: string;
  description: string | null;
  scope_level: Scope;
  project_id: string | null;
  entry_count: number | null;
  assignment_count: number | null;
  datastream_count: number | null;
  sample_source_field: string | null;
  updated_at: string | null;
}

interface Entry {
  id: string;
  source_value: string;
  canonical_value: string;
}

interface Assignment {
  assignment_id: string;
  datastream_id: string;
  datastream_name: string | null;
  source_field: string;
}

interface TableDetail {
  table: ValueMappingTable;
  entries: Entry[];
  impact_state: "known" | "unknown";
  datastream_count: number | null;
  assignments: Assignment[];
}

const ROOT = (projectId: string) =>
  `/api/projects/${encodeURIComponent(projectId)}/value-mapping-tables`;

/** What a table translates, DERIVED from its assignment — never typed twice.
 *  A table nobody assigned translates nothing yet, and says so. */
function translates(table: ValueMappingTable): string {
  return table.sample_source_field
    ? `${table.sample_source_field} → ${table.name}`
    : "Not applied to any field yet";
}

/** The count of affected Datastreams, or the word that is not a number.
 *  `0` is only ever printed when the server measured zero. */
function impactLabel(count: number | null | undefined): string {
  return count === null || count === undefined ? "unknown" : String(count);
}

export default function TransformationsLibrary({ projectId }: { projectId: string }) {
  const [tables, setTables] = useState<ValueMappingTable[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [openTableId, setOpenTableId] = useState<string | null>(null);
  const [detail, setDetail] = useState<TableDetail | null>(null);
  const [detailError, setDetailError] = useState<string | null>(null);
  const [createOpen, setCreateOpen] = useState(false);
  const [importOpen, setImportOpen] = useState(false);
  const [assignOpen, setAssignOpen] = useState(false);
  const [pendingDelete, setPendingDelete] = useState<ValueMappingTable | null>(null);
  /** The server's own refusal of the unacknowledged deletion: its message and
   *  the count it read at that instant. Held so the second, explicit button
   *  shows the evidence the guard used rather than a number this screen kept. */
  const [refusal, setRefusal] = useState<{ message: string; count: number | null } | null>(null);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  /** Bumped after every write, so the history re-reads what the write recorded
   *  rather than what the browser believed it wrote. */
  const [historyToken, setHistoryToken] = useState(0);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const body = await apiGet<{ tables: ValueMappingTable[] }>(ROOT(projectId));
      setTables(body.tables);
      setError(null);
    } catch (err) {
      // BROKEN is not EMPTY. The list is dropped so no empty table can be
      // rendered from a read that did not happen.
      setTables(null);
      setError(err instanceof ApiError ? err.message : "The request did not complete.");
    } finally {
      setLoading(false);
    }
  }, [projectId]);

  useEffect(() => {
    void load();
  }, [load]);

  const openTable = useCallback(
    async (tableId: string) => {
      setOpenTableId(tableId);
      setDetail(null);
      setDetailError(null);
      try {
        setDetail(await apiGet<TableDetail>(`${ROOT(projectId)}/${tableId}`));
      } catch (err) {
        setDetailError(
          err instanceof ApiError ? err.message : "The request did not complete.",
        );
      }
    },
    [projectId],
  );

  const reloadBoth = useCallback(async () => {
    await load();
    if (openTableId) await openTable(openTableId);
    setHistoryToken((token) => token + 1);
  }, [load, openTable, openTableId]);

  /** Delete, in two acts.
   *
   *  `acknowledged` is FALSE on the first call, always. The server answers 409
   *  `value_mapping_impact_not_acknowledged` when the table is really applied
   *  somewhere, and that refusal is surfaced with the count IT read — then a
   *  second, explicit button calls this again with the acknowledgement. Sending
   *  it on the first call would acknowledge on the person's behalf.
   */
  const runDelete = useCallback(
    async (acknowledged: boolean) => {
      if (!pendingDelete) return;
      setBusy(true);
      try {
        await apiDelete(
          `${ROOT(projectId)}/${pendingDelete.id}${
            acknowledged ? "?acknowledge_impact=true" : ""
          }`,
        );
        setPendingDelete(null);
        setRefusal(null);
        if (openTableId === pendingDelete.id) {
          setOpenTableId(null);
          setDetail(null);
        }
        await load();
      } catch (err) {
        const body =
          err instanceof ApiError ? (err.body as Record<string, unknown> | undefined) : undefined;
        const code = err instanceof ApiError ? err.code : "";
        if (code === "value_mapping_impact_not_acknowledged") {
          const impact = (body?.impact ?? {}) as Record<string, unknown>;
          setRefusal({
            message: String(body?.message ?? (err as ApiError).message),
            count:
              typeof impact.datastream_count === "number" ? impact.datastream_count : null,
          });
        } else if (code === "value_mapping_impact_unavailable") {
          // Fail closed: the deletion is abandoned, not retried with an
          // acknowledgement nobody could have given.
          setPendingDelete(null);
          setError(
            "The number of affected Datastreams could not be read, so this table was not " +
              "deleted. This is not a count of zero.",
          );
        } else {
          setError(err instanceof ApiError ? err.message : "The request did not complete.");
        }
      } finally {
        setBusy(false);
      }
    },
    [pendingDelete, projectId, openTableId, load],
  );

  const removeAssignment = useCallback(
    async (assignment: Assignment) => {
      if (!openTableId) return;
      setBusy(true);
      try {
        await apiDelete(
          `${ROOT(projectId)}/${openTableId}/assignments?assignment_id=${encodeURIComponent(
            assignment.assignment_id,
          )}`,
        );
        await reloadBoth();
      } catch (err) {
        setDetailError(
          err instanceof ApiError ? err.message : "The request did not complete.",
        );
      } finally {
        setBusy(false);
      }
    },
    [openTableId, projectId, reloadBoth],
  );

  return (
    <div className="flex flex-col gap-4" data-testid="transformations-library">
      <Panel flush>
        <PanelHeader
          title="Value mapping tables"
          description="The vocabulary of this organization: a source value, what it becomes, and the Datastreams the table is applied to. Nothing here is applied to a report yet."
        />
        <PanelBody className="flex flex-col gap-4">
          <div className="flex items-center justify-between gap-3">
            <p className="m-0 text-caption text-text-secondary">
              {tables
                ? `${tables.length} table${tables.length === 1 ? "" : "s"} readable from this Project.`
                : " "}
            </p>
            <Button
              variant="default"
              disabled={!tables}
              onClick={() => setCreateOpen(true)}
              data-testid="new-value-table"
            >
              + New value table
            </Button>
          </div>

          {loading && (
            <p role="status" className="m-0 text-body text-text-secondary">
              Loading value mapping tables…
            </p>
          )}

          {/* BROKEN — its own sentence, and no list of any kind underneath. */}
          {!loading && error && (
            <Status
              as="block"
              tone="error"
              title="The transformations library could not be read."
              data-testid="library-broken"
              action={
                <Button variant="secondary" onClick={() => void load()}>
                  Retry
                </Button>
              }
            >
              {error} No table is listed, because none was read — this is not a Project without
              value tables.
            </Status>
          )}

          {/* EMPTY — the other sentence, and it names who writes one. */}
          {!loading && tables && tables.length === 0 && (
            <EmptyState
              title="No mapping table on this Project yet."
              description={
                <span data-testid="library-empty">
                  A value mapping table is written here, in Governance › Semantic Model › Value
                  Tables. Nothing has been hidden or substituted.
                </span>
              }
            />
          )}

          {!loading && tables && tables.length > 0 && (
            <TableScroll label="Value mapping tables">
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Table</TableHead>
                    <TableHead>Translates</TableHead>
                    <TableHead>Scope</TableHead>
                    <TableHead>Pairs</TableHead>
                    <TableHead>Datastreams</TableHead>
                    <TableHead>Actions</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {tables.map((table) => (
                    <TableRow key={table.id} data-testid={`value-table-${table.id}`}>
                      <TableCell>
                        <button
                          type="button"
                          className="font-semibold text-text underline-offset-2 hover:underline focus-visible:outline-3 focus-visible:outline-offset-2 focus-visible:outline-focus"
                          onClick={() => void openTable(table.id)}
                          data-testid={`open-${table.id}`}
                        >
                          {table.name}
                        </button>
                      </TableCell>
                      <TableCell className="text-text-secondary">{translates(table)}</TableCell>
                      <TableCell className="text-text-secondary">{table.scope_level}</TableCell>
                      <TableCell className="font-numeric text-text-secondary">
                        {impactLabel(table.entry_count)}
                      </TableCell>
                      <TableCell className="font-numeric text-text-secondary">
                        {impactLabel(table.datastream_count)}
                      </TableCell>
                      <TableCell>
                        {/* An unknown count really disables the destruction —
                            the guard the header of this file claims. Deleting a
                            table whose dependants could not be counted is
                            exactly the act "I could not check" forbids. */}
                        <Button
                          variant="secondary"
                          disabled={table.datastream_count === null}
                          title={
                            table.datastream_count === null
                              ? "The number of affected Datastreams could not be read. Nothing is deleted on an unknown impact."
                              : undefined
                          }
                          onClick={() => {
                            setRefusal(null);
                            setPendingDelete(table);
                          }}
                          data-testid={`delete-${table.id}`}
                        >
                          Delete
                        </Button>
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </TableScroll>
          )}

          {notice && (
            <Status as="block" tone="neutral" data-testid="import-report">
              {notice}
            </Status>
          )}
        </PanelBody>
      </Panel>

      {openTableId && (
        <Panel flush data-testid="value-table-detail">
          <PanelHeader
            title={detail ? detail.table.name : "Opening…"}
            description="Its pairs, and where it is applied. Both are read from the server, never joined in the browser."
          />
          <PanelBody className="flex flex-col gap-4">
            {detailError && (
              <Status as="block" tone="error" title="This table could not be read." data-testid="detail-broken"
          action={<Retry onClick={() => void reloadBoth()} />}
        >
                {detailError} No pair and no assignment is shown.
              </Status>
            )}

            {detail && (
              <>
                <div className="flex items-center justify-between gap-3">
                  <p className="m-0 text-caption text-text-secondary" data-testid="detail-impact">
                    Applied to {impactLabel(detail.datastream_count)} Datastream
                    {detail.datastream_count === 1 ? "" : "s"}
                    {detail.impact_state === "unknown"
                      ? " — the assignment store could not be read, so this is not a count of zero."
                      : "."}
                  </p>
                  <div className="flex gap-2">
                    <Button variant="secondary" onClick={() => setImportOpen(true)} data-testid="open-import">
                      Import pairs
                    </Button>
                    <Button variant="secondary" onClick={() => setAssignOpen(true)} data-testid="open-assign">
                      Assign to a Datastream
                    </Button>
                  </div>
                </div>

                {detail.entries.length === 0 ? (
                  <EmptyState
                    title="No pair in this table yet."
                    description="Add one below, or import a two-column file. A pair is live the moment it is written."
                  />
                ) : (
                  <TableScroll label="Value pairs">
                    <Table>
                      <TableHeader>
                        <TableRow>
                          <TableHead>Source value</TableHead>
                          <TableHead>Becomes</TableHead>
                          <TableHead>Actions</TableHead>
                        </TableRow>
                      </TableHeader>
                      <TableBody>
                        {detail.entries.map((entry) => (
                          <PairRow
                            key={entry.id}
                            projectId={projectId}
                            tableId={detail.table.id}
                            entry={entry}
                            onChanged={() => void reloadBoth()}
                          />
                        ))}
                      </TableBody>
                    </Table>
                  </TableScroll>
                )}

                <AddPairForm
                  projectId={projectId}
                  tableId={detail.table.id}
                  onAdded={() => void reloadBoth()}
                />

                {detail.assignments.length === 0 ? (
                  <EmptyState
                    title="This table is applied nowhere."
                    description="Assign it to a Datastream and name the collected column it reads."
                  />
                ) : (
                  <TableScroll label="Assignments">
                    <Table>
                      <TableHeader>
                        <TableRow>
                          <TableHead>Datastream</TableHead>
                          <TableHead>On the field</TableHead>
                          <TableHead>Actions</TableHead>
                        </TableRow>
                      </TableHeader>
                      <TableBody>
                        {detail.assignments.map((assignment) => (
                          <TableRow
                            key={assignment.assignment_id}
                            data-testid={`assignment-${assignment.assignment_id}`}
                          >
                            {/* `assess_table_impact` LEFT JOINs `app.datastreams`
                                for `d.name` (`NOT NULL`, migration 023), so an
                                absent name is an assignment pointing at a
                                Datastream this Project no longer has -- not a
                                nameless one. The cell says that; it used to say
                                `ds_<ULID>` under a "Datastream" heading. */}
                            <TableCell className="text-text">
                              {assignment.datastream_name ?? "No longer in this Project"}
                            </TableCell>
                            <TableCell className="font-mono text-text-secondary">
                              {assignment.source_field}
                            </TableCell>
                            <TableCell>
                              {/* Same guard as the table Delete: an impact this
                                  detail could not measure disables the act that
                                  would change what a Datastream renders. */}
                              <Button
                                variant="secondary"
                                disabled={busy || detail.impact_state === "unknown"}
                                title={
                                  detail.impact_state === "unknown"
                                    ? "The assignment store could not be read. Nothing is removed on an unknown impact."
                                    : undefined
                                }
                                onClick={() => void removeAssignment(assignment)}
                                data-testid={`unassign-${assignment.assignment_id}`}
                              >
                                Remove
                              </Button>
                            </TableCell>
                          </TableRow>
                        ))}
                      </TableBody>
                    </Table>
                  </TableScroll>
                )}

                {/* Story 60.5. Its own read, because it is its own question:
                    folding the history into the detail payload would make an
                    unreadable ledger look like a table nobody ever edited. */}
                <div className="flex flex-col gap-2">
                  <h3 className="m-0 text-body font-semibold text-text">Version history</h3>
                  <RuleVersionHistory
                    projectId={projectId}
                    family="value-mapping-table"
                    objectId={detail.table.id}
                    reloadToken={historyToken}
                  />
                </div>
              </>
            )}
          </PanelBody>
        </Panel>
      )}

      <CreateTableDialog
        open={createOpen}
        projectId={projectId}
        onClose={() => setCreateOpen(false)}
        onCreated={() => {
          setCreateOpen(false);
          void load();
        }}
      />

      {detail && (
        <ImportPairsDialog
          open={importOpen}
          projectId={projectId}
          tableId={detail.table.id}
          onClose={() => setImportOpen(false)}
          onImported={(message) => {
            setImportOpen(false);
            setNotice(message);
            void reloadBoth();
          }}
        />
      )}

      {detail && (
        <AssignDialog
          open={assignOpen}
          projectId={projectId}
          tableId={detail.table.id}
          onClose={() => setAssignOpen(false)}
          onAssigned={() => {
            setAssignOpen(false);
            void reloadBoth();
          }}
        />
      )}

      {/* The confirmation names the COUNT BEFORE the act. Once the server has
          refused the unacknowledged deletion, it is the SERVER's sentence and
          the SERVER's count that are shown, and the acknowledgement becomes a
          second, separately labelled button. */}
      <ConfirmDialog
        open={pendingDelete !== null}
        onOpenChange={(open) => {
          if (!open) {
            setPendingDelete(null);
            setRefusal(null);
          }
        }}
        title={pendingDelete ? `Delete “${pendingDelete.name}”?` : "Delete value table?"}
        description={
          pendingDelete
            ? `This table is applied to ${impactLabel(
                refusal ? refusal.count : pendingDelete.datastream_count,
              )} Datastream${
                (refusal ? refusal.count : pendingDelete.datastream_count) === 1 ? "" : "s"
              } today. ` +
              `Its ${impactLabel(pendingDelete.entry_count)} pair${
                pendingDelete.entry_count === 1 ? "" : "s"
              } and every assignment go with it. Already collected days are not rewritten.`
            : ""
        }
        error={refusal ? refusal.message : null}
        confirmLabel={refusal ? "Delete anyway — impact acknowledged" : "Delete value table"}
        destructive
        busy={busy}
        onConfirm={() => void runDelete(refusal !== null)}
        data-testid="delete-table-confirm"
      />
    </div>
  );
}

/**
 * One pair, and the two verbs the story's « Permet » names beside "add":
 * **edit** and **delete**.
 *
 * The SOURCE VALUE is not editable, and that is a decision rather than an
 * omission: the store holds one live pair per source value
 * (`uq_value_mapping_entries_source`), so changing it is creating a different
 * pair and retiring this one — two acts, and the form above already performs the
 * first. Only what the value BECOMES is edited here, which is what
 * `PATCH .../entries/{entry_id}` accepts.
 */
function PairRow({
  projectId,
  tableId,
  entry,
  onChanged,
}: {
  projectId: string;
  tableId: string;
  entry: Entry;
  onChanged: () => void;
}) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(entry.canonical_value);
  const [busy, setBusy] = useState(false);
  const [failure, setFailure] = useState<string | null>(null);
  const [confirming, setConfirming] = useState(false);
  const [preview, setPreview] = useState<ChangePreview | null>(null);
  const [previewFailure, setPreviewFailure] = useState<string | null>(null);

  /** Ask the SERVER what this edit would record, then show it. The hash a person
   *  confirms is computed by the same function the write uses. */
  const askPreview = useCallback(async () => {
    setPreview(null);
    setPreviewFailure(null);
    setConfirming(true);
    try {
      setPreview(
        await fetchChangePreview(projectId, "value-mapping-table", tableId, {
          entry_id: entry.id,
          canonical_value: draft,
        }),
      );
    } catch (err) {
      setPreviewFailure(
        err instanceof ApiError ? err.message : "The request did not complete.",
      );
    }
  }, [projectId, tableId, entry.id, draft]);

  const save = useCallback(async () => {
    setBusy(true);
    setFailure(null);
    try {
      await apiPatch(`${ROOT(projectId)}/${tableId}/entries/${entry.id}`, {
        canonical_value: draft,
      });
      setConfirming(false);
      setEditing(false);
      onChanged();
    } catch (err) {
      setFailure(err instanceof ApiError ? err.message : "The request did not complete.");
    } finally {
      setBusy(false);
    }
  }, [projectId, tableId, entry.id, draft, onChanged]);

  const remove = useCallback(async () => {
    setBusy(true);
    setFailure(null);
    try {
      await apiDelete(`${ROOT(projectId)}/${tableId}/entries/${entry.id}`);
      onChanged();
    } catch (err) {
      setFailure(err instanceof ApiError ? err.message : "The request did not complete.");
    } finally {
      setBusy(false);
    }
  }, [projectId, tableId, entry.id, onChanged]);

  return (
    <TableRow data-testid={`entry-${entry.id}`}>
      <TableCell className="text-text">{entry.source_value}</TableCell>
      <TableCell className="text-text-secondary">
        {editing ? (
          <Input
            aria-label={`What ${entry.source_value} becomes`}
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            data-testid={`edit-value-${entry.id}`}
          />
        ) : (
          entry.canonical_value
        )}
        {failure && <Status tone="error">{failure}</Status>}
      </TableCell>
      <TableCell>
        <div className="flex gap-2">
          {editing ? (
            <>
              <Button
                disabled={busy || !draft.trim() || draft === entry.canonical_value}
                onClick={() => void askPreview()}
                data-testid={`save-entry-${entry.id}`}
              >
                Save
              </Button>
              <Button
                variant="secondary"
                onClick={() => {
                  setDraft(entry.canonical_value);
                  setEditing(false);
                  setFailure(null);
                }}
                data-testid={`cancel-entry-${entry.id}`}
              >
                Cancel
              </Button>
            </>
          ) : (
            <>
              <Button
                variant="secondary"
                onClick={() => setEditing(true)}
                data-testid={`edit-entry-${entry.id}`}
              >
                Edit
              </Button>
              <Button
                variant="secondary"
                disabled={busy}
                onClick={() => void remove()}
                data-testid={`remove-entry-${entry.id}`}
              >
                Remove
              </Button>
            </>
          )}
        </div>
        <RuleChangeConfirmation
          open={confirming}
          title={`Change what “${entry.source_value}” becomes?`}
          onOpenChange={(next) => {
            if (!next) setConfirming(false);
          }}
          onConfirm={() => void save()}
          preview={preview}
          previewFailure={previewFailure}
          busy={busy}
          error={failure}
          confirmLabel="Record this version"
          testId={`entry-version-confirm-${entry.id}`}
        />
      </TableCell>
    </TableRow>
  );
}

function AddPairForm({
  projectId,
  tableId,
  onAdded,
}: {
  projectId: string;
  tableId: string;
  onAdded: () => void;
}) {
  const [source, setSource] = useState("");
  const [canonical, setCanonical] = useState("");
  const [busy, setBusy] = useState(false);
  const [failure, setFailure] = useState<string | null>(null);

  const submit = useCallback(async () => {
    setBusy(true);
    setFailure(null);
    try {
      await apiPost(`${ROOT(projectId)}/${tableId}/entries`, {
        source_value: source,
        canonical_value: canonical,
      });
      setSource("");
      setCanonical("");
      onAdded();
    } catch (err) {
      setFailure(err instanceof ApiError ? err.message : "The request did not complete.");
    } finally {
      setBusy(false);
    }
  }, [projectId, tableId, source, canonical, onAdded]);

  return (
    <div className="flex flex-col gap-2 rounded-lg border border-divider-base p-4">
      <div className="flex flex-wrap items-end gap-3">
        <Field label="Source value">
          {(props) => (
            <Input
              {...props}
              value={source}
              onChange={(event) => setSource(event.target.value)}
              data-testid="pair-source"
            />
          )}
        </Field>
        <Field label="Becomes">
          {(props) => (
            <Input
              {...props}
              value={canonical}
              onChange={(event) => setCanonical(event.target.value)}
              data-testid="pair-canonical"
            />
          )}
        </Field>
        <Button
          disabled={busy || !source.trim() || !canonical.trim()}
          onClick={() => void submit()}
          data-testid="add-pair"
        >
          Add pair
        </Button>
      </div>
      {failure && <Status tone="error">{failure}</Status>}
    </div>
  );
}

function CreateTableDialog({
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
  const [description, setDescription] = useState("");
  const [scope, setScope] = useState<Scope>("PROJECT");
  const [busy, setBusy] = useState(false);
  const [failure, setFailure] = useState<string | null>(null);

  const submit = useCallback(async () => {
    setBusy(true);
    setFailure(null);
    try {
      await apiPost(ROOT(projectId), {
        name,
        description: description || undefined,
        scope_level: scope,
      });
      setName("");
      setDescription("");
      onCreated();
    } catch (err) {
      setFailure(err instanceof ApiError ? err.message : "The request did not complete.");
    } finally {
      setBusy(false);
    }
  }, [projectId, name, description, scope, onCreated]);

  if (!open) return null;
  return (
    <Dialog open={open} onOpenChange={(next) => !next && onClose()}>
      <DialogContent data-testid="create-table-dialog">
        <DialogHeader>
          <DialogTitle>New value mapping table</DialogTitle>
          <DialogDescription>
            A table carries the vocabulary of this organization. It is not applied to any report
            until it is assigned to a Datastream.
          </DialogDescription>
        </DialogHeader>
        <div className="flex flex-col gap-3">
          <Field label="Name">
            {(props) => (
              <Input
                {...props}
                value={name}
                onChange={(event) => setName(event.target.value)}
                data-testid="table-name"
              />
            )}
          </Field>
          <Field label="Description" hint="What this table is for, in the team's own words.">
            {(props) => (
              <Input
                {...props}
                value={description}
                onChange={(event) => setDescription(event.target.value)}
              />
            )}
          </Field>
          <Field
            label="Scope"
            hint="An organization table is readable from every Project of this organization. There is no platform scope: seeds are the platform authority."
          >
            {(props) => (
              <NativeSelect
                {...props}
                value={scope}
                onChange={(event) => setScope(event.target.value as Scope)}
              >
                <option value="PROJECT">This Project</option>
                <option value="ORG">This organization</option>
              </NativeSelect>
            )}
          </Field>
          {failure && <Status tone="error">{failure}</Status>}
        </div>
        <DialogFooter>
          <Button variant="secondary" onClick={onClose}>
            Cancel
          </Button>
          <Button disabled={busy || !name.trim()} onClick={() => void submit()} data-testid="submit-table">
            {busy ? "Saving…" : "Create table"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function ImportPairsDialog({
  open,
  projectId,
  tableId,
  onClose,
  onImported,
}: {
  open: boolean;
  projectId: string;
  tableId: string;
  onClose: () => void;
  onImported: (message: string) => void;
}) {
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [failure, setFailure] = useState<string | null>(null);

  const submit = useCallback(async () => {
    setBusy(true);
    setFailure(null);
    try {
      const result = await apiPost<{
        imported_count: number;
        rejected_count: number;
        rejected: Array<{ line: number; reason: string }>;
      }>(`${ROOT(projectId)}/${tableId}/import`, { text });
      setText("");
      // Every refusal is NAMED with its line. An import that reported only the
      // successes would present a partial vocabulary as a complete one.
      const refusals = result.rejected
        .map((row) => `line ${row.line} (${row.reason.replaceAll("_", " ")})`)
        .join(", ");
      onImported(
        `${result.imported_count} pair(s) imported. ${result.rejected_count} line(s) refused` +
          (refusals ? `: ${refusals}.` : "."),
      );
    } catch (err) {
      setFailure(err instanceof ApiError ? err.message : "The request did not complete.");
    } finally {
      setBusy(false);
    }
  }, [projectId, tableId, text, onImported]);

  if (!open) return null;
  return (
    <Dialog open={open} onOpenChange={(next) => !next && onClose()}>
      <DialogContent data-testid="import-dialog">
        <DialogHeader>
          <DialogTitle>Import pairs</DialogTitle>
          <DialogDescription>
            Two columns per line: the source value, then what it becomes. A line that does not
            carry two columns is refused and named — the import is never partial in silence.
          </DialogDescription>
        </DialogHeader>
        <Field label="Pairs">
          {(props) => (
            <Textarea
              {...props}
              rows={8}
              value={text}
              onChange={(event) => setText(event.target.value)}
              data-testid="import-text"
            />
          )}
        </Field>
        {failure && <Status tone="error">{failure}</Status>}
        <DialogFooter>
          <Button variant="secondary" onClick={onClose}>
            Cancel
          </Button>
          <Button disabled={busy || !text.trim()} onClick={() => void submit()} data-testid="submit-import">
            {busy ? "Importing…" : "Import"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function AssignDialog({
  open,
  projectId,
  tableId,
  onClose,
  onAssigned,
}: {
  open: boolean;
  projectId: string;
  tableId: string;
  onClose: () => void;
  onAssigned: () => void;
}) {
  const [datastreamId, setDatastreamId] = useState("");
  const [sourceField, setSourceField] = useState("");
  const [busy, setBusy] = useState(false);
  const [failure, setFailure] = useState<string | null>(null);

  const submit = useCallback(async () => {
    setBusy(true);
    setFailure(null);
    try {
      await apiPost(`${ROOT(projectId)}/${tableId}/assignments`, {
        datastream_id: datastreamId,
        source_field: sourceField,
      });
      setDatastreamId("");
      setSourceField("");
      onAssigned();
    } catch (err) {
      setFailure(err instanceof ApiError ? err.message : "The request did not complete.");
    } finally {
      setBusy(false);
    }
  }, [projectId, tableId, datastreamId, sourceField, onAssigned]);

  if (!open) return null;
  return (
    <Dialog open={open} onOpenChange={(next) => !next && onClose()}>
      <DialogContent data-testid="assign-dialog">
        <DialogHeader>
          <DialogTitle>Assign this table to a Datastream</DialogTitle>
          <DialogDescription>
            The field is the collected column as the source emits it. It does not have to be
            mapped to a concept: a value table exists to normalise a column before anything maps
            it.
          </DialogDescription>
        </DialogHeader>
        <div className="flex flex-col gap-3">
          <Field label="Datastream">
            {(props) => (
              <Input
                {...props}
                value={datastreamId}
                onChange={(event) => setDatastreamId(event.target.value)}
                data-testid="assign-datastream"
              />
            )}
          </Field>
          <Field label="On the field">
            {(props) => (
              <Input
                {...props}
                value={sourceField}
                onChange={(event) => setSourceField(event.target.value)}
                data-testid="assign-field"
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
            disabled={busy || !datastreamId.trim() || !sourceField.trim()}
            onClick={() => void submit()}
            data-testid="submit-assignment"
          >
            {busy ? "Assigning…" : "Assign"}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
