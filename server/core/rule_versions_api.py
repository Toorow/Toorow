"""toorow -- REST surface of the transformation rule histories (Story 60.5).

Exports RULE_VERSION_ROUTES: list[Route] -- a flat list admin_api.py splices into
its router at startup. Never imported by admin_api at module level (no circular
import), the pattern of `dimension_lineage_api.py` and `value_mapping_api.py`.

  GET  /api/projects/{project_id}/rule-versions/{object_kind}/{object_id}
  POST /api/projects/{project_id}/rule-versions/{object_kind}/{object_id}/preview

ONE MODULE FOR BOTH FAMILIES, AND THAT IS THE POINT. A value mapping table (60.1)
and a cleanup rule (60.3) live in two different stores with two different APIs,
and their history is the SAME question asked twice. Answering it in each of their
modules would give the console two shapes for one screen, and the second one to be
written would drift. `object_kind` is validated against `core.rule_versions.LEDGERS`
and against nothing else, so an unregistered family is a 404 rather than a query
built from a caller's string.

THE PREVIEW IS WHAT MAKES THE CONFIRMATION HONEST. It answers, BEFORE the change:
the version number that would be created, the content hash of the proposed body,
the Datastreams the change reaches BY NAME, and the sentence that separates the
future from the past (`rule_versions.NO_BACKFILL_FACT` -- the GENERIC half of a
statement the retired `geographic_change` module composed, declared once at its
reader; the geographic half would be false on either of these two objects). The
caller sends the SAME
fields it is about to PATCH, and the server
composes the proposed body with the same function the write uses -- so the hash a
person confirms is the hash that lands.

AND THE FAN-OUT NEVER DEFAULTS TO ZERO. A fan-out that could not be read comes
back `impact_state: "unknown"` with `datastream_count: null` and an empty list. A
`0` there would tell a person that nothing depends on the rule they are about to
change, which is the one thing they must not be told wrongly.

Guards: reading a history requires org MEMBERSHIP, previewing a change requires
org-manage -- the same split and the same shape as `value_mapping_api._guard`, and
a `project_id` the guarded org does not own answers 404 rather than 403, so a
non-member never learns the Project exists.
"""

from __future__ import annotations

import json
import logging

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

logger = logging.getLogger(__name__)

_BASE = "/api/projects/{project_id}/rule-versions/{object_kind}/{object_id}"


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


def _guard(project_id: str, identity: str, *, manage: bool) -> tuple[str | None, Response | None]:
    """The guard of `value_mapping_api._guard`, and for the same three reasons."""
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
        logger.error("rule_versions_api: guard failed project=%s: %s", project_id, exc)
        return None, _server_error()

    if not allowed:
        if manage:
            return None, JSONResponse(
                {"code": "forbidden", "message": "Insufficient rights."}, status_code=403
            )
        return None, _not_found()
    return org_id, None


def _resolve_kind(object_kind: str):
    """The family, or None. Never a table name built from a caller's string."""
    from core.rule_versions import UnknownRuleFamily, ledger_for  # noqa: PLC0415

    try:
        return ledger_for(object_kind)
    except UnknownRuleFamily:
        return None


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


# ---------------------------------------------------------------------------
# The history
# ---------------------------------------------------------------------------


