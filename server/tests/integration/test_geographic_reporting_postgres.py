"""Live-Postgres guardrails for migration 057 (Story 37.1)."""

from __future__ import annotations

import uuid

import psycopg
import pytest


def _insert_project(conn) -> str:
    project_id = f"proj_geo_{uuid.uuid4().hex[:12]}"
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.projects
                (id, name, slug, status, currency, timezone, created_by)
            VALUES (%s, 'Geo test', %s, 'active', 'EUR', 'Europe/Paris', 'pytest')
            """,
            (project_id, f"geo-{uuid.uuid4().hex[:12]}"),
        )
    return project_id


def test_migration_057_gives_legacy_rows_global_defaults(live_postgres):
    project_id = _insert_project(live_postgres)
    with live_postgres.cursor() as cur:
        cur.execute(
            "INSERT INTO app.project_preferences (project_id) VALUES (%s)",
            (project_id,),
        )
        cur.execute(
            """
            SELECT geographic_mode, local_market_country_codes
            FROM app.project_preferences
            WHERE project_id = %s
            """,
            (project_id,),
        )
        row = cur.fetchone()

    assert row == ("global", [])


def test_migration_057_rejects_empty_local_markets(live_postgres):
    project_id = _insert_project(live_postgres)

    with pytest.raises(psycopg.errors.CheckViolation):
        with live_postgres.cursor() as cur:
            cur.execute(
                """
                INSERT INTO app.project_preferences
                    (project_id, geographic_mode, local_market_country_codes)
                VALUES (%s, 'local_markets', ARRAY[]::TEXT[])
                """,
                (project_id,),
            )
