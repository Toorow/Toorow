import { useCallback, useEffect, useMemo, useState } from "react";
import MdmConflictResolutionDialog from "../../governance/MdmConflictResolutionDialog";
import { apiFetch } from "../../lib/apiFetch";
import {
  Badge,
  Button,
  Metric,
  PageHeader,
  Panel,
  PanelHeader,
  Status,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
  TableScroll,
} from "../../ui";
import "../application.css";

/** The codes this screen can close itself; the others name their own lever. */
const RESOLVABLE_HERE = new Set(["CURRENCY_CONFLICT", "CURRENCY_GAP", "MEASURE_NULL"]);

type ConflictSeverity = "advisory" | "blocking";
type FieldKind = "metric" | "dimension";

interface TargetField {
  name: string;
  displayName: string;
  kind: FieldKind;
  usedByCount: number;
}

export interface ConflictEvidence {
  fieldName: string;
  fieldLabel: string;
  code: string;
  message: string;
  affectedStreams: string[];
  severity: ConflictSeverity;
  /** The Connectors a currency binds to. The wire says `resolutions_by_module`;
   *  `module_name` is the legacy spelling of Connector (glossary.md), and a
   *  retired noun does not travel from the wire into what a person reads. */
  connectors: string[];
  /** Connector -> the source currency already bound, when one is. */
  resolvedByConnector: Record<string, string | null>;
}

interface MappingEvidence {
  fields: TargetField[];
  conflicts: ConflictEvidence[];
}

type MappingState =
  | { status: "loading"; projectId: string }
  | { status: "ready"; projectId: string; evidence: MappingEvidence }
  | { status: "empty"; projectId: string }
  | { status: "unconfigured"; projectId: string }
  | { status: "denied"; projectId: string }
  | { status: "error"; projectId: string }
  | { status: "schema_error"; projectId: string };

type FailureKind = "unconfigured" | "denied" | "error" | "schema_error";

export class MappingReadFailure extends Error {
  kind: FailureKind;

  constructor(kind: FailureKind) {
    super(kind);
    this.name = "MappingReadFailure";
    this.kind = kind;
  }
}

const UNCONFIGURED_CODES = new Set([
  "not_configured",
  "unconfigured",
  "capability_unavailable",
  "unsupported",
]);

const READ_TIMEOUT_MS = 15_000;
function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function nonEmptyString(value: unknown): value is string {
  return typeof value === "string" && value.trim().length > 0;
}

function readErrorCode(value: unknown): string {
  if (!isRecord(value) || typeof value.code !== "string") return "";
  return value.code.trim().toLowerCase();
}

function failureForResponse(status: number, body: unknown): MappingReadFailure {
  if (status === 401 || status === 403 || status === 404) {
    return new MappingReadFailure("denied");
  }
  const code = readErrorCode(body);
  if (UNCONFIGURED_CODES.has(code) || status === 501) {
    return new MappingReadFailure("unconfigured");
  }
  return new MappingReadFailure("error");
}

async function readJson(path: string, signal: AbortSignal): Promise<unknown> {
  let response: Response;
  try {
    response = await apiFetch(path, { method: "GET", cache: "no-store", signal });
  } catch {
    throw new MappingReadFailure("error");
  }

  let body: unknown = null;
  try {
    body = await response.json();
  } catch {
    if (response.ok) throw new MappingReadFailure("schema_error");
  }

  if (!response.ok) throw failureForResponse(response.status, body);
  return body;
}

function parseFields(value: unknown): TargetField[] {
  if (!Array.isArray(value)) throw new MappingReadFailure("schema_error");
  return value.map((item) => {
    if (
      !isRecord(item) ||
      !nonEmptyString(item.name) ||
      !nonEmptyString(item.display_name) ||
      (item.field_kind !== "metric" && item.field_kind !== "dimension") ||
      typeof item.used_by_count !== "number" ||
      !Number.isSafeInteger(item.used_by_count) ||
      item.used_by_count < 0
    ) {
      throw new MappingReadFailure("schema_error");
    }
    return {
      name: item.name,
      displayName: item.display_name,
      kind: item.field_kind,
      usedByCount: item.used_by_count,
    };
  });
}

