"""Tests for Stories 34.2 & 34.3 -- trial-governance enforcement (Epic 34).

All OFFLINE (no Postgres): the DB I/O boundary functions of
``core.trial_enforcement`` (_resolve_org_for_project, _count_active_datastreams,
_resolve_org_for_connection) and ``resolve_entitlements`` are mocked, exactly as
test_org_entitlements.py mocks _fetch_org_plan_row. This isolates the pure
enforcement logic and covers every AC without a live database.

Coverage map:
  34.2 -- 3 OK / 4th refused (typed) + signal ; full/internal unlimited ;
          existing >cap untouched (no destructive path exercised) ;
          drafts (enabled=False) skip the guard at the create seam.
  34.3 -- clamp 365->30 ; full/internal no clamp ; recent pull not clamped ;
          super-admin allow-list deny-by-default (404 vs staff).
"""

from __future__ import annotations

import json
import os
from datetime import date, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

from core import super_admin as sa  # noqa: E402
from core import trial_enforcement as te  # noqa: E402

_ENT = "core.org_entitlements.resolve_entitlements"


@pytest.fixture()
def anyio_backend():
    """Pin anyio to asyncio (trio is not a project dep) -- suite convention."""
    return "asyncio"


# ===========================================================================
# 34.2 -- datastream count guard
# ===========================================================================


def _conn() -> MagicMock:
    """A throwaway conn -- the I/O functions are mocked so it is never used."""
    return MagicMock()


def test_datastream_third_ok_at_limit_minus_one():
    """Trial org with 2 active -> creating the 3rd is allowed (3 == limit)."""
    with patch.object(te, "_resolve_org_for_project", return_value="org_x"), patch.object(
        te, "_count_active_datastreams", return_value=2
    ), patch(_ENT, return_value={"max_datastreams": 3, "max_backfill_days": 30}):
        # No exception == allowed.
        te.check_datastream_limit("proj_1", _conn())


def test_datastream_fourth_refused_typed_and_signal():
    """Trial org already at 3 -> 4th refused with typed error + signal recorded."""
    with patch.object(te, "_resolve_org_for_project", return_value="org_x"), patch.object(
        te, "_count_active_datastreams", return_value=3
    ), patch(_ENT, return_value={"max_datastreams": 3, "max_backfill_days": 30}), patch.object(
        te, "_record_trial_limit_signal"
    ) as rec:
        with pytest.raises(te.TrialDatastreamLimitError) as exc:
            te.check_datastream_limit("proj_1", _conn(), identity="alice")
    assert exc.value.code == "trial_datastream_limit"
    assert exc.value.limit == 3
    assert exc.value.current == 3
    # Typed dict for the surface (409).
    d = exc.value.to_dict()
    assert d["code"] == "trial_datastream_limit"
    assert d["org_id"] == "org_x"
    # Signal emitted exactly once.
    rec.assert_called_once()
    assert rec.call_args[0][0] == "org_x"


def test_datastream_full_plan_unlimited():
    """full/internal (max_datastreams=None) -> guard is a no-op even at 10 active."""
    with patch.object(te, "_resolve_org_for_project", return_value="org_full"), patch.object(
        te, "_count_active_datastreams", return_value=10
    ) as cnt, patch(_ENT, return_value={"max_datastreams": None, "max_backfill_days": None}):
        te.check_datastream_limit("proj_1", _conn())
        # Unlimited short-circuits BEFORE counting.
        cnt.assert_not_called()


def test_datastream_no_org_skips_guard():
    """Org-less legacy project -> no guard (fail-open, no entitlement read)."""
    with patch.object(te, "_resolve_org_for_project", return_value=None), patch(
        _ENT
    ) as ent:
        te.check_datastream_limit("default", _conn())
        ent.assert_not_called()


