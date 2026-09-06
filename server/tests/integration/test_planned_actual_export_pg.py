"""Story 62.2 -- the planned-versus-actual extract against a real store and warehouse.

WHAT THIS PROVES THAT THE UNIT SUITE CANNOT. `test_planned_actual_export.py`
scripts the connection and answers the mart from a list, so it proves the gates
and the four counts. Here the media plan, its published version, its lines and
its placement matches are ROWS in Postgres under the real triggers of migration
040, the consolidated variance is a relation in DuckDB, and the file is composed
by reading it. Then the whole extract is run again while `pg_stat_xact_user_tables`
is watched on both sides of the call -- the claim of the story is that a read is a
read, and it cannot be made against a stub.

THE FIXTURE CANNOT DRIFT FROM THE MART. `test_the_columns_read_are_columns_the_mart_declares`
parses `dbt/models/marts/plan_vs_actual_daily.sql` itself, so a DuckDB relation
built here with a column the mart does not publish -- or a column the export
selects and the mart dropped -- goes red instead of passing over a shape nobody
checked. « Un instrument ne mesure pas sa propre copie. »

NOTHING HERE NAMES A REAL DOMAIN, A REAL PLAN OR A REAL CONNECTOR.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from uuid import uuid4

import duckdb
import pytest
from ulid import ULID

_SERVER_DIR = Path(__file__).resolve().parents[2]
if str(_SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(_SERVER_DIR))

from core import planned_actual_export  # noqa: E402
from core.currency_refusal import REFUSAL_CROSS_CURRENCY  # noqa: E402
from core.mmm_export import ExportRefused  # noqa: E402
from core.planned_actual_export import (  # noqa: E402
    METRIC_ACTUAL,
    METRIC_PLANNED,
    REFUSAL_MAPPING_NOT_CONFIRMED,
    REFUSAL_MAPPING_ORPHANED,
    REFUSAL_MART_UNAVAILABLE,
    REFUSAL_PLAN_NOT_PUBLISHED,
    build_extract,
    extract_csv,
)

pytestmark = pytest.mark.pg

_AUTHOR = "story-62.2-harness"
_MART = "main_marts.plan_vs_actual_daily"
_START, _END = "2026-07-01", "2026-07-04"

#: The day the mart lands NOTHING on, on purpose -- the only calendar hole.
_EMPTY_DAY = "2026-07-03"

#: Every column `plan_vs_actual_daily.sql` publishes, in its own order. The
#: contract test below re-derives this list from the model and compares.
_MART_COLUMNS = (
    ("project_id", "VARCHAR"),
    ("plan_id", "VARCHAR"),
    ("plan_version_id", "VARCHAR"),
    ("line_key", "VARCHAR"),
    ("day", "DATE"),
    ("label", "VARCHAR"),
    ("channel", "VARCHAR"),
    ("currency", "VARCHAR"),
    ("plan_currency", "VARCHAR"),
    ("actual_currency", "VARCHAR"),
    ("reporting_currency", "VARCHAR"),
    ("money_policy_version_id", "VARCHAR"),
    ("money_gap_code", "VARCHAR"),
    ("budget", "DECIMAL(18,6)"),
    ("budget_micros", "BIGINT"),
    ("line_start_date", "DATE"),
    ("line_end_date", "DATE"),
    ("is_plan_only", "BOOLEAN"),
    ("sort_order", "BIGINT"),
    ("allocated_amount", "DECIMAL(18,6)"),
    ("allocated_micros", "BIGINT"),
    ("actual_amount", "DECIMAL(18,6)"),
    ("actual_micros", "BIGINT"),
    ("actual_withheld", "BOOLEAN"),
    ("native_currency", "VARCHAR"),
    ("fx_as_of_date_min", "DATE"),
    ("fx_as_of_date_max", "DATE"),
    ("fx_source", "VARCHAR"),
    ("fx_tier", "VARCHAR"),
    ("fx_method", "VARCHAR"),
    ("actual_pull_id", "VARCHAR"),
)


def _uid(prefix: str) -> str:
    return f"{prefix}_{ULID()}"


# ---------------------------------------------------------------------------
# The warehouse: the consolidated variance of one plan, as the mart publishes it.
# ---------------------------------------------------------------------------


def _row(
    ids,
    *,
    day,
    line_key,
    label,
    channel,
    allocated,
    actual,
    is_plan_only=False,
    withheld=False,
    gap=None,
    plan_currency="EUR",
    actual_currency="EUR",
    sort_order=1,
):
    return (
        ids["project"], ids["plan"], ids["version"], line_key, day, label, channel,
        plan_currency, plan_currency, actual_currency if actual is not None else None,
        "EUR", "mp_EXAMPLE", gap,
        300, 300_000_000, "2026-07-01", "2026-07-04", is_plan_only, sort_order,
        allocated, None if allocated is None else int(allocated * 1_000_000),
        actual, None if actual is None else int(actual * 1_000_000),
        withheld, "EUR", None, None, None, None, None,
        "pull_EXAMPLE" if (actual is not None or withheld) else None,
    )


@pytest.fixture()
def ids():
    return {
        "org": _uid("org"),
        "project": _uid("proj"),
        "plan": str(uuid4()),
        "version": str(uuid4()),
    }


@pytest.fixture()
def warehouse(tmp_path, monkeypatch, ids):
    """The mart, with one day of each of the four things a day can be."""
    path = tmp_path / "pva.duckdb"
    con = duckdb.connect(str(path))
    con.execute("CREATE SCHEMA IF NOT EXISTS main_marts")
    con.execute(
        f"CREATE TABLE {_MART} ("
        + ", ".join(f"{name} {kind}" for name, kind in _MART_COLUMNS)
        + ")"
    )
    rows = [
        # planned and observed -- the ordinary day.
        _row(ids, day="2026-07-01", line_key="line-a", label="Brand awareness",
             channel="social", allocated=100, actual=90),
        # planned, nothing observed, money statable -- an UNDER-DELIVERY.
        _row(ids, day="2026-07-02", line_key="line-a", label="Brand awareness",
             channel="social", allocated=100, actual=None),
        # 2026-07-03 lands NOTHING, on purpose -- the only calendar hole.
        # planned, and the money could not be stated -- a HOLE, with its code.
        _row(ids, day="2026-07-04", line_key="line-a", label="Brand awareness",
             channel="social", allocated=100, actual=None, withheld=True,
             gap="fx_rate_unavailable"),
        # a plan-only line: no actuals source exists, so neither of the two.
        _row(ids, day="2026-07-01", line_key="line-b", label="TV burst",
             channel="tv", allocated=50, actual=None, is_plan_only=True,
             sort_order=2),
        # observed with nothing planned -- spend nobody budgeted, NAMED.
        _row(ids, day="2026-07-01", line_key="line-c", label="Retargeting",
             channel="social", allocated=None, actual=30, sort_order=3),
    ]
    con.executemany(
        f"INSERT INTO {_MART} VALUES ("
        + ",".join("?" for _ in _MART_COLUMNS)
        + ")",
        rows,
    )
    con.close()
    monkeypatch.setenv("TOOROW_DB_MODE", "duckdb")
    monkeypatch.setenv("TOOROW_DUCKDB_PATH", str(path))
    monkeypatch.setenv("TOOROW_ORG_SCHEMAS", "0")
    return str(path)


# ---------------------------------------------------------------------------
# The store: a real plan, a real published version, real matches.
# ---------------------------------------------------------------------------


def _seed(conn, ids, *, status="published", active=True, mappings=(("line-a", "active"),)):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO app.organizations (id, name, slug, created_by) VALUES (%s,%s,%s,%s)",
            (ids["org"], "62.2", ids["org"].lower().replace("_", "-"), _AUTHOR))
        cur.execute(
            "INSERT INTO app.projects (id, org_id, name, slug, created_by) "
            "VALUES (%s,%s,%s,%s,%s)",
            (ids["project"], ids["org"], "62.2",
             ids["project"].lower().replace("_", "-"), _AUTHOR))
        cur.execute(
            "INSERT INTO app.media_plans (id, project_id, name, currency, created_by) "
            "VALUES (%s,%s,%s,%s,%s)",
            (ids["plan"], ids["project"], "Q3 brand", "EUR", _AUTHOR))
        cur.execute(
            "INSERT INTO app.media_plan_versions "
            "(id, plan_id, version_number, status, is_active, created_by) "
            "VALUES (%s,%s,1,%s,%s,%s)",
            (ids["version"], ids["plan"], status, active, _AUTHOR))
        for line_key, label, channel, order in (
            ("line-a", "Brand awareness", "social", 1),
            ("line-b", "TV burst", "tv", 2),
            ("line-c", "Retargeting", "social", 3),
        ):
            cur.execute(
                "INSERT INTO app.media_plan_lines "
                "(id, version_id, line_key, label, channel, start_date, end_date, "
                " budget, is_plan_only, sort_order) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (str(uuid4()), ids["version"], line_key, label, channel,
                 "2026-07-01", "2026-07-04", 300, line_key == "line-b", order))
        for line_key, match_status in mappings:
            cur.execute(
                "INSERT INTO app.plan_line_mappings "
                "(id, plan_id, line_key, connector, campaign_ref, split_weight, "
                " status, match_method, created_by) "
                "VALUES (%s,%s,%s,'meta-ads',%s,1.0,%s,'exact',%s)",
                (str(uuid4()), ids["plan"], line_key, f"c-{line_key}",
                 match_status, _AUTHOR))
    conn.commit()
    return ids


def _extract(conn, ids, **kwargs):
    call = {
        "project_id": ids["project"],
        "plan_id": ids["plan"],
        "start": _START,
        "end": _END,
    }
    call.update(kwargs)
    return build_extract(conn, **call)


# ---------------------------------------------------------------------------
# The fixture is the mart's own shape, or this whole file proves nothing.
# ---------------------------------------------------------------------------


def test_the_columns_read_are_columns_the_mart_declares():
    """The export's projection, checked against the model file itself.

    `plan_vs_actual_daily.sql` names every column it publishes in the
    `toorow_absent_source_stub` list it falls back to when the plan mirror is not
    in the warehouse, so that list IS the model's own column contract. A column
    this export selects and the mart no longer publishes would otherwise fail
    only in production.
    """
    model = (
        Path(__file__).resolve().parents[3]
        / "dbt" / "models" / "marts" / "plan_vs_actual_daily.sql"
    ).read_text(encoding="utf-8")
    stub = model.split("toorow_absent_source_stub", 1)[1]
    declared = set(re.findall(r"\['([a-z_]+)',\s*'[a-z()0-9,]+'\]", stub))
    assert len(declared) > 20, sorted(declared)
    assert set(planned_actual_export._COLUMNS) <= declared, (
        sorted(set(planned_actual_export._COLUMNS) - declared)
    )
    assert {name for name, _ in _MART_COLUMNS} == declared, (
        sorted({name for name, _ in _MART_COLUMNS} ^ declared)
    )


# ---------------------------------------------------------------------------
# The file, composed off the real relation.
# ---------------------------------------------------------------------------


def test_the_file_is_long_and_every_row_carries_both_pins(live_postgres, warehouse, ids):
    _seed(live_postgres, ids)
    payload = _extract(live_postgres, ids)

    assert payload["columns"] == [
        "date", "plan_line_key", "plan_line_label", "channel",
        "metric", "value", "currency",
        "plan_version_id", "placement_mapping_fingerprint",
    ]
    fingerprint = payload["provenance"]["placement_mapping"]["fingerprint"]
    for row in payload["rows"]:
        assert row["plan_version_id"] == ids["version"]
        assert row["placement_mapping_fingerprint"] == fingerprint
        assert row["currency"] == "EUR"
        assert row["metric"] in (METRIC_PLANNED, METRIC_ACTUAL)
        assert row["value"] is not None


def test_the_rows_are_the_five_mart_rows_melted_and_nothing_else(
    live_postgres, warehouse, ids
):
    _seed(live_postgres, ids)
    payload = _extract(live_postgres, ids)
    got = {
        (row["date"], row["plan_line_key"], row["metric"], float(row["value"]))
        for row in payload["rows"]
    }
    assert got == {
        ("2026-07-01", "line-a", METRIC_PLANNED, 100.0),
        ("2026-07-01", "line-a", METRIC_ACTUAL, 90.0),
        ("2026-07-02", "line-a", METRIC_PLANNED, 100.0),
        ("2026-07-04", "line-a", METRIC_PLANNED, 100.0),
        ("2026-07-01", "line-b", METRIC_PLANNED, 50.0),
        ("2026-07-01", "line-c", METRIC_ACTUAL, 30.0),
    }
    assert payload["provenance"]["source_row_count"] == 5


def test_a_planned_day_without_spend_is_an_under_delivery_and_not_a_hole(
    live_postgres, warehouse, ids
):
    """The rule this story adds, against the mart's own two NULL shapes."""
    _seed(live_postgres, ids)
    coverage = _extract(live_postgres, ids)["provenance"]["date_coverage"]

    assert coverage["variance_days"]["count"] == 1
    assert coverage["variance_days"]["entries"] == [
        {"date": "2026-07-02", "plan_line_key": "line-a"}
    ]
    # The withheld day carries its reason and IS a hole; the plan-only day is
    # neither; the empty calendar day is the other hole.
    assert coverage["withheld_days"]["entries"] == [
        {"date": "2026-07-04", "plan_line_key": "line-a",
         "money_gap_code": "fx_rate_unavailable"}
    ]
    assert coverage["plan_only_days"]["count"] == 1
    assert coverage["days_with_no_row"]["entries"] == [{"date": _EMPTY_DAY}]
    assert coverage["total_holes"] == 2
    assert "under-delivery" in coverage["message"]


