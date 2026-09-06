"""toorow -- Data Quality monitors v1 (Story 8.6, Epic 8).

Zero-config monitors, evaluated per enabled datastream.
DQ pattern: warning-severity firings, never halt load in v1.

THE VOCABULARY LIVES IN `core.dq_monitor_registry` SINCE STORY 59.5, and no count
is written down here any more: this file used to say "five", dispatch seven,
publish eight and share its `dq_timeliness` type with a ninth check nothing named.
The registry answers, per monitor, its key, its `alert_type`, its English label,
its scope (`datastream` or `project`), whether it is dispatched and whether a
policy may publish it. `CHECK_PROFILES` below is the registry's dispatched half.

Monitors
--------
(a) volume        -- rolling median of per-day row counts (last 30 days ledger).
                     Flags yesterday when |count - median| > 2.6 * robust_sigma
                     and >= 10 prior data points exist.
                     Robust sigma = MAD * 1.4826 (median absolute deviation scale).

(b) timeliness    -- yesterday's extract (status ok|partial) must exist by
                     DQ_TIMELINESS_DUE_HOUR (default 9, project timezone via
                     SCHEDULER_TIMEZONE). Flags if missing when now > due time.

(c) duplication   -- count duplicate full rows in the raw table for yesterday.
                     Raw table resolved from verification._get_raw_table_name(module_name).
                     Flags when duplicate_count > 0.

(d) schema        -- current raw table column list vs the frozen baseline of the
                     Datastream's published `schema` DQ Monitor version.
                     First run: seeds baseline, no firing.
                     On drift: fires once, then AUTO-RESETS baseline.

(e) date_format   -- Story 8.10 / R3: for streams whose datastream config declares
                     date_format, verify that yesterday's raw rows in raw_generic_daily
                     have dates parseable as canonical ISO 'YYYY-MM-DD'.
                     Uses DuckDB try_strptime or regex pattern YYYY-MM-DD.
                     Fires when non-ISO rows are found: alert_type='dq_date_format'.
                     Applies only to the 'generic' module (other modules bypass gracefully).

(g) null_rate    -- Story 59.3: the MISSING rate -- NULL or blank, one predicate,
                    2026-08-12 -- of every field the active mapping
                    version declares in its `grain`, over one day, read through
                    `collected_mapped_reader` (both dialects) and never through a
                    hand-rolled DuckDB connection. Fires per field above the
                    threshold: alert_type='dq_null_rate', and it is the first
                    monitor that also opens an `app.dq_issues` naming the
                    Datastream and the run. See `core.dq_null_rate`.

(h) zero_rows    -- Story 59.4: a window whose verdict is `empty`
                    (`verification.py:576-578`) on a Datastream that HAS been
                    producing -- three days with status `ok` among the last 31 of
                    the ledger, the same window the volume monitor reads. Below
                    that evidence it answers `not_applicable` naming the number of
                    days found, never a pass. Fires per WINDOW and not per day:
                    alert_type='dq_zero_rows'. See `core.dq_zero_rows`.

`window_offset_days` IS READ HERE SINCE STORY 59.4, and nothing in this file read
it before. The dispatch sets `end_date = yesterday - (offset - 1)`
(`scheduler.py:1335-1340`), so a Datastream at offset 3 has no ledger row for
yesterday BY CONSTRUCTION, and a check that recomputes yesterday itself judges a
day the product deliberately never asked for.

THE REPAIR IS PROSPECTIVE, AND THE MEASUREMENT SAYS SO. `window_offset_days` is 1
on 1421 rows of 1421 (disposable base, role `postgres`, 2026-08-08) and 47 of 47
(preprod), so no firing in either base was caused by this defect -- the 2421
`dq_timeliness` rows of the disposable base all carry offset 1, ONE `window_date`
and the status `never_fetched`: the offset explains NONE of them. What the guard protects is the
first Datastream someone moves above 1, which `SchedulePanel.tsx:203` allows in a
click. `effective_window_end` is the one rule both `_check_timeliness` and
`_check_zero_rows` read.

(i) arrival_timeliness -- the timeliness of a feed that ARRIVES instead of being
                    pulled. Reached THROUGH `_check_timeliness`, never dispatched on
                    its own, and it fires under the `dq_timeliness` type of its
                    sibling because it answers the same operator question. Its
                    firing carries `monitor_kind='arrival'` -- the one value that
                    tells the two apart in `app.alert_firings`.

(f) geography    -- unresolved country vocabulary, evaluated per PROJECT and not
                    per Datastream, so it is publishable and displayable but NOT in
                    the dispatch table. It fires `dq_geography` from
                    `geographic_conformance.emit_country_dq_firing`.

(j) unresolved_values -- AI-326: the values a mapped Datastream carries that the
                    reading cannot name, for EVERY armed dimension and not only for
                    the country one. Datastream-scoped and dispatched, which is
                    where it differs from `geography`. It fires on the DELTA -- a
                    value not in the previous evaluation's unresolved set -- and the
                    first evaluation of a (Datastream, dimension) seeds its baseline
                    and fires nothing, exactly as `schema` seeds its column list.
                    One firing per `(datastream, dimension, reason)`, never one per
                    value. See `core.unresolved_values_monitor` and
                    `docs/product-architecture/unresolved-values.md`.

All findings -> infra_alerts.write_infra_firing(alert_type=<one of
`dq_monitor_registry.FIRING_ALERT_TYPES`>, ...). Eight types, nine call sites.

Isolation: per-stream try/except -- one failing stream never blocks others.
Never raises at module level.

Public API
----------
run_dq_monitors(project_id=None) -> dict
    Evaluate every DISPATCHED monitor -- `dq_monitor_registry.DISPATCHED_KEYS` --
    for each enabled datastream (or a single project), then the project-scoped
    ones. Returns a summary dict keyed on those same profile names plus
    {evaluated, total_issues, errors}.

Environment variables
---------------------
DQ_MONITORS_ENABLED      default "true"  -- master switch (false = skip in scheduler)
DQ_TIMELINESS_DUE_HOUR   default "9"     -- hour (0-23) in SCHEDULER_TIMEZONE
SCHEDULER_TIMEZONE        default "Europe/Paris"
TOOROW_DUCKDB_PATH     -- path to DuckDB warehouse file

AD-2: no module-specific strings in core logic; module_name passed as data.
AD-5: project_id scoping enforced throughout.
ASCII-only log strings (AI-03).
"""

from __future__ import annotations

import logging
import os
import statistics
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Mapping, Sequence

from core import dq_monitor_registry

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration helpers
# ---------------------------------------------------------------------------


def _dq_enabled() -> bool:
    return os.environ.get("DQ_MONITORS_ENABLED", "true").lower() != "false"


def _due_hour() -> int:
    try:
        return int(os.environ.get("DQ_TIMELINESS_DUE_HOUR", "9"))
    except (ValueError, TypeError):
        return 9


def _scheduler_tz() -> str:
    return os.environ.get("SCHEDULER_TIMEZONE", "Europe/Paris")


def _duckdb_path() -> str:
    return os.environ.get("TOOROW_DUCKDB_PATH", "")


def window_offset_days(datastream: Mapping | int | None) -> int:
    """The extraction offset of a Datastream, clamped to the migration's floor.

    Accepts the row or the value alone: `_check_timeliness` has a flat signature
    and is called positionally by its tests, so it carries the number rather than
    the row it came from.

    `window_offset_days` is bounded `[1, 90]` by
    `206_datastream_window_offset.sql:12-13`; absent, NULL or unreadable means 1,
    which is what 1421 rows of 1421 hold today.
    """
    raw: Any = datastream
    if isinstance(datastream, Mapping):
        raw = datastream.get("window_offset_days")
    try:
        offset = int(raw or 1)
    except (TypeError, ValueError):
        offset = 1
    return offset if offset >= 1 else 1


def effective_window_end(datastream: Mapping | int | None, today: date) -> date:
    """The last day this Datastream can possibly have data for, as of *today*.

    THE HELPER STORY 59.4 EXISTS TO SHARE, and the repair of a class defect rather
    than of one check. The dispatch never asks a source for a day inside its
    extraction offset: `scheduler.py:1335-1340` fixes
    `end_date = yesterday - (window_offset_days - 1)`, which is `today - offset`.
    A monitor that recomputes `yesterday` on its own therefore measures a day the
    product deliberately never fetched, and reports "no valid extraction" about a
    Datastream behaving exactly as configured -- the firing on a legitimately empty
    window that `epic-59:121-123` forbids.

    PROSPECTIVE, AND THE MEASUREMENT SAYS SO. `window_offset_days` is 1 on every
    row of both bases (1421 of 1421 disposable, role `postgres`; 47 of 47 preprod),
    so NO existing firing has this cause -- the 2421 `dq_timeliness` rows of the
    disposable base all carry offset 1, one `window_date` and the status
    `never_fetched`. What this guards is the first Datastream someone moves above
    1, which `SchedulePanel.tsx:203` allows in a click.

    Read by BOTH `_check_timeliness` and `_check_zero_rows`, and by the nightly
    sweep that hands them their date. One arithmetic, one vocabulary: a second
    lookback rule in this file would be a second doctrine thirty lines from the
    first.
    """
    return today - timedelta(days=window_offset_days(datastream))


# ---------------------------------------------------------------------------
# Rolling stats for volume monitor
# ---------------------------------------------------------------------------


#: The shape of the band, and the ONLY thing about `volume` that can be frozen at
#: publication: the median it compares to is recomputed every night, by design.
#: `app.dq_monitor_versions` refuses a `volume` version with an empty baseline
#: (`controls_quality.DQ_CHECKS_REQUIRING_BASELINE`), and these three numbers are
#: what "normal" is defined as -- the lookback, the deviation scale and the
#: multiplier beyond which a day is called anomalous.
#: The number of prior days the band needs, and `_volume_anomaly` enforces it.
VOLUME_MIN_PRIOR_POINTS = 10
VOLUME_LOOKBACK_DAYS = 30
VOLUME_SIGMA_SCALE = 1.4826
VOLUME_SIGMA_MULTIPLIER = 2.6


def volume_baseline() -> dict[str, float]:
    """The frozen definition of a normal volume, as published on the version."""
    return {
        "lookback_days": float(VOLUME_LOOKBACK_DAYS + 1),
        "min_prior_points": float(VOLUME_MIN_PRIOR_POINTS),
        "sigma_scale": VOLUME_SIGMA_SCALE,
        "sigma_multiplier": VOLUME_SIGMA_MULTIPLIER,
    }


def _rolling_median_and_sigma(counts: list[float]) -> tuple[float, float]:
    """Compute (median, robust_sigma) from a list of count values.

    robust_sigma = MAD * 1.4826  (consistent estimator of std dev under normality).
    Returns (0.0, 0.0) for empty or single-element lists.
    """
    if len(counts) < 2:
        return 0.0, 0.0
    med = statistics.median(counts)
    mad = statistics.median([abs(x - med) for x in counts])
    sigma = mad * VOLUME_SIGMA_SCALE
    return med, sigma


def _volume_anomaly(prior_counts: list[float], yesterday_count: float) -> bool:
    """Return True when yesterday_count is anomalous given prior history.

    Requires >= 10 prior data points.
    Threshold: |x - median| > 2.6 * robust_sigma.
    """
    if len(prior_counts) < VOLUME_MIN_PRIOR_POINTS:
        return False
    med, sigma = _rolling_median_and_sigma(prior_counts)
    if sigma == 0.0:
        # All prior counts identical -- any deviation is anomalous.
        return yesterday_count != med
    return abs(yesterday_count - med) > VOLUME_SIGMA_MULTIPLIER * sigma


# ---------------------------------------------------------------------------
# Fetch enabled datastreams
# ---------------------------------------------------------------------------


def _fetch_enabled_datastreams(conn, project_id: str | None) -> list[dict]:
    """Fetch enabled datastreams from Postgres.

    Returns list of {id, project_id, org_id, module_name, name, report_profile_id,
    config, window_offset_days}. Empty on error. `config` is included so monitor (e)
    date_format can inspect declared date_format.

    `window_offset_days` joined the projection in story 59.4: without it every check
    measures the deployment's `yesterday` while the dispatch measures
    `yesterday - (offset - 1)`, and the gap is a nightly false firing. See
    :func:`effective_window_end`.

    `org_id` and `report_profile_id` joined the projection in story 59.3 and both
    are load-bearing there: `app.dq_monitors.org_id` is NOT NULL and references
    `app.organizations`, and the report profile is what says WHICH relation of the
    connector this flux's rows land in. Reading them again per Datastream would
    have been a second query per stream per night for two columns already in
    reach.
    """
    try:
        with conn.cursor() as cur:
            if project_id is not None:
                cur.execute(
                    """
                    SELECT ds.id, ds.project_id, ds.org_id, ds.module_name, ds.name,
                           ds.report_profile_id, ds.config, ds.window_offset_days
                    FROM app.datastreams ds
                    WHERE ds.enabled = TRUE AND ds.project_id = %s
                    ORDER BY ds.project_id, ds.id
                    """,
                    (project_id,),
                )
            else:
                cur.execute(
                    """
                    SELECT ds.id, ds.project_id, ds.org_id, ds.module_name, ds.name,
                           ds.report_profile_id, ds.config, ds.window_offset_days
                    FROM app.datastreams ds
                    WHERE ds.enabled = TRUE
                    ORDER BY ds.project_id, ds.id
                    """
                )
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]
    except Exception as exc:
        logger.warning("dq_monitors: fetch_datastreams_failed: %s", exc)
        return []


# ---------------------------------------------------------------------------
# (a) Volume monitor
# ---------------------------------------------------------------------------


