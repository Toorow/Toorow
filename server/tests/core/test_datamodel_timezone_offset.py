"""Field-level detector + list_conflicts advisory regression for Story 39.8.

Focused module (does not touch test_datamodel.py / test_datamodel_timezone_gap.py). Verifies:
  - _detect_conflicts emits TIMEZONE_DAY_OFFSET (advisory) when a field is fed by >= 2 streams
    carrying DISTINCT captured report_timezone -- alongside any existing codes, which stay
    byte-unchanged (16). NOT gated on is_monetary (a non-monetary field fed by two timezones
    signals).
  - Same captured timezone across streams -> NO TIMEZONE_DAY_OFFSET (17, AC3).
  - Used-by rows with NO report_timezone -> NO TIMEZONE_DAY_OFFSET (18, defer to 39.7 GAP).
  - _stream_report_timezone reads the generic key, fail-closed on blank/None (19).
  - list_conflicts renders a TIMEZONE_DAY_OFFSET as an ADVISORY, not dumped in the MEASURE_NULL
    else branch (20).
  - Backward compat: a currency-only field (no tz divergence) produces the same conflicts as
    before 39.8 -- no new key, no new object (21).

is_metric_monetary is patched so these stay offline (no DB), mirroring test_datamodel_timezone_gap.
"""

from __future__ import annotations

from unittest.mock import patch

from core.datamodel import _detect_conflicts, _stream_report_timezone


def _monetary(names_true):
    def fake(name, *, project_id=None):
        return name in names_true

    return patch("core.metric_semantics.is_metric_monetary", side_effect=fake)


# ---------------------------------------------------------------------------
# 16 -- TIMEZONE_DAY_OFFSET fires (advisory) alongside existing codes, byte-unchanged
# ---------------------------------------------------------------------------


def test_day_offset_fires_on_distinct_timezones():  # 16
    field = {"name": "revenue", "data_type": "currency", "measure": "sum",
             "field_kind": "metric"}
    used_by = [
        {"module_name": "mod-a", "datastream_name": "A", "report_timezone": "Europe/Paris",
         "source_currency": "EUR"},
        {"module_name": "mod-b", "datastream_name": "B", "report_timezone": "UTC",
         "source_currency": "EUR"},
    ]
    with _monetary({"revenue"}):
        result = _detect_conflicts(field, used_by, project_id=None)
    by_code = {c["code"]: c for c in result}
    assert "TIMEZONE_DAY_OFFSET" in by_code
    offset = by_code["TIMEZONE_DAY_OFFSET"]
    assert offset["severity"] == "advisory"
    assert offset["realignable"] is False
    assert offset["distinct_timezones"] == ["Europe/Paris", "UTC"]
    assert offset["metric"] == "revenue"
    # Existing CURRENCY_CONFLICT still present + unchanged shape (refusal, not advisory).
    assert by_code["CURRENCY_CONFLICT"]["severity"] == "refusal"


def test_day_offset_not_monetary_gated():  # 16b -- fires for a NON-monetary field
    field = {"name": "sessions", "data_type": "integer", "measure": "sum",
             "field_kind": "metric"}
    used_by = [
        {"module_name": "ga", "datastream_name": "GA", "report_timezone": "Europe/Paris"},
        {"module_name": "gam", "datastream_name": "GAM", "report_timezone": "UTC"},
    ]
    with _monetary(set()):  # NOT monetary
        result = _detect_conflicts(field, used_by, project_id=None)
    codes = {c["code"] for c in result}
    assert "TIMEZONE_DAY_OFFSET" in codes  # a day-offset signals regardless of monetary
    assert "CURRENCY_GAP" not in codes  # currency gap stays monetary-gated
    assert "TIMEZONE_GAP" not in codes


# ---------------------------------------------------------------------------
# 17 -- same timezone flags nothing (AC3)
# ---------------------------------------------------------------------------


def test_same_timezone_no_day_offset():  # 17
    field = {"name": "sessions", "data_type": "integer", "measure": "sum",
             "field_kind": "metric"}
    used_by = [
        {"module_name": "a", "datastream_name": "A", "report_timezone": "Europe/Paris"},
        {"module_name": "b", "datastream_name": "B", "report_timezone": "Europe/Paris"},
    ]
    with _monetary(set()):
        result = _detect_conflicts(field, used_by, project_id=None)
    assert "TIMEZONE_DAY_OFFSET" not in {c["code"] for c in result}


# ---------------------------------------------------------------------------
# 18 -- unknown timezone -> no day-offset (defer to 39.7 GAP)
# ---------------------------------------------------------------------------


