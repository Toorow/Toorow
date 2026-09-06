"""Story 50.3 -- the server-owned Report / Notebook / Render API.

ONE SERVICE, MANY ADAPTERS. Every handler here is a thin translation of HTTP into
`core.analyze_artifacts`. No validation, no lifecycle SQL and no outcome logic
lives in this file, so a future MCP adapter (Story 50.6) calls the same module and
reaches the same objects without a second lifecycle path (AC1, AC13). That is also
why none of this is added to `admin_api.py`, where the legacy notebook CRUD still
lives as in-file SQL.

NON-DISCLOSURE IS THE DEFAULT, and it matches Story 50.1 byte for byte:
`ArtifactNotFound` becomes the same 404 envelope whether the object is foreign,
denied or absent, and the denial path answers before any work is done so response
timing does not become an enumeration oracle.

ROUTE ORDER IS LOAD-BEARING. Literal segments are declared before parameterized
ones, so `/reports/seeds` can never be captured as a Report id and
`/notebook-runs/{id}` can never be captured by `/notebooks/{id}`. The order of
`ROUTES` at the bottom is the contract; a seam test asserts it rather than
trusting it.

WHAT THIS DELIBERATELY DOES NOT EXPOSE. There is no route that creates a share
token, and no response on any route carries a bearer. Story 50.7 owns the AD-30
fragment exchange; until it lands, `GET .../renders/{id}` returns an explicit
`canonical_share_available: false` with the reason (AC15). Creating a raw path
token here "for now" is exactly how `app.render_snapshot_shares` came to hold
plaintext bearers.
"""

from __future__ import annotations

import json

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from core.analyze_artifacts import (
    ArtifactNotFound,
    ArtifactRefused,
    archive_notebook,
    archive_report,
    classify_legacy_artifacts,
    create_notebook,
    create_notebook_version,
    create_render,
    create_report,
    create_report_version,
    get_notebook,
    get_notebook_run,
    get_render,
    get_report,
    list_legacy_notebooks,
    list_legacy_snapshots,
    list_notebooks,
    list_pinnable_presentation_versions,
    list_renders,
    list_report_seeds,
    list_reports,
    render_contract_state,
    run_notebook,
    run_report_version,
    set_notebook_schedule,
)

# The RLS floor is armed in ONE place for all four Analyze modules; see
# `core.query_specs_api.analyze_connection` for why it cannot live in `_authorize`.
from core.query_specs_api import analyze_connection

_BASE = "/api/projects/{project_id}/analyze"

#: One envelope for foreign, denied and absent.
_NOT_FOUND = {"code": "not_found", "message": "Not found"}


async def _authorize(request: Request, role: str = "viewer"):
    """Return (identity, org_id) or a Response. Denial answers before any work."""
    from core.admin_api import _check_auth, _require_datastream_role  # noqa: PLC0415
    from core.db import get_connection  # noqa: PLC0415

    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse({"code": "unauthorized", "message": "Authentication required"}, 401)
    project_id = request.path_params["project_id"]
    with get_connection() as conn:
        denied = _require_datastream_role(project_id, identity, role, conn)
        if denied is not None:
            return denied
        with conn.cursor() as cur:
            cur.execute("SELECT org_id FROM app.projects WHERE id = %s", (project_id,))
            row = cur.fetchone()
    if row is None:
        return JSONResponse(_NOT_FOUND, 404)
    return str(identity), str(row[0])


async def _json_body(request: Request) -> dict:
    raw = await request.body()
    if not raw.strip():
        return {}
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("request body must be an object")
    return value


def _refused(exc: ArtifactRefused) -> Response:
    return JSONResponse(exc.as_dict(), 422)


# ---------------------------------------------------------------------------
# Reports.
# ---------------------------------------------------------------------------


