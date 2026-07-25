"""Anomaly surveillance evaluator (Story 5.4, AD-13).

Reads anomalies_daily mart after nightly dbt run.
Ranks anomalies by |zscore| (largest first).
For each anomaly, joins mirror.context_events for candidate causes.
Writes firings to app.alert_firings (type='anomaly').
Never raises. Guarded by ANOMALY_ALERTS_ENABLED (default false).

Design decisions:
  - Reads from marts.anomalies_daily (z-score pre-computed by dbt).
  - Context events: LEFT JOIN mirror.context_events for candidate context.
    Never asserts causality — only lists context events (AD-9).
  - pull_ids = [] for anomaly firings: anomalies are derived signals (dbt),
    not raw data events. Provenance = anomalies_daily dbt model.
  - Severity: 'warning' for 3.0 <= |z| < 5.0, 'error' for |z| >= 5.0.
  - One firing per anomaly row (one per project × connector × metric × date).
  - Prohibited causal language: 'cause', 'caused by', 'due to', 'because of',
    'as a result of', 'en raison de' -- unit-tested (AC10).

Environment variables:
  ANOMALY_ALERTS_ENABLED  default "false"  -- master switch (HG-5 equivalent)
  TOOROW_DUCKDB_PATH   -- path to local DuckDB warehouse file
"""

from __future__ import annotations

import logging
import os
from datetime import date, datetime, timedelta, timezone

logger = logging.getLogger(__name__)


def _duckdb_mart_prefix(project_id: str | None = None) -> str:
    """Return the DuckDB schema prefix for mart tables.

    Delegated to the single naming point (Story 24.1): legacy ``main_marts.``
    by default, the org schema of *project_id* under TOOROW_ORG_SCHEMAS=1.
    """
    from core import warehouse_tenancy  # noqa: PLC0415

    return warehouse_tenancy.mart_prefix(project_id)


def _mart_prefixes_for_scan(project_id: str | None) -> list[str]:
    """Return the mart schema prefixes to scan for a cross-project evaluation.

    Story 24.4 (AC5, CONDITION DE FLIP): the ``project_id=None`` scan is the path
    that would degrade SILENTLY once ``main_marts`` no longer exists under the org
    topology. Its resolution now depends on the flag:

      * flag OFF (default) OR an explicit ``project_id`` -> the EXACT legacy single
        prefix (``main_marts.`` or the project's org schema) -- bit-identical, no
        active-org listing (the resolver/list is NOT called on the OFF path).
      * flag ON with ``project_id=None`` -> ONE prefix per ACTIVE org
        (``org_<wslug>_marts.``), resolved via ``warehouse_tenancy.list_active_orgs``
        (the single naming point). No ``mart_prefix(None)`` degradation to
        ``main_marts`` remains on this path.
    """
    from core import warehouse_tenancy  # noqa: PLC0415

    if project_id is not None or not warehouse_tenancy.org_schemas_enabled():
        # Legacy / project-scoped path: exactly one prefix, unchanged behaviour.
        return [warehouse_tenancy.mart_prefix(project_id)]

    # Flag ON, cross-project scan: fan out over every active org's marts schema.
    return [f"{schemas.marts}." for schemas in warehouse_tenancy.list_active_orgs()]


def _severity(zscore: float) -> str:
    """Return severity string based on |zscore|.

    'error' for |z| >= 5.0, 'warning' for 3.0 <= |z| < 5.0.
    """
    return "error" if abs(zscore) >= 5.0 else "warning"


def _fetch_context_events_for_anomaly(
    project_id: str,
    anomaly_date: date,
    conn,
) -> list[str]:
    """Fetch context event labels from mirror.context_events for an anomaly date.

    Returns list of label strings. Returns [] if table does not exist (graceful).
    Never raises.
    """
    try:
        rows = conn.execute(
            "SELECT label FROM mirror.context_events "
            "WHERE project_id = ? AND event_date = ?",
            [project_id, str(anomaly_date)],
        ).fetchall()
        return [r[0] for r in rows if r[0]]
    except Exception as exc:
        # mirror.context_events may not exist (CatalogException) or be empty
        logger.debug(
            "anomaly_alerts: context_events_fetch_skipped project=%s date=%s: %s",
            project_id,
            anomaly_date,
            exc,
        )
        return []


