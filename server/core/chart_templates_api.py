"""Story 72.5 -- the Chart Template REST family, thin over the four modules that own it.

ONE SERVICE PER SUBJECT, AND NOT ONE RULE HERE. Every handler translates HTTP
into `core.chart_template_store` (reads and writes of the object),
`core.template_compatibility` (the verdict), `core.template_materialization`
(applying) or `core.connector_chart_template_seeds` (AC15). No grammar, no
verdict, no SQL and no sentence a person reads is composed in this file.

IT REUSES THE ANALYZE SEAM RATHER THAN FORKING IT. `_authorize`, `_json_body`,
`_NOT_FOUND` and `analyze_connection` come from `core.query_specs_api`, exactly as
`visualization_specs_api` takes them: this is a sibling route family on the same
analytical base, and a second authorization helper is a second place for the role
check to be wrong.

ROUTE ORDER IS PART OF THE CONTRACT, same rule as the two siblings: the literal
segment is declared before the identifier, so `/chart-templates/seeds` can never
be read as a template id.

NO IDEMPOTENCY KEY, AND THAT IS THE DOMAIN'S PATTERN, NOT AN OMISSION. Neither
`POST /visualizations` nor `POST /visualizations/{id}/versions` carries one --
both are append-only writes of an immutable version whose identity is its
content, and a replay appends a version with the SAME `content_hash` rather than
corrupting anything. `analyze_artifacts_api` takes an `idempotency_key` on
exactly one route, `POST /reports/{id}/runs`, because a run EXECUTES a question
and a duplicated execution costs a warehouse query. Applying a template executes
nothing (AC26).
"""

from __future__ import annotations

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from core.chart_template_store import (
    append_chart_template_version,
    archive_chart_template,
    create_chart_template,
    list_chart_templates,
    list_project_results,
    load_chart_template,
    restore_chart_template,
)
from core.connector_chart_template_seeds import (
    connector_seed_catalogue,
    seed_connector_chart_templates,
    seed_head_id,
)
from core.context_seed import connected_modules
from core.query_specs_api import (
    _BASE,
    _NOT_FOUND,
    _authorize,
    _json_body,
    analyze_connection,
)
from core.template_compatibility import (
    COMPATIBLE,
    read_template_compatibility,
)
from core.template_materialization import MaterializationRefused, materialize_template
from core.visualization_specs import VisualizationNotFound, VisualizationSpecRefused
from core.visualization_templates import template_vocabulary


def _refused(exc: VisualizationSpecRefused) -> Response:
    return JSONResponse(exc.as_dict(), 422)


def _bad_request(message: str, field: str) -> Response:
    return JSONResponse({"code": "missing_field", "message": message, "subject": field}, 400)


# ---------------------------------------------------------------------------
# 1. GET {base}/chart-template-vocabulary
# ---------------------------------------------------------------------------


async def _vocabulary(request: Request) -> Response:
    """The wells, roles, families and profiles a template document may speak.

    The Presentation tab renders THIS. A browser holding its own well list would
    be the second vocabulary the ratified criterion refuses, and it would keep
    offering a well the day the registry drops one.
    """
    auth = await _authorize(request, "viewer")
    if isinstance(auth, Response):
        return auth
    return JSONResponse(template_vocabulary())


# ---------------------------------------------------------------------------
# 2. GET {base}/chart-templates?include_archived=&result_id=
# ---------------------------------------------------------------------------


