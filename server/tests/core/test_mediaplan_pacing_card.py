"""Tests Story 22.5 — Carte Médiaplan & surface agent.

Suis les patterns AI-54 (fixture = snapshot du VRAI resolver sur stand-ins warehouse
fidèles) et leçon 17.5 (couverture affichée dans le commentaire).

Stand-ins warehouse : mêmes DDL/données que test_mediaplan_pacing.py (22.4),
enrichis du plan-only TV et d'un plan_pacing_by_plan row.

Cas couverts :
  1. Cas chiffré 22.4 — line-digital-a 3000/30j/10j/1250 → pace +25 %
  2. Ligne plan-only → « plan seul » dans le commentaire, jamais de % inventé
  3. Canal LLM ≤30 lignes
  4. Provenance présente (plan_version_id + pull_ids)
  5. La carte n'est JAMAIS dans les suggestions auto (context card)
  6. pace NULL → valeur None dans les données (jamais 0)
  7. build_mediaplan_pacing_comment : formule citée + couverture + version
"""

from __future__ import annotations

import importlib

import pytest

pytest.importorskip("duckdb")


# ---------------------------------------------------------------------------
# Stand-in DuckDB (identique à test_mediaplan_pacing.py — fidèle aux dbt DDL)
# ---------------------------------------------------------------------------

_DDL = """
CREATE SCHEMA IF NOT EXISTS main_marts;

CREATE TABLE main_marts.plan_pacing_by_line (
    project_id VARCHAR, plan_id VARCHAR, plan_version_id VARCHAR, line_key VARCHAR,
    label VARCHAR, channel VARCHAR, currency VARCHAR, budget DOUBLE,
    line_start_date DATE, line_end_date DATE, is_plan_only BOOLEAN, sort_order INTEGER,
    as_of_day DATE, days_total INTEGER, days_elapsed INTEGER,
    allocated_to_date DOUBLE, actual_to_date DOUBLE, consumed_pct DOUBLE, pace DOUBLE,
    remaining_budget DOUBLE, extrapolated_spend DOUBLE,
    actual_pull_id_min VARCHAR, actual_pull_id_max VARCHAR, actual_pull_id_count BIGINT
);

CREATE TABLE main_marts.plan_pacing_by_channel (
    project_id VARCHAR, plan_id VARCHAR, plan_version_id VARCHAR, channel VARCHAR,
    budget DOUBLE, allocated_to_date DOUBLE, actual_to_date DOUBLE,
    remaining_budget DOUBLE, consumed_pct DOUBLE, pace DOUBLE, extrapolated_spend DOUBLE,
    plan_only_line_count BIGINT, paceable_line_count BIGINT,
    actual_pull_id_min VARCHAR, actual_pull_id_max VARCHAR, actual_pull_id_count BIGINT
);

CREATE TABLE main_marts.plan_pacing_by_plan (
    project_id VARCHAR, plan_id VARCHAR, plan_version_id VARCHAR, currency VARCHAR,
    as_of_day DATE, budget DOUBLE, allocated_to_date DOUBLE, actual_to_date DOUBLE,
    remaining_budget DOUBLE, consumed_pct DOUBLE, pace DOUBLE, extrapolated_spend DOUBLE,
    plan_only_line_count BIGINT, paceable_line_count BIGINT,
    actual_pull_id_min VARCHAR, actual_pull_id_max VARCHAR, actual_pull_id_count BIGINT
);
"""

PLAN_ID = "00000000-0000-4000-8000-000000000022"
VERSION_ID = "00000000-0000-4000-8000-000000000221"
PROJECT_ID = "default"

_C_A = 1250.0 / 3000.0


