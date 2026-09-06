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

    def test_module_without_declared_topology_is_refused(self):
        """A provider that declares no account_topology is REFUSED.

        This used to proceed: the guard was gated on a runtime flag that
        defaulted to off, so an undeclared provider was waved through for
        compatibility. Story 46.4 retired the flag and the policy is now
        unconditional. It costs nothing in production — all 37 shipped manifests
        declare a topology — so what this pins is that a registry regression
        cannot silently re-open the seam.
        """
        ref_row = ("conn_1", "nango_1", "some-provider", "proj_1")
        get_conn, conn, cur = _fake_connection(fetchone_return=ref_row)
        cur.description = [("id",), ("nango_connection_id",), ("provider",), ("project_id",)]

        with patch("core.db.get_connection", new=get_conn), \
             self._patch_registry({"name": "some-provider"}), \
             patch("core.queue._backend") as backend:
            from core.queue import enqueue_pull

            result = enqueue_pull("conn_1", "2026-07-01", "2026-07-03", requested_by="u")

        assert result["state"] == "refused"
        backend.enqueue_pull.assert_not_called()

    def test_topology_without_ready_scope_refuses(self):
        """A topology-declaring provider with no selected account is REFUSED (AC4).

        The guard asks the DATASTREAM which account this pull is about, and only
        falls back to the credential-wide verified scope when it names none
        (migration 211). Nothing selected on either side is the refusal.
        """
        ref_row = ("conn_1", "nango_1", "google-analytics", "proj_1")
        get_conn, conn, cur = _fake_connection(fetchone_return=ref_row)
        cur.description = [("id",), ("nango_connection_id",), ("provider",), ("project_id",)]

        manifest = {"name": "google-analytics", "account_topology": _VALID_TOPOLOGY}
        with patch("core.db.get_connection", new=get_conn), \
             self._patch_registry(manifest), \
             patch("core.queue._resolve_selected_account", return_value=None), \
             patch("core.queue._backend") as backend:
            from core.queue import enqueue_pull

            result = enqueue_pull("conn_1", "2026-07-01", "2026-07-03", requested_by="u")

        assert result["state"] == "refused"
        assert result["code"] == "account_not_selected"
        backend.enqueue_pull.assert_not_called()

    def test_topology_with_ready_scope_still_needs_exact_account_access(self):
        """A ready scope is necessary and NOT sufficient.

        Before Story 46.4 a ready scope alone let the pull through, because the
        strict seam was behind a runtime flag that defaulted to off. It now also
        revalidates the exact provider-account exposure for the requesting
        identity, so a ready scope with no authorized account is a refusal.
        """
        ref_row = ("conn_1", "nango_1", "google-analytics", "proj_1")
        get_conn, conn, cur = _fake_connection(fetchone_return=ref_row)
        cur.description = [("id",), ("nango_connection_id",), ("provider",), ("project_id",)]

        manifest = {"name": "google-analytics", "account_topology": _VALID_TOPOLOGY}
        denied = SimpleNamespace(allowed=False, reason="account_exposure_required")
        with patch("core.db.get_connection", new=get_conn), \
             self._patch_registry(manifest), \
             patch("core.queue._resolve_selected_account", return_value="acct_1"), \
             patch("core.account_topology.is_account_ready", return_value=True), \
             patch("core.project_access.resolve_provider_account_access", return_value=denied), \
             patch("core.queue._backend") as backend:
            from core.queue import enqueue_pull

            result = enqueue_pull("conn_1", "2026-07-01", "2026-07-03", requested_by="u")

        assert result["state"] == "refused"
        backend.enqueue_pull.assert_not_called()

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


