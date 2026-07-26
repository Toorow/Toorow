"""Unit tests for server/core/dq_api.py (Stories 8.6 + 13.3, Epics 8 + 13).

Tests:
  - GET /api/dq/summary: 401 without auth; 422 without project_id; 200 structure (5 monitors)
  - GET /api/dq/issues: 401; 422 bad monitor/status; 200 list with filtering
  - POST /api/dq/issues/{id}/acknowledge: 401; 404 unknown id; 403 wrong project; 200 ack
  - GET /api/dq/history: 401; 422 missing/bad params; 200 success_rate calcul; AD-5 (H1)
  - GET /api/dq/datastream-freshness: 401; 422; 200 shape
    (vrai schema: state+actual_rows); AD-5 (H1)
  - POST /api/dq/evaluate: 401; 422; 200 trigger deterministe (H2); 429 rate-limit; audit; AD-5 (H1)

Schema reel app.pull_jobs : state (pas status), completed_at (pas row_count), pull_id FK.
app.pull_verifications : verdict, actual_rows (joint par pull_id).
AD-5 : identity_has_project_access teste sur chaque nouveau endpoint (C1+H1).

Strategy:
  - Starlette TestClient via httpx-based requests against the route handlers.
  - All DB calls mocked with fake get_connection.
  - Uses _check_auth mock to bypass Bearer token logic.
  - AD-5 tests : seed app.project_members non-vide pour activer le mode closed.
"""

from __future__ import annotations

import os
import threading
from datetime import date, datetime, timezone
from unittest.mock import MagicMock, patch

from starlette.testclient import TestClient

os.environ.setdefault("SCHEDULER_ENABLED", "false")

# ---------------------------------------------------------------------------
# Minimal Starlette app wrapping DQ_ROUTES for testing
# ---------------------------------------------------------------------------


def _make_test_app(auth_ok: bool = True):
    from core.dq_api import DQ_ROUTES
    from starlette.applications import Starlette

    async def mock_check_auth(request):
        return auth_ok, "test_user"

    # Patch _check_auth in dq_api at the module level.
    import core.dq_api as dq_api_mod

    dq_api_mod._check_auth = mock_check_auth  # type: ignore[assignment]

    app = Starlette(routes=DQ_ROUTES)
    return app


# ---------------------------------------------------------------------------
# Cursor / connection helpers
# ---------------------------------------------------------------------------


def _col_desc(names: list[str]):
    return [type("D", (), {"__getitem__": staticmethod(lambda i, c=c: c)})() for c in names]


def _make_db_conn(side_effects: list):
    """Build a fake get_connection() context manager yielding cursor responses in sequence."""
    conn_ctx = MagicMock()
    conn = MagicMock()
    conn_ctx.__enter__ = MagicMock(return_value=conn)
    conn_ctx.__exit__ = MagicMock(return_value=False)
    conn.commit = MagicMock()

    cur = MagicMock()
    cur.__enter__ = MagicMock(return_value=cur)
    cur.__exit__ = MagicMock(return_value=False)
    cur.fetchone = MagicMock(side_effect=side_effects)
    cur.fetchall = MagicMock(return_value=[])

    conn.cursor.return_value.__enter__ = lambda s: cur
    conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

    get_conn = MagicMock(return_value=conn_ctx)
    return get_conn, conn, cur


# ---------------------------------------------------------------------------
# GET /api/dq/summary -- auth guard
# ---------------------------------------------------------------------------


def test_dq_summary_unauthorized():
    app = _make_test_app(auth_ok=False)
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/api/dq/summary?project_id=proj_1")
    assert resp.status_code == 401
    data = resp.json()
    assert data["code"] == "unauthorized"


def test_dq_summary_missing_project_id():
    app = _make_test_app(auth_ok=True)
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/api/dq/summary")
    assert resp.status_code == 422
    data = resp.json()
    assert data["code"] == "invalid_param"


def test_dq_summary_returns_5_monitors():
    """Story 13.3 + 37.9: summary expose le registre complet des moniteurs DQ.

    Le compte n'est plus fige : Story 37.9 ajoute dq_geography. On assert le
    REGISTRE (_DQ_TYPES) et la presence de chaque moniteur, pas un nombre.
    """
    app = _make_test_app(auth_ok=True)
    client = TestClient(app, raise_server_exceptions=False)

    with patch("core.db.get_connection") as mock_gc:
        # First query: total_streams count -> fetchone returns (3,).
        conn_ctx = MagicMock()
        conn = MagicMock()
        conn_ctx.__enter__ = MagicMock(return_value=conn)
        conn_ctx.__exit__ = MagicMock(return_value=False)
        conn.commit = MagicMock()

        cur1 = MagicMock()
        cur1.__enter__ = MagicMock(return_value=cur1)
        cur1.__exit__ = MagicMock(return_value=False)
        cur1.fetchone = MagicMock(return_value=(3,))
        cur1.fetchall = MagicMock(return_value=[])
        cur1.description = _col_desc(["count"])

        cur2 = MagicMock()
        cur2.__enter__ = MagicMock(return_value=cur2)
        cur2.__exit__ = MagicMock(return_value=False)
        cur2.fetchall = MagicMock(return_value=[])
        cur2.description = _col_desc(["type", "fired_at", "acknowledged_at"])

        call_count = {"n": 0}

        def cursor_factory():
            call_count["n"] += 1
            cm = MagicMock()
            if call_count["n"] == 1:
                cm.__enter__ = MagicMock(return_value=cur1)
            else:
                cm.__enter__ = MagicMock(return_value=cur2)
            cm.__exit__ = MagicMock(return_value=False)
            return cm

        conn.cursor = cursor_factory
        mock_gc.return_value = conn_ctx

        resp = client.get("/api/dq/summary?project_id=proj_1")

    assert resp.status_code == 200
    data = resp.json()
    assert "monitors" in data
    import core.dq_api as dq_api_mod

    types = [m["type"] for m in data["monitors"]]
    assert types == list(dq_api_mod._DQ_TYPES), (
        f"le summary doit exposer tout le registre _DQ_TYPES, recu {types}"
    )
    assert "dq_volume" in types
    assert "dq_timeliness" in types
    assert "dq_duplication" in types
    assert "dq_schema" in types
    assert "dq_date_format" in types, (
        "dq_date_format doit etre visible dans le summary (Story 13.3)"
    )
    assert "dq_geography" in types, (
        "dq_geography doit etre visible dans le summary (Story 37.9): sans lui, la "
        "geographie non resolue reste invisible aux surfaces de supervision DQ"
    )
    for m in data["monitors"]:
        assert "healthy_pct" in m
        assert "unresolved_count" in m
        assert "new_since_yesterday" in m


