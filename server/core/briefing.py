"""toorow — Morning briefing builder (Story 6.7, AC2, AC3).

Pure function — no warehouse queries, no Postgres calls, no LLM calls.

The briefing builder receives pre-fetched data from the caller
(_run_due_briefings in scheduler.py) and returns the insights JSONB dict
(AC2 shape) ready for storage in app.morning_briefings.

This is the canonical implementation of the AD-1 "precomputed briefing"
pattern: nightly scheduler builds it once, get_daily_report serves it via
ONE SELECT from app.morning_briefings (zero warehouse calls on the hot path).

Step order (Story 6.7, Dev Notes):
    1. dispatch_nightly  (data pulls)
    2. _run_alert_check  (business thresholds)
    3. _run_anomaly_alert_check (anomalies)
    4. run_due_notebooks (scheduled notebooks)
    5. run_due_briefings ← this module is called by step 5 (always last)
"""

from __future__ import annotations

import logging
import time

from core import candidate_emission, candidate_fate

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# French metric labels (UX-DR10 — French-first headlines).
# ---------------------------------------------------------------------------

METRIC_LABELS_FR: dict[str, str] = {
    "clicks": "Clics",
    "impressions": "Impressions",
    "average_position": "Average position",
    "sessions": "Sessions",
    "active_users": "Utilisateurs actifs",
    "conversions": "Conversions",
    "cost": "Cost",
}


def _metric_label(metric: str) -> str:
    """Return the French label for *metric*, falling back to metric name as-is."""
    return METRIC_LABELS_FR.get(metric, metric)


def _direction(delta: float | None) -> str:
    """Return 'en baisse', 'en hausse', or '' when no delta is computable.

    Story 53.8 (AC2). This used to fall through to ``"en hausse"`` whenever the
    delta was unknown — a rise asserted from the absence of evidence, and it fired
    on EVERY anomaly on the real path, because the anomaly dicts carry
    ``expected_value`` and the builder was reading ``threshold``. With no
    computable delta there is no direction word in the sentence.
    """
    if delta is None:
        return ""
    return "en baisse" if delta < 0 else "en hausse"


def _format_value(value: float | None) -> str:
    """Return a human-readable string for a metric value."""
    if value is None:
        return "N/A"
    if value == int(value):
        return str(int(value))
    return f"{value:.1f}"


# ---------------------------------------------------------------------------
# Story 53.8 — CAV-14: what the prior term of the sentence IS.
#
# The arrow "1 000 → 700" can only be read as before → after. It is legitimate
# between two observations of the same kind measured the same way, and it is a
# false claim anywhere else. So the grammar is chosen by the NATURE of the prior
# term, not by the insight's label.
# ---------------------------------------------------------------------------

PRIOR_OBSERVATION = "observation"  # an earlier measurement -> a move, arrow allowed
PRIOR_THRESHOLD = "threshold"      # a configured policy    -> a breach, never an arrow
PRIOR_BASELINE = "baseline"        # a rolling mean         -> a gap, never an arrow


def _build_headline(
    insight_type: str,
    metric: str,
    connector: str,
    delta: float | None,
    delta_pct: str | None,
    value_curr: float | None,
    value_prev: float | None,
    prior_kind: str = PRIOR_OBSERVATION,
) -> str:
    """Build a French headline for one insight.

    Story 53.8 (AC1, AC2). ``prior_kind`` decides the grammar:

      * ``PRIOR_THRESHOLD``  — the observation, the threshold, and which side was
        crossed. No arrow and no direction word: nothing moved.
      * ``PRIOR_BASELINE``   — the observation against the expected value. A
        rolling mean is not an earlier observation either.
      * ``PRIOR_OBSERVATION``— a genuine period-over-period move: the arrow says
        exactly what happened, and the direction word is only added when a delta
        was actually computed.
    """
    label = _metric_label(metric)
    curr_str = _format_value(value_curr)
    prev_str = _format_value(value_prev)
    subject = f"{label} {connector.upper()}".strip() if connector else label

    if prior_kind == PRIOR_THRESHOLD:
        if value_prev is None:
            return f"{subject} : {curr_str} observé, seuil non communiqué"
        if delta is None:
            return f"{subject} : {curr_str} observé, seuil {prev_str}"
        side = "sous le seuil" if delta < 0 else "au-dessus du seuil"
        return f"{subject} : {curr_str} observé, {side} {prev_str}"

    if prior_kind == PRIOR_BASELINE:
        if value_prev is None:
            return f"Anomaly: {subject} : {curr_str} observé, baseline indisponible"
        return f"Anomaly: {subject} : {curr_str} observé, baseline attendue {prev_str}"

    # PRIOR_OBSERVATION — two observations of the same kind: the arrow is honest.
    direction = _direction(delta)
    if delta_pct is not None:
        pct_str = delta_pct.lstrip("+-").rstrip("%")
        try:
            pct_num = float(pct_str)
        except (ValueError, TypeError):
            pct_num = None
    else:
        pct_num = None

    move = f"({prev_str} → {curr_str})"
    if direction and pct_num is not None:
        return f"{subject} {direction} de {pct_num:.0f} % {move}"
    if direction:
        return f"{subject} {direction} {move}"
    return f"{subject} : {prev_str} → {curr_str}"


