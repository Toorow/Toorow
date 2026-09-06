"""Tests for notebooks REST CRUD endpoints (Story 6.5, AC5, AC8).

Covers (from AC8):
  - test_list_notebooks_project_scoped: two projects -> GET returns only own notebooks.
  - test_patch_notebook_updates_title: PATCH -> title updated, updated_at bumped.
  - test_delete_notebook_cascades_runs: DELETE notebook -> notebook_runs also gone.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

from starlette.applications import Starlette
from starlette.routing import Mount
from starlette.testclient import TestClient

# ---------------------------------------------------------------------------
# Test app factory
# ---------------------------------------------------------------------------


def _make_app():
    """Create a minimal Starlette test app mounting only the admin_api router."""
    from core.admin_api import router

    return Starlette(routes=[Mount("/", app=router)])


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def client():
    """Test client with auth disabled."""
    os.environ["TOOROW_AUTH_MODE"] = "disabled"
    from core import api_auth

    api_auth.reset_verifier_cache()
    app = _make_app()
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c
    os.environ.pop("TOOROW_AUTH_MODE", None)
    api_auth.reset_verifier_cache()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fake_ts():
    return datetime(2026, 7, 12, 12, 0, 0, tzinfo=timezone.utc)


def _make_mock_conn(cursor_mock=None):
    """Build a mock psycopg connection usable as a context manager."""
    if cursor_mock is None:
        cursor_mock = MagicMock()
    conn_mock = MagicMock()
    conn_mock.__enter__ = MagicMock(return_value=conn_mock)
    conn_mock.__exit__ = MagicMock(return_value=False)
    cursor_cm = MagicMock()
    cursor_cm.__enter__ = MagicMock(return_value=cursor_mock)
    cursor_cm.__exit__ = MagicMock(return_value=False)
    conn_mock.cursor = MagicMock(return_value=cursor_cm)
    return conn_mock


# ---------------------------------------------------------------------------
# test_list_notebooks_project_scoped
# ---------------------------------------------------------------------------


def test_list_notebooks_project_scoped(client):
    """GET /api/notebooks?project_id=proj_A returns only proj_A notebooks."""
    proj_a_row = (
        "nb_A1", "Notebook A1", "adhoc", "last_30d",
        _fake_ts(), None, None,
    )
    cursor_mock = MagicMock()
    cursor_mock.fetchall.return_value = [proj_a_row]
    cursor_mock.description = [
        ("id",), ("title",), ("report_ref",), ("window_rule",),
        ("created_at",), ("last_run_at",), ("last_run_status",),
    ]
    conn_mock = _make_mock_conn(cursor_mock)

    with patch("core.db.get_connection", return_value=conn_mock):
        resp = client.get("/api/notebooks?project_id=proj_A")

    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    assert len(data) == 1
    assert data[0]["id"] == "nb_A1"
    # Verify the WHERE clause filters by project_id
    sql_called = cursor_mock.execute.call_args[0][0]
    assert "project_id" in sql_called
    params_called = cursor_mock.execute.call_args[0][1]
    assert "proj_A" in params_called


def test_list_notebooks_missing_project_id(client):
    """GET /api/notebooks without project_id -> 400."""
    resp = client.get("/api/notebooks")
    assert resp.status_code == 400


def test_list_notebooks_returns_last_run_status(client):
    """GET /api/notebooks includes last_run_at and last_run_status."""
    row = (
        "nb_RUN1", "Running NB", "adhoc", "last_7d",
        _fake_ts(), _fake_ts(), "success",
    )
    cursor_mock = MagicMock()
    cursor_mock.fetchall.return_value = [row]
    cursor_mock.description = [
        ("id",), ("title",), ("report_ref",), ("window_rule",),
        ("created_at",), ("last_run_at",), ("last_run_status",),
    ]
    conn_mock = _make_mock_conn(cursor_mock)

    with patch("core.db.get_connection", return_value=conn_mock):
        resp = client.get("/api/notebooks?project_id=proj_X")

    assert resp.status_code == 200
    data = resp.json()
    assert data[0]["last_run_status"] == "success"
    assert data[0]["last_run_at"] is not None


# ---------------------------------------------------------------------------
# test_patch_notebook_updates_title
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# PATCH et DELETE : le magasin herite ne prend plus d'ecriture (2026-08-22, 67.23)
# ---------------------------------------------------------------------------
#
# CE QUE CES CINQ TESTS TENAIENT, et pourquoi ce n'est plus vrai. Ils prouvaient
# qu'un PATCH met a jour un titre, qu'un DELETE cascade sur les runs, et que les
# deux rendent 404 sur un identifiant absent. Les trois proprietes etaient
# justes ; l'objet qu'elles gardaient ne doit plus exister.
#
# `app.notebooks` avait UN seul ecrivain -- l'outil MCP `save_notebook`, il n'y a
# aucune route REST de creation -- et les ecrans canoniques d'Analyze lisent
# `app.analysis_notebooks`. Un modele creait donc un objet reel, audite, que rien
# ne montrait. La porte MCP a bascule sur le magasin gouverne ; ces ecritures-ci
# sont l'autre moitie de l'acte.
#
# LES LECTURES RESTENT, et leurs tests avec elles : `_list_notebooks`,
# `_get_notebook` et `_export_notebook_html` sont intacts et tiennent AC12
# << remain readable >>.


def _refusal_of(resp) -> dict:
    assert resp.status_code == 409, resp.text
    return resp.json()


def test_a_patch_is_refused_and_names_the_surface_that_works(client):
    body = _refusal_of(client.patch("/api/notebooks/nb_TEST", json={"title": "x"}))
    assert body["code"] == "legacy_store_is_read_only"
    # LA PHRASE NOMME UN GESTE, pas une cause technique : la personne doit savoir
    # ou aller, pas de quelle table il s'agit.
    assert "Analyze" in body["message"]
    assert "app.notebooks" not in body["message"]


def test_a_delete_is_refused_with_the_same_words(client):
    """Un seul refus pour les trois portes -- deux phrases seraient deux regles."""
    patched = _refusal_of(client.patch("/api/notebooks/nb_TEST", json={"title": "x"}))
    deleted = _refusal_of(client.delete("/api/notebooks/nb_TEST"))
    scheduled = _refusal_of(
        client.patch("/api/notebooks/nb_TEST/schedule", json={"scheduled": True})
    )
    assert patched == deleted == scheduled


def test_the_refusal_needs_no_database_at_all(client):
    """Il tombe AVANT toute lecture, et c'est ce qui le rend indistinguable.

    Aucune doublure de connexion n'est posee ici : si la porte ouvrait une
    connexion, ce test echouerait sur la vraie base ou sur son absence. Qu'il
    passe est la preuve que le refus ne depend d'aucune ligne -- donc qu'un
    identifiant reel et un identifiant invente recoivent exactement la meme
    reponse.
    """
    real = client.delete("/api/notebooks/nb_TEST")
    absent = client.delete("/api/notebooks/nb_DOES_NOT_EXIST")
    assert real.status_code == absent.status_code == 409
    assert real.json() == absent.json()


def test_the_read_doors_are_untouched(client):
    """AC12 << remain readable >> : refuser les ecritures n'a ferme aucune lecture."""
    from core import notebooks_api

    paths = {
        (route.path, frozenset(route.methods - {"HEAD"}))
        for route in list(notebooks_api.NOTEBOOKS_ROUTES_1)
        + list(notebooks_api.NOTEBOOKS_ROUTES_2)
    }
    assert ("/api/notebooks", frozenset({"GET"})) in paths
    assert ("/api/notebooks/{notebook_id}", frozenset({"GET"})) in paths
    assert (
        "/api/notebooks/{notebook_id}/runs/{run_id}/export/html",
        frozenset({"GET"}),
    ) in paths
    # Et le magasin herite ne porte plus AUCUNE ecriture, mesure sur la source.
    import inspect

    source = inspect.getsource(notebooks_api)
    for verb in ("INSERT INTO app.notebooks", "UPDATE app.notebooks", "DELETE FROM app.notebooks"):
        assert verb not in source, f"une ecriture subsiste : {verb}"


class TestSlideXssEscaping:
    """review-epic-6 F-3: stored values must never render as live HTML."""

    def test_data_table_escapes_script_tags(self):
        from core.notebooks_api import _build_data_table_html  # noqa: PLC0415

        evil = {"data": {"metrics": {"<script>alert(1)</script>": "<img onerror=x>"}}}
        html_out = _build_data_table_html(evil)
        assert "<script>" not in html_out
        assert "&lt;script&gt;" in html_out
        assert "<img" not in html_out
