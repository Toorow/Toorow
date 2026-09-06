/**
 * The client half of the preconfigured metric catalogue.
 *
 * WHAT THE SERVER ALREADY DID, AND WHAT THIS FILE MUST NOT REDO. The offer is
 * resolved server-side against this Project's readable Concepts: an adoptable
 * preset arrives with the EXACT `create_concept` intent the governed change set
 * takes, operands already pinned to concept ids and version ids. Rebuilding that
 * intent in the browser would be a second answer to "what does roas mean", and
 * the browser is the one place that cannot check it.
 *
 * So this file carries and validates. The dialog posts `preset.intent` verbatim
 * through the same three routes it has always used — create, prepare, confirm —
 * and there is no adoption endpoint, deliberately: a fourth route would be a
 * fourth way to write meaning.
 */
import { apiGet } from "../lib/apiFetch";

/** Exclusive, and only `adoptable` carries an intent. */
export type PresetState = "adoptable" | "blocked" | "already_declared";

export interface MetricPreset {
  name: string;
  label: string;
  value_type: string;
  additivity_class: string;
  aggregation: { function?: string } | null;
  /** True when the formula reads OTHER Concepts. A fact of the tree, not a tag. */
  calculated: boolean;
  dependencies: string[];
  missing_dependencies: string[];
  state: PresetState;
  /** Present only when blocked: the move that unblocks it, already a sentence. */
  gesture: string | null;
  declared_scope: "project" | "platform" | null;
  /** Present only when adoptable: posted verbatim, never rebuilt here. */
  intent: {
    action: string;
    concept: Record<string, unknown>;
  } | null;
  source: string;
}

export interface MetricPresetCatalogue {
  source: string;
  presets: MetricPreset[];
  counts: Record<PresetState, number>;
  not_offered: string[];
}

const STATES: readonly PresetState[] = ["adoptable", "blocked", "already_declared"];

/** Refuses a row whose state is not one of the three, rather than rendering a
 *  preset nobody can act on. A shape the server never sends is a bug somewhere,
 *  and a screen that draws it hides which side. */
function isPreset(value: unknown): value is MetricPreset {
  if (!value || typeof value !== "object") return false;
  const row = value as Record<string, unknown>;
  return (
    typeof row.name === "string" &&
    row.name !== "" &&
    typeof row.state === "string" &&
    (STATES as readonly string[]).includes(row.state)
  );
}

export async function getMetricPresets(
  projectId: string,
  signal?: AbortSignal,
): Promise<MetricPresetCatalogue> {
  const payload = await apiGet<MetricPresetCatalogue>(
    `/api/projects/${encodeURIComponent(projectId)}/governance/semantic-model/metric-presets`,
    { signal },
  );
  const presets = Array.isArray(payload?.presets) ? payload.presets.filter(isPreset) : [];
  return {
    source: typeof payload?.source === "string" ? payload.source : "",
    presets,
    counts: payload?.counts ?? { adoptable: 0, blocked: 0, already_declared: 0 },
    not_offered: Array.isArray(payload?.not_offered) ? payload.not_offered : [],
  };
}
