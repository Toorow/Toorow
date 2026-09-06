"""The platform-clock REST seam: the routes exist, and each refusal has a shape.

Offline. `core.platform_clocks` is replaced by a fake, the connection is a
MagicMock, and no GCP call is attempted. The handlers are driven directly with a
constructed `Request` rather than through `TestClient(build_asgi_app())`: the
routes are not spliced into `admin_api` yet (the splice diff is in the delivery
report), and an ASGI seam test costs 30-45s per test for no extra evidence about
the handler itself.

What is pinned:

  * the five routes are exported as `Route` objects with the right methods, and
    no read path can reach a write;
  * an unauthenticated call is 401 and touches nothing;
  * a caller outside the platform allow-list gets the NONDISCLOSING 404 -- and a
    failure of the allow-list check itself refuses too (fail-closed);
  * a read renders drift and never applies or runs it;
  * the three writes refuse without a bounded `Idempotency-Key` AND without the
    clock name echoed in `confirm`.

File paths are anchored on `Path(__file__).resolve().parents[3]` (the repository
root), never relative to the working directory -- AI-136.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from core.platform_clocks_api import (
    PLATFORM_CLOCK_ROUTES,
    _apply_clock,
    _get_clock,
    _list_clocks,
    _patch_clock,
    _run_clock,
)
from starlette.requests import Request
from starlette.responses import JSONResponse

REPO_ROOT = Path(__file__).resolve().parents[3]
API_MODULE_PATH = REPO_ROOT / "server" / "core" / "platform_clocks_api.py"

COLLECTION = "/api/platform/clocks"
OBJECT = "/api/platform/clocks/{clock_name}"
APPLY = "/api/platform/clocks/{clock_name}/apply"
RUN = "/api/platform/clocks/{clock_name}/run"
# The nightly STEP ledger (`Incomplete if` 2, third locus). OUTSIDE the
# `/clocks/` prefix on purpose: under it, `nightly-steps` is a valid clock NAME
# and `_checked_clock_name` would accept it, so the route would work only by
# declaration order -- and a route that survives on ordering is a route the next
# reordering breaks silently.
NIGHTLY_STEPS = "/api/platform/nightly-steps"

PLATFORM_ADMIN = "ops@example.com"
CLOCK = "dispatch-nightly"


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_environment(monkeypatch):
    for name in (
        "TOOROW_GCP_PROJECT",
        "TOOROW_GCP_REGION",
        "GOOGLE_CLOUD_PROJECT",
        "GOOGLE_CLOUD_REGION",
    ):
        monkeypatch.delenv(name, raising=False)


class _Conn:
    def __init__(self):
        self.commit = MagicMock()
        self.rollback = MagicMock()


@contextmanager
def _connection():
    yield _Conn()


def _seam(**overrides) -> SimpleNamespace:
    seam = SimpleNamespace(
        list_declared=MagicMock(
            return_value=[
                {"name": CLOCK, "schedule": "0 2 * * *"},
                {"name": "drain-outbox", "schedule": "*/5 * * * *"},
            ]
        ),
        observe=MagicMock(return_value=[{"name": CLOCK, "schedule": "0 3 * * *"}]),
        reconcile=MagicMock(
            return_value={CLOCK: "drifted", "drain-outbox": "missing_in_gcp"}
        ),
        apply=MagicMock(return_value={"action": "updated"}),
        run_now=MagicMock(return_value={"dispatched": True}),
        set_declared=MagicMock(return_value={"schedule": "0 4 * * *"}),
    )
    for key, value in overrides.items():
        setattr(seam, key, value)
    return seam


def _request(method: str, path: str, *, path_params=None, headers=None, body=b""):
    raw_headers = [
        (key.lower().encode(), value.encode()) for key, value in (headers or {}).items()
    ]
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "server": ("testserver", 80),
        "client": ("test", 1234),
        "root_path": "",
        "path": path,
        "raw_path": path.encode(),
        "query_string": b"",
        "headers": raw_headers,
        "path_params": path_params or {},
    }

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(scope, receive)


def _write_request(method: str, path: str, *, clock=CLOCK, key="idem-1", confirm=CLOCK):
    headers = {"content-type": "application/json"}
    if key is not None:
        headers["idempotency-key"] = key
    payload: dict = {"schedule": "0 4 * * *"}
    if confirm is not None:
        payload["confirm"] = confirm
    return _request(
        method,
        path,
        path_params={"clock_name": clock},
        headers=headers,
        body=json.dumps(payload).encode(),
    )


@contextmanager
def _authorized(seam, *, authenticated=True, platform_admin=True, admin_raises=False):
    """Patch authentication, the platform allow-list, the seam and the connection."""
    if admin_raises:
        enforce = AsyncMock(side_effect=RuntimeError("allow-list unreadable"))
    elif platform_admin:
        enforce = AsyncMock(return_value=None)
    else:
        enforce = AsyncMock(
            return_value=JSONResponse(
                {"code": "not_found", "message": "Not found"}, status_code=404
            )
        )
    from core import platform_clocks_read_model

    def execute_clock_directly(conn, **kwargs):
        return kwargs["mutation"](conn)

    with (
        patch(
            "core.api_auth.authenticate_api_request",
            new=AsyncMock(return_value=(authenticated, PLATFORM_ADMIN)),
        ),
        patch("core.admin_api._enforce_platform_admin", new=enforce),
        patch.object(platform_clocks_read_model, "_seam", return_value=seam),
        patch("core.platform_clocks_api._clock_operation", new=execute_clock_directly),
        patch("core.db.get_connection", side_effect=_connection),
    ):
        yield enforce


def _call(handler, request):
    return asyncio.run(handler(request))


def _payload(response) -> dict:
    return json.loads(response.body.decode("utf-8"))


def _route(path: str, method: str):
    return next(
        (r for r in PLATFORM_CLOCK_ROUTES if r.path == path and method in r.methods),
        None,
    )


# ---------------------------------------------------------------------------
# The routes exist, and reads cannot write.
# ---------------------------------------------------------------------------


def test_the_five_routes_are_exported_with_their_methods():
    assert all(hasattr(route, "path") for route in PLATFORM_CLOCK_ROUTES)
    for path, method in (
        (COLLECTION, "GET"),
        (OBJECT, "GET"),
        (OBJECT, "PATCH"),
        (APPLY, "POST"),
        (RUN, "POST"),
        (NIGHTLY_STEPS, "GET"),
    ):
        assert _route(path, method) is not None, f"{method} {path} is unreachable"

    # A read must never be reachable by a method that writes.
    assert _route(COLLECTION, "POST") is None
    assert _route(COLLECTION, "PATCH") is None
    assert _route(COLLECTION, "DELETE") is None
    assert _route(OBJECT, "DELETE") is None
    # The step ledger is append-only and written only by the scheduler. Nothing
    # on this surface may close, re-open or delete one of its rows.
    for method in ("POST", "PATCH", "PUT", "DELETE"):
        assert _route(NIGHTLY_STEPS, method) is None

    # It is NOT under the clocks prefix, where a clock named `nightly-steps`
    # would shadow it -- or be shadowed by it.
    assert not NIGHTLY_STEPS.startswith(f"{COLLECTION}/")

    # The action paths come first, so `/{clock_name}` cannot shadow them.
    order = [route.path for route in PLATFORM_CLOCK_ROUTES]
    assert order.index(APPLY) < order.index(OBJECT)
    assert order.index(RUN) < order.index(OBJECT)


def test_no_read_handler_can_reach_a_write_seam():
    """Source-level guard, anchored on the repository root (AI-136)."""
    source = API_MODULE_PATH.read_text(encoding="utf-8")
    read_section = source.split("async def _list_clocks", 1)[1].split(
        "async def _patch_clock", 1
    )[0]
    for forbidden in ("apply_declared", "run_clock_now", "set_declared_cadence"):
        assert forbidden not in read_section, (
            f"a read handler references {forbidden}: drift would be repaired by "
            "the act of looking at it"
        )

    # The step-ledger handler is a read too, and it is the one that would be
    # most tempting to make "helpful": closing an open row is exactly the
    # silence the ledger exists to end.
    ledger_section = source.split("async def _nightly_steps", 1)[1].split(
        "PLATFORM_CLOCK_ROUTES", 1
    )[0]
    for forbidden in ("apply_declared", "run_clock_now", "set_declared_cadence", "UPDATE"):
        assert forbidden not in ledger_section, (
            f"the nightly-steps handler references {forbidden}: a read that "
            "closes a row erases the record it was opened to show"
        )


# ---------------------------------------------------------------------------
# Authorization.
# ---------------------------------------------------------------------------


def test_an_unauthenticated_call_is_401_and_touches_nothing():
    seam = _seam()
    with _authorized(seam, authenticated=False) as enforce:
        response = _call(_list_clocks, _request("GET", COLLECTION))
    assert response.status_code == 401
    assert _payload(response)["code"] == "unauthorized"
    enforce.assert_not_awaited()
    seam.list_declared.assert_not_called()


def test_a_caller_outside_the_platform_allow_list_gets_a_nondisclosing_404():
    seam = _seam()
    with _authorized(seam, platform_admin=False):
        response = _call(_list_clocks, _request("GET", COLLECTION))
    assert response.status_code == 404
    assert _payload(response)["code"] == "not_found"
    seam.list_declared.assert_not_called()


def test_a_failing_allow_list_check_refuses_rather_than_proceeds():
    """Fail-closed: an authorization seam that could not run has granted nothing."""
    seam = _seam()
    with _authorized(seam, admin_raises=True):
        response = _call(
            _run_clock, _write_request("POST", RUN)
        )
    assert response.status_code == 404
    seam.run_now.assert_not_called()


@pytest.mark.parametrize(
    ("handler", "method", "path"),
    [
        (_get_clock, "GET", OBJECT),
        (_patch_clock, "PATCH", OBJECT),
        (_apply_clock, "POST", APPLY),
        (_run_clock, "POST", RUN),
    ],
)
def test_every_object_route_enforces_the_platform_role(handler, method, path):
    seam = _seam()
    with _authorized(seam, platform_admin=False):
        request = (
            _write_request(method, path)
            if method in ("PATCH", "POST")
            else _request(method, path, path_params={"clock_name": CLOCK})
        )
        response = _call(handler, request)
    assert response.status_code == 404
    seam.apply.assert_not_called()
    seam.run_now.assert_not_called()
    seam.set_declared.assert_not_called()


# ---------------------------------------------------------------------------
# Reads render drift; they never repair it.
# ---------------------------------------------------------------------------


def test_the_collection_read_renders_drift_and_repairs_nothing():
    seam = _seam()
    with _authorized(seam):
        response = _call(_list_clocks, _request("GET", COLLECTION))

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    body = _payload(response)
    assert {c["clock_name"]: c["verdict"] for c in body["clocks"]} == {
        CLOCK: "drifted",
        "drain-outbox": "missing_in_gcp",
    }
    assert body["out_of_sync"] == [CLOCK, "drain-outbox"]
    assert body["drift_is_reported_not_repaired"] is True
    seam.apply.assert_not_called()
    seam.run_now.assert_not_called()


def test_an_unobservable_gcp_is_reported_unknown_not_in_sync():
    seam = _seam(observe=MagicMock(side_effect=RuntimeError("no credential")))
    with _authorized(seam):
        response = _call(_list_clocks, _request("GET", COLLECTION))

    body = _payload(response)
    assert body["observation"]["reachable"] is False
    assert [c["verdict"] for c in body["clocks"]] == ["unknown", "unknown"]
    assert all(c["in_sync"] is False for c in body["clocks"])


def test_an_unknown_clock_is_404():
    seam = _seam()
    with _authorized(seam):
        response = _call(
            _get_clock,
            _request("GET", OBJECT, path_params={"clock_name": "poll-health"}),
        )
    assert response.status_code == 404


def test_a_malformed_clock_name_never_reaches_the_seam():
    seam = _seam()
    with _authorized(seam):
        response = _call(
            _get_clock,
            _request("GET", OBJECT, path_params={"clock_name": "../../etc/passwd"}),
        )
    assert response.status_code == 404
    seam.list_declared.assert_not_called()


def test_an_unreachable_seam_is_503_not_an_empty_clock_list():
    """An empty list reads exactly like 'nothing is scheduled'. Never guess it."""
    from core.platform_clocks_read_model import PlatformClockSeamError

    seam = _seam(
        list_declared=MagicMock(side_effect=PlatformClockSeamError("no seam"))
    )
    with _authorized(seam):
        response = _call(_list_clocks, _request("GET", COLLECTION))
    assert response.status_code == 503
    assert _payload(response)["code"] == "seam_unavailable"


# ---------------------------------------------------------------------------
# The three writes require a real confirmation.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("handler", "method", "path"),
    [
        (_patch_clock, "PATCH", OBJECT),
        (_apply_clock, "POST", APPLY),
        (_run_clock, "POST", RUN),
    ],
)
def test_a_write_without_an_idempotency_key_is_refused(handler, method, path):
    seam = _seam()
    with _authorized(seam):
        response = _call(handler, _write_request(method, path, key=None))
    assert response.status_code == 400
    assert _payload(response)["code"] == "invalid_idempotency_key"
    seam.apply.assert_not_called()
    seam.run_now.assert_not_called()
    seam.set_declared.assert_not_called()


@pytest.mark.parametrize(
    ("handler", "method", "path"),
    [
        (_patch_clock, "PATCH", OBJECT),
        (_apply_clock, "POST", APPLY),
        (_run_clock, "POST", RUN),
    ],
)
def test_a_write_without_the_echoed_clock_name_is_refused(handler, method, path):
    seam = _seam()
    with _authorized(seam):
        response = _call(handler, _write_request(method, path, confirm=None))
    assert response.status_code == 400
    assert _payload(response)["code"] == "confirmation_required"

    with _authorized(seam):
        response = _call(handler, _write_request(method, path, confirm="drain-outbox"))
    assert response.status_code == 400
    seam.apply.assert_not_called()
    seam.run_now.assert_not_called()
    seam.set_declared.assert_not_called()


def test_a_confirmed_run_dispatches_exactly_once_and_answers_202():
    seam = _seam()
    with _authorized(seam):
        response = _call(_run_clock, _write_request("POST", RUN))
    assert response.status_code == 202
    body = _payload(response)
    assert body["clock_name"] == CLOCK
    seam.run_now.assert_called_once()
    assert seam.run_now.call_args.kwargs["clock_name"] == CLOCK
    assert seam.run_now.call_args.kwargs["actor"] == PLATFORM_ADMIN
    # Running does not repoint the clock.
    seam.set_declared.assert_not_called()
    seam.apply.assert_not_called()


def test_a_confirmed_apply_returns_the_state_it_actually_produced():
    seam = _seam()
    with _authorized(seam):
        response = _call(_apply_clock, _write_request("POST", APPLY))
    assert response.status_code == 200
    body = _payload(response)
    seam.apply.assert_called_once()
    assert body["state"]["clocks"][0]["clock_name"] == CLOCK
    assert seam.observe.call_count >= 1


def test_a_confirmed_cadence_edit_writes_the_declaration_only():
    seam = _seam()
    with _authorized(seam):
        response = _call(_patch_clock, _write_request("PATCH", OBJECT))
    assert response.status_code == 200
    body = _payload(response)
    seam.set_declared.assert_called_once()
    assert seam.set_declared.call_args.kwargs["schedule"] == "0 4 * * *"
    assert body["applied_to_gcp"] is False
    seam.apply.assert_not_called()


def test_clock_commands_bind_the_exact_key_to_the_durable_operation_seam():
    from core.platform_clocks_api import _clock_operation

    conn = MagicMock()
    persisted = SimpleNamespace(result={"clock_name": CLOCK, "outcome": {"accepted": True}})
    with patch("core.operations.execute_operation", return_value=persisted) as execute:
        result = _clock_operation(
            conn,
            command="platform_clock.run_now",
            clock_name=CLOCK,
            actor=PLATFORM_ADMIN,
            key="idem-stable",
            payload={},
            mutation=MagicMock(),
        )

    assert result == persisted.result
    assert execute.call_args.args[1].idempotency_key == "idem-stable"
