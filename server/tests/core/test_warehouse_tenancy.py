"""Tests for Story 24.1 -- single point of warehouse topology naming.

Offline only: ``core.db.get_connection`` is mocked (AI-60 -- no new MCP tool
nor REST endpoint in this module). Contracts under test: resolution
project/org -> org_<wslug>_raw/_marts, kebab->underscore sanitisation (BigQuery
dataset charset), legacy fallback + WARNING (never an exception on a read
path), the TOOROW_ORG_SCHEMAS dual-mode (default OFF = bit-identical legacy
names, no DB touched), and the short TTL resolution cache.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from core import warehouse_tenancy as wt

_ROW = ("org_01ABC", "acme-media")


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    """Each test starts flag-OFF with an empty resolution cache."""
    monkeypatch.delenv("TOOROW_ORG_SCHEMAS", raising=False)
    wt._reset_cache()
    yield
    wt._reset_cache()


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
# Flag parsing (AC2)
# ---------------------------------------------------------------------------


def test_flag_default_off(monkeypatch):
    monkeypatch.delenv("TOOROW_ORG_SCHEMAS", raising=False)
    assert wt.org_schemas_enabled() is False


@pytest.mark.parametrize("value,expected", [
    ("1", True),
    ("true", True),
    ("TRUE", True),
    ("0", False),
    ("false", False),
    ("", False),
])
def test_flag_parsing(monkeypatch, value, expected):
    monkeypatch.setenv("TOOROW_ORG_SCHEMAS", value)
    assert wt.org_schemas_enabled() is expected


# ---------------------------------------------------------------------------
# Sanitisation (AC1 -- BigQuery dataset charset)
# ---------------------------------------------------------------------------


def test_sanitize_kebab_to_underscore():
    assert wt.sanitize_warehouse_slug("acme-media") == "acme_media"


def test_sanitize_passthrough_when_already_safe():
    assert wt.sanitize_warehouse_slug("acme42") == "acme42"


@pytest.mark.parametrize("bad", [None, "", "café", "a b", "a.b"])
def test_sanitize_rejects_unsafe_forms(bad):
    assert wt.sanitize_warehouse_slug(bad) is None


# ---------------------------------------------------------------------------
# resolve_org_schemas (AC1)
# ---------------------------------------------------------------------------


def test_resolves_schemas_by_project_id():
    with _connection_patch(row=_ROW):
        resolved = wt.resolve_org_schemas(project_id="proj_x")
    assert resolved == wt.OrgSchemas(
        org_id="org_01ABC",
        org_slug="acme-media",
        warehouse_slug="acme_media",
        raw="org_acme_media_raw",
        marts="org_acme_media_marts",
    )


def test_resolves_schemas_by_org_id():
    with _connection_patch(row=_ROW):
        resolved = wt.resolve_org_schemas(org_id="org_01ABC")
    assert resolved is not None
    assert resolved.marts == "org_acme_media_marts"


def test_returns_none_without_identifier_and_never_touches_db():
    with _connection_patch(row=_ROW) as mocked:
        assert wt.resolve_org_schemas() is None
        mocked.assert_not_called()


def test_returns_none_for_project_without_org(caplog):
    with _connection_patch(row=None), caplog.at_level("WARNING"):
        assert wt.resolve_org_schemas(project_id="proj_legacy") is None
    assert any("no org resolvable" in r.message for r in caplog.records)


def test_returns_none_on_db_error_never_raises(caplog):
    with patch("core.db.get_connection", side_effect=RuntimeError("db down")), \
            caplog.at_level("WARNING"):
        assert wt.resolve_org_schemas(project_id="proj_x") is None
    assert any("resolution failed" in r.message for r in caplog.records)


def test_uses_caller_connection_when_provided():
    cur = MagicMock()
    cur.fetchone.return_value = _ROW
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    with _connection_patch(row=None) as own:
        resolved = wt.resolve_org_schemas(project_id="proj_x", conn=conn)
        own.assert_not_called()
    assert resolved is not None and resolved.raw == "org_acme_media_raw"


def test_resolution_cache_avoids_second_query():
    with _connection_patch(row=_ROW) as mocked:
        first = wt.resolve_org_schemas(project_id="proj_x")
        second = wt.resolve_org_schemas(project_id="proj_x")
    assert first == second
    assert mocked.call_count == 1


def test_negative_result_is_cached_too():
    with _connection_patch(row=None) as mocked:
        assert wt.resolve_org_schemas(project_id="proj_legacy") is None
        assert wt.resolve_org_schemas(project_id="proj_legacy") is None
    assert mocked.call_count == 1


# ---------------------------------------------------------------------------
# mart_prefix (AC2/AC3)
# ---------------------------------------------------------------------------


def test_mart_prefix_flag_off_is_exact_legacy_and_touches_no_db():
    with _connection_patch(row=_ROW) as mocked:
        assert wt.mart_prefix("proj_x") == "main_marts."
        assert wt.mart_prefix(None) == "main_marts."
        mocked.assert_not_called()


def test_mart_prefix_flag_on_resolves_org_schema(monkeypatch):
    monkeypatch.setenv("TOOROW_ORG_SCHEMAS", "1")
    with _connection_patch(row=_ROW):
        assert wt.mart_prefix("proj_x") == "org_acme_media_marts."


def test_mart_prefix_flag_on_unresolvable_degrades_to_legacy(monkeypatch, caplog):
    monkeypatch.setenv("TOOROW_ORG_SCHEMAS", "1")
    with _connection_patch(row=None), caplog.at_level("WARNING"):
        assert wt.mart_prefix("proj_legacy") == "main_marts."
    assert any("no org resolvable" in r.message for r in caplog.records)


def test_mart_prefix_flag_on_without_project_warns_and_degrades(monkeypatch, caplog):
    monkeypatch.setenv("TOOROW_ORG_SCHEMAS", "1")
    with caplog.at_level("WARNING"):
        assert wt.mart_prefix(None) == "main_marts."
    assert any("without project_id" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# bigquery_marts_dataset (AC5)
# ---------------------------------------------------------------------------


def test_bq_dataset_flag_off_is_exact_legacy_and_touches_no_db():
    with _connection_patch(row=_ROW) as mocked:
        assert wt.bigquery_marts_dataset("proj_x") == "marts_proj_x"
        mocked.assert_not_called()


def test_bq_dataset_flag_on_resolves_org_dataset(monkeypatch):
    monkeypatch.setenv("TOOROW_ORG_SCHEMAS", "1")
    with _connection_patch(row=_ROW):
        assert wt.bigquery_marts_dataset("proj_x") == "org_acme_media_marts"


def test_bq_dataset_flag_on_unresolvable_degrades_to_legacy(monkeypatch):
    monkeypatch.setenv("TOOROW_ORG_SCHEMAS", "1")
    with _connection_patch(row=None):
        assert wt.bigquery_marts_dataset("proj_legacy") == "marts_proj_legacy"
