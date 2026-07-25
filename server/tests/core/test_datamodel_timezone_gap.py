"""Field-level detector tests for Story 39.7 -- the read-time TIMEZONE_GAP typed refusal.

Focused module (does not touch test_datamodel.py / test_datamodel_currency_gap.py). Verifies:
  - TIMEZONE_GAP fires for a date-grain (classified-monetary) field whose feeding streams
    carry NO resolvable report timezone -> fail-closed, never a silent zone (AC3, E39-NFR02).
  - A field whose used-by streams carry a report_timezone does NOT trip TIMEZONE_GAP.
  - A non-monetary field never trips a spurious TIMEZONE_GAP (gated like CURRENCY_GAP).
  - The emitted gap is shape-aligned with CURRENCY_GAP (code/severity/metric/affected_streams/
    resolvable_via).

is_metric_monetary is patched so these stay offline (no DB), mirroring test_datamodel_currency_gap.
"""

from __future__ import annotations

from unittest.mock import patch

from core.datamodel import _detect_conflicts


def _monetary(names_true):
    def fake(name, *, project_id=None):
        return name in names_true
    return patch("core.metric_semantics.is_metric_monetary", side_effect=fake)


def test_timezone_gap_fires_when_no_stream_carries_a_zone():
    field = {"name": "ad_revenue", "data_type": "currency", "measure": "sum",
             "field_kind": "metric"}
    used_by = [{"module_name": "mod-a", "datastream_name": "A"}]  # no report_timezone
    with _monetary({"ad_revenue"}):
        result = _detect_conflicts(field, used_by, project_id=None)
    codes = {c["code"] for c in result}
    assert "TIMEZONE_GAP" in codes
    gap = next(c for c in result if c["code"] == "TIMEZONE_GAP")
    assert gap["severity"] == "refusal"
    assert gap["metric"] == "ad_revenue"
    assert gap["resolvable_via"] == "source_timezone_declaration"
    assert gap["affected_streams"] == ["A"]


def test_no_timezone_gap_when_a_stream_carries_a_zone():
    field = {"name": "ad_revenue", "data_type": "currency", "measure": "sum",
             "field_kind": "metric"}
    used_by = [
        {"module_name": "mod-a", "datastream_name": "A", "report_timezone": "America/New_York"},
    ]
    with _monetary({"ad_revenue"}):
        result = _detect_conflicts(field, used_by, project_id=None)
    codes = {c["code"] for c in result}
    assert "TIMEZONE_GAP" not in codes


def test_no_timezone_gap_for_non_monetary_field():
    # Gated like CURRENCY_GAP: a non-monetary field never trips a spurious timezone gap.
    field = {"name": "sessions", "data_type": "integer", "measure": "sum",
             "field_kind": "metric"}
    used_by = [{"module_name": "ga", "datastream_name": "GA"}]  # no report_timezone
    with _monetary(set()):
        result = _detect_conflicts(field, used_by, project_id=None)
    codes = {c["code"] for c in result}
    assert "TIMEZONE_GAP" not in codes


def test_timezone_gap_shape_aligned_with_currency_gap():
    field = {"name": "ad_revenue", "data_type": "currency", "measure": "sum",
             "field_kind": "metric"}
    used_by = [{"module_name": "mod-a", "datastream_name": "A"}]
    with _monetary({"ad_revenue"}):
        result = _detect_conflicts(field, used_by, project_id=None)
    ccy = next(c for c in result if c["code"] == "CURRENCY_GAP")
    tz = next(c for c in result if c["code"] == "TIMEZONE_GAP")
    # Same key set (shape-aligned so conflict_resolutions renders it without a new branch).
    assert set(ccy.keys()) == set(tz.keys())