def test_datastream_inherited_over_cap_not_destructive():
    """An org sitting at 5 active (inherited/demoted) -> creating a NEW one is refused,
    but the guard NEVER deletes/alters the existing rows (it only raises)."""
    with patch.object(te, "_resolve_org_for_project", return_value="org_y"), patch.object(
        te, "_count_active_datastreams", return_value=5
    ), patch(_ENT, return_value={"max_datastreams": 3, "max_backfill_days": 30}), patch.object(
        te, "_record_trial_limit_signal"
    ):
        with pytest.raises(te.TrialDatastreamLimitError):
            te.check_datastream_limit("proj_y", _conn())
    # No count/delete of existing rows is performed -- the guard is read + raise only.


def test_create_datastream_draft_skips_guard():
    """create_datastream with enabled=False (draft) must NOT invoke the guard."""
    from core import datastreams as ds

    conn = MagicMock()
    cur = conn.cursor.return_value.__enter__.return_value
    cur.description = [("id",), ("project_id",), ("name",), ("module_name",)]
    cur.fetchone.return_value = ("ds_1", "proj_1", "n", "shopify")
    with patch.object(ds, "_row_to_dict", return_value={"id": "ds_1"}), patch(
        "core.trial_enforcement.check_datastream_limit"
    ) as guard:
        ds.create_datastream(
            {"name": "n", "module_name": "shopify", "enabled": False},
            "proj_1",
            "alice",
            conn,
        )
        guard.assert_not_called()


def test_create_datastream_enabled_invokes_guard():
    """create_datastream with enabled=True must invoke the 34.2 guard before INSERT."""
    from core import datastreams as ds

    conn = MagicMock()
    cur = conn.cursor.return_value.__enter__.return_value
    cur.description = [("id",)]
    cur.fetchone.return_value = ("ds_1",)
    with patch.object(ds, "_row_to_dict", return_value={"id": "ds_1"}), patch(
        "core.trial_enforcement.check_datastream_limit"
    ) as guard:
        ds.create_datastream(
            {"name": "n", "module_name": "shopify", "enabled": True},
            "proj_1",
            "alice",
            conn,
        )
        guard.assert_called_once()


# ===========================================================================
# 34.3 -- backfill window clamp
# ===========================================================================

_TODAY = date(2026, 7, 22)


def test_backfill_clamp_365_to_30():
    """Trial org: a 365-day backfill is clamped to today-30d (not rejected)."""
    with patch.object(te, "_resolve_org_for_connection", return_value="org_x"), patch(
        _ENT, return_value={"max_backfill_days": 30, "max_datastreams": 3}
    ):
        out = te.clamp_backfill_window(
            "conn_1", "2025-07-22", "2026-07-21", _conn(), today=_TODAY
        )
    assert out["clamped"] is True
    assert out["date_from"] == "2026-06-22"  # 2026-07-22 minus 30 days
    assert out["date_to"] == "2026-07-21"  # date_to untouched
    assert out["max_backfill_days"] == 30


def test_backfill_full_plan_no_clamp():
    """full/internal (max_backfill_days=None) -> requested window respected."""
    with patch.object(te, "_resolve_org_for_connection", return_value="org_full"), patch(
        _ENT, return_value={"max_backfill_days": None, "max_datastreams": None}
    ):
        out = te.clamp_backfill_window(
            "conn_1", "2024-01-01", "2026-07-21", _conn(), today=_TODAY
        )
    assert out["clamped"] is False
    assert out["date_from"] == "2024-01-01"
    assert out["max_backfill_days"] is None


def test_backfill_recent_pull_not_clamped():
    """Trial org: a recent pull (< 30d) is inside the ceiling -> no clamp."""
    with patch.object(te, "_resolve_org_for_connection", return_value="org_x"), patch(
        _ENT, return_value={"max_backfill_days": 30, "max_datastreams": 3}
    ):
        out = te.clamp_backfill_window(
            "conn_1", "2026-07-10", "2026-07-21", _conn(), today=_TODAY
        )
    assert out["clamped"] is False
    assert out["date_from"] == "2026-07-10"