class TestOneAuthorizationManyAccounts:
    """Migration 211: the Datastream names the account, the credential no longer does.

    Before it, `connection_account_scope` carried a UNIQUE index on the
    connection alone, so one Google consent could name exactly one account for
    every Connector and every Datastream reached through it.
    """

    def test_pull_reads_the_datastreams_account_not_the_credentials(self):
        """Two Datastreams of one authorization read two different accounts."""
        from core.queue import _resolve_selected_account

        cur = MagicMock()
        cur.__enter__ = MagicMock(return_value=cur)
        cur.__exit__ = MagicMock(return_value=False)
        # The Datastream binding answers; the credential-wide scope is never reached.
        cur.fetchone = MagicMock(return_value=("properties/222",))
        conn = MagicMock()
        conn.cursor = MagicMock(return_value=cur)

        account = _resolve_selected_account(conn, "conn_1", "ds_2")

        assert account == "properties/222"
        assert "app.datastreams" in cur.execute.call_args_list[0].args[0]
        assert cur.execute.call_count == 1

    def test_a_job_without_a_datastream_still_falls_back_to_the_credential(self):
        """The legacy per-connection path keeps the answer it always had.

        One ready account under the consent: taking it is not a guess, it is the
        only thing there is. `fetchall` and not `fetchone` since 2026-08-30 --
        the fallback COUNTS the candidates before it answers, because a count of
        one and a count of three are two different answers.
        """
        from core.queue import _resolve_selected_account

        cur = MagicMock()
        cur.__enter__ = MagicMock(return_value=cur)
        cur.__exit__ = MagicMock(return_value=False)
        cur.fetchall = MagicMock(return_value=[("sc-domain:example.org", "gsc")])
        conn = MagicMock()
        conn.cursor = MagicMock(return_value=cur)

        account = _resolve_selected_account(conn, "conn_1", None, connector="gsc")

        assert account == "sc-domain:example.org"
        assert "connection_account_scope" in cur.execute.call_args_list[0].args[0]

    def test_a_datastream_without_a_binding_falls_back_too(self):
        """A row created before 211 keeps reading what its pulls already read."""
        from core.queue import _resolve_selected_account

        cur = MagicMock()
        cur.__enter__ = MagicMock(return_value=cur)
        cur.__exit__ = MagicMock(return_value=False)
        cur.fetchone = MagicMock(return_value=None)
        cur.fetchall = MagicMock(return_value=[("sc-domain:example.org", "gsc")])
        conn = MagicMock()
        conn.cursor = MagicMock(return_value=cur)

        assert (
            _resolve_selected_account(conn, "conn_1", "ds_old", connector="gsc")
            == "sc-domain:example.org"
        )
        assert cur.execute.call_count == 2

    def test_the_fallback_is_narrowed_to_the_connectors_own_accounts(self):
        """2026-08-30: the SQL carries the connector, and NULL stays admissible.

        `app.connection_account_scope` has no connector column -- measured on the
        disposable base, its columns are id / connection_ref_id / account_id /
        account_label / state / verified_at / selected_by / created_at /
        updated_at / selection_path. The dimension is
        `app.credential_accounts.discovered_for_connector` (migration 210), and
        the only correct way to ask is `account_topology.account_connector_sql`,
        which admits NULL because a pre-210 row is unknown, not foreign.
        """
        from core.queue import _resolve_selected_account

        cur = MagicMock()
        cur.__enter__ = MagicMock(return_value=cur)
        cur.__exit__ = MagicMock(return_value=False)
        cur.fetchall = MagicMock(return_value=[("sc-domain:example.org", "gsc")])
        conn = MagicMock()
        conn.cursor = MagicMock(return_value=cur)

        _resolve_selected_account(conn, "conn_1", None, connector="gsc")

        sql = " ".join(cur.execute.call_args_list[0].args[0].split()).lower()
        params = cur.execute.call_args_list[0].args[1]
        assert "discovered_for_connector = any(%s)" in sql, sql
        assert "discovered_for_connector is null" in sql, sql
        assert ["gsc"] in params, params

    def test_two_ready_accounts_of_one_connector_refuse_instead_of_picking(self):
        """The guess this repair removes, stated as a raise.

        `ORDER BY verified_at DESC LIMIT 1` answered THIS case with whichever
        account was verified last -- a coin flip between two properties a person
        never told apart.
        """
        from core.queue import AccountSelectionAmbiguous, _resolve_selected_account

        cur = MagicMock()
        cur.__enter__ = MagicMock(return_value=cur)
        cur.__exit__ = MagicMock(return_value=False)
        cur.fetchone = MagicMock(return_value=None)
        cur.fetchall = MagicMock(
            return_value=[
                ("properties/222", "google-analytics"),
                ("properties/111", "google-analytics"),
            ]
        )
        conn = MagicMock()
        conn.cursor = MagicMock(return_value=cur)

        with pytest.raises(AccountSelectionAmbiguous) as raised:
            _resolve_selected_account(
                conn, "conn_1", "ds_unbound", connector="google-analytics"
            )

        assert raised.value.accounts == ["properties/222", "properties/111"]
        assert raised.value.connector == "google-analytics"

    def test_selecting_a_second_account_adds_it_instead_of_overwriting(self):
        """The UPSERT conflicts on (connection, account), never on the connection."""
        get_conn, conn, cur = _fake_connection(
            fetchone_return=(
                "ascope_2", "conn_1", "properties/222", "Second", "ready",
                None, "u", None, None,
            ),
            description=[
                ("id",), ("connection_ref_id",), ("account_id",), ("account_label",),
                ("state",), ("verified_at",), ("selected_by",), ("created_at",),
                ("updated_at",),
            ],
        )
        with patch("core.db.get_connection", new=get_conn):
            at.upsert_scope(
                "conn_1",
                account_id="properties/222",
                account_label="Second",
                state=at.STATE_READY,
                selected_by="u",
                verified_at=None,
            )

        sql = cur.execute.call_args.args[0]
        assert "ON CONFLICT (connection_ref_id, account_id) WHERE account_id IS NOT NULL" in sql

    def test_the_pending_row_still_conflicts_on_the_connection_alone(self):
        """"A selection is expected here" is one row per authorization."""
        get_conn, conn, cur = _fake_connection(
            fetchone_return=(
                "ascope_1", "conn_1", None, None, "pending_account_selection",
                None, "u", None, None,
            ),
            description=[
                ("id",), ("connection_ref_id",), ("account_id",), ("account_label",),
                ("state",), ("verified_at",), ("selected_by",), ("created_at",),
                ("updated_at",),
            ],
        )
        with patch("core.db.get_connection", new=get_conn):
            at.upsert_scope(
                "conn_1",
                account_id=None,
                account_label=None,
                state=at.STATE_PENDING,
                selected_by="u",
                verified_at=None,
            )

        sql = cur.execute.call_args.args[0]
        assert "ON CONFLICT (connection_ref_id) WHERE account_id IS NULL" in sql

    def test_the_connector_predicate_never_asks_the_authorization(self):
        """`connection_ref.connector_id` does not exist; the ACCOUNT holds the answer."""
        sql = at.account_connector_sql()

        assert "discovered_for_connector" in sql
        assert "connector_id" not in sql
        # NULL is "unknown" (migration 210), not "belongs to no Connector".
        assert "IS NULL" in sql
