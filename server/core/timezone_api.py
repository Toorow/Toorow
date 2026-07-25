"""toorow -- cross-source day-offset SIGNAL REST API (Story 39.8, Epic 39).

Route (spliced into admin_api.router at startup, pattern of money_api / conflict_resolutions_api
-- never imported by admin_api at module level; no cycle):

  POST /api/timezone/day-offset-check
      Body: { project_id, metric?, datastreams: [ <datastream_id>, ... ] }
      -> do these datastreams disagree on report timezone at DATE grain, and is there a
         source-side adjustment lever?

  This is the NON-field-scoped cross-source compare path: a card / agent / control-tower
  comparing e.g. revenue-by-day across two connectors does NOT go through get_target_field, so
  it asks here. The route resolves each datastream's captured report_timezone (Story 39.7's
  single-source-of-truth accessor) + its has_lever / lever_hint (from the connector's OWN
  source-capabilities descriptor, AD-2), builds the per-stream list, and calls the PURE engine.

  Auth: Bearer token via core.admin_api._check_auth (same as money_api).
  AD-5: project_id scoping -> 404 (non-disclosant) on cross-project access
        (mirror money_api._guard_project_access).

  200 (no signal):  { "signalled": false }                       # same-tz / <2 known tz (AC3)
  200 (advisory):   { "signalled": true, "signal": {<TIMEZONE_DAY_OFFSET payload>} }  # AC1/AC2
        A signal is a VALID, HONEST answer -- NOT an HTTP error (AD-9). A 4xx would say "you did
        something wrong"; the signal says "these daily figures may be offset -- here's why, and
        whether you can fix it". The caller renders it.
  401 on missing/invalid token; 404 on cross-project; 422 on malformed body.

Design decisions:
  - The engine (core.timezone_signal.check_cross_source_day_offset) is PURE and NEVER realigns
    (realignable:false constant); this module only resolves the two inputs it cannot know:
    the captured report_timezone (39.7 accessor) and the lever posture (descriptor).
  - AI-13: calls NO external API (pure engine + captured-provenance read + descriptor read) ->
    no live external-integration pass required.
  - AI-53: consumes captured report_timezone provenance + source-capabilities descriptors
    already present; introduces no card and no warehouse metric/dimension. The "canonical
    inputs verified" line is N/A (no data-consuming card surface added).
  - AD-2 / E39-NFR05: ZERO provider names / provider field names here. Timezones come from the
    39.7 accessor (captured provenance); the lever comes from the connector's OWN descriptor
    declaration (opaque locus), never a provider filter name encoded in core.

French error messages throughout.
ASCII-only stdout (AI-03). No private framework attributes (AI-02).
"""

from __future__ import annotations

import json
import logging

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Auth + AD-5 project guard (mirror money_api)
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
            "timezone_api: project_access_check_failed project=%s: %s", project_id, exc
        )
        return False


# ---------------------------------------------------------------------------
# Per-datastream resolution: captured report timezone (39.7 accessor) + lever posture
# (source-capabilities descriptor). Source-agnostic (AD-2).
# ---------------------------------------------------------------------------


def _resolve_report_timezone(datastream_id: str) -> str | None:
    """Resolve a datastream's captured report timezone via Story 39.7's accessor (fail-soft).

    Single source of truth: ``report_timezone.report_timezone_for_datastream`` (manifest/
    declaration read). None on any unresolved/undetermined datastream (the engine EXCLUDES such
    a stream -- defer to 39.7's GAP, never a fabricated UTC).
    """
    try:
        from core.report_timezone import report_timezone_for_datastream  # noqa: PLC0415

        return report_timezone_for_datastream(str(datastream_id))
    except Exception as exc:  # noqa: BLE001 -- fail-soft, never crash the read path.
        logger.warning("timezone_api: report_timezone_read_failed ds=%s: %s", datastream_id, exc)
        return None


