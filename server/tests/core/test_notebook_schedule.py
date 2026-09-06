"""La programmation d'un Notebook, et le pas nocturne qui la tire.

CE QUE CE FICHIER TENAIT JUSQU'AU 2026-08-22, et pourquoi la moitie est partie.
Il gardait la porte `PATCH /api/notebooks/{id}/schedule` (elle ecrivait
`app.notebooks.scheduled`) et la boucle `_run_due_notebooks` qui lisait ce
drapeau. Les deux servaient un magasin HERITE : son seul ecrivain etait l'outil
MCP `save_notebook` -- il n'existe aucune route REST de creation -- et les ecrans
canoniques d'Analyze lisent `app.analysis_notebooks`. Un modele programmait donc
un objet que rien ne montrait, et la boucle le tirait dans le vide.

`save_notebook` et `run_notebook` sont passes au magasin gouverne, la porte de
programmation heritee refuse, et la boucle heritee a ete retiree -- c'est la
bascule AC12 que le docstring de `_dispatch_due_canonical_notebooks` nommait
comme sa condition de sortie.

CE QUI RESTE TENU, ET C'EST PLUS QU'AVANT :
  - le refus de la porte heritee, et le fait qu'il ne depend d'aucune ligne ;
  - que le pas nocturne appelle TOUJOURS le tir des Notebooks, a sa place dans
    l'ordre -- la propriete que `test_run_due_notebooks_called_in_nightly`
    gardait, et qui n'a rien a voir avec le magasin ;
  - que ce tir passe par le service gouverne et par aucun autre.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

from starlette.applications import Starlette  # noqa: E402
from starlette.routing import Mount  # noqa: E402
from starlette.testclient import TestClient  # noqa: E402

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


def _fake_ts():
    return datetime(2026, 7, 12, 12, 0, 0, tzinfo=timezone.utc)


def _fetchone_sequence(notebook_row):
    """Answer the OWNER-PROJECT query, then the UPDATE ... RETURNING row.

    AI-120. These two tests went red on main answering ONE fixed tuple to every
    `fetchone`. That was enough until story 7.4 (AC7, AI-38) put a scope check in
    front of the mutation: the handler now asks `SELECT project_id FROM
    app.notebooks WHERE id = %s` FIRST, read `row[0]` -- which was "nb_TEST",
    the notebook id -- treated it as the owning project, and refused the
    cross-scope write with a 404. The guarantee under test is real; the mock had
    simply stopped modelling the handler.

    Answering per query rather than per test also means a future pre-check will
    fail loudly here instead of silently reading the wrong column.
    """
    rows = iter([(notebook_row[1],), notebook_row])
    return lambda *_args, **_kwargs: next(rows, notebook_row)


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
# test_schedule_notebook_sets_flag (AC6)
# ---------------------------------------------------------------------------


def test_the_legacy_schedule_door_refuses_and_names_where_it_lives_now(client):
    """Elle ecrivait `app.notebooks.scheduled`, que plus aucune boucle ne lit."""
    resp = client.patch("/api/notebooks/nb_TEST/schedule", json={"scheduled": True})
    assert resp.status_code == 409, resp.text
    body = resp.json()
    assert body["code"] == "legacy_store_is_read_only"
    assert "Analyze" in body["message"]


def test_the_schedule_refusal_reads_no_row(client):
    """Aucune doublure de connexion : si la porte lisait, ce test tomberait.

    C'est aussi ce qui rend le refus indistinguable entre un identifiant reel et
    un identifiant invente -- comparer deux refus n'apprend rien.
    """
    real = client.patch("/api/notebooks/nb_TEST/schedule", json={"scheduled": True})
    absent = client.patch("/api/notebooks/nb_NOPE/schedule", json={"scheduled": False})
    assert real.status_code == absent.status_code == 409
    assert real.json() == absent.json()


def test_run_due_notebooks_called_in_nightly():
    """run_nightly_steps() must call _run_due_notebooks before _run_due_briefings.

    Updated for Story 6.7 (AC4): run_due_briefings is now the LAST step (6th).
    run_due_notebooks is the 5th step (penultimate). Both must run.
    """
    from datetime import date

    # Patch all individual step functions + _run_due_notebooks + _run_due_briefings
    call_order: list[str] = []

    def fake_dispatch(**kwargs):
        call_order.append("dispatch_nightly")

    def fake_alert():
        call_order.append("alert_check")

    def fake_business():
        call_order.append("business_alert_check")

    def fake_anomaly():
        call_order.append("anomaly_alert_check")

    def fake_due_notebooks():
        call_order.append("run_due_notebooks")

    def fake_due_briefings(nightly_run_id):
        call_order.append("run_due_briefings")

    with (
        patch("core.scheduler.dispatch_nightly", side_effect=fake_dispatch),
        patch("core.scheduler._run_alert_check", side_effect=fake_alert),
        patch("core.scheduler._run_business_alert_check", side_effect=fake_business),
        patch("core.scheduler._run_anomaly_alert_check", side_effect=fake_anomaly),
        patch("core.scheduler._run_due_notebooks", side_effect=fake_due_notebooks),
        patch("core.scheduler._run_due_briefings", side_effect=fake_due_briefings),
    ):
        from core.scheduler import run_nightly_steps

        run_nightly_steps(date.today())

    # Assert notebooks ran before briefings, and briefings is last
    assert "dispatch_nightly" in call_order
    assert "run_due_notebooks" in call_order
    assert "run_due_briefings" in call_order
    notebooks_pos = call_order.index("run_due_notebooks")
    briefings_pos = call_order.index("run_due_briefings")
    assert notebooks_pos < briefings_pos, (
        f"run_due_notebooks must come before run_due_briefings; got: {call_order}"
    )
    assert call_order[-1] == "run_due_briefings", (
        f"run_due_briefings must be the LAST step (Story 6.7); got: {call_order}"
    )


# ---------------------------------------------------------------------------
# test_scheduled_notebook_failure_does_not_block_next (AC6)
# ---------------------------------------------------------------------------


def test_the_nightly_step_reaches_the_governed_dispatch_and_nothing_else():
    """Ce que `_run_due_notebooks` EST devenu, et ce qu'il n'est plus.

    Il portait une boucle : lire `app.notebooks WHERE scheduled`, appeler
    `run_notebook_direct` par notebook, isoler chaque echec, poser une meta-alerte.
    Il ne porte plus qu'un appel. Le test lit la SOURCE plutot que de se fier a un
    mock, parce que la propriete en cause est << il n'existe plus de second
    chemin >> et qu'un mock ne peut pas la nier.
    """
    import inspect

    from core import scheduler

    body = inspect.getsource(scheduler._run_due_notebooks)
    assert "_dispatch_due_canonical_notebooks()" in body
    # Plus aucune lecture du magasin herite dans le corps.
    assert "FROM app.notebooks" not in body
    assert "run_notebook_direct" not in body.split('"""')[-1], (
        "le second moteur d'execution est encore appele"
    )


def test_a_failing_dispatch_does_not_take_the_nightly_step_down():
    """La resilience que la boucle heritee portait, tenue par le tir gouverne.

    `test_scheduled_notebook_failure_does_not_block_next` prouvait qu'un notebook
    en echec n'empechait pas le suivant. Le service gouverne isole chaque
    Notebook par SAVEPOINT et rend un rapport ; ce qui reste a prouver ici est le
    dernier maillon -- que le pas nocturne survit a un tir qui LEVE, et pose la
    meta-alerte plutot que d'emporter la nuit avec lui.
    """
    from core import scheduler

    alerts: list[tuple[str, str]] = []
    with (
        patch(
            "core.analyze_artifacts.dispatch_due_notebook_schedules",
            side_effect=RuntimeError("dispatch exploded"),
        ),
        patch("core.db.get_connection", return_value=_make_mock_conn(MagicMock())),
        patch.object(
            scheduler, "_insert_meta_alert", side_effect=lambda *a: alerts.append(a)
        ),
    ):
        scheduler._run_due_notebooks()  # ne doit PAS lever

    assert alerts, "un tir qui explose doit poser une meta-alerte"
    assert alerts[0][0] == "dispatch_canonical_notebooks"
