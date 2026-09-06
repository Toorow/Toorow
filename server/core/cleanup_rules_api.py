"""toorow -- REST surface of the cleanup rules (Story 60.3).

Exports CLEANUP_RULE_ROUTES: list[Route] -- a flat list admin_api.py splices into
its router at startup. Never imported by admin_api at module level (no circular
import), the pattern of `dimension_lineage_api.py` and `value_mapping_api.py`.

Routes (all under one Project address, because Governance is where a rule is
written -- `datastream-workbench-and-wizard.md:989` and `data.md:82-85` say the
Processing tab references governed rules and does not redefine them):

  GET    /api/projects/{project_id}/cleanup-rules
  POST   /api/projects/{project_id}/cleanup-rules
  POST   /api/projects/{project_id}/cleanup-rules/preview
  GET    /api/projects/{project_id}/cleanup-rules/{rule_id}
  PATCH  /api/projects/{project_id}/cleanup-rules/{rule_id}
  DELETE /api/projects/{project_id}/cleanup-rules/{rule_id}
  GET    /api/projects/{project_id}/cleanup-rules/{rule_id}/effect

FIVE REFUSALS THIS MODULE EXISTS FOR:

  * a `project_id` the guarded org does not own -> **404**, never 403:
    existence-hiding, lesson F-3 of 27.2 (`dimension_lineage_api.py:17-19`).
  * a pattern over its bound, or using a construct RE2 does not implement ->
    **422** `cleanup_rule_refused`, carrying the sentence
    `core.cleanup_rules.validate_pattern` raised. The codes are the
    `ExpressionError` family of `derived_columns.py:171-218`; none is invented
    here, and none comes from the adaptation sandbox -- that path is not taken by
    this story.
  * a compiled SELECT the WAREHOUSE refuses -> **422** `cleanup_rule_refused`
    with the engine's own message, and NOTHING is stored. The dry run happens
    before the INSERT, exactly like `derived_columns.build_dry_run_sql:227-239`
    is meant to be used.
  * an impact read that FAILS -> **503** `cleanup_rule_impact_unavailable` with
    `impact_state: "unknown"` and no count of any kind. An empty answer would say
    "nothing depends on this", which was not observed
    (`value_mapping_tables.assess_table_impact:251-257`).
  * a measured effect that could NOT be measured -> `effect_state: "unmeasured"`
    with the reason, and no number. Never a `0`: a zero would say "this rule
    removes nothing", which is the one thing a person must not be told wrongly
    about a rule that removes rows.

THE WAREHOUSE RUNNER BELOW RAISES INSTEAD OF ANSWERING `[]`. `warehouse
._query_duckdb:186-197` returns an empty list when the mart file does not exist
-- a silent success that a preview would render as "no row matches this pattern".
`_run_mart_query` proves the relation is there first and raises
`WarehouseUnavailable` otherwise, the distinction `warehouse
._duckdb_relation_exists:161` was written for.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime, timedelta

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

logger = logging.getLogger(__name__)

_BASE = "/api/projects/{project_id}/cleanup-rules"


# ---------------------------------------------------------------------------
# Auth and org guard -- the shape of `value_mapping_api._guard`.
# ---------------------------------------------------------------------------


async def _check_auth(request: Request) -> tuple[bool, str]:
    from core.admin_api import _check_auth as _admin_check_auth  # noqa: PLC0415

    return await _admin_check_auth(request)


def _unauthorized() -> Response:
    return JSONResponse(
        {"code": "unauthorized", "message": "Authentication is required."}, status_code=401
    )


def _not_found(message: str = "Project not found.") -> Response:
    return JSONResponse({"code": "not_found", "message": message}, status_code=404)


def _server_error() -> Response:
    return JSONResponse({"code": "server_error", "message": "Server error."}, status_code=500)


def _refused(message: str) -> Response:
    return JSONResponse({"code": "cleanup_rule_refused", "message": message}, status_code=422)


def _impact_unavailable(detail: str) -> Response:
    return JSONResponse(
        {
            "code": "cleanup_rule_impact_unavailable",
            "message": (
                "The number of Datastreams this rule reaches could not be read. "
                'Nothing is written and no count is shown: "I could not check" is '
                'not "nothing depends on this".'
            ),
            "impact_state": "unknown",
            "datastream_count": None,
            "detail": detail,
        },
        status_code=503,
    )


def _guard(project_id: str, identity: str, *, manage: bool) -> tuple[str | None, Response | None]:
    """Resolve the org of *project_id* and verify the caller may touch it.

    Reads require org membership, writes require org-manage, and a project that
    cannot be resolved is 404 rather than a fall-through to an unguarded read
    (`dimension_lineage_api.py:14-19`).
    """
    from core.db import get_connection  # noqa: PLC0415
    from core.project_access import (  # noqa: PLC0415
        identity_can_manage_org,
        identity_has_org_access,
    )

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT org_id FROM app.projects WHERE id = %s", (project_id,))
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
        logger.error("cleanup_rules_api: guard failed project=%s: %s", project_id, exc)
        return None, _server_error()

    if not allowed:
        if manage:
            return None, JSONResponse(
                {"code": "forbidden", "message": "Insufficient rights."}, status_code=403
            )
        # A reader who is not a member must not learn the Project exists.
        return None, _not_found()
    return org_id, None


async def _body(request: Request) -> tuple[dict | None, Response | None]:
    try:
        parsed = json.loads(await request.body() or b"{}")
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None, JSONResponse(
            {"code": "invalid_json", "message": "Invalid JSON body."}, status_code=400
        )
    if not isinstance(parsed, dict):
        return None, JSONResponse(
            {"code": "invalid_json", "message": "Invalid JSON body."}, status_code=400
        )
    return parsed, None


def _store_refusal(exc: Exception) -> Response | None:
    """Map a store refusal to its status. Returns None when it is not one."""
    from core import cleanup_rules as store  # noqa: PLC0415

    if isinstance(exc, store.CleanupRuleImpactUnavailable):
        return _impact_unavailable(str(exc))
    if isinstance(exc, store.CleanupRuleNotFound):
        return _not_found("Cleanup rule not found.")
    if isinstance(exc, store.CleanupRuleConflict):
        return JSONResponse(
            {"code": "cleanup_rule_conflict", "message": str(exc)}, status_code=409
        )
    if isinstance(exc, store.ExpressionError):
        return _refused(str(exc))
    return None


# ---------------------------------------------------------------------------
# The warehouse. One dialect at a time, and never a silent empty answer.
# ---------------------------------------------------------------------------


def _dialect() -> str:
    """Which of the two dialects this deployment speaks, from the one authority.

    `warehouse._db_mode` is the single switch between the DuckDB fixture chain and
    BigQuery. Reading it here rather than declaring a dialect in the row is why
    one stored pattern serves both.
    """
    from core import warehouse  # noqa: PLC0415

    return "bigquery" if warehouse._db_mode() == "bigquery" else "duckdb"


def _mart_prefix(project_id: str) -> str:
    from core import warehouse_tenancy  # noqa: PLC0415

    return warehouse_tenancy.mart_prefix(project_id)


def _run_mart_query(sql: str, params: list, *, project_id: str) -> list[dict]:
    """Execute against the mart, RAISING when the mart is not there.

    `warehouse._query_duckdb` answers `[]` when the file is absent. A preview
    built on that answer would render "no row matches" for "there is no
    warehouse", so the relation is proven first.
    """
    from core import warehouse  # noqa: PLC0415
    from core.cleanup_rules import MART_RELATION  # noqa: PLC0415

    if warehouse._db_mode() == "bigquery":
        return warehouse._query_bigquery(sql, params)
    path = warehouse._duckdb_path()
    if not path or not os.path.exists(path):
        raise warehouse.WarehouseUnavailable(
            "No local warehouse file is present, so no engine could judge this rule."
        )
    if not warehouse._duckdb_relation_exists(path, MART_RELATION):
        raise warehouse.WarehouseUnavailable(
            f"The warehouse holds no {MART_RELATION} relation, so no engine could "
            "judge this rule."
        )
    return warehouse._query_duckdb(sql, params)


def _dry_run(
    *, project_id: str, rule_kind: str, source_field: str, pattern: str
) -> tuple[str, str | None, Response | None]:
    """Submit the compiled SELECT before storing anything.

    Returns `(dry_run_state, detail, refusal)`. Three outcomes, and they are three
    different facts:
      * the engine accepted it        -> ("passed", None, None)
      * no engine could be reached    -> ("not_attempted", why, None)
      * the engine REFUSED it         -> the refusal, and nothing is stored.
    """
    from core import warehouse  # noqa: PLC0415
    from core.cleanup_rules import build_dry_run_sql  # noqa: PLC0415

    dialect = _dialect()
    sql, params = build_dry_run_sql(
        rule_kind=rule_kind,
        source_field=source_field,
        pattern=pattern,
        dialect=dialect,
        mart_prefix=_mart_prefix(project_id),
    )
    try:
        _run_mart_query(sql, params, project_id=project_id)
    except warehouse.WarehouseUnavailable as exc:
        return "not_attempted", f"{dialect}: {exc}"[:2000], None
    except Exception as exc:  # noqa: BLE001 -- the engine's own words are the answer
        return (
            "not_attempted",
            None,
            _refused(f"The warehouse refused this rule ({dialect}): {str(exc)[:400]}"),
        )
    return "passed", f"{dialect}: accepted by a dry run over {_mart_prefix(project_id)}", None


# ---------------------------------------------------------------------------
# Collection
# ---------------------------------------------------------------------------


async def _list_rules(request: Request) -> Response:
    authorized, identity = await _check_auth(request)
    if not authorized:
        return _unauthorized()
    project_id = request.path_params["project_id"]
    org_id, refusal = _guard(project_id, identity, manage=False)
    if refusal is not None:
        return refusal

    from core.cleanup_rules import list_rules  # noqa: PLC0415
    from core.db import get_connection  # noqa: PLC0415

    try:
        with get_connection() as conn:
            rules = list_rules(conn, project_id=project_id)
    except Exception as exc:  # noqa: BLE001
        logger.error("cleanup_rules_api: list failed project=%s: %s", project_id, exc)
        return _server_error()
    return JSONResponse(
        {
            "project_id": project_id,
            "organization_id": org_id,
            "rules": rules,
            # Stated rather than inferred from an empty list, so a screen never has
            # to guess whether the read happened.
            "impact_state": "known",
        }
    )


async def _create_rule(request: Request) -> Response:
    authorized, identity = await _check_auth(request)
    if not authorized:
        return _unauthorized()
    project_id = request.path_params["project_id"]
    body, refusal = await _body(request)
    if refusal is not None:
        return refusal
    org_id, refusal = _guard(project_id, identity, manage=True)
    if refusal is not None:
        return refusal

    from core.cleanup_rules import (  # noqa: PLC0415
        assess_rule_impact,
        create_rule,
        validate_field,
        validate_kind,
        validate_pattern,
    )
    from core.db import get_connection  # noqa: PLC0415

    name = str(body.get("name") or "").strip()
    source_field = str(body.get("source_field") or "").strip()
    rule_kind = str(body.get("rule_kind") or "").strip()
    pattern = body.get("pattern")
    datastream_id = (body.get("datastream_id") or None) or None
    if not name:
        return JSONResponse(
            {"code": "missing_param", "message": "name is required."}, status_code=422
        )
    # Structural refusals FIRST: nothing is sent to a warehouse and no impact is
    # read for a rule that could never be stored.
    try:
        validate_kind(rule_kind)
        validate_field(source_field)
        validate_pattern(pattern if isinstance(pattern, str) else "")
    except Exception as exc:  # noqa: BLE001
        mapped = _store_refusal(exc)
        return mapped if mapped is not None else _server_error()

    state, detail, engine_refusal = _dry_run(
        project_id=project_id, rule_kind=rule_kind, source_field=source_field, pattern=pattern
    )
    if engine_refusal is not None:
        return engine_refusal

    try:
        with get_connection() as conn:
            # The reach is READ before the write, so the answer carries the number
            # the confirmation named.
            impact = assess_rule_impact(
                conn, project_id=project_id, datastream_id=datastream_id
            )
            rule = create_rule(
                conn,
                org_id=org_id,
                project_id=project_id,
                datastream_id=datastream_id,
                name=name,
                source_field=source_field,
                rule_kind=rule_kind,
                pattern=pattern,
                identity=identity,
                dry_run_state=state,
                dry_run_detail=detail,
                enabled=bool(body.get("enabled", True)),
            )
            conn.commit()
    except Exception as exc:  # noqa: BLE001
        mapped = _store_refusal(exc)
        if mapped is not None:
            return mapped
        logger.error("cleanup_rules_api: create failed project=%s: %s", project_id, exc)
        return _server_error()
    return JSONResponse({**rule, **impact.as_dict()}, status_code=201)


async def _preview(request: Request) -> Response:
    """Real rows, or a named refusal. Never a fabricated row and never a partial
    render presented as a whole one (`derived_columns.PreviewResult:250-252`)."""
    authorized, identity = await _check_auth(request)
    if not authorized:
        return _unauthorized()
    project_id = request.path_params["project_id"]
    body, refusal = await _body(request)
    if refusal is not None:
        return refusal
    _, refusal = _guard(project_id, identity, manage=False)
    if refusal is not None:
        return refusal

    from core.cleanup_rules import MAX_PREVIEW_ROWS, preview_rule  # noqa: PLC0415

    try:
        limit = int(body.get("limit") or 20)
    except (TypeError, ValueError):
        return _refused(f"the preview is limited to between 1 and {MAX_PREVIEW_ROWS} rows")
    try:
        result = preview_rule(
            rule_kind=str(body.get("rule_kind") or ""),
            source_field=str(body.get("source_field") or ""),
            pattern=body.get("pattern") if isinstance(body.get("pattern"), str) else "",
            dialect=_dialect(),
            mart_prefix=_mart_prefix(project_id),
            project_id=project_id,
            run_query=lambda sql, params: _run_mart_query(sql, params, project_id=project_id),
            limit=limit,
        )
    except Exception as exc:  # noqa: BLE001
        mapped = _store_refusal(exc)
        if mapped is not None:
            return mapped
        logger.error("cleanup_rules_api: preview failed project=%s: %s", project_id, exc)
        return _server_error()
    if not result.ok:
        return JSONResponse(
            {
                "code": "cleanup_rule_preview_unavailable",
                "message": result.error,
                "rows": [],
                "preview_state": "unavailable",
            },
            status_code=503,
        )
    return JSONResponse(
        {
            "rows": [dict(row) for row in result.rows],
            "row_count": len(result.rows),
            "preview_state": "available",
        }
    )


# ---------------------------------------------------------------------------
# One rule
# ---------------------------------------------------------------------------


async def _get_one(request: Request) -> Response:
    authorized, identity = await _check_auth(request)
    if not authorized:
        return _unauthorized()
    project_id = request.path_params["project_id"]
    _, refusal = _guard(project_id, identity, manage=False)
    if refusal is not None:
        return refusal

    from core.cleanup_rules import assess_rule_impact, get_rule  # noqa: PLC0415
    from core.db import get_connection  # noqa: PLC0415

    try:
        with get_connection() as conn:
            rule = get_rule(
                conn, rule_id=request.path_params["rule_id"], project_id=project_id
            )
            impact = assess_rule_impact(
                conn, project_id=project_id, datastream_id=rule["datastream_id"]
            )
    except Exception as exc:  # noqa: BLE001
        mapped = _store_refusal(exc)
        if mapped is not None:
            return mapped
        logger.error("cleanup_rules_api: read failed project=%s: %s", project_id, exc)
        return _server_error()
    return JSONResponse({"rule": rule, **impact.as_dict()})


async def _patch_one(request: Request) -> Response:
    authorized, identity = await _check_auth(request)
    if not authorized:
        return _unauthorized()
    project_id = request.path_params["project_id"]
    body, refusal = await _body(request)
    if refusal is not None:
        return refusal
    _, refusal = _guard(project_id, identity, manage=True)
    if refusal is not None:
        return refusal

    from core.cleanup_rules import get_rule, update_rule  # noqa: PLC0415
    from core.db import get_connection  # noqa: PLC0415

    rule_id = request.path_params["rule_id"]
    try:
        with get_connection() as conn:
            before = get_rule(conn, rule_id=rule_id, project_id=project_id)
    except Exception as exc:  # noqa: BLE001
        mapped = _store_refusal(exc)
        if mapped is not None:
            return mapped
        logger.error("cleanup_rules_api: patch read failed rule=%s: %s", rule_id, exc)
        return _server_error()

    # A changed pattern, field or kind is a changed SELECT, so it is dry-run again
    # before it is stored. Enabling or disabling changes neither.
    pattern = body.get("pattern") if isinstance(body.get("pattern"), str) else None
    rule_kind = str(body.get("rule_kind") or "") or None
    source_field = str(body.get("source_field") or "") or None
    state: str | None = None
    detail: str | None = None
    if pattern is not None or rule_kind is not None or source_field is not None:
        state, detail, engine_refusal = _dry_run(
            project_id=project_id,
            rule_kind=rule_kind or before["rule_kind"],
            source_field=source_field or before["source_field"],
            pattern=pattern if pattern is not None else before["pattern"],
        )
        if engine_refusal is not None:
            return engine_refusal

    try:
        with get_connection() as conn:
            rule = update_rule(
                conn,
                rule_id=rule_id,
                project_id=project_id,
                identity=identity,
                name=body.get("name"),
                pattern=pattern,
                rule_kind=rule_kind,
                source_field=source_field,
                enabled=body.get("enabled"),
                dry_run_state=state,
                dry_run_detail=detail,
            )
            conn.commit()
    except Exception as exc:  # noqa: BLE001
        mapped = _store_refusal(exc)
        if mapped is not None:
            return mapped
        logger.error("cleanup_rules_api: patch failed rule=%s: %s", rule_id, exc)
        return _server_error()
    return JSONResponse(rule)


async def _delete_one(request: Request) -> Response:
    authorized, identity = await _check_auth(request)
    if not authorized:
        return _unauthorized()
    project_id = request.path_params["project_id"]
    _, refusal = _guard(project_id, identity, manage=True)
    if refusal is not None:
        return refusal

    from core.cleanup_rules import delete_rule  # noqa: PLC0415
    from core.db import get_connection  # noqa: PLC0415

    try:
        with get_connection() as conn:
            result = delete_rule(
                conn,
                rule_id=request.path_params["rule_id"],
                project_id=project_id,
                identity=identity,
            )
            conn.commit()
    except Exception as exc:  # noqa: BLE001
        mapped = _store_refusal(exc)
        if mapped is not None:
            return mapped
        logger.error("cleanup_rules_api: delete failed project=%s: %s", project_id, exc)
        return _server_error()
    return JSONResponse(result)


async def _effect(request: Request) -> Response:
    """How many rows this rule changes over its window, or why that is unknown.

    `effect_state` is `measured` or `unmeasured`, and the second carries a
    sentence rather than a number. There is no third state and there is no zero
    by default: a rule reported as removing zero rows when nothing was counted is
    the exact lie this route exists to refuse.
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return _unauthorized()
    project_id = request.path_params["project_id"]
    _, refusal = _guard(project_id, identity, manage=False)
    if refusal is not None:
        return refusal

    from core.cleanup_rules import (  # noqa: PLC0415
        EFFECT_WINDOW_DAYS,
        build_effect_sql,
        get_rule,
    )
    from core.db import get_connection  # noqa: PLC0415

    rule_id = request.path_params["rule_id"]
    try:
        with get_connection() as conn:
            rule = get_rule(conn, rule_id=rule_id, project_id=project_id)
    except Exception as exc:  # noqa: BLE001
        mapped = _store_refusal(exc)
        if mapped is not None:
            return mapped
        logger.error("cleanup_rules_api: effect read failed rule=%s: %s", rule_id, exc)
        return _server_error()

    end = datetime.now(UTC).date()
    start = end - timedelta(days=EFFECT_WINDOW_DAYS - 1)
    sql, params = build_effect_sql(
        rule_kind=rule["rule_kind"],
        source_field=rule["source_field"],
        pattern=rule["pattern"],
        dialect=_dialect(),
        mart_prefix=_mart_prefix(project_id),
        project_id=project_id,
        start_date=start.isoformat(),
        end_date=end.isoformat(),
    )
    try:
        rows = _run_mart_query(sql, params, project_id=project_id)
    except Exception as exc:  # noqa: BLE001 -- unmeasured is a sentence, not a zero
        return JSONResponse(
            {
                "rule_id": rule_id,
                "effect_state": "unmeasured",
                "affected_rows": None,
                "window_days": EFFECT_WINDOW_DAYS,
                "message": (
                    "The rows this rule changes could not be counted: "
                    f"{str(exc)[:300]} This is not a count of zero."
                ),
            }
        )
    value = rows[0].get("affected_rows") if rows else None
    if value is None:
        return JSONResponse(
            {
                "rule_id": rule_id,
                "effect_state": "unmeasured",
                "affected_rows": None,
                "window_days": EFFECT_WINDOW_DAYS,
                "message": (
                    "The warehouse answered without a count, so the effect of this "
                    "rule is unknown. This is not a count of zero."
                ),
            }
        )
    return JSONResponse(
        {
            "rule_id": rule_id,
            "effect_state": "measured",
            "affected_rows": int(value),
            "window_days": EFFECT_WINDOW_DAYS,
        }
    )


CLEANUP_RULE_ROUTES: list[Route] = [
    Route(_BASE, _list_rules, methods=["GET"]),
    Route(_BASE, _create_rule, methods=["POST"]),
    # Declared before `{rule_id}` so a literal segment is never eaten by a
    # parameter of the same shape.
    Route(f"{_BASE}/preview", _preview, methods=["POST"]),
    Route(f"{_BASE}/{{rule_id}}", _get_one, methods=["GET"]),
    Route(f"{_BASE}/{{rule_id}}", _patch_one, methods=["PATCH"]),
    Route(f"{_BASE}/{{rule_id}}", _delete_one, methods=["DELETE"]),
    Route(f"{_BASE}/{{rule_id}}/effect", _effect, methods=["GET"]),
]

__all__ = ["CLEANUP_RULE_ROUTES"]
