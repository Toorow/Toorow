-- Generic dbt test: sessions >= active_users (AC6, AD-4)
-- Fails if any row violates the constraint.
{% test sessions_gte_active_users(model) %}
SELECT *
FROM {{ model }}
WHERE sessions < active_users
{% endtest %}