async def _list_reports(request: Request) -> Response:
    """GET {base}/reports -- configured Reports AND available seeds, kept apart.

    Two lists in one response, never merged. AC4: an enablement toggle is seed
    availability; it is not a Report and must not appear as one.
    """
    auth = await _authorize(request, "viewer")
    if isinstance(auth, Response):
        return auth
    identity, org_id = auth
    project_id = request.path_params["project_id"]

    with analyze_connection(identity) as conn:
        payload = {
            "reports": list_reports(
                conn, org_id=org_id, project_id=project_id,
                include_archived=_wants_archived(request),
            ),
            "seeds": list_report_seeds(conn, project_id=project_id),
            "presentation_contract": render_contract_state(conn),
        }
    return JSONResponse(payload)


async def _create_report(request: Request) -> Response:
    """POST {base}/reports -- create a Report head plus its version 1."""
    auth = await _authorize(request, "member")
    if isinstance(auth, Response):
        return auth
    identity, org_id = auth
    project_id = request.path_params["project_id"]
    try:
        body = await _json_body(request)
    except (ValueError, json.JSONDecodeError) as exc:
        return JSONResponse({"code": "invalid_body", "message": str(exc)}, 400)

    try:
        with analyze_connection(identity) as conn:
            created = create_report(
                conn,
                org_id=org_id,
                project_id=project_id,
                label=body.get("label") or "",
                description=body.get("description"),
                actor=identity,
                query_spec_version_id=str(body.get("query_spec_version_id") or ""),
                presentation=body.get("presentation"),
                seed_origin=str(body.get("seed_origin") or "project"),
                seed_module_name=body.get("seed_module_name"),
                seed_report_id=body.get("seed_report_id"),
            )
            conn.commit()
    except ArtifactNotFound:
        return JSONResponse(_NOT_FOUND, 404)
    except ArtifactRefused as exc:
        return _refused(exc)
    return JSONResponse(created, 201)


async def _get_report(request: Request) -> Response:
    """GET {base}/reports/{id} -- head, every immutable version, every Run."""
    auth = await _authorize(request, "viewer")
    if isinstance(auth, Response):
        return auth
    identity, org_id = auth

    try:
        with analyze_connection(identity) as conn:
            payload = get_report(
                conn,
                org_id=org_id,
                project_id=request.path_params["project_id"],
                report_id=request.path_params["report_id"],
            )
            #  Story 72.5, AC21. The Presentation tab pins a presentation version,
            #  and until now it asked a person to TYPE its identifier because
            #  nothing listed one. It travels with the Report because it is the
            #  same read: what this Report version can be re-pinned to.
            payload["pinnable_presentation_versions"] = list_pinnable_presentation_versions(
                conn, org_id=org_id, project_id=request.path_params["project_id"]
            )
    except ArtifactNotFound:
        return JSONResponse(_NOT_FOUND, 404)
    return JSONResponse(payload)


async def _create_report_version(request: Request) -> Response:
    """POST {base}/reports/{id}/versions -- revise, never mutate."""
    auth = await _authorize(request, "member")
    if isinstance(auth, Response):
        return auth
    identity, org_id = auth
    try:
        body = await _json_body(request)
    except (ValueError, json.JSONDecodeError) as exc:
        return JSONResponse({"code": "invalid_body", "message": str(exc)}, 400)

    try:
        with analyze_connection(identity) as conn:
            created = create_report_version(
                conn,
                org_id=org_id,
                project_id=request.path_params["project_id"],
                report_id=request.path_params["report_id"],
                actor=identity,
                query_spec_version_id=str(body.get("query_spec_version_id") or ""),
                label=body.get("label"),
                description=body.get("description"),
                presentation=body.get("presentation"),
            )
            conn.commit()
    except ArtifactNotFound:
        return JSONResponse(_NOT_FOUND, 404)
    except ArtifactRefused as exc:
        return _refused(exc)
    return JSONResponse(created, 201)


