"""Integration tests (pg-gated) pour la galerie des rendus (Story 13.5 volet a).

Skips unless TEST_POSTGRES_DSN is set. Applique la migration 051 de maniere
idempotente avant les tests (meme pattern que test_async_reports.py).

Seams verifies sur Postgres reel :
  - Ecriture d'un snapshot lors d'un rendu (persist_render_envelope)
  - Lecture de la galerie (list_render_snapshots)
  - Lecture d'un snapshot complet (get_render_snapshot)
  - Denégation cross-projet AD-5 : snapshot projet A invisible depuis projet B
  - Retention : les snapshots au-dela de la borne sont purges au write suivant
  - Best-effort : une erreur de connexion ne casse pas get_card/get_report
    (simulée via mock dans test_best_effort_does_not_raise, pas besoin de Postgres)

ASCII-only stdout (L-3).
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_POSTGRES_DSN"),
    reason="TEST_POSTGRES_DSN not set -- live Postgres test skipped",
)

_MIGRATION_PATH = (
    Path(__file__).parent.parent.parent.parent
    / "infra"
    / "nango"
    / "migrations"
    / "051_render_snapshots.sql"
)


def _apply_migration(conn):
    """Appliquer migration 051 de maniere idempotente."""
    sql = _MIGRATION_PATH.read_text(encoding="utf-8")
    with conn.cursor() as cur:
        cur.execute(sql)
    conn.commit()


def _seed_project(conn, project_id: str):
    """Créer un projet de test (idempotent)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.projects (id, name, slug, created_by, org_id)
            VALUES (%s, %s, %s, 'test', 'org_test_fixture')
            ON CONFLICT DO NOTHING
            """,
            (project_id, f"Test {project_id}", project_id),
        )
    conn.commit()


def _cleanup(conn, *project_ids):
    """Supprimer les données de test."""
    for pid in project_ids:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM app.render_snapshots WHERE project_id = %s", (pid,))
            cur.execute("DELETE FROM app.projects WHERE id = %s", (pid,))
        conn.commit()


@pytest.fixture
def pg(live_postgres):
    """Fixture Postgres avec migration 051 appliquée."""
    _apply_migration(live_postgres)
    return live_postgres


# ---------------------------------------------------------------------------
# Test : persist + list + get
# ---------------------------------------------------------------------------


def test_persist_and_list(pg):
    """persist_render_envelope -> list_render_snapshots retourne le snapshot."""
    from core.snapshots import list_render_snapshots, persist_render_envelope

    pid = f"proj_snap_{uuid.uuid4().hex[:8]}"
    _seed_project(pg, pid)

    try:
        envelope = {
            "schema_version": "1",
            "meta": {"freshness": "live", "provenance": "warehouse", "alerts": []},
            "data": {"rows": [1, 2, 3]},
        }

        snap_id = persist_render_envelope(
            project_id=pid,
            tool_name="get_card",
            envelope=envelope,
            widget_uri="mcp://widget/kpi",
            summary="résumé live",
            tool_args={"template": "kpi"},
            identity="user_integ",
            trace_id="trace_live",
            conn=pg,
        )

        assert snap_id is not None
        assert snap_id.startswith("rsn_")

        rows = list_render_snapshots(pid, pg)
        assert len(rows) == 1
        r = rows[0]
        assert r["id"] == snap_id
        assert r["tool_name"] == "get_card"
        assert r["widget_uri"] == "mcp://widget/kpi"
        # meta de l'envelope preservee (AD-9)
        meta = r.get("meta") or {}
        assert meta.get("freshness") == "live"

    finally:
        _cleanup(pg, pid)


def test_get_render_snapshot_full_envelope(pg):
    """get_render_snapshot retourne l'envelope complete (AD-9)."""
    from core.snapshots import get_render_snapshot, persist_render_envelope

    pid = f"proj_snap_{uuid.uuid4().hex[:8]}"
    _seed_project(pg, pid)

    try:
        envelope = {
            "schema_version": "1",
            "meta": {"freshness": "stale", "provenance": "cache"},
            "data": {"key": "val"},
        }

        snap_id = persist_render_envelope(
            project_id=pid,
            tool_name="get_report",
            envelope=envelope,
            widget_uri=None,
            summary="",
            tool_args={"report_id": "shopify/revenue"},
            identity=None,
            trace_id=None,
            conn=pg,
        )

        result = get_render_snapshot(snap_id, pid, pg)

        assert result is not None
        assert result["id"] == snap_id
        # AD-9 : fraicheur et provenance preservees intactes
        assert result["envelope"]["meta"]["freshness"] == "stale"
        assert result["envelope"]["meta"]["provenance"] == "cache"
        assert result["envelope"]["data"] == {"key": "val"}

    finally:
        _cleanup(pg, pid)


