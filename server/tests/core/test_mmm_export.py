"""Story 62.1 -- what the MMM extract refuses, and what its file may say.

Every test here asserts a PROPERTY OF THE CLASS and never of a connector: the
shape of the file, the order of the gates, and each refusal by its own code. The
warehouse half -- a real relation, a real read, a real melt -- is proven against
Postgres and DuckDB in
`server/tests/integration/test_mmm_export_pg.py`; what is proven here is
everything that decides BEFORE a row is read.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_SERVER_DIR = Path(__file__).resolve().parents[2]
if str(_SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(_SERVER_DIR))

from core import mmm_export  # noqa: E402

# THE REAL CONSTANTS, imported and never retyped: a stub that answers a code the
# MDM no longer emits would keep this suite green while the product refused with
# another word. The pg suite asserts the same two against the live engine.
from core.analytics_alignment_read import (  # noqa: E402
    REFUSAL_DIMENSION_NOT_IN_GRAIN,
    REFUSAL_NO_GOVERNED_GRAIN,
)
from core.mmm_export import MmmExportRefused, build_extract, extract_csv  # noqa: E402

PROJECT = "proj_EXAMPLE"
VIEW_V = "svv_EXAMPLE"

SPEND, CLICKS, DAY, CHANNEL = "sc_spend", "sc_clicks", "sc_day", "sc_channel"


# ---------------------------------------------------------------------------
# A scripted connection -- the shape `test_query_execution` already uses.
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


def _view_row(status="published"):
    return (VIEW_V, "sv_EXAMPLE", status, 3, "media_daily", "h" * 64)


def _members(day_value_type="date", spend_value_type="decimal"):
    #  concept_id, concept_version_id, role, name, value_type, unit, aggregation
    return [
        (DAY, "scv_day", "dimension", "date", day_value_type, "", None),
        (CHANNEL, "scv_channel", "dimension", "channel", "string", "", None),
        (SPEND, "scv_spend", "metric", "spend", spend_value_type, "", None),
        (CLICKS, "scv_clicks", "metric", "clicks", "integer", "", None),
    ]


def _conn(status="published", members=None):
    return _Conn(
        [
            ("semantic_view_versions", _view_row(status)),
            ("semantic_view_version_concepts", _members() if members is None else members),
        ]
    )


def _extract(conn=None, **kwargs):
    call = {
        "project_id": PROJECT,
        "semantic_view_version_id": VIEW_V,
        "metrics": ["spend"],
        "dimensions": ["channel"],
        "start": "2026-07-01",
        "end": "2026-07-03",
    }
    call.update(kwargs)
    return build_extract(conn or _conn(), **call)


def _refusal(**kwargs) -> MmmExportRefused:
    with pytest.raises(MmmExportRefused) as caught:
        _extract(**kwargs)
    return caught.value


# ---------------------------------------------------------------------------
# The window is two calendar days, and there is no hourly grain to ask for.
# ---------------------------------------------------------------------------


def test_a_window_that_is_not_two_calendar_days_is_refused_before_anything_is_read():
    conn = _conn()
    with pytest.raises(MmmExportRefused) as caught:
        _extract(conn, start="2026-07-01T08:00:00Z", end="2026-07-03")
    assert caught.value.code == mmm_export.REFUSAL_WINDOW_MALFORMED
    # BEFORE anything is read: the window is malformed whatever the View says,
    # and asking the database first would make a typo cost a query.
    assert conn.statements == []


def test_a_window_that_ends_before_it_starts_is_refused():
    assert _refusal(start="2026-07-10", end="2026-07-01").code == (
        mmm_export.REFUSAL_WINDOW_MALFORMED
    )


def test_a_window_wider_than_one_file_is_refused_and_names_the_bound():
    refused = _refusal(start="2000-01-01", end="2026-07-01")
    assert refused.code == mmm_export.REFUSAL_WINDOW_TOO_WIDE
    assert refused.detail["maximum"] == mmm_export.MAX_WINDOW_DAYS
    assert "Narrow the window" in refused.gesture


def test_an_extract_with_no_metric_is_refused_as_a_list_of_days():
    assert _refusal(metrics=[]).code == mmm_export.REFUSAL_NO_MEASURE_ASKED


# ---------------------------------------------------------------------------
# Gate 1 -- the View version is published, and it belongs to this Project.
# ---------------------------------------------------------------------------


def test_a_view_version_this_project_does_not_carry_is_not_found():
    refused = pytest.raises(MmmExportRefused, match="no Semantic View version")
    with refused:
        build_extract(
            _Conn([("semantic_view_versions", None)]),
            project_id=PROJECT,
            semantic_view_version_id=VIEW_V,
            metrics=["spend"],
            dimensions=[],
            start="2026-07-01",
            end="2026-07-02",
        )


def test_the_view_lookup_is_scoped_by_the_project_and_by_the_view_alike():
    """A foreign version must not resolve through the head row either."""
    conn = _conn()
    with pytest.raises(MmmExportRefused):
        _extract(conn)
    lookup = next(sql for sql in conn.statements if "semantic_view_versions" in sql)
    assert lookup.count("%(project_id)s") == 2


def test_a_draft_view_version_is_refused_and_says_it_can_still_change():
    refused = _refusal(conn=_conn(status="draft"))
    assert refused.code == mmm_export.REFUSAL_VIEW_NOT_PUBLISHED
    assert refused.detail["semantic_view_version_status"] == "draft"
    assert "Publish" in refused.gesture


def test_a_member_the_view_does_not_publish_is_refused_naming_what_it_does():
    refused = _refusal(metrics=["reach"])
    assert refused.code == mmm_export.REFUSAL_MEMBER_NOT_IN_VIEW
    assert refused.detail["available"] == ["clicks", "spend"]
    assert "`clicks`" in refused.message and "`spend`" in refused.message


def test_a_metric_asked_for_as_a_dimension_is_refused_by_role():
    """The roles are not interchangeable, and a wrong one is not silently accepted."""
    refused = _refusal(dimensions=["spend"])
    assert refused.code == mmm_export.REFUSAL_MEMBER_NOT_IN_VIEW
    assert refused.detail["role"] == "dimension"


# ---------------------------------------------------------------------------
# The day is FOUND, never chosen and never assumed.
# ---------------------------------------------------------------------------


def test_a_view_with_no_calendar_day_cannot_answer_a_daily_extract():
    refused = _refusal(conn=_conn(members=_members(day_value_type="string")))
    assert refused.code == mmm_export.REFUSAL_NO_DAILY_GRAIN
    assert "value type is `date`" in refused.gesture


def test_two_calendar_days_in_one_view_is_a_decision_and_is_refused():
    members = _members() + [
        ("sc_day2", "scv_day2", "dimension", "reported_on", "date", "", None)
    ]
    refused = _refusal(conn=_conn(members=members))
    assert refused.code == mmm_export.REFUSAL_SEVERAL_DAILY_GRAINS
    assert refused.detail["candidates"] == ["date", "reported_on"]


def test_the_day_is_not_asked_for_and_is_not_duplicated_when_it_is(monkeypatch):
    """Naming `date` among the dimensions must not cut the file by the day twice."""
    seen = {}

    def _sanction(conn, *, project_id, metric_field_id, dimension_field_ids):
        seen["dimensions"] = list(dimension_field_ids)
        return {"sanctioned": True, "sliced_by": {"measurement_grain_id": "mg", "version_id": "v"}}

    monkeypatch.setattr(
        "core.canonical_field_registry.list_visible_canonical_fields",
        lambda conn, *, project_id: [
            {"id": f"cf_{name}", "canonical_name": name}
            for name in ("date", "channel", "spend", "clicks")
        ],
    )
    monkeypatch.setattr("core.analytics_alignment_read.sanction_breakdown", _sanction)
    monkeypatch.setattr(
        "core.query_execution.resolve_physical_plan",
        lambda *a, **k: {"unavailable_reason": "stop here", "missing_link": "test"},
    )
    with pytest.raises(MmmExportRefused):
        _extract(dimensions=["date", "channel"])
    assert seen["dimensions"] == ["cf_date", "cf_channel"]


# ---------------------------------------------------------------------------
# Gate 2 -- the governed grain sanctions the cut, and it is asked FIRST.
# ---------------------------------------------------------------------------


def _governed(monkeypatch, *, sanction=None, names=("date", "channel", "spend", "clicks")):
    monkeypatch.setattr(
        "core.canonical_field_registry.list_visible_canonical_fields",
        lambda conn, *, project_id: [
            {"id": f"cf_{name}", "canonical_name": name} for name in names
        ],
    )
    monkeypatch.setattr(
        "core.analytics_alignment_read.sanction_breakdown",
        sanction
        or (
            lambda conn, **kw: {
                "sanctioned": True,
                "measurement_grain_name": "spend by day and channel",
                "sliced_by": {"measurement_grain_id": "mg_1", "version_id": "mgv_1"},
            }
        ),
    )


def test_a_member_outside_the_canonical_registry_cannot_be_sanctioned(monkeypatch):
    _governed(monkeypatch, names=("date", "spend"))
    refused = _refusal()
    assert refused.code == mmm_export.REFUSAL_NOT_A_CANONICAL_FIELD
    assert refused.detail["member"] == "channel"


def test_a_cut_no_grain_sanctions_is_refused_in_the_grains_own_words(monkeypatch):
    _governed(
        monkeypatch,
        sanction=lambda conn, **kw: {
            "sanctioned": False,
            "grain_versions": [{"version_id": "mgv_9", "dimensions": ["cf_date"]}],
            "refusal": {
                "code": REFUSAL_DIMENSION_NOT_IN_GRAIN,
                "reason": "No live measurement grain relates cf_channel to cf_spend.",
                "gesture": "Append a version to the measurement grain of this metric.",
            },
        },
    )
    refused = _refusal()
    # THE GRAIN'S OWN CODE, not a second vocabulary invented at the export door.
    assert refused.code == REFUSAL_DIMENSION_NOT_IN_GRAIN
    assert refused.message.startswith("spend: ")
    assert refused.detail["grain_versions"][0]["version_id"] == "mgv_9"


def test_the_grain_is_asked_before_the_warehouse_is_planned(monkeypatch):
    """Order is the answer: a declaration is repairable without waiting for a run."""
    planned = []
    _governed(
        monkeypatch,
        sanction=lambda conn, **kw: {
            "sanctioned": False,
            "refusal": {
                "code": REFUSAL_NO_GOVERNED_GRAIN,
                "reason": "none",
                "gesture": "declare one",
            },
        },
    )
    monkeypatch.setattr(
        "core.query_execution.resolve_physical_plan",
        lambda *a, **k: planned.append(1) or {"unavailable_reason": "x", "missing_link": "y"},
    )
    assert _refusal().code == REFUSAL_NO_GOVERNED_GRAIN
    assert planned == []


def test_every_metric_of_one_file_is_cut_by_the_same_dimensions(monkeypatch):
    """Two metrics, one dimension list -- a file cannot mix two cuts."""
    asked = []

    def _sanction(conn, *, project_id, metric_field_id, dimension_field_ids):
        asked.append((metric_field_id, tuple(dimension_field_ids)))
        return {"sanctioned": True, "sliced_by": {"measurement_grain_id": "mg", "version_id": "v"}}

    _governed(monkeypatch, sanction=_sanction)
    monkeypatch.setattr(
        "core.query_execution.resolve_physical_plan",
        lambda *a, **k: {"unavailable_reason": "stop", "missing_link": "test"},
    )
    with pytest.raises(MmmExportRefused):
        _extract(metrics=["spend", "clicks"])
    assert [entry[0] for entry in asked] == ["cf_spend", "cf_clicks"]
    assert len({entry[1] for entry in asked}) == 1


# ---------------------------------------------------------------------------
# Gate 3 -- the planner's own sentence is carried whole.
# ---------------------------------------------------------------------------


def test_a_dimension_the_relation_does_not_publish_reaches_the_caller_unchanged(monkeypatch):
    """AI-342's refusal is not re-worded here; it is passed through with its link."""
    _governed(monkeypatch)
    monkeypatch.setattr(
        "core.query_execution.resolve_physical_plan",
        lambda *a, **k: {
            "unavailable_reason": (
                "The Datastream that answers this request does not publish a "
                "`channel` breakdown. It publishes `country`."
            ),
            "missing_link": "breakdown_dimension",
        },
    )
    refused = _refusal()
    assert refused.code == mmm_export.REFUSAL_PLAN_UNAVAILABLE
    assert refused.detail["missing_link"] == "breakdown_dimension"
    assert "does not publish a `channel` breakdown" in refused.message