async def _list_versions(request: Request) -> Response:
    """Every recorded version of one rule, newest first.

    An empty list and a failure are two different answers and they get two
    different status codes: `200` with `versions: []` means the ledger answered
    and nothing has been recorded yet; `503` means it could not be read, and the
    screen says so instead of saying "never edited".
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return _unauthorized()
    project_id = request.path_params["project_id"]
    org_id, refused = _guard(project_id, identity, manage=False)
    if refused is not None:
        return refused

    ledger = _resolve_kind(request.path_params["object_kind"])
    if ledger is None:
        return _not_found("No version ledger for that object kind.")

    from core.db import get_connection  # noqa: PLC0415
    from core.rule_versions import (  # noqa: PLC0415
        RuleVersionUnavailable,
        head_version,
        list_versions,
    )

    object_id = request.path_params["object_id"]
    try:
        with get_connection() as conn:
            if not _owns_object(conn, ledger, object_id, org_id=org_id, project_id=project_id):
                return _not_found("Rule not found.")
            versions = list_versions(conn, kind=ledger.kind, object_id=object_id)
            head = head_version(conn, kind=ledger.kind, object_id=object_id)
    except RuleVersionUnavailable as exc:
        return JSONResponse(
            {
                "code": "rule_version_history_unavailable",
                "message": (
                    "The version history could not be read. No version is listed, "
                    "because none was read -- this is not a rule that has never "
                    "been edited."
                ),
                "detail": str(exc),
            },
            status_code=503,
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("rule_versions_api: history failed object=%s: %s", object_id, exc)
        return _server_error()
    return JSONResponse(
        {
            "object_kind": ledger.kind,
            "object_id": object_id,
            "current_version_id": None if head is None else head["id"],
            "versions": versions,
        }
    )


def _owns_object(conn, ledger, object_id: str, *, org_id: str, project_id: str) -> bool:
    """The object exists inside the guarded org. A neighbour's rule is 404.

    Two different WHERE clauses because the two parents are scoped differently: a
    cleanup rule is Project-scoped by construction (240) and a value mapping
    table may be ORG-scoped and readable from every Project of the org (235).
    Widening either one would let a Project read a neighbour's history.
    """
    from core.rule_versions import KIND_CLEANUP_RULE  # noqa: PLC0415

    with conn.cursor() as cur:
        if ledger.kind == KIND_CLEANUP_RULE:
            cur.execute(
                "SELECT 1 FROM app.cleanup_rules WHERE id = %s AND project_id = %s",
                (object_id, project_id),
            )
        else:
            cur.execute(
                "SELECT 1 FROM app.value_mapping_tables WHERE id = %s AND org_id = %s "
                "AND (scope_level = 'ORG' OR project_id = %s)",
                (object_id, org_id, project_id),
            )
        return cur.fetchone() is not None


# ---------------------------------------------------------------------------
# The confirmation preview
# ---------------------------------------------------------------------------


async def _preview(request: Request) -> Response:
    """What confirming this change would record, before it is recorded.

    The caller sends the fields it is about to write; the server READS the object,
    applies them, and composes the proposed body with the same function the write
    uses. It never trusts a body the caller composed: a hash computed on the
    client would be a claim about a state the server never saw.
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return _unauthorized()
    project_id = request.path_params["project_id"]
    org_id, refused = _guard(project_id, identity, manage=True)
    if refused is not None:
        return refused

    ledger = _resolve_kind(request.path_params["object_kind"])
    if ledger is None:
        return _not_found("No version ledger for that object kind.")

    payload, invalid = await _body(request)
    if invalid is not None:
        return invalid

    from core.db import get_connection  # noqa: PLC0415
    from core.rule_versions import KIND_CLEANUP_RULE, preview_change  # noqa: PLC0415

    object_id = request.path_params["object_id"]
    try:
        with get_connection() as conn:
            if not _owns_object(conn, ledger, object_id, org_id=org_id, project_id=project_id):
                return _not_found("Rule not found.")
            if ledger.kind == KIND_CLEANUP_RULE:
                body = _proposed_rule_body(conn, object_id, project_id, payload or {})
            else:
                body = _proposed_table_body(conn, object_id, org_id, payload or {})
            preview = preview_change(
                conn,
                kind=ledger.kind,
                object_id=object_id,
                body=body,
                project_id=project_id,
            )
    except Exception as exc:  # noqa: BLE001
        logger.error("rule_versions_api: preview failed object=%s: %s", object_id, exc)
        return _server_error()
    return JSONResponse(preview)


def _proposed_table_body(conn, table_id: str, org_id: str, payload: dict) -> dict:
    """The table as it WOULD be, composed by the server from what it holds now.

    Three shapes of change reach this surface and each is applied to the stored
    state rather than replacing it: renaming or re-describing the table, editing
    what one pair becomes, and removing a pair. Anything the payload does not name
    stays exactly as the store holds it.
    """
    from core.rule_versions import value_table_body  # noqa: PLC0415
    from core.value_mapping_tables import get_table, list_entries  # noqa: PLC0415

    table = dict(get_table(conn, table_id=table_id, org_id=org_id))
    if "name" in payload and str(payload["name"] or "").strip():
        table["name"] = str(payload["name"]).strip()
    if "description" in payload:
        table["description"] = payload["description"]

    pairs: list[tuple[str, str]] = []
    entry_id = payload.get("entry_id")
    for entry in list_entries(conn, table_id=table_id):
        if entry_id and entry["id"] == entry_id:
            if payload.get("removed"):
                continue
            if "canonical_value" in payload:
                pairs.append((entry["source_value"], str(payload["canonical_value"])))
                continue
        pairs.append((entry["source_value"], entry["canonical_value"]))
    if payload.get("source_value") and payload.get("canonical_value") and not entry_id:
        pairs.append((str(payload["source_value"]), str(payload["canonical_value"])))
    return value_table_body(table, pairs)


def _proposed_rule_body(conn, rule_id: str, project_id: str, payload: dict) -> dict:
    """The rule as it WOULD be. `enabled` is ignored on purpose: it is not part of
    the versioned body, so a toggle records nothing and must not be previewed as
    if it did."""
    from core.cleanup_rules import get_rule  # noqa: PLC0415
    from core.rule_versions import cleanup_rule_body  # noqa: PLC0415

    rule = dict(get_rule(conn, rule_id=rule_id, project_id=project_id))
    for field in ("name", "source_field", "rule_kind", "pattern"):
        if field in payload and str(payload[field] or "").strip():
            rule[field] = str(payload[field]).strip()
    return cleanup_rule_body(rule)


RULE_VERSION_ROUTES: list[Route] = [
    Route(f"{_BASE}/preview", _preview, methods=["POST"]),
    Route(_BASE, _list_versions, methods=["GET"]),
]

__all__ = ["RULE_VERSION_ROUTES"]
