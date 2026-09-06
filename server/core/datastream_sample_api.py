"""A Datastream's sample, and the exports taken from it.

AD-43, 2026-08-12. Four routes: the sample itself, its Excel export, the dataset
export and the columns that export declares. They answer one question -- what
does this Datastream actually hold, and how do I take it away -- and they carry
the ambiguity refusal that a materialization with two candidates must produce.
"""

from __future__ import annotations

import logging

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

logger = logging.getLogger("core.admin_api")

# --- le joint qui reste dans admin_api -----------------------------------
async def _check_auth(*args, **kwargs):
    """Le joint d'`admin_api`, atteint a l'APPEL -- jamais capture a l'import.

    Une soixantaine de suites patchent `core.admin_api._check_auth` ; un import
    de tete ignorerait le patch EN SILENCE et lirait la production.
    """
    from core.admin_api import _check_auth as _impl  # noqa: PLC0415
    return await _impl(*args, **kwargs)

def _require_datastream_role(*args, **kwargs):
    """Le joint d'`admin_api`, atteint a l'APPEL -- jamais capture a l'import.

    Une soixantaine de suites patchent `core.admin_api._require_datastream_role` ; un import
    de tete ignorerait le patch EN SILENCE et lirait la production.
    """
    from core.admin_api import _require_datastream_role as _impl  # noqa: PLC0415
    return _impl(*args, **kwargs)

# --- les handlers, deplaces TELS QUELS -----------------------------------

#: The refusal a mart read answers when it cannot tell two Datastreams apart.
#: ONE code and ONE sentence: three handlers refuse on this rule and story 58.1
#: added a fourth, and three inline copies of a phrase are three phrases waiting
#: to drift -- the one that drifted would be the one a person quoted back.
AMBIGUOUS_MATERIALIZATION = "ambiguous_materialization"

AMBIGUOUS_MATERIALIZATION_MESSAGE = (
    "No Datastream-scoped sample materialisation is available for this connector."
)

async def _datastream_sample(request: Request) -> Response:
    """GET /api/datastreams/{id}/sample -- deterministic masked daily sample (12.19).

    Query params:
      stage      -- collected|mapped|processed|published (default: processed)
      date_from  -- inclusive ISO day (required)
      date_to    -- inclusive ISO day (required)
      limit      -- per-day row cap (default 5, hard-capped at 20)
      project_id -- required route-project scope (the Datastream may be shared)

    Errors: 400 (bad params/range/stage), 401 (unauthorized), 404 (unknown/cross-
    scope datastream), 502 (warehouse unreachable / marts absent backend), 500 (DB).
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Token d'acces requis"},
            status_code=401,
        )

    # THE PATH IS NOW THE PROJECT-SCOPED ONE, AND THIS HANDLER WAS NOT ADAPTED.
    #
    # The route registered for it is
    # `/api/projects/{project_id}/datastreams/{ds_id}/sample`, but these reads
    # were written for the retired `/api/datastreams/{id}/sample?project_id=`:
    # `path_params["id"]` is absent under the new shape, so `ds_id` was the empty
    # string and every call resolved nothing. Mounting a route is not the same as
    # wiring it — the second half is these five lines.
    #
    # Both shapes are read, path first, so a caller written against either one
    # keeps working and nothing already in someone's history breaks.
    ds_id = (request.path_params.get("ds_id") or request.path_params.get("id") or "").strip()
    stage = (request.query_params.get("stage") or "processed").strip().lower()
    date_from = (request.query_params.get("date_from") or "").strip()
    date_to = (request.query_params.get("date_to") or "").strip()
    claimed_project_id = (
        request.path_params.get("project_id") or request.query_params.get("project_id") or ""
    ).strip()
    if not claimed_project_id or not date_from or not date_to:
        return JSONResponse(
            {
                "code": "missing_param",
                "message": "project_id, date_from et date_to sont requis",
            },
            status_code=400,
        )

    try:
        limit = int(request.query_params.get("limit") or "5")
    except (TypeError, ValueError):
        return JSONResponse(
            {"code": "invalid_param", "message": "limit must be an integer"},
            status_code=400,
        )

    try:
        from core.cache_warehouse import SampleReadError, read_datastream_sample  # noqa: PLC0415
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            role_error = _require_datastream_role(
                claimed_project_id,
                identity,
                "viewer",
                conn,
                datastream_id=ds_id,
            )
            if role_error is not None:
                return role_error

            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT ds.project_id, ds.module_name, ds.source_kind,
                           ds.name, ds.enabled
                    FROM app.datastreams ds
                    JOIN app.project_flux pf
                      ON pf.flux_id = ds.id AND pf.org_id = ds.org_id
                    WHERE ds.id = %s AND pf.project_id = %s
                      AND ds.archived_at IS NULL
                    """,
                    (ds_id, claimed_project_id),
                )
                row = cur.fetchone()
            if row is None:
                return JSONResponse(
                    {"code": "not_found", "message": "Flux de donnees introuvable"},
                    status_code=404,
                )

            data_project_id = row[0]
            module_name = row[1]
            source_kind = row[2]
            datastream_name = row[3]
            collection_expected = bool(row[4])
            connector = module_name or ""

            # fact_daily_kpi has no Datastream discriminator. More than one
            # Datastream for the same owner-project/connector is therefore
            # ambiguous and must fail closed rather than mix their rows.
            if datastream_materialization_is_ambiguous(
                conn, data_project_id=data_project_id, connector=connector
            ):
                return ambiguous_materialization_response()

            try:
                sample = read_datastream_sample(
                    project_id=data_project_id,
                    connector=connector,
                    stage=stage,
                    date_from=date_from,
                    date_to=date_to,
                    limit=limit,
                )
            except SampleReadError as exc:
                status = 502 if exc.code == "warehouse_unavailable" else 400
                return JSONResponse({"code": exc.code, "message": str(exc)}, status_code=status)
    except Exception as exc:
        logger.error("admin_api: datastream_sample_error: %s", exc)
        return JSONResponse(
            {"code": "db_error", "message": "Erreur base de donnees"},
            status_code=500,
        )

    return JSONResponse(
        {
            "datastream_id": ds_id,
            "project_id": claimed_project_id,
            "stage": stage,
            "served_stage": sample["served_stage"],
            "stage_note": sample["stage_note"],
            "datastream": {
                "id": ds_id,
                "name": datastream_name,
                "module_name": module_name,
                "source_kind": source_kind,
            },
            "collection_expected": collection_expected,
            "materialization_available": sample["materialization_available"],
            "sample_watermark": sample["sample_watermark"],
            "date_from": date_from,
            "date_to": date_to,
            "limit": max(1, min(limit, 20)),
            "masked_fields": sample["masked_fields"],
            "masked_value_count": sample["masked_value_count"],
            "version_binding_available": False,
            "days": sample["days"],
        }
    )

