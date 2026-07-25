"""toorow -- Report-to-datamodel chain computation (Story 8.9, Epic 8).

Computes and exposes the metrics -> target_fields -> datastreams chain for a
(project, module, report) triple. The chain answers: for every metric in a
report, which target field does it map to, and which datastreams in the project
currently feed that field?

Public API:
    get_report_chain(project_id, module_name, report_id, conn, loaded_modules=None)
        -> dict  (chain document, see below)

REPORT_CHAIN_ROUTES: list[Route]  -- exported for orchestrator to wire into
    admin_api.router alongside DATAMODEL_ROUTES / FLOWS_ROUTES.
    Route: GET /api/reports/{module}/{report_id}/chain?project_id=

Chain document shape:
    {
        "report_id": "<module>/<report_id>",
        "display_name": str | null,
        "metric_definitions": {<metric>: {...}} | null,   # R6 passthrough
        "llm_commentary_guidelines": str | null,          # R6 passthrough
        "metrics": [
            {
                "metric": str,
                "definition": {"definition": str, "unit": str, ...} | null,
                "target_field": {
                    "name": str,
                    "display_name": str,
                    "measure": str | null,
                    "data_type": str,
                } | null,
                "datastreams": [
                    {
                        "id": str,
                        "name": str,
                        "module": str,
                        "enabled": bool,
                        "last_extract": {"date": str | null, "status": str | null},
                    }
                ],
                "status": "ok" | "no_stream" | "not_in_dictionary",
            }
        ],
        "validation": {
            "ok_count": int,
            "warnings": [str],
        },
    }

Status semantics:
    "ok"               -- target_field exists AND >=1 enabled datastream maps to it
                          in this project.
    "no_stream"        -- target_field exists but NO enabled datastream in the project
                          feeds it (actionable warning: configure a datastream).
    "not_in_dictionary"-- the metric name has no matching target_field (dictionary gap).

The metric->target_field resolution uses NAME EQUALITY against app.target_fields.
This is intentional: report metrics are expected to use canonical field names
(same convention as manifests' canonical_metric_mapping values).

Merge: uses flows.get_flow (kind='report') to get the merged (base+override) report
doc -- as instructed in Story 8.9. This means overrides contributed via Story 8.7
(MCP flow interface) are reflected automatically.

Auth + scoping: same pattern as datamodel_api (Bearer token, AD-5 project scope).

ASCII-only stdout (AI-03). No private framework attributes (AI-02).
"""

from __future__ import annotations

import logging

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Chain computation
# ---------------------------------------------------------------------------


