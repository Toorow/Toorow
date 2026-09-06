"""toorow -- the door of the MDM common key (story 66.1).

Exports MDM_COMMON_KEY_ROUTES: list[Route] -- a flat list `admin_api.py` splices
into its router at startup. Never imported by `admin_api` at module level, the
pattern of `mdm_canonical_fields_api.py` and `cleanup_rules_api.py`.

  GET    /api/projects/{project_id}/mdm/common-keys
  POST   /api/projects/{project_id}/mdm/common-keys
  GET    /api/projects/{project_id}/mdm/common-keys/{common_key_id}
  POST   /api/projects/{project_id}/mdm/common-keys/{common_key_id}/versions
  DELETE /api/projects/{project_id}/mdm/common-keys/{common_key_id}

THE ORDER OF DECLARATION IS PART OF THE CONTRACT. Starlette resolves in order,
so `/common-keys` is declared before `/common-keys/{common_key_id}` and the
`/versions` segment before nothing else could swallow it.

THREE ANSWERS THAT ARE NOT THE SAME, and this module keeps them apart:

  200 + `empty_reason`   nobody has declared a key yet. A fact about the
                         vocabulary, not about the address -- a 404 here would
                         tell a person the page does not exist.
  404                    foreign, denied or nonexistent, indistinguishably. A
                         caller of another org must not learn an id exists.
  503                    the store could not be read. "I could not look" is not
                         "there is nothing", and only one of the two invites
                         somebody to start declaring.

REFUSALS ARE 422 WITH THEIR CODE. `component_is_metric` and
`component_not_keyable:money` are sentences a screen can render beside the
component that caused them; a flat 400 would send a person back to guess which
of eight ids was wrong.
"""

from __future__ import annotations

import logging

from ulid import ULID

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

logger = logging.getLogger(__name__)

_BASE = "/api/projects/{project_id}/mdm/common-keys"
_ONE = _BASE + "/{common_key_id}"
_VERSIONS = _ONE + "/versions"

#: The empty answer's reason, in the vocabulary of the person reading it: it
#: names the gesture, not a table and not a deployment state.
_EMPTY_REASON = {
    "code": "no_common_key_declared",
    "message": (
        "No common key has been declared yet. A common key names the business "
        "identity two sources share -- a day, a campaign, a market -- and it is "
        "declared from the canonical fields this Project already uses."
    ),
}


async def _check_auth(request: Request) -> tuple[bool, str]:
    from core.admin_api import _check_auth as _admin_check_auth  # noqa: PLC0415

    return await _admin_check_auth(request)


def _unauthorized() -> Response:
    return JSONResponse(
        {"code": "unauthorized", "message": "Authentication is required."}, status_code=401
    )


def _not_found() -> Response:
    return JSONResponse({"code": "not_found", "message": "Not found."}, status_code=404)


def _refused(exc) -> Response:
    return JSONResponse({"code": exc.code, "message": exc.message}, status_code=422)


def _unavailable(what: str) -> Response:
    return JSONResponse(
        {
            "code": "common_keys_unavailable",
            "message": (
                f"The {what} could not be read, so what this Project declares is "
                "unknown. This is not a count of zero."
            ),
        },
        status_code=503,
    )


def _guard(
    project_id: str, identity: str, minimum_capability: str = "view"
) -> tuple[str | None, Response | None]:
    """Resolve the org of *project_id* and verify the caller may read it.

    A project the guarded org does not own answers 404 and never 403 -- the same
    posture `mdm_canonical_fields_api._guard` applies, for the same reason.
    """
    from core.project_access import resolve_strict_resource_access  # noqa: PLC0415
    from core.query_specs_api import analyze_connection  # noqa: PLC0415

    try:
        with analyze_connection(identity) as conn:
            decision = resolve_strict_resource_access(
                identity,
                conn,
                project_id=project_id,
                minimum_capability=minimum_capability,
                hold_access=minimum_capability == "edit",
            )
    except Exception as exc:  # noqa: BLE001
        logger.error("mdm_common_keys_api: guard failed project=%s: %s", project_id, exc)
        return None, JSONResponse(
            {"code": "server_error", "message": "Server error."}, status_code=500
        )

    if not decision.allowed or not decision.org_id:
        return None, _not_found()
    return str(decision.org_id), None


async def _read_body(request: Request) -> dict:
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return {}
    return body if isinstance(body, dict) else {}