#: Why the volume band could not judge a day. STORY 59.5, ARBITRAGE 2, AND THE
#: MEASUREMENT BEHIND IT: `row_count` is published only for a collection window
#: exactly one day wide (`extract_ledger.py:448-458`), and it is NULL on 141 pull
#: jobs of 141 on the disposable base and 6 of 6 on preprod -- the ten one-day
#: windows included. This check therefore returns `False` for every Datastream in
#: both bases and `app.alert_firings` holds no `dq_volume` row at all. Bridging it
#: as a PASS would manufacture a green for 1725 flux; it is bridged as
#: `not_applicable` naming its reason, which is what 59.4 did for missing history.
#:
#: The word itself is the LEDGER's, not a second one: `extract_ledger` already
#: answers `row_count_reason = "measured_per_window"` for exactly this absence,
#: and the evaluation quotes the reason the reader would have seen.
VOLUME_MEASURED_PER_WINDOW = "measured_per_window"
VOLUME_NO_COUNT_FOR_WINDOW = "no_count_for_window"
VOLUME_NOT_ENOUGH_HISTORY = "not_enough_history"
#: The Datastream has no extract ledger AT ALL over the window -- it has never
#: collected, or every one of its runs is younger than the lookback. It is the
#: only volume absence with no ledger row to quote, which is why its word is this
#: module's and not `extract_ledger`'s: there is nothing there to have said one.
VOLUME_NO_LEDGER = "no_ledger_read"

_VOLUME_MESSAGES = {
    VOLUME_MEASURED_PER_WINDOW: (
        "The ledger publishes a row count only for a collection window exactly one day "
        "wide, and none of this Datastream's windows is. A volume band cannot be built "
        "from counts that were never published, and a missing count is not a normal day."
    ),
    VOLUME_NO_COUNT_FOR_WINDOW: (
        "The judged day carries no row count of its own, so there is nothing to compare "
        "against the band. An absent measurement is never a measurement within range."
    ),
    VOLUME_NOT_ENOUGH_HISTORY: (
        "Fewer than ten earlier days carry a row count, so the rolling median and its "
        "robust deviation would describe noise rather than this source's normal volume."
    ),
    VOLUME_NO_LEDGER: (
        "This Datastream published no collection window at all over the thirty-one days "
        "the band is built from, so there is no volume to judge. Never collecting is not "
        "collecting a normal amount."
    ),
}


def volume_message_for(reason: str | None) -> str | None:
    """The sentence of a volume reason, or `None`."""
    if not reason:
        return None
    return _VOLUME_MESSAGES.get(reason)


def _check_volume(
    ds_id: str,
    project_id: str,
    module_name: str,
    ds_name: str,
    conn,
    yesterday: date,
    ds: Mapping | None = None,
) -> CheckVerdict:
    """Evaluate volume monitor for one datastream.

    Reads 31 days of ledger (yesterday + 30 prior days).
    Uses row_count from pull_verifications via the ledger query.
    Returns True when a firing was issued.

    STORY 59.5 -- IT NOW WRITES ITS VERDICT DOWN. The boolean it answers has never
    been able to tell "this volume is normal" from "no volume was ever published
    for this Datastream", and the second is the case on 100% of the rows of both
    bases. Every outcome now writes one `app.dq_evaluations` row through
    :func:`_volume_recorded`, exactly as `null_rate` and `zero_rows` do, and the
    only outcome a person will see today is `not_applicable` naming
    `measured_per_window`. *ds* carries the `org_id` a governed monitor needs;
    without it the check behaves exactly as it did before, and says so.
    """
    from core.extract_ledger import get_extract_ledger  # noqa: PLC0415

    date_from = (yesterday - timedelta(days=VOLUME_LOOKBACK_DAYS)).isoformat()
    date_to = yesterday.isoformat()

    entries = get_extract_ledger(ds_id, date_from, date_to, conn)
    if not entries:
        # No ledger read at all: nothing was measured, so no governed object is
        # derived. A monitor for a Datastream with no ledger asserts nothing.
        #
        # AND IT ANSWERS `not_applicable`, NOT `False`. This line returned a bare
        # boolean until 2026-09-01 while the signature above and the four other
        # exits said `CheckVerdict`, and the sweep counts what it is handed:
        # `evaluate_profile` reads `getattr(verdict, "status", STATUS_EVALUATED)`,
        # so a Datastream that has NEVER COLLECTED was counted `passed` -- the
        # dashboard going green over nothing that `_volume_recorded` and
        # `ck_dq_evaluations_empty_is_not_a_pass` both exist to abolish. No row is
        # written here, deliberately: there is no governed monitor to derive from
        # a ledger that holds nothing. Only the answer changes.
        return CheckVerdict(
            False,
            STATUS_NOT_APPLICABLE,
            {
                "datastream_id": ds_id,
                "project_id": project_id,
                "window_date": yesterday.isoformat(),
                "reason": VOLUME_NO_LEDGER,
                "message": _VOLUME_MESSAGES[VOLUME_NO_LEDGER],
            },
        )

    # Only count days with a real row_count (status ok/partial/empty counts;
    # never_fetched/running/failed do not contribute a row_count).
    data_points: list[tuple[str, float]] = []
    for entry in entries:
        rc = entry.get("row_count")
        if rc is not None:
            data_points.append((entry["date"], float(rc)))

    yesterday_str = yesterday.isoformat()
    judged = next((e for e in entries if str(e.get("date")) == yesterday_str), None)
    observed: dict[str, Any] = {
        "window_date": yesterday_str,
        "data_points": len(data_points),
        "ledger_days": len(entries),
    }
    monitor = _volume_monitor(ds, ds_id, project_id, ds_name, entries)
    execution_id = (judged or {}).get("execution_id")

    if not data_points:
        return _volume_recorded(
            monitor,
            ds_id,
            project_id,
            yesterday,
            execution_id,
            "not_applicable",
            {**observed, "reason": VOLUME_MEASURED_PER_WINDOW},
        )

    # Yesterday is the last data point (date_to).
    # Prior points = all data points EXCEPT yesterday.
    prior: list[float] = [rc for d, rc in data_points if d != yesterday_str]
    yesterday_pts: list[float] = [rc for d, rc in data_points if d == yesterday_str]

    if not yesterday_pts:
        # No data point for yesterday => nothing to flag.
        return _volume_recorded(
            monitor,
            ds_id,
            project_id,
            yesterday,
            execution_id,
            "not_applicable",
            {**observed, "reason": VOLUME_NO_COUNT_FOR_WINDOW},
        )

    yesterday_count = yesterday_pts[0]
    observed["row_count"] = yesterday_count
    observed["prior_n"] = len(prior)

    if len(prior) < VOLUME_MIN_PRIOR_POINTS:
        # The band itself refuses below ten points (`_volume_anomaly`), and until
        # story 59.5 that refusal was indistinguishable from "the volume is fine".
        return _volume_recorded(
            monitor,
            ds_id,
            project_id,
            yesterday,
            execution_id,
            "not_applicable",
            {
                **observed,
                "reason": VOLUME_NOT_ENOUGH_HISTORY,
                "min_prior_points": VOLUME_MIN_PRIOR_POINTS,
            },
        )

    if not _volume_anomaly(prior, yesterday_count):
        med, sigma = _rolling_median_and_sigma(prior)
        return _volume_recorded(
            monitor,
            ds_id,
            project_id,
            yesterday,
            execution_id,
            "pass",
            {**observed, "prior_median": med, "robust_sigma": sigma},
        )

    # Fire DQ volume alert.
    med, sigma = _rolling_median_and_sigma(prior)
    from core import infra_alerts  # noqa: PLC0415

    infra_alerts.write_infra_firing(
        alert_type="dq_volume",
        project_id=project_id,
        metric="row_count",
        severity="warning",
        message=(
            f"Volume anomaly on datastream '{ds_name}' "
            f"on {yesterday_str}: {int(yesterday_count)} rows "
            f"(median={med:.0f}, sigma={sigma:.1f})"
        ),
        metadata={
            "datastream_id": ds_id,
            "datastream_name": ds_name,
            "module_name": module_name,
            "window_date": yesterday_str,
            "yesterday_count": yesterday_count,
            "prior_median": med,
            "robust_sigma": sigma,
            "prior_n": len(prior),
        },
    )
    logger.info(
        "dq_monitors: volume_firing ds=%s date=%s count=%.0f median=%.0f sigma=%.1f",
        ds_id,
        yesterday_str,
        yesterday_count,
        med,
        sigma,
    )
    # RETURNED, not discarded: `_volume_recorded` answers the verdict it just
    # wrote, and the four quiet exits above already return it. This one built the
    # same verdict, threw it away and answered a bare `True`, so the firing
    # reached the sweep without the `detail` every other outcome carries -- and
    # `evaluate_profile` fills migration 222's evaluation half from exactly that
    # detail, which left the FAILING window as the only one with no run named.
    return _volume_recorded(
        monitor,
        ds_id,
        project_id,
        yesterday,
        execution_id,
        "fail",
        {**observed, "prior_median": med, "robust_sigma": sigma},
        fired=True,
    )


def _volume_monitor(
    ds: Mapping | None,
    ds_id: str,
    project_id: str,
    ds_name: str,
    entries: Sequence[Mapping[str, Any]],
) -> dict[str, str] | None:
    """The governed `volume` monitor of a Datastream that CAN be judged, or None.

    Bounded exactly as `zero_rows` bounds its own: a Datastream the ledger shows
    has completed at least one collection. Deriving one for every enabled stream
    would fill the registry with objects that assert nothing.
    """
    from core.dq_zero_rows import COLLECTED_STATUSES  # noqa: PLC0415

    if not isinstance(ds, Mapping):
        # Called through a path that carries no row -- the flat signature its
        # tests use. Nothing is claimed, and nothing is written.
        return None
    if not any(str(entry.get("status") or "") in COLLECTED_STATUSES for entry in entries):
        return None
    from core import dq_monitor_bridge  # noqa: PLC0415

    return dq_monitor_bridge.derive_monitor(
        "volume",
        org_id=str(ds.get("org_id") or ""),
        project_id=project_id,
        datastream_id=ds_id,
        datastream_name=ds_name or ds_id,
        parameters={"thresholds": {"volume": float(VOLUME_MIN_PRIOR_POINTS)}},
        # The band's shape, frozen. The median it compares to is deliberately NOT
        # frozen -- it is a rolling reference by design, recomputed every night --
        # so what a published version pins is the definition of normal, not an
        # observation of it.
        baseline=volume_baseline(),
    )


def _volume_recorded(
    monitor: Mapping | None,
    ds_id: str,
    project_id: str,
    window_date: date,
    execution_id: str | None,
    outcome: str,
    observed: Mapping[str, Any],
    fired: bool = False,
) -> CheckVerdict:
    """Write the day's evaluation, then answer the firing question.

    The answer is a verdict and not a boolean because this function ALREADY
    knows the honest outcome -- it is the argument it just wrote to
    `app.dq_evaluations`. Returning `fired` threw that away on the way back to
    the sweep, so a `not_applicable` night and a clean night became the same
    `False` the moment they left this frame.

    On EVERY outcome and not only on a firing, for the reason `open_issue`
    documents: the issue's run moves to the night that saw the anomaly last, and
    the append-only evaluation of the earlier window is the only thing that keeps
    the earlier run. The window is the judged DAY on both ends -- the band judges
    one day against the thirty before it.
    """
    if monitor:
        from core import dq_monitor_bridge  # noqa: PLC0415

        reason = observed.get("reason")
        dq_monitor_bridge.record_verdict(
            "volume",
            project_id=project_id,
            monitor_id=str(monitor["monitor_id"]),
            monitor_version_id=str(monitor["monitor_version_id"]),
            datastream_id=ds_id,
            execution_id=execution_id,
            window_start=window_date,
            window_end=window_date,
            outcome=outcome,
            observed={
                **observed,
                **({"message": volume_message_for(str(reason))} if reason else {}),
            },
            fired=fired,
        )
    return CheckVerdict(
        fired,
        _STATUS_FOR_OUTCOME.get(outcome, STATUS_UNAVAILABLE),
        {"reason": observed.get("reason"), "datastream_id": ds_id, "outcome": outcome},
    )


# ---------------------------------------------------------------------------
# (b) Timeliness monitor
# ---------------------------------------------------------------------------


def _arrival_monitor(conn, ds_id: str, project_id: str) -> dict | None:
    """The declared arrival expectation for a delivered feed, or None.

    `app.datastream_arrival_monitors` is written at activation for the
    `inbound_email` and `webhook` channels -- and until this function existed it
    was READ BY NOBODY. The expectation an operator set ("a file every day")
    lived in a table and reached no surface at all.
    """
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT expected_interval_minutes, owner_person_id "
                "FROM app.datastream_arrival_monitors "
                "WHERE datastream_id = %s AND project_id = %s AND state = 'active'",
                (ds_id, project_id),
            )
            row = cur.fetchone()
    except Exception as exc:  # noqa: BLE001 -- unreadable is not "no expectation"
        logger.warning("dq_monitors: arrival_monitor_unreadable ds=%s: %s", ds_id, exc)
        return None
    if row is None:
        return None
    return {"expected_interval_minutes": int(row[0] or 0), "owner_person_id": row[1]}


#: Why an arrival expectation could not judge a day -- story 59.5, arbitrage 3.
ARRIVAL_NO_EXPECTED_INTERVAL = "no_expected_interval"
ARRIVAL_RECEIPTS_UNREADABLE = "receipts_unreadable"
ARRIVAL_NO_REFERENCE = "no_arrival_reference"

_ARRIVAL_MESSAGES = {
    ARRIVAL_NO_EXPECTED_INTERVAL: (
        "This feed declares no usable delivery interval, so there is no promise a "
        "delivery could be late against. Reporting lateness against a zero interval "
        "would fire on every Datastream the instant it was activated."
    ),
    ARRIVAL_RECEIPTS_UNREADABLE: (
        "The inbound receipts could not be read, so nothing was measured. An unreadable "
        "delivery history is never a pass."
    ),
    ARRIVAL_NO_REFERENCE: (
        "No file has ever been delivered and this feed carries no activation date, so "
        "there is no moment to measure the delay from."
    ),
}


def arrival_message_for(reason: str | None) -> str | None:
    """The sentence of an arrival reason, or `None`."""
    if not reason:
        return None
    return _ARRIVAL_MESSAGES.get(reason)


