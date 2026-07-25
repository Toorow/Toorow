-- Generic dbt test: active_users >= conversions (AC6, AD-4)
-- Fails if any row violates the constraint.
{% test active_users_gte_conversions(model) %}
SELECT *
FROM {{ model }}
WHERE active_users < conversions
{% endtest %}