def _build_citation(connector: str, metric: str, pull_ids: list[str]) -> str:
    """Build the provenance citation, or the explicit-absence marker (AC6 / AD-9).

    Story 53.8. ``pull_id = pull_ids[0] if pull_ids else "unknown"`` produced the
    token ``(connector:metric, unknown)`` — which reads like a citation and is
    not one. An anomaly firing carries ``pull_ids=[]`` BY DESIGN (a derived
    signal has no raw pull), so that fabricated token was the normal case, not
    the edge one.

    The absence marker is ``narrative``'s, imported rather than re-typed: the
    briefing and the report narrative cannot drift on how an absent provenance
    looks. ``_metric_citation`` also owns the tightness budget, so a long ULID
    truncates the same way on both surfaces.
    """
    from core.narrative import _metric_citation  # noqa: PLC0415

    return _metric_citation(
        {
            "source_system": connector,
            "source_field": metric,
            "pull_id": pull_ids[0] if pull_ids else None,
        }
    )


# ---------------------------------------------------------------------------
# Story 53.8 — CAV-15: a context event attached to a claim is context about THAT
# claim. The vocabulary below is the payload's, and it is shared with
# ``core.anomaly_alerts`` (the other half of the same class) so the two sides
# cannot describe the same pairing with two different words.
#
# What the schema actually supports, measured: ``app.context_events`` carries
# ``id, project_id, event_date, type, label, description, created_by, created_at``
# (migration 009) plus ``platform, value, source`` (migration 055) plus
# ``metric`` (migration 322). All three of the criterion's dimensions -- metric,
# connector, date -- are therefore comparable, and each of the three is compared
# only where BOTH sides name it: an event that names no metric is about every
# metric and stays admissible, and the payload then NAMES ``metric`` as a
# dimension it could not check rather than implying it did.
# ---------------------------------------------------------------------------

CONTEXT_BASIS_EXACT_DATE = "event_date == claim_date"
CONTEXT_BASIS_PLATFORM = "event.platform == claim.connector"

#: Migration 322. Read the same way as ``CONTEXT_BASIS_PLATFORM``: it appears in
#: ``basis`` when the attached event ACTUALLY named a metric and the claim did
#: too, never merely because the walk looked.
CONTEXT_BASIS_METRIC = "event.metric == claim.metric"
CONTEXT_DIM_METRIC = "metric"
CONTEXT_DIM_CONNECTOR = "connector"

#: AI-169 -- the day BOUNDARY, which this pairing can never check.
#:
#: `capabilities/reporting-timezone.md` is incomplete if "business events and measures
#: align on different undisclosed boundaries". This pairing is precisely where a business
#: event meets a measure, and it aligns them by STRING EQUALITY on a calendar date:
#:
#:   * an event's `event_date` carries no clock at all -- `app.context_events` validates a
#:     bare `YYYY-MM-DD` and stores no timezone column;
#:   * the claim's date is drawn on the SOURCE's reporting clock, which the platform now
#:     observes and records per run (AI-161).
#:
#: So "same day" here means "same calendar string", and the two days can begin hours
#: apart. That is not a defect to fix -- at DATE grain there is no sub-day data to
#: re-slice, and an event a human logged has no clock to recover. It is a fact to DISCLOSE,
#: which is the same posture the epic takes on cross-source offsets: signal, never realign.
#:
#: ALWAYS unscoped -- and now the only one that is. `metric` left this list on
#: 2026-08-30 (migration 322) because a column arrived to support it; the day
#: boundary has no such future: no column supports it on either side, so there is
#: no state of the data in which it could become checkable. A conditional would
#: suggest it is sometimes verified.
CONTEXT_DIM_DAY_BOUNDARY = "day_boundary"


def context_pairing_descriptor(
    *, platform_checked: bool, claim_date: str, metric_checked: bool = False
) -> dict:
    """Build the pairing descriptor carried on the payload (AC5).

    ``basis`` states what was actually compared; ``unscoped_dimensions`` states
    what could not be.

    ``metric`` moved from the second list to the first on 2026-08-30. Migration
    322 gave `app.context_events` a nullable `metric`, so the dimension is
    checkable -- but only when BOTH sides name one. An event that names ANOTHER
    metric is disqualified before this descriptor is ever built; an event that
    names NONE is about every metric, stays admissible, and leaves ``metric`` in
    ``unscoped_dimensions``, because nothing about it was compared. Reporting it
    as scoped there would be the defect this whole descriptor exists to prevent,
    moved one column to the right.

    ``day_boundary`` stays in the second list forever (AI-169): an event carries
    no clock and the claim's day is drawn on the source's, so "same day" here
    means "same calendar string" and the two days can begin hours apart.
    """
    basis = [CONTEXT_BASIS_EXACT_DATE]
    unscoped = [CONTEXT_DIM_DAY_BOUNDARY]
    if metric_checked:
        basis.append(CONTEXT_BASIS_METRIC)
    else:
        unscoped.insert(0, CONTEXT_DIM_METRIC)
    if platform_checked:
        basis.append(CONTEXT_BASIS_PLATFORM)
    else:
        unscoped.append(CONTEXT_DIM_CONNECTOR)
    return {
        "basis": basis,
        "unscoped_dimensions": unscoped,
        "claim_date": claim_date,
    }


#: The window form of ``CONTEXT_BASIS_EXACT_DATE``. A daily report's claim is not
#: about one day but about the window it covers, so the date basis it can honestly
#: state is containment in that window -- never "same day".
CONTEXT_BASIS_CLAIM_WINDOW = "event_date within claim_window"


