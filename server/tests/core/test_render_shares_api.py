"""Story 50.7 -- the public HTTP surface: headers, cookie, ordering, envelope.

WHY A STANDALONE STARLETTE APP AND NOT `build_asgi_app()`. These routes are mounted
into `admin_api.py` by the orchestrator (that file is contended and this story does
not write it), so composing the whole application here would test the mount rather
than the handlers. The mount itself is asserted in
`test_render_share_retirement.py` against the real route tables, and the ASGI-level
`/share` dispatch is asserted against `core.routing`. What is proved here is what
the handlers actually return.

THE HEADER SET IS ASSERTED ON ERRORS TOO. A header set that differs by outcome is
itself a signal: a recipient who can tell "revoked" from "unknown" by counting
response headers has the enumeration oracle the common envelope exists to close.
"""

from __future__ import annotations

import json
import os
import pathlib

import pytest

# The pepper and the origin, for THIS module's tests only -- a module-level
# `os.environ.setdefault` wrote them for the whole session (AI-377). One
# declaration for the four modules that need them, so they cannot drift apart.
from tests.support.render_share_env import render_share_env  # noqa: F401

_DSN = os.environ.get("TEST_POSTGRES_DSN", "")


@pytest.fixture()
def client():
    from core.render_shares_api import render_share_routes
    from starlette.applications import Starlette
    from starlette.testclient import TestClient

    return TestClient(
        Starlette(routes=render_share_routes), raise_server_exceptions=False
    )


_REQUIRED_HEADERS = {
    "cache-control": "no-store, max-age=0",
    "pragma": "no-cache",
    "referrer-policy": "no-referrer",
    "x-content-type-options": "nosniff",
    "x-frame-options": "DENY",
}


def _strip_comments(document: str) -> str:
    """Serve-able document with HTML and JS line comments removed."""
    import re as _re

    without_html = _re.sub(r"<!--.*?-->", " ", document, flags=_re.S)
    lines = [
        _re.sub(r"//.*$", "", line) if "//" in line and "http" not in line else line
        for line in without_html.splitlines()
    ]
    return chr(10).join(lines)


def _assert_security_headers(response) -> None:
    for name, value in _REQUIRED_HEADERS.items():
        assert response.headers.get(name) == value, f"{name} missing or wrong"
    csp = response.headers["content-security-policy"]
    assert "default-src 'none'" in csp
    assert "frame-ancestors 'none'" in csp
    assert "base-uri 'none'" in csp
    assert "form-action 'none'" in csp
    assert "'unsafe-inline'" not in csp, "a nonce CSP that allows unsafe-inline is not a CSP"
    for scheme in ("http://", "https://"):
        assert scheme not in csp, "no third-party origin may appear in any directive"


# ---------------------------------------------------------------------------
# AC4 -- the bootstrap shell.
# ---------------------------------------------------------------------------


def test_the_bootstrap_shell_carries_the_full_header_set(client):
    response = client.get("/share")
    assert response.status_code == 200
    _assert_security_headers(response)


def test_the_bootstrap_nonce_is_per_response(client):
    """A fixed nonce is a permanent allow-list entry for anyone who reads the page
    once."""
    first = client.get("/share").headers["content-security-policy"]
    second = client.get("/share").headers["content-security-policy"]
    assert first != second


def test_the_bootstrap_replaces_the_history_entry_before_any_network_access(client):
    """AC4's ordering property, read off the served document.

    `history.replaceState` must precede the first `fetch` -- and there must be no
    earlier network access of ANY kind for the fragment to leak into: no image, no
    font, no preconnect, no prefetch, no beacon."""
    body = client.get("/share").text
    replace_at = body.index("history.replaceState")
    fetch_at = body.index("fetch(")
    assert replace_at < fetch_at, "the fragment is still in the URL at the first request"

    # The shell EXPLAINS in a comment that it preconnects to nothing, so the scan
    # below has to read code rather than prose -- otherwise the explanation itself
    # fails the test that the explanation is true.
    code = _strip_comments(body)
    for forbidden in (
        "<img", "<link", "preconnect", "prefetch", "dns-prefetch", "sendBeacon",
        "XMLHttpRequest", "new Image", "@font-face", "importScripts",
    ):
        assert forbidden not in code, f"{forbidden!r} can fire before replaceState"


def test_the_bootstrap_reads_the_bearer_only_from_the_fragment(client):
    body = client.get("/share").text
    assert "location.hash" in body
    assert "location.search" not in body, "a query string reaches the server and the access log"
    assert "#render=" in body


def test_the_bootstrap_posts_the_bearer_in_the_body(client):
    body = client.get("/share").text
    assert "'/api/render-shares/exchange'" in body
    assert "method: 'POST'" in body
    assert "credentials: 'same-origin'" in body
    assert "JSON.stringify({ bearer: bearer })" in body


def test_the_bootstrap_states_are_english(client):
    body = client.get("/share").text
    assert 'lang="en"' in body
    assert "Ask the sender for a new one." in body


# ---------------------------------------------------------------------------
# AC5 / AC13 -- one envelope, one header set, on every refusal.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        None,                       # malformed body
        {"bearer": 12345},          # wrong type
        {},                         # absent
        {"bearer": "too-short"},    # out of bounds
    ],
)
def test_every_malformed_exchange_answers_the_same_envelope(client, payload):
    if payload is None:
        response = client.post("/api/render-shares/exchange", content=b"not json")
    else:
        response = client.post("/api/render-shares/exchange", json=payload)
    assert response.status_code == 404
    assert response.json() == {
        "code": "share_unavailable",
        "message": "This shared result is not available.",
    }
    _assert_security_headers(response)


def test_a_refusal_and_a_malformed_body_are_byte_identical(client):
    """Different causes, indistinguishable responses -- body, status and header set."""
    malformed = client.post("/api/render-shares/exchange", content=b"{")
    wrong_type = client.post("/api/render-shares/exchange", json={"bearer": None})
    assert malformed.status_code == wrong_type.status_code
    assert malformed.content == wrong_type.content
    ignorable = {"date", "server", "content-length"}
    assert {k.lower() for k in malformed.headers} - ignorable == {
        k.lower() for k in wrong_type.headers
    } - ignorable


@pytest.mark.parametrize(
    "method,path",
    [
        ("get", "/api/render-shares/session/render"),
        ("get", "/api/render-shares/session/rows"),
        ("post", "/api/render-shares/session/export"),
        ("post", "/api/render-shares/session/feedback"),
    ],
)
def test_a_session_route_without_a_cookie_is_refused_with_the_common_envelope(
    client, method, path
):
    response = getattr(client, method)(path)
    assert response.status_code in (400, 401)
    body = response.json()
    assert body["code"] == "share_unavailable"
    _assert_security_headers(response)


