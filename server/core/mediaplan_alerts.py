"""Evaluateur d'alertes pacing médiaplan (Story 22.6, FR9 / FR38 / CAP-26).

Lit les marts plan_pacing_by_line / by_channel / by_plan (via warehouse.query_plan_vs_actual)
pour chaque plan actif du projet ; compare le pace constaté aux seuils configurés dans
project_preferences ; écrit les dépassements dans app.alert_firings (type='mediaplan_pace').

Réutilise le chemin existant (5.3 / 5.4) -- JAMAIS un moteur d'alerting parallèle.
Guarded par MEDIAPLAN_ALERTS_ENABLED (défaut "true" -- peu risqué, lecture seule).

Règles métier actées (spike 22-0 §5, décision 6) :
  - Seuils asymétriques configurables par projet :
      mediaplan_overrun_threshold        : pace > seuil -> alerte ROUGE "Dépassement budgétaire"
      mediaplan_underdelivery_threshold  : pace < seuil -> alerte JAUNE "Sous-livraison"
  - Défauts documentés : +10 % et -10 % (jamais en dur dans le code -- lus depuis prefs).
  - Deux kind distincts : metric='mediaplan_pace_overrun' et 'mediaplan_pace_underdelivery'.
  - JAMAIS d'alerte sur is_plan_only, pace NULL, ou allocated_to_date = 0.
  - Évaluation PAR PLAN (jamais inter-plans) ; chaque alerte cite plan_version_id + formule.
  - Changer le seuil éteint/allume au run suivant (pas d'état persistant : ON CONFLICT DO NOTHING).

Provenance AD-9 :
  Chaque alerte cite : plan_id, plan_version_id, line_key/channel, pace constaté,
  seuil utilisé, threshold_source ('project_preference' ou 'documented_default'),
  formule = "(actual_to_date - allocated_to_date) / allocated_to_date".

Environment :
  MEDIAPLAN_ALERTS_ENABLED  default "true"
"""

from __future__ import annotations

import hashlib
import logging
import os
from datetime import date, datetime, timezone

logger = logging.getLogger(__name__)

# Longueur maximale de la colonne metric d'alert_firings.
_METRIC_KEY_MAX_LEN = 255


def _build_metric_key(kind: str, plan_id: str, level: str, row_key: str) -> str:
    """Construire la clé de dédup metric, sûre pour des row_key longs (E1-F-3).

    Clé lisible : ``kind:plan_id:level:row_key``. Si elle dépasse 255 caractères,
    une simple troncature ``[:255]`` ferait COLLISIONNER deux row_key longs partageant
    le même préfixe -> la 2e alerte serait avalée par le dédup (ON CONFLICT DO NOTHING).

    On garde donc un préfixe lisible (``kind:plan_id:level:`` + début du row_key) puis
    on remplace la FIN par ``sha256(clé complète)[:16]`` (déterministe) pour désambiguïser.
    La clé finale fait exactement 255 caractères et deux row_key distincts produisent
    des hashes distincts.
    """
    full = f"{kind}:{plan_id}:{level}:{row_key}"
    if len(full) <= _METRIC_KEY_MAX_LEN:
        return full
    digest = hashlib.sha256(full.encode("utf-8")).hexdigest()[:16]
    # 1 caractère de séparation ':' + 16 de hash = 17 réservés à la fin.
    prefix = full[: _METRIC_KEY_MAX_LEN - len(digest) - 1]
    return f"{prefix}:{digest}"

# ---------------------------------------------------------------------------
# Valeurs par défaut documentées (jamais interpolées en dur dans les requêtes)
# ---------------------------------------------------------------------------

DEFAULT_OVERRUN_THRESHOLD: float = 0.10        # +10 % -> alerte rouge
DEFAULT_UNDERDELIVERY_THRESHOLD: float = -0.10  # -10 % -> alerte jaune

PACE_FORMULA = "(actual_to_date - allocated_to_date) / allocated_to_date"

# Types et kinds d'alertes
ALERT_TYPE = "mediaplan_pace"
KIND_OVERRUN = "mediaplan_pace_overrun"
KIND_UNDERDELIVERY = "mediaplan_pace_underdelivery"