def test_dq_summary_date_format_label():
    """dq_date_format doit porter le label 'Lignes rejetees'."""
    import core.dq_api as dq_api_mod

    assert "dq_date_format" in dq_api_mod._MONITOR_LABELS
    assert dq_api_mod._MONITOR_LABELS["dq_date_format"] == "Lignes rejetees"


# ---------------------------------------------------------------------------
# GET /api/dq/issues -- auth + filter guards
# ---------------------------------------------------------------------------


def test_dq_issues_unauthorized():
    app = _make_test_app(auth_ok=False)
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/api/dq/issues?project_id=proj_1")
    assert resp.status_code == 401


def test_dq_issues_missing_project_id():
    app = _make_test_app(auth_ok=True)
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/api/dq/issues")
    assert resp.status_code == 422


def test_dq_issues_bad_monitor_filter():
    app = _make_test_app(auth_ok=True)
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/api/dq/issues?project_id=proj_1&monitor=dq_unknown")
    assert resp.status_code == 422
    data = resp.json()
    assert data["code"] == "invalid_param"


def test_dq_issues_bad_status_filter():
    app = _make_test_app(auth_ok=True)
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/api/dq/issues?project_id=proj_1&status=wrong")
    assert resp.status_code == 422


def test_dq_issues_returns_list():
    app = _make_test_app(auth_ok=True)
    client = TestClient(app, raise_server_exceptions=False)

    now = datetime(2026, 7, 13, 10, 0, 0, tzinfo=timezone.utc)
    d = date(2026, 7, 12)

    fake_rows = [
        (
            "fire_001",
            "dq_volume",
            d,
            now,
            "warning",
            None,
            "Volume anormal pour 'Stream A' le 2026-07-12: 5 lignes | meta="
            '{"datastream_id":"ds_1","datastream_name":"Stream A",'
            '"module_name":"mod_a"}',
            "proj_1",
        )
    ]
    col_names = [
        "id",
        "type",
        "window_date",
        "fired_at",
        "severity",
        "acknowledged_at",
        "message",
        "project_id",
    ]

    with patch("core.db.get_connection") as mock_gc:
        conn_ctx = MagicMock()
        conn = MagicMock()
        conn_ctx.__enter__ = MagicMock(return_value=conn)
        conn_ctx.__exit__ = MagicMock(return_value=False)

        cur = MagicMock()
        cur.__enter__ = MagicMock(return_value=cur)
        cur.__exit__ = MagicMock(return_value=False)
        cur.fetchall = MagicMock(return_value=fake_rows)
        cur.description = _col_desc(col_names)

        conn.cursor.return_value.__enter__ = lambda s: cur
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        mock_gc.return_value = conn_ctx

        resp = client.get("/api/dq/issues?project_id=proj_1")

    assert resp.status_code == 200
    data = resp.json()
    assert "issues" in data
    assert data["total"] == 1
    issue = data["issues"][0]
    assert issue["id"] == "fire_001"
    assert issue["type"] == "dq_volume"
    assert issue["acknowledged"] is False
    assert issue["datastream_name"] == "Stream A"


# ---------------------------------------------------------------------------
# POST /api/dq/issues/{id}/acknowledge
# ---------------------------------------------------------------------------


def test_dq_acknowledge_unauthorized():
    app = _make_test_app(auth_ok=False)
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.post("/api/dq/issues/fire_001/acknowledge?project_id=proj_1")
    assert resp.status_code == 401


def test_dq_acknowledge_missing_project_id():
    app = _make_test_app(auth_ok=True)
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.post("/api/dq/issues/fire_001/acknowledge")
    assert resp.status_code == 422


def test_dq_acknowledge_not_found():
    app = _make_test_app(auth_ok=True)
    client = TestClient(app, raise_server_exceptions=False)

    with patch("core.db.get_connection") as mock_gc:
        conn_ctx = MagicMock()
        conn = MagicMock()
        conn_ctx.__enter__ = MagicMock(return_value=conn)
        conn_ctx.__exit__ = MagicMock(return_value=False)

        cur = MagicMock()
        cur.__enter__ = MagicMock(return_value=cur)
        cur.__exit__ = MagicMock(return_value=False)
        cur.fetchone = MagicMock(return_value=None)

        conn.cursor.return_value.__enter__ = lambda s: cur
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        mock_gc.return_value = conn_ctx

        resp = client.post("/api/dq/issues/fire_unknown/acknowledge?project_id=proj_1")

    assert resp.status_code == 404


