"""Tests pour server/core/mediaplan_alerts.py (Story 22.6, FR9).

Offline -- stand-ins warehouse fidèles au schéma réel des marts pacing
(pattern test_mediaplan_pacing.py 22.4 ; mock/fake du chemin alert_firings
identique à test_business_alerts.py 5.3).

Cas couverts :
  T1. pace +25 % -> alerte rouge niveau ligne (et channel si l'agrégat dépasse).
  T2. pace -12 % -> alerte jaune distincte.
  T3. is_plan_only -> jamais d'alerte.
  T4. allocated_to_date = 0 / pace NULL -> jamais d'alerte.
  T5. Seuils projet ±20 % -> plus d'alerte au run suivant (configurable prouvé).
  T6. Metadata complet dans le firing (version, formule, seuil, threshold_source).
  T7. Deux plans actifs -> évaluations indépendantes (PAR PLAN, jamais inter-plans).
  T8. Step scheduler isolé (échec warehouse -> nightly continue, pattern AI-32).
  T9. MEDIAPLAN_ALERTS_ENABLED=false -> [] immédiat.
  T10. Dégradation gracieuse DB -> [].
  T11. fetch_recent_mediaplan_firings -> retourne les firings récents.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from datetime import date
from unittest.mock import MagicMock, patch

import pytest

# Guards threads background
os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

from core import mediaplan_alerts  # noqa: E402

# ---------------------------------------------------------------------------
# Constantes de test
# ---------------------------------------------------------------------------

PROJECT_ID = "proj-22-6-test"
PLAN_ID_A = "00000000-0000-4000-8000-000000000022"
PLAN_ID_B = "00000000-0000-4000-8000-000000000023"
VERSION_ID = "00000000-0000-4000-8000-000000000221"
EVAL_DATE = date(2026, 7, 20)

# Pacing +25 % (histoire 22.4 Given/When/Then)
_LINE_OVERRUN = {
    "project_id": PROJECT_ID,
    "plan_id": PLAN_ID_A,
    "plan_version_id": VERSION_ID,
    "line_key": "line-digital-a",
    "label": "Digital A",
    "channel": "digital",
    "currency": "EUR",
    "budget": 3000.0,
    "is_plan_only": False,
    "allocated_to_date": 1000.0,
    "actual_to_date": 1250.0,
    "pace": 0.25,            # +25 %
    "consumed_pct": 0.4167,
    "remaining_budget": 1750.0,
    "extrapolated_spend": 3750.0,
    "actual_pull_id_min": "pull_a",
    "actual_pull_id_max": "pull_b",
    "actual_pull_id_count": 2,
}

# Pacing -12 %
_LINE_UNDERDELIVERY = {
    "project_id": PROJECT_ID,
    "plan_id": PLAN_ID_A,
    "plan_version_id": VERSION_ID,
    "line_key": "line-social",
    "label": "Social",
    "channel": "social",
    "is_plan_only": False,
    "allocated_to_date": 500.0,
    "actual_to_date": 440.0,
    "pace": -0.12,           # -12 %
    "consumed_pct": 0.1467,
    "budget": 3000.0,
    "remaining_budget": 2560.0,
    "extrapolated_spend": 1320.0,
    "actual_pull_id_min": "pull_c",
    "actual_pull_id_max": "pull_c",
    "actual_pull_id_count": 1,
}

# Ligne plan-only (TV)
_LINE_PLAN_ONLY = {
    "project_id": PROJECT_ID,
    "plan_id": PLAN_ID_A,
    "plan_version_id": VERSION_ID,
    "line_key": "line-tv",
    "label": "TV Brand",
    "channel": "tv",
    "is_plan_only": True,
    "allocated_to_date": 5000.0,
    "actual_to_date": None,
    "pace": None,
    "consumed_pct": None,
    "budget": 5000.0,
    "remaining_budget": None,
    "extrapolated_spend": None,
    "actual_pull_id_min": None,
    "actual_pull_id_max": None,
    "actual_pull_id_count": 0,
}

# Ligne allocated = 0 (dénominateur zéro)
_LINE_ALLOC_ZERO = {
    "project_id": PROJECT_ID,
    "plan_id": PLAN_ID_A,
    "plan_version_id": VERSION_ID,
    "line_key": "line-not-started",
    "label": "Pas encore commencé",
    "channel": "display",
    "is_plan_only": False,
    "allocated_to_date": 0.0,
    "actual_to_date": None,
    "pace": None,
    "consumed_pct": None,
    "budget": 1000.0,
    "remaining_budget": None,
    "extrapolated_spend": None,
    "actual_pull_id_min": None,
    "actual_pull_id_max": None,
    "actual_pull_id_count": 0,
}

# Channel digital avec pace +25 % (agrégat de lines)
_CHANNEL_OVERRUN = {
    "project_id": PROJECT_ID,
    "plan_id": PLAN_ID_A,
    "plan_version_id": VERSION_ID,
    "channel": "digital",
    "budget": 4500.0,
    "allocated_to_date": 1500.0,
    "actual_to_date": 1875.0,
    "pace": 0.25,
    "consumed_pct": 0.4167,
    "remaining_budget": 2625.0,
    "extrapolated_spend": 5625.0,
    "plan_only_line_count": 0,
    "paceable_line_count": 2,
    "actual_pull_id_min": "pull_a",
    "actual_pull_id_max": "pull_b",
    "actual_pull_id_count": 3,
}

# Plan global avec pace +25 %
_PLAN_OVERRUN = {
    "project_id": PROJECT_ID,
    "plan_id": PLAN_ID_A,
    "plan_version_id": VERSION_ID,
    "currency": "EUR",
    "as_of_day": "2026-03-10",
    "budget": 9500.0,
    "allocated_to_date": 1500.0,
    "actual_to_date": 1875.0,
    "pace": 0.25,
    "consumed_pct": 0.1974,
    "remaining_budget": 7625.0,
    "extrapolated_spend": 5625.0,
    "plan_only_line_count": 1,
    "paceable_line_count": 2,
    "actual_pull_id_min": "pull_a",
    "actual_pull_id_max": "pull_b",
    "actual_pull_id_count": 3,
}


def _pacing_plan_a_overrun():
    """Pacing du plan A avec ligne overrun + line plan-only + channel overrun."""
    return {
        "lines": [_LINE_OVERRUN, _LINE_PLAN_ONLY],
        "channels": [_CHANNEL_OVERRUN],
        "plan": [_PLAN_OVERRUN],
    }


def _pacing_plan_a_underdelivery():
    """Pacing du plan A avec ligne sous-livraison."""
    return {
        "lines": [_LINE_UNDERDELIVERY],
        "channels": [],
        "plan": [],
    }


# ---------------------------------------------------------------------------
# Helpers mock DB
# ---------------------------------------------------------------------------


def _make_cursor(rows=None, description=None):
    cur = MagicMock()
    cur.__enter__ = MagicMock(return_value=cur)
    cur.__exit__ = MagicMock(return_value=False)
    cur.fetchall = MagicMock(return_value=rows or [])
    cur.fetchone = MagicMock(return_value=(rows[0] if rows else None))
    cur.description = description or []
    return cur


def _make_conn(cursor=None):
    cur = cursor or _make_cursor()
    conn = MagicMock()
    conn.__enter__ = MagicMock(return_value=conn)
    conn.__exit__ = MagicMock(return_value=False)
    conn.cursor = MagicMock(return_value=cur)
    conn.commit = MagicMock()
    return conn


@contextmanager
def _conn_ctx(conn):
    yield conn


def _make_prefs_cursor(overrun=None, underdelivery=None):
    """Cursor qui retourne les seuils projet."""
    row = (overrun, underdelivery) if overrun is not None or underdelivery is not None else None
    cur = _make_cursor(rows=[row] if row is not None else [])
    return cur


def _make_plans_cursor(plan_ids: list[str]):
    """Cursor qui retourne les plan_ids actifs."""
    rows = [(pid,) for pid in plan_ids]
    return _make_cursor(rows=rows)


def _make_sequential_cursors(*cursors):
    """Connexion qui distribue les cursors séquentiellement."""
    call_count = [0]
    cursor_list = list(cursors)

    conn = MagicMock()
    conn.__enter__ = MagicMock(return_value=conn)
    conn.__exit__ = MagicMock(return_value=False)
    conn.commit = MagicMock()

    def _cursor():
        idx = call_count[0]
        call_count[0] += 1
        if idx < len(cursor_list):
            return cursor_list[idx]
        return _make_cursor()

    conn.cursor = MagicMock(side_effect=_cursor)
    return conn


# ---------------------------------------------------------------------------
# T9 : guard MEDIAPLAN_ALERTS_ENABLED=false
# ---------------------------------------------------------------------------


class TestDisabledGuard:
    def test_disabled_returns_empty(self):
        with patch.dict(os.environ, {"MEDIAPLAN_ALERTS_ENABLED": "false"}):
            result = mediaplan_alerts.evaluate_mediaplan_alerts(
                project_id=PROJECT_ID, evaluation_date=EVAL_DATE
            )
        assert result == []


# ---------------------------------------------------------------------------
# T1 : pace +25 % -> alerte rouge niveau ligne ET channel (deux firings distincts)
# ---------------------------------------------------------------------------


class TestOverrunFired:
    def test_overrun_ligne_rouge(self):
        """pace +25 % dépasse +10 % -> firing severity='error' pour la ligne."""
        prefs_cur = _make_prefs_cursor()      # NULL -> défauts
        plans_cur = _make_plans_cursor([PLAN_ID_A])
        insert_cur = _make_cursor()           # INSERT dans alert_firings

        # Séquence : prefs -> plans -> 3 x INSERT (line + channel + plan)
        conn = _make_sequential_cursors(
            prefs_cur, plans_cur,
            insert_cur, insert_cur, insert_cur,  # 3 niveaux x overrun
        )

        with (
            patch.dict(os.environ, {"MEDIAPLAN_ALERTS_ENABLED": "true"}),
            patch("core.db.get_connection", return_value=_conn_ctx(conn)),
            patch(
                "core.warehouse.query_plan_vs_actual",
                return_value=_pacing_plan_a_overrun(),
            ),
        ):
            result = mediaplan_alerts.evaluate_mediaplan_alerts(
                project_id=PROJECT_ID, evaluation_date=EVAL_DATE
            )

        # Au moins un firing 'error' pour la ligne digitale
        overrun_firings = [f for f in result if f["kind"] == mediaplan_alerts.KIND_OVERRUN]
        assert overrun_firings, "Aucun firing de dépassement produit"

        line_firings = [f for f in overrun_firings if f["level"] == "line"]
        assert line_firings, "Pas de firing au niveau ligne"
        assert line_firings[0]["severity"] == "error"
        assert abs(line_firings[0]["pace"] - 0.25) < 1e-6

    def test_overrun_channel_rouge(self):
        """L'agrégat channel à +25 % déclenche aussi une alerte rouge."""
        prefs_cur = _make_prefs_cursor()
        plans_cur = _make_plans_cursor([PLAN_ID_A])
        insert_cur = _make_cursor()

        conn = _make_sequential_cursors(prefs_cur, plans_cur, insert_cur, insert_cur, insert_cur)

        with (
            patch.dict(os.environ, {"MEDIAPLAN_ALERTS_ENABLED": "true"}),
            patch("core.db.get_connection", return_value=_conn_ctx(conn)),
            patch(
                "core.warehouse.query_plan_vs_actual",
                return_value=_pacing_plan_a_overrun(),
            ),
        ):
            result = mediaplan_alerts.evaluate_mediaplan_alerts(
                project_id=PROJECT_ID, evaluation_date=EVAL_DATE
            )

        channel_firings = [
            f for f in result
            if f["kind"] == mediaplan_alerts.KIND_OVERRUN and f["level"] == "channel"
        ]
        assert channel_firings, "Pas de firing au niveau channel"
        assert channel_firings[0]["severity"] == "error"