# ---------------------------------------------------------------------------
# Lecture des seuils depuis project_preferences
# ---------------------------------------------------------------------------


def _load_thresholds(project_id: str, conn) -> tuple[float, float, str, str]:
    """Lire les seuils depuis project_preferences pour le projet.

    Retourne (overrun_thr, underdelivery_thr, overrun_source, underdelivery_source).
    overrun_source / underdelivery_source = 'project_preference' | 'documented_default'.
    En cas d'erreur DB, retourne les défauts documentés (dégradation gracieuse).
    """
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT mediaplan_overrun_threshold, mediaplan_underdelivery_threshold
                FROM app.project_preferences
                WHERE project_id = %s
                """,
                (project_id,),
            )
            row = cur.fetchone()
    except Exception as exc:
        logger.warning(
            "mediaplan_alerts: load_thresholds_error project_id=%s: %s -- using defaults",
            project_id,
            exc,
        )
        return (
            DEFAULT_OVERRUN_THRESHOLD,
            DEFAULT_UNDERDELIVERY_THRESHOLD,
            "documented_default",
            "documented_default",
        )

    if row is None:
        return (
            DEFAULT_OVERRUN_THRESHOLD,
            DEFAULT_UNDERDELIVERY_THRESHOLD,
            "documented_default",
            "documented_default",
        )

    overrun_raw, underdelivery_raw = row[0], row[1]

    overrun_thr = float(overrun_raw) if overrun_raw is not None else DEFAULT_OVERRUN_THRESHOLD
    overrun_source = "project_preference" if overrun_raw is not None else "documented_default"

    underdelivery_thr = (
        float(underdelivery_raw)
        if underdelivery_raw is not None
        else DEFAULT_UNDERDELIVERY_THRESHOLD
    )
    underdelivery_source = (
        "project_preference" if underdelivery_raw is not None else "documented_default"
    )

    return overrun_thr, underdelivery_thr, overrun_source, underdelivery_source


# ---------------------------------------------------------------------------
# Récupération des plans actifs du projet
# ---------------------------------------------------------------------------


def _load_active_plan_ids(project_id: str, conn) -> list[str]:
    """Retourne la liste des plan_id actifs (avec une version active) pour le projet.

    Un plan actif = app.media_plans avec au moins une version is_active=TRUE.
    Retourne [] en cas d'erreur ou si la table n'existe pas encore (dégradation).
    """
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT mp.id
                FROM app.media_plans mp
                JOIN app.media_plan_versions mpv
                    ON mpv.plan_id = mp.id AND mpv.is_active = TRUE
                WHERE mp.project_id = %s
                """,
                (project_id,),
            )
            return [row[0] for row in cur.fetchall()]
    except Exception as exc:
        logger.warning(
            "mediaplan_alerts: load_active_plans_error project_id=%s: %s",
            project_id,
            exc,
        )
        return []


# ---------------------------------------------------------------------------
# Écriture d'un firing dans alert_firings
# ---------------------------------------------------------------------------


