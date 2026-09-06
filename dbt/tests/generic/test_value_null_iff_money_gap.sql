-- Generic dbt test: a money value is NULL if and ONLY if a gap code explains it.
--
-- Story 48.3 made `fact_daily_kpi.value` nullable on purpose. `fx_convert_at_read`
-- propagates NULL instead of converting at parity when no rate resolves, and the
-- row is KEPT -- with `native_value`, `native_currency` and a typed
-- `money_gap_code` -- because deleting it would erase the evidence that something
-- needs repairing.
--
-- The column therefore cannot carry a bare `not_null`. But "nullable" is not
-- "unconstrained", and this test is what replaces it. Two failure shapes, both
-- returned as rows (zero rows = pass):
--
--   * `value IS NULL` with NO `money_gap_code` -- a silent hole. A reader sees a
--     missing number and has no way to learn why. This is the shape 48.3 exists
--     to forbid, and the one a bare `not_null` used to catch by accident.
--   * `value IS NOT NULL` WITH a `money_gap_code` -- a contradiction. The row
--     claims both "here is your converted figure" and "I could not convert it".
--     One of the two is a lie and the reader cannot tell which.
--
-- Non-money rows (counts, ratios, anything the money seam never touched) carry
-- neither a NULL value nor a gap code and satisfy both branches trivially.
--
-- `key_columns` NAMES THE ROW, and it is a parameter because the contract is not
-- `fact_daily_kpi`'s alone. `cross_source_revenue` carries the same pair on its
-- day total and has neither `metric` nor `breakdown_dimension` -- it is one row
-- per (project, date) -- so a hard-coded column list would have forced a SECOND
-- test saying the same thing, and two tests for one invariant drift. The default
-- is the fact's own grain, so its call site is unchanged.
{% test value_null_iff_money_gap(model, column_name, gap_column='money_gap_code',
                                 key_columns=['project_id', 'date', 'connector',
                                              'metric', 'breakdown_dimension']) %}
SELECT
    {%- for key_column in key_columns %}
    {{ key_column }},
    {%- endfor %}
    {{ column_name }} AS value,
    {{ gap_column }}  AS money_gap_code,
    CASE
        WHEN {{ column_name }} IS NULL THEN 'null_value_without_gap_code'
        ELSE 'converted_value_still_carries_gap_code'
    END AS failure_reason
FROM {{ model }}
WHERE ({{ column_name }} IS NULL     AND {{ gap_column }} IS NULL)
   OR ({{ column_name }} IS NOT NULL AND {{ gap_column }} IS NOT NULL)
{% endtest %}
