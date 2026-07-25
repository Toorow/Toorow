"""toorow -- Money aggregation-check REST API (Story 39.3, Epic 39).

Route (spliced into admin_api.router at startup, pattern of conflict_resolutions_api /
metric_semantics_api -- never imported by admin_api at module level; no cycle):

  POST /api/money/aggregation-check
      Body: {
          project_id: str,
          metric: str,
          contributions: [{amount, currency, source_system, source_field, pull_id,
                           datastream_id?}, ...]
      }
      -> would summing this metric across these contributions be refused?

  Auth: Bearer token via core.admin_api._check_auth (same as conflict_resolutions_api).
  AD-5: project_id scoping -> 404 (non-disclosant) on cross-project access
        (mirror conflict_resolutions_api._guard_project_access).

  200 (legal):   { "refused": false, "result_currency": <ccy|null> }
  200 (refused): { "refused": true, "refusal": {<typed refusal payload>} }
        A refusal is a VALID, HONEST answer -- NOT an HTTP error (AD-9). The caller renders it.
  401 on missing/invalid token; 404 on cross-project; 422 on malformed body.

Design decisions:
  - The engine (core.currency_refusal.check_monetary_aggregation) is PURE; this module only
    resolves the two inputs it cannot know: ``is_monetary`` (from the metric definition, via
    the 39.1 seam) and ``reporting_currency`` (from app.project_preferences.canonical_currency,
    default EUR -- a per-project pref, never a core hardcode, E39-NFR04).
  - AI-13: calls NO external API (pure engine + app-DB preference read) -> no live
    external-integration pass required.
  - AI-53: consumes metric definitions already in app.metric_definitions (27.1 seed import);
    introduces no card and no warehouse metric/dimension. "canonical inputs verified" is N/A.
  - AD-6: no FX conversion here (that is Story 39.4). This route only DECIDES legality.

French error messages throughout.
ASCII-only stdout (AI-03). No private framework attributes (AI-02).
"""

from __future__ import annotations

import json
import logging
from datetime import date

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Auth + AD-5 project guard (mirror conflict_resolutions_api)
# ---------------------------------------------------------------------------


async def _check_auth(request: Request) -> tuple[bool, str]:
    """Delegate to the shared auth layer in core.admin_api."""
    from core.admin_api import _check_auth as _admin_check_auth  # noqa: PLC0415

    return await _admin_check_auth(request)


async def _guard_project_access(project_id: str, identity: str, conn) -> bool:
    """Return True if identity has access to project_id; False -> caller returns 404."""
    if not project_id:
        return True
    try:
        from core.project_access import identity_has_project_access  # noqa: PLC0415

        return identity_has_project_access(project_id, identity, conn)
    except Exception as exc:
        logger.warning(
            "money_api: project_access_check_failed project=%s: %s", project_id, exc
        )
        return False


# ---------------------------------------------------------------------------
# Input resolution helpers
# ---------------------------------------------------------------------------


def _resolve_reporting_currency(project_id: str, conn) -> str | None:
    """Read app.project_preferences.canonical_currency for *project_id* (default EUR).

    Reuses the existing per-project preference row (migration 008); never a core hardcode
    (E39-NFR04). Returns None only if the read fails -- the engine treats a None/unknown
    reporting currency as legal for a single-currency sum (§B.2).
    """
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT canonical_currency FROM app.project_preferences WHERE project_id = %s",
                (project_id,),
            )
            row = cur.fetchone()
        if row and row[0]:
            return str(row[0]).strip().upper() or None
    except Exception as exc:
        logger.warning("money_api: reporting_currency_read_failed project=%s: %s", project_id, exc)
    return None


def _resolve_is_monetary(metric: str) -> bool:
    """Resolve whether *metric* is monetary via the 39.1 classifier seam (single source)."""
    from core.currency_refusal import _is_monetary_metric  # noqa: PLC0415

    definition = None
    try:
        from core.metric_semantics import (  # noqa: PLC0415
            SCOPE_PLATFORM,
            get_metric_definition,
        )

        definition = get_metric_definition(scope_level=SCOPE_PLATFORM, canonical_name=metric)
    except Exception as exc:
        logger.warning("money_api: metric_definition_read_failed metric=%s: %s", metric, exc)
    return _is_monetary_metric(metric, definition=definition)


# ---------------------------------------------------------------------------
# POST /api/money/aggregation-check
# ---------------------------------------------------------------------------


