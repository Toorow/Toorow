/**
 * The history of a transformation rule, and the confirmation that writes to it —
 * Story 60.5.
 *
 * ONE COMPONENT FOR THE TWO FAMILIES. A value mapping table (60.1) and a cleanup
 * rule (60.3) are two stores, but "what did this rule used to be" is one question
 * and one screen. Writing it twice would give the console two shapes for one
 * answer, and the second one would drift the first time either changed.
 *
 * « VIDE » AND « CASSÉ » ARE TWO SENTENCES, AND THEY ARE NOT INTERCHANGEABLE.
 *
 *   * populated — one row per recorded version, newest first, with the version
 *     number, the content hash, when it was recorded and by whom, and the CURRENT
 *     one named;
 *   * EMPTY — "No version has been recorded for this rule yet." and the sentence
 *     says who will write one: the next confirmation;
 *   * BROKEN — "The version history could not be read." No list of any kind is
 *     rendered underneath, because a screen that could not read must not look
 *     like a screen that read and found nothing.
 *
 * THE CONFIRMATION NAMES WHAT WILL HAPPEN BEFORE IT HAPPENS. The version number
 * that would be created, the content hash of the proposed body, the Datastreams
 * the change reaches BY NAME — not a count on its own — and the sentence that
 * separates the future from the past. All four come from the server's preview,
 * which computes the hash with the same function the write uses: a hash composed
 * in the browser would be a claim about a state the server never saw. The
 * sentence is `geographic_change.NO_BACKFILL_FACT` — the GENERIC half of the
 * statement that module composes, because its geographic half would be false
 * about a rule. It is rendered exactly as the server sent it: nothing here
 * re-words it and nothing here carries a fallback for it.
 *
 * AND THE FAN-OUT IS NEVER A DEFAULT ZERO. `impact_state: "unknown"` renders the
 * word "unknown" and no list at all. A `0` there would tell a person nothing
 * depends on the rule they are about to change, which is the single thing they
 * must not be told wrongly.
 */
import { useCallback, useEffect, useState } from "react";
import {
  Button,
  ConfirmDialog,
  EmptyState,
  Status,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
  TableScroll,
} from "../ui";
import { ApiError, apiGet, apiPost } from "../lib/apiFetch";

export type RuleFamily = "value-mapping-table" | "cleanup-rule";

export interface RuleVersion {
  id: string;
  version_number: number;
  status: string;
  content_hash: string;
  created_at: string | null;
  created_by: string | null;
}

export interface AffectedDatastream {
  datastream_id: string;
  datastream_name: string | null;
  source_field?: string | null;
}

export interface ChangePreview {
  history_state: "available" | "unavailable";
  current_version_number: number | null;
  next_version_number: number | null;
  returns_to_existing_version: boolean;
  content_hash: string;
  unchanged: boolean;
  impact_state: "known" | "unknown";
  datastream_count: number | null;
  datastreams: AffectedDatastream[];
  backfill_statement: string;
}

const ROOT = (projectId: string, family: RuleFamily, objectId: string) =>
  `/api/projects/${encodeURIComponent(projectId)}/rule-versions/` +
  `${encodeURIComponent(family)}/${encodeURIComponent(objectId)}`;

/** The first twelve characters of a sha256, which is what a person can compare.
 *  The whole hash is on the row's title so nothing is hidden. */
function shortHash(hash: string): string {
  return hash.slice(0, 12);
}

export function fetchChangePreview(
  projectId: string,
  family: RuleFamily,
  objectId: string,
  change: Record<string, unknown>,
): Promise<ChangePreview> {
  return apiPost<ChangePreview>(`${ROOT(projectId, family, objectId)}/preview`, change);
}

/**
 * The recorded history of one rule.
 *
 * It reads on its own rather than being handed rows by its parent, for the same
 * reason the cleanup rule's measured effect does: it is its own question, and
 * folding it into the parent's payload would make an unreadable ledger look like
 * a rule nobody ever edited.
 */
