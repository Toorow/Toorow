"""toorow -- Extract ledger: day-grain status per (datastream, date) (Story 8.3).

Provides:
  get_extract_ledger(datastream_id, date_from, date_to, conn) -> list[dict]

Each returned dict covers one calendar day:
  {
    "date":               "YYYY-MM-DD",
    "status":             "ok" | "partial" | "empty" | "failed" | "running" | "never_fetched",
    "row_count":          int | None,   -- rows landed ON THIS DAY, or None; see below
    "row_count_reason":   str | None,   -- why row_count is None, when it is
    "expected_rows":      int | None,   -- from manifest profile; None when not declared
    "completeness_ratio": float | None, -- actual/expected; None when expected unknown
    "pull_id":            str | None,   -- pull_id of the covering pull
    "loaded_at":          str | None,   -- ISO-8601 timestamp of pull completion
    "job_state":          str | None,   -- the covering window's own state (63.6 registry)
    "execution_id":       str | None,   -- the run that produced the covering window
    "extract_count":      int,          -- how many pulls of this stream cover this day
    "provenance":         str | None,   -- PROVENANCE_PRE_8_2 when served by the fallback
    # Story 25.2 (AC5): present ONLY on failed days (status == "failed").
    "error_class":        str | None,   -- canonical taxonomy class; None for legacy text
    "user_action":        str | None,   -- e.g. "reconnect"; None when not user-actionable
    # AI-307: present ONLY on a day whose window is `prevented`.
    "prevented_reason":   str | None,   -- the connector's machine token for the gate
    "prevented_message":  str | None,   -- the connector's sentence, naming the gesture
  }

`row_count` IS A DAY'S VOLUME OR IT IS NOTHING (story 58.1, arbitrage 9).
`pull_verifications.actual_rows` is counted PER PULL (`verification._count_raw_rows`
groups by `pull_id`, never by date), so it is the volume of the whole WINDOW. Until
2026-08-06 it was copied onto every day that window covered: a 30-day backfill
published its own total thirty times, and `CoverageStrip` displayed it as a daily
figure -- wrong by a factor equal to the width of the window, on every catch-up
this product has ever run. It is now published only when the window covers EXACTLY
ONE DAY, which is the only case where the two questions have the same answer.
Otherwise the day says `None` and NAMES why. Deriving a real per-day volume needs a
`GROUP BY date` on the `raw_*` relation, which no reader writes and which carries no
Datastream discriminator -- that is story 58.3's problem, not a number to guess here.

The rule covers `expected_rows` and `completeness_ratio` too, and it has to. All
three are written per `pull_id` by `verification.py`, so all three describe the
window. Repairing only `row_count` would leave a day of a 30-day backfill reporting
`row_count: null` next to `expected_rows: 9000` and `completeness_ratio: 1.0` -- the
same wrong number, twice, under names nobody had flagged. One gate, one reason,
`row_count_reason`, for the whole measured trio.

`job_state` TRAVELS WITH `status` and does not replace it (story 58.1, arbitrage 7).
`cancelled`, `superseded` and `prevented` all report `never_fetched` to the ledger,
which is the right day-grain answer -- the day was not fetched -- and the wrong
sentence for a person: a window somebody STOPPED, and a window the SOURCE refused,
both read as days nobody ever asked for. The screen needs both words. AI-307 is why
`prevented` joined that list rather than becoming a seventh day status: the day-grain
question ("was this day fetched?") has the same answer for all three, and the
question that differs ("why not, and what releases it?") is answered by `job_state`
plus the window's own sentence, not by the day.

Status derivation (AD-7-consistent, Refinement R1):
  For each calendar day D, find the LATEST pull_jobs row covering D
  (date_from <= D <= date_to) for the given datastream_id.

  Priority 1: pulls with matching datastream_id (post-8.2 mode).
  Priority 2 (pre-8.2 fallback): pulls where datastream_id IS NULL but
    connection_ref_id = ds.connection_ref_id AND date_from <= D AND date_to >= D.

  If a covering pull is found:
    - state IN ('queued', 'running')        -> status = 'running'
    - state IN ('failed', 'dead_letter')    -> status = 'failed'
      (unless a NEWER successful pull also covers D -- then use that)
    - state = 'done': join pull_verifications by pull_id:
        - verdict = 'ok'     -> status = 'ok'
        - verdict = 'partial' -> status = 'partial'
        - verdict = 'empty'  -> status = 'empty'
        - no verification yet -> status = 'ok' (conservative; verification pending)
  If no covering pull found at all: status = 'never_fetched'.

Design decisions:
  - We always prefer the LATEST pull (by enqueued_at DESC) covering the day.
    A re-fetch turning a failed pull into a successful one resolves to the new status.
  - expected_rows is read from pull_verifications.expected_rows (set at verification
    time from the manifest profile), NOT re-computed here. When the pull has no
    verification row (e.g., still running or verification failed silently),
    expected_rows and completeness_ratio are None.
  - This function issues a single batch query for the entire date range, not N+1
    per-day queries. The result is assembled in Python.

AD-2: no module-specific strings.
AD-5: caller must supply a project-scoped datastream_id (already enforced by the REST
      layer before calling this function).
ASCII-only log strings (AI-03).
"""

