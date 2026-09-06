"""Story 50.4 -- the Visualization Spec REST family.

ONE SERVICE, MANY ADAPTERS. Every handler here is a thin translation of HTTP into
`core.visualization_specs` and `core.visualization_families`. No validation, no
compatibility rule and no SQL lives in this file, so a future MCP adapter reaches
the same grammar without a second presentation authority.

IT REUSES STORY 50.1's SEAM RATHER THAN FORKING IT. `_authorize`, `_json_body`
and the `_NOT_FOUND` envelope are imported from `core.query_specs_api`: this is a
SIBLING route family on the same analytical base, and a second authorization
helper would be a second place for the role check to be wrong.

ROUTE ORDER IS PART OF THE CONTRACT. Starlette matches in declaration order, so
the more specific route is declared first, without exception:
`/visualizations/{id}/versions` before `/visualizations/{id}`, and
`/visualization-spec-versions/{id}/validate` before
`/visualization-spec-versions/{id}`. This mirrors `query_specs_api.py:433-434`
("Literal segments first: ... so a literal can never be read as an id"). A suite
that only checks that every route resolves passes on a list that has silently
been reordered, so the regression test asserts the DECLARED order.

THERE IS NO BYPASS. `proposed_by` is `person` or `model` and nothing else, and it
is evidence rather than a permission: both values travel this route, this
validator and this refusal envelope. No parameter in this module skips
validation, and a test asserts the request model carries no such field.
"""

from __future__ import annotations

import json

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from core.query_specs_api import (
    _BASE,
    _NOT_FOUND,
    _authorize,
    _json_body,
    analyze_connection,
)
from core.visualization_families import (
    ROLE_UNAVAILABLE_OWNER,
    ROLE_UNAVAILABLE_REASON,
    get_family,
    registry_payload,
    role_availability,
)
from core.visualization_specs import (
    GRAMMAR_KEYS,
    MEMBER_METADATA_FACETS,
    RESPONSIVE_PROFILES,
    VISUALIZATION_SPEC_CONTRACT_VERSION,
    VISUALIZATION_SPEC_SCHEMA_VERSION,
    VisualizationNotFound,
    VisualizationSpecRefused,
    create_visualization_spec_version,
    evaluate_result_disclosures,
    load_member_labels,
    load_member_presentation_metadata,
    load_visualization,
    load_visualization_spec_version,
    resolve_multi_source_plan_members,
    validate_visualization_spec,
)


def _refused(exc: VisualizationSpecRefused) -> Response:
    return JSONResponse(exc.as_dict(), 422)


# ---------------------------------------------------------------------------
# 1. GET {base}/visualization-families
# ---------------------------------------------------------------------------


async def _visualization_families(request: Request) -> Response:
    """The registry, exactly as the server holds it.

    The Builder renders THIS. A browser that carried its own family table would be
    a second compatibility authority and would keep offering a family the day a
    constraint moved here.
    """
    auth = await _authorize(request, "viewer")
    if isinstance(auth, Response):
        return auth
    payload = registry_payload()
    payload["spec_contract_version"] = VISUALIZATION_SPEC_CONTRACT_VERSION
    payload["schema_version"] = VISUALIZATION_SPEC_SCHEMA_VERSION
    payload["responsive_profiles"] = list(RESPONSIVE_PROFILES)
    payload["grammar_keys"] = list(GRAMMAR_KEYS)
    return JSONResponse(payload)


# ---------------------------------------------------------------------------
# 2. GET {base}/visualization-options?query_spec_version_id=
# ---------------------------------------------------------------------------