def _write_firing(
    *,
    conn,
    project_id: str,
    metric: str,
    severity: str,
    observed_value: float,
    threshold: float,
    window_date: date,
    message: str,
    metadata: dict,
) -> str | None:
    """Écrire un firing dans app.alert_firings (type='mediaplan_pace').

    Utilise ON CONFLICT DO NOTHING (dedup index alert_firings_dedup_mediaplan_pace
    sur (project_id, metric, window_date) WHERE type='mediaplan_pace').

    Retourne le firing_id si inséré, None si déjà présent ou erreur.
    """
    try:
        import json as _json  # noqa: PLC0415

        from ulid import ULID  # noqa: PLC0415

        firing_id = f"fire_{ULID()}"
        fired_at = datetime.now(tz=timezone.utc)

        # Encode metadata dans la colonne message (migration 014) : alert_firings
        # n'a pas de colonne metadata dediee. Un echec d'INSERT est un ERROR
        # (review 22.6 F-1) : une alerte non persistee ne doit jamais etre muette.
        meta_str = _json.dumps(metadata, ensure_ascii=True, default=str)
        full_message = f"{message} | {meta_str}"

        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO app.alert_firings
                    (id, definition_id, type, project_id, metric, fired_at,
                     observed_value, threshold, pull_ids, window_date, severity, message)
                VALUES (%s, NULL, %s, %s, %s, %s, %s, %s, '{}', %s, %s, %s)
                ON CONFLICT DO NOTHING
                """,
                (
                    firing_id,
                    ALERT_TYPE,
                    project_id,
                    metric,
                    fired_at,
                    observed_value,
                    threshold,
                    window_date,
                    severity,
                    full_message,
                ),
            )
        conn.commit()
        return firing_id
    except Exception as exc:
        # ERROR, pas warning (review 22.6 F-1) : un firing perdu = alerte muette.
        logger.error(
            "mediaplan_alerts: firing_insert_error project_id=%s metric=%s: %s",
            project_id,
            metric,
            exc,
        )
        return None


# ---------------------------------------------------------------------------
# Évaluation d'un ensemble de lignes pacing (par ligne ou par canal)
# ---------------------------------------------------------------------------


def _evaluate_rows(
    *,
    rows: list[dict],
    level: str,               # 'line' | 'channel' | 'plan'
    plan_id: str,
    plan_version_id: str,
    project_id: str,
    overrun_thr: float,
    underdelivery_thr: float,
    overrun_source: str,
    underdelivery_source: str,
    window_date: date,
    as_of_day: str | None,
    conn,
) -> list[dict]:
    """Évaluer les lignes pacing d'un niveau donné et écrire les firings.

    Retourne la liste des dicts de firings produits.

    E2-F-2 (DOC) : aux niveaux 'channel' et 'plan', l'exclusion des lignes plan-only
    ne repose PAS sur une colonne ``is_plan_only`` (absente à ces grains) mais sur
    ``allocated_to_date IS NULL`` produit par les marts pour les rollups 100 %
    plan-only (garde ``allocated is None`` plus bas). Quand la colonne
    ``paceable_line_count`` EST présente (rollups channel/plan), un rollup à
    ``paceable_line_count == 0`` (aucune ligne réellement paceable) est ignoré
    défensivement : aucun pace n'y est significatif.
    """
    firings: list[dict] = []

    for row in rows:
        # --- Exclusions obligatoires ---
        is_plan_only = row.get("is_plan_only")
        # Niveau 'plan' et 'channel' n'ont pas is_plan_only ; on vérifie pace + alloc.
        if is_plan_only:
            logger.debug(
                "mediaplan_alerts: skip_plan_only plan=%s level=%s key=%s",
                plan_id, level, _row_key(row, level),
            )
            continue

        # E2-F-2 : garde défensive quand paceable_line_count est présent (channel/plan).
        # Un rollup 100 % plan-only (0 ligne paceable) ne porte aucun pace signifiant.
        paceable = row.get("paceable_line_count")
        if paceable is not None and int(paceable) == 0:
            logger.debug(
                "mediaplan_alerts: skip_zero_paceable plan=%s level=%s key=%s",
                plan_id, level, _row_key(row, level),
            )
            continue

        pace = row.get("pace")
        if pace is None:
            logger.debug(
                "mediaplan_alerts: skip_null_pace plan=%s level=%s key=%s",
                plan_id, level, _row_key(row, level),
            )
            continue

        allocated = row.get("allocated_to_date")
        if allocated is None or float(allocated) == 0.0:
            logger.debug(
                "mediaplan_alerts: skip_zero_alloc plan=%s level=%s key=%s alloc=%s",
                plan_id, level, _row_key(row, level), allocated,
            )
            continue

        pace_float = float(pace)
        row_key = _row_key(row, level)
        row_label = row.get("label") or row.get("channel") or row_key

        # --- Vérification dépassement (rouge) ---
        if pace_float > overrun_thr:
            # E1-F-3 : clé de dédup sûre pour des row_key longs (pas de collision).
            metric_key = _build_metric_key(KIND_OVERRUN, plan_id, level, row_key)
            message_fr = (
                f"Dépassement budgétaire : {row_label} "
                f"(pace {pace_float:+.1%}, seuil {overrun_thr:+.1%})"
            )
            metadata = {
                "kind": KIND_OVERRUN,
                "level": level,
                "plan_id": plan_id,
                "plan_version_id": plan_version_id,
                "row_key": row_key,
                "pace": pace_float,
                "overrun_threshold": overrun_thr,
                "threshold_source": overrun_source,
                "formula": PACE_FORMULA,
                "allocated_to_date": float(allocated),
                "actual_to_date": row.get("actual_to_date"),
                # E1-F-4 : date de référence effective du chiffre (AD-9 provenance).
                "as_of_day": as_of_day,
            }
            firing_id = _write_firing(
                conn=conn,
                project_id=project_id,
                metric=metric_key,
                severity="error",
                observed_value=pace_float,
                threshold=overrun_thr,
                window_date=window_date,
                message=message_fr,
                metadata=metadata,
            )
            if firing_id:
                firings.append({
                    "firing_id": firing_id,
                    "kind": KIND_OVERRUN,
                    "severity": "error",
                    "level": level,
                    "plan_id": plan_id,
                    "plan_version_id": plan_version_id,
                    "row_key": row_key,
                    "pace": pace_float,
                    "threshold": overrun_thr,
                    "threshold_source": overrun_source,
                    "message": message_fr,
                })
                logger.info(
                    "mediaplan_alerts: overrun_fired plan=%s level=%s key=%s"
                    " pace=%.4f threshold=%.4f firing_id=%s",
                    plan_id, level, row_key, pace_float, overrun_thr, firing_id,
                )

        # --- Vérification sous-livraison (jaune) ---
        if pace_float < underdelivery_thr:
            # E1-F-3 : clé de dédup sûre pour des row_key longs (pas de collision).
            metric_key = _build_metric_key(KIND_UNDERDELIVERY, plan_id, level, row_key)
            message_fr = (
                f"Sous-livraison : {row_label} "
                f"(pace {pace_float:+.1%}, seuil {underdelivery_thr:+.1%})"
            )
            metadata = {
                "kind": KIND_UNDERDELIVERY,
                "level": level,
                "plan_id": plan_id,
                "plan_version_id": plan_version_id,
                "row_key": row_key,
                "pace": pace_float,
                "underdelivery_threshold": underdelivery_thr,
                "threshold_source": underdelivery_source,
                "formula": PACE_FORMULA,
                "allocated_to_date": float(allocated),
                "actual_to_date": row.get("actual_to_date"),
                # E1-F-4 : date de référence effective du chiffre (AD-9 provenance).
                "as_of_day": as_of_day,
            }
            firing_id = _write_firing(
                conn=conn,
                project_id=project_id,
                metric=metric_key,
                severity="warning",
                observed_value=pace_float,
                threshold=underdelivery_thr,
                window_date=window_date,
                message=message_fr,
                metadata=metadata,
            )
            if firing_id:
                firings.append({
                    "firing_id": firing_id,
                    "kind": KIND_UNDERDELIVERY,
                    "severity": "warning",
                    "level": level,
                    "plan_id": plan_id,
                    "plan_version_id": plan_version_id,
                    "row_key": row_key,
                    "pace": pace_float,
                    "threshold": underdelivery_thr,
                    "threshold_source": underdelivery_source,
                    "message": message_fr,
                })
                logger.info(
                    "mediaplan_alerts: underdelivery_fired plan=%s level=%s key=%s"
                    " pace=%.4f threshold=%.4f firing_id=%s",
                    plan_id, level, row_key, pace_float, underdelivery_thr, firing_id,
                )

    return firings


def _row_key(row: dict, level: str) -> str:
    """Retourne la clé naturelle d'une ligne pacing selon le niveau."""
    if level == "line":
        return row.get("line_key") or ""
    if level == "channel":
        return row.get("channel") or ""
    # level == 'plan'
    return row.get("plan_id") or ""


