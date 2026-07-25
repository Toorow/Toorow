"""Tests for the Story 22.4 plan-vs-actual pacing read (FR38 / CAP-26).

Offline, à la test_warehouse.py: a real DuckDB is built on the fly with the three
pacing marts materialised at main_marts.* -- the schema DERIVES FROM the dbt models
plan_pacing_by_line / _by_channel / _by_plan (lesson 19.4: stand-ins are faithful to
the real dbt schemas, never invented). The rows encode the story's Given/When/Then:

  * line-digital-a: budget 3000 EUR / 30 j, 10 j écoulés, actual 1250 EUR ->
      consumed 41,7 %, pace +25 %, remaining 1750, extrapolated 3750;
  * a SHARED campaign split 0.5/0.5 -> line-digital-a 1250, line-digital-b 250
      (100 partagé donne 50/50, jamais 100/100 -- ventilation exacte, total 100);
  * line-tv (plan-only): actual / pace NULL (no pacing);
  * channel 'digital' actual = 1250 + 250 = 1500 == somme exacte des lignes.

These tests prove the READER (query_plan_vs_actual: scoping, provenance passthrough,
degrade contract) + the API seam. The MART LOGIC (ventilation re-sums to origin, pace
NULL at alloc 0, plan-only actual NULL, channel == Σ lines) is proven by the dbt
singular tests against the REAL compiled marts (dbt/tests/test_plan_*).
"""

from __future__ import annotations

import importlib

import pytest

pytest.importorskip("duckdb")


# ---------------------------------------------------------------------------
# Faithful stand-in DuckDB (schema derived from the dbt pacing models).
# ---------------------------------------------------------------------------

# main_marts.* is the DuckDB schema dbt materialises marts into (see warehouse.
# _duckdb_mart_prefix -> "main_marts.").
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
OTHER_PLAN_ID = "00000000-0000-4000-8000-000000000099"

# consumed 1250/3000, pace (1250-1000)/1000, remaining 3000-1250, extrapolated 1250/10*30
_C_A = 1250.0 / 3000.0
_C_B = 250.0 / 1500.0