async def _visualization_options(request: Request) -> Response:
    """The members the PINNED Query Spec version selected, and nothing else.

    Structurally incapable of becoming a second member catalog: it reads the
    version's own `spec.measures` / `spec.dimensions` and never the compiled
    artifact's full matrix, so it cannot offer a member the query did not select.
    Adding a member is a query change, and the Builder says so.
    """
    auth = await _authorize(request, "viewer")
    if isinstance(auth, Response):
        return auth
    identity, _org_id = auth
    project_id = request.path_params["project_id"]
    version_id = request.query_params.get("query_spec_version_id") or ""
    if not version_id:
        return JSONResponse(
            {"code": "missing_field", "message": "query_spec_version_id is required"}, 400
        )

    with analyze_connection(identity) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT query_spec_id, semantic_view_id, semantic_view_version_id, spec
                FROM app.query_spec_versions
                WHERE id = %s AND project_id = %s
                """,
                (version_id, project_id),
            )
            row = cur.fetchone()
        if row is None:
            return JSONResponse(_NOT_FOUND, 404)

        spec = row[3] if isinstance(row[3], dict) else {}
        is_multi_source = spec.get("contract_version") == "multi-source-plan.v1"
        if is_multi_source:
            # THE SAME PRODUCER THE BINDING CHECK READS. This rail listed the
            # cross-source members on its own once, and the labels drifted back to
            # `mdm_01KZ...` the moment the second contract arrived. One resolver
            # now answers "which members, under which name" for both surfaces --
            # `visualization_specs.resolve_multi_source_plan_members`.
            resolved = resolve_multi_source_plan_members(
                conn, project_id=project_id, spec=spec
            )
            selected_by_role: dict[str, list[dict[str, str]]] = {
                "measure": [
                    {"id": e["id"], "version_id": e["version_id"], "label": e["label"]}
                    for e in resolved
                    if e["role"] == "measure"
                ],
                "dimension": [
                    {"id": e["id"], "version_id": e["version_id"], "label": e["label"]}
                    for e in resolved
                    if e["role"] == "dimension"
                ],
            }
        else:
            selected_by_role = {
                role: [
                    {
                        "id": str((entry or {}).get("id") or ""),
                        "version_id": str((entry or {}).get("version_id") or ""),
                        "label": str((entry or {}).get("id") or ""),
                    }
                    for entry in (spec.get(key) or [])
                    if (entry or {}).get("id")
                ]
                for key, role in (("measures", "measure"), ("dimensions", "dimension"))
            }
        selected = [entry for entries in selected_by_role.values() for entry in entries]
        # AC7's five rail facets, SERVED. Every one is either a recorded value or
        # a named absence -- the rail never prints a definition or a grain this
        # server did not read. See `load_member_presentation_metadata`.
        metadata = load_member_presentation_metadata(conn, project_id=project_id, members=selected)
        # The Builder printed `sc_01KZ…` where a person expects "Views": the label
        # WAS the identifier, literally. It is now read off the PINNED concept
        # version -- the same word `query-facets` serves -- and the identifier
        # survives only when no label is recorded.
        labels = load_member_labels(conn, project_id=project_id, members=selected)

    def _members(role: str) -> list[dict]:
        return [
            {
                "id": entry["id"],
                "version_id": entry["version_id"],
                "label": labels.get(entry["id"]) or entry["label"],
                "role": role,
                "metadata": metadata.get(entry["id"], {}),
            }
            for entry in selected_by_role[role]
        ]

    return JSONResponse(
        {
            "query_spec_id": row[0],
            "query_spec_version_id": version_id,
            "semantic_view_id": row[1],
            "semantic_view_version_id": row[2],
            "measures": _members("measure"),
            "dimensions": _members("dimension"),
            # Stated, never inferred from an empty list. The Builder renders the
            # Time and Classifications groups EMPTY WITH THIS REASON rather than
            # hiding them: hiding erases the inventory of remaining work, and
            # guessing their contents from member names would manufacture a
            # semantic authority the Builder is forbidden to have.
            "time": [],
            "classifications": [],
            "roles": list(role_availability().values()),
            # The five facets the rail must render per member (AC7), named here so
            # the client renders the server's list rather than a copy of it -- a
            # sixth facet appearing on the server reaches the rail without a
            # client change, and a facet dropped here stops being rendered.
            "member_metadata_facets": list(MEMBER_METADATA_FACETS),
            "unavailable_reason": ROLE_UNAVAILABLE_REASON,
            "unavailable_owner": ROLE_UNAVAILABLE_OWNER,
            # Read from the QUERY, so the Builder can say why a family that needs
            # a grain is unavailable without inventing one.
            "grain": spec.get("grain"),
            "comparison": spec.get("comparison") or "none",
            "row_limit": (
                (spec.get("bounds") or {}).get("row_limit")
                if is_multi_source
                else spec.get("row_limit")
            ),
            "analysis_context": spec.get("analysis_context"),
        }
    )


# ---------------------------------------------------------------------------
# 3. POST {base}/visualizations
# ---------------------------------------------------------------------------


def _proposed_by(body: dict) -> str:
    return str(body.get("proposed_by") or "person")


async def _create_visualization(request: Request) -> Response:
    """Validate, then append version 1 of a new saved presentation."""
    auth = await _authorize(request, "member")
    if isinstance(auth, Response):
        return auth
    identity, org_id = auth
    project_id = request.path_params["project_id"]
    try:
        body = await _json_body(request)
    except (ValueError, json.JSONDecodeError) as exc:
        return JSONResponse({"code": "invalid_body", "message": str(exc)}, 400)

    version_id = str(body.get("query_spec_version_id") or "")
    if not version_id:
        return JSONResponse(
            {"code": "missing_field", "message": "query_spec_version_id is required"}, 400
        )

    try:
        with analyze_connection(identity) as conn:
            validated = validate_visualization_spec(
                conn,
                project_id=project_id,
                query_spec_version_id=version_id,
                payload=body.get("spec"),
            )
            created = create_visualization_spec_version(
                conn,
                org_id=org_id,
                project_id=project_id,
                validated=validated,
                actor=identity,
                proposed_by=_proposed_by(body),
                name=(str(body["name"]) if body.get("name") else None),
            )
            # `core.db.get_connection` NEVER commits: it closes, and psycopg rolls
            # back the open transaction. Without this line the route answered
            # `201` with an id, a version number and a content hash -- for a row
            # that never existed. A write that answers 201 and writes nothing is
            # worse than an error: nobody goes looking for it.
            conn.commit()
    except VisualizationNotFound:
        return JSONResponse(_NOT_FOUND, 404)
    except VisualizationSpecRefused as exc:
        return _refused(exc)
    return JSONResponse(created, 201)


# ---------------------------------------------------------------------------
# 4. POST {base}/visualizations/{visualization_id}/versions
# ---------------------------------------------------------------------------


async def _add_visualization_version(request: Request) -> Response:
    """Append version N+1.

    A presentation-only change keeps the SAME `query_spec_version_id` and touches
    no Story 50.1 table. Re-pinning to a new Query Spec version is allowed, and is
    revalidated in full first: if revalidation fails the repin is refused with the
    complete list and NO version is written -- a binding is never silently dropped
    to make a repin succeed.
    """
    auth = await _authorize(request, "member")
    if isinstance(auth, Response):
        return auth
    identity, org_id = auth
    project_id = request.path_params["project_id"]
    visualization_id = request.path_params["visualization_id"]
    try:
        body = await _json_body(request)
    except (ValueError, json.JSONDecodeError) as exc:
        return JSONResponse({"code": "invalid_body", "message": str(exc)}, 400)

    try:
        with analyze_connection(identity) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT query_spec_version_id
                    FROM app.visualization_spec_versions
                    WHERE visualization_id = %s AND org_id = %s AND project_id = %s
                    ORDER BY version_number DESC LIMIT 1
                    """,
                    (visualization_id, org_id, project_id),
                )
                current = cur.fetchone()
            if current is None:
                return JSONResponse(_NOT_FOUND, 404)
            # Absent, the pin is INHERITED. A caller changing only presentation
            # must not have to restate the query pin, and must not be able to
            # change it by forgetting it.
            version_id = str(body.get("query_spec_version_id") or current[0])

            validated = validate_visualization_spec(
                conn,
                project_id=project_id,
                query_spec_version_id=version_id,
                payload=body.get("spec"),
            )
            created = create_visualization_spec_version(
                conn,
                org_id=org_id,
                project_id=project_id,
                validated=validated,
                actor=identity,
                proposed_by=_proposed_by(body),
                visualization_id=visualization_id,
            )
            # Same reason as on creation: without `commit`, version N+1 is
            # announced and lost when the connection closes.
            conn.commit()
    except VisualizationNotFound:
        return JSONResponse(_NOT_FOUND, 404)
    except VisualizationSpecRefused as exc:
        return _refused(exc)
    return JSONResponse(created, 201)