def test_spend_nobody_planned_is_named(live_postgres, warehouse, ids):
    _seed(live_postgres, ids)
    coverage = _extract(live_postgres, ids)["provenance"]["date_coverage"]
    assert coverage["unplanned_days"]["entries"] == [
        {"date": "2026-07-01", "plan_line_key": "line-c"}
    ]


def test_the_provenance_names_the_plan_the_version_and_the_match_set(
    live_postgres, warehouse, ids
):
    _seed(live_postgres, ids)
    provenance = _extract(live_postgres, ids)["provenance"]
    assert provenance["media_plan_id"] == ids["plan"]
    assert provenance["media_plan_version_id"] == ids["version"]
    assert provenance["media_plan_version_status"] == "published"
    mapping = provenance["placement_mapping"]
    assert mapping["active_match_count"] == 1
    assert mapping["orphaned_match_count"] == 0
    assert mapping["match_methods"] == {"exact": 1}
    assert len(mapping["fingerprint"]) == 64
    assert provenance["currency"]["currency"] == "EUR"
    assert provenance["actual_pull_ids"] == ["pull_EXAMPLE"]
    assert provenance["window"] == {"start": _START, "end": _END, "days": 4}


def test_the_csv_holds_exactly_the_rows_the_read_returned(live_postgres, warehouse, ids):
    _seed(live_postgres, ids)
    payload = _extract(live_postgres, ids)
    lines = extract_csv(payload).decode("utf-8").strip().splitlines()
    assert lines[0] == ",".join(payload["columns"])
    assert len(lines) == 1 + len(payload["rows"])
    assert all("EUR" in line for line in lines[1:])