def _check_arrival_timeliness(
    ds_id: str,
    project_id: str,
    ds_name: str,
    monitor: dict,
    conn,
    now_utc: datetime,
    ds: Mapping | None = None,
    window_date: date | None = None,
) -> bool:
    """Timeliness for a feed that ARRIVES instead of being pulled.

    TWO DEFECTS IN ONE PLACE, and they are the same defect seen from both sides.

    A delivered Datastream has no extract ledger -- nothing pulls it, by design:
    activation sets `schedule_mode='manual'` precisely so the nightly puller's
    `WHERE ds.schedule_mode = 'nightly'` never selects it. So the pull-based
    timeliness check found no entry for yesterday and fired "no valid
    extraction" every single day, for every inbound Datastream, forever. A
    monitor that cannot be satisfied is worse than no monitor: it trains people
    to ignore the channel it fires on.

    And the expectation that SHOULD have been measured -- "a file every N
    minutes" -- sat in a table nobody read.

    Measured here against the last DELIVERY, which is the only event that means
    anything for this channel. Fires with the same `dq_timeliness` type as the
    pull monitor because it answers the same operator question ("is my data
    late?"), and splitting it would put half the answer on a surface nobody
    thinks to open.

    STORY 59.5 -- ITS VERDICT IS WRITTEN DOWN, AND THE WINDOW IS ARBITRAGE 3. This
    check measures ELAPSED MINUTES and has no window of its own, while
    `app.dq_evaluations.window_start` and `window_end` are `DATE NOT NULL`
    (migration 145:460-461). The day of the observation is used on both ends --
    the same day its sibling checks of the same sweep are judged on -- rather than
    the day of the last delivery, which is undefined for a feed no file has ever
    arrived for, the case this function already handles. *ds* carries the `org_id`
    a governed monitor needs; without it nothing is written, and the check answers
    exactly what it answered before.
    """
    judged_day = window_date or (_local_today(now_utc) - timedelta(days=1))
    interval = monitor.get("expected_interval_minutes") or 0
    if interval <= 0:
        # No usable expectation: reporting lateness against a zero interval
        # would fire on every Datastream the instant it was activated. Not
        # eligible, so no governed object is derived -- there is nothing for a
        # monitor to assert about a promise nobody made.
        return False

    governed = _arrival_governed_monitor(ds, ds_id, project_id, ds_name, interval)

    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT max(created_at) FROM app.inbound_receipts "
                "WHERE datastream_id = %s",
                (ds_id,),
            )
            last_receipt = (cur.fetchone() or (None,))[0]
    except Exception as exc:  # noqa: BLE001
        logger.warning("dq_monitors: arrival_receipts_unreadable ds=%s: %s", ds_id, exc)
        return _arrival_recorded(
            governed,
            ds_id,
            project_id,
            judged_day,
            "unverifiable",
            {
                "reason": ARRIVAL_RECEIPTS_UNREADABLE,
                "expected_interval_minutes": interval,
            },
        )

    # A grace of one full interval: a sender who is an hour late on a daily feed
    # is not an incident, and an alert that fires on ordinary variance is an
    # alert people mute.
    deadline_minutes = interval * 2

    if last_receipt is None:
        # Never delivered. Measured from ACTIVATION, not from epoch: a
        # Datastream activated five minutes ago is not late.
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT activated_at FROM app.datastream_arrival_monitors "
                    "WHERE datastream_id = %s AND project_id = %s",
                    (ds_id, project_id),
                )
                since = (cur.fetchone() or (None,))[0]
        except Exception:  # noqa: BLE001
            return _arrival_recorded(
                governed,
                ds_id,
                project_id,
                judged_day,
                "unverifiable",
                {
                    "reason": ARRIVAL_RECEIPTS_UNREADABLE,
                    "expected_interval_minutes": interval,
                },
            )
        if since is None:
            return _arrival_recorded(
                governed,
                ds_id,
                project_id,
                judged_day,
                "not_applicable",
                {
                    "reason": ARRIVAL_NO_REFERENCE,
                    "expected_interval_minutes": interval,
                },
            )
        reference, detail = since, "no file has ever been delivered"
    else:
        reference, detail = last_receipt, "the last delivery"

    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=timezone.utc)
    elapsed_minutes = (now_utc - reference).total_seconds() / 60.0
    measured = {
        "expected_interval_minutes": interval,
        "deadline_minutes": deadline_minutes,
        "minutes_since_last_delivery": int(elapsed_minutes),
        "ever_delivered": last_receipt is not None,
    }
    if elapsed_minutes <= deadline_minutes:
        return _arrival_recorded(
            governed, ds_id, project_id, judged_day, "pass", measured
        )

    from core import infra_alerts  # noqa: PLC0415

    infra_alerts.write_infra_firing(
        alert_type="dq_timeliness",
        project_id=project_id,
        metric="timeliness",
        severity="warning",
        message=(
            f"Arrival: '{ds_name}' expects a file every {interval} min; "
            f"{int(elapsed_minutes)} min since {detail}"
        ),
        metadata={
            "datastream_id": ds_id,
            "datastream_name": ds_name,
            "monitor_kind": "arrival",
            "expected_interval_minutes": interval,
            "minutes_since_last_delivery": int(elapsed_minutes),
            "owner_person_id": monitor.get("owner_person_id"),
        },
    )
    logger.info(
        "dq_monitors: arrival_firing ds=%s interval=%dmin elapsed=%dmin",
        ds_id,
        interval,
        int(elapsed_minutes),
    )
    return _arrival_recorded(
        governed, ds_id, project_id, judged_day, "fail", measured, fired=True
    )


def _arrival_governed_monitor(
    ds: Mapping | None,
    ds_id: str,
    project_id: str,
    ds_name: str,
    interval: int,
) -> dict[str, str] | None:
    """The governed `arrival_timeliness` monitor, for a feed that CAN be judged.

    Eligibility is the declared interval itself: an operator promised a file every
    N minutes, so there is something to assert. It is derived under its OWN check
    profile even though it fires under `dq_timeliness` -- the alert type answers
    "is my data late?", the profile answers "which check said so", and collapsing
    them would make a delivered feed's evidence indistinguishable from a pulled
    one's.
    """
    if not isinstance(ds, Mapping):
        return None
    from core import dq_monitor_bridge  # noqa: PLC0415

    return dq_monitor_bridge.derive_monitor(
        "arrival_timeliness",
        org_id=str(ds.get("org_id") or ""),
        project_id=project_id,
        datastream_id=ds_id,
        datastream_name=ds_name or ds_id,
        parameters={"thresholds": {"arrival_timeliness": float(interval)}},
    )


def _arrival_recorded(
    monitor: Mapping | None,
    ds_id: str,
    project_id: str,
    window_date: date,
    outcome: str,
    observed: Mapping[str, Any],
    fired: bool = False,
) -> bool:
    """Write the day's evaluation, then answer the firing question.

    No `execution_id`: a delivered feed has no run. `app.dq_issues` accepts NULL
    there as long as the Datastream is named, and inventing a run to fill the
    column would put an anomaly under a collection that never happened.
    """
    if monitor:
        from core import dq_monitor_bridge  # noqa: PLC0415

        reason = observed.get("reason")
        dq_monitor_bridge.record_verdict(
            "arrival_timeliness",
            project_id=project_id,
            monitor_id=str(monitor["monitor_id"]),
            monitor_version_id=str(monitor["monitor_version_id"]),
            datastream_id=ds_id,
            execution_id=None,
            window_start=window_date,
            window_end=window_date,
            outcome=outcome,
            observed={
                **observed,
                **({"message": arrival_message_for(str(reason))} if reason else {}),
            },
            fired=fired,
        )
    return fired


def _check_timeliness(
    ds_id: str,
    project_id: str,
    module_name: str,
    ds_name: str,
    conn,
    yesterday: date,
    now_utc: datetime | None = None,
    ds_window_offset_days: int = 1,
    ds: Mapping | None = None,
) -> CheckVerdict:
    """Evaluate timeliness monitor for one datastream.

    Yesterday's extract (status ok|partial) must exist by DQ_TIMELINESS_DUE_HOUR
    in the project timezone. Fires if missing when now is past due.
    `bool(verdict)` is True when a firing was issued, so every caller that read a
    plain boolean is unchanged.

    IT ANSWERS A VERDICT AND NOT A BOOLEAN because its two `timeliness_skip`
    exits meant "this day was never due" and both said `False`, which the sweep
    reports as "no issue". A day inside the extraction offset and an hour before
    the deadline are `not_applicable` naming their reason: neither is a
    Datastream that arrived on time.

    STORY 59.4 -- THE OFFSET GUARD, and the reason this check changed at all. It
    read "yesterday" hard-coded while `scheduler.py:1335-1340` sets
    `end_date = yesterday - (window_offset_days - 1)`. A Datastream at offset 3 has
    NO ledger row for yesterday BY CONSTRUCTION, so this check judged a day nobody
    asked for. A day inside the extraction offset is now not a late day, and the
    check says nothing about it. See :func:`effective_window_end`.

    WHEN THIS CHECK ACTUALLY FIRES, AND WHAT THE EXISTING ROWS ARE. Three things
    invoke `run_dq_monitors`, and they do not all reach the branch below:

    * the **fifteen-minute** Cloud Scheduler job -- `internal_api.py#_run_dq_monitors_internal`,
      `/internal/scheduler/run-dq-monitors`, `*/15 * * * *`
      (`infra/gcp/provision_ad36_substrate.sh:113`). Past 09:00 this DOES reach the
      ledger branch, roughly sixty times a day;
    * the **nightly sweep** -- `scheduler.py:659`, step 5, at
      `SCHEDULER_NIGHTLY_HOUR` = 2 (`scheduler.py:2041`). It does NOT reach it:
      `DQ_TIMELINESS_DUE_HOUR` is 9 (`:110`) and `2 < 9`, so the not-yet-due branch
      below returns first;
    * the manual `/api/dq/evaluate` (`dq_api.py:961-966`), whose route has been
      unmounted since story 49.4.

    THE 2421 EXISTING ROWS WERE NOT PRODUCED BY THAT JOB. Measured under role
    `postgres` on `date_trunc('minute', fired_at)`: thirteen distinct minutes, all
    on 2026-08-05, NONE of them on a quarter hour (12:32, 12:43, 12:58, 13:20,
    13:38, 13:39, 14:02, 14:03, 14:22, 14:23, 14:46, 21:18, 21:19), and ONE
    `window_date`. Irregular bursts on a single afternoon are a harness driving the
    endpoint by hand on a disposable base; a cron would have put every row on
    :00, :15, :30 or :45.

    AND THE OFFSET CAUSED NONE OF THEM. `window_offset_days` is 1 on 1421 rows of
    1421 (disposable, role `postgres`) and 47 of 47 (preprod), so no existing
    firing has this defect as its cause. The guard is PROSPECTIVE: it protects the
    first Datastream someone moves above 1, which `SchedulePanel.tsx:203` allows in
    a click.
    """
    from core.extract_ledger import get_extract_ledger  # noqa: PLC0415

    # Determine current time in project timezone.
    if now_utc is None:
        now_utc = datetime.now(tz=timezone.utc)

    # A DELIVERED feed is measured against its arrival expectation, never
    # against the pull ledger it has no entries in. See
    # `_check_arrival_timeliness` for what the pull-based check did to these
    # Datastreams before this branch existed.
    monitor = _arrival_monitor(conn, ds_id, project_id)
    if monitor is not None:
        # `ds` travels down for the `org_id` its governed monitor needs, and
        # `yesterday` is the day both branches of this check are judged on --
        # arbitrage 3 of story 59.5, and the reason the arrival branch does not
        # invent a window of its own.
        return _check_arrival_timeliness(
            ds_id, project_id, ds_name, monitor, conn, now_utc, ds, yesterday
        )

    tz_name = _scheduler_tz()
    try:
        from zoneinfo import ZoneInfo  # noqa: PLC0415

        now_local = now_utc.astimezone(ZoneInfo(tz_name))
    except Exception:
        now_local = now_utc

    # A day the dispatch never asked for cannot be late. The arrival branch above
    # is deliberately upstream of this: a DELIVERED feed is measured against the
    # interval a person declared, and no pull offset applies to it. `now_local`
    # rather than `now_utc` because the dispatch counts back from a LOCAL
    # yesterday (`scheduler.py:1332`, `project_yesterday`).
    last_fetchable = effective_window_end(ds_window_offset_days, now_local.date())
    if yesterday > last_fetchable:
        logger.debug(
            "dq_monitors: timeliness_skip: ds=%s inside_extraction_offset date=%s last=%s",
            ds_id,
            yesterday.isoformat(),
            last_fetchable.isoformat(),
        )
        return CheckVerdict(
            False,
            STATUS_NOT_APPLICABLE,
            {
                "reason": "inside_extraction_offset",
                "datastream_id": ds_id,
                "last_fetchable": last_fetchable.isoformat(),
            },
        )

    due_hour = _due_hour()

    # Not yet past due? Nothing to check.
    if now_local.hour < due_hour:
        logger.debug(
            "dq_monitors: timeliness_skip: ds=%s not_yet_due hour=%d due=%d",
            ds_id,
            now_local.hour,
            due_hour,
        )
        return CheckVerdict(
            False,
            STATUS_NOT_APPLICABLE,
            {"reason": "not_yet_due", "datastream_id": ds_id, "due_hour": due_hour},
        )

    # Check if yesterday's extract exists and has ok/partial status.
    yesterday_str = yesterday.isoformat()
    entries = get_extract_ledger(ds_id, yesterday_str, yesterday_str, conn)

    if entries:
        entry = entries[0]
        status = entry.get("status", "never_fetched")
        if status in ("ok", "partial"):
            # The one honest pass: the ledger was read and it holds the day.
            return CheckVerdict(
                False, STATUS_EVALUATED, {"datastream_id": ds_id, "status": status}
            )

    # Missing or bad status -- fire timeliness alert.
    from core import infra_alerts  # noqa: PLC0415

    infra_alerts.write_infra_firing(
        alert_type="dq_timeliness",
        project_id=project_id,
        metric="timeliness",
        severity="warning",
        message=(
            f"Timeliness: no valid extraction for '{ds_name}' "
            f"on {yesterday_str} after {due_hour:02d}:00 ({tz_name})"
        ),
        # Story 59.4, arbitrage 5: the day the finding SPEAKS of, never the day it
        # was written. `write_infra_firing` dated every row `date.today()`.
        window_date=yesterday,
        metadata={
            "datastream_id": ds_id,
            "datastream_name": ds_name,
            "module_name": module_name,
            "window_date": yesterday_str,
            "due_hour": due_hour,
            "timezone": tz_name,
            "actual_status": entries[0].get("status") if entries else "never_fetched",
        },
    )
    logger.info(
        "dq_monitors: timeliness_firing ds=%s date=%s due_hour=%d",
        ds_id,
        yesterday_str,
        due_hour,
    )
    return CheckVerdict(
        True, STATUS_EVALUATED, {"datastream_id": ds_id, "window_date": yesterday_str}
    )