def test_backfill_no_org_no_clamp():
    """Unresolvable org -> no clamp, no entitlement read."""
    with patch.object(te, "_resolve_org_for_connection", return_value=None), patch(
        _ENT
    ) as ent:
        out = te.clamp_backfill_window(
            "conn_1", "2020-01-01", "2026-07-21", _conn(), today=_TODAY
        )
    assert out["clamped"] is False
    ent.assert_not_called()


def test_backfill_malformed_date_untouched():
    """A malformed date_from is not touched (the pull path validates dates)."""
    with patch.object(te, "_resolve_org_for_connection", return_value="org_x"), patch(
        _ENT, return_value={"max_backfill_days": 30, "max_datastreams": 3}
    ):
        out = te.clamp_backfill_window(
            "conn_1", "not-a-date", "2026-07-21", _conn(), today=_TODAY
        )
    assert out["clamped"] is False
    assert out["date_from"] == "not-a-date"


# ---------------------------------------------------------------------------
# 58.4 -- the ceiling never returns a window that means nothing.
#
# PURE DATE ARITHMETIC, WITH THE NUMBERS WRITTEN OUT. `today` is passed in and
# every date is a literal: this is the one rule of this module that a fixture
# could hide, because the floor is computed from the clock and a test that let
# the clock decide would pass today and rot next month.
#
# The three cases are the ones reproduced on 2026-08-06 against the shipped
# helper -- the first two wrote `date_from > date_to` into `app.pull_jobs`, and
# `grep -rn "date_from > date_to" server/core` finds nothing that refused them.
# ---------------------------------------------------------------------------

_TODAY_58_4 = date(2026, 8, 6)


def _ceiling(days: int):
    return patch.object(te, "_resolve_org_for_connection", return_value="org_x"), patch(
        _ENT, return_value={"max_backfill_days": days, "max_datastreams": 3}
    )


def _clamped(date_from: str, date_to: str, ceiling: int) -> dict:
    org, ent = _ceiling(ceiling)
    with org, ent:
        return te.clamp_backfill_window(
            "conn_1", date_from, date_to, _conn(), today=_TODAY_58_4
        )


def test_a_day_older_than_a_thirty_day_ceiling_is_empty_not_inverted():
    """asked 2026-06-15..2026-06-15, ceiling 30d -> floor 2026-07-07."""
    out = _clamped("2026-06-15", "2026-06-15", 30)

    # What it used to do: date_from = 2026-07-07, three weeks AFTER date_to.
    assert out["date_from"] == "2026-06-15"
    assert out["date_to"] == "2026-06-15"
    assert out["date_from"] <= out["date_to"]
    # And the fact, which is not a clamp: there is no entitled day in here.
    assert out["empty"] is True
    assert out["reason"] == te.WINDOW_BEFORE_CEILING
    assert out["floor"] == "2026-07-07"
    assert out["max_backfill_days"] == 30


def test_the_same_day_under_a_seven_day_ceiling_is_empty_too():
    """asked 2026-06-15..2026-06-15, ceiling 7d -> floor 2026-07-30.

    The SAME window under a different ceiling, because a rule that only held for
    one entitlement value would pass on 30 and ship the hole for every other plan.
    """
    out = _clamped("2026-06-15", "2026-06-15", 7)

    assert out["floor"] == "2026-07-30"
    assert out["date_from"] == "2026-06-15" and out["date_to"] == "2026-06-15"
    assert out["date_from"] <= out["date_to"]
    assert out["empty"] is True


def test_a_day_inside_the_ceiling_is_untouched_and_not_empty():
    """asked 2026-08-01..2026-08-01, ceiling 30d -> inside, nothing happens."""
    out = _clamped("2026-08-01", "2026-08-01", 30)

    assert out["date_from"] == "2026-08-01"
    assert out["clamped"] is False
    assert out["empty"] is False
    assert out["reason"] is None


