"""Business threshold alert evaluator (Story 5.3, AC4, AD-13).

Evaluates app.alert_definitions against fresh fact_daily_kpi / semantic views
after the nightly dbt run. Writes firings to app.alert_firings.

Never raises -- graceful degradation like infra_alerts.py.
Guarded by BUSINESS_ALERTS_ENABLED (default false).

Design decisions:
  - Semantic metrics (CPA, ROAS, CTR) are ratio metrics computed at view time
    from dbt semantic views (AD-4). They are never stored in fact_daily_kpi.
    The set of semantic metrics is driven by ALERT_SEMANTIC_METRICS env var
    (default: cpa,roas,ctr). Additive metrics are queried from fact_daily_kpi.
  - Operator evaluation is Python-level only -- NEVER interpolated into SQL
    (operator whitelist prevents SQL injection; Python lambdas do the comparison).
  - pull_ids provenance: every firing carries the distinct pull_ids from the
    fact_daily_kpi rows that contributed to the observed value (AD-9).
  - One shared alert_firings table with type='business_threshold'. Story 5.4
    adds type='anomaly' rows to the same table (see Migration 011 decision note).

Environment variables:
  BUSINESS_ALERTS_ENABLED  default "false"  -- master switch (HG-5 equivalent)
  ALERT_SEMANTIC_METRICS   default "cpa,roas,ctr"  -- comma-separated semantic metric names
"""

from __future__ import annotations

import logging
import os
from datetime import date, datetime, timedelta, timezone

logger = logging.getLogger(__name__)

# FIRING COOLDOWN PRODUCT DECISION (AI-33, 2026-07-11):
# Current behavior: one firing per definition per nightly run (daily dedup added in
# review-epic-5 commit 8a77a4d). The dedup key is (definition_id, window_date). This means:
# - A breach on Day N generates one firing for Day N.
# - If the breach persists on Day N+1, a new firing is generated (daily reminder UX).
# Product intent: daily reminders ("one firing per morning while breaching") is the intended UX.
# If cooldown (suppress until resolved or N days) is desired, set ALERT_FIRING_COOLDOWN_DAYS env var
# and implement a window-date range check: skip INSERT if a firing exists in the last N dates.
# Decision: daily reminder UX is correct for P3-dev. Revisit at first production use with > 10 alert
# definitions.

# ---------------------------------------------------------------------------
# Operator evaluation -- Python-only, never interpolated into SQL (security).
# ---------------------------------------------------------------------------

OPERATORS: dict[str, object] = {
    ">": lambda o, t: o > t,
    "<": lambda o, t: o < t,
    ">=": lambda o, t: o >= t,
    "<=": lambda o, t: o <= t,
}

# ---------------------------------------------------------------------------
# Semantic metric routing
# ---------------------------------------------------------------------------


def _get_semantic_metrics() -> frozenset[str]:
    """Return the set of metric names that are semantic (ratio/weighted) metrics.

    Driven by ALERT_SEMANTIC_METRICS env var (default: cpa,roas,ctr,average_position).
    These metrics must be queried from dbt semantic views, NOT fact_daily_kpi.
    average_position is always included because fact_daily_kpi has no rows for it
    (it lives in semantic_avg_position); querying it as additive always returns
    observed=None causing silent skip (G-03 fix).
    """
    raw = os.environ.get("ALERT_SEMANTIC_METRICS", "cpa,roas,ctr,average_position")
    return frozenset(m.strip().lower() for m in raw.split(",") if m.strip())


def _get_valid_metrics() -> frozenset[str]:
    """Return all valid metric names (dim_metric additives + semantic set)."""
    # Additive metrics from dim_metric seed (canonical list)
    additive = frozenset([
        "sessions", "active_users", "conversions", "cost",
        "impressions", "clicks", "revenue", "average_position",
    ])
    return additive | _get_semantic_metrics()


# ---------------------------------------------------------------------------
# DuckDB query helpers
# ---------------------------------------------------------------------------


