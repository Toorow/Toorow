/**
 * The mapping ledger, read as a HISTORY — 2026-08-18.
 *
 * WHAT IT REPLACED, measured in place. The table listed a ULID, a state, two
 * counts, two 64-character hashes and a date. Everything a history is for was
 * missing: no version number, though the server orders the list by it and has
 * always selected it; no author, though the column is in the same SELECT; no way
 * to see what one version decided that its predecessor did not; and no way to go
 * back to one. A person could read that eleven versions exist and nothing about
 * what any of them changed.
 *
 * FOUR THINGS ARE ADDED AND NOT ONE OF THEM WRITES HISTORY.
 *
 *   * `version_number` and `created_by` — read off the tab payload, which
 *     already carries both (`datastream_workbench.py`, the mapping branch). Not
 *     invented, not derived: absent values say so rather than counting rows.
 *   * an ORDER, through `SortableHead`. Every row of the ledger is on the page —
 *     the read has no cursor — so the sort is complete and no page-scope note is
 *     owed. Mounting one would promise a limitation that does not exist here.
 *   * a COMPARISON of two versions, composed by the server
 *     (`mapping/versions/compare`) with the very entries the confirmation dialog
 *     renders. Reading history and confirming a change say "this column stopped
 *     landing" in one voice or the product has two vocabularies for one fact.
 *   * a PROPOSAL from an older version — the honest shape of "restore". The
 *     ledger is append-only and stays append-only: nothing is rewritten and no
 *     pointer moves. The old contract is offered as the PROPOSED payload of a
 *     normal governed change, so the person sees, value by value, what returning
 *     to it would do before anything is appended.
 */
import { useState } from "react";
import {
  Button, EmptyState, NativeSelect, Panel, PanelHeader, SortableHead, Status, Table, TableBody,
  TableCell, TableHead, TableHeader, TableRow, TableScroll, Timestamp, sortRows, useTableSort,
} from "../../../ui";
import { apiFetch } from "../../../lib/apiFetch";
import { numberText, record, text, type EvidenceRecord } from "../evidence";
import DatastreamChangeDialog from "../DatastreamChangeDialog";
import MappingValueDiff, { type ValueDiff } from "./MappingValueDiff";

/** How one version is named to a person: by its number, never by its ULID. */
export function versionLabel(version: EvidenceRecord | null | undefined): string {
  const number = version?.version_number;
  return typeof number === "number" ? `Version ${number}` : text(version?.id, "Unknown version");
}

type Comparison = {
  againstId: string;
  value_diff?: ValueDiff;
  base?: EvidenceRecord;
  against?: EvidenceRecord;
};