def _seed_pacing_db(path: str) -> None:
    import duckdb  # noqa: PLC0415

    con = duckdb.connect(path)
    try:
        con.execute(_DDL)
        # by-line: the story's three lines (+ one row of a DIFFERENT plan to prove scoping).
        con.executemany(
            """
            INSERT INTO main_marts.plan_pacing_by_line VALUES
            (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            [
                # line-digital-a: 3000/30j, 10j, 1250 -> 41,7% / +25% / 1750 / 3750.
                (PROJECT_ID, PLAN_ID, VERSION_ID, "line-digital-a", "Digital A", "digital",
                 "EUR", 3000.0, "2026-03-01", "2026-03-30", False, 0,
                 "2026-03-10", 30, 10, 1000.0, 1250.0, _C_A, 0.25, 1750.0, 3750.0,
                 "pull_a", "pull_b", 2),
                # line-digital-b: shared 0.5 -> 250. pace (250-500)/500 = -0.5.
                (PROJECT_ID, PLAN_ID, VERSION_ID, "line-digital-b", "Digital B", "digital",
                 "EUR", 1500.0, "2026-03-01", "2026-03-30", False, 1,
                 "2026-03-10", 30, 10, 500.0, 250.0, _C_B, -0.5, 1250.0, 750.0,
                 "pull_b", "pull_b", 1),
                # line-tv plan-only: actual / pace NULL (no pacing).
                (PROJECT_ID, PLAN_ID, VERSION_ID, "line-tv", "TV Brand", "tv",
                 "EUR", 5000.0, "2026-03-01", "2026-03-30", True, 2,
                 "2026-03-10", 30, 10, 5000.0, None, None, None, None, None,
                 None, None, 0),
                # a line of ANOTHER plan (must NOT leak into this plan's read).
                (PROJECT_ID, OTHER_PLAN_ID, "v-other", "line-other", "Other", "digital",
                 "EUR", 999.0, "2026-03-01", "2026-03-30", False, 0,
                 "2026-03-10", 30, 10, 100.0, 100.0, 0.1, 0.0, 899.0, 300.0,
                 "pull_x", "pull_x", 1),
            ],
        )
        # by-channel: digital = 1250+250 = 1500 (Σ exacte des lignes ventilées); tv plan-only.
        con.executemany(
            """
            INSERT INTO main_marts.plan_pacing_by_channel VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            [
                (PROJECT_ID, PLAN_ID, VERSION_ID, "digital",
                 4500.0, 1500.0, 1500.0, 3000.0, 1500.0 / 4500.0, 0.0, 4500.0,
                 0, 2, "pull_a", "pull_b", 3),
                (PROJECT_ID, PLAN_ID, VERSION_ID, "tv",
                 5000.0, None, None, None, None, None, None,
                 1, 0, None, None, 0),
            ],
        )
        # by-plan: single row.
        con.executemany(
            """
            INSERT INTO main_marts.plan_pacing_by_plan VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            [
                (PROJECT_ID, PLAN_ID, VERSION_ID, "EUR", "2026-03-10",
                 9500.0, 1500.0, 1500.0, 8000.0, 1500.0 / 9500.0, 0.0, 4500.0,
                 1, 2, "pull_a", "pull_b", 3),
            ],
        )
    finally:
        con.close()


@pytest.fixture
def pacing_db(tmp_path, monkeypatch):
    db_file = tmp_path / "pacing.duckdb"
    _seed_pacing_db(str(db_file))
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(db_file))
    monkeypatch.setenv("TOOROW_CACHE_ENABLED", "false")
    from core import warehouse  # noqa: PLC0415
    importlib.reload(warehouse)
    return warehouse


# ---------------------------------------------------------------------------
# query_plan_vs_actual -- happy path (the story numbers) + scoping + provenance
# ---------------------------------------------------------------------------

def test_pacing_returns_lines_channels_plan(pacing_db):
    result = pacing_db.query_plan_vs_actual(project_id=PROJECT_ID, plan_id=PLAN_ID)
    assert set(result.keys()) == {"lines", "channels", "plan"}
    # scoping: only THIS plan's lines (the OTHER_PLAN line is excluded).
    line_keys = {r["line_key"] for r in result["lines"]}
    assert line_keys == {"line-digital-a", "line-digital-b", "line-tv"}
    assert "line-other" not in line_keys


def test_pacing_worked_example_line_a(pacing_db):
    """3000/30j, 10j, 1250 -> consumed 41,7 %, pace +25 %, remaining 1750, extrapolated 3750."""
    result = pacing_db.query_plan_vs_actual(project_id=PROJECT_ID, plan_id=PLAN_ID)
    a = next(r for r in result["lines"] if r["line_key"] == "line-digital-a")
    assert a["actual_to_date"] == pytest.approx(1250.0)
    assert a["allocated_to_date"] == pytest.approx(1000.0)
    assert a["consumed_pct"] == pytest.approx(0.41666666, abs=1e-4)
    assert a["pace"] == pytest.approx(0.25, abs=1e-6)
    assert a["remaining_budget"] == pytest.approx(1750.0)
    assert a["extrapolated_spend"] == pytest.approx(3750.0)
    # provenance carried through (AD-9).
    assert a["plan_version_id"] == VERSION_ID
    assert a["actual_pull_id_count"] == 2


def test_pacing_shared_campaign_split_50_50(pacing_db):
    """Shared campaign 0.5/0.5 ventilates 50/50 -> line-b 250, never 500 (no double count)."""
    result = pacing_db.query_plan_vs_actual(project_id=PROJECT_ID, plan_id=PLAN_ID)
    b = next(r for r in result["lines"] if r["line_key"] == "line-digital-b")
    assert b["actual_to_date"] == pytest.approx(250.0)
    a = next(r for r in result["lines"] if r["line_key"] == "line-digital-a")
    # line-a (100% solo 1000 + 0.5*shared 250) + line-b (0.5*shared 250) = shared total 500
    # ventilated exactly once (250+250), plus the solo 1000 -> 1500 total (channel sum below).
    assert a["actual_to_date"] + b["actual_to_date"] == pytest.approx(1500.0)


def test_pacing_plan_only_actual_null(pacing_db):
    """A plan-only line carries budget but NULL actual / pace (never 0)."""
    result = pacing_db.query_plan_vs_actual(project_id=PROJECT_ID, plan_id=PLAN_ID)
    tv = next(r for r in result["lines"] if r["line_key"] == "line-tv")
    assert tv["is_plan_only"] is True
    assert tv["budget"] == pytest.approx(5000.0)
    assert tv["actual_to_date"] is None
    assert tv["pace"] is None
    assert tv["consumed_pct"] is None
    assert tv["extrapolated_spend"] is None


def test_pacing_channel_equals_sum_of_lines(pacing_db):
    """Channel 'digital' actual == exact sum of its ventilated lines (1250 + 250 = 1500)."""
    result = pacing_db.query_plan_vs_actual(project_id=PROJECT_ID, plan_id=PLAN_ID)
    digital = next(r for r in result["channels"] if r["channel"] == "digital")
    assert digital["actual_to_date"] == pytest.approx(1500.0)
    line_sum = sum(
        r["actual_to_date"] or 0.0
        for r in result["lines"]
        if r["channel"] == "digital"
    )
    assert digital["actual_to_date"] == pytest.approx(line_sum)


def test_pacing_channel_plan_only_null(pacing_db):
    """A fully plan-only channel ('tv') has NULL actual (never 0)."""
    result = pacing_db.query_plan_vs_actual(project_id=PROJECT_ID, plan_id=PLAN_ID)
    tv = next(r for r in result["channels"] if r["channel"] == "tv")
    assert tv["actual_to_date"] is None
    assert tv["pace"] is None
    assert tv["plan_only_line_count"] == 1


def test_pacing_plan_rollup_single_row(pacing_db):
    result = pacing_db.query_plan_vs_actual(project_id=PROJECT_ID, plan_id=PLAN_ID)
    assert len(result["plan"]) == 1
    p = result["plan"][0]
    assert p["plan_id"] == PLAN_ID
    assert p["actual_to_date"] == pytest.approx(1500.0)
    assert p["plan_version_id"] == VERSION_ID


def test_pacing_empty_when_unknown_plan(pacing_db):
    """A present-but-unmatched plan_id returns empty lists (honest empty, not an error)."""
    result = pacing_db.query_plan_vs_actual(
        project_id=PROJECT_ID, plan_id="00000000-0000-4000-8000-0000000000ff"
    )
    assert result == {"lines": [], "channels": [], "plan": []}


# ---------------------------------------------------------------------------
# Degrade contract (AD-9): missing file / missing relation -> WarehouseUnavailable
# ---------------------------------------------------------------------------

def test_pacing_raises_when_duckdb_absent(tmp_path, monkeypatch):
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(tmp_path / "nope.duckdb"))
    from core import warehouse  # noqa: PLC0415
    importlib.reload(warehouse)
    with pytest.raises(warehouse.WarehouseUnavailable):
        warehouse.query_plan_vs_actual(project_id=PROJECT_ID, plan_id=PLAN_ID)


def test_pacing_raises_when_relation_absent(tmp_path, monkeypatch):
    """A DuckDB file WITHOUT the pacing relations raises (never a silent [])."""
    import duckdb  # noqa: PLC0415
    db_file = tmp_path / "empty.duckdb"
    duckdb.connect(str(db_file)).close()  # file exists but has no main_marts.* relations
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(db_file))
    from core import warehouse  # noqa: PLC0415
    importlib.reload(warehouse)
    with pytest.raises(warehouse.WarehouseUnavailable):
        warehouse.query_plan_vs_actual(project_id=PROJECT_ID, plan_id=PLAN_ID)


def test_pacing_query_scopes_by_project_and_plan_bound_params(pacing_db):
    """project_id / plan_id are BOUND params, never interpolated (HG-3)."""
    sql, params = pacing_db._build_plan_pacing_query(
        "main_marts.", "plan_pacing_by_line", PROJECT_ID, PLAN_ID
    )
    assert PLAN_ID not in sql
    assert PROJECT_ID not in sql or PROJECT_ID == "default"  # 'default' is a common word
    assert params == [PROJECT_ID, PLAN_ID]
    assert "plan_pacing_by_line" in sql


def test_pacing_query_rejects_unknown_relation(pacing_db):
    with pytest.raises(ValueError):
        pacing_db._build_plan_pacing_query(
            "main_marts.", "fact_daily_kpi", PROJECT_ID, PLAN_ID
        )


# ---------------------------------------------------------------------------
# API seam -- the GET /pacing handler returns the reader's shape + 503 on failure.
# Offline: query_plan_vs_actual is patched; the DB auth path is stubbed.
# ---------------------------------------------------------------------------

@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_api_get_pacing_happy(monkeypatch):
    from unittest.mock import MagicMock, patch  # noqa: PLC0415

    from core import mediaplan_api  # noqa: PLC0415

    async def _auth_ok(_request):
        return True, "user@toorow.test"

    monkeypatch.setattr(mediaplan_api, "_check_auth", _auth_ok)
    monkeypatch.setattr(mediaplan_api, "_plan_project_id", lambda conn, plan_id: PROJECT_ID)
    monkeypatch.setattr(mediaplan_api, "_check_project_role", lambda *a, **k: True)

    fake_result = {
        "lines": [{"line_key": "line-digital-a", "actual_to_date": 1250.0}],
        "channels": [{"channel": "digital", "actual_to_date": 1500.0}],
        "plan": [{"plan_id": PLAN_ID, "actual_to_date": 1500.0}],
    }

    ctx = MagicMock()
    ctx.__enter__ = MagicMock(return_value=MagicMock())
    ctx.__exit__ = MagicMock(return_value=False)

    request = MagicMock()
    request.path_params = {"plan_id": PLAN_ID}

    with patch("core.db.get_connection", return_value=ctx), \
         patch("core.warehouse.query_plan_vs_actual", return_value=fake_result):
        resp = await mediaplan_api._get_pacing(request)

    assert resp.status_code == 200
    import json  # noqa: PLC0415
    body = json.loads(resp.body)
    assert body["plan_id"] == PLAN_ID
    assert body["lines"][0]["line_key"] == "line-digital-a"
    assert body["channels"][0]["actual_to_date"] == 1500.0


@pytest.mark.anyio
async def test_api_get_pacing_503_on_warehouse_unavailable(monkeypatch):
    from unittest.mock import MagicMock, patch  # noqa: PLC0415

    from core import mediaplan_api  # noqa: PLC0415
    from core.warehouse import WarehouseUnavailable  # noqa: PLC0415

    async def _auth_ok(_request):
        return True, "user@toorow.test"

    monkeypatch.setattr(mediaplan_api, "_check_auth", _auth_ok)
    monkeypatch.setattr(mediaplan_api, "_plan_project_id", lambda conn, plan_id: PROJECT_ID)
    monkeypatch.setattr(mediaplan_api, "_check_project_role", lambda *a, **k: True)

    ctx = MagicMock()
    ctx.__enter__ = MagicMock(return_value=MagicMock())
    ctx.__exit__ = MagicMock(return_value=False)

    request = MagicMock()
    request.path_params = {"plan_id": PLAN_ID}

    def _raise(*a, **k):
        raise WarehouseUnavailable("down")

    with patch("core.db.get_connection", return_value=ctx), \
         patch("core.warehouse.query_plan_vs_actual", side_effect=_raise):
        resp = await mediaplan_api._get_pacing(request)

    assert resp.status_code == 503
    import json  # noqa: PLC0415
    assert json.loads(resp.body)["code"] == "warehouse_unavailable"


@pytest.mark.anyio
async def test_api_get_pacing_404_on_bad_uuid(monkeypatch):
    from unittest.mock import MagicMock  # noqa: PLC0415

    from core import mediaplan_api  # noqa: PLC0415

    async def _auth_ok(_request):
        return True, "user@toorow.test"

    monkeypatch.setattr(mediaplan_api, "_check_auth", _auth_ok)

    request = MagicMock()
    request.path_params = {"plan_id": "not-a-uuid"}
    resp = await mediaplan_api._get_pacing(request)
    assert resp.status_code == 404
