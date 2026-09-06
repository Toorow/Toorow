"""`_authorize_internal` -- the gate in front of the eight /internal endpoints.

AI-127. Story 56.5 was RIGHT to break the conjunction (shared secret AND user
Bearer): Cloud Scheduler and Cloud Tasks carry no Bearer, so the conjunction
would have answered 401 to every scheduled execution after the 56.8 cutover.
What it left behind is what these tests are about -- the human branch fell
through to `_check_auth` alone, so any authenticated principal, from any
organization, could start platform-scale work: dispatch-nightly,
reconcile-queues, drain-outbox, poll-health, run-dq-monitors, dispatch-hourly,
and the two targets that execute a NAMED job -- execute-pull/{id} and
execute-activation/{id}. Eight, where the action item named four.

There was no test at all on this function. The two that touched the area patched
`_check_internal_auth`, which had no caller -- an inert patch reads exactly like
a guard and proves nothing. So each machine branch is pinned here alongside the
refusal, because the whole risk of this repair is breaking one of them: a gate
that also blocks Cloud Tasks turns a queue into a silent stall.
"""

from __future__ import annotations

import asyncio
import json
import os
from unittest.mock import patch

from core import admin_api
from starlette.requests import Request

_SECRET = "s3cret-internal"
_PATH = "/internal/scheduler/dispatch-nightly"


def _request(headers: list[tuple[bytes, bytes]] | None = None) -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": _PATH,
            "path_params": {},
            "headers": headers or [],
            "query_string": b"",
        }
    )


def _authorize(request: Request) -> object | None:
    return asyncio.run(admin_api._authorize_internal(request))


def _body(response) -> dict:
    return json.loads(bytes(response.body).decode())


async def _auth_as(identity: str):
    async def _checker(_request):
        return True, identity

    return _checker


def _as_human(identity: str, *, allow_list: str = ""):
    """A caller holding a VALID Bearer token, and nothing else.

    `_enforce_platform_admin` also consults the OAuth-verified email, so that
    seam is stubbed too -- otherwise the refusal could come from an unrelated
    failure to read it, and the test would pass for the wrong reason.
    """
    return (
        patch.dict(
            os.environ,
            {"TOOROW_SUPER_ADMINS": allow_list, "INTERNAL_ENDPOINTS_REQUIRE_HEADER": ""},
        ),
        patch.object(admin_api, "_check_auth", asyncio.run(_auth_as(identity))),
        patch.object(
            admin_api,
            "_check_invitation_identity",
            asyncio.run(_auth_as("")),
        ),
    )


# ---------------------------------------------------------------------------
# The defect: authenticated is not authorized.
# ---------------------------------------------------------------------------


def test_valid_bearer_without_platform_role_is_refused():
    """THE DEFECT AI-127 NAMES. Before the repair this returned None (allowed)."""
    env, auth, invite = _as_human("someone@example.com")
    with env, auth, invite:
        refusal = _authorize(_request())

    assert refusal is not None, "a valid Bearer alone must not open platform work"
    assert refusal.status_code == 404
    assert _body(refusal)["code"] == "not_found"


def test_refusal_is_404_not_403_so_the_surface_is_not_confirmed():
    """Same posture as `_enforce_platform_admin` everywhere else in the module.

    403 would tell a caller who is not allow-listed that the endpoint exists.
    """
    env, auth, invite = _as_human("outsider@example.com")
    with env, auth, invite:
        refusal = _authorize(_request())

    assert refusal.status_code != 403
    assert refusal.status_code == 404


def test_platform_admin_on_the_allow_list_is_authorized():
    """The human path still works -- for a human who is actually platform-level."""
    env, auth, invite = _as_human("ops@example.com", allow_list="ops@example.com")
    with env, auth, invite:
        assert _authorize(_request()) is None


def test_empty_allow_list_denies_every_human():
    """Deny-by-default is inherited from TOOROW_SUPER_ADMINS, and it is correct here.

    The other two callers of these endpoints are machines. A human who needs one
    by hand joins the allow-list or presents the internal secret.
    """
    env, auth, invite = _as_human("ops@example.com", allow_list="")
    with env, auth, invite:
        assert _authorize(_request()) is not None


