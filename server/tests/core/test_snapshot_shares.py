"""Tests unitaires pour server/core/snapshot_shares.py (Story 13.5 volet b).

Seam tests couverts :
  - create_share    : INSERT + retour (share_id, token) ; snapshot introuvable -> None
  - revoke_share    : UPDATE revoked_at ; introuvable/deja revoque -> False
  - list_shares     : liste avec/sans filtre active_only
  - get_shared_snapshot : lecture par token ; revoque -> None ; token inconnu -> None
  - AD-5 cross-projet : create_share / revoke_share bloquent sur project_id
  - O1 strict : get_shared_snapshot ne touche PAS les marts (verifie via mocks)
  - Rate-limit : _check_rendus_rate_limit (N+1 -> False)
  - REST API shares :
      POST /api/rendus/snapshots/{id}/share -> 201 / 404
      GET  /api/rendus/snapshots/{id}/shares -> 200
      DELETE /api/rendus/shares/{share_id} -> 204 / 404
      GET /api/rendus/shared/{token} -> 200 HTML ; 404 si revoque ; 429 rate-limit

Tous les tests sont offline (mock psycopg). La section pg-gatee est dans
test_snapshot_shares_live.py (marquee TEST_POSTGRES_DSN).

ASCII-only stdout (L-3).
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

os.environ.setdefault("SCHEDULER_ENABLED", "false")


# ---------------------------------------------------------------------------
# Helpers de mock
# ---------------------------------------------------------------------------


def _make_conn(fetchone_return=None, fetchall_return=None, rowcount=1):
    """Construire un faux objet connexion psycopg."""
    conn = MagicMock()
    conn.commit = MagicMock()
    conn.rollback = MagicMock()

    cur = MagicMock()
    cur.__enter__ = MagicMock(return_value=cur)
    cur.__exit__ = MagicMock(return_value=False)
    cur.fetchone = MagicMock(return_value=fetchone_return)
    cur.fetchall = MagicMock(return_value=fetchall_return or [])
    cur.rowcount = rowcount
    cur.description = []

    conn.cursor.return_value.__enter__ = lambda s: cur
    conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

    return conn, cur


def _col_desc(names):
    """Creer de faux objets descripteurs de colonnes."""
    return [type("D", (), {"__getitem__": staticmethod(lambda i, c=c: c)})() for c in names]


# ---------------------------------------------------------------------------
# create_share
# ---------------------------------------------------------------------------


def test_create_share_ok():
    """create_share insere un partage et retourne (share_id, token)."""
    from core.snapshot_shares import create_share

    # Premier fetchone : snapshot existe dans le projet.
    # Deuxieme cursor : INSERT du partage.
    cur_calls = []
    fetchone_seq = [("rsn_1",), None]  # premier appel OK, deuxieme non appele
    call_idx = [0]

    conn = MagicMock()
    conn.commit = MagicMock()
    cur = MagicMock()
    cur.__enter__ = MagicMock(return_value=cur)
    cur.__exit__ = MagicMock(return_value=False)
    cur.rowcount = 1

    def fetchone_side():
        idx = call_idx[0]
        call_idx[0] += 1
        return fetchone_seq[idx] if idx < len(fetchone_seq) else None

    cur.fetchone = MagicMock(side_effect=fetchone_side)

    def execute_side(sql, params=None):
        cur_calls.append((sql, params))

    cur.execute = MagicMock(side_effect=execute_side)
    conn.cursor.return_value.__enter__ = lambda s: cur
    conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

    result = create_share("rsn_1", "proj_a", "user_1", conn)

    assert result is not None
    share_id, token = result
    assert share_id.startswith("rss_")
    assert len(token) >= 20  # token_urlsafe(24) >= 20 chars

    conn.commit.assert_called_once()

    # L'INSERT doit avoir ete appele
    insert_sql = next((s for s, _ in cur_calls if "INSERT" in s), None)
    assert insert_sql is not None


def test_create_share_snapshot_not_found():
    """create_share retourne None si le snapshot n'appartient pas au projet."""
    from core.snapshot_shares import create_share

    conn, cur = _make_conn(fetchone_return=None)

    result = create_share("rsn_inconnu", "proj_b", "user_1", conn)
    assert result is None
    # Aucun INSERT ne doit avoir ete effectue
    insert_calls = [c for c in cur.execute.call_args_list if "INSERT" in str(c)]
    assert len(insert_calls) == 0