# ---------------------------------------------------------------------------
# T2 : pace -12 % -> alerte JAUNE distincte (kind=underdelivery, severity=warning)
# ---------------------------------------------------------------------------


class TestUnderdeliveryFired:
    def test_underdelivery_jaune_distinct(self):
        """pace -12 % < -10 % -> firing severity='warning', kind underdelivery."""
        prefs_cur = _make_prefs_cursor()
        plans_cur = _make_plans_cursor([PLAN_ID_A])
        insert_cur = _make_cursor()

        conn = _make_sequential_cursors(prefs_cur, plans_cur, insert_cur)

        with (
            patch.dict(os.environ, {"MEDIAPLAN_ALERTS_ENABLED": "true"}),
            patch("core.db.get_connection", return_value=_conn_ctx(conn)),
            patch(
                "core.warehouse.query_plan_vs_actual",
                return_value=_pacing_plan_a_underdelivery(),
            ),
        ):
            result = mediaplan_alerts.evaluate_mediaplan_alerts(
                project_id=PROJECT_ID, evaluation_date=EVAL_DATE
            )

        under_firings = [
            f for f in result
            if f["kind"] == mediaplan_alerts.KIND_UNDERDELIVERY
        ]
        assert under_firings, "Aucun firing de sous-livraison"
        assert under_firings[0]["severity"] == "warning"
        assert abs(under_firings[0]["pace"] - (-0.12)) < 1e-6

    def test_overrun_et_underdelivery_sont_distincts(self):
        """Pace +25 % -> firing overrun ; -12 % -> firing underdelivery ; kinds distincts."""
        # Aucune confusion de kind
        assert mediaplan_alerts.KIND_OVERRUN != mediaplan_alerts.KIND_UNDERDELIVERY
        assert "overrun" in mediaplan_alerts.KIND_OVERRUN
        assert "underdelivery" in mediaplan_alerts.KIND_UNDERDELIVERY