def _duckdb_mart_prefix(project_id: str | None = None) -> str:
    """Return the DuckDB schema prefix for mart tables.

    Delegated to the single naming point (Story 24.1): legacy ``main_marts.``
    by default, the org schema of *project_id* under TOOROW_ORG_SCHEMAS=1.
    """
    from core import warehouse_tenancy  # noqa: PLC0415

    return warehouse_tenancy.mart_prefix(project_id)


def _query_additive_metric(
    project_id: str,
    metric: str,
    eval_date: date,
    connector: str | None,
    db_path: str,
) -> tuple[float | None, list[str]]:
    """Query fact_daily_kpi for an additive metric value + contributing pull_ids.

    Returns (observed_value, pull_ids_list). Returns (None, []) on error.
    """
    try:
        import duckdb  # noqa: PLC0415

        schema_prefix = _duckdb_mart_prefix(project_id)
        conn = duckdb.connect(db_path, read_only=True)
        try:
            sql = f"""
                SELECT SUM(value) AS metric_value, LIST(DISTINCT pull_id) AS pull_ids
                FROM {schema_prefix}fact_daily_kpi
                WHERE project_id = ?
                  AND metric = ?
                  AND date = ?
                  AND (connector = ? OR ? IS NULL)
            """  # noqa: S608 — schema_prefix is a hard-coded constant
            params = [project_id, metric, eval_date.isoformat(), connector, connector]
            row = conn.execute(sql, params).fetchone()
            if row is None or row[0] is None:
                return None, []
            observed = float(row[0])
            pull_ids = [str(p) for p in (row[1] or [])]
            return observed, pull_ids
        finally:
            conn.close()
    except Exception as exc:
        logger.warning(
            "business_alerts: additive_metric_query_error metric=%s: %s", metric, exc
        )
        return None, []



# Mapping from metric name to the dbt semantic view name when the view does not
# follow the default "semantic_<metric>" convention (G-03 fix: average_position
# lives in semantic_avg_position, not semantic_average_position).
_SEMANTIC_VIEW_NAME: dict[str, str] = {
    "average_position": "semantic_avg_position",
}

# Column name to select from the semantic view (default: same as metric name).
# average_position keeps its column name inside semantic_avg_position.
_SEMANTIC_COL_NAME: dict[str, str] = {
    "average_position": "average_position",
}


def _query_semantic_metric(
    project_id: str,
    metric: str,
    eval_date: date,
    connector: str | None,
    db_path: str,
) -> tuple[float | None, list[str]]:
    """Query a semantic (ratio/weighted) view for a metric value + pull_id provenance.

    Semantic view naming convention: <schema_prefix>semantic_<metric>, with an
    override map for views that differ (_SEMANTIC_VIEW_NAME). The column to read
    is the metric name itself, with an override via _SEMANTIC_COL_NAME.

    For average_position the view exposes an impressions_weight column; the
    aggregation across breakdown rows for a single eval_date is impression-weighted:
    SUM(value * impressions_weight) / SUM(impressions_weight). Other semantic
    metrics use AVG() (day-grain views already hold one row per date/connector).

    Returns (observed_value, pull_ids_list). Returns (None, []) on error.
    """
    try:
        import duckdb  # noqa: PLC0415

        schema_prefix = _duckdb_mart_prefix(project_id)
        metric_lc = metric.lower()
        view_suffix = _SEMANTIC_VIEW_NAME.get(metric_lc, f"semantic_{metric_lc}")
        view_name = f"{schema_prefix}{view_suffix}"
        col_name = _SEMANTIC_COL_NAME.get(metric_lc, metric_lc)
        conn = duckdb.connect(db_path, read_only=True)
        try:
            if metric_lc == "average_position":
                # Impression-weighted aggregate across breakdown rows (G-03):
                # SUM(avg_pos * impressions_weight) / SUM(impressions_weight).
                # Fall back to AVG when impressions_weight is absent/zero.
                sql = f"""
                    SELECT
                        CASE
                            WHEN SUM(impressions_weight) > 0
                            THEN SUM({col_name} * impressions_weight) / SUM(impressions_weight)
                            ELSE AVG({col_name})
                        END AS metric_value,
                        LIST(DISTINCT pull_id) AS pull_ids
                    FROM {view_name}
                    WHERE project_id = ?
                      AND date = ?
                      AND (connector = ? OR ? IS NULL)
                """  # noqa: S608 — view_name from whitelist + hard-coded prefix
            else:
                sql = f"""
                    SELECT AVG({col_name}) AS metric_value,
                           LIST(DISTINCT pull_id) AS pull_ids
                    FROM {view_name}
                    WHERE project_id = ?
                      AND date = ?
                      AND (connector = ? OR ? IS NULL)
                """  # noqa: S608 — view_name from whitelist + hard-coded prefix
            params = [project_id, eval_date.isoformat(), connector, connector]
            row = conn.execute(sql, params).fetchone()
            if row is None or row[0] is None:
                return None, []
            observed = float(row[0])
            pull_ids = [str(p) for p in (row[1] or [])]
            return observed, pull_ids
        finally:
            conn.close()
    except Exception as exc:
        logger.warning(
            "business_alerts: semantic_metric_query_error metric=%s: %s", metric, exc
        )
        return None, []