def test_create_share_ad5_cross_project():
    """AD-5 : create_share avec un project_id different retourne None (non-disclosant)."""
    from core.snapshot_shares import create_share

    # fetchone retourne None : le snapshot n'appartient pas a proj_b
    conn, cur = _make_conn(fetchone_return=None)

    result = create_share("rsn_projet_a", "proj_b", "user_2", conn)
    assert result is None

    # Verifier que la requete filtre bien sur proj_b
    execute_sql = cur.execute.call_args[0][0]
    execute_params = cur.execute.call_args[0][1]
    assert "project_id" in execute_sql
    assert "proj_b" in execute_params


# ---------------------------------------------------------------------------
# revoke_share
# ---------------------------------------------------------------------------


def test_revoke_share_ok():
    """revoke_share retourne True quand la revocation est effectuee."""
    from core.snapshot_shares import revoke_share

    conn, cur = _make_conn(rowcount=1)
    result = revoke_share("rss_1", "proj_a", conn)
    assert result is True
    conn.commit.assert_called_once()


def test_revoke_share_not_found():
    """revoke_share retourne False si le partage est introuvable ou deja revoque."""
    from core.snapshot_shares import revoke_share

    conn, cur = _make_conn(rowcount=0)
    result = revoke_share("rss_inconnu", "proj_a", conn)
    assert result is False


def test_revoke_share_ad5_cross_project():
    """AD-5 : revoke_share filtre par project_id (jointure avec render_snapshots)."""
    from core.snapshot_shares import revoke_share

    conn, cur = _make_conn(rowcount=0)
    result = revoke_share("rss_projet_a", "proj_b", conn)
    assert result is False

    # Verifier que le SQL jointure sur project_id
    executed_sql = cur.execute.call_args[0][0]
    assert "project_id" in executed_sql
    executed_params = cur.execute.call_args[0][1]
    assert "proj_b" in executed_params


# ---------------------------------------------------------------------------
# list_shares
# ---------------------------------------------------------------------------


def _share_row(share_id="rss_1", snap_id="rsn_1", revoked=False):
    return (
        share_id,
        snap_id,
        "tok_abc123",
        datetime(2026, 7, 21, 14, 0, tzinfo=timezone.utc),
        "user_1",
        datetime(2026, 7, 21, 15, 0, tzinfo=timezone.utc) if revoked else None,
    )


def test_list_shares_returns_rows():
    """list_shares retourne les partages du snapshot."""
    from core.snapshot_shares import list_shares

    row = _share_row()
    conn, cur = _make_conn(fetchall_return=[row])
    cur.description = _col_desc(
        ["id", "snapshot_id", "share_token", "shared_at", "shared_by", "revoked_at"]
    )

    result = list_shares("rsn_1", "proj_a", conn)
    assert len(result) == 1
    assert result[0]["id"] == "rss_1"
    assert result[0]["share_token"] == "tok_abc123"
    assert "2026-07-21" in result[0]["shared_at"]
    assert result[0]["revoked_at"] is None


def test_list_shares_active_only():
    """list_shares avec active_only=True filtre sur revoked_at IS NULL (SQL)."""
    from core.snapshot_shares import list_shares

    conn, cur = _make_conn(fetchall_return=[])
    cur.description = _col_desc(
        ["id", "snapshot_id", "share_token", "shared_at", "shared_by", "revoked_at"]
    )
    execute_calls = []
    cur.execute = MagicMock(side_effect=lambda sql, params=None: execute_calls.append(sql) or None)
    cur.fetchall = MagicMock(return_value=[])

    list_shares("rsn_1", "proj_a", conn, active_only=True)

    assert len(execute_calls) == 1
    assert "revoked_at IS NULL" in execute_calls[0]


def test_list_shares_all_includes_revoked():
    """list_shares sans active_only renvoie aussi les partages revoques."""
    from core.snapshot_shares import list_shares

    row_active = _share_row("rss_1", revoked=False)
    row_revoked = _share_row("rss_2", revoked=True)
    conn, cur = _make_conn(fetchall_return=[row_active, row_revoked])
    cur.description = _col_desc(
        ["id", "snapshot_id", "share_token", "shared_at", "shared_by", "revoked_at"]
    )

    result = list_shares("rsn_1", "proj_a", conn, active_only=False)
    assert len(result) == 2
    # Le partage revoque a un revoked_at non-None
    revoked = next(r for r in result if r["id"] == "rss_2")
    assert revoked["revoked_at"] is not None


# ---------------------------------------------------------------------------
# get_shared_snapshot (O1 strict)
# ---------------------------------------------------------------------------