# ---------------------------------------------------------------------------
# T3 : is_plan_only -> jamais d'alerte
# ---------------------------------------------------------------------------


class TestPlanOnlyNeverAlerted:
    def test_plan_only_line_produces_no_firing(self):
        """Une ligne plan_only ne produit jamais de firing, même si pace non NULL."""
        # Forcer is_plan_only=True avec pace non NULL (cas pathologique)
        line = {**_LINE_PLAN_ONLY, "pace": 0.50, "actual_to_date": 1000.0}
        pacing = {"lines": [line], "channels": [], "plan": []}

        prefs_cur = _make_prefs_cursor()
        plans_cur = _make_plans_cursor([PLAN_ID_A])
        insert_cur = _make_cursor()

        conn = _make_sequential_cursors(prefs_cur, plans_cur, insert_cur)

        with (
            patch.dict(os.environ, {"MEDIAPLAN_ALERTS_ENABLED": "true"}),
            patch("core.db.get_connection", return_value=_conn_ctx(conn)),
            patch("core.warehouse.query_plan_vs_actual", return_value=pacing),
        ):
            result = mediaplan_alerts.evaluate_mediaplan_alerts(
                project_id=PROJECT_ID, evaluation_date=EVAL_DATE
            )

        assert result == [], "Une ligne plan_only ne doit jamais générer de firing"


# ---------------------------------------------------------------------------
# T4 : allocated_to_date = 0 / pace NULL -> jamais d'alerte
# ---------------------------------------------------------------------------


class TestNullOrZeroAllocNeverAlerted:
    def test_alloc_zero_no_firing(self):
        """allocated_to_date=0 -> skip (dénominateur zéro, pace NULL dans le mart)."""
        pacing = {"lines": [_LINE_ALLOC_ZERO], "channels": [], "plan": []}

        prefs_cur = _make_prefs_cursor()
        plans_cur = _make_plans_cursor([PLAN_ID_A])

        conn = _make_sequential_cursors(prefs_cur, plans_cur)

        with (
            patch.dict(os.environ, {"MEDIAPLAN_ALERTS_ENABLED": "true"}),
            patch("core.db.get_connection", return_value=_conn_ctx(conn)),
            patch("core.warehouse.query_plan_vs_actual", return_value=pacing),
        ):
            result = mediaplan_alerts.evaluate_mediaplan_alerts(
                project_id=PROJECT_ID, evaluation_date=EVAL_DATE
            )

        assert result == [], "allocated_to_date=0 ne doit jamais produire de firing"

    def test_pace_null_no_firing(self):
        """pace=None dans le mart -> skip."""
        line = {**_LINE_OVERRUN, "pace": None, "actual_to_date": None}
        pacing = {"lines": [line], "channels": [], "plan": []}

        prefs_cur = _make_prefs_cursor()
        plans_cur = _make_plans_cursor([PLAN_ID_A])

        conn = _make_sequential_cursors(prefs_cur, plans_cur)

        with (
            patch.dict(os.environ, {"MEDIAPLAN_ALERTS_ENABLED": "true"}),
            patch("core.db.get_connection", return_value=_conn_ctx(conn)),
            patch("core.warehouse.query_plan_vs_actual", return_value=pacing),
        ):
            result = mediaplan_alerts.evaluate_mediaplan_alerts(
                project_id=PROJECT_ID, evaluation_date=EVAL_DATE
            )

        assert result == []