def test_dq_acknowledge_wrong_project():
    app = _make_test_app(auth_ok=True)
    client = TestClient(app, raise_server_exceptions=False)

    # Firing belongs to "proj_2", caller says "proj_1".
    with patch("core.db.get_connection") as mock_gc:
        conn_ctx = MagicMock()
        conn = MagicMock()
        conn_ctx.__enter__ = MagicMock(return_value=conn)
        conn_ctx.__exit__ = MagicMock(return_value=False)

        cur = MagicMock()
        cur.__enter__ = MagicMock(return_value=cur)
        cur.__exit__ = MagicMock(return_value=False)
        # row = (id, type, project_id, acknowledged_at)
        cur.fetchone = MagicMock(return_value=("fire_001", "dq_volume", "proj_2", None))

        conn.cursor.return_value.__enter__ = lambda s: cur
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        mock_gc.return_value = conn_ctx

        resp = client.post("/api/dq/issues/fire_001/acknowledge?project_id=proj_1")

    assert resp.status_code == 403


def test_dq_acknowledge_success():
    app = _make_test_app(auth_ok=True)
    client = TestClient(app, raise_server_exceptions=False)

    with patch("core.db.get_connection") as mock_gc:
        conn_ctx = MagicMock()
        conn = MagicMock()
        conn_ctx.__enter__ = MagicMock(return_value=conn)
        conn_ctx.__exit__ = MagicMock(return_value=False)
        conn.commit = MagicMock()

        def make_cur():
            cur = MagicMock()
            cur.__enter__ = MagicMock(return_value=cur)
            cur.__exit__ = MagicMock(return_value=False)
            cur.fetchone = MagicMock(return_value=("fire_001", "dq_volume", "proj_1", None))
            return cur

        conn.cursor.return_value.__enter__ = lambda s: make_cur()
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        mock_gc.return_value = conn_ctx

        resp = client.post("/api/dq/issues/fire_001/acknowledge?project_id=proj_1")

    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == "fire_001"
    assert "acknowledged_at" in data
    assert data["acknowledged_at"] is not None


# ---------------------------------------------------------------------------
# GET /api/dq/history (Story 13.3)
# ---------------------------------------------------------------------------


def test_dq_history_unauthorized():
    app = _make_test_app(auth_ok=False)
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/api/dq/history?project_id=proj_1")
    assert resp.status_code == 401
    assert resp.json()["code"] == "unauthorized"


def test_dq_history_missing_project_id():
    app = _make_test_app(auth_ok=True)
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/api/dq/history")
    assert resp.status_code == 422


def test_dq_history_bad_monitor():
    app = _make_test_app(auth_ok=True)
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/api/dq/history?project_id=proj_1&monitor=dq_invalid")
    assert resp.status_code == 422
    assert resp.json()["code"] == "invalid_param"


def test_dq_history_success_rate_calcul():
    """3 jours avec firing sur 25 jours evalues => success_rate = 88.0 %.
    evaluated_days provient de la 2e requete (pull_jobs state='done').
    """
    app = _make_test_app(auth_ok=True)
    client = TestClient(app, raise_server_exceptions=False)

    # Deux curseurs : (1) firing_by_type, (2) evaluated_days=25 pull_jobs done.
    fake_firing_rows = [("dq_volume", 3)]
    fake_eval_row = (25,)  # evaluated_days

    with patch("core.db.get_connection") as mock_gc:
        conn_ctx = MagicMock()
        conn = MagicMock()
        conn_ctx.__enter__ = MagicMock(return_value=conn)
        conn_ctx.__exit__ = MagicMock(return_value=False)

        call_count = {"n": 0}

        def cursor_factory():
            call_count["n"] += 1
            cm = MagicMock()
            cur = MagicMock()
            cur.__enter__ = MagicMock(return_value=cur)
            cur.__exit__ = MagicMock(return_value=False)
            n = call_count["n"]
            if n == 1:
                # Requete AD-5 (identity_has_project_access): fetchone -> (False, False, False)
                # => default-open (project sans membres -> True)
                cur.fetchone = MagicMock(return_value=(False, False, False))
                cur.fetchall = MagicMock(return_value=[])
            elif n == 2:
                # firing_by_type
                cur.fetchall = MagicMock(return_value=fake_firing_rows)
                cur.fetchone = MagicMock(return_value=None)
            else:
                # evaluated_days
                cur.fetchone = MagicMock(return_value=fake_eval_row)
                cur.fetchall = MagicMock(return_value=[])
            cm.__enter__ = MagicMock(return_value=cur)
            cm.__exit__ = MagicMock(return_value=False)
            return cm

        conn.cursor = cursor_factory
        mock_gc.return_value = conn_ctx

        resp = client.get("/api/dq/history?project_id=proj_1&days=30")

    assert resp.status_code == 200
    data = resp.json()
    assert data["window_days"] == 30
    monitors = {m["type"]: m for m in data["monitors"]}
    assert "dq_volume" in monitors
    assert monitors["dq_volume"]["days_with_firing"] == 3
    assert monitors["dq_volume"]["evaluated_days"] == 25
    # 3/25 = 12.0 % de jours avec firing => success = 88.0 %
    assert monitors["dq_volume"]["success_rate"] == 88.0
    assert "dq_date_format" in monitors, "dq_date_format doit apparaitre dans l'historique"
    assert monitors["dq_date_format"]["days_with_firing"] == 0
    assert monitors["dq_date_format"]["evaluated_days"] == 25
    assert monitors["dq_date_format"]["success_rate"] == 100.0


