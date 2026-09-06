"""toorow -- an evidence inspection is an observable act (Story 55.2, AC4/AC6).

WHAT THIS MODULE OWNS. One row of `app.evidence_inspections` (migration 175):
somebody, on one surface, opened one step of one walk, at one time, and was shown
one honest state. Which Result, which step, which actor, when -- the four facts
AC4 names, plus the state, because `mcp_app_behavior` judges honest states and a
recording that omits which state was shown cannot be graded on them.

WHY IT EXISTS AT ALL. Migration 168 recorded, on the constraint that keeps
`mcp_app_behavior` unable to pass, that no table in `app` records an interaction.
Story 55.2 ships an evidence drill-down -- exactly the object that dimension
judges. Shipping it without recording its use would have delivered the surface
and left it ungradable.

THREE REFUSALS, and each one is the whole point of a function below.

1. **Recording never breaks the display.** :func:`record_inspection` catches
   everything and returns ``None``. This is the rule `core.ai_path_recorder`
   already states -- *"evidence collection that can take down the thing it
   observes is a liability, not a control"* -- applied to the other direction of
   the same act. The screen that failed to record its own opening still opens.
2. **The vocabulary is closed, and it is validated OUT LOUD.**
   :func:`validate_inspection` RAISES. It is the seam a test pins and the seam a
   caller uses when it wants to know; :func:`record_inspection` calls it and
   swallows the refusal, so a bad call is a missing row and a warning, never a
   500 in front of a person. Two functions rather than a flag: a validator that
   sometimes raises is a validator nobody can rely on.
3. **An absence is never counted as a zero.** ``branches_listed`` may only
   accompany :data:`STATE_BRANCHES_LISTED`. The database enforces it too
   (`ck_evidence_inspections_absence_counts_nothing`); this module refuses first
   so the caller gets a named error instead of a constraint violation.

WHAT IS HERE SINCE 2026-08-17, AND WHAT IS STILL NOT. This module shipped with
"no read API, no aggregation, no verdict". The first two are now here, and the
third still is not.

The read exists because the table had TWO writers -- `ai_paths_api` for the
Console, `evidence_inspection_mcp` for the app -- and no `SELECT` anywhere in
production. `context-hub.md` refuses exactly that shape one notch later in its
life: *"what a retrieval discarded is ... kept and read by nothing -- a recurring
rejection that no surface shows is a missing link nobody can repair"*. A usage
signal written by two surfaces and read by none is the same defect, so
:func:`summarize_inspections` is the missing half.

It COUNTS; it does not GRADE. No verdict is formed here and none should be: Story
55.2 produces this dimension's inputs and does not score them -- an evaluator that
both emits and scores its own evidence is marking its own homework, and the story
text names that as epic 51's decision. The third refusal above therefore extends
to the read: an absence is never counted as a zero on the way OUT either, which is
what the four `displayed_state` buckets and the nullable total below are for.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# The closed vocabularies. They ARE the CHECK constraints of migration 175, and
# `test_evidence_inspections.py` reads the .sql file and compares, so a value
# added on one side without the other is red rather than silently unwritable.
# ---------------------------------------------------------------------------

KIND_SUBTREE_EXPANDED = "branch_subtree_expanded"
KIND_SUBTREE_COLLAPSED = "branch_subtree_collapsed"
KIND_DRILLDOWN_OPENED = "evidence_drilldown_opened"
KIND_TEXT_FALLBACK_SHOWN = "text_fallback_shown"

#: Every act this table can record. `text_fallback_shown` is in the set because a
#: host that could not mount the widget and fell back to text is a fact
#: `mcp_app_behavior` judges by name ("capability-safe host fallback"), and it is
#: unobservable if only successful mounts are recorded.
KINDS: tuple[str, ...] = (
    KIND_SUBTREE_EXPANDED,
    KIND_SUBTREE_COLLAPSED,
    KIND_DRILLDOWN_OPENED,
    KIND_TEXT_FALLBACK_SHOWN,
)

SURFACE_CONSOLE = "console"
SURFACE_MCP_APP = "mcp_app"
SURFACE_SHARE = "share"

#: The three surfaces that mount the shared visualization runtime. One table for
#: the three, because three tables would be three truths about one drawing.
SURFACES: tuple[str, ...] = (SURFACE_CONSOLE, SURFACE_MCP_APP, SURFACE_SHARE)

STATE_BRANCHES_LISTED = "branches_listed"
STATE_NO_BRANCH_JUDGED = "no_branch_judged"
STATE_BRANCHES_NOT_RECORDED = "branches_not_recorded"
STATE_UNAVAILABLE = "unavailable"

#: The honest states of a branch listing. The third is the one a boolean would
#: destroy: Story 54.2 emits the judged candidates on the live progress stream and
#: nothing stores them, so a reader of a persisted walk cannot know what was
#: judged. That is NOT "nothing was considered".
DISPLAYED_STATES: tuple[str, ...] = (
    STATE_BRANCHES_LISTED,
    STATE_NO_BRANCH_JUDGED,
    STATE_BRANCHES_NOT_RECORDED,
    STATE_UNAVAILABLE,
)

#: The only state a branch count may travel with. Mirrors
#: `ck_evidence_inspections_absence_counts_nothing`.
STATE_REQUIRING_COUNT = STATE_BRANCHES_LISTED

_MAX_REF_CHARS = 200

#: How many inspections ONE summary may look at. A count nobody bounded grows
#: with traffic on a table that only ever grows, and the first slow read of an
#: aggregate is the last time anybody runs it. The payload carries this number
#: AND says when it was reached, the shape `context_search.py` already reports as
#: `scan_truncated`: a bounded scan that stays silent reads as a complete one.
SUMMARY_SCAN_ROW_LIMIT = 5000

#: How many (walk, step) pairs the "most opened" facet may name. The question is
#: "which branches do people open MOST", not "list every step anyone touched" --
#: an unbounded facet would turn a usage summary into an evidence export.
SUMMARY_TOP_STEPS = 10


class InspectionRefused(ValueError):
    """The inspection could not be recorded as described."""

    code = "invalid_evidence_inspection"


def _ref(value: Any, label: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise InspectionRefused(f"{label} must be a non-empty string or None")
    text = value.strip()
    if len(text) > _MAX_REF_CHARS:
        raise InspectionRefused(f"{label} exceeds {_MAX_REF_CHARS} characters")
    return text


def validate_inspection(
    *,
    org_id: Any,
    project_id: Any,
    actor: Any,
    kind: Any,
    surface: Any,
    displayed_state: Any,
    branches_listed: Any = None,
    result_ref: Any = None,
    render_ref: Any = None,
    ai_path_id: Any = None,
    step_ordinal: Any = None,
) -> dict[str, Any]:
    """Return the normalised row, or raise :class:`InspectionRefused`.

    Pure: touches no database. It is what makes the vocabulary testable without a
    PostgreSQL, and what lets :func:`record_inspection` fail as a no-op rather
    than as a constraint violation the caller would have to interpret.
    """
    row: dict[str, Any] = {
        "org_id": _require(org_id, "org_id"),
        "project_id": _require(project_id, "project_id"),
        "actor": _require(actor, "actor"),
    }

    for label, value, vocabulary in (
        ("kind", kind, KINDS),
        ("surface", surface, SURFACES),
        ("displayed_state", displayed_state, DISPLAYED_STATES),
    ):
        if value not in vocabulary:
            raise InspectionRefused(
                f"{label}={value!r} is not one of {vocabulary}; the vocabulary is "
                "closed so a surface cannot invent a state it was never graded on"
            )
        row[label] = value

    row["result_ref"] = _ref(result_ref, "result_ref")
    row["render_ref"] = _ref(render_ref, "render_ref")
    row["ai_path_id"] = _ref(ai_path_id, "ai_path_id")

    if step_ordinal is None:
        row["step_ordinal"] = None
    else:
        if isinstance(step_ordinal, bool) or not isinstance(step_ordinal, int):
            raise InspectionRefused("step_ordinal must be an integer or None")
        if step_ordinal < 0:
            raise InspectionRefused("step_ordinal must be >= 0")
        if row["ai_path_id"] is None:
            # Mirrors `ck_evidence_inspections_step_needs_path`: a position with
            # no walk to index into is a dangling claim.
            raise InspectionRefused("step_ordinal names a step of no walk; ai_path_id is required")
        row["step_ordinal"] = step_ordinal

    if branches_listed is None:
        if row["displayed_state"] == STATE_REQUIRING_COUNT:
            raise InspectionRefused(
                f"displayed_state={STATE_REQUIRING_COUNT!r} must state how many "
                "branches were listed"
            )
        row["branches_listed"] = None
    else:
        if isinstance(branches_listed, bool) or not isinstance(branches_listed, int):
            raise InspectionRefused("branches_listed must be an integer or None")
        if branches_listed < 0:
            raise InspectionRefused("branches_listed must be >= 0")
        if row["displayed_state"] != STATE_REQUIRING_COUNT:
            raise InspectionRefused(
                f"branches_listed may only accompany {STATE_REQUIRING_COUNT!r}; a "
                f"count beside {row['displayed_state']!r} would assert an "
                "examination that did not happen"
            )
        row["branches_listed"] = branches_listed

    return row


def _require(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise InspectionRefused(f"{label} is required")
    text = value.strip()
    if len(text) > _MAX_REF_CHARS:
        raise InspectionRefused(f"{label} exceeds {_MAX_REF_CHARS} characters")
    return text


def _mint() -> str:
    from ulid import ULID  # noqa: PLC0415

    return f"evi_{ULID()}"


def insert_inspection(conn, **fields: Any) -> str:
    """Write one inspection and return its id. RAISES on refusal or on failure.

    The strict half. `record_inspection` is the one a display calls; this one is
    for a caller that genuinely wants to know whether the row landed -- a test,
    or a future evaluator harness.
    """
    row = validate_inspection(**fields)
    inspection_id = _mint()
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.evidence_inspections (
                id, org_id, project_id, result_ref, render_ref,
                ai_path_id, step_ordinal, kind, surface, displayed_state,
                branches_listed, actor
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                inspection_id,
                row["org_id"],
                row["project_id"],
                row["result_ref"],
                row["render_ref"],
                row["ai_path_id"],
                row["step_ordinal"],
                row["kind"],
                row["surface"],
                row["displayed_state"],
                row["branches_listed"],
                row["actor"],
            ),
        )
    return inspection_id


def record_inspection(conn, **fields: Any) -> str | None:
    """Write one inspection. Returns its id, or ``None``. NEVER RAISES.

    AC6: observing must not be able to take down the observed. Every failure --
    a refused vocabulary, a dead connection, a constraint violation, a table that
    does not exist yet on an un-migrated deployment -- is caught and logged, and
    the caller carries on rendering. There is no reraise flag: a switch that
    turns this into a raising function would eventually be left on.
    """
    try:
        return insert_inspection(conn, **fields)
    except Exception as exc:  # noqa: BLE001 -- observation never breaks the observed
        logger.warning(
            "evidence_inspections: not recorded (%s: %s)", type(exc).__name__, exc
        )
        return None


# ---------------------------------------------------------------------------
# The read. ONE statement, and it writes nothing.
# ---------------------------------------------------------------------------

#: Four facets over one bounded window, in one round trip. `context-hub.md`:
#: *"Measurement on this path is written in ONE statement. The cost of keeping it
#: is round-trips ... and a measurement that slows what it measures is a
#: measurement that ends up cut."* The window is materialised once and the three
#: aggregates read it, so the table is scanned once and the four numbers all
#: describe the SAME set of rows -- three separate statements would let the
#: buckets disagree with the total under concurrent writes.
#:
#: The keys are `kind`, `displayed_state` and `(ai_path_id, step_ordinal)`, and
#: that choice is measured rather than default: the Console posts NO `result_ref`
#: and NO `render_ref` (`AiPathPage.tsx`), so a facet on either would render empty
#: forever -- honest, and useless.
_SUMMARY_SQL = """
WITH windowed AS (
    SELECT kind, displayed_state, ai_path_id, step_ordinal
      FROM app.evidence_inspections
     WHERE project_id = %s
       AND occurred_at >= now() - (%s * INTERVAL '1 day')
     ORDER BY occurred_at DESC, id
     LIMIT %s
)
SELECT 'kind'::text AS facet, kind AS key_a, NULL::text AS key_b, COUNT(*) AS n
  FROM windowed GROUP BY kind