# ---------------------------------------------------------------------------
# Gate 4 -- one declared currency per monetary metric, or the file is refused.
# ---------------------------------------------------------------------------


_PLAN = {
    "relation": "marts.raw_media_daily",
    "dataset": "",
    "columns": {DAY: "date", CHANNEL: "channel", SPEND: "cost", CLICKS: "clicks"},
    "present_columns": ["date", "channel", "cost", "cost_source_currency", "clicks"],
    "grain": ["date", "channel"],
    "grain_restrictions": [],
    "long_form": None,
    "datastream_id": "ds_1",
    "mapping_version_id": "dmv_1",
    "output_id": "dso_1",
    "output_version_id": "dsov_1",
    "pull_id": "pj_1",
    "publication_log_id": "pl_1",
    "chosen_by": None,
}


def _money(monkeypatch, *, currencies, plan=None, rows=None):
    _governed(monkeypatch)
    monkeypatch.setattr(
        "core.query_execution.resolve_physical_plan", lambda *a, **k: dict(plan or _PLAN)
    )
    monkeypatch.setattr(mmm_export, "_distinct_currencies", lambda **kw: list(currencies))
    monkeypatch.setattr(mmm_export, "_run", lambda plan, spec: list(rows or []))


def _money_members():
    members = _members(spend_value_type="money")
    return members