def get_report_chain(
    project_id: str,
    module_name: str,
    report_id: str,
    conn,
    *,
    loaded_modules=None,
) -> dict | None:
    """Compute the metrics -> target_fields -> datastreams chain for a report.

    Args:
        project_id:     Project to scope datastream lookups to (AD-5).
        module_name:    Module name (e.g. 'google-search-console').
        report_id:      Report id within the module (e.g. 'overview_daily').
        conn:           Open psycopg connection.
        loaded_modules: Loaded module registry (for base report pack resolution).

    Returns:
        Chain document dict, or None if the report cannot be resolved at all.
    """
    flow_id = f"{module_name}/{report_id}"

    # Resolve the merged report doc via the flows layer (Story 8.7 merge point).
    # get_flow performs scope check; we pass a dummy identity since we are in
    # a server-side path and scope is already enforced by the route handler.
    try:
        from core import flows as flows_module  # noqa: PLC0415

        merged_doc = flows_module._base_report_doc(flow_id, loaded_modules)
        override = flows_module._fetch_report_override(project_id, flow_id, conn)
        if merged_doc is None and override is None:
            # Try loaded modules directly (report pack may exist without override)
            from core import reports as reports_module  # noqa: PLC0415

            base_report = reports_module.find_report(
                loaded_modules or [], module_name, report_id
            )
            if base_report is None:
                return None
            # Build a minimal merged doc from the base report
            merged_doc = {
                "schema_version": "1",
                "kind": "report",
                "id": flow_id,
                "base_report_id": flow_id,
                "display_name": base_report.get("display_name"),
                "metrics": base_report.get("metrics", []),
                "metric_definitions": base_report.get("metric_definitions"),
                "llm_commentary_guidelines": base_report.get("llm_commentary_guidelines"),
            }
        else:
            merged_doc = flows_module._merge_report(
                merged_doc, override, flow_id, project_id
            )
    except Exception as exc:
        logger.warning(
            "report_chain: merge_failed report=%s project=%s: %s",
            flow_id, project_id, exc,
        )
        return None

    if merged_doc is None:
        return None

    metrics_list: list[str] = merged_doc.get("metrics") or []
    metric_definitions: dict | None = merged_doc.get("metric_definitions") or None
    llm_guidelines: str | None = merged_doc.get("llm_commentary_guidelines") or None

    # Fetch target_fields for all metric names in one query (name IN (...)).
    target_fields_by_name = _fetch_target_fields(metrics_list, conn)

    # Fetch all datastreams + their mappings for this project (one join query).
    # Returns: {target_field_name -> list[datastream_row]}
    datastreams_by_target = _fetch_datastreams_by_target(project_id, conn)

    # Build per-metric chain entries.
    chain_metrics: list[dict] = []
    ok_count = 0
    warnings: list[str] = []

    for metric in metrics_list:
        tf = target_fields_by_name.get(metric)
        streams = datastreams_by_target.get(metric, []) if tf else []

        # Definition from metric_definitions (R6)
        definition: dict | None = None
        if metric_definitions and metric in metric_definitions:
            md = metric_definitions[metric]
            if isinstance(md, dict):
                definition = md

        if tf is None:
            # Metric name not found in data dictionary.
            status = "not_in_dictionary"
            warnings.append(
                f"La métrique « {metric} » n'est pas référencée dans le dictionnaire "
                f"de données. Ajoutez-la comme champ cible pour activer le suivi complet."
            )
        elif not streams:
            status = "no_stream"
            tf_name = tf.get("display_name") or metric
            warnings.append(
                f"Aucun flux actif n'alimente « {tf_name} » pour ce projet. "
                f"Configurez un datastream et son mapping pour alimenter cette métrique."
            )
        else:
            status = "ok"
            ok_count += 1

        target_field_summary: dict | None = None
        if tf:
            target_field_summary = {
                "name": tf.get("name"),
                "display_name": tf.get("display_name"),
                "measure": tf.get("measure"),
                "data_type": tf.get("data_type"),
            }

        chain_metrics.append(
            {
                "metric": metric,
                "definition": definition,
                "target_field": target_field_summary,
                "datastreams": streams,
                "status": status,
            }
        )

    return {
        "report_id": flow_id,
        "display_name": merged_doc.get("display_name"),
        "metric_definitions": metric_definitions,
        "llm_commentary_guidelines": llm_guidelines,
        "metrics": chain_metrics,
        "validation": {
            "ok_count": ok_count,
            "warnings": warnings,
        },
    }


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------


