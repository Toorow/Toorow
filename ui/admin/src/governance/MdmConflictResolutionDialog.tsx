/**
 * MdmConflictResolutionDialog — the gesture that closes an MDM conflict.
 *
 * WHAT THIS FILE USED TO BE, because the correction is the point. It offered
 * four strategies — `master_override`, `coalesce`, `latest_timestamp`,
 * `manual_exception` — and posted `{field_name, conflict_code, strategy,
 * override_value, notes}`. The server has never accepted any of it: the route
 * requires `{project_id, target_field, source_module, resolved_source_currency}`
 * and answers 400 `target_field is required` to that body
 * (`conflict_resolutions_api.py:251`). Nothing imported the component either, so
 * the mismatch had never once run. `screens/expectations.md` still reported it
 * as mounted, because the proof looked for the NAME and found the declaration.
 *
 * WHAT THE SERVER ACTUALLY OFFERS, and it is two gestures, not four strategies:
 *
 *   CURRENCY_CONFLICT / CURRENCY_GAP  bind ONE source currency per Connector
 *       — POST /api/mdm/conflicts/resolutions. No conversion happens
 *       here: the row is data, and dbt staging reads it at the next publish.
 *       So the dialog says the resolution is not retroactive, because it is not.
 *
 *   MEASURE_NULL  declare how the field aggregates —
 *       PATCH /api/mdm/conflicts/measure/{field_name}. The measure is
 *       PLATFORM-WIDE (`app.target_fields` carries no project), and the dialog
 *       says so before the click rather than after it.
 *
 * Every other code — TIMEZONE_DAY_OFFSET, TIMEZONE_GAP — is repaired somewhere
 * else, and this dialog names that place instead of offering a control that
 * would post nothing.
 *
 * The currency comes from `ReferenceSelect` over `/api/reference/currencies`,
 * the ONE governed vocabulary (story 48.3). A hard-coded list here would have
 * been a second authority, and the door on the other side now reads the same
 * vocabulary (`core.conflict_resolutions._validate_currency`).
 */
import { useState } from "react";
import { apiFetch } from "../lib/apiFetch";
import {
  Button,
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  Field,
  Input,
  NativeSelect,
  ReferenceSelect,
  Status,
} from "../ui";

/** The measures `core.datamodel._VALID_MEASURES` accepts, and no others. */
const MEASURES = ["sum", "average", "min", "max", "count"] as const;

const CURRENCY_CODES = new Set(["CURRENCY_CONFLICT", "CURRENCY_GAP"]);

export interface MdmConflict {
  fieldName: string;
  fieldLabel: string;
  code: string;
  message: string;
  severity: "advisory" | "blocking";
  /** The Connectors that report this field — what a currency binds to. The wire
   *  key is `source_module`; `module_name` is the legacy spelling of Connector
   *  (glossary.md), and a retired noun stops at the wire. */
  connectors: string[];
  /** Connector -> the source currency already bound, when one is. */
  resolvedByConnector: Record<string, string | null>;
}

interface MdmConflictResolutionDialogProps {
  open: boolean;
  projectId: string;
  conflict: MdmConflict | null;
  onClose: () => void;
  onResolved: () => void;
}