def format_anomaly_line(anomaly: dict) -> str:
    """Format an anomaly dict as a one-line French summary for the LLM channel.

    Per AC6 spec:
      ⚠️ Anomalie : {metric} (z=+{z:.1f}) le {date} — Contexte : {labels}
      ⚠️ Anomalie : {metric} (z=+{z:.1f}) le {date} — Contexte manquant.

    AD-9 hard rule: MUST NOT use 'cause', 'caused by', 'due to', 'because of',
    'as a result of', 'en raison de'. Unit-tested in test_anomaly_alerts.py.
    Uses ASCII-safe formatting (AD-2: no em-dash in log strings).
    """
    metric = (anomaly.get("metric") or "").lower()
    zscore = float(anomaly.get("zscore", 0))
    z_sign = "+" if zscore >= 0 else ""
    z_str = f"z={z_sign}{zscore:.1f}"
    anomaly_date = anomaly.get("window_date") or anomaly.get("date", "")
    context_labels: list[str] = anomaly.get("context_events", [])

    if context_labels:
        labels_str = ", ".join(f'"{lbl}"' for lbl in context_labels)
        context_part = f"Contexte : {labels_str}"
    else:
        context_part = "Contexte manquant."

    return f"⚠️ Anomalie : {metric} ({z_str}) le {anomaly_date} -- {context_part}"


def _build_widget_alert(anomaly: dict) -> dict:
    """Build the meta.alerts[] widget dict for an anomaly (AC6).

    Format:
      {
        "code": "anomaly",
        "severity": "warning" or "error",
        "metric": <metric_name>,
        "message": "Metric anormalement ... Contexte : ...",
        "observed_value": <float>,
        "expected_value": <float>,
        "zscore": <float>,
        "context_events": [<label>, ...],
        "context_missing": <bool>,
      }

    AD-9: no causal language in message. Only 'Contexte :' or 'Contexte manquant.'
    """
    metric = (anomaly.get("metric") or "").lower()
    metric_display = metric.capitalize()
    zscore = float(anomaly.get("zscore", 0))
    z_sign = "+" if zscore >= 0 else ""
    z_str = f"z={z_sign}{zscore:.1f}"
    observed = float(anomaly.get("observed_value", 0))
    expected = float(anomaly.get("expected_value", 0))
    severity = _severity(zscore)
    context_labels: list[str] = anomaly.get("context_events", [])

    if context_labels:
        labels_str = ", ".join(context_labels)
        context_part = f"Contexte : {labels_str}."
    else:
        context_part = "Contexte manquant."

    message = (
        f"{metric_display} anormalement "
        f"{'elevee' if zscore > 0 else 'basse'} ({z_str}). {context_part}"
    )

    return {
        "code": "anomaly",
        "severity": severity,
        "metric": metric,
        "message": message,
        "observed_value": observed,
        "expected_value": expected,
        "zscore": zscore,
        "context_events": context_labels,
        "context_missing": len(context_labels) == 0,
    }