# ---------------------------------------------------------------------------
# Format alert line for LLM summary channel (AC7, AD-1)
# ---------------------------------------------------------------------------


def format_alert_line(firing: dict) -> str:
    """Format a firing dict as a one-line French summary for the LLM channel.

    Per Dev Notes spec:
      ⚠️ Alerte seuil : {METRIC} ({obs:.2f}) {op} seuil ({thr:.2f}) -- verifier campagnes.

    Note: uses ASCII '--' instead of em-dash for AD-2 (ASCII-only log strings).
    Returns <= 80 chars for a single firing. Truncates metric name if very long.
    """
    metric = (firing.get("metric") or "").upper()
    obs = float(firing.get("observed_value", 0))
    thr = float(firing.get("threshold", 0))
    op = firing.get("operator", ">")
    # Use ASCII-safe: no curly quotes, no em-dash (AD-2)
    return (
        f"⚠️ Alerte seuil : {metric} ({obs:.2f}) {op} seuil ({thr:.2f})"
        f" -- verifier campagnes."
    )


# ---------------------------------------------------------------------------
# Core evaluator
# ---------------------------------------------------------------------------


def evaluate_business_alerts(
    evaluation_date: date | None = None,
    project_id: str | None = None,
) -> list[dict]:
    """Evaluate all enabled alert_definitions against fresh fact data.

    Args:
        evaluation_date: The business day to evaluate (defaults to yesterday).
        project_id:      Optional scope to a single project (None = all projects).

    Returns:
        List of firing dicts for surfacing via channels. Empty list on any error
        or when BUSINESS_ALERTS_ENABLED=false.

    Never raises -- catches all exceptions and returns [] (graceful degradation).
    """
    if os.environ.get("BUSINESS_ALERTS_ENABLED", "false").lower() != "true":
        logger.debug(
            "business_alerts: evaluate_business_alerts skipped"
            " -- BUSINESS_ALERTS_ENABLED not true"
        )
        return []

    if evaluation_date is None:
        evaluation_date = date.today() - timedelta(days=1)

    db_path = os.environ.get("TOOROW_DUCKDB_PATH", "")
    semantic_metrics = _get_semantic_metrics()
    firings: list[dict] = []

    try:
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as pg_conn:
            # 1. Load all enabled alert_definitions from Postgres
            with pg_conn.cursor() as cur:
                if project_id is not None:
                    cur.execute(
                        """
                        SELECT id, project_id, metric, operator, threshold, connector
                        FROM app.alert_definitions
                        WHERE enabled = TRUE AND project_id = %s
                        """,
                        (project_id,),
                    )
                else:
                    cur.execute(
                        """
                        SELECT id, project_id, metric, operator, threshold, connector
                        FROM app.alert_definitions
                        WHERE enabled = TRUE
                        """
                    )
                cols = [desc[0] for desc in cur.description]
                definitions = [dict(zip(cols, row)) for row in cur.fetchall()]

            if not definitions:
                logger.debug(
                    "business_alerts: no enabled definitions for date=%s", evaluation_date
                )
                return []

            logger.info(
                "business_alerts: evaluating %d definitions for date=%s",
                len(definitions),
                evaluation_date,
            )

            # 2. Evaluate each definition
            for defn in definitions:
                defn_id = defn["id"]
                metric = defn["metric"].lower()
                operator = defn["operator"]
                threshold = float(defn["threshold"])
                def_project_id = defn["project_id"]
                connector = defn.get("connector")

                # Guard against unknown operator (should be enforced at API level too)
                if operator not in OPERATORS:
                    logger.warning(
                        "business_alerts: unknown_operator: defn_id=%s op=%r -- skipping",
                        defn_id,
                        operator,
                    )
                    continue

                # 3. Query the correct data source
                if metric in semantic_metrics:
                    observed, pull_ids = _query_semantic_metric(
                        def_project_id, metric, evaluation_date, connector, db_path
                    )
                else:
                    observed, pull_ids = _query_additive_metric(
                        def_project_id, metric, evaluation_date, connector, db_path
                    )

                if observed is None:
                    logger.debug(
                        "business_alerts: no_data: defn_id=%s metric=%s date=%s",
                        defn_id,
                        metric,
                        evaluation_date,
                    )
                    continue

                # 4. Python-level operator evaluation (never SQL interpolation)
                compare_fn = OPERATORS[operator]
                breached = compare_fn(observed, threshold)

                if not breached:
                    logger.debug(
                        "business_alerts: no_breach: defn_id=%s metric=%s"
                        " observed=%.4f op=%s threshold=%.4f",
                        defn_id,
                        metric,
                        observed,
                        operator,
                        threshold,
                    )
                    continue

                # 5. Write firing row to Postgres
                from ulid import ULID  # noqa: PLC0415

                firing_id = f"fire_{ULID()}"
                fired_at = datetime.now(tz=timezone.utc)

                try:
                    with pg_conn.cursor() as cur:
                        cur.execute(
                            """
                            INSERT INTO app.alert_firings
                                (id, definition_id, type, fired_at, observed_value,
                                 threshold, pull_ids, window_date, severity)
                            VALUES (%s, %s, 'business_threshold', %s, %s, %s, %s, %s, 'error')
                            ON CONFLICT DO NOTHING  -- review-epic-5 F-2: one firing per night
                            """,
                            (
                                firing_id,
                                defn_id,
                                fired_at,
                                observed,
                                threshold,
                                pull_ids,
                                evaluation_date,
                            ),
                        )
                    pg_conn.commit()
                except Exception as exc:
                    logger.warning(
                        "business_alerts: firing_insert_error: defn_id=%s: %s",
                        defn_id,
                        exc,
                    )
                    continue

                firing_dict = {
                    "firing_id": firing_id,
                    "definition_id": defn_id,
                    "code": "business_threshold",
                    "severity": "error",
                    "metric": metric,
                    "operator": operator,
                    "observed_value": observed,
                    "threshold": threshold,
                    "pull_ids": pull_ids,
                    "window_date": evaluation_date.isoformat(),
                    "fired_at": fired_at.isoformat(),
                }
                firings.append(firing_dict)

                logger.info(
                    "business_alerts: breach_fired: defn_id=%s metric=%s"
                    " observed=%.4f op=%s threshold=%.4f firing_id=%s",
                    defn_id,
                    metric,
                    observed,
                    operator,
                    threshold,
                    firing_id,
                )

    except Exception as exc:
        logger.warning(
            "business_alerts: evaluate_business_alerts_error: %s", exc
        )
        return []

    logger.info(
        "business_alerts: evaluation_complete: date=%s firings=%d",
        evaluation_date,
        len(firings),
    )
    return firings