# ---------------------------------------------------------------------------
# (c) Duplication monitor
# ---------------------------------------------------------------------------


def _get_raw_table_for_ds(module_name: str) -> str:
    """Resolve the raw DuckDB table name for a module (AD-2: name is data).

    Uses verification._get_raw_table_name(provider=module_name).
    Falls back to env TOOROW_RAW_TABLE_NAME.
    Returns empty string if unresolvable.
    """
    try:
        from core.verification import _get_raw_table_name  # noqa: PLC0415

        return _get_raw_table_name(provider=module_name)
    except Exception:
        return os.environ.get("TOOROW_RAW_TABLE_NAME", "")


def _unmeasured_duplication(ds_id: str, status: str, reason: str) -> CheckVerdict:
    """No count was taken, and the answer says which of the reasons it was.

    NEVER `False`: a table nobody could open holds no duplicate the same way an
    empty room holds no noise -- the sentence is about the reading, not about the
    client's data.
    """
    return CheckVerdict(False, status, {"reason": reason, "datastream_id": ds_id})


def _check_duplication(
    ds_id: str,
    project_id: str,
    module_name: str,
    ds_name: str,
    yesterday: date,
) -> CheckVerdict:
    """Evaluate duplication monitor for one datastream.

    Counts duplicate full rows in the raw DuckDB table for yesterday.
    Uses DuckDB directly (raw tables live in DuckDB, not Postgres).
    `bool(verdict)` is True when a firing was issued, so every caller that read a
    plain boolean is unchanged.

    IT ANSWERS A VERDICT AND NOT A BOOLEAN because SIX of its exits meant
    "nothing was measured" and all six said `False`, which the sweep reports as
    "no issue" -- the sentence `governance.md`'s `Incomplete if` forbids. The
    `duplication_skip` log lines were the only place the difference existed.
    """
    raw_table = _get_raw_table_for_ds(module_name)
    if not raw_table:
        logger.debug(
            "dq_monitors: duplication_unmeasured: ds=%s reason=%s module=%s",
            ds_id,
            NO_RAW_TABLE,
            module_name,
        )
        return _unmeasured_duplication(ds_id, STATUS_UNAVAILABLE, NO_RAW_TABLE)

    db_path = _duckdb_path()
    if not db_path:
        logger.debug(
            "dq_monitors: duplication_unmeasured: ds=%s reason=%s",
            ds_id,
            WAREHOUSE_UNREACHABLE,
        )
        return _unmeasured_duplication(ds_id, STATUS_UNAVAILABLE, WAREHOUSE_UNREACHABLE)

    yesterday_str = yesterday.isoformat()
    duplicate_count = 0

    try:
        import duckdb  # noqa: PLC0415

        with duckdb.connect(db_path, read_only=True) as duck_conn:
            # Get column list for the raw table.
            try:
                col_rows = duck_conn.execute(
                    "SELECT column_name FROM information_schema.columns "  # noqa: S608
                    "WHERE table_name = ? ORDER BY ordinal_position",
                    [raw_table.split(".")[-1]],
                ).fetchall()
                columns = [r[0] for r in col_rows]
            except Exception:
                columns = []

            if not columns:
                logger.debug(
                    "dq_monitors: duplication_unmeasured: ds=%s raw_table=%s reason=%s",
                    ds_id,
                    raw_table,
                    RELATION_HAS_NO_COLUMN,
                )
                return _unmeasured_duplication(
                    ds_id, STATUS_NOT_APPLICABLE, RELATION_HAS_NO_COLUMN
                )

            # Exclude system/audit columns from grain check.
            grain_cols = [
                c for c in columns
                if c not in ("pull_id", "loaded_at", "_dq_checked_at")
            ]
            if not grain_cols:
                # Every column was a system column: there is no grain to compare
                # rows on, so no duplicate can be defined -- not a clean table.
                return _unmeasured_duplication(
                    ds_id, STATUS_NOT_APPLICABLE, "no_grain_column"
                )

            col_list = ", ".join(f'"{c}"' for c in grain_cols)
            # Count rows that appear more than once (duplicates) for yesterday.
            sql = (
                f"SELECT COALESCE(SUM(cnt - 1), 0) AS dup_count "  # noqa: S608
                f"FROM ("
                f"  SELECT COUNT(*) AS cnt FROM {raw_table} "
                f"  WHERE project_id = ? AND date = ? "
                f"  GROUP BY {col_list} "
                f"  HAVING COUNT(*) > 1"
                f") sub"
            )
            row = duck_conn.execute(sql, [project_id, yesterday_str]).fetchone()
            duplicate_count = int(row[0]) if row and row[0] is not None else 0

    except Exception as exc:
        logger.warning(
            "dq_monitors: duplication_check_failed ds=%s: %s", ds_id, exc
        )
        return _unmeasured_duplication(ds_id, STATUS_UNAVAILABLE, "relation_uncountable")

    if duplicate_count == 0:
        # The one honest pass: the grain was counted and nothing repeated.
        return CheckVerdict(False, STATUS_EVALUATED, {"datastream_id": ds_id})

    from core import infra_alerts  # noqa: PLC0415

    infra_alerts.write_infra_firing(
        alert_type="dq_duplication",
        project_id=project_id,
        metric="duplicate_rows",
        severity="warning",
        message=(
            f"Duplicate rows detected in '{ds_name}' on {yesterday_str}: "
            f"{duplicate_count} duplicated rows"
        ),
        metadata={
            "datastream_id": ds_id,
            "datastream_name": ds_name,
            "module_name": module_name,
            "window_date": yesterday_str,
            "duplicate_count": duplicate_count,
            "raw_table": raw_table,
        },
    )
    logger.info(
        "dq_monitors: duplication_firing ds=%s date=%s dup_count=%d raw_table=%s",
        ds_id,
        yesterday_str,
        duplicate_count,
        raw_table,
    )
    return CheckVerdict(
        True,
        STATUS_EVALUATED,
        {"datastream_id": ds_id, "duplicate_count": duplicate_count},
    )


# ---------------------------------------------------------------------------
# (d) Schema consistency monitor
# ---------------------------------------------------------------------------


#: Why no column could be read. Each names a DIFFERENT thing to go and fix, which
#: is the whole reason an empty list was not an answer: `[]` meant "no raw table
#: configured", "warehouse unreachable" and "the relation is genuinely empty"
#: all at once, and `_check_schema` reported the three of them as "no issue".
NO_RAW_TABLE = "raw_table_unknown"
WAREHOUSE_UNREACHABLE = "warehouse_unreachable"
RELATION_HAS_NO_COLUMN = "relation_has_no_column"


def _why_no_raw_columns(module_name: str) -> str:
    """Which of the three, asked only once the column list came back empty.

    Both branches are the same cheap lookups `_fetch_raw_columns` already makes;
    nothing is queried twice. The third answer is the one a check may treat as a
    measurement -- the relation was reachable and holds no column -- and the
    first two are states of THIS deployment, never of the client's data.
    """
    if not _get_raw_table_for_ds(module_name):
        return NO_RAW_TABLE
    if not _duckdb_path():
        return WAREHOUSE_UNREACHABLE
    return RELATION_HAS_NO_COLUMN


def _read_raw_columns(
    module_name: str, project_id: str, yesterday: date
) -> tuple[list[str], str | None]:
    """The columns, and WHY there are none when there are none.

    Goes through `_fetch_raw_columns` on purpose rather than around it: that is
    the seam the tests patch, and a reader that bypassed it would be measuring a
    different thing from the one the suite pins.
    """
    columns = _fetch_raw_columns(module_name, project_id, yesterday)
    if columns:
        return columns, None
    return [], _why_no_raw_columns(module_name)


def _fetch_raw_columns(module_name: str, project_id: str, yesterday: date) -> list[str]:
    """Read current column list from the raw DuckDB table for module_name.

    Returns sorted list of column names. Returns [] on any error -- callers that
    need to tell the errors apart go through :func:`_read_raw_columns`.
    """
    raw_table = _get_raw_table_for_ds(module_name)
    if not raw_table:
        return []

    db_path = _duckdb_path()
    if not db_path:
        return []

    try:
        import duckdb  # noqa: PLC0415

        with duckdb.connect(db_path, read_only=True) as duck_conn:
            table_name = raw_table.split(".")[-1]
            rows = duck_conn.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = ? ORDER BY column_name",
                [table_name],
            ).fetchall()
            return sorted(r[0] for r in rows if r[0])
    except Exception as exc:
        logger.debug(
            "dq_monitors: schema_fetch_cols_failed ds_module=%s: %s", module_name, exc
        )
        return []


def _governed_baseline_columns(
    ds_id: str, project_id: str
) -> tuple[list[str] | None, str | None]:
    """The frozen column set of this Datastream's published `schema` monitor.

    Answers ``(columns, absence)``, exactly one side filled, forwarding the two
    distinct absences :func:`core.dq_monitor_bridge.published_baseline` keeps
    apart.

    THIS REPLACED `_read_dq_baseline` / `_write_dq_baseline`, AND THE PAIR IS
    GONE RATHER THAN WRAPPED. They read and wrote `app.dq_baselines`: one mutable
    row per Datastream, keyed on the Datastream alone, carrying no monitor, no
    version and no decision date. Migration 145 REFUSES to apply while that table
    holds a single row -- "adopting them would invent the governed identity they
    never had" -- and the nightly sweep re-created exactly those rows every
    night, on the evaluation path, for every Datastream including the ones a
    published monitor already covered. A store one migration declares
    un-adoptable and the runtime keeps refilling is a parallel evidence store,
    and a check that WRITES while it reads is not a read.

    `schema` was the last check on that store. `volume`, `arrival_timeliness`,
    `null_rate` and `zero_rows` already freeze their reference inside an
    immutable published version through `dq_monitor_bridge`; this is `schema`
    joining them, so there is one answer to "what is this monitor judged
    against?" and not two.
    """
    from core import dq_monitor_bridge  # noqa: PLC0415

    baseline, absence = dq_monitor_bridge.published_baseline(
        "schema", project_id=project_id, datastream_id=ds_id
    )
    if absence is not None:
        return None, absence
    columns = (baseline or {}).get("columns")
    if not isinstance(columns, list):
        # A published version whose baseline carries no column list cannot judge a
        # column list. That is `unavailable` -- never "nothing drifted", and never
        # an invitation to freeze tonight's columns over it.
        return None, "governed_baseline_has_no_columns"
    return sorted(str(c) for c in columns), None


def _seed_governed_baseline(
    ds: Mapping[str, Any] | None,
    ds_id: str,
    project_id: str,
    ds_name: str,
    current_cols: list[str],
) -> bool:
    """Freeze the first observation as a published DQ Monitor version.

    The seed is a GOVERNED decision with a date and an actor (`system`), because
    that is what `governance.md` requires of any baseline: "accepting a new
    baseline is publishing a new immutable DQ Monitor version". The previous seed
    was an `INSERT ... ON CONFLICT DO UPDATE` on a side table that anyone could
    overwrite and nobody could point at.

    Only ever called when the governed store answered `no_published_monitor` --
    never on an unreadable one, because `publish_version` is idempotent by
    CONTENT and a seed run over an unread existing version would mint a second
    version carrying the drifted columns. That is the auto-reset returning
    through a failed query, and it is why the two absences are separate.
    """
    from core import dq_monitor_bridge  # noqa: PLC0415

    org_id = str((ds or {}).get("org_id") or "")
    if not org_id:
        # A governed object needs an owner org. Without one nothing is frozen and
        # the check says so rather than falling back to a store that needed none.
        return False
    return (
        dq_monitor_bridge.derive_monitor(
            "schema",
            org_id=org_id,
            project_id=project_id,
            datastream_id=ds_id,
            datastream_name=ds_name or ds_id,
            baseline={"columns": sorted(current_cols)},
        )
        is not None
    )