def _seed_card_db(path: str) -> None:
    import duckdb  # noqa: PLC0415

    con = duckdb.connect(path)
    try:
        con.execute(_DDL)
        con.executemany(
            "INSERT INTO main_marts.plan_pacing_by_line "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                # line-digital-a: 3000/30j, 10j, 1250 → pace +25 %
                (PROJECT_ID, PLAN_ID, VERSION_ID, "line-digital-a",
                 "Digital A", "digital", "EUR", 3000.0,
                 "2026-03-01", "2026-03-30", False, 0,
                 "2026-03-10", 30, 10, 1000.0, 1250.0, _C_A, 0.25, 1750.0, 3750.0,
                 "pull_a", "pull_b", 2),
                # line-tv: plan-only — actual / pace NULL
                (PROJECT_ID, PLAN_ID, VERSION_ID, "line-tv",
                 "TV Brand", "tv", "EUR", 5000.0,
                 "2026-03-01", "2026-03-30", True, 1,
                 "2026-03-10", 30, 10, 5000.0, None, None, None, None, None,
                 None, None, 0),
            ],
        )
        con.executemany(
            "INSERT INTO main_marts.plan_pacing_by_channel "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                (PROJECT_ID, PLAN_ID, VERSION_ID, "digital",
                 3000.0, 1000.0, 1250.0, 1750.0, _C_A, 0.25, 3750.0, 0, 1,
                 "pull_a", "pull_b", 2),
                (PROJECT_ID, PLAN_ID, VERSION_ID, "tv",
                 5000.0, None, None, None, None, None, None, 1, 0,
                 None, None, 0),
            ],
        )
        con.executemany(
            "INSERT INTO main_marts.plan_pacing_by_plan VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                (PROJECT_ID, PLAN_ID, VERSION_ID, "EUR", "2026-03-10",
                 8000.0, 1000.0, 1250.0, 6750.0, 1250.0 / 8000.0, 0.25, 3750.0,
                 1, 1, "pull_a", "pull_b", 2),
            ],
        )
    finally:
        con.close()


@pytest.fixture
def card_db(tmp_path, monkeypatch):
    db_file = tmp_path / "card_pacing.duckdb"
    _seed_card_db(str(db_file))
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(db_file))
    monkeypatch.setenv("TOOROW_CACHE_ENABLED", "false")
    from core import warehouse  # noqa: PLC0415
    importlib.reload(warehouse)

    # Stub get_plan (not needed for mart reads; avoids real PG in offline tests).
    def _stub_get_plan(conn, *, plan_id):  # noqa: ARG001
        return {
            "id": plan_id,
            # project_id required since review 22.5 F-1 (AD-5 same-project guard).
            "project_id": PROJECT_ID,
            "name": "Plan Test 22.5",
            "currency": "EUR",
            "active_version": {"id": VERSION_ID},
            "lines": [],
        }

    import unittest.mock as _mock  # noqa: PLC0415
    monkeypatch.setattr("core.mediaplan_store.get_plan", _stub_get_plan)
    # Stub db.get_connection to avoid real PG. The context manager must return
    # a fake connection object so mediaplan_store.get_plan receives something.
    _fake_ctx = _mock.MagicMock()
    _fake_ctx.__enter__ = _mock.MagicMock(return_value=_mock.MagicMock())
    _fake_ctx.__exit__ = _mock.MagicMock(return_value=False)
    monkeypatch.setattr("core.db.get_connection", lambda: _fake_ctx)

    from core import cards  # noqa: PLC0415
    importlib.reload(cards)
    return cards


# ---------------------------------------------------------------------------
# 1. Résolveur direct : _resolve_mediaplan_pacing_card
# ---------------------------------------------------------------------------

def test_card_resolver_returns_summary_envelope_widget(card_db):
    from core.cards import get_template  # noqa: PLC0415

    tpl = get_template("mediaplan_pacing")
    assert tpl is not None
    summary, envelope, widget_uri = card_db._resolve_mediaplan_pacing_card(
        tpl,
        PROJECT_ID,
        PLAN_ID,
        context_events=[],
        alerts=[],
        trace_id=None,
    )
    assert isinstance(summary, str)
    assert isinstance(envelope, dict)
    assert widget_uri == "ui://core/card-mediaplan-pacing"


