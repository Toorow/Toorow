"""Story 50.7 -- the CONSOLE side of Render Sharing: authenticated, Project-scoped.

WHY THIS IS A SEPARATE MODULE FROM `render_shares_api.py`, and it is not tidiness.
AC6 layer 3 requires the PUBLIC module's transitive imports to exclude
`core.query_specs`, `core.query_execution`, `core.warehouse`, `core.reports`,
`core.cards` and `core.main`. The console handlers below need
`core.query_specs_api` for `analyze_connection` -- and that module
imports `core.query_specs` and `core.query_execution` at module scope. Keeping both
halves in one file would therefore make the public module's import set contain the
whole analytical service, and the import-boundary test would either fail or have to
be weakened until it proved nothing.

Two modules, two import sets, one assertion that still means something.

WHAT THIS SIDE MAY DO that the public side may not: read a `project_id` and a
`render_id` from the request path, because the caller here HAS an identity and a
Project role. The public side derives every identity from a session cookie.

THE AUTHORITY SPLIT IS PRESERVED, not invented: `edit` to create a Share, `manage`
to revoke one. That is what `rendus_api._authorize_snapshot_project` enforces for
the retired snapshot shares, and the same asymmetry is right here -- handing out a
public link is an ordinary editorial act, and cutting one off is an administrative
one that a recipient cannot undo.

AND SINCE MIGRATION 323, `edit` HELD BY SOMEBODY ELSE to confirm it.
`proactive-assertions.md` decision 2 -- "external sharing requires a
project-scoped capability plus confirmation by a *second* role holder" -- lands
on these four routes as: the POST REQUESTS a Share and receives no link; a second
`edit` holder POSTs its `/confirmation` and receives the only link there will
ever be; and BOTH DOORS THAT LET SOMETHING OUT -- the request and the
confirmation -- refuse first when the Project's `external_sharing` posture
forbids the exit, with a refusal that names who can change it and where.

BOTH, and it was one until 2026-08-30. The request read the posture; the
confirmation did not, so a Project could allow the exit, receive a request,
switch back to `forbidden`, and still have a live delivery URL minted afterwards
-- the switch guarded the door nobody was standing at. The exit happens when the
BEARER IS MINTED, which is the confirmation. Listing and revoking are not doors
out: one reports the posture so the screen never discovers it by being refused,
and the other CLOSES an exit, which a forbidding Project can only want.

WHAT IS NEVER RETURNED HERE: a bearer, a delivery URL on any path but the
confirmation, or any value from which either could be rebuilt. The retired listing returned
`share_token` in every row and argued in its docstring
(`snapshot_shares.py:167-170`) that this was acceptable because the caller is
authenticated. It is not: it turns every console screenshot, browser cache entry and
support-ticket paste into a live public grant.
"""

from __future__ import annotations

import logging
from datetime import datetime

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from core.project_external_sharing import (
    ENABLE_GESTURE,
    ExternalSharingForbidden,
    read_external_sharing,
)
from core.render_shares import (
    RenderShareConfirmationRefused,
    RenderShareValidationError,
    ShareNotRevocable,
    confirm_share,
    confirmation_window_hours,
    create_share,
    list_shares,
    max_share_lifetime_days,
    revoke_share,
)

logger = logging.getLogger(__name__)

_CONSOLE_BASE = "/api/projects/{project_id}/renders"