async def _list_keys(request: Request) -> Response:
    """GET {base} -- every common key of the project, current version included."""
    authorized, identity = await _check_auth(request)
    if not authorized:
        return _unauthorized()
    project_id = request.path_params["project_id"]
    org_id, refusal = _guard(project_id, identity)
    if refusal is not None:
        return refusal

    from core.mdm_common_keys import list_common_keys  # noqa: PLC0415
    from core.query_specs_api import analyze_connection  # noqa: PLC0415

    try:
        with analyze_connection(identity) as conn:
            keys = list_common_keys(conn, project_id=project_id)
    except Exception as exc:  # noqa: BLE001
        logger.error("mdm_common_keys_api: list failed project=%s: %s", project_id, exc)
        return _unavailable("common keys of this Project")

    return JSONResponse(
        {
            "project_id": project_id,
            "organization_id": org_id,
            "common_keys": keys,
            "empty_reason": _EMPTY_REASON if not keys else None,
        }
    )


async def _proposals(request: Request) -> Response:
    """GET {base}/proposals -- the identities the Project's flows share, project-wide.

    Derived from every current mapping version in one read, never stored; names
    the carriers, the ones already pinned, the ones still to pin and the
    canonical field proposed (`governance.md`, amendment of 2026-09-04).
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return _unauthorized()
    project_id = request.path_params["project_id"]
    org_id, refusal = _guard(project_id, identity)
    if refusal is not None:
        return refusal
    from core.mdm_common_keys import propose_shared_identities  # noqa: PLC0415
    from core.query_specs_api import analyze_connection  # noqa: PLC0415

    try:
        with analyze_connection(identity) as conn:
            proposed = propose_shared_identities(conn, project_id=project_id)
    except Exception as exc:  # noqa: BLE001
        logger.error("mdm_common_keys_api: proposals failed project=%s: %s", project_id, exc)
        return _unavailable("shared identities of this Project")
    empty_reason = None
    if not proposed["proposals"]:
        empty_reason = {
            "code": "no_shared_identity",
            "message": (
                "No published flow carries a dimension another flow shares, nor a measure "
                "to govern, yet. Publish a flow with a mapping, then come back."
            ),
        }
    return JSONResponse({"project_id": project_id, "organization_id": org_id, **proposed, "empty_reason": empty_reason})


async def _pin_proposal(request: Request) -> Response:
    """POST {base}/proposals/pin -- pin the named columns of the named flows to a canonical field.

    The console door onto the function the MCP tool `publish_shared_identity`
    calls (`governance.md`, 2026-09-05): one governed mapping change per flow,
    binding-only, published as an overlay. Body: {canonical_field_id, carriers:
    [{datastream_id, column}], reason?}. Answers per flow -- pinned,
    already_pinned, refused -- and never a confirmation secret.
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return _unauthorized()
    project_id = request.path_params["project_id"]
    _org_id, refusal = _guard(project_id, identity, "edit")
    if refusal is not None:
        return refusal
    body = await _read_body(request)
    from core.query_specs_api import analyze_connection  # noqa: PLC0415
    from core.shared_identity_pins import SharedIdentityRefused, pin_shared_identity  # noqa: PLC0415

    idempotency_key = (request.headers.get("Idempotency-Key") or "").strip() or f"console-pin-{ULID()}"
    try:
        with analyze_connection(identity) as conn:
            pinned = pin_shared_identity(
                conn,
                project_id=project_id,
                canonical_field_id=str(body.get("canonical_field_id") or ""),
                carriers=body.get("carriers") or [],
                actor=identity,
                idempotency_key=idempotency_key,
                reason=body.get("reason"),
            )
            conn.commit()
    except SharedIdentityRefused as exc:
        return JSONResponse({"code": exc.code, "message": exc.message}, status_code=422)
    except Exception as exc:  # noqa: BLE001
        logger.error("mdm_common_keys_api: pin failed project=%s: %s", project_id, exc)
        return _unavailable("shared identities of this Project")
    return JSONResponse({"project_id": project_id, **pinned})


async def _create_key(request: Request) -> Response:
    """POST {base} -- declare a key and freeze its version 1."""
    authorized, identity = await _check_auth(request)
    if not authorized:
        return _unauthorized()
    project_id = request.path_params["project_id"]
    _org_id, refusal = _guard(project_id, identity, "edit")
    if refusal is not None:
        return refusal

    body = await _read_body(request)
    from core.mdm_common_keys import (  # noqa: PLC0415
        CommonKeyNotFound,
        CommonKeyRefused,
        create_common_key,
    )
    from core.query_specs_api import analyze_connection  # noqa: PLC0415

    try:
        with analyze_connection(identity) as conn:
            created = create_common_key(
                conn,
                project_id=project_id,
                name=body.get("name"),
                canonical_field_ids=body.get("components") or body.get("canonical_field_ids") or [],
                description=body.get("description"),
                actor=identity,
            )
            conn.commit()
    except CommonKeyRefused as exc:
        return _refused(exc)
    except CommonKeyNotFound:
        return _not_found()
    except Exception as exc:  # noqa: BLE001
        logger.error("mdm_common_keys_api: create failed project=%s: %s", project_id, exc)
        return _unavailable("common key store")

    return JSONResponse({"project_id": project_id, "common_key": created}, status_code=201)


