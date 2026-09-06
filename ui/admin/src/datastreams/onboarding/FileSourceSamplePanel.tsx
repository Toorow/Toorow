/**
 * Story 22.19 — the entry point the file-source chain never had.
 *
 * Measured 2026-08-01: NO screen in the console called the import routes. Not
 * `POST /api/datastreams/{id}/imports/preview`, not `/imports`. The whole Epic 22
 * framework — template, recognizer, placement, required-field gate — was
 * reachable only from a test. `FileSourceOnboardingReview` had been written for
 * it and was mounted nowhere, which is how it stayed correct and invisible for
 * weeks.
 *
 * This panel is the missing entry: an operator picks a sample of the file this
 * Datastream receives, and sees — BEFORE importing anything — which column
 * became which canonical field, with what confidence, where the rows land in the
 * matrix, and whether the landing gate would refuse.
 *
 * WHY HERE. The workbench Mapping tab is where the ratified target puts mapping
 * review, and a Datastream exists here — which the setup wizard cannot offer,
 * because the preview route is scoped to a Datastream and the wizard runs before
 * one is materialized.
 *
 * AND WHAT IT IS NOT — lot B1, amendment 6 of the 2026-08-11 review. This panel
 * tests a file that has NOT arrived yet: a layout that changed, a country
 * variant, a first file before any channel is wired. It does NOT show what the
 * Datastream already received; `DatastreamLandedFile`, on the `Data` tab, reads
 * the file that actually landed. Two gestures, two names, and neither pretends
 * to be the other — « `Preview a sample` — il sert à quoi » was the question
 * that made the difference explicit.
 *
 * NOTHING IS WRITTEN. The route builds a bounded preview and opens no ledger row
 * (`build_file_source_preview` commits nothing, and a test asserts it). Importing
 * remains a separate, explicit act on another route.
 *
 * THE PANEL DOES NOT APPEAR FOR A DATASTREAM THAT HAS NO TEMPLATE. The server
 * answers `file_source: null` when the Datastream carries no `fst_` binding, and
 * that is rendered as what it is — no template, nothing to review — rather than
 * an empty table implying a chain this Datastream does not have.
 */
import { useRef, useState } from "react";
import { Button, Input, NativeSelect, Panel, PanelHeader, Status } from "../../ui";
import { apiFetch } from "../../lib/apiFetch";
import FileSourceOnboardingReview, {
  type Ambiguity,
  type Classification,
  type Coercion,
  type GateStatus,
  type MappedField,
  type Placement,
  type RowValidation,
  type UnitReading,
  type VocabularyReading,
} from "./FileSourceOnboardingReview";

/** The `file_source` object of the preview response. `null` = no template bound. */
interface FileSourcePreview {
  fields: MappedField[];
  placement: Placement | null;
  gate: GateStatus | null;
  ambiguities: Ambiguity[];
  /**
   * The five readings of 38.16 AC3. The server already served them — the four
   * international ones since 2026-08-08 — and this panel did not pass them on:
   * a route that answers a question no screen asks.
   */
  coercions?: Coercion[];
  row_validation?: RowValidation | null;
  units?: UnitReading[];
  classification?: Classification | null;
  vocabularies?: VocabularyReading[];
  blocked: boolean;
  reason: string | null;
}

interface PreviewResponse {
  columns?: Array<{ name?: string }>;
  file_source?: FileSourcePreview | null;
}

/** Read a File as base64 without the data: prefix the API does not accept. */
function readBase64(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(new Error("The file could not be read."));
    reader.onload = () => {
      const value = String(reader.result ?? "");
      const comma = value.indexOf(",");
      resolve(comma >= 0 ? value.slice(comma + 1) : value);
    };
    reader.readAsDataURL(file);
  });
}