# ---------------------------------------------------------------------------
# 5. GET {base}/visualizations/{visualization_id}
# ---------------------------------------------------------------------------


async def _get_visualization(request: Request) -> Response:
    auth = await _authorize(request, "viewer")
    if isinstance(auth, Response):
        return auth
    identity, org_id = auth
    project_id = request.path_params["project_id"]

    try:
        with analyze_connection(identity) as conn:
            payload = load_visualization(
                conn,
                org_id=org_id,
                project_id=project_id,
                visualization_id=request.path_params["visualization_id"],
            )
    except VisualizationNotFound:
        return JSONResponse(_NOT_FOUND, 404)
    return JSONResponse(payload)


# ---------------------------------------------------------------------------
# 6. POST {base}/visualization-spec-versions/{id}/validate?result_id=
# ---------------------------------------------------------------------------


async def _validate_visualization_spec_version(request: Request) -> Response:
    """Re-check one stored version, and disclose it against one exact Result.

    Two tiers, deliberately (decision D4). SHAPE compatibility is a property of the
    saved spec and is evaluated against the pinned Query Spec version. CARDINALITY
    and VOLUME are properties of one exact Result and are returned as DISCLOSURES
    here; they are never written into a version. A spec that pinned a row count
    would be a cached analytical answer, which the object model forbids.

    `result_id` is optional. Without it the answer is the shape verdict alone, and
    it says so rather than implying the volume tier passed.
    """
    auth = await _authorize(request, "viewer")
    if isinstance(auth, Response):
        return auth
    identity, org_id = auth
    project_id = request.path_params["project_id"]
    version_id = request.path_params["visualization_spec_version_id"]
    result_id = request.query_params.get("result_id") or ""

    try:
        with analyze_connection(identity) as conn:
            stored = load_visualization_spec_version(
                conn,
                org_id=org_id,
                project_id=project_id,
                visualization_spec_version_id=version_id,
            )
            try:
                validate_visualization_spec(
                    conn,
                    project_id=project_id,
                    query_spec_version_id=stored["query_spec_version_id"],
                    payload=stored["spec"],
                )
                shape = {"compatible": True, "refusals": []}
            except VisualizationSpecRefused as exc:
                shape = {"compatible": False, "refusals": exc.as_dict()["refusals"]}

            disclosures = None
            if result_id:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT r.outcome, r.row_count, r.truncated,
                               p.result_schema, p.manifest, p.rows_chunk
                        FROM app.query_results r
                        JOIN app.query_result_payloads p
                          ON p.result_id = r.id AND p.org_id = r.org_id
                        WHERE r.id = %s AND r.org_id = %s AND r.project_id = %s
                        """,
                        (result_id, org_id, project_id),
                    )
                    result = cur.fetchone()
                if result is None:
                    return JSONResponse(_NOT_FOUND, 404)
                family = get_family(stored["family"])
                if family is not None:
                    rows = result[5] if isinstance(result[5], list) else []
                    disclosures = evaluate_result_disclosures(
                        stored["spec"],
                        family,
                        rows=rows,
                        row_count=int(result[1] or 0),
                        truncated=bool(result[2]),
                        outcome=str(result[0]),
                        result_schema=result[3] if isinstance(result[3], dict) else {},
                        result_manifest=result[4] if isinstance(result[4], dict) else {},
                    )
    except VisualizationNotFound:
        return JSONResponse(_NOT_FOUND, 404)

    return JSONResponse(
        {
            "visualization_spec_version_id": version_id,
            "query_spec_version_id": stored["query_spec_version_id"],
            "family": stored["family"],
            "shape": shape,
            # `null` means "not evaluated", and the client renders that as its own
            # state. It never reads as "passed".
            "result_disclosures": disclosures,
            "result_id": result_id or None,
        }
    )


# ---------------------------------------------------------------------------
# 7. GET {base}/visualization-spec-versions/{id}
# ---------------------------------------------------------------------------


async def _get_visualization_spec_version(request: Request) -> Response:
    auth = await _authorize(request, "viewer")
    if isinstance(auth, Response):
        return auth
    identity, org_id = auth
    project_id = request.path_params["project_id"]

    try:
        with analyze_connection(identity) as conn:
            payload = load_visualization_spec_version(
                conn,
                org_id=org_id,
                project_id=project_id,
                visualization_spec_version_id=request.path_params["visualization_spec_version_id"],
            )
    except VisualizationNotFound:
        return JSONResponse(_NOT_FOUND, 404)
    return JSONResponse(payload)


# Literal segments first, and the SPECIFIC before the GENERIC:
# `/visualizations/{id}/versions` is declared ahead of `/visualizations/{id}` and
# `/visualization-spec-versions/{id}/validate` ahead of
# `/visualization-spec-versions/{id}`, so `versions` and `validate` can never be
# read as an id. Same rule, same reason as `query_specs_api.py:433-434`. The order
# of this list is load-bearing, not cosmetic.
visualization_spec_routes = [
    Route(
        f"{_BASE}/visualization-families",
        endpoint=_visualization_families,
        methods=["GET"],
    ),
    Route(
        f"{_BASE}/visualization-options",
        endpoint=_visualization_options,
        methods=["GET"],
    ),
    Route(f"{_BASE}/visualizations", endpoint=_create_visualization, methods=["POST"]),
    Route(
        f"{_BASE}/visualizations/{{visualization_id}}/versions",
        endpoint=_add_visualization_version,
        methods=["POST"],
    ),
    Route(
        f"{_BASE}/visualizations/{{visualization_id}}",
        endpoint=_get_visualization,
        methods=["GET"],
    ),
    Route(
        f"{_BASE}/visualization-spec-versions/{{visualization_spec_version_id}}/validate",
        endpoint=_validate_visualization_spec_version,
        methods=["POST"],
    ),
    Route(
        f"{_BASE}/visualization-spec-versions/{{visualization_spec_version_id}}",
        endpoint=_get_visualization_spec_version,
        methods=["GET"],
    ),
]

#: The orchestrator mounts this name. Same object, so a reader looking for either
#: spelling finds the one list.
ROUTES = visualization_spec_routes
