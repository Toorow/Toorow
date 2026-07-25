/**
 * Types pour la console Médiaplans (Story 22.7, FR38 / CAP-26).
 *
 * Calqués sur les réponses réelles de l'API (AI-54) :
 *   GET /api/projects/{project_id}/mediaplans → {"plans": [...]}
 *   GET /api/mediaplans/{plan_id}             → plan + active_version + lines
 *   GET /api/mediaplans/{plan_id}/versions    → {"versions": [...]}
 *   GET /api/mediaplans/{plan_id}/diff        → {added, removed, changed}
 *   GET /api/mediaplans/{plan_id}/mappings    → {by_line: {...}}
 *   GET /api/mediaplans/{plan_id}/unmapped-actuals → {actuals: [...], as_of: ...}
 *   GET /api/mediaplans/{plan_id}/pacing      → {lines, channels, plan}
 *   GET /api/mediaplans/{plan_id}/import-contracts → {contracts: [...]}
 *   POST /api/mediaplans/{plan_id}/import     → rapport d'import
 *
 * DIVERGENCE VOLONTAIRE (E3-F-4 / E2-F-2) :
 *   Les shapes du pacing ont DEUX représentations distinctes qui NE doivent PAS
 *   être « alignées » l'une sur l'autre à la légère :
 *     - la réponse de GET /pacing (API-shaped) — ce que ces types décrivent ;
 *     - l'enveloppe de la CARTE mediaplan_pacing (mart-raw, produite côté serveur
 *       par cards._resolve_mediaplan_pacing_card, ex. clé `pace` brute vs `pace_pct`
 *       en pourcentage arrondi, colonnes de table pré-formatées).
 *   Ces deux formes divergent PAR CONCEPTION (l'API sert le mart brut ; la carte le
 *   met en forme pour le widget/LLM). Toute tentative d'unifier l'un sur l'autre
 *   DOIT modifier les DEUX côtés en connaissance de cause — ne jamais « corriger »
 *   un seul côté pour les faire coïncider.
 */

// ---------------------------------------------------------------------------
// Plans
// ---------------------------------------------------------------------------

export interface MediaPlan {
  id: string;
  project_id: string;
  name: string;
  currency: string;
  created_by: string;
  created_at: string;
  updated_at: string;
  archived_at: string | null;
  /** Version active (présente si la plan a été publié au moins une fois). */
  active_version?: MediaPlanVersion | null;
}

// ---------------------------------------------------------------------------
// Versions
// ---------------------------------------------------------------------------

export type VersionStatus = "candidate" | "published" | "superseded";

export interface MediaPlanVersion {
  id: string;
  plan_id: string;
  version_number: number;
  status: VersionStatus;
  source_note: string | null;
  is_active: boolean;
  created_by: string;
  created_at: string;
  published_at: string | null;
}

// ---------------------------------------------------------------------------
// Lignes (scopées à une version)
// ---------------------------------------------------------------------------

export interface MediaPlanLine {
  id: string;
  version_id: string;
  line_key: string;
  label: string;
  support: string | null;
  channel: string | null;
  date_debut: string | null;
  date_fin: string | null;
  budget: number | null;
  mode_achat: string | null;
  /** Ligne sans source de données réelles (TV, OOH…) — AD-9, jamais 0 %. */
  is_plan_only: boolean;
}

// ---------------------------------------------------------------------------
// Détail plan (GET /api/mediaplans/{plan_id})
// ---------------------------------------------------------------------------

export interface MediaPlanDetail extends MediaPlan {
  active_version: MediaPlanVersion | null;
  lines: MediaPlanLine[];
}

// ---------------------------------------------------------------------------
// Diff
// ---------------------------------------------------------------------------

export interface LineDiff {
  line_key: string;
  field: string;
  from: unknown;
  to: unknown;
}

export interface DiffResult {
  added: MediaPlanLine[];
  removed: MediaPlanLine[];
  changed: LineDiff[];
}

// ---------------------------------------------------------------------------
// Mappings (Story 22.3)
// ---------------------------------------------------------------------------

export type MappingStatus = "active" | "orphaned";

export interface LineMapping {
  id: string;
  plan_id: string;
  line_key: string;
  connector: string;
  campaign_ref: string;
  split_weight: number;
  status: MappingStatus;
  created_at: string;
  updated_at: string;
}

/** Réponse de GET /api/mediaplans/{plan_id}/mappings */
export interface MappingsResponse {
  by_line: Record<string, LineMapping[]>;
}

/** Réponse de GET /api/mediaplans/{plan_id}/unmapped-actuals */
export interface UnmappedActual {
  connector: string;
  campaign_ref: string;
  spend: number;
  currency: string;
  date_debut: string | null;
  date_fin: string | null;
}

export interface UnmappedActualsResponse {
  actuals: UnmappedActual[];
  as_of: string | null;
  plan_id: string;
}

// ---------------------------------------------------------------------------
// Pacing (Story 22.4) — AD-9 : NULL honnête, jamais 0
// ---------------------------------------------------------------------------

export interface PacingLine {
  line_key: string;
  label: string;
  support: string | null;
  is_plan_only: boolean;
  budget: number | null;
  actual_spend: number | null;
  allocated_to_date: number | null;
  consumed_pct: number | null;   // NULL si pas de plan todate
  pace_pct: number | null;       // NULL si allocated_to_date = 0
  remaining: number | null;
  extrapolated_spend: number | null;
  plan_version_id: string;
  as_of: string | null;
}

export interface PacingChannel {
  support: string;
  budget: number | null;
  actual_spend: number | null;
  consumed_pct: number | null;
  pace_pct: number | null;
  plan_version_id: string;
}

export interface PacingPlan {
  budget: number | null;
  actual_spend: number | null;
  consumed_pct: number | null;
  pace_pct: number | null;
  remaining: number | null;
  plan_version_id: string;
  as_of: string | null;
}

export interface PacingResponse {
  plan_id: string;
  project_id: string;
  lines: PacingLine[];
  channels: PacingChannel[];
  plan: PacingPlan | null;
}

// ---------------------------------------------------------------------------
// Import contracts (Story 22.2)
// ---------------------------------------------------------------------------

export interface ImportContract {
  id: string;
  plan_id: string;
  sheet_name: string;
  contract: Record<string, unknown>;
  created_by: string;
  created_at: string;
  updated_at: string;
}

export interface ImportContractsResponse {
  contracts: ImportContract[];
}

// ---------------------------------------------------------------------------
// Rapport d'import (Story 22.2)
// ---------------------------------------------------------------------------

export interface ImportLineResult {
  line_key: string;
  status: "created" | "updated" | "unchanged" | "rejected";
  reason?: string;
}

export interface ImportReport {
  version: MediaPlanVersion;
  per_line: ImportLineResult[];
  rejected: ImportLineResult[];
  sums: {
    file_total: number;
    imported_total: number;
    rejected_total: number;
  };
}