async def _export_datastream_sample_excel(request: Request) -> Response:
    """GET /api/projects/{project_id}/datastreams/{ds_id}/sample/export

    Story 43.16: Governed export of bounded, masked datastream samples to CSV/Excel.
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Token d'acces requis"}, status_code=401
        )

    project_id = request.path_params.get("project_id", "").strip()
    ds_id = request.path_params.get("ds_id", "").strip()
    stage = (request.query_params.get("stage") or "processed").strip().lower()
    date_from = (request.query_params.get("date_from") or "").strip()
    date_to = (request.query_params.get("date_to") or "").strip()

    if not project_id or not ds_id:
        return JSONResponse(
            {"code": "missing_param", "message": "project_id et ds_id sont requis"},
            status_code=400,
        )
    if not date_from or not date_to:
        return JSONResponse(
            {"code": "missing_param", "message": "date_from et date_to sont requis"},
            status_code=400,
        )
    try:
        limit = int(request.query_params.get("limit") or "5")
    except (TypeError, ValueError):
        return JSONResponse(
            {"code": "invalid_param", "message": "limit must be an integer"},
            status_code=400,
        )

    try:
        from core.cache_warehouse import SampleReadError, read_datastream_sample  # noqa: PLC0415
        from core.db import get_connection  # noqa: PLC0415
        with get_connection() as conn:
            role_error = _require_datastream_role(
                project_id,
                identity,
                "viewer",
                conn,
                datastream_id=ds_id,
            )
            if role_error is not None:
                return role_error

            resolved = _resolve_datastream_mart(conn, project_id, ds_id)
            if isinstance(resolved, Response):
                return resolved
            data_project_id, connector = resolved
            try:
                sample = read_datastream_sample(
                    project_id=data_project_id,
                    connector=connector,
                    stage=stage,
                    date_from=date_from,
                    date_to=date_to,
                    limit=limit,
                )
            except SampleReadError as exc:
                status = 502 if exc.code == "warehouse_unavailable" else 400
                return JSONResponse({"code": exc.code, "message": str(exc)}, status_code=status)
    except Exception as exc:
        logger.error("admin_api: sample_export db_error: %s", exc)
        return JSONResponse(
            {"code": "db_error", "message": "Erreur base de donnees."}, status_code=500
        )

    # THE ROWS THE READER RETURNED, AND NOTHING ELSE.
    #
    # This handler used to authenticate, check the role, and then write a
    # fabricated two-line CSV whose only data was the literal string
    # `"masked_bounded_sample"` — it never touched the warehouse. It is
    # unreachable from the console today, which is the only reason a person has
    # never been handed a file that looked like their own data and was not. The
    # moment an export control were wired to it, it would have shipped invented
    # evidence under a governed filename.
    import csv  # noqa: E401, PLC0415
    import io
    days = sample.get("days") or []
    columns: list[str] = []
    for day in days:
        for sample_row in day.get("rows") or []:
            for key in sample_row:
                if key not in columns:
                    columns.append(key)
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["date", *columns])
    for day in days:
        for sample_row in day.get("rows") or []:
            writer.writerow([day.get("date"), *[sample_row.get(col, "") for col in columns]])
    csv_bytes = output.getvalue().encode("utf-8")

    return Response(
        content=csv_bytes,
        media_type="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="datastream_{ds_id}_sample_{stage}.csv"'
        },
    )

async def _export_datastream_dataset(request: Request) -> Response:
    """GET /api/projects/{project_id}/datastreams/{ds_id}/export

    A bounded, masked CSV over a chosen period and a chosen column set.

    This is NOT the governed asynchronous export story 43.16 describes -- that
    needs "a durable, authorized job/worker contract bound to project,
    Datastream, stage, interval, filters and publication version", the story puts
    creating it under **Ask First**, and no such substrate exists. This read is
    request-scoped and therefore bounded, and it REFUSES past its ceiling instead
    of truncating: a truncated export is a file a person will treat as complete.
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Token d'acces requis"}, status_code=401
        )

    project_id = request.path_params.get("project_id", "").strip()
    ds_id = request.path_params.get("ds_id", "").strip()
    stage = (request.query_params.get("stage") or "published").strip().lower()
    date_from = (request.query_params.get("date_from") or "").strip()
    date_to = (request.query_params.get("date_to") or "").strip()
    columns = [
        column.strip()
        for column in (request.query_params.get("columns") or "").split(",")
        if column.strip()
    ]

    if not project_id or not ds_id:
        return JSONResponse(
            {"code": "missing_param", "message": "project_id et ds_id sont requis"},
            status_code=400,
        )
    if not date_from or not date_to:
        return JSONResponse(
            {"code": "missing_param", "message": "date_from et date_to sont requis"},
            status_code=400,
        )

    try:
        from core.cache_warehouse import SampleReadError, read_datastream_export  # noqa: PLC0415
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            role_error = _require_datastream_role(
                project_id, identity, "viewer", conn, datastream_id=ds_id
            )
            if role_error is not None:
                return role_error
            resolved = _resolve_datastream_mart(conn, project_id, ds_id)
            if isinstance(resolved, Response):
                return resolved
            data_project_id, connector = resolved
            try:
                export = read_datastream_export(
                    project_id=data_project_id,
                    connector=connector,
                    stage=stage,
                    date_from=date_from,
                    date_to=date_to,
                    columns=columns or None,
                )
            except SampleReadError as exc:
                status = 502 if exc.code == "warehouse_unavailable" else 400
                return JSONResponse(
                    {"code": exc.code, "message": str(exc)}, status_code=status
                )
    except Exception as exc:
        logger.error("admin_api: dataset_export db_error: %s", exc)
        return JSONResponse(
            {"code": "db_error", "message": "Erreur base de donnees."}, status_code=500
        )

    import csv  # noqa: E401, PLC0415
    import io
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(export["columns"])
    for row in export["rows"]:
        writer.writerow([row.get(col, "") for col in export["columns"]])
    filename = f"datastream_{ds_id}_{stage}_{date_from}_{date_to}.csv"
    return Response(
        content=output.getvalue().encode("utf-8"),
        media_type="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            # So a caller can tell a complete export from a coincidence.
            "X-Toorow-Export-Rows": str(len(export["rows"])),
            "X-Toorow-Export-Masked": ",".join(export["masked_fields"]),
        },
    )