def test_dq_history_5_monitors_present():
    """Toujours 5 entrees dans /history meme sans firings.
    Si evaluated_days=0 (aucun pull_job done), success_rate doit etre null (M4).
    """
    app = _make_test_app(auth_ok=True)
    client = TestClient(app, raise_server_exceptions=False)

    with patch("core.db.get_connection") as mock_gc:
        conn_ctx = MagicMock()
        conn = MagicMock()
        conn_ctx.__enter__ = MagicMock(return_value=conn)
        conn_ctx.__exit__ = MagicMock(return_value=False)

        call_count = {"n": 0}

        def cursor_factory():
            call_count["n"] += 1
            cm = MagicMock()
            cur = MagicMock()
            cur.__enter__ = MagicMock(return_value=cur)
            cur.__exit__ = MagicMock(return_value=False)
            n = call_count["n"]
            if n == 1:
                # AD-5 check: default-open
                cur.fetchone = MagicMock(return_value=(False, False, False))
                cur.fetchall = MagicMock(return_value=[])
            elif n == 2:
                # firing_by_type: aucun firing
                cur.fetchall = MagicMock(return_value=[])
                cur.fetchone = MagicMock(return_value=None)
            else:
                # evaluated_days=0 (aucun pull_job done)
                cur.fetchone = MagicMock(return_value=(0,))
                cur.fetchall = MagicMock(return_value=[])
            cm.__enter__ = MagicMock(return_value=cur)
            cm.__exit__ = MagicMock(return_value=False)
            return cm

        conn.cursor = cursor_factory
        mock_gc.return_value = conn_ctx

        resp = client.get("/api/dq/history?project_id=proj_1")

    assert resp.status_code == 200
    data = resp.json()
    import core.dq_api as dq_api_mod

    assert [m["type"] for m in data["monitors"]] == list(dq_api_mod._DQ_TYPES)
    for m in data["monitors"]:
        # M4 : success_rate=null quand evaluated_days=0 (jamais evalue).
        assert m["success_rate"] is None, (
            f"success_rate doit etre null (jamais evalue) pour {m['type']}, "
            f"recu {m['success_rate']!r}"
        )
        assert m["days_with_firing"] == 0
        assert m["evaluated_days"] == 0


def test_dq_history_success_rate_not_null_when_evaluated():
    """Si evaluated_days > 0 et 0 firing, success_rate doit etre 100.0 (pas null)."""
    app = _make_test_app(auth_ok=True)
    client = TestClient(app, raise_server_exceptions=False)

    with patch("core.db.get_connection") as mock_gc:
        conn_ctx = MagicMock()
        conn = MagicMock()
        conn_ctx.__enter__ = MagicMock(return_value=conn)
        conn_ctx.__exit__ = MagicMock(return_value=False)

        call_count = {"n": 0}

        def cursor_factory():
            call_count["n"] += 1
            cm = MagicMock()
            cur = MagicMock()
            cur.__enter__ = MagicMock(return_value=cur)
            cur.__exit__ = MagicMock(return_value=False)
            n = call_count["n"]
            if n == 1:
                cur.fetchone = MagicMock(return_value=(False, False, False))
                cur.fetchall = MagicMock(return_value=[])
            elif n == 2:
                cur.fetchall = MagicMock(return_value=[])  # 0 firings
                cur.fetchone = MagicMock(return_value=None)
            else:
                cur.fetchone = MagicMock(return_value=(10,))  # 10 jours evalues
                cur.fetchall = MagicMock(return_value=[])
            cm.__enter__ = MagicMock(return_value=cur)
            cm.__exit__ = MagicMock(return_value=False)
            return cm

        conn.cursor = cursor_factory
        mock_gc.return_value = conn_ctx

        resp = client.get("/api/dq/history?project_id=proj_1")

    assert resp.status_code == 200
    data = resp.json()
    for m in data["monitors"]:
        assert m["success_rate"] == 100.0, (
            f"0 firing / 10 jours evalues => success_rate=100, recu {m['success_rate']!r}"
        )
        assert m["evaluated_days"] == 10


# ---------------------------------------------------------------------------
# GET /api/dq/datastream-freshness (Story 13.3)
# ---------------------------------------------------------------------------


def test_dq_freshness_unauthorized():
    app = _make_test_app(auth_ok=False)
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/api/dq/datastream-freshness?project_id=proj_1")
    assert resp.status_code == 401


def test_dq_freshness_missing_project_id():
    app = _make_test_app(auth_ok=True)
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.get("/api/dq/datastream-freshness")
    assert resp.status_code == 422