def test_card_line_digital_a_pace_25_pct(card_db):
    """Cas chiffré 22.4 : 3000/30j/10j/1250 → pace +25 %."""
    from core.cards import get_template  # noqa: PLC0415

    tpl = get_template("mediaplan_pacing")
    _, envelope, _ = card_db._resolve_mediaplan_pacing_card(
        tpl, PROJECT_ID, PLAN_ID, context_events=[], alerts=[], trace_id=None,
    )
    composition = envelope["data"]["composition"]
    line_table = next(b for b in composition if b["binding"]["source"] == "plan_lines")
    row_a = next(r for r in line_table["data"]["rows"] if r["line_key"] == "line-digital-a")
    assert row_a["actual_to_date"] == pytest.approx(1250.0)
    assert row_a["pace_pct"] == pytest.approx(25.0, abs=0.1)   # +25 %
    assert row_a["consumed_pct"] == pytest.approx(41.7, abs=0.15)
    assert row_a["remaining_budget"] == pytest.approx(1750.0)
    assert row_a["extrapolated_spend"] == pytest.approx(3750.0)


def test_card_plan_only_line_has_null_pace(card_db):
    """Ligne plan-only → actual / pace / consumed NULL (jamais 0)."""
    from core.cards import get_template  # noqa: PLC0415

    tpl = get_template("mediaplan_pacing")
    _, envelope, _ = card_db._resolve_mediaplan_pacing_card(
        tpl, PROJECT_ID, PLAN_ID, context_events=[], alerts=[], trace_id=None,
    )
    composition = envelope["data"]["composition"]
    line_table = next(b for b in composition if b["binding"]["source"] == "plan_lines")
    row_tv = next(r for r in line_table["data"]["rows"] if r["line_key"] == "line-tv")
    assert row_tv["is_plan_only"] is True
    assert row_tv["actual_to_date"] is None
    assert row_tv["pace_pct"] is None
    assert row_tv["consumed_pct"] is None
    assert row_tv["extrapolated_spend"] is None


def test_card_provenance_present(card_db):
    """Provenance : plan_version_id + pull_ids présents (AD-9)."""
    from core.cards import get_template  # noqa: PLC0415

    tpl = get_template("mediaplan_pacing")
    _, envelope, _ = card_db._resolve_mediaplan_pacing_card(
        tpl, PROJECT_ID, PLAN_ID, context_events=[], alerts=[], trace_id=None,
    )
    meta = envelope["meta"]
    assert meta["provenance"]["plan_version_id"] == VERSION_ID
    assert isinstance(meta["provenance"]["pull_ids"], list)
    assert len(meta["provenance"]["pull_ids"]) > 0


def test_card_llm_channel_under_30_lines(card_db):
    """Canal LLM ≤30 lignes (AD-1)."""
    from core.cards import get_template  # noqa: PLC0415

    tpl = get_template("mediaplan_pacing")
    summary, _, _ = card_db._resolve_mediaplan_pacing_card(
        tpl, PROJECT_ID, PLAN_ID, context_events=[], alerts=[], trace_id=None,
    )
    assert len(summary.splitlines()) <= 30


# ---------------------------------------------------------------------------
# 2. Carte jamais dans les suggestions auto (context card)
# ---------------------------------------------------------------------------

def test_mediaplan_pacing_never_auto_suggested():
    """La carte mediaplan_pacing est JAMAIS dans la pool de suggestions auto."""
    from core.cards import suggest_template  # noqa: PLC0415

    best, alternatives = suggest_template(
        available_metrics={"clicks", "impressions", "conversions", "sessions", "budget"},
        available_dimensions={"date", "channel"},
    )
    all_suggested = (
        [best.id if best else None]
        + [t.id for t in alternatives]
    )
    assert "mediaplan_pacing" not in all_suggested


def test_mediaplan_pacing_in_catalog():
    """La carte mediaplan_pacing est exposée via list_templates (LLM la découvre)."""
    from core.cards import list_templates  # noqa: PLC0415

    ids = [t["id"] for t in list_templates()]
    assert "mediaplan_pacing" in ids


def test_mediaplan_pacing_is_context_kind():
    """kind=context → jamais auto-suggérée (single choke point, suggest_template)."""
    from core.cards import get_template  # noqa: PLC0415

    tpl = get_template("mediaplan_pacing")
    assert tpl is not None
    assert tpl.is_context


