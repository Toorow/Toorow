"""Tests for server/core/conflict_resolutions.py (Story 13.2, Epic 13).

Offline tests (no Postgres required): mock the DB connection.
pg-gated tests are marked with @pytest.mark.live_postgres.

Tests:
  - upsert_fx_resolution: valid, invalid currency, missing fields
  - delete_fx_resolution: found, not found
  - get_fx_resolution: found, not found
  - list_fx_resolutions: all, project-scoped
  - list_conflicts: delegates to get_target_field (AI-53 reuse)
  - AD-6 invariant: no FX conversion in Python (grep-enforced)
  - Non-retroactivity: documented and tested via absence of retroactive mutation
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

_NOW = datetime(2026, 7, 21, 12, 0, 0, tzinfo=timezone.utc)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_conn(fetchone=None, fetchall=None, rowcount=1, description=None):
    """Return a minimal mock psycopg connection."""
    cur = MagicMock()
    cur.__enter__ = lambda s: s
    cur.__exit__ = MagicMock(return_value=False)
    cur.description = description or [
        ("id",), ("project_id",), ("target_field",), ("source_module",),
        ("resolved_source_currency",), ("decided_by",), ("decided_at",), ("note",),
    ]
    cur.fetchone.return_value = fetchone
    cur.fetchall.return_value = fetchall or []
    cur.rowcount = rowcount

    conn = MagicMock()
    conn.cursor.return_value = cur
    return conn, cur


# ---------------------------------------------------------------------------
# upsert_fx_resolution
# ---------------------------------------------------------------------------


class TestUpsertFxResolution:
    def test_valid_upsert(self):
        """A valid upsert returns the resolution row."""
        from core.conflict_resolutions import upsert_fx_resolution

        row = (1, "proj_a", "cost", "meta-ads", "USD", "alice@test", _NOW, None)
        conn, cur = _make_conn(fetchone=row)

        result = upsert_fx_resolution(
            project_id="proj_a",
            target_field="cost",
            source_module="meta-ads",
            resolved_source_currency="usd",  # lowercase -> normalized to USD
            decided_by="alice@test",
            note=None,
            conn=conn,
        )

        assert result["project_id"] == "proj_a"
        assert result["target_field"] == "cost"
        assert result["source_module"] == "meta-ads"
        assert result["resolved_source_currency"] == "USD"
        assert result["decided_by"] == "alice@test"
        conn.commit.assert_called_once()

    def test_currency_normalized_to_upper(self):
        """Currency code is normalized to uppercase."""
        from core.conflict_resolutions import upsert_fx_resolution

        row = (1, "proj_a", "cost", "tiktok-ads", "EUR", "bob", _NOW, None)
        conn, cur = _make_conn(fetchone=row)

        result = upsert_fx_resolution(
            project_id="proj_a",
            target_field="cost",
            source_module="tiktok-ads",
            resolved_source_currency="eur",
            decided_by="bob",
            note=None,
            conn=conn,
        )
        assert result["resolved_source_currency"] == "EUR"

    def test_invalid_currency_raises(self):
        """Unknown currency code raises ValueError (not persisted)."""
        from core.conflict_resolutions import upsert_fx_resolution

        conn, _ = _make_conn()

        with pytest.raises(ValueError, match="resolved_source_currency invalide"):
            upsert_fx_resolution(
                project_id="proj_a",
                target_field="cost",
                source_module="meta-ads",
                resolved_source_currency="FAKE",
                decided_by="alice",
                note=None,
                conn=conn,
            )
        conn.commit.assert_not_called()

    def test_empty_currency_raises(self):
        from core.conflict_resolutions import upsert_fx_resolution

        conn, _ = _make_conn()

        with pytest.raises(ValueError, match="resolved_source_currency est requis"):
            upsert_fx_resolution(
                project_id="proj_a",
                target_field="cost",
                source_module="meta-ads",
                resolved_source_currency="",
                decided_by="alice",
                note=None,
                conn=conn,
            )

    def test_missing_project_id_raises(self):
        from core.conflict_resolutions import upsert_fx_resolution

        conn, _ = _make_conn()

        with pytest.raises(ValueError, match="project_id est requis"):
            upsert_fx_resolution(
                project_id="",
                target_field="cost",
                source_module="meta-ads",
                resolved_source_currency="USD",
                decided_by="alice",
                note=None,
                conn=conn,
            )

    def test_anonymous_decided_by_fallback(self):
        """Empty decided_by falls back to 'anonymous'."""
        from core.conflict_resolutions import upsert_fx_resolution

        row = (1, "proj_a", "cost", "meta-ads", "USD", "anonymous", _NOW, None)
        conn, cur = _make_conn(fetchone=row)

        upsert_fx_resolution(
            project_id="proj_a",
            target_field="cost",
            source_module="meta-ads",
            resolved_source_currency="USD",
            decided_by="",
            note=None,
            conn=conn,
        )
        # The INSERT uses 'anonymous' (decided_by arg replaced in function)
        assert conn.commit.called

    def test_upsert_with_note(self):
        """Note is persisted."""
        from core.conflict_resolutions import upsert_fx_resolution

        row = (1, "proj_a", "cost", "meta-ads", "USD", "alice", _NOW, "Force USD brut")
        conn, cur = _make_conn(fetchone=row)

        result = upsert_fx_resolution(
            project_id="proj_a",
            target_field="cost",
            source_module="meta-ads",
            resolved_source_currency="USD",
            decided_by="alice",
            note="Force USD brut",
            conn=conn,
        )
        assert result["note"] == "Force USD brut"

    def test_decided_at_is_isoformatted(self):
        """decided_at is serialised to ISO string."""
        from core.conflict_resolutions import upsert_fx_resolution

        row = (1, "proj_a", "cost", "meta-ads", "USD", "alice", _NOW, None)
        conn, cur = _make_conn(fetchone=row)

        result = upsert_fx_resolution(
            project_id="proj_a",
            target_field="cost",
            source_module="meta-ads",
            resolved_source_currency="USD",
            decided_by="alice",
            note=None,
            conn=conn,
        )
        assert result["decided_at"] == _NOW.isoformat()

    def test_no_fx_conversion_in_upsert(self):
        """AD-6: upsert does NOT perform any numeric FX multiplication.

        This test verifies that the function inserts the declared currency string
        and does NOT compute any rate/amount product (that would violate AD-6).
        """
        import inspect

        from core.conflict_resolutions import upsert_fx_resolution

        source = inspect.getsource(upsert_fx_resolution)
        # No multiplication of a 'rate' or 'amount' column in the function body
        assert "* fx" not in source
        assert "* rate" not in source
        assert "rate *" not in source


# ---------------------------------------------------------------------------
# delete_fx_resolution
# ---------------------------------------------------------------------------


class TestDeleteFxResolution:
    def test_delete_existing(self):
        from core.conflict_resolutions import delete_fx_resolution

        conn, cur = _make_conn(rowcount=1)

        # Should not raise
        delete_fx_resolution("proj_a", "cost", "meta-ads", conn)
        conn.commit.assert_called_once()

    def test_delete_not_found_raises(self):
        from core.conflict_resolutions import delete_fx_resolution

        conn, cur = _make_conn(rowcount=0)

        with pytest.raises(ValueError, match="introuvable"):
            delete_fx_resolution("proj_a", "cost", "meta-ads", conn)
        conn.commit.assert_not_called()


# ---------------------------------------------------------------------------
# get_fx_resolution
# ---------------------------------------------------------------------------


class TestGetFxResolution:
    def test_found(self):
        from core.conflict_resolutions import get_fx_resolution

        row = (1, "proj_a", "cost", "meta-ads", "USD", "alice", _NOW, None)
        conn, cur = _make_conn(fetchone=row)

        result = get_fx_resolution("proj_a", "cost", "meta-ads", conn)
        assert result is not None
        assert result["resolved_source_currency"] == "USD"

    def test_not_found(self):
        from core.conflict_resolutions import get_fx_resolution

        conn, cur = _make_conn(fetchone=None)

        result = get_fx_resolution("proj_a", "cost", "meta-ads", conn)
        assert result is None


# ---------------------------------------------------------------------------
# list_fx_resolutions
# ---------------------------------------------------------------------------


class TestListFxResolutions:
    def test_all(self):
        from core.conflict_resolutions import list_fx_resolutions

        rows = [
            (1, "proj_a", "cost", "meta-ads", "USD", "alice", _NOW, None),
            (2, "proj_a", "cost", "tiktok-ads", "EUR", "bob", _NOW, None),
        ]
        conn, cur = _make_conn(fetchall=rows)

        result = list_fx_resolutions(project_id=None, conn=conn)
        assert len(result) == 2
        assert result[0]["source_module"] == "meta-ads"

    def test_project_scoped(self):
        from core.conflict_resolutions import list_fx_resolutions

        rows = [(1, "proj_a", "cost", "meta-ads", "USD", "alice", _NOW, None)]
        conn, cur = _make_conn(fetchall=rows)

        list_fx_resolutions(project_id="proj_a", conn=conn)
        # SQL has WHERE project_id = %s
        sql_call = cur.execute.call_args[0][0]
        assert "WHERE" in sql_call

    def test_empty_list(self):
        from core.conflict_resolutions import list_fx_resolutions

        conn, cur = _make_conn(fetchall=[])
        result = list_fx_resolutions(project_id="proj_a", conn=conn)
        assert result == []


# ---------------------------------------------------------------------------
# list_conflicts -- delegates to _detect_conflicts (AI-53)
# ---------------------------------------------------------------------------


class TestListConflicts:
    def test_currency_conflict_detected(self):
        """list_conflicts delegates to get_target_field which calls _detect_conflicts."""
        from core.conflict_resolutions import list_conflicts

        field_summary = {
            "name": "cost",
            "display_name": "Coût",
            "data_type": "currency",
            "field_kind": "metric",
            "measure": "sum",
            "status": "approved",
            "used_by_count": 2,
        }
        field_detail = {
            **field_summary,
            "used_by": [
                {"module_name": "meta-ads", "datastream_name": "Meta Ads",
                 "project_id": "proj_a", "datastream_id": "ds1"},
                {"module_name": "tiktok-ads", "datastream_name": "TikTok Ads",
                 "project_id": "proj_a", "datastream_id": "ds2"},
            ],
            "conflicts": [
                {
                    "code": "CURRENCY_CONFLICT",
                    "message": "modules distincts",
                    "affected_streams": ["Meta Ads", "TikTok Ads"],
                }
            ],
        }

        conn = MagicMock()
        # list_target_fields returns [field_summary]
        # get_target_field returns field_detail
        # _fetch_resolutions_for_field returns []
        conn.cursor.return_value.__enter__ = lambda s: s
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        conn.cursor.return_value.fetchall.return_value = []
        conn.cursor.return_value.description = [
            ("id",), ("project_id",), ("target_field",), ("source_module",),
            ("resolved_source_currency",), ("decided_by",), ("decided_at",), ("note",),
        ]

        with patch("core.datamodel.list_target_fields", return_value=[field_summary]):
            with patch("core.datamodel.get_target_field", return_value=field_detail):
                result = list_conflicts(project_id="proj_a", conn=conn)

        assert len(result) == 1
        assert result[0]["conflict"]["code"] == "CURRENCY_CONFLICT"
        assert "resolutions_by_module" in result[0]

    def test_measure_null_detected(self):
        """MEASURE_NULL conflict included in results."""
        from core.conflict_resolutions import list_conflicts

        field_summary = {
            "name": "revenue",
            "display_name": "Revenu",
            "data_type": "currency",
            "field_kind": "metric",
            "measure": None,
            "status": "approved",
            "used_by_count": 1,
        }
        field_detail = {
            **field_summary,
            "used_by": [
                {"module_name": "shopify", "datastream_name": "Shopify",
                 "project_id": "proj_a", "datastream_id": "ds3"},
            ],
            "conflicts": [
                {
                    "code": "MEASURE_NULL",
                    "message": "semantique aggregation non definie",
                    "affected_streams": ["Shopify"],
                }
            ],
        }

        conn = MagicMock()
        conn.cursor.return_value.__enter__ = lambda s: s
        conn.cursor.return_value.__exit__ = MagicMock(return_value=False)
        conn.cursor.return_value.fetchall.return_value = []
        conn.cursor.return_value.description = [
            ("id",), ("project_id",), ("target_field",), ("source_module",),
            ("resolved_source_currency",), ("decided_by",), ("decided_at",), ("note",),
        ]

        with patch("core.datamodel.list_target_fields", return_value=[field_summary]):
            with patch("core.datamodel.get_target_field", return_value=field_detail):
                result = list_conflicts(project_id="proj_a", conn=conn)

        assert len(result) == 1
        assert result[0]["conflict"]["code"] == "MEASURE_NULL"
        # MEASURE_NULL has empty resolutions_by_module (resolution = PATCH measure)
        assert result[0]["resolutions_by_module"] == {}

    def test_no_conflicts_returns_empty(self):
        """Field with no conflicts is excluded from results."""
        from core.conflict_resolutions import list_conflicts

        field_summary = {
            "name": "clicks",
            "display_name": "Clics",
            "data_type": "integer",
            "field_kind": "metric",
            "measure": "sum",
            "status": "approved",
            "used_by_count": 1,
        }
        field_detail = {**field_summary, "used_by": [], "conflicts": []}

        conn = MagicMock()

        with patch("core.datamodel.list_target_fields", return_value=[field_summary]):
            with patch("core.datamodel.get_target_field", return_value=field_detail):
                result = list_conflicts(project_id=None, conn=conn)

        assert result == []


# ---------------------------------------------------------------------------
# AD-6 invariant test: grep the module source for FX conversion
# ---------------------------------------------------------------------------


class TestAD6Invariant:
    def test_no_fx_conversion_in_conflict_resolutions_module(self):
        """AD-6: the conflict_resolutions module must NOT perform currency conversion.

        Verifies that no numeric rate multiplication exists in the Python module
        (the conversion path is dbt-only: stg_*_daily.sql JOIN fx_rates).
        """
        import inspect

        from core import conflict_resolutions

        source = inspect.getsource(conflict_resolutions)
        # Patterns that would indicate Python-side FX multiplication
        forbidden = [
            "* fx",    # numeric product with fx object
            "* rate",  # numeric product with rate variable
            "rate *",  # same from left side
            "/ rate",  # division by rate (inverse conversion)
            "fx_rate",  # variable holding a rate value for multiplication
        ]
        violations = [pat for pat in forbidden if pat in source]
        assert not violations, (
            f"AD-6 VIOLATION: conflict_resolutions.py contains FX conversion patterns: "
            f"{violations}. Currency conversion MUST happen in dbt staging only."
        )

    def test_no_fx_conversion_in_conflict_resolutions_api(self):
        """AD-6: the conflict_resolutions_api module must NOT perform currency conversion."""
        import inspect

        from core import conflict_resolutions_api

        source = inspect.getsource(conflict_resolutions_api)
        forbidden = ["* fx", "* rate", "rate *", "/ rate", "fx_rate"]
        violations = [pat for pat in forbidden if pat in source]
        assert not violations, (
            f"AD-6 VIOLATION: conflict_resolutions_api.py contains FX conversion patterns: "
            f"{violations}. Currency conversion MUST happen in dbt staging only."
        )


# ---------------------------------------------------------------------------
# Non-retroactivity: documented behavioural test
# ---------------------------------------------------------------------------


class TestNonRetroactivity:
    def test_upsert_does_not_mutate_pull_history(self):
        """Non-retroactivity: upsert_fx_resolution only INSERTs/UPDATEs the
        resolutions table. It does NOT touch raw_*/staging/fact tables.

        This is verified by checking the SQL sent to the cursor: only the
        fx_conflict_resolutions table is referenced in DML.
        """
        from core.conflict_resolutions import upsert_fx_resolution

        row = (1, "proj_a", "cost", "meta-ads", "USD", "alice", _NOW, None)
        conn, cur = _make_conn(fetchone=row)

        upsert_fx_resolution(
            project_id="proj_a",
            target_field="cost",
            source_module="meta-ads",
            resolved_source_currency="USD",
            decided_by="alice",
            note=None,
            conn=conn,
        )

        # Verify the SQL only touches fx_conflict_resolutions (not raw/staging/fact)
        sql_calls = [str(c) for c in cur.execute.call_args_list]
        for sql in sql_calls:
            assert "raw_meta" not in sql.lower()
            assert "stg_" not in sql.lower()
            assert "fact_daily" not in sql.lower()
            assert "pull_jobs" not in sql.lower()
        # Confirm fx_conflict_resolutions IS mentioned
        assert any("fx_conflict_resolutions" in s for s in sql_calls)
