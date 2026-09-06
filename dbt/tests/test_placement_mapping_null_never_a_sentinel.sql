-- CRITERION [3] OF `docs/product-architecture/capabilities/placement-mapping.md`,
-- asserted on a built warehouse.
--
--   Incomplete if: "the two columns carry `0`, `""` or `Unmapped` where no plan
--   line matched".
--
-- A dbt singular test FAILS when it returns rows; zero rows is a pass.
--
-- FOUR THINGS ARE ASSERTED, and the fourth is the one that keeps the other three
-- from being vacuous:
--
--   1. neither column ever carries one of the three forbidden sentinels -- and
--      `''` is checked after TRIM, because a column of blanks is the same lie
--      spelled with whitespace;
--   2. `plan_line_label` is never carried without `plan_line_key`: a name with
--      no identity states a match nobody can look up, which is `Unmapped`
--      wearing better clothes;
--   3. an unmatched row states its whole spend rather than a share -- a
--      `split_weight` on a row no line matched would be a ventilation toward
--      nothing;
--   4. THE NULL BRANCH IS ACTUALLY EXERCISED. A test that only forbids values
--      passes perfectly over a model whose unmatched branch never emitted a row,
--      which is exactly the state this repair found the capability in: the
--      columns did not exist at all, so nothing could carry a sentinel in them.
--      So the test also fails when the model has rows and NONE of them is
--      unmatched. `an-instrument-must-not-measure-its-own-copy`: an assertion
--      that cannot fail is not an assertion.

{#- A PREMISE THAT IS NOT THERE IS NOT A DEFECT (AI-314). `mirror_sync` defers
    its BigQuery writes, so in production the plan mirror is absent, the model
    builds EMPTY and this test would report a red that accuses the projection of
    a fault whose cause is that there is nothing to project. It DECLINES, out
    loud, exactly as `test_plan_vs_actual_ventilation_sum.sql` does. -#}
{%- set mirror_missing = toorow_absent_sources('mirror', ['media_plans', 'plan_line_mappings']) -%}
{%- if mirror_missing | length > 0 %}
{{ toorow_absent_source_stub('mirror', mirror_missing, [['declined', 'string']]) }}
{%- else %}
WITH rows_with_a_sentinel AS (
    SELECT
        'sentinel_in_an_added_column' AS defect,
        project_id,
        plan_id,
        connector,
        campaign_ref,
        CAST(day AS {{ dbt.type_string() }}) AS day
    FROM {{ ref('placement_mapped_spend_daily') }}
    WHERE TRIM(COALESCE(plan_line_key, 'x'))   IN ('', '0', 'Unmapped', 'unmapped')
       OR TRIM(COALESCE(plan_line_label, 'x')) IN ('', '0', 'Unmapped', 'unmapped')
),

label_without_a_key AS (
    SELECT
        'label_without_a_plan_line' AS defect,
        project_id,
        plan_id,
        connector,
        campaign_ref,
        CAST(day AS {{ dbt.type_string() }}) AS day
    FROM {{ ref('placement_mapped_spend_daily') }}
    WHERE plan_line_key IS NULL
      AND plan_line_label IS NOT NULL
),

weight_on_an_unmatched_row AS (
    SELECT
        'unmatched_row_carries_a_weight' AS defect,
        project_id,
        plan_id,
        connector,
        campaign_ref,
        CAST(day AS {{ dbt.type_string() }}) AS day
    FROM {{ ref('placement_mapped_spend_daily') }}
    WHERE plan_line_key IS NULL
      AND split_weight IS NOT NULL
),

-- The vacuity guard. One row, and only when the model has rows at all and not
-- one of them is unmatched.
null_branch_never_exercised AS (
    SELECT
        'no_unmatched_row_to_measure' AS defect,
        CAST(NULL AS {{ dbt.type_string() }}) AS project_id,
        CAST(NULL AS {{ dbt.type_string() }}) AS plan_id,
        CAST(NULL AS {{ dbt.type_string() }}) AS connector,
        CAST(NULL AS {{ dbt.type_string() }}) AS campaign_ref,
        CAST(NULL AS {{ dbt.type_string() }}) AS day
    FROM (SELECT 1) AS _guard
    WHERE (SELECT COUNT(*) FROM {{ ref('placement_mapped_spend_daily') }}) > 0
      AND (SELECT COUNT(*) FROM {{ ref('placement_mapped_spend_daily') }}
           WHERE plan_line_key IS NULL) = 0
)

SELECT * FROM rows_with_a_sentinel
UNION ALL
SELECT * FROM label_without_a_key
UNION ALL
SELECT * FROM weight_on_an_unmatched_row
UNION ALL
SELECT * FROM null_branch_never_exercised
{%- endif -%}