async def _authorize(request: Request, minimum_capability: str):
    """Return `(identity, org_id)` or the Response that refuses.

    THE VOCABULARY IS THE CAPABILITY ONE -- `view`, `edit`, `manage` -- and this
    function is the reason the four routes below answer at all. It used to
    delegate to `query_specs_api._authorize`, which takes a ROLE and hands it to
    `project_access.identity_has_project_role`; that function reads
    `project_access._ROLE_CAPABILITY`, whose keys are `viewer`, `member`,
    `admin`, `owner`. So `_authorize(request, "edit")` and
    `_authorize(request, "manage")` raised `ValueError: unknown project role`
    inside the handler and every one of these four routes answered 500 --
    including the LISTING, which needs no `edit` at all but asks the question to
    fill `may_confirm`.

    WHY THE CAPABILITY DOOR AND NOT THE ROLE NAMES. Passing `"member"` and
    `"admin"` would have compiled, and it would have put a second translation
    table between the ratified floor and the check:

      * the floors are RATIFIED in capability words. `proactive-assertions.md`
        decision 2 and `project-settings.md` both say `edit` and `manage`; a
        handler spelling them `member` and `admin` makes the document and the
        code two vocabularies that have to be kept in step by hand.
      * `manage` has no single role. `_ROLE_CAPABILITY` maps BOTH `admin` and
        `owner` onto it, and the inverse table `_CAPABILITY_ROLE` picks `owner`
        -- so a round trip through role names is lossy in the direction that
        matters, and one spelling of the revoke floor would silently exclude the
        admins who hold it.
      * the nearest neighbour is `project_settings_api._authorize`, which owns
        the very switch this family consults (`external_sharing`) and resolves
        it through `_strict_project_capability_allowed` with exactly these three
        words. Two doors onto one ceremony is how the two halves drift apart.
      * `query_specs_api._authorize` resolves through
        `admin_api#_require_datastream_role`, a DATASTREAM-surface guard that
        also decides the stream/project pair. These routes are Render-scoped and
        name no Datastream, so they were borrowing a guard for a question they
        do not ask.

    `hold_access=True` on `manage` for the same reason `project_settings_api`
    does it: revoking a live link and moving the project's posture are the same
    administrative rank, and both take the access rows under a share lock so the
    decision cannot be overtaken by a concurrent revocation of the grant itself.

    The refusal is the shared non-disclosing 404: a project the caller may not
    see must not be distinguishable from one that does not exist.
    """
    # Imported inside the function, not at module scope: `core.admin_api`
    # imports THIS module to splice `render_share_console_routes` into its
    # router, so a top-level import here is a cycle.
    from core.admin_api import (  # noqa: PLC0415
        _check_auth,
        _project_not_found_response,
        _strict_project_capability_allowed,
    )
    from core.db import get_connection  # noqa: PLC0415

    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Authentication required"}, status_code=401
        )
    actor = identity or "anonymous"
    project_id = request.path_params["project_id"]
    with get_connection() as conn:
        if not _strict_project_capability_allowed(
            conn,
            identity=actor,
            project_id=project_id,
            minimum_capability=minimum_capability,
            hold_access=minimum_capability == "manage",
        ):
            return _project_not_found_response()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT org_id FROM app.projects WHERE id = %s AND status = 'active'",
                (project_id,),
            )
            row = cur.fetchone()
    if row is None:
        return _project_not_found_response()
    return str(actor), str(row[0])


def _connection(identity: str):
    """The Analyze connection, with the Epic-36 RLS floor armed ON that connection.

    Every Analyze handler takes its connection from `analyze_connection()` rather
    than a bare `get_connection()`; arming a different connection would protect
    nothing, because `set_config(..., true)` is local to the transaction it ran on.
    """
    from core.query_specs_api import analyze_connection  # noqa: PLC0415

    return analyze_connection(identity)


async def _list_render_shares(request: Request) -> Response:
    authorized = await _authorize(request, "view")
    if isinstance(authorized, Response):
        return authorized
    identity, org_id = authorized
    project_id = request.path_params["project_id"]
    render_id = request.path_params["render_id"]
    # The listing floor is `viewer`; CONFIRMING is an `edit` act, so whether this
    # reader could be the second role holder is a second question with a second
    # answer. A refusal here is not an error -- it is the answer "no".
    may_confirm = not isinstance(await _authorize(request, "edit"), Response)
    with _connection(identity) as conn:
        shares = list_shares(
            conn,
            org_id=org_id,
            project_id=project_id,
            render_id=render_id,
            viewer=identity,
            viewer_may_confirm=may_confirm,
        )
        posture = read_external_sharing(conn, project_id=project_id)
    return JSONResponse(
        {
            "shares": shares,
            "max_lifetime_days": max_share_lifetime_days(),
            "confirmation_window_hours": confirmation_window_hours(),
            # Stated by the server, so the screen never has to discover the
            # project's posture by getting a create refused.
            "external_sharing": posture["state"],
            "external_sharing_decided_by": posture["decided_by"],
            "external_sharing_gesture": ENABLE_GESTURE,
        }
    )


