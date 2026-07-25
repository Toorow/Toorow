# dbt Marts

Canonical fact tables consumed by the connector MCP reporting layer.

## Grain Uniqueness (mandatory — AI-05)

Every mart model MUST declare a `unique` test on its full grain key in
`schema.yml`. This is a **blocking** requirement enforced at code review.

**Rationale:** a missing grain test means double-counted KPIs reach the widget.
Caught in review-1-4 F-05; tracked and enforced from Story 3.4 onward (AI-05).

### Template — schema.yml

```yaml
models:
  - name: fact_my_model
    tests:
      - unique:
          name: fact_my_model_grain_unique
          arguments:
            column_name: "col_a || '|' || col_b || '|' || col_c"
```

List every column that together identifies one logical row. The concatenated
key with `|` separators is the canonical form (see `fact_daily_kpi_grain_unique`
in `schema.yml` as the reference implementation).

### Reference: fact_daily_kpi_grain_unique

```yaml
- unique:
    name: fact_daily_kpi_grain_unique
    arguments:
      column_name: >
        project_id || '|' || date || '|' || connector || '|' || metric
        || '|' || breakdown_dimension || '|' || breakdown_value
```

## Mart models

| Model | Grain | Notes |
|---|---|---|
| `fact_daily_kpi` | project_id x date x connector x metric x breakdown | AD-4 additive metrics only. Composite `country>device` split emitted for GA4 (Story 8.11) and GSC (AI-51); meta-ads skipped (no country/device dims) |
| `semantic_ctr` | project_id x date x connector x breakdown | VIEW: clicks/impressions ratio |
| `semantic_cpa` | project_id x date x connector x breakdown | VIEW: cost/conversions ratio |
| `semantic_roas` | project_id x date x connector x breakdown | VIEW: revenue/cost ratio |
| `semantic_avg_position` | project_id x date x connector x breakdown | VIEW: impression-weighted GSC average_position (page/country/device) |
| `semantic_avg_position_composite` | project_id x date x connector x country>device | VIEW: impression-weighted GSC average_position at the country>device composite grain (AI-52); NULL when impressions=0 |
| `cross_source_conversions` | project_id x date | Cross-source dedup with priority |

### Composite sub-dimension splits (Story 8.11 / AI-51 / AI-52)

`country>device` composites are stored as ordinary long-format `fact_daily_kpi` rows
using a `'>'` path separator (`breakdown_dimension='country>device'`,
`breakdown_value='fra>mobile'`). They are ADDITIVE-only (clicks, impressions,
sessions, ...). The composite is one more parallel series that independently totals
the day, so grain-uniqueness holds and marts picking a canonical day total via
`MIN(breakdown_dimension)` never select it. A module only emits the composite when it
lands BOTH `country` and `device` on the same source row (GA4, GSC do; meta-ads does
NOT and is skipped). Non-additive `average_position` is NEVER composited additively;
its correct composite lives in `semantic_avg_position_composite` (impression-weighted,
AI-52). Reconciliation tests (`test_composite_reconciliation.sql` for GA4,
`test_composite_reconciliation_gsc.sql` for GSC) prove composite totals == single-dim
totals for every additive metric; `test_composite_additive_only.sql` forbids
non-additive composites in the fact.

## Full-grain relation vs the canonical projection — two relations, one truth (Story 12.4)

`candidate_full_grain` is the governed FULL-GRAIN relation: it preserves EVERY
selected dimension at the declared JOINT grain, each dimension kept as its OWN
typed column (never a long-format `breakdown_dimension`/`breakdown_value` pair),
every source measure kept alongside the grain, a deterministic `grain_key` (AI-05
uniqueness, `candidate_full_grain_grain_unique`), and provenance columns
(`execution_id`, `pull_id`, `mapping_version_id`, `plan_version_id`,
`loaded_at`). It is the ANALYSIS-side companion to `fact_daily_kpi`'s canonical
projection — **two relations, one truth**.