def test_a_window_straddling_the_floor_is_still_reduced_and_never_empty():
    """The clamp that WORKS is not broken by the guard.

    A window that starts before the floor and ends after it keeps its entitled
    tail: `date_from` is raised, `date_to` is untouched, `clamped` is true and
    `empty` is false. Turning this case into a refusal would have traded one
    defect for a worse one -- refusing a pull the organisation may have.
    """
    out = _clamped("2025-01-01", "2026-08-05", 30)

    assert out["date_from"] == "2026-07-07"
    assert out["date_to"] == "2026-08-05"
    assert out["clamped"] is True
    assert out["empty"] is False


def test_the_ceiling_never_returns_a_window_whose_start_is_after_its_end():
    """The invariant itself, swept over every ceiling and every offset.

    Written as a sweep rather than three cases because the defect was not a bad
    number, it was a comparison that was never made: the helper raising
    `date_from` cannot see `date_to`. Any (ceiling, day) pair may reach here.
    """
    for ceiling in (1, 7, 30, 90, 365):
        for offset in range(0, 400, 13):
            day = (_TODAY_58_4 - timedelta(days=offset)).isoformat()
            out = _clamped(day, day, ceiling)
            assert out["date_from"] <= out["date_to"], (ceiling, day, out)
            # And an empty window is never dressed as a reduced one.
            entitled = out["floor"] is None or out["floor"] <= day
            assert out["empty"] is not entitled, (ceiling, day, out)


def test_an_unresolved_org_has_no_floor_and_no_emptiness():
    """No entitlement read, no ceiling, no new fact invented."""
    with patch.object(te, "_resolve_org_for_connection", return_value=None), patch(_ENT):
        out = te.clamp_backfill_window(
            "conn_1", "2020-01-01", "2020-01-01", _conn(), today=_TODAY_58_4
        )
    assert out["empty"] is False
    assert out["floor"] is None


# ===========================================================================
# super-admin allow-list (deny-by-default) -- gates the org_plan control surface
# ===========================================================================


def test_super_admin_deny_by_default_when_unset(monkeypatch):
    monkeypatch.delenv("TOOROW_SUPER_ADMINS", raising=False)
    assert sa.is_super_admin("alice@toorow.io") is False
    assert sa.is_super_admin("") is False
    assert sa.is_super_admin(None) is False


def test_super_admin_allows_listed_email(monkeypatch):
    monkeypatch.setenv("TOOROW_SUPER_ADMINS", "alice@toorow.io, bob@toorow.io")
    assert sa.is_super_admin("alice@toorow.io") is True
    # Case-insensitive + whitespace-trimmed.
    assert sa.is_super_admin("  BOB@toorow.io ") is True
    # Not on the list -> denied.
    assert sa.is_super_admin("mallory@evil.io") is False


def test_super_admin_blank_env_denies(monkeypatch):
    monkeypatch.setenv("TOOROW_SUPER_ADMINS", "   ")
    assert sa.is_super_admin("alice@toorow.io") is False


# ===========================================================================
# org_plan control endpoint (Story 34.3) -- staff OK + audit / non-staff 404
# ===========================================================================

_AUTHN = "core.api_auth.authenticate_api_request"


def _post(body: dict) -> MagicMock:
    req = MagicMock()
    req.body = AsyncMock(return_value=json.dumps(body).encode())
    req.path_params = {}
    return req


@pytest.mark.anyio
async def test_org_plan_endpoint_non_staff_gets_404(monkeypatch):
    """A non-super-admin caller gets 404 (deny-by-default; surface hidden)."""
    monkeypatch.setenv("TOOROW_SUPER_ADMINS", "alice@toorow.io")
    from core.org_plan_api import _set_org_plan

    with patch(_AUTHN, AsyncMock(return_value=(True, "mallory@evil.io"))), patch(
        "core.org_entitlements.set_org_plan"
    ) as sop:
        r = await _set_org_plan(_post({"org_id": "org_x", "plan": "full"}))
    assert r.status_code == 404
    # set_org_plan must NOT be called for a denied caller.
    sop.assert_not_called()