def context_events_in_claim_scope(
    context_events: list[dict],
    *,
    start: str | None,
    end: str | None,
    metrics: list[str] | None = None,
    connectors: list[str] | None = None,
) -> tuple[list[dict], dict]:
    """The events admissible under a WINDOW claim, and the descriptor that says why.

    THE THIRD PROACTIVE PATH. ``core.narrative._why_lines`` renders a "Why"
    section directly under the claims of a daily report, and until 2026-08-30 it
    received `fetch_context_events(project, start, end)` whole: every annotation
    of the window, attached to every claim of the window, on no basis but
    co-occurrence in a date range. That is the same defect Story 53.8 repaired on
    the briefing and the anomaly evaluator -- the third instance of one class,
    and treating two of three is how a class survives.

    The rules are the per-claim walk's, widened to a window because the claim is:

    * **date** -- the event's day falls inside ``[start, end]``. An unreadable or
      absent day is out: a marker that is not about a day cannot be about this
      window either;
    * **metric** -- an event naming a metric the claim does not report is OUT
      (migration 322); one naming none is about every metric and stays;
    * **connector** -- an event declaring a platform none of the claim's
      connectors names is OUT; one declaring none stays.

    ``basis`` names only what was actually compared. A dimension the CLAIM does
    not name (a report with no connector filter, a rollup with no metrics) is not
    a dimension that passed -- it is one nobody could check, and it is listed in
    ``unscoped_dimensions``. So is a dimension the KEPT events all left blank:
    admissible is not the same as verified, and the descriptor is the only place
    a reader can tell them apart.
    """
    from datetime import date as _date  # noqa: PLC0415

    claim_metrics = {str(m).strip().lower() for m in (metrics or []) if str(m).strip()}
    claim_connectors = {
        str(c).strip().lower() for c in (connectors or []) if str(c).strip()
    }

    def _day(value):
        try:
            return _date.fromisoformat(str(value)[:10])
        except (ValueError, TypeError):
            return None

    first, last = _day(start), _day(end)
    kept: list[dict] = []
    metric_named = 0
    platform_named = 0
    for evt in context_events or []:
        evt_day = _day(evt.get("event_date") or evt.get("date"))
        if evt_day is None:
            continue
        if (first is not None and evt_day < first) or (last is not None and evt_day > last):
            continue
        evt_metric = str(evt.get("metric") or "").strip().lower()
        if evt_metric and claim_metrics and evt_metric not in claim_metrics:
            continue
        evt_platform = str(evt.get("platform") or "").strip().lower()
        if evt_platform and claim_connectors and evt_platform not in claim_connectors:
            continue
        kept.append(evt)
        metric_named += bool(evt_metric and claim_metrics)
        platform_named += bool(evt_platform and claim_connectors)

    metric_checked = bool(kept) and metric_named == len(kept)
    connector_checked = bool(kept) and platform_named == len(kept)

    basis = [CONTEXT_BASIS_CLAIM_WINDOW]
    unscoped = [CONTEXT_DIM_DAY_BOUNDARY]
    if metric_checked:
        basis.append(CONTEXT_BASIS_METRIC)
    else:
        unscoped.insert(0, CONTEXT_DIM_METRIC)
    if connector_checked:
        basis.append(CONTEXT_BASIS_PLATFORM)
    else:
        unscoped.append(CONTEXT_DIM_CONNECTOR)
    return kept, {
        "basis": basis,
        "unscoped_dimensions": unscoped,
        "claim_window": {"start": start, "end": end},
    }


# --- Story 54.2, Half B: the branches of the pairing, including those not taken.
#
# The pairing already judged every supplied event and kept one. It threw the
# other judgements away, which is the same defect Half A repaired on the
# knowledge-tree walk: "this event is the context" is indistinguishable from
# "this was the only event". So the judgement now RETURNS all of them, each with
# its enumerated fate, and `_find_context_event` reads the survivor off that ONE
# pass -- no second scan, no second result set.

#: What the pairing IS, stated as a field so a surface cannot draw a search that
#: did not happen. It is an equality join on a date, not a similarity, not a
#: window, and it walks no graph.
CONTEXT_PAIRING_MODE = "exact_date_join"

#: One event attaches to one claim. Events that qualified and lost to that cut
#: are reported as such -- they were judged, and saying so is the difference
#: between a cut and a silence.
CONTEXT_PAIRING_LIMIT = 1

#: What the subject of the crossing is called on the text channel.
CONTEXT_PAIRING_SUBJECT = "Business context pairing"

#: `metric_mismatch` was declared in the vocabulary and PRODUCED BY NOBODY until
#: migration 322 gave `app.context_events` a `metric` column. It is producible
#: HERE and only here, for an event that names a DIFFERENT metric than the claim;
#: an event naming none is not rejected at all, and the descriptor reports the
#: dimension as unscoped instead. Claiming the reason for an absent metric would
#: invent an examination, exactly as claiming it for an absent column did.
CONTEXT_EVENT_PRODUCIBLE_REASONS = candidate_fate.BRIEFING_CONTEXT_EVENT_REASONS