def _check_schema(
    ds_id: str,
    project_id: str,
    module_name: str,
    ds_name: str,
    conn,
    yesterday: date,
    frozen_baseline: list[str] | None = None,
    ds: Mapping[str, Any] | None = None,
) -> CheckVerdict:
    """Evaluate schema consistency monitor for one datastream.

    First observation: freezes the baseline as a published version, no firing.
    On drift: fires, and the baseline STANDS.
    `bool(verdict)` is True when a firing was issued, so every caller that read a
    plain boolean is unchanged.

    IT ANSWERS A VERDICT AND NOT A BOOLEAN because three of its exits meant
    "nothing was measured" and all three said `False`, which the sweep reports as
    "no issue" -- the exact sentence `governance.md`'s `Incomplete if` forbids.
    An unreadable column list, a warehouse this deployment cannot reach and a
    first run with nothing to compare against are now `unavailable` or
    `not_applicable`, each naming its reason, and never a pass.

    Story 49.4 removed the auto-reset. The previous behaviour fired once and then
    advanced the baseline to the drifted state, so the NEXT run compared the source
    against what the source had just become and passed. A drift check that agrees
    with the drift is not a check -- it reports a schema change exactly once and is
    blind to it forever after, including to the same column disappearing again.

    The baseline now only ever moves through
    :func:`core.dq_governance.propose_baseline_change`, which returns a candidate
    and applies nothing: accepting a new baseline is publishing a new immutable
    DQ Monitor version, with a date and an actor.

    AND THE BASELINE ITSELF NO LONGER LIVES IN A SIDE TABLE. `app.dq_baselines`
    is neither read nor written from anywhere: on the sweep as on the manual
    route, the reference is the `baseline.columns` of this Datastream's published
    `schema` monitor version. The sweep used to pass no version at all and fall
    through to that table for EVERY Datastream, so the ratified order -- the
    published version wins, the side table answers only where no published
    monitor covers -- was written and never implemented; there was no code
    anywhere that asked whether a published monitor covered a Datastream.

    The consequence is deliberate and is the point: this monitor keeps firing on
    every run until someone decides. A repeated alarm about a real unresolved
    change is the honest state; silence after one alarm was not.
    """
    current_cols, absence = _read_raw_columns(module_name, project_id, yesterday)
    if absence is not None:
        logger.debug(
            "dq_monitors: schema_unmeasured ds=%s reason=%s module=%s",
            ds_id,
            absence,
            module_name,
        )
        # NEVER `False`. A column list nobody could read is not a schema that did
        # not drift, and the log line that said `schema_skip` was the only place
        # the difference existed.
        status = (
            STATUS_NOT_APPLICABLE if absence == RELATION_HAS_NO_COLUMN else STATUS_UNAVAILABLE
        )
        return CheckVerdict(False, status, {"reason": absence, "datastream_id": ds_id})

    if frozen_baseline is not None:
        # A GOVERNED monitor supplied its version's baseline directly -- the
        # manual route already resolved the version it is evaluating.
        baseline = sorted(str(c) for c in frozen_baseline)
    else:
        # THE SWEEP RESOLVES THE SAME GOVERNED VERSION INSTEAD OF A SIDE TABLE.
        # It used to pass `None` here for every Datastream and fall through to
        # `app.dq_baselines`, so a Datastream a published monitor already covered
        # was still judged against a mutable row -- the ratified order
        # (`governance.md`, "The DQ baseline has the same settled order") said the
        # published version wins and no code asked whether one existed.
        baseline, absence = _governed_baseline_columns(ds_id, project_id)
        if absence == "no_published_monitor":
            # First observation. Freezing it is PUBLISHING a version, with a date
            # and an actor -- not an upsert on a row keyed by Datastream alone.
            seeded = _seed_governed_baseline(ds, ds_id, project_id, ds_name, current_cols)
            logger.info(
                "dq_monitors: schema_baseline_frozen ds=%s cols=%d published=%s",
                ds_id,
                len(current_cols),
                seeded,
            )
            # `not_applicable` and not a pass either way: there was nothing to
            # compare against, and a drift check that has never had two
            # observations has not cleared anything. When the freeze itself did
            # not land, the next run has nothing to judge against and says so
            # rather than reporting a schema that did not drift.
            return CheckVerdict(
                False,
                STATUS_NOT_APPLICABLE if seeded else STATUS_UNAVAILABLE,
                {
                    "reason": "baseline_frozen" if seeded else "baseline_not_frozen",
                    "datastream_id": ds_id,
                },
            )
        if absence is not None:
            # A reference nobody could read is NEVER a pass, and never an
            # invitation to freeze tonight's columns over a version that may
            # exist -- that would be the auto-reset returning through a failed
            # query.
            return CheckVerdict(
                False,
                STATUS_UNAVAILABLE,
                {"reason": absence, "datastream_id": ds_id},
            )

    if sorted(baseline) == sorted(current_cols):
        # The one honest pass of this function: two column lists were read and
        # they agree.
        return CheckVerdict(False, STATUS_EVALUATED, {"datastream_id": ds_id})

    # Drift detected.
    added = sorted(set(current_cols) - set(baseline))
    removed = sorted(set(baseline) - set(current_cols))
    yesterday_str = yesterday.isoformat()

    from core import infra_alerts  # noqa: PLC0415

    infra_alerts.write_infra_firing(
        alert_type="dq_schema",
        project_id=project_id,
        metric="schema_consistency",
        severity="warning",
        message=(
            f"Schema changed for '{ds_name}' "
            f"(+{len(added)} columns, -{len(removed)} columns)"
        ),
        metadata={
            "datastream_id": ds_id,
            "datastream_name": ds_name,
            "module_name": module_name,
            "window_date": yesterday_str,
            "added_columns": added,
            "removed_columns": removed,
            "baseline_cols": len(baseline),
            "current_cols": len(current_cols),
        },
    )
    logger.info(
        "dq_monitors: schema_firing ds=%s added=%s removed=%s",
        ds_id,
        added,
        removed,
    )

    # NO AUTO-RESET (Story 49.4). The baseline stands until a governed decision
    # advances it. `propose_baseline_change` describes what that decision would be
    # and performs nothing, so the proposal is available to Controls & Quality
    # without this run silently becoming the new truth.
    _log_baseline_candidate(ds_id, project_id, current_cols)
    return CheckVerdict(
        True,
        STATUS_EVALUATED,
        {
            "datastream_id": ds_id,
            "added_columns": added,
            "removed_columns": removed,
        },
    )


def _log_baseline_candidate(ds_id: str, project_id: str, current_cols: list[str]) -> None:
    """Record that a baseline change is available, without making one.

    Fail-soft: a Project with no governed DQ Monitor yet has nothing to propose
    against, and that must not break the legacy check that just fired correctly.
    """
    try:
        from core.db import get_connection  # noqa: PLC0415
        from core.dq_governance import DqMonitorNotFound, propose_baseline_change  # noqa: PLC0415

        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id FROM app.dq_monitors WHERE project_id = %s "
                    "AND target_kind = 'datastream' AND target_id = %s "
                    "AND lifecycle_status = 'published' LIMIT 1",
                    (project_id, ds_id),
                )
                row = cur.fetchone()
            if row is None:
                logger.info(
                    "dq_monitors: baseline_candidate_unrecorded ds=%s "
                    "(no governed monitor yet; the baseline still stands)",
                    ds_id,
                )
                return
            proposal = propose_baseline_change(
                conn,
                project_id=project_id,
                monitor_id=str(row[0]),
                observed_baseline={"columns": sorted(current_cols)},
            )
        logger.info(
            "dq_monitors: baseline_candidate ds=%s monitor=%s applied=%s",
            ds_id,
            proposal["monitor_id"],
            proposal["applied"],
        )
    except DqMonitorNotFound:
        logger.info("dq_monitors: baseline_candidate_no_published_version ds=%s", ds_id)
    except Exception as exc:  # noqa: BLE001 -- never break a correct firing
        logger.warning("dq_monitors: baseline_candidate_failed ds=%s: %s", ds_id, exc)


# ---------------------------------------------------------------------------
# (e) Rejected rows monitor (Story 8.10, R3 redesign -- fix review-epic-8 #7)
# ---------------------------------------------------------------------------

def _rejected_rows_threshold() -> int:
    """Read the rejected_rows firing threshold at call time (so tests can override via env)."""
    try:
        return int(os.environ.get("DQ_REJECTED_ROWS_THRESHOLD", "0"))
    except (ValueError, TypeError):
        return 0


def _check_date_format(
    ds_id: str,
    project_id: str,
    module_name: str,
    ds_name: str,
    yesterday: date,
    config: dict | None = None,
) -> CheckVerdict:
    """Evaluate the rejected-rows monitor for one datastream (replaces dead date_format check).

    Fix [MEDIUM #7]: the original date_format monitor checked raw_generic_daily for
    non-ISO dates, but those rows are already rejected at landing time by the generic
    connector and never written to the table -- so the check always found 0 bad rows.

    Redesign: read the rejected_rows count from the pull_jobs result (stored in
    pull_verifications.rejected_rows when the column exists, or inferred from the
    pull result metadata stored in pull_jobs.result_payload where available).
    Fires when rejected_rows for yesterday's pull(s) exceeds DQ_REJECTED_ROWS_THRESHOLD
    (default 0 -- fire on any rejection).

    Applies to all modules (AD-2: module_name is data).
    Other modules that do not report rejected_rows produce 0 and do not fire.
    `bool(verdict)` is True when a firing was issued. Never raises (per-stream
    isolation) -- and the exception path answers `unavailable` rather than the
    `False` it used to, because a query that did not run counted no rejected row
    the same way an unopened book contains no words.
    """
    yesterday_str = yesterday.isoformat()
    total_rejected = 0

    try:
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            with conn.cursor() as cur:
                # Query pull_verifications.rejected_rows for yesterday's completed pulls.
                # The column was added as part of this fix; the query uses COALESCE so
                # older rows without the column default to 0 (safe).
                cur.execute(
                    """
                    SELECT COALESCE(SUM(pv.rejected_rows), 0)
                    FROM app.pull_jobs pj
                    JOIN app.pull_verifications pv ON pv.pull_id = pj.pull_id
                    WHERE pj.datastream_id = %s
                      AND pj.date_to = %s::date
                      AND pj.state = 'done'
                    """,
                    (ds_id, yesterday_str),
                )
                row = cur.fetchone()
                total_rejected = int(row[0] or 0) if row else 0

    except Exception as exc:
        logger.warning(
            "dq_monitors: rejected_rows_check_failed ds=%s: %s", ds_id, exc
        )
        return CheckVerdict(
            False,
            STATUS_UNAVAILABLE,
            {"reason": "rejected_rows_uncountable", "datastream_id": ds_id},
        )

    threshold = _rejected_rows_threshold()
    if total_rejected <= threshold:
        # A pass that was measured: the sum ran and came back under the bar.
        return CheckVerdict(
            False,
            STATUS_EVALUATED,
            {"datastream_id": ds_id, "rejected_rows": total_rejected},
        )

    from core import infra_alerts  # noqa: PLC0415

    infra_alerts.write_infra_firing(
        alert_type="dq_date_format",
        project_id=project_id,
        metric="rejected_rows",
        severity="warning",
        message=(
            f"Rejected rows detected in '{ds_name}' "
            f"on {yesterday_str}: {total_rejected} rejected row(s) "
            f"(threshold={threshold})"
        ),
        metadata={
            "datastream_id": ds_id,
            "datastream_name": ds_name,
            "module_name": module_name,
            "window_date": yesterday_str,
            "rejected_rows": total_rejected,
            "threshold": threshold,
        },
    )
    logger.info(
        "dq_monitors: rejected_rows_firing ds=%s date=%s rejected=%d threshold=%d",
        ds_id,
        yesterday_str,
        total_rejected,
        threshold,
    )
    return CheckVerdict(
        True,
        STATUS_EVALUATED,
        {"datastream_id": ds_id, "rejected_rows": total_rejected, "threshold": threshold},
    )


# ---------------------------------------------------------------------------
# (g) Null rate monitor (Story 59.3, epic 59) -- REQUIRED fields only.
#
# The measurement, the required-field rule and the governance bridge live in
# `core.dq_null_rate`; what is here is the monitor's own two faces: the boolean
# one the nightly sweep has always spoken, and the three-way verdict a governed
# evaluation needs so that "could not measure" never renders as "passed".
# ---------------------------------------------------------------------------


class CheckVerdict:
    """What a check answers when a boolean cannot say it.

    `bool(verdict)` is "did it fire", so the scheduled sweep and its summary are
    unchanged and every check that still returns a plain `bool` keeps working.
    `status` is the part a boolean was never able to carry:

    * ``evaluated``      -- the check ran. `fired` then means pass or fail.
    * ``not_applicable`` -- nothing eligible was measured. NEVER a pass: a
      dashboard that goes green over nothing is the exact failure
      `ck_dq_evaluations_empty_is_not_a_pass` exists to make unstorable.
    * ``unavailable``    -- the check could not run. Also never a pass, and a
      different sentence from "the target is broken".
    """

    __slots__ = ("fired", "status", "detail")

    def __init__(self, fired: bool, status: str = "evaluated", detail: dict | None = None):
        self.fired = bool(fired)
        self.status = status
        self.detail = dict(detail or {})

    def __bool__(self) -> bool:
        return self.fired

    def __eq__(self, other: object) -> bool:
        if isinstance(other, bool):
            return self.fired is other
        if isinstance(other, CheckVerdict):
            return (self.fired, self.status) == (other.fired, other.status)
        return NotImplemented

    def __hash__(self) -> int:
        return hash((self.fired, self.status))

    def __repr__(self) -> str:  # pragma: no cover -- diagnostics only
        return f"CheckVerdict(fired={self.fired}, status={self.status!r})"


STATUS_EVALUATED = "evaluated"
STATUS_NOT_APPLICABLE = "not_applicable"
STATUS_UNAVAILABLE = "unavailable"


def _check_null_rate(
    ds: dict,
    conn,
    window_date: date,
    required: Sequence[str],
    threshold: float,
) -> CheckVerdict:
    """Did the null rate of a REQUIRED field of *ds* cross *threshold* on that day?

    THE FUNCTION THAT RUNS. `CHECK_PROFILES["null_rate"]` reaches it through
    `_adapt_null_rate`, exactly as its five siblings are reached through theirs,
    so the per-flux isolation stated here is the isolation the nightly sweep
    actually gets. It answers a :class:`CheckVerdict` rather than a bare `bool`
    because a bool cannot tell "measured and fine" from "could not measure";
    `bool(...)` of it is the firing question, and it is `False` when no field is
    required, when the relation cannot be read, and when every rate is at or under
    the threshold.

    It NEVER raises: one flux must not take the sweep down
    (`_run_monitors_for_datastream`), and an exception is answered as
    `unavailable` rather than swallowed into a pass.

    Order matters and is the arbitrage: the governed monitor is derived as soon as
    the Datastream is ELIGIBLE -- its active mapping version declares a grain --
    and not only when something fires. A Datastream that is watched and quiet must
    carry a monitor, or `Check` on the Workbench cannot tell "watched and fine"
    from "watched by nobody". And the night's evaluation is written on every
    outcome, not only on a firing: it is what keeps the run that the issue is
    about to overwrite.
    """
    try:
        return _null_rate_verdict(ds, conn, window_date, required, threshold)
    except Exception as exc:  # noqa: BLE001 -- per-flux isolation, stated in the contract
        logger.warning("dq_monitors: null_rate_failed ds=%s: %s", ds.get("id"), exc)
        return CheckVerdict(
            False,
            STATUS_UNAVAILABLE,
            {"datastream_id": ds.get("id"), "reason": f"check_raised:{type(exc).__name__}"},
        )