def _shared_snap_row(revoked=False):
    """Ligne de resultat simulee pour get_shared_snapshot."""
    envelope = {"schema_version": "1", "meta": {"freshness": "stale"}, "data": {"rows": []}}
    return (
        "rss_1",                               # share_id
        datetime(2026, 7, 21, 14, 0, tzinfo=timezone.utc),  # shared_at
        "user_1",                              # shared_by
        "rsn_1",                               # snapshot_id
        "get_card",                            # tool_name
        json.dumps(envelope),                  # envelope (JSONB -> str)
        "mcp://widget/kpi",                    # widget_uri
        "resume test",                         # summary_snippet
        "kpi_question",                        # question
        datetime(2026, 7, 20, 10, 0, tzinfo=timezone.utc),  # rendered_at
    )


def test_get_shared_snapshot_ok():
    """get_shared_snapshot retourne le snapshot fige si le token est valide."""
    from core.snapshot_shares import get_shared_snapshot

    row = _shared_snap_row()
    conn, cur = _make_conn(fetchone_return=row)
    cur.description = _col_desc([
        "share_id", "shared_at", "shared_by", "snapshot_id",
        "tool_name", "envelope", "widget_uri", "summary_snippet",
        "question", "rendered_at",
    ])

    result = get_shared_snapshot("valid_token_abc", conn)

    assert result is not None
    assert result["share_id"] == "rss_1"
    assert result["snapshot_id"] == "rsn_1"
    # L'envelope doit etre deserialisee
    assert result["envelope"]["meta"]["freshness"] == "stale"
    # stale_since = freshness figee (AD-9)
    assert result["stale_since"] == "stale"
    # shared_at est une chaine ISO
    assert "2026-07-21" in result["shared_at"]


def test_get_shared_snapshot_not_found():
    """get_shared_snapshot retourne None si le token est inconnu."""
    from core.snapshot_shares import get_shared_snapshot

    conn, cur = _make_conn(fetchone_return=None)
    cur.description = _col_desc([
        "share_id", "shared_at", "shared_by", "snapshot_id",
        "tool_name", "envelope", "widget_uri", "summary_snippet",
        "question", "rendered_at",
    ])

    result = get_shared_snapshot("token_inconnu", conn)
    assert result is None


def test_get_shared_snapshot_revoked_is_none():
    """get_shared_snapshot retourne None si le partage est revoque.

    La requete filtre sur revoked_at IS NULL : un token revoque ne matchera
    pas et fetchone retournera None.
    """
    from core.snapshot_shares import get_shared_snapshot

    # Simuler : fetchone retourne None (comme si revoked_at IS NOT NULL)
    conn, cur = _make_conn(fetchone_return=None)
    cur.description = _col_desc([
        "share_id", "shared_at", "shared_by", "snapshot_id",
        "tool_name", "envelope", "widget_uri", "summary_snippet",
        "question", "rendered_at",
    ])

    result = get_shared_snapshot("token_revoque", conn)
    assert result is None

    # Verifier que la requete SQL contient revoked_at IS NULL
    executed_sql = cur.execute.call_args[0][0]
    assert "revoked_at IS NULL" in executed_sql


def test_get_shared_snapshot_o1_no_marts_access():
    """O1 strict : get_shared_snapshot ne touche QUE render_snapshot_shares + render_snapshots.

    Verifie qu'aucune requete SQL ne reference d'autres tables.
    """
    from core.snapshot_shares import get_shared_snapshot

    row = _shared_snap_row()
    executed_sqls = []
    conn = MagicMock()
    conn.commit = MagicMock()
    cur = MagicMock()
    cur.__enter__ = MagicMock(return_value=cur)
    cur.__exit__ = MagicMock(return_value=False)
    cur.fetchone = MagicMock(return_value=row)
    cur.description = _col_desc([
        "share_id", "shared_at", "shared_by", "snapshot_id",
        "tool_name", "envelope", "widget_uri", "summary_snippet",
        "question", "rendered_at",
    ])

    def capture_execute(sql, params=None):
        executed_sqls.append(sql)

    cur.execute = MagicMock(side_effect=capture_execute)
    conn.cursor.return_value.__enter__ = lambda s: cur
    conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

    get_shared_snapshot("valid_token", conn)

    # Verifier que SEULES les tables autorisees sont referencees
    forbidden_tables = [
        "marts", "connection_ref", "connection_health", "pull_jobs",
        "project_preferences", "datastreams", "notebooks", "projects",
    ]
    for sql in executed_sqls:
        sql_lower = sql.lower()
        for forbidden in forbidden_tables:
            assert forbidden not in sql_lower, (
                f"O1 violation : get_shared_snapshot accede a '{forbidden}' "
                f"qui n'est pas autorisee (tables autorisees : "
                f"render_snapshot_shares, render_snapshots)."
            )

    # Verifier que les bonnes tables sont bien utilisees
    all_sqls = " ".join(executed_sqls).lower()
    assert "render_snapshot_shares" in all_sqls
    assert "render_snapshots" in all_sqls