def _wants_archived(request: Request) -> bool:
    """`?include_archived=true` — the one way back to a retired artifact.

    Archiving empties the list; without this the archived artifact would be
    unreachable, which is deletion wearing another word. The schema refuses to
    delete these heads precisely because their versions and runs are evidence.
    """
    return str(request.query_params.get("include_archived", "")).lower() in {
        "1",
        "true",
        "yes",
    }


async def _archive_report(request: Request) -> Response:
    """POST {base}/reports/{id}/archive -- retire it, timestamped and attributed.

    `member`, like every other write on this surface. Idempotent: asking twice
    answers 200 twice rather than inventing a conflict for a state that is
    already what the caller asked for.
    """
    auth = await _authorize(request, "member")
    if isinstance(auth, Response):
        return auth
    identity, org_id = auth
    try:
        with analyze_connection(identity) as conn:
            archived = archive_report(
                conn,
                org_id=org_id,
                project_id=request.path_params["project_id"],
                report_id=request.path_params["report_id"],
                actor=identity,
            )
            conn.commit()
    except ArtifactNotFound:
        return JSONResponse(_NOT_FOUND, 404)
    except ArtifactRefused as exc:
        return _refused(exc)
    return JSONResponse(archived)


async def _archive_notebook(request: Request) -> Response:
    """POST {base}/notebooks/{id}/archive -- the sibling gesture, same contract."""
    auth = await _authorize(request, "member")
    if isinstance(auth, Response):
        return auth
    identity, org_id = auth
    try:
        with analyze_connection(identity) as conn:
            archived = archive_notebook(
                conn,
                org_id=org_id,
                project_id=request.path_params["project_id"],
                notebook_id=request.path_params["notebook_id"],
                actor=identity,
            )
            conn.commit()
    except ArtifactNotFound:
        return JSONResponse(_NOT_FOUND, 404)
    except ArtifactRefused as exc:
        return _refused(exc)
    return JSONResponse(archived)


async def _run_report_version(request: Request) -> Response:
    """POST {base}/report-versions/{id}/run -- execute, create a NEW Result.

    202, like Story 50.1's execute: the run was accepted and evidence exists. Its
    OUTCOME may be `unavailable` -- a delivered answer, not a transport failure.
    """
    auth = await _authorize(request, "member")
    if isinstance(auth, Response):
        return auth
    identity, org_id = auth
    try:
        body = await _json_body(request)
    except (ValueError, json.JSONDecodeError) as exc:
        return JSONResponse({"code": "invalid_body", "message": str(exc)}, 400)

    try:
        with analyze_connection(identity) as conn:
            payload = run_report_version(
                conn,
                org_id=org_id,
                project_id=request.path_params["project_id"],
                report_version_id=request.path_params["report_version_id"],
                actor=identity,
                requested_as_of=body.get("as_of"),
            )
            conn.commit()
    except ArtifactNotFound:
        return JSONResponse(_NOT_FOUND, 404)
    except ArtifactRefused as exc:
        return _refused(exc)
    return JSONResponse(payload, 202)


# ---------------------------------------------------------------------------
# Notebooks.
# ---------------------------------------------------------------------------


async def _list_notebooks(request: Request) -> Response:
    auth = await _authorize(request, "viewer")
    if isinstance(auth, Response):
        return auth
    identity, org_id = auth

    with analyze_connection(identity) as conn:
        payload = {
            "notebooks": list_notebooks(
                conn,
                org_id=org_id,
                project_id=request.path_params["project_id"],
                include_archived=_wants_archived(request),
            )
        }
    return JSONResponse(payload)


async def _list_legacy_notebooks(request: Request) -> Response:
    """GET {base}/notebooks/legacy -- the legacy definitions and Runs, readable.

    AC12 requires them to stay readable with their actual evidence. They were,
    through `NotebooksPanel.tsx`, until the canonical screens took over the Analyze
    sections and that panel stopped being mounted -- which made the requirement
    false without anyone repealing it. A separate route rather than a flag on the
    collection, so the two families can never be rendered as one list.
    """
    auth = await _authorize(request, "viewer")
    if isinstance(auth, Response):
        return auth
    identity, _org_id = auth

    with analyze_connection(identity) as conn:
        legacy = list_legacy_notebooks(
            conn,
            project_id=request.path_params["project_id"],
            limit=int(request.query_params.get("limit") or 50),
        )
    return JSONResponse({"notebooks": legacy})


