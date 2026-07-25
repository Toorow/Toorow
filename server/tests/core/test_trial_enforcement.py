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
from datetime import date
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
# 34.2 (F1 fix) -- draft->enable path also re-checks the cap
# ---------------------------------------------------------------------------


def test_update_enabling_draft_over_cap_is_refused():
    """Enabling a currently-disabled datastream (False->True) re-checks the trial
    cap -- closes the create-draft-then-PATCH-enabled bypass (review F1)."""
    from core import datastreams
    from core.trial_enforcement import TrialDatastreamLimitError

    existing = {"id": "ds_4", "project_id": "p1", "enabled": False}
    with patch.object(datastreams, "get_datastream", return_value=existing), patch(
        "core.trial_enforcement.check_datastream_limit",
        side_effect=TrialDatastreamLimitError(org_id="org_x", limit=3, current=3),
    ):
        with pytest.raises(TrialDatastreamLimitError):
            datastreams.update_datastream("ds_4", "p1", {"enabled": True}, MagicMock())


def test_update_already_enabled_is_not_rechecked():
    """True->True (idempotent) must NOT re-check the cap (the ds already counts)."""
    from core import datastreams

    existing = {"id": "ds_1", "project_id": "p1", "enabled": True}
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value.fetchone.return_value = None
    guard = MagicMock(side_effect=AssertionError("cap must not be re-checked"))
    with patch.object(datastreams, "get_datastream", return_value=existing), patch(
        "core.trial_enforcement.check_datastream_limit", guard
    ):
        datastreams.update_datastream("ds_1", "p1", {"enabled": True}, conn)
    guard.assert_not_called()


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