# ---------------------------------------------------------------------------
# Rate-limit : _check_rendus_rate_limit
# ---------------------------------------------------------------------------


def test_rate_limit_allows_within_limit():
    """_check_rendus_rate_limit autorise les requetes dans la limite."""
    # Reinitialiser l'etat du rate-limiter.
    import core.rendus_api as rendus_mod
    rendus_mod._shared_rendus_rate.clear()

    from core.rendus_api import _check_rendus_rate_limit

    # La premiere requete doit etre autorisee.
    allowed, retry_after = _check_rendus_rate_limit("1.2.3.4", "tok_abc123")
    assert allowed is True
    assert retry_after == 0.0


def test_rate_limit_blocks_after_limit():
    """_check_rendus_rate_limit bloque apres 60 requetes par fenetre."""
    import core.rendus_api as rendus_mod
    rendus_mod._shared_rendus_rate.clear()

    from core.rendus_api import (
        _SHARED_RATE_LIMIT,
        _check_rendus_rate_limit,
    )

    token = "tok_ratelimitest"
    ip = "5.6.7.8"
    token_prefix = token[:8]

    # Remplir le bucket directement en injectant le compteur.
    ip_key = f"ip::{ip}::{token_prefix}"
    ts = time.monotonic()
    rendus_mod._shared_rendus_rate[ip_key] = (_SHARED_RATE_LIMIT, ts)

    # La prochaine requete doit etre bloquee.
    allowed, retry_after = _check_rendus_rate_limit(ip, token)
    assert allowed is False
    assert retry_after >= 1.0


# ---------------------------------------------------------------------------
# REST API : rendus_api endpoints de partage
# ---------------------------------------------------------------------------


def _make_rendus_app_shares(auth_ok: bool = True, identity: str = "test_user"):
    """Creer une application Starlette de test pour rendus_api (volet b)."""
    from core.rendus_api import RENDUS_ROUTES
    from starlette.applications import Starlette

    async def mock_check_auth(request):
        return auth_ok, identity

    import core.rendus_api as rendus_mod
    rendus_mod._check_auth = mock_check_auth  # type: ignore[assignment]

    return Starlette(routes=RENDUS_ROUTES)


def _mock_conn_ctx():
    mock_ctx = MagicMock()
    mock_conn = MagicMock()
    mock_ctx.__enter__ = MagicMock(return_value=mock_conn)
    mock_ctx.__exit__ = MagicMock(return_value=False)
    return mock_ctx, mock_conn


def test_create_share_endpoint_ok():
    """POST /api/rendus/snapshots/{id}/share -> 201 avec share_url."""
    from starlette.testclient import TestClient

    app = _make_rendus_app_shares()
    client = TestClient(app, raise_server_exceptions=False)

    mock_ctx, mock_conn = _mock_conn_ctx()

    with (
        patch("core.db.get_connection", return_value=mock_ctx),
        patch("core.rendus_api._authorize_snapshot_project", return_value=True),
        patch(
            "core.snapshot_shares.create_share",
            return_value=("rss_test", "tok_abc123"),
        ),
        patch("core.audit.write_audit_row"),
    ):
        resp = client.post(
            "/api/rendus/snapshots/rsn_1/share?project_id=proj_a"
        )

    assert resp.status_code == 201
    data = resp.json()
    assert data["share_id"] == "rss_test"
    assert data["share_token"] == "tok_abc123"
    assert "/api/rendus/shared/tok_abc123" in data["share_url"]


def test_create_share_endpoint_not_found():
    """POST /api/rendus/snapshots/{id}/share avec snapshot inconnu -> 404."""
    from starlette.testclient import TestClient

    app = _make_rendus_app_shares()
    client = TestClient(app, raise_server_exceptions=False)

    mock_ctx, mock_conn = _mock_conn_ctx()

    with (
        patch("core.db.get_connection", return_value=mock_ctx),
        patch("core.rendus_api._authorize_snapshot_project", return_value=True),
        patch("core.snapshot_shares.create_share", return_value=None),
    ):
        resp = client.post(
            "/api/rendus/snapshots/rsn_inconnu/share?project_id=proj_a"
        )

    assert resp.status_code == 404


def test_create_share_endpoint_ad5_denied():
    """POST /api/rendus/snapshots/{id}/share avec projet sans acces -> 404."""
    from starlette.testclient import TestClient

    app = _make_rendus_app_shares()
    client = TestClient(app, raise_server_exceptions=False)

    mock_ctx, mock_conn = _mock_conn_ctx()

    with (
        patch("core.db.get_connection", return_value=mock_ctx),
        patch("core.rendus_api._authorize_snapshot_project", return_value=False),
    ):
        resp = client.post(
            "/api/rendus/snapshots/rsn_projet_a/share?project_id=proj_b"
        )

    assert resp.status_code == 404
    assert resp.json()["code"] == "not_found"