def _resolve_lever(datastream_id: str) -> tuple[bool, str | None]:
    """Resolve a datastream's adjustment-lever posture from its source-capabilities descriptor.

    Reads the ADDITIVE ``report_timezone_lever`` declaration (Story 39.8, §B.3) off the module's
    manifest: present with a ``locus`` => (True, locus); absent/malformed => (False, None) --
    the common case, GAM included ("fixed on the detected timezone, no lever available"). AD-2:
    core reads only the PRESENCE + opaque ``locus``; it never encodes a provider's filter name.
    Fail-soft: any lookup error -> (False, None).
    """
    try:
        from core.report_timezone import (  # noqa: PLC0415 -- reuse the same DS->manifest seam.
            _module_name_for_datastream,
        )

        module_name = _module_name_for_datastream(str(datastream_id))
        if not module_name:
            return (False, None)
        from core.main import _loaded_modules  # noqa: PLC0415

        manifest = next(
            (m.manifest for m in _loaded_modules if getattr(m, "name", None) == module_name),
            None,
        )
        if not isinstance(manifest, dict):
            return (False, None)
        caps = manifest.get("source_capabilities")
        if not isinstance(caps, dict):
            return (False, None)
        lever = caps.get("report_timezone_lever")
        if not isinstance(lever, dict):
            return (False, None)
        locus = lever.get("locus")
        if isinstance(locus, str) and locus.strip():
            return (True, locus.strip())
        return (False, None)
    except Exception as exc:  # noqa: BLE001 -- fail-soft, degraded (no lever) not crashed.
        logger.warning("timezone_api: lever_read_failed ds=%s: %s", datastream_id, exc)
        return (False, None)


# ---------------------------------------------------------------------------
# POST /api/timezone/day-offset-check
# ---------------------------------------------------------------------------


async def _day_offset_check(request: Request) -> Response:
    """POST /api/timezone/day-offset-check -- signal a cross-source day-offset (advisory).

    The signal is returned as a 200 body (valid honest answer, AD-9), never a 4xx. NEVER
    realigns (the engine's ``realignable`` is a constant False).
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
    metric = body.get("metric")
    metric = metric.strip() if isinstance(metric, str) and metric.strip() else None
    datastreams = body.get("datastreams")

    if not isinstance(datastreams, list):
        return JSONResponse(
            {"code": "missing_field", "message": "datastreams (liste) est requis"},
            status_code=422,
        )
    for i, ds in enumerate(datastreams):
        if not isinstance(ds, (str, int)):
            return JSONResponse(
                {
                    "code": "invalid_body",
                    "message": f"datastreams[{i}] doit etre un identifiant",
                },
                status_code=422,
            )

    try:
        from core.db import get_connection  # noqa: PLC0415
        from core.timezone_signal import check_cross_source_day_offset  # noqa: PLC0415

        with get_connection() as conn:
            ok = await _guard_project_access(project_id, identity or "", conn)
            if not ok:
                return JSONResponse(
                    {"code": "not_found", "message": "Projet introuvable"},
                    status_code=404,
                )
        # Build the per-stream provenance list: captured report timezone (39.7 accessor) + the
        # lever posture (descriptor). Both are source-agnostic reads (AD-2). Undetermined-tz
        # streams flow through with report_timezone=None and are EXCLUDED by the engine.
        streams: list[dict] = []
        for ds in datastreams:
            has_lever, lever_hint = _resolve_lever(str(ds))
            streams.append(
                {
                    "datastream": str(ds),
                    "report_timezone": _resolve_report_timezone(str(ds)),
                    "has_lever": has_lever,
                    "lever_hint": lever_hint,
                }
            )

        signal = check_cross_source_day_offset(metric=metric, streams=streams)
    except Exception as exc:
        logger.error("timezone_api: day_offset_check_error: %s", exc)
        return JSONResponse(
            {"code": "db_error", "message": "Erreur de base de donnees"},
            status_code=500,
        )

    # A signal is an honest 200 (AD-9), never a 4xx. No signal => signalled:false (AC3).
    if signal is not None:
        return JSONResponse({"signalled": True, "signal": signal})
    return JSONResponse({"signalled": False})


# ---------------------------------------------------------------------------
# TIMEZONE_ROUTES -- spliced into admin_api at startup
# ---------------------------------------------------------------------------

TIMEZONE_ROUTES: list[Route] = [
    Route("/api/timezone/day-offset-check", _day_offset_check, methods=["POST"]),
]