from __future__ import annotations

import json
import logging
from datetime import date, timedelta

from core import pull_job_states

logger = logging.getLogger(__name__)

#: Why a day carries no row count. Two distinct absences, never one silence:
#: nothing measured this pull at all, versus something measured it at a grain
#: coarser than the day being reported.
ROW_COUNT_MEASURED_PER_WINDOW = "measured_per_window"
ROW_COUNT_NOT_VERIFIED = "not_verified"

#: A day resolved through the pre-8.2 branch, where the covering pull carries no
#: `datastream_id` and was matched on the shared `connection_ref_id` instead
#: (story 58.1, arbitrage 5). Two Datastreams on one connection then claim the
#: same orphan pulls. The branch stays -- switching it off would erase days that
#: WERE collected -- so the ambiguity is stated on the row instead of guessed at.
PROVENANCE_PRE_8_2 = "pre_8_2_connection_fallback"


def _parse_error_class(error_detail) -> tuple[str | None, str | None]:
    """Extract (error_class, user_action) from a pull's error_detail (Story 25.2).

    Story 25.2 writes error_detail as a structured JSON envelope. Legacy pulls
    stored plain text (e.g. "upstream error"). Parse best-effort: try json.loads
    and read the two keys; on any failure (legacy text, malformed, non-dict)
    return (None, None). Never raises. Additive: the pair is only surfaced on
    failed days, existing keys are untouched.
    """
    if not error_detail or not isinstance(error_detail, str):
        return None, None
    try:
        parsed = json.loads(error_detail)
    except (ValueError, TypeError):
        return None, None
    if not isinstance(parsed, dict):
        return None, None
    return parsed.get("error_class"), parsed.get("user_action")

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def get_extract_ledger(
    datastream_id: str,
    date_from: str,
    date_to: str,
    conn,
) -> list[dict]:
    """Return day-grain extract ledger for *datastream_id* over [date_from, date_to].

    Args:
        datastream_id: The ds_<ULID> identifier (already project-scoped by caller).
        date_from:     Inclusive start date, YYYY-MM-DD.
        date_to:       Inclusive end date, YYYY-MM-DD.
        conn:          Open psycopg connection (not committed here).

    Returns:
        List of dicts, one per calendar day, ordered date ASC.
        See module docstring for field definitions.
    """
    try:
        d_from = date.fromisoformat(date_from)
        d_to = date.fromisoformat(date_to)
    except (ValueError, TypeError) as exc:
        logger.warning("extract_ledger: invalid_dates ds=%s: %s", datastream_id, exc)
        return []

    if d_to < d_from:
        return []

    # Build the list of calendar days.
    days: list[date] = []
    cur_d = d_from
    while cur_d <= d_to:
        days.append(cur_d)
        cur_d += timedelta(days=1)

    # ---------------------------------------------------------------------------
    # Step 1: Fetch the datastream's connection_ref_id for the pre-8.2 fallback.
    # ---------------------------------------------------------------------------
    connection_ref_id: str | None = None
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT connection_ref_id FROM app.datastreams WHERE id = %s",
                (datastream_id,),
            )
            row = cur.fetchone()
            if row:
                connection_ref_id = row[0]
    except Exception as exc:
        logger.warning(
            "extract_ledger: ds_lookup_failed ds=%s: %s", datastream_id, exc
        )

    # ---------------------------------------------------------------------------
    # Step 2: Batch-fetch all pull_jobs covering this date range for this
    # datastream (post-8.2) and also pre-8.2 pulls via connection_ref_id overlap.
    # Ordered enqueued_at DESC so we can pick the LATEST pull per day.
    # ---------------------------------------------------------------------------
    pulls: list[dict] = []
    try:
        # review-epic-8 CRITICAL: this is an OVERLAP test, not containment.
        # A pull overlaps the window when pull.date_from <= window_end (date_to)
        # AND pull.date_to >= window_start (date_from). The previous binding
        # (date_from<=date_from AND date_to>=date_to) required the pull to FULLY
        # CONTAIN the window, so any narrow nightly pull was excluded and every
        # multi-day ledger query returned 'never_fetched'.
        # Base query has 3 placeholders (datastream_id, window_end, window_start);
        # the UNION branch extends 3 more. The previous list carried 2 extra
        # window params, so the connection_ref path passed 8 params for 6
        # placeholders -> the query ALWAYS raised and the fail-soft path returned
        # 'never_fetched' for every day.
        params: list = [datastream_id, date_to, date_from]
        sql = """
            SELECT
                pj.id         AS job_id,
                pj.pull_id,
                pj.datastream_id,
                pj.connection_ref_id,
                pj.date_from,
                pj.date_to,
                pj.state,
                pj.execution_id,
                pj.completed_at,
                pj.enqueued_at,
                pj.error_detail,
                pv.verdict,
                pv.actual_rows,
                pv.expected_rows,
                pv.completeness_ratio
            FROM app.pull_jobs pj
            LEFT JOIN app.pull_verifications pv ON pv.pull_id = pj.pull_id
            WHERE pj.datastream_id = %s
              AND pj.date_from <= %s::date
              AND pj.date_to   >= %s::date
        """
        # Pre-8.2 fallback: also match by connection_ref_id when available.
        if connection_ref_id:
            sql += """
              UNION ALL
            SELECT
                pj.id         AS job_id,
                pj.pull_id,
                pj.datastream_id,
                pj.connection_ref_id,
                pj.date_from,
                pj.date_to,
                pj.state,
                pj.execution_id,
                pj.completed_at,
                pj.enqueued_at,
                pj.error_detail,
                pv.verdict,
                pv.actual_rows,
                pv.expected_rows,
                pv.completeness_ratio
            FROM app.pull_jobs pj
            LEFT JOIN app.pull_verifications pv ON pv.pull_id = pj.pull_id
            WHERE pj.datastream_id IS NULL
              AND pj.connection_ref_id = %s
              AND pj.date_from <= %s::date
              AND pj.date_to   >= %s::date
            """
            params.extend([connection_ref_id, date_to, date_from])
        sql += " ORDER BY enqueued_at DESC"

        with conn.cursor() as cur:
            cur.execute(sql, params)
            cols = [d[0] for d in cur.description]
            for row in cur.fetchall():
                rec: dict = {}
                for col, val in zip(cols, row):
                    if col in ("date_from", "date_to") and val is not None:
                        rec[col] = str(val)
                    elif col in ("completed_at", "enqueued_at") and val is not None:
                        rec[col] = val.isoformat()
                    else:
                        rec[col] = val
                pulls.append(rec)
    except Exception as exc:
        logger.warning(
            "extract_ledger: pulls_query_failed ds=%s: %s", datastream_id, exc
        )
        pulls = []

    # ---------------------------------------------------------------------------
    # Step 3: For each calendar day, find the latest pull covering it.
    # Prefer post-8.2 pulls (datastream_id matches) over pre-8.2 (fallback).
    # Within same tier, take the one with latest enqueued_at (list is DESC sorted).
    # ---------------------------------------------------------------------------

    def _covers(pull: dict, day: date) -> bool:
        try:
            pf = date.fromisoformat(pull["date_from"])
            pt = date.fromisoformat(pull["date_to"])
            return pf <= day <= pt
        except (ValueError, TypeError):
            return False

    result: list[dict] = []
    for day in days:
        # Find best pull: prefer datastream_id-matching first.
        best_pull: dict | None = None
        covering = 0
        for pull in pulls:
            if not _covers(pull, day):
                continue
            covering += 1
            if best_pull is None:
                best_pull = pull
                continue
            # Prefer datastream_id match over fallback pulls.
            current_is_ds = best_pull.get("datastream_id") == datastream_id
            candidate_is_ds = pull.get("datastream_id") == datastream_id
            if candidate_is_ds and not current_is_ds:
                best_pull = pull
            # Already DESC-sorted; first matching pull in same tier wins.

        result.append(
            _day_to_ledger_entry(
                day, best_pull, datastream_id=datastream_id, extract_count=covering
            )
        )

    return result


