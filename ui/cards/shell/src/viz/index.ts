/**
 * Story 50.5 -- the shared Visualization runtime's public surface.
 *
 * `ui/cards/shell/src/viz/` inside `@toorow/card-shell`, per AD-35
 * (`_bmad-output/specs/spec-toorow/SPEC.md:162`, `:164`), which names
 * `ui/cards/shell` as the shared Visualization runtime and says the console and
 * this runtime "migrate as one". See `ui/cards/shell/README.md` for why this is
 * not a fourth workspace package.
 */

export { default as VisualizationRuntime } from "./Runtime";
export {
  prepareVisualization,
  serializeVisualModel,
  buildNewExecutionRequest,
} from "./Runtime";
export type { SerializedVisualModel, VisualizationRuntimeProps } from "./Runtime";

export { default as ConsoleVisualization, serializeForConsole } from "./entries/console";
export { default as McpAppVisualization, serializeForMcpApp } from "./entries/mcpApp";
export {
  default as ShareVisualization,
  serializeForShare,
  readFrozenRender,
  readFrozenFeedbackContext,
  submitFrozenShareFeedback,
} from "./entries/share";

export {
  resolveRenderer,
  resolveCapability,
  registered as registeredRenderers,
  registeredCapabilities,
  KNOWN_UNBUILT_FAMILIES,
} from "./registry";
export type {
  CapabilityRendererDeclaration,
  RendererDeclaration,
  RendererProps,
} from "./registry";
export { installStandardRenderers } from "./renderers";
export { default as TableFallback } from "./renderers/tableFallback";
export { default as EvidencePath } from "./renderers/evidencePath";
// Story 55.1 -- the `ai_path` family. ONE drawing, consumed by the Console
// (`AiPathPage`, the `ai-path` lens) and bundled into the MCP App resource by
// the same build. It is not resolved through `resolveRenderer`: see the file.
export { default as AiPathFamily } from "./renderers/aiPath";
export {
  AI_PATH_LEVELS,
  AI_PATH_LEVEL_LABELS,
  AI_PATH_NODE_STATES,
  AI_PATH_STATE_STATEMENT,
  AI_PATH_FALLBACK_COLUMNS,
  NO_AI_PATH,
  aiPathFallbackRows,
  aiPathRungs,
  aiPathStepsFromWire,
  levelOfStep,
  nodeStateOfStep,
  reachedNothingGoverned,
} from "./renderers/aiPath";
export type {
  AiPathFamilyProps,
  AiPathJob,
  AiPathLevel,
  AiPathNodeState,
  AiPathSkill,
  AiPathStep,
  AiPathWireSkill,
  AiPathWireStep,
} from "./renderers/aiPath";
export { default as AiPathCapability } from "./aiPathCapability";
export { default as TargetedFeedback } from "./targetedFeedback";
export {
  EXACT_FEEDBACK_SCHEMA,
  EXACT_FEEDBACK_RECEIPT_SCHEMA,
  MAX_FEEDBACK_COMMENT_CHARS,
  decodeExactFeedbackContext,
} from "./targetedFeedback";
export type {
  ExactFeedbackContext,
  ExactFeedbackRequest,
  ExactFeedbackSubmitter,
  FeedbackSelection,
  FeedbackTarget,
  TargetedFeedbackProps,
} from "./targetedFeedback";
export {
  AI_PATH_CAPABILITY_SCHEMA,
  AI_PATH_MISSING_CONTEXT_CODES,
  MAX_OBSERVED_AI_PATH_BYTES,
  MAX_OBSERVED_AI_PATH_STEPS,
  decodeAiPathCapability,
} from "./aiPathCapability";
export type {
  AiPathCapabilityProps,
  AiPathCapabilitySurface,
  ObservedAiPathOwner,
  ObservedAiPathProjection,
  ObservedAiPathSkill,
  ObservedAiPathStep,
} from "./aiPathCapability";
// Story 55.2 -- the branches not taken, and the trace their inspection leaves.
export { default as AiPathBranchSubtree } from "./renderers/aiPathBranches";
export {
  AI_PATH_BRANCH_FATES,
  AI_PATH_BRANCH_STATES,
  AI_PATH_INSPECTION_KINDS,
  AI_PATH_REJECTION_REASONS,
  BRANCHES_NOT_RECORDED,
  BRANCHES_UNAVAILABLE,
  MAX_BRANCHES_PER_STEP,
  MAX_BRANCHES_PER_PATH,
  MAX_BRANCH_EVIDENCE_BYTES,
  MAX_BRANCH_EVIDENCE_BYTES_PER_PATH,
  NOT_REACHED_NOTICE,
  NO_BRANCH_JUDGED,
  RETRIEVAL_BRANCH_EVIDENCE_SCHEMA,
  SELECTED_MEANING,
  allocateAiPathBranchEvidence,
  aiPathBranchesText,
  branchFate,
  branchListingState,
  branchReason,
  decodeAiPathBranchEvidence,
  safeInspect,
  tierScaleStatement,
  walkStatement,
} from "./renderers/aiPathBranches";
export type {
  AiPathBranch,
  AiPathBranchEvidenceAllocationInput,
  AiPathBranchFate,
  AiPathBranchEvidence,
  AiPathBranchKind,
  AiPathBranchState,
  AiPathBranchSubtreeProps,
  AiPathBranchTier,
  AiPathInspection,
  AiPathInspectionKind,
  AiPathInspectionSink,
  AiPathRejectionReason,
  AiPathWalk,
} from "./renderers/aiPathBranches";

export { validateRenderInput, SPEC_CONTRACT_VERSION, SUPPORTED_SCHEMA_VERSIONS } from "./validate";
export { compileVisualModel, DATUM_KEY_SEP } from "./compile/dataset";
export type { CompiledVisualModel } from "./compile/dataset";
export { MAX_CELLS, MAX_ROWS, MAX_SERIES, evaluateVolume } from "./compile/limits";
export { resolveDatumEvidence, datumKeyAt } from "./evidence/resolve";
export type { DatumEvidence, DatumEvidenceResolution } from "./evidence/resolve";
export {
  RESPONSIVE_PROFILES,
  KNOWN_UNSUPPORTED_PROFILES,
  CONTAINER_REFLOW_PX,
  resolveProfile,
  profileLayout,
} from "./responsive";
export {
  RUNTIME_BUILD,
  THEME_VERSION,
  FORMATTER_VERSION,
  checkBuildIdentity,
  rendererBuild,
} from "./buildInfo";
export { VizStatePanel, VIZ_STATE_KINDS } from "./states";
export type { VizStateKind } from "./states";
export {
  formatCell,
  formatDate,
  formatDuration,
  formatNumber,
  FORMATTER_LOCALE,
} from "./theme/formatters";
// The read-time micros division, shared by the Console tables and the MCP App
// so one Result cannot state one amount two ways.
export { formatGovernedValue, microsToUnits } from "./theme/canonicalAmount";
export type { GovernedValueMeaning } from "./theme/canonicalAmount";

export type {
  CellValue,
  DisplayState,
  EvidenceRef,
  RenderInput,
  RenderPins,
  RequestNewExecution,
  ResponsiveProfile,
  ResultOutcome,
  VisualFamilyId,
  VizRefusal,
  VizResult,
  VizSpec,
  VizSpecDocument,
  WellName,
} from "./contracts";
export { VizInputRefused } from "./contracts";