export function RuleVersionHistory({
  projectId,
  family,
  objectId,
  reloadToken = 0,
}: {
  projectId: string;
  family: RuleFamily;
  objectId: string;
  /** Bumped by the parent after a confirmed change, so the history re-reads. */
  reloadToken?: number;
}) {
  const [versions, setVersions] = useState<RuleVersion[] | null>(null);
  const [currentId, setCurrentId] = useState<string | null>(null);
  const [failure, setFailure] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const body = await apiGet<{
        versions: RuleVersion[];
        current_version_id: string | null;
      }>(ROOT(projectId, family, objectId));
      setVersions(body.versions);
      setCurrentId(body.current_version_id);
      setFailure(null);
    } catch (err) {
      // BROKEN is not EMPTY. The list is dropped so no empty history can be
      // rendered from a read that did not happen.
      setVersions(null);
      setFailure(err instanceof ApiError ? err.message : "The request did not complete.");
    } finally {
      setLoading(false);
    }
  }, [projectId, family, objectId]);

  useEffect(() => {
    void load();
  }, [load, reloadToken]);

  if (loading) {
    return (
      <p role="status" className="m-0 text-body text-text-secondary">
        Loading the version history…
      </p>
    );
  }

  if (failure) {
    return (
      <Status
        as="block"
        tone="error"
        title="The version history could not be read."
        data-testid="version-history-broken"
        action={
          <Button variant="secondary" onClick={() => void load()}>
            Retry
          </Button>
        }
      >
        {failure} No version is listed, because none was read — this is not a rule that has
        never been edited.
      </Status>
    );
  }

  if (!versions || versions.length === 0) {
    return (
      <EmptyState
        title="No version has been recorded for this rule yet."
        description={
          <span data-testid="version-history-empty">
            The next confirmed change writes version 1. Nothing has been hidden or
            substituted.
          </span>
        }
      />
    );
  }

  return (
    <TableScroll label="Version history">
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>Version</TableHead>
            <TableHead>Body hash</TableHead>
            <TableHead>Recorded</TableHead>
            <TableHead>By</TableHead>
            <TableHead>State</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {versions.map((version) => (
            <TableRow key={version.id} data-testid={`version-${version.id}`}>
              <TableCell className="font-numeric text-text">
                {version.version_number}
              </TableCell>
              <TableCell className="font-mono text-text-secondary" title={version.content_hash}>
                {shortHash(version.content_hash)}
              </TableCell>
              <TableCell className="text-text-secondary">
                {version.created_at ?? "unknown"}
              </TableCell>
              <TableCell className="text-text-secondary">
                {version.created_by ?? "unknown"}
              </TableCell>
              <TableCell>
                {/* Read from the pointer, never from the position: a rule edited
                    back to a body it already carried points at an OLDER version,
                    and calling the highest number current would name a body the
                    rule does not have. */}
                <Status tone={version.id === currentId ? "success" : "neutral"}>
                  {version.id === currentId ? "Current" : "Previous"}
                </Status>
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </TableScroll>
  );
}

/**
 * The confirmation of one change, with everything the server measured about it.
 *
 * `preview` is `null` while the server is answering and `"unavailable"` when it
 * could not: in neither case is a number drawn. The confirm button stays enabled
 * on an unknown fan-out — this is an EDIT, not a destruction, and the guard the
 * repository applies to an unknown impact is on the destructive act — but the
 * word "unknown" is shown rather than a zero.
 */
export function RuleChangeConfirmation({
  open,
  title,
  onOpenChange,
  onConfirm,
  preview,
  previewFailure,
  busy = false,
  error = null,
  confirmLabel,
  testId,
}: {
  open: boolean;
  title: string;
  onOpenChange: (open: boolean) => void;
  onConfirm: () => void;
  preview: ChangePreview | null;
  previewFailure: string | null;
  busy?: boolean;
  error?: string | null;
  confirmLabel: string;
  testId: string;
}) {
  return (
    <ConfirmDialog
      open={open}
      onOpenChange={onOpenChange}
      title={title}
      description={
        <span data-testid={`${testId}-body`}>
          {previewFailure ? (
            <span data-testid={`${testId}-preview-broken`}>
              What this change would record could not be read: {previewFailure} No version
              number, no hash and no list of Datastreams is shown, because none was measured.
            </span>
          ) : !preview ? (
            <span role="status">Measuring what this change would record…</span>
          ) : (
            <>
              <span data-testid={`${testId}-version`}>
                {preview.history_state === "unavailable"
                  ? "The version history could not be read, so the number this change would create is unknown."
                  : preview.unchanged
                    ? `This body is identical to version ${preview.current_version_number}. Nothing new would be recorded.`
                    : preview.returns_to_existing_version
                      ? `This returns the rule to version ${preview.next_version_number}, a body it already carried.`
                      : `This creates version ${preview.next_version_number}.`}
              </span>{" "}
              <span className="font-mono" data-testid={`${testId}-hash`}>
                {preview.content_hash}
              </span>{" "}
              <span data-testid={`${testId}-fan-out`}>
                {preview.impact_state === "unknown"
                  ? "The Datastreams this change reaches could not be read, so their number is unknown. This is not a count of zero."
                  : preview.datastream_count === 0
                    ? "It reaches no Datastream today."
                    : `It reaches ${preview.datastream_count} Datastream${
                        preview.datastream_count === 1 ? "" : "s"
                      }: ${preview.datastreams
                        .map(
                          (stream) =>
                            // `cleanup_rules.rule_impact` reads
                            // `SELECT id, name FROM app.datastreams`, and `name`
                            // is `NOT NULL` (migration 023). The sentence a
                            // person reads before applying a rule therefore
                            // names every Datastream it reaches; it never listed
                            // `ds_<ULID>` and now cannot.
                            `${stream.datastream_name ?? "Unnamed Datastream"}${
                              stream.source_field ? ` (${stream.source_field})` : ""
                            }`,
                        )
                        .join(", ")}.`}
              </span>{" "}
              <span data-testid={`${testId}-scope`}>{preview.backfill_statement}</span>
            </>
          )}
        </span>
      }
      confirmLabel={confirmLabel}
      busy={busy}
      error={error}
      onConfirm={onConfirm}
      data-testid={testId}
      confirmTestId={`${testId}-confirm`}
    />
  );
}
