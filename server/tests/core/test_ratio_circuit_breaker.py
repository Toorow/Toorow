"""Unit tests for the ratio circuit-breaker (Story 32.1 / AD-4).

A ratio (ROAS, CTR, *_rate...) is non-additive and must never be declared as an additive
metric that could land in fact_daily_kpi. Two guards enforce this on the self-service
definition path:
  - core.metric_semantics.upsert_metric_definition  (refuses additive=True on a ratio)
  - core.datamodel.create_target_field              (refuses measure='sum' on a ratio metric)
Both raise ValueError BEFORE any DB access, so no Postgres is needed here.

The shared detector is core.metric_semantics.is_ratio_name.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest
from core.datamodel import create_target_field
from core.metric_semantics import is_ratio_name, upsert_metric_definition

# ---------------------------------------------------------------------------
# is_ratio_name (pure, no DB)
# ---------------------------------------------------------------------------

_RATIO_POSITIVES = [
    "roas", "ROAS", "Roas",           # bare + case-insensitive
    "ctr", "cpc", "cpm", "cpa", "roi", "cvr", "frequency",
    "conversion_rate", "bounce_rate",
    "custom_rate", "bounce_pct", "spend_ratio", "open_percent",  # suffix families
    "meta_roas", "google_ctr",        # prefixed acronym ratios
]

_RATIO_NEGATIVES = [
    "revenue", "cost", "sessions", "conversions", "clicks", "impressions",
    "average_order_value", "orders_count", "attributed_revenue",
    "rate_card_id",   # 'rate' token in the WRONG position -> not a ratio
    "separate",       # substring 'rate' but no underscore anchor
    "generate_leads",
    "",
    None,
]


@pytest.mark.parametrize("name", _RATIO_POSITIVES)
def test_is_ratio_name_positive(name):
    assert is_ratio_name(name) is True


@pytest.mark.parametrize("name", _RATIO_NEGATIVES)
def test_is_ratio_name_negative(name):
    assert is_ratio_name(name) is False


# ---------------------------------------------------------------------------
# create_target_field guard (datamodel.py)
# ---------------------------------------------------------------------------

_TF_RETURNING_COLS = [
    "name", "display_name", "data_type", "field_kind", "measure", "description",
    "created_by", "is_default", "created_at", "status", "approved_at", "approved_by",
    "updated_at",
]
_NOW = datetime(2026, 7, 21, 10, 0, 0, tzinfo=timezone.utc)


def _conn_returning(row_values):
    """Mock psycopg conn whose cursor returns one RETURNING row (for the allowed path)."""
    conn = MagicMock()
    cur = MagicMock()
    conn.cursor.return_value.__enter__ = lambda _s: cur
    conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
    cur.description = [(c,) for c in _TF_RETURNING_COLS]
    cur.fetchone.return_value = row_values
    return conn


def test_create_target_field_rejects_additive_ratio():
    """A ratio metric declared additive (measure='sum') is refused before any INSERT."""
    conn = MagicMock()  # must never be touched
    with pytest.raises(ValueError, match="ratio"):
        create_target_field(
            {
                "name": "roas",
                "display_name": "ROAS",
                "data_type": "decimal",
                "field_kind": "metric",
                "measure": "sum",
            },
            created_by="tester",
            conn=conn,
        )
    conn.cursor.assert_not_called()


def test_create_target_field_allows_ratio_as_average():
    """A ratio recomputed at view time (measure='average') is allowed."""
    row = (
        "custom_rate", "Custom Rate", "decimal", "metric", "average", None,
        "tester", False, _NOW, "draft", None, None, None,
    )
    conn = _conn_returning(row)
    result = create_target_field(
        {
            "name": "custom_rate",
            "display_name": "Custom Rate",
            "data_type": "decimal",
            "field_kind": "metric",
            "measure": "average",
        },
        created_by="tester",
        conn=conn,
    )
    assert result["name"] == "custom_rate"
    conn.commit.assert_called_once()


def test_create_target_field_allows_ratio_named_dimension():
    """A dimension named like a ratio is not additive -> guard must not fire."""
    row = (
        "bounce_rate", "Bounce Rate", "string", "dimension", None, None,
        "tester", False, _NOW, "draft", None, None, None,
    )
    conn = _conn_returning(row)
    result = create_target_field(
        {
            "name": "bounce_rate",
            "display_name": "Bounce Rate",
            "data_type": "string",
            "field_kind": "dimension",
        },
        created_by="tester",
        conn=conn,
    )
    assert result["field_kind"] == "dimension"


def test_create_target_field_allows_legit_additive_metric():
    """A genuine additive metric (revenue, measure='sum') is unaffected by the guard."""
    row = (
        "revenue", "Revenue", "currency", "metric", "sum", None,
        "tester", False, _NOW, "draft", None, None, None,
    )
    conn = _conn_returning(row)
    result = create_target_field(
        {
            "name": "revenue",
            "display_name": "Revenue",
            "data_type": "currency",
            "field_kind": "metric",
            "measure": "sum",
        },
        created_by="tester",
        conn=conn,
    )
    assert result["name"] == "revenue"


# ---------------------------------------------------------------------------
# upsert_metric_definition guard (metric_semantics.py)
# ---------------------------------------------------------------------------


def test_upsert_metric_definition_rejects_additive_ratio_by_name():
    """additive=True on a ratio-named metric raises before touching the DB."""
    with pytest.raises(ValueError, match="ratio"):
        upsert_metric_definition(
            canonical_name="roas",
            aggregation_type="sum",
            additive=True,
            created_by="tester",
        )


def test_upsert_metric_definition_rejects_additive_ratio_by_aggregation_type():
    """additive=True with aggregation_type='ratio' raises even for a neutral name."""
    with pytest.raises(ValueError, match="ratio"):
        upsert_metric_definition(
            canonical_name="custom_metric",
            aggregation_type="ratio",
            additive=True,
            created_by="tester",
        )