async def _aggregation_check(request: Request) -> Response:
    """POST /api/money/aggregation-check -- decide whether a monetary sum is refused.

    Refusal is returned as a 200 body (valid honest answer, AD-9), never a 4xx.
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Authentification requise"},
            status_code=401,
        )

    try:
        body_bytes = await request.body()
        body: dict = json.loads(body_bytes) if body_bytes.strip() else {}
    except Exception as exc:
        return JSONResponse(
            {"code": "invalid_body", "message": f"Corps JSON invalide : {exc}"},
            status_code=422,
        )

    if not isinstance(body, dict):
        return JSONResponse(
            {"code": "invalid_body", "message": "Le corps doit etre un objet JSON"},
            status_code=422,
        )

    project_id = (body.get("project_id") or "").strip()
    metric = (body.get("metric") or "").strip()
    contributions = body.get("contributions")

    if not metric:
        return JSONResponse(
            {"code": "missing_field", "message": "metric est requis"},
            status_code=422,
        )
    if not isinstance(contributions, list):
        return JSONResponse(
            {"code": "missing_field", "message": "contributions (liste) est requis"},
            status_code=422,
        )
    # Each contribution must be an object.
    for i, contrib in enumerate(contributions):
        if not isinstance(contrib, dict):
            return JSONResponse(
                {
                    "code": "invalid_body",
                    "message": f"contributions[{i}] doit etre un objet",
                },
                status_code=422,
            )

    try:
        from core.currency_refusal import check_monetary_aggregation  # noqa: PLC0415
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            ok = await _guard_project_access(project_id, identity or "", conn)
            if not ok:
                return JSONResponse(
                    {"code": "not_found", "message": "Projet introuvable"},
                    status_code=404,
                )
            reporting_currency = _resolve_reporting_currency(project_id, conn)
        # is_monetary resolution opens its own short-lived connection via metric_semantics.
        is_monetary = _resolve_is_monetary(metric)

        refusal = check_monetary_aggregation(
            metric=metric,
            contributions=contributions,
            is_monetary=is_monetary,
            reporting_currency=reporting_currency,
        )
    except Exception as exc:
        logger.error("money_api: aggregation_check_error: %s", exc)
        return JSONResponse(
            {"code": "db_error", "message": "Erreur de base de donnees"},
            status_code=500,
        )

    if refusal is not None:
        return JSONResponse({"refused": True, "refusal": refusal})

    # Legal: report the single (or reporting) currency when it is knowable.
    result_currency = _legal_result_currency(contributions, reporting_currency)
    return JSONResponse({"refused": False, "result_currency": result_currency})


def _legal_result_currency(
    contributions: list[dict], reporting_currency: str | None
) -> str | None:
    """The currency of a legal sum: the single observed currency, else the reporting ccy."""
    from core.currency_refusal import _normalize_currency  # noqa: PLC0415

    observed = {
        _normalize_currency(c.get("currency"))
        for c in contributions
        if _normalize_currency(c.get("currency")) is not None
    }
    if len(observed) == 1:
        return next(iter(observed))
    return reporting_currency


# ---------------------------------------------------------------------------
# POST /api/money/reconcile  (Story 39.6 -- cross-datastream money reconciliation
# on normalized values). Appended surgically: reuses _check_auth /
# _guard_project_access / _resolve_reporting_currency / _resolve_is_monetary
# above (single source of truth); adds no new auth/guard/currency helper.
# ---------------------------------------------------------------------------


def _contribution_json(contrib) -> dict:
    """Render a 39.6 SourceContribution as JSON (source inputs + the 39.4 fx block)."""
    return {
        "connector": contrib.connector,
        "source_amount": contrib.source_amount,
        "source_currency": contrib.source_currency,
        "on_date": contrib.on_date.isoformat() if contrib.on_date is not None else None,
        "amount": contrib.converted.amount,
        "reporting_currency": contrib.converted.reporting_currency,
        "fx": dict(contrib.converted.fx),
    }


def _route_json(route) -> dict | None:
    """Render a 27.3 RouteDecision as JSON (the definition/method applied), or None."""
    if route is None:
        return None
    reason = route.reason
    return {
        "status": route.status,
        "method": route.method,
        "scope_level": route.scope_level,
        "target_mart": route.target_mart,
        "priority_order": list(route.priority_order),
        "series": [
            {"connector": s.connector, "canonical_name": s.canonical_name}
            for s in route.series
        ],
        "reason": (
            None
            if reason is None
            else {
                "code": reason.code,
                "message": reason.message,
                "emitters": list(reason.emitters),
            }
        ),
    }


def _reconciled_money_json(result) -> dict:
    """Render a 39.6 ReconciledMoney as JSON (amount + route + per-source fx drill-down)."""
    return {
        "metric": result.metric,
        "status": result.status,
        "amount": result.amount,
        "reporting_currency": result.reporting_currency,
        "route": _route_json(result.route),
        "contributions": [_contribution_json(c) for c in result.contributions],
        "provenance": result.as_ad9_provenance(),
    }


async def _reconcile(request: Request) -> Response:
    """POST /api/money/reconcile -- reconcile a monetary metric on NORMALIZED values (39.6).

    Body: {project_id, metric, contributions:[{connector?, amount, currency, on_date?,
    source_system?, source_field?, pull_id?}, ...], on_date?, tier?}.

    A refusal / FX gap is returned as an HONEST 200 (AD-9), never a 4xx -- mirrors
    _aggregation_check. 401 on auth, 404 on cross-project (AD-5), 422 on a malformed body,
    500 on a DB error.
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Authentification requise"},
            status_code=401,
        )

    try:
        body_bytes = await request.body()
        body: dict = json.loads(body_bytes) if body_bytes.strip() else {}
    except Exception as exc:
        return JSONResponse(
            {"code": "invalid_body", "message": f"Corps JSON invalide : {exc}"},
            status_code=422,
        )

    if not isinstance(body, dict):
        return JSONResponse(
            {"code": "invalid_body", "message": "Le corps doit etre un objet JSON"},
            status_code=422,
        )

    project_id = (body.get("project_id") or "").strip()
    metric = (body.get("metric") or "").strip()
    contributions = body.get("contributions")

    if not metric:
        return JSONResponse(
            {"code": "missing_field", "message": "metric est requis"},
            status_code=422,
        )
    if not isinstance(contributions, list):
        return JSONResponse(
            {"code": "missing_field", "message": "contributions (liste) est requis"},
            status_code=422,
        )
    for i, contrib in enumerate(contributions):
        if not isinstance(contrib, dict):
            return JSONResponse(
                {
                    "code": "invalid_body",
                    "message": f"contributions[{i}] doit etre un objet",
                },
                status_code=422,
            )
        # Coerce a JSON on_date (ISO string) to a date object: fx_helper.convert needs a date,
        # not a str (a str makes identity .isoformat() / valid_from<=on_date comparisons raise a
        # non-FxError that would surface as a MISLABELED 500). Malformed -> honest 422.
        raw_on_date = contrib.get("on_date")
        if isinstance(raw_on_date, str) and raw_on_date.strip():
            try:
                contrib["on_date"] = date.fromisoformat(raw_on_date.strip())
            except ValueError:
                return JSONResponse(
                    {
                        "code": "invalid_body",
                        "message": f"contributions[{i}].on_date invalide (attendu YYYY-MM-DD)",
                    },
                    status_code=422,
                )

    tier = (body.get("tier") or "fixed").strip() or "fixed"
    # 39.6 serves the FIXED (seed) tier only; the historical/live as-of feed is wired in 39.9
    # (AI-28, Phase B). An unsupported tier is an honest 422, never a mislabeled 500.
    if tier != "fixed":
        return JSONResponse(
            {"code": "invalid_body", "message": "tier non supporte (seul 'fixed' est disponible)"},
            status_code=422,
        )

    try:
        from core.db import get_connection  # noqa: PLC0415
        from core.money_reconciliation import (  # noqa: PLC0415
            KEEP_SEPARATE,
            REFUSED,
            reconcile_monetary_metric,
        )

        with get_connection() as conn:
            ok = await _guard_project_access(project_id, identity or "", conn)
            if not ok:
                return JSONResponse(
                    {"code": "not_found", "message": "Projet introuvable"},
                    status_code=404,
                )
            reporting_currency = _resolve_reporting_currency(project_id, conn)
        # is_monetary resolution opens its own short-lived connection via metric_semantics.
        is_monetary = _resolve_is_monetary(metric)

        result = reconcile_monetary_metric(
            project_id,
            metric,
            contributions,
            reporting_currency=reporting_currency,
            tier=tier,
            is_monetary=is_monetary,
        )
    except Exception as exc:
        logger.error("money_api: reconcile_error: %s", exc)
        return JSONResponse(
            {"code": "db_error", "message": "Erreur de base de donnees"},
            status_code=500,
        )

    # A refusal / FX gap is an honest 200 (AD-9), not a 4xx (mirror _aggregation_check).
    if result.status == REFUSED:
        return JSONResponse({"reconciled": False, "refusal": result.refusal})
    if result.status == KEEP_SEPARATE:
        return JSONResponse(
            {
                "reconciled": False,
                "keep_separate": [_contribution_json(c) for c in result.contributions],
                "result": _reconciled_money_json(result),
            }
        )
    return JSONResponse({"reconciled": True, "result": _reconciled_money_json(result)})


# ---------------------------------------------------------------------------
# MONEY_ROUTES -- spliced into admin_api at startup
# ---------------------------------------------------------------------------

MONEY_ROUTES: list[Route] = [
    Route("/api/money/aggregation-check", _aggregation_check, methods=["POST"]),
    Route("/api/money/reconcile", _reconcile, methods=["POST"]),
]