def context_event_walk(
    context_events: list[dict],
    claim_date: str | None,
    connector: str | None = None,
    metric: str | None = None,
) -> tuple[list[dict], dict | None, dict | None]:
    """Judge EVERY supplied event once. Return ``(candidates, pairing, walk)``.

    * ``candidates`` -- one dict per supplied event, in judged order, carrying its
      fate and, when rejected, an enumerated reason. Empty when the claim carries
      no usable date: nothing was judged, and reporting rejections for a pairing
      that never ran would invent an examination.
    * ``pairing`` -- the unchanged three-key payload descriptor of Story 53.8, or
      ``None`` when no event qualified.
    * ``walk`` -- the fuller descriptor the crossing and the text channel need:
      the pairing basis plus what the pairing IS and how many it judged.

    The rules are Story 53.8's, unchanged: the CLAIM's own date, exactly (never
    the briefing date, and no +/-7 day window -- proximity is not a
    relationship), and a declared ``platform`` that contradicts the claim's
    connector DISQUALIFIES the event, while a null one does not silently qualify
    it.

    ``metric`` is the third dimension of the same rule and reads exactly like the
    second (migration 322, 2026-08-30). An event that declares ANOTHER metric
    than the claim is disqualified with ``metric_mismatch``; an event that
    declares none is about every metric, stays admissible, and does not silently
    count as scoped -- ``metric_checked`` is carried per candidate and the
    descriptor reports the dimension as unscoped for the one that was kept.
    """
    if not context_events or not claim_date:
        return [], None, None

    from datetime import date as _date  # noqa: PLC0415

    try:
        target = _date.fromisoformat(str(claim_date)[:10])
    except (ValueError, TypeError):
        return [], None, None

    claim_connector = (connector or "").strip().lower()
    claim_metric = (metric or "").strip().lower()
    qualified: list[tuple[bool, bool, str, dict]] = []
    disqualified: list[tuple[str, str, dict]] = []

    for evt in context_events:
        evt_id = str(evt.get("id") or "")
        evt_date_raw = evt.get("event_date") or evt.get("date")
        evt_date = None
        if evt_date_raw:
            try:
                evt_date = _date.fromisoformat(str(evt_date_raw)[:10])
            except (ValueError, TypeError):
                evt_date = None
        if evt_date != target:
            # No date, an unreadable date, or another day: the one key the schema
            # genuinely supports did not match.
            disqualified.append((candidate_fate.REASON_DATE_MISMATCH, evt_id, evt))
            continue
        evt_metric = str(evt.get("metric") or "").strip().lower()
        if evt_metric and claim_metric and evt_metric != claim_metric:
            # It says which metric it is about, and it is not this one. Migration
            # 322 is what makes this branch reachable; before it, every event of
            # the day was admissible under every claim of the day.
            disqualified.append((candidate_fate.REASON_METRIC_MISMATCH, evt_id, evt))
            continue
        platform = str(evt.get("platform") or "").strip().lower()
        if platform and claim_connector and platform != claim_connector:
            disqualified.append((candidate_fate.REASON_CONNECTOR_MISMATCH, evt_id, evt))
            continue
        qualified.append(
            (
                bool(evt_metric and claim_metric),
                bool(platform and claim_connector),
                evt_id,
                evt,
            )
        )

    # Deterministic: the event whose dimensions were actually COMPARED outranks
    # the one that merely could not contradict the claim, the connector breaking
    # the tie between two events checked on one dimension each; ties break on the
    # event id so the same inputs always yield the same pairing. With no metric
    # anywhere the key collapses to Story 53.8's, so a pairing that held before
    # migration 322 holds after it.
    qualified.sort(
        key=lambda item: (-(int(item[0]) + int(item[1])), not item[1], item[2])
    )
    disqualified.sort(key=lambda item: (item[0], item[1]))

    candidates: list[dict] = []
    rank = 0
    for index, (metric_checked, platform_checked, evt_id, evt) in enumerate(qualified):
        rank += 1
        kept = index < CONTEXT_PAIRING_LIMIT
        candidates.append(
            _context_event_candidate(
                evt,
                evt_id,
                rank,
                fate=(
                    candidate_fate.FATE_SELECTED if kept else candidate_fate.FATE_REJECTED
                ),
                # Qualified, judged, and lost to the one-event cut -- the same
                # cause the capped tree walk names, and for the same reason.
                reason=None if kept else candidate_fate.REASON_BELOW_CUTOFF,
                platform_checked=platform_checked,
                metric_checked=metric_checked,
            )
        )
    for reason, evt_id, evt in disqualified:
        rank += 1
        candidates.append(
            _context_event_candidate(
                evt,
                evt_id,
                rank,
                fate=candidate_fate.FATE_REJECTED,
                reason=reason,
                platform_checked=False,
                metric_checked=False,
            )
        )

    pairing = None
    if qualified:
        pairing = context_pairing_descriptor(
            metric_checked=qualified[0][0],
            platform_checked=qualified[0][1],
            claim_date=target.isoformat(),
        )

    kept_count = min(len(qualified), CONTEXT_PAIRING_LIMIT)
    walk = dict(
        pairing
        or context_pairing_descriptor(platform_checked=False, claim_date=target.isoformat())
    )
    walk.update(
        {
            "mode": CONTEXT_PAIRING_MODE,
            "graph_hop_depth": 0,
            "semantic_recall": False,
            "limit": CONTEXT_PAIRING_LIMIT,
            # Events outside the fetch window were never judged. They are not
            # listed, and the payload says so rather than letting the absence
            # read as an exhaustive review of the project's history.
            "not_reached_enumerated": False,
            "reached_count": len(candidates),
            "selected_count": kept_count,
            "rejected_count": len(candidates) - kept_count,
        }
    )
    return candidates, pairing, walk