def test_the_view_page_without_a_session_carries_no_data_and_explains_itself(client):
    """AC13, and the reason the page is a PUBLIC shell.

    The session cookie is path-scoped to `/api/render-shares`, so the browser never
    sends it to `/share/view`. A server-rendered page could not read it -- measured:
    an earlier version answered 401 to a browser that had just exchanged
    successfully -- and widening the cookie to `/` to fix that would hand the session
    to every path on the origin.

    So the shell is public, always 200, and carries NO frozen data. Access is decided
    in exactly one place (the session API), and the shell renders that decision as
    English copy. Its existence therefore discloses nothing: an unknown caller and a
    revoked caller receive the identical document."""
    response = client.get("/share/view")
    assert response.status_code == 200
    body = response.text
    assert 'lang="en"' in body
    assert "This shared result is not available" in body
    assert "Ask the person who shared it to send a new link." in body
    assert "opens once" in body, "single-use is explained rather than left mysterious"
    # No frozen data, and nothing that names a tenant.
    assert '"render":' not in body
    _assert_security_headers(response)


# ---------------------------------------------------------------------------
# Route shape.
# ---------------------------------------------------------------------------


def test_literal_routes_precede_parameterized_ones():
    """Starlette matches in declaration order. `/share` must not be captured by a
    later pattern, and `/session/render` must not be read as an id."""
    from core.render_shares_api import render_share_routes

    paths = [route.path for route in render_share_routes]
    assert paths.index("/share") < paths.index("/share/view")
    assert paths == sorted(paths, key=lambda p: (p.count("{"), paths.index(p)))


def test_no_public_route_accepts_an_object_identifier_in_its_path():
    """AC6.1: every identity on the public surface is derived from `share_id`."""
    from core.render_shares_api import render_share_routes

    for route in render_share_routes:
        assert "{" not in route.path, f"{route.path} takes a caller-supplied identifier"


def test_the_console_routes_are_not_on_the_public_surface():
    from core.render_shares_api import render_share_routes

    for route in render_share_routes:
        assert not route.path.startswith("/api/projects/"), (
            f"{route.path} is a Project-scoped route mounted on the public surface"
        )


# ---------------------------------------------------------------------------
# THE CONSOLE ROUTES, DRIVEN AS ROUTES.
#
# WHAT STOOD HERE UNTIL 2026-08-30, and why it was worse than no test at all.
# Three tests read the handlers with `inspect.getsource` and asserted the
# substring `_authorize(request, "edit")`. That literal WAS the defect:
# `_authorize` delegated to a helper that takes a ROLE, `"edit"` is not a role
# name, and all four console routes answered 500 -- the listing included,
# because it asks the same question to fill `may_confirm`. The assertions stayed
# green the whole time, because a substring check can only ever prove that a
# string is present in a file. These are HTTP routes, so they are exercised over
# HTTP.
#
# WHAT THIS SEAM PROVES AND WHAT IT LEAVES TO POSTGRES. Here: that each route
# dispatches, that its floor is a word the strict access seam accepts, and that
# a refused caller meets the non-disclosing 404 rather than a stack trace. The
# ceremony itself -- pending, second holder, delivery URL, revoke, posture --
# needs real rows and real grants, and is walked in
# `tests/integration/test_project_external_sharing_pg.py`.
# ---------------------------------------------------------------------------

#: A caller who authenticates and holds NOTHING. Canonical-shaped so the
#: identity bridge short-circuits instead of querying the fake connection.
_CONSOLE_CALLER = "person_01JCONSOLECALLERTESTONLY"

_CONSOLE_CALLS = {
    "list": ("GET", "/api/projects/proj_EXAMPLE/renders/rnd_EXAMPLE/shares", None),
    "create": (
        "POST",
        "/api/projects/proj_EXAMPLE/renders/rnd_EXAMPLE/shares",
        {"expires_at": "2030-01-01T00:00:00Z"},
    ),
    "confirm": (
        "POST",
        "/api/projects/proj_EXAMPLE/renders/shares/rsh_EXAMPLE/confirmation",
        {},
    ),
    "revoke": ("DELETE", "/api/projects/proj_EXAMPLE/renders/shares/rsh_EXAMPLE", None),
}


@pytest.fixture()
def console_client(monkeypatch):
    """The four console routes on their own Starlette app, for a caller with nothing.

    `TOOROW_AUTH_MODE=oauth` on purpose: the auth-disabled branch of
    `_strict_project_capability_allowed` grants every capability to `anonymous`,
    so a run under it would never reach the strict resolver -- which is the thing
    that raised on an unknown floor word.
    """
    from unittest.mock import AsyncMock, MagicMock

    from core.render_shares_console_api import render_share_console_routes
    from starlette.applications import Starlette
    from starlette.testclient import TestClient

    monkeypatch.setenv("TOOROW_AUTH_MODE", "oauth")

    cursor = MagicMock()
    cursor.__enter__ = MagicMock(return_value=cursor)
    cursor.__exit__ = MagicMock(return_value=False)
    # `SELECT p.org_id, o.status, m.role, m.status` with no membership row: an
    # authenticated caller who is not a member of the project's organization.
    cursor.fetchone.return_value = ("org_console", "active", None, None)
    connection = MagicMock()
    connection.cursor.return_value = cursor
    holder = MagicMock()
    holder.__enter__ = MagicMock(return_value=connection)
    holder.__exit__ = MagicMock(return_value=False)

    monkeypatch.setattr(
        "core.admin_api._check_auth", AsyncMock(return_value=(True, _CONSOLE_CALLER))
    )
    monkeypatch.setattr("core.db.get_connection", MagicMock(return_value=holder))

    # `raise_server_exceptions=False` so a 500 arrives AS a 500 instead of
    # re-raising: the finding under repair was a 500, and a client that cannot
    # observe one cannot refuse one.
    return TestClient(
        Starlette(routes=render_share_console_routes), raise_server_exceptions=False
    )


@pytest.mark.parametrize("route", sorted(_CONSOLE_CALLS))
def test_no_console_route_answers_500_to_an_authenticated_caller(console_client, route):
    """The reviewer's parcours: four routes, four real status codes.

    A caller with no grant is refused 404 -- non-disclosing, the shape every
    project surface gives (`core/admin_api.py#_project_not_found_response`).
    What is refused here is the 500: `ValueError: unknown project role` reached
    the ASGI error handler on all four, and that is not an authorization answer
    at all.
    """
    method, path, body = _CONSOLE_CALLS[route]
    response = console_client.request(method, path, json=body)
    assert response.status_code == 404, (route, response.status_code, response.text)
    assert response.json()["code"] == "not_found"