UNION ALL
SELECT 'displayed_state'::text, displayed_state, NULL::text, COUNT(*)
  FROM windowed GROUP BY displayed_state
UNION ALL
SELECT * FROM (
    SELECT 'step'::text, ai_path_id, step_ordinal::text, COUNT(*)
      FROM windowed
     WHERE ai_path_id IS NOT NULL AND step_ordinal IS NOT NULL
     GROUP BY ai_path_id, step_ordinal
     ORDER BY COUNT(*) DESC, ai_path_id, step_ordinal
     LIMIT %s
) AS most_opened
UNION ALL
SELECT 'scanned'::text, NULL::text, NULL::text, COUNT(*) FROM windowed
"""


def summarize_inspections(
    conn, *, project_id: str, days: int = 30, row_limit: int | None = None
) -> dict[str, Any]:
    """Which evidence branches people open most, and how often nothing was shown.

    THE PRODUCT QUESTION, and the only one this answers. It is a usage reading,
    not a verdict: nothing here decides whether a surface behaved well.

    THREE HONESTIES, each one a thing this could have got wrong.

    1. **The four displayed states stay four.** They are seeded at zero and
       returned whole, because the difference between them is the whole reason
       the column has four values: `no_branch_judged` is an honest zero, while
       `branches_not_recorded` and `unavailable` say *we cannot know what was
       judged*. Summing them into one "empty" number here would undo, on the way
       out, exactly what `ck_evidence_inspections_absence_counts_nothing` protects
       on the way in.
    2. **A total this could not prove is ``None``.** The scan is capped; when the
       cap bites, the number of inspections in the window is unknown and is
       returned as ``None`` rather than as the capped count, which would be a
       ceiling presented as a total. ``inspections_scanned`` stays an integer --
       it is what was actually read, and every bucket below is a count over
       exactly those rows.
    3. **It reads.** No INSERT, no UPDATE, no ``commit()``. The caller owns the
       transaction; a read that commits decides for a caller that did not ask.
    """
    days = max(1, min(int(days), 90))
    limit = (
        SUMMARY_SCAN_ROW_LIMIT
        if row_limit is None
        else max(1, min(int(row_limit), SUMMARY_SCAN_ROW_LIMIT))
    )
    project_id = _require(project_id, "project_id")

    with conn.cursor() as cur:
        cur.execute(_SUMMARY_SQL, (project_id, days, limit, SUMMARY_TOP_STEPS))
        rows = cur.fetchall()

    # Seeded from the vocabularies, not from what came back: a state absent from
    # the window is a state that was not shown, and it has to SAY so with a zero
    # rather than by missing from the payload -- a caller cannot tell a key that
    # is absent from a key that was never possible.
    kinds: dict[str, int] = dict.fromkeys(KINDS, 0)
    states: dict[str, int] = dict.fromkeys(DISPLAYED_STATES, 0)
    top_steps: list[dict[str, Any]] = []
    scanned = 0

    for facet, key_a, key_b, count in rows:
        if facet == "kind":
            # Assigned, never filtered against KINDS: a value the database holds
            # and this module does not know about is a drift to SEE, not to hide.
            kinds[str(key_a)] = int(count)
        elif facet == "displayed_state":
            states[str(key_a)] = int(count)
        elif facet == "step":
            top_steps.append(
                {
                    "ai_path_id": str(key_a),
                    "step_ordinal": int(key_b),
                    "inspections": int(count),
                }
            )
        elif facet == "scanned":
            scanned = int(count)

    truncated = scanned >= limit
    return {
        "window_days": days,
        "inspections_scanned": scanned,
        # The window held more than the scan could read, so its size is not a
        # thing this call knows. `None`, never the ceiling, and never 0.
        "inspections_total": None if truncated else scanned,
        "window_truncated": truncated,
        "scan_row_limit": limit,
        "kinds": kinds,
        "displayed_states": states,
        "top_steps": top_steps,
        "top_steps_limit": SUMMARY_TOP_STEPS,
    }


__all__ = [
    "DISPLAYED_STATES",
    "KINDS",
    "KIND_DRILLDOWN_OPENED",
    "KIND_SUBTREE_COLLAPSED",
    "KIND_SUBTREE_EXPANDED",
    "KIND_TEXT_FALLBACK_SHOWN",
    "STATE_BRANCHES_LISTED",
    "STATE_BRANCHES_NOT_RECORDED",
    "STATE_NO_BRANCH_JUDGED",
    "STATE_REQUIRING_COUNT",
    "STATE_UNAVAILABLE",
    "SUMMARY_SCAN_ROW_LIMIT",
    "SUMMARY_TOP_STEPS",
    "SURFACES",
    "SURFACE_CONSOLE",
    "SURFACE_MCP_APP",
    "SURFACE_SHARE",
    "InspectionRefused",
    "insert_inspection",
    "record_inspection",
    "summarize_inspections",
    "validate_inspection",
]