def execution_for_day(conn, datastream_id: str, day: date) -> str | None:
    """The run the LEDGER attributes *day* to, or `None`. ONE answer, for everyone.

    Story 59.4, arbitrage 4. "Which run saw this day" must have a single answer or
    two monitors of the same epic report two different runs for one anomaly, and
    the run panel of story 59.1 then reads two truths.

    THE ORDER IS THE LEDGER'S, `enqueued_at DESC` (:func:`get_extract_ledger`,
    step 2), and it is the order every screen already displays. Story 59.3 wrote a
    second one -- `completed_at DESC NULLS LAST, id DESC` -- which is populated on
    preprod (6 rows of 6) but NULL on the whole disposable base (130 of 130), so on
    the base its tests run against it degenerated to `id DESC` and proved less than
    it appeared to. Both now come through here.

    It costs no query a caller holding the day's ledger entry has not already paid:
    such a caller reads `entry["execution_id"]` directly, and this function exists
    for the ones that hold no entry.

    `None` is a real answer and is written as NULL. `fk_pull_jobs_execution`
    (`218:161-164`, `ON DELETE SET NULL`) guarantees a non-NULL id names a run that
    exists; what it does not guarantee is that the run belongs to the same
    Datastream and project, which is what `fk_dq_issues_execution` requires -- a
    caller writing an issue keeps its own failure path for that.
    """
    iso = day.isoformat()
    try:
        entries = get_extract_ledger(datastream_id, iso, iso, conn)
    except Exception as exc:  # noqa: BLE001 -- an unresolvable run is not a failure
        logger.warning(
            "extract_ledger: execution_unresolvable ds=%s day=%s: %s",
            datastream_id,
            iso,
            exc,
        )
        return None
    for entry in entries:
        execution_id = entry.get("execution_id")
        if execution_id:
            return str(execution_id)
    return None


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _window_days(pull: dict) -> int | None:
    """How many calendar days the covering pull ASKED FOR, or None if unreadable.

    This is the divisor between "a number measured about this day" and "a number
    measured about a fortnight that happens to contain this day".
    """
    try:
        pf = date.fromisoformat(str(pull.get("date_from")))
        pt = date.fromisoformat(str(pull.get("date_to")))
    except (ValueError, TypeError):
        return None
    span = (pt - pf).days + 1
    return span if span >= 1 else None


