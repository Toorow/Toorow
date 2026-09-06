"""Anomaly surveillance evaluator (Story 5.4, AD-13).

Reads anomalies_daily mart after nightly dbt run.
Ranks anomalies by |zscore| (largest first).
For each anomaly, joins mirror.context_events for candidate causes.
Writes firings to app.alert_firings (type='anomaly').
Never raises. Guarded by ANOMALY_ALERTS_ENABLED (default true -- an off-switch:
an absent or empty anomalies_daily mart yields nothing).

Design decisions:
  - Reads from marts.anomalies_daily (z-score pre-computed by dbt).
  - Context events: candidate context is read from mirror.context_events when
    the warehouse carries it, else from the record app.context_events (AI-344,
    context-hub.md amendment of 2026-09-01); when neither can serve, the anomaly
    keeps its verdict and SAYS the walk was unavailable instead of "no cause".
    Never asserts causality — only lists context events (AD-9).
  - pull_ids = [] for anomaly firings: anomalies are derived signals (dbt),
    not raw data events. Provenance = anomalies_daily dbt model.
  - Severity: 'warning' for 3.0 <= |z| < 5.0, 'error' for |z| >= 5.0.
  - One firing per anomaly row (one per project × connector × metric × date).
  - Prohibited causal language: 'cause', 'caused by', 'due to', 'because of',
    'as a result of', 'en raison de' -- unit-tested (AC10).

Environment variables:
  ANOMALY_ALERTS_ENABLED  default "true"   -- off-switch; empty mart, nothing fired
  TOOROW_DUCKDB_PATH   -- path to local DuckDB warehouse file
"""

from __future__ import annotations

import contextlib
import logging
import os
from datetime import date, datetime, timedelta, timezone

# An alert line is a RECIT: it is read by a person and by the model, so it is
# rendered in the reader's language and never spelled here (`analyze-and-test.md`,
# amendment 2026-08-25). The three alert sentences of this module -- the text
# channel, the widget message and the firing message -- resolve through the one
# catalogue, which is also how the module stops carrying two alert languages.
from core.narrative_phrases import phrase

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


# ---------------------------------------------------------------------------
# Story 53.8 — CAV-13: how many observations the detector needs before it can
# say anything at all.
#
# `metric_baselines` computes the mean AND the sample stddev over a window that
# INCLUDES the tested observation. That bounds the attainable z-score:
#
#     max |z| = (n - 1) / sqrt(n)
#
# so the configured threshold is unreachable below a certain n -- 11 at the
# default 3.0, 27 for the `error` severity at 5.0. Below it, a x10 spike on day 5
# is MATHEMATICALLY incapable of firing, and the screen is indistinguishable from
# a healthy monitored series.
#
# The number is DERIVED from the configured threshold, never written down: a
# hardcoded 11 becomes a lie the day someone edits ANOMALY_Z_THRESHOLD, and the
# disclosure would go on claiming it was measured.
#
# The window itself is NOT changed. Excluding the tested point would change the
# detector's sensitivity and add firings; proactive-assertions.md decision 3
# refuses exactly that. This makes the silence informative, not the detector
# louder.
# ---------------------------------------------------------------------------

ANOMALY_Z_THRESHOLD_DEFAULT = 3.0


def anomaly_z_threshold() -> float:
    """Return the configured |z| threshold, falling back to the documented default."""
    raw = os.environ.get("ANOMALY_Z_THRESHOLD", "")
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return ANOMALY_Z_THRESHOLD_DEFAULT
    return value if value > 0 else ANOMALY_Z_THRESHOLD_DEFAULT


def minimum_observations_for_threshold(threshold: float | None = None) -> int:
    """Smallest ``n`` such that ``(n-1)/sqrt(n) >= threshold``.

    Closed form: ``(n-1)/sqrt(n) = t`` solves to ``sqrt(n) = (t + sqrt(t^2+4))/2``.
    The float result is then walked to the exact integer boundary in both
    directions, so no rounding error can shift the disclosed number by one.
    """
    import math  # noqa: PLC0415

    if threshold is None:
        threshold = anomaly_z_threshold()
    if threshold <= 0:
        return 2

    root = (threshold + math.sqrt(threshold * threshold + 4.0)) / 2.0
    n = max(2, int(root * root))

    def _reaches(candidate: int) -> bool:
        return (candidate - 1) / math.sqrt(candidate) >= threshold

    while n > 2 and _reaches(n - 1):
        n -= 1
    while not _reaches(n):
        n += 1
    return n


