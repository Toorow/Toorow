-- Generic dbt test: no NULL pull_id in fact_daily_kpi (AC6, AD-7)
{% test no_null_pull_id_in_mart(model) %}
SELECT *
FROM {{ model }}
WHERE pull_id IS NULL
{% endtest %}