async def _list_chart_templates(request: Request) -> Response:
    """The Project's templates, the seeds not yet drawn from, and the Results to try.

    THREE EMPTINESSES, THREE FACTS, NEVER ONE (AC18). `templates` empty is not
    `available_seeds` empty is not "nothing here fits the open Result". Each is a
    separate key so a screen cannot collapse them into one sentence, and each is
    MEASURED: `available_seeds` is what the connectors of this Project declare
    minus the heads already present, so "no seed available" is a fact and not a
    placeholder.

    `result_id` narrows nothing away. When it is given, every row carries its
    verdict and the incompatible ones say WHICH predicate is missing -- the
    ratified rule that they must not disappear.
    """
    auth = await _authorize(request, "viewer")
    if isinstance(auth, Response):
        return auth
    identity, org_id = auth
    project_id = request.path_params["project_id"]
    include_archived = request.query_params.get("include_archived") == "true"
    result_id = request.query_params.get("result_id") or ""

    with analyze_connection(identity) as conn:
        templates = list_chart_templates(
            conn, org_id=org_id, project_id=project_id, include_archived=include_archived
        )
        modules = connected_modules(conn, project_id)
        declared, unusable = connector_seed_catalogue(modules)
        present = {row["id"] for row in templates}
        available_seeds = [
            {
                "module_name": seed.module_name,
                "seed_template_id": seed.seed_template_id,
                "label": seed.label,
                "answers_question": seed.answers_question,
                "family": seed.family,
            }
            for seed in declared
            if seed_head_id(seed.module_name, seed.seed_template_id, project_id) not in present
        ]
        results = list_project_results(conn, org_id=org_id, project_id=project_id)

        compatible_count = 0
        if result_id:
            for row in templates:
                version_id = row.get("current_version_id")
                if not version_id:
                    row["verdict"] = None
                    continue
                verdict = read_template_compatibility(
                    conn,
                    org_id=org_id,
                    project_id=project_id,
                    template_version_id=version_id,
                    result_id=result_id,
                ).as_dict()
                row["verdict"] = verdict
                if verdict.get("state") == COMPATIBLE:
                    compatible_count += 1

    return JSONResponse(
        {
            "templates": templates,
            "available_seeds": available_seeds,
            "unusable_seeds": [
                {
                    "module_name": entry.module_name,
                    "seed_template_id": entry.seed_template_id,
                    "reason": entry.reason,
                }
                for entry in unusable
            ],
            "connected_modules": modules,
            "results": results,
            "result_id": result_id or None,
            #: `null` when no Result was chosen. It never reads as zero: "none of
            #: them fits" and "no Result was opened" are two different emptinesses.
            "compatible_count": compatible_count if result_id else None,
        }
    )


# ---------------------------------------------------------------------------
# 3. POST {base}/chart-templates
# ---------------------------------------------------------------------------


async def _create_chart_template(request: Request) -> Response:
    auth = await _authorize(request, "member")
    if isinstance(auth, Response):
        return auth
    identity, org_id = auth
    project_id = request.path_params["project_id"]
    try:
        body = await _json_body(request)
    except ValueError as exc:
        return _bad_request(str(exc), "/")
    label = body.get("label")
    if not isinstance(label, str) or not label.strip():
        return _bad_request("a name is required", "/label")
    try:
        with analyze_connection(identity) as conn:
            created = create_chart_template(
                conn,
                org_id=org_id,
                project_id=project_id,
                label=label,
                description=(
                    body.get("description") if isinstance(body.get("description"), str) else None
                ),
                document=body.get("document"),
                actor=identity,
                proposed_by=str(body.get("proposed_by") or "person"),
            )
    except VisualizationSpecRefused as exc:
        return _refused(exc)
    return JSONResponse(created, 201)


# ---------------------------------------------------------------------------
# 4. POST {base}/chart-templates/seeds
# ---------------------------------------------------------------------------


async def _seed_chart_templates(request: Request) -> Response:
    """AC15 -- bring one connector's declared templates into this Project.

    The connector does not own what it seeds: the head lands in the Project, and
    the first edit makes it project-owned. Running this twice creates nothing the
    first run created.
    """
    auth = await _authorize(request, "member")
    if isinstance(auth, Response):
        return auth
    identity, org_id = auth
    project_id = request.path_params["project_id"]
    try:
        body = await _json_body(request)
    except ValueError as exc:
        return _bad_request(str(exc), "/")
    module_name = body.get("module_name")
    if not isinstance(module_name, str) or not module_name.strip():
        return _bad_request("the connector that seeds the template is required", "/module_name")
    try:
        with analyze_connection(identity) as conn:
            outcome = seed_connector_chart_templates(
                conn, org_id=org_id, project_id=project_id, module_name=module_name.strip()
            )
    except VisualizationSpecRefused as exc:
        return _refused(exc)
    return JSONResponse(outcome, 201 if outcome["created"] else 200)


# ---------------------------------------------------------------------------
# 5. POST {base}/chart-templates/{id}/versions
# ---------------------------------------------------------------------------