export default function FileSourceSamplePanel({
  projectId,
  datastreamId,
  mappingVersionId,
}: {
  projectId: string;
  datastreamId: string;
  /** Which mapping the gate replays. AD-8: never re-recognized at arrival. */
  mappingVersionId?: string | null;
}) {
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [preview, setPreview] = useState<FileSourcePreview | null>(null);
  const [noTemplate, setNoTemplate] = useState(false);
  const [columns, setColumns] = useState<string[]>([]);
  const [sample, setSample] = useState<{ filename: string; base64: string } | null>(null);
  const [canonicalOptions, setCanonicalOptions] = useState<string[]>([]);
  // AI-248 -- Jean's arbitrage, 2026-08-08. The canonical vocabulary has TWO
  // scopes: the platform's, governed, and the project's, which the client
  // declares. Measured that day: one video asks for 11 fields and the 13
  // governed ones cover ZERO of them, so without this door an operator has
  // nothing to choose and the gate can never pass.
  const [declaring, setDeclaring] = useState<string | null>(null);
  const [declName, setDeclName] = useState("");
  const [declKind, setDeclKind] = useState<"dimension" | "metric">("dimension");
  const [declAggregation, setDeclAggregation] = useState("sum");
  // Story 64.14 -- the missing half. `semantic_concept_versions.value_type` is
  // NOT NULL, so a field declared without it could never become a Concept; the
  // server now refuses it, and the screen asks for it rather than letting the
  // refusal arrive after the send.
  const [declValueType, setDeclValueType] = useState("string");
  const [declBusy, setDeclBusy] = useState(false);
  const [resolutions, setResolutions] = useState<Record<string, string>>({});
  const [warningsAccepted, setWarningsAccepted] = useState(false);
  const [warningReason, setWarningReason] = useState("");
  const [confirming, setConfirming] = useState(false);
  const [confirmation, setConfirmation] = useState<string | null>(null);

  const submit = async (file: File) => {
    setBusy(true);
    setMessage(null);
    setPreview(null);
    setCanonicalOptions([]);
    setNoTemplate(false);
    setResolutions({});
    setWarningsAccepted(false);
    setWarningReason("");
    setConfirmation(null);
    try {
      const fileBase64 = await readBase64(file);
      setSample({ filename: file.name, base64: fileBase64 });
      const response = await apiFetch(
        `/api/datastreams/${encodeURIComponent(datastreamId)}/imports/preview`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            project_id: projectId,
            filename: file.name,
            file_base64: fileBase64,
            mapping_version_id: mappingVersionId ?? undefined,
          }),
        },
      );
      if (!response.ok) {
        const detail = await response.json().catch(() => null);
        throw new Error(detail?.message ?? "The sample could not be previewed.");
      }
      const payload: PreviewResponse = await response.json();
      setColumns((payload.columns ?? []).map((column) => String(column?.name ?? "")));
      if (!payload.file_source) {
        setNoTemplate(true);
        return;
      }
      const discovered = new Set(
        payload.file_source.fields
          .map((field) => field.canonical_target)
          .filter((value): value is string => Boolean(value)),
      );
      if (
        payload.file_source.fields.some((field) =>
          ["ambiguous", "flagged", "unmatched"].includes(field.status),
        ) ||
        (payload.file_source.gate?.flagged.length ?? 0) > 0 ||
        (payload.file_source.gate?.missing_required.length ?? 0) > 0
      ) {
        const catalog = await apiFetch(
          `/api/projects/${encodeURIComponent(projectId)}/file-source-templates/canonical-fields`,
        );
        if (!catalog.ok) {
          const detail = await catalog.json().catch(() => null);
          throw new Error(detail?.message ?? "Canonical fields could not be loaded.");
        }
        {
          const catalogPayload = await catalog.json();
          for (const field of catalogPayload.fields ?? []) {
            if (field?.id) discovered.add(String(field.id));
          }
        }
      }
      setCanonicalOptions([...discovered].sort());
      setPreview(payload.file_source);
    } catch (reason) {
      setMessage(reason instanceof Error ? reason.message : "The sample could not be previewed.");
    } finally {
      setBusy(false);
    }
  };

  const gateFlaggedSources = new Set(
    preview?.gate?.flagged
      .map((field) => field.source_column ?? field.field_id)
      .filter((value): value is string => Boolean(value)) ?? [],
  );
  const resolvedFields =
    preview?.fields.map((field) => {
      const target = resolutions[field.source_column];
      if (target) {
        return { ...field, canonical_target: target, status: "matched" as const };
      }
      if (gateFlaggedSources.has(field.source_column)) {
        return { ...field, status: "flagged" as const };
      }
      return field;
    }) ?? [];
  const resolvedTargets = new Set(Object.values(resolutions));
  const canSubmitResolutions = Boolean(
    preview?.gate &&
      !preview.gate.passed &&
      preview.gate.missing_required.every((field) => resolvedTargets.has(field)) &&
      preview.gate.flagged.every((field) => {
        const source = field.source_column ?? field.field_id;
        return Boolean(source && resolutions[source]);
      }),
  );
  const declareField = async () => {
    if (!declaring) return;
    setDeclBusy(true);
    setMessage(null);
    try {
      // A metric MUST declare an aggregation or be non-additive -- the server
      // refuses otherwise, with migration 032's own sentence: "a measure must
      // never be silently non-summable". So the screen sends the chosen
      // aggregation rather than letting the refusal arrive afterwards.
      const response = await apiFetch(
        `/api/projects/${encodeURIComponent(projectId)}/file-source-templates/canonical-fields`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            // Story 64.15 -- the screen names the FLUX, not the object. The
            // server reads which object this Datastream feeds and stamps the
            // fields with it: sending the `object_kind` from here would let a
            // column be hung off another object's definition.
            datastream_id: datastreamId,
            fields: [
              declKind === "metric"
                ? {
                    canonical_name: declName,
                    concept_kind: "metric",
                    value_type: declValueType,
                    aggregation: declAggregation,
                  }
                : {
                    canonical_name: declName,
                    concept_kind: "dimension",
                    value_type: declValueType,
                  },
            ],
          }),
        },
      );
      if (!response.ok) {
        const detail = await response.json().catch(() => null);
        throw new Error(detail?.message ?? "The canonical field could not be declared.");
      }
      const payload = await response.json();
      const minted = String(payload?.fields?.[0]?.id ?? "");
      if (!minted) throw new Error("The server declared no field id.");
      // The field is born AND resolves the column that made it exist: asking the
      // operator to re-select it afterwards would be making them redo the
      // gesture they have just done.
      setCanonicalOptions((current) => [...new Set([...current, minted])].sort());
      setResolutions((current) => ({ ...current, [declaring]: minted }));
      setDeclaring(null);
    } catch (reason) {
      setMessage(
        reason instanceof Error ? reason.message : "The canonical field could not be declared.",
      );
    } finally {
      setDeclBusy(false);
    }
  };

  const confirm = async () => {
    if (!sample || !mappingVersionId) {
      setMessage("A pinned mapping version is required before confirmation.");
      return;
    }
    setConfirming(true);
    setMessage(null);
    try {
      const response = await apiFetch(
        `/api/projects/${encodeURIComponent(projectId)}/file-source-templates/confirm`,
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            datastream_id: datastreamId,
            mapping_version_id: mappingVersionId,
            filename: sample.filename,
            file_base64: sample.base64,
            resolutions: Object.entries(resolutions).map(
              ([source_column, canonical_target]) => ({
                source_column,
                canonical_target,
              }),
            ),
            accept_warnings: warningsAccepted,
            warning_reason: warningReason || undefined,
          }),
        },
      );
      const payload = await response.json().catch(() => null);
      if (!response.ok) {
        throw new Error(payload?.message ?? "Confirmation could not be recorded.");
      }
      setConfirmation(String(payload?.mapping_version_id ?? "pending mapping created"));
    } catch (reason) {
      setMessage(
        reason instanceof Error ? reason.message : "Confirmation could not be recorded.",
      );
    } finally {
      setConfirming(false);
    }
  };

  return (
    <Panel className="grid gap-4 p-5" data-testid="file-source-sample-panel">
      {/* TWO GESTURES, TWO NAMES — lot B1, amendment 6 of the 2026-08-11 review.
          « `Preview a sample` — il sert à quoi » was the right question: on a
          Datastream that has already received a file, a control that asks for a
          re-upload is not the one a person came for. That question is now
          answered on the `Data` tab, which reads the file that actually landed.

          What is left here is the OTHER need, and it is a real one: trying a
          file that has NOT arrived yet — a layout that changed, a country
          variant, a first file before any channel is wired. So the panel keeps
          the upload and stops claiming to preview what arrived. */}
      <PanelHeader
        title="Test a file before it arrives"
        description="Nothing is imported and nothing is kept. A file you choose here is parsed against this Datastream's template to show the mapping, the placement and the landing gate. The file that already arrived is read on the Data tab."
      />
      <Input
        ref={fileInput}
        type="file"
        accept=".csv,.tsv,.xlsx,.sav"
        disabled={busy}
        aria-label="File to test against the template"
        data-testid="sample-file"
        className="py-2"
        onChange={(event) => {
          const file = event.currentTarget.files?.[0];
          if (file) void submit(file);
        }}
      />

      {busy && (
        <Status as="block" tone="neutral" active title="Reading the file" data-testid="sample-busy">
          Parsing and scoring the columns against the template.
        </Status>
      )}

      {message && (
        <Status
          as="block"
          tone="error"
          title="This file could not be read"
          data-testid="sample-error"
          action={(
            <Button variant="secondary" size="sm" onClick={() => fileInput.current?.click()}>
              Choose another file
            </Button>
          )}
        >
          {message}
        </Status>
      )}

      {noTemplate && (
        <Status as="block" tone="neutral" title="No file-source template" data-testid="sample-no-template">
          This Datastream carries no template binding, so there is no mapping,
          placement or gate to review. The file parsed
          {columns.length > 0 ? ` into ${columns.length} column(s).` : "."}
        </Status>
      )}

      {preview?.blocked && (
        <Status as="block" tone="error" title="The import would be refused" data-testid="sample-blocked">
          {preview.reason === "no_placement_class"
            ? "The template declares no matrix class (planned, actual or extrapolated), so no row could be placed. The import is refused before landing."
            : preview.reason === "source_drift"
              ? "Required source columns disappeared. Review and confirm a new mapping before importing."
              : `The template refused this file: ${preview.reason ?? "unknown reason"}.`}
        </Status>
      )}

      {confirmation && (
        <Status
          as="block"
          tone="success"
          title="Confirmation recorded"
          data-testid="sample-confirmed"
        >
          Pending mapping version {confirmation} was created. The active pointer was
          not changed; publish it through the governed Outputs flow.
        </Status>
      )}

      {!confirmation && preview && !preview.blocked && preview.gate && preview.placement && (
        <FileSourceOnboardingReview
          fields={resolvedFields}
          placement={preview.placement}
          gate={preview.gate}
          ambiguities={preview.ambiguities}
          coercions={preview.coercions}
          rowValidation={preview.row_validation}
          units={preview.units}
          classification={preview.classification}
          vocabularies={preview.vocabularies}
          canonicalOptions={canonicalOptions}
          onResolveField={(sourceColumn, canonicalId) =>
            setResolutions((current) => ({
              ...current,
              [sourceColumn]: canonicalId,
            }))
          }
          onDeclareField={(sourceColumn) => {
            setDeclaring(sourceColumn);
            setDeclName(sourceColumn);
            setDeclKind("dimension");
            setDeclValueType("string");
          }}
          onConfirm={mappingVersionId && sample ? () => void confirm() : undefined}
          warningsAccepted={warningsAccepted}
          warningReason={warningReason}
          onWarningAcceptanceChange={setWarningsAccepted}
          onWarningReasonChange={setWarningReason}
          confirming={confirming}
          canSubmitResolutions={canSubmitResolutions}
        />
      )}

      {declaring && (
        <Status
          as="block"
          tone="neutral"
          title={`Declare a canonical field for “${declaring}”`}
          data-testid="declare-canonical"
        >
          <div className="flex flex-col gap-2">
            <span className="text-body text-text-secondary">
              This field belongs to THIS project. The platform vocabulary stays
              governed and is not declared here.
            </span>
            <Input
              aria-label="Canonical field name"
              data-testid="declare-name"
              value={declName}
              onChange={(e: React.ChangeEvent<HTMLInputElement>) => setDeclName(e.target.value)}
            />
            <NativeSelect
              aria-label="Field kind"
              data-testid="declare-kind"
              value={declKind}
              onChange={(e: React.ChangeEvent<HTMLSelectElement>) =>
                setDeclKind(e.target.value === "metric" ? "metric" : "dimension")
              }
            >
              <option value="dimension">Dimension</option>
              <option value="metric">Metric</option>
            </NativeSelect>
            <NativeSelect
              aria-label="Value type"
              data-testid="declare-value-type"
              value={declValueType}
              onChange={(e: React.ChangeEvent<HTMLSelectElement>) =>
                setDeclValueType(e.target.value)
              }
            >
              {[
                "string",
                "integer",
                "decimal",
                "date",
                "timestamp",
                "boolean",
                "money",
                "duration",
                "ratio",
                "percent",
              ].map((v) => (
                <option key={v} value={v}>
                  {v}
                </option>
              ))}
            </NativeSelect>
            {declKind === "metric" && (
              <NativeSelect
                aria-label="Aggregation"
                data-testid="declare-aggregation"
                value={declAggregation}
                onChange={(e: React.ChangeEvent<HTMLSelectElement>) =>
                  setDeclAggregation(e.target.value)
                }
              >
                {["sum", "average", "min", "max", "count"].map((a) => (
                  <option key={a} value={a}>
                    {a}
                  </option>
                ))}
              </NativeSelect>
            )}
            <div className="flex gap-2">
              <Button
                size="sm"
                data-testid="declare-submit"
                disabled={declBusy || !declName.trim()}
                onClick={() => void declareField()}
              >
                {declBusy ? "Declaring…" : "Declare"}
              </Button>
              <Button
                size="sm"
                variant="secondary"
                data-testid="declare-cancel"
                onClick={() => setDeclaring(null)}
              >
                Cancel
              </Button>
            </div>
          </div>
        </Status>
      )}
    </Panel>
  );
}
