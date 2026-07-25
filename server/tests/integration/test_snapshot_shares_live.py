"""Integration tests (pg-gated) pour le partage tokenise (Story 13.5 volet b).

Skips unless TEST_POSTGRES_DSN is set. Applique les migrations 051 et 054 de
maniere idempotente avant les tests.

Seams verifies sur Postgres reel :
  - create_share + get_shared_snapshot (flux complet create -> read OK)
  - revoke_share -> get_shared_snapshot retourne None (revoke -> read = 404)
  - AD-5 cross-projet : create_share avec mauvais project_id retourne None
  - AD-5 cross-projet : revoke_share avec mauvais project_id retourne False
  - list_shares : liste les partages actifs et historiques
  - O1 strict : get_shared_snapshot ne reference pas les marts (verifie offline)
  - Token unique : deux create_share sur le meme snapshot -> deux tokens distincts

ASCII-only stdout (L-3).
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("TEST_POSTGRES_DSN"),
    reason="TEST_POSTGRES_DSN not set -- live Postgres test skipped",
)

_MIGRATION_051 = (
    Path(__file__).parent.parent.parent.parent
    / "infra" / "nango" / "migrations" / "051_render_snapshots.sql"
)
_MIGRATION_054 = (
    Path(__file__).parent.parent.parent.parent
    / "infra" / "nango" / "migrations" / "054_render_snapshot_shares.sql"
)


def _get_conn():
    import psycopg
    dsn = os.environ["TEST_POSTGRES_DSN"]
    return psycopg.connect(dsn)


def _apply_migrations(conn):
    for path in [_MIGRATION_051, _MIGRATION_054]:
        sql = path.read_text(encoding="utf-8")
        with conn.cursor() as cur:
            cur.execute(sql)
    conn.commit()


def _seed_project(conn, project_id: str):
    """Creer un projet de test (idempotent)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.projects (id, name, org_id, created_at)
            VALUES (%s, %s, 'org_test', NOW())
            ON CONFLICT (id) DO NOTHING
            """,
            (project_id, f"Test project {project_id}"),
        )
    conn.commit()


def _seed_snapshot(conn, project_id: str) -> str:
    """Inserer un snapshot de test, retourner son id."""
    snap_id = f"rsn_{uuid.uuid4().hex[:16]}"
    envelope = {"schema_version": "1", "meta": {"freshness": "stale"}, "data": {}}
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.render_snapshots
                (id, project_id, tool_name, envelope, created_at)
            VALUES (%s, %s, 'get_card', %s::jsonb, NOW())
            """,
            (snap_id, project_id, json.dumps(envelope)),
        )
    conn.commit()
    return snap_id


# ---------------------------------------------------------------------------
# Tests d'integration
# ---------------------------------------------------------------------------


def test_create_and_read_share():
    """Flux complet : create -> get (token valide, non revoque) -> snapshot fige."""
    from core.snapshot_shares import create_share, get_shared_snapshot

    project_id = f"proj_share_{uuid.uuid4().hex[:8]}"
    conn = _get_conn()
    try:
        _apply_migrations(conn)
        _seed_project(conn, project_id)
        snap_id = _seed_snapshot(conn, project_id)

        # Creer un partage
        result = create_share(snap_id, project_id, "user_live", conn)
        assert result is not None
        share_id, token = result
        assert share_id.startswith("rss_")
        assert len(token) >= 20

        # Lire le snapshot via le token public (O1)
        snap = get_shared_snapshot(token, conn)
        assert snap is not None
        assert snap["snapshot_id"] == snap_id
        assert snap["share_id"] == share_id
        assert snap["stale_since"] == "stale"
        assert isinstance(snap["envelope"], dict)

    finally:
        conn.close()