def test_every_console_floor_is_a_word_the_access_seam_accepts(monkeypatch):
    """The floors, read off the ROUTES, checked against the seam's own vocabulary.

    `view` to list, `edit` to request, `edit` to confirm, `manage` to revoke --
    `proactive-assertions.md` decision 2 and `project-settings.md` spell them in
    exactly these words, and `project_access._CAPABILITY_ORDER` is the table that
    has to accept them. The retired version of this test asserted the SPELLING in
    the source; this one asserts that the spelling RESOLVES.

    The confirmation is deliberately NOT an escalation to `manage`: decision 2
    asks for a second role holder, not a higher one, and turning the two-person
    rule into an approval hierarchy is a product decision nobody has taken.
    """
    from core import project_access
    from core import render_shares_console_api as console
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.testclient import TestClient

    seen: list[str] = []

    async def _record(request, minimum_capability):
        seen.append(minimum_capability)
        return JSONResponse({"code": "recorded"}, status_code=403)

    monkeypatch.setattr(console, "_authorize", _record)
    client = TestClient(
        Starlette(routes=console.render_share_console_routes),
        raise_server_exceptions=False,
    )
    floors = {}
    for name, (method, path, body) in _CONSOLE_CALLS.items():
        seen.clear()
        client.request(method, path, json=body)
        assert seen, f"{name} never asked for an authorization"
        # The FIRST question each route asks -- the floor it refuses on. The
        # listing asks a SECOND one (`edit`, to answer "could this reader be the
        # second holder"), which a refused caller never reaches; that one is
        # walked with real grants in `test_project_external_sharing_pg.py`.
        floors[name] = seen[0]

    assert floors["list"] == "view"
    assert floors["create"] == "edit"
    assert floors["confirm"] == "edit"
    assert floors["revoke"] == "manage"
    for name, capability in floors.items():
        assert capability in project_access._CAPABILITY_ORDER, (  # noqa: SLF001
            f"{name} asks for {capability!r}, which the strict access seam does "
            "not know -- that is the 500 this repair removed"
        )


def test_the_console_listing_never_returns_a_delivery_url():
    """AC3: the URL exists once, and since migration 323 that once is the
    CONFIRMATION. Neither the listing nor the request may carry it -- the request
    has no bearer to carry.

    Kept as a source read because it asserts an ABSENCE, which no single response
    can prove; the matching PRESENCE -- a real bearer, returned once, by the
    confirmation and by nothing else -- is asserted on live 201 bodies in
    `tests/integration/test_project_external_sharing_pg.py`.
    """
    import inspect

    from core import render_shares_console_api as console

    listing = inspect.getsource(console._list_render_shares)  # noqa: SLF001
    assert "delivery_url" not in listing
    creation = inspect.getsource(console._create_render_share)  # noqa: SLF001
    assert "created.delivery_url" not in creation


def test_the_refusal_of_a_forbidden_project_names_the_gesture():
    """Criterion [7] of `proactive-assertions.md`: the refusal a person meets
    carries the project-scoped code AND the gesture that repairs it.

    The sentence is a module constant so both doors of the ceremony quote the
    same words. That both doors actually raise it -- the request AND the
    confirmation, which did not until 2026-08-30 -- is walked over HTTP in
    `tests/integration/test_project_external_sharing_pg.py`.
    """
    from core.project_external_sharing import ENABLE_GESTURE, ExternalSharingForbidden

    assert "Manage role" in ENABLE_GESTURE
    assert "Project settings" in ENABLE_GESTURE
    assert ExternalSharingForbidden.code == "external_sharing_forbidden"
    assert ENABLE_GESTURE in str(ExternalSharingForbidden())


def test_both_halves_of_the_ceremony_are_human_confirmed_with_a_real_reference():
    """The measurement criterion [7] was judged on: `confirmation_mode="none"` on
    creation and a `confirmation_reference=None` on both operations."""
    import inspect

    from core import render_shares

    create = inspect.getsource(render_shares.create_share)
    confirm = inspect.getsource(render_shares.confirm_share)
    for source in (create, confirm):
        assert 'confirmation_mode="human"' in source
        assert "confirmation_reference=confirmation_id" in source
        assert "confirmation_reference=None" not in source


@pytest.mark.parametrize(
    ("minted_feedback", "has_feedback"),
    [
        (
            {
                "schema_version": "exact-feedback.v1",
                "token": "signed",
                "interaction_ref": "afi_EXAMPLE",
                "expires_at": "2026-08-10T00:15:00Z",
            },
            True,
        ),
        (None, False),
    ],
)
def test_session_render_emits_frozen_ai_path_beside_render(
    client, monkeypatch, minted_feedback, has_feedback
):
    """AI Path is adjacent application data, never a sixth RenderInput field."""
    import contextlib
    from types import SimpleNamespace

    from core import render_shares_api as api

    class Connection:
        def commit(self):
            return None

    projection = {
        "schema_version": "observed-ai-path.v1",
        "state": "completed",
        "path_id": "aip_EXAMPLE",
        "lifecycle": "finalized",
        "outcome": "succeeded",
        "steps": [],
    }
    monkeypatch.setattr(
        api, "render_share_connection", lambda: contextlib.nullcontext(Connection())
    )
    monkeypatch.setattr(
        api,
        "resolve_session",
        lambda *_args, **_kwargs: SimpleNamespace(
            share_id="rsh_EXAMPLE",
            org_id="org_EXAMPLE",
            project_id="proj_EXAMPLE",
            render_id="rnd_EXAMPLE",
        ),
    )
    monkeypatch.setattr(
        api,
        "load_frozen_render",
        lambda *_args, **_kwargs: {
            "runtime_input": {
                "result": {}, "spec": {}, "pins": {}, "profile": "share", "display": {}
            },
            "ai_path_evidence": projection,
            "evidence_manifest": {},
        },
    )
    monkeypatch.setattr(
        api,
        "share_disclosure",
        lambda *_args, **_kwargs: {
            "shared_on": "2026-08-10T00:00:00+00:00",
            "expires_at": "2026-08-11T00:00:00+00:00",
        },
    )
    monkeypatch.setattr(api, "append_access_event", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        api,
        "mint_share_feedback_context",
        lambda *_args, **_kwargs: minted_feedback,
    )

    response = client.get("/api/render-shares/session/render")
    assert response.status_code == 200
    assert response.json()["ai_path_evidence"] == projection
    assert ("feedback_context" in response.json()) is has_feedback
    if has_feedback:
        assert response.json()["feedback_context"]["schema_version"] == "exact-feedback.v1"
    assert set(response.json()["render"]) == {"result", "spec", "pins", "profile", "display"}