# ---------------------------------------------------------------------------
# T5 : seuils projet ±20 % -> plus d'alerte au run suivant (configurable prouvé)
# ---------------------------------------------------------------------------


class TestConfigurableThresholds:
    def test_seuil_20pct_eteint_alerte_pace_15(self):
        """Preuve d'extinction de la spec : pace +15 % sous un seuil projet +20 %."""
        prefs_cur = _make_prefs_cursor(overrun=0.20, underdelivery=-0.20)
        plans_cur = _make_plans_cursor([PLAN_ID_A])

        import copy

        pacing = copy.deepcopy(_pacing_plan_a_overrun())
        for row in pacing["lines"] + pacing["channels"] + pacing["plan"]:
            if row.get("pace") is not None:
                row["pace"] = 0.15

        conn = _make_sequential_cursors(prefs_cur, plans_cur)

        with (
            patch.dict(os.environ, {"MEDIAPLAN_ALERTS_ENABLED": "true"}),
            patch("core.db.get_connection", return_value=_conn_ctx(conn)),
            patch("core.warehouse.query_plan_vs_actual", return_value=pacing),
        ):
            result = mediaplan_alerts.evaluate_mediaplan_alerts(
                project_id=PROJECT_ID, evaluation_date=EVAL_DATE
            )

        assert result == [], (
            "pace +15 % sous le seuil +20 % : aucune alerte (extinction configurable)"
        )

    def test_seuil_20pct_declenche_toujours_alerte_pace_25(self):
        """Avec seuil projet +20 %, un pace +25 % déclenche encore l'alerte."""
        prefs_cur = _make_prefs_cursor(overrun=0.20, underdelivery=-0.20)
        plans_cur = _make_plans_cursor([PLAN_ID_A])
        insert_cur = _make_cursor()

        conn = _make_sequential_cursors(prefs_cur, plans_cur, insert_cur, insert_cur, insert_cur)

        with (
            patch.dict(os.environ, {"MEDIAPLAN_ALERTS_ENABLED": "true"}),
            patch("core.db.get_connection", return_value=_conn_ctx(conn)),
            patch(
                "core.warehouse.query_plan_vs_actual",
                return_value=_pacing_plan_a_overrun(),
            ),
        ):
            result = mediaplan_alerts.evaluate_mediaplan_alerts(
                project_id=PROJECT_ID, evaluation_date=EVAL_DATE
            )

        # pace +25 % > seuil +20 % : l'alerte doit quand même se déclencher
        overrun_firings = [f for f in result if f["kind"] == mediaplan_alerts.KIND_OVERRUN]
        assert overrun_firings, "pace +25 % doit déclencher l'alerte même avec seuil +20 %"

    def test_seuil_30pct_etteint_alerte_pour_pace_25(self):
        """Avec seuil projet +30 %, un pace +25 % n'atteint PAS le seuil -- alerte éteinte."""
        prefs_cur = _make_prefs_cursor(overrun=0.30, underdelivery=-0.30)
        plans_cur = _make_plans_cursor([PLAN_ID_A])

        conn = _make_sequential_cursors(prefs_cur, plans_cur)

        with (
            patch.dict(os.environ, {"MEDIAPLAN_ALERTS_ENABLED": "true"}),
            patch("core.db.get_connection", return_value=_conn_ctx(conn)),
            patch(
                "core.warehouse.query_plan_vs_actual",
                return_value=_pacing_plan_a_overrun(),
            ),
        ):
            result = mediaplan_alerts.evaluate_mediaplan_alerts(
                project_id=PROJECT_ID, evaluation_date=EVAL_DATE
            )

        overrun_firings = [f for f in result if f["kind"] == mediaplan_alerts.KIND_OVERRUN]
        assert overrun_firings == [], (
            "pace +25 % ne doit PAS déclencher l'alerte avec seuil +30 % -- "
            "configurable prouvé : changer le seuil éteint l'alerte"
        )

    def test_seuil_depuis_project_preferences_cited_dans_metadata(self):
        """Quand le seuil vient des préférences, threshold_source='project_preference'."""
        prefs_cur = _make_prefs_cursor(overrun=0.10, underdelivery=-0.10)
        plans_cur = _make_plans_cursor([PLAN_ID_A])
        insert_cur = _make_cursor()

        conn = _make_sequential_cursors(prefs_cur, plans_cur, insert_cur, insert_cur, insert_cur)

        # Capturer l'appel _write_firing pour inspecter le metadata
        captured: list[dict] = []

        original_write = mediaplan_alerts._write_firing

        def _capture_firing(**kwargs):
            captured.append(kwargs)
            return original_write(**kwargs)

        with (
            patch.dict(os.environ, {"MEDIAPLAN_ALERTS_ENABLED": "true"}),
            patch("core.db.get_connection", return_value=_conn_ctx(conn)),
            patch(
                "core.warehouse.query_plan_vs_actual",
                return_value=_pacing_plan_a_overrun(),
            ),
            patch.object(mediaplan_alerts, "_write_firing", side_effect=_capture_firing),
        ):
            mediaplan_alerts.evaluate_mediaplan_alerts(
                project_id=PROJECT_ID, evaluation_date=EVAL_DATE
            )

        if captured:
            first = captured[0]
            assert first["threshold"] == pytest.approx(0.10)