# ---------------------------------------------------------------------------
# 3. build_mediaplan_pacing_comment — formule, couverture, version citée
# ---------------------------------------------------------------------------

def test_comment_formula_cited():
    """La formule de pace est citée dans le commentaire (AD-9)."""
    from core.narrative import build_mediaplan_pacing_comment  # noqa: PLC0415

    comment = build_mediaplan_pacing_comment(
        block_data={},
        plan_id=PLAN_ID,
        plan_name="Plan Test",
        plan_version_id=VERSION_ID,
        as_of_day="2026-03-10",
        plan_rows=[{
            "pace": 0.25,
            "actual_to_date": 1250.0,
            "budget": 8000.0,
        }],
        plan_only_keys=[],
        pull_ids=["pull_a", "pull_b"],
        context_events=[],
    )
    assert "Pace" in comment
    assert "réel" in comment
    assert "prévu" in comment


def test_comment_as_of_day_cited():
    """as_of_day doit apparaître dans le commentaire (couverture affichée)."""
    from core.narrative import build_mediaplan_pacing_comment  # noqa: PLC0415

    comment = build_mediaplan_pacing_comment(
        block_data={},
        plan_id=PLAN_ID,
        plan_name="Plan Test",
        plan_version_id=VERSION_ID,
        as_of_day="2026-03-10",
        plan_rows=[],
        plan_only_keys=[],
        pull_ids=["pull_b"],
        context_events=[],
    )
    assert "2026-03-10" in comment


def test_comment_version_cited():
    """La version du plan est citée dans le commentaire (AD-9 provenance)."""
    from core.narrative import build_mediaplan_pacing_comment  # noqa: PLC0415

    comment = build_mediaplan_pacing_comment(
        block_data={},
        plan_id=PLAN_ID,
        plan_name="Plan Test",
        plan_version_id=VERSION_ID,
        as_of_day="2026-03-10",
        plan_rows=[],
        plan_only_keys=[],
        pull_ids=["pull_b"],
        context_events=[],
    )
    assert VERSION_ID in comment


def test_comment_plan_only_signaled():
    """Les lignes plan-only sont signalées « plan seul » dans le commentaire."""
    from core.narrative import build_mediaplan_pacing_comment  # noqa: PLC0415

    comment = build_mediaplan_pacing_comment(
        block_data={},
        plan_id=PLAN_ID,
        plan_name="Plan Test",
        plan_version_id=VERSION_ID,
        as_of_day="2026-03-10",
        plan_rows=[],
        plan_only_keys=["line-tv"],
        pull_ids=["pull_b"],
        context_events=[],
    )
    assert "plan seul" in comment.lower()
    assert "line-tv" in comment


def test_comment_context_missing_when_no_events():
    """Sans context_events → _CONTEXT_MISSING_LINE verbatim (AD-9)."""
    from core.narrative import build_mediaplan_pacing_comment  # noqa: PLC0415

    comment = build_mediaplan_pacing_comment(
        block_data={},
        plan_id=PLAN_ID,
        plan_name=None,
        plan_version_id=None,
        as_of_day=None,
        plan_rows=[],
        plan_only_keys=[],
        pull_ids=[],
        context_events=[],
    )
    assert "Contexte manquant" in comment


def test_comment_under_3_lines():
    """Le commentaire est ≤3 lignes (pattern 9.7)."""
    from core.narrative import build_mediaplan_pacing_comment  # noqa: PLC0415

    comment = build_mediaplan_pacing_comment(
        block_data={},
        plan_id=PLAN_ID,
        plan_name="Plan Test",
        plan_version_id=VERSION_ID,
        as_of_day="2026-03-10",
        plan_rows=[{"pace": 0.25, "actual_to_date": 1250.0, "budget": 8000.0}],
        plan_only_keys=["line-tv"],
        pull_ids=["pull_a"],
        context_events=[],
    )
    assert len(comment.splitlines()) <= 3