def test_session_feedback_delegates_the_exact_command_and_returns_receipt(client, monkeypatch):
    import contextlib
    from types import SimpleNamespace

    from core import render_shares_api as api

    class Connection:
        def commit(self):
            return None

    command = {
        "context": {"schema_version": "exact-feedback.v1", "token": "signed"},
        "target": {"kind": "datum", "row_index": 2, "field": "sessions"},
        "polarity": "positive",
        "comment": "Useful.",
        "retry_key": "retry-1",
    }
    seen = {}
    monkeypatch.setattr(
        api, "render_share_connection", lambda: contextlib.nullcontext(Connection())
    )
    monkeypatch.setattr(
        api,
        "resolve_session",
        lambda *_args, **_kwargs: SimpleNamespace(
            share_id="rsh_EXAMPLE",
            org_id="org_EXAMPLE",
            project_id="proj_EXAMPLE",
            render_id="rnd_EXAMPLE",
            dossier_version_id=None,
        ),
    )
    monkeypatch.setattr(api, "load_frozen_render", lambda *_args: {"frozen": True})

    def write(_conn, _session, _frozen, *, payload):
        seen["payload"] = payload
        return {
            "schema_version": "exact-feedback-receipt.v1",
            "status": "recorded",
            "feedback_id": "rsfb_EXAMPLE",
            "interaction_ref": "afi_EXAMPLE",
            "target": payload["target"],
        }

    monkeypatch.setattr(api, "record_targeted_feedback", write)
    monkeypatch.setattr(api, "append_access_event", lambda *_args, **_kwargs: None)
    response = client.post("/api/render-shares/session/feedback", json=command)
    assert response.status_code == 200
    assert seen["payload"] == command
    assert response.json() == {
        "schema_version": "exact-feedback-receipt.v1",
        "status": "recorded",
        "feedback_id": "rsfb_EXAMPLE",
        "interaction_ref": "afi_EXAMPLE",
        "target": command["target"],
    }


# ---------------------------------------------------------------------------
# AC11 -- the export is inert.
# ---------------------------------------------------------------------------


def test_the_export_document_makes_no_network_request():
    """A self-contained file: no script, no remote reference, nothing to fetch."""
    from core.render_shares_api import _build_export_document  # noqa: PLC0415

    document = _build_export_document(
        {
            "render_id": "rnd_example",
            "evidence_manifest": {"freshness": "2026-07-30T00:00:00Z", "grain": "day"},
        },
        {"shared_on": "2026-07-31T09:00:00+00:00", "expires_at": "2026-08-02T09:00:00+00:00"},
    )
    for forbidden in ("<script", "<link", "<img", "http://", "https://", "fetch(", "@import"):
        assert forbidden not in document, f"the export can reach the network via {forbidden!r}"
    assert "Shared on" in document and "2026-07-31" in document
    assert 'lang="en"' in document


def test_the_export_document_names_no_internal_identity():
    """AC11: no Project, organization, Result, Query Spec or Datastream identifier."""
    from core.render_shares_api import _build_export_document  # noqa: PLC0415

    document = _build_export_document(
        {
            "render_id": "rnd_secret",
            "result_id": "qr_secret",
            "visualization_spec_version_id": "vsv_secret",
            "evidence_manifest": {"freshness": "2026-07-30T00:00:00Z"},
        },
        {"shared_on": "2026-07-31T09:00:00+00:00", "expires_at": "2026-08-02T09:00:00+00:00"},
    )
    for identity in ("rnd_secret", "qr_secret", "vsv_secret"):
        assert identity not in document, f"{identity} leaked into the export"


# ---------------------------------------------------------------------------
# AC6 -- the view page mounts the Story 50.5 runtime and nothing else.
# ---------------------------------------------------------------------------


def test_the_view_page_mounts_the_shared_runtime_after_the_envelope_exists():
    """`window.__TOOROW_FROZEN_RENDER__` is the global `ui/cards/shell/src/viz/
    entries/share.tsx` already reads on mount.

    THE ORDER IS THE PROPERTY. The runtime module is served INERT and promoted only
    after the fetch resolves and the global is assigned -- otherwise the bundle
    mounts first, finds no envelope, and paints its "carries no frozen result" state
    before the real one arrives. The page draws once, and what it draws is the
    frozen answer."""
    from core.render_shares_api import _compose_view  # noqa: PLC0415

    html = _compose_view("NONCE123")
    assert 'id="widget-mount"' in html
    assert 'nonce="NONCE123"' in html
    assert "window.__TOOROW_FROZEN_RENDER__" in html
    assert "window.__TOOROW_FROZEN_AI_PATH_EVIDENCE__" in html
    assert "window.__TOOROW_FROZEN_FEEDBACK_CONTEXT__" in html
    # Inert until promoted.
    assert "data-toorow-runtime" in html
    render_assignment = html.index("__TOOROW_FROZEN_RENDER__ = body.render")
    evidence_assignment = html.index(
        "__TOOROW_FROZEN_AI_PATH_EVIDENCE__ = body.ai_path_evidence"
    )
    feedback_assignment = html.index(
        "__TOOROW_FROZEN_FEEDBACK_CONTEXT__ = body.feedback_context"
    )
    # Re-stated 2026-09-02 (73-2): the dossier branch carries its OWN promote,
    # textually earlier -- the property is per FLOW, so each branch is probed
    # from its own assignments forward.
    runtime_start = html.index("startRuntime();", feedback_assignment)
    assert render_assignment < evidence_assignment < feedback_assignment < runtime_start
    dossier_assignment = html.index("__TOOROW_FROZEN_DOSSIER__ = body.dossier")
    assert dossier_assignment < html.index("startRuntime();", dossier_assignment)
    assert html.count("fetch('/api/render-shares/session/render'") == 1
    assert html.count("fetch('/api/render-shares/session/dossier'") == 1
    # It mounts the shared runtime; it does not implement one.
    assert "canvas" not in html.lower().split("data-toorow-runtime")[0]


def test_the_view_page_carries_no_frozen_data_of_its_own():
    """The shell is public. Every value the recipient sees arrives through the
    session API, so there is exactly one place where access is decided."""
    from core.render_shares_api import _compose_view  # noqa: PLC0415

    html = _compose_view("N")
    assert "/api/render-shares/session/render" in html
    assert "credentials: 'same-origin'" in html


def test_the_page_and_the_export_share_one_disclosure_implementation():
    """An exported artifact must never make a weaker claim than the page it came
    from, so both read the same rows."""
    import inspect

    from core import render_shares_api as api

    assert "_disclosure_rows(" in inspect.getsource(api._disclosure_block)  # noqa: SLF001
    assert "_disclosure_rows(" in inspect.getsource(api._session_render)  # noqa: SLF001