@pytest.mark.anyio
async def test_org_plan_endpoint_unauthenticated_401(monkeypatch):
    monkeypatch.setenv("TOOROW_SUPER_ADMINS", "alice@toorow.io")
    from core.org_plan_api import _set_org_plan

    with patch(_AUTHN, AsyncMock(return_value=(False, "anonymous"))):
        r = await _set_org_plan(_post({"org_id": "org_x", "plan": "full"}))
    assert r.status_code == 401


@pytest.mark.anyio
async def test_org_plan_endpoint_staff_ok_and_audited(monkeypatch):
    """Super-admin promotion calls set_org_plan and audits the change (AD-7)."""
    monkeypatch.setenv("TOOROW_SUPER_ADMINS", "alice@toorow.io")
    from core.org_plan_api import _set_org_plan

    with patch(_AUTHN, AsyncMock(return_value=(True, "alice@toorow.io"))), patch(
        "core.org_entitlements.set_org_plan"
    ) as sop, patch("core.audit.write_audit_row") as aud:
        r = await _set_org_plan(_post({"org_id": "org_x", "plan": "full", "reason": "paid"}))
    assert r.status_code == 200
    payload = json.loads(r.body)
    assert payload == {"org_id": "org_x", "plan": "full"}
    # set_org_plan called with the caller as granted_by (AD-14).
    sop.assert_called_once()
    assert sop.call_args.args[0] == "org_x"
    assert sop.call_args.args[1] == "full"
    assert sop.call_args.kwargs.get("granted_by") == "alice@toorow.io"
    # Audited exactly once.
    aud.assert_called_once()


@pytest.mark.anyio
async def test_org_plan_endpoint_invalid_plan_422(monkeypatch):
    monkeypatch.setenv("TOOROW_SUPER_ADMINS", "alice@toorow.io")
    from core.org_plan_api import _set_org_plan

    with patch(_AUTHN, AsyncMock(return_value=(True, "alice@toorow.io"))), patch(
        "core.org_entitlements.set_org_plan", side_effect=ValueError("Invalid plan 'gold'")
    ), patch("core.audit.write_audit_row"):
        r = await _set_org_plan(_post({"org_id": "org_x", "plan": "gold"}))
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# 34.2 (F1 fix) -- the draft->enable PATCH path is closed by the governed guard
# ---------------------------------------------------------------------------


def test_update_enabling_draft_is_refused_before_any_cap_check():
    """The False->True enable PATCH no longer reaches the trial cap: the
    governed-fields guard in update_datastream refuses `enabled` first --
    lifecycle changes require a reviewed Datastream operation. The
    create-draft-then-PATCH-enabled bypass this family closed (34.2, review F1)
    stays closed, one gate earlier; the cap check must not even run."""
    from core import datastreams

    guard = MagicMock(side_effect=AssertionError("cap check must not run"))
    with patch("core.trial_enforcement.check_datastream_limit", guard):
        with pytest.raises(ValueError, match="reviewed Datastream operation"):
            datastreams.update_datastream("ds_4", "p1", {"enabled": True}, MagicMock())
    guard.assert_not_called()


def test_update_enable_patch_is_refused_even_when_already_enabled():
    """True->True is refused by the same guard: no enable PATCH exists at all,
    so the question of re-checking the cap on that path is moot."""
    from core import datastreams

    with pytest.raises(ValueError, match="reviewed Datastream operation"):
        datastreams.update_datastream("ds_1", "p1", {"enabled": True}, MagicMock())


def test_update_without_enabling_skips_the_guard():
    """A patch that does not flip enabled (e.g. rename) never invokes the cap guard."""
    from core import datastreams

    existing = {"id": "ds_1", "project_id": "p1", "enabled": False}
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value.fetchone.return_value = None
    guard = MagicMock(side_effect=AssertionError("cap must not be checked on rename"))
    with patch.object(datastreams, "get_datastream", return_value=existing), patch(
        "core.trial_enforcement.check_datastream_limit", guard
    ):
        datastreams.update_datastream("ds_1", "p1", {"name": "renamed"}, conn)
    guard.assert_not_called()