def test_unmapped_actuals_block_present_with_honest_note_when_unavailable(card_db):
    """E1-F-2 : le bloc [Unmapped Actuals] existe TOUJOURS, jamais omis en silence.

    Dans le harnais offline, ni le PG réel (MagicMock) ni fact_daily_kpi ne sont
    disponibles -> le bloc est rendu avec la note honnête « Périmètre non
    vérifiable », JAMAIS absent (un bloc manquant lirait « 100 % mappé »).
    """
    from core.cards import get_template  # noqa: PLC0415

    tpl = get_template("mediaplan_pacing")
    summary, envelope, _ = card_db._resolve_mediaplan_pacing_card(
        tpl, PROJECT_ID, PLAN_ID, context_events=[], alerts=[], trace_id=None,
    )
    composition = envelope["data"]["composition"]
    block = next(b for b in composition if b["binding"]["source"] == "plan_unmapped_actuals")
    assert block["data"]["verifiable"] is False
    assert "non vérifiable" in block["data"]["note"]
    # Le résumé LLM cite le périmètre non mappé en 1 ligne.
    assert "Actuals non mappés" in summary


def test_pace_null_never_displayed_as_zero(card_db):
    """pace NULL ne se retrouve pas en 0.0 dans les données (jamais 0 déguisé)."""
    from core.cards import get_template  # noqa: PLC0415

    tpl = get_template("mediaplan_pacing")
    _, envelope, _ = card_db._resolve_mediaplan_pacing_card(
        tpl, PROJECT_ID, PLAN_ID, context_events=[], alerts=[], trace_id=None,
    )
    composition = envelope["data"]["composition"]
    line_table = next(b for b in composition if b["binding"]["source"] == "plan_lines")
    for row in line_table["data"]["rows"]:
        if row.get("is_plan_only"):
            # AD-9 CRITIQUE : jamais un 0 déguisé pour pace / consumed / extrapolated
            assert row["pace_pct"] is None, f"pace_pct non-None pour plan-only {row['line_key']}"
            assert row["consumed_pct"] is None
            assert row["extrapolated_spend"] is None


# ---------------------------------------------------------------------------
# 4. Fixture AI-54 — snapshot du vrai get_card chargé par le test
#    (jamais écrit à la main : construit par _resolve_mediaplan_pacing_card)
# ---------------------------------------------------------------------------

def test_ai54_fixture_get_card_snapshot(card_db):
    """AI-54 : charge la fixture via le VRAI _resolve_mediaplan_pacing_card."""
    from core.cards import get_template  # noqa: PLC0415

    tpl = get_template("mediaplan_pacing")
    _, envelope, _ = card_db._resolve_mediaplan_pacing_card(
        tpl, PROJECT_ID, PLAN_ID, context_events=[], alerts=[], trace_id=None,
    )
    # Snapshot shape assertions (contrat AI-54).
    assert envelope["schema_version"] == "1"
    assert envelope["data"]["card_id"] == "mediaplan_pacing"
    assert envelope["data"]["plan_id"] == PLAN_ID
    assert envelope["data"]["plan_version_id"] == VERSION_ID
    assert envelope["data"]["as_of_day"] == "2026-03-10"
    # Composition : 4 blocs (3 tables dont « Actuals non mappés » + 1 comment).
    assert len(envelope["data"]["composition"]) == 4
    types = [b["type"] for b in envelope["data"]["composition"]]
    assert types == ["table", "table", "table", "comment"]
    # E1-F-2 : le bloc [Unmapped Actuals] est TOUJOURS présent (jamais omis).
    unmapped_block = next(
        b for b in envelope["data"]["composition"]
        if b["binding"]["source"] == "plan_unmapped_actuals"
    )
    assert unmapped_block["title"] == "Actuals non mappés"
    # pacing_meta (AD-9 provenance sur l'envelope).
    pacing_meta = envelope["data"]["pacing_meta"]
    assert pacing_meta["plan_version_id"] == VERSION_ID
    assert "Pace" in pacing_meta["pace_formula"]
    assert "Estimation" in pacing_meta["estimate_label"]