def test_unknown_timezone_no_day_offset():  # 18
    field = {"name": "sessions", "data_type": "integer", "measure": "sum",
             "field_kind": "metric"}
    used_by = [
        {"module_name": "a", "datastream_name": "A"},  # no report_timezone
        {"module_name": "b", "datastream_name": "B"},  # no report_timezone
    ]
    with _monetary(set()):
        result = _detect_conflicts(field, used_by, project_id=None)
    assert "TIMEZONE_DAY_OFFSET" not in {c["code"] for c in result}


def test_one_known_one_unknown_no_day_offset():  # 18b -- exclusion leaves 1 known
    field = {"name": "sessions", "data_type": "integer", "measure": "sum",
             "field_kind": "metric"}
    used_by = [
        {"module_name": "a", "datastream_name": "A", "report_timezone": "UTC"},
        {"module_name": "b", "datastream_name": "B"},  # unknown -> excluded
    ]
    with _monetary(set()):
        result = _detect_conflicts(field, used_by, project_id=None)
    assert "TIMEZONE_DAY_OFFSET" not in {c["code"] for c in result}


# ---------------------------------------------------------------------------
# 19 -- _stream_report_timezone seam (generic key, fail-closed)
# ---------------------------------------------------------------------------


def test_stream_report_timezone_reads_generic_key():  # 19
    assert _stream_report_timezone({"report_timezone": "Europe/Paris"}) == "Europe/Paris"
    assert _stream_report_timezone({"captured_report_timezone": "UTC"}) == "UTC"
    assert _stream_report_timezone({"report_timezone": "  UTC  "}) == "UTC"


def test_stream_report_timezone_fail_closed_on_blank():  # 19b
    assert _stream_report_timezone({"report_timezone": ""}) is None
    assert _stream_report_timezone({"report_timezone": "   "}) is None
    assert _stream_report_timezone({"report_timezone": None}) is None
    assert _stream_report_timezone({}) is None
    assert _stream_report_timezone({"report_timezone": 123}) is None


# ---------------------------------------------------------------------------
# 20 -- list_conflicts renders TIMEZONE_DAY_OFFSET as an advisory (not the else/MEASURE_NULL)
# ---------------------------------------------------------------------------


def test_list_conflicts_renders_day_offset_as_advisory():  # 20
    from unittest.mock import MagicMock

    from core import conflict_resolutions

    offset_conflict = {
        "code": "TIMEZONE_DAY_OFFSET",
        "message": "cross-source day offset",
        "affected_streams": ["A", "B"],
        "severity": "advisory",
        "report_timezones": [],
        "distinct_timezones": ["Europe/Paris", "UTC"],
        "realignable": False,
        "metric": "revenue",
    }
    detail = {
        "name": "revenue",
        "display_name": "Revenue",
        "data_type": "currency",
        "field_kind": "metric",
        "measure": "sum",
        "status": "approved",
        "used_by": [
            {"module_name": "mod-a"},
            {"module_name": "mod-b"},
        ],
        "conflicts": [offset_conflict],
    }
    conn = MagicMock()
    with patch(
        "core.datamodel.list_target_fields", return_value=[{"name": "revenue"}]
    ), patch(
        "core.datamodel.get_target_field", return_value=detail
    ), patch.object(
        conflict_resolutions, "_fetch_resolutions_for_field", return_value=[]
    ):
        out = conflict_resolutions.list_conflicts(project_id=None, conn=conn)
    assert len(out) == 1
    entry = out[0]
    assert entry["conflict"]["code"] == "TIMEZONE_DAY_OFFSET"
    assert entry["conflict"]["severity"] == "advisory"
    # Advisory -> empty resolutions_by_module (not a per-module currency bind), rendered
    # EXPLICITLY (the key is present, the advisory carried through intact).
    assert entry["resolutions_by_module"] == {}


# ---------------------------------------------------------------------------
# 21 -- backward compat: currency-only field (no tz divergence) unchanged
# ---------------------------------------------------------------------------


def test_currency_only_field_unchanged_no_day_offset():  # 21
    field = {"name": "revenue", "data_type": "currency", "measure": "sum",
             "field_kind": "metric"}
    # Two modules, SAME captured timezone -> no day-offset; currency conflict still fires.
    used_by = [
        {"module_name": "mod-a", "datastream_name": "A", "report_timezone": "UTC",
         "source_currency": "USD"},
        {"module_name": "mod-b", "datastream_name": "B", "report_timezone": "UTC",
         "source_currency": "EUR"},
    ]
    with _monetary({"revenue"}):
        result = _detect_conflicts(field, used_by, project_id=None)
    codes = [c["code"] for c in result]
    assert "TIMEZONE_DAY_OFFSET" not in codes
    assert "CURRENCY_CONFLICT" in codes  # existing behaviour intact
