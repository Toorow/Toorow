"""AI-176 -- the MEMBER's read of the org plan (GET /api/organizations/{id}/plan).

Epic 34 shipped the trial ceiling as enforcement only: `check_datastream_limit`
refuses the 4th datastream with a typed 409, and `org_plan_api` exposed a single
super-admin POST hidden behind a 404. Measured 2026-08-04: `grep -ril entitlement
ui/admin/src` returned ZERO files, and no route of any kind let an org member
read its own plan. The person met the ceiling by hitting it.

What these tests pin is not "the endpoint answers 200". It is the four ways a
plan counter is worse than none:

  1. it counts something OTHER than what the refusal counts;
  2. it renders `None` (full/internal = no cap) as a number, so an unlimited org
     reads as capped at 0;
  3. it discloses the plan of an org the caller is not a member of;
  4. it invents a reassuring empty state when the read fails.

All OFFLINE -- the DB seam and the entitlement resolution are mocked, same
posture as test_trial_enforcement.py.
"""

from __future__ import annotations

import json
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

_AUTHN = "core.api_auth.authenticate_api_request"
_ACCESS = "core.project_access.identity_has_org_access"
_COUNT = "core.trial_enforcement.count_active_datastreams"
_PLAN = "core.org_entitlements.get_org_plan"
_LIMITS = "core.org_entitlements.resolve_entitlements"


@pytest.fixture()
def anyio_backend():
    """Pin anyio to asyncio (trio is not a project dep) -- suite convention."""
    return "asyncio"


def _get(org_id: str = "org_x") -> MagicMock:
    req = MagicMock()
    req.path_params = {"org_id": org_id}
    return req


def _conn_ctx() -> MagicMock:
    """A `get_connection()` context manager whose conn is never really used."""
    ctx = MagicMock()
    ctx.__enter__ = MagicMock(return_value=MagicMock())
    ctx.__exit__ = MagicMock(return_value=False)
    return ctx


# ---------------------------------------------------------------------------
# The happy path, and the shape the console renders
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_trial_member_reads_plan_limits_and_usage():
    from core.org_plan_api import _get_org_plan

    with patch(_AUTHN, AsyncMock(return_value=(True, "owner@example.com"))), patch(
        "core.db.get_connection", return_value=_conn_ctx()
    ), patch(_ACCESS, return_value=True), patch(_COUNT, return_value=2), patch(
        _PLAN,
        return_value={
            "org_id": "org_x",
            "plan": "trial",
            "entitlements": {"max_datastreams": 3, "max_backfill_days": 30},
            "granted_by": "alice@toorow.io",
            "granted_at": None,
        },
    ), patch(_LIMITS, return_value={"max_datastreams": 3, "max_backfill_days": 30}):
        r = await _get_org_plan(_get())

    assert r.status_code == 200
    payload = json.loads(r.body)
    assert payload["plan"] == "trial"
    assert payload["limits"] == {"max_datastreams": 3, "max_backfill_days": 30}
    assert payload["usage"] == {"active_datastreams": 2}


@pytest.mark.anyio
async def test_granted_by_is_never_disclosed_to_the_tenant():
    """Who inside toorow moved an org's plan is operator information.

    `get_org_plan` returns it; the member surface must drop it. A tenant reading
    a toorow staff identity out of its own settings page is a disclosure, not a
    feature.
    """
    from core.org_plan_api import _get_org_plan

    with patch(_AUTHN, AsyncMock(return_value=(True, "owner@example.com"))), patch(
        "core.db.get_connection", return_value=_conn_ctx()
    ), patch(_ACCESS, return_value=True), patch(_COUNT, return_value=0), patch(
        _PLAN,
        return_value={
            "org_id": "org_x",
            "plan": "full",
            "entitlements": {},
            "granted_by": "alice@toorow.io",
            "granted_at": None,
        },
    ), patch(_LIMITS, return_value={"max_datastreams": None, "max_backfill_days": None}):
        r = await _get_org_plan(_get())

    payload = json.loads(r.body)
    assert "granted_by" not in payload
    assert "alice@toorow.io" not in r.body.decode()


