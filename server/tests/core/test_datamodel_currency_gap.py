"""Field-level detector tests for Story 39.3 currency enrichment + CURRENCY_GAP.

Focused module (does not touch the shared test_datamodel.py). Verifies:
  - CURRENCY_CONFLICT stays backward-compatible (code/message/affected_streams unchanged)
    and gains the additive keys (severity/monetary/metric/resolvable_via) -- test 19.
  - conflicting_currencies is the sorted observed set when used_by carries currencies, else
    None (honest at field layer) -- test 20.
  - CURRENCY_GAP fires for a classified-monetary field whose streams carry no currency.
  - MEASURE_NULL and non-currency paths are unchanged -- test 21.

is_metric_monetary is patched so these stay offline (no DB).
"""

from __future__ import annotations

from unittest.mock import patch

from core.datamodel import _detect_conflicts


def _monetary(names_true):
    """Patch is_metric_monetary to return True only for names in *names_true*."""
    def fake(name, *, project_id=None):
        return name in names_true
    return patch("core.metric_semantics.is_metric_monetary", side_effect=fake)


# ---------------------------------------------------------------------------
# 19 -- backward-compatible enrichment
# ---------------------------------------------------------------------------


def test_currency_conflict_backward_compatible_plus_new_keys():
    field = {"name": "revenue", "data_type": "currency", "measure": "sum",
             "field_kind": "metric"}
    used_by = [
        {"module_name": "mod-a", "datastream_name": "Stream A"},
        {"module_name": "mod-b", "datastream_name": "Stream B"},
    ]
    with _monetary({"revenue"}):
        result = _detect_conflicts(field, used_by, project_id=None)
    conflict = next(c for c in result if c["code"] == "CURRENCY_CONFLICT")
    # Unchanged base shape.
    assert conflict["code"] == "CURRENCY_CONFLICT"
    assert "unités" in conflict["message"] or "unites" in conflict["message"] or conflict["message"]
    assert "Stream A" in conflict["affected_streams"]
    assert "Stream B" in conflict["affected_streams"]
    # New additive keys.
    assert conflict["severity"] == "refusal"
    assert conflict["monetary"] is True
    assert conflict["metric"] == "revenue"
    assert conflict["resolvable_via"] == "fx_conversion"


# ---------------------------------------------------------------------------
# 20 -- conflicting_currencies from observed used_by currencies
# ---------------------------------------------------------------------------


def test_conflicting_currencies_from_observed_used_by():
    field = {"name": "revenue", "data_type": "currency", "measure": "sum",
             "field_kind": "metric"}
    used_by = [
        {"module_name": "mod-a", "datastream_name": "A", "source_currency": "USD"},
        {"module_name": "mod-b", "datastream_name": "B", "source_currency": "EUR"},
    ]
    with _monetary({"revenue"}):
        result = _detect_conflicts(field, used_by, project_id=None)
    conflict = next(c for c in result if c["code"] == "CURRENCY_CONFLICT")
    assert conflict["conflicting_currencies"] == ["EUR", "USD"]  # sorted distinct


def test_conflicting_currencies_none_when_absent():
    field = {"name": "revenue", "data_type": "currency", "measure": "sum",
             "field_kind": "metric"}
    used_by = [
        {"module_name": "mod-a", "datastream_name": "A"},
        {"module_name": "mod-b", "datastream_name": "B"},
    ]
    with _monetary({"revenue"}):
        result = _detect_conflicts(field, used_by, project_id=None)
    conflict = next(c for c in result if c["code"] == "CURRENCY_CONFLICT")
    assert conflict["conflicting_currencies"] is None


# ---------------------------------------------------------------------------
# CURRENCY_GAP -- classified-monetary field with no resolvable source currency
# ---------------------------------------------------------------------------


def test_currency_gap_fires_when_no_stream_carries_currency():
    field = {"name": "revenue", "data_type": "currency", "measure": "sum",
             "field_kind": "metric"}
    used_by = [{"module_name": "mod-a", "datastream_name": "A"}]  # single module, no ccy
    with _monetary({"revenue"}):
        result = _detect_conflicts(field, used_by, project_id=None)
    codes = {c["code"] for c in result}
    assert "CURRENCY_GAP" in codes
    gap = next(c for c in result if c["code"] == "CURRENCY_GAP")
    assert gap["monetary"] is True
    assert gap["conflicting_currencies"] is None
    assert gap["metric"] == "revenue"
    assert gap["resolvable_via"] == "fx_conversion"


def test_no_currency_gap_when_a_stream_carries_currency():
    field = {"name": "revenue", "data_type": "currency", "measure": "sum",
             "field_kind": "metric"}
    used_by = [{"module_name": "mod-a", "datastream_name": "A", "source_currency": "EUR"}]
    with _monetary({"revenue"}):
        result = _detect_conflicts(field, used_by, project_id=None)
    codes = {c["code"] for c in result}
    assert "CURRENCY_GAP" not in codes


def test_no_currency_gap_for_non_monetary_field():
    # A plain decimal field that is NOT classified monetary -> no CURRENCY_GAP (money-only).
    field = {"name": "some_ratio", "data_type": "decimal", "measure": "average",
             "field_kind": "metric"}
    used_by = [{"module_name": "mod-a", "datastream_name": "A"}]
    with _monetary(set()):  # nothing monetary
        result = _detect_conflicts(field, used_by, project_id=None)
    codes = {c["code"] for c in result}
    assert "CURRENCY_GAP" not in codes


# ---------------------------------------------------------------------------
# 21 -- MEASURE_NULL / non-currency unchanged
# ---------------------------------------------------------------------------


def test_measure_null_unchanged():
    field = {"name": "sessions", "data_type": "integer", "measure": None,
             "field_kind": "metric"}
    used_by = [{"module_name": "ga", "datastream_name": "GA"}]
    with _monetary(set()):
        result = _detect_conflicts(field, used_by, project_id=None)
    assert result == [
        {
            "code": "MEASURE_NULL",
            "message": (
                "La sémantique d'agrégation (measure) n'est pas définie pour ce champ "
                "numérique. Les consommateurs pourraient appliquer une agrégation incorrecte "
                "(somme vs moyenne). Définissez 'measure' pour sécuriser les rollups."
            ),
            "affected_streams": ["GA"],
        }
    ]


def test_non_currency_dimension_no_conflict():
    field = {"name": "country", "data_type": "string", "measure": None,
             "field_kind": "dimension"}
    used_by = [{"module_name": "ga", "datastream_name": "GA"}]
    with _monetary(set()):
        result = _detect_conflicts(field, used_by, project_id=None)
    assert result == []