def test_untrusted_manifest_text_can_never_reach_a_script_or_markup_context():
    """The escaping question is REMOVED rather than answered.

    The page no longer embeds the envelope in a `<script>` block: it fetches JSON and
    writes each value with `textContent`. A `</script>` inside a manifest value
    therefore has nothing to close, and markup has nothing to open. The export, which
    does render HTML, escapes with `html.escape` -- asserted here too, because it is
    the one surface where the question still applies (AD-31)."""
    import inspect

    from core.render_shares_api import _compose_view, _disclosure_block  # noqa: PLC0415

    html = _compose_view("N")
    assert ".textContent = list[i].value" in html
    # Scoped to THIS story's bootstrap: the bundled React runtime uses `.innerHTML`
    # in its own minified internals, and asserting over the whole document would be
    # asserting about a vendored library rather than about this page.
    from core.render_shares_api import _VIEW_CHROME_SCRIPT  # noqa: PLC0415

    assert ".innerHTML" not in _VIEW_CHROME_SCRIPT

    hostile = "</script><script>alert(1)</script><img src=x onerror=alert(1)>"
    block = _disclosure_block(
        {"evidence_manifest": {"truncation": hostile}},
        {"shared_on": "2026-07-31T09:00:00+00:00"},
    )
    assert "<script" not in block and "<img" not in block
    assert "&lt;script" in block
    assert inspect.getsource(_disclosure_block).count("html.escape") >= 2


def test_the_view_page_discloses_shared_on_and_data_as_of_from_frozen_facts():
    """AC7: `Shared on` is the Share's creation date, never "now"; `Data as of` is
    the Render's own frozen freshness, never recomputed."""
    from core.render_shares_api import _disclosure_block  # noqa: PLC0415

    block = _disclosure_block(
        {"evidence_manifest": {"freshness": "2026-07-28T00:00:00Z", "grain": "day",
                               "filters": ["country = FR"], "truncation": "top 100"}},
        {"shared_on": "2026-07-31T09:00:00+00:00"},
    )
    assert "Shared on" in block and "2026-07-31" in block
    assert "Data as of" in block and "2026-07-28" in block
    assert "Filters" in block and "country = FR" in block
    assert "Truncation" in block and "top 100" in block


def test_the_view_page_reads_the_freshness_the_result_writer_actually_records():
    """`query_execution` writes `freshness.output_created_at` -- a dict, not a
    string. Measured 2026-09-04 on the deployment: every share page said
    « Not disclosed by this Render » while the frozen manifest carried the instant.
    """
    from core.render_shares_api import _disclosure_block  # noqa: PLC0415

    block = _disclosure_block(
        {
            "runtime_input": {
                "result": {
                    "manifest": {"freshness": {"output_created_at": "2026-09-04T10:43:36+00:00"}}
                }
            },
            "evidence_manifest": {},
        },
        {"shared_on": "2026-09-04T17:00:00+00:00"},
    )
    assert "Data as of" in block and "2026-09-04" in block
    assert "Not disclosed by this Render" not in block


def test_a_render_without_a_frozen_freshness_says_so_rather_than_inventing_one():
    from core.render_shares_api import _disclosure_block  # noqa: PLC0415

    block = _disclosure_block({"evidence_manifest": {}}, {"shared_on": "2026-07-31T09:00:00+00:00"})
    assert "Not disclosed by this Render" in block


def test_the_disclosure_escapes_untrusted_manifest_text():
    """AD-31: manifest values are data. They are rendered neutrally, never as markup."""
    from core.render_shares_api import _disclosure_block  # noqa: PLC0415

    block = _disclosure_block(
        {"evidence_manifest": {"grain": "<img src=x onerror=alert(1)>"}},
        {"shared_on": "2026-07-31T09:00:00+00:00"},
    )
    assert "<img" not in block
    assert "&lt;img" in block


# ---------------------------------------------------------------------------
# AC9 -- rate limiting.
# ---------------------------------------------------------------------------


def test_the_rate_limit_returns_429_with_retry_after_and_the_common_envelope(client, monkeypatch):
    from core import render_shares

    monkeypatch.setattr(render_shares, "check_rate_limit", lambda **_: (False, 42.0))
    import core.render_shares_api as api

    monkeypatch.setattr(api, "check_rate_limit", lambda **_: (False, 42.0))

    response = client.post("/api/render-shares/exchange", json={"bearer": "b" * 43})
    assert response.status_code == 429
    assert response.headers["retry-after"] == "43"
    assert response.json()["code"] == "share_unavailable"
    _assert_security_headers(response)


def test_the_rate_limiter_never_keys_on_plaintext():
    from core import render_shares

    render_shares.reset_rate_limits()
    bearer = "PLAINTEXTBEARER0123456789abcdefghijklmno"
    render_shares.check_rate_limit(
        ip_hash=render_shares.client_ip_hash("192.0.2.10"),
        subject_hash=render_shares.session_hash(bearer),
    )
    keys = " ".join(render_shares._RATE_BUCKETS)  # noqa: SLF001
    assert bearer not in keys and bearer[:8] not in keys
    assert "192.0.2.10" not in keys


# ---------------------------------------------------------------------------
# AC3 -- the delivery URL is the only place a bearer appears.
# ---------------------------------------------------------------------------


def test_the_delivery_origin_is_validated_like_the_invitation_one(monkeypatch):
    """A misconfigured origin carrying a query string would turn the fragment into a
    query parameter -- the one place the bearer must never be."""
    from core import render_shares

    for bad in (
        "http://share.example.com",              # not HTTPS
        "https://user:pw@share.example.com",     # credentials
        "https://share.example.com?x=1",         # query
        "https://share.example.com#f",           # fragment
        "https://share.example.com/deep/path",   # non-root path
        "",                                      # unset
    ):
        monkeypatch.setenv("TOOROW_RENDER_SHARE_ORIGIN", bad)
        with pytest.raises(render_shares.RenderShareValidationError):
            render_shares.build_fragment_delivery_url("b" * 43)
    monkeypatch.setenv("TOOROW_RENDER_SHARE_ORIGIN", "https://share.example.com")
    url = render_shares.build_fragment_delivery_url("b" * 43)
    assert url == "https://share.example.com/share#render=" + "b" * 43


def test_a_short_pepper_is_refused(monkeypatch):
    """D3: the pepper is purpose-scoped and refused below 32 bytes, exactly as
    `core.invitations` refuses its own."""
    from core import render_shares

    monkeypatch.setenv("TOOROW_RENDER_SHARE_PEPPER", "tooshort")  # restored by monkeypatch (AI-377)
    with pytest.raises(render_shares.RenderShareValidationError):
        render_shares.bearer_hash("b" * 43)