@pytest.mark.anyio
async def test_unlimited_plan_reports_none_not_zero():
    """full/internal resolve every limit to None -- "no cap", not "capped at 0".

    Serialising None as 0 here would make an unlimited org read as one that may
    create nothing at all: the most alarming possible lie, on the surface whose
    whole job is to say what you are allowed.
    """
    from core.org_plan_api import _get_org_plan

    with patch(_AUTHN, AsyncMock(return_value=(True, "owner@example.com"))), patch(
        "core.db.get_connection", return_value=_conn_ctx()
    ), patch(_ACCESS, return_value=True), patch(_COUNT, return_value=41), patch(
        _PLAN,
        return_value={
            "org_id": "org_x",
            "plan": "full",
            "entitlements": {},
            "granted_by": None,
            "granted_at": None,
        },
    ), patch(_LIMITS, return_value={"max_datastreams": None, "max_backfill_days": None}):
        r = await _get_org_plan(_get())

    payload = json.loads(r.body)
    assert payload["limits"]["max_datastreams"] is None
    assert payload["limits"]["max_backfill_days"] is None
    # Usage is still real -- an unlimited org still gets to see what it uses.
    assert payload["usage"]["active_datastreams"] == 41


# ---------------------------------------------------------------------------
# The counter and the refusal must count the SAME thing
# ---------------------------------------------------------------------------


def test_public_counter_delegates_to_the_private_one_the_refusal_uses():
    """`count_active_datastreams` must not re-derive the count.

    If the read surface ever grows its own query, the day the enforcement query
    changes the console starts displaying 2/3 while the create is already
    refused. Pinning the delegation is what makes that impossible.
    """
    from core import trial_enforcement as te

    conn = MagicMock()
    with patch.object(te, "_count_active_datastreams", return_value=7) as private:
        assert te.count_active_datastreams("org_x", conn) == 7
    private.assert_called_once_with("org_x", conn)


def test_public_counter_opens_its_own_connection_when_none_is_given():
    from core import trial_enforcement as te

    ctx = _conn_ctx()
    with patch.object(te, "_count_active_datastreams", return_value=1) as private, patch(
        "core.db.get_connection", return_value=ctx
    ):
        assert te.count_active_datastreams("org_x") == 1
    private.assert_called_once()
    ctx.__exit__.assert_called_once()


# ---------------------------------------------------------------------------
# Scoping and failure -- the two ways this surface must refuse
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_non_member_gets_404_and_no_plan_is_read():
    """7.4 pattern: existence is not disclosed, and nothing is read past the gate."""
    from core.org_plan_api import _get_org_plan

    with patch(_AUTHN, AsyncMock(return_value=(True, "stranger@example.com"))), patch(
        "core.db.get_connection", return_value=_conn_ctx()
    ), patch(_ACCESS, return_value=False), patch(_COUNT) as count, patch(_PLAN) as plan:
        r = await _get_org_plan(_get())

    assert r.status_code == 404
    assert json.loads(r.body)["code"] == "not_found"
    count.assert_not_called()
    plan.assert_not_called()


@pytest.mark.anyio
async def test_unauthenticated_gets_401():
    from core.org_plan_api import _get_org_plan

    with patch(_AUTHN, AsyncMock(return_value=(False, "anonymous"))):
        r = await _get_org_plan(_get())
    assert r.status_code == 401


@pytest.mark.anyio
async def test_read_failure_is_503_and_never_a_reassuring_empty_state():
    """Fail CLOSED, unlike the enforcement guard which fails open.

    A guard that fails open lets a legitimate create through -- harmless. A
    counter that fails open renders "0 of 3 used" over a dead read, which is the
    counter lying at exactly the moment it matters.
    """
    from core.org_plan_api import _get_org_plan

    with patch(_AUTHN, AsyncMock(return_value=(True, "owner@example.com"))), patch(
        "core.db.get_connection", side_effect=RuntimeError("pg is down")
    ):
        r = await _get_org_plan(_get())

    assert r.status_code == 503
    body = r.body.decode()
    assert json.loads(body)["code"] == "unavailable"
    assert "0" not in json.loads(body).get("message", "")


@pytest.mark.anyio
async def test_missing_org_id_is_422():
    from core.org_plan_api import _get_org_plan

    with patch(_AUTHN, AsyncMock(return_value=(True, "owner@example.com"))):
        r = await _get_org_plan(_get(""))
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# The route is actually mounted -- an unreachable handler proves nothing
# ---------------------------------------------------------------------------


def test_the_read_route_is_declared_and_is_a_GET():
    from core.org_plan_api import ORG_PLAN_ROUTES

    paths = {r.path: r.methods for r in ORG_PLAN_ROUTES}
    assert "/api/organizations/{org_id}/plan" in paths
    assert "GET" in paths["/api/organizations/{org_id}/plan"]
    # The super-admin write is untouched by this addition.
    assert "POST" in paths["/api/admin/org-plan"]
