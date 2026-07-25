"""toorow -- Data Quality REST route handlers (Stories 8.6 + 13.3, Epics 8 + 13).

Provides DQ_ROUTES: list[Route] -- a flat list the orchestrator splices into
admin_api.router at startup. Never imported by admin_api.py at module level.

Routes:
  GET  /api/dq/summary?project_id=           -> 5 monitor KPI summary
  GET  /api/dq/issues?project_id=&monitor=&status=  -> dq_* firings table
  POST /api/dq/issues/{firing_id}/acknowledge        -> ack a firing
  GET  /api/dq/history?project_id=&monitor=&days=   -> success rate per monitor (Story 13.3)
  GET  /api/dq/datastream-freshness?project_id=      -> last run per datastream (Story 13.3)
  POST /api/dq/evaluate?project_id=                  -> manual trigger (rate-limited, Story 13.3)

Auth: same _check_auth from core.admin_api (Bearer token via core.api_auth).
AD-5: project_id scoping on every query AND identity access (identity_has_project_access).
AD-8: admin console communicates through this REST layer only.
French error messages (Epic 8 Part B).

ASCII-only stdout (AI-03). No private framework attributes (AI-02).

Story 13.3 additions:
  - 5th monitor dq_date_format (lignes rejetees a l'extraction).
  - GET /api/dq/history : success_rate = jours sans firing / jours evalues (30 j glissants).
    Un "jour evalue" = window_date ayant au moins 1 firing OU 1 pull_job reussi ce jour-la.
    Si aucun firing ET aucun pull_job reussi sur la fenetre : evaluated_days=0, success_rate=null
    (distingue "jamais evalue" de "100 % succes").
  - GET /api/dq/datastream-freshness : last_pull_at, last_success_at, last_status, row_count
    depuis app.pull_jobs JOIN app.pull_verifications (colonnes reelles : state, completed_at,
    actual_rows -- PAS status/row_count qui n'existent pas).
  - POST /api/dq/evaluate : rate-limite 2 appels / 60 s par (project_id), audite via
    core.audit.write_audit_row avec action 'dq.evaluate.triggered'.
    Appelle run_dq_monitors(project_id) en thread (non bloquant).

Schema pull_jobs (colonnes reelles -- reference extract_ledger.py l.168-201) :
  state      : queued | running | done | failed | dead_letter
  completed_at : timestamp | NULL
  enqueued_at  : timestamp
  pull_id    : uuid (FK vers app.pull_verifications.pull_id)

app.pull_verifications (jointure par pull_id) :
  verdict    : ok | partial | failed | NULL
  actual_rows: int | NULL
"""

from __future__ import annotations

import concurrent.futures
import json
import logging
import time
from datetime import datetime, timedelta, timezone

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

logger = logging.getLogger(__name__)

# DQ alert type prefixes -- Story 13.3: dq_date_format est le 5e moniteur.
_DQ_TYPES = (
    "dq_volume",
    "dq_timeliness",
    "dq_duplication",
    "dq_schema",
    "dq_date_format",
)

# French display labels for each monitor.
# dq_date_format verifie les rejected_rows signalees par l'extracteur (lignes mal formees
# ou hors plage de dates toleree). Label honnete : "Lignes rejetees".
_MONITOR_LABELS: dict[str, str] = {
    "dq_volume": "Volume",
    "dq_timeliness": "Ponctualite",
    "dq_duplication": "Doublons",
    "dq_schema": "Coherence de schema",
    "dq_date_format": "Lignes rejetees",
}

# ---------------------------------------------------------------------------
# Rate-limit state for POST /api/dq/evaluate (Story 13.3)
# 2 appels maximum par project_id par fenetre de 60 secondes.
# TODO(Phase-B): migrer vers Redis pour la securite multi-replica.
# ---------------------------------------------------------------------------

_EVALUATE_RATE_LIMIT = 2         # max appels par fenetre
_EVALUATE_RATE_WINDOW = 60.0     # secondes
_evaluate_calls: dict[str, list[float]] = {}  # project_id -> [monotonic timestamps]


def _evaluate_rate_limited(project_id: str, *, clock=time.monotonic) -> bool:
    """Renvoie True si project_id a depasse la limite (2 appels / 60 s). Enregistre l'appel.

    Fenetre glissante de 60 secondes. Thread-safe en single-process (GIL Python).
    Purge les entrees vides pour eviter la fuite memoire sur les project_id inactifs (M3).
    """
    now = clock()
    cutoff = now - _EVALUATE_RATE_WINDOW
    calls = [t for t in _evaluate_calls.get(project_id, []) if t >= cutoff]
    if len(calls) >= _EVALUATE_RATE_LIMIT:
        _evaluate_calls[project_id] = calls
        return True
    calls.append(now)
    if calls:
        _evaluate_calls[project_id] = calls
    else:
        # Purge: la liste est vide apres filtrage -> supprimer l'entree (M3).
        _evaluate_calls.pop(project_id, None)
    return False


# ---------------------------------------------------------------------------
# Auth helper
# ---------------------------------------------------------------------------


async def _check_auth(request: Request) -> tuple[bool, str]:
    """Delegue a la couche d'auth partagee dans core.admin_api."""
    from core.admin_api import _check_auth as _admin_check_auth  # noqa: PLC0415

    return await _admin_check_auth(request)