def _null_rate_verdict(
    ds: dict,
    conn,
    window_date: date,
    required: Sequence[str],
    threshold: float,
) -> CheckVerdict:
    """The body of :func:`_check_null_rate`, which owns the isolation."""
    from core import dq_null_rate  # noqa: PLC0415

    fields = [str(field) for field in (required or []) if str(field).strip()]
    ds_id = str(ds.get("id") or "")
    project_id = str(ds.get("project_id") or "")
    if not fields:
        # Not eligible under arbitrage 1, so no governed object is derived and
        # there is nothing to evaluate: a monitor that could never evaluate
        # anything is an empty governed object.
        return CheckVerdict(
            False,
            STATUS_NOT_APPLICABLE,
            {
                "reason": dq_null_rate.NO_REQUIRED_FIELD,
                "message": dq_null_rate.message_for(dq_null_rate.NO_REQUIRED_FIELD),
                "datastream_id": ds_id,
                "project_id": project_id,
            },
        )

    monitor = dq_null_rate.derive_monitor(
        org_id=str(ds.get("org_id") or ""),
        project_id=project_id,
        datastream_id=ds_id,
        datastream_name=str(ds.get("name") or ds_id),
        threshold=threshold,
    )
    window_day = window_date.isoformat()
    # Resolved for EVERY outcome and not only for a firing: the evaluation carries
    # the run of its own window, and that row is what survives the issue's
    # overwrite on the next night.
    execution_id = dq_null_rate.resolve_execution_id(
        conn, project_id=project_id, datastream_id=ds_id, window_date=window_date
    )
    detail: dict[str, Any] = {
        "datastream_id": ds_id,
        "project_id": project_id,
        "execution_id": execution_id,
        "required_fields": fields,
        "window_date": window_day,
    }

    resolved = dq_null_rate.resolve_collected_relation(
        connector=ds.get("module_name"), report_profile_id=ds.get("report_profile_id")
    )
    relation = resolved.get("relation")
    if not relation:
        detail.update(
            {
                "relation": None,
                "reason": resolved.get("reason"),
                "message": resolved.get("message"),
            }
        )
        return _null_rate_recorded(
            CheckVerdict(False, STATUS_UNAVAILABLE, detail), monitor, window_date, ()
        )

    measurement = dq_null_rate.measure_null_rates(
        project_id=project_id,
        relation=str(relation),
        fields=fields,
        window_date=window_date,
    )
    detail.update(
        {
            "relation": measurement.get("relation"),
            "reason": measurement.get("reason"),
            "message": measurement.get("message"),
        }
    )
    if measurement["status"] == dq_null_rate.STATUS_UNAVAILABLE:
        return _null_rate_recorded(
            CheckVerdict(False, STATUS_UNAVAILABLE, detail), monitor, window_date, ()
        )
    if measurement["status"] == dq_null_rate.STATUS_NOT_APPLICABLE:
        return _null_rate_recorded(
            CheckVerdict(False, STATUS_NOT_APPLICABLE, detail), monitor, window_date, ()
        )

    findings = dq_null_rate.findings_over_threshold(measurement, threshold)
    detail["row_count"] = int(measurement.get("row_count") or 0)
    if not findings:
        return _null_rate_recorded(
            CheckVerdict(False, STATUS_EVALUATED, detail), monitor, window_date, ()
        )

    from core import infra_alerts  # noqa: PLC0415

    for finding in findings:
        infra_alerts.write_infra_firing(
            alert_type=dq_null_rate.ALERT_TYPE,
            project_id=project_id,
            metric="null_rate",
            severity="warning",
            # The sentence names BOTH spellings of absent, because they are
            # repaired in two different places: a NULL comes from the source, a
            # blank usually comes from what the extraction wrote in its place.
            message=(
                f"Null rate: required field '{finding['field']}' of "
                f"'{ds.get('name') or ds_id}' carries no value on "
                f"{finding.get('missing_count', finding['null_count'])} of "
                f"{finding['row_count']} rows for {window_day} "
                f"({finding['null_count']} null, {finding.get('blank_count', 0)} blank)"
            ),
            # THE MEASUREMENT, NEVER THE ROWS. Faulty rows are replayed on demand
            # (Jean, 2026-08-07); migration 145 already said it of `observed` --
            # "bounded, masked, and NOT the rows themselves".
            metadata={
                "datastream_id": ds_id,
                "datastream_name": ds.get("name"),
                "module_name": ds.get("module_name"),
                "field": finding["field"],
                "null_count": finding["null_count"],
                "blank_count": finding.get("blank_count", 0),
                "missing_count": finding.get("missing_count", finding["null_count"]),
                "row_count": finding["row_count"],
                "null_rate": finding["null_rate"],
                "missing_rate": finding.get("missing_rate", finding["null_rate"]),
                "threshold": finding["threshold"],
                "window_date": window_day,
                "execution_id": execution_id,
                "relation": measurement.get("relation"),
            },
        )

    logger.info(
        "dq_monitors: null_rate_firing ds=%s date=%s fields=%d rows=%d execution=%s",
        ds_id,
        window_day,
        len(findings),
        int(measurement.get("row_count") or 0),
        execution_id,
    )
    detail["findings"] = findings
    return _null_rate_recorded(
        CheckVerdict(True, STATUS_EVALUATED, detail), monitor, window_date, findings
    )


#: What each verdict status is worth as a governed OUTCOME. The same mapping the
#: governed evaluator applies, written once so a night's evidence and a manual
#: evaluation of the same window cannot disagree about the word.
_OUTCOME_FOR_VERDICT = {
    (STATUS_EVALUATED, True): "fail",
    (STATUS_EVALUATED, False): "pass",
    (STATUS_NOT_APPLICABLE, False): "not_applicable",
    (STATUS_UNAVAILABLE, False): "unverifiable",
}

#: The same table read the other way, for the checks that decide their stored
#: outcome first and answer the sweep second. DERIVED and not restated: two
#: hand-written tables would be a mapping that disagrees with its own inverse.
_STATUS_FOR_OUTCOME = {outcome: status for (status, _), outcome in _OUTCOME_FOR_VERDICT.items()}


def _null_rate_recorded(
    verdict: CheckVerdict,
    monitor: dict | None,
    window_date: date,
    findings: Sequence[Mapping[str, Any]],
) -> CheckVerdict:
    """Write the night's evaluation and the issues that point at it, then answer.

    WHY ON EVERY OUTCOME AND NOT ONLY ON A FIRING. `open_issue` overwrites the
    issue's `execution_id` with the run that saw the anomaly last, and that
    overwrite is only honest because the earlier window kept an append-only
    evaluation naming its own run. Writing evidence solely on the nights that
    fired would leave the quiet nights unrecorded and the moved run unrecoverable.
    """
    from core import dq_null_rate  # noqa: PLC0415

    if not monitor:
        if findings:
            logger.warning(
                "dq_null_rate: firing_without_issue ds=%s (no governed monitor)",
                verdict.detail.get("datastream_id"),
            )
        return verdict

    outcome = _OUTCOME_FOR_VERDICT.get((verdict.status, verdict.fired), "unverifiable")
    written = dq_null_rate.record_verdict(
        project_id=str(verdict.detail.get("project_id") or ""),
        monitor_id=str(monitor["monitor_id"]),
        monitor_version_id=str(monitor["monitor_version_id"]),
        datastream_id=str(verdict.detail.get("datastream_id") or ""),
        execution_id=verdict.detail.get("execution_id"),
        window_date=window_date,
        outcome=outcome,
        observed={
            key: verdict.detail.get(key)
            for key in ("relation", "reason", "required_fields", "row_count", "findings")
            if verdict.detail.get(key) is not None
        },
        findings=findings,
    )
    verdict.detail["outcome"] = outcome
    verdict.detail["evaluation_id"] = written.get("evaluation_id")
    verdict.detail["issues"] = [issue.get("id") for issue in written.get("issues") or []]
    return verdict


# ---------------------------------------------------------------------------
# (h) Zero-row monitor (Story 59.4, epic 59) -- an empty window on a source that
# has been producing.
#
# The activity rule, the window naming and the governance bridge live in
# `core.dq_zero_rows`; what is here is the monitor's two faces, exactly as
# `null_rate` has them: the boolean the nightly sweep speaks, and the three-way
# verdict a governed evaluation needs so that "nothing to measure" never renders
# as "passed".
# ---------------------------------------------------------------------------


def _local_today(now_utc: datetime | None) -> date:
    """Today in the scheduling timezone -- the reference the dispatch counts from.

    `scheduler.py:1332` takes the PROJECT's yesterday; this module has always used
    the deployment's `SCHEDULER_TIMEZONE` instead, and story 59.4 does not widen
    itself to repair that. What it does repair is the day it counts back FROM
    being UTC while the dispatch's is local.
    """
    now = now_utc or datetime.now(tz=timezone.utc)
    try:
        from zoneinfo import ZoneInfo  # noqa: PLC0415

        return now.astimezone(ZoneInfo(_scheduler_tz())).date()
    except Exception:  # noqa: BLE001 -- an unknown zone is not a reason to answer nothing
        return now.date()


def _check_zero_rows(
    ds: dict,
    conn,
    window_date: date,
    now_utc: datetime | None = None,
    min_active: int | None = None,
) -> CheckVerdict:
    """Did this Datastream's window return nothing where the source has been producing?

    THE FUNCTION THAT RUNS. `CHECK_PROFILES["zero_rows"]` reaches it through
    `_adapt_zero_rows`, exactly as its siblings are reached through theirs, so the
    per-flux isolation stated here is the isolation the nightly sweep actually
    gets. It answers a :class:`CheckVerdict` rather than a bare `bool` because a
    bool cannot tell "measured and fine" from "not enough history to judge".

    It NEVER raises: one flux must not take the sweep down
    (`_run_monitors_for_datastream`), and an exception is answered as
    `unavailable` -- never swallowed into a pass on an unreadable ledger.
    """
    try:
        return _zero_rows_verdict(ds, conn, window_date, now_utc, min_active)
    except Exception as exc:  # noqa: BLE001 -- per-flux isolation, stated in the contract
        from core import dq_zero_rows  # noqa: PLC0415

        logger.warning("dq_monitors: zero_rows_failed ds=%s: %s", ds.get("id"), exc)
        return CheckVerdict(
            False,
            STATUS_UNAVAILABLE,
            {
                "datastream_id": ds.get("id"),
                "reason": dq_zero_rows.LEDGER_UNREADABLE,
                "message": dq_zero_rows.message_for(dq_zero_rows.LEDGER_UNREADABLE),
                "error": f"check_raised:{type(exc).__name__}",
            },
        )


def _zero_rows_verdict(
    ds: dict,
    conn,
    window_date: date,
    now_utc: datetime | None = None,
    min_active: int | None = None,
) -> CheckVerdict:
    """The body of :func:`_check_zero_rows`, which owns the isolation."""
    from core import dq_zero_rows  # noqa: PLC0415

    ds_id = str(ds.get("id") or "")
    project_id = str(ds.get("project_id") or "")
    day = window_date.isoformat()

    # 1. A day the dispatch never asked for. `epic-59:121-123` forbids firing on a
    # window legitimately empty because of `window_offset_days`, and the guard is
    # the SAME helper `_check_timeliness` reads -- one rule, two checks. No
    # governed object is derived here: nothing was measured, so there is nothing
    # for a monitor to assert.
    last_fetchable = effective_window_end(ds, _local_today(now_utc))
    if window_date > last_fetchable:
        return CheckVerdict(
            False,
            STATUS_NOT_APPLICABLE,
            {
                "datastream_id": ds_id,
                "project_id": project_id,
                "window_date": day,
                "reason": dq_zero_rows.INSIDE_EXTRACTION_OFFSET,
                "message": dq_zero_rows.message_for(dq_zero_rows.INSIDE_EXTRACTION_OFFSET),
                "window_offset_days": window_offset_days(ds),
                "last_fetchable_date": last_fetchable.isoformat(),
            },
        )

    entries = dq_zero_rows.read_window(conn, datastream_id=ds_id, window_date=window_date)
    judged = next(
        (entry for entry in entries if str(entry.get("date")) == day), None
    )
    evidence = dq_zero_rows.activity_evidence(entries, window_date, min_active=min_active)
    detail: dict[str, Any] = {
        "datastream_id": ds_id,
        "project_id": project_id,
        "window_date": day,
        **evidence,
    }

    if judged is None:
        # An empty ledger read: never a firing, and never a pass either.
        detail.update(
            {
                "reason": dq_zero_rows.NO_LEDGER_DAY,
                "message": dq_zero_rows.message_for(dq_zero_rows.NO_LEDGER_DAY),
            }
        )
        return CheckVerdict(False, STATUS_NOT_APPLICABLE, detail)

    if not evidence["collected_days"]:
        # Not eligible: nothing has ever landed, so no governed object is derived.
        # This is what bounds the registry to the Datastreams that have collected.
        detail.update(
            {
                "reason": dq_zero_rows.NO_LEDGER_HISTORY,
                "message": dq_zero_rows.message_for(dq_zero_rows.NO_LEDGER_HISTORY),
            }
        )
        return CheckVerdict(False, STATUS_NOT_APPLICABLE, detail)

    # From here the Datastream CAN be evaluated, so it carries a monitor -- watched
    # and quiet has to be tellable from watched by nobody (`epic-59:114-115`).
    monitor = dq_zero_rows.derive_monitor(
        org_id=str(ds.get("org_id") or ""),
        project_id=project_id,
        datastream_id=ds_id,
        datastream_name=str(ds.get("name") or ds_id),
    )

    status = str(judged.get("status") or "")
    pull_id = judged.get("pull_id")
    window_start, window_end = dq_zero_rows.pull_window(entries, pull_id)
    detail.update(
        {
            "status": status,
            "pull_id": pull_id,
            # Arbitrage 4: the ledger entry ALREADY carries the run
            # (`extract_ledger.py:469,473`), so resolving it costs no query.
            "execution_id": judged.get("execution_id"),
            "window_start": window_start or day,
            "window_end": window_end or day,
        }
    )

    if status != dq_zero_rows.EMPTY_STATUS:
        if status in ("ok", "partial"):
            # Measured, and the window carried rows. The only pass this check has.
            return _zero_rows_recorded(
                CheckVerdict(False, STATUS_EVALUATED, detail), ds, monitor
            )
        # `never_fetched`, `failed`, `running` -- and `cancelled` / `superseded`,
        # which the ledger reports as `never_fetched` (`pull_job_states.py:94-116`).
        # A stream a person stopped is not a source anomaly.
        detail.update(
            {
                "reason": dq_zero_rows.NOT_A_COLLECTED_DAY,
                "message": dq_zero_rows.message_for(dq_zero_rows.NOT_A_COLLECTED_DAY),
            }
        )
        return _zero_rows_recorded(
            CheckVerdict(False, STATUS_NOT_APPLICABLE, detail), ds, monitor
        )

    if evidence["active_days"] < evidence["min_active_days"]:
        # Empty, but the source has not proven it produces. `not_applicable`
        # NAMING the number of days found -- never a pass, never a firing.
        detail.update(
            {
                "reason": dq_zero_rows.NOT_HISTORICALLY_ACTIVE,
                "message": dq_zero_rows.message_for(dq_zero_rows.NOT_HISTORICALLY_ACTIVE),
            }
        )
        return _zero_rows_recorded(
            CheckVerdict(False, STATUS_NOT_APPLICABLE, detail), ds, monitor
        )

    from core import infra_alerts  # noqa: PLC0415

    ds_name = str(ds.get("name") or ds_id)
    span = (
        day
        if detail["window_start"] == detail["window_end"]
        else f"{detail['window_start']}..{detail['window_end']}"
    )
    infra_alerts.write_infra_firing(
        alert_type=dq_zero_rows.ALERT_TYPE,
        project_id=project_id,
        metric="row_count",
        severity="warning",
        message=(
            f"Zero rows: the window {span} of '{ds_name}' returned nothing, "
            f"while {evidence['active_days']} days carried rows in the last "
            f"{evidence['lookback_days']}"
        ),
        # Arbitrage 5, and the four values this writer used to invent: the window
        # the finding speaks of, the rows it returned, the count below which it
        # fires, and the pull that measured it.
        observed_value=0,
        threshold=1,
        pull_ids=[pull_id] if pull_id else (),
        window_date=window_date,
        # THE MEASUREMENT, NEVER THE ROWS -- and here there are none by nature, as
        # for `dq_timeliness`. What makes the zero anomalous is the evidence.
        metadata={
            "datastream_id": ds_id,
            "datastream_name": ds_name,
            "module_name": ds.get("module_name"),
            "window_date": day,
            "window_start": detail["window_start"],
            "window_end": detail["window_end"],
            "pull_id": pull_id,
            "execution_id": detail["execution_id"],
            "active_days": evidence["active_days"],
            "active_dates": evidence["active_dates"],
            "min_active_days": evidence["min_active_days"],
            "lookback_days": evidence["lookback_days"],
        },
    )
    logger.info(
        "dq_monitors: zero_rows_firing ds=%s date=%s pull=%s active_days=%d execution=%s",
        ds_id,
        day,
        pull_id,
        evidence["active_days"],
        detail["execution_id"],
    )
    return _zero_rows_recorded(CheckVerdict(True, STATUS_EVALUATED, detail), ds, monitor)