def fetch_detector_readiness(
    project_id: str | None = None,
    evaluation_date: date | None = None,
    conn=None,
    threshold: float | None = None,
) -> list[dict]:
    """Read, per (connector, metric), whether the detector could speak at all.

    Story 53.8, AC9. Reads ``observation_count`` from ``metric_baselines`` — the
    mart that has a row for EVERY project x connector x metric x date, including
    the days nothing fires. ``anomalies_daily`` cannot answer this: it keeps only
    rows that already cleared the threshold, so below it there is no row.

    Module posture (unchanged): never raises. Any failure returns ``[]``, and an
    empty list is reported downstream as ``no_series`` — never as ``no anomaly``.
    """
    if evaluation_date is None:
        evaluation_date = date.today() - timedelta(days=1)
    minimum = minimum_observations_for_threshold(threshold)

    owns_connection = conn is None
    duck_conn = conn
    try:
        if owns_connection:
            import duckdb  # noqa: PLC0415

            db_path = os.environ.get("TOOROW_DUCKDB_PATH", "")
            if not db_path:
                logger.debug(
                    "anomaly_alerts: readiness_skipped -- TOOROW_DUCKDB_PATH not set"
                )
                return []
            duck_conn = duckdb.connect(db_path, read_only=True)

        rows_out: list[dict] = []
        for schema_prefix in _mart_prefixes_for_scan(project_id):
            params: list = [str(evaluation_date)]
            project_filter = ""
            if project_id is not None:
                project_filter = " AND project_id = ?"
                params.append(project_id)
            sql = (
                f"SELECT project_id, connector, metric, observation_count "  # noqa: S608
                f"FROM {schema_prefix}metric_baselines "
                f"WHERE date = ?{project_filter}"
            )
            try:
                rows = duck_conn.execute(sql, list(params)).fetchall()
            except Exception as exc:  # noqa: BLE001 -- an org schema may be absent
                logger.debug(
                    "anomaly_alerts: readiness_scan_skipped prefix=%s date=%s: %s",
                    schema_prefix,
                    evaluation_date,
                    exc,
                )
                continue
            for row in rows:
                row_project_id, connector, metric, observations = row
                observations = int(observations or 0)
                rows_out.append({
                    "project_id": row_project_id,
                    "connector": connector,
                    "metric": metric,
                    "observations": observations,
                    "minimum_observations": minimum,
                    "armed": observations >= minimum,
                })
        return rows_out
    except Exception as exc:
        logger.warning("anomaly_alerts: readiness_error date=%s: %s", evaluation_date, exc)
        return []
    finally:
        if owns_connection and duck_conn is not None:
            try:
                duck_conn.close()
            except Exception:  # noqa: BLE001 -- closing must never surface
                pass


def _severity(zscore: float) -> str:
    """Return severity string based on |zscore|.

    'error' for |z| >= 5.0, 'warning' for 3.0 <= |z| < 5.0.
    """
    return "error" if abs(zscore) >= 5.0 else "warning"


