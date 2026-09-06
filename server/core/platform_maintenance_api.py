"""Les gestes d exploitation : le cache, les schemas, le miroir, la sante.

AD-43, 2026-08-13. Cinq routes que personne n ouvre dans le parcours normal et
qui n appartiennent a aucun objet du produit. Elles ne sont PAS `/internal` :
chacune exige une identite humaine, et deux d entre elles reclament le role
plateforme. Voisines par le geste, pas par le sujet -- ce qui est exactement la
raison de ne pas les laisser dans un fourre-tout de 6 425 lignes.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from core.audit import (
    write_audit_row,
)

logger = logging.getLogger("core.admin_api")

# --- le joint qui reste dans admin_api -----------------------------------
async def _check_auth(*args, **kwargs):
    """Le joint d'`admin_api`, atteint a l'APPEL -- jamais capture a l'import."""
    from core.admin_api import _check_auth as _impl  # noqa: PLC0415
    return await _impl(*args, **kwargs)

def _refuse_unless_project_allowed(*args, **kwargs):
    """Le joint d'`admin_api`, atteint a l'APPEL -- jamais capture a l'import."""
    from core.admin_api import _refuse_unless_project_allowed as _impl  # noqa: PLC0415
    return _impl(*args, **kwargs)

# --- les handlers, deplaces TELS QUELS -----------------------------------

ACTION_CACHE_REBUILD = "cache.rebuild"

async def _cache_status(request: Request) -> Response:
    """GET /api/admin/cache/status -- etat du cache read-through (Story 19.3).

    Retourne :
      {
        "cache_state":   "disabled" | "no-cache" | "stale" | "fresh",
        "cache_enabled": bool,
        "cache_built_at": str | null,       -- UTC ISO-8601 (depuis le manifeste)
        "age_seconds":   float | null,      -- maintenant - cache_built_at
        "min_date":      str | null,        -- borne basse de la fenetre
        "max_date":      str | null,        -- borne haute de la fenetre
        "tables":        [str, ...],        -- tables cachees (manifeste)
        "row_counts":    {table: int, ...}, -- counts par table (manifeste)
        "project_ids":   [str, ...],        -- projets couverts (manifeste)
        "hit_rate":      float | null,      -- hits / (hits + misses) sur la session
        "stats":         {decision: int},   -- compteurs bruts (AD-13)
        "last_rebuild_cause": null          -- nightly | manual (enrichi en phase B)
      }

    Honnete : "no-cache" quand le fichier est absent/ephemere perdu, "stale" quand
    le cache est perime selon la meme regle que 19.2 (_cache_is_fresh), "fresh" sinon.
    "disabled" quand TOOROW_CACHE_ENABLED=false (independant de l'existence du fichier).
    """
    authorized, _identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Token Bearer requis."},
            status_code=401,
        )

    # Lecture du flag enabled.
    cache_enabled = os.environ.get("TOOROW_CACHE_ENABLED", "false").lower() == "true"

    # Lecture du manifeste (None si absent/corrompu -- invariant f).
    from core.cache_warehouse import read_manifest  # noqa: PLC0415

    manifest = read_manifest()

    # Compteurs hit/miss (AD-13 / NFR7) -- ne leve jamais.
    try:
        from core.warehouse import get_cache_stats  # noqa: PLC0415

        stats = get_cache_stats()
    except Exception:  # noqa: BLE001
        stats = {}

    hits = stats.get("hit", 0)
    total_decisions = sum(stats.values())
    # Le hit rate agrege = hits / toutes les decisions (hit + miss + bypass + ...).
    hit_rate: float | None = (hits / total_decisions) if total_decisions > 0 else None

    now_utc = datetime.now(tz=timezone.utc)

    if not cache_enabled:
        return JSONResponse(
            {
                "cache_state": "disabled",
                "cache_enabled": False,
                "cache_built_at": None,
                "age_seconds": None,
                "min_date": None,
                "max_date": None,
                "tables": [],
                "row_counts": {},
                "project_ids": [],
                # review-19-3 F-3: cache off -> pas de hit rate (les stats ne comptent
                # que des decisions "disabled", un ratio serait un mensonge semantique).
                "hit_rate": None,
                "stats": stats,
                "last_rebuild_cause": None,
            }
        )

    if manifest is None:
        return JSONResponse(
            {
                "cache_state": "no-cache",
                "cache_enabled": True,
                "cache_built_at": None,
                "age_seconds": None,
                "min_date": None,
                "max_date": None,
                "tables": [],
                "row_counts": {},
                "project_ids": [],
                "hit_rate": hit_rate,
                "stats": stats,
                "last_rebuild_cause": None,
            }
        )

    cache_built_at = manifest.get("cache_built_at")
    age_seconds: float | None = None
    if cache_built_at:
        try:
            built_dt = datetime.fromisoformat(cache_built_at)
            if built_dt.tzinfo is None:
                built_dt = built_dt.replace(tzinfo=timezone.utc)
            age_seconds = (now_utc - built_dt).total_seconds()
        except Exception:  # noqa: BLE001
            pass

    # Fraicheur : reuse la MEME regle que 19.2 (_cache_is_fresh) -- pas de duplication.
    try:
        from core.warehouse import _cache_is_fresh  # noqa: PLC0415

        is_fresh = _cache_is_fresh(cache_built_at, now=now_utc)
    except Exception:  # noqa: BLE001
        is_fresh = False

    cache_state = "fresh" if is_fresh else "stale"

    return JSONResponse(
        {
            "cache_state": cache_state,
            "cache_enabled": True,
            "cache_built_at": cache_built_at,
            "age_seconds": age_seconds,
            "min_date": manifest.get("min_date"),
            "max_date": manifest.get("max_date"),
            "tables": manifest.get("tables") or [],
            "row_counts": manifest.get("row_counts") or {},
            "project_ids": manifest.get("project_ids") or [],
            "hit_rate": hit_rate,
            "stats": stats,
            "last_rebuild_cause": None,
        }
    )