def _zero_rows_recorded(
    verdict: CheckVerdict, ds: Mapping, monitor: Mapping | None
) -> CheckVerdict:
    """Write the night's evaluation and the issue that points at it, then answer.

    On EVERY outcome and not only on a firing, for the reason `open_issue`
    documents: the issue's run moves to the night that saw the anomaly last, and
    the append-only evaluation of the earlier window is the only thing that keeps
    the earlier run.
    """
    from core import dq_zero_rows  # noqa: PLC0415

    if not monitor:
        if verdict.fired:
            logger.warning(
                "dq_zero_rows: firing_without_issue ds=%s (no governed monitor)",
                verdict.detail.get("datastream_id"),
            )
        return verdict

    outcome = _OUTCOME_FOR_VERDICT.get((verdict.status, verdict.fired), "unverifiable")
    detail = verdict.detail
    written = dq_zero_rows.record_verdict(
        project_id=str(detail.get("project_id") or ""),
        monitor_id=str(monitor["monitor_id"]),
        monitor_version_id=str(monitor["monitor_version_id"]),
        datastream_id=str(detail.get("datastream_id") or ""),
        execution_id=detail.get("execution_id"),
        window_start=date.fromisoformat(str(detail.get("window_start") or detail["window_date"])),
        window_end=date.fromisoformat(str(detail.get("window_end") or detail["window_date"])),
        outcome=outcome,
        observed={
            key: detail.get(key)
            for key in (
                "status",
                "reason",
                "pull_id",
                "active_days",
                "active_dates",
                "min_active_days",
                "lookback_days",
            )
            if detail.get(key) is not None
        },
        fired=verdict.fired,
    )
    detail["outcome"] = outcome
    detail["evaluation_id"] = written.get("evaluation_id")
    detail["issues"] = [issue.get("id") for issue in written.get("issues") or []]
    return verdict


# ---------------------------------------------------------------------------
# (f) Geography monitor (Story 37.9) -- unresolved country vocabulary.
#
# PROJECT-scoped, not datastream-scoped: the country partition is read once per
# project (posture is a project aggregate), so this monitor runs outside the
# per-datastream loop. It closes the gap the spec names explicitly -- an unmapped
# country value must reach the DQ supervision surfaces (Epic 13 monitors and
# get_data_quality_report), not only the report envelope's alert list.
# ---------------------------------------------------------------------------


def _geography_window_days() -> int:
    try:
        return max(1, int(os.environ.get("DQ_GEOGRAPHY_WINDOW_DAYS", "7")))
    except (ValueError, TypeError):
        return 7


def _check_geography(project_id: str, conn, yesterday: date) -> bool:
    """Evaluate unresolved geography for one project. Returns True when it fired.

    Only Local-markets projects are evaluated: a Global project makes no
    geographic promise, so an unmapped country value is not a gap against
    anything it published. Never raises (per-project isolation).

    Story 59.4 deliberately leaves this one on the deployment's `yesterday`: the
    window is PROJECT-scoped and spans Datastreams that may each carry a different
    `window_offset_days`, so there is no single last-fetchable day to shift to. It
    reads what the warehouse HOLDS over a window rather than asking whether one day
    arrived, so a day still inside an offset simply contributes no rows -- it cannot
    produce the false firing :func:`effective_window_end` exists to prevent.
    """
    # Story 37.9: the GOVERNED geography, not `project_preferences`. This read used
    # `fetch_project_geographic_posture`, and the early return below is what made it
    # dangerous: a Project governed through the ratified Country capability has an
    # EMPTY preference row, so `mode` was Global and this monitor NEVER RAN -- no
    # evidence at all for unmapped provider spellings, for exactly the Projects that
    # published a Country meaning to compare them against. A monitor that is silent
    # because it read the wrong authority is worse than no monitor.
    from core.country_activation import governed_posture  # noqa: PLC0415
    from core.geographic_reporting import LOCAL_MARKETS  # noqa: PLC0415

    posture = governed_posture(conn, project_id=project_id)
    if posture.mode != LOCAL_MARKETS:
        return False

    from core import warehouse  # noqa: PLC0415
    from core.geographic_conformance import (  # noqa: PLC0415
        aggregate_unmapped_evidence,
        emit_country_dq_firing,
        make_country_resolver,
        register_unmapped_country_values,
    )
    from core.geographic_semantics import COUNTRY_PARTITION  # noqa: PLC0415

    window_start = (yesterday - timedelta(days=_geography_window_days() - 1)).isoformat()
    window_end = yesterday.isoformat()
    observed = warehouse.query_breakdown_values(
        project_id, COUNTRY_PARTITION, window_start, window_end
    )
    if not observed:
        return False

    resolver = make_country_resolver(project_id=project_id)
    evidence: list[dict] = []
    for row in observed:
        raw_value = row.get("breakdown_value")
        connector = row.get("connector") or ""
        if resolver(raw_value, connector) is not None:
            continue
        evidence.append(
            {
                "raw_value": raw_value,
                "connector": connector,
                "occurrences": int(row.get("row_count") or 1),
            }
        )
    if not evidence:
        return False

    items = aggregate_unmapped_evidence(evidence)
    counts = register_unmapped_country_values(project_id, items)
    fired = emit_country_dq_firing(project_id, items, window_date=window_end)
    logger.info(
        "dq_monitors: geography_firing project=%s distinct=%d proposed=%d unresolved=%d",
        project_id,
        len(items),
        counts.get("proposed", 0),
        counts.get("unresolved", 0),
    )
    return fired


# ---------------------------------------------------------------------------
# (j) Unresolved values monitor (AI-326) -- every armed dimension, on the delta.
#
# DATASTREAM-scoped and dispatched, which is exactly where it parts from (f): the
# country partition is a project aggregate, while an unresolved set is read per
# Datastream and per dimension its active mapping version declares.
#
# The memory, the arming and the firing live in `core.unresolved_values_monitor`;
# the reading lives in `core.unresolved_values_api` and is the SAME one screens S1
# and S2 render. What is here is only the verdict a dispatched check owes.
# ---------------------------------------------------------------------------


def _check_unresolved_values(
    ds: Mapping[str, Any],
    conn,
    day: date,
    now_utc: datetime | None = None,
    reading: Mapping[str, Any] | None = None,
) -> CheckVerdict:
    """Announce the new unresolved values of one Datastream. Never raises.

    THREE OUTCOMES AND NOT TWO, for the reason the whole file is organised
    against:

    * `not_applicable` -- no dimension is mapped yet, or none of the mapped ones
      is ARMED (a value mapping table assigned to its field). Nothing was
      measured, and a green over nothing is what
      `ck_dq_evaluations_empty_is_not_a_pass` exists to make unstorable;
    * `unavailable`    -- the warehouse or the assignment store could not be read.
      "I could not look" is never "there is nothing";
    * `evaluated`      -- the reading happened. `fired` then says whether any
      group carried a value nobody has been told about yet.
    """
    from core import unresolved_values_monitor  # noqa: PLC0415

    # `day` IS this Datastream's last fetchable day (`effective_window_end`), and
    # `window_for` takes an INCLUSIVE end -- so the seven-day window the panel
    # renders and the one the monitor judges are the same seven days. Handing it
    # `day + 1` would make the alert speak of a day the dispatch never asked for.
    outcome = unresolved_values_monitor.sweep_datastream(
        conn, ds, today=day, reading=reading
    )
    status = {
        "evaluated": STATUS_EVALUATED,
        "not_applicable": STATUS_NOT_APPLICABLE,
        "unavailable": STATUS_UNAVAILABLE,
    }.get(str(outcome.get("status") or ""), STATUS_UNAVAILABLE)
    detail = {
        key: outcome.get(key)
        for key in (
            "datastream_id",
            "project_id",
            "reason",
            "fired",
            "armed",
            "recorded",
            "evaluated_dimensions",
            "threshold",
            "threshold_source",
        )
        if outcome.get(key) is not None
    }
    detail["window_date"] = day.isoformat()
    fired = int(outcome.get("fired") or 0)
    if fired:
        logger.info(
            "dq_monitors: unresolved_values_firing ds=%s groups=%d dimensions=%d",
            ds.get("id"),
            fired,
            int(outcome.get("evaluated_dimensions") or 0),
        )
    return CheckVerdict(bool(fired), status, detail)


# ---------------------------------------------------------------------------
# Per-datastream orchestrator
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# The single dispatch table. Both callers below go through it.
#
# Story 49.4 AC7 asks manual evaluation to use "the same evaluator as scheduled
# work". Sharing a module was not enough for that: the scheduled path called the
# five `_check_*` functions by name in a hand-written sequence, so a manual path
# would have had to write the same sequence a second time and could drift from it
# silently. One table, two callers, and a drift is impossible by construction.
#
# The keys are the `check_profile` values stored on `app.dq_monitor_versions`,
# and they are also the keys of the scheduled result dict -- deliberately the
# same strings, so a governed monitor and a scheduled run name the same check.
#
# `geography` is absent on purpose: it is evaluated per PROJECT, not per
# Datastream, and pretending otherwise here would give it the wrong denominator.
# ---------------------------------------------------------------------------


def _adapt_volume(
    ds: dict, conn, yesterday: date, now_utc: datetime | None, version: Mapping | None
) -> bool:
    # `ds` is handed over whole so the check can derive its governed monitor: the
    # `org_id` `app.dq_monitors` requires is on the row and nowhere else.
    return _check_volume(
        ds["id"], ds["project_id"], ds["module_name"], ds["name"], conn, yesterday, ds
    )


def _adapt_timeliness(
    ds: dict, conn, yesterday: date, now_utc: datetime | None, version: Mapping | None
) -> bool:
    return _check_timeliness(
        ds["id"],
        ds["project_id"],
        ds["module_name"],
        ds["name"],
        conn,
        yesterday,
        now_utc,
        # Story 59.4: the check reads the offset itself so a caller that never
        # went through the sweep -- the governed route, which answers about a
        # NAMED window -- cannot skip the guard.
        window_offset_days(ds),
        # Story 59.5: for the arrival branch, which derives a governed monitor.
        ds,
    )


def _adapt_duplication(
    ds: dict, conn, yesterday: date, now_utc: datetime | None, version: Mapping | None
) -> bool:
    return _check_duplication(
        ds["id"], ds["project_id"], ds["module_name"], ds["name"], yesterday
    )


def _adapt_schema(
    ds: dict, conn, yesterday: date, now_utc: datetime | None, version: Mapping | None
) -> bool:
    # The only adapter that reads its version, and the reason the parameter
    # exists: a drift check judged against a mutable side table is not a
    # governed check.
    columns = ((version or {}).get("baseline") or {}).get("columns")
    return _check_schema(
        ds["id"],
        ds["project_id"],
        ds["module_name"],
        ds["name"],
        conn,
        yesterday,
        list(columns) if isinstance(columns, list) else None,
        # The row travels so the sweep can freeze a FIRST observation as a
        # published version, which needs the owning org. Without it the check
        # would have to fall back to a store that needed no owner -- which is how
        # `app.dq_baselines` came to hold rows nothing could be pinned to.
        ds=ds,
    )


def _adapt_date_format(
    ds: dict, conn, yesterday: date, now_utc: datetime | None, version: Mapping | None
) -> bool:
    return _check_date_format(
        ds["id"], ds["project_id"], ds["module_name"], ds["name"], yesterday, ds.get("config")
    )