async def _create_notebook(request: Request) -> Response:
    auth = await _authorize(request, "member")
    if isinstance(auth, Response):
        return auth
    identity, org_id = auth
    try:
        body = await _json_body(request)
    except (ValueError, json.JSONDecodeError) as exc:
        return JSONResponse({"code": "invalid_body", "message": str(exc)}, 400)

    try:
        with analyze_connection(identity) as conn:
            created = create_notebook(
                conn,
                org_id=org_id,
                project_id=request.path_params["project_id"],
                label=body.get("label") or "",
                description=body.get("description"),
                actor=identity,
                blocks=body.get("blocks"),
            )
            conn.commit()
    except ArtifactNotFound:
        return JSONResponse(_NOT_FOUND, 404)
    except ArtifactRefused as exc:
        return _refused(exc)
    return JSONResponse(created, 201)


async def _get_notebook(request: Request) -> Response:
    auth = await _authorize(request, "viewer")
    if isinstance(auth, Response):
        return auth
    identity, org_id = auth

    try:
        with analyze_connection(identity) as conn:
            payload = get_notebook(
                conn,
                org_id=org_id,
                project_id=request.path_params["project_id"],
                notebook_id=request.path_params["notebook_id"],
            )
    except ArtifactNotFound:
        return JSONResponse(_NOT_FOUND, 404)
    return JSONResponse(payload)


async def _create_notebook_version(request: Request) -> Response:
    auth = await _authorize(request, "member")
    if isinstance(auth, Response):
        return auth
    identity, org_id = auth
    try:
        body = await _json_body(request)
    except (ValueError, json.JSONDecodeError) as exc:
        return JSONResponse({"code": "invalid_body", "message": str(exc)}, 400)

    try:
        with analyze_connection(identity) as conn:
            created = create_notebook_version(
                conn,
                org_id=org_id,
                project_id=request.path_params["project_id"],
                notebook_id=request.path_params["notebook_id"],
                actor=identity,
                blocks=body.get("blocks"),
                label=body.get("label"),
            )
            conn.commit()
    except ArtifactNotFound:
        return JSONResponse(_NOT_FOUND, 404)
    except ArtifactRefused as exc:
        return _refused(exc)
    return JSONResponse(created, 201)


async def _set_notebook_schedule(request: Request) -> Response:
    """PUT {base}/notebooks/{id}/schedule -- operational policy, not content (AC7)."""
    auth = await _authorize(request, "member")
    if isinstance(auth, Response):
        return auth
    identity, org_id = auth
    try:
        body = await _json_body(request)
    except (ValueError, json.JSONDecodeError) as exc:
        return JSONResponse({"code": "invalid_body", "message": str(exc)}, 400)

    try:
        with analyze_connection(identity) as conn:
            payload = set_notebook_schedule(
                conn,
                org_id=org_id,
                project_id=request.path_params["project_id"],
                notebook_id=request.path_params["notebook_id"],
                recurrence=str(body.get("recurrence") or ""),
                enabled=bool(body.get("enabled")),
                timezone=str(body.get("timezone") or "UTC"),
                actor=identity,
            )
            conn.commit()
    except ArtifactNotFound:
        return JSONResponse(_NOT_FOUND, 404)
    except ArtifactRefused as exc:
        return _refused(exc)
    return JSONResponse(payload)


