/**
 * What changes, in values — the ONE renderer both readings of a mapping use.
 *
 * THE DEFECT IT CLOSES, measured 2026-08-18. `DatastreamChangeDialog` rendered
 * the server's `diff`: one row per top-level contract path, carrying
 * `before_hash` and `after_hash`. Excluding one column of forty-six therefore
 * asked a person to confirm this, and nothing else:
 *
 *     $.fields   9f2c…a1   4b70…de
 *
 * Two hex strings at the exact moment the product demands an exact
 * confirmation. The entries rendered here are composed by the server
 * (`core/mapping_value_diff.py`) because the BASE is the server's — the pointer
 * when one exists, the head of the ledger when none does — and because the
 * concept a binding names is resolved where the vocabulary lives.
 *
 * ONE RENDERER, TWO READINGS. The confirmation dialog and the version-to-version
 * comparison of the ledger both mount this, so "this column stopped landing" has
 * one spelling in the product. Which two documents are being compared is the
 * caller's sentence, not this component's: it renders a list of entries.
 *
 * A CUT VALUE SAYS IT WAS CUT. The server caps a long value and flags it; the
 * flag is rendered rather than dropped, because a truncation nobody announces is
 * a value that reads as complete and is not.
 */
import {
  Status, Table, TableBody, TableCell, TableHead, TableHeader, TableRow, TableScroll,
} from "../../../ui";

/** One reading that differs between two contracts. The server's shape, verbatim. */
export interface ValueDiffEntry {
  subject: string;
  subject_kind?: string;
  reading: string;
  before: string;
  after: string;
  truncated?: boolean;
}

export interface ValueDiff {
  /** `composed` — the entries are the answer. `unavailable` — say so, with the reason. */
  state?: string;
  entries?: ValueDiffEntry[];
  reason?: string;
}

/** What the subject of a row is, in the reader's words rather than the wire's. */
const SUBJECT_LABEL: Record<string, string> = {
  field: "Source column",
  treatment: "Declared treatment",
  path: "Contract path",
};

export default function MappingValueDiff({
  diff,
  label,
  emptyTitle,
  emptyDescription,
  testId,
}: {
  diff: ValueDiff | null | undefined;
  /** What the two sides ARE — "Base" and "Proposed", or two version numbers. */
  label: { before: string; after: string };
  emptyTitle: string;
  emptyDescription: string;
  testId?: string;
}) {
  // ABSENT AND EMPTY ARE NOT ONE STATE. A server that predates this composition
  // sends nothing, and a server that failed to compose it says `unavailable`
  // with its reason; neither of them means "the two contracts agree".
  if (!diff || diff.state === undefined) return null;
  if (diff.state !== "composed") {
    return (
      <Status
        as="block"
        tone="warning"
        title="What changes could not be read value by value"
        data-testid={testId ? `${testId}-unavailable` : undefined}
      >
        {diff.reason
          || "The reading could not be composed. The contract paths above still say which parts of the contract differ."}
      </Status>
    );
  }
  const entries = diff.entries ?? [];
  if (entries.length === 0) {
    return (
      <Status as="block" tone="neutral" title={emptyTitle} data-testid={testId}>
        {emptyDescription}
      </Status>
    );
  }
  return (
    <TableScroll
      label="What changes, value by value"
      className="max-h-80 overflow-y-auto"
      data-testid={testId}
    >
      <Table>
        <TableHeader>
          <TableRow>
            <TableHead>Subject</TableHead>
            <TableHead>Reading</TableHead>
            <TableHead>{label.before}</TableHead>
            <TableHead>{label.after}</TableHead>
          </TableRow>
        </TableHeader>
        <TableBody>
          {entries.map((entry, index) => (
            <TableRow key={`${entry.subject}-${entry.reading}-${index}`}>
              <TableCell>
                <span className="block font-mono">{entry.subject}</span>
                <span className="block text-caption text-text-secondary">
                  {SUBJECT_LABEL[entry.subject_kind ?? ""] ?? "Contract path"}
                </span>
              </TableCell>
              <TableCell>{entry.reading}</TableCell>
              <TableCell className="text-caption">{entry.before}</TableCell>
              <TableCell className="text-caption">
                {entry.after}
                {entry.truncated && (
                  <span
                    className="mt-1 block text-caption text-text-secondary"
                    data-testid={`diff-truncated-${entry.subject}`}
                  >
                    This value is longer than the reading shows. Open the raw contract to read all
                    of it.
                  </span>
                )}
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </TableScroll>
  );
}