def test_a_monetary_metric_whose_landing_reports_no_currency_fails_closed(monkeypatch):
    _money(monkeypatch, currencies=[])
    plan = dict(_PLAN, present_columns=["date", "channel", "cost", "clicks"])
    monkeypatch.setattr("core.query_execution.resolve_physical_plan", lambda *a, **k: plan)
    refused = _refusal(conn=_conn(members=_money_members()))
    assert refused.code == "UNKNOWN_CURRENCY_GAP"
    assert refused.detail["metric"] == "spend"
    # THE COLUMN IS ABSENT, which is not the same fact as a column full of
    # nulls -- the refusal below carries `currency_column` and this one cannot.
    assert "currency_column" not in refused.detail
    assert "does not report which currency" in refused.message


def test_a_monetary_metric_with_no_currency_on_any_row_fails_closed(monkeypatch):
    _money(monkeypatch, currencies=[])
    refused = _refusal(conn=_conn(members=_money_members()))
    assert refused.code == "UNKNOWN_CURRENCY_GAP"
    assert refused.detail["currency_column"] == "cost_source_currency"


def test_two_currencies_in_one_column_is_the_cross_currency_refusal(monkeypatch):
    _money(monkeypatch, currencies=["EUR", "USD"])
    refused = _refusal(conn=_conn(members=_money_members()))
    assert refused.code == "CROSS_CURRENCY_REFUSAL"
    assert refused.detail["currencies"] == ["EUR", "USD"]
    assert "one currency at a time" in refused.gesture