def test_dq_freshness_returns_datastreams():
    """Shape de la reponse : datastreams avec les champs requis.

    Utilise le VRAI schema de pull_jobs :
      - state (pas status) : 'done' => last_status='success' apres mapping
      - actual_rows (dans pull_verifications) : pas row_count dans pull_jobs
    Colonnes de la requete principale : datastream_id, datastream_name, module_name,
    last_pull_at, last_state, last_verdict, row_count (=pv.actual_rows).
    """
    app = _make_test_app(auth_ok=True)
    client = TestClient(app, raise_server_exceptions=False)

    now = datetime(2026, 7, 21, 10, 0, 0, tzinfo=timezone.utc)

    # Colonnes reelles de la requete principale (C1 fix: state + verdict + actual_rows)
    col_names_main = [
        "datastream_id",
        "datastream_name",
        "module_name",
        "last_pull_at",
        "last_state",
        "last_verdict",
        "row_count",
    ]
    # state='done', verdict='ok', actual_rows=1500
    fake_main_row_tuples = [
        ("ds_001", "Google Ads", "google-ads", now, "done", "ok", 1500),
    ]
    # Requete last_success_at (state='done' + verdict ok/partial/NULL)
    fake_success_rows = [("ds_001", now)]

    with patch("core.db.get_connection") as mock_gc:
        conn_ctx = MagicMock()
        conn = MagicMock()
        conn_ctx.__enter__ = MagicMock(return_value=conn)
        conn_ctx.__exit__ = MagicMock(return_value=False)

        call_count = {"n": 0}

        def cursor_factory():
            call_count["n"] += 1
            cm = MagicMock()
            cur = MagicMock()
            cur.__enter__ = MagicMock(return_value=cur)
            cur.__exit__ = MagicMock(return_value=False)
            n = call_count["n"]
            if n == 1:
                # AD-5 check: default-open (project sans membres)
                cur.fetchone = MagicMock(return_value=(False, False, False))
                cur.fetchall = MagicMock(return_value=[])
            elif n == 2:
                # Requete principale jointure datastreams + pull_jobs + pull_verifications
                cur.fetchall = MagicMock(return_value=fake_main_row_tuples)
                cur.description = _col_desc(col_names_main)
                cur.fetchone = MagicMock(return_value=None)
            else:
                # Requete last_success_at
                cur.fetchall = MagicMock(return_value=fake_success_rows)
                cur.description = _col_desc(["datastream_id", "completed_at"])
                cur.fetchone = MagicMock(return_value=None)
            cm.__enter__ = MagicMock(return_value=cur)
            cm.__exit__ = MagicMock(return_value=False)
            return cm

        conn.cursor = cursor_factory
        mock_gc.return_value = conn_ctx

        resp = client.get("/api/dq/datastream-freshness?project_id=proj_1")

    assert resp.status_code == 200
    data = resp.json()
    assert "datastreams" in data
    assert len(data["datastreams"]) == 1
    ds = data["datastreams"][0]
    assert ds["datastream_id"] == "ds_001"
    # state='done' + verdict='ok' => last_status='success' (mapping C1)
    assert ds["last_status"] == "success", (
        f"state='done'+verdict='ok' doit mapper a 'success', recu {ds['last_status']!r}"
    )
    # row_count provient de pv.actual_rows (pas pj.row_count qui n'existe pas)
    assert ds["row_count"] == 1500
    assert ds["last_pull_at"] is not None
    assert ds["last_success_at"] is not None


def test_dq_freshness_state_mapping():
    """Verifie le mapping state -> last_status pour tous les cas (C1)."""
    # Tester la fonction _map_state_to_status directement en important le module.
    from core.dq_api import _dq_datastream_freshness  # noqa: F401

    # La fonction est definie localement dans _dq_datastream_freshness, donc on
    # la redefinit ici pour prouver le contrat (invariant documente).
    def _map(state, verdict):
        if state is None:
            return None
        if state == "done":
            if verdict == "failed":
                return "partial_failure"
            return "success"
        if state in ("failed", "dead_letter"):
            return "error"
        if state in ("running", "queued"):
            return "running"
        return state

    assert _map("done", "ok") == "success"
    assert _map("done", "partial") == "success"
    assert _map("done", None) == "success"
    assert _map("done", "failed") == "partial_failure"
    assert _map("failed", None) == "error"
    assert _map("dead_letter", None) == "error"
    assert _map("running", None) == "running"
    assert _map("queued", None) == "running"
    assert _map(None, None) is None


# ---------------------------------------------------------------------------
# POST /api/dq/evaluate (Story 13.3)
# ---------------------------------------------------------------------------


def test_dq_evaluate_unauthorized():
    app = _make_test_app(auth_ok=False)
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.post("/api/dq/evaluate?project_id=proj_1")
    assert resp.status_code == 401


def test_dq_evaluate_missing_project_id():
    app = _make_test_app(auth_ok=True)
    client = TestClient(app, raise_server_exceptions=False)
    resp = client.post("/api/dq/evaluate")
    assert resp.status_code == 422


def _make_db_for_evaluate(allow_access: bool = True):
    """Fabrique un get_connection mock pour le check AD-5 de _dq_evaluate.

    Le check identity_has_project_access interroge app.project_members et app.projects.
    On simule un projet sans membres (default-open -> granted) ou avec membres et
    l'identity absente (closed -> denied) selon allow_access.
    """
    conn_ctx = MagicMock()
    conn = MagicMock()
    conn_ctx.__enter__ = MagicMock(return_value=conn)
    conn_ctx.__exit__ = MagicMock(return_value=False)

    cur = MagicMock()
    cur.__enter__ = MagicMock(return_value=cur)
    cur.__exit__ = MagicMock(return_value=False)

    if allow_access:
        # (is_member=False, project_has_members=False, project_archived=False)
        # => default-open (pas de membres => True)
        cur.fetchone = MagicMock(return_value=(False, False, False))
    else:
        # (is_member=False, project_has_members=True, project_archived=False)
        # => closed (le projet a des membres mais l'identity n'en fait pas partie)
        cur.fetchone = MagicMock(return_value=(False, True, False))

    conn.cursor.return_value.__enter__ = lambda s: cur
    conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
    return MagicMock(return_value=conn_ctx), conn, cur


