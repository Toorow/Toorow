"""Story 62.2 -- what the planned-versus-actual extract refuses, and what its file says.

Every test here asserts a PROPERTY OF THE CLASS: the order of the gates, each
refusal by its own code and gesture, the long row shape shared with the MMM
extract, and the one rule this story adds -- a planned day with no observed spend
is an UNDER-DELIVERY and never a hole. The warehouse half (a real mart, a real
read, a real melt) is proven against Postgres and DuckDB in
`server/tests/integration/test_planned_actual_export_pg.py`; what is proven here
is everything that decides BEFORE a row is read, plus the melt over rows the mart
would have produced.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SERVER_DIR = Path(__file__).resolve().parents[2]
if str(_SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(_SERVER_DIR))

from core import mmm_export, planned_actual_export  # noqa: E402

# THE REAL CONSTANTS, imported and never retyped -- a refusal respelled in a test
# keeps the suite green while the product answers another word.
from core.currency_refusal import (  # noqa: E402
    REFUSAL_CROSS_CURRENCY,
    REFUSAL_UNKNOWN_CURRENCY,
)
from core.mmm_export import ExportRefused  # noqa: E402
from core.planned_actual_export import (  # noqa: E402
    METRIC_ACTUAL,
    METRIC_PLANNED,
    REFUSAL_MAPPING_NOT_CONFIRMED,
    REFUSAL_MAPPING_ORPHANED,
    REFUSAL_MART_UNAVAILABLE,
    REFUSAL_NO_PLAN_ASKED,
    REFUSAL_PLAN_ARCHIVED,
    REFUSAL_PLAN_NOT_FOUND,
    REFUSAL_PLAN_NOT_PUBLISHED,
    build_extract,
    extract_csv,
)

PROJECT = "proj_EXAMPLE"
PLAN = "plan_EXAMPLE"
VERSION = "planv_EXAMPLE"
START, END = "2026-07-01", "2026-07-03"


# ---------------------------------------------------------------------------
# A scripted connection -- the shape `test_mmm_export` already uses.
# ---------------------------------------------------------------------------


class _Cursor:
    def __init__(self, script):
        self._script = script
        self._last = ""
        self.executed = []

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def execute(self, sql, params=None):
        self._last = sql
        self.executed.append((sql, params))

    def fetchone(self):
        for pattern, value in self._script:
            if pattern in self._last:
                return value
        return None

    def fetchall(self):
        for pattern, value in self._script:
            if pattern in self._last:
                return value
        return []


class _Conn:
    def __init__(self, script=()):
        self._script = list(script)
        self.cursors = []

    def cursor(self):
        cur = _Cursor(self._script)
        self.cursors.append(cur)
        return cur

    @property
    def statements(self):
        return [sql for cur in self.cursors for sql, _ in cur.executed]


def _plan_row(*, archived=None, version=VERSION, status="published"):
    #  id, name, currency, archived_at, version id, version number, status, active
    return (PLAN, "Q3 brand", "EUR", archived, version, 4, status, True)


def _mapping_rows(*, status="active", method="exact"):
    #  line_key, connector, campaign_ref, split_weight, status, method, score, updated
    return [
        ("line-a", "meta-ads", "c1", "1.000000", status, method, None, None),
    ]


def _conn(plan_row=None, mapping_rows=None):
    return _Conn(
        [
            ("app.media_plans", _plan_row() if plan_row is None else plan_row),
            (
                "app.plan_line_mappings",
                _mapping_rows() if mapping_rows is None else mapping_rows,
            ),
        ]
    )


def _mart_row(**over):
    row = {
        "plan_version_id": VERSION,
        "line_key": "line-a",
        "label": "Brand awareness",
        "channel": "social",
        "day": "2026-07-01",
        "is_plan_only": False,
        "sort_order": 1,
        "plan_currency": "EUR",
        "actual_currency": "EUR",
        "reporting_currency": "EUR",
        "money_policy_version_id": "mp_EXAMPLE",
        "money_gap_code": None,
        "budget": 300,
        "allocated_amount": 100,
        "actual_amount": 90,
        "actual_withheld": False,
        "actual_pull_id": "pull_EXAMPLE",
    }
    row.update(over)
    return row


@pytest.fixture()
def mart(monkeypatch):
    """The mart, answered from a list. The read itself is proven in the pg suite."""
    held: dict[str, list] = {"rows": [_mart_row()]}

    def _read(*, project_id, plan_id, first, last):
        return list(held["rows"])

    monkeypatch.setattr(planned_actual_export, "_read_mart", _read)
    return held


def _extract(conn=None, **kwargs):
    call = {
        "project_id": PROJECT,
        "plan_id": PLAN,
        "start": START,
        "end": END,
    }
    call.update(kwargs)
    return build_extract(conn if conn is not None else _conn(), **call)


# ---------------------------------------------------------------------------
# It is the SECOND READER of the seam, and that is checkable.
# ---------------------------------------------------------------------------


def test_the_envelope_is_the_mmm_seam_and_not_a_second_one():
    """Not one of these is respelled here -- they are the same objects."""
    assert planned_actual_export.ExportRefused is mmm_export.MmmExportRefused
    assert planned_actual_export.extract_csv is mmm_export.extract_csv
    assert planned_actual_export.MAX_SOURCE_ROWS is mmm_export.MAX_SOURCE_ROWS
    assert planned_actual_export.parse_window is mmm_export._window


def test_the_row_shape_shares_its_head_with_the_mmm_extract(mart):
    """`date`, the dimensions, `metric`, `value`, `currency` -- then the pins."""
    payload = _extract()
    assert payload["columns"][0] == "date"
    assert payload["columns"][-5:] == [
        "metric",
        "value",
        "currency",
        "plan_version_id",
        "placement_mapping_fingerprint",
    ]


def test_the_metric_axis_carries_planned_and_actual_and_no_variance_row(mart):
    payload = _extract()
    assert [row["metric"] for row in payload["rows"]] == [
        METRIC_PLANNED,
        METRIC_ACTUAL,
    ]
    assert "variance" not in {row["metric"] for row in payload["rows"]}
    assert "derived by the reader" in payload["provenance"]["variance"]


def test_every_row_carries_both_pins(mart):
    payload = _extract()
    fingerprint = payload["provenance"]["placement_mapping"]["fingerprint"]
    assert len(fingerprint) == 64
    for row in payload["rows"]:
        assert row["plan_version_id"] == VERSION
        assert row["placement_mapping_fingerprint"] == fingerprint


# ---------------------------------------------------------------------------
# Gate 1 -- the plan version is published and active.
# ---------------------------------------------------------------------------


def test_no_plan_named_refuses_before_any_read():
    conn = _Conn()
    with pytest.raises(ExportRefused) as caught:
        _extract(conn, plan_id="")
    assert caught.value.code == REFUSAL_NO_PLAN_ASKED
    assert conn.statements == []


def test_a_plan_of_another_project_is_not_found():
    with pytest.raises(ExportRefused) as caught:
        _extract(_Conn())
    assert caught.value.code == REFUSAL_PLAN_NOT_FOUND
    assert "Governance > Media plans" in caught.value.gesture


def test_an_archived_plan_refuses():
    conn = _conn(plan_row=_plan_row(archived="2026-06-01T00:00:00Z"))
    with pytest.raises(ExportRefused) as caught:
        _extract(conn)
    assert caught.value.code == REFUSAL_PLAN_ARCHIVED


def test_a_plan_with_no_active_version_refuses_with_the_gesture():
    conn = _conn(plan_row=_plan_row(version=None, status=None))
    with pytest.raises(ExportRefused) as caught:
        _extract(conn)
    assert caught.value.code == REFUSAL_PLAN_NOT_PUBLISHED
    assert "Publish a version" in caught.value.gesture


def test_a_candidate_active_version_refuses_and_names_its_status():
    conn = _conn(plan_row=_plan_row(status="candidate"))
    with pytest.raises(ExportRefused) as caught:
        _extract(conn)
    assert caught.value.code == REFUSAL_PLAN_NOT_PUBLISHED
    assert caught.value.detail["media_plan_version_status"] == "candidate"


# ---------------------------------------------------------------------------
# Gate 2 -- the placement mapping is confirmed. The gate proper to this extract.
# ---------------------------------------------------------------------------


def test_a_plan_with_no_active_match_refuses_rather_than_serving_zeroes():
    conn = _conn(mapping_rows=[])
    with pytest.raises(ExportRefused) as caught:
        _extract(conn)
    assert caught.value.code == REFUSAL_MAPPING_NOT_CONFIRMED
    assert "accuses a campaign that ran" in caught.value.message
    assert "Placements" in caught.value.gesture


def test_an_orphaned_match_refuses_and_names_the_line(mart):
    conn = _conn(mapping_rows=_mapping_rows() + [
        ("line-gone", "meta-ads", "c9", "1.000000", "orphaned", None, None, None),
    ])
    with pytest.raises(ExportRefused) as caught:
        _extract(conn)
    assert caught.value.code == REFUSAL_MAPPING_ORPHANED
    assert caught.value.detail["orphaned_line_keys"] == ["line-gone"]
    assert "Placements" in caught.value.gesture


def test_gate_two_runs_before_the_warehouse_is_touched(monkeypatch):
    """A declaration nobody confirmed stays true whatever the warehouse answers."""
    touched = []

    def _read(**kwargs):
        touched.append(kwargs)
        return []

    monkeypatch.setattr(planned_actual_export, "_read_mart", _read)
    with pytest.raises(ExportRefused):
        _extract(_conn(mapping_rows=[]))
    assert touched == []


def test_the_fingerprint_moves_when_a_weight_moves(mart):
    first = _extract()["provenance"]["placement_mapping"]["fingerprint"]
    second = _extract(
        _conn(mapping_rows=[
            ("line-a", "meta-ads", "c1", "0.500000", "active", "exact", None, None),
            ("line-a", "meta-ads", "c2", "0.500000", "active", "exact", None, None),
        ])
    )["provenance"]["placement_mapping"]["fingerprint"]
    assert first != second


def test_a_match_with_no_recorded_level_is_an_absence_and_never_manual(mart):
    payload = _extract(_conn(mapping_rows=_mapping_rows(method=None)))
    methods = payload["provenance"]["placement_mapping"]["match_methods"]
    assert methods == {"not_recorded": 1}
    assert "manual" not in methods


# ---------------------------------------------------------------------------
# Gate 3 -- the mart resolves, and an empty answer is refused, never served.
# ---------------------------------------------------------------------------


def test_an_empty_mart_for_a_published_plan_refuses_instead_of_a_blank_file(mart):
    mart["rows"] = []
    with pytest.raises(ExportRefused) as caught:
        _extract()
    assert caught.value.code == REFUSAL_MART_UNAVAILABLE
    assert "Rebuild this Project's warehouse" in caught.value.gesture


def test_a_read_over_the_cap_refuses_and_truncates_nothing(mart, monkeypatch):
    monkeypatch.setattr(planned_actual_export, "MAX_SOURCE_ROWS", 2)
    mart["rows"] = [_mart_row(line_key=f"l{i}") for i in range(4)]
    with pytest.raises(ExportRefused) as caught:
        _extract()
    assert caught.value.code == mmm_export.REFUSAL_TOO_MANY_ROWS
    assert "Nothing was truncated" in caught.value.message


def test_an_unreadable_mart_is_a_refusal_and_not_an_empty_extract(monkeypatch):
    def _boom(**kwargs):
        raise RuntimeError("relation absent")

    monkeypatch.setattr(planned_actual_export, "_read_mart", _boom)
    with pytest.raises(ExportRefused) as caught:
        _extract()
    assert caught.value.code == mmm_export.REFUSAL_WAREHOUSE_UNREADABLE
    assert "This is not an empty extract" in caught.value.message


# ---------------------------------------------------------------------------
# Gate 4 -- exactly one declared currency, in the mart's own two words.
# ---------------------------------------------------------------------------


def test_the_marts_own_mismatch_code_refuses_the_file(mart):
    mart["rows"] = [
        _mart_row(plan_currency="USD", money_gap_code="plan_currency_mismatch")
    ]
    with pytest.raises(ExportRefused) as caught:
        _extract()
    assert caught.value.code == REFUSAL_CROSS_CURRENCY


def test_two_currencies_over_the_window_refuse_the_file(mart):
    mart["rows"] = [_mart_row(), _mart_row(day="2026-07-02", plan_currency="USD")]
    with pytest.raises(ExportRefused) as caught:
        _extract()
    assert caught.value.code == REFUSAL_CROSS_CURRENCY


def test_no_currency_at_all_refuses_the_file(mart):
    mart["rows"] = [_mart_row(plan_currency=None, actual_currency=None)]
    with pytest.raises(ExportRefused) as caught:
        _extract()
    assert caught.value.code == REFUSAL_UNKNOWN_CURRENCY


def test_the_one_currency_is_named_on_every_row_and_in_the_provenance(mart):
    payload = _extract()
    assert {row["currency"] for row in payload["rows"]} == {"EUR"}
    assert payload["provenance"]["currency"]["currency"] == "EUR"
    assert "converted once at read" in payload["provenance"]["currency"]["conversion"]


# ---------------------------------------------------------------------------
# The rule this story adds: a variance is not a hole.
# ---------------------------------------------------------------------------


def test_a_planned_day_with_no_spend_is_a_variance_and_not_a_hole(mart):
    mart["rows"] = [
        _mart_row(),
        _mart_row(day="2026-07-02", actual_amount=None, actual_pull_id=None),
        _mart_row(day="2026-07-03"),
    ]
    coverage = _extract()["provenance"]["date_coverage"]
    assert coverage["variance_days"]["count"] == 1
    assert coverage["variance_days"]["entries"][0]["date"] == "2026-07-02"
    assert coverage["total_holes"] == 0
    assert coverage["withheld_days"]["count"] == 0


def test_an_observed_day_with_no_plan_is_named(mart):
    mart["rows"] = [
        _mart_row(),
        _mart_row(day="2026-07-02", allocated_amount=None),
        _mart_row(day="2026-07-03"),
    ]
    coverage = _extract()["provenance"]["date_coverage"]
    assert coverage["unplanned_days"]["count"] == 1
    assert coverage["unplanned_days"]["entries"][0] == {
        "date": "2026-07-02",
        "plan_line_key": "line-a",
    }
    assert coverage["total_holes"] == 0


def test_a_withheld_amount_is_a_hole_and_carries_its_gap_code(mart):
    mart["rows"] = [
        _mart_row(),
        _mart_row(
            day="2026-07-02",
            actual_amount=None,
            actual_withheld=True,
            money_gap_code="fx_rate_unavailable",
        ),
        _mart_row(day="2026-07-03"),
    ]
    coverage = _extract()["provenance"]["date_coverage"]
    assert coverage["withheld_days"]["count"] == 1
    assert coverage["withheld_days"]["entries"][0]["money_gap_code"] == (
        "fx_rate_unavailable"
    )
    assert coverage["total_holes"] == 1
    assert coverage["variance_days"]["count"] == 0


def test_a_plan_only_line_is_neither_a_variance_nor_a_hole(mart):
    mart["rows"] = [
        _mart_row(),
        _mart_row(
            day="2026-07-02", is_plan_only=True, actual_amount=None, actual_pull_id=None
        ),
        _mart_row(day="2026-07-03"),
    ]
    coverage = _extract()["provenance"]["date_coverage"]
    assert coverage["plan_only_days"]["count"] == 1
    assert coverage["variance_days"]["count"] == 0
    assert coverage["total_holes"] == 0


def test_a_calendar_day_with_no_row_at_all_is_a_hole(mart):
    mart["rows"] = [_mart_row(), _mart_row(day="2026-07-03")]
    coverage = _extract()["provenance"]["date_coverage"]
    assert coverage["days_with_no_row"]["entries"] == [{"date": "2026-07-02"}]
    assert coverage["total_holes"] == 1


def test_the_message_says_a_variance_is_not_counted_as_a_hole(mart):
    mart["rows"] = [
        _mart_row(),
        _mart_row(day="2026-07-02", actual_amount=None, actual_pull_id=None),
    ]
    coverage = _extract()["provenance"]["date_coverage"]
    assert "under-delivery" in coverage["message"]


def test_a_null_amount_is_dropped_and_never_written_as_an_empty_cell(mart):
    mart["rows"] = [_mart_row(actual_amount=None, actual_pull_id=None)]
    payload = _extract()
    assert [row["metric"] for row in payload["rows"]] == [METRIC_PLANNED]
    assert all(row["value"] is not None for row in payload["rows"])


# ---------------------------------------------------------------------------
# The window, and the file.
# ---------------------------------------------------------------------------


def test_a_malformed_window_refuses_in_the_seams_own_words():
    with pytest.raises(ExportRefused) as caught:
        _extract(_Conn(), start="2026-07", end="2026-07-03")
    assert caught.value.code == mmm_export.REFUSAL_WINDOW_MALFORMED


def test_a_window_wider_than_the_seam_allows_refuses():
    with pytest.raises(ExportRefused) as caught:
        _extract(_Conn(), start="1990-01-01", end="2026-07-03")
    assert caught.value.code == mmm_export.REFUSAL_WINDOW_TOO_WIDE


def test_the_csv_is_the_table_alone_and_holds_no_comment_line(mart):
    payload = _extract()
    lines = extract_csv(payload).decode("utf-8").strip().splitlines()
    assert lines[0] == ",".join(payload["columns"])
    assert len(lines) == 1 + len(payload["rows"])
    assert not any(line.startswith("#") for line in lines)


def test_the_provenance_states_that_nothing_was_written(mart):
    provenance = _extract()["provenance"]
    assert provenance["writes"].startswith("none")
    assert provenance["truncated"] is False
    assert provenance["relation"] == "plan_vs_actual_daily"
    assert "not here" in provenance["ventilation"]