export default function MappingVersionLedger({
  versions,
  activeId,
  selectedId,
  onSelect,
  projectId,
  datastreamId,
  onConfirmed,
  actions,
  rawImportId,
}: {
  versions: EvidenceRecord[];
  /** `app.datastreams.current_mapping_version_id` — what is in force, or null. */
  activeId: string | null;
  selectedId: string | null;
  onSelect: (id: string) => void;
  projectId: string;
  datastreamId: string;
  onConfirmed: () => void;
  /** The panel's own acts, composed by the tab: publication and the raw door. */
  actions?: React.ReactNode;
  rawImportId?: string | null;
}) {
  const { sort, toggleSort } = useTableSort(null);
  const [againstId, setAgainstId] = useState("");
  const [comparison, setComparison] = useState<Comparison | null>(null);
  const [comparing, setComparing] = useState(false);
  const [compareError, setCompareError] = useState<string | null>(null);
  const [proposingFrom, setProposingFrom] = useState<string | null>(null);

  const selected = versions.find((version) => text(version.id) === selectedId) ?? null;
  const others = versions.filter((version) => text(version.id) !== selectedId);

  const ordered = sortRows(versions, sort, (version, key) => {
    if (key === "version") return typeof version.version_number === "number" ? version.version_number : null;
    if (key === "state") return version.id === activeId ? "Active" : "Non-live";
    if (key === "blocking") return typeof version.blocking_count === "number" ? version.blocking_count : null;
    if (key === "author") return typeof version.created_by === "string" ? version.created_by : null;
    if (key === "created") return typeof version.created_at === "string" ? version.created_at : null;
    return null;
  });

  const compare = async (baseId: string, targetId: string) => {
    setComparing(true);
    setCompareError(null);
    try {
      const response = await apiFetch(
        `/api/datastreams/${encodeURIComponent(datastreamId)}/mapping/versions/compare`
        + `?project_id=${encodeURIComponent(projectId)}`
        + `&base=${encodeURIComponent(baseId)}&against=${encodeURIComponent(targetId)}`,
      );
      const value = await response.json().catch(() => null) as
        | { message?: string; value_diff?: ValueDiff; base?: EvidenceRecord; against?: EvidenceRecord }
        | null;
      if (!response.ok) throw new Error(value?.message || "The two versions could not be compared.");
      setComparison({
        againstId: targetId,
        value_diff: value?.value_diff,
        base: value?.base,
        against: value?.against,
      });
    } catch (reason) {
      setComparison(null);
      setCompareError(
        reason instanceof Error ? reason.message : "The two versions could not be compared.",
      );
    } finally {
      setComparing(false);
    }
  };

  const proposedFrom = versions.find((version) => text(version.id) === proposingFrom) ?? null;

  return (
    <Panel flush>
      <PanelHeader
        title="Immutable mapping versions"
        description="A confirmed change appends a non-live version and dispatches one candidate. Select a version to read its bindings, compare two, or propose a change starting from an older one."
        actions={actions}
      />
      {versions.length === 0 ? (
        <div className="p-5">
          {/* THE ONLY GESTURE THAT EXISTS AT ZERO IS NAMED, and it is the one in
              this panel's own header: the column controls are drawn from the
              selected version, so with no version there is nothing under this
              list to press. */}
          <EmptyState
            title="Nothing has been decided about this Datastream's columns yet"
            description="No source column is bound to a field, so nothing this Datastream collects can land. Use “Edit raw contract…” above to agree a first mapping; from then on the columns are edited here."
          />
        </div>
      ) : (
        <>
          <TableScroll label="Mapping versions">
            <Table>
              <TableHeader>
                <TableRow>
                  <SortableHead sortKey="version" sort={sort} onSort={toggleSort}>
                    Version
                  </SortableHead>
                  <SortableHead sortKey="state" sort={sort} onSort={toggleSort}>
                    State
                  </SortableHead>
                  {/* WHO DECIDED IT. `created_by` is the actor the append
                      recorded; a ledger that cannot name one is a ledger nobody
                      can ask about. */}
                  <SortableHead sortKey="author" sort={sort} onSort={toggleSort}>
                    Proposed by
                  </SortableHead>
                  <SortableHead sortKey="blocking" sort={sort} onSort={toggleSort} numeric>
                    Blocking fields
                  </SortableHead>
                  <TableHead>Hashes</TableHead>
                  <SortableHead sortKey="created" sort={sort} onSort={toggleSort}>
                    Created
                  </SortableHead>
                  {/* NOT "Acts": the binding table on the same tab already owns
                      that word for the acts of a COLUMN. Two tables, one page,
                      one word would be two questions with one name. */}
                  <TableHead>Reuse this version</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {ordered.map((version) => {
                  const id = text(version.id);
                  const isSelected = id === text(selected?.id);
                  return (
                    <TableRow
                      key={id}
                      onClick={() => onSelect(id)}
                      aria-selected={isSelected}
                      className="cursor-pointer"
                    >
                      <TableCell>
                        <span className="block">{versionLabel(version)}</span>
                        {/* The ULID stays, one size down: it is what a support
                            request quotes, and it is not what a person reads.
                            It is NOT repeated when the number is missing and the
                            label already IS the id — one fact, printed once. */}
                        {typeof version.version_number === "number" && (
                          <span className="block font-mono text-caption text-text-secondary">
                            {id}
                          </span>
                        )}
                      </TableCell>
                      <TableCell>
                        <Status tone={version.id === activeId ? "success" : "neutral"}>
                          {version.id === activeId ? "Active" : "Non-live"}
                        </Status>
                      </TableCell>
                      <TableCell data-testid={`version-author-${id}`}>
                        {typeof version.created_by === "string" && version.created_by.trim()
                          ? version.created_by
                          : "Not recorded"}
                      </TableCell>
                      <TableCell numeric>{numberText(version.blocking_count)}</TableCell>
                      {/* BOTH HASHES SURVIVE THE REPAIR, in one cell instead of
                          two columns. They are the identity of what was frozen —
                          a support request quotes them — and dropping the source
                          schema one while adding readings would be paying for
                          this amendment with a fact nobody agreed to lose. */}
                      <TableCell className="font-mono text-caption">
                        <span className="block">{text(version.content_hash)}</span>
                        <span className="block text-text-secondary">
                          {`Source schema ${text(version.source_schema_hash)}`}
                        </span>
                      </TableCell>
                      <TableCell>
                        <Timestamp value={typeof version.created_at === "string" ? version.created_at : null} />
                      </TableCell>
                      <TableCell>
                        <Button
                          variant="secondary"
                          size="sm"
                          data-testid={`propose-from-${id}`}
                          disabled={!record(version.mapping_payload)}
                          onClick={(event: React.MouseEvent) => {
                            event.stopPropagation();
                            setProposingFrom(id);
                          }}
                        >
                          Propose a change from this version…
                        </Button>
                      </TableCell>
                    </TableRow>
                  );
                })}
              </TableBody>
            </Table>
          </TableScroll>

          {/* WHAT TWO VERSIONS DISAGREE ABOUT. One question at a time: the row
              click already answered "which version am I reading"; this asks only
              which other one to read it against. */}
          {others.length > 0 && selected && (
            <div className="grid gap-3 border-t border-divider-base p-5" data-testid="version-compare">
              <div className="flex flex-wrap items-end gap-2">
                <label className="grid gap-1 text-label text-text-secondary">
                  Compare {versionLabel(selected)} with
                  <NativeSelect
                    aria-label="Version to compare against"
                    data-testid="compare-against"
                    value={againstId}
                    onChange={(event: React.ChangeEvent<HTMLSelectElement>) =>
                      setAgainstId(event.target.value)
                    }
                  >
                    <option value="">Choose a version…</option>
                    {others.map((version) => (
                      <option key={text(version.id)} value={text(version.id)}>
                        {versionLabel(version)}
                      </option>
                    ))}
                  </NativeSelect>
                </label>
                <Button
                  size="sm"
                  data-testid="compare-versions"
                  disabled={!againstId || comparing}
                  onClick={() => void compare(text(selected.id), againstId)}
                >
                  {comparing ? "Comparing…" : "Compare these two"}
                </Button>
                {comparison && (
                  <Button variant="secondary" size="sm" onClick={() => setComparison(null)}>
                    Clear this comparison
                  </Button>
                )}
              </div>
              {compareError && (
                <Status
                  as="block"
                  tone="error"
                  title="The comparison could not be read"
                  action={<Retry onClick={() => void compare(text(selected.id), againstId)} />}
                >
                  {compareError}
                </Status>
              )}
              {comparison && (
                <MappingValueDiff
                  diff={comparison.value_diff}
                  label={{
                    before: versionLabel(selected),
                    after: versionLabel(
                      versions.find((version) => text(version.id) === comparison.againstId),
                    ),
                  }}
                  emptyTitle="These two versions read the same"
                  emptyDescription="Every reading is identical on both sides; the two contracts differ only where nothing reads them."
                  testId="version-value-diff"
                />
              )}
            </div>
          )}

          {/* GOING BACK IS A PROPOSAL, NEVER A REWRITE. The old contract becomes
              the proposed payload of a governed change: the review names what it
              would do, the confirmation appends a NEW version, and every version
              in this table stays exactly as it was recorded. */}
          {proposedFrom && (
            <div className="flex flex-wrap items-center justify-between gap-3 border-t border-divider-base p-5" data-testid="propose-from-version">
              <span className="text-ui text-text-secondary">
                {`Proposes the contract of ${versionLabel(proposedFrom)} as a new version. `}
                {`${versionLabel(proposedFrom)} stays in the ledger; nothing is rewritten and no `}
                {"pointer moves until a governed publication."}
              </span>
              <div className="flex items-center gap-2">
                <Button variant="secondary" onClick={() => setProposingFrom(null)}>
                  Cancel
                </Button>
                <DatastreamChangeDialog
                  projectId={projectId}
                  datastreamId={datastreamId}
                  kind="mapping"
                  initialPayload={record(proposedFrom.mapping_payload)}
                  proposedPayload={record(proposedFrom.mapping_payload)}
                  scope={`Returns every reading to the contract recorded as ${versionLabel(proposedFrom)}.`}
                  triggerLabel={`Prepare a change from ${versionLabel(proposedFrom)}`}
                  testId="prepare-restore-change"
                  rawImportId={rawImportId}
                  onConfirmed={() => {
                    setProposingFrom(null);
                    onConfirmed();
                  }}
                />
              </div>
            </div>
          )}
        </>
      )}
    </Panel>
  );
}