async def _datastream_export_columns(request: Request) -> Response:
    """GET /api/projects/{project_id}/datastreams/{ds_id}/export/columns

    The columns a person may choose from. Without this the "custom columns" of
    an export is a text box people have to guess into, and a guessed column name
    is a 400 they cannot diagnose."""
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Token d'acces requis"}, status_code=401
        )
    project_id = request.path_params.get("project_id", "").strip()
    ds_id = request.path_params.get("ds_id", "").strip()
    if not project_id or not ds_id:
        return JSONResponse(
            {"code": "missing_param", "message": "project_id et ds_id sont requis"},
            status_code=400,
        )
    try:
        from core.cache_warehouse import export_columns_for  # noqa: PLC0415
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            role_error = _require_datastream_role(
                project_id, identity, "viewer", conn, datastream_id=ds_id
            )
            if role_error is not None:
                return role_error
            resolved = _resolve_datastream_mart(conn, project_id, ds_id)
            if isinstance(resolved, Response):
                return resolved
            data_project_id, connector = resolved
            columns, masked = export_columns_for(data_project_id, connector)
    except Exception as exc:
        logger.error("admin_api: export_columns db_error: %s", exc)
        return JSONResponse(
            {"code": "db_error", "message": "Erreur base de donnees."}, status_code=500
        )
    return JSONResponse(
        {
            "project_id": project_id,
            "datastream_id": ds_id,
            "columns": columns,
            # Named, not hidden: a person choosing a masked column should know it
            # will arrive masked rather than discover it in the file.
            "masked_columns": masked,
        },
        status_code=200,
    )