def test_list_shares_endpoint_ok():
    """GET /api/rendus/snapshots/{id}/shares -> 200 avec liste."""
    from starlette.testclient import TestClient

    app = _make_rendus_app_shares()
    client = TestClient(app, raise_server_exceptions=False)

    fake_shares = [
        {
            "id": "rss_1",
            "snapshot_id": "rsn_1",
            "share_token": "tok_123",
            "shared_at": "2026-07-21T14:00:00+00:00",
            "shared_by": "user_1",
            "revoked_at": None,
        }
    ]
    mock_ctx, mock_conn = _mock_conn_ctx()

    with (
        patch("core.db.get_connection", return_value=mock_ctx),
        patch("core.rendus_api._authorize_snapshot_project", return_value=True),
        patch("core.snapshot_shares.list_shares", return_value=fake_shares),
    ):
        resp = client.get(
            "/api/rendus/snapshots/rsn_1/shares?project_id=proj_a"
        )

    assert resp.status_code == 200
    data = resp.json()
    assert len(data["shares"]) == 1
    assert data["shares"][0]["id"] == "rss_1"
    # share_url doit etre enrichi
    assert data["shares"][0]["share_url"] is not None
    assert "/api/rendus/shared/tok_123" in data["shares"][0]["share_url"]


def test_revoke_share_endpoint_ok():
    """DELETE /api/rendus/shares/{share_id} -> 204."""
    from starlette.testclient import TestClient

    app = _make_rendus_app_shares()
    client = TestClient(app, raise_server_exceptions=False)

    mock_ctx, mock_conn = _mock_conn_ctx()

    with (
        patch("core.db.get_connection", return_value=mock_ctx),
        patch("core.rendus_api._authorize_snapshot_project", return_value=True),
        patch("core.snapshot_shares.revoke_share", return_value=True),
        patch("core.audit.write_audit_row"),
    ):
        resp = client.delete(
            "/api/rendus/shares/rss_1?project_id=proj_a"
        )

    assert resp.status_code == 204


def test_revoke_share_endpoint_not_found():
    """DELETE /api/rendus/shares/{share_id} avec id inconnu -> 404."""
    from starlette.testclient import TestClient

    app = _make_rendus_app_shares()
    client = TestClient(app, raise_server_exceptions=False)

    mock_ctx, mock_conn = _mock_conn_ctx()

    with (
        patch("core.db.get_connection", return_value=mock_ctx),
        patch("core.rendus_api._authorize_snapshot_project", return_value=True),
        patch("core.snapshot_shares.revoke_share", return_value=False),
    ):
        resp = client.delete(
            "/api/rendus/shares/rss_inconnu?project_id=proj_a"
        )

    assert resp.status_code == 404


def test_revoke_share_endpoint_ad5_denied():
    """DELETE /api/rendus/shares/{share_id} avec projet sans acces -> 404."""
    from starlette.testclient import TestClient

    app = _make_rendus_app_shares()
    client = TestClient(app, raise_server_exceptions=False)

    mock_ctx, mock_conn = _mock_conn_ctx()

    with (
        patch("core.db.get_connection", return_value=mock_ctx),
        patch("core.rendus_api._authorize_snapshot_project", return_value=False),
    ):
        resp = client.delete(
            "/api/rendus/shares/rss_projet_a?project_id=proj_b"
        )

    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# GET /api/rendus/shared/{token} -- endpoint public HTML (O1)
# ---------------------------------------------------------------------------


def _make_fake_snap():
    """Retourner un faux snapshot partage."""
    envelope = {
        "schema_version": "1",
        "meta": {"freshness": "stale", "provenance": "shopify"},
        "data": {"rows": [{"metric": "sessions", "value": 1234}]},
    }
    return {
        "share_id": "rss_1",
        "shared_at": "2026-07-21T14:00:00+00:00",
        "shared_by": "user_1",
        "snapshot_id": "rsn_1",
        "tool_name": "get_card",
        "envelope": envelope,
        "widget_uri": "mcp://widget/kpi",
        "summary_snippet": "Résumé test",
        "question": "kpi_question",
        "rendered_at": "2026-07-20T10:00:00+00:00",
        "stale_since": "stale",
    }