# ---------------------------------------------------------------------------
# Évaluateur principal
# ---------------------------------------------------------------------------


def evaluate_mediaplan_alerts(
    project_id: str | None = None,
    evaluation_date: date | None = None,
) -> list[dict]:
    """Evaluer les alertes pacing pour tous les plans actifs du projet.

    Args:
        project_id:      Projet à évaluer. None = tous les projets avec des plans actifs.
        evaluation_date: Date de référence (window_date du firing). Défaut = hier.

    Retourne la liste des firings produits (dicts). [] en cas d'erreur ou de flag off.
    Ne lève jamais.
    """
    if os.environ.get("MEDIAPLAN_ALERTS_ENABLED", "true").lower() != "true":
        logger.debug(
            "mediaplan_alerts: evaluate_mediaplan_alerts skipped"
            " -- MEDIAPLAN_ALERTS_ENABLED not true"
        )
        return []

    if evaluation_date is None:
        from datetime import timedelta  # noqa: PLC0415
        evaluation_date = date.today() - timedelta(days=1)

    all_firings: list[dict] = []

    try:
        from core import warehouse as _wh  # noqa: PLC0415
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as pg_conn:
            # Résoudre les projets à évaluer
            if project_id is not None:
                project_ids = [project_id]
            else:
                project_ids = _load_all_project_ids(pg_conn)

            for pid in project_ids:
                try:
                    project_firings = _evaluate_project(
                        project_id=pid,
                        pg_conn=pg_conn,
                        wh=_wh,
                        evaluation_date=evaluation_date,
                    )
                    all_firings.extend(project_firings)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "mediaplan_alerts: project_eval_error project_id=%s: %s",
                        pid,
                        exc,
                    )

    except Exception as exc:
        logger.warning(
            "mediaplan_alerts: evaluate_mediaplan_alerts_error: %s", exc
        )
        return []

    logger.info(
        "mediaplan_alerts: evaluation_complete date=%s firings=%d",
        evaluation_date,
        len(all_firings),
    )
    return all_firings