def _adapt_null_rate(
    ds: dict, conn, yesterday: date, now_utc: datetime | None, version: Mapping | None
) -> "CheckVerdict":
    """The required fields and the threshold, then the check.

    The threshold comes from the published version when there is one and from the
    environment otherwise, in that order: a governed monitor is judged against the
    policy it was published with, never against whatever the process happens to
    have in its environment tonight.
    """
    from core import dq_null_rate  # noqa: PLC0415

    thresholds = ((version or {}).get("parameters") or {}).get("thresholds") or {}
    declared = thresholds.get(dq_null_rate.CHECK_PROFILE)
    try:
        threshold = float(declared) if declared is not None else dq_null_rate.threshold_from_env()
    except (TypeError, ValueError):
        threshold = dq_null_rate.threshold_from_env()

    required = dq_null_rate.read_required_fields(
        conn, project_id=str(ds.get("project_id") or ""), datastream_id=str(ds.get("id") or "")
    )
    return _check_null_rate(ds, conn, yesterday, required, threshold)


def _adapt_zero_rows(
    ds: dict, conn, yesterday: date, now_utc: datetime | None, version: Mapping | None
) -> "CheckVerdict":
    """The activity threshold, then the check.

    The threshold comes from the published version when there is one and from the
    environment otherwise, in that order, exactly as `null_rate`'s does: a governed
    monitor is judged against the policy it was published with, never against
    whatever the process happens to have in its environment tonight.
    """
    from core import dq_zero_rows  # noqa: PLC0415

    thresholds = ((version or {}).get("parameters") or {}).get("thresholds") or {}
    declared = thresholds.get(dq_zero_rows.CHECK_PROFILE)
    try:
        required = int(float(declared)) if declared is not None else None
    except (TypeError, ValueError):
        required = None
    # Passed down, never written to the environment: the sweep walks every
    # Datastream in one process, and a published threshold parked in `os.environ`
    # would silently become the next Datastream's policy.
    return _check_zero_rows(ds, conn, yesterday, now_utc, min_active=required)


def _adapt_unresolved_values(
    ds: dict, conn, yesterday: date, now_utc: datetime | None, version: Mapping | None
) -> "CheckVerdict":
    """AI-326. No parameter is read from the version, and that is stated.

    The one threshold this monitor has -- the share of the window's rows the new
    values must carry -- is a PROJECT preference
    (`app.project_preferences.min_unresolved_new_value_row_share`, migration 326),
    not a per-monitor parameter, because the same declared cost governs every
    Datastream of a project. Reading it from a published version instead would
    give one project two answers to "what is worth waking somebody for".
    """
    return _check_unresolved_values(ds, conn, yesterday, now_utc)


#: The adapter of each check profile. The KEYS of the dispatch table are NOT
#: written here: they are `dq_monitor_registry.DISPATCHED_KEYS`, so a monitor
#: cannot be dispatched by one vocabulary and unknown to the other. A dispatched
#: entry with no adapter is a build defect, and it is loud at import rather than
#: silent at 02:00.
_ADAPTERS: dict[str, Callable[[dict, Any, date, "datetime | None", "Mapping | None"], bool]] = {
    "volume": _adapt_volume,
    "timeliness": _adapt_timeliness,
    "duplication": _adapt_duplication,
    "schema": _adapt_schema,
    "date_format": _adapt_date_format,
    # Story 59.3. The SAME key as `controls_quality.DQ_CHECKS`: a profile in one
    # vocabulary and not the other is either unpublishable or `unverifiable`.
    "null_rate": _adapt_null_rate,
    # Story 59.4, and the same rule.
    "zero_rows": _adapt_zero_rows,
    # AI-326, and the same rule again.
    "unresolved_values": _adapt_unresolved_values,
}

CHECK_PROFILES: dict[
    str, Callable[[dict, Any, date, "datetime | None", "Mapping | None"], bool]
] = {key: _ADAPTERS[key] for key in dq_monitor_registry.DISPATCHED_KEYS}


def _run_monitors_for_datastream(
    ds: dict,
    conn,
    yesterday: date,
    now_utc: datetime | None = None,
) -> dict:
    """Run every profile of the dispatch table for one datastream. Never raises.

    Returns {volume, timeliness, duplication, schema, date_format, null_rate}
    (True = issue fired; `null_rate` answers a `CheckVerdict` whose truth value is
    the same firing question). A check that RAISED is reported as False here, which is
    the pre-existing behaviour and is why the governed path below reports it as
    `unavailable` instead: "the check could not run" is not "the check passed",
    and the scheduled summary has never been able to tell them apart.

    Story 59.4: the date handed to the checks is THIS Datastream's last fetchable
    day, not the deployment's `yesterday`. The shift lives here, once, because the
    defect is the class's and not one check's: the others recompute nothing and
    simply trust the date they are given.
    """

    # The sweep is handed `yesterday`; `effective_window_end` speaks in `today`
    # because `today` is the reference `scheduler.py:1335-1340` computes its own
    # `end_date` from. One vocabulary is worth one line of arithmetic here.
    window_date = effective_window_end(ds, yesterday + timedelta(days=1))

    result = {name: False for name in CHECK_PROFILES}
    for name, check in CHECK_PROFILES.items():
        try:
            # No version: the scheduled sweep is not a governed monitor and
            # keeps the baseline behaviour it has always had.
            result[name] = check(ds, conn, window_date, now_utc, None)
        except Exception as exc:  # noqa: BLE001
            logger.warning("dq_monitors: %s_failed ds=%s: %s", name, ds["id"], exc)
    return result


def governed_evaluator(
    conn,
    *,
    project_id: str,
    monitor_id: str,
    monitor_version_id: str,
    window_start: date,
    window_end: date,
):
    """Evaluate ONE governed monitor. The callable Story 49.4's manual route passes.

    Returns ``(outcome, EvaluationCounts, dependency_refs, observed)`` -- the shape
    :func:`core.dq_governance.evaluate_monitor` writes as immutable evidence.

    Three distinctions the scheduled summary cannot express, and which exist here
    because a dashboard that cannot express them reports health it never measured:

    * **Nothing eligible** is ``not_applicable``, never a pass. A monitor whose
      target Datastream is disabled has measured nothing.
    * **A check that raised** counts as ``unavailable``, never as a pass.
    * **A profile this build does not implement** is ``unverifiable`` -- the
      monitor exists, and nothing here can answer for it.
    """

    from core.dq_governance import EvaluationCounts  # noqa: PLC0415

    with conn.cursor() as cur:
        cur.execute(
            "SELECT m.target_kind, m.target_id, v.check_profile, v.baseline, v.parameters "
            "FROM app.dq_monitors m "
            "JOIN app.dq_monitor_versions v ON v.id = %s AND v.monitor_id = m.id "
            "WHERE m.id = %s AND m.project_id = %s",
            (monitor_version_id, monitor_id, project_id),
        )
        row = cur.fetchone()
    if row is None:
        return (
            "unverifiable",
            EvaluationCounts(total_eligible=0),
            {},
            {"reason": "monitor_version_not_found"},
        )
    target_kind, target_id, check_profile = row[0], row[1], row[2]
    version = {"baseline": row[3] or {}, "parameters": row[4] or {}}

    if target_kind != "datastream":
        return (
            "unverifiable",
            EvaluationCounts(total_eligible=0),
            {"target": {"kind": target_kind, "id": target_id}},
            {"reason": f"no evaluator is implemented for target kind {target_kind}"},
        )

    check = CHECK_PROFILES.get(check_profile)
    eligible = [ds for ds in _fetch_enabled_datastreams(conn, project_id) if ds["id"] == target_id]
    dependency_refs = {
        "target": {"kind": target_kind, "id": target_id},
        "check_profile": check_profile,
    }

    if check is None:
        return (
            "unverifiable",
            EvaluationCounts(total_eligible=len(eligible)),
            dependency_refs,
            {"reason": f"no evaluator is implemented for check profile {check_profile}"},
        )
    if not eligible:
        # NOT a pass: the target is absent or disabled, so nothing was measured.
        return (
            "not_applicable",
            EvaluationCounts(total_eligible=0),
            dependency_refs,
            {"reason": "the target Datastream is absent or not enabled"},
        )

    passed = failed = unavailable = skipped = 0
    errors: list[str] = []
    notes: list[dict] = []
    for ds in eligible:
        try:
            # NOT shifted by `window_offset_days`, unlike the scheduled sweep: this
            # window was NAMED by the caller and is echoed verbatim in `observed`
            # below. Moving it here would answer about a day nobody asked for while
            # reporting the day they did. The sweep shifts because it invents its own
            # date; this path never invents one.
            verdict = check(ds, conn, window_end, None, version)
        except Exception as exc:  # noqa: BLE001 -- an unrunnable check is not a pass
            unavailable += 1
            errors.append(f"{ds['id']}: {type(exc).__name__}")
            continue
        # A check that answers a plain `bool` says `evaluated` by omission, which
        # is what the five older profiles have always meant.
        status = getattr(verdict, "status", STATUS_EVALUATED)
        detail = getattr(verdict, "detail", None)
        if detail:
            # EVERY verdict that carries a detail leaves a note, firing or not:
            # `dq_governance._located_target` reads the run out of these notes to
            # fill migration 222's evaluation half, and a note written only on the
            # quiet outcomes would have left the FAILING window -- the one that
            # matters -- without its run.
            notes.append({"datastream_id": ds["id"], "status": status, **detail})
        if status == STATUS_UNAVAILABLE:
            # NOT a pass: the relation could not be read, and saying "fine" about
            # something nobody could look at is the failure this whole module is
            # organised against.
            unavailable += 1
            if not detail:
                notes.append({"datastream_id": ds["id"], "status": status})
        elif status == STATUS_NOT_APPLICABLE:
            # NOT a pass either, and not a failure: nothing eligible was measured.
            skipped += 1
            if not detail:
                notes.append({"datastream_id": ds["id"], "status": status})
        elif verdict:
            failed += 1
        else:
            passed += 1

    counts = EvaluationCounts(
        total_eligible=len(eligible),
        evaluated=passed + failed,
        passed=passed,
        failed=failed,
        unavailable=unavailable,
    )
    if failed:
        outcome = "fail"
    elif passed:
        outcome = "pass"
    elif skipped and not unavailable:
        # Every eligible target answered "nothing to measure". `not_applicable` is
        # the only outcome the schema lets a zero-denominator verdict carry.
        outcome = "not_applicable"
    else:
        outcome = "unverifiable"
    observed = {
        "window": [window_start.isoformat(), window_end.isoformat()],
        "errors": errors,
        # Bounded and NOT the rows themselves: a reason per target, which is what
        # tells `unavailable` apart from `not_applicable` after the fact.
        "notes": notes,
    }
    return outcome, counts, dependency_refs, observed


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def run_dq_monitors(
    project_id: str | None = None,
    as_of_date: date | None = None,
    now_utc: datetime | None = None,
) -> dict:
    """Evaluate every per-Datastream DQ monitor for every enabled datastream.

    Args:
        project_id:  Optional scope to a single project (None = all projects).
        as_of_date:  Reference date (defaults to date.today()); yesterday = as_of_date - 1.
        now_utc:     Override current UTC time (for testing timeliness due logic).

    Returns:
        Summary dict: {
            evaluated: int,
            volume_issues: int,
            timeliness_issues: int,
            duplication_issues: int,
            schema_issues: int,
            date_format_issues: int,
            null_rate_issues: int,
            total_issues: int,
            errors: int,
        }

    Never raises. Respects DQ_MONITORS_ENABLED env var.
    """
    summary: dict = {
        "evaluated": 0,
        "volume_issues": 0,
        "timeliness_issues": 0,
        "duplication_issues": 0,
        "schema_issues": 0,
        "date_format_issues": 0,
        "null_rate_issues": 0,
        "zero_rows_issues": 0,
        "unresolved_values_issues": 0,
        "geography_issues": 0,
        "total_issues": 0,
        "errors": 0,
    }

    if not _dq_enabled():
        logger.debug("dq_monitors: skipped -- DQ_MONITORS_ENABLED not true")
        return summary

    if as_of_date is None:
        as_of_date = date.today()
    yesterday = as_of_date - timedelta(days=1)

    try:
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            datastreams = _fetch_enabled_datastreams(conn, project_id)

            if not datastreams:
                logger.info("dq_monitors: no_enabled_datastreams project_id=%s", project_id)
                return summary

            logger.info(
                "dq_monitors: starting: datastreams=%d yesterday=%s",
                len(datastreams),
                yesterday,
            )

            for ds in datastreams:
                try:
                    r = _run_monitors_for_datastream(ds, conn, yesterday, now_utc)
                    summary["evaluated"] += 1
                    if r["volume"]:
                        summary["volume_issues"] += 1
                    if r["timeliness"]:
                        summary["timeliness_issues"] += 1
                    if r["duplication"]:
                        summary["duplication_issues"] += 1
                    if r["schema"]:
                        summary["schema_issues"] += 1
                    if r["date_format"]:
                        summary["date_format_issues"] += 1
                    if r["null_rate"]:
                        summary["null_rate_issues"] += 1
                    if r["zero_rows"]:
                        summary["zero_rows_issues"] += 1
                    if r["unresolved_values"]:
                        summary["unresolved_values_issues"] += 1
                except Exception as exc:  # noqa: BLE001
                    summary["errors"] += 1
                    logger.warning(
                        "dq_monitors: ds_failed ds=%s: %s", ds.get("id"), exc
                    )

            # (f) geography -- PROJECT-scoped, one evaluation per distinct project.
            for evaluated_project in sorted(
                {str(ds.get("project_id")) for ds in datastreams if ds.get("project_id")}
            ):
                try:
                    if _check_geography(evaluated_project, conn, yesterday):
                        summary["geography_issues"] += 1
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "dq_monitors: geography_failed project=%s: %s",
                        evaluated_project,
                        exc,
                    )

    except Exception as exc:  # noqa: BLE001
        logger.warning("dq_monitors: run_failed: %s", exc)
        summary["errors"] += 1
        return summary

    summary["total_issues"] = (
        summary["volume_issues"]
        + summary["timeliness_issues"]
        + summary["duplication_issues"]
        + summary["schema_issues"]
        + summary["date_format_issues"]
        + summary["null_rate_issues"]
        + summary["zero_rows_issues"]
        + summary["unresolved_values_issues"]
        + summary["geography_issues"]
    )

    logger.info(
        "dq_monitors: complete: evaluated=%d total_issues=%d errors=%d",
        summary["evaluated"],
        summary["total_issues"],
        summary["errors"],
    )
    return summary