def _fetch_context_events_for_anomaly(
    project_id: str,
    anomaly_date: date,
    conn,
    connector: str | None = None,
    metric: str | None = None,
    record_connection=None,
) -> tuple[list[str], dict]:
    """Fetch context event labels scoped to ONE anomaly, plus the pairing basis.

    Story 53.8, CAV-15 — the OTHER half of the class repaired in
    ``core.briefing._find_context_event``. This side already joined on the exact
    anomaly date, but on ``project_id`` and ``event_date`` alone: an event logged
    for Meta Ads was listed as candidate context under a Search Console anomaly on
    the same day. Repairing the briefing side only would have reproduced exactly
    the defect this repository keeps repairing.

    The connector discriminant is expressed as ``platform IS NULL OR platform = ?``:
    an event that DECLARES another platform is out; one that declares none stays
    in, because nothing about it can be checked -- and the returned pairing
    descriptor says so, naming ``connector`` as an unscoped dimension.

    ``metric`` is the THIRD discriminant, and it was unscoped here until
    migration 322 gave ``app.context_events`` a nullable ``metric`` column
    (2026-08-30). It is expressed the same way as the connector one --
    ``metric IS NULL OR metric = ?`` -- so an event that names ANOTHER metric is
    out and one that names none stays in, because it is about every metric.

    WHAT ``metric_checked`` MEANS ON THIS SIDE, and it is not what it means on
    the briefing side. Here the discriminant runs inside the SQL, so the
    descriptor reports whether the COMPARISON WAS APPLIED -- exactly the reading
    ``platform_checked`` has carried on this function since Story 53.8. The
    briefing side receives the window in memory and judges each event, so there
    the flag reports whether the SELECTED event named the dimension. Two honest
    answers to two different questions; folding them into one would make one of
    the two lie.

    WHERE IT READS (AI-344, context-hub.md amendment of 2026-09-01). *conn* is
    the warehouse connection; when it carries ``mirror.context_events`` the walk
    runs there, as it always did. When it does not -- the deployment keeps no
    mirror, which is every ``bigquery`` deployment -- the walk runs on the RECORD,
    ``app.context_events``, through the Postgres connection *record_connection*
    returns (a zero-argument callable, so the evaluator opens one connection for
    the whole scan and only if a walk needs it). The two discriminants are
    applied in memory there, on columns the record always carries, and the
    descriptor reports them exactly as it does for the SQL.

    Never raises. When NEITHER store can serve, the anomaly keeps its verdict and
    the descriptor carries ``unavailable: {reason, repair}`` beside an empty
    label list -- so the alert says the walk did not run, never "no cause".
    """
    from core.briefing import context_pairing_descriptor  # noqa: PLC0415
    from core.context_events import mirror_has_context_events_table  # noqa: PLC0415

    claim_date = str(anomaly_date)
    if conn is not None and mirror_has_context_events_table(conn):
        served = _candidate_causes_from_mirror(
            conn, project_id, claim_date, connector=connector, metric=metric
        )
        if served is not None:
            return served

    try:
        return _candidate_causes_from_record(
            record_connection, project_id, claim_date, connector=connector, metric=metric
        )
    except Exception as exc:  # noqa: BLE001 -- said in the descriptor, never raised
        logger.warning(
            "anomaly_alerts: context_events_unavailable project=%s date=%s: %s: %s",
            project_id,
            claim_date,
            type(exc).__name__,
            exc,
        )
        pairing = context_pairing_descriptor(
            platform_checked=False, metric_checked=False, claim_date=claim_date
        )
        pairing["unavailable"] = _unavailable_payload(exc)
        return [], pairing


def _unavailable_payload(exc: Exception) -> dict:
    """The ``{reason, repair}`` a reader can act on, from whatever stopped the walk.

    The words are the ones `core.context_events` already declares for the same
    state, so the fetch and this walk say the same thing about the same store.
    """
    from core.context_events import (  # noqa: PLC0415
        _RECORD_UNREACHABLE_REASON,
        _RECORD_UNREACHABLE_REPAIR,
        ContextEventsUnavailable,
    )

    if isinstance(exc, ContextEventsUnavailable):
        return exc.payload
    return ContextEventsUnavailable(_RECORD_UNREACHABLE_REASON, _RECORD_UNREACHABLE_REPAIR).payload


def _candidate_causes_from_record(
    record_connection, project_id: str, claim_date: str, *, connector: str | None,
    metric: str | None,
) -> tuple[list[str], dict]:
    """The walk on ``app.context_events`` -- same discriminants, applied in memory.

    Reuses the record read `fetch_context_events` shares, so the retired
    semantics and the column list are the ones the other proactive walk uses.
    The record always carries ``platform`` and ``metric``, so a discriminant is
    applied whenever the claim names one -- and the descriptor says exactly that.
    """
    from core.briefing import context_pairing_descriptor  # noqa: PLC0415
    from core.context_events import (  # noqa: PLC0415
        ContextEventsUnavailable,
        _fetch_context_events_from_postgres,
    )

    if record_connection is None:
        raise ContextEventsUnavailable(
            "The candidate causes were not read: the warehouse carries no events "
            "mirror and this walk was given no connection to the events store.",
            "Hand the evaluator the connection the nightly scan already holds, or "
            "point TOOROW_DUCKDB_PATH at a synced mirror.",
        )
    events = _fetch_context_events_from_postgres(
        record_connection(), project_id, claim_date, claim_date, include_retired=False
    )
    labels = [
        e["label"]
        for e in events
        if e.get("label")
        and (not connector or e.get("platform") in (None, connector))
        and (not metric or e.get("metric") in (None, metric))
    ]
    return labels, context_pairing_descriptor(
        platform_checked=bool(connector),
        metric_checked=bool(metric),
        claim_date=claim_date,
    )


