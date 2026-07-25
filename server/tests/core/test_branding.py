"""Tests for Story 23.1 -- org branding resolution + additive envelope key (AC1).

Offline only: ``core.db.get_connection`` is mocked (AI-60 discipline -- this
story adds NO MCP tool nor REST endpoint, so no ASGI seam test is required;
the contract under test is resolve_org_branding's degradation behaviour and
the AI-31 additive ``meta.branding`` key of the canonical envelope).
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from core.branding import resolve_org_branding
from core.envelope import build_canonical_envelope

_ROW = ("org_01ABC", "#0F6FFF", None, "#AA00CC", "https://cdn.example/logo.png")


def _connection_patch(row=None):
    """Patch core.db.get_connection with a context manager yielding a fake cursor."""
    cur = MagicMock()
    cur.fetchone.return_value = row
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    cm = MagicMock()
    cm.__enter__.return_value = conn
    cm.__exit__.return_value = False
    return patch("core.db.get_connection", return_value=cm)


# ---------------------------------------------------------------------------
# resolve_org_branding (AC1)
# ---------------------------------------------------------------------------


def test_resolves_branding_dict_when_org_has_colors():
    with _connection_patch(row=_ROW):
        branding = resolve_org_branding("proj_x")
    assert branding == {
        "org_id": "org_01ABC",
        "brand_primary": "#0F6FFF",
        "brand_secondary": None,
        "brand_accent": "#AA00CC",
        "logo_url": "https://cdn.example/logo.png",
    }


def test_returns_none_when_project_has_no_org():
    with _connection_patch(row=None):
        assert resolve_org_branding("proj_orphan") is None


def test_returns_none_when_org_defined_no_color():
    # Org exists, all three colors NULL -> same as absent (AC1), logo alone
    # does not constitute branding.
    with _connection_patch(row=("org_01ABC", None, None, None, "https://cdn/l.png")):
        assert resolve_org_branding("proj_x") is None


def test_returns_none_on_db_error_never_raises():
    with patch("core.db.get_connection", side_effect=RuntimeError("db down")):
        assert resolve_org_branding("proj_x") is None


def test_returns_none_for_missing_project_id_without_touching_db():
    with _connection_patch(row=_ROW) as mocked:
        assert resolve_org_branding(None) is None
        assert resolve_org_branding("") is None
        mocked.assert_not_called()


# ---------------------------------------------------------------------------
# build_canonical_envelope -- additive meta.branding key (AC1 / AI-31)
# ---------------------------------------------------------------------------

_FRESHNESS = {"last_pull": "2026-07-21T00:00:00Z", "cadence_hours": 24, "stale_since": None}
_PROVENANCE = [{"source_system": "seeded", "source_field": "fact_daily_kpi", "pull_id": "p1"}]
_DATE_RANGE = {"start": "2026-04-22", "end": "2026-07-21"}


def test_envelope_carries_branding_when_provided():
    branding = {
        "org_id": "org_01ABC",
        "brand_primary": "#0F6FFF",
        "brand_secondary": None,
        "brand_accent": None,
        "logo_url": None,
    }
    env = build_canonical_envelope(
        rows=[], meta_freshness=_FRESHNESS, meta_provenance=_PROVENANCE,
        date_range=_DATE_RANGE, connectors=["seeded"], branding=branding,
    )
    assert env["meta"]["branding"] == branding


def test_envelope_omits_branding_key_entirely_when_none():
    env = build_canonical_envelope(
        rows=[], meta_freshness=_FRESHNESS, meta_provenance=_PROVENANCE,
        date_range=_DATE_RANGE, connectors=["seeded"],
    )
    assert "branding" not in env["meta"]  # ABSENT, never null (AC1)
    # AI-31: existing keys unchanged.
    assert set(env["meta"].keys()) == {"freshness", "provenance", "alerts"}
