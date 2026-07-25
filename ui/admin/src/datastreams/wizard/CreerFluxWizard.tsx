/**
 * Story 12.13 — the ONE source-first "Créer un flux" wizard shell.
 *
 * Owns the whole local draft, a revisitable 6-stage stepper (Source, Configure,
 * Destination, Classify & map, Preview & validate, Schedule & activate), a
 * always-available Save draft, and the governed API orchestration. All source
 * paths converge on the SAME versioned mapping / preview / validation /
 * activation contract (AC2). Blocking validation OR source/schema drift disables
 * activation (AC1/AC4).
 *
 * Conventions match the Epic-36 datastream cards exactly: internal state-machine
 * routing (no react-router), MUI components + design tokens, a bearer-token
 * fetch helper (wizardApi), an assertive focusable error region + a polite live
 * region (WizardRegions), French copy that states data impact plainly, and a
 * one-column reflow at narrow widths. WCAG 2.2 AA.
 *
 * Honest Phase-B: the external-BigQuery registration route and a server-side
 * connector "validate plan" route are not yet wired; those paths degrade with
 * explicit copy (see BigQueryConfigSubflow) and the client assembles the
 * validation plan from data it actually holds — it never fabricates a server
 * response.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  Box,
  Button,
  Card,
  CardContent,
  Divider,
  Stack,
  Step,
  StepButton,
  Stepper,
  Typography,
} from "@mui/material";
import { ErrorRegion, LiveRegion } from "./WizardRegions";
import SourceStage from "./SourceStage";
import ConfigureStage from "./ConfigureStage";
import DestinationStage from "./DestinationStage";
import ClassifyStage from "./ClassifyStage";
import PreviewStage from "./PreviewStage";
import ScheduleStage from "./ScheduleStage";
import {
  configureManagedFeed,
  fileToBase64,
  getSourceCapabilities,
  listConnections,
  previewImport,
  reviseDatastreamDraft,
  saveDatastreamDraft,
  type WizardApiConfig,
} from "./wizardApi";
import {
  activationBlockingReasons,
  buildIntent,
  currentSchemaSignature,
  deriveClassifiedFields,
  isPreviewObsolete,
} from "./wizardLogic";
import {
  type ConnectionSummary,
  emptyDraft,
  type ManagedFeedFormat,
  type SourceCapabilities,
  type SourceKind,
  STAGE_LABELS,
  type ValidationPlan,
  WIZARD_STAGES,
  type WizardDraft,
  type WizardStage,
} from "./wizardTypes";

interface Props {
  projectId: string;
  apiBase?: string;
  apiToken?: string;
  /** Called after activation succeeds (e.g. return to the list). */
  onActivated?: (datastreamId: string) => void;
  /** Called when the operator cancels/leaves the wizard. */
  onCancel?: () => void;
}