export function parseConflicts(value: unknown): ConflictEvidence[] {
  if (!Array.isArray(value)) throw new MappingReadFailure("schema_error");
  return value.map((item) => {
    if (!isRecord(item) || !isRecord(item.field) || !isRecord(item.conflict)) {
      throw new MappingReadFailure("schema_error");
    }
    const affectedStreams = item.conflict.affected_streams;
    const rawSeverity = item.conflict.severity;
    if (
      !nonEmptyString(item.field.name) ||
      !nonEmptyString(item.conflict.code) ||
      !nonEmptyString(item.conflict.message) ||
      !Array.isArray(affectedStreams) ||
      !affectedStreams.every(nonEmptyString) ||
      (rawSeverity !== undefined &&
        rawSeverity !== "advisory" &&
        rawSeverity !== "refusal")
    ) {
      throw new MappingReadFailure("schema_error");
    }
    // The server already answers, per currency conflict, which Connectors report
    // this field and which of them already has a currency bound. The page read
    // it and dropped it, so the only screen that knows a conflict exists could
    // not name what would close it. The wire key keeps its legacy spelling; the
    // reading does not carry it any further.
    const wire = isRecord(item.resolutions_by_module) ? item.resolutions_by_module : {};
    const resolvedByConnector: Record<string, string | null> = {};
    for (const [connector, resolution] of Object.entries(wire)) {
      resolvedByConnector[connector] = isRecord(resolution)
        ? typeof resolution.resolved_source_currency === "string"
          ? resolution.resolved_source_currency
          : null
        : null;
    }
    return {
      fieldName: item.field.name,
      fieldLabel: nonEmptyString(item.field.display_name)
        ? item.field.display_name
        : item.field.name,
      code: item.conflict.code,
      message: item.conflict.message,
      affectedStreams: [...affectedStreams],
      severity: rawSeverity === "advisory" ? "advisory" : "blocking",
      connectors: Object.keys(resolvedByConnector),
      resolvedByConnector,
    };
  });
}

function selectFailure(
  results: PromiseSettledResult<unknown>[],
): MappingReadFailure | null {
  const failures = results
    .filter(
      (result): result is PromiseRejectedResult => result.status === "rejected",
    )
    .map((result) =>
      result.reason instanceof MappingReadFailure
        ? result.reason
        : new MappingReadFailure("error"),
    );
  const priority: Record<FailureKind, number> = {
    denied: 4,
    schema_error: 3,
    unconfigured: 2,
    error: 1,
  };
  return failures.sort((left, right) => priority[right.kind] - priority[left.kind])[0] ?? null;
}

async function loadMappingEvidence(
  projectId: string,
  signal: AbortSignal,
): Promise<MappingEvidence> {
  const scope = encodeURIComponent(projectId);
  const results = await Promise.allSettled([
    readJson(`/api/datamodel/fields?project_id=${scope}`, signal),
    readJson(`/api/mdm/conflicts?project_id=${scope}`, signal),
  ]);
  const failure = selectFailure(results);
  if (failure) throw failure;
  const [fieldResult, conflictResult] = results as [
    PromiseFulfilledResult<unknown>,
    PromiseFulfilledResult<unknown>,
  ];
  const fieldBody = fieldResult.value;
  const conflictBody = conflictResult.value;
  const fields = parseFields(fieldBody);
  const conflicts = parseConflicts(conflictBody);
  const fieldNames = new Set(fields.map((field) => field.name));
  if (conflicts.some((conflict) => !fieldNames.has(conflict.fieldName))) {
    throw new MappingReadFailure("schema_error");
  }
  return { fields, conflicts };
}

function initialState(projectId: string): MappingState {
  return projectId
    ? { status: "loading", projectId }
    : { status: "unconfigured", projectId };
}

function displayKind(kind: FieldKind): string {
  return kind === "metric" ? "Metric" : "Dimension";
}

/** A business number — the numeric face with lining tabular figures (the
 *  legacy `.number` helper, spelled in utilities). */
