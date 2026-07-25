"""Tests unitaires pour server/core/snapshots.py (Story 13.5 volet a).

Seam tests couverts :
  - persist_render_envelope : insertion + purge de retention
  - list_render_snapshots   : lecture filtrée par projet
  - get_render_snapshot     : lecture d'un snapshot complet
  - delete_render_snapshot  : suppression
  - AD-5 : denégation cross-projet (snapshot projet A, lecture projet B -> None)
  - retention : test que la purge garde les N derniers selon la borne
  - best-effort : une erreur de persistance ne lève pas d'exception

Tous les tests sont offline (mock psycopg). La section pg-gatee est dans
test_snapshots_integration.py (marquée TEST_POSTGRES_DSN).

ASCII-only stdout (L-3).
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
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
    cur.description = []  # rempli par les tests qui en ont besoin

    conn.cursor.return_value.__enter__ = lambda s: cur
    conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

    return conn, cur


def _col_desc(names):
    """Créer de faux objets descripteurs de colonnes."""
    return [type("D", (), {"__getitem__": staticmethod(lambda i, c=c: c)})() for c in names]


# ---------------------------------------------------------------------------
# persist_render_envelope
# ---------------------------------------------------------------------------


def test_persist_render_envelope_inserts_and_purges():
    """Un appel à persist_render_envelope effectue INSERT + DELETE purge."""
    from core.snapshots import persist_render_envelope

    conn, cur = _make_conn()
    execute_calls = []
    cur.execute = MagicMock(side_effect=lambda sql, params=None: execute_calls.append((sql, params)))

    result = persist_render_envelope(
        project_id="proj_a",
        tool_name="get_card",
        envelope={"schema_version": "1", "meta": {"freshness": "live"}, "data": {}},
        widget_uri="mcp://widget/kpi",
        summary="résumé test",
        tool_args={"template": "kpi"},
        identity="user_1",
        trace_id="abc123",
        conn=conn,
    )

    # Un snapshot_id est retourné
    assert result is not None
    assert result.startswith("rsn_")

    # Deux appels cursor.execute : INSERT + DELETE purge
    assert len(execute_calls) == 2
    insert_sql, insert_params = execute_calls[0]
    assert "INSERT INTO app.render_snapshots" in insert_sql
    assert insert_params[0] == result          # id
    assert insert_params[1] == "proj_a"        # project_id
    assert insert_params[2] == "get_card"      # tool_name

    delete_sql, delete_params = execute_calls[1]
    assert "DELETE FROM app.render_snapshots" in delete_sql
    assert delete_params[0] == "proj_a"

    # commit appelé
    conn.commit.assert_called_once()


def test_persist_render_envelope_returns_none_on_error():
    """Best-effort : une exception DB ne propage pas (retourne None)."""
    from core.snapshots import persist_render_envelope

    conn = MagicMock()
    conn.cursor.return_value.__enter__ = MagicMock(side_effect=RuntimeError("db down"))
    conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

    result = persist_render_envelope(
        project_id="proj_a",
        tool_name="get_report",
        envelope={"schema_version": "1", "meta": {}, "data": {}},
        widget_uri=None,
        summary="",
        tool_args=None,
        identity=None,
        trace_id=None,
        conn=conn,
    )

    assert result is None  # jamais d'exception levée


def test_persist_render_envelope_question_extracted_from_tool_args():
    """La question est extraite de tool_args (report_id / template)."""
    from core.snapshots import persist_render_envelope

    conn, cur = _make_conn()
    captured = {}

    def capture_execute(sql, params=None):
        if "INSERT" in sql and params:
            captured["question"] = params[7]  # indice de la colonne question

    cur.execute = MagicMock(side_effect=capture_execute)

    persist_render_envelope(
        project_id="proj_a",
        tool_name="get_report",
        envelope={"schema_version": "1", "meta": {}, "data": {}},
        widget_uri=None,
        summary="",
        tool_args={"report_id": "shopify/revenue"},
        identity=None,
        trace_id=None,
        conn=conn,
    )

    assert captured.get("question") == "shopify/revenue"


# ---------------------------------------------------------------------------
# list_render_snapshots
# ---------------------------------------------------------------------------


def _snapshot_row(snap_id="rsn_1", project_id="proj_a", tool_name="get_card"):
    """Retourner une ligne de résultat DB simulée."""
    return (
        snap_id,                      # id
        project_id,                   # project_id
        tool_name,                    # tool_name
        json.dumps({"template": "kpi"}),  # tool_args (JSONB retourné en str par psycopg mock)
        "mcp://widget/kpi",           # widget_uri
        "résumé tronqué",             # summary_snippet
        "kpi",                        # question
        "user_1",                     # identity
        "abc",                        # trace_id
        datetime(2026, 7, 21, 10, 0, tzinfo=timezone.utc),  # created_at
        json.dumps({"freshness": "live"}),  # meta (JSONB)
    )


def test_list_render_snapshots_returns_rows():
    """list_render_snapshots renvoie les lignes du projet."""
    from core.snapshots import list_render_snapshots

    row = _snapshot_row()
    conn, cur = _make_conn(fetchall_return=[row])
    cur.description = _col_desc([
        "id", "project_id", "tool_name", "tool_args",
        "widget_uri", "summary_snippet", "question",
        "identity", "trace_id", "created_at", "meta",
    ])

    result = list_render_snapshots("proj_a", conn)

    assert len(result) == 1
    assert result[0]["id"] == "rsn_1"
    assert result[0]["project_id"] == "proj_a"
    assert result[0]["tool_name"] == "get_card"
    # tool_args doit être désérialisé depuis la chaîne JSON
    assert result[0]["tool_args"] == {"template": "kpi"}
    # created_at doit être une chaîne ISO
    assert "2026-07-21" in result[0]["created_at"]


def test_list_render_snapshots_empty():
    """list_render_snapshots renvoie [] pour un projet sans rendus."""
    from core.snapshots import list_render_snapshots

    conn, cur = _make_conn(fetchall_return=[])
    cur.description = _col_desc([
        "id", "project_id", "tool_name", "tool_args",
        "widget_uri", "summary_snippet", "question",
        "identity", "trace_id", "created_at", "meta",
    ])

    result = list_render_snapshots("proj_vide", conn)
    assert result == []


def test_list_render_snapshots_tool_name_filter():
    """Le filtre tool_name est passé à la requête SQL."""
    from core.snapshots import list_render_snapshots

    conn, cur = _make_conn(fetchall_return=[])
    cur.description = _col_desc([
        "id", "project_id", "tool_name", "tool_args",
        "widget_uri", "summary_snippet", "question",
        "identity", "trace_id", "created_at", "meta",
    ])
    execute_calls = []
    cur.execute = MagicMock(side_effect=lambda sql, params=None: execute_calls.append((sql, params)) or None)
    cur.fetchall = MagicMock(return_value=[])

    list_render_snapshots("proj_a", conn, tool_name="get_report")

    assert len(execute_calls) == 1
    sql, params = execute_calls[0]
    assert "tool_name" in sql
    assert "get_report" in params


# ---------------------------------------------------------------------------
# get_render_snapshot
# ---------------------------------------------------------------------------


def test_get_render_snapshot_found():
    """get_render_snapshot retourne le snapshot complet."""
    from core.snapshots import get_render_snapshot

    envelope = {"schema_version": "1", "meta": {"freshness": "live"}, "data": {"rows": []}}
    row = (
        "rsn_1",
        "proj_a",
        "get_card",
        json.dumps({"template": "kpi"}),
        json.dumps(envelope),
        "mcp://widget/kpi",
        "résumé",
        "kpi",
        "user_1",
        "abc",
        datetime(2026, 7, 21, 10, 0, tzinfo=timezone.utc),
    )
    conn, cur = _make_conn(fetchone_return=row)
    cur.description = _col_desc([
        "id", "project_id", "tool_name", "tool_args", "envelope",
        "widget_uri", "summary_snippet", "question",
        "identity", "trace_id", "created_at",
    ])

    result = get_render_snapshot("rsn_1", "proj_a", conn)

    assert result is not None
    assert result["id"] == "rsn_1"
    # L'envelope doit être désérialisée
    assert result["envelope"]["meta"]["freshness"] == "live"


def test_get_render_snapshot_not_found():
    """get_render_snapshot retourne None si introuvable."""
    from core.snapshots import get_render_snapshot

    conn, cur = _make_conn(fetchone_return=None)
    cur.description = _col_desc([
        "id", "project_id", "tool_name", "tool_args", "envelope",
        "widget_uri", "summary_snippet", "question",
        "identity", "trace_id", "created_at",
    ])

    result = get_render_snapshot("rsn_inexistant", "proj_a", conn)
    assert result is None


def test_get_render_snapshot_ad5_cross_project():
    """AD-5 : un snapshot du projet A est invisible depuis le projet B.

    La clause WHERE project_id = %s dans la requête garantit que get_render_snapshot
    retourne None pour un projet non propriétaire.
    """
    from core.snapshots import get_render_snapshot

    # Simuler : la requête ne retourne rien quand project_id ne correspond pas
    conn, cur = _make_conn(fetchone_return=None)
    cur.description = _col_desc([
        "id", "project_id", "tool_name", "tool_args", "envelope",
        "widget_uri", "summary_snippet", "question",
        "identity", "trace_id", "created_at",
    ])

    # Snapshot appartenant au projet A, demandé depuis le projet B
    result = get_render_snapshot("rsn_projet_a", "proj_b", conn)
    assert result is None

    # Vérifier que la requête filtre bien sur projet B
    executed_sql = cur.execute.call_args[0][0]
    executed_params = cur.execute.call_args[0][1]
    assert "project_id" in executed_sql
    assert "proj_b" in executed_params


# ---------------------------------------------------------------------------
# delete_render_snapshot
# ---------------------------------------------------------------------------


def test_delete_render_snapshot_success():
    """delete_render_snapshot retourne True si la suppression a lieu."""
    from core.snapshots import delete_render_snapshot

    conn, cur = _make_conn(rowcount=1)
    result = delete_render_snapshot("rsn_1", "proj_a", conn)
    assert result is True
    conn.commit.assert_called_once()


def test_delete_render_snapshot_not_found():
    """delete_render_snapshot retourne False si rien à supprimer."""
    from core.snapshots import delete_render_snapshot

    conn, cur = _make_conn(rowcount=0)
    result = delete_render_snapshot("rsn_inexistant", "proj_a", conn)
    assert result is False


# ---------------------------------------------------------------------------
# Retention : test que la purge supprime les snapshots trop anciens
# ---------------------------------------------------------------------------


def test_purge_deletes_old_snapshots_only():
    """La purge de rétention passe la cutoff correcte à DELETE."""
    from core.snapshots import persist_render_envelope

    conn, cur = _make_conn()
    delete_params_captured = {}

    def capture(sql, params=None):
        if params and "DELETE" in sql:
            delete_params_captured["params"] = params

    cur.execute = MagicMock(side_effect=capture)

    with patch.dict(os.environ, {"RENDER_SNAPSHOT_RETENTION_DAYS": "7"}):
        persist_render_envelope(
            project_id="proj_ret",
            tool_name="get_card",
            envelope={"schema_version": "1", "meta": {}, "data": {}},
            widget_uri=None,
            summary="",
            tool_args=None,
            identity=None,
            trace_id=None,
            conn=conn,
        )

    # La cutoff doit être dans les paramètres DELETE
    assert "params" in delete_params_captured
    dp = delete_params_captured["params"]
    assert dp[0] == "proj_ret"
    # La cutoff est une datetime timezone-aware
    cutoff = dp[1]
    assert isinstance(cutoff, datetime)
    expected_cutoff = datetime.now(tz=timezone.utc) - timedelta(days=7)
    # Tolérance de 5 secondes
    assert abs((cutoff - expected_cutoff).total_seconds()) < 5


# ---------------------------------------------------------------------------
# REST API : rendus_api
# ---------------------------------------------------------------------------


def _make_rendus_app(auth_ok: bool = True, identity: str = "test_user"):
    """Créer une application Starlette de test pour rendus_api."""
    from core.rendus_api import RENDUS_ROUTES
    from starlette.applications import Starlette

    async def mock_check_auth(request):
        return auth_ok, identity

    import core.rendus_api as rendus_mod
    rendus_mod._check_auth = mock_check_auth  # type: ignore[assignment]

    return Starlette(routes=RENDUS_ROUTES)


def test_list_snapshots_unauthorized():
    """GET /api/rendus/snapshots sans auth -> 401."""
    from starlette.testclient import TestClient

    app = _make_rendus_app(auth_ok=False)
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/api/rendus/snapshots?project_id=proj_a")
    assert resp.status_code == 401


def test_list_snapshots_missing_project_id():
    """GET /api/rendus/snapshots sans project_id -> 422."""
    from starlette.testclient import TestClient

    app = _make_rendus_app()
    client = TestClient(app, raise_server_exceptions=False)

    with patch("core.db.get_connection"):
        resp = client.get("/api/rendus/snapshots")
    assert resp.status_code == 422


def test_list_snapshots_ok():
    """GET /api/rendus/snapshots avec project_id valide -> 200 avec snapshots."""
    from starlette.testclient import TestClient

    app = _make_rendus_app()
    client = TestClient(app, raise_server_exceptions=False)

    fake_snapshots = [{"id": "rsn_1", "tool_name": "get_card"}]

    mock_conn_ctx = MagicMock()
    mock_conn = MagicMock()
    mock_conn_ctx.__enter__ = MagicMock(return_value=mock_conn)
    mock_conn_ctx.__exit__ = MagicMock(return_value=False)

    with (
        patch("core.db.get_connection", return_value=mock_conn_ctx),
        patch("core.project_access.identity_has_project_access", return_value=True),
        patch("core.snapshots.list_render_snapshots", return_value=fake_snapshots),
    ):
        resp = client.get("/api/rendus/snapshots?project_id=proj_a")

    assert resp.status_code == 200
    data = resp.json()
    assert data["snapshots"] == fake_snapshots
    assert data["total"] == 1


def test_list_snapshots_ad5_denied():
    """GET /api/rendus/snapshots avec un projet sans acces -> 404 non-disclosant."""
    from starlette.testclient import TestClient

    app = _make_rendus_app()
    client = TestClient(app, raise_server_exceptions=False)

    mock_conn_ctx = MagicMock()
    mock_conn = MagicMock()
    mock_conn_ctx.__enter__ = MagicMock(return_value=mock_conn)
    mock_conn_ctx.__exit__ = MagicMock(return_value=False)

    with (
        patch("core.db.get_connection", return_value=mock_conn_ctx),
        patch("core.project_access.identity_has_project_access", return_value=False),
    ):
        resp = client.get("/api/rendus/snapshots?project_id=proj_autre")

    assert resp.status_code == 404
    assert resp.json()["code"] == "not_found"


def test_get_snapshot_ok():
    """GET /api/rendus/snapshots/{id} retourne l'envelope complète."""
    from starlette.testclient import TestClient

    app = _make_rendus_app()
    client = TestClient(app, raise_server_exceptions=False)

    fake_snap = {
        "id": "rsn_1",
        "envelope": {"schema_version": "1", "meta": {"freshness": "live"}, "data": {}},
    }

    mock_conn_ctx = MagicMock()
    mock_conn = MagicMock()
    mock_conn_ctx.__enter__ = MagicMock(return_value=mock_conn)
    mock_conn_ctx.__exit__ = MagicMock(return_value=False)

    with (
        patch("core.db.get_connection", return_value=mock_conn_ctx),
        patch("core.project_access.identity_has_project_access", return_value=True),
        patch("core.snapshots.get_render_snapshot", return_value=fake_snap),
    ):
        resp = client.get("/api/rendus/snapshots/rsn_1?project_id=proj_a")

    assert resp.status_code == 200
    assert resp.json()["envelope"]["meta"]["freshness"] == "live"