def _candidate_causes_from_mirror(
    conn, project_id: str, claim_date: str, *, connector: str | None, metric: str | None
) -> tuple[list[str], dict] | None:
    """The walk on ``mirror.context_events``, or ``None`` when this mirror cannot serve.

    An absent ``platform`` column (pre-055 mirror) or an absent ``metric`` column
    (pre-322 mirror) degrades to a narrower query -- and the descriptor then says
    which dimension went unchecked rather than presenting the result as scoped.
    A failure of the LAST-RESORT query is not an empty day: it is ``None``, and
    the caller turns to the record.
    """
    from core.briefing import context_pairing_descriptor  # noqa: PLC0415
    from core.context_events import (  # noqa: PLC0415
        mirror_has_metric_column,
        mirror_live_events_clause,
    )

    # A RETIRED event is not a candidate cause. This is the second reader of
    # `mirror.context_events` (migration 286) and the one that names a cause out
    # loud, so honouring the withdrawal in `fetch_context_events` alone would
    # leave the product still citing what a human withdrew. The clause is empty on
    # a mirror that predates 286, which is why it is resolved once here rather
    # than pasted into both queries below.
    live_only = mirror_live_events_clause(conn)
    # The metric discriminant is decided BEFORE the read, never by catching the
    # BinderException a pre-322 mirror would raise: an exception-driven guard
    # falls back to a query with no metric clause at all, on precisely the
    # mirrors that cannot apply one, and the descriptor would still have claimed
    # the dimension was checked.
    metric_scoped = bool(metric) and mirror_has_metric_column(conn)
    metric_clause = " AND (metric IS NULL OR metric = ?)" if metric_scoped else ""
    metric_params = [metric] if metric_scoped else []
    scoped_sql = (
        "SELECT label FROM mirror.context_events "
        "WHERE project_id = ? AND event_date = ? "
        f"AND (platform IS NULL OR platform = ?){metric_clause}{live_only}"  # noqa: S608
    )
    if connector:
        try:
            rows = conn.execute(
                scoped_sql, [project_id, claim_date, connector, *metric_params]
            ).fetchall()
            return (
                [r[0] for r in rows if r[0]],
                context_pairing_descriptor(
                    platform_checked=True,
                    metric_checked=metric_scoped,
                    claim_date=claim_date,
                ),
            )
        except Exception as exc:
            # A pre-055 mirror has no `platform` column (BinderException). Fall
            # back to the date-only join, and SAY that the connector dimension
            # could not be checked rather than presenting the result as scoped.
            logger.debug(
                "anomaly_alerts: context_events_platform_scope_unavailable"
                " project=%s date=%s connector=%s: %s",
                project_id,
                claim_date,
                connector,
                exc,
            )

    # No connector on the claim, or the platform join failed. The metric clause
    # is kept: losing a dimension that IS comparable because another one is not
    # would widen the pairing for no reason.
    unscoped = context_pairing_descriptor(
        platform_checked=False, metric_checked=metric_scoped, claim_date=claim_date
    )
    try:
        rows = conn.execute(
            "SELECT label FROM mirror.context_events "
            f"WHERE project_id = ? AND event_date = ?{metric_clause}{live_only}",  # noqa: S608
            [project_id, claim_date, *metric_params],
        ).fetchall()
        return [r[0] for r in rows if r[0]], unscoped
    except Exception as exc:
        logger.debug(
            "anomaly_alerts: context_events_mirror_walk_failed project=%s date=%s: %s",
            project_id,
            claim_date,
            exc,
        )
        return None