export default function CreerFluxWizard({
  projectId,
  apiBase = "",
  apiToken = import.meta.env.VITE_ADMIN_API_TOKEN ?? "",
  onActivated,
  onCancel,
}: Props) {
  const [draft, setDraft] = useState<WizardDraft>(emptyDraft);
  const [stageIndex, setStageIndex] = useState(0);
  const [visited, setVisited] = useState<Set<WizardStage>>(new Set(["source"]));

  const [connections, setConnections] = useState<ConnectionSummary[]>([]);
  const [capabilities, setCapabilities] = useState<SourceCapabilities | null>(null);
  const [capabilitiesLoading, setCapabilitiesLoading] = useState(false);

  const [error, setError] = useState<string | null>(null);
  const [announcement, setAnnouncement] = useState("");
  const [savingDraft, setSavingDraft] = useState(false);
  const [previewBusy, setPreviewBusy] = useState(false);
  const [validating, setValidating] = useState(false);
  const [activating, setActivating] = useState(false);

  const errorRef = useRef<HTMLDivElement | null>(null);
  const headingRef = useRef<HTMLHeadingElement | null>(null);
  const mounted = useRef(true);

  const cfg: WizardApiConfig = useMemo(
    () => ({ apiBase, apiToken, projectId }),
    [apiBase, apiToken, projectId],
  );

  const stage = WIZARD_STAGES[stageIndex];

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  // Move focus to the error region when an error appears (WCAG focus mgmt).
  useEffect(() => {
    if (error && errorRef.current) errorRef.current.focus();
  }, [error]);

  // Load project connections once (Source/Configure need them for the connector path).
  useEffect(() => {
    listConnections(cfg)
      .then((rows) => {
        if (mounted.current) setConnections(rows);
      })
      .catch(() => {
        /* Non-fatal: the connector picker simply shows "no connections". */
      });
  }, [cfg]);

  const patch = useCallback((next: Partial<WizardDraft>) => {
    setDraft((prev) => ({ ...prev, ...next }));
  }, []);

  // ---- Connector capabilities load on connection change -------------------
  const loadCapabilities = useCallback(
    async (connectionRefId: string) => {
      setCapabilitiesLoading(true);
      setCapabilities(null);
      try {
        const caps = await getSourceCapabilities(cfg, connectionRefId);
        if (mounted.current) setCapabilities(caps);
      } catch (reason) {
        if (mounted.current) {
          setError(reason instanceof Error ? reason.message : "Capacités indisponibles.");
        }
      } finally {
        if (mounted.current) setCapabilitiesLoading(false);
      }
    },
    [cfg],
  );

  const onConnectionChange = useCallback(
    (id: string) => {
      patch({
        connectionRefId: id,
        reportId: null,
        selectedMetrics: [],
        selectedDimensions: [],
        grain: [],
        plan: null,
        validatedSchemaHash: null,
      });
      void loadCapabilities(id);
    },
    [patch, loadCapabilities],
  );

  const onReportChange = useCallback(
    (reportId: string) => {
      const report = capabilities?.reports.find((r) => r.id === reportId);
      patch({
        reportId,
        selectedMetrics: [],
        selectedDimensions: [],
        grain: report?.supported_grains[0] ?? [],
        plan: null,
        validatedSchemaHash: null,
      });
    },
    [capabilities, patch],
  );

  const onToggleField = useCallback(
    (fieldId: string, kind: "metric" | "dimension") => {
      setDraft((prev) => {
        const key = kind === "metric" ? "selectedMetrics" : "selectedDimensions";
        const current = prev[key];
        const next = current.includes(fieldId)
          ? current.filter((f) => f !== fieldId)
          : [...current, fieldId];
        // Any field change invalidates a previously validated plan.
        return { ...prev, [key]: next, plan: null, validatedSchemaHash: null };
      });
    },
    [],
  );

  // ---- Managed-feed file upload preview (12.9) ----------------------------
  const onUpload = useCallback(
    async (file: File) => {
      if (!draft.datastreamId) {
        setError("Enregistrez d’abord un brouillon avant de prévisualiser un fichier.");
        return;
      }
      setPreviewBusy(true);
      setError(null);
      try {
        const b64 = await fileToBase64(file);
        const preview = await previewImport(cfg, draft.datastreamId, b64, file.name);
        if (!mounted.current) return;
        // A new preview supersedes any validated plan (schema may have drifted).
        patch({ importPreview: preview, plan: null, validatedSchemaHash: null });
        setAnnouncement(
          `Aperçu prêt : ${preview.row_count} ligne(s), ${preview.rejected_count} rejet(s).`,
        );
      } catch (reason) {
        if (mounted.current) {
          setError(reason instanceof Error ? reason.message : "Aperçu impossible.");
        }
      } finally {
        if (mounted.current) setPreviewBusy(false);
      }
    },
    [cfg, draft.datastreamId, patch],
  );

  // ---- Save draft (always available) --------------------------------------
  const saveDraft = useCallback(async () => {
    if (!draft.sourceKind) {
      setError("Choisissez une source avant d’enregistrer un brouillon.");
      return;
    }
    setSavingDraft(true);
    setError(null);
    setAnnouncement("");
    try {
      const result = await saveDatastreamDraft(cfg, {
        name: draft.name || "Nouveau flux",
        intent: buildIntent(draft),
        reason: "wizard_draft_saved",
      });
      if (!mounted.current) return;
      patch({ datastreamId: result.id });
      setAnnouncement("Brouillon enregistré. Les versions du plan sont figées côté serveur.");

      // Google Sheets: once the draft exists, persist the recurring sync config
      // (12.10). The server REQUIRES a non-empty connection_id/spreadsheet_id/
      // sheet_range (422 otherwise), so we ONLY fire the call when a Google
      // connection has actually been picked. Missing connection => honest,
      // non-fabricated degradation copy instead of a doomed request.
      if (
        draft.sourceKind === "managed_feed" &&
        draft.managedFeedFormat === "google_sheets" &&
        draft.spreadsheetId &&
        draft.sheetRange
      ) {
        if (draft.sheetsConnectionId) {
          try {
            await configureManagedFeed(cfg, result.id, {
              connection_id: draft.sheetsConnectionId,
              spreadsheet_id: draft.spreadsheetId,
              sheet_range: draft.sheetRange,
              cadence_mode: draft.schedule.cadence_mode,
              quota_profile: { allow_hourly: draft.schedule.allow_hourly },
            });
            if (mounted.current) {
              setAnnouncement(
                "Brouillon enregistré. Synchronisation Google Sheets configurée.",
              );
            }
          } catch (reason) {
            // Non-fatal for the draft itself: the draft is saved; surface the
            // real reason the sync config did not persist.
            if (mounted.current) {
              setAnnouncement(
                reason instanceof Error
                  ? `Brouillon enregistré. Configuration de synchronisation non appliquée : ${reason.message}`
                  : "Brouillon enregistré. Configuration de synchronisation non appliquée.",
              );
            }
          }
        } else if (mounted.current) {
          setAnnouncement(
            "Brouillon enregistré. Sélectionnez une connexion Google à l’étape Configurer pour programmer la synchronisation récurrente.",
          );
        }
      }
    } catch (reason) {
      if (mounted.current) {
        setError(reason instanceof Error ? reason.message : "Enregistrement impossible.");
      }
    } finally {
      if (mounted.current) setSavingDraft(false);
    }
  }, [cfg, draft, patch]);

  // ---- Classified fields (source-agnostic) --------------------------------
  const classifiedFields = useMemo(
    () => deriveClassifiedFields(draft, capabilities),
    [draft, capabilities],
  );

  // ---- Validate the plan (freeze schema hash for drift detection) ----------
  const validatePlan = useCallback(async () => {
    setValidating(true);
    setError(null);
    try {
      const signature = currentSchemaSignature(draft);
      const grain =
        draft.sourceKind === "connector_pull"
          ? draft.grain
          : classifiedFields
              .filter((f) => f.semantic_role === "date" || f.semantic_role === "identifier")
              .map((f) => f.field_id);

      const blocking: { code: string; message: string }[] = [];
      if (draft.sourceKind === "connector_pull" && draft.selectedMetrics.length === 0) {
        blocking.push({
          code: "no_metrics",
          message: "Sélectionnez au moins une métrique.",
        });
      }
      if (classifiedFields.length === 0) {
        blocking.push({
          code: "no_fields",
          message: "Aucun champ classé : complétez la configuration.",
        });
      }

      const plan: ValidationPlan = {
        // The immutable plan/mapping version ids are minted SERVER-SIDE when the
        // intent is versioned (save_datastream_intent). This local estimate does
        // not have them, so we leave them null rather than fabricating a
        // "pending:<id>" that never exists server-side (M1).
        plan_version_id: null,
        mapping_version_id: null,
        schema_hash: signature,
        full_grain: grain,
        interval: null,
        timezone: draft.schedule.timezone,
        safe_kpi_projection:
          draft.sourceKind === "connector_pull"
            ? draft.selectedMetrics.map((m) => ({ name: m }))
            : [],
        // DQ/rejections known only for the managed-feed preview; otherwise null
        // (honest — no server DQ run happens at wizard-validate time).
        dq:
          draft.sourceKind === "managed_feed" && draft.importPreview
            ? { total_unresolved: draft.importPreview.rejected_count, degraded: false }
            : null,
        rejected_count: draft.importPreview?.rejected_count ?? null,
        classified_fields: classifiedFields,
        blocking_issues: blocking,
      };

      if (!mounted.current) return;
      patch({ plan, validatedSchemaHash: signature });
      setAnnouncement(
        blocking.length > 0
          ? `Plan validé avec ${blocking.length} problème(s) bloquant(s).`
          : "Plan validé : versions et empreinte de schéma figées.",
      );
    } finally {
      if (mounted.current) setValidating(false);
    }
  }, [draft, classifiedFields, patch]);

  // ---- Activate -----------------------------------------------------------
  const drifted = isPreviewObsolete(draft);
  const blockingReasons = useMemo(
    () => activationBlockingReasons(draft, draft.plan),
    [draft],
  );

  // C2 — honest final action. There is NO versioned enable/activate seam wired:
  // PATCH /api/datastreams/{id} with enabled:true returns 422
  // `activation_not_available` ("l'activation versionnée appartient à la Story
  // 12.6"), and the versioned create persists enabled:FALSE (flows.py). So this
  // action PERSISTS the ready-to-activate flow (a fresh versioned intent) and
  // honestly reports that scheduled activation is Phase B — it never claims the
  // server enabled the flow. `finalize` always writes a NEW version so the saved
  // intent reflects the current selection/cadence.
  const finalize = useCallback(async () => {
    if (blockingReasons.length > 0) return; // guarded; button is also disabled.
    setActivating(true);
    setError(null);
    try {
      const payload = {
        name: draft.name || "Nouveau flux",
        intent: buildIntent(draft),
        reason: "wizard_ready_to_activate",
      };
      // Version an EXISTING draft in place (PATCH) rather than creating a
      // duplicate row; create only when there is no draft id yet.
      const result = draft.datastreamId
        ? await reviseDatastreamDraft(cfg, draft.datastreamId, payload)
        : await saveDatastreamDraft(cfg, payload);
      const id = result.id;
      if (!mounted.current) return;
      patch({ datastreamId: id });
      setAnnouncement(
        "Flux enregistré, prêt à activer. L’activation planifiée (mise en service) arrive en Phase B (Story 12.6) ; le flux reste désactivé côté serveur jusque-là.",
      );
      onActivated?.(id);
    } catch (reason) {
      if (mounted.current) {
        setError(reason instanceof Error ? reason.message : "Enregistrement impossible.");
      }
    } finally {
      if (mounted.current) setActivating(false);
    }
  }, [blockingReasons, cfg, draft, patch, onActivated]);

  // ---- Stage navigation (revisitable) -------------------------------------
  const goToStage = useCallback((index: number) => {
    setStageIndex(index);
    setVisited((prev) => new Set(prev).add(WIZARD_STAGES[index]));
    // Return focus to the stage heading for logical focus order.
    requestAnimationFrame(() => headingRef.current?.focus());
  }, []);

  const canAdvance = useMemo(() => {
    if (stage === "source") return Boolean(draft.sourceKind && draft.name.trim());
    return true; // later stages are freely revisitable per AC1.
  }, [stage, draft.sourceKind, draft.name]);

  const next = useCallback(() => {
    if (stageIndex < WIZARD_STAGES.length - 1) goToStage(stageIndex + 1);
  }, [stageIndex, goToStage]);
  const back = useCallback(() => {
    if (stageIndex > 0) goToStage(stageIndex - 1);
  }, [stageIndex, goToStage]);

  return (
    <Card component="section" variant="outlined" sx={{ borderRadius: 3, maxWidth: 980 }}>
      <CardContent>
        <Stack spacing={2}>
          <Box>
            <Typography
              component="h2"
              variant="h5"
              tabIndex={-1}
              ref={headingRef}
              sx={{ outline: "none" }}
            >
              Créer un flux — {STAGE_LABELS[stage]}
            </Typography>
            <Typography color="text.secondary" sx={{ mt: 0.5 }}>
              Un seul assistant, quelle que soit la source. Les étapes sont
              révisables et « Enregistrer le brouillon » est disponible à tout
              moment.
            </Typography>
          </Box>

          {/* Revisitable stepper (non-linear). */}
          <Stepper
            nonLinear
            activeStep={stageIndex}
            alternativeLabel
            sx={{ flexWrap: "wrap" }}
          >
            {WIZARD_STAGES.map((s, i) => (
              <Step key={s} completed={visited.has(s) && i < stageIndex}>
                <StepButton
                  onClick={() => goToStage(i)}
                  data-testid={`stepper-${s}`}
                >
                  {STAGE_LABELS[s]}
                </StepButton>
              </Step>
            ))}
          </Stepper>

          <ErrorRegion ref={errorRef} error={error} />
          <LiveRegion message={announcement} />

          <Divider />

          {/* Active stage body. */}
          <Box sx={{ minHeight: 240 }}>
            {stage === "source" && (
              <SourceStage
                name={draft.name}
                onNameChange={(name) => patch({ name })}
                sourceKind={draft.sourceKind}
                onSourceKindChange={(kind: SourceKind) =>
                  patch({ sourceKind: kind, plan: null, validatedSchemaHash: null })
                }
                managedFeedFormat={draft.managedFeedFormat}
                onManagedFeedFormatChange={(format: ManagedFeedFormat) =>
                  patch({ managedFeedFormat: format })
                }
              />
            )}

            {stage === "configure" && draft.sourceKind && (
              <ConfigureStage
                sourceKind={draft.sourceKind}
                managedFeedFormat={draft.managedFeedFormat}
                datastreamId={draft.datastreamId}
                connections={connections}
                connectionRefId={draft.connectionRefId}
                onConnectionChange={onConnectionChange}
                capabilities={capabilities}
                capabilitiesLoading={capabilitiesLoading}
                reportId={draft.reportId}
                onReportChange={onReportChange}
                selectedMetrics={draft.selectedMetrics}
                selectedDimensions={draft.selectedDimensions}
                onToggleField={onToggleField}
                grain={draft.grain}
                onGrainChange={(grain) => patch({ grain, plan: null, validatedSchemaHash: null })}
                bigqueryTable={draft.bigqueryTable}
                onTableChange={(bigqueryTable) =>
                  patch({ bigqueryTable, plan: null, validatedSchemaHash: null })
                }
                importPreview={draft.importPreview}
                previewBusy={previewBusy}
                onUpload={onUpload}
                spreadsheetId={draft.spreadsheetId}
                sheetRange={draft.sheetRange}
                onSpreadsheetChange={(spreadsheetId) => patch({ spreadsheetId })}
                onSheetRangeChange={(sheetRange) => patch({ sheetRange })}
                sheetsConnectionId={draft.sheetsConnectionId}
                onSheetsConnectionChange={(sheetsConnectionId) =>
                  patch({ sheetsConnectionId })
                }
              />
            )}
            {stage === "configure" && !draft.sourceKind && (
              <Typography color="text.secondary">
                Revenez à l’étape Source pour choisir une source.
              </Typography>
            )}

            {stage === "destination" && draft.sourceKind && (
              <DestinationStage
                sourceKind={draft.sourceKind}
                destinationDataset={draft.destinationDataset}
                onDatasetChange={(destinationDataset) => patch({ destinationDataset })}
                bigqueryTable={draft.bigqueryTable}
              />
            )}

            {stage === "classify" && (
              <ClassifyStage fields={classifiedFields} loading={capabilitiesLoading} />
            )}

            {stage === "preview" && (
              <PreviewStage
                plan={draft.plan}
                validating={validating}
                drifted={drifted}
                onValidate={validatePlan}
                canValidate={Boolean(draft.sourceKind)}
              />
            )}

            {stage === "schedule" && (
              <ScheduleStage
                schedule={draft.schedule}
                onScheduleChange={(schedule) => patch({ schedule })}
                blockingReasons={blockingReasons}
                activating={activating}
                onActivate={finalize}
              />
            )}
          </Box>

          <Divider />

          {/* Footer nav: Back / Next + always-available Save draft + Cancel. */}
          <Stack
            direction={{ xs: "column", sm: "row" }}
            spacing={1}
            sx={{ justifyContent: "space-between", alignItems: { sm: "center" } }}
          >
            <Stack direction="row" spacing={1}>
              <Button onClick={back} disabled={stageIndex === 0} data-testid="wizard-back">
                Précédent
              </Button>
              <Button
                variant="outlined"
                onClick={next}
                disabled={stageIndex === WIZARD_STAGES.length - 1 || !canAdvance}
                data-testid="wizard-next"
              >
                Suivant
              </Button>
            </Stack>
            <Stack direction="row" spacing={1}>
              {onCancel && (
                <Button onClick={onCancel} data-testid="wizard-cancel">
                  Annuler
                </Button>
              )}
              <Button
                variant="outlined"
                onClick={saveDraft}
                disabled={savingDraft || !draft.sourceKind}
                data-testid="wizard-save-draft"
              >
                {savingDraft ? "Enregistrement…" : "Enregistrer le brouillon"}
              </Button>
            </Stack>
          </Stack>
        </Stack>
      </CardContent>
    </Card>
  );
}