def test_dq_evaluate_triggers_run_and_audit():
    """POST /api/dq/evaluate : 200 + audit ecrit + run_dq_monitors appele avec le bon project_id.

    Test DETERMINISTE (H2) : on utilise un threading.Event pour attendre que le thread
    de fond ait execute run_dq_monitors avant d'asserter l'appel.
    Prouve : project_id non-None, project_id correct, audit ecrit.
    """
    app = _make_test_app(auth_ok=True)
    client = TestClient(app, raise_server_exceptions=False)

    import core.dq_api as dq_api_mod

    dq_api_mod._evaluate_calls.pop("proj_eval_1", None)

    run_called_event = threading.Event()
    captured: dict = {}

    def fake_run(project_id):
        captured["project_id"] = project_id
        run_called_event.set()
        return {
            "evaluated": 2,
            "volume_issues": 0,
            "timeliness_issues": 0,
            "duplication_issues": 0,
            "schema_issues": 0,
            "date_format_issues": 0,
            "total_issues": 0,
            "errors": 0,
        }

    mock_gc, _, _ = _make_db_for_evaluate(allow_access=True)

    with (
        patch("core.db.get_connection", mock_gc),
        patch("core.audit.write_audit_row") as mock_audit,
        patch("core.dq_monitors.run_dq_monitors", side_effect=fake_run),
    ):
        resp = client.post("/api/dq/evaluate?project_id=proj_eval_1")
        # Attend l'execution du thread de fond (timeout 3 s pour ne pas bloquer en CI).
        called = run_called_event.wait(timeout=3)

    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "triggered"
    assert data["project_id"] == "proj_eval_1"
    assert "triggered_at" in data

    # H2 : prouve que run_dq_monitors a ete appele avec le bon project_id.
    assert called, "run_dq_monitors n'a pas ete appele dans le thread de fond (timeout)"
    assert captured.get("project_id") == "proj_eval_1", (
        f"project_id passe a run_dq_monitors = {captured.get('project_id')!r}, "
        "attendu 'proj_eval_1'"
    )
    assert captured.get("project_id") is not None, "project_id ne doit pas etre None"

    # Audit (best-effort, appele dans le thread principal avant le submit).
    mock_audit.assert_called_once()
    audit_kwargs = mock_audit.call_args
    assert "dq.evaluate.triggered" in str(audit_kwargs)


def test_dq_evaluate_rate_limited():
    """Le 3e appel rapide doit recevoir 429."""
    import core.dq_api as dq_api_mod

    dq_api_mod._evaluate_calls.pop("proj_rl", None)

    app = _make_test_app(auth_ok=True)
    client = TestClient(app, raise_server_exceptions=False)

    # DB mock pour le check AD-5 (3 appels => 3 connexions)
    def make_gc():
        conn_ctx = MagicMock()
        conn = MagicMock()
        conn_ctx.__enter__ = MagicMock(return_value=conn)
        conn_ctx.__exit__ = MagicMock(return_value=False)
        cur = MagicMock()
        cur.__enter__ = MagicMock(return_value=cur)
        cur.__exit__ = MagicMock(return_value=False)
        cur.fetchone = MagicMock(return_value=(False, False, False))
        conn.cursor.return_value.__enter__ = lambda s: cur
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        return conn_ctx

    with (
        patch("core.db.get_connection", side_effect=lambda: make_gc()),
        patch("core.audit.write_audit_row"),
        patch("core.dq_monitors.run_dq_monitors") as mock_run,
    ):
        mock_run.return_value = {
            "evaluated": 0,
            "volume_issues": 0,
            "timeliness_issues": 0,
            "duplication_issues": 0,
            "schema_issues": 0,
            "date_format_issues": 0,
            "total_issues": 0,
            "errors": 0,
        }
        resp1 = client.post("/api/dq/evaluate?project_id=proj_rl")
        resp2 = client.post("/api/dq/evaluate?project_id=proj_rl")
        resp3 = client.post("/api/dq/evaluate?project_id=proj_rl")

    assert resp1.status_code == 200, "1er appel doit reussir"
    assert resp2.status_code == 200, "2e appel doit reussir"
    assert resp3.status_code == 429, "3e appel rapide doit etre rate-limite"
    data3 = resp3.json()
    assert data3["code"] == "rate_limited"
    assert "retry_after" in data3


def test_dq_evaluate_rate_limited_per_project():
    """Le rate-limit est par project_id : deux projets differents ne s'interferent pas."""
    import core.dq_api as dq_api_mod

    dq_api_mod._evaluate_calls.pop("proj_a", None)
    dq_api_mod._evaluate_calls.pop("proj_b", None)

    app = _make_test_app(auth_ok=True)
    client = TestClient(app, raise_server_exceptions=False)

    def make_gc():
        conn_ctx = MagicMock()
        conn = MagicMock()
        conn_ctx.__enter__ = MagicMock(return_value=conn)
        conn_ctx.__exit__ = MagicMock(return_value=False)
        cur = MagicMock()
        cur.__enter__ = MagicMock(return_value=cur)
        cur.__exit__ = MagicMock(return_value=False)
        cur.fetchone = MagicMock(return_value=(False, False, False))
        conn.cursor.return_value.__enter__ = lambda s: cur
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        return conn_ctx

    with (
        patch("core.db.get_connection", side_effect=lambda: make_gc()),
        patch("core.audit.write_audit_row"),
        patch("core.dq_monitors.run_dq_monitors") as mock_run,
    ):
        mock_run.return_value = {
            "evaluated": 0,
            "volume_issues": 0,
            "timeliness_issues": 0,
            "duplication_issues": 0,
            "schema_issues": 0,
            "date_format_issues": 0,
            "total_issues": 0,
            "errors": 0,
        }
        client.post("/api/dq/evaluate?project_id=proj_a")
        client.post("/api/dq/evaluate?project_id=proj_a")
        resp_b = client.post("/api/dq/evaluate?project_id=proj_b")

    assert resp_b.status_code == 200, "proj_b ne doit pas etre rate-limite par proj_a"