# ---------------------------------------------------------------------------
# Test AD-5 : denégation cross-projet
# ---------------------------------------------------------------------------


def test_ad5_cross_project_denied(pg):
    """AD-5 : snapshot projet A est invisible depuis get_render_snapshot avec projet B."""
    from core.snapshots import get_render_snapshot, list_render_snapshots, persist_render_envelope

    pid_a = f"proj_a_{uuid.uuid4().hex[:8]}"
    pid_b = f"proj_b_{uuid.uuid4().hex[:8]}"
    _seed_project(pg, pid_a)
    _seed_project(pg, pid_b)

    try:
        snap_id = persist_render_envelope(
            project_id=pid_a,
            tool_name="get_card",
            envelope={"schema_version": "1", "meta": {}, "data": {}},
            widget_uri=None,
            summary="",
            tool_args=None,
            identity="user_a",
            trace_id=None,
            conn=pg,
        )

        # Depuis le projet A : visible
        result_a = get_render_snapshot(snap_id, pid_a, pg)
        assert result_a is not None

        # Depuis le projet B : invisible (AD-5 -- 404 non-disclosant)
        result_b = get_render_snapshot(snap_id, pid_b, pg)
        assert result_b is None

        # list depuis proj_b : vide
        rows_b = list_render_snapshots(pid_b, pg)
        assert all(r["id"] != snap_id for r in rows_b)

    finally:
        _cleanup(pg, pid_a, pid_b)


# ---------------------------------------------------------------------------
# Test retention : la purge supprime les snapshots trop anciens
# ---------------------------------------------------------------------------


def test_retention_purges_old_snapshots(pg):
    """La purge inline retire les snapshots plus anciens que la borne."""
    from core.snapshots import list_render_snapshots, persist_render_envelope

    pid = f"proj_ret_{uuid.uuid4().hex[:8]}"
    _seed_project(pg, pid)

    try:
        # Insérer manuellement un snapshot ancien (created_at dans le passé lointain)
        old_id = f"rsn_old_{uuid.uuid4().hex[:8]}"
        old_dt = datetime.now(tz=timezone.utc) - timedelta(days=60)
        with pg.cursor() as cur:
            cur.execute(
                """
                INSERT INTO app.render_snapshots
                    (id, project_id, tool_name, envelope, created_at)
                VALUES (%s, %s, 'get_card', '{"schema_version":"1","meta":{},"data":{}}'::jsonb, %s)
                """,
                (old_id, pid, old_dt),
            )
        pg.commit()

        # Insérer un snapshot récent (déclenche la purge avec retention 30 jours)
        with patch_retention(30):
            new_id = persist_render_envelope(
                project_id=pid,
                tool_name="get_card",
                envelope={"schema_version": "1", "meta": {}, "data": {}},
                widget_uri=None,
                summary="",
                tool_args=None,
                identity=None,
                trace_id=None,
                conn=pg,
            )

        assert new_id is not None, "persist_render_envelope doit retourner un snap_id"

        rows = list_render_snapshots(pid, pg, limit=200)
        ids = [r["id"] for r in rows]

        # Le snapshot récent est conservé
        assert new_id in ids
        # L'ancien snapshot (60 jours) est purgé
        assert old_id not in ids

    finally:
        _cleanup(pg, pid)


class patch_retention:
    """Context manager pour surcharger RENDER_SNAPSHOT_RETENTION_DAYS."""

    def __init__(self, days: int):
        self._days = str(days)
        self._orig = os.environ.get("RENDER_SNAPSHOT_RETENTION_DAYS")

    def __enter__(self):
        os.environ["RENDER_SNAPSHOT_RETENTION_DAYS"] = self._days
        return self

    def __exit__(self, *_):
        if self._orig is None:
            os.environ.pop("RENDER_SNAPSHOT_RETENTION_DAYS", None)
        else:
            os.environ["RENDER_SNAPSHOT_RETENTION_DAYS"] = self._orig


# ---------------------------------------------------------------------------
# Test best-effort (offline -- pas besoin de Postgres)
# ---------------------------------------------------------------------------


def test_best_effort_does_not_raise():
    """Une erreur de persistance ne propage jamais d'exception (best-effort)."""
    from unittest.mock import MagicMock

    from core.snapshots import persist_render_envelope

    broken_conn = MagicMock()
    broken_conn.cursor.return_value.__enter__ = MagicMock(side_effect=RuntimeError("db down"))
    broken_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

    # Ne doit pas lever d'exception
    result = persist_render_envelope(
        project_id="proj_x",
        tool_name="get_card",
        envelope={"schema_version": "1", "meta": {}, "data": {}},
        widget_uri=None,
        summary="",
        tool_args=None,
        identity=None,
        trace_id=None,
        conn=broken_conn,
    )
    assert result is None