def test_shared_endpoint_ok_returns_html():
    """GET /api/rendus/shared/{token} avec token valide -> 200 HTML."""
    import core.rendus_api as rendus_mod
    rendus_mod._shared_rendus_rate.clear()

    from starlette.testclient import TestClient

    app = _make_rendus_app_shares()
    client = TestClient(app, raise_server_exceptions=False)

    fake_snap = _make_fake_snap()
    mock_ctx, mock_conn = _mock_conn_ctx()

    with (
        patch("core.db.get_connection", return_value=mock_ctx),
        patch("core.snapshot_shares.get_shared_snapshot", return_value=fake_snap),
        patch("core.audit.write_audit_row"),
    ):
        resp = client.get("/api/rendus/shared/valid_token_abc")

    assert resp.status_code == 200
    assert "text/html" in resp.headers.get("content-type", "")
    body = resp.text
    # L'envelope doit etre injectee dans le HTML
    assert "__MCP_STRUCTURED_CONTENT__" in body
    assert "sessions" in body or "envelope" in body.lower()
    # "Partage le" (date FR) doit apparaitre
    assert "Partag" in body or "partag" in body
    # stale_since doit etre mentionne
    assert "stale" in body


def test_shared_endpoint_not_found():
    """GET /api/rendus/shared/{token} avec token inconnu -> 404."""
    import core.rendus_api as rendus_mod
    rendus_mod._shared_rendus_rate.clear()

    from starlette.testclient import TestClient

    app = _make_rendus_app_shares()
    client = TestClient(app, raise_server_exceptions=False)

    mock_ctx, mock_conn = _mock_conn_ctx()

    with (
        patch("core.db.get_connection", return_value=mock_ctx),
        patch("core.snapshot_shares.get_shared_snapshot", return_value=None),
    ):
        resp = client.get("/api/rendus/shared/token_inconnu")

    assert resp.status_code == 404
    assert resp.json()["code"] == "not_found"


def test_shared_endpoint_revoked_is_404():
    """GET /api/rendus/shared/{token} apres revocation -> 404 (non-disclosant).

    La revocation est simulee par get_shared_snapshot retournant None
    (la requete SQL filtre sur revoked_at IS NULL).
    """
    import core.rendus_api as rendus_mod
    rendus_mod._shared_rendus_rate.clear()

    from starlette.testclient import TestClient

    app = _make_rendus_app_shares()
    client = TestClient(app, raise_server_exceptions=False)

    mock_ctx, mock_conn = _mock_conn_ctx()

    with (
        patch("core.db.get_connection", return_value=mock_ctx),
        # Simule le partage revoque (revoked_at IS NOT NULL -> fetchone None)
        patch("core.snapshot_shares.get_shared_snapshot", return_value=None),
    ):
        resp = client.get("/api/rendus/shared/token_revoque")

    assert resp.status_code == 404


def test_shared_endpoint_rate_limit_429():
    """GET /api/rendus/shared/{token} apres N+1 requetes -> 429 avec Retry-After."""
    import core.rendus_api as rendus_mod
    rendus_mod._shared_rendus_rate.clear()

    from core.rendus_api import _SHARED_RATE_LIMIT
    from starlette.testclient import TestClient

    app = _make_rendus_app_shares()
    client = TestClient(app, raise_server_exceptions=False)

    token = "tok_ratelimithtml"
    token_prefix = token[:8]
    ip = "testclient"  # Starlette TestClient utilise "testclient" comme IP
    ip_key = f"ip::{ip}::{token_prefix}"
    ts = time.monotonic()
    rendus_mod._shared_rendus_rate[ip_key] = (_SHARED_RATE_LIMIT, ts)

    mock_ctx, mock_conn = _mock_conn_ctx()

    # Ne doit PAS appeler get_connection (rejete avant)
    resp = client.get(f"/api/rendus/shared/{token}")

    assert resp.status_code == 429
    assert "Retry-After" in resp.headers


def test_shared_endpoint_html_contains_frozen_envelope():
    """Le HTML servi contient l'envelope GELEE (pas de re-run, pas de DB live).

    Verifie que l'envelope injectee dans le HTML correspond exactement a celle
    du snapshot stocke -- aucune transformation (AD-9).
    """
    import core.rendus_api as rendus_mod
    rendus_mod._shared_rendus_rate.clear()

    from starlette.testclient import TestClient

    app = _make_rendus_app_shares()
    client = TestClient(app, raise_server_exceptions=False)

    fake_snap = _make_fake_snap()
    mock_ctx, mock_conn = _mock_conn_ctx()

    with (
        patch("core.db.get_connection", return_value=mock_ctx),
        patch("core.snapshot_shares.get_shared_snapshot", return_value=fake_snap),
        patch("core.audit.write_audit_row"),
    ):
        resp = client.get("/api/rendus/shared/valid_token")

    assert resp.status_code == 200
    body = resp.text

    # L'envelope injectee contient les donnees figees (pas rechargees)
    assert "sessions" in body      # champ data de l'envelope
    assert "1234" in body          # valeur figee

    # Verifier que le HTML contient la banniere "Partage le"
    assert "Partag" in body        # "Partagé le" (escape HTML)
    assert "stale" in body         # stale_since honnete

    # Verifier l'injection dans window.__MCP_STRUCTURED_CONTENT__
    assert "window.__MCP_STRUCTURED_CONTENT__" in body
    assert "window.__TOOROW_SHARED_META__" in body