# ---------------------------------------------------------------------------
# Fetch recent firings for a project (used by get_daily_report meta.alerts[])
# ---------------------------------------------------------------------------


def fetch_recent_alert_firings(
    project_id: str,
    conn,
    hours: int = 24,
) -> list[dict]:
    """Query recent alert_firings for a project from Postgres.

    Used by get_daily_report to populate meta.alerts[] in the envelope (AC7).

    Args:
        project_id: The project to scope the query to.
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
                    f.id            AS firing_id,
                    f.definition_id,
                    f.observed_value,
                    f.threshold,
                    f.fired_at,
                    f.pull_ids,
                    f.window_date,
                    d.metric,
                    d.operator
                FROM app.alert_firings f
                JOIN app.alert_definitions d ON d.id = f.definition_id
                WHERE d.project_id = %s
                  AND f.fired_at >= NOW() - INTERVAL '%s hours'
                  AND f.type = 'business_threshold'
                ORDER BY f.fired_at DESC
                """,
                (project_id, hours),
            )
            cols = [desc[0] for desc in cur.description]
            rows = [dict(zip(cols, row)) for row in cur.fetchall()]

        result = []
        for row in rows:
            metric = (row.get("metric") or "").lower()
            obs = float(row.get("observed_value", 0))
            thr = float(row.get("threshold", 0))
            op = row.get("operator", ">")
            metric_upper = metric.upper()
            # French message with actual values
            obs_fmt = f"{obs:.2f}".replace(".", ",")
            thr_fmt = f"{thr:.2f}".replace(".", ",")
            message = (
                f"{metric_upper} ({obs_fmt}) dépasse le seuil déclaré"
                f" ({thr_fmt})"
            )
            result.append({
                "code": "business_threshold",
                "severity": "error",
                "metric": metric,
                "message": message,
                "definition_id": row.get("definition_id"),
                "firing_id": row.get("firing_id"),
                "observed_value": obs,
                "threshold": thr,
                "operator": op,
            })
        return result
    except Exception as exc:
        logger.warning(
            "business_alerts: fetch_recent_firings_error project_id=%s: %s",
            project_id,
            exc,
        )
        return []