async def _add_chart_template_version(request: Request) -> Response:
    auth = await _authorize(request, "member")
    if isinstance(auth, Response):
        return auth
    identity, org_id = auth
    project_id = request.path_params["project_id"]
    try:
        body = await _json_body(request)
    except ValueError as exc:
        return _bad_request(str(exc), "/")
    try:
        with analyze_connection(identity) as conn:
            created = append_chart_template_version(
                conn,
                org_id=org_id,
                project_id=project_id,
                template_id=request.path_params["template_id"],
                document=body.get("document"),
                actor=identity,
                proposed_by=str(body.get("proposed_by") or "person"),
            )
    except VisualizationNotFound:
        return JSONResponse(_NOT_FOUND, 404)
    except VisualizationSpecRefused as exc:
        return _refused(exc)
    return JSONResponse(created, 201)


# ---------------------------------------------------------------------------
# 6. POST {base}/chart-templates/{id}/archive
#    POST {base}/chart-templates/{id}/restore
# ---------------------------------------------------------------------------


async def _archive_chart_template(request: Request) -> Response:
    """Retire one template of this Project, and keep every version readable.

    THE GESTURE THE SERVICE ALREADY NAMED AND NO ROUTE OFFERED.
    `append_chart_template_version` has refused an archived head since story 72.5
    with the remedy "List archived templates and restore this one first" -- a
    sentence that pointed at two gestures the API did not carry. Naming a gesture
    a person cannot perform is the same defect as a button that does nothing.

    NO `Idempotency-Key`, and for this family's stated reason. This is not an
    append of an immutable version and it is not an execution: it is a state a
    caller ASKS FOR, and asking twice for the same state is answered with the
    state rather than an error, so a key would guard nothing. `unchanged` says
    which of the two happened.
    """
    return await _set_archived(request, archiving=True)


async def _restore_chart_template(request: Request) -> Response:
    return await _set_archived(request, archiving=False)


async def _set_archived(request: Request, *, archiving: bool) -> Response:
    auth = await _authorize(request, "member")
    if isinstance(auth, Response):
        return auth
    identity, org_id = auth
    write = archive_chart_template if archiving else restore_chart_template
    try:
        with analyze_connection(identity) as conn:
            outcome = write(
                conn,
                org_id=org_id,
                project_id=request.path_params["project_id"],
                template_id=request.path_params["template_id"],
            )
    except VisualizationNotFound:
        return JSONResponse(_NOT_FOUND, 404)
    #: Always 200: the resource this names already existed, and the call changed
    #: its state or found it already there. Neither is a creation, and a 201 on
    #: the first call and a 200 on the second would make a repeat look like a
    #: different outcome than it is.
    return JSONResponse(outcome, 200)


# ---------------------------------------------------------------------------
# 7. GET {base}/chart-templates/{id}
# ---------------------------------------------------------------------------


async def _get_chart_template(request: Request) -> Response:
    auth = await _authorize(request, "viewer")
    if isinstance(auth, Response):
        return auth
    identity, org_id = auth
    project_id = request.path_params["project_id"]
    result_id = request.query_params.get("result_id") or ""
    try:
        with analyze_connection(identity) as conn:
            payload = load_chart_template(
                conn,
                org_id=org_id,
                project_id=project_id,
                template_id=request.path_params["template_id"],
            )
            payload["results"] = list_project_results(
                conn, org_id=org_id, project_id=project_id
            )
            payload["verdict"] = None
            if result_id and payload.get("current_version_id"):
                payload["verdict"] = read_template_compatibility(
                    conn,
                    org_id=org_id,
                    project_id=project_id,
                    template_version_id=payload["current_version_id"],
                    result_id=result_id,
                ).as_dict()
            payload["result_id"] = result_id or None
    except VisualizationNotFound:
        return JSONResponse(_NOT_FOUND, 404)
    return JSONResponse(payload)


# ---------------------------------------------------------------------------
# 8. GET {base}/chart-template-versions/{id}/compatibility?result_id=
# ---------------------------------------------------------------------------