def _load_all_project_ids(conn) -> list[str]:
    """Retourne tous les project_id ayant au moins un plan actif."""
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT mp.project_id
                FROM app.media_plans mp
                JOIN app.media_plan_versions mpv
                    ON mpv.plan_id = mp.id AND mpv.is_active = TRUE
                """
            )
            return [row[0] for row in cur.fetchall()]
    except Exception as exc:
        logger.warning(
            "mediaplan_alerts: load_all_projects_error: %s", exc
        )
        return []


def _evaluate_project(
    *,
    project_id: str,
    pg_conn,
    wh,
    evaluation_date: date,
) -> list[dict]:
    """Evaluer tous les plans actifs d'un projet. Retourne la liste des firings."""
    overrun_thr, underdelivery_thr, overrun_src, underdelivery_src = _load_thresholds(
        project_id, pg_conn
    )

    plan_ids = _load_active_plan_ids(project_id, pg_conn)
    if not plan_ids:
        logger.debug(
            "mediaplan_alerts: no_active_plans project_id=%s", project_id
        )
        return []

    logger.info(
        "mediaplan_alerts: evaluating project_id=%s plans=%d"
        " overrun_thr=%.4f underdelivery_thr=%.4f",
        project_id,
        len(plan_ids),
        overrun_thr,
        underdelivery_thr,
    )

    project_firings: list[dict] = []

    for plan_id in plan_ids:
        try:
            pacing = wh.query_plan_vs_actual(project_id=project_id, plan_id=plan_id)
        except Exception as exc:
            logger.warning(
                "mediaplan_alerts: warehouse_error project_id=%s plan_id=%s: %s",
                project_id,
                plan_id,
                exc,
            )
            continue

        # Extraire plan_version_id depuis la première ligne disponible
        plan_version_id = _extract_version_id(pacing)

        # E1-F-4 : as_of_day = date de référence effective du chiffre, portée par le
        # row plan-level du pacing (mart). Injectée dans les metadata de chaque firing.
        as_of_day = _extract_as_of_day(pacing)

        common = dict(
            plan_id=plan_id,
            plan_version_id=plan_version_id,
            project_id=project_id,
            overrun_thr=overrun_thr,
            underdelivery_thr=underdelivery_thr,
            overrun_source=overrun_src,
            underdelivery_source=underdelivery_src,
            window_date=evaluation_date,
            as_of_day=as_of_day,
            conn=pg_conn,
        )

        # Niveau ligne
        project_firings.extend(
            _evaluate_rows(rows=pacing.get("lines", []), level="line", **common)
        )
        # Niveau canal/support (channel)
        project_firings.extend(
            _evaluate_rows(rows=pacing.get("channels", []), level="channel", **common)
        )
        # Niveau plan global
        project_firings.extend(
            _evaluate_rows(rows=pacing.get("plan", []), level="plan", **common)
        )

    return project_firings


