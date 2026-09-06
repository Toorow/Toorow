"""The project-scoped capability of `proactive-assertions.md` decision 2.

WHAT IS PROVED HERE: the switch exists, its DEFAULT is `forbidden`, the default
holds for a Project that has no preferences row at all -- which is every Project
that existed before migration 323 -- and moving it is an audited operation
naming the person who moved it.

WHY THE DEFAULT IS THE INTERESTING ASSERTION. A switch that shipped `allowed`
would be decorative on the whole existing population: nobody would ever have to
decide, and the criterion it answers -- "a model-authored assertion LEAVES THE
PLATFORM without a project-scoped capability" -- would still be open with the
column in place. The default IS the capability.
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone

import pytest
from ulid import ULID

pytestmark = pytest.mark.skipif(
    not (os.getenv("TEST_POSTGRES_DSN") or os.getenv("PLATFORM_DB_URL")),
    reason="TEST_POSTGRES_DSN not set -- the posture lives in Postgres",
)


def _uid(prefix: str) -> str:
    return f"{prefix}_{ULID()}"


@pytest.fixture()
def project(live_postgres):
    org_id, project_id = _uid("org"), _uid("proj")
    with live_postgres.cursor() as cur:
        cur.execute(
            "INSERT INTO app.organizations (id, name, slug, status, created_by) "
            "VALUES (%s, 'external sharing fixture', %s, 'active', 'test')",
            (org_id, org_id.replace("_", "-")),
        )
        cur.execute(
            "INSERT INTO app.projects (id, org_id, name, slug, created_by) "
            "VALUES (%s, %s, 'external sharing fixture', %s, 'test')",
            (project_id, org_id, project_id.replace("_", "-")),
        )
    yield org_id, project_id
    live_postgres.rollback()


def test_a_project_with_no_preferences_row_reads_as_the_platform_default(
    live_postgres, project
):
    from core.project_external_sharing import read_external_sharing

    _org_id, project_id = project
    posture = read_external_sharing(live_postgres, project_id=project_id)
    assert posture == {
        "state": "forbidden",
        "decided_by": None,
        "decided_at": None,
        "is_platform_default": True,
    }


def test_an_existing_preferences_row_is_backfilled_forbidden(live_postgres, project):
    """Migration 323's `DEFAULT` clause, on the population that already exists."""
    from core.project_external_sharing import read_external_sharing

    _org_id, project_id = project
    with live_postgres.cursor() as cur:
        cur.execute(
            "INSERT INTO app.project_preferences (project_id) VALUES (%s)",
            (project_id,),
        )
    posture = read_external_sharing(live_postgres, project_id=project_id)
    assert posture["state"] == "forbidden"
    # Nobody decided it: the platform default stands, and the screen must say so
    # rather than attribute the refusal to a person.
    assert posture["is_platform_default"] is True


def test_allowing_the_exit_names_who_allowed_it_and_writes_an_audit_row(
    live_postgres, project
):
    from core.project_external_sharing import (
        SET_EXTERNAL_SHARING_COMMAND,
        read_external_sharing,
        set_external_sharing,
    )

    org_id, project_id = project
    moved = set_external_sharing(
        live_postgres,
        org_id=org_id,
        project_id=project_id,
        state="allowed",
        actor="admin@example.com",
        idempotency_key=f"allow-{project_id}",
    )
    assert moved["state"] == "allowed"
    assert moved["decided_by"] == "admin@example.com"
    assert moved["is_platform_default"] is False
    assert read_external_sharing(live_postgres, project_id=project_id)["state"] == (
        "allowed"
    )
    with live_postgres.cursor() as cur:
        cur.execute(
            "SELECT identity, outcome FROM app.audit_log WHERE action = %s "
            "AND effective_org_id = %s",
            (SET_EXTERNAL_SHARING_COMMAND, org_id),
        )
        assert cur.fetchall() == [("admin@example.com", "succeeded")]


