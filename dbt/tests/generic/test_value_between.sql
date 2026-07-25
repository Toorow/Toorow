-- Generic range test (replaces dbt_utils.accepted_range -- no package dep).
-- Usage in schema.yml:
--   tests:
--     - value_between:
--         arguments: { min_value: 1.0, max_value: 100.0 }
{% test value_between(model, column_name, min_value, max_value) %}
SELECT *
FROM {{ model }}
WHERE {{ column_name }} IS NOT NULL
  AND ({{ column_name }} < {{ min_value }} OR {{ column_name }} > {{ max_value }})
{% endtest %}