async def _create_render_share(request: Request) -> Response:
    authorized = await _authorize(request, "edit")
    if isinstance(authorized, Response):
        return authorized
    identity, org_id = authorized
    project_id = request.path_params["project_id"]
    render_id = request.path_params["render_id"]

    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}

    raw_expiry = body.get("expires_at")
    if not isinstance(raw_expiry, str) or not raw_expiry:
        # Expiry is REQUIRED, and the refusal says so rather than quietly choosing
        # one. AD-20 calls a Share "revocable, expiring and audited"; the retired
        # table had no expiry column at all, which is why every legacy link is
        # unexpiring today.
        return JSONResponse(
            {
                "code": "expiry_required",
                "message": (
                    "A share must expire. Choose an expiry within "
                    f"{max_share_lifetime_days()} days."
                ),
            },
            status_code=422,
        )
    try:
        expires_at = datetime.fromisoformat(raw_expiry.replace("Z", "+00:00"))
    except ValueError:
        return JSONResponse(
            {"code": "expiry_invalid", "message": "expires_at must be an ISO 8601 timestamp."},
            status_code=422,
        )

    idempotency_key = request.headers.get("idempotency-key")
    if not idempotency_key:
        idempotency_key = f"render-share:{render_id}:{expires_at.isoformat()}"

    try:
        with _connection(identity) as conn:
            created = create_share(
                conn,
                org_id=org_id,
                project_id=project_id,
                render_id=render_id,
                actor=identity,
                expires_at=expires_at,
                idempotency_key=idempotency_key,
            )
            conn.commit()
    except ExternalSharingForbidden as exc:
        # The refusal names the gesture and who can make it, because the thing
        # blocking this person is a decision somebody else has to take.
        return JSONResponse(
            {"code": exc.code, "message": str(exc), "gesture": ENABLE_GESTURE},
            status_code=422,
        )
    except RenderShareValidationError as exc:
        return JSONResponse({"code": "share_refused", "message": str(exc)}, status_code=422)

    # NO `delivery_url`, AND THAT IS THE POINT. Until a second role holder
    # confirms, no bearer exists at all -- so there is nothing here to leak, to
    # copy, or to send ahead of the authorization.
    return JSONResponse(
        {
            "share_id": created.share_id,
            "render_id": created.render_id,
            "state": created.state,
            "expires_at": created.expires_at.isoformat(),
            "confirmation_requested_by": created.confirmation_requested_by,
            "confirmation_expires_at": created.confirmation_expires_at.isoformat(),
        },
        status_code=201,
    )