def _enforce_project_scope(
    project_id: str, identity: str, conn, *, fail_closed: bool = False
) -> bool:
    """Verifie qu'identity a acces a project_id (AD-5).

    Utilise identity_has_project_access (default-open quand app.project_members est vide,
    closed si le projet a des membres). Renvoie False -> appelant doit retourner 404.
    Audit best-effort via logger (pas d'import audit pour ne pas alourdir le path chaud).

    fail_closed=True (obligatoire pour les endpoints WRITE) :
        Si identity_has_project_access leve une exception (DB injoignable, curseur en
        erreur...), le comportement par defaut est fail-open (l'exception remonte au
        try/except externe qui renvoie 500, mais l'appel peut quand meme s'executer si
        le 500 est mal gere). Avec fail_closed=True, toute exception est capturee et
        renvoie False (acces refuse) au lieu de propager -- garantit qu'un check ACL
        defaillant ne laisse jamais passer un WRITE. Le caller voit un 404 non-disclosant,
        pas un 500 qui pourrait etre reutilise comme oracle.

    fail_closed=False (defaut, READ) :
        L'exception remonte normalement ; le caller externe catch et renvoie 500. Acceptable
        pour les lectures car une DB down empeche de toute facon de lire des donnees.
    """
    from core.project_access import identity_has_project_access  # noqa: PLC0415

    if fail_closed:
        try:
            granted = identity_has_project_access(project_id, identity, conn)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "dq_api: acl_check_failed_closed project=%s identity=%s (AD-5 fail-closed): %s",
                project_id,
                identity,
                exc,
            )
            return False
    else:
        granted = identity_has_project_access(project_id, identity, conn)

    if not granted:
        logger.warning(
            "dq_api: access_denied project=%s identity=%s (AD-5)",
            project_id,
            identity,
        )
    return granted


# ---------------------------------------------------------------------------
# GET /api/dq/summary
# ---------------------------------------------------------------------------