# ---------------------------------------------------------------------------
# C1 -- XSS breakout protection (_json_for_script)
# ---------------------------------------------------------------------------


def test_json_for_script_escapes_script_close_tag():
    """_json_for_script doit echapper </script> pour empecher le breakout XSS.

    Un champ data contenant '</script><script>alert(1)</script>' NE DOIT PAS
    apparaitre tel quel dans le HTML -- il doit etre transforme en '<\\/script>'.
    """
    from core.rendus_api import _json_for_script

    payload = "</script><script>alert(document.cookie)</script>"
    result = _json_for_script(payload)

    # La sequence de breakout brute ne doit pas apparaitre
    assert "</script>" not in result
    # La version echappee valide doit etre presente
    assert "<\\/script>" in result


def test_shared_endpoint_xss_payload_in_envelope_field():
    """GET /api/rendus/shared/{token} avec payload XSS dans l'envelope -> body safe.

    Un champ data de l'envelope contenant le vecteur de breakout classique
    '</script><script>x</script>' ne doit PAS apparaitre non echappe dans le HTML.
    Le body doit contenir '<\\/script>' a la place.
    """
    import core.rendus_api as rendus_mod
    rendus_mod._shared_rendus_rate.clear()

    from starlette.testclient import TestClient

    app = _make_rendus_app_shares()
    client = TestClient(app, raise_server_exceptions=False)

    xss_payload = "</script><script>alert(document.cookie)</script>"
    fake_snap = _make_fake_snap()
    fake_snap["envelope"]["data"]["rows"][0]["campaign_name"] = xss_payload
    mock_ctx, mock_conn = _mock_conn_ctx()

    with (
        patch("core.db.get_connection", return_value=mock_ctx),
        patch("core.snapshot_shares.get_shared_snapshot", return_value=fake_snap),
        patch("core.audit.write_audit_row"),
    ):
        resp = client.get("/api/rendus/shared/valid_token_xss")

    assert resp.status_code == 200
    body = resp.text
    # Le payload brut ne doit PAS apparaitre dans le HTML
    assert "</script><script>" not in body
    # La forme echappee doit etre presente
    assert "<\\/script>" in body


def test_shared_endpoint_xss_payload_in_question():
    """GET /api/rendus/shared/{token} avec payload XSS dans le champ question -> body safe."""
    import core.rendus_api as rendus_mod
    rendus_mod._shared_rendus_rate.clear()

    from starlette.testclient import TestClient

    app = _make_rendus_app_shares()
    client = TestClient(app, raise_server_exceptions=False)

    xss_payload = "</script><script>alert(1)</script>"
    fake_snap = _make_fake_snap()
    fake_snap["question"] = xss_payload
    mock_ctx, mock_conn = _mock_conn_ctx()

    with (
        patch("core.db.get_connection", return_value=mock_ctx),
        patch("core.snapshot_shares.get_shared_snapshot", return_value=fake_snap),
        patch("core.audit.write_audit_row"),
    ):
        resp = client.get("/api/rendus/shared/valid_token_xss2")

    assert resp.status_code == 200
    body = resp.text
    # La sequence de breakout brute ne doit PAS apparaitre dans le body HTML
    assert "</script><script>" not in body
    assert "<\\/script>" in body


# ---------------------------------------------------------------------------
# H2 -- Audit appele avec la bonne signature (autospec)
# ---------------------------------------------------------------------------