async def _cache_rebuild(request: Request) -> Response:
    """POST /api/admin/cache/rebuild -- trigger manuel de rebuild (Story 19.3).

    Declenche cache_warehouse.rebuild_cache() de facon bornee et synchrone.
    Audite avec performed_by = identite REELLE du Bearer token (AD-14).
    AD-5 : verifie que l'appelant est authentifie (meme guard que tout endpoint admin).
    Invariant (f) : JAMAIS de 500 brut -- un echec retourne {"status": "failed"}.

    Response (200):
      {"status": "ok" | "disabled" | "failed" | "skipped",
       "tables": [...], "row_counts": {...}, "project_ids": [...],
       "min_date": str, "max_date": str, "cache_built_at": str,
       "performed_by": str}

    Response (401): non authentifie.
    Response (403): TOOROW_CACHE_ENABLED=false (inutile de reconstruire un cache
                    desactive, et declencher un rebuild serait trompeur).
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Token Bearer requis."},
            status_code=401,
        )

    # review-19-3 F-2: une identite chaine-vide (auth disabled) ne doit pas
    # contourner la valeur sentinelle "anonymous" dans l'audit AD-14.
    performed_by = (identity or "").strip() or "anonymous"

    # AD-5 / Guard : si le cache est desactive, le rebuild est refuse avec 403
    # (cela eviterait une confusion : le service retourne "disabled" sans construire
    # quoi que ce soit -- le caller doit activer TOOROW_CACHE_ENABLED d'abord).
    cache_enabled = os.environ.get("TOOROW_CACHE_ENABLED", "false").lower() == "true"
    if not cache_enabled:
        write_audit_row(
            identity=performed_by,
            action=ACTION_CACHE_REBUILD,
            provider_account="",
            connection_ref="",
            metadata={
                "trigger": "manual",
                "result": "refused_disabled",
                "performed_by": performed_by,
            },
        )
        return JSONResponse(
            {
                "code": "cache_disabled",
                "message": (
                    "The cache is disabled (TOOROW_CACHE_ENABLED=false). "
                    "Enable it before triggering a rebuild."
                ),
            },
            status_code=403,
        )

    # Audit AVANT le rebuild (AD-14 On-Behalf-Of : on trace la demande, pas seulement
    # le resultat -- coherent avec la revocation Google et les autres actions on-demand).
    write_audit_row(
        identity=performed_by,
        action=ACTION_CACHE_REBUILD,
        provider_account="",
        connection_ref="",
        metadata={
            "trigger": "manual",
            "performed_by": performed_by,
        },
    )

    # Rebuild on-demand -- JAMAIS de raise (invariant f : rebuild_cache() l'absorbe).
    try:
        from core import cache_warehouse  # noqa: PLC0415

        result = cache_warehouse.rebuild_cache()
    except Exception as exc:  # noqa: BLE001 -- filet de securite supplementaire
        logger.warning("admin_api: cache_rebuild_unexpected: %s: %s", type(exc).__name__, exc)
        result = {"status": "failed", "reason": f"{type(exc).__name__}: {exc}"}

    logger.info(
        "admin_api: cache_rebuild_manual status=%s performed_by=%s",
        result.get("status"),
        performed_by,
    )

    # Audit du resultat (AD-14 : on trace aussi le resultat pour observabilite).
    write_audit_row(
        identity=performed_by,
        action=ACTION_CACHE_REBUILD,
        provider_account="",
        connection_ref="",
        metadata={
            "trigger": "manual_result",
            "status": result.get("status"),
            "tables": result.get("tables", []),
            "performed_by": performed_by,
        },
    )

    return JSONResponse({**result, "performed_by": performed_by})

async def _backfill_warehouse_schemas(request: Request) -> Response:
    """POST /api/admin/warehouse/provision-schemas -- backfill all orgs (AC3).

    Auth: any authenticated user (_check_auth).  Idempotent.
    Body (optional): {"include_archived": true} -- default false.
    Returns: {"provisioned": N, "skipped": M, "errors": [...]}
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Bearer token required"},
            status_code=401,
        )

    try:
        raw_body = await request.body()
        body: dict = json.loads(raw_body) if raw_body else {}
    except Exception:
        body = {}

    include_archived = bool(body.get("include_archived", False))

    try:
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            with conn.cursor() as cur:
                if include_archived:
                    cur.execute(
                        "SELECT id FROM app.organizations "
                        "WHERE status IN ('active', 'archived') ORDER BY created_at ASC"
                    )
                else:
                    cur.execute(
                        "SELECT id FROM app.organizations "
                        "WHERE status = 'active' ORDER BY created_at ASC"
                    )
                org_ids = [row[0] for row in cur.fetchall()]
    except Exception:
        logger.exception("admin_api: backfill_warehouse_schemas db_error")
        return JSONResponse(
            {"code": "db_error", "message": "database operation failed"},
            status_code=500,
        )

    from core import warehouse_tenancy as _wt  # noqa: PLC0415

    provisioned = 0
    skipped = 0
    errors: list[dict] = []

    for oid in org_ids:
        try:
            result = _wt.provision_org_schemas(org_id=oid, conn=None)
            if result.get("status") == "ok":
                provisioned += 1
            else:
                skipped += 1
                logger.info(
                    "admin_api: backfill_warehouse_schemas skip org=%s reason=%s",
                    oid,
                    result.get("reason"),
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("admin_api: backfill_warehouse_schemas error org=%s error=%s", oid, exc)
            errors.append({"org_id": oid, "reason": "provision_failed"})

    logger.info(
        "admin_api: backfill_warehouse_schemas done provisioned=%d skipped=%d errors=%d",
        provisioned,
        skipped,
        len(errors),
    )
    return JSONResponse(
        {"provisioned": provisioned, "skipped": skipped, "errors": errors},
        status_code=200,
    )

async def _trigger_mirror_sync(request: Request) -> Response:
    """POST /api/mirror/sync -- trigger a mirror sync manually (Story 4.4, AC8).

    Useful for dev and for Story 5.3 forced syncs. Auth-guarded (same pattern as
    all other admin API endpoints). Returns the sync result dict from mirror_sync.py.

    Response (200):
        {"synced": {...}, "lag_seconds": float, "synced_at": str}

    Response (500):
        {"code": "sync_error", "message": str}  on sync failure.
    """
    authorized, _identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Bearer token required"},
            status_code=401,
        )

    try:
        from core import mirror_sync  # noqa: PLC0415

        result = mirror_sync.sync_tables()
    except Exception as exc:
        logger.error("admin_api: mirror_sync_error: %s", exc)
        return JSONResponse(
            {"code": "sync_error", "message": f"Mirror sync failed: {exc}"},
            status_code=500,
        )

    return JSONResponse(result)