async def _run_notebook(request: Request) -> Response:
    """POST {base}/notebooks/{id}/runs -- one idempotent Run, manual or scheduled."""
    auth = await _authorize(request, "member")
    if isinstance(auth, Response):
        return auth
    identity, org_id = auth
    try:
        body = await _json_body(request)
    except (ValueError, json.JSONDecodeError) as exc:
        return JSONResponse({"code": "invalid_body", "message": str(exc)}, 400)

    try:
        with analyze_connection(identity) as conn:
            payload = run_notebook(
                conn,
                org_id=org_id,
                project_id=request.path_params["project_id"],
                notebook_id=request.path_params["notebook_id"],
                actor=identity,
                idempotency_key=str(body.get("idempotency_key") or ""),
                dispatch_source=str(body.get("dispatch_source") or "manual"),
                requested_as_of=body.get("as_of"),
            )
            conn.commit()
    except ArtifactNotFound:
        return JSONResponse(_NOT_FOUND, 404)
    except ArtifactRefused as exc:
        return _refused(exc)
    # 200 on an idempotent replay, 202 on a newly accepted Run: a caller can tell
    # whether its retry did work without parsing the body.
    return JSONResponse(payload, 200 if payload.get("idempotent_replay") else 202)


async def _get_notebook_run(request: Request) -> Response:
    auth = await _authorize(request, "viewer")
    if isinstance(auth, Response):
        return auth
    identity, org_id = auth

    try:
        with analyze_connection(identity) as conn:
            payload = get_notebook_run(
                conn,
                org_id=org_id,
                project_id=request.path_params["project_id"],
                run_id=request.path_params["run_id"],
            )
    except ArtifactNotFound:
        return JSONResponse(_NOT_FOUND, 404)
    return JSONResponse(payload)


# ---------------------------------------------------------------------------
# Renders.
# ---------------------------------------------------------------------------


async def _list_renders(request: Request) -> Response:
    """GET {base}/renders -- the canonical preserved-artifact browser (AC9)."""
    auth = await _authorize(request, "viewer")
    if isinstance(auth, Response):
        return auth
    identity, org_id = auth

    with analyze_connection(identity) as conn:
        payload = list_renders(
            conn,
            org_id=org_id,
            project_id=request.path_params["project_id"],
            limit=int(request.query_params.get("limit") or 50),
            cursor=request.query_params.get("cursor"),
        )
    return JSONResponse(payload)


async def _list_legacy_snapshots(request: Request) -> Response:
    """GET {base}/renders/legacy -- the same Project's legacy snapshots, labelled.

    A separate route rather than a flag on the collection, so a client cannot
    accidentally render the two families in one list and call them all Renders.
    """
    auth = await _authorize(request, "viewer")
    if isinstance(auth, Response):
        return auth
    identity, _org_id = auth

    with analyze_connection(identity) as conn:
        snapshots = list_legacy_snapshots(
            conn,
            project_id=request.path_params["project_id"],
            limit=int(request.query_params.get("limit") or 50),
        )
    return JSONResponse({"snapshots": snapshots})


async def _create_render(request: Request) -> Response:
    """POST {base}/renders -- freeze one presentation over one exact Result.

    On a migrated deployment this WRITES the Render: migrations 156 and 160 landed
    both replay registries, so `render_contract_unavailable` no longer fires here
    (re-measured 2026-08-17 -- this docstring claimed the refusal was the delivered
    behaviour, which stopped being true when those migrations landed).

    It still refuses pin by pin: the ten replay pins must each be present and none
    may be a placeholder word. `render_contract_unavailable` is reserved for an
    unmigrated deployment, and names the exact missing links.
    """
    auth = await _authorize(request, "member")
    if isinstance(auth, Response):
        return auth
    identity, org_id = auth
    try:
        body = await _json_body(request)
    except (ValueError, json.JSONDecodeError) as exc:
        return JSONResponse({"code": "invalid_body", "message": str(exc)}, 400)

    try:
        with analyze_connection(identity) as conn:
            created = create_render(
                conn, org_id=org_id, project_id=request.path_params["project_id"],
                actor=identity, payload=body,
            )
            conn.commit()
    except ArtifactNotFound:
        return JSONResponse(_NOT_FOUND, 404)
    except ArtifactRefused as exc:
        return _refused(exc)
    return JSONResponse(created, 201)