def test_turning_the_exit_off_again_leaves_the_decision_visible(live_postgres, project):
    from core.project_external_sharing import read_external_sharing, set_external_sharing

    org_id, project_id = project
    for state in ("allowed", "forbidden"):
        set_external_sharing(
            live_postgres,
            org_id=org_id,
            project_id=project_id,
            state=state,
            actor="admin@example.com",
            idempotency_key=f"{state}-{project_id}",
        )
    posture = read_external_sharing(live_postgres, project_id=project_id)
    assert posture["state"] == "forbidden"
    # A deliberate `forbidden` is NOT the platform default: someone chose it, and
    # the screen may say who.
    assert posture["is_platform_default"] is False
    assert posture["decided_by"] == "admin@example.com"


def test_a_third_posture_is_refused_by_the_module_and_by_the_schema(
    live_postgres, project
):
    from core.project_external_sharing import (
        ExternalSharingValidationError,
        set_external_sharing,
    )

    org_id, project_id = project
    # Refused BEFORE any SQL, so nothing needs unwinding and the transaction the
    # schema half runs in is still usable.
    with pytest.raises(ExternalSharingValidationError):
        set_external_sharing(
            live_postgres,
            org_id=org_id,
            project_id=project_id,
            state="sometimes",
            actor="admin@example.com",
            idempotency_key=f"bad-{project_id}",
        )
    with live_postgres.cursor() as cur:
        cur.execute(
            "INSERT INTO app.project_preferences (project_id) VALUES (%s)",
            (project_id,),
        )
    with live_postgres.cursor() as cur, pytest.raises(Exception) as exc:
        cur.execute(
            "UPDATE app.project_preferences SET external_sharing = 'sometimes' "
            "WHERE project_id = %s",
            (project_id,),
        )
    assert getattr(exc.value, "sqlstate", None) == "23514"
    live_postgres.rollback()


def test_the_settings_envelope_carries_the_posture_and_who_may_change_it(
    live_postgres, project
):
    """The screen reads authority, it never infers it."""
    from core.project_settings import read_project_settings

    _org_id, project_id = project
    envelope = read_project_settings(
        live_postgres, project_id=project_id, can_edit=True, can_manage=False
    )
    posture = envelope["project"]["external_sharing"]
    assert posture["state"] == "forbidden"
    assert posture["can_change"] is False
    assert envelope["project"]["can_manage"] is False

    manager_view = read_project_settings(
        live_postgres, project_id=project_id, can_edit=True, can_manage=True
    )
    assert manager_view["project"]["external_sharing"]["can_change"] is True


# ═══════════════════════════════════════════════════════════════════════════════
# THE CEREMONY, WALKED OVER HTTP.
#
# WHAT WAS HERE BEFORE. One test that read two handlers with `inspect.getsource`
# and asserted the substring `_authorize(request, "edit")`. That substring was
# the DEFECT: the console's `_authorize` delegated to a helper that takes a ROLE
# (`viewer`/`member`/`admin`/`owner`), so `"edit"` and `"manage"` raised
# `ValueError: unknown project role` and all four console routes answered 500 --
# listing included. No test called a route, so the whole console surface of
# migration 323 was dead while its suite was green.
#
# So the parcours is walked here, over HTTP, with real identities and real
# grants on a real database: the only shape of test that could have caught it.
#
# WHY A SMALL APP AND NOT `build_asgi_app()`. Two reasons, both measured. The
# mount of these routes into the router is already asserted against the real
# route tables (`tests/core/test_render_share_retirement.py`), so composing the
# whole application would re-prove that and cost 30-45 s per test. And the walk
# needs FOUR different callers; the composed app's static-token auth carries one
# subject. `_check_auth` is replaced by a header read, and nothing else is: the
# capability resolution, the SQL, the RLS floor, the ceremony and the posture
# all run for real.
# ═══════════════════════════════════════════════════════════════════════════════

_IDENTITY_HEADER = "x-walk-identity"