def test_the_extract_writes_nothing_anywhere(live_postgres, warehouse, ids):
    """A read is a read. Nothing in `app` moves, and no relation is created."""
    _seed(live_postgres, ids)
    with live_postgres.cursor() as cur:
        cur.execute(
            "SELECT relname, n_tup_ins + n_tup_upd + n_tup_del FROM pg_stat_xact_user_tables"
        )
        before = dict(cur.fetchall())
    _extract(live_postgres, ids)
    with live_postgres.cursor() as cur:
        cur.execute(
            "SELECT relname, n_tup_ins + n_tup_upd + n_tup_del FROM pg_stat_xact_user_tables"
        )
        after = dict(cur.fetchall())
    moved = {
        name: (before.get(name, 0), count)
        for name, count in after.items()
        if count != before.get(name, 0)
    }
    assert moved == {}, moved

    con = duckdb.connect(warehouse, read_only=True)
    try:
        tables = {row[0] for row in con.execute(
            "SELECT table_name FROM information_schema.tables"
        ).fetchall()}
    finally:
        con.close()
    assert tables == {"plan_vs_actual_daily"}, tables


# ---------------------------------------------------------------------------
# The gates, against the real stores.
# ---------------------------------------------------------------------------


def test_a_candidate_version_refuses_before_the_warehouse_is_read(
    live_postgres, warehouse, ids
):
    _seed(live_postgres, ids, status="candidate")
    with pytest.raises(ExportRefused) as caught:
        _extract(live_postgres, ids)
    assert caught.value.code == REFUSAL_PLAN_NOT_PUBLISHED
    assert caught.value.detail["media_plan_version_status"] == "candidate"