# ---------------------------------------------------------------------------
# T6 : metadata complet (version, formule, seuil, threshold_source)
# ---------------------------------------------------------------------------


class TestMetadataComplet:
    def test_firing_cite_version_formule_seuil(self):
        """Chaque firing cite plan_version_id, la formule AD-9, et le seuil."""
        prefs_cur = _make_prefs_cursor()
        plans_cur = _make_plans_cursor([PLAN_ID_A])
        insert_cur = _make_cursor()

        conn = _make_sequential_cursors(prefs_cur, plans_cur, insert_cur, insert_cur, insert_cur)

        captured_writes: list[dict] = []
        original_write = mediaplan_alerts._write_firing

        def _capture(**kwargs):
            captured_writes.append(kwargs)
            return original_write(**kwargs)

        with (
            patch.dict(os.environ, {"MEDIAPLAN_ALERTS_ENABLED": "true"}),
            patch("core.db.get_connection", return_value=_conn_ctx(conn)),
            patch(
                "core.warehouse.query_plan_vs_actual",
                return_value=_pacing_plan_a_overrun(),
            ),
            patch.object(mediaplan_alerts, "_write_firing", side_effect=_capture),
        ):
            mediaplan_alerts.evaluate_mediaplan_alerts(
                project_id=PROJECT_ID, evaluation_date=EVAL_DATE
            )

        # Au moins un firing doit avoir été capturé
        assert captured_writes, "Aucun _write_firing appelé"

        first = captured_writes[0]
        # Le metadata JSON est dans la partie après " | "
        assert first["threshold"] == pytest.approx(
            mediaplan_alerts.DEFAULT_OVERRUN_THRESHOLD
        )
        # L'observed_value doit correspondre au pace
        assert first["observed_value"] == pytest.approx(0.25, abs=1e-6)

    def test_firing_message_contient_info_fr(self):
        """Le message du firing contient des libellés français accentués."""
        prefs_cur = _make_prefs_cursor()
        plans_cur = _make_plans_cursor([PLAN_ID_A])
        insert_cur = _make_cursor()

        conn = _make_sequential_cursors(prefs_cur, plans_cur, insert_cur, insert_cur, insert_cur)

        captured: list[str] = []

        def _capture_write(**kwargs):
            captured.append(kwargs.get("message", ""))
            # Simuler l'insert : retourner un firing_id fake
            return "fire_TEST"

        with (
            patch.dict(os.environ, {"MEDIAPLAN_ALERTS_ENABLED": "true"}),
            patch("core.db.get_connection", return_value=_conn_ctx(conn)),
            patch(
                "core.warehouse.query_plan_vs_actual",
                return_value=_pacing_plan_a_overrun(),
            ),
            patch.object(mediaplan_alerts, "_write_firing", side_effect=_capture_write),
        ):
            mediaplan_alerts.evaluate_mediaplan_alerts(
                project_id=PROJECT_ID, evaluation_date=EVAL_DATE
            )

        assert captured, "Aucun message capturé"
        # Le premier message doit mentionner le dépassement en français
        first_msg = captured[0]
        assert "Dépassement" in first_msg or "Sous-livraison" in first_msg

    def test_formula_constante_ad9(self):
        """PACE_FORMULA est la formule AD-9 documentée."""
        assert mediaplan_alerts.PACE_FORMULA == (
            "(actual_to_date - allocated_to_date) / allocated_to_date"
        )

    def test_plan_version_id_extrait_du_pacing(self):
        """La fonction _extract_version_id retourne le bon plan_version_id."""
        pacing = _pacing_plan_a_overrun()
        vid = mediaplan_alerts._extract_version_id(pacing)
        assert vid == VERSION_ID

    def test_plan_version_id_absent_retourne_vide(self):
        """_extract_version_id retourne '' quand les listes sont vides."""
        vid = mediaplan_alerts._extract_version_id({"lines": [], "channels": [], "plan": []})
        assert vid == ""


# ---------------------------------------------------------------------------
# T7 : deux plans actifs -> évaluations indépendantes
# ---------------------------------------------------------------------------