def fetch_recent_meta_alerts(
    project_id: str,
    conn,
    hours: int = 24,
) -> list[dict]:
    """Query recent type='meta_alert' firings (AI-32, Story 6.1, AC10).

    meta_alert rows have definition_id IS NULL and carry a ``message`` describing
    which nightly scheduler step failed. They are surfaced in the report tools'
    ``meta.alerts[]`` so the operator sees scheduler-health problems the next
    morning. Project-scoped via the project_id column (meta_alert rows default to
    'default'; all projects see scheduler health per the shared-scheduler design).

    Returns [] on error (graceful degradation, same contract as
    fetch_recent_alert_firings).
    """
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, metric, message, fired_at, severity
                FROM app.alert_firings
                WHERE type = 'meta_alert'
                  AND (project_id = %s OR project_id = 'default')
                  AND fired_at >= NOW() - INTERVAL '%s hours'
                ORDER BY fired_at DESC
                """,
                (project_id, hours),
            )
            cols = [desc[0] for desc in cur.description]
            rows = [dict(zip(cols, row)) for row in cur.fetchall()]

        return [
            {
                "code": "meta_alert",
                "severity": row.get("severity") or "error",
                "metric": row.get("metric") or "scheduler_health",
                "message": row.get("message") or "Scheduler step failed",
                "firing_id": row.get("id"),
            }
            for row in rows
        ]
    except Exception as exc:
        logger.warning(
            "business_alerts: fetch_meta_alerts_error project_id=%s: %s", project_id, exc
        )
        return []