def test_a_plan_with_no_active_version_refuses(live_postgres, warehouse, ids):
    _seed(live_postgres, ids, active=False)
    with pytest.raises(ExportRefused) as caught:
        _extract(live_postgres, ids)
    assert caught.value.code == REFUSAL_PLAN_NOT_PUBLISHED


def test_a_plan_of_another_project_is_not_found(live_postgres, warehouse, ids):
    _seed(live_postgres, ids)
    with pytest.raises(ExportRefused) as caught:
        _extract(live_postgres, ids, project_id=_uid("proj"))
    assert caught.value.code == "media_plan_not_found"


def test_an_orphaned_match_refuses_and_names_the_line(live_postgres, warehouse, ids):
    _seed(
        live_postgres, ids,
        mappings=(("line-a", "active"), ("line-gone", "orphaned")),
    )
    with pytest.raises(ExportRefused) as caught:
        _extract(live_postgres, ids)
    assert caught.value.code == REFUSAL_MAPPING_ORPHANED
    assert caught.value.detail["orphaned_line_keys"] == ["line-gone"]


def test_a_plan_with_no_match_at_all_refuses(live_postgres, warehouse, ids):
    _seed(live_postgres, ids, mappings=())
    with pytest.raises(ExportRefused) as caught:
        _extract(live_postgres, ids)
    assert caught.value.code == REFUSAL_MAPPING_NOT_CONFIRMED


