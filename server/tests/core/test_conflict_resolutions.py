"""Tests for server/core/conflict_resolutions.py (Story 13.2, Epic 13).

Offline tests (no Postgres required): mock the DB connection.
pg-gated tests are marked with @pytest.mark.live_postgres.

WHAT MOVED (Story 67.20). These five functions kept their names and signatures,
but the store underneath is no longer `app.fx_conflict_resolutions` -- migration
145 dethroned it and migration 282 sealed it against writes. Reads now come from
`app.fx_source_currency_bindings_v` and writes go through
`core.source_currency_bindings`, which publishes an immutable version.

So the tests split by what they are actually about:
  * the READS still mock a cursor, because they still are one SELECT;
  * the WRITES now mock the GOVERNED STORE, because that is the seam this module
    owns -- "did it delegate, normalize and commit". What the governed store
    itself guarantees (a published version, preserved attribution, a superseded
    predecessor) is proven against a real Postgres in
    `tests/core/test_source_currency_bindings.py`, not against a MagicMock.

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
    # The column list of `app.fx_source_currency_bindings_v` (migration 282). The
    # surrogate `id` is gone: a declaration is identified by the version that
    # published it.
    cur.description = description or [
        ("project_id",), ("target_field",), ("source_module",),
        ("resolved_source_currency",), ("decided_by",), ("decided_at",), ("note",),
        ("rule_set_id",), ("rule_set_version_id",),
    ]
    cur.fetchone.return_value = fetchone
    cur.fetchall.return_value = fetchall or []
    cur.rowcount = rowcount

    conn = MagicMock()
    conn.cursor.return_value = cur
    return conn, cur


def _binding_row(**overrides):
    """One row of the governed projection, in cursor order."""
    row = {
        "project_id": "proj_a",
        "target_field": "cost",
        "source_module": "meta-ads",
        "resolved_source_currency": "USD",
        "decided_by": "alice@example.com",
        "decided_at": _NOW,
        "note": None,
        "rule_set_id": "grs_EXAMPLE",
        "rule_set_version_id": "grsv_EXAMPLE",
    }
    row.update(overrides)
    return tuple(row.values())


def _declared(**overrides):
    """What `source_currency_bindings.declare_binding` answers."""
    row = {
        "project_id": "proj_a",
        "target_field": "cost",
        "source_module": "meta-ads",
        "resolved_source_currency": "USD",
        "decided_by": "alice@example.com",
        "decided_at": _NOW.isoformat(),
        "note": None,
        "rule_set_id": "grs_EXAMPLE",
        "rule_set_version_id": "grsv_EXAMPLE",
        "content_hash": "0" * 64,
    }
    row.update(overrides)
    return row


# ---------------------------------------------------------------------------
# upsert_fx_resolution
# ---------------------------------------------------------------------------


class TestUpsertFxResolution:
    def test_valid_upsert_delegates_to_the_governed_store(self):
        """A valid declaration reaches the governed store and returns its row."""
        from core.conflict_resolutions import upsert_fx_resolution

        conn, _ = _make_conn()
        declared = _declared(decided_by="alice@test")

        with patch(
            "core.source_currency_bindings.declare_binding", return_value=declared
        ) as declare:
            result = upsert_fx_resolution(
                project_id="proj_a",
                target_field="cost",
                source_module="meta-ads",
                resolved_source_currency="usd",  # lowercase -> normalized to USD
                decided_by="alice@test",
                note=None,
                conn=conn,
            )

        # It is the governed door that was opened, with the normalized currency.
        declare.assert_called_once()
        kwargs = declare.call_args.kwargs
        assert kwargs["project_id"] == "proj_a"
        assert kwargs["target_field"] == "cost"
        assert kwargs["source_module"] == "meta-ads"
        assert kwargs["source_currency"] == "USD"
        assert kwargs["actor"] == "alice@test"

        assert result["resolved_source_currency"] == "USD"
        assert result["decided_by"] == "alice@test"
        # And the answer names the version the declaration was published under --
        # the question the overwritten row could never answer.
        assert result["rule_set_version_id"].startswith("grsv_")
        conn.commit.assert_called_once()

    def test_currency_normalized_to_upper(self):
        """Currency code is normalized to uppercase before it reaches the store."""
        from core.conflict_resolutions import upsert_fx_resolution

        conn, _ = _make_conn()

        with patch(
            "core.source_currency_bindings.declare_binding",
            return_value=_declared(source_module="tiktok-ads", resolved_source_currency="EUR"),
        ) as declare:
            result = upsert_fx_resolution(
                project_id="proj_a",
                target_field="cost",
                source_module="tiktok-ads",
                resolved_source_currency="eur",
                decided_by="bob",
                note=None,
                conn=conn,
            )
        assert declare.call_args.kwargs["source_currency"] == "EUR"
        assert result["resolved_source_currency"] == "EUR"

    def test_invalid_currency_raises(self):
        """Unknown currency code raises ValueError (not persisted)."""
        from core.conflict_resolutions import upsert_fx_resolution

        conn, _ = _make_conn()

        with pytest.raises(ValueError, match="resolved_source_currency is not a valid currency"):
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

        with pytest.raises(ValueError, match="resolved_source_currency is required"):
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

        with pytest.raises(ValueError, match="project_id is required"):
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

        conn, _ = _make_conn()

        with patch(
            "core.source_currency_bindings.declare_binding",
            return_value=_declared(decided_by="anonymous"),
        ) as declare:
            upsert_fx_resolution(
                project_id="proj_a",
                target_field="cost",
                source_module="meta-ads",
                resolved_source_currency="USD",
                decided_by="",
                note=None,
                conn=conn,
            )
        assert declare.call_args.kwargs["actor"] == "anonymous"
        assert conn.commit.called

    def test_upsert_with_note(self):
        """The note reaches the governed store and comes back on the declaration."""
        from core.conflict_resolutions import upsert_fx_resolution

        conn, _ = _make_conn()

        with patch(
            "core.source_currency_bindings.declare_binding",
            return_value=_declared(note="Force USD brut"),
        ) as declare:
            result = upsert_fx_resolution(
                project_id="proj_a",
                target_field="cost",
                source_module="meta-ads",
                resolved_source_currency="USD",
                decided_by="alice",
                note="Force USD brut",
                conn=conn,
            )
        assert declare.call_args.kwargs["note"] == "Force USD brut"
        assert result["note"] == "Force USD brut"

    def test_decided_at_is_isoformatted(self):
        """decided_at is an ISO string, as it was when the flat table served it."""
        from core.conflict_resolutions import upsert_fx_resolution

        conn, _ = _make_conn()

        with patch(
            "core.source_currency_bindings.declare_binding", return_value=_declared()
        ):
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
    def test_delete_existing_withdraws_through_the_governed_store(self):
        from core.conflict_resolutions import delete_fx_resolution

        conn, _ = _make_conn()

        with patch(
            "core.source_currency_bindings.withdraw_binding",
            return_value={"rule_set_version_id": "grsv_EXAMPLE"},
        ) as withdraw:
            delete_fx_resolution("proj_a", "cost", "meta-ads", conn)

        kwargs = withdraw.call_args.kwargs
        assert kwargs["project_id"] == "proj_a"
        assert kwargs["target_field"] == "cost"
        assert kwargs["source_module"] == "meta-ads"
        conn.commit.assert_called_once()

    def test_delete_not_found_raises(self):
        """A pair nobody declared refuses -- the route turns it into a 404."""
        from core.conflict_resolutions import delete_fx_resolution
        from core.governance_rule_sets import RuleSetError

        conn, _ = _make_conn()

        with patch(
            "core.source_currency_bindings.withdraw_binding",
            side_effect=RuleSetError(
                "no source currency is declared for cost/meta-ads in this Project"
            ),
        ):
            # RuleSetError subclasses ValueError, so the seam's 404 mapping holds.
            with pytest.raises(ValueError, match="no source currency is declared"):
                delete_fx_resolution("proj_a", "cost", "meta-ads", conn)
        conn.commit.assert_not_called()


# ---------------------------------------------------------------------------
# get_fx_resolution
# ---------------------------------------------------------------------------


class TestGetFxResolution:
    def test_found(self):
        from core.conflict_resolutions import get_fx_resolution

        conn, _ = _make_conn(fetchall=[_binding_row()])

        result = get_fx_resolution("proj_a", "cost", "meta-ads", conn)
        assert result is not None
        assert result["resolved_source_currency"] == "USD"
        assert result["rule_set_version_id"] == "grsv_EXAMPLE"

    def test_not_found(self):
        from core.conflict_resolutions import get_fx_resolution

        conn, _ = _make_conn(fetchall=[])

        result = get_fx_resolution("proj_a", "cost", "meta-ads", conn)
        assert result is None


# ---------------------------------------------------------------------------
# list_fx_resolutions
# ---------------------------------------------------------------------------


class TestListFxResolutions:
    def test_all(self):
        from core.conflict_resolutions import list_fx_resolutions

        rows = [
            _binding_row(source_module="meta-ads", resolved_source_currency="USD"),
            _binding_row(source_module="tiktok-ads", resolved_source_currency="EUR"),
        ]
        conn, cur = _make_conn(fetchall=rows)

        result = list_fx_resolutions(project_id=None, conn=conn)
        assert len(result) == 2
        assert result[0]["source_module"] == "meta-ads"

    def test_project_scoped(self):
        from core.conflict_resolutions import list_fx_resolutions

        rows = [_binding_row()]
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
            "display_name": "Cost",
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
            with patch(
                "core.datamodel.get_target_field", return_value=field_detail
            ) as mock_get:
                result = list_conflicts(project_id="proj_a", conn=conn)

        mock_get.assert_called_once_with("cost", conn, project_id="proj_a")
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
        """Non-retroactivity: declaring a source currency writes the governed
        declaration and NOTHING else. It does not touch raw_*/staging/fact tables.

        Read on the SOURCE rather than on a mock's call log: the write is one
        delegation now, so a cursor mock would observe no SQL at all and the guard
        would pass vacuously -- which is worse than no guard.
        """
        import inspect

        from core import conflict_resolutions, source_currency_bindings

        # The CODE of the write path, with the docstrings removed: those
        # legitimately name `stg_*_daily.sql` when explaining who consumes the
        # declaration, and a grep that cannot tell prose from code would fail on
        # the explanation rather than on a mutation.
        def _code(fn) -> str:
            # `inspect.getdoc` re-indents, so a plain string replace misses the
            # raw block. `fn.__doc__` is the literal that appears in the source.
            source = inspect.getsource(fn)
            doc = fn.__doc__
            return source.replace(doc, "") if doc else source

        bodies = [
            _code(conflict_resolutions.upsert_fx_resolution),
            _code(source_currency_bindings.declare_binding),
            _code(source_currency_bindings._publish),
        ]
        for body in bodies:
            lowered = body.lower()
            assert "raw_meta" not in lowered
            assert "stg_" not in lowered
            assert "fact_daily" not in lowered
            assert "pull_jobs" not in lowered

        # And the write path names the governed door, not the sealed table.
        write_path = inspect.getsource(conflict_resolutions.upsert_fx_resolution)
        assert "declare_binding" in write_path
        assert "fx_conflict_resolutions" not in write_path