def evaluate_anomalies(
    evaluation_date: date | None = None,
    project_id: str | None = None,
) -> list[dict]:
    """Evaluate anomalies_daily mart and write firings to app.alert_firings.

    Args:
        evaluation_date: The business day to evaluate (defaults to yesterday).
        project_id:      Optional scope to a single project (None = all projects).

    Returns:
        List of anomaly dicts for surfacing via channels. Empty list on any error
        or when ANOMALY_ALERTS_ENABLED=false.

    Never raises -- catches all exceptions and returns [] (graceful degradation).
    """
    if os.environ.get("ANOMALY_ALERTS_ENABLED", "false").lower() != "true":
        logger.debug(
            "anomaly_alerts: evaluate_anomalies skipped"
            " -- ANOMALY_ALERTS_ENABLED not true"
        )
        return []

    if evaluation_date is None:
        evaluation_date = date.today() - timedelta(days=1)

    db_path = os.environ.get("TOOROW_DUCKDB_PATH", "")
    firings: list[dict] = []

    try:
        import duckdb  # noqa: PLC0415

        # Story 24.4 (AC5): one prefix on the legacy/scoped path, N (one per active
        # org) when flag ON and scanning all projects -- no silent main_marts read.
        schema_prefixes = _mart_prefixes_for_scan(project_id)

        # Query anomalies_daily for evaluation_date, ranked by |zscore| DESC
        with duckdb.connect(db_path, read_only=True) as duck_conn:
            project_filter = ""
            base_params: list = [str(evaluation_date)]
            if project_id is not None:
                project_filter = " AND project_id = ?"
                base_params.append(project_id)

            # Build anomaly dicts with context events, accumulating across schemas.
            anomaly_dicts: list[dict] = []
            for schema_prefix in schema_prefixes:
                sql = (
                    f"SELECT project_id, connector, metric, observed_value, expected_value, zscore "  # noqa: S608
                    f"FROM {schema_prefix}anomalies_daily "
                    f"WHERE date = ? {project_filter} "
                    f"ORDER BY ABS(zscore) DESC"
                )
                try:
                    rows = duck_conn.execute(sql, list(base_params)).fetchall()
                except Exception as exc:  # noqa: BLE001 -- an org schema may be empty/absent
                    # Review 24.4 F-2: single legacy prefix (flag OFF) = a real
                    # main_marts error, keep it at WARNING like pre-24.4; only
                    # the multi-org scan may skip absent schemas quietly.
                    log = logger.debug if len(schema_prefixes) > 1 else logger.warning
                    log(
                        "anomaly_alerts: schema_scan_skipped prefix=%s date=%s: %s",
                        schema_prefix,
                        evaluation_date,
                        exc,
                    )
                    continue

                for row in rows:
                    row_project_id, connector, metric, observed, expected, zscore = row
                    context_labels = _fetch_context_events_for_anomaly(
                        row_project_id, evaluation_date, duck_conn
                    )
                    anomaly_dicts.append({
                        "project_id": row_project_id,
                        "connector": connector,
                        "metric": metric,
                        "observed_value": float(observed),
                        "expected_value": float(expected),
                        "zscore": float(zscore),
                        "window_date": evaluation_date.isoformat(),
                        "context_events": context_labels,
                    })

            if not anomaly_dicts:
                logger.debug(
                    "anomaly_alerts: no_anomalies: date=%s", evaluation_date
                )
                return []

            logger.info(
                "anomaly_alerts: found %d anomalies for date=%s across %d schema(s)",
                len(anomaly_dicts),
                evaluation_date,
                len(schema_prefixes),
            )

        # Write firings to Postgres
        from ulid import ULID  # noqa: PLC0415

        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as pg_conn:
            for anomaly in anomaly_dicts:
                firing_id = f"fire_{ULID()}"
                fired_at = datetime.now(tz=timezone.utc)
                severity = _severity(anomaly["zscore"])

                try:
                    with pg_conn.cursor() as cur:
                        cur.execute(
                            """
                            INSERT INTO app.alert_firings
                                (id, definition_id, type, fired_at, observed_value,
                                 threshold, pull_ids, window_date, severity,
                                 project_id, metric)
                            VALUES (%s, NULL, 'anomaly', %s, %s, %s, %s, %s, %s, %s, %s)
                            ON CONFLICT DO NOTHING  -- F-2: one firing/night/grain
                            """,
                            (
                                firing_id,
                                fired_at,
                                anomaly["observed_value"],
                                anomaly["expected_value"],  # baseline mean as threshold
                                [],  # pull_ids=[]: derived signal, no raw pull (see docstring)
                                evaluation_date,
                                severity,
                                anomaly.get("project_id", "default"),
                                anomaly.get("metric", ""),
                            ),
                        )
                    pg_conn.commit()
                except Exception as exc:
                    logger.warning(
                        "anomaly_alerts: firing_insert_error metric=%s: %s",
                        anomaly.get("metric"),
                        exc,
                    )
                    continue

                firing_dict = {
                    "firing_id": firing_id,
                    "definition_id": None,
                    "code": "anomaly",
                    "severity": severity,
                    "metric": anomaly["metric"],
                    "observed_value": anomaly["observed_value"],
                    "expected_value": anomaly["expected_value"],
                    "zscore": anomaly["zscore"],
                    "pull_ids": [],
                    "window_date": evaluation_date.isoformat(),
                    "fired_at": fired_at.isoformat(),
                    "context_events": anomaly["context_events"],
                    "project_id": anomaly["project_id"],
                    "connector": anomaly["connector"],
                }
                firings.append(firing_dict)

                logger.info(
                    "anomaly_alerts: anomaly_fired: metric=%s zscore=%.4f"
                    " severity=%s firing_id=%s",
                    anomaly["metric"],
                    anomaly["zscore"],
                    severity,
                    firing_id,
                )

    except Exception as exc:
        logger.warning(
            "anomaly_alerts: evaluate_anomalies_error: %s", exc
        )
        return []

    logger.info(
        "anomaly_alerts: evaluation_complete: date=%s firings=%d",
        evaluation_date,
        len(firings),
    )
    return firings