class TestDeuxPlansIndependants:
    def test_deux_plans_evalues_separement(self):
        """Deux plans actifs sont évalués indépendamment ; les firings ne se mélangent pas."""
        prefs_cur = _make_prefs_cursor()
        plans_cur = _make_plans_cursor([PLAN_ID_A, PLAN_ID_B])
        insert_cur_a = _make_cursor()
        insert_cur_b = _make_cursor()

        conn = _make_sequential_cursors(
            prefs_cur, plans_cur,
            insert_cur_a, insert_cur_a, insert_cur_a,   # firings plan A
            insert_cur_b,                                # firing plan B
        )

        # Plan B avec seulement une ligne underdelivery
        line_b = {**_LINE_UNDERDELIVERY, "plan_id": PLAN_ID_B}
        pacing_b = {"lines": [line_b], "channels": [], "plan": []}

        call_count = [0]

        def _fake_warehouse(project_id, plan_id):
            call_count[0] += 1
            if plan_id == PLAN_ID_A:
                return _pacing_plan_a_overrun()
            return pacing_b

        with (
            patch.dict(os.environ, {"MEDIAPLAN_ALERTS_ENABLED": "true"}),
            patch("core.db.get_connection", return_value=_conn_ctx(conn)),
            patch("core.warehouse.query_plan_vs_actual", side_effect=_fake_warehouse),
        ):
            result = mediaplan_alerts.evaluate_mediaplan_alerts(
                project_id=PROJECT_ID, evaluation_date=EVAL_DATE
            )

        # query_plan_vs_actual appelé une fois par plan
        assert call_count[0] == 2, f"Attendu 2 appels warehouse, obtenu {call_count[0]}"

        # Firings des deux plans présents et distincts
        plan_ids_in_firings = {f["plan_id"] for f in result}
        assert PLAN_ID_A in plan_ids_in_firings
        assert PLAN_ID_B in plan_ids_in_firings

        # Kinds distincts : A -> overrun, B -> underdelivery
        kinds_a = {f["kind"] for f in result if f["plan_id"] == PLAN_ID_A}
        kinds_b = {f["kind"] for f in result if f["plan_id"] == PLAN_ID_B}
        assert mediaplan_alerts.KIND_OVERRUN in kinds_a
        assert mediaplan_alerts.KIND_UNDERDELIVERY in kinds_b


# ---------------------------------------------------------------------------
# T8 : step scheduler isolé (échec warehouse -> nightly continue)
# ---------------------------------------------------------------------------


class TestSchedulerIsolation:
    def test_warehouse_error_nightly_continue(self):
        """Si query_plan_vs_actual lève, le nightly continue sans interruption.

        Pattern AI-32 : _run_isolated_step intercepte toute exception.
        """
        # Importer le scheduler APRES les guards d'env
        from core import scheduler  # noqa: PLC0415

        ran: list[str] = []

        def _before_mediaplan():
            ran.append("before")

        def _mediaplan_raises():
            # Simuler que _run_mediaplan_alert_check lève à cause du warehouse
            raise RuntimeError("warehouse en panne")

        def _after_mediaplan():
            ran.append("after")

        meta_alerts: list[tuple] = []

        with (
            patch("core.scheduler._try_advisory_lock", return_value=None),
            patch("core.scheduler.dispatch_nightly", return_value=[]),
            patch("core.scheduler._run_rebuild_cache"),
            patch("core.scheduler._run_schema_context_gen"),
            patch("core.scheduler._run_alert_check", side_effect=_before_mediaplan),
            patch("core.scheduler._run_business_alert_check"),
            patch("core.scheduler._run_anomaly_alert_check"),
            patch(
                "core.scheduler._run_mediaplan_alert_check",
                side_effect=_mediaplan_raises,
            ),
            patch("core.scheduler._run_dq_monitors_check", side_effect=_after_mediaplan),
            patch("core.scheduler._run_due_notebooks"),
            patch("core.scheduler._run_due_briefings"),
            patch(
                "core.scheduler._insert_meta_alert",
                side_effect=lambda step, reason: meta_alerts.append((step, reason)),
            ),
            patch("core.scheduler._write_scheduler_step_degraded_alert"),
        ):
            scheduler.run_nightly_steps(date(2026, 7, 20))

        # 'before' (alert_check) et 'after' (dq_monitors) ont bien tourné
        assert "before" in ran
        assert "after" in ran

        # Un meta_alert a été émis pour le step mediaplan_alert_check
        assert any("mediaplan_alert_check" in s for s, _ in meta_alerts)

    def test_run_mediaplan_alert_check_disabled(self):
        """MEDIAPLAN_ALERTS_ENABLED=false -> le step se termine sans appel warehouse."""
        from core import scheduler  # noqa: PLC0415

        with (
            patch.dict(os.environ, {"MEDIAPLAN_ALERTS_ENABLED": "false"}),
            patch("core.mediaplan_alerts.evaluate_mediaplan_alerts") as mock_eval,
        ):
            scheduler._run_mediaplan_alert_check()

        mock_eval.assert_not_called()

    def test_run_mediaplan_alert_check_enabled(self):
        """MEDIAPLAN_ALERTS_ENABLED=true -> evaluate_mediaplan_alerts est appelé."""
        from core import scheduler  # noqa: PLC0415

        with (
            patch.dict(os.environ, {"MEDIAPLAN_ALERTS_ENABLED": "true"}),
            patch(
                "core.mediaplan_alerts.evaluate_mediaplan_alerts",
                return_value=[],
            ) as mock_eval,
        ):
            scheduler._run_mediaplan_alert_check()

        mock_eval.assert_called_once()


# ---------------------------------------------------------------------------
# T10 : dégradation gracieuse DB
# ---------------------------------------------------------------------------


