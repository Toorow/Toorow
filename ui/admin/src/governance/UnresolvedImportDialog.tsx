/**
 * The import half of S4 — bringing the filled file back.
 *
 * `unresolved-values.md` S4 asks for three things, and each of them is a defect
 * this dialog exists to prevent:
 *
 *   * **it previews before writing** — accepted rows, and every rejected row
 *     with its line number and reason. "Never a partial write in silence." The
 *     server route behind it (`.../import/preview`) opens no write cursor at
 *     all, and it classifies with the SAME `parse_pairs` the write uses, so the
 *     preview cannot disagree with what follows it;
 *   * **the write mode is stated on the dialog, not assumed** — this appends by
 *     source value, the unique index of migration 235:128 is the contract, and
 *     the sentence saying so comes from the server;
 *   * **it names the destination it fills.** S4 offers two — the assigned value
 *     table, or the mapping file behind the Template. Only the first is served,
 *     and the second says so instead of being quietly absent. The export half
 *     already draws the same line.
 *
 * AND IT DOES NOT CLAIM THE READING CHANGED (2026-08-30). It used to print "It
 * applies at the next read of this window" on the write's own `200`. The pairs
 * land in `app.value_mapping_entries`; the list that showed the gap resolves
 * `app.dimension_value_mappings`, and nothing joins the two stores — the ratified
 * page's Open question 4, which no component may settle. So this dialog asks the
 * panel for THE SAME READING again and prints what it answered, in the server's
 * words. The rule that reads the verdict lives in the repair drawer and is
 * imported: one store, one route, one way of deciding whether a repair landed.
 *
 * WHY A DIALOG AND NOT A DRAWER. The repair drawer is read BESIDE the table it
 * repairs, one value at a time. This is the opposite gesture: a whole file at
 * once, finished or abandoned, with nothing on screen behind it worth consulting
 * mid-way. `SheetContent` is the shape the product uses for that.
 */
import { useCallback, useState } from "react";
import {
  Button,
  Field,
  NativeSelect,
  Sheet,
  SheetContent,
  SheetHeader,
  SheetBody,
  SheetFooter,
  SheetTitle,
  Status,
  Textarea,
} from "../ui";
import { ApiError, apiPost } from "../lib/apiFetch";
import type { UnresolvedEnvelope, UnresolvedGroup } from "./UnresolvedValuesPanel";
import { verdictAfterWrite } from "./UnresolvedRepairDrawer";
import type { ValueMappingTableSummary, WriteVerdict } from "./UnresolvedRepairDrawer";

const REJECTION_LABEL: Record<string, string> = {
  not_two_columns: "This line does not carry exactly two columns",
  empty_canonical_value: "The canonical value is empty",
  duplicate_source_value: "This source value appears twice in the file",
  already_present: "The table already carries this source value — it is not overwritten",
};

interface Preview {
  would_import_count: number;
  rejected_count: number;
  /** The pairs the write would carry. Read to know WHICH source values to look
   *  for in the reading afterwards — a count alone could not tell whether the
   *  list still carries them. */
  would_import?: { source_value: string; canonical_value: string }[];
  rejected: { line: number; reason: string; content: string }[];
  over_limit: boolean;
  limit: number;
  impact: {
    impact_state: "known" | "unknown";
    datastream_count: number | null;
    message?: string;
  } | null;
}

