/**
 * The governed relation a Semantic View reads, and the grain that identifies a row.
 *
 * NOT AN OPERATOR CHOICE. `fact_daily_kpi` is a constant of the product -- AD-12,
 * "marts only" -- declared in `core/cache_warehouse.py` and `core/cleanup_rules.py`
 * as `_MART_RELATION`. Every KPI Datastream of every Project lands there. Asking
 * someone to type a dataset name would be asking them to restate a decision the
 * product already made, and to get it wrong.
 *
 * It is declared here because the Semantic View intent has to carry it: the
 * compiler refuses a Concept "bound to dataset '', which this Semantic View does
 * not declare". Mirrored, not invented -- and a mirror is why the name sits in one
 * file rather than at each call site.
 */
export const MART_RELATION = "fact_daily_kpi";

/** What makes a row of that relation unique. */
export const MART_PRIMARY_KEY = ["project_id", "connector", "date"] as const;

export function martDatasetDeclaration() {
  return {
    name: MART_RELATION,
    source: MART_RELATION,
    primary_key: [...MART_PRIMARY_KEY],
    unique_keys: [] as string[][],
  };
}