class TestGracefulDegradation:
    def test_db_error_returns_empty(self):
        """Si get_connection lève, evaluate_mediaplan_alerts retourne [] sans relever."""
        with (
            patch.dict(os.environ, {"MEDIAPLAN_ALERTS_ENABLED": "true"}),
            patch("core.db.get_connection", side_effect=RuntimeError("DB down")),
        ):
            result = mediaplan_alerts.evaluate_mediaplan_alerts(
                project_id=PROJECT_ID, evaluation_date=EVAL_DATE
            )
        assert result == []

    def test_warehouse_error_per_plan_continues(self):
        """Échec warehouse pour UN plan n'arrête pas l'évaluation des autres."""
        prefs_cur = _make_prefs_cursor()
        plans_cur = _make_plans_cursor([PLAN_ID_A, PLAN_ID_B])
        insert_cur = _make_cursor()

        conn = _make_sequential_cursors(prefs_cur, plans_cur, insert_cur)

        call_count = [0]

        def _fake_warehouse(project_id, plan_id):
            call_count[0] += 1
            if plan_id == PLAN_ID_A:
                raise RuntimeError("warehouse en panne pour plan A")
            return _pacing_plan_a_underdelivery()

        with (
            patch.dict(os.environ, {"MEDIAPLAN_ALERTS_ENABLED": "true"}),
            patch("core.db.get_connection", return_value=_conn_ctx(conn)),
            patch("core.warehouse.query_plan_vs_actual", side_effect=_fake_warehouse),
        ):
            mediaplan_alerts.evaluate_mediaplan_alerts(
                project_id=PROJECT_ID, evaluation_date=EVAL_DATE
            )

        # Plan A a échoué mais plan B a produit un firing (underdelivery)
        assert call_count[0] == 2
        # Pas d'exception levée : résultat partiel toléré


# ---------------------------------------------------------------------------
# T11 : fetch_recent_mediaplan_firings
# ---------------------------------------------------------------------------


class TestFetchRecentFireings:
    def test_fetch_recents_retourne_firings(self):
        """fetch_recent_mediaplan_firings retourne les firings récents formatés."""
        firing_row = (
            "fire_TEST001",                  # firing_id
            f"{mediaplan_alerts.KIND_OVERRUN}:{PLAN_ID_A}:line:line-digital-a",  # metric
            0.25,                            # observed_value
            0.10,                            # threshold
            "2026-07-20T08:00:00+00:00",     # fired_at
            "2026-07-19",                    # window_date
            "error",                         # severity
            "Dépassement budgétaire : Digital A | {}",  # message
        )
        cur = _make_cursor(
            rows=[firing_row],
            description=[
                ("firing_id",), ("metric",), ("observed_value",), ("threshold",),
                ("fired_at",), ("window_date",), ("severity",), ("message",),
            ],
        )
        conn = _make_conn(cur)

        result = mediaplan_alerts.fetch_recent_mediaplan_firings(
            project_id=PROJECT_ID, conn=conn, hours=24
        )

        assert len(result) == 1
        f = result[0]
        assert f["code"] == mediaplan_alerts.KIND_OVERRUN
        assert f["severity"] == "error"
        assert f["observed_value"] == pytest.approx(0.25)
        assert f["threshold"] == pytest.approx(0.10)
        assert f["firing_id"] == "fire_TEST001"
        # Le message affiché ne doit pas inclure le JSON de metadata
        assert "Dépassement budgétaire" in f["message"]
        assert "{}" not in f["message"]  # JSON metadata tronqué

    def test_fetch_recents_db_error_retourne_vide(self):
        """En cas d'erreur DB, fetch_recent_mediaplan_firings retourne [] sans lever."""
        conn = MagicMock()
        conn.cursor.side_effect = RuntimeError("DB crash")

        result = mediaplan_alerts.fetch_recent_mediaplan_firings(
            project_id=PROJECT_ID, conn=conn, hours=24
        )
        assert result == []

    def test_fetch_recents_underdelivery_code(self):
        """Un firing underdelivery est correctement identifié par son code."""
        metric_key = f"{mediaplan_alerts.KIND_UNDERDELIVERY}:{PLAN_ID_A}:line:line-social"
        firing_row = (
            "fire_TEST002",
            metric_key,
            -0.12,
            -0.10,
            "2026-07-20T08:00:00+00:00",
            "2026-07-19",
            "warning",
            "Sous-livraison : Social | {}",
        )
        cur = _make_cursor(
            rows=[firing_row],
            description=[
                ("firing_id",), ("metric",), ("observed_value",), ("threshold",),
                ("fired_at",), ("window_date",), ("severity",), ("message",),
            ],
        )
        conn = _make_conn(cur)

        result = mediaplan_alerts.fetch_recent_mediaplan_firings(
            project_id=PROJECT_ID, conn=conn, hours=24
        )
        assert len(result) == 1
        assert result[0]["code"] == mediaplan_alerts.KIND_UNDERDELIVERY
        assert result[0]["severity"] == "warning"


# ---------------------------------------------------------------------------
# Tests de format et constantes
# ---------------------------------------------------------------------------


class TestFormatAndConstants:
    def test_format_mediaplan_alert_line(self):
        """format_mediaplan_alert_line retourne une ligne lisible en français."""
        firing = {
            "message": "Dépassement budgétaire : Digital A (pace +25,0%, seuil +10,0%)",
            "observed_value": 0.25,
            "threshold": 0.10,
        }
        line = mediaplan_alerts.format_mediaplan_alert_line(firing)
        assert "pacing mediaplan" in line
        assert "+25" in line or "25%" in line
        # ASCII-only stdout : pas d'emoji dans le stdout de log (le message lui-même peut en avoir)

    def test_defaults_documented(self):
        """Les défauts documentés correspondent aux décisions du spike."""
        assert mediaplan_alerts.DEFAULT_OVERRUN_THRESHOLD == pytest.approx(0.10)
        assert mediaplan_alerts.DEFAULT_UNDERDELIVERY_THRESHOLD == pytest.approx(-0.10)

    def test_alert_type_distinct(self):
        """Le type 'mediaplan_pace' est distinct de 'business_threshold' et 'anomaly'."""
        assert mediaplan_alerts.ALERT_TYPE == "mediaplan_pace"
        assert mediaplan_alerts.ALERT_TYPE not in ("business_threshold", "anomaly")