def _context_event_candidate(
    evt: dict,
    evt_id: str,
    rank: int,
    *,
    fate: str,
    reason: str | None,
    platform_checked: bool,
    metric_checked: bool = False,
) -> dict:
    """One judged event, in the shared candidate shape (`core.candidate_emission`).

    ``score``, ``tier`` and ``matched`` are ``None`` because the pairing has no
    such notion: it is an equality join, it does not rank and it walks no graph.
    ``None`` there means *not applicable*, never *zero* -- a zero score would
    invent a measurement nobody took. ``_event`` is carried for this module's own
    use and is ignored by the emitter, whose field set is closed.
    """
    candidate_fate.validate_fate(fate, reason)
    return {
        "id": evt_id or None,
        "kind": candidate_emission.OBJECT_TYPE_CONTEXT_EVENT,
        "title": evt.get("label"),
        "score": None,
        "tier": None,
        "matched": None,
        "rank": rank,
        "fate": fate,
        "reason": reason,
        "platform_checked": platform_checked,
        # Migration 322. Per candidate, like `platform_checked`: an event that
        # named no metric is admissible AND unscoped, and the two facts have to
        # stay distinguishable on the row a surface draws.
        "metric_checked": metric_checked,
        "_event": evt,
    }


def emit_context_event_walk(candidates: list[dict], walk: dict | None) -> bool:
    """Post the pairing as ONE crossing on Story 54.1's seam. Never raises.

    Same seam as the knowledge-tree walk, same shape, same refusal to invent a
    reason -- the two halves of Story 54.2 do not get two channels.
    """
    if not walk:
        return False
    selected = next(
        (c for c in candidates if c.get("fate") == candidate_fate.FATE_SELECTED), None
    )
    return candidate_emission.emit_candidates(
        candidates=candidates,
        descriptor=walk,
        producer=candidate_emission.PRODUCER_BRIEFING_CONTEXT_EVENT,
        tool_name=candidate_emission.TOOL_BRIEFING_CONTEXT_EVENT,
        owner_object_type=candidate_emission.OBJECT_TYPE_CONTEXT_EVENT,
        owner_object_id=(selected or {}).get("id"),
    )


def context_event_text_line(candidates: list[dict], walk: dict | None) -> str:
    """The pairing on the text channel, as cited data (AC6/AC7).

    Composed by `candidate_emission.cited_fate_line` from constants, counts and
    enumerated reasons only: it states what was compared and what could not be,
    and it asserts nothing about why anything happened.
    """
    if not walk:
        return ""
    return candidate_emission.cited_fate_line(
        subject=CONTEXT_PAIRING_SUBJECT,
        descriptor=walk,
        candidates=candidates,
    )


def _find_context_event(
    context_events: list[dict],
    claim_date: str | None,
    connector: str | None = None,
    metric: str | None = None,
) -> tuple[dict | None, dict | None]:
    """Return ``(event, pairing)`` for ONE claim, or ``(None, None)``.

    Story 53.8, CAV-15 -- now a thin read over :func:`context_event_walk`, so the
    survivor a caller sees and the candidates a surface draws come out of the SAME
    judgement. A second scan here would be a second result set, which is the
    defect Half A refused on the tree walk.
    """
    candidates, pairing, _walk = context_event_walk(
        context_events, claim_date, connector, metric
    )
    for candidate in candidates:
        if candidate.get("fate") == candidate_fate.FATE_SELECTED:
            return candidate.get("_event"), pairing
    return None, None


# ---------------------------------------------------------------------------
# Story 53.8 — AC3: classify on a key the PRODUCERS emit.
#
# ``build_briefing`` classified on ``firing["type"]``. Measured at e21e718c:
# ``grep -c '"type":' server/core/{anomaly_alerts,business_alerts,mediaplan_alerts}.py``
# returns 0, 0, 0 — not one of the three producers emits that key. They all emit
# ``code`` (the ``meta.alerts[]`` shape). So every anomaly and every media-plan
# firing fell into the ``else`` branch and was rendered as a business alert, the
# anomaly branch of this module never ran from the scheduler, and
# ``anomalies_count`` was structurally 0 for every project every day — which is
# exactly the belief CAV-13 describes.
#
# ``type`` is still read FIRST, because a row read straight out of
# ``app.alert_firings`` carries that column; ``code`` is the fallback and is what
# the three producers actually put on the wire.
# ---------------------------------------------------------------------------

_MEDIAPLAN_FIRING_PREFIX = "mediaplan_"


def _firing_kind(firing: dict) -> str:
    """Return one of 'meta_alert' | 'anomaly' | 'mediaplan_alert' | 'business_alert'."""
    raw = str(firing.get("type") or firing.get("code") or "").strip().lower()
    if raw == "meta_alert":
        return "meta_alert"
    if raw == "anomaly":
        return "anomaly"
    if raw.startswith(_MEDIAPLAN_FIRING_PREFIX):
        return "mediaplan_alert"
    return "business_alert"


def _prior_value(firing: dict):
    """Return the firing's prior term, whichever key its producer used.

    ``business_alerts`` and ``mediaplan_alerts`` emit ``threshold``;
    ``anomaly_alerts`` emits ``expected_value`` (the rolling baseline mean).
    Reading only ``threshold`` left every anomaly with no prior term at all —
    hence the '(N/A → 700)' headlines.
    """
    prior = firing.get("threshold")
    if prior is None:
        prior = firing.get("expected_value")
    return prior