def test_the_bearer_is_256_bits_and_distinct_every_time():
    from core import render_shares

    minted = {render_shares.mint_bearer() for _ in range(64)}
    assert len(minted) == 64
    assert all(len(value) >= 43 for value in minted)


def test_the_pepper_is_distinct_from_every_other_bearer_class():
    """A shared pepper means a bearer minted for one class produces a valid digest in
    another class's table, and one leak compromises both."""
    import io
    import pathlib
    import tokenize

    # Code only: the module explains in prose WHY it does not reuse the invitation
    # pepper, and a text search cannot tell that explanation from a use of it.
    source = (
        pathlib.Path(__file__).resolve().parents[2] / "core" / "render_shares.py"
    ).read_text(encoding="utf-8")
    code = " ".join(
        token.string
        for token in tokenize.generate_tokens(io.StringIO(source).readline)
        if token.type not in (tokenize.COMMENT,)
    )
    assert "TOOROW_RENDER_SHARE_PEPPER" in code
    # A STRING literal naming the invitation pepper would be a real use, so string
    # tokens are deliberately kept here -- only comments and docstrings' prose are
    # excluded, and docstrings are STRING tokens, so narrow the check to the
    # function that actually reads the environment.
    import re as _re

    reader = _re.search(r"def _pepper\(\).*?return value\.encode", source, _re.S)
    assert reader is not None
    assert "TOOROW_INVITATION_PEPPER" not in reader.group(0).split('"""')[-1]


def test_the_three_digest_purposes_are_distinct():
    """Two purposes, two hashes -- a session value and a bearer must not collide."""
    from core import render_shares

    value = "same-secret-material-0123456789abcdef"
    assert render_shares.bearer_hash(value) != render_shares.session_hash(value)
    assert render_shares.bearer_hash(value) != render_shares.client_ip_hash(value)


# ---------------------------------------------------------------------------
# The cookie. Asserted end to end where a database is available.
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not _DSN, reason="TEST_POSTGRES_DSN not set; a skip is not a pass")
def test_the_exchange_sets_a_narrow_strict_cookie(client):
    """AC5: `HttpOnly; Secure; SameSite=Strict`, path-scoped to the public API
    family, and never longer than the Share it opens."""
    # RESTORED, NOT POPPED -- and the difference was worth 115 red tests.
    # This `finally` used to `os.environ.pop("PLATFORM_DB_URL", None)`, which
    # does not put back what was there: it DELETES it. Every DB-touching test
    # that ran after this file in the same session then fell back to the default
    # `localhost:5432` and answered `password authentication failed`, so
    # `pytest server/tests/core` reported ~115 failures and errors -- from
    # `test_reports_api` (the very next module alphabetically) to
    # `test_warehouse_write_pg` -- that vanish the moment the file is run alone.
    # Measured 2026-09-01: the first casualty is the module immediately after
    # this one, which is what named the leak.
    _previous_db_url = os.environ.get("PLATFORM_DB_URL")
    os.environ["PLATFORM_DB_URL"] = _DSN
    import psycopg  # noqa: PLC0415

    from tests.integration.test_render_shares_postgres import Chain  # noqa: PLC0415

    conn = psycopg.connect(_DSN)
    try:
        chain = Chain(conn).build()
        created = chain.share()
        conn.commit()
        bearer = created.delivery_url.split("#render=")[1]

        response = client.post("/api/render-shares/exchange", json={"bearer": bearer})
        assert response.status_code == 200, response.text
        assert json.loads(response.content)["next_url"] == "/share/view"

        raw = response.headers["set-cookie"]
        assert "__session=" in raw
        assert "HttpOnly" in raw
        assert "Secure" in raw
        assert "SameSite=strict" in raw.replace("SameSite=Strict", "SameSite=strict")
        assert "Path=/api/render-shares" in raw
        # Nothing in the response identifies the Project, the Render or the Result.
        body = response.text
        assert chain.project_id not in body and chain.render_id not in body
        assert chain.result_id not in body and chain.org_id not in body
    finally:
        conn.rollback()
        conn.close()
        if _previous_db_url is None:
            os.environ.pop("PLATFORM_DB_URL", None)
        else:
            os.environ["PLATFORM_DB_URL"] = _previous_db_url


def test_the_bundle_is_stamped_by_naming_its_tags_not_by_pattern_matching():
    r"""A blanket `replace("<script", ...)` also rewrites the string literal inside
    React's minified source (`o.innerHTML='<script><\/script>'`), editing a
    vendored library to say something its authors did not write. It did, and this
    pins the fix: exactly one module tag is neutralized and React's internals are
    left byte-identical."""
    from core.render_shares_api import _compose_view, share_bundle_path  # noqa: PLC0415

    if not share_bundle_path().exists():
        pytest.skip("share bundle not built; run pnpm --filter @toorow/card-shell build")
    raw = share_bundle_path().read_text(encoding="utf-8")
    html = _compose_view("NONCE")

    assert raw.count("<script") == 2, "bundle shape changed; re-check the stamping"
    assert html.count('data-toorow-runtime="1"') == 1
    # React's internal literal survives untouched.
    assert r"o.innerHTML=`<script><\/script>`" in html
    assert 'o.innerHTML=`<script nonce' not in html


# ---------------------------------------------------------------------------
# F1 -- the envelope the runtime actually receives.
#
# THE TWO HALVES OF THIS PROOF LIVE IN TWO LANGUAGES, and neither is enough alone.
# Python can assert what the server sends; only the runtime can say whether it
# renders. So the server-side test below pins the envelope against a committed
# fixture, and `ui/cards/shell/src/viz/__tests__/shareFrozenEnvelope.test.tsx`
# mounts `ShareVisualization` over THAT SAME FILE and asserts a frozen value
# appears in the DOM. Change the composition and one of the two goes red;
# regenerate the fixture without fixing the runtime and the other does.
# ---------------------------------------------------------------------------

_FIXTURE = (
    pathlib.Path(__file__).resolve().parents[3]
    / "ui" / "cards" / "shell" / "src" / "viz" / "__tests__" / "fixtures"
    / "shareSessionRender.json"
)


def test_the_share_fixture_normalizer_is_importable_from_the_test_package():
    from types import SimpleNamespace

    from tests.fixture_generators.share_session_render import normalize_envelope

    body = {
        "ai_path_evidence": {
            "path_id": "aip_actual",
            "steps": [{"observed_at": "2026-08-10T10:11:12+00:00"}],
        }
    }
    normalized = normalize_envelope(
        body,
        SimpleNamespace(
            result_id="qr_actual",
            render_id="rnd_actual",
            spec_version_id="vsv_actual",
            project_id="proj_actual",
            org_id="org_actual",
            ai_path_id="aip_actual",
        ),
    )
    assert normalized["ai_path_evidence"] == {
        "path_id": "aip_FIXTURE",
        "steps": [{"observed_at": "2026-01-01T00:00:00Z"}],
    }


