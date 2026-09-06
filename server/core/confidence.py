"""toorow -- confidence lookup: completeness, freshness, provenance (AD-9).

Extracted verbatim from ``core.main.get_daily_report`` in Story 5.1 (AI-27
decomposition). Extended with freshness and provenance terms (G-Confidence-3 fix).

THREE TERMS, AND NO FOURTH NUMBER OVER THEM. This module used to end with
``score = completeness * freshness * provenance``, rounded into a ``"score"``
key and carried onto the proactive surface through ``meta.confidence``.
`proactive-assertions.md` ("Incomplete if": *a single score merges evidence of
different natures*) forbids that object, and it forbids it for a reason the
product already applies everywhere else: completeness, freshness and
traceability are three different questions, and a product that multiplies them
lets a perfectly traceable report buy back the days it is behind. `score` is
gone. What survives is the three named terms, `unknown_terms`, and
`limiting_term` -- the weakest known term, which is the only summary this
evidence supports, exactly as `overview.md:61-63` lets the STALEST source decide
`complete_through` rather than an average of the sources.

Terms:
  completeness: ratio from app.pull_verifications, scoped to this project's
                Datastreams and to pulls overlapping the report window.
  freshness:    1.0 when the STALEST contributing source loaded within 48 h of
                date_to; degrades linearly to 0.0 over the next 7 days. Derived
                from rows' loaded_at values passed by the caller (no extra DB
                round-trip) and capped by the connection-health verdict on the
                same envelope, so one report never carries two answers.
  provenance:   fraction of rows carrying a non-empty pull_id (AD-9: traceability).

Return shapes (backward-compatible -- existing consumers read ``completeness``):
  * Single connector requested -> ``{"completeness": float, "freshness": float,
                                      "provenance": float, "limiting_term": str}``.
  * Multiple / all connectors  -> ``{"per_connector": {name: ratio, ...},
                                      "freshness": float, "provenance": float,
                                      "limiting_term": str}``
                                   where ``limiting_term`` weighs mean(per_connector)
                                   as the completeness term.
  * No data / DB error         -> ``None`` (best-effort; never blocks the report).

AD-2: connector resolution uses generic ``app.connection_ref`` columns
(connector_name). No module-specific names are hardcoded here.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# Freshness cadence constants (AD-9 / G-Confidence-3):
_FRESHNESS_GRACE_HOURS = 48   # within this window of date_to: freshness = 1.0
_FRESHNESS_DECAY_DAYS = 7     # after grace + this many days: freshness = 0.0

#: What the completeness term actually measured, carried in the response so a
#: reader is not left to assume it is project- and window-scoped. See the
#: `compute_confidence` docstring: `app.pull_verifications` has no project column.
_COMPLETENESS_SCOPE = (
    "latest pull verification for this connector, restricted to pulls this "
    "project's Datastreams requested over a window overlapping the report's; "
    "pulls with no Datastream carry no project identity and are excluded"
)

#: What the freshness term measures, and WHICH input decided it. Symmetrical with
#: `_COMPLETENESS_SCOPE`, for the same reason: the number is read by a human and
#: by a model, and neither can see whether "fresh" meant the newest source or the
#: stalest one. It means the stalest (`overview.md:61-63`).
_FRESHNESS_SCOPE = (
    "age of the STALEST contributing source at the report's end date -- each "
    "source counted at its own newest load, the oldest of those deciding; the "
    "connection-health verdict on the same envelope caps it, so stale_since and "
    "this term can never disagree"
)

#: Said instead of a number when the report window could not be read. The window
#: is half of what this module claims to describe; without it there is nothing to
#: be complete or fresh *for*, and today is not a stand-in for the period asked
#: about.
_WINDOW_UNKNOWN = (
    "the report window could not be read, so no term was measured against it"
)


def _earliest_row_date(rows: list[dict]) -> str | None:
    """Earliest ISO date present in *rows*, or None."""
    dates = [str(r["date"])[:10] for r in rows if r.get("date")]
    return min(dates) if dates else None


def _assemble(
    *,
    completeness_key: str,
    completeness_value,
    completeness_term: float | None,
    freshness: float | None,
    provenance: float | None,
    window_unknown: bool = False,
) -> dict:
    """Build the response, propagating unknown instead of absorbing it as 1.0.

    NO COMPENSATING SCALAR IS BUILT HERE, and none may be added back. The keys
    are the three terms, what is unknown among them, and which known one is
    weakest. `tests/conformance/test_no_compensating_confidence_scalar.py` fails
    if a fourth number over the three reappears under any name.
    """
    terms = {
        "completeness": completeness_term,
        "freshness": freshness,
        "provenance": provenance,
    }
    known = {name: value for name, value in terms.items() if value is not None}
    unknown = sorted(name for name, value in terms.items() if value is None)
    payload = {
        completeness_key: completeness_value,
        "freshness": freshness,
        "provenance": provenance,
        "unknown_terms": unknown,
        "limiting_term": min(known, key=known.get) if known else None,
        "completeness_scope": _COMPLETENESS_SCOPE,
        "freshness_scope": _FRESHNESS_SCOPE,
    }
    if window_unknown:
        payload["completeness_scope"] = _WINDOW_UNKNOWN
        payload["freshness_scope"] = _WINDOW_UNKNOWN
    return payload


def _parse_loaded_at(raw) -> datetime | None:
    """A single ``loaded_at`` value as an aware datetime, or None when unreadable."""
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
    try:
        parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _parse_window_end(date_to: str | None) -> datetime | None:
    """The report end date as midnight UTC, or None when it cannot be read.

    NEVER ``datetime.now()``. Both call sites used to fall back to today on an
    unreadable or absent ``date_to``, so a report whose window could not be read
    was measured against a window nobody asked for -- and, because "now" is always
    close to "now", it scored ``freshness = 1.0``. An unknown window produces an
    unknown term (`overview.md:38`: missing evidence is `Unknown`, never Healthy).
    """
    if not date_to:
        return None
    try:
        return datetime.fromisoformat(str(date_to)[:10] + "T00:00:00+00:00")
    except (ValueError, TypeError):
        return None


def _stalest_contributing_load(rows: list[dict]) -> datetime | None:
    """The load timestamp of the STALEST contributing source, or None.

    Per source: its own newest load, because a source that reported three times
    is as fresh as its last report. Across sources: the OLDEST of those, because
    a figure that adds two sources is only as current as the one that stopped
    reporting first (`overview.md:61-63`). Rows with no readable ``loaded_at``
    contribute nothing rather than a zero -- a missing timestamp is unknown, and
    it is the empty return below that says so.
    """
    newest_per_source: dict[str, datetime] = {}
    for row in rows:
        loaded_at = _parse_loaded_at(row.get("loaded_at"))
        if loaded_at is None:
            continue
        source = str(row.get("connector") or "unknown")
        current = newest_per_source.get(source)
        if current is None or loaded_at > current:
            newest_per_source[source] = loaded_at
    if not newest_per_source:
        return None
    return min(newest_per_source.values())


def _compute_freshness(
    rows: list[dict], date_to: str | None, *, stale_since: str | datetime | None = None
) -> float | None:
    """Compute freshness term [0.0, 1.0] from loaded_at values in *rows* (AD-9).

    Freshness is 1.0 when the stalest contributing source loaded within
    ``_FRESHNESS_GRACE_HOURS`` hours of ``date_to`` (interpreted as midnight UTC),
    degrading linearly to 0.0 over ``_FRESHNESS_DECAY_DAYS`` days beyond the grace
    window.

    ``stale_since`` is the verdict `core.health_enrichment` already wrote onto the
    same envelope, and it is folded in here so that ONE envelope carries ONE
    answer. A connection that has fetched nothing since T cannot have loaded a row
    after T, so the effective load is ``min(stalest row load, stale_since)``. Before
    this, `enrich_envelope_with_health` and `compute_confidence` ran back to back
    on the same report (`main.py`) and could state `stale_since` set **and**
    `freshness = 1.0` -- two opposite answers to one question, in one payload.

    ``None`` -- not 1.0 -- when freshness cannot be derived:

      * no rows at all (an empty result has no freshness to state);
      * rows exist but NONE carries a parsable ``loaded_at``;
      * ``date_to`` is absent or unreadable -- there is no window to be fresh
        *for*, and today is not a substitute for the window that was asked about.

    The second case is the one that mattered. It used to return 1.0, so a report
    whose rows had lost their load timestamps scored MAXIMUM freshness: unknown
    presented as perfect, which `docs/product-architecture/overview.md:38`
    forbids for the Project posture: *"Missing evidence is `Unknown` or
    `Unavailable`, never Healthy."*

    THAT CITATION WAS FABRICATED AND IS REPAIRED HERE. It read "`README.md:123`
    (invariant 8) ... in exactly those words", and `README.md:123` is a line about
    `NANGO_ENCRYPTION_KEY`; the word "unverifiable" appears nowhere in that file.
    An invented source is worse than none -- the next reader trusts it and stops
    looking. The same stale citation survives elsewhere, deliberately not cited by
    line here because those files are moving:
    ``grep -rn "README.md:123" server ui`` finds them, four of them in
    `core/cards.py`, and this story's file scope does not cover it.

    THE STALEST CONTRIBUTING SOURCE DECIDES, not the newest. The term used to be
    ``max(loaded_at)`` over every row, so one connector refreshed minutes ago
    masked one frozen for days -- and it disagreed with the two other places in
    this repository that answer the same question: `core.envelope`'s
    ``complete_through`` (``min`` across connectors) and `core.project_overview`'s
    ``min(verified_dates)``. `overview.md:61-63` settles it: *"`Complete through`
    is the latest interval complete across every required active input under
    policy, never the newest timestamp from one isolated source."* So the load
    timestamp is taken per contributing source (its own newest load), and the
    OLDEST of those decides -- exactly the rule `_worst_health` applies to
    connection health in `core.health_enrichment`.
    """
    if not rows:
        return None

    effective_load = _stalest_contributing_load(rows)
    if effective_load is None:
        return None

    # One envelope, one answer: the health verdict cannot be later than the load
    # it describes, and the stalest of the two decides.
    health_anchor = _parse_loaded_at(stale_since)
    if health_anchor is not None and health_anchor < effective_load:
        effective_load = health_anchor

    # date_to is the report end-date (YYYY-MM-DD); interpret as midnight UTC.
    end_midnight = _parse_window_end(date_to)
    if end_midnight is None:
        return None

    # Age = how far the stalest contributing load is behind end_midnight.
    age_seconds = (end_midnight - effective_load).total_seconds()
    grace_seconds = _FRESHNESS_GRACE_HOURS * 3600
    decay_seconds = _FRESHNESS_DECAY_DAYS * 86400

    if age_seconds <= grace_seconds:
        return 1.0
    if age_seconds >= grace_seconds + decay_seconds:
        return 0.0
    # Linear decay from 1.0 to 0.0 over the decay window.
    elapsed_decay = age_seconds - grace_seconds
    return round(1.0 - elapsed_decay / decay_seconds, 4)


def _compute_provenance(rows: list[dict]) -> float | None:
    """Compute provenance term [0.0, 1.0]: fraction of rows with a non-empty pull_id.

    A pull_id on every row means full traceability (AD-9). ``None`` when there are
    no rows: a fraction with a zero denominator is not 1.0, and
    `overview.md:123-124` states the rule this term has to obey -- *"Every count
    names its denominator and window. A zero with unknown coverage is not
    presented as an all-clear."*
    """
    if not rows:
        return None
    with_pull = sum(1 for r in rows if r.get("pull_id"))
    return round(with_pull / len(rows), 4)


#: The two row-derived terms, under public names, because a SECOND surface now
#: needs exactly them: `core.insight_confidence` derives the confidence of a
#: published daily insight from the rows that carry the members the insight
#: CITED. Re-implementing "how fresh" or "how traceable" one module over is how
#: two surfaces start answering the same question differently -- the defect
#: story 53.3 closed between `_worst_health` and `_compute_freshness`. Same
#: functions, narrower rows.
freshness_term = _compute_freshness
provenance_term = _compute_provenance

#: The band boundaries a term is READ at, in one place, because the insight
#: derivation and any later reader must not each invent their own. See
#: `core.insight_confidence.term_reading`.
TERM_READING_HIGH = 0.9
TERM_READING_MEDIUM = 0.6


def compute_confidence(
    project_id: str,
    effective_connectors: list[str] | None,
    *,
    rows: list[dict] | None = None,
    date_to: str | None = None,
    date_from: str | None = None,
    stale_since: str | datetime | None = None,
) -> dict | None:
    """Return the confidence dict for the report, or None.

    Args:
        project_id:          The project to scope the completeness lookup.
        effective_connectors: Connectors in scope (None = all).
        rows:                Optional list of fact rows used to derive freshness
                             and provenance terms. When omitted both terms are
                             ``None`` (unknown), never 1.0.
        date_to:             Report end date (ISO YYYY-MM-DD), used for freshness
                             age calculation. When absent or unreadable, EVERY
                             window-bound term is ``None``; today is never
                             substituted for it.
        stale_since:         The verdict `core.health_enrichment` already wrote on
                             this envelope, so the two halves of "how fresh is
                             this" cannot contradict each other.

    Best-effort: any DB error yields None (no ``confidence`` key in meta). See the
    module docstring for the return shapes.

    Two disclosures travel with every returned dict, because this number is read
    by a human and by a model and neither can see how it was built:

      * ``unknown_terms`` names every term that could not be measured. There is no
        longer a product to poison: the key that carried one (``score``) is
        removed, because an unknown factor used to enter the multiplication as
        1.0 and make an unmeasurable report score HIGHER than a measured,
        imperfect one -- and even once that was fixed, the product itself was the
        defect. A reader gets three numbers and the list of the ones nobody could
        take.
      * ``limiting_term`` names the weakest known term. A single number over three
        different natures -- completeness, freshness, traceability -- compensates,
        and `overview.md:32-34` refuses exactly that collapse for posture:
        *"Business signals, operational health and trust/readiness remain visually
        and semantically separate; they never collapse into one compensating
        score."* (This line was cited as `overview.md:37`, which is a table
        separator -- a citation nobody could have checked without noticing.)
        Naming the limiter is the minimum that keeps the number readable, and
        since the compensating scalar is gone it is now the ONLY summary offered.

    COMPLETENESS SCOPE (story 53.3). ``app.pull_verifications`` has no project
    column (migration 007) and is keyed by ``connection_ref_id``, which is
    ORG-owned since story 21.3 -- so the previous query returned the org's most
    recent verification for the connector, whatever project it was pulled for and
    whatever period it covered. In an organization running several projects off one
    Google authorization, the number described someone else's pull.

    The project identity was reachable and unused: ``app.pull_jobs`` carries
    ``datastream_id``, and a Datastream is project-bound
    (``app.datastreams.project_id``, NOT NULL). That is the GOVERNED chain. Note
    what is deliberately NOT used: ``connection_ref.project_id`` still exists but
    migration 037 keeps it "for compat (removed in a later dedicated story)" --
    scoping on a condemned column would be a repair with an expiry date.

    ``pull_jobs.date_from`` / ``date_to`` give the window, so the verification is
    also restricted to pulls that actually overlap the report's period.

    ``datastream_id`` is NULLABLE (ad-hoc and pre-8.2 pulls). Those verifications
    have no project identity at all, so they are EXCLUDED rather than assumed, and
    ``completeness_scope`` says which of the two situations produced the number.

    A WINDOW THAT CANNOT BE READ IS NOT TODAY. ``_date_to`` used to fall back to
    ``datetime.now()``, so an absent or malformed ``date_to`` produced a query over
    a period nobody asked about and a freshness measured against "now" -- which is
    always fresh. Both window-bound terms are now ``None`` and the scope strings
    say why, because this function's whole subject is *this project and this
    window* and half of that subject was missing.
    """
    _rows = rows or []
    # The report window. `date_from` is optional so the existing call sites keep
    # working unchanged; absent, it is the earliest date actually present in the
    # rows, which is the window those rows came from. With no rows at all the
    # window collapses to the single end date -- narrower than the truth, never
    # wider, so the term can under-report coverage but cannot borrow another
    # period's.
    _window_to = date_to if _parse_window_end(date_to) is not None else None
    _window_from = date_from or _earliest_row_date(_rows) or _window_to

    freshness = _compute_freshness(_rows, _window_to, stale_since=stale_since)
    provenance = _compute_provenance(_rows)

    if _window_to is None:
        # No window: the completeness lookup has no period to restrict itself to,
        # and an unrestricted one would return another period's verification --
        # the exact defect this story closed. Say unknown instead.
        return _assemble(
            completeness_key="completeness",
            completeness_value=None,
            completeness_term=None,
            freshness=freshness,
            provenance=provenance,
            window_unknown=True,
        )

    try:
        from core.db import get_connection as _get_pg_conn  # noqa: PLC0415

        with _get_pg_conn() as _pg_conn:
            with _pg_conn.cursor() as _pg_cur:
                if effective_connectors and len(effective_connectors) == 1:
                    # Single-connector request, scoped to this project's own pulls.
                    #
                    # The column is `provider`, NOT `connector_name`. These three queries
                    # said `r.connector_name` -- a column `app.connection_ref` has never
                    # had -- so every call raised UndefinedColumn, the bare
                    # `except Exception: return None` below swallowed it, and
                    # `meta.confidence` was ABSENT from every envelope this function has
                    # ever produced. A dead feature reads exactly like a feature with no
                    # data yet, which is why nothing surfaced it. `core.mirror_sync:110`
                    # already carries the correct mapping (`provider AS connector_name`),
                    # so the fix is verified against a peer, not guessed (AD-2: `provider`
                    # holds the module kebab-case name).
                    _pg_cur.execute(
                        """
                        SELECT pv.completeness_ratio
                        FROM app.pull_verifications pv
                        JOIN app.connection_ref r ON r.id = pv.connection_ref_id
                          JOIN app.pull_jobs pj ON pj.pull_id = pv.pull_id
                          JOIN app.datastreams ds ON ds.id = pj.datastream_id
                        WHERE ds.project_id = %s
                          AND pj.date_from <= %s AND pj.date_to >= %s
                          AND r.provider = %s
                        ORDER BY pv.verified_at DESC
                        LIMIT 1
                        """,
                        (project_id, _window_from, _window_to, effective_connectors[0]),
                    )
                    _conf_row = _pg_cur.fetchone()
                    if _conf_row:
                        completeness = float(_conf_row[0])
                        return _assemble(
                            completeness_key="completeness",
                            completeness_value=completeness,
                            completeness_term=completeness,
                            freshness=freshness,
                            provenance=provenance,
                        )
                    return None

                # Multi-connector or all-connectors: return per-connector confidence map.
                # Each connector gets its own most-recent completeness ratio.
                # Returns {connector_name: completeness_ratio} dict.
                _connector_filter = effective_connectors  # None = all connectors
                if _connector_filter:
                    _pg_cur.execute(
                        """
                        SELECT r.provider AS connector_name, pv.completeness_ratio
                        FROM app.pull_verifications pv
                        JOIN app.connection_ref r ON r.id = pv.connection_ref_id
                          JOIN app.pull_jobs pj ON pj.pull_id = pv.pull_id
                          JOIN app.datastreams ds ON ds.id = pj.datastream_id
                        WHERE ds.project_id = %s
                          AND pj.date_from <= %s AND pj.date_to >= %s
                          AND r.provider = ANY(%s)
                        ORDER BY r.provider, pv.verified_at DESC
                        """,
                        (project_id, _window_from, _window_to, list(_connector_filter)),
                    )
                else:
                    _pg_cur.execute(
                        """
                        SELECT r.provider AS connector_name, pv.completeness_ratio
                        FROM app.pull_verifications pv
                        JOIN app.connection_ref r ON r.id = pv.connection_ref_id
                          JOIN app.pull_jobs pj ON pj.pull_id = pv.pull_id
                          JOIN app.datastreams ds ON ds.id = pj.datastream_id
                        WHERE ds.project_id = %s
                          AND pj.date_from <= %s AND pj.date_to >= %s
                        ORDER BY r.provider, pv.verified_at DESC
                        """,
                        (project_id, _window_from, _window_to),
                    )
                _per_connector: dict[str, float] = {}
                for _conn_name, _ratio in _pg_cur.fetchall():
                    # Keep only the most-recent row per connector (query is ordered DESC)
                    if _conn_name not in _per_connector:
                        _per_connector[_conn_name] = float(_ratio)
                if _per_connector:
                    # The mean is what makes this compensating: one connector at
                    # 0.2 and three at 1.0 reads 0.8. The per-connector map is
                    # returned alongside precisely so the mean is never the only
                    # thing available, and `worst_connector` names the one that
                    # a reader would otherwise have to find by eye.
                    mean_completeness = sum(_per_connector.values()) / len(_per_connector)
                    payload = _assemble(
                        completeness_key="per_connector",
                        completeness_value=_per_connector,
                        completeness_term=mean_completeness,
                        freshness=freshness,
                        provenance=provenance,
                    )
                    payload["worst_connector"] = min(
                        _per_connector, key=_per_connector.get
                    )
                    return payload
                return None
    except Exception:
        return None  # confidence is best-effort; never block the report