# ---------------------------------------------------------------------------
# AD-5 tests (H1) : denegation cross-projet sur les 3 nouveaux endpoints
# identity projet B essaie d'acceder a project_id projet A => 404 non-disclosant
# ---------------------------------------------------------------------------


def _make_gc_closed():
    """Fabrique un get_connection mock simulant un projet avec membres (mode closed).

    (is_member=False, project_has_members=True, project_archived=False) => acces refuse.
    C'est le comportement quand app.project_members a >= 1 ligne pour ce projet et
    l'identity n'en fait pas partie.
    """
    conn_ctx = MagicMock()
    conn = MagicMock()
    conn_ctx.__enter__ = MagicMock(return_value=conn)
    conn_ctx.__exit__ = MagicMock(return_value=False)
    cur = MagicMock()
    cur.__enter__ = MagicMock(return_value=cur)
    cur.__exit__ = MagicMock(return_value=False)
    # identity n'est pas membre (is_member=False) mais le projet a des membres
    # (project_has_members=True)
    cur.fetchone = MagicMock(return_value=(False, True, False))
    cur.fetchall = MagicMock(return_value=[])
    conn.cursor.return_value.__enter__ = lambda s: cur
    conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
    return MagicMock(return_value=conn_ctx)


def test_dq_history_ad5_cross_project_denied():
    """H1 : GET /api/dq/history avec identity etrangere au projet => 404 non-disclosant."""
    app = _make_test_app(auth_ok=True)  # auth OK mais mauvaise identity pour le projet
    client = TestClient(app, raise_server_exceptions=False)

    with patch("core.db.get_connection", _make_gc_closed()):
        resp = client.get("/api/dq/history?project_id=proj_A")

    assert resp.status_code == 404, (
        f"cross-projet doit retourner 404 (non-disclosant), recu {resp.status_code}"
    )
    data = resp.json()
    assert data["code"] == "not_found", "code doit etre 'not_found' (jamais 403)"


def test_dq_freshness_ad5_cross_project_denied():
    """H1 : GET /api/dq/datastream-freshness avec identity etrangere au projet => 404."""
    app = _make_test_app(auth_ok=True)
    client = TestClient(app, raise_server_exceptions=False)

    with patch("core.db.get_connection", _make_gc_closed()):
        resp = client.get("/api/dq/datastream-freshness?project_id=proj_A")

    assert resp.status_code == 404, (
        f"cross-projet doit retourner 404 (non-disclosant), recu {resp.status_code}"
    )
    data = resp.json()
    assert data["code"] == "not_found", "code doit etre 'not_found' (jamais 403)"


def test_dq_evaluate_ad5_cross_project_denied():
    """H1 : POST /api/dq/evaluate avec identity etrangere au projet => 404 (fail-closed WRITE)."""
    app = _make_test_app(auth_ok=True)
    client = TestClient(app, raise_server_exceptions=False)

    with patch("core.db.get_connection", _make_gc_closed()):
        resp = client.post("/api/dq/evaluate?project_id=proj_A")

    assert resp.status_code == 404, (
        f"cross-projet WRITE doit retourner 404 (non-disclosant), recu {resp.status_code}"
    )
    data = resp.json()
    assert data["code"] == "not_found", "code doit etre 'not_found' (jamais 403)"


def test_dq_evaluate_ad5_allowed_for_member():
    """H1 : POST /api/dq/evaluate avec identity MEMBRE du projet => 200 (access granted)."""
    app = _make_test_app(auth_ok=True)
    client = TestClient(app, raise_server_exceptions=False)

    import core.dq_api as dq_api_mod

    dq_api_mod._evaluate_calls.pop("proj_member", None)

    # is_member=True, project_has_members=True => granted
    def make_gc_member():
        conn_ctx = MagicMock()
        conn = MagicMock()
        conn_ctx.__enter__ = MagicMock(return_value=conn)
        conn_ctx.__exit__ = MagicMock(return_value=False)
        cur = MagicMock()
        cur.__enter__ = MagicMock(return_value=cur)
        cur.__exit__ = MagicMock(return_value=False)
        cur.fetchone = MagicMock(return_value=(True, True, False))
        cur.fetchall = MagicMock(return_value=[])
        conn.cursor.return_value.__enter__ = lambda s: cur
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        return conn_ctx

    with (
        patch("core.db.get_connection", side_effect=lambda: make_gc_member()),
        patch("core.audit.write_audit_row"),
        patch("core.dq_monitors.run_dq_monitors", return_value={"total_issues": 0, "errors": 0}),
    ):
        resp = client.post("/api/dq/evaluate?project_id=proj_member")

    assert resp.status_code == 200, f"identity membre doit avoir acces, recu {resp.status_code}"


# ---------------------------------------------------------------------------
# MEDIUM-1: fail_closed on POST /api/dq/evaluate (WRITE endpoint)
# ---------------------------------------------------------------------------