def _day_to_ledger_entry(
    day: date,
    pull: dict | None,
    *,
    datastream_id: str | None = None,
    extract_count: int = 0,
) -> dict:
    """Convert a (day, best covering pull) pair into a ledger row dict."""
    day_str = day.isoformat()

    if pull is None:
        return {
            "date": day_str,
            "status": "never_fetched",
            "row_count": None,
            "row_count_reason": None,
            "expected_rows": None,
            "completeness_ratio": None,
            "pull_id": None,
            "loaded_at": None,
            "job_state": None,
            "execution_id": None,
            "extract_count": 0,
            "provenance": None,
        }

    state: str = pull.get("state") or ""
    verdict: str | None = pull.get("verdict")
    actual_rows: int | None = pull.get("actual_rows")
    expected_rows: int | None = pull.get("expected_rows")
    completeness_ratio: float | None = pull.get("completeness_ratio")
    loaded_at: str | None = pull.get("completed_at")

    # Story 63.6: the mapping state -> day status is the REGISTRY's, not this
    # function's. Four names were listed here and everything else fell through an
    # `else` into `never_fetched` -- so `superseded` already reported a day as
    # never fetched by accident rather than by decision, and every state added to
    # the CHECK constraint would have joined it in silence.
    status = pull_job_states.ledger_status(state)
    if status == pull_job_states.LEDGER_FROM_VERDICT:
        # The only state that LANDED rows: what the day is worth is then a
        # measurement of those rows, read off the verification verdict.
        if verdict == "partial":
            status = "partial"
        elif verdict == "empty":
            status = "empty"
        else:
            # `ok`, or no verification row yet (still processing).
            status = "ok"

    # Cast completeness_ratio to float if it came as Decimal from psycopg.
    if completeness_ratio is not None:
        try:
            completeness_ratio = float(completeness_ratio)
        except (TypeError, ValueError):
            completeness_ratio = None

    # Story 58.1, arbitrage 9. `actual_rows` is the volume of the WINDOW; it is
    # this day's volume only when the window IS this day. See the module
    # docstring for the measurement that made this a repair and not an option.
    #
    # AND THE SAME LINE CARRIES `expected_rows` AND `completeness_ratio`. All
    # three are written per `pull_id` by `verification.py` and were copied onto
    # every day the window covered; repairing only `row_count` would leave a day
    # of a 30-day backfill saying `row_count: null` beside `expected_rows: 9000`
    # and `completeness_ratio: 1.0` -- a ratio of a window presented as a ratio of
    # a day, which is the SAME defect wearing two other names. One gate, one
    # reason, the whole measured trio.
    window_days = _window_days(pull)
    if actual_rows is None and expected_rows is None and completeness_ratio is None:
        row_count, row_count_reason = None, ROW_COUNT_NOT_VERIFIED
    elif window_days == 1:
        row_count, row_count_reason = actual_rows, (
            None if actual_rows is not None else ROW_COUNT_NOT_VERIFIED
        )
    else:
        row_count, row_count_reason = None, ROW_COUNT_MEASURED_PER_WINDOW
        expected_rows = None
        completeness_ratio = None

    entry = {
        "date": day_str,
        "status": status,
        "row_count": row_count,
        # One reason for the whole measured trio below: they share a grain and
        # they share the absence.
        "row_count_reason": row_count_reason,
        "expected_rows": expected_rows,
        "completeness_ratio": completeness_ratio,
        "pull_id": pull.get("pull_id"),
        "loaded_at": loaded_at,
        # The window's OWN state, beside the day-grain status derived from it.
        "job_state": state or None,
        "execution_id": pull.get("execution_id"),
        "extract_count": extract_count,
        "provenance": (
            PROVENANCE_PRE_8_2
            if datastream_id is not None
            and pull.get("datastream_id") != datastream_id
            else None
        ),
    }

    # Story 25.2 (AC5): failed days surface the canonical error_class + user_action
    # parsed from the covering pull's structured error_detail. Legacy plain-text
    # error_detail yields (None, None). Additive: only present on failed days.
    if status == "failed":
        error_class, user_action = _parse_error_class(pull.get("error_detail"))
        entry["error_class"] = error_class
        entry["user_action"] = user_action

    # AI-307 -- THE CONNECTOR'S OWN SENTENCE, CARRIED ONTO THE DAY.
    #
    # A prevented window reports `never_fetched` at the day grain (above), which
    # is the right day-grain answer and says nothing a person can act on: the
    # provider WAS asked and refused, and only a grant obtained at the provider
    # releases it. The sentence that names that gesture is written on the window
    # by `queue._execute_job` and had, when this state shipped, exactly one
    # reader -- the MCP diagnosis. Measured 2026-08-21: `grep -rn
    # "prevented_message" ui/ web/` returned 0. It travels here so the day grid,
    # which is the screen somebody opens over a missing day, can say it too.
    #
    # ADDITIVE AND STATE-SCOPED, like `error_class` above: these two keys exist
    # on a prevented day and nowhere else, so no other day carries a null pair a
    # screen would have to test before trusting.
    if state == pull_job_states.PREVENTED:
        from core.pull_envelope import prevented_pair  # noqa: PLC0415

        prevented_reason, prevented_message = prevented_pair(pull.get("error_detail"))
        entry["prevented_reason"] = prevented_reason
        entry["prevented_message"] = prevented_message

    return entry