def test_create_share_endpoint_audit_called_with_correct_signature():
    """POST share -> write_audit_row appelee avec la bonne signature (identity, action, ...).

    Utilise autospec=True pour valider que les arguments correspondent exactement
    a la signature reelle de write_audit_row (H2 : la mauvaise signature levait
    TypeError avale silencieusement).
    """
    import core.audit as audit_mod
    from starlette.testclient import TestClient

    app = _make_rendus_app_shares(identity="user_audit_test")
    client = TestClient(app, raise_server_exceptions=False)

    mock_ctx, mock_conn = _mock_conn_ctx()

    with (
        patch("core.db.get_connection", return_value=mock_ctx),
        patch("core.rendus_api._authorize_snapshot_project", return_value=True),
        patch(
            "core.snapshot_shares.create_share",
            return_value=("rss_audit", "tok_audit_abc"),
        ),
        patch("core.audit.write_audit_row", autospec=True) as mock_audit,
    ):
        resp = client.post(
            "/api/rendus/snapshots/rsn_audit/share?project_id=proj_audit"
        )

    assert resp.status_code == 201
    mock_audit.assert_called_once()
    call_kwargs = mock_audit.call_args
    # Verifier les arguments positionnels/nommees : identity, action, provider_account,
    # connection_ref doivent etre presents et corrects.
    args, kwargs = call_kwargs
    # write_audit_row(identity, action, provider_account, connection_ref, metadata=...)
    # Peut etre appele en kwargs uniquement.
    _pos = ["identity", "action", "provider_account", "connection_ref"]
    all_kwargs = {**dict(zip(_pos, args)), **kwargs}
    assert all_kwargs.get("identity") == "user_audit_test"
    assert all_kwargs.get("action") == audit_mod.ACTION_SNAPSHOT_SHARED
    assert all_kwargs.get("provider_account") == "rendus"
    assert all_kwargs.get("connection_ref") == ""
    assert "project_id" in (all_kwargs.get("metadata") or {})


def test_revoke_share_endpoint_audit_called_with_correct_signature():
    """DELETE share -> write_audit_row appelee avec la bonne signature (identity, action, ...).

    Verifie que l'audit revoke utilise la vraie signature (H2).
    """
    import core.audit as audit_mod
    from starlette.testclient import TestClient

    app = _make_rendus_app_shares(identity="user_revoke_test")
    client = TestClient(app, raise_server_exceptions=False)

    mock_ctx, mock_conn = _mock_conn_ctx()

    with (
        patch("core.db.get_connection", return_value=mock_ctx),
        patch("core.rendus_api._authorize_snapshot_project", return_value=True),
        patch("core.snapshot_shares.revoke_share", return_value=True),
        patch("core.audit.write_audit_row", autospec=True) as mock_audit,
    ):
        resp = client.delete(
            "/api/rendus/shares/rss_revoke_audit?project_id=proj_audit"
        )

    assert resp.status_code == 204
    mock_audit.assert_called_once()
    args, kwargs = mock_audit.call_args
    _pos = ["identity", "action", "provider_account", "connection_ref"]
    all_kwargs = {**dict(zip(_pos, args)), **kwargs}
    assert all_kwargs.get("identity") == "user_revoke_test"
    assert all_kwargs.get("action") == audit_mod.ACTION_SNAPSHOT_SHARE_REVOKED
    assert all_kwargs.get("provider_account") == "rendus"
    assert all_kwargs.get("connection_ref") == ""
    assert "project_id" in (all_kwargs.get("metadata") or {})


# ---------------------------------------------------------------------------
# M1 -- Purge du dict rate-limit (pas de croissance illimitee)
# ---------------------------------------------------------------------------


def test_rate_limit_dict_purged_after_window_expiry():
    """M1 : le dict rate-limit est purge apres expiration de la fenetre.

    Simule N requetes d'une IP fictive avec une fenetre passee, puis effectue
    une nouvelle requete depuis une IP differente. Le dict ne doit pas conserver
    les entrees expirees indefiniment.
    """
    import core.rendus_api as rendus_mod
    rendus_mod._shared_rendus_rate.clear()

    from core.rendus_api import _SHARED_RATE_WINDOW, _check_rendus_rate_limit

    # Injecter des entrees deja expirees (window dans le passe)
    old_ts = time.monotonic() - _SHARED_RATE_WINDOW - 5.0  # bien avant la fenetre
    for i in range(20):
        rendus_mod._shared_rendus_rate[f"ip::old_ip_{i}::tok_old"] = (60, old_ts)

    size_before = len(rendus_mod._shared_rendus_rate)
    assert size_before == 20

    # Effectuer une requete legitime : la purge doit etre declenchee
    _check_rendus_rate_limit("new_ip_fresh", "tok_fresh1")

    # Les entrees expirees doivent avoir ete purgees
    size_after = len(rendus_mod._shared_rendus_rate)
    assert size_after < size_before, (
        f"Le dict rate-limit n'a pas ete purge : {size_before} -> {size_after}"
    )
    # Les 20 cles expirees ne doivent plus etre la
    expired_keys_remaining = [
        k for k in rendus_mod._shared_rendus_rate if k.startswith("ip::old_ip_")
    ]
    assert len(expired_keys_remaining) == 0