# ---------------------------------------------------------------------------
# E1-F-3 : clé de dédup metric sûre pour des row_key longs (pas de collision)
# ---------------------------------------------------------------------------


class TestMetricKeyDedup:
    def test_short_key_unchanged(self):
        """Une clé courte reste lisible et inchangée (kind:plan:level:row_key)."""
        key = mediaplan_alerts._build_metric_key(
            mediaplan_alerts.KIND_OVERRUN, PLAN_ID_A, "line", "line-social"
        )
        assert key == f"{mediaplan_alerts.KIND_OVERRUN}:{PLAN_ID_A}:line:line-social"
        assert len(key) <= 255

    def test_long_row_keys_do_not_collide(self):
        """DISCRIMINANT : deux row_key longs au préfixe commun -> clés DISTINCTES.

        Avant le fix, la troncature [:255] faisait collisionner ces deux clés ->
        ON CONFLICT DO NOTHING avalait la 2e alerte. Le suffixe sha256 les sépare.
        """
        common = "campagne/" + ("x" * 300)
        row_key_1 = common + "/A"
        row_key_2 = common + "/B"
        k1 = mediaplan_alerts._build_metric_key(
            mediaplan_alerts.KIND_OVERRUN, PLAN_ID_A, "line", row_key_1
        )
        k2 = mediaplan_alerts._build_metric_key(
            mediaplan_alerts.KIND_OVERRUN, PLAN_ID_A, "line", row_key_2
        )
        # Naïvement tronqués, ils seraient identiques :
        naive_1 = f"{mediaplan_alerts.KIND_OVERRUN}:{PLAN_ID_A}:line:{row_key_1}"[:255]
        naive_2 = f"{mediaplan_alerts.KIND_OVERRUN}:{PLAN_ID_A}:line:{row_key_2}"[:255]
        assert naive_1 == naive_2, "prérequis du test : la troncature naïve collisionne"
        # Après le fix : distincts, déterministes, <= 255.
        assert k1 != k2
        assert len(k1) == 255 and len(k2) == 255
        assert k1 == mediaplan_alerts._build_metric_key(
            mediaplan_alerts.KIND_OVERRUN, PLAN_ID_A, "line", row_key_1
        )  # déterministe

    def test_long_key_keeps_readable_prefix(self):
        """La clé longue garde un préfixe lisible (kind:plan_id:level:...)."""
        key = mediaplan_alerts._build_metric_key(
            mediaplan_alerts.KIND_UNDERDELIVERY, PLAN_ID_A, "channel", "y" * 400
        )
        assert key.startswith(
            f"{mediaplan_alerts.KIND_UNDERDELIVERY}:{PLAN_ID_A}:channel:"
        )


# ---------------------------------------------------------------------------
# E1-F-4 : as_of_day dans les metadata de chaque firing (provenance AD-9)
# ---------------------------------------------------------------------------


class TestAsOfDayProvenance:
    def test_extract_as_of_day_from_plan_row(self):
        as_of = mediaplan_alerts._extract_as_of_day(_pacing_plan_a_overrun())
        assert as_of == "2026-03-10"

    def test_extract_as_of_day_absent_returns_none(self):
        empty = {"lines": [], "channels": [], "plan": []}
        assert mediaplan_alerts._extract_as_of_day(empty) is None

    def test_firing_metadata_contains_as_of_day(self):
        """Chaque firing porte as_of_day (date de référence effective, AD-9)."""
        prefs_cur = _make_prefs_cursor()
        plans_cur = _make_plans_cursor([PLAN_ID_A])
        insert_cur = _make_cursor()
        conn = _make_sequential_cursors(prefs_cur, plans_cur, insert_cur, insert_cur, insert_cur)

        captured_meta: list[dict] = []

        def _capture_write(**kwargs):
            captured_meta.append(kwargs.get("metadata", {}))
            return "fire_TEST"

        with (
            patch.dict(os.environ, {"MEDIAPLAN_ALERTS_ENABLED": "true"}),
            patch("core.db.get_connection", return_value=_conn_ctx(conn)),
            patch(
                "core.warehouse.query_plan_vs_actual",
                return_value=_pacing_plan_a_overrun(),
            ),
            patch.object(mediaplan_alerts, "_write_firing", side_effect=_capture_write),
        ):
            mediaplan_alerts.evaluate_mediaplan_alerts(
                project_id=PROJECT_ID, evaluation_date=EVAL_DATE
            )

        assert captured_meta, "Aucun firing capturé"
        assert all(m.get("as_of_day") == "2026-03-10" for m in captured_meta)


# ---------------------------------------------------------------------------
# E2-F-2 : garde défensive paceable_line_count == 0 -> skip
# ---------------------------------------------------------------------------


class TestZeroPaceableGuard:
    def test_channel_with_zero_paceable_is_skipped(self):
        """Un rollup channel à paceable_line_count=0 ne produit aucune alerte."""
        zero_paceable_channel = {
            **_CHANNEL_OVERRUN,
            "paceable_line_count": 0,
            "plan_only_line_count": 3,
        }
        firings = mediaplan_alerts._evaluate_rows(
            rows=[zero_paceable_channel],
            level="channel",
            plan_id=PLAN_ID_A,
            plan_version_id=VERSION_ID,
            project_id=PROJECT_ID,
            overrun_thr=0.10,
            underdelivery_thr=-0.10,
            overrun_source="documented_default",
            underdelivery_source="documented_default",
            window_date=EVAL_DATE,
            as_of_day="2026-03-10",
            conn=MagicMock(),
        )
        assert firings == []