async def _get_render(request: Request) -> Response:
    auth = await _authorize(request, "viewer")
    if isinstance(auth, Response):
        return auth
    identity, org_id = auth

    try:
        with analyze_connection(identity) as conn:
            payload = get_render(
                conn,
                org_id=org_id,
                project_id=request.path_params["project_id"],
                render_id=request.path_params["render_id"],
            )
    except ArtifactNotFound:
        return JSONResponse(_NOT_FOUND, 404)
    return JSONResponse(payload)


async def _legacy_classification(request: Request) -> Response:
    """GET {base}/artifact-migration -- the measured, restartable classification.

    A read. It never writes, so a reviewer can run it before and after anything
    and compare, and it cannot make a count look complete by deleting a row.
    """
    auth = await _authorize(request, "viewer")
    if isinstance(auth, Response):
        return auth
    identity, _org_id = auth

    with analyze_connection(identity) as conn:
        payload = classify_legacy_artifacts(
            conn, project_id=request.path_params["project_id"]
        )
    return JSONResponse(payload)


# Literal segments before parameterized ones, and the more specific of two
# parameterized families first. A seam test asserts every one of these positions.
ROUTES = [
    Route(f"{_BASE}/artifact-migration", endpoint=_legacy_classification, methods=["GET"]),
    Route(f"{_BASE}/reports", endpoint=_list_reports, methods=["GET"]),
    Route(f"{_BASE}/reports", endpoint=_create_report, methods=["POST"]),
    Route(
        f"{_BASE}/report-versions/{{report_version_id}}/run",
        endpoint=_run_report_version,
        methods=["POST"],
    ),
    Route(
        f"{_BASE}/reports/{{report_id}}/versions",
        endpoint=_create_report_version,
        methods=["POST"],
    ),
    Route(
        f"{_BASE}/reports/{{report_id}}/archive",
        endpoint=_archive_report,
        methods=["POST"],
    ),
    Route(f"{_BASE}/reports/{{report_id}}", endpoint=_get_report, methods=["GET"]),
    Route(f"{_BASE}/notebooks", endpoint=_list_notebooks, methods=["GET"]),
    Route(f"{_BASE}/notebooks", endpoint=_create_notebook, methods=["POST"]),
    Route(f"{_BASE}/notebooks/legacy", endpoint=_list_legacy_notebooks, methods=["GET"]),
    Route(f"{_BASE}/notebook-runs/{{run_id}}", endpoint=_get_notebook_run, methods=["GET"]),
    Route(
        f"{_BASE}/notebooks/{{notebook_id}}/versions",
        endpoint=_create_notebook_version,
        methods=["POST"],
    ),
    Route(
        f"{_BASE}/notebooks/{{notebook_id}}/schedule",
        endpoint=_set_notebook_schedule,
        methods=["PUT"],
    ),
    Route(f"{_BASE}/notebooks/{{notebook_id}}/runs", endpoint=_run_notebook, methods=["POST"]),
    Route(
        f"{_BASE}/notebooks/{{notebook_id}}/archive",
        endpoint=_archive_notebook,
        methods=["POST"],
    ),
    Route(f"{_BASE}/notebooks/{{notebook_id}}", endpoint=_get_notebook, methods=["GET"]),
    Route(f"{_BASE}/renders/legacy", endpoint=_list_legacy_snapshots, methods=["GET"]),
    Route(f"{_BASE}/renders", endpoint=_list_renders, methods=["GET"]),
    Route(f"{_BASE}/renders", endpoint=_create_render, methods=["POST"]),
    Route(f"{_BASE}/renders/{{render_id}}", endpoint=_get_render, methods=["GET"]),
]