def test_unauthenticated_caller_is_401_not_404():
    """The role check must not swallow the distinction between WHO and WHETHER."""

    async def _no_auth(_request):
        return False, ""

    with (
        patch.dict(os.environ, {"INTERNAL_ENDPOINTS_REQUIRE_HEADER": ""}),
        patch.object(admin_api, "_check_auth", _no_auth),
    ):
        refusal = _authorize(_request())

    assert refusal.status_code == 401


# ---------------------------------------------------------------------------
# The two machine branches -- unchanged, and that IS the requirement.
# ---------------------------------------------------------------------------


def test_shared_secret_authorizes_without_any_bearer():
    """Cloud Tasks and Cloud Scheduler reach the endpoints by the secret alone.

    Pinned because it is what the repair must not break: no Bearer, no
    allow-list entry, and it must still be None.
    """
    with patch.dict(
        os.environ,
        {"TOOROW_SUPER_ADMINS": "", "INTERNAL_ENDPOINTS_REQUIRE_HEADER": _SECRET},
    ):
        allowed = _authorize(_request([(b"x-internal-auth", _SECRET.encode())]))

    assert allowed is None


def test_oidc_caller_authorizes_before_anything_else_is_consulted():
    """A Pub/Sub push subscription has ONLY this: it cannot send a custom header."""
    with (
        patch.dict(os.environ, {"TOOROW_SUPER_ADMINS": ""}),
        patch.object(admin_api, "_google_oidc_caller_is_ours", lambda _r: True),
    ):
        assert _authorize(_request()) is None


def test_wrong_secret_falls_back_to_the_human_branch_and_is_refused_there():
    """The mismatch was AUDITED and then waved through. It is now audited AND refused."""
    env_patch = patch.dict(
        os.environ,
        {"TOOROW_SUPER_ADMINS": "", "INTERNAL_ENDPOINTS_REQUIRE_HEADER": _SECRET},
    )
    _, auth, invite = _as_human("someone@example.com")
    with env_patch, auth, invite:
        refusal = _authorize(_request([(b"x-internal-auth", b"wrong")]))

    assert refusal is not None
    assert refusal.status_code == 404


def test_missing_secret_under_push_backend_is_503_not_401():
    """Story 56.5's fail-closed case, pinned so the role check does not shadow it.

    503 makes Cloud Tasks come back once an operator sets the variable; 401
    would look like a broken queue and the work would die.
    """
    with patch.dict(
        os.environ,
        {"INTERNAL_ENDPOINTS_REQUIRE_HEADER": "", "QUEUE_BACKEND": "cloud_tasks"},
    ):
        refusal = _authorize(_request())

    assert refusal.status_code == 503
    assert _body(refusal)["code"] == "internal_auth_unconfigured"


# ---------------------------------------------------------------------------
# The gate is reached from every endpoint it is supposed to guard.
# ---------------------------------------------------------------------------


def test_every_internal_endpoint_goes_through_the_gate():
    """Eight call sites, counted from the source rather than from a hand-kept list.

    AI-127 named four `scheduler` endpoints; reading the code found EIGHT, and
    the extra ones include the two that execute a named job. The first draft of
    this test asserted nine -- it had counted the `def` line -- and the assertion
    is what caught it. That is the whole reason the count is derived from the
    source and not from a list kept by hand here.

    The count is read across every `core` module, not from `admin_api` alone:
    AD-43 moved the `/internal` doors into `core/internal_api.py`, and a guard
    that kept reading one path would have found zero call sites and reported it
    as "no defect" -- the failure mode this file was written against.
    """
    from pathlib import Path

    core = Path(admin_api.__file__).parent
    sources = {
        path.name: path.read_text(encoding="utf-8", errors="ignore")
        for path in sorted(core.glob("*.py"))
    }
    call_sites = sum(
        text.count("await _authorize_internal(request)") for text in sources.values()
    )

    assert call_sites >= 8, f"expected the gate on 8+ endpoints, found {call_sites}"
    source = "\n".join(sources.values())
    # The dead Phase-A scaffold is gone. Its DEFINITION and its CALL are what
    # must stay gone -- the prose above it still names it, deliberately, because
    # a docstring that once claimed this endpoint was "also gated by
    # _check_internal_auth" is exactly the kind of false assurance worth
    # recording. Asserting on the bare name would forbid saying so.
    assert "def _check_internal_auth" not in source
    assert "_check_internal_auth(" not in source