def _normalise_pull_ids(raw) -> list[str]:
    """Coerce a firing's ``pull_ids`` to a list (it can arrive as a JSON string)."""
    if not raw:
        return []
    if isinstance(raw, str):
        import json as _json  # noqa: PLC0415

        try:
            parsed = _json.loads(raw)
        except Exception:  # noqa: BLE001 — a bare id is not JSON, and that is fine
            return [raw]
        return parsed if isinstance(parsed, list) else [raw]
    return list(raw)


# ---------------------------------------------------------------------------
# Story 53.8 — CAV-13: the detector's silence is a state, and it is disclosed.
#
# `anomalies_count == 0` reads as "nothing is wrong". It also reads that way when
# the detector was mathematically incapable of firing — a series shorter than
# (n-1)/sqrt(n) >= threshold allows. The two are opposite facts and had the same
# rendering, so a brand-new Datastream looked exactly like a monitored one with
# nothing to report.
#
# The envelope now carries `anomaly_state`, and the four values are disjoint:
#
#   not_supplied            — no readiness input reached this builder. NEVER
#                             collapsed into `no_anomaly`: not knowing is not
#                             an all-clear. See the AC9 boundary in Dev Notes:
#                             the caller is scheduler.py, out of file scope.
#   no_series               — readiness was supplied and is empty: there is no
#                             baseline row at all for that date.
#   insufficient_observations — every series is below its derived minimum.
#   no_anomaly              — at least one series is armed and nothing fired.
#   anomalies_reported      — something fired.
# ---------------------------------------------------------------------------

ANOMALY_STATE_NOT_SUPPLIED = "not_supplied"
ANOMALY_STATE_NO_SERIES = "no_series"
ANOMALY_STATE_INSUFFICIENT = "insufficient_observations"
ANOMALY_STATE_NO_ANOMALY = "no_anomaly"
ANOMALY_STATE_REPORTED = "anomalies_reported"


def _build_detector_readiness(
    detector_readiness: list[dict] | None,
    anomalies_count: int,
) -> tuple[str, dict]:
    """Return ``(anomaly_state, readiness_payload)`` for the envelope (AC9)."""
    if detector_readiness is None:
        return ANOMALY_STATE_NOT_SUPPLIED, {
            "state": ANOMALY_STATE_NOT_SUPPLIED,
            "minimum_observations": None,
            "armed_count": 0,
            "insufficient_count": 0,
            "insufficient": [],
        }

    armed: list[dict] = []
    insufficient: list[dict] = []
    minimum: int | None = None
    for entry in detector_readiness:
        entry_min = entry.get("minimum_observations")
        if minimum is None and entry_min is not None:
            minimum = int(entry_min)
        if entry.get("armed"):
            armed.append(entry)
            continue
        observations = entry.get("observations")
        insufficient.append(
            {
                "project_id": entry.get("project_id"),
                "connector": entry.get("connector"),
                "metric": entry.get("metric"),
                "observations": observations,
                "minimum_observations": entry_min,
                # The exact phrase decision 3 asks the surface to separate from
                # `no anomaly`. It is a field, not prose a renderer may drop.
                "label": f"insufficient observations ({observations}/{entry_min})",
            }
        )

    if not detector_readiness:
        state = ANOMALY_STATE_NO_SERIES
    elif anomalies_count > 0:
        state = ANOMALY_STATE_REPORTED
    elif not armed:
        state = ANOMALY_STATE_INSUFFICIENT
    else:
        state = ANOMALY_STATE_NO_ANOMALY

    return state, {
        "state": state,
        "minimum_observations": minimum,
        "armed_count": len(armed),
        "insufficient_count": len(insufficient),
        "insufficient": insufficient,
    }