async def _dq_summary(request: Request) -> Response:
    """GET /api/dq/summary -- 5-monitor health KPIs for the Quality page (Story 13.3).

    Query params:
        project_id  (required) -- scope to one project (AD-5)

    Response (200):
        {
          "monitors": [
            {
              "type": "dq_volume",
              "label": "Volume",
              "healthy_pct": 87.5,          -- % of evaluated streams passing (last 24h)
              "unresolved_count": 3,         -- open (unacknowledged) firings last 24h
              "new_since_yesterday": 1,      -- firings fired in the last 24h vs prior 24h
            },
            ...
          ],
          "total_issues_24h": int,
          "total_unresolved": int,
        }
    """
    authorized, _identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Authentification requise"},
            status_code=401,
        )

    project_id = (request.query_params.get("project_id") or "").strip() or None
    if not project_id:
        return JSONResponse(
            {"code": "invalid_param", "message": "project_id est requis"},
            status_code=422,
        )

    try:
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            # Count enabled datastreams for the project (denominator for %).
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT COUNT(*) FROM app.datastreams WHERE project_id = %s AND enabled = TRUE",
                    (project_id,),
                )
                row = cur.fetchone()
                total_streams = int(row[0]) if row else 0

            # Fetch last 48h of dq_* firings for the project.
            cutoff_48h = datetime.now(tz=timezone.utc) - timedelta(hours=48)
            cutoff_24h = datetime.now(tz=timezone.utc) - timedelta(hours=24)
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT type, fired_at, acknowledged_at
                    FROM app.alert_firings
                    WHERE project_id = %s
                      AND type LIKE 'dq_%%'
                      AND fired_at >= %s
                    ORDER BY fired_at DESC
                    """,
                    (project_id, cutoff_48h),
                )
                cols = [d[0] for d in cur.description]
                firings = [dict(zip(cols, r)) for r in cur.fetchall()]

        monitor_data = []
        total_issues_24h = 0
        total_unresolved = 0

        for dq_type in _DQ_TYPES:
            type_firings = [f for f in firings if f["type"] == dq_type]
            last_24h = [f for f in type_firings if f["fired_at"] >= cutoff_24h]
            prev_24h = [f for f in type_firings if f["fired_at"] < cutoff_24h]

            unresolved = [
                f for f in last_24h
                if f.get("acknowledged_at") is None
            ]
            unresolved_count = len(unresolved)
            total_unresolved += unresolved_count
            total_issues_24h += len(last_24h)

            # new_since_yesterday: firings in last 24h not seen in prior 24h
            # (simple count delta -- proxy for new issues).
            new_since_yesterday = max(0, len(last_24h) - len(prev_24h))

            # healthy_pct: (streams with NO firing in last 24h) / total streams * 100
            affected_streams: set[str] = set()
            for f in last_24h:
                if f.get("acknowledged_at") is None:
                    # Extract datastream_id from message metadata if available.
                    # For simplicity: count each unique firing as one affected stream.
                    affected_streams.add(f.get("id", ""))

            if total_streams > 0:
                healthy = max(0, total_streams - unresolved_count)
                healthy_pct = round(healthy / total_streams * 100, 1)
            else:
                healthy_pct = 100.0

            monitor_data.append(
                {
                    "type": dq_type,
                    "label": _MONITOR_LABELS[dq_type],
                    "healthy_pct": healthy_pct,
                    "unresolved_count": unresolved_count,
                    "new_since_yesterday": new_since_yesterday,
                }
            )

        return JSONResponse(
            {
                "monitors": monitor_data,
                "total_issues_24h": total_issues_24h,
                "total_unresolved": total_unresolved,
            }
        )

    except Exception as exc:
        logger.warning("dq_api: summary_error project=%s: %s", project_id, exc)
        return JSONResponse(
            {"code": "db_error", "message": "Erreur lors de la recuperation du resume"},
            status_code=500,
        )


# ---------------------------------------------------------------------------
# Shared helper: parse firing metadata from message suffix
# ---------------------------------------------------------------------------


def _parse_firing_meta(msg: str) -> tuple[str, str, str]:
    """Extract (datastream_id, datastream_name, module_name) from a firing message.

    Firing messages written by ``infra_alerts.write_infra_firing`` append JSON
    via ``json.dumps(metadata)``, which produces a space after the colon
    (e.g. ``"module_name": "gads"``).  This helper matches that canonical format
    and also tolerates the compact form (no space) for robustness.

    Returns a 3-tuple of strings; each field defaults to "" if absent or unparseable.
    """
    ds_id, ds_name, module_name = "", "", ""
    if "| meta=" not in msg:
        return ds_id, ds_name, module_name
    try:
        meta = json.loads(msg.split("| meta=", 1)[1])
        ds_id = meta.get("datastream_id", "")
        ds_name = meta.get("datastream_name", "")
        module_name = meta.get("module_name", "")
    except Exception:  # noqa: BLE001
        pass
    return ds_id, ds_name, module_name


def _module_like_pattern(module: str) -> str:
    """Return a LIKE pattern matching the canonical json.dumps format for module_name.

    ``json.dumps`` produces ``"module_name": "gads"`` (space after colon).
    We also accept the compact form ``"module_name":"gads"`` in case the message
    was written via a different serialiser.

    Usage: use TWO LIKE conditions joined by OR, or call this function twice.
    Prefer calling ``_module_filter_clauses`` which appends both predicates.
    """
    return f'%"module_name": "{module}"%'


def _module_filter_clauses(module: str) -> tuple[str, list[str]]:
    """Return (SQL fragment, params) matching either json.dumps or compact JSON format.

    The returned SQL fragment is:  ``AND (af.message LIKE %s OR af.message LIKE %s)``
    with two parameters.  Append the fragment to the query and extend params with
    the returned list.
    """
    clause = " AND (af.message LIKE %s OR af.message LIKE %s)"
    params = [f'%"module_name": "{module}"%', f'%"module_name":"{module}"%']
    return clause, params


# ---------------------------------------------------------------------------
# GET /api/dq/issues
# ---------------------------------------------------------------------------


async def _dq_issues(request: Request) -> Response:
    """GET /api/dq/issues -- paginated list of dq_* firings with datastream info.

    Query params:
        project_id  (required) -- scope to one project (AD-5)
        monitor     (optional) -- filter by type: dq_volume|dq_timeliness|dq_duplication|dq_schema
        status      (optional) -- 'open' (unacknowledged) | 'acknowledged'

    Response (200):
        {
          "issues": [
            {
              "id":              "fire_...",
              "type":            "dq_volume",
              "label":           "Volume",
              "datastream_id":   "ds_...",     -- from metadata in message (best-effort)
              "datastream_name": "...",
              "module_name":     "...",
              "window_date":     "YYYY-MM-DD",
              "fired_at":        "ISO-8601",
              "severity":        "warning",
              "acknowledged":    false,
              "acknowledged_at": null | "ISO-8601",
              "message":         "...",
            },
            ...
          ],
          "total": int,
        }

    Issues are ordered by fired_at DESC (most recent first).
    Returns last 30 days of firings by default.
    """
    authorized, _identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Authentification requise"},
            status_code=401,
        )

    project_id = (request.query_params.get("project_id") or "").strip() or None
    if not project_id:
        return JSONResponse(
            {"code": "invalid_param", "message": "project_id est requis"},
            status_code=422,
        )

    monitor_filter = (request.query_params.get("monitor") or "").strip() or None
    status_filter = (request.query_params.get("status") or "").strip() or None

    if monitor_filter and monitor_filter not in _DQ_TYPES:
        return JSONResponse(
            {
                "code": "invalid_param",
                "message": f"monitor doit etre l'un de: {', '.join(_DQ_TYPES)}",
            },
            status_code=422,
        )
    if status_filter and status_filter not in ("open", "acknowledged"):
        return JSONResponse(
            {
                "code": "invalid_param",
                "message": "status doit etre 'open' ou 'acknowledged'",
            },
            status_code=422,
        )

    try:
        from core.db import get_connection  # noqa: PLC0415

        cutoff = datetime.now(tz=timezone.utc) - timedelta(days=30)
        params: list = [project_id, cutoff]
        sql = """
            SELECT
                af.id,
                af.type,
                af.window_date,
                af.fired_at,
                af.severity,
                af.acknowledged_at,
                af.message,
                af.project_id
            FROM app.alert_firings af
            WHERE af.project_id = %s
              AND af.type LIKE 'dq_%%'
              AND af.fired_at >= %s
        """
        if monitor_filter:
            sql += " AND af.type = %s"
            params.append(monitor_filter)
        if status_filter == "open":
            sql += " AND af.acknowledged_at IS NULL"
        elif status_filter == "acknowledged":
            sql += " AND af.acknowledged_at IS NOT NULL"

        sql += " ORDER BY af.fired_at DESC LIMIT 500"

        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(sql, params)
                cols = [d[0] for d in cur.description]
                rows = [dict(zip(cols, r)) for r in cur.fetchall()]

        issues = []
        for row in rows:
            msg = row.get("message") or ""
            ds_id, ds_name, module_name = _parse_firing_meta(msg)

            fired_at = row.get("fired_at")
            ack_at = row.get("acknowledged_at")

            issues.append(
                {
                    "id": row.get("id"),
                    "type": row.get("type"),
                    "label": _MONITOR_LABELS.get(row.get("type", ""), row.get("type", "")),
                    "datastream_id": ds_id,
                    "datastream_name": ds_name,
                    "module_name": module_name,
                    "window_date": (
                        row["window_date"].isoformat()
                        if row.get("window_date") and hasattr(row["window_date"], "isoformat")
                        else str(row.get("window_date", ""))
                    ),
                    "fired_at": (
                        fired_at.isoformat()
                        if fired_at and hasattr(fired_at, "isoformat")
                        else str(fired_at or "")
                    ),
                    "severity": row.get("severity", "warning"),
                    "acknowledged": ack_at is not None,
                    "acknowledged_at": (
                        ack_at.isoformat() if ack_at and hasattr(ack_at, "isoformat") else None
                    ),
                    "message": msg.split("| meta=")[0].strip() if "| meta=" in msg else msg,
                }
            )

        return JSONResponse({"issues": issues, "total": len(issues)})

    except Exception as exc:
        logger.warning("dq_api: issues_error project=%s: %s", project_id, exc)
        return JSONResponse(
            {"code": "db_error", "message": "Erreur lors de la recuperation des incidents"},
            status_code=500,
        )


# ---------------------------------------------------------------------------
# POST /api/dq/issues/{firing_id}/acknowledge
# ---------------------------------------------------------------------------


async def _dq_acknowledge(request: Request) -> Response:
    """POST /api/dq/issues/{firing_id}/acknowledge -- mark a DQ firing as acknowledged.

    Path params:
        firing_id  -- the alert_firings.id to acknowledge

    Query params:
        project_id  (required) -- project scoping (AD-5: must match the firing's project_id)

    Response (200):
        {"id": "fire_...", "acknowledged_at": "ISO-8601"}

    Error responses:
        401 -- unauthorized
        403 -- firing belongs to a different project
        404 -- firing not found or already a non-DQ type
        422 -- missing project_id
        500 -- DB error
    """
    authorized, _identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Authentification requise"},
            status_code=401,
        )

    firing_id = request.path_params.get("firing_id", "")
    project_id = (request.query_params.get("project_id") or "").strip() or None
    if not project_id:
        return JSONResponse(
            {"code": "invalid_param", "message": "project_id est requis"},
            status_code=422,
        )

    try:
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            # Fetch the firing to verify it exists, is a DQ type, and belongs to this project.
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, type, project_id, acknowledged_at
                    FROM app.alert_firings
                    WHERE id = %s AND type LIKE 'dq_%%'
                    """,
                    (firing_id,),
                )
                row = cur.fetchone()

            if row is None:
                return JSONResponse(
                    {"code": "not_found", "message": "Incident introuvable"},
                    status_code=404,
                )

            firing_project = row[2]
            if firing_project != project_id:
                return JSONResponse(
                    {"code": "forbidden", "message": "Acces non autorise a cet incident"},
                    status_code=403,
                )

            ack_at = datetime.now(tz=timezone.utc)

            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE app.alert_firings
                    SET acknowledged_at = %s
                    WHERE id = %s
                    """,
                    (ack_at, firing_id),
                )
            conn.commit()

        logger.info(
            "dq_api: acknowledged firing_id=%s project=%s", firing_id, project_id
        )
        return JSONResponse({"id": firing_id, "acknowledged_at": ack_at.isoformat()})

    except Exception as exc:
        logger.warning(
            "dq_api: acknowledge_error firing_id=%s project=%s: %s",
            firing_id,
            project_id,
            exc,
        )
        return JSONResponse(
            {"code": "db_error", "message": "Erreur lors de l'acquittement"},
            status_code=500,
        )


# ---------------------------------------------------------------------------
# GET /api/dq/history  (Story 13.3)
# ---------------------------------------------------------------------------


async def _dq_history(request: Request) -> Response:
    """GET /api/dq/history -- taux de succes par moniteur sur une fenetre glissante.

    Query params:
        project_id  (requis) -- scope projet (AD-5)
        monitor     (opt)    -- filtrer par type (dq_volume|...|dq_date_format)
        days        (opt)    -- fenetre en jours (defaut 30, max 90)

    Taux de succes : pour chaque moniteur, on identifie les window_date distinctes
    ayant au moins 1 firing (alert_firings). Le taux est calcule comme :
        success_rate = 1 - (jours_avec_firing / evaluated_days)
    ou evaluated_days = nombre de jours distincts ayant au moins 1 firing OU 1 pull_job
    reussi (state='done') dans la fenetre.

    Distinction "jamais evalue" vs "succes" (M4) :
      - Si evaluated_days == 0 (aucun firing ET aucun pull_job reussi) : success_rate = null
        et evaluated_days = 0. Indique que le moniteur n'a pas de donnee sur la periode.
      - Sinon : success_rate = (1 - days_with_firing/evaluated_days) * 100 arrondi 1 dec.
    Limitation : un projet tres recent ou sans pull_jobs verra evaluated_days = 0 (null).
    Ce choix evite l'affichage trompeur "100 %" pour un moniteur jamais execute.

    AD-5 : identity_has_project_access verifie que le porteur du token appartient au
    projet ; renvoie 404 non-disclosant si refus (jamais 403).

    Response (200):
        {
          "window_days": 30,
          "monitors": [
            {
              "type": "dq_volume",
              "label": "Volume",
              "days_with_firing": 3,
              "evaluated_days": 25,
              "success_rate": 88.0,    -- % (arrondi 1 decimale) | null si evaluated_days=0
            },
            ...
          ]
        }
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Authentification requise"},
            status_code=401,
        )

    project_id = (request.query_params.get("project_id") or "").strip() or None
    if not project_id:
        return JSONResponse(
            {"code": "invalid_param", "message": "project_id est requis"},
            status_code=422,
        )

    monitor_filter = (request.query_params.get("monitor") or "").strip() or None
    if monitor_filter and monitor_filter not in _DQ_TYPES:
        return JSONResponse(
            {
                "code": "invalid_param",
                "message": f"monitor doit etre l'un de : {', '.join(_DQ_TYPES)}",
            },
            status_code=422,
        )

    try:
        days = int(request.query_params.get("days", "30"))
    except (ValueError, TypeError):
        days = 30
    days = max(1, min(days, 90))

    try:
        from core.db import get_connection  # noqa: PLC0415

        cutoff = datetime.now(tz=timezone.utc) - timedelta(days=days)

        with get_connection() as conn:
            # AD-5 : verifie l'acces au projet avant toute lecture.
            if not _enforce_project_scope(project_id, identity, conn):
                return JSONResponse(
                    {"code": "not_found", "message": "Projet introuvable"},
                    status_code=404,
                )

            # Requete 1 : jours avec firing par moniteur.
            params: list = [project_id, cutoff]
            sql = """
                SELECT type, COUNT(DISTINCT window_date) AS days_with_firing
                FROM app.alert_firings
                WHERE project_id = %s
                  AND type LIKE 'dq_%%'
                  AND fired_at >= %s
            """
            if monitor_filter:
                sql += " AND type = %s"
                params.append(monitor_filter)
            sql += " GROUP BY type"

            with conn.cursor() as cur:
                cur.execute(sql, params)
                rows = cur.fetchall()

            firing_by_type: dict[str, int] = {r[0]: int(r[1]) for r in rows}

            # Requete 2 : jours avec au moins 1 pull_job reussi (state='done') pour
            # les datastreams du projet, pour calculer evaluated_days (M4).
            # Utilise completed_at (reel) ou enqueued_at (fallback si NULL).
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT COUNT(DISTINCT
                        DATE(COALESCE(pj.completed_at, pj.enqueued_at) AT TIME ZONE 'UTC')
                    ) AS evaluated_days
                    FROM app.pull_jobs pj
                    JOIN app.datastreams ds ON ds.id = pj.datastream_id
                    WHERE ds.project_id = %s
                      AND pj.state = 'done'
                      AND COALESCE(pj.completed_at, pj.enqueued_at) >= %s
                    """,
                    (project_id, cutoff),
                )
                row_eval = cur.fetchone()
            evaluated_days = int(row_eval[0]) if row_eval and row_eval[0] is not None else 0

        types_to_report = (monitor_filter,) if monitor_filter else _DQ_TYPES
        result = []
        for dq_type in types_to_report:
            days_with_firing = firing_by_type.get(dq_type, 0)
            # M4 : success_rate=null quand evaluated_days=0 (jamais evalue).
            if evaluated_days == 0:
                success_rate = None
            else:
                success_rate = round(
                    (1.0 - days_with_firing / evaluated_days) * 100.0, 1
                )
                success_rate = max(0.0, min(100.0, success_rate))
            result.append(
                {
                    "type": dq_type,
                    "label": _MONITOR_LABELS.get(dq_type, dq_type),
                    "days_with_firing": days_with_firing,
                    "evaluated_days": evaluated_days,
                    "success_rate": success_rate,
                }
            )

        return JSONResponse({"window_days": days, "monitors": result})

    except Exception as exc:
        logger.warning("dq_api: history_error project=%s: %s", project_id, exc)
        return JSONResponse(
            {"code": "db_error", "message": "Erreur lors du calcul de l'historique"},
            status_code=500,
        )


# ---------------------------------------------------------------------------
# GET /api/dq/datastream-freshness  (Story 13.3)
# ---------------------------------------------------------------------------


async def _dq_datastream_freshness(request: Request) -> Response:
    """GET /api/dq/datastream-freshness -- derniere extraction par datastream.

    Source : app.pull_jobs JOIN app.pull_verifications (via pull_id) + app.datastreams.

    Schema reel de app.pull_jobs (ATTENTION : pas de colonnes 'status' ni 'row_count') :
      state        : queued | running | done | failed | dead_letter
      completed_at : timestamp | NULL  (rempli quand le job est termine)
      enqueued_at  : timestamp         (toujours present)
      pull_id      : uuid FK -> app.pull_verifications.pull_id

    app.pull_verifications (jointure par pull_id) :
      verdict      : ok | partial | failed | NULL
      actual_rows  : int | NULL   (nombre de lignes extraites)

    Derivation des champs de reponse :
      last_pull_at    = MAX(COALESCE(completed_at, enqueued_at)) par datastream_id
      last_status     = derive de state du dernier job :
                          'done' + verdict in (ok, partial) -> 'success'
                          'done' + verdict='failed'         -> 'partial_failure'
                          'done' + verdict IS NULL          -> 'success'  (pas de verif)
                          'failed' | 'dead_letter'          -> 'error'
                          'running' | 'queued'              -> 'running'
      last_success_at = dernier completed_at où state='done' et verdict IN ('ok','partial',NULL)
      row_count       = pv.actual_rows du dernier job termine (state='done')

    Integre dans /api/dq/datastream-freshness (route separee) pour ne pas surcharger
    le payload initial du tableau de bord.

    AD-5 : identity_has_project_access controle l'acces au projet ; 404 si refus.

    Query params:
        project_id  (requis) -- scope projet (AD-5)

    Response (200):
        {
          "datastreams": [
            {
              "datastream_id":   "ds_...",
              "datastream_name": "...",
              "module_name":     "...",
              "last_pull_at":    "ISO-8601" | null,
              "last_success_at": "ISO-8601" | null,
              "last_status":     "success" | "partial_failure" | "error" | "running" | null,
              "row_count":       int | null,
            },
            ...
          ]
        }
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Authentification requise"},
            status_code=401,
        )

    project_id = (request.query_params.get("project_id") or "").strip() or None
    if not project_id:
        return JSONResponse(
            {"code": "invalid_param", "message": "project_id est requis"},
            status_code=422,
        )

    try:
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            # AD-5 : verifie l'acces au projet avant toute lecture.
            if not _enforce_project_scope(project_id, identity, conn):
                return JSONResponse(
                    {"code": "not_found", "message": "Projet introuvable"},
                    status_code=404,
                )

            with conn.cursor() as cur:
                # Jointure datastreams -> pull_jobs (colonnes reelles: state, completed_at)
                # + pull_verifications (actual_rows, verdict) via pull_id.
                # LATERAL ORDER BY COALESCE(completed_at, enqueued_at) DESC : prend le job
                # le plus recent (meme si completed_at est NULL pour un job en cours).
                cur.execute(
                    """
                    SELECT
                        ds.id              AS datastream_id,
                        ds.name            AS datastream_name,
                        ds.module_name     AS module_name,
                        pj.completed_at    AS last_pull_at,
                        pj.state           AS last_state,
                        pv.verdict         AS last_verdict,
                        pv.actual_rows     AS row_count
                    FROM app.datastreams ds
                    LEFT JOIN LATERAL (
                        SELECT pj2.completed_at, pj2.state, pj2.pull_id
                        FROM app.pull_jobs pj2
                        WHERE pj2.datastream_id = ds.id
                        ORDER BY COALESCE(pj2.completed_at, pj2.enqueued_at) DESC NULLS LAST
                        LIMIT 1
                    ) pj ON TRUE
                    LEFT JOIN app.pull_verifications pv ON pv.pull_id = pj.pull_id
                    WHERE ds.project_id = %s
                      AND ds.enabled = TRUE
                    ORDER BY ds.name
                    """,
                    (project_id,),
                )
                cols = [d[0] for d in cur.description]
                rows = [dict(zip(cols, r)) for r in cur.fetchall()]

            # Requete separee pour last_success_at : dernier completed_at ou state='done'
            # et verdict IN ('ok', 'partial') ou verdict IS NULL (pas de verification).
            ds_ids = [r["datastream_id"] for r in rows]
            last_success: dict[str, str | None] = {}
            if ds_ids:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        SELECT DISTINCT ON (pj.datastream_id)
                            pj.datastream_id,
                            pj.completed_at
                        FROM app.pull_jobs pj
                        LEFT JOIN app.pull_verifications pv ON pv.pull_id = pj.pull_id
                        WHERE pj.datastream_id = ANY(%s)
                          AND pj.state = 'done'
                          AND (pv.verdict IN ('ok', 'partial') OR pv.verdict IS NULL)
                        ORDER BY pj.datastream_id, pj.completed_at DESC NULLS LAST
                        """,
                        (ds_ids,),
                    )
                    for srow in cur.fetchall():
                        ds_id = srow[0]
                        ts = srow[1]
                        last_success[ds_id] = (
                            ts.isoformat() if ts and hasattr(ts, "isoformat") else None
                        )

        def _map_state_to_status(state: str | None, verdict: str | None) -> str | None:
            """Derive un statut lisible depuis pj.state + pv.verdict."""
            if state is None:
                return None
            if state == "done":
                if verdict == "failed":
                    return "partial_failure"
                return "success"  # ok, partial ou None (pas de verification) -> succes
            if state in ("failed", "dead_letter"):
                return "error"
            if state in ("running", "queued"):
                return "running"
            return state  # valeur inconnue: passer telle quelle

        datastreams = []
        for row in rows:
            last_pull_at = row.get("last_pull_at")
            datastreams.append(
                {
                    "datastream_id": row.get("datastream_id", ""),
                    "datastream_name": row.get("datastream_name", ""),
                    "module_name": row.get("module_name", ""),
                    "last_pull_at": (
                        last_pull_at.isoformat()
                        if last_pull_at and hasattr(last_pull_at, "isoformat")
                        else None
                    ),
                    "last_success_at": last_success.get(row.get("datastream_id", ""), None),
                    "last_status": _map_state_to_status(
                        row.get("last_state"), row.get("last_verdict")
                    ),
                    "row_count": row.get("row_count"),
                }
            )

        return JSONResponse({"datastreams": datastreams})

    except Exception as exc:
        logger.warning("dq_api: freshness_error project=%s: %s", project_id, exc)
        return JSONResponse(
            {"code": "db_error", "message": "Erreur lors du calcul de la fraicheur"},
            status_code=500,
        )


# ---------------------------------------------------------------------------
# POST /api/dq/evaluate  (Story 13.3)
# ---------------------------------------------------------------------------


async def _dq_evaluate(request: Request) -> Response:
    """POST /api/dq/evaluate -- declenche run_dq_monitors manuellement (Story 13.3).

    Rate-limite : 2 appels / 60 s par project_id (fenetre glissante en memoire).
    Audit : ecrit une ligne audit_log avec action 'dq.evaluate.triggered'.
    Execution : run_dq_monitors(project_id) lance en thread separee (non bloquant).

    AD-5 (fail-closed obligatoire car WRITE) : identity_has_project_access controle
    l'acces avant le rate-limit check et le lancement du thread. 404 si refus.

    Query params:
        project_id  (requis) -- scope projet (AD-5)

    Response (200):
        {"status": "triggered", "project_id": "...", "triggered_at": "ISO-8601"}

    Error responses:
        401 -- non authentifie
        404 -- projet non trouve ou acces refuse (non-disclosant, AD-5)
        422 -- project_id manquant
        429 -- rate limit depasse (Retry-After header)
        500 -- erreur interne
    """
    authorized, identity = await _check_auth(request)
    if not authorized:
        return JSONResponse(
            {"code": "unauthorized", "message": "Authentification requise"},
            status_code=401,
        )

    project_id = (request.query_params.get("project_id") or "").strip() or None
    if not project_id:
        return JSONResponse(
            {"code": "invalid_param", "message": "project_id est requis"},
            status_code=422,
        )

    # AD-5 (fail-closed : c'est un WRITE). Ouvre une connexion dedicee pour le check.
    # fail_closed=True : si le check ACL leve une exception (DB down, curseur defaillant),
    # _enforce_project_scope renvoie False au lieu de propager -- garantit qu'aucun WRITE
    # n'est execute quand le check d'autorisation est indisponible (jamais fail-open).
    try:
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            if not _enforce_project_scope(project_id, identity, conn, fail_closed=True):
                return JSONResponse(
                    {"code": "not_found", "message": "Projet introuvable"},
                    status_code=404,
                )
    except Exception as exc:
        # get_connection() lui-meme a echoue (pas le check ACL qui est fail-closed).
        # Renvoie 503 non-disclosant : DB injoignable, pas d'execution.
        logger.warning("dq_api: evaluate_scope_check_failed project=%s: %s", project_id, exc)
        return JSONResponse(
            {"code": "service_unavailable", "message": "Service temporairement indisponible"},
            status_code=503,
        )

    # Rate-limit : 2 appels / 60 s par project_id.
    if _evaluate_rate_limited(project_id):
        return JSONResponse(
            {
                "code": "rate_limited",
                "message": (
                    "Limite de declenchement manuel atteinte "
                    f"({_EVALUATE_RATE_LIMIT} appels / {int(_EVALUATE_RATE_WINDOW)} s). "
                    "Reessayez dans quelques secondes."
                ),
                "retry_after": _EVALUATE_RATE_WINDOW,
            },
            status_code=429,
            headers={"Retry-After": str(int(_EVALUATE_RATE_WINDOW))},
        )

    triggered_at = datetime.now(tz=timezone.utc)

    # Audit (best-effort, ne bloque pas la reponse).
    try:
        from core.audit import write_audit_row  # noqa: PLC0415

        write_audit_row(
            identity=identity or "anonymous",
            action="dq.evaluate.triggered",
            provider_account="",
            connection_ref="",
            metadata={"project_id": project_id, "triggered_at": triggered_at.isoformat()},
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("dq_api: evaluate_audit_failed project=%s: %s", project_id, exc)

    # Lance run_dq_monitors en arriere-plan (thread) pour ne pas bloquer la reponse.
    def _run() -> None:
        try:
            from core.dq_monitors import run_dq_monitors  # noqa: PLC0415

            summary = run_dq_monitors(project_id=project_id)
            logger.info(
                "dq_api: evaluate_complete project=%s total_issues=%d errors=%d",
                project_id,
                summary.get("total_issues", 0),
                summary.get("errors", 0),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("dq_api: evaluate_run_failed project=%s: %s", project_id, exc)

    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="dq_eval")
    executor.submit(_run)
    executor.shutdown(wait=False)

    return JSONResponse(
        {
            "status": "triggered",
            "project_id": project_id,
            "triggered_at": triggered_at.isoformat(),
        }
    )


# ---------------------------------------------------------------------------
# Story 13.4 -- shared helper for get_data_quality_report MCP tool.
# Keeps DB query logic in one place (DRY); REST endpoints above delegate to
# the same logic via their own queries.  Returns a plain dict that the MCP
# tool can wrap in the AD-1 envelope without touching Starlette / HTTP.
# ---------------------------------------------------------------------------


def fetch_dq_report_data(
    project_id: str,
    conn,
    *,
    module: str | None = None,
) -> dict:
    """Fetch DQ monitor summary + open issues for the MCP tool (Story 13.4).

    Called exclusively by the MCP tool ``get_data_quality_report``; the REST
    endpoints (GET /api/dq/summary, /api/dq/issues) run their own independent
    queries.

    Executes FIVE queries against the live Postgres connection already opened
    by the caller (conn):

    1. Q1: COUNT enabled datastreams (denominator for healthy_pct).
    2. Q2: GROUP BY type count of unresolved firings (last 30 days) -- builds
       the 5-monitor summary.  Sets ``monitors_unavailable`` in the result when
       this query fails (H1 coherence flag).
    3. Q3: up to 50 most-recent unacknowledged firings (open issues).
       Each issue carries ``firing_id`` (primary provenance, AD-9) and
       ``pull_ids`` (TEXT[], best-effort -- hardcoded ``'{}'`` by
       ``infra_alerts.write_infra_firing`` for aggregated DQ monitors, so
       typically empty; preserved as-is without fabrication, per AD-9).
       If Q2 failed but Q3 succeeded, ``total_unresolved`` is derived from
       ``len(issues)`` so summary and structuredContent stay coherent (H1).
    4. Q4: MAX(completed_at) from pull_jobs (freshness).
    5. Q5: COUNT(DISTINCT DATE(completed_at)) for last 30 days (evaluated_days).

    Parameter *module* is an opaque filter passed as-is to the WHERE clause
    via ``_module_filter_clauses()`` (AD-2: no module-name hardcoding here).
    When None, all datastreams are included.

    Returns a dict:
      {
        "monitors":              [{"type", "label", "healthy_pct", "unresolved_count"}, ...],
        "monitors_unavailable":  bool,  # True when Q2 failed; caller surfaces warning
        "issues":                [{"firing_id", "type", "datastream_id", "datastream_name",
                                   "module_name", "message", "fired_at", "severity",
                                   "pull_ids"}, ...],
        "freshness_last_pull_at": "ISO-8601" | null,
        "evaluated_days_30d":    int,
        "total_unresolved":      int,
      }

    Never raises -- any query failure returns partial/empty data so the MCP
    tool can still build a meaningful degraded response (AD-1 durable path).
    """
    cutoff_30d = datetime.now(tz=timezone.utc) - timedelta(days=30)
    result: dict = {
        "monitors": [],
        "issues": [],
        "freshness_last_pull_at": None,
        "evaluated_days_30d": 0,
        "total_unresolved": 0,
    }

    # ------------------------------------------------------------------
    # Query 1: count enabled datastreams (denominator for healthy_pct).
    # ------------------------------------------------------------------
    try:
        ds_sql = (
            "SELECT COUNT(*) FROM app.datastreams WHERE project_id = %s AND enabled = TRUE"
        )
        ds_params: list = [project_id]
        if module is not None:
            ds_sql += " AND module_name = %s"
            ds_params.append(module)
        with conn.cursor() as cur:
            cur.execute(ds_sql, ds_params)
            row = cur.fetchone()
            total_streams = int(row[0]) if row and row[0] is not None else 0
    except Exception as exc:  # noqa: BLE001
        logger.debug("fetch_dq_report_data: stream_count_failed project=%s: %s", project_id, exc)
        total_streams = 0

    # ------------------------------------------------------------------
    # Query 2: unresolved firings in last 30 days (per monitor type).
    # ------------------------------------------------------------------
    firing_query_ok = False
    try:
        firing_sql = """
            SELECT type, COUNT(*) AS cnt
            FROM app.alert_firings af
            WHERE af.project_id = %s
              AND af.type LIKE 'dq_%%'
              AND af.fired_at >= %s
              AND af.acknowledged_at IS NULL
        """
        firing_params: list = [project_id, cutoff_30d]
        if module is not None:
            # Module filter: extract from message JSON metadata (best-effort).
            # We use a LIKE heuristic on the message column because module_name
            # is stored in the JSON suffix, not a dedicated column.
            # json.dumps produces "module_name": "gads" (WITH space after colon).
            # We match both the canonical form and the compact form for robustness.
            _fc, _fp = _module_filter_clauses(module)
            firing_sql += _fc
            firing_params.extend(_fp)
        firing_sql += " GROUP BY type"
        with conn.cursor() as cur:
            cur.execute(firing_sql, firing_params)
            unresolved_by_type: dict[str, int] = {r[0]: int(r[1]) for r in cur.fetchall()}
        firing_query_ok = True
    except Exception as exc:  # noqa: BLE001
        logger.debug("fetch_dq_report_data: firing_count_failed project=%s: %s", project_id, exc)
        unresolved_by_type = {}

    # Build monitor summaries for all 5 standard DQ types -- but ONLY when the
    # firing query actually ran. If it failed (DB unreachable), we cannot assert
    # anything about monitor health: return monitors=[] ("nothing evaluated"),
    # NOT 5 monitors at 100% healthy (that would be a false all-clear). AD-1
    # durable path: honest degraded state, never a fabricated green.
    #
    # H1 coherence: if monitors are unavailable (firing_query_ok=False), we flag
    # monitors_unavailable=True so the caller can signal this honestly in the
    # summary.  total_unresolved will be derived from the raw issues list (Q3)
    # after Q3 runs, preventing a contradictory "0 issues" while issues[] is
    # non-empty.
    total_unresolved = 0
    monitor_rows = []
    for dq_type in (_DQ_TYPES if firing_query_ok else ()):
        unresolved_count = unresolved_by_type.get(dq_type, 0)
        total_unresolved += unresolved_count
        if total_streams > 0:
            healthy = max(0, total_streams - unresolved_count)
            healthy_pct = round(healthy / total_streams * 100, 1)
        else:
            healthy_pct = 100.0  # no streams => trivially healthy (no data to evaluate)
        monitor_rows.append(
            {
                "type": dq_type,
                "label": _MONITOR_LABELS[dq_type],
                "healthy_pct": healthy_pct,
                "unresolved_count": unresolved_count,
            }
        )
    result["monitors"] = monitor_rows
    result["total_unresolved"] = total_unresolved
    # Track monitor availability for H1 coherence fix (resolved after Q3).
    result["monitors_unavailable"] = not firing_query_ok

    # ------------------------------------------------------------------
    # Query 3: open issues (with pull_ids for AD-9) -- top 50 by fired_at.
    # ------------------------------------------------------------------
    try:
        issues_sql = """
            SELECT
                af.id          AS firing_id,
                af.type,
                af.fired_at,
                af.severity,
                af.message,
                af.pull_ids
            FROM app.alert_firings af
            WHERE af.project_id = %s
              AND af.type LIKE 'dq_%%'
              AND af.fired_at >= %s
              AND af.acknowledged_at IS NULL
        """
        issues_params: list = [project_id, cutoff_30d]
        if module is not None:
            # Same dual-format LIKE as Q2 (json.dumps with space + compact fallback).
            _ic, _ip = _module_filter_clauses(module)
            issues_sql += _ic
            issues_params.extend(_ip)
        issues_sql += " ORDER BY af.fired_at DESC LIMIT 50"

        with conn.cursor() as cur:
            cur.execute(issues_sql, issues_params)
            cols = [d[0] for d in cur.description]
            issue_rows = [dict(zip(cols, r)) for r in cur.fetchall()]

        issues = []
        for row in issue_rows:
            msg = row.get("message") or ""
            ds_id, ds_name, mod_name = _parse_firing_meta(msg)
            fired_at = row.get("fired_at")
            raw_pull_ids = row.get("pull_ids") or []
            issues.append(
                {
                    "firing_id": row.get("firing_id", ""),
                    "type": row.get("type", ""),
                    "datastream_id": ds_id,
                    "datastream_name": ds_name,
                    "module_name": mod_name,
                    "message": msg.split("| meta=")[0].strip() if "| meta=" in msg else msg,
                    "fired_at": (
                        fired_at.isoformat()
                        if fired_at and hasattr(fired_at, "isoformat")
                        else str(fired_at or "")
                    ),
                    "severity": row.get("severity", "warning"),
                    # AD-9: pull_ids is TEXT[] in Postgres; psycopg returns list or None.
                    "pull_ids": list(raw_pull_ids) if raw_pull_ids else [],
                }
            )
        result["issues"] = issues
        # H1 coherence: if the firing GROUP-BY query (Q2) failed but Q3 succeeded,
        # monitors=[] and total_unresolved=0 would contradict a non-empty issues list.
        # Derive total_unresolved from the raw issue count so summary<->structuredContent
        # are consistent.  The caller (MCP tool) will see monitors_unavailable=True and
        # surface an explicit warning in the LLM-channel summary.
        if result.get("monitors_unavailable") and issues:
            result["total_unresolved"] = len(issues)
    except Exception as exc:  # noqa: BLE001
        logger.debug("fetch_dq_report_data: issues_failed project=%s: %s", project_id, exc)

    # ------------------------------------------------------------------
    # Query 4: freshness -- last successful pull_jobs.completed_at.
    # ------------------------------------------------------------------
    try:
        fresh_sql = """
            SELECT MAX(pj.completed_at)
            FROM app.pull_jobs pj
            JOIN app.datastreams ds ON ds.id = pj.datastream_id
            WHERE ds.project_id = %s
              AND pj.state = 'done'
        """
        fresh_params: list = [project_id]
        if module is not None:
            fresh_sql += " AND ds.module_name = %s"
            fresh_params.append(module)
        with conn.cursor() as cur:
            cur.execute(fresh_sql, fresh_params)
            row = cur.fetchone()
            last_pull = row[0] if row and row[0] is not None else None
            result["freshness_last_pull_at"] = (
                last_pull.isoformat()
                if last_pull and hasattr(last_pull, "isoformat")
                else None
            )
    except Exception as exc:  # noqa: BLE001
        logger.debug("fetch_dq_report_data: freshness_failed project=%s: %s", project_id, exc)

    # ------------------------------------------------------------------
    # Query 5: evaluated_days_30d (days with at least one pull_job done).
    # ------------------------------------------------------------------
    try:
        eval_sql = """
            SELECT COUNT(DISTINCT DATE(pj.completed_at AT TIME ZONE 'UTC'))
            FROM app.pull_jobs pj
            JOIN app.datastreams ds ON ds.id = pj.datastream_id
            WHERE ds.project_id = %s
              AND pj.state = 'done'
              AND pj.completed_at >= %s
        """
        eval_params: list = [project_id, cutoff_30d]
        if module is not None:
            eval_sql += " AND ds.module_name = %s"
            eval_params.append(module)
        with conn.cursor() as cur:
            cur.execute(eval_sql, eval_params)
            row = cur.fetchone()
            result["evaluated_days_30d"] = int(row[0]) if row and row[0] is not None else 0
    except Exception as exc:  # noqa: BLE001
        logger.debug(
            "fetch_dq_report_data: eval_days_failed project=%s: %s", project_id, exc
        )

    return result


# ---------------------------------------------------------------------------
# Route table -- exported; admin_api wires these at startup.
# ---------------------------------------------------------------------------

DQ_ROUTES: list[Route] = [
    Route("/api/dq/summary", endpoint=_dq_summary, methods=["GET"]),
    Route("/api/dq/issues", endpoint=_dq_issues, methods=["GET"]),
    Route(
        "/api/dq/issues/{firing_id}/acknowledge",
        endpoint=_dq_acknowledge,
        methods=["POST"],
    ),
    # Story 13.3 -- nouveaux endpoints
    Route("/api/dq/history", endpoint=_dq_history, methods=["GET"]),
    Route("/api/dq/datastream-freshness", endpoint=_dq_datastream_freshness, methods=["GET"]),
    Route("/api/dq/evaluate", endpoint=_dq_evaluate, methods=["POST"]),
]