class _Walk:
    """One console, four callers, one Render."""

    def __init__(self, client, conn, chain, people):
        self.client = client
        self.conn = conn
        self.chain = chain
        self.people = people

    def _headers(self, who: str, key: str | None = None) -> dict[str, str]:
        headers = {_IDENTITY_HEADER: self.people[who]}
        if key:
            headers["Idempotency-Key"] = key
        return headers

    def list(self, who: str):
        return self.client.get(
            f"/api/projects/{self.chain.project_id}/renders/"
            f"{self.chain.render_id}/shares",
            headers=self._headers(who),
        )

    def request_share(self, who: str, *, key: str, expires_at: str | None = None):
        return self.client.post(
            f"/api/projects/{self.chain.project_id}/renders/"
            f"{self.chain.render_id}/shares",
            headers=self._headers(who, key),
            json={
                "expires_at": expires_at
                or (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
            },
        )

    def confirm(self, who: str, share_id: str, *, key: str):
        return self.client.post(
            f"/api/projects/{self.chain.project_id}/renders/shares/"
            f"{share_id}/confirmation",
            headers=self._headers(who, key),
            json={},
        )

    def revoke(self, who: str, share_id: str, *, key: str):
        return self.client.delete(
            f"/api/projects/{self.chain.project_id}/renders/shares/{share_id}",
            headers=self._headers(who, key),
        )

    def set_posture(self, who: str, state: str, *, key: str):
        return self.client.put(
            f"/api/projects/{self.chain.project_id}/settings/external-sharing",
            headers=self._headers(who, key),
            json={"external_sharing": state},
        )


@pytest.fixture()
def walk(live_postgres, monkeypatch):
    """A Render, four people, and the console routes mounted over them.

    THE FOUR PEOPLE, and each one exists to make a refusal fall the right way:

      * `reader`    org `viewer` + a `view` grant  -- lists, confirms nothing
      * `author`    org `member` + an `edit` grant -- requests a share
      * `second`    org `member` + an `edit` grant -- the SECOND holder
      * `manager`   org `admin`  + a `manage` grant -- revokes, flips the switch

    Only `owner` has a role floor; every other role needs an exact
    `app.resource_grants` row for the scope, and the effective capability is the
    MINIMUM of the role's and the grant's. A caller with neither is refused 404,
    not 403, because a project you may not see must not be distinguishable from
    one that does not exist.

    THE SEED IS COMMITTED, and it has to be: the handlers open their own
    connections through `core.db`, so an uncommitted fixture is invisible to
    them. The rows stay on the disposable base afterwards -- a share row can
    never be deleted (`trg_render_shares_no_delete`), which is the product
    decision, not an oversight -- and every id is a fresh ULID.
    """
    from core.project_settings_api import _put_external_sharing  # noqa: SLF001
    from core.render_shares_console_api import render_share_console_routes
    from starlette.applications import Starlette
    from starlette.routing import Route
    from starlette.testclient import TestClient

    from tests.integration.test_render_shares_postgres import Chain

    dsn = os.environ.get("TEST_POSTGRES_DSN") or os.environ["PLATFORM_DB_URL"]
    monkeypatch.setenv("PLATFORM_DB_URL", dsn)
    # NOT `disabled`: that mode grants every capability to `anonymous` and the
    # strict resolver never runs, so the walk would prove nothing about who may
    # do what.
    monkeypatch.setenv("TOOROW_AUTH_MODE", "oauth")
    monkeypatch.setenv(
        "TOOROW_RENDER_SHARE_PEPPER", "render-share-walk-pepper-0123456789abcd"
    )
    monkeypatch.setenv("TOOROW_RENDER_SHARE_ORIGIN", "https://share.example.com")

    chain = Chain(live_postgres).build()
    people = {name: f"person_{ULID()}" for name in ("reader", "author", "second", "manager")}
    seats = {
        "reader": ("viewer", "view"),
        "author": ("member", "edit"),
        "second": ("member", "edit"),
        "manager": ("admin", "manage"),
    }
    with live_postgres.cursor() as cur:
        for name, (role, capability) in seats.items():
            cur.execute(
                "INSERT INTO app.org_members (id, org_id, identity, role, status, "
                "joined_at) VALUES (%s, %s, %s, %s, 'active', NOW())",
                (_uid("omem"), chain.org_id, people[name], role),
            )
            cur.execute(
                "INSERT INTO app.resource_grants (id, org_id, identity, scope_type, "
                "scope_id, capability, granted_by) "
                "VALUES (%s, %s, %s, 'project', %s, %s, 'pytest')",
                (
                    _uid("rg"),
                    chain.org_id,
                    people[name],
                    chain.project_id,
                    capability,
                ),
            )
    live_postgres.commit()

    async def _identity_from_header(request):
        """The ONLY thing replaced. Token verification is proved elsewhere; what
        this walk needs is four callers on one app."""
        identity = request.headers.get(_IDENTITY_HEADER)
        return (True, identity) if identity else (False, "")

    monkeypatch.setattr("core.admin_api._check_auth", _identity_from_header)

    routes = [
        *render_share_console_routes,
        Route(
            "/api/projects/{project_id}/settings/external-sharing",
            endpoint=_put_external_sharing,
            methods=["PUT"],
        ),
    ]
    client = TestClient(Starlette(routes=routes), raise_server_exceptions=False)
    yield _Walk(client, live_postgres, chain, people)


def test_no_console_route_answers_500_and_a_viewer_can_still_list(walk):
    """R1/R2, the regression itself: the listing 500'd for an AUTHORIZED viewer.

    Not because listing needs `edit` -- it needs `view` -- but because the
    handler asks a SECOND question to fill `may_confirm`, and that question was
    spelled in a vocabulary the access seam does not know. A reader who may
    legitimately read the page could not open it.
    """
    listing = walk.list("reader")
    assert listing.status_code == 200, listing.text
    body = listing.json()
    assert body["shares"] == []
    assert body["external_sharing"] == "allowed"
    assert body["external_sharing_gesture"]

    # And the second question is answered, not raised: a `view` holder may not
    # be the second role holder of anything.
    requested = walk.request_share("author", key=_uid("req"))
    assert requested.status_code == 201, requested.text
    reader_view = walk.list("reader").json()["shares"]
    assert [row["can_confirm"] for row in reader_view] == [False]
    editor_view = walk.list("second").json()["shares"]
    assert [row["can_confirm"] for row in editor_view] == [True]


def test_a_caller_with_no_grant_meets_the_non_disclosing_refusal(walk):
    """Not a 500, and not a 403 either: a project you may not see reads as absent."""
    walk.people["stranger"] = f"person_{ULID()}"
    refused = walk.list("stranger")
    assert refused.status_code == 404, refused.text
    assert refused.json()["code"] == "not_found"


def test_the_two_person_ceremony_walked_over_http(walk):
    """Request, self-confirmation refused, second holder confirms, manager revokes.

    Every status code below is the one the console actually returns; before this
    repair the first call of this test answered 500 and the rest were unreachable.
    """
    created = walk.request_share("author", key=_uid("req"))
    assert created.status_code == 201, created.text
    share = created.json()
    assert share["state"] == "pending_confirmation"
    # NOTHING to leak: no bearer exists until a second person authorizes the exit.
    assert "delivery_url" not in share

    self_confirm = walk.confirm("author", share["share_id"], key=_uid("conf"))
    assert self_confirm.status_code == 422, self_confirm.text
    assert self_confirm.json()["code"] == "second_role_holder_required"

    reader_confirm = walk.confirm("reader", share["share_id"], key=_uid("conf"))
    assert reader_confirm.status_code == 404, "a `view` holder confirmed an exit"

    confirmed = walk.confirm("second", share["share_id"], key=_uid("conf"))
    assert confirmed.status_code == 201, confirmed.text
    live = confirmed.json()
    assert live["state"] == "active"
    assert live["delivery_url"].startswith("https://share.example.com")
    assert live["delivery_url_shown_once"] is True

    # `manage` to cut it off. An editor may hand a link out and may not take it back.
    editor_revoke = walk.revoke("author", share["share_id"], key=_uid("rev"))
    assert editor_revoke.status_code == 404, editor_revoke.text
    revoked = walk.revoke("manager", share["share_id"], key=_uid("rev"))
    assert revoked.status_code == 200, revoked.text
    assert revoked.json()["state"] == "revoked"


def test_switching_the_exit_off_refuses_both_doors_and_freezes_the_pending_one(walk):
    """R3: the posture is read at CONFIRMATION time, because that is the exit.

    Replayed before the repair: request while allowed, switch to forbidden,
    second holder confirms -> 201 with a LIVE delivery URL over a project that
    forbids external sharing. The switch stopped the door nobody was standing at.
    """
    pending = walk.request_share("author", key=_uid("req")).json()

    switched = walk.set_posture("manager", "forbidden", key=_uid("sw"))
    assert switched.status_code == 200, switched.text

    refused_request = walk.request_share("author", key=_uid("req"))
    assert refused_request.status_code == 422, refused_request.text
    assert refused_request.json()["code"] == "external_sharing_forbidden"
    assert "Manage role" in refused_request.json()["gesture"]

    refused_confirm = walk.confirm("second", pending["share_id"], key=_uid("conf"))
    assert refused_confirm.status_code == 422, refused_confirm.text
    assert refused_confirm.json()["code"] == "external_sharing_forbidden"
    # The refusal names the gesture that repairs it, not the column that caused it.
    assert "Manage role" in refused_confirm.json()["gesture"]
    assert "Project settings" in refused_confirm.json()["message"]

    # FROZEN, not destroyed: the request is still pending and still visible.
    rows = {row["share_id"]: row for row in walk.list("second").json()["shares"]}
    assert rows[pending["share_id"]]["state"] == "pending_confirmation"

    # Re-allowed, the same request is confirmable again -- the window has not closed.
    walk.set_posture("manager", "allowed", key=_uid("sw"))
    confirmed = walk.confirm("second", pending["share_id"], key=_uid("conf"))
    assert confirmed.status_code == 201, confirmed.text
    assert confirmed.json()["delivery_url"]


def test_only_a_manage_holder_may_flip_the_switch(walk):
    """The posture is an administrative act, the same rank as revoking a link."""
    for who in ("reader", "author"):
        refused = walk.set_posture(who, "forbidden", key=_uid("sw"))
        assert refused.status_code == 404, (who, refused.text)
    allowed = walk.set_posture("manager", "forbidden", key=_uid("sw"))
    assert allowed.status_code == 200, allowed.text
    assert walk.list("reader").json()["external_sharing"] == "forbidden"


def test_a_revoke_that_matched_nothing_says_so_instead_of_claiming_revoked(walk):
    """Re-review residue 1: `share_not_revocable` had zero coverage.

    Three shapes, because the console and an API caller reach different ones:
    an unknown id is 404 for everyone; a SECOND revoke under a DIFFERENT
    idempotency key is 404 (nothing matched -- the row is already revoked);
    a second revoke under the SAME key -- what the console sends, since its
    default key is constant per share -- is the operation REPLAY and answers
    200 `revoked`, which is true and idempotent, not a lie.
    """
    walk.set_posture("manager", "allowed", key=_uid("pos"))
    created = walk.request_share("author", key=_uid("req")).json()
    share_id = created["share_id"]

    unknown = walk.revoke("manager", "rshare_01UNKNOWNEXAMPLE0000000000", key=_uid("rev"))
    assert unknown.status_code == 404
    assert unknown.json()["code"] == "share_not_revocable"

    same_key = _uid("rev")
    first = walk.revoke("manager", share_id, key=same_key)
    assert first.status_code == 200 and first.json()["state"] == "revoked"

    replay = walk.revoke("manager", share_id, key=same_key)
    assert replay.status_code == 200, (
        "the console's key is constant per share: a replay answers the stored"
        " outcome, not a refusal"
    )

    fresh_key = walk.revoke("manager", share_id, key=_uid("rev"))
    assert fresh_key.status_code == 404
    assert fresh_key.json()["code"] == "share_not_revocable"