# ---------------------------------------------------------------------------
# Fetch recent anomaly firings for a project (used by get_daily_report meta.alerts[])
# ---------------------------------------------------------------------------


def fetch_recent_anomaly_firings(
    project_id: str,
    conn,
    hours: int = 24,
) -> list[dict]:
    """Query recent anomaly firings (type='anomaly') for a project from Postgres.

    Used by get_daily_report to populate meta.alerts[] with anomaly alerts.
    Mirrors the structure of business_alerts.fetch_recent_alert_firings.

    Args:
        project_id: Scopes firings to the project (review-epic-5 F-1 —
                    alert_firings.project_id column since migration 013).
        conn:       An open psycopg connection (sync).
        hours:      Look-back window in hours (default 24).

    Returns:
        List of alert dicts in the meta.alerts[] shape. Empty list on error.
    """
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    id            AS firing_id,
                    observed_value,
                    threshold     AS expected_value,
                    fired_at,
                    window_date,
                    severity,
                    metric
                FROM app.alert_firings
                WHERE type = 'anomaly'
                  AND project_id = %s
                  AND fired_at >= NOW() - make_interval(hours => %s)
                ORDER BY fired_at DESC
                """,
                (project_id, hours),
            )
            cols = [desc[0] for desc in cur.description]
            rows = [dict(zip(cols, row)) for row in cur.fetchall()]

        result = []
        for row in rows:
            obs = float(row.get("observed_value", 0))
            exp = float(row.get("expected_value", 0))
            severity = row.get("severity", "warning")
            result.append({
                "code": "anomaly",
                "severity": severity,
                "metric": row.get("metric", ""),
                "message": (
                    f"Anomalie detectee sur {row.get('metric', '?')} "
                    f"(obs={obs:.2f}, baseline={exp:.2f})"
                ),
                "firing_id": row.get("firing_id"),
                "observed_value": obs,
                "expected_value": exp,
                "context_events": [],
                "context_missing": True,
            })
        return result
    except Exception as exc:
        logger.warning(
            "anomaly_alerts: fetch_recent_firings_error project_id=%s: %s",
            project_id,
            exc,
        )
        return []