async def _health_proxy(request: Request) -> Response:
    """GET /api/health -- REST proxy for the health MCP tool (Story 5.2, AC4).

    Returns the health tool's dict response as JSON.
    Useful for the admin Pipeline panel which needs circuit breaker states and
    mirror sync lag without going through the MCP protocol.

    Response (200):
        {"data": {"status": "ok", "quota": [...], "mirror_sync": {...}|null, ...}, ...}

    Response (400): missing project_id
    Response (401): unauthorized
    Response (404): project unknown OR not visible to this identity
    Response (500): internal error calling health tool

    AI-171 -- CE HANDLER PORTAIT LE MOTIF QUE LA STORY 7.1 AC5 INTERDIT, mot pour
    mot : `request.query_params.get("project_id", "default")`. Un appelant qui
    omettait le parametre n'etait pas refuse, il etait RATTACHE en silence au
    projet nomme `default` ; un appelant qui en nommait un autre recevait l'etat
    de sante -- quotas, retard de mirror_sync, disjoncteurs -- d'un tenant qui
    n'est pas le sien. `get_connection()` n'installe aucun contexte d'identite,
    donc aucune RLS ne bornait cette lecture par ailleurs.
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Bearer token required"},
            status_code=401,
        )

    project_id = (request.query_params.get("project_id") or "").strip()
    if not project_id:
        return JSONResponse(
            {"code": "missing_param", "message": "project_id is required"},
            status_code=400,
        )
    denied = _refuse_unless_project_allowed(identity, project_id, "view", "health")
    if denied is not None:
        return denied

    try:
        from core.main import health  # noqa: PLC0415

        result = health(project_id=project_id)
    except Exception as exc:
        logger.error("admin_api: health_proxy_error: %s", exc)
        return JSONResponse(
            {"code": "health_error", "message": f"Health tool error: {exc}"},
            status_code=500,
        )

    return JSONResponse(result)


# --- LES ROUTES, dans l'ordre exact ou elles etaient declarees ------------
#
# Starlette resout dans l'ordre de declaration. Chaque collection est
# epissee par `admin_api` a la position que ses routes occupaient : memes
# chemins, memes methodes, meme ordre. La preuve est un dump de
# `admin_api.router.routes` avant/apres, pas une lecture de diff.

PLATFORM_MAINTENANCE_ROUTES_1 = [
    Route(
        "/api/admin/warehouse/provision-schemas",
        endpoint=_backfill_warehouse_schemas,
        methods=["POST"],
    ),
]

PLATFORM_MAINTENANCE_ROUTES_2 = [
    # Story 4.4 (AC8): manual mirror sync trigger
    Route("/api/mirror/sync", endpoint=_trigger_mirror_sync, methods=["POST"]),
    # Story 5.2 (AC4): REST proxy for health MCP tool (Pipeline panel)
    Route("/api/health", endpoint=_health_proxy, methods=["GET"]),
]

PLATFORM_MAINTENANCE_ROUTES_3 = [
    # Story 19.3: cache DuckDB observability + rebuild trigger.
    # /status precede /rebuild pour clarte (pas de conflit de routes ici).
    Route("/api/admin/cache/status", endpoint=_cache_status, methods=["GET"]),
    Route("/api/admin/cache/rebuild", endpoint=_cache_rebuild, methods=["POST"]),
]