def test_a_window_the_mart_does_not_cover_refuses_rather_than_serving_a_blank_file(
    live_postgres, warehouse, ids
):
    _seed(live_postgres, ids)
    with pytest.raises(ExportRefused) as caught:
        _extract(live_postgres, ids, start="2026-09-01", end="2026-09-02")
    assert caught.value.code == REFUSAL_MART_UNAVAILABLE


def test_two_currencies_over_the_window_refuse_the_file(
    live_postgres, warehouse, ids
):
    """The mart's own mismatch code, read off a real relation."""
    _seed(live_postgres, ids)
    con = duckdb.connect(warehouse)
    con.execute(
        f"UPDATE {_MART} SET plan_currency = 'USD', "
        "money_gap_code = 'plan_currency_mismatch' WHERE line_key = 'line-a'"
    )
    con.close()
    with pytest.raises(ExportRefused) as caught:
        _extract(live_postgres, ids)
    assert caught.value.code == REFUSAL_CROSS_CURRENCY


def test_a_read_over_the_cap_refuses_and_truncates_nothing(
    live_postgres, warehouse, ids, monkeypatch
):
    """The LIMIT asks for one row over the bound so the refusal is COUNTED."""
    _seed(live_postgres, ids)
    monkeypatch.setattr(planned_actual_export, "MAX_SOURCE_ROWS", 3)
    with pytest.raises(ExportRefused) as caught:
        _extract(live_postgres, ids)
    assert caught.value.code == "extract_larger_than_one_file"
    assert "Nothing was truncated" in caught.value.message