# ---------------------------------------------------------------------------
# 34.2 (P0-1, audit 2026-08-17) -- the guard on the ACT that goes live
#
# `publish_activate_mutation` is the statement that writes `enabled=TRUE`, and it
# is the single act behind BOTH doors that publish: the wizard's REST seam
# (`datastream_preconfiguration_api`) and the MCP tool
# (`datastream_first_candidate_mcp.publish_activate_datastream_candidate`).
# Before this fix it never met `check_datastream_limit`, so the full wizard
# journey and its MCP equivalent activated the (cap+1)-th Datastream of a trial
# org. These tests exercise the ACT, which is what makes them cover every door.
# ---------------------------------------------------------------------------


def _publish_candidate() -> dict:
    """A Ready, verified, isolated, non-empty candidate -- the only shape the
    review accepts. Identical in spirit to the fixture in
    test_datastream_activation.py; kept local so this file stays self-contained."""
    return {
        "project_id": "proj_1",
        "datastream_id": "ds_1",
        "execution_id": "dse_01",
        "state": "ready",
        "adapter_verified": True,
        "placeholder": False,
        "artifact_ref": "candidate/dse_01/output",
        "content_hash": "f" * 64,
        "row_count": 10,
        "plan_version_id": "dsp_1",
        "mapping_version_id": "dmap_1",
        "projection_hash": "a" * 64,
        "output_plan": {"kind": "full_grain"},
        "schedule": {"next_run_at": "2026-07-30T02:00:00Z"},
        "expected_current_execution_id": None,
        "dq": {"blocking": []},
    }


def _publish_conn(*, enabled: bool) -> tuple[MagicMock, MagicMock]:
    """A conn whose locked row reports the Datastream's current `enabled`.

    `org_id` (index 4) is left None on purpose: the Outputs block that follows the
    activation is a different subject, proven where a warehouse exists
    (`tests/integration/test_recurring_retrieval_arming_pg.py`). What is measured
    here is whether the cap is met BEFORE any of it runs.
    """
    conn = MagicMock()
    cur = conn.cursor.return_value.__enter__.return_value
    cur.fetchone.return_value = ("ready", "f" * 64, 10, None, None, enabled)
    cur.rowcount = 1
    return conn, cur


def _trial(count: int, limit: int | None = 3):
    """Patch context: a trial org at *count* active datastreams."""
    return (
        patch.object(te, "_resolve_org_for_project", return_value="org_x"),
        patch.object(te, "_count_active_datastreams", return_value=count),
        patch(_ENT, return_value={"max_datastreams": limit, "max_backfill_days": 30}),
        patch.object(te, "_record_trial_limit_signal"),
    )


def test_publish_activate_refuses_the_cap_plus_one_and_writes_nothing():
    """A trial org already at 3 active cannot publish-activate a 4th.

    This is the hole P0-1 named: the review was frozen, the candidate was Ready,
    and the act went through. It is now refused BEFORE the first write, so the
    execution stays `ready` and the Current pointer is untouched -- there is
    nothing to unwind and the person can retry after freeing an allowance."""
    from core.datastream_activation import build_candidate_review, publish_activate_mutation

    conn, cur = _publish_conn(enabled=False)
    org, count, ent, signal = _trial(3)
    with org, count, ent, signal, patch(
        "core.datastream_activation._promote_managed_candidate"
    ) as promote, patch("core.datastream_publication.advance_state") as machine:
        with pytest.raises(te.TrialDatastreamLimitError) as exc:
            publish_activate_mutation(
                conn,
                review=build_candidate_review(_publish_candidate()),
                actor="operator",
                operation_id="op_1",
            )

    assert exc.value.code == "trial_datastream_limit"
    # Nothing was published: no promotion, and no statement that moves the
    # execution out of `ready` or flips the Datastream live.
    promote.assert_not_called()
    # The raw `state='publishing'` literal left this module with AI-223: the one
    # state machine is now the only writer, so "nothing moved" is asserted on it.
    machine.assert_not_called()
    written = "\n".join(str(call.args[0]) for call in cur.execute.call_args_list)
    assert "enabled=TRUE" not in written