**Provenance backfill in the 12.5 publication-triggered build.** Three of the five
provenance columns — `execution_id`, `mapping_version_id`, `plan_version_id` — are
honest `NULL` in the pure-dbt v1 (staging carries no execution linkage; AD-9: an
explicit, present, queryable NULL, never a fabricated value). Story 12.5's
publication path triggers `dbt build --select candidate_full_grain --vars
'{"execution_id":"dse_...","mapping_version_id":"dmap_...","plan_version_id":"dsp_..."}'`
so the model reads those `var()`s and the three columns carry REAL provenance. This
is NOT a recomputation — the SAME rows are re-emitted with the provenance columns
filled in; the content hash covers grain + measures ONLY, so the backfill never
changes a published total. Proven by `test_candidate_provenance_complete.sql`
(zero NULL provenance rows after a publication-context build; vacuously passes in
CI without the vars).

It is a **DISTINCT artifact** from `fact_daily_kpi`: materializing it writes
NOTHING to `fact_daily_kpi`, mutates no existing mart total, and does not touch
`fact_daily_kpi_grain_unique`. Proven by
`test_full_grain_isolated_from_fact_kpi.sql` (the full grain's own SUM per
`(project, date)` reconciles to `fact_daily_kpi`'s canonical `country` total).

**Safe projection accept/reject rules** (compiled by
`server/core/datastream_projection.py`, NOT dbt — dbt is the mart writer, AD-8):

- **Accept a measure** into `fact_daily_kpi` ONLY iff the 12.3 mapping declares it
  `aggregation='sum'` AND `non_additive=false` (resolved through
  `app.mdm_canonical_fields`).
- **Accept exactly ONE governed dimension projection** into the canonical
  `breakdown_dimension`/`breakdown_value` slot; it must sort so
  `MIN(breakdown_dimension)` NEVER selects it (a parallel series, not a new
  canonical slot).
- **Reject** (fail closed, blocks publication): `non_additive_measure_projected`
  (routed to the `semantic_*` VIEW pattern, NEVER summed),
  `ungoverned_dimension_projection` (an arbitrary/second dimension squeezed into
  the canonical slot), `mixed_grain_projection` (two source grains folded into
  one series), `cardinality_over_limit` / `scan_over_limit` (over the governed
  project-scoped threshold — names the expensive field), `mapping_not_executable`
  (12.3 blocking bindings), `mapping_drift` (schema/capability fingerprint
  changed).

**Non-additive routing:** a rejected non-additive measure (ratios ctr/cpa/roas,
`average_position`) is NOT dropped — it stays in the full-grain relation and is
served by the `semantic_*` views (`route_to_semantic` repair). Guarded by
`test_projection_additive_only.sql`. The canonical-partition selection is proven
byte-identical by `test_projection_min_breakdown_stable.sql` (GA4 MIN stays
`country`) and by the `compute_rollup` invariance cases in
`server/tests/core/test_rollup.py`.

## Aggregation Rules and Non-Additive Metrics (AC8)

The `dim_metric` seed declares `aggregation_rule` for all metrics:

- `sum` — standard additive metrics (sessions, active_users, conversions, cost, impressions, clicks, revenue)
- `ratio` — ratio metrics (ROAS, CTR, CPA): declared in `dim_metric.csv` with `additive=false`; computed at VIEW time by the semantic layer, never stored in `fact_daily_kpi` (AD-4)
- `impression_weighted_average` — used by GSC's `average_position` (pre-declared in `dim_metric.csv` for Story 6.2, no runtime enforcement yet)

**Aggregation rule enforcement** is applied exclusively by the semantic layer (this directory). The `fact_kpi_metric_additive_only` generic test on `fact_daily_kpi` prevents ratio metrics from being stored, acting as the hard guard (AD-4).

The `average_position` entry in `dim_metric.csv` pre-declares the `impression_weighted_average` rule for Story 6.2 (GSC integration). No runtime enforcement is added in Story 4.1 — this is a schema-only addition to avoid a breaking schema change when GSC arrives in Epic 6.