@pytest.mark.skipif(not _DSN, reason="TEST_POSTGRES_DSN not set; a skip is not a pass")
def test_the_session_render_envelope_is_the_runtimes_own_contract():
    """`render` IS `RenderInput`: five fields, the frozen rows, the Render's pins.

    It used to be this module's metadata dict -- `content_hash`,
    `datum_evidence_keys`, `display_state`, `render_id`, ... -- which
    `ui/cards/shell/src/viz/entries/share.tsx` casts to `RenderInput` and hands to
    a validator that refuses every one of those keys by name. The recipient met a
    refusal panel listing missing fields.
    """
    # Restored, not popped -- see the comment on the sibling above.
    _previous_db_url = os.environ.get("PLATFORM_DB_URL")
    os.environ["PLATFORM_DB_URL"] = _DSN
    import psycopg  # noqa: PLC0415
    from core.render_shares_api import render_share_routes  # noqa: PLC0415
    from starlette.applications import Starlette  # noqa: PLC0415
    from starlette.testclient import TestClient  # noqa: PLC0415

    from tests.fixture_generators.share_session_render import (  # noqa: PLC0415
        normalize_envelope,
    )
    from tests.integration.test_render_shares_postgres import RESULT_ROWS, Chain  # noqa: PLC0415

    conn = psycopg.connect(_DSN)
    try:
        chain = Chain(conn, with_ai_path=True).build()
        created = chain.share()
        conn.commit()
        bearer = created.delivery_url.split("#render=")[1]
        # HTTPS, because the session cookie is `Secure` and an http base URL makes
        # the client silently drop it -- a 401 that looks like a broken handler.
        http = TestClient(
            Starlette(routes=render_share_routes),
            base_url="https://testserver",
            raise_server_exceptions=False,
        )
        assert http.post("/api/render-shares/exchange", json={"bearer": bearer}).status_code == 200
        response = http.get("/api/render-shares/session/render")
        assert response.status_code == 200, response.text
        body = json.loads(response.content)

        envelope = body["render"]
        assert envelope is not None, "the page would mount the runtime over nothing"
        assert set(envelope) == {"result", "spec", "pins", "profile", "display"}
        assert envelope["result"]["rows"] == RESULT_ROWS
        assert "unavailable" not in body
        assert body["ai_path_evidence"]["state"] == "completed"
        assert body["ai_path_evidence"]["path_id"] == chain.ai_path_id

        normalized = normalize_envelope(body, chain)
        committed = json.loads(_FIXTURE.read_text(encoding="utf-8"))
        assert normalized == committed, (
            "the /session/render envelope changed. The runtime test in "
            "ui/cards/shell/src/viz/__tests__/shareFrozenEnvelope.test.tsx mounts the "
            "committed fixture, so regenerate it ONLY after confirming the runtime "
            "still renders the new shape."
        )
    finally:
        conn.rollback()
        conn.close()
        if _previous_db_url is None:
            os.environ.pop("PLATFORM_DB_URL", None)
        else:
            os.environ["PLATFORM_DB_URL"] = _previous_db_url


def test_session_dossier_serves_the_sequence_with_inline_figures(client, monkeypatch):
    """73-2: ONE round trip serves the whole document, and every Render identity
    comes from the pinned version's own blocks -- never from the request, which
    is the retirement rule of this surface."""
    import contextlib

    from core import render_shares_api as api
    from core.render_shares import ShareSession

    class Connection:
        def commit(self):
            return None

    events: list[str] = []
    monkeypatch.setattr(
        api, "render_share_connection", lambda: contextlib.nullcontext(Connection())
    )
    monkeypatch.setattr(
        api,
        "resolve_session",
        lambda *_a, **_k: ShareSession(
            share_id="rsh_EXAMPLE",
            org_id="org_EXAMPLE",
            project_id="proj_EXAMPLE",
            render_id=None,
            dossier_version_id="dosv_EXAMPLE",
        ),
    )
    monkeypatch.setattr(
        api,
        "dossier_sequence",
        lambda *_a, **_k: {
            "label": "Q3 review",
            "version_number": 1,
            "blocks": [
                {"kind": "narrative", "text": "What the quarter says."},
                {"kind": "render", "render_id": "rnd_EXAMPLE"},
            ],
        },
    )

    def _frozen(_conn, session):
        assert session.render_id == "rnd_EXAMPLE"
        return {
            "runtime_input": {
                "result": {}, "spec": {}, "pins": {}, "profile": "share", "display": {}
            },
            "ai_path_evidence": {"state": "unavailable"},
        }

    monkeypatch.setattr(api, "load_frozen_render", _frozen)
    monkeypatch.setattr(api, "share_disclosure", lambda *_a, **_k: {})
    monkeypatch.setattr(
        api, "_disclosure_rows", lambda *_a, **_k: [("Shared on", "2026-09-02")]
    )
    monkeypatch.setattr(
        api, "mint_share_feedback_context", lambda *_a, **_k: {"interaction_ref": "afi_EXAMPLE"}
    )
    monkeypatch.setattr(
        api, "append_access_event", lambda *_a, **k: events.append(k.get("event"))
    )

    response = client.get(
        "/api/render-shares/session/dossier", cookies={"toorow_share_session": "s" * 64}
    )
    assert response.status_code == 200
    body = response.json()
    assert body["dossier"]["label"] == "Q3 review"
    figure = body["dossier"]["blocks"][1]
    assert figure["render"]["profile"] == "share"
    assert figure["facts"] == [{"label": "Shared on", "value": "2026-09-02"}]
    assert figure["feedback_context"] == {"interaction_ref": "afi_EXAMPLE"}
    assert "unavailable" not in figure
    assert events == ["read"]


def test_session_dossier_is_unavailable_for_a_single_render_grant(client, monkeypatch):
    """A grant over one Render opens no sequence: the same 401-unavailable as
    every other mismatch of this surface, disclosing nothing."""
    import contextlib

    from core import render_shares_api as api
    from core.render_shares import ShareSession

    class Connection:
        def commit(self):
            return None

    monkeypatch.setattr(
        api, "render_share_connection", lambda: contextlib.nullcontext(Connection())
    )
    monkeypatch.setattr(
        api,
        "resolve_session",
        lambda *_a, **_k: ShareSession(
            share_id="rsh_EXAMPLE",
            org_id="org_EXAMPLE",
            project_id="proj_EXAMPLE",
            render_id="rnd_EXAMPLE",
        ),
    )
    response = client.get(
        "/api/render-shares/session/dossier", cookies={"toorow_share_session": "s" * 64}
    )
    assert response.status_code == 401