def _resolve_datastream_mart(conn, project_id: str, ds_id: str):
    """(data_project_id, connector) for a Datastream, or a refusing Response.

    Extracted because three handlers need the SAME resolution and the SAME
    fail-closed rule: `fact_daily_kpi` carries no Datastream discriminator, so
    two Datastreams on one owner-project/connector are ambiguous and must refuse
    rather than mix their rows. Three copies of that rule would eventually
    disagree, and the one that drifted would be the one that leaked."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT ds.project_id, ds.module_name
            FROM app.datastreams ds
            JOIN app.project_flux pf
              ON pf.flux_id = ds.id AND pf.org_id = ds.org_id
            WHERE ds.id = %s AND pf.project_id = %s
              AND ds.archived_at IS NULL
            """,
            (ds_id, project_id),
        )
        row = cur.fetchone()
    if row is None:
        return JSONResponse(
            {"code": "not_found", "message": "Flux de donnees introuvable"},
            status_code=404,
        )
    data_project_id, connector = row[0], (row[1] or "")
    if datastream_materialization_is_ambiguous(
        conn, data_project_id=data_project_id, connector=connector
    ):
        return ambiguous_materialization_response()
    return data_project_id, connector

def ambiguous_materialization_response() -> Response:
    """The 409 every mart-scoped reader answers when the rule below refuses."""
    return JSONResponse(
        {"code": AMBIGUOUS_MATERIALIZATION, "message": AMBIGUOUS_MATERIALIZATION_MESSAGE},
        status_code=409,
    )

def datastream_materialization_is_ambiguous(
    conn, *, data_project_id: str, connector: str
) -> bool:
    """Can a mart row be attributed to ONE Datastream of this project?

    `fact_daily_kpi` carries `(project_id, connector)` and no Datastream
    discriminator, so two live Datastreams on one owner-project/connector make
    every row of that slice unattributable. The rule fails CLOSED rather than
    mixing them: a screen showing another stream's rows under this stream's name
    is a wrong number nobody can see is wrong.

    A Datastream with no connector at all is not ambiguous -- it has no mart
    slice to confuse, and its caller says so with an absence of its own.
    """
    if not connector:
        return False
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT COUNT(*)
            FROM app.datastreams
            WHERE project_id = %s AND module_name = %s
              AND archived_at IS NULL
            """,
            (data_project_id, connector),
        )
        return int(cur.fetchone()[0]) != 1


# --- LES ROUTES, dans l'ordre exact ou elles etaient declarees ------------
#
# Starlette resout dans l'ordre de declaration. Chaque collection est epissee
# par `admin_api` a la position que ses routes occupaient : memes chemins,
# memes methodes, meme ordre. La preuve est un dump avant/apres, pas une
# lecture de diff.

DATASTREAM_SAMPLE_ROUTES_1 = [
    # Story 43.16: Sample bounded reader and export
    Route(
        "/api/projects/{project_id}/datastreams/{ds_id}/sample",
        endpoint=_datastream_sample,
        methods=["GET"],
    ),
    Route(
        "/api/projects/{project_id}/datastreams/{ds_id}/sample/export",
        endpoint=_export_datastream_sample_excel,
        methods=["GET"],
    ),
    # Story 43.16 left the governed ASYNC export unbuilt on purpose -- its
    # job/worker substrate is listed under **Ask First** and does not exist.
    # These two are the bounded, request-scoped door instead: choose a
    # period and a column set, get a masked CSV.
    Route(
        "/api/projects/{project_id}/datastreams/{ds_id}/export",
        endpoint=_export_datastream_dataset,
        methods=["GET"],
    ),
    Route(
        "/api/projects/{project_id}/datastreams/{ds_id}/export/columns",
        endpoint=_datastream_export_columns,
        methods=["GET"],
    ),
]