def test_a_metric_that_is_not_money_carries_no_currency_and_that_is_not_a_gap(monkeypatch):
    _money(
        monkeypatch,
        currencies=["EUR"],
        rows=[{"date": "2026-07-01", "channel": "search", "clicks": 12}],
    )
    payload = _extract(conn=_conn(), metrics=["clicks"])
    assert payload["provenance"]["currencies"]["clicks"]["monetary"] is False
    assert payload["rows"][0]["currency"] == ""


def test_the_single_currency_is_declared_on_every_row_and_named_as_unconverted(monkeypatch):
    _money(
        monkeypatch,
        currencies=["EUR"],
        rows=[{"date": "2026-07-01", "channel": "search", "cost": 10.5}],
    )
    payload = _extract(conn=_conn(members=_money_members()))
    assert payload["rows"] == [
        {
            "date": "2026-07-01",
            "channel": "search",
            "metric": "spend",
            "value": 10.5,
            "currency": "EUR",
            "measurement_grain_version_id": "mgv_1",
        }
    ]
    spend = payload["provenance"]["currencies"]["spend"]
    assert spend["currency"] == "EUR"
    assert "none" in spend["conversion"]


# ---------------------------------------------------------------------------
# The file itself: shape, provenance, gaps, and the refusal to truncate.
# ---------------------------------------------------------------------------