async def _confirm_render_share(request: Request) -> Response:
    """The second role holder authorizes the exit, and receives the only link.

    `edit`, the SAME authority the request needed. The rule decision 2 states is
    "a second role holder", not "a higher one": escalating to `manage` here would
    quietly turn a two-person rule into an approval hierarchy, which is a
    different product decision nobody has taken.

    AND THE POSTURE IS READ AGAIN HERE, not only at request time. The exit
    happens when the bearer is minted, which is on THIS route -- so a project
    switched to `forbidden` after a request was filed refuses the confirmation
    and no link is ever minted. `render_shares.confirm_share` owns that check on
    the same connection as the transition; this handler only turns it into the
    refusal that names the gesture.
    """
    authorized = await _authorize(request, "edit")
    if isinstance(authorized, Response):
        return authorized
    identity, org_id = authorized
    project_id = request.path_params["project_id"]
    share_id = request.path_params["share_id"]
    idempotency_key = (
        request.headers.get("idempotency-key") or f"confirm-render-share:{share_id}"
    )
    try:
        with _connection(identity) as conn:
            confirmed = confirm_share(
                conn,
                org_id=org_id,
                project_id=project_id,
                share_id=share_id,
                actor=identity,
                idempotency_key=idempotency_key,
            )
            conn.commit()
    except ExternalSharingForbidden as exc:
        # The same refusal the request door gives, in the same words, because a
        # person who filed a request yesterday and is confirming today must not
        # meet a different sentence for the same decision.
        return JSONResponse(
            {"code": exc.code, "message": str(exc), "gesture": ENABLE_GESTURE},
            status_code=422,
        )
    except RenderShareConfirmationRefused as exc:
        return JSONResponse({"code": exc.code, "message": str(exc)}, status_code=422)
    except RenderShareValidationError as exc:
        return JSONResponse({"code": "share_refused", "message": str(exc)}, status_code=422)

    return JSONResponse(
        {
            "share_id": confirmed.share_id,
            "render_id": confirmed.render_id,
            "state": confirmed.state,
            "expires_at": confirmed.expires_at.isoformat(),
            # Returned ONCE, here, to the person who authorized the exit. Only the
            # bearer's HMAC was stored, so this URL is not re-derivable by anyone
            # -- including this server. The console must say so where it shows it.
            "delivery_url": confirmed.delivery_url,
            "delivery_url_shown_once": True,
        },
        status_code=201,
    )


async def _revoke_render_share(request: Request) -> Response:
    authorized = await _authorize(request, "manage")
    if isinstance(authorized, Response):
        return authorized
    identity, org_id = authorized
    project_id = request.path_params["project_id"]
    share_id = request.path_params["share_id"]
    idempotency_key = request.headers.get("idempotency-key") or f"revoke-render-share:{share_id}"
    try:
        with _connection(identity) as conn:
            revoke_share(
                conn,
                org_id=org_id,
                project_id=project_id,
                share_id=share_id,
                actor=identity,
                idempotency_key=idempotency_key,
            )
            conn.commit()
    except ShareNotRevocable as exc:
        return JSONResponse(
            {"code": "share_not_revocable", "message": str(exc)}, status_code=404
        )
    # The row is not deleted and never will be: revocation keeps its full history,
    # which is the only durable proof that a public grant existed and was closed.
    return JSONResponse({"share_id": share_id, "state": "revoked"})


_DOSSIER_BASE = "/api/projects/{project_id}/analyze/dossiers"


async def _list_dossier_shares(request: Request) -> Response:
    """GET .../analyze/dossiers/{id}/shares -- every grant over any version."""
    authorized = await _authorize(request, "view")
    if isinstance(authorized, Response):
        return authorized
    identity, org_id = authorized
    project_id = request.path_params["project_id"]
    dossier_id = request.path_params["dossier_id"]
    may_confirm = not isinstance(await _authorize(request, "edit"), Response)
    with _connection(identity) as conn:
        shares = list_shares(
            conn,
            org_id=org_id,
            project_id=project_id,
            dossier_id=dossier_id,
            viewer=identity,
            viewer_may_confirm=may_confirm,
        )
        posture = read_external_sharing(conn, project_id=project_id)
    return JSONResponse(
        {
            "shares": shares,
            "max_lifetime_days": max_share_lifetime_days(),
            "confirmation_window_hours": confirmation_window_hours(),
            "external_sharing": posture["state"],
            "external_sharing_decided_by": posture["decided_by"],
            "external_sharing_gesture": ENABLE_GESTURE,
        }
    )


