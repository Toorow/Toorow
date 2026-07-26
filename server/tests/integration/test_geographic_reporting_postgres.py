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
                (id, name, slug, status, currency, timezone, created_by, org_id)
            VALUES (%s, 'Geo test', %s, 'active', 'EUR', 'Europe/Paris', 'pytest',
                'org_test_fixture')
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


def _insert_local_markets(conn, project_id: str, codes: list[str], markets: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.project_preferences
                (project_id, geographic_mode, local_market_country_codes, local_markets)
            VALUES (%s, 'local_markets', %s, %s::jsonb)
            """,
            (project_id, codes, markets),
        )


def test_migration_102_accepts_a_multi_country_market(live_postgres):
    """Story 37.8: any composition satisfying the invariants is accepted."""

    project_id = _insert_project(live_postgres)
    _insert_local_markets(
        live_postgres,
        project_id,
        ["AT", "CH", "DE"],
        '[{"id": "dach", "label": "DACH", "country_codes": ["DE", "AT", "CH"]}]',
    )

    with live_postgres.cursor() as cur:
        cur.execute(
            "SELECT jsonb_array_length(local_markets) FROM app.project_preferences "
            "WHERE project_id = %s",
            (project_id,),
        )
        assert cur.fetchone()[0] == 1


def test_migration_102_rejects_overlapping_markets(live_postgres):
    project_id = _insert_project(live_postgres)

    with pytest.raises(psycopg.errors.CheckViolation):
        _insert_local_markets(
            live_postgres,
            project_id,
            ["FR", "MC"],
            '[{"id": "fr", "label": "France", "country_codes": ["FR", "MC"]},'
            ' {"id": "south", "label": "South", "country_codes": ["MC"]}]',
        )


def test_migration_102_rejects_duplicate_market_ids_and_empty_members(live_postgres):
    project_id = _insert_project(live_postgres)

    with pytest.raises(psycopg.errors.CheckViolation):
        _insert_local_markets(
            live_postgres,
            project_id,
            ["FR", "DE"],
            '[{"id": "fr", "label": "France", "country_codes": ["FR"]},'
            ' {"id": "FR", "label": "Hexagone", "country_codes": ["DE"]}]',
        )

    project_id = _insert_project(live_postgres)
    with pytest.raises(psycopg.errors.CheckViolation):
        _insert_local_markets(
            live_postgres,
            project_id,
            ["FR"],
            '[{"id": "fr", "label": "France", "country_codes": []}]',
        )


def test_migration_102_requires_markets_in_local_mode(live_postgres):
    project_id = _insert_project(live_postgres)

    with pytest.raises(psycopg.errors.CheckViolation):
        _insert_local_markets(live_postgres, project_id, ["FR"], "[]")