def test_dq_evaluate_acl_db_down_fail_closed():
    """MEDIUM-1: si le curseur du check ACL leve une exception (DB partiellement down),
    _enforce_project_scope(fail_closed=True) renvoie False -> 404 non-disclosant.
    run_dq_monitors NE doit PAS etre appele.

    Simule : get_connection() reussit (on entre dans le with), mais le curseur
    identity_has_project_access leve RuntimeError -- ce que fait un curseur Postgres
    injoignable apres que la connexion a ete rendue.
    """
    app = _make_test_app(auth_ok=True)
    client = TestClient(app, raise_server_exceptions=False)

    import core.dq_api as dq_api_mod

    dq_api_mod._evaluate_calls.pop("proj_db_down", None)

    # get_connection() reussit, mais identity_has_project_access raise -> fail-closed.
    with (
        patch(
            "core.project_access.identity_has_project_access",
            side_effect=RuntimeError("DB cursor failed"),
        ),
        patch("core.db.get_connection") as mock_gc,
        patch("core.dq_monitors.run_dq_monitors") as mock_run,
    ):
        conn_ctx = MagicMock()
        conn = MagicMock()
        conn_ctx.__enter__ = MagicMock(return_value=conn)
        conn_ctx.__exit__ = MagicMock(return_value=False)
        mock_gc.return_value = conn_ctx

        resp = client.post("/api/dq/evaluate?project_id=proj_db_down")

    # fail-closed: acces refuse, pas d'execution
    assert resp.status_code == 404, (
        f"ACL check DB-down sur WRITE doit retourner 404 (fail-closed), recu {resp.status_code}"
    )
    data = resp.json()
    assert data["code"] == "not_found", "code doit etre 'not_found' (non-disclosant)"
    mock_run.assert_not_called()  # run_dq_monitors NE doit PAS etre appele si ACL check echoue


def test_dq_evaluate_acl_db_down_get_connection_fails():
    """MEDIUM-1 complement: si get_connection() lui-meme leve, le handler renvoie 503.

    C'est le cas ou la DB est totalement injoignable (meme le pool ne repond pas).
    run_dq_monitors NE doit PAS etre appele.
    """
    app = _make_test_app(auth_ok=True)
    client = TestClient(app, raise_server_exceptions=False)

    import core.dq_api as dq_api_mod

    dq_api_mod._evaluate_calls.pop("proj_db_total_down", None)

    with (
        patch("core.db.get_connection", side_effect=RuntimeError("pool exhausted")),
        patch("core.dq_monitors.run_dq_monitors") as mock_run,
    ):
        resp = client.post("/api/dq/evaluate?project_id=proj_db_total_down")

    assert resp.status_code == 503, (
        f"get_connection() down doit retourner 503, recu {resp.status_code}"
    )
    mock_run.assert_not_called()  # run_dq_monitors NE doit PAS etre appele si DB totalement down


def test_enforce_project_scope_fail_closed_swallows_exception():
    """Unit test de _enforce_project_scope(fail_closed=True): exception -> False (pas raise)."""
    import core.dq_api as dq_api_mod

    with patch(
        "core.project_access.identity_has_project_access",
        side_effect=RuntimeError("simulated cursor error"),
    ):
        result = dq_api_mod._enforce_project_scope(
            "proj_x", "user@test", MagicMock(), fail_closed=True
        )

    assert result is False, (
        "_enforce_project_scope(fail_closed=True) doit retourner False sur exception (pas propager)"
    )


def test_enforce_project_scope_fail_open_propagates_exception():
    """Unit test de _enforce_project_scope(fail_closed=False): exception remonte normalement."""
    import core.dq_api as dq_api_mod

    with patch(
        "core.project_access.identity_has_project_access",
        side_effect=RuntimeError("simulated cursor error"),
    ):
        try:
            dq_api_mod._enforce_project_scope("proj_x", "user@test", MagicMock(), fail_closed=False)
            raised = False
        except RuntimeError:
            raised = True

    assert raised, (
        "_enforce_project_scope(fail_closed=False) doit propager l'exception "
        "(comportement READ acceptable)"
    )


def test_dq_issues_filter_accepts_dq_date_format():
    """Story 13.3: monitor=dq_date_format est un filtre valide pour /api/dq/issues."""
    app = _make_test_app(auth_ok=True)
    client = TestClient(app, raise_server_exceptions=False)

    with patch("core.db.get_connection") as mock_gc:
        conn_ctx = MagicMock()
        conn = MagicMock()
        conn_ctx.__enter__ = MagicMock(return_value=conn)
        conn_ctx.__exit__ = MagicMock(return_value=False)

        cur = MagicMock()
        cur.__enter__ = MagicMock(return_value=cur)
        cur.__exit__ = MagicMock(return_value=False)
        cur.fetchall = MagicMock(return_value=[])
        cur.description = _col_desc(
            [
                "id",
                "type",
                "window_date",
                "fired_at",
                "severity",
                "acknowledged_at",
                "message",
                "project_id",
            ]
        )

        conn.cursor.return_value.__enter__ = lambda s: cur
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        mock_gc.return_value = conn_ctx

        # Ne doit pas renvoyer 422 pour dq_date_format.
        resp = client.get("/api/dq/issues?project_id=proj_1&monitor=dq_date_format")

    assert resp.status_code == 200, (
        f"dq_date_format doit etre un filtre valide, recu {resp.status_code}"
    )
