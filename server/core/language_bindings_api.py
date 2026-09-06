"""toorow -- the REST surface of the LANGUAGE dimension family's binding cycle.

Target: `core.language_dimensions` (Story 27.8), the three-dimension family that
`docs/product-architecture/` states is never merged, never summed and never
compared with itself.

WHY THIS MODULE EXISTS AT ALL. The primitive is rich and its binding lifecycle is
complete -- `persist_binding_proposals`, `confirm_binding`, `reject_binding`,
`list_bindings` all exist, all write their own semantics audit. And every one of
them had ZERO production callers: measured 2026-08-17, the only consumer of
`language_dimensions` anywhere in production was the comparability guard in
`query_specs.py:398-405`. So a client could not declare that a column carries the
language it was TARGETED at rather than the language OBSERVED on the person --
the distinction the whole module exists to protect. This module is that address,
the same repair `unresolved_values_api` made for `core.unresolved_values`.

THE HUMAN ACT IS THE CONFIRMATION, AND IT STAYS ONE. `language_dimensions`
refuses, in code, to let the automatic path write a `confirmed` row: the
classifier may only ever write proposed/pending/excluded. That invariant is NOT
relaxed here. A POST below is a human naming a dimension explicitly, so it
persists a proposal carrying `evidence_source='human'` and then confirms it by
id -- two primitive calls for one human gesture, both audited by the primitive.
Nothing on this surface can confirm a dimension the caller did not spell out.

THE THREE DIMENSIONS ARE THE WHOLE VOCABULARY. A binding whose target is not one
of `audience_language` / `content_language` / `targeting_language` is refused with
the family listed, not stored and left to fail later. This is the language
surface; a request to bind a column to `country` is a wrong address, not a value
error.

THE URL'S PROJECT PINS THE ROW. A binding id in a path is never trusted on its
own: `_load_project_binding` re-reads the row and refuses it unless it is stored
at PROJECT scope for the project named in the URL. Without that, a caller who may
manage project A could retire a PLATFORM binding -- the exact class d59ebc2a
closed for grant changes ("confirm epingle au project_id de l'URL").

NO NEW NAVIGATION NODE, AND THE SCREEN IS NAMED RATHER THAN BUILT. Language is not
a project capability -- there is no toggle for it in `app.project_capabilities`,
unlike Country or Tax & Fees -- so the write gate here is `org-manage`, the same
gate `unresolved_values_api` uses. The screen that should carry this reading is
the Datastream Workbench `Map` tab, beside the field table that already binds a
column to a canonical target and already hosts the unresolved-values panel S1
(d734a294). That mount is NOT delivered here and is named in the return so it is
somebody's criterion rather than a silence.
"""

from __future__ import annotations

import json
import logging

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

logger = logging.getLogger(__name__)

#: The envelope contract, named and versioned the way `unresolved_values.v1` is.
LANGUAGE_BINDINGS_SCHEMA = "language_bindings.v1"

_PROJECT_BASE = "/api/projects/{project_id}/language-bindings"
_BINDING_BASE = _PROJECT_BASE + "/{binding_id}"

#: The provider's own words are the evidence of a classifier proposal. A human
#: declaration has no catalog line to quote, and the table's CHECK already
#: enumerates `human` as a legal source -- so the evidence says who decided.
_HUMAN_EVIDENCE_SOURCE = "human"


# ---------------------------------------------------------------------------
# Refusals -- one sentence per refusal, shared by every route.
# ---------------------------------------------------------------------------


async def _check_auth(request: Request) -> tuple[bool, str]:
    from core.admin_api import _check_auth as _admin_check_auth  # noqa: PLC0415

    return await _admin_check_auth(request)


def _unauthorized() -> Response:
    return JSONResponse(
        {"code": "unauthorized", "message": "Authentication is required."},
        status_code=401,
    )


def _not_found(message: str = "Project not found.") -> Response:
    return JSONResponse({"code": "not_found", "message": message}, status_code=404)


def _forbidden() -> Response:
    return JSONResponse(
        {"code": "forbidden", "message": "Insufficient rights."}, status_code=403
    )


def _invalid(code: str, message: str) -> Response:
    return JSONResponse({"code": code, "message": message}, status_code=400)


def _server_error() -> Response:
    return JSONResponse(
        {"code": "server_error", "message": "Server error."}, status_code=500
    )