async def _create_dossier_share(request: Request) -> Response:
    """POST .../analyze/dossiers/{id}/shares -- request a grant over the
    CURRENT version, pinned now so a later edit cannot change what the second
    role holder confirmed. The confirmation and revocation doors are the same
    as a single-Render share's -- one sharing system, two targets (73-2)."""
    authorized = await _authorize(request, "edit")
    if isinstance(authorized, Response):
        return authorized
    identity, org_id = authorized
    project_id = request.path_params["project_id"]
    dossier_id = request.path_params["dossier_id"]

    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}

    raw_expiry = body.get("expires_at")
    if not isinstance(raw_expiry, str) or not raw_expiry:
        return JSONResponse(
            {
                "code": "expiry_required",
                "message": (
                    "A share must expire. Choose an expiry within "
                    f"{max_share_lifetime_days()} days."
                ),
            },
            status_code=422,
        )
    try:
        expires_at = datetime.fromisoformat(raw_expiry.replace("Z", "+00:00"))
    except ValueError:
        return JSONResponse(
            {"code": "expiry_invalid", "message": "expires_at must be an ISO 8601 timestamp."},
            status_code=422,
        )

    with _connection(identity) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT current_version_id FROM app.analysis_dossiers "
                "WHERE id = %s AND org_id = %s AND project_id = %s AND archived_at IS NULL",
                (dossier_id, org_id, project_id),
            )
            row = cur.fetchone()
    if row is None or not row[0]:
        return JSONResponse(
            {"code": "not_found", "message": "No such Dossier, or it has no version yet."},
            status_code=404,
        )
    dossier_version_id = str(row[0])

    idempotency_key = request.headers.get("idempotency-key")
    if not idempotency_key:
        idempotency_key = f"dossier-share:{dossier_version_id}:{expires_at.isoformat()}"

    try:
        with _connection(identity) as conn:
            created = create_share(
                conn,
                org_id=org_id,
                project_id=project_id,
                dossier_version_id=dossier_version_id,
                actor=identity,
                expires_at=expires_at,
                idempotency_key=idempotency_key,
            )
            conn.commit()
    except ExternalSharingForbidden as exc:
        return JSONResponse(
            {"code": exc.code, "message": str(exc), "gesture": ENABLE_GESTURE},
            status_code=422,
        )
    except RenderShareValidationError as exc:
        return JSONResponse({"code": "share_refused", "message": str(exc)}, status_code=422)

    return JSONResponse(
        {
            "share_id": created.share_id,
            "dossier_id": dossier_id,
            "dossier_version_id": created.dossier_version_id,
            "state": created.state,
            "expires_at": created.expires_at.isoformat(),
            "confirmation_requested_by": created.confirmation_requested_by,
            "confirmation_expires_at": created.confirmation_expires_at.isoformat(),
        },
        status_code=201,
    )



#: Literal `shares` before the parameterized `{share_id}`, per the ordering rule
#: `query_specs_api.py:433-452` states is load-bearing.
render_share_console_routes: list[Route] = [
    Route(
        f"{_CONSOLE_BASE}/shares/{{share_id}}/confirmation",
        endpoint=_confirm_render_share,
        methods=["POST"],
    ),
    Route(
        f"{_CONSOLE_BASE}/shares/{{share_id}}",
        endpoint=_revoke_render_share,
        methods=["DELETE"],
    ),
    Route(f"{_CONSOLE_BASE}/{{render_id}}/shares", endpoint=_list_render_shares, methods=["GET"]),
    Route(f"{_CONSOLE_BASE}/{{render_id}}/shares", endpoint=_create_render_share, methods=["POST"]),
    # 73-2: the same sharing system, aimed at a Dossier version.
    Route(
        f"{_DOSSIER_BASE}/{{dossier_id}}/shares",
        endpoint=_list_dossier_shares,
        methods=["GET"],
    ),
    Route(
        f"{_DOSSIER_BASE}/{{dossier_id}}/shares",
        endpoint=_create_dossier_share,
        methods=["POST"],
    ),
]

__all__ = ["render_share_console_routes"]