async def _compatibility(request: Request) -> Response:
    """The verdict for one exact version against one exact Result. A read, and only a read.

    Nothing is written here and nothing is cached (AC12): the same call a moment
    later re-reads the shipped registry, which is the whole reason this value is
    not a column.
    """
    auth = await _authorize(request, "viewer")
    if isinstance(auth, Response):
        return auth
    identity, org_id = auth
    result_id = request.query_params.get("result_id") or ""
    if not result_id:
        return _bad_request("choose the Result to judge this template against", "/result_id")
    with analyze_connection(identity) as conn:
        verdict = read_template_compatibility(
            conn,
            org_id=org_id,
            project_id=request.path_params["project_id"],
            template_version_id=request.path_params["template_version_id"],
            result_id=result_id,
        )
    return JSONResponse(verdict.as_dict())


# ---------------------------------------------------------------------------
# 9. POST {base}/chart-template-versions/{id}/apply
# ---------------------------------------------------------------------------


async def _apply(request: Request) -> Response:
    """AC19 -- apply the template to a Result and get a Visualization Spec version.

    `materialize_template` is the ONLY thing this handler calls to do it. It asks
    the verdict first and refuses before composing a document, it pins the Query
    Spec version the Result already carries, and it writes through
    `create_visualization_spec_version` and nothing else -- so the Spec produced
    here is indistinguishable from one composed by hand in the Builder.
    """
    auth = await _authorize(request, "member")
    if isinstance(auth, Response):
        return auth
    identity, org_id = auth
    try:
        body = await _json_body(request)
    except ValueError as exc:
        return _bad_request(str(exc), "/")
    result_id = body.get("result_id")
    if not isinstance(result_id, str) or not result_id:
        return _bad_request("choose the Result this template is applied to", "/result_id")
    try:
        with analyze_connection(identity) as conn:
            created = materialize_template(
                conn,
                org_id=org_id,
                project_id=request.path_params["project_id"],
                template_version_id=request.path_params["template_version_id"],
                result_id=result_id,
                actor=identity,
                proposed_by=str(body.get("proposed_by") or "person"),
                visualization_id=body.get("visualization_id")
                if isinstance(body.get("visualization_id"), str)
                else None,
                name=body.get("name") if isinstance(body.get("name"), str) else None,
            )
    except MaterializationRefused as exc:
        return _refused(exc)
    except VisualizationNotFound:
        return JSONResponse(_NOT_FOUND, 404)
    except VisualizationSpecRefused as exc:
        return _refused(exc)
    return JSONResponse(created, 201)


# Literal segments first, and the SPECIFIC before the GENERIC: `/chart-templates/seeds`
# is declared ahead of `/chart-templates/{template_id}`, and
# `/chart-template-versions/{id}/apply` ahead of `.../compatibility` is irrelevant
# only because both are literal tails -- the rule stands for the reader. Same
# rule, same reason as `query_specs_api.py:433-434`. The order of this list is
# load-bearing, not cosmetic.
chart_template_routes = [
    Route(f"{_BASE}/chart-template-vocabulary", endpoint=_vocabulary, methods=["GET"]),
    Route(f"{_BASE}/chart-templates", endpoint=_list_chart_templates, methods=["GET"]),
    Route(f"{_BASE}/chart-templates", endpoint=_create_chart_template, methods=["POST"]),
    Route(f"{_BASE}/chart-templates/seeds", endpoint=_seed_chart_templates, methods=["POST"]),
    Route(
        f"{_BASE}/chart-templates/{{template_id}}/versions",
        endpoint=_add_chart_template_version,
        methods=["POST"],
    ),
    Route(
        f"{_BASE}/chart-templates/{{template_id}}/archive",
        endpoint=_archive_chart_template,
        methods=["POST"],
    ),
    Route(
        f"{_BASE}/chart-templates/{{template_id}}/restore",
        endpoint=_restore_chart_template,
        methods=["POST"],
    ),
    Route(
        f"{_BASE}/chart-templates/{{template_id}}",
        endpoint=_get_chart_template,
        methods=["GET"],
    ),
    Route(
        f"{_BASE}/chart-template-versions/{{template_version_id}}/compatibility",
        endpoint=_compatibility,
        methods=["GET"],
    ),
    Route(
        f"{_BASE}/chart-template-versions/{{template_version_id}}/apply",
        endpoint=_apply,
        methods=["POST"],
    ),
]

#: The orchestrator mounts this name.
ROUTES = chart_template_routes