def _guard(
    project_id: str, identity: str, *, manage: bool
) -> tuple[str | None, Response | None]:
    """Resolve the org of *project_id* and verify the caller may touch it.

    Read is org membership, write is `org-manage`, and a `project_id` is never
    believed alone -- it is checked against the guarded org, 404 otherwise.
    Existence-hiding on read, not 403: a reader who is not a member must not
    learn the Project exists.
    """
    from core.db import get_connection  # noqa: PLC0415
    from core.project_access import (  # noqa: PLC0415
        identity_can_manage_org,
        identity_has_org_access,
    )

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT org_id FROM app.projects WHERE id = %s", (project_id,)
                )
                row = cur.fetchone()
            if not row or not row[0]:
                return None, _not_found()
            org_id = str(row[0])
            allowed = (
                identity_can_manage_org(org_id, identity, conn)
                if manage
                else identity_has_org_access(org_id, identity, conn)
            )
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "language_bindings_api: guard failed project=%s: %s", project_id, exc
        )
        return None, _server_error()

    if not allowed:
        return None, _forbidden() if manage else _not_found()
    return org_id, None


async def _authorized_project(
    request: Request, *, manage: bool
) -> tuple[str, str, str, None] | tuple[None, None, None, Response]:
    """The three-step opening every route below shares: auth, project, org guard."""
    authorized, identity = await _check_auth(request)
    if not authorized:
        return None, None, None, _unauthorized()

    project_id = str(request.path_params.get("project_id") or "").strip()
    if not project_id:
        return None, None, None, _not_found()

    org_id, denied = _guard(project_id, identity, manage=manage)
    if denied is not None:
        return None, None, None, denied
    return project_id, str(org_id), identity, None


# ---------------------------------------------------------------------------
# The family, as the surface states it.
# ---------------------------------------------------------------------------


def family_reference() -> list[dict]:
    """The three dimensions with what each is ABOUT -- the reason they never merge.

    Read from `language_dimensions` rather than re-spelled here: a second copy of
    the family is a second thing to keep in step with the guard.
    """
    from core.language_dimensions import (  # noqa: PLC0415
        LANGUAGE_DIMENSION_FAMILY,
        describe_dimension,
    )

    listed: list[dict] = []
    for dimension in LANGUAGE_DIMENSION_FAMILY:
        described = describe_dimension(dimension)
        listed.append(
            {
                "dimension": dimension,
                "nature": getattr(described, "nature", None),
                "definition": getattr(described, "definition", None),
            }
        )
    return listed


def _reject_non_family(canonical_dimension: str) -> Response | None:
    """Refuse a target outside the family, listing the family in the refusal."""
    from core.language_dimensions import (  # noqa: PLC0415
        LANGUAGE_DIMENSION_FAMILY,
        is_language_dimension,
    )

    if is_language_dimension(canonical_dimension):
        return None
    return _invalid(
        "not_a_language_dimension",
        "A language binding targets one of "
        + ", ".join(LANGUAGE_DIMENSION_FAMILY)
        + f"; got {canonical_dimension!r}.",
    )


def _load_project_binding(binding_id: str, project_id: str) -> dict | None:
    """Re-read *binding_id* and return it ONLY if it is this project's own row.

    The id in the path is a claim. A binding stored at PLATFORM or ORG scope, or
    at PROJECT scope for another project, is not reachable through this project's
    URL -- it is `None` here and a 404 above, never an operation on someone
    else's row.
    """
    from core.db import get_connection  # noqa: PLC0415
    from core.language_dimensions import SCOPE_PROJECT  # noqa: PLC0415

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT scope_level, project_id, status, canonical_dimension "
                "FROM app.dimension_field_bindings WHERE id = %s",
                (binding_id,),
            )
            row = cur.fetchone()
    if row is None:
        return None
    scope_level, row_project, status, dimension = row
    if scope_level != SCOPE_PROJECT or str(row_project or "") != project_id:
        return None
    return {"status": status, "canonical_dimension": dimension}


# ---------------------------------------------------------------------------
# Routes.
# ---------------------------------------------------------------------------


async def _list(request: Request) -> Response:
    """GET -- the project's own bindings, plus the family they may target."""
    project_id, org_id, _identity, denied = await _authorized_project(
        request, manage=False
    )
    if denied is not None:
        return denied

    from core.language_dimensions import SCOPE_PROJECT, list_bindings  # noqa: PLC0415

    status_filter = request.query_params.get("status") or None
    try:
        rows = list_bindings(
            scope_level=SCOPE_PROJECT,
            org_id=org_id,
            project_id=project_id,
            status=status_filter,
        )
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "language_bindings_api: list failed project=%s: %s", project_id, exc
        )
        return _server_error()

    return JSONResponse(
        {
            "schema": LANGUAGE_BINDINGS_SCHEMA,
            "project_id": project_id,
            "family": family_reference(),
            "bindings": rows,
            "count": len(rows),
        }
    )


