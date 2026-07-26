/**
 * Canonical source-first Datastream wizard.
 *
 * The six revisitable stages share one versioned server contract. Validation
 * always targets the exact immutable plan saved by this wizard, and activation
 * is blocked after any configuration drift until the plan is revalidated.
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
  activateDatastream,
  configureManagedFeed,
  fileToBase64,
  getSourceCapabilities,
  listConnections,
  previewImport,
  reviseDatastreamDraft,
  validateDatastreamDraft,
  saveDatastreamDraft,
  type WizardApiConfig,
} from "./wizardApi";
import {
  activationBlockingReasons,
  buildIntent,
  connectionBlockingReason,
  currentIntentSignature,
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
  /** Called after activation succeeds (e.g. return to the list). */
  onActivated?: (datastreamId: string) => void;
  /** Called when the operator cancels/leaves the wizard. */
  onCancel?: () => void;
}

export default function CreerFluxWizard({
  projectId,
  apiBase = "",
  onActivated,
  onCancel,
}: Props) {
  const [draft, setDraft] = useState<WizardDraft>(emptyDraft);
  const [stageIndex, setStageIndex] = useState(0);
  const [visited, setVisited] = useState<Set<WizardStage>>(new Set(["source"]));

  const [connections, setConnections] = useState<ConnectionSummary[]>([]);
  const [connectionsLoading, setConnectionsLoading] = useState(true);
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
  const capabilitiesRequest = useRef(0);
  const scopeGeneration = useRef(0);
  const mutationKeys = useRef<
    Record<string, { signature: string; key: string } | undefined>
  >({});

  const idempotencyKeyFor = useCallback((operation: string, payload: unknown) => {
    const signature = JSON.stringify(payload);
    const pending = mutationKeys.current[operation];
    if (pending?.signature === signature) return pending.key;
    const key = crypto.randomUUID();
    mutationKeys.current[operation] = { signature, key };
    return key;
  }, []);

  const clearIdempotencyKey = useCallback((operation: string, key: string) => {
    if (mutationKeys.current[operation]?.key === key) {
      delete mutationKeys.current[operation];
    }
  }, []);

  const cfg: WizardApiConfig = useMemo(
    () => ({ apiBase, projectId }),
    [apiBase, projectId],
  );

  const stage = WIZARD_STAGES[stageIndex];

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  // Route scope is part of every command identity. A mounted wizard changing
  // projects must discard all prior draft/evidence and invalidate in-flight work.
  useEffect(() => {
    scopeGeneration.current += 1;
    capabilitiesRequest.current += 1;
    mutationKeys.current = {};
    setDraft(emptyDraft());
    setStageIndex(0);
    setVisited(new Set(["source"]));
    setConnections([]);
    setConnectionsLoading(true);
    setCapabilities(null);
    setCapabilitiesLoading(false);
    setError(null);
    setAnnouncement("");
    setSavingDraft(false);
    setPreviewBusy(false);
    setValidating(false);
    setActivating(false);
  }, [apiBase, projectId]);

  // Move focus to the error region when an error appears (WCAG focus mgmt).
  useEffect(() => {
    if (error && errorRef.current) errorRef.current.focus();
  }, [error]);

  // Load only provider accounts authorized for the route project.
  useEffect(() => {
    let cancelled = false;
    setConnectionsLoading(true);
    listConnections(cfg)
      .then((rows) => {
        if (!cancelled && mounted.current) {
          setConnections(rows);
          setConnectionsLoading(false);
        }
      })
      .catch((reason) => {
        if (!cancelled && mounted.current) {
          setConnections([]);
          setConnectionsLoading(false);
          setError(reason instanceof Error ? reason.message : "Provider accounts are unavailable.");
        }
      });
    return () => {
      cancelled = true;
    };
  }, [cfg]);

  const patch = useCallback((next: Partial<WizardDraft>) => {
    setDraft((prev) => ({ ...prev, ...next }));
  }, []);

  // ---- Connector capabilities load on connection change -------------------
  const loadCapabilities = useCallback(
    async (connectionRefId: string) => {
      const requestId = ++capabilitiesRequest.current;
      setCapabilitiesLoading(true);
      setCapabilities(null);
      setError(null);
      try {
        const caps = await getSourceCapabilities(cfg, connectionRefId);
        if (mounted.current && requestId === capabilitiesRequest.current) {
          setCapabilities(caps);
        }
      } catch (reason) {
        if (mounted.current && requestId === capabilitiesRequest.current) {
          setError(reason instanceof Error ? reason.message : "Source capabilities are unavailable.");
        }
      } finally {
        if (mounted.current && requestId === capabilitiesRequest.current) {
          setCapabilitiesLoading(false);
        }
      }
    },
    [cfg],
  );

  const onConnectionChange = useCallback(
    (id: string) => {
      const selected = connections.find(
        (connection) => (connection.connection_ref_id ?? connection.id) === id,
      );
      const blocked = connectionBlockingReason(selected);
      if (blocked) {
        setError(blocked);
        return;
      }
      patch({
        connectionRefId: id,
        reportId: null,
        selectedMetrics: [],
        selectedDimensions: [],
        grain: [],
        plan: null,
        validatedIntentSignature: null,
      });
      void loadCapabilities(id);
    },
    [connections, patch, loadCapabilities],
  );

  const onSheetsConnectionChange = useCallback(
    (id: string) => {
      const selected = connections.find(
        (connection) => (connection.connection_ref_id ?? connection.id) === id,
      );
      const blocked = connectionBlockingReason(selected);
      if (blocked) {
        setError(blocked);
        return;
      }
      patch({ sheetsConnectionId: id, plan: null, validatedIntentSignature: null });
    },
    [connections, patch],
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
        validatedIntentSignature: null,
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
        return { ...prev, [key]: next, plan: null, validatedIntentSignature: null };
      });
    },
    [],
  );

  // ---- Managed-feed file upload preview (12.9) ----------------------------
  const onUpload = useCallback(
    async (file: File) => {
      if (!draft.datastreamId) {
        setError("Save a draft before previewing a file.");
        return;
      }
      const operationScope = scopeGeneration.current;
      setPreviewBusy(true);
      setError(null);
      try {
        const b64 = await fileToBase64(file);
        const preview = await previewImport(cfg, draft.datastreamId, b64, file.name);
        if (!mounted.current || operationScope !== scopeGeneration.current) return;
        // A new preview supersedes any validated plan (schema may have drifted).
        patch({ importPreview: preview, plan: null, validatedIntentSignature: null });
        setAnnouncement(
          `Preview ready: ${preview.row_count} row(s), ${preview.rejected_count} rejected.`,
        );
      } catch (reason) {
        if (mounted.current && operationScope === scopeGeneration.current) {
          setError(reason instanceof Error ? reason.message : "Preview failed.");
        }
      } finally {
        if (mounted.current && operationScope === scopeGeneration.current) {
          setPreviewBusy(false);
        }
      }
    },
    [cfg, draft.datastreamId, patch],
  );

  // ---- Save/revise one recoverable versioned draft ------------------------
  const persistDraft = useCallback(
    async (reason: string) => {
      if (!draft.sourceKind) throw new Error("Choose a source before saving a draft.");
      if (!draft.name.trim()) throw new Error("Enter a Datastream name before saving.");

      const intent = buildIntent(draft);
      const body = { name: draft.name.trim(), intent, reason };
      const operation = draft.datastreamId ? `revise:${draft.datastreamId}` : "create";
      // The reason is evidence, not command identity. A retry from another wizard
      // action with the same exact name/intent must recover the same create.
      const key = idempotencyKeyFor(operation, { name: body.name, intent });
      const result = draft.datastreamId
        ? await reviseDatastreamDraft(cfg, draft.datastreamId, body, key)
        : await saveDatastreamDraft(cfg, body, key);
      clearIdempotencyKey(operation, key);
      return { result, intent, signature: currentIntentSignature(draft) };
    },
    [cfg, clearIdempotencyKey, draft, idempotencyKeyFor],
  );

  const saveDraft = useCallback(async () => {
    const operationScope = scopeGeneration.current;
    setSavingDraft(true);
    setError(null);
    setAnnouncement("");
    try {
      const { result } = await persistDraft("wizard_draft_saved");
      if (!mounted.current || operationScope !== scopeGeneration.current) return;
      patch({ datastreamId: result.id, plan: null, validatedIntentSignature: null });
      setAnnouncement("Draft saved. The server created an immutable plan version.");

      if (
        draft.sourceKind === "managed_feed" &&
        draft.managedFeedFormat === "google_sheets" &&
        draft.spreadsheetId &&
        draft.sheetRange
      ) {
        if (draft.sheetsConnectionId) {
          const syncBody = {
            connection_id: draft.sheetsConnectionId,
            spreadsheet_id: draft.spreadsheetId,
            sheet_range: draft.sheetRange,
            cadence_mode: draft.schedule.cadence_mode,
            quota_profile: { allow_hourly: draft.schedule.allow_hourly },
          };
          const operation = `managed-feed:${result.id}`;
          const key = idempotencyKeyFor(operation, syncBody);
          try {
            await configureManagedFeed(cfg, result.id, syncBody, key);
            clearIdempotencyKey(operation, key);
            if (mounted.current && operationScope === scopeGeneration.current) {
              setAnnouncement("Draft saved. Google Sheets synchronization is configured.");
            }
          } catch (reason) {
            if (mounted.current && operationScope === scopeGeneration.current) {
              setAnnouncement(
                reason instanceof Error
                  ? `Draft saved, but synchronization configuration failed: ${reason.message}`
                  : "Draft saved, but synchronization configuration failed.",
              );
            }
          }
        } else if (mounted.current && operationScope === scopeGeneration.current) {
          setAnnouncement(
            "Draft saved. Select a Google provider account before scheduling synchronization.",
          );
        }
      }
    } catch (reason) {
      if (mounted.current && operationScope === scopeGeneration.current) {
        setError(reason instanceof Error ? reason.message : "Draft could not be saved.");
      }
    } finally {
      if (mounted.current && operationScope === scopeGeneration.current) setSavingDraft(false);
    }
  }, [
    cfg,
    clearIdempotencyKey,
    draft,
    idempotencyKeyFor,
    patch,
    persistDraft,
  ]);

  // ---- Classified fields (source-agnostic) --------------------------------
  const classifiedFields = useMemo(
    () => deriveClassifiedFields(draft, capabilities),
    [draft, capabilities],
  );

  // ---- Validate the exact saved plan against current server capabilities ---
  const validatePlan = useCallback(async () => {
    const operationScope = scopeGeneration.current;
    setValidating(true);
    setError(null);
    setAnnouncement("");
    try {
      const { result, intent, signature } = await persistDraft("wizard_preview_validated");
      if (!mounted.current || operationScope !== scopeGeneration.current) return;
      // Preserve the server-owned draft identity before any later validation call
      // can fail, so retry revises this draft instead of creating another shell.
      patch({ datastreamId: result.id });
      const savedPlan = result.plan_version;
      if (!savedPlan?.id) {
        throw new Error("The server did not return an immutable plan version.");
      }
      const validation = await validateDatastreamDraft(cfg, result.id, intent);
      if (
        savedPlan.content_hash &&
        validation.content_hash &&
        savedPlan.content_hash !== validation.content_hash
      ) {
        throw new Error("Intent evidence no longer matches the saved plan version.");
      }
      const savedCapabilityContract = savedPlan.capability_contract_version ?? null;
      const savedCapabilityFingerprint = savedPlan.capability_fingerprint ?? null;
      if (
        draft.sourceKind === "connector_pull" &&
        (!savedCapabilityContract || !savedCapabilityFingerprint)
      ) {
        throw new Error("The saved connector plan lacks frozen capability evidence.");
      }
      if (
        savedCapabilityContract !== validation.capability_contract_version ||
        savedCapabilityFingerprint !== validation.capability_fingerprint
      ) {
        throw new Error("Capability evidence no longer matches the saved plan version.");
      }

      const normalizedSource =
        (validation.normalized_intent.source as Record<string, unknown> | undefined) ?? {};
      const selection =
        (normalizedSource.selection as Record<string, unknown> | undefined) ?? {};
      const historical =
        (validation.normalized_intent.historical as Record<string, unknown> | undefined) ?? {};
      const schedule =
        (validation.normalized_intent.schedule as Record<string, unknown> | undefined) ?? {};

      const grain = Array.isArray(selection.grain)
        ? selection.grain.filter((value): value is string => typeof value === "string")
        : [];
      const plan: ValidationPlan = {
        plan_version_id: savedPlan.id,
        mapping_version_id: null,
        intent_content_hash: savedPlan.content_hash ?? validation.content_hash,
        capability_contract_version: savedCapabilityContract,
        capability_fingerprint: savedCapabilityFingerprint,
        full_grain: grain,
        interval: {
          start: typeof historical.start === "string" ? historical.start : null,
          end_exclusive:
            typeof historical.end_exclusive === "string" ? historical.end_exclusive : null,
        },
        timezone: typeof schedule.timezone === "string" ? schedule.timezone : null,
        // This endpoint does not issue Safe-KPI or server-DQ evidence.
        safe_kpi_projection: [],
        dq: null,
        rejected_count: draft.importPreview?.rejected_count ?? null,
        classified_fields: classifiedFields,
        blocking_issues: validation.issues.map((issue) => ({
          code: issue.code,
          message: issue.path ? `${issue.message} (${issue.path})` : issue.message,
        })),
      };

      if (!mounted.current || operationScope !== scopeGeneration.current) return;
      patch({
        datastreamId: result.id,
        plan,
        validatedIntentSignature: signature,
      });
      setAnnouncement(
        validation.executable
          ? "Plan validated by the server and ready for activation."
          : `Server validation found ${validation.issues.length} blocking issue(s).`,
      );
    } catch (reason) {
      if (mounted.current && operationScope === scopeGeneration.current) {
        setError(reason instanceof Error ? reason.message : "Plan validation failed.");
      }
    } finally {
      if (mounted.current && operationScope === scopeGeneration.current) setValidating(false);
    }
  }, [cfg, classifiedFields, draft, patch, persistDraft]);

  // ---- Activate -----------------------------------------------------------
  const drifted = isPreviewObsolete(draft);
  const blockingReasons = useMemo(
    () => activationBlockingReasons(draft, draft.plan),
    [draft],
  );

  // Activate the exact current immutable plan. Configuration drift is already
  // represented by blockingReasons, so no plan revision occurs in this command.
  const finalize = useCallback(async () => {
    if (blockingReasons.length > 0 || !draft.datastreamId || !draft.plan?.plan_version_id) {
      return;
    }
    const operationScope = scopeGeneration.current;
    setActivating(true);
    setError(null);
    setAnnouncement("");
    const operation = `activate:${draft.datastreamId}`;
    const command = {
      datastream_id: draft.datastreamId,
      plan_version_id: draft.plan.plan_version_id,
    };
    const key = idempotencyKeyFor(operation, command);
    try {
      const result = await activateDatastream(
        cfg,
        draft.datastreamId,
        draft.plan.plan_version_id,
        key,
      );
      if (result.enabled !== true) {
        throw new Error("The server did not confirm Datastream activation.");
      }
      clearIdempotencyKey(operation, key);
      if (!mounted.current || operationScope !== scopeGeneration.current) return;
      setAnnouncement("Datastream activated. Opening first publication.");
      onActivated?.(draft.datastreamId);
    } catch (reason) {
      if (mounted.current && operationScope === scopeGeneration.current) {
        setError(reason instanceof Error ? reason.message : "Datastream activation failed.");
      }
    } finally {
      if (mounted.current && operationScope === scopeGeneration.current) setActivating(false);
    }
  }, [
    blockingReasons.length,
    cfg,
    clearIdempotencyKey,
    draft.datastreamId,
    draft.plan?.plan_version_id,
    idempotencyKeyFor,
    onActivated,
  ]);

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
              Create a Datastream — {STAGE_LABELS[stage]}
            </Typography>
            <Typography color="text.secondary" sx={{ mt: 0.5 }}>
              Use the same six-stage workflow for every source. You can revisit any
              stage and save a recoverable draft at any time.
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
                onSourceKindChange={(kind: SourceKind) => {
                  capabilitiesRequest.current += 1;
                  setCapabilities(null);
                  setCapabilitiesLoading(false);
                  patch({
                    sourceKind: kind,
                    managedFeedFormat:
                      kind === "managed_feed" ? draft.managedFeedFormat : null,
                    connectionRefId: null,
                    reportId: null,
                    selectedMetrics: [],
                    selectedDimensions: [],
                    grain: [],
                    bigqueryTable: null,
                    importPreview: null,
                    spreadsheetId: null,
                    sheetRange: null,
                    sheetsConnectionId: null,
                    plan: null,
                    validatedIntentSignature: null,
                  });
                }}
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
                connectionsLoading={connectionsLoading}
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
                onGrainChange={(grain) => patch({ grain, plan: null, validatedIntentSignature: null })}
                bigqueryTable={draft.bigqueryTable}
                onTableChange={(bigqueryTable) =>
                  patch({ bigqueryTable, plan: null, validatedIntentSignature: null })
                }
                importPreview={draft.importPreview}
                previewBusy={previewBusy}
                onUpload={onUpload}
                spreadsheetId={draft.spreadsheetId}
                sheetRange={draft.sheetRange}
                onSpreadsheetChange={(spreadsheetId) => patch({ spreadsheetId })}
                onSheetRangeChange={(sheetRange) => patch({ sheetRange })}
                sheetsConnectionId={draft.sheetsConnectionId}
                onSheetsConnectionChange={onSheetsConnectionChange}
              />
            )}
            {stage === "configure" && !draft.sourceKind && (
              <Typography color="text.secondary">
                Return to Source and choose a source.
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
                Back
              </Button>
              <Button
                variant="outlined"
                onClick={next}
                disabled={stageIndex === WIZARD_STAGES.length - 1 || !canAdvance}
                data-testid="wizard-next"
              >
                Next
              </Button>
            </Stack>
            <Stack direction="row" spacing={1}>
              {onCancel && (
                <Button onClick={onCancel} data-testid="wizard-cancel">
                  Cancel
                </Button>
              )}
              <Button
                variant="outlined"
                onClick={saveDraft}
                disabled={savingDraft || !draft.sourceKind}
                data-testid="wizard-save-draft"
              >
                {savingDraft ? "Saving…" : "Save draft"}
              </Button>
            </Stack>
          </Stack>
        </Stack>
      </CardContent>
    </Card>
  );
}