def _extract_version_id(pacing: dict) -> str:
    """Extraire le plan_version_id depuis le résultat de query_plan_vs_actual."""
    for key in ("lines", "channels", "plan"):
        rows = pacing.get(key) or []
        if rows and rows[0].get("plan_version_id"):
            return str(rows[0]["plan_version_id"])
    return ""


def _extract_as_of_day(pacing: dict) -> str | None:
    """Extraire l'as_of_day du row plan-level du pacing (E1-F-4, AD-9).

    C'est la date de référence effective à laquelle les chiffres to-date sont arrêtés.
    Portée par le mart plan_pacing_by_plan (clé 'plan'). Retourne None si absente.
    """
    plan_rows = pacing.get("plan") or []
    if plan_rows:
        value = plan_rows[0].get("as_of_day")
        if value:
            return str(value)
    return None


# ---------------------------------------------------------------------------
# Récupération des firings récents pour le morning briefing (chemin 5.3/5.4)
# ---------------------------------------------------------------------------


def fetch_recent_mediaplan_firings(
    project_id: str,
    conn,
    hours: int = 24,
) -> list[dict]:
    """Lire les firings récents type='mediaplan_pace' pour un projet.

    Utilisé par le morning briefing (get_daily_report / _build_project_briefing)
    pour alimenter meta.alerts[], exactement comme fetch_recent_alert_firings (5.3)
    et fetch_recent_anomaly_firings (5.4).

    Args:
        project_id: Projet à filtrer.
        conn:       Connexion psycopg ouverte (sync).
        hours:      Fenêtre de look-back en heures (défaut 24).

    Retourne [] en cas d'erreur (dégradation gracieuse).
    """
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    id          AS firing_id,
                    metric,
                    observed_value,
                    threshold,
                    fired_at,
                    window_date,
                    severity,
                    message
                FROM app.alert_firings
                WHERE type = %s
                  AND project_id = %s
                  AND fired_at >= NOW() - make_interval(hours => %s)
                ORDER BY fired_at DESC
                """,
                (ALERT_TYPE, project_id, hours),
            )
            cols = [desc[0] for desc in cur.description]
            rows = [dict(zip(cols, row)) for row in cur.fetchall()]

        result = []
        for row in rows:
            metric = row.get("metric") or ""
            severity = row.get("severity") or "warning"
            obs = float(row.get("observed_value") or 0)
            thr = float(row.get("threshold") or 0)
            raw_msg = row.get("message") or ""
            # Extraire uniquement la partie lisible avant le JSON de metadata
            display_msg = raw_msg.split(" | ")[0] if " | " in raw_msg else raw_msg

            # Distinguer overrun vs underdelivery depuis le metric key
            if KIND_OVERRUN in metric:
                code = KIND_OVERRUN
            else:
                code = KIND_UNDERDELIVERY

            result.append({
                "code": code,
                "severity": severity,
                "metric": metric,
                "message": display_msg,
                "firing_id": row.get("firing_id"),
                "observed_value": obs,
                "threshold": thr,
            })
        return result
    except Exception as exc:
        logger.warning(
            "mediaplan_alerts: fetch_recent_firings_error project_id=%s: %s",
            project_id,
            exc,
        )
        return []


# ---------------------------------------------------------------------------
# Formatage 1 ligne pour le canal LLM (pattern format_alert_line / format_anomaly_line)
# ---------------------------------------------------------------------------


def format_mediaplan_alert_line(firing: dict) -> str:
    """Formater un firing pacing en une ligne française pour le canal LLM.

    Format : Alerte pacing : {message_court} (pace {obs:+.1%}, seuil {thr:+.1%}).
    ASCII-only stdout ; accentué dans les messages.
    """
    msg = firing.get("message") or ""
    obs = float(firing.get("observed_value") or 0)
    thr = float(firing.get("threshold") or 0)
    # Pas d'emoji dans le log stdout (AD-2 : ASCII-only) ; emoji ok dans le message
    return f"Alerte pacing mediaplan : {msg} (pace {obs:+.1%}, seuil {thr:+.1%})."