def _three_days(monkeypatch, rows):
    _money(monkeypatch, currencies=["EUR"], rows=rows)


def test_the_file_is_long_and_its_columns_are_the_concepts_own_names(monkeypatch):
    _three_days(
        monkeypatch,
        [{"date": "2026-07-01", "channel": "search", "cost": 4, "clicks": 9}],
    )
    payload = _extract(conn=_conn(members=_money_members()), metrics=["spend", "clicks"])
    assert payload["columns"] == [
        "date", "channel", "metric", "value", "currency", "measurement_grain_version_id"
    ]
    assert [row["metric"] for row in payload["rows"]] == ["spend", "clicks"]


def test_every_row_carries_the_grain_version_that_sanctioned_it(monkeypatch):
    _three_days(monkeypatch, [{"date": "2026-07-01", "channel": "s", "cost": 1}])
    payload = _extract(conn=_conn(members=_money_members()))
    assert all(row["measurement_grain_version_id"] == "mgv_1" for row in payload["rows"])
    # And the id is never carried without its version.
    grain = payload["provenance"]["measurement_grains"]["spend"]
    assert grain["measurement_grain_id"] and grain["version_id"]


def test_the_provenance_names_the_view_version_the_relation_and_the_pull(monkeypatch):
    _three_days(monkeypatch, [{"date": "2026-07-01", "channel": "s", "cost": 1}])
    provenance = _extract(conn=_conn(members=_money_members()))["provenance"]
    assert provenance["semantic_view_version_id"] == VIEW_V
    assert provenance["relation"] == "marts.raw_media_daily"
    assert provenance["output_version_id"] == "dsov_1"
    assert provenance["pull_id"] == "pj_1"
    assert len(provenance["request_hash"]) == 64
    assert provenance["writes"].startswith("none")


def test_a_missing_week_is_listed_by_name_and_never_swallowed(monkeypatch):
    _three_days(monkeypatch, [{"date": "2026-07-02", "channel": "s", "cost": 1}])
    provenance = _extract(conn=_conn(members=_money_members()))["provenance"]
    gaps = provenance["date_gaps"]
    assert gaps["total"] == 2
    assert gaps["per_metric"]["spend"]["dates"] == ["2026-07-01", "2026-07-03"]
    assert "moves a mix model's coefficient" in gaps["message"]


def test_a_complete_window_says_so_rather_than_saying_nothing(monkeypatch):
    _three_days(
        monkeypatch,
        [
            {"date": "2026-07-01", "channel": "s", "cost": 1},
            {"date": "2026-07-02", "channel": "s", "cost": 2},
            {"date": "2026-07-03", "channel": "s", "cost": 3},
        ],
    )
    gaps = _extract(conn=_conn(members=_money_members()))["provenance"]["date_gaps"]
    assert gaps["total"] == 0
    assert "Every day of the window" in gaps["message"]


