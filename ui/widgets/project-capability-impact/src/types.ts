/**
 * The bounded payload contract, mirrored from the server serializer.
 *
 * These types describe exactly what `core.project_capabilities_mcp` puts in
 * `structuredContent` — nothing wider. The app renders this payload and derives
 * no coverage of its own: a second calculation here would be a second authority,
 * and the whole point of Story 48.1 is that there is one.
 */

/** The five states that partition the applicable denominator, plus the exclusion. */
export type CoverageState =
  | "complete"
  | "partial"
  | "unavailable"
  | "excluded"
  | "pending"
  | "not_applicable";

export interface Coverage {
  applicable: number;
  complete: number;
  partial: number;
  unavailable: number;
  excluded: number;
  pending: number;
  not_applicable?: number;
  /** Server-composed. `Not applicable` when the denominator is zero — never 100%. */
  label: string;
  percentage: number | null;
}

export interface MatrixRow {
  capability_key: string;
  datastream_id: string;
  applicability: string;
  coverage_state: CoverageState;
  reason?: string;
  changes_grain?: boolean;
  backfill_required?: boolean;
  downstream_consumers?: number;
  blocker_count?: number;
  exception_count?: number;
  proposal_id?: string | null;
  content_hash?: string;
}

export interface Blocker {
  code: string;
  message: string;
  required?: boolean;
  capability_key?: string;
  datastream_id?: string;
}

export interface ExceptionRef {
  id?: string;
  capability_key?: string;
  datastream_id?: string | null;
  kind?: string;
  severity?: string;
  reason_code?: string;
  reason?: string;
  owner_kind?: string;
}

/** A semantic owner reference. The Console builds the href; the app never does. */
export interface OwnerReference {
  surface: string;
  workspace: string | null;
  section: string | null;
  global_surface: string | null;
  global_section: string | null;
  object_type: string | null;
  object_id: string | null;
  tab: string | null;
  action: string | null;
  version_id: string | null;
  evidence_id: string | null;
}

export interface ConsoleReference {
  kind: string;
  requires_authenticated_session: boolean;
  project_id: string;
  change_set_id?: string | null;
  owner_reference: OwnerReference;
}

export interface CapabilityImpactPayload {
  schema: string;
  project_id: string;
  change_set_id?: string;
  state?: string;
  capability_key?: string;
  coverage?: Coverage;
  matrix?: MatrixRow[];
  matrix_rows_withheld?: number;
  blockers?: Blocker[];
  exceptions?: ExceptionRef[];
  referenced_versions?: Array<{
    owner_kind: string;
    object_type: string;
    object_id: string;
    version_id: string;
    capability_key?: string | null;
    owner_reference?: OwnerReference;
  }>;
  review_reference?: string;
  authorizing?: boolean;
  confirmation_available?: boolean;
  console?: ConsoleReference;
}