export default function UnresolvedImportDialog({
  projectId,
  group,
  payload,
  tables,
  onClose,
  onImported,
}: {
  projectId: string;
  group: UnresolvedGroup;
  payload: UnresolvedEnvelope;
  tables: ValueMappingTableSummary[];
  onClose: () => void;
  /** The panel re-reads and hands the reading back. `null` means the re-read did
   *  not happen — a third answer, never a success. */
  onImported: (written: number) => Promise<UnresolvedEnvelope | null>;
}) {
  const [tableId, setTableId] = useState(group.destination_table?.table_id ?? "");
  const [text, setText] = useState("");
  const [preview, setPreview] = useState<Preview | null>(null);
  const [busy, setBusy] = useState(false);
  const [broken, setBroken] = useState<string | null>(null);
  const [written, setWritten] = useState<{
    count: number;
    verdict: WriteVerdict;
  } | null>(null);

  const base = `/api/projects/${encodeURIComponent(projectId)}/value-mapping-tables`;

  const check = useCallback(async () => {
    if (!tableId || !text.trim()) return;
    setBusy(true);
    setBroken(null);
    try {
      setPreview(
        await apiPost<Preview>(`${base}/${encodeURIComponent(tableId)}/import/preview`, {
          text,
        }),
      );
    } catch (error) {
      setPreview(null);
      setBroken(
        error instanceof ApiError ? error.message : "This file could not be read.",
      );
    } finally {
      setBusy(false);
    }
  }, [base, tableId, text]);

  const write = useCallback(async () => {
    if (!tableId || !preview) return;
    setBusy(true);
    setBroken(null);
    try {
      const answer = await apiPost<{ imported_count: number }>(
        `${base}/${encodeURIComponent(tableId)}/import`,
        { text },
      );
      // The same list, read again. The write's own count says what went into a
      // table; only the list says whether the gap closed.
      const reading = await onImported(answer.imported_count);
      setWritten({
        count: answer.imported_count,
        verdict: verdictAfterWrite(
          reading,
          group.datastream_id,
          group.dimension,
          (preview.would_import ?? []).map((pair) => pair.source_value),
        ),
      });
    } catch (error) {
      setBroken(error instanceof ApiError ? error.message : "Nothing was written.");
    } finally {
      setBusy(false);
    }
  }, [base, tableId, text, preview, onImported, group]);

  /** One reason at a time, each naming its gesture. */
  const blocker: string | null = !tableId
    ? "Choose the table this file fills."
    : !text.trim()
      ? "Paste the two columns of the filled file."
      : !preview
        ? "Check the file first — nothing is written before you have seen what it would do."
        : preview.over_limit
          ? `This file carries more than ${preview.limit} pairs. Split it.`
          : preview.impact?.impact_state === "unknown"
            ? (preview.impact.message ??
              "What depends on this table could not be read, so this import is held.")
            : preview.would_import_count === 0
              ? "This file would write nothing: every pair in it is already in the table."
              : null;

  return (
    <Sheet open onOpenChange={(open) => !open && onClose()}>
      <SheetContent side="right" data-testid="unresolved-import-dialog">
        <SheetHeader>
          <SheetTitle>Import a filled file — {group.dimension}</SheetTitle>
        </SheetHeader>
        <SheetBody className="flex flex-col gap-4">
          <Field label="Destination">
            {(field) => (
              <NativeSelect
                {...field}
                value={tableId}
                data-testid="import-destination"
                onChange={(event) => {
                  setTableId(event.target.value);
                  setPreview(null);
                }}
              >
                <option value="">Choose a table…</option>
                {tables.map((one) => (
                  <option key={one.id} value={one.id}>
                    {one.name}
                  </option>
                ))}
              </NativeSelect>
            )}
          </Field>

          {/* THE WRITE MODE, STATED. S4:447 refuses a dialog that writes without
              saying whether it appends or replaces. */}
          <Status as="block" tone="info" title="What this import does" data-testid="import-write-mode">
            {payload.import.write_mode_note}
          </Status>

          <Field label="The two columns">
            {(field) => (
              <Textarea
                {...field}
                rows={10}
                value={text}
                placeholder={"source_value,canonical_value\nFR - Paris,France"}
                data-testid="import-text"
                onChange={(event) => {
                  setText(event.target.value);
                  setPreview(null);
                }}
              />
            )}
          </Field>

          {preview && (
            <div data-testid="import-preview">
              <p className="text-body-sm">
                {preview.would_import_count} pair
                {preview.would_import_count === 1 ? "" : "s"} would be written,{" "}
                {preview.rejected_count} refused.
              </p>
              {preview.impact?.impact_state === "unknown" && (
                <Status as="block" tone="warning" title="Impact unknown">
                  {preview.impact.message}
                </Status>
              )}
              {preview.rejected_count > 0 && (
                <ul className="mt-2 max-h-56 overflow-y-auto text-caption" data-testid="import-rejected">
                  {preview.rejected.map((one) => (
                    <li key={`${one.line}-${one.reason}`}>
                      Line {one.line}: {REJECTION_LABEL[one.reason] ?? one.reason} — {one.content}
                    </li>
                  ))}
                </ul>
              )}
            </div>
          )}

          {written !== null && (
            <Status
              as="block"
              tone={written.verdict === "cleared" ? "success" : "warning"}
              title={
                written.verdict === "cleared"
                  ? "Written, and the list no longer carries them"
                  : written.verdict === "still_listed"
                    ? "Written, and the list still carries them"
                    : "Written, and the list could not be read again"
              }
              data-testid="import-done"
            >
              {written.count} pair{written.count === 1 ? "" : "s"} written.{" "}
              {written.verdict === "cleared"
                ? payload.repair_drawer.after_write.cleared_note
                : written.verdict === "still_listed"
                  ? payload.repair_drawer.after_write.still_listed_note
                  : payload.repair_drawer.after_write.unknown_note}
            </Status>
          )}

          {broken && (
            <Status as="block" tone="error" title="Nothing was written" data-testid="import-broken">
              {broken}
            </Status>
          )}

          {/* The destination S4 names and this dialog does not serve. */}
          <p className="text-caption text-text-secondary" data-testid="import-other-destination">
            {payload.import.other_destination_note}
          </p>
        </SheetBody>
        <SheetFooter>
          {written === null ? (
            <>
              <Button
                size="sm"
                variant="secondary"
                disabled={busy || !tableId || !text.trim()}
                data-testid="import-check"
                onClick={() => void check()}
              >
                Check the file
              </Button>
              <Button
                size="sm"
                disabled={blocker !== null || busy}
                title={blocker ?? undefined}
                data-testid="import-confirm"
                onClick={() => void write()}
              >
                Import
              </Button>
              <Button size="sm" variant="ghost" onClick={onClose}>
                Cancel
              </Button>
            </>
          ) : (
            <Button size="sm" onClick={onClose}>
              Close
            </Button>
          )}
        </SheetFooter>
      </SheetContent>
    </Sheet>
  );
}