def test_a_null_measure_is_dropped_from_the_file_and_reappears_as_a_gap(monkeypatch):
    """A `value` cell holding nothing is a day a model reads as a zero."""
    _three_days(
        monkeypatch,
        [
            {"date": "2026-07-01", "channel": "s", "cost": 1, "clicks": None},
            {"date": "2026-07-02", "channel": "s", "cost": 2, "clicks": 7},
            {"date": "2026-07-03", "channel": "s", "cost": 3, "clicks": 8},
        ],
    )
    payload = _extract(conn=_conn(members=_money_members()), metrics=["spend", "clicks"])
    assert [row["value"] for row in payload["rows"] if row["metric"] == "clicks"] == [7, 8]
    gaps = payload["provenance"]["date_gaps"]["per_metric"]
    assert gaps["clicks"]["dates"] == ["2026-07-01"]
    assert gaps["spend"]["count"] == 0


def test_more_rows_than_one_file_carries_is_refused_and_nothing_is_truncated(monkeypatch):
    over = mmm_export.MAX_SOURCE_ROWS + 1
    _three_days(
        monkeypatch,
        [{"date": "2026-07-01", "channel": str(i), "cost": 1} for i in range(over)],
    )
    refused = _refusal(conn=_conn(members=_money_members()))
    assert refused.code == mmm_export.REFUSAL_TOO_MANY_ROWS
    assert "Nothing was truncated" in refused.message
    assert refused.detail["maximum"] == mmm_export.MAX_SOURCE_ROWS


def test_an_unreadable_warehouse_is_not_an_empty_extract(monkeypatch):
    _money(monkeypatch, currencies=["EUR"])

    def _boom(plan, spec):
        raise RuntimeError("relation vanished")

    monkeypatch.setattr(mmm_export, "_run", _boom)
    refused = _refusal(conn=_conn(members=_money_members()))
    assert refused.code == mmm_export.REFUSAL_WAREHOUSE_UNREADABLE
    assert "not an empty extract" in refused.message


def test_the_read_asks_for_its_own_bound_and_not_the_result_storage_ceiling(monkeypatch):
    """`MAX_INLINE_ROWS` bounds what a Result stores; it does not bound this read."""
    from core.query_execution import MAX_INLINE_ROWS, build_sql

    spec = {
        "measures": [{"id": SPEND}],
        "dimensions": [{"id": DAY}],
        "time": {"member_id": DAY, "start": "2026-07-01", "end": "2026-07-03"},
    }
    stored, _ = build_sql(_PLAN, spec)
    exported, _ = build_sql(_PLAN, spec, max_rows=mmm_export.MAX_SOURCE_ROWS)
    assert stored.endswith(f"LIMIT {MAX_INLINE_ROWS + 1}")
    assert exported.endswith(f"LIMIT {mmm_export.MAX_SOURCE_ROWS + 1}")


# ---------------------------------------------------------------------------
# The CSV is the table and nothing else.
# ---------------------------------------------------------------------------


def test_the_csv_carries_the_header_and_the_rows_and_no_comment_preamble():
    csv_bytes = extract_csv(
        {
            "columns": ["date", "channel", "metric", "value", "currency"],
            "rows": [
                {"date": "2026-07-01", "channel": "search", "metric": "spend",
                 "value": 10.5, "currency": "EUR"},
                {"date": "2026-07-02", "channel": None, "metric": "spend",
                 "value": 0, "currency": "EUR"},
            ],
        }
    )
    lines = csv_bytes.decode("utf-8").splitlines()
    assert lines[0] == "date,channel,metric,value,currency"
    assert lines[1] == "2026-07-01,search,spend,10.5,EUR"
    # An absent dimension is an empty cell, never the four letters `None`.
    assert lines[2] == "2026-07-02,,spend,0,EUR"
    assert not any(line.startswith("#") for line in lines)


def test_the_csv_holds_exactly_the_rows_the_read_returned(monkeypatch):
    _three_days(
        monkeypatch,
        [
            {"date": "2026-07-01", "channel": "s", "cost": 1},
            {"date": "2026-07-02", "channel": "s", "cost": 2},
        ],
    )
    payload = _extract(conn=_conn(members=_money_members()))
    lines = extract_csv(payload).decode("utf-8").strip().splitlines()
    assert len(lines) == 1 + len(payload["rows"])