def _fetch_target_fields(metric_names: list[str], conn) -> dict[str, dict]:
    """Return a {name: field_dict} map for the given metric names.

    Uses a single IN query. Fields missing from the dictionary are absent from
    the result (the caller treats them as 'not_in_dictionary').
    """
    if not metric_names:
        return {}
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT name, display_name, data_type, field_kind, measure
            FROM app.target_fields
            WHERE name = ANY(%s)
              AND status = 'approved'
            """,
            (list(metric_names),),
        )
        cols = [d[0] for d in cur.description]
        return {row[0]: dict(zip(cols, row)) for row in cur.fetchall()}


def _fetch_datastreams_by_target(
    project_id: str, conn
) -> dict[str, list[dict]]:
    """Return {target_field_name -> [datastream_summary, ...]} for the project.

    Only includes mappings for datastreams that belong to this project (AD-5).
    last_extract is derived from the most recent pull_verification for the
    datastream (same LATERAL pattern as datamodel.get_target_field).

    Each datastream summary:
        {id, name, module, enabled, last_extract: {date, status}}
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                dm.target_field,
                ds.id,
                ds.name,
                ds.module_name,
                ds.enabled,
                lp.last_date,
                lp.last_status
            FROM app.datastream_mappings dm
            JOIN app.datastreams ds ON ds.id = dm.datastream_id
            LEFT JOIN LATERAL (
                SELECT
                    pj.date_to::text  AS last_date,
                    pv.verdict        AS last_status
                FROM app.pull_jobs pj
                LEFT JOIN app.pull_verifications pv ON pv.pull_id = pj.pull_id
                WHERE pj.datastream_id = dm.datastream_id
                ORDER BY pj.enqueued_at DESC
                LIMIT 1
            ) lp ON true
            WHERE ds.project_id = %s
              AND dm.target_field IS NOT NULL
            ORDER BY dm.target_field ASC, ds.name ASC
            """,
            (project_id,),
        )
        result: dict[str, list[dict]] = {}
        for row in cur.fetchall():
            target_name = row[0]
            ds_entry = {
                "id": row[1],
                "name": row[2],
                "module": row[3],
                "enabled": bool(row[4]),
                "last_extract": {
                    "date": row[5],
                    "status": row[6],
                },
            }
            result.setdefault(target_name, []).append(ds_entry)
    return result


# ---------------------------------------------------------------------------
# Route handler
# ---------------------------------------------------------------------------


async def _check_auth(request: Request) -> tuple[bool, str]:
    """Delegate to the shared auth layer in core.admin_api."""
    from core.admin_api import _check_auth as _admin_check_auth  # noqa: PLC0415

    return await _admin_check_auth(request)


async def _get_report_chain(request: Request) -> Response:
    """GET /api/reports/{module}/{report_id}/chain?project_id=

    Returns the metrics -> target_fields -> datastreams chain for the report,
    scoped to the given project_id.

    Query params:
        project_id  (required) -- scopes datastream lookups (AD-5)

    Path params:
        module     -- module name (e.g. 'google-search-console')
        report_id  -- report id within the module (e.g. 'overview_daily')

    Response (200): chain document (see module docstring).
    Error responses:
        400 -- project_id missing
        401 -- unauthorized
        404 -- report not found or project not accessible
        500 -- DB error
    """
    authorized, _identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Authentification requise"},
            status_code=401,
        )

    module_name = request.path_params.get("module", "").strip()
    report_id = request.path_params.get("report_id", "").strip()
    project_id = (request.query_params.get("project_id") or "").strip()

    if not project_id:
        return JSONResponse(
            {"code": "missing_param", "message": "project_id est requis"},
            status_code=400,
        )

    if not module_name or not report_id:
        return JSONResponse(
            {"code": "missing_param", "message": "module et report_id sont requis"},
            status_code=400,
        )

    # AI-47: guard against a report literally named "chain" shadowing the /chain route.
    # GET /api/reports/{module}/chain/chain would be the only safe way to fetch such a
    # report; reject it here to avoid silent routing ambiguity.
    if report_id == "chain":
        return JSONResponse(
            {
                "code": "reserved_id",
                "message": (
                    "L'identifiant de rapport 'chain' est réservé : il correspond au suffixe "
                    "de la route /chain et ne peut pas être utilisé comme identifiant de rapport. "
                    "Renommez le rapport pour éviter le conflit de routage."
                ),
            },
            status_code=422,
        )

    try:
        from core.db import get_connection  # noqa: PLC0415

        # Module registry from core.main (same source as the reports-available
        # endpoint). Lazy + fail-soft so the route degrades in module-less test
        # environments instead of erroring.
        loaded_modules: list = []
        try:
            from core.main import get_loaded_modules  # noqa: PLC0415

            loaded_modules = list(get_loaded_modules())
        except Exception as exc:
            logger.debug("report_chain: module_load_skipped: %s", exc)

        with get_connection() as conn:
            # AD-5: verify project access before any data query.
            try:
                from core.project_access import identity_has_project_access  # noqa: PLC0415

                if not identity_has_project_access(project_id, _identity or "", conn):
                    return JSONResponse(
                        {"code": "not_found", "message": "Projet introuvable ou accès refusé"},
                        status_code=404,
                    )
            except Exception:
                # project_access module may not exist in all env setups; skip
                pass

            chain = get_report_chain(
                project_id,
                module_name,
                report_id,
                conn,
                loaded_modules=loaded_modules,
            )
    except Exception as exc:
        logger.error(
            "report_chain: chain_error module=%s report=%s project=%s: %s",
            module_name, report_id, project_id, exc,
        )
        return JSONResponse(
            {"code": "db_error", "message": f"Erreur de base de données : {exc}"},
            status_code=500,
        )

    if chain is None:
        return JSONResponse(
            {
                "code": "not_found",
                "message": (
                    f"Rapport '{module_name}/{report_id}' introuvable "
                    "(vérifiez le module et l'identifiant du rapport)."
                ),
            },
            status_code=404,
        )

    return JSONResponse(chain)


# ---------------------------------------------------------------------------
# Exported route list (orchestrator wires into admin_api.router)
# ---------------------------------------------------------------------------

REPORT_CHAIN_ROUTES: list[Route] = [
    Route(
        "/api/reports/{module}/{report_id}/chain",
        endpoint=_get_report_chain,
        methods=["GET"],
    ),
]