const NUMBER = "font-numeric [font-variant-numeric:lining-nums_tabular-nums]";

/** The state box, shared by every non-ready branch. Failure states tint the
 *  border toward the error tone (the sheet's `--denied/--error/--schema_error`
 *  modifiers, one rule for all three). */
const STATE_BOX =
  "grid min-h-[220px] place-content-center justify-items-start gap-2.25 rounded-large border border-divider-base bg-surface-light p-8";
const STATE_BOX_FAILED =
  "border-[color-mix(in_srgb,var(--color-error)_35%,var(--color-divider-base))]";
const STATE_TITLE = "m-0 font-display text-h2 font-semibold";
const STATE_DETAIL = "m-0 max-w-[62ch] leading-[1.55] text-text-secondary";

function StatePanel({
  state,
  onRetry,
}: {
  state: Exclude<MappingState, { status: "ready" }>;
  onRetry: () => void;
}) {
  if (state.status === "loading") {
    return (
      <section className={STATE_BOX} role="status" aria-label="Loading project mapping evidence">
        <h2 className={STATE_TITLE}>Loading project mapping</h2>
        <p className={STATE_DETAIL}>Checking the authorized concepts and conflict evidence for this project.</p>
      </section>
    );
  }

  const copy = {
    empty: {
      title: "No mapped concepts yet",
      detail: "This project returned no canonical concepts. Configure mapping through a governed Datastream workflow.",
    },
    unconfigured: {
      title: "Project mapping is not configured",
      detail: "Select an active project with mapping capability before loading governance evidence.",
    },
    denied: {
      title: "Project mapping unavailable",
      detail: "Choose a project you are authorized to view. No project evidence has been disclosed.",
    },
    error: {
      title: "Project mapping could not be loaded",
      detail: "The authorized read is temporarily unavailable. Retry without substituting local values.",
    },
    schema_error: {
      title: "Mapping response could not be verified",
      detail: "The server response did not match the required evidence contract. Retry after the service is repaired.",
    },
  } as const;
  const content = copy[state.status];
  const retryable = state.status === "error" || state.status === "schema_error";
  const failed = state.status === "denied" || retryable;
  return (
    <section
      className={failed ? `${STATE_BOX} ${STATE_BOX_FAILED}` : STATE_BOX}
      role={failed ? "alert" : "status"}
      data-testid={`mapping-${state.status}`}
    >
      <h2 className={STATE_TITLE}>{content.title}</h2>
      <p className={STATE_DETAIL}>{content.detail}</p>
      {retryable ? (
        <Button className="mt-1" type="button" variant="secondary" onClick={onRetry}>
          Retry
        </Button>
      ) : null}
    </section>
  );
}

interface ProjectMappingProps {
  projectId?: string;
}