def test_revoke_then_read_is_none():
    """Apres revocation, get_shared_snapshot retourne None (lecture-apres-revocation = 404)."""
    from core.snapshot_shares import create_share, get_shared_snapshot, revoke_share

    project_id = f"proj_revoke_{uuid.uuid4().hex[:8]}"
    conn = _get_conn()
    try:
        _apply_migrations(conn)
        _seed_project(conn, project_id)
        snap_id = _seed_snapshot(conn, project_id)

        # Creer un partage
        result = create_share(snap_id, project_id, "user_live", conn)
        assert result is not None
        share_id, token = result

        # Verifier que le lien fonctionne
        snap_before = get_shared_snapshot(token, conn)
        assert snap_before is not None

        # Revoquer
        revoked = revoke_share(share_id, project_id, conn)
        assert revoked is True

        # Apres revocation -> None (-> 404 cote API)
        snap_after = get_shared_snapshot(token, conn)
        assert snap_after is None

    finally:
        conn.close()


def test_create_share_ad5_cross_project():
    """AD-5 : create_share avec project_id different retourne None."""
    from core.snapshot_shares import create_share

    project_a = f"proj_a_{uuid.uuid4().hex[:8]}"
    project_b = f"proj_b_{uuid.uuid4().hex[:8]}"
    conn = _get_conn()
    try:
        _apply_migrations(conn)
        _seed_project(conn, project_a)
        _seed_project(conn, project_b)
        snap_id = _seed_snapshot(conn, project_a)

        # Tenter de partager depuis project_b (cross-projet)
        result = create_share(snap_id, project_b, "attaquant", conn)
        assert result is None

    finally:
        conn.close()


def test_revoke_share_ad5_cross_project():
    """AD-5 : revoke_share avec project_id different retourne False."""
    from core.snapshot_shares import create_share, revoke_share

    project_a = f"proj_a_{uuid.uuid4().hex[:8]}"
    project_b = f"proj_b_{uuid.uuid4().hex[:8]}"
    conn = _get_conn()
    try:
        _apply_migrations(conn)
        _seed_project(conn, project_a)
        _seed_project(conn, project_b)
        snap_id = _seed_snapshot(conn, project_a)

        # Creer un partage depuis project_a
        result = create_share(snap_id, project_a, "user_a", conn)
        assert result is not None
        share_id, _ = result

        # Tenter de revoquer depuis project_b (cross-projet)
        revoked = revoke_share(share_id, project_b, conn)
        assert revoked is False

        # Le partage doit toujours etre actif
        from core.snapshot_shares import list_shares
        shares = list_shares(snap_id, project_a, conn, active_only=True)
        assert any(s["id"] == share_id for s in shares)

    finally:
        conn.close()


def test_two_shares_distinct_tokens():
    """Deux create_share sur le meme snapshot -> deux tokens distincts."""
    from core.snapshot_shares import create_share

    project_id = f"proj_multi_{uuid.uuid4().hex[:8]}"
    conn = _get_conn()
    try:
        _apply_migrations(conn)
        _seed_project(conn, project_id)
        snap_id = _seed_snapshot(conn, project_id)

        r1 = create_share(snap_id, project_id, "user_1", conn)
        r2 = create_share(snap_id, project_id, "user_1", conn)

        assert r1 is not None
        assert r2 is not None
        _, token1 = r1
        _, token2 = r2

        assert token1 != token2

    finally:
        conn.close()


def test_list_shares_active_and_history():
    """list_shares retourne tous les partages, active_only filtre les revoques."""
    from core.snapshot_shares import create_share, list_shares, revoke_share

    project_id = f"proj_list_{uuid.uuid4().hex[:8]}"
    conn = _get_conn()
    try:
        _apply_migrations(conn)
        _seed_project(conn, project_id)
        snap_id = _seed_snapshot(conn, project_id)

        r1 = create_share(snap_id, project_id, "user_1", conn)
        r2 = create_share(snap_id, project_id, "user_1", conn)
        assert r1 is not None
        assert r2 is not None
        share_id1, _ = r1

        # Revoquer le premier
        revoke_share(share_id1, project_id, conn)

        # Historique complet : 2 partages
        all_shares = list_shares(snap_id, project_id, conn, active_only=False)
        assert len(all_shares) >= 2

        # Seulement les actifs : 1
        active = list_shares(snap_id, project_id, conn, active_only=True)
        assert len(active) == 1
        assert active[0]["revoked_at"] is None

    finally:
        conn.close()