async def _read_key(request: Request) -> Response:
    """GET {base}/{id} -- one key, its versions, its coverage and its used-by."""
    authorized, identity = await _check_auth(request)
    if not authorized:
        return _unauthorized()
    project_id = request.path_params["project_id"]
    common_key_id = request.path_params["common_key_id"]
    _org_id, refusal = _guard(project_id, identity)
    if refusal is not None:
        return refusal

    from core.mdm_common_keys import CommonKeyNotFound, read_common_key  # noqa: PLC0415
    from core.query_specs_api import analyze_connection  # noqa: PLC0415

    try:
        with analyze_connection(identity) as conn:
            key = read_common_key(conn, project_id=project_id, common_key_id=common_key_id)
    except CommonKeyNotFound:
        return _not_found()
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "mdm_common_keys_api: read failed project=%s key=%s: %s",
            project_id, common_key_id, exc,
        )
        return _unavailable("common key")

    return JSONResponse({"project_id": project_id, "common_key": key})


async def _append_version(request: Request) -> Response:
    """POST {base}/{id}/versions -- append version N+1; never edit version N."""
    authorized, identity = await _check_auth(request)
    if not authorized:
        return _unauthorized()
    project_id = request.path_params["project_id"]
    common_key_id = request.path_params["common_key_id"]
    _org_id, refusal = _guard(project_id, identity, "edit")
    if refusal is not None:
        return refusal

    body = await _read_body(request)
    from core.mdm_common_keys import (  # noqa: PLC0415
        CommonKeyNotFound,
        CommonKeyRefused,
        append_version,
    )
    from core.query_specs_api import analyze_connection  # noqa: PLC0415

    try:
        with analyze_connection(identity) as conn:
            appended = append_version(
                conn,
                project_id=project_id,
                common_key_id=common_key_id,
                canonical_field_ids=body.get("components") or body.get("canonical_field_ids") or [],
                actor=identity,
            )
            conn.commit()
    except CommonKeyRefused as exc:
        return _refused(exc)
    except CommonKeyNotFound:
        return _not_found()
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "mdm_common_keys_api: append failed project=%s key=%s: %s",
            project_id, common_key_id, exc,
        )
        return _unavailable("common key store")

    return JSONResponse({"project_id": project_id, "common_key": appended}, status_code=201)


async def _archive_key(request: Request) -> Response:
    """DELETE {base}/{id} -- archive, unless a relationship pins one of its versions."""
    authorized, identity = await _check_auth(request)
    if not authorized:
        return _unauthorized()
    project_id = request.path_params["project_id"]
    common_key_id = request.path_params["common_key_id"]
    _org_id, refusal = _guard(project_id, identity, "edit")
    if refusal is not None:
        return refusal

    from core.mdm_common_keys import (  # noqa: PLC0415
        CommonKeyNotFound,
        CommonKeyRefused,
        archive_common_key,
    )
    from core.query_specs_api import analyze_connection  # noqa: PLC0415

    try:
        with analyze_connection(identity) as conn:
            archived = archive_common_key(
                conn, project_id=project_id, common_key_id=common_key_id, actor=identity
            )
            conn.commit()
    except CommonKeyRefused as exc:
        # `common_key_in_use` is a CONFLICT, not a malformed request: the caller
        # sent something valid and the world says no. 409 is what a screen turns
        # into "retire these relationships first".
        status = 409 if exc.code == "common_key_in_use" else 422
        return JSONResponse({"code": exc.code, "message": exc.message}, status_code=status)
    except CommonKeyNotFound:
        return _not_found()
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "mdm_common_keys_api: archive failed project=%s key=%s: %s",
            project_id, common_key_id, exc,
        )
        return _unavailable("common key store")

    return JSONResponse({"project_id": project_id, "common_key": archived})


MDM_COMMON_KEY_ROUTES: list[Route] = [
    Route(_BASE, _list_keys, methods=["GET"]),
    Route(_BASE, _create_key, methods=["POST"]),
    # Before `_ONE`: `/proposals` is not a key id.
    Route(_BASE + "/proposals", _proposals, methods=["GET"]),
    Route(_BASE + "/proposals/pin", _pin_proposal, methods=["POST"]),
    # `/versions` before `/{common_key_id}` would be harmless here because the
    # methods differ, but the order is kept explicit: a later GET on /versions
    # must not be swallowed by the parameterised read.
    Route(_VERSIONS, _append_version, methods=["POST"]),
    Route(_ONE, _read_key, methods=["GET"]),
    Route(_ONE, _archive_key, methods=["DELETE"]),
]

__all__ = ["MDM_COMMON_KEY_ROUTES"]