def build_briefing(
    project_id: str,
    briefing_date: str,
    alert_firings: list[dict],
    rollup: dict,
    context_events: list[dict],
    nightly_run_id: str | None,
    detector_readiness: list[dict] | None = None,
    context_events_unavailable: dict | None = None,
) -> dict:
    """Build the insights JSONB dict for one project's morning briefing (AC2 shape).

    Returns the insights JSONB dict (AC2 shape).
    Pure Python — no warehouse queries, no Postgres calls, no LLM calls.
    Inputs are all pre-fetched by the caller.

    Args:
        project_id:      Project identifier.
        briefing_date:   ISO date string (today in project tz).
        alert_firings:   Rows from app.alert_firings for the nightly window
                         (type != 'meta_alert').
        rollup:          From compute_rollup() for the project's default report.
        context_events:  From app.context_events for the last 7 days.
        nightly_run_id:  Identifier of the nightly run that produced this briefing.
        detector_readiness: Per-series readiness for the evaluated day (Story 53.8).
        context_events_unavailable:
                         AI-344 (context-hub.md, amendment of 2026-09-01). The
                         ``{reason, repair}`` the caller received when the window
                         could NOT be read from any store. ``context_events=[]``
                         means one thing only -- a store was read and the window
                         holds no event -- so this is the ONLY channel by which
                         "not read" reaches the row written to
                         ``app.morning_briefings``. Without it, a briefing built
                         on an unread window is byte-for-byte a briefing built on
                         a quiet week, which is the state ledger clauses
                         context-hub[75] and [78] refuse. It is the same
                         parameter, with the same shape, that the card builder
                         (`core.cards.get_card`) and the report builder
                         (`core.reports.build_summary`) already take.

    Returns:
        insights JSONB dict conforming to AC2 schema.
    """
    t0 = time.monotonic()

    # ------------------------------------------------------------------
    # Step 1: Classify alert_firings into business_alerts vs anomalies.
    # business_alerts: type == 'business_threshold'
    # anomalies: type == 'anomaly'
    # Both ranked by abs(delta_magnitude) = abs(observed_value - threshold) or
    # abs(observed_value) as a proxy when threshold is not meaningful.
    # ------------------------------------------------------------------
    business_alert_candidates: list[dict] = []
    mediaplan_alert_candidates: list[dict] = []
    anomaly_candidates: list[dict] = []

    for firing in alert_firings:
        kind = _firing_kind(firing)
        if kind == "meta_alert":
            continue  # always skip meta_alerts
        elif kind == "anomaly":
            anomaly_candidates.append(firing)
        elif kind == "mediaplan_alert":
            mediaplan_alert_candidates.append(firing)
        else:
            business_alert_candidates.append(firing)

    def _firing_magnitude(firing: dict) -> float:
        """Compute a sortable magnitude for ranking within type (highest first)."""
        obs = firing.get("observed_value")
        # Story 53.8: the anomaly producers emit `expected_value`, never
        # `threshold` — so this fell back to 0.0 and ranked every anomaly by its
        # raw observed value.
        thr = _prior_value(firing)
        try:
            obs_f = float(obs) if obs is not None else 0.0
            thr_f = float(thr) if thr is not None else 0.0
            delta = obs_f - thr_f
            return abs(delta) if abs(delta) > 0 else abs(obs_f)
        except (TypeError, ValueError):
            return 0.0

    # Sort each group by magnitude desc
    business_alert_candidates.sort(key=_firing_magnitude, reverse=True)
    mediaplan_alert_candidates.sort(key=_firing_magnitude, reverse=True)
    anomaly_candidates.sort(key=_firing_magnitude, reverse=True)

    # ------------------------------------------------------------------
    # Step 2: Build notable_delta candidates from rollup.
    # These are metrics that have a delta but did NOT already fire an alert.
    # ------------------------------------------------------------------
    alerted_metrics: set[str] = {
        (f.get("metric") or "")
        for f in (
            business_alert_candidates + mediaplan_alert_candidates + anomaly_candidates
        )
    }

    notable_delta_candidates: list[dict] = []
    for metric, data in rollup.items():
        if metric in alerted_metrics:
            continue
        delta = data.get("delta")
        if delta is None:
            continue
        notable_delta_candidates.append(
            {
                "_metric": metric,
                "_data": data,
                "_magnitude": abs(float(delta)) if delta is not None else 0.0,
            }
        )
    notable_delta_candidates.sort(key=lambda x: x["_magnitude"], reverse=True)

    # ------------------------------------------------------------------
    # Step 3: Assemble ranked insights — business_alerts first,
    #         then anomalies, then notable_deltas. Max 5, target 3.
    # ------------------------------------------------------------------
    insights: list[dict] = []
    rank = 1
    MAX_INSIGHTS = 5

    # -- Threshold-shaped alerts: business thresholds, then media-plan pacing --
    # Both compare an observation to a CONFIGURED policy, so both are rendered in
    # the grammar of a breach (CAV-14) rather than of a move.
    for group_type, candidates in (
        ("business_alert", business_alert_candidates),
        ("mediaplan_alert", mediaplan_alert_candidates),
    ):
        for firing in candidates:
            if rank > MAX_INSIGHTS:
                break
            metric = firing.get("metric") or ""
            connector = firing.get("connector") or firing.get("source_system") or ""
            obs_value = firing.get("observed_value")
            threshold = _prior_value(firing)
            pull_ids = _normalise_pull_ids(firing.get("pull_ids"))
            firing_id = firing.get("id") or firing.get("firing_id")

            try:
                obs_f = float(obs_value) if obs_value is not None else None
                thr_f = float(threshold) if threshold is not None else None
                delta = (
                    (obs_f - thr_f) if (obs_f is not None and thr_f is not None) else None
                )
            except (TypeError, ValueError):
                obs_f = None
                thr_f = None
                delta = None

            headline = _build_headline(
                group_type, metric, connector,
                delta, None, obs_f, thr_f,
                prior_kind=PRIOR_THRESHOLD,
            )
            citation = _build_citation(connector, metric, pull_ids)

            insights.append(
                {
                    "rank": rank,
                    "type": group_type,
                    "metric": metric,
                    "connector": connector,
                    "headline": headline,
                    "citation": citation,
                    "context_event_id": None,
                    "context_event_label": None,
                    "context_event_pairing": None,
                    "context_event_candidates": [],
                    "context_event_retrieval": None,
                    "alert_id": firing_id,
                    "pull_ids": pull_ids,
                }
            )
            rank += 1

    # -- Anomalies --
    for firing in anomaly_candidates:
        if rank > MAX_INSIGHTS:
            break
        metric = firing.get("metric") or ""
        connector = firing.get("connector") or firing.get("source_system") or ""
        obs_value = firing.get("observed_value")
        # An anomaly's prior term is the rolling baseline mean (`expected_value`),
        # not a configured threshold — and it is still not an earlier observation.
        threshold = _prior_value(firing)
        pull_ids = _normalise_pull_ids(firing.get("pull_ids"))
        firing_id = firing.get("id") or firing.get("firing_id")

        try:
            obs_f = float(obs_value) if obs_value is not None else None
            thr_f = float(threshold) if threshold is not None else None
            delta = (obs_f - thr_f) if (obs_f is not None and thr_f is not None) else None
        except (TypeError, ValueError):
            obs_f = None
            thr_f = None
            delta = None

        # Story 53.8 (CAV-15): scope the event to THIS claim -- the anomaly's own
        # window_date and its connector -- not to the briefing date. A claim that
        # does not carry its own date cannot be scoped, so nothing attaches.
        claim_date = firing.get("window_date") or firing.get("date")
        # Story 54.2, Half B: ONE judgement, read twice -- the survivor for the
        # claim, and every branch for the reader. The pairing is repaired
        # (Story 53.8, `608a8ae6`), so explaining it explains a right choice.
        ctx_candidates, ctx_pairing, ctx_walk = context_event_walk(
            context_events, claim_date, connector, metric
        )
        ctx_evt = next(
            (
                c.get("_event")
                for c in ctx_candidates
                if c.get("fate") == candidate_fate.FATE_SELECTED
            ),
            None,
        )
        ctx_id = (ctx_evt.get("id") if ctx_evt else None)
        ctx_label = (ctx_evt.get("label") if ctx_evt else None)
        emit_context_event_walk(ctx_candidates, ctx_walk)
        ctx_public = [
            {k: v for k, v in c.items() if not k.startswith("_")} for c in ctx_candidates
        ]

        headline = _build_headline(
            "anomaly", metric, connector,
            delta, None, obs_f, thr_f,
            prior_kind=PRIOR_BASELINE,
        )
        citation = _build_citation(connector, metric, pull_ids)

        insights.append(
            {
                "rank": rank,
                "type": "anomaly",
                "metric": metric,
                "connector": connector,
                "headline": headline,
                "citation": citation,
                "context_event_id": ctx_id,
                "context_event_label": ctx_label,
                # AC5: the basis on which the event was attached, and the
                # dimensions that could NOT be checked -- a field, not prose a
                # surface may choose to omit. None when no event is attached.
                "context_event_pairing": ctx_pairing,
                # Story 54.2 (AC1/AC5): the branches, and what the pairing IS.
                # An attached event with no list of what it was chosen over is
                # indistinguishable from an attached event that had no rival.
                "context_event_candidates": ctx_public,
                "context_event_retrieval": ctx_walk,
                "alert_id": firing_id,
                "pull_ids": pull_ids,
            }
        )
        rank += 1

    # -- Notable deltas --
    for nd in notable_delta_candidates:
        if rank > MAX_INSIGHTS:
            break
        metric = nd["_metric"]
        data = nd["_data"]
        connector = data.get("source_system") or ""
        pull_id = data.get("pull_id")
        pull_ids = [pull_id] if pull_id else []
        value = data.get("value")
        delta = data.get("delta")
        delta_pct = data.get("delta_pct")
        prior = (float(value) - float(delta)) if (value is not None and delta is not None) else None

        try:
            curr_f = float(value) if value is not None else None
            prev_f = float(prior) if prior is not None else None
            delta_f = float(delta) if delta is not None else None
        except (TypeError, ValueError):
            curr_f = None
            prev_f = None
            delta_f = None

        headline = _build_headline(
            "notable_delta", metric, connector,
            delta_f, delta_pct, curr_f, prev_f,
            prior_kind=PRIOR_OBSERVATION,
        )
        citation = _build_citation(connector, metric, pull_ids)

        insights.append(
            {
                "rank": rank,
                "type": "notable_delta",
                "metric": metric,
                "connector": connector,
                "headline": headline,
                "citation": citation,
                "context_event_id": None,
                "context_event_label": None,
                "context_event_pairing": None,
                "context_event_candidates": [],
                "context_event_retrieval": None,
                "alert_id": None,
                "pull_ids": pull_ids,
            }
        )
        rank += 1

    # ------------------------------------------------------------------
    # Step 4: Build the final insights JSONB envelope (AC2 shape).
    # ------------------------------------------------------------------
    build_duration_ms = round((time.monotonic() - t0) * 1000)
    alerts_count = len(business_alert_candidates)
    mediaplan_alerts_count = len(mediaplan_alert_candidates)
    anomalies_count = len(anomaly_candidates)
    anomaly_state, readiness_payload = _build_detector_readiness(
        detector_readiness, anomalies_count
    )

    payload = {
        "version": 1,
        "briefing_date": briefing_date,
        "insights": insights,
        "alerts_count": alerts_count,
        "mediaplan_alerts_count": mediaplan_alerts_count,
        "anomalies_count": anomalies_count,
        # Story 53.8 (CAV-13): anomalies_count == 0 does not say WHY. These two
        # do, and they say it without a heuristic on the consumer's side.
        "anomaly_state": anomaly_state,
        "detector_readiness": readiness_payload,
        "build_duration_ms": build_duration_ms,
    }

    # AI-344: the key sits BESIDE the counts, because it qualifies them. A
    # consumer reading `insights: []` next to `alerts_count: 0` on a briefing
    # whose context window was never read would otherwise be reading a quiet
    # week. Absent when the window WAS read -- an always-present null would make
    # "read, and empty" and "not read" the same shape again.
    if context_events_unavailable:
        payload["context_events_unavailable"] = context_events_unavailable

    return payload