def test_get_snapshot_not_found():
    """GET /api/rendus/snapshots/{id} avec id inconnu -> 404."""
    from starlette.testclient import TestClient

    app = _make_rendus_app()
    client = TestClient(app, raise_server_exceptions=False)

    mock_conn_ctx = MagicMock()
    mock_conn = MagicMock()
    mock_conn_ctx.__enter__ = MagicMock(return_value=mock_conn)
    mock_conn_ctx.__exit__ = MagicMock(return_value=False)

    with (
        patch("core.db.get_connection", return_value=mock_conn_ctx),
        patch("core.project_access.identity_has_project_access", return_value=True),
        patch("core.snapshots.get_render_snapshot", return_value=None),
    ):
        resp = client.get("/api/rendus/snapshots/rsn_inexistant?project_id=proj_a")

    assert resp.status_code == 404


def test_delete_snapshot_ok():
    """DELETE /api/rendus/snapshots/{id} -> 204."""
    from starlette.testclient import TestClient

    app = _make_rendus_app()
    client = TestClient(app, raise_server_exceptions=False)

    mock_conn_ctx = MagicMock()
    mock_conn = MagicMock()
    mock_conn_ctx.__enter__ = MagicMock(return_value=mock_conn)
    mock_conn_ctx.__exit__ = MagicMock(return_value=False)

    with (
        patch("core.db.get_connection", return_value=mock_conn_ctx),
        patch("core.project_access.identity_has_project_access", return_value=True),
        patch("core.snapshots.delete_render_snapshot", return_value=True),
    ):
        resp = client.delete("/api/rendus/snapshots/rsn_1?project_id=proj_a")

    assert resp.status_code == 204


def test_delete_snapshot_not_found():
    """DELETE /api/rendus/snapshots/{id} avec id inconnu -> 404."""
    from starlette.testclient import TestClient

    app = _make_rendus_app()
    client = TestClient(app, raise_server_exceptions=False)

    mock_conn_ctx = MagicMock()
    mock_conn = MagicMock()
    mock_conn_ctx.__enter__ = MagicMock(return_value=mock_conn)
    mock_conn_ctx.__exit__ = MagicMock(return_value=False)

    with (
        patch("core.db.get_connection", return_value=mock_conn_ctx),
        patch("core.project_access.identity_has_project_access", return_value=True),
        patch("core.snapshots.delete_render_snapshot", return_value=False),
    ):
        resp = client.delete("/api/rendus/snapshots/rsn_inexistant?project_id=proj_a")

    assert resp.status_code == 404
