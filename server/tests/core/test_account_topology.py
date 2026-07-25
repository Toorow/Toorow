"""Unit tests for server/core/account_topology.py (Story 25.5, AC7).

Covers:
  * topology contract validation (valid + each malformed shape);
  * get_topology reader (absent -> None, invalid -> None, valid -> dict);
  * scope state machine (upsert pending->ready, resolve_selected_account gating);
  * enqueue guard via core.queue.enqueue_pull (with/without topology, without
    ready scope, with ready scope) -- the AC4 refusal contract;
  * backfill windowing edges (31, 32, 365, invalid 0/366) + trial window.

Strategy: all DB calls mocked (no live Postgres); the module registry is mocked
via core.main.get_loaded_modules so no real module load is required.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from datetime import date
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")

from core import account_topology as at  # noqa: E402

_VALID_TOPOLOGY = {
    "levels": [
        {"id": "account", "label": "Account"},
        {"id": "property", "label": "Property"},
    ],
    "selection_level": "property",
    "discovery": {"callable": "discover_accounts"},
}


# ---------------------------------------------------------------------------
# Fake DB connection helper (mirrors test_queue.py idioms)
# ---------------------------------------------------------------------------


def _fake_connection(fetchone_return=None, description=None):
    cur = MagicMock()
    cur.__enter__ = MagicMock(return_value=cur)
    cur.__exit__ = MagicMock(return_value=False)
    cur.fetchone = MagicMock(return_value=fetchone_return)
    cur.description = description or []

    conn = MagicMock()
    conn.__enter__ = MagicMock(return_value=conn)
    conn.__exit__ = MagicMock(return_value=False)
    conn.cursor = MagicMock(return_value=cur)
    conn.commit = MagicMock()

    @contextmanager
    def _get_connection():
        yield conn

    return _get_connection, conn, cur


def _fake_loaded_module(name, manifest, connector_module=None):
    return SimpleNamespace(
        name=name,
        manifest=manifest,
        connector_module=connector_module or SimpleNamespace(),
    )


# ---------------------------------------------------------------------------
# AC1 -- contract validation
# ---------------------------------------------------------------------------


class TestValidateTopology:
    def test_valid_contract_has_no_errors(self):
        assert at.validate_topology(_VALID_TOPOLOGY) == []

    def test_not_an_object(self):
        assert at.validate_topology(["nope"])

    def test_empty_levels(self):
        bad = {**_VALID_TOPOLOGY, "levels": []}
        errors = at.validate_topology(bad)
        assert any("levels" in e for e in errors)

    def test_duplicate_level_ids(self):
        bad = {
            **_VALID_TOPOLOGY,
            "levels": [
                {"id": "x", "label": "X"},
                {"id": "x", "label": "X2"},
            ],
            "selection_level": "x",
        }
        errors = at.validate_topology(bad)
        assert any("unique" in e for e in errors)

    def test_selection_level_not_in_levels(self):
        bad = {**_VALID_TOPOLOGY, "selection_level": "missing"}
        errors = at.validate_topology(bad)
        assert any("selection_level" in e for e in errors)

    def test_missing_discovery_callable(self):
        bad = {**_VALID_TOPOLOGY, "discovery": {}}
        errors = at.validate_topology(bad)
        assert any("callable" in e for e in errors)

    def test_level_missing_label(self):
        bad = {
            **_VALID_TOPOLOGY,
            "levels": [{"id": "account"}],
            "selection_level": "account",
        }
        errors = at.validate_topology(bad)
        assert any("label" in e for e in errors)


class TestGetTopology:
    def test_absent_key_returns_none(self):
        assert at.get_topology({"name": "m"}) is None

    def test_invalid_contract_returns_none(self):
        assert at.get_topology({"name": "m", "account_topology": {"levels": []}}) is None

    def test_valid_returns_dict(self):
        manifest = {"name": "m", "account_topology": _VALID_TOPOLOGY}
        assert at.get_topology(manifest) == _VALID_TOPOLOGY

    def test_non_dict_manifest(self):
        assert at.get_topology(None) is None


# ---------------------------------------------------------------------------
# AC2 -- scope state machine + resolve_selected_account
# ---------------------------------------------------------------------------


class TestScopeStateMachine:
    _COLS = [
        "id",
        "connection_ref_id",
        "account_id",
        "account_label",
        "state",
        "verified_at",
        "selected_by",
        "created_at",
        "updated_at",
    ]

    def _scope_row(self, state, account_id="properties/456"):
        return (
            "ascope_1",
            "conn_1",
            account_id,
            "Acme Web",
            state,
            None,
            "user@test",
            None,
            None,
        )

    def test_resolve_selected_account_ready_returns_id(self):
        get_conn, _, cur = _fake_connection(
            fetchone_return=self._scope_row(at.STATE_READY),
            description=[(c,) for c in self._COLS],
        )
        with patch("core.db.get_connection", new=get_conn):
            assert at.resolve_selected_account("conn_1") == "properties/456"

    def test_resolve_selected_account_pending_returns_none(self):
        get_conn, _, cur = _fake_connection(
            fetchone_return=self._scope_row(at.STATE_PENDING, account_id=None),
            description=[(c,) for c in self._COLS],
        )
        with patch("core.db.get_connection", new=get_conn):
            assert at.resolve_selected_account("conn_1") is None

    def test_resolve_selected_account_absent_returns_none(self):
        get_conn, _, cur = _fake_connection(fetchone_return=None)
        with patch("core.db.get_connection", new=get_conn):
            assert at.resolve_selected_account("conn_1") is None

    def test_has_ready_scope_true(self):
        get_conn, conn, cur = _fake_connection(fetchone_return=(1,))
        with patch("core.db.get_connection", new=get_conn):
            assert at.has_ready_scope("conn_1") is True

    def test_has_ready_scope_false_when_no_row(self):
        get_conn, conn, cur = _fake_connection(fetchone_return=None)
        with patch("core.db.get_connection", new=get_conn):
            assert at.has_ready_scope("conn_1") is False

    def test_has_ready_scope_fails_closed_on_error(self):
        # DB raises -> fail closed (never treat unverifiable scope as ready).
        def _boom():
            raise RuntimeError("db down")

        with patch("core.db.get_connection", side_effect=_boom):
            assert at.has_ready_scope("conn_1") is False


class TestVerifyAndSelect:
    def test_account_not_reachable_raises(self):
        discovered = {
            "topology": _VALID_TOPOLOGY,
            "accounts": [
                {"id": "accounts/1", "label": "A", "children": [
                    {"id": "properties/9", "label": "P9"},
                ]},
            ],
        }
        with patch.object(at, "discover_accounts", return_value=discovered):
            with pytest.raises(at.AccountNotReachable):
                at.verify_and_select_account(
                    "conn_1", "properties/UNKNOWN", selected_by="u"
                )

    def test_reachable_account_upserts_ready(self):
        discovered = {
            "topology": _VALID_TOPOLOGY,
            "accounts": [
                {"id": "accounts/1", "label": "A", "children": [
                    {"id": "properties/9", "label": "P9"},
                ]},
            ],
        }
        captured = {}

        def _fake_upsert(connection_ref_id, **kwargs):
            captured.update(kwargs)
            captured["connection_ref_id"] = connection_ref_id
            return {
                "account_id": kwargs["account_id"],
                "account_label": kwargs["account_label"],
                "state": kwargs["state"],
                "verified_at": "2026-07-21T00:00:00+00:00",
            }

        with patch.object(at, "discover_accounts", return_value=discovered), \
             patch.object(at, "upsert_scope", side_effect=_fake_upsert):
            scope = at.verify_and_select_account(
                "conn_1", "properties/9", selected_by="u"
            )

        assert scope["state"] == at.STATE_READY
        assert captured["account_id"] == "properties/9"
        assert captured["account_label"] == "P9"
        assert captured["verified_at"] is not None


# ---------------------------------------------------------------------------
# AC4 -- enqueue guard through core.queue.enqueue_pull
# ---------------------------------------------------------------------------


class TestEnqueueGuard:
    def _patch_registry(self, manifest):
        loaded = [_fake_loaded_module("google-analytics", manifest)] if manifest else []
        return patch("core.main.get_loaded_modules", return_value=loaded)

    def test_no_topology_module_proceeds_unchanged(self):
        """A module without account_topology enqueues exactly as before (AC4)."""
        # connection resolves to a provider with NO topology manifest.
        ref_row = ("conn_1", "nango_1", "some-provider", "proj_1")
        get_conn, conn, cur = _fake_connection(fetchone_return=ref_row)
        cur.description = [("id",), ("nango_connection_id",), ("provider",), ("project_id",)]

        sentinel = {"job_id": "job_x", "pull_id": "pull_x", "state": "queued"}
        with patch("core.db.get_connection", new=get_conn), \
             self._patch_registry({"name": "some-provider"}), \
             patch("core.queue._backend") as backend:
            backend.enqueue_pull.return_value = sentinel
            from core.queue import enqueue_pull

            result = enqueue_pull("conn_1", "2026-07-01", "2026-07-03", requested_by="u")

        assert result == sentinel
        backend.enqueue_pull.assert_called_once()

    def test_topology_without_ready_scope_refuses(self):
        """A topology-declaring provider with no ready scope is REFUSED (AC4)."""
        ref_row = ("conn_1", "nango_1", "google-analytics", "proj_1")
        get_conn, conn, cur = _fake_connection(fetchone_return=ref_row)
        cur.description = [("id",), ("nango_connection_id",), ("provider",), ("project_id",)]

        manifest = {"name": "google-analytics", "account_topology": _VALID_TOPOLOGY}
        with patch("core.db.get_connection", new=get_conn), \
             self._patch_registry(manifest), \
             patch("core.account_topology.has_ready_scope", return_value=False), \
             patch("core.queue._backend") as backend:
            from core.queue import enqueue_pull

            result = enqueue_pull("conn_1", "2026-07-01", "2026-07-03", requested_by="u")

        assert result["state"] == "refused"
        assert result["code"] == "account_not_selected"
        backend.enqueue_pull.assert_not_called()

    def test_topology_with_ready_scope_proceeds(self):
        """A topology-declaring provider WITH a ready scope enqueues normally (AC4)."""
        ref_row = ("conn_1", "nango_1", "google-analytics", "proj_1")
        get_conn, conn, cur = _fake_connection(fetchone_return=ref_row)
        cur.description = [("id",), ("nango_connection_id",), ("provider",), ("project_id",)]

        manifest = {"name": "google-analytics", "account_topology": _VALID_TOPOLOGY}
        sentinel = {"job_id": "job_y", "pull_id": "pull_y", "state": "queued"}
        with patch("core.db.get_connection", new=get_conn), \
             self._patch_registry(manifest), \
             patch("core.account_topology.has_ready_scope", return_value=True), \
             patch("core.queue._backend") as backend:
            backend.enqueue_pull.return_value = sentinel
            from core.queue import enqueue_pull

            result = enqueue_pull("conn_1", "2026-07-01", "2026-07-03", requested_by="u")

        assert result == sentinel
        backend.enqueue_pull.assert_called_once()

    def test_unknown_connection_is_not_a_topology_refusal(self):
        """Unknown connection -> guard proceeds (worker dead-letters it), not refused."""
        get_conn, conn, cur = _fake_connection(fetchone_return=None)
        cur.description = [("id",), ("nango_connection_id",), ("provider",), ("project_id",)]

        sentinel = {"job_id": "job_z", "pull_id": "pull_z", "state": "queued"}
        with patch("core.db.get_connection", new=get_conn), \
             patch("core.queue._backend") as backend:
            backend.enqueue_pull.return_value = sentinel
            from core.queue import enqueue_pull

            result = enqueue_pull("conn_unknown", "2026-07-01", "2026-07-03", requested_by="u")

        assert result == sentinel


# ---------------------------------------------------------------------------
# AC3 -- backfill windowing edges + trial window
# ---------------------------------------------------------------------------


class TestBackfillWindows:
    _TODAY = date(2026, 7, 21)  # date_to (yesterday) = 2026-07-20

    def test_31_days_single_window(self):
        windows = at.compute_backfill_windows(31, today=self._TODAY)
        assert len(windows) == 1
        assert windows[0]["date_to"] == "2026-07-20"
        assert windows[0]["date_from"] == "2026-06-20"  # 31 days inclusive

    def test_32_days_splits_into_two(self):
        windows = at.compute_backfill_windows(32, today=self._TODAY)
        assert len(windows) == 2
        # oldest-first, contiguous, no overlap; last ends yesterday.
        assert windows[0]["date_from"] == "2026-06-19"
        assert windows[-1]["date_to"] == "2026-07-20"
        # first window is 31 days, second is the remaining 1 day.
        assert windows[0]["date_to"] == "2026-07-19"
        assert windows[1]["date_from"] == "2026-07-20"

    def test_365_days_twelve_windows(self):
        windows = at.compute_backfill_windows(365, today=self._TODAY)
        # ceil(365 / 31) = 12 windows.
        assert len(windows) == 12
        assert windows[-1]["date_to"] == "2026-07-20"

    def test_windows_are_contiguous_and_cover_exactly_days(self):
        windows = at.compute_backfill_windows(70, today=self._TODAY)
        # contiguity: each window starts the day after the previous one ends.
        for prev, nxt in zip(windows, windows[1:]):
            assert date.fromisoformat(nxt["date_from"]) == date.fromisoformat(
                prev["date_to"]
            ) + __import__("datetime").timedelta(days=1)
        # total coverage == 70 days.
        total = sum(
            (date.fromisoformat(w["date_to"]) - date.fromisoformat(w["date_from"])).days + 1
            for w in windows
        )
        assert total == 70

    def test_zero_days_invalid(self):
        with pytest.raises(at.BackfillDaysInvalid):
            at.compute_backfill_windows(0, today=self._TODAY)

    def test_366_days_invalid(self):
        with pytest.raises(at.BackfillDaysInvalid):
            at.compute_backfill_windows(366, today=self._TODAY)

    def test_non_integer_invalid(self):
        with pytest.raises(at.BackfillDaysInvalid):
            at.validate_backfill_days("not-a-number")

    def test_validate_days_ok(self):
        assert at.validate_backfill_days("30") == 30


class TestTrialWindow:
    def test_trial_window_is_last_three_days(self):
        df, dt = at.trial_window(today=date(2026, 7, 21))
        assert dt == "2026-07-20"  # yesterday
        assert df == "2026-07-18"  # 3 days inclusive
        assert at.TRIAL_PULL_DAYS == 3