def test_the_view_page_prints_through_a_stylesheet_not_a_second_path():
    """73-3: the PDF is the page, printed. The amendment names the path itself
    -- a print stylesheet over the share page -- so what lands on paper is what
    the share shows, and no second rendering path exists to drift."""
    from core.render_shares_api import _compose_view  # noqa: PLC0415

    html = _compose_view("NONCE123")
    assert '<style media="print" nonce="NONCE123">' in html
    assert 'id="share-print"' in html
    assert 'id="share-print-footer"' in html
    # The controls stay off the paper; a figure stays whole on its page.
    assert ".share-actions, #share-status { display: none !important; }" in html
    assert "break-inside: avoid" in html
    # The footer stamps the export date the amendment wants on the document.
    assert "Exported to PDF from a frozen share on" in html


def test_session_export_of_a_dossier_is_one_scriptless_file_in_document_order(
    client, monkeypatch
):
    """73-3: the dossier exports as ONE self-contained file -- narrative
    verbatim, each figure's disclosure and values by the SAME builders the
    single-result export uses, identities from the pinned version's blocks."""
    import contextlib

    from core import render_shares_api as api
    from core.render_shares import ShareSession

    class Connection:
        def commit(self):
            return None

    monkeypatch.setattr(
        api, "render_share_connection", lambda: contextlib.nullcontext(Connection())
    )
    monkeypatch.setattr(
        api,
        "resolve_session",
        lambda *_a, **_k: ShareSession(
            share_id="rsh_EXAMPLE",
            org_id="org_EXAMPLE",
            project_id="proj_EXAMPLE",
            render_id=None,
            dossier_version_id="dosv_EXAMPLE",
        ),
    )
    monkeypatch.setattr(
        api,
        "dossier_sequence",
        lambda *_a, **_k: {
            "label": "Q3 channel review",
            "version_number": 1,
            "blocks": [
                {"kind": "narrative", "text": "What the quarter says."},
                {"kind": "render", "render_id": "rnd_EXAMPLE"},
            ],
        },
    )
    monkeypatch.setattr(
        api,
        "load_frozen_render",
        lambda _c, session: {
            "runtime_input": {
                "result": {
                    "outcome": "success",
                    "rows": [{"day": "2026-07-01", "views": 700}],
                    "schema": {"fields": [{"name": "day"}, {"name": "views"}]},
                },
                "spec": {},
                "pins": {},
                "profile": "share",
                "display": {},
            },
            "evidence_manifest": None,
        },
    )
    monkeypatch.setattr(api, "share_disclosure", lambda *_a, **_k: {})
    monkeypatch.setattr(api, "_disclosure_block", lambda *_a, **_k: "<dl></dl>")
    monkeypatch.setattr(api, "append_access_event", lambda *_a, **_k: None)

    response = client.post(
        "/api/render-shares/session/export", cookies={"toorow_share_session": "s" * 64}
    )
    assert response.status_code == 200
    assert response.headers["content-disposition"] == 'attachment; filename="shared-dossier.html"'
    document = response.text
    assert "Q3 channel review" in document
    assert "What the quarter says." in document
    assert "<table>" in document and ">700<" in document
    assert "<script" not in document


def test_dossier_feedback_is_aimed_by_the_verified_context_never_the_request(
    client, monkeypatch
):
    """73-2, last half: the figure a feedback targets comes from the SUBMITTED
    minted context -- verified before any claim is read -- with membership
    re-checked against the pinned version's own blocks."""
    import contextlib

    from core import analyze_feedback as af
    from core import render_shares as rs
    from core import render_shares_api as api
    from core.render_shares import ShareSession

    class Connection:
        def commit(self):
            return None

    monkeypatch.setattr(
        api, "render_share_connection", lambda: contextlib.nullcontext(Connection())
    )
    monkeypatch.setattr(
        api,
        "resolve_session",
        lambda *_a, **_k: ShareSession(
            share_id="rsh_EXAMPLE",
            org_id="org_EXAMPLE",
            project_id="proj_EXAMPLE",
            render_id=None,
            session_hash="h" * 64,
            dossier_version_id="dosv_EXAMPLE",
        ),
    )
    monkeypatch.setattr(
        af, "verify_feedback_context", lambda *_a, **_k: {"render_id": "rnd_A"}
    )
    monkeypatch.setattr(
        rs, "dossier_version_render_ids", lambda *_a, **_k: ["rnd_A", "rnd_B"]
    )

    aimed_at: list[str] = []

    def _frozen(_conn, session):
        aimed_at.append(session.render_id)
        return {"runtime_input": {}}

    monkeypatch.setattr(api, "load_frozen_render", _frozen)
    monkeypatch.setattr(api, "append_access_event", lambda *_a, **_k: None)
    monkeypatch.setattr(
        api,
        "record_targeted_feedback",
        lambda *_a, **_k: {"schema_version": "exact-feedback-receipt.v1", "status": "recorded"},
    )

    response = client.post(
        "/api/render-shares/session/feedback",
        cookies={"toorow_share_session": "s" * 64},
        json={
            "context": "sealed",
            "target": {"kind": "answer"},
            "polarity": "up",
            "comment": "",
            "retry_key": "r" * 32,
        },
    )
    assert response.status_code == 200
    assert response.json()["status"] == "recorded"
    assert aimed_at == ["rnd_A"]


def test_dossier_feedback_on_a_figure_outside_the_version_is_refused(client, monkeypatch):
    import contextlib

    from core import analyze_feedback as af
    from core import render_shares as rs
    from core import render_shares_api as api
    from core.render_shares import ShareSession

    class Connection:
        def commit(self):
            return None

    monkeypatch.setattr(
        api, "render_share_connection", lambda: contextlib.nullcontext(Connection())
    )
    monkeypatch.setattr(
        api,
        "resolve_session",
        lambda *_a, **_k: ShareSession(
            share_id="rsh_EXAMPLE",
            org_id="org_EXAMPLE",
            project_id="proj_EXAMPLE",
            render_id=None,
            session_hash="h" * 64,
            dossier_version_id="dosv_EXAMPLE",
        ),
    )
    monkeypatch.setattr(
        af, "verify_feedback_context", lambda *_a, **_k: {"render_id": "rnd_ELSEWHERE"}
    )
    monkeypatch.setattr(
        rs, "dossier_version_render_ids", lambda *_a, **_k: ["rnd_A", "rnd_B"]
    )
    response = client.post(
        "/api/render-shares/session/feedback",
        cookies={"toorow_share_session": "s" * 64},
        json={
            "context": "sealed",
            "target": {"kind": "answer"},
            "polarity": "up",
            "comment": "",
            "retry_key": "r" * 32,
        },
    )
    assert response.status_code == 401