def pairing_walk_descriptor(pairing: dict | None, selected_count: int) -> dict | None:
    """Widen a Story 53.8 pairing into the descriptor the text channel needs.

    Story 54.2 (AC5/AC6). Two counts are DELIBERATELY absent, and their absence
    is the honest report: this side filters inside the SQL
    (``platform IS NULL OR platform = ?``), so an event that does not qualify is
    never fetched -- never seen, never judged, never dropped. Publishing
    ``rejected_count: 0`` would claim an examination that did not take place, and
    publishing a total would claim knowledge of rows the query excluded. The
    briefing side, which receives the whole window in memory and judges each
    event, is the half that can enumerate branches (`briefing.context_event_walk`).
    """
    if not pairing:
        return None
    from core.briefing import CONTEXT_PAIRING_MODE  # noqa: PLC0415

    widened = dict(pairing)
    widened.update(
        {
            "mode": CONTEXT_PAIRING_MODE,
            # No graph is walked on either side of this class. Stated rather than
            # omitted: "not said" and "zero" read the same to a surface.
            "graph_hop_depth": 0,
            "semantic_recall": False,
            "not_reached_enumerated": False,
            "selected_count": int(selected_count),
        }
    )
    return widened


def format_anomaly_line(anomaly: dict) -> str:
    """Format an anomaly dict as a one-line French summary for the LLM channel.

    Per AC6 spec:
      ⚠️ Anomalie : {metric} (z=+{z:.1f}) le {date} — Contexte : {labels}
      ⚠️ Anomalie : {metric} (z=+{z:.1f}) le {date} — Contexte manquant.

    AD-9 hard rule: MUST NOT use 'cause', 'caused by', 'due to', 'because of',
    'as a result of', 'en raison de'. Unit-tested in test_anomaly_alerts.py.
    Uses ASCII-safe formatting (AD-2: no em-dash in log strings).

    Story 54.2 (AC6/AC7): when the anomaly carries its pairing, the BASIS of the
    attachment travels with the labels, as cited data built by
    ``core.candidate_emission`` from constants and counts only. A label sits next
    to a claim; without the basis, "context" reads as "explanation", and that is
    the causal step nothing here is allowed to take.
    """
    metric = (anomaly.get("metric") or "").lower()
    zscore = float(anomaly.get("zscore", 0))
    z_sign = "+" if zscore >= 0 else ""
    z_str = f"z={z_sign}{zscore:.1f}"
    anomaly_date = anomaly.get("window_date") or anomaly.get("date", "")
    context_labels: list[str] = anomaly.get("context_events", [])
    unavailable = _context_unavailable(anomaly)

    if context_labels:
        labels_str = ", ".join(f'"{lbl}"' for lbl in context_labels)
        context_part = phrase("anomaly_context_labels", labels=labels_str)
    elif unavailable:
        # AI-344: the walk did not run. "Contexte manquant." would claim it did
        # and found nothing; the verdict stands, the context is UNKNOWN.
        context_part = phrase("anomaly_context_unavailable", repair=unavailable["repair"])
    else:
        context_part = phrase("anomaly_context_missing")

    line = phrase(
        "anomaly_line",
        metric=metric,
        zscore=z_str,
        date=anomaly_date,
        context=context_part,
    )

    descriptor = pairing_walk_descriptor(
        anomaly.get("context_pairing"), len(context_labels)
    )
    if descriptor:
        from core import candidate_emission  # noqa: PLC0415
        from core.briefing import CONTEXT_PAIRING_SUBJECT  # noqa: PLC0415

        cited = candidate_emission.cited_fate_line(
            subject=CONTEXT_PAIRING_SUBJECT, descriptor=descriptor
        )
        # CAV-18 treated as a CLASS, not as an instance: `llm_commentary_guidelines`
        # is the operator field `cards` and `reports` already frame. If it ever
        # reaches this channel it arrives behind the SAME imported frame, next to
        # the cited line rather than indistinguishable from it.
        line = candidate_emission.compose_text_channel(
            f"{line} {cited}".strip(), anomaly.get("llm_commentary_guidelines")
        )
    return line