export default function ProjectMapping({ projectId }: ProjectMappingProps) {
  const scope = projectId?.trim() ?? "";
  const [retryKey, setRetryKey] = useState(0);
  const [state, setState] = useState<MappingState>(() => initialState(scope));

  useEffect(() => {
    if (!scope) {
      setState({ status: "unconfigured", projectId: scope });
      return;
    }

    let cancelled = false;
    const controller = new AbortController();
    const timeoutId = window.setTimeout(() => controller.abort(), READ_TIMEOUT_MS);
    setState({ status: "loading", projectId: scope });
    void loadMappingEvidence(scope, controller.signal)
      .then((evidence) => {
        if (cancelled) return;
        setState(
          evidence.fields.length === 0
            ? { status: "empty", projectId: scope }
            : { status: "ready", projectId: scope, evidence },
        );
      })
      .catch((error: unknown) => {
        if (cancelled) return;
        const status =
          error instanceof MappingReadFailure ? error.kind : "error";
        setState({ status, projectId: scope });
      })
      .finally(() => window.clearTimeout(timeoutId));
    return () => {
      cancelled = true;
      controller.abort();
      window.clearTimeout(timeoutId);
    };
  }, [scope, retryKey]);

  const visibleState = state.projectId === scope ? state : initialState(scope);
  const retry = useCallback(() => setRetryKey((value) => value + 1), []);
  const [resolving, setResolving] = useState<ConflictEvidence | null>(null);

  const conflictByField = useMemo(() => {
    const grouped = new Map<string, ConflictEvidence[]>();
    if (visibleState.status !== "ready") return grouped;
    for (const conflict of visibleState.evidence.conflicts) {
      const current = grouped.get(conflict.fieldName) ?? [];
      current.push(conflict);
      grouped.set(conflict.fieldName, current);
    }
    return grouped;
  }, [visibleState]);

  const unavailableReason =
    "Unavailable until an exact governed command and object route are exposed by the server.";

  return (
    <div>
      <PageHeader
        className="mb-3"
        title="Project mapping"
        description="Authorized canonical concepts, consumer counts, and conflict evidence for the active project."
        actions={
          <Button type="button" disabled title={unavailableReason} aria-describedby="mapping-action-reason">
            + Add concept
          </Button>
        }
      />
      <p
        className="m-0 mb-4.5 rounded-lg border border-divider-base bg-surface-subtle px-3.25 py-2.5 text-caption text-text-secondary"
        id="mapping-action-reason"
        role="note"
      >
        Add and open-evidence actions are disabled: {unavailableReason}
      </p>

      {visibleState.status !== "ready" ? (
        <StatePanel state={visibleState} onRetry={retry} />
      ) : (
        <MappingReady
          evidence={visibleState.evidence}
          conflictByField={conflictByField}
          unavailableReason={unavailableReason}
          onResolve={setResolving}
        />
      )}

      {/* The resolution happens HERE, on the screen that says the conflict
          exists. Until this mount, `Resolve conflict` was a disabled button
          whose reason read "until an exact governed command and object route are
          exposed by the server" — and both routes had been served since story
          13.2. A refusal that names a deployment state instead of a gesture is
          the defect; this one named a state that was not even true. */}
      <MdmConflictResolutionDialog
        open={resolving !== null}
        projectId={scope}
        conflict={resolving}
        onClose={() => setResolving(null)}
        onResolved={retry}
      />
    </div>
  );
}