def test_publish_activate_under_the_cap_publishes():
    """The same org at 2 active publishes its 3rd: the guard is a limit, not a wall."""
    from core.datastream_activation import build_candidate_review, publish_activate_mutation

    conn, cur = _publish_conn(enabled=False)
    org, count, ent, signal = _trial(2)
    # The subject here is the trial cap, not the run's walk: the one state
    # machine (AI-223) reads state for real, which a frozen MagicMock row
    # cannot answer twice, so it is stubbed like _promote_managed_candidate.
    with org, count, ent, signal, patch(
        "core.datastream_activation._promote_managed_candidate", return_value=None
    ), patch("core.datastream_publication.advance_state"):
        result = publish_activate_mutation(
            conn,
            review=build_candidate_review(_publish_candidate()),
            actor="operator",
            operation_id="op_1",
        )

    assert result.outcome == "succeeded"
    written = "\n".join(str(call.args[0]) for call in cur.execute.call_args_list)
    assert "lifecycle_state='active',enabled=TRUE" in written


def test_publish_activate_not_slowed_for_an_org_off_trial():
    """A full/internal org (max_datastreams=None) publishes at 10 active, and the
    count is never even read -- the guard costs an unbounded org nothing."""
    from core.datastream_activation import build_candidate_review, publish_activate_mutation

    conn, _cur = _publish_conn(enabled=False)
    with patch.object(te, "_resolve_org_for_project", return_value="org_full"), patch.object(
        te, "_count_active_datastreams", return_value=10
    ) as counted, patch(
        _ENT, return_value={"max_datastreams": None, "max_backfill_days": None}
    ), patch("core.datastream_activation._promote_managed_candidate", return_value=None), patch(
        "core.datastream_publication.advance_state"
    ):
        result = publish_activate_mutation(
            conn,
            review=build_candidate_review(_publish_candidate()),
            actor="operator",
            operation_id="op_1",
        )

    assert result.outcome == "succeeded"
    counted.assert_not_called()


def test_publish_activate_republication_of_a_live_datastream_is_not_refused():
    """Republishing an ALREADY active Datastream at exactly the cap must pass.

    It changes no count. Counting it would refuse a trial org the right to fix
    the mapping of its own last Datastream -- freezing it on whatever shape it
    happened to launch with. Same reading as the schedule door's
    `if enabled and not already_enabled`."""
    from core.datastream_activation import build_candidate_review, publish_activate_mutation

    conn, _cur = _publish_conn(enabled=True)
    guard = MagicMock(side_effect=AssertionError("a republication consumes no allowance"))
    with patch("core.trial_enforcement.check_datastream_limit", guard), patch(
        "core.datastream_activation._promote_managed_candidate", return_value=None
    ), patch("core.datastream_publication.advance_state"):
        result = publish_activate_mutation(
            conn,
            review=build_candidate_review(_publish_candidate()),
            actor="operator",
            operation_id="op_1",
        )

    assert result.outcome == "succeeded"
    guard.assert_not_called()


def test_the_refusal_names_the_gesture_and_speaks_the_product_language():
    """The message tells the person what frees an allowance, in one language.

    It used to be half French half English and asked for a DELETION, which the
    counter never required -- it reads `enabled = TRUE`."""
    message = te.TrialDatastreamLimitError(org_id="org_x", limit=3, current=3).message
    assert message.isascii()
    lowered = message.lower()
    assert "turn off" in lowered  # the gesture that frees an allowance
    assert "full plan" in lowered  # the other way out
    assert "delete" not in lowered
    # No table, column or state name leaks to the person.
    for db_word in ("enabled", "datastreams.", "lifecycle_state", "app.", "true"):
        assert db_word not in lowered


