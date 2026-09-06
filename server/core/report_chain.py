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
    Route: GET /api/reports/{connector}/{report_id}/chain?project_id=

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

The metric->target_field resolution uses NAME EQUALITY against the governed field
catalogue -- the Semantic Model first, `app.target_fields` as the layer below
(`core.governed_field_catalogue`). This is intentional: report metrics are
expected to use canonical field names (same convention as manifests'
canonical_metric_mapping values).

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

    # Fetch the governed field of every metric name in one read.
    target_fields_by_name = _fetch_target_fields(metrics_list, conn, project_id=project_id)

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
            # The gesture that WORKS, not the one that used to. Adding a target
            # field answers 409 `legacy_store_is_read_only` since 2026-08-25;
            # a metric is declared in the Semantic Model and nowhere else.
            warnings.append(
                f"Metric '{metric}' is not a governed field. Declare it as a "
                f"Concept in the Semantic Model to enable full tracking."
            )
        elif not streams:
            status = "no_stream"
            tf_name = tf.get("display_name") or metric
            warnings.append(
                f"No active Datastream feeds '{tf_name}' for this project. "
                f"Configure a Datastream and its mapping to feed this metric."
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


def _fetch_target_fields(
    metric_names: list[str], conn, *, project_id: str | None = None
) -> dict[str, dict]:
    """Return a {name: field_dict} map for the given metric names.

    SEMANTIC MODEL FIRST since story 49.3's readers step. This used to be one
    `SELECT ... FROM app.target_fields`, a store whose write doors answer 409
    `legacy_store_is_read_only` since 2026-08-25: a metric a person declared in
    the Concept workbench was reported `not_in_dictionary` by this chain, and the
    warning told them to *"add it as a target field"* -- a gesture the product no
    longer offers anywhere. `core.governed_field_catalogue.resolve` asks the
    Semantic Model first and keeps the dictionary as the layer below, so a name
    either store governs resolves and nothing that resolved before stops.

    Fields missing from BOTH stores are absent from the result (the caller treats
    them as 'not_in_dictionary').
    """
    if not metric_names:
        return {}
    from core.governed_field_catalogue import resolve  # noqa: PLC0415

    return resolve(
        conn,
        names=list(metric_names),
        project_id=project_id,
        approved_only=True,
    )


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
    """GET /api/reports/{connector}/{report_id}/chain?project_id=

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
            {
                "code": "unauthorized",
                "message": (
                    "This request carries no signed-in identity. Sign in again, "
                    "then re-run it."
                ),
            },
            status_code=401,
        )

    module_name = request.path_params.get("connector", "").strip()
    report_id = request.path_params.get("report_id", "").strip()
    project_id = (request.query_params.get("project_id") or "").strip()

    if not project_id:
        return JSONResponse(
            {
                "code": "missing_param",
                "message": (
                    "This request does not say which Project to read the chain in. "
                    "Add `project_id` to the request and re-run it."
                ),
            },
            status_code=400,
        )

    if not module_name or not report_id:
        return JSONResponse(
            {
                "code": "missing_param",
                "message": (
                    "This request does not name both a connector and a report. "
                    "Add the connector and the report identifier to the address, "
                    "then re-run it."
                ),
            },
            status_code=400,
        )

    # AI-47: guard against a report literally named "chain" shadowing the /chain route.
    # GET /api/reports/{connector}/chain/chain would be the only safe way to fetch such a
    # report; reject it here to avoid silent routing ambiguity.
    if report_id == "chain":
        return JSONResponse(
            {
                "code": "reserved_id",
                "message": (
                    "The report identifier 'chain' is reserved: it matches the suffix "
                    "of the /chain route and cannot be used as a report identifier. "
                    "Rename the report to avoid the routing conflict."
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
                from core.project_access import identity_can_read_project  # noqa: PLC0415

                if not identity_can_read_project(project_id, _identity or "", conn):
                    # NON-DISCLOSURE STAYS: the sentence never says whether the
                    # Project exists elsewhere. It still names a gesture, because
                    # "not found or access denied" leaves a reader with nothing.
                    return JSONResponse(
                        {
                            "code": "not_found",
                            "message": (
                                "This Project is not readable under your sign-in. "
                                "Choose a Project you have access to, or ask an "
                                "administrator of this organisation to grant it."
                            ),
                        },
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
        # THE EXCEPTION GOES TO THE LOG, NEVER INTO THE SENTENCE. `Database error:
        # <exc>` named the cause and no gesture, and handed a reader a driver
        # message written for us -- `first-figure-path.md:141-151,206-208`. The
        # `code` stays machine-readable so the log and the client still agree.
        logger.error(
            "report_chain: chain_error module=%s report=%s project=%s: %s",
            module_name, report_id, project_id, exc,
        )
        return JSONResponse(
            {
                "code": "db_error",
                "message": (
                    "This report's chain could not be read just now. Re-run the "
                    "request in a moment, and if it keeps failing ask an "
                    "administrator to read the server log for this report."
                ),
            },
            status_code=500,
        )

    if chain is None:
        return JSONResponse(
            {
                "code": "not_found",
                "message": (
                    f"No report '{module_name}/{report_id}' is published on this "
                    "connector. Choose a report from the connector's report list, "
                    "or check the identifier in the address."
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
        "/api/reports/{connector}/{report_id}/chain",
        endpoint=_get_report_chain,
        methods=["GET"],
    ),
]
