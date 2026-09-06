/**
 * Story 22.19 — File-source onboarding: mapping result and validation gate.
 *
 * The surface shown when a sample import is previewed. It renders the per-column
 * recognition with confidence (22.17), the declared matrix placement (22.14) and
 * the landing gate (22.15) — all three read from `POST /api/datastreams/{id}/
 * imports/preview`, whose `file_source` object is exactly this component's props.
 *
 * TWO LEVELS, AND THEY ARE NOT THE ONES THE FIRST VERSION SHOWED.
 * `datastream-workbench-and-wizard.md:60-61` fixes the vocabulary:
 *
 *   Blocking — safe execution or publication is impossible. "Unresolved required
 *              mapping" is named in that list.
 *   Warning  — permits an explicit decision, and RECORDS the accepted exception
 *              in the plan version.
 *
 * Measured in the gate rather than assumed: `flagged` only ever carries REQUIRED
 * fields whose binding is blocking (`file_source_gate.py:123`). So
 * `missing_required` AND `flagged` are both **Blocking** — splitting them across
 * the two levels, which is what "implement Blocking/Warning" first looked like,
 * would have been a regression dressed as conformance. The Warning level is
 * `ambiguities`: a column that matched two canonical fields too closely to
 * choose, or two columns claiming one target. It does not block the lock.
 *
 * WHAT THIS SCREEN DELIBERATELY DOES NOT DO. The target says a warning "records
 * the accepted exception in the plan version". No such write path exists — there
 * is no endpoint, and no column, that records an accepted exception. So the
 * ambiguities are STATED and nothing offers to accept them. A button that
 * recorded nothing would be worse than its absence: it would make the plan
 * version look decided when it is not.
 *
 * AD-35: composed from `../../ui` only — the console's single entry point. It
 * used to import eleven components from the MUI package, as the 53rd importer of
 * a library the repo is migrating away from. The package name is deliberately
 * NOT spelled here: `grep -rl` over that string is how the migration is
 * measured, and a mention in prose would keep this file counted as an importer
 * of something it no longer imports.
 *
 * WCAG 2.2 AA: a semantic table with header scope; every state is textual (never
 * colour alone); the gate is an `aria-live` region so a change after a
 * resolution is announced.
 */
import {
  Badge,
  Button,
  Checkbox,
  NativeSelect,
  Panel,
  Status,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
  TableScroll,
  Textarea,
  formatPercent,
} from "../../ui";

export type FieldStatus = "matched" | "ambiguous" | "unmatched" | "flagged";

export interface MappedField {
  source_column: string;
  canonical_target: string | null;
  confidence: number | null;
  status: FieldStatus;
}

export interface Placement {
  class: "planned" | "actual" | "extrapolated";
  metric: string;
  dimension: string[];
  period: string;
}

export interface FlaggedField {
  field_id?: string;
  source_column?: string;
  canonical_target?: string;
  blocking_reason?: string | null;
}

export interface GateStatus {
  passed: boolean;
  missing_required: string[];
  flagged: FlaggedField[];
}

/** The WARNING level: recognizer ambiguities, which never block the lock. */
export interface Ambiguity {
  code: string;
  source_column?: string;
  canonical_target?: string;
  candidates?: string[];
  source_columns?: string[];
}

/**
 * Les lectures que l'AC3 de 38.16 demande a l'apercu de RAPPORTER, et que cet
 * ecran ne montrait pas — bien que le serveur les serve.
 *
 * Le verbe est « rapporte ». Rien ici n'est un choix : un format de date est
 * DECLARE par le gabarit, une unite est DECLAREE par le champ canonique, un
 * rejet est COMPTE par l'analyseur. L'ecran les rend ; il n'en decide aucune.
 */
export interface Coercion {
  column: string;
  coercion: "date" | "amount";
  /** `null` = le defaut de l'analyseur. Ce n'est PAS un format declare. */
  declared_format: string | null;
}