def _context_unavailable(anomaly: dict) -> dict | None:
    """The ``{reason, repair}`` the pairing carries when the walk could not run."""
    pairing = anomaly.get("context_pairing")
    if isinstance(pairing, dict) and isinstance(pairing.get("unavailable"), dict):
        return pairing["unavailable"]
    return None


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
        "context_unavailable": {"reason", "repair"}   -- only when the walk did not run
      }

    AD-9: no causal language in message. Only 'Contexte :' or 'Contexte manquant.'
    AI-344: ``context_missing`` is True only when the walk RAN and found nothing;
    a walk that could not run says so in ``context_unavailable`` and leaves
    ``context_missing`` False -- "not read" must never render as "none".
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
    unavailable = _context_unavailable(anomaly)

    if context_labels:
        labels_str = ", ".join(context_labels)
        context_part = phrase("anomaly_context_labels_period", labels=labels_str)
    elif unavailable:
        context_part = phrase("anomaly_context_unavailable", repair=unavailable["repair"])
    else:
        context_part = phrase("anomaly_context_missing")

    message = phrase(
        "anomaly_widget_message",
        metric=metric_display,
        level=phrase("anomaly_level_high" if zscore > 0 else "anomaly_level_low"),
        zscore=z_str,
        context=context_part,
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
        "context_missing": len(context_labels) == 0 and unavailable is None,
        **({"context_unavailable": unavailable} if unavailable else {}),
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
    if os.environ.get("ANOMALY_ALERTS_ENABLED", "true").lower() != "true":
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

        # Query anomalies_daily for evaluation_date, ranked by |zscore| DESC.
        #
        # AI-344: the candidate-cause walk reads the RECORD when the warehouse
        # carries no events mirror. One Postgres connection for the whole scan,
        # opened only if a walk asks for it; a background path with no human
        # caller, so it is the unisolated seam and says why.
        record_stack = contextlib.ExitStack()
        record_holder: list = []

        def _record_connection():
            if not record_holder:
                from core.db import background_connection  # noqa: PLC0415

                record_holder.append(
                    record_stack.enter_context(
                        # The reason, as a key: the nightly scan runs for every
                        # project and has no human to arm the floor for.
                        background_connection(
                            "anomaly_evaluator.candidate_causes_nightly_scan_no_human_caller"
                        )
                    )
                )
            return record_holder[0]

        with duckdb.connect(db_path, read_only=True) as duck_conn, record_stack:
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
                    # Story 53.8 (CAV-15): scoped to this anomaly's connector and,
                    # since migration 322, to its metric -- the pairing basis
                    # travels with the labels.
                    context_labels, context_pairing = _fetch_context_events_for_anomaly(
                        row_project_id,
                        evaluation_date,
                        duck_conn,
                        connector=connector,
                        metric=metric,
                        record_connection=_record_connection,
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
                        "context_pairing": context_pairing,
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
                    "context_pairing": anomaly.get("context_pairing"),
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
            window_date = row.get("window_date")
            result.append({
                "code": "anomaly",
                "severity": severity,
                "metric": row.get("metric", ""),
                # ONE ALERT LANGUAGE PER PRODUCT (CLAUDE.md, « Un seul langage
                # d'alerte par produit »). This line used to be the only English
                # sentence in a module whose other two alerts were French -- two
                # languages for one signal, in one file. It resolves through the
                # SAME catalogue as `format_anomaly_line` and
                # `_build_widget_alert`, so the three can no longer disagree.
                "message": phrase(
                    "anomaly_firing_message",
                    metric=row.get("metric", "?"),
                    observed=f"{obs:.2f}",
                    baseline=f"{exp:.2f}",
                ),
                "firing_id": row.get("firing_id"),
                "observed_value": obs,
                "expected_value": exp,
                # Story 53.8 (CAV-15): the claim must carry its OWN date, or no
                # context event can ever be scoped to it. The column was already
                # SELECTed and then dropped on the floor.
                "window_date": (
                    window_date.isoformat()
                    if hasattr(window_date, "isoformat")
                    else (str(window_date) if window_date else None)
                ),
                # AC6: [] is the DESIGNED state of an anomaly firing (derived
                # signal, no raw pull). Stated, so the consumer does not have to
                # guess whether the key was simply forgotten.
                "pull_ids": [],
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