async def _declare(request: Request) -> Response:
    """POST -- a human binds one source field to one language dimension.

    The body names the column (`connector`, `report_id`, `source_field`) and the
    dimension. There is no request shape that omits the dimension: a confirmation
    that does not carry what is being confirmed is the thing the primitive
    refuses, and this surface does not offer it either.
    """
    project_id, org_id, identity, denied = await _authorized_project(
        request, manage=True
    )
    if denied is not None:
        return denied

    try:
        body = json.loads(await request.body() or b"{}")
    except (ValueError, TypeError):
        return _invalid("invalid_body", "The request body must be JSON.")
    if not isinstance(body, dict):
        return _invalid("invalid_body", "The request body must be a JSON object.")

    connector = str(body.get("connector") or "").strip()
    report_id = str(body.get("report_id") or "").strip()
    source_field = str(body.get("source_field") or "").strip()
    canonical_dimension = str(body.get("canonical_dimension") or "").strip()
    missing = [
        name
        for name, value in (
            ("connector", connector),
            ("report_id", report_id),
            ("source_field", source_field),
            ("canonical_dimension", canonical_dimension),
        )
        if not value
    ]
    if missing:
        return _invalid(
            "missing_fields",
            "A language binding names the column and the dimension; missing: "
            + ", ".join(missing)
            + ".",
        )
    if (refused := _reject_non_family(canonical_dimension)) is not None:
        return refused

    from core.language_dimensions import (  # noqa: PLC0415
        SCOPE_PROJECT,
        STATUS_PROPOSED,
        BindingEvidence,
        BindingProposal,
        confirm_binding,
        list_bindings,
        persist_binding_proposals,
    )

    rationale = str(body.get("rationale") or "").strip()
    proposal = BindingProposal(
        connector=connector,
        report_id=report_id,
        source_field=source_field,
        canonical_dimension=canonical_dimension,
        status=STATUS_PROPOSED,
        confidence=1.0,
        evidence=BindingEvidence(
            quote=rationale or f"Declared by {identity}.",
            source=_HUMAN_EVIDENCE_SOURCE,
            markers=(),
        ),
        rationale=rationale or "Declared on the project language surface.",
    )

    try:
        counts = persist_binding_proposals(
            [proposal],
            scope_level=SCOPE_PROJECT,
            org_id=org_id,
            project_id=project_id,
            identity=identity,
        )
        if counts.get("skipped"):
            return JSONResponse(
                {
                    "code": "already_decided",
                    "message": (
                        "This column already carries a decided binding. Retire it "
                        "before declaring a different dimension."
                    ),
                },
                status_code=409,
            )
        stored = [
            row
            for row in list_bindings(
                scope_level=SCOPE_PROJECT, org_id=org_id, project_id=project_id
            )
            if row.get("connector") == connector
            and row.get("report_id") == report_id
            and row.get("source_field") == source_field
        ]
        if not stored:
            logger.error(
                "language_bindings_api: proposal vanished project=%s field=%s",
                project_id,
                source_field,
            )
            return _server_error()
        confirmed = confirm_binding(
            str(stored[0]["id"]),
            identity=identity,
            canonical_dimension=canonical_dimension,
        )
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "language_bindings_api: declare failed project=%s field=%s: %s",
            project_id,
            source_field,
            exc,
        )
        return _server_error()

    return JSONResponse(
        {"schema": LANGUAGE_BINDINGS_SCHEMA, "binding": confirmed}, status_code=201
    )


async def _retire(request: Request) -> Response:
    """DELETE -- retire a binding this project declared.

    A retirement is a RECORDED refusal (`rejected`), not a deletion: the row stays
    with who reviewed it and when, so the reading that stopped resolving can say
    why. `resolve_field_bindings` returns confirmed rows only, so a rejected row
    stops applying the moment this returns.
    """
    project_id, _org_id, identity, denied = await _authorized_project(
        request, manage=True
    )
    if denied is not None:
        return denied

    binding_id = str(request.path_params.get("binding_id") or "").strip()
    if not binding_id:
        return _not_found("Binding not found.")

    from core.language_dimensions import reject_binding  # noqa: PLC0415

    try:
        if _load_project_binding(binding_id, project_id) is None:
            return _not_found("Binding not found.")
        rejected = reject_binding(binding_id, identity=identity)
    except Exception as exc:  # noqa: BLE001
        logger.error(
            "language_bindings_api: retire failed project=%s binding=%s: %s",
            project_id,
            binding_id,
            exc,
        )
        return _server_error()

    return JSONResponse({"schema": LANGUAGE_BINDINGS_SCHEMA, "binding": rejected})


# Static sub-resources before parametrized routes (house order).
LANGUAGE_BINDINGS_ROUTES: list[Route] = [
    Route(_PROJECT_BASE, _list, methods=["GET"]),
    Route(_PROJECT_BASE, _declare, methods=["POST"]),
    Route(_BINDING_BASE, _retire, methods=["DELETE"]),
]

__all__ = [
    "LANGUAGE_BINDINGS_ROUTES",
    "LANGUAGE_BINDINGS_SCHEMA",
    "family_reference",
]