export default function MdmConflictResolutionDialog({
  open,
  projectId,
  conflict,
  onClose,
  onResolved,
}: MdmConflictResolutionDialogProps) {
  const [sourceConnector, setSourceConnector] = useState<string>("");
  const [currency, setCurrency] = useState<string | null>(null);
  const [measure, setMeasure] = useState<string>("sum");
  const [note, setNote] = useState<string>("");
  const [busy, setBusy] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);

  if (!conflict) return null;

  const isCurrency = CURRENCY_CODES.has(conflict.code);
  const isMeasure = conflict.code === "MEASURE_NULL";
  const connectors = conflict.connectors;
  // One Connector is not a choice: it is preselected rather than offered, and the
  // person still reads WHICH source they are binding.
  const chosen = sourceConnector || (connectors.length === 1 ? connectors[0] : "");

  // An arrow CONST, not a hoisted `function`: TypeScript drops the narrowing
  // from `if (!conflict) return null` above inside a declaration that could, in
  // principle, be called before the guard runs.
  const resolve = async () => {
    setBusy(true);
    setError(null);
    try {
      const response = isCurrency
        ? await apiFetch("/api/mdm/conflicts/resolutions", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              project_id: projectId,
              target_field: conflict.fieldName,
              source_module: chosen,
              resolved_source_currency: currency,
              note: note.trim() || null,
            }),
          })
        : await apiFetch(
            `/api/mdm/conflicts/measure/${encodeURIComponent(conflict.fieldName)}`,
            {
              method: "PATCH",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ measure }),
            },
          );
      if (!response.ok) {
        const body = (await response.json().catch(() => ({}))) as { message?: string };
        throw new Error(body.message ?? `The server refused this resolution (${response.status}).`);
      }
      onResolved();
      onClose();
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  const ready = isCurrency ? Boolean(chosen && currency) : isMeasure && Boolean(measure);

  return (
    <Dialog open={open} onOpenChange={(value) => !value && onClose()}>
      <DialogContent className="sm:max-w-[560px]">
        <DialogHeader>
          <DialogTitle>Resolve {conflict.fieldLabel}</DialogTitle>
          <DialogDescription>
            {conflict.message} ({conflict.severity === "advisory" ? "advisory" : "blocking"} ·{" "}
            <code>{conflict.code}</code>)
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-4 py-2">
          {isCurrency ? (
            connectors.length === 0 ? (
              // A conflict with no source to bind is not an error to hide: the
              // used-by read returned no Datastream carrying this field, so the
              // gesture that unblocks it happens in Data, not here.
              <Status tone="warning">
                No Datastream currently reports this field, so there is no source to bind a
                currency to. Publish a mapping version for a Datastream that carries it, then
                resolve the conflict here.
              </Status>
            ) : (
              <>
                <Field
                  label="Source"
                  hint="A currency is bound per source. Repeat for each source that reports this field."
                >
                  {(props) =>
                    connectors.length === 1 ? (
                      <Input {...props} value={connectors[0]} readOnly />
                    ) : (
                      <NativeSelect
                        {...props}
                        value={chosen}
                        onChange={(event) => setSourceConnector(event.target.value)}
                      >
                        <option value="">Choose a source…</option>
                        {connectors.map((connector) => (
                          <option key={connector} value={connector}>
                            {connector}
                            {conflict.resolvedByConnector[connector]
                              ? ` — currently ${conflict.resolvedByConnector[connector]}`
                              : ""}
                          </option>
                        ))}
                      </NativeSelect>
                    )
                  }
                </Field>

                <Field
                  label="Currency this source reports in"
                  hint="The currency of the raw amounts. Nothing is converted here — conversion happens once, at read."
                >
                  {(props) => (
                    <ReferenceSelect
                      {...props}
                      endpoint="/api/reference/currencies"
                      value={currency}
                      onChange={setCurrency}
                      vocabularyLabel="currencies"
                      placeholder="Search ISO 4217…"
                    />
                  )}
                </Field>

                <Field label="Why (optional)" hint="Kept with the decision, and read by whoever revisits it.">
                  {(props) => (
                    <Input
                      {...props}
                      type="text"
                      value={note}
                      onChange={(event) => setNote(event.target.value)}
                      placeholder="Reason for this binding"
                    />
                  )}
                </Field>

                <Status tone="info">
                  This applies to the next publish, not to what is already published.
                </Status>
              </>
            )
          ) : isMeasure ? (
            <>
              <Field
                label="How this field aggregates"
                hint="Declared once for the whole platform: this field has one meaning everywhere."
              >
                {(props) => (
                  <NativeSelect
                    {...props}
                    value={measure}
                    onChange={(event) => setMeasure(event.target.value)}
                  >
                    {MEASURES.map((candidate) => (
                      <option key={candidate} value={candidate}>
                        {candidate}
                      </option>
                    ))}
                  </NativeSelect>
                )}
              </Field>
              <Status tone="info">
                The measure is platform-wide, so every project reading this field is affected.
              </Status>
            </>
          ) : (
            <Status tone="warning">
              This conflict is not resolved here. {conflict.message} Its lever is the source
              declaration of the Datastreams involved — open them in Data › Datastreams.
            </Status>
          )}

          {error && <Status tone="error">{error}</Status>}
        </div>

        <DialogFooter className="flex gap-2">
          <Button variant="secondary" onClick={onClose} disabled={busy}>
            Cancel
          </Button>
          {(isCurrency && connectors.length > 0) || isMeasure ? (
            <Button variant="default" onClick={resolve} disabled={busy || !ready}>
              {busy ? "Resolving…" : isCurrency ? "Bind currency" : "Declare measure"}
            </Button>
          ) : null}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