export interface RowValidation {
  detected_row_count: number;
  accepted_row_count: number;
  rejected_row_count: number;
  /** `null` quand rien n'a ete compte. 0 rejetee sur un total inconnu ≠ 0 %. */
  rejected_row_pct: number | null;
  by_rule: Array<{
    rule: string;
    count: number;
    first_row_number: number | null;
    fields: string[];
  }>;
}

export interface UnitReading {
  field_id: string;
  /** The canonical field's own word, served by `_unit_report`. `NOT NULL` on
   *  the registry, so it is absent only from a preview built before that read
   *  carried it. */
  canonical_name?: string | null;
  concept_kind: string | null;
  unit: string | null;
  currency_scope: string | null;
  aggregation: string | null;
  non_additive: boolean;
}

export interface Classification {
  vocabulary: string[];
  declared_by: string | null;
  undeclared_reason: string | null;
  fields: Array<{ field_id: string; classification: string }>;
}

export interface VocabularyReading {
  column: string;
  axis?: string;
  [key: string]: unknown;
}

interface Props {
  fields: MappedField[];
  placement: Placement;
  gate: GateStatus;
  /** Canonical field ids the operator may pick to resolve a flagged/unmatched field. */
  canonicalOptions: string[];
  /** Warnings. Absent is not the same as empty, but both render as "none". */
  ambiguities?: Ambiguity[];
  /**
   * Les cinq lectures restantes de l'AC3. TOUTES optionnelles, et absentes ≠
   * vides : un apercu bloque n'en porte pas, et rendre « 0 ligne rejetee » pour
   * une analyse qui n'a jamais tourne serait la meme invention que le taux de 0 %
   * que le serveur refuse de calculer.
   */
  coercions?: Coercion[];
  rowValidation?: RowValidation | null;
  units?: UnitReading[];
  classification?: Classification | null;
  vocabularies?: VocabularyReading[];
  onResolveField?: (sourceColumn: string, canonicalId: string) => void;
  /**
   * AI-248. Le vocabulaire canonique peut etre VIDE -- mesure le 2026-08-08 :
   * `app.mdm_canonical_fields` portait 0 ligne aux deux portees, alors que six
   * modules le lisent. Le select ci-dessous s'affichait donc avec sa seule option
   * vide, et `canSubmitResolutions` restait faux pour toujours : un ecran sans
   * issue et sans phrase.
   *
   * Optionnel, et pour la raison que ce fichier se reproche deja plus bas : un
   * controle n'existe que s'il a un gestionnaire.
   */
  onDeclareField?: (sourceColumn: string) => void;
  onConfirm?: () => void;
  warningsAccepted?: boolean;
  warningReason?: string;
  onWarningAcceptanceChange?: (accepted: boolean) => void;
  onWarningReasonChange?: (reason: string) => void;
  confirming?: boolean;
  canSubmitResolutions?: boolean;
}

function confidenceLabel(confidence: number | null): string {
  // Null = not scored yet; never a fabricated percentage. `to confirm` is this
  // screen's word for that, and it is not the console's dash.
  return confidence == null ? "to confirm" : formatPercent(confidence);
}

function statusLabel(status: FieldStatus): string {
  switch (status) {
    case "matched":
      return "Matched";
    case "ambiguous":
      return "Ambiguous — pick one";
    case "flagged":
      return "Flagged — needs a target";
    default:
      return "Unmatched (ignored)";
  }
}

function needsResolution(status: FieldStatus): boolean {
  return status === "ambiguous" || status === "flagged" || status === "unmatched";
}

function ambiguityLabel(item: Ambiguity): string {
  if (item.code === "duplicate_target") {
    return `${(item.source_columns ?? []).join(" and ")} both claim ${item.canonical_target}`;
  }
  const candidates = (item.candidates ?? []).join(" or ");
  return candidates
    ? `${item.source_column} matched ${candidates} too closely to choose`
    : `${item.source_column ?? "A column"} is ambiguous`;
}