def test_wizard_seam_maps_the_trial_refusal_to_409_not_to_its_mute_503():
    """Unmapped, the refusal fell into `_error`'s 503 "Datastream setup is
    unavailable" -- a trial org would be told its setup was broken. It now gets
    the same 409 body as the creation seam."""
    import json as _json

    from core.datastream_preconfiguration_api import _activation_error

    response = _activation_error(te.TrialDatastreamLimitError(org_id="org_x", limit=3, current=3))
    assert response.status_code == 409
    body = _json.loads(response.body)
    assert body["code"] == "trial_datastream_limit"
    assert body["limit"] == 3


def test_mcp_publish_door_maps_the_trial_refusal_to_its_own_code():
    """The MCP door must not report a plan refusal as `server_error`.

    The catch-all below it turns any unmapped exception into
    "Publication unavailable", which would send the agent retrying a call that
    can only keep failing instead of naming the gesture to its user."""
    import inspect

    from core import datastream_first_candidate_mcp as door

    source = inspect.getsource(door)
    assert "except TrialDatastreamLimitError as exc:" in source
    assert "_tool_error(exc.code, exc.message)" in source


# ---------------------------------------------------------------------------
# 34.3 (#3 provisioning bridge) -- service-token path for toorow-admin
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_org_plan_service_token_authorizes_as_service(monkeypatch):
    """A matching X-Provision-Token authorizes as svc:toorow-admin without OAuth."""
    monkeypatch.setenv("TOOROW_PROVISION_TOKEN", "svc-secret-123")
    from core.org_plan_api import _set_org_plan

    req = _post({"org_id": "org_x", "plan": "trial"})
    req.headers = {"X-Provision-Token": "svc-secret-123"}
    with patch(_AUTHN) as authn, patch("core.org_entitlements.set_org_plan") as sop, patch(
        "core.audit.write_audit_row"
    ):
        r = await _set_org_plan(req)
    assert r.status_code == 200
    authn.assert_not_called()  # service path bypasses the human OAuth verifier
    sop.assert_called_once()
    assert sop.call_args.kwargs["granted_by"] == "svc:toorow-admin"


@pytest.mark.anyio
async def test_org_plan_wrong_service_token_falls_through_to_oauth(monkeypatch):
    """A wrong/absent provision token must NOT bypass -- falls to the OAuth path."""
    monkeypatch.setenv("TOOROW_PROVISION_TOKEN", "svc-secret-123")
    monkeypatch.setenv("TOOROW_SUPER_ADMINS", "alice@toorow.io")
    from core.org_plan_api import _set_org_plan

    req = _post({"org_id": "org_x", "plan": "trial"})
    req.headers = {"X-Provision-Token": "WRONG"}
    with patch(_AUTHN, AsyncMock(return_value=(True, "mallory@evil.io"))), patch(
        "core.org_entitlements.set_org_plan"
    ) as sop:
        r = await _set_org_plan(req)
    assert r.status_code == 404  # non-staff via OAuth -> hidden
    sop.assert_not_called()


@pytest.mark.anyio
async def test_org_plan_service_token_disabled_when_secret_unset(monkeypatch):
    """If TOOROW_PROVISION_TOKEN is unset, the service path never authorizes."""
    monkeypatch.delenv("TOOROW_PROVISION_TOKEN", raising=False)
    monkeypatch.setenv("TOOROW_SUPER_ADMINS", "alice@toorow.io")
    from core.org_plan_api import _set_org_plan

    req = _post({"org_id": "org_x", "plan": "trial"})
    req.headers = {"X-Provision-Token": "anything"}
    with patch(_AUTHN, AsyncMock(return_value=(True, "mallory@evil.io"))), patch(
        "core.org_entitlements.set_org_plan"
    ) as sop:
        r = await _set_org_plan(req)
    assert r.status_code == 404
    sop.assert_not_called()