function MappingReady({
  evidence,
  conflictByField,
  unavailableReason,
  onResolve,
}: {
  evidence: MappingEvidence;
  conflictByField: Map<string, ConflictEvidence[]>;
  unavailableReason: string;
  onResolve: (conflict: ConflictEvidence) => void;
}) {
  const dimensions = evidence.fields.filter((field) => field.kind === "dimension").length;
  const metrics = evidence.fields.filter((field) => field.kind === "metric").length;
  return (
    <>
      {/* The 1px gaps over a divider-toned ground draw the cell hairlines in
          both the 4-column and the wrapped 2-column layout with one rule. */}
      <section
        aria-label="Verified mapping summary"
        className="mb-4.5 grid grid-cols-4 gap-px overflow-hidden rounded-large border border-divider-base bg-divider-base max-[1320px]:grid-cols-2"
      >
        <div className="bg-surface-light"><Metric label="Canonical concepts" value={evidence.fields.length} hint="Server-provided concepts" /></div>
        <div className="bg-surface-light"><Metric label="Dimensions" value={dimensions} hint="Verified field kind" /></div>
        <div className="bg-surface-light"><Metric label="Metrics" value={metrics} hint="Verified field kind" /></div>
        <div className="bg-surface-light"><Metric label="Conflicts" value={evidence.conflicts.length} hint="Joined conflict records" /></div>
      </section>

      <Panel flush>
        <PanelHeader
          title="Concept consumption"
          description="Only evidence available from the current authorized reads is shown."
        />
        <TableScroll label="Scrollable project mapping table">
          <Table className="min-w-[920px] table-fixed">
            <TableHeader>
              <TableRow>
                <TableHead className="w-[29%]">Canonical concept</TableHead>
                <TableHead className="w-[13%]">Kind</TableHead>
                <TableHead className="w-[16%]">Used by</TableHead>
                <TableHead className="w-[20%]">Conflicts</TableHead>
                <TableHead className="w-[22%]">Evidence</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {evidence.fields.map((field) => {
                const conflicts = conflictByField.get(field.name) ?? [];
                const blockingCount = conflicts.filter(
                  (conflict) => conflict.severity === "blocking",
                ).length;
                const advisoryCount = conflicts.length - blockingCount;
                const summary = [
                  blockingCount > 0 ? `${blockingCount} blocking` : null,
                  advisoryCount > 0 ? `${advisoryCount} advisory` : null,
                ]
                  .filter(Boolean)
                  .join(", ");
                return (
                  <TableRow key={field.name}>
                    <TableCell>
                      <div className="min-w-0">
                        <strong className="block">{field.displayName}</strong>
                        <small className="mt-0.5 block font-mono text-caption text-text-secondary">{field.name}</small>
                      </div>
                    </TableCell>
                    <TableCell><Badge outline>{displayKind(field.kind)}</Badge></TableCell>
                    <TableCell><strong className={NUMBER}>{field.usedByCount}</strong> Datastreams</TableCell>
                    <TableCell>
                      {conflicts.length > 0 ? (
                        <details
                          className="min-w-[210px]"
                          aria-label={`Conflict evidence for ${field.displayName}`}
                        >
                          <summary className="cursor-pointer marker:text-text-secondary">
                            <Status tone={blockingCount > 0 ? "error" : "warning"}>{summary}</Status>
                          </summary>
                          <div className="mt-2.5 grid gap-2 rounded-control border border-divider-base bg-background-light p-2.5">
                            {conflicts.map((conflict, index) => (
                              <section
                                className={`border-l-[3px] pl-2.25 ${
                                  conflict.severity === "advisory" ? "border-l-warning" : "border-l-error"
                                }`}
                                key={`${conflict.code}-${index}`}
                                aria-label={`${conflict.severity} conflict ${conflict.code}`}
                              >
                                <p className="mb-1 leading-[1.45]">
                                  <strong>{conflict.severity === "advisory" ? "Advisory" : "Blocking"}</strong>{" "}
                                  <code className="[overflow-wrap:anywhere]">{conflict.code}</code>
                                </p>
                                <p className="mb-1 leading-[1.45]">{conflict.message}</p>
                                <p className="mb-1 leading-[1.45]">Affected Datastreams</p>
                                {conflict.affectedStreams.length > 0 ? (
                                  <ul className="mb-1 pl-5">
                                    {conflict.affectedStreams.map((stream) => (
                                      <li key={stream}><code className="[overflow-wrap:anywhere]">{stream}</code></li>
                                    ))}
                                  </ul>
                                ) : (
                                  <p className="mb-1 leading-[1.45]">None identified by the server.</p>
                                )}
                                <Button
                                  type="button"
                                  variant="secondary"
                                  onClick={() => onResolve(conflict)}
                                >
                                  {RESOLVABLE_HERE.has(conflict.code) ? "Resolve" : "What resolves this"}
                                </Button>
                              </section>
                            ))}
                          </div>
                        </details>
                      ) : (
                        <span title="Verified by the conflict read">
                          <Status tone="success">None (verified)</Status>
                        </span>
                      )}
                    </TableCell>
                    <TableCell>
                      <Button
                        type="button"
                        variant="ghost"
                        disabled
                        title={unavailableReason}
                        aria-label={`Open evidence for ${field.displayName} unavailable: exact object route is not exposed`}
                      >
                        Open evidence
                      </Button>
                    </TableCell>
                  </TableRow>
                );
              })}
            </TableBody>
          </Table>
        </TableScroll>
        <div className="flex min-h-11 items-center justify-between gap-4.5 border-t border-divider-base bg-background-light px-4 text-caption text-text-secondary max-[1320px]:flex-col max-[1320px]:items-start max-[1320px]:py-2.5">
          <span><span className={NUMBER}>{evidence.fields.length}</span> verified concepts</span>
          <span>Provider, coverage, mapping-state, and update evidence are unavailable from this read contract.</span>
        </div>
      </Panel>
    </>
  );
}