export default function FileSourceOnboardingReview({
  fields,
  placement,
  gate,
  canonicalOptions,
  ambiguities = [],
  onResolveField,
  onDeclareField,
  onConfirm,
  warningsAccepted = false,
  warningReason = "",
  onWarningAcceptanceChange,
  onWarningReasonChange,
  confirming = false,
  canSubmitResolutions = false,
  coercions,
  rowValidation,
  units,
  classification,
  vocabularies,
}: Props) {
  // Both lists are Blocking: `flagged` carries required fields whose binding is
  // blocking, never an optional low-confidence one.
  const blockedCount = gate.missing_required.length + gate.flagged.length;

  return (
    <div className="grid gap-4" data-testid="file-source-onboarding-review">
      <p className="m-0 text-body text-text-secondary">
        Review how each source column maps to a canonical field, the placement, and
        the validation gate. Resolve any blocking field, then confirm to lock the
        template.
      </p>

      {/*
        CE QUI VA ARRIVER A CE FICHIER — l'AC3 de 38.16.

        La grille ci-dessous dit sur QUEL champ chaque colonne atterrit. Elle ne
        dit pas ce qu'il advient des VALEURS : sous quel format une date sera
        lue, dans quelle unite un montant sera compte, combien de lignes ne
        passeront pas. Une personne l'apprenait en important.

        Chaque bloc est absent quand la lecture l'est. « Aucune ligne rejetee »
        pour une analyse qui n'a jamais tourne serait la meme invention que le
        taux de 0 % que le serveur refuse de calculer.
      */}
      {(rowValidation || coercions?.length || units?.length || classification) && (
        <div className="grid gap-2" data-testid="ac3-readings">
          {rowValidation && (
            <p className="m-0 text-ui text-text-secondary" data-testid="row-validation">
              {rowValidation.accepted_row_count} of{" "}
              {rowValidation.detected_row_count} row(s) would land
              {rowValidation.rejected_row_pct !== null
                ? ` (${rowValidation.rejected_row_pct}% rejected)`
                : ""}
              {rowValidation.by_rule.length > 0
                ? `: ${rowValidation.by_rule
                    .map(
                      (entry) =>
                        `${entry.count} x ${entry.rule}` +
                        (entry.first_row_number !== null
                          ? ` (first at line ${entry.first_row_number})`
                          : ""),
                    )
                    .join("; ")}`
                : "."}
            </p>
          )}

          {coercions?.map((entry) => (
            <p
              key={`coercion-${entry.column}`}
              className="m-0 text-ui text-text-secondary"
              data-testid={`coercion-${entry.column}`}
            >
              {entry.column} will be read as {entry.coercion}
              {/*
                `null` dit « le defaut de l'analyseur », et ce n'est pas le meme
                enonce qu'un format declare. Imprimer un defaut ici cacherait la
                declaration qui manque.
              */}
              {entry.coercion === "date"
                ? entry.declared_format
                  ? ` using ${entry.declared_format}`
                  : " using the parser default (no format declared)"
                : ""}
            </p>
          ))}

          {units?.filter((entry) => (entry.canonical_name ?? "").trim()).map((entry) => (
            <p
              key={`unit-${entry.field_id}`}
              className="m-0 text-ui text-text-secondary"
              data-testid={`unit-${entry.field_id}`}
            >
              {/* THE FIELD HAS A NAME, so this line prints it. It read
                  "mdm_01KZ... is measured in EUR" -- the canonical id as bare
                  prose, the same defect as a Builder legend reading
                  `mdm_01KZ...`. The name comes from the server
                  (`import_preview._unit_report`); a preview that predates it
                  carries none, and then the sentence is not printed at all
                  rather than printed around an identifier. */}
              {entry.canonical_name}
              {entry.unit ? ` is measured in ${entry.unit}` : " declares no unit"}
              {entry.currency_scope ? `, currency scope ${entry.currency_scope}` : ""}
              {entry.aggregation
                ? `, aggregated by ${entry.aggregation}`
                : entry.non_additive
                  ? ", non-additive"
                  : ""}
            </p>
          ))}

          {classification && (
            <p className="m-0 text-ui text-text-secondary" data-testid="classification">
              {/*
                JE NE SAIS PAS, ET JE LE DIS. Rendre six cases vides se lirait
                comme « rien de sensible ici ». Le refus se nomme.
              */}
              {classification.declared_by
                ? `Sensitivity declared by ${classification.declared_by}.`
                : "Sensitive classification is not declared for these fields " +
                  "(the canonical field registry carries none), so every field " +
                  "reads unknown."}
            </p>
          )}

          {vocabularies?.map((entry) => (
            <p
              key={`vocabulary-${entry.column}`}
              className="m-0 text-ui text-text-secondary"
              data-testid={`vocabulary-${entry.column}`}
            >
              {entry.column} reads against {entry.axis ?? "an international standard"}.
            </p>
          ))}
        </div>
      )}

      {/* Placement summary (matrix class + coordinates). */}
      <div
        data-testid="placement-summary"
        className="flex flex-wrap items-center gap-2"
      >
        <Badge outline data-testid="placement-class">
          Class: {placement.class}
        </Badge>
        <span className="text-body text-text-secondary">
          Metric <strong className="text-text">{placement.metric}</strong> · Period{" "}
          <strong className="text-text">{placement.period}</strong> · Dimensions{" "}
          <strong className="text-text">{placement.dimension.join(", ") || "—"}</strong>
        </span>
      </div>

      {/* Per-column recognition with confidence. */}
      <TableScroll label="Column mapping">
        <Table aria-label="Column mapping">
          <TableHeader>
            <TableRow>
              <TableHead scope="col">Source column</TableHead>
              <TableHead scope="col">Canonical field</TableHead>
              <TableHead scope="col">Confidence</TableHead>
              <TableHead scope="col">Status</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {fields.map((f) => (
              <TableRow key={f.source_column} data-testid={`map-row-${f.source_column}`}>
                <TableCell>
                  <strong className="text-text">{f.source_column}</strong>
                </TableCell>
                <TableCell>
                  {/* Un controle n'existe que s'il a un gestionnaire. Sans
                      `onResolveField`, ce `select` s'affichait et ne faisait
                      RIEN -- c'est ce que ce fichier reproche par ailleurs au
                      bouton d'acceptation des Warnings, et je l'avais livre ici
                      moi-meme. En lecture seule, l'etat se lit, il ne se
                      manipule pas. */}
                  {(needsResolution(f.status) || !gate.passed) &&
                  canonicalOptions.length === 0 ? (
                    // Le vocabulaire est vide : un select a une seule option vide
                    // n'est pas un controle, c'est une impasse. On dit ce qui
                    // manque et on ouvre la porte -- ou, sans gestionnaire, on dit
                    // au moins pourquoi rien n'est proposable.
                    <div className="flex flex-col gap-1">
                      <span
                        className="text-body text-text-secondary"
                        data-testid={`empty-vocabulary-${f.source_column}`}
                      >
                        This project has declared no canonical field.
                      </span>
                      {onDeclareField ? (
                        <Button
                          size="sm"
                          variant="secondary"
                          data-testid={`declare-${f.source_column}`}
                          onClick={() => onDeclareField(f.source_column)}
                        >
                          Declare “{f.source_column}”
                        </Button>
                      ) : null}
                    </div>
                  ) : (needsResolution(f.status) || !gate.passed) && onResolveField ? (
                    // `Field` carries a VISIBLE label, which a table cell must not
                    // repeat on every row; the accessible name goes on the control
                    // itself. Not a new pattern: it is what this component already
                    // did, and `Field` gains nothing here.
                    <NativeSelect
                      aria-label={`Canonical field for ${f.source_column}`}
                      data-testid={`resolve-${f.source_column}`}
                      value={f.canonical_target ?? ""}
                      onChange={(e: React.ChangeEvent<HTMLSelectElement>) =>
                        onResolveField?.(f.source_column, e.target.value)
                      }
                    >
                      <option value="">— select —</option>
                      {canonicalOptions.map((id) => (
                        <option key={id} value={id}>
                          {id}
                        </option>
                      ))}
                    </NativeSelect>
                  ) : f.canonical_target ? (
                    <Badge outline>{f.canonical_target}</Badge>
                  ) : (
                    <span className="text-body text-text-secondary">Unmapped</span>
                  )}
                </TableCell>
                <TableCell data-testid={`confidence-${f.source_column}`}>
                  {confidenceLabel(f.confidence)}
                </TableCell>
                <TableCell data-testid={`status-${f.source_column}`}>
                  {statusLabel(f.status)}
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </TableScroll>

      {/* Blocking — the gate. aria-live so a resolution is announced. */}
      <div role="status" aria-live="polite" data-testid="gate-status">
        {gate.passed ? (
          <Status as="block" tone="success" title="Validation gate passed" data-testid="gate-passed">
            The required canonical fields were recovered and every required mapping
            is confirmed.
          </Status>
        ) : (
          <Status
            as="block"
            tone="error"
            title={`Blocking — ${blockedCount} field(s) prevent the lock`}
            data-testid="gate-flagged"
          >
            {gate.missing_required.length > 0 && (
              <span data-testid="gate-missing-required">
                Missing required: {gate.missing_required.join(", ")}.{" "}
              </span>
            )}
            {gate.flagged.length > 0 && (
              <span data-testid="gate-flagged-fields">
                Unresolved required mapping:{" "}
                {gate.flagged
                  .map((f) => f.source_column ?? f.field_id ?? f.canonical_target ?? "field")
                  .join(", ")}
                .
              </span>
            )}
          </Status>
        )}
      </div>

      {/* Warning — stated, never silently accepted. */}
      {ambiguities.length > 0 && (
        <Status
          as="block"
          tone="warning"
          title={`Warning — ${ambiguities.length} ambiguous match(es)`}
          data-testid="gate-ambiguities"
        >
          <ul className="m-0 grid gap-1 pl-4">
            {ambiguities.map((item, index) => (
              <li key={`${item.code}-${item.source_column ?? item.canonical_target ?? index}`}>
                {ambiguityLabel(item)}
              </li>
            ))}
          </ul>
          {onWarningAcceptanceChange && (
            <div className="mt-3 grid gap-2">
              <label className="flex items-start gap-2 text-body text-text">
                <Checkbox
                  checked={warningsAccepted}
                  onCheckedChange={(value) =>
                    onWarningAcceptanceChange(value === true)
                  }
                  aria-label="Accept file-source warnings"
                  data-testid="accept-warnings"
                />
                <span>
                  I reviewed these warnings and accept them for the pending mapping
                  version.
                </span>
              </label>
              {warningsAccepted && onWarningReasonChange && (
                <Textarea
                  value={warningReason}
                  onChange={(event) => onWarningReasonChange(event.target.value)}
                  aria-label="Warning acceptance reason"
                  placeholder="Why is this exception safe?"
                  data-testid="warning-reason"
                />
              )}
            </div>
          )}
        </Status>
      )}

      {/* Meme regle pour le verrouillage. Le panneau de revue d'echantillon
          (workbench) n'a rien a verrouiller : le template EXISTE deja et il est
          immuable. Afficher un bouton « Confirm and lock » y ferait croire a une
          action qui n'a pas de sens dans ce contexte -- et, tant qu'aucun ecran
          d'authoring ne passe `onConfirm`, a une action qui n'aboutit nulle
          part. */}
      {onConfirm && (
        <Panel className="flex flex-wrap items-center gap-3 p-4">
          <Button
            disabled={
              (!gate.passed && !canSubmitResolutions) ||
              confirming ||
              (ambiguities.length > 0 && (!warningsAccepted || !warningReason.trim()))
            }
            onClick={() => onConfirm()}
            data-testid="confirm-lock"
          >
            {confirming ? "Recording confirmation" : "Confirm pending mapping"}
          </Button>
          {!gate.passed && !canSubmitResolutions && (
            <span className="text-caption text-text-secondary" data-testid="confirm-blocked-hint">
              Map every required field and resolve the blocking mappings to enable lock.
            </span>
          )}
        </Panel>
      )}
    </div>
  );
}
