/**
 * A row is `[name, value]` — or `[name, value, meaning]`, and the third slot is
 * the repair of 2026-08-16. This table WAS the whole `Definition` tab of several
 * objects, built from `Object.entries(summary)`, so a person opening a governed
 * object read a column list: `Content hash`, `Ordinal`, `Additivity class`. The
 * words are the storage layer's, and de-snaking them changed the typography and
 * not the vocabulary. The meaning comes from the shared `fieldMeaning`, so the
 * sentence a person reads here is the same one every other surface shows.
 *
 * It lives in its own module since 2026-08-30: the typed Master Data Overview
 * renders the same table, and importing it from `GovernanceObjectWorkbench`
 * would close a cycle — the workbench mounts that Overview.
 *
 * `label` and `displayValue` come from `ui/Evidence` — the shared vocabulary for
 * "show me this record". A private copy here would be the third one, and the
 * second copy is already the defect that primitive was extracted to fix.
 */
import {
  EmptyState,
  Table,
  TableBody,
  TableCell,
  TableRow,
  TableScroll,
  displayValue,
} from "../ui";

export type FieldRow = [string, unknown] | [string, unknown, string | null];

export function FieldTable({
  rows,
  caption,
}: {
  rows: FieldRow[];
  caption: string;
}) {
  if (rows.length === 0) {
    return (
      <EmptyState
        title="No owned fields"
        description="This object's owner exposes no field under this tab."
      />
    );
  }
  return (
    <TableScroll label={caption}>
      <Table>
        <TableBody>
          {rows.map(([key, value, meaning]) => (
            <TableRow key={key}>
              <TableCell className="w-64 align-top">
                <span className="font-semibold text-text">{key}</span>
                {meaning && (
                  <span className="mt-0.5 block text-caption font-normal text-text-secondary">
                    {meaning}
                  </span>
                )}
              </TableCell>
              {/* The WHOLE value stays reachable: a hash is shortened to be
                  comparable, never to be hidden. */}
              <TableCell
                className="text-ui text-text-secondary"
                title={typeof value === "string" ? value : undefined}
              >
                {displayValue(value)}
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      </Table>
    </TableScroll>
  );
}

export default FieldTable;
