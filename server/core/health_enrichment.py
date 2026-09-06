"""toorow -- health-aware envelope enrichment (Story 2.5; extracted 5.1/AI-27).

Extracted verbatim from ``core.main`` in Story 5.1 (AI-27 decomposition). Pure
behaviour-preserving move: ``main.py`` re-exports ``_enrich_envelope_with_health``
for backward compatibility with existing imports/tests.

Reads connection health from ``app.connection_health`` (Postgres cache) and maps it
onto the canonical envelope's ``meta`` (freshness + alerts). No-op when the DB is
unreachable or no connection_ref rows exist (Epic 1 backward compat).

AD-2: source-agnostic -- no module-specific strings.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone

from core.constants import AUTH_EXPIRED_CODE

logger = logging.getLogger(__name__)

_STALE_CADENCE_HOURS_DEFAULT = 26  # generous for daily pulls that run at night


def _contributing_connectors(envelope: dict) -> list[str]:
    """Connector names this envelope's figures actually came from.

    Read from `meta.provenance[].source_system`, which
    `core.envelope.derive_meta_from_rows` fills from the data rows themselves --
    so it names who contributed, not who exists.
    """
    provenance = (envelope.get("meta") or {}).get("provenance") or []
    if not isinstance(provenance, list):
        return []
    names = {
        str(entry.get("source_system"))
        for entry in provenance
        if isinstance(entry, dict) and entry.get("source_system")
    }
    return sorted(names)


#: Health statuses ordered worst-first. A report is as healthy as its WORST input,
#: never as its first or its newest.
#:
#: THE ARBITRATION, WRITTEN ONCE. This module and `core.confidence` both answer
#: "how fresh is this report", they run back to back on the same envelope
#: (`core/main.py`, health first then confidence), and they used to answer in
#: opposite directions: the WORST connection decided here, the NEWEST load decided
#: there -- so one payload could carry `stale_since` set and `freshness = 1.0`.
#: `docs/product-architecture/overview.md:61-63` settles it in favour of this
#: module's rule: *"`Complete through` is the latest interval complete across
#: every required active input under policy, never the newest timestamp from one
#: isolated source."* `core.confidence` now applies the same rule to loads (per
#: source, oldest decides) and takes `stale_since` as a cap.
_STATUS_SEVERITY = {
    "revoked": 0,
    "provider_denied": 1,
    "populate_failed": 2,
    "stale": 3,
    "ok": 4,
}

#: The connections this project pulled through, over a window overlapping the
#: report's -- the GOVERNED chain, identical to the one `core.confidence` uses for
#: completeness (`pull_jobs.datastream_id` -> `datastreams.project_id`, NOT NULL).
#:
#: WHAT THIS REPLACES, AND WHY IT WAS THE STORY'S OWN DEFECT. Both branches read
#: `r.owner_org_id = (SELECT org_id FROM app.projects WHERE id = %s)`: the ORG, not
#: the project. In an organization running several projects off one authorization
#: -- the shape this product is built for -- a healthy project inherited the badge
#: of its neighbour's revoked credential. And no date entered the query at all, so
#: a report on January read the health of a connection that only ever served July.
#: The story is titled "describe this project and this window" and the query did
#: neither.
#:
#: `connection_ref.project_id` is deliberately NOT used: migration 037 keeps it
#: "for compat (removed in a later dedicated story)", and scoping on a condemned
#: column is a repair with an expiry date.
#:
#: The three `populate_failed_*` columns are the pull identity migration 276
#: added (AI-302): WHICH collection filed the verdict that raised the current
#: red, what it said (`empty` / `partial`), and when. Without them the alert
#: below could only say "a pull landed no rows" -- a status with no pull to
#: open, which is what made the 2026-08-12 silence illegible.
_CONNECTIONS_FOR_PROJECT_AND_WINDOW = """
                        SELECT h.status, h.last_fetched_at, r.created_at,
                               h.populate_failed_pull_id,
                               h.populate_failed_verdict,
                               h.populate_failed_at,
                               h.provider_denied_pull_id,
                               h.provider_denied_at
                        FROM app.connection_ref r
                        LEFT JOIN app.connection_health h
                               ON h.connection_ref_id = r.id
                        WHERE r.id IN (
                                  SELECT pj.connection_ref_id
                                  FROM app.pull_jobs pj
                                  JOIN app.datastreams ds ON ds.id = pj.datastream_id
                                  WHERE ds.project_id = %s
                                    AND pj.date_from <= %s
                                    AND pj.date_to >= %s
                              )
"""


def _iso_date(value) -> str | None:
    """*value* as an ISO ``YYYY-MM-DD`` string, or None when it is not one.

    A malformed date is refused rather than coerced. The alternative -- what
    `core.confidence` did until this story -- is to fall back on today, which
    turns "I could not read the window" into a confident answer about a different
    window.
    """
    if not value:
        return None
    text = str(value)[:10]
    try:
        datetime.strptime(text, "%Y-%m-%d")  # noqa: DTZ007 -- a calendar date, not an instant
    except (ValueError, TypeError):
        return None
    return text


def _report_window(
    envelope: dict, date_from: str | None, date_to: str | None
) -> tuple[str | None, str | None]:
    """The window this envelope describes: the caller's, else the envelope's own.

    `get_daily_report` has already validated its `date_range` by the time it calls
    here, so the caller's pair is authoritative. The envelope fallback exists for
    the builders that will reach this function later (`get_card`, `get_report`
    still never evaluate staleness -- CAV-04's open half) and it reads the range
    they already publish under `data.date_range`, rather than inventing one.
    """
    resolved_from = _iso_date(date_from)
    resolved_to = _iso_date(date_to)
    if resolved_from and resolved_to:
        return resolved_from, resolved_to

    declared = (envelope.get("data") or {}).get("date_range") or {}
    if not isinstance(declared, dict):
        return resolved_from, resolved_to
    return (
        resolved_from or _iso_date(declared.get("start")),
        resolved_to or _iso_date(declared.get("end")),
    )


def _worst_health(rows) -> tuple | None:
    """The worst (status, last_fetched_at, created_at) among contributing connections.

    Ties on status break on the OLDEST `last_fetched_at`, so the stalest input
    decides. Rows whose health cache is empty (`status IS NULL`) lose to any known
    status: an unknown one cannot be shown as the reason, but it must not mask a
    known bad one either.
    """
    # Shape guard: only rows the query's own SELECT can produce are considered.
    # A row of another arity means this function was handed something it did not
    # ask for; the module's posture is best-effort, so it declines to enrich rather
    # than raising inside a report. Two arities are the query's own: 6 is the
    # SELECT since AI-302 (status, last_fetched_at, created_at + the
    # populate_failed pull identity of migration 276); 3 is the same row without
    # the identity -- the shape every caller produced before, which still
    # arbitrates but cannot name a pull; 8 is the SELECT since AI-341 (the same
    # row plus the provider_denied pull identity of migration 331).
    shaped = [r for r in (rows or []) if isinstance(r, (tuple, list)) and len(r) in (3, 6, 8)]
    if not shaped:
        return None
    known = [r for r in shaped if r[0] is not None]
    if not known:
        return shaped[0]

    def _rank(row):
        status, last_fetched_at = row[0], row[1]
        return (
            _STATUS_SEVERITY.get(str(status), 9),
            last_fetched_at if last_fetched_at is not None else datetime.min.replace(
                tzinfo=timezone.utc
            ),
        )

    return min(known, key=_rank)


def enrich_envelope_with_health(
    envelope: dict,
    project_id: str,
    *,
    date_from: str | None = None,
    date_to: str | None = None,
) -> dict:
    """Enrich the canonical envelope's meta with connection health (Story 2.5, T4).

    Reads health from app.connection_health (Postgres cache) -- does NOT call Nango.
    This is a no-op when:
      - PLATFORM_DB_URL is not set / DB is unreachable
      - the report window is not stated (see below)
      - no connection this project pulled through over that window (Epic 1
        backward compat)

    ``date_from`` / ``date_to`` are the report window, and they are REQUIRED for
    this function to say anything. Without them there is no "this window" to
    describe, and reading every connection the project ever used would answer a
    question nobody asked. The envelope then keeps the builders'
    ``stale_since_evaluated: False``, which honestly reads "nobody looked" --
    `overview.md:38`: missing evidence is `Unknown`, never Healthy.

    Health -> freshness mapping (AC5, AC7):
      ok:      stale_since absent (unless cadence check fires -- AC7)
      stale:   meta.freshness.stale_since = last_fetched_at (Nango proxy)
      revoked: meta.alerts[] += auth_expired entry

    AC7 cadence check (independent of Nango stale status):
      If last_fetched_at is older than STALE_CADENCE_HOURS, set stale_since anyway.
      If last_fetched_at is None and connection is older than 2h, also set stale_since.
      TODO(Epic-3): replace with manifest cadence_hours per connector
    """
    cadence_hours = int(os.environ.get("STALE_CADENCE_HOURS", str(_STALE_CADENCE_HOURS_DEFAULT)))

    # WHICH connections this envelope actually depends on (story 53.3, CAV-04).
    #
    # This query used to read ONE row -- `ORDER BY r.created_at LIMIT 1`, the
    # organization's OLDEST credential -- whichever connectors produced the figures.
    # So the staleness badge on a two-source report described a connection that may
    # have contributed nothing, and a genuinely frozen contributor stayed invisible.
    #
    # `core.envelope.derive_meta_from_rows` already records who contributed, in
    # `meta.provenance[].source_system`. Reading it here scopes the health lookup to
    # the connections the numbers came from, and the WORST of them wins: a report is
    # exactly as fresh as its stalest input, which is the rule `overview.md:61-63`
    # states for Project posture.
    contributors = _contributing_connectors(envelope)

    window_from, window_to = _report_window(envelope, date_from, date_to)
    if window_from is None or window_to is None:
        logger.debug(
            "health_enrich: no readable window for project_id=%s -- not evaluated",
            project_id,
        )
        return envelope

    try:
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            with conn.cursor() as cur:
                if contributors:
                    cur.execute(
                        _CONNECTIONS_FOR_PROJECT_AND_WINDOW
                        + "                          AND r.provider = ANY(%s)\n",
                        (project_id, window_to, window_from, list(contributors)),
                    )
                else:
                    # No provenance on the envelope (empty result, or a builder
                    # that does not record it): the provider filter is dropped and
                    # NOTHING ELSE. The project and the window still bound the
                    # read, so the fallback is wider than the scoped branch but
                    # never wider than this project and this window -- where it
                    # used to widen to the whole organization, every provider,
                    # all time.
                    #
                    # `_worst_health` takes the worst of however many rows it is
                    # given, which is the rule `overview.md:61-63` states: a report
                    # is as fresh as its stalest input.
                    cur.execute(
                        _CONNECTIONS_FOR_PROJECT_AND_WINDOW
                        + "                        ORDER BY r.created_at\n",
                        (project_id, window_to, window_from),
                    )
                rows = cur.fetchall()
    except Exception:
        # DB unreachable or table absent -- silently no-op (Epic 1 backward compat)
        logger.debug("health_enrich: db unavailable for project_id=%s -- skipping", project_id)
        return envelope

    row = _worst_health(rows)
    if row is None:
        # No connection_ref for this project -- no-op (Epic 1 backward compat)
        return envelope

    status, last_fetched_at, conn_created_at = row[0], row[1], row[2]
    # The pull identity migration 276 records next to a populate_failed red.
    # A 3-column row (a caller predating AI-302) simply carries none, which is
    # the same truth as a red raised before the migration: nothing to name.
    pf_pull_id, pf_verdict, pf_raised_at = row[3:6] if len(row) >= 6 else (None, None, None)
    # The provider_denied pull identity migration 331 records (AI-341); a shorter
    # row simply carries none, same truth as a red raised before the migration.
    pd_pull_id, pd_raised_at = row[6:8] if len(row) == 8 else (None, None)

    # If health row does not exist yet (poller has not run), status is None
    if status is None:
        # No health cache yet -- conservative: no stale_since, no alert
        return envelope

    now = datetime.now(tz=timezone.utc)
    meta = envelope.setdefault("meta", {})
    freshness = meta.setdefault("freshness", {})
    # This function is the ONLY code in the repository that evaluates staleness,
    # and it is reached from `get_daily_report` alone -- never from `get_card` or
    # `get_report`, the surfaces that get frozen into a Render and shared. The
    # builders mark `stale_since_evaluated: False`; reaching this line is what
    # makes the null below mean "evaluated, and not stale" rather than "nobody
    # looked".
    #
    # The reading is now scoped to the connections that actually contributed, and
    # the WORST of them decides (story 53.3).
    freshness["stale_since_evaluated"] = True
    alerts: list = meta.setdefault("alerts", [])

    if status == "revoked":
        # AC6, AC8: auth_expired alert
        alerts.append(
            {
                "code": AUTH_EXPIRED_CODE,
                "severity": "error",
                "message": (
                    "Token de connexion expire -- reconnectez dans la console admin."
                ),
            }
        )

    elif status == "provider_denied":
        # AI-341: the provider refuses the DATA while the authorization itself is
        # alive -- the one red whose sentence must NOT say "reconnect", because
        # reconnecting the same account repairs nothing. The gesture is at the
        # provider: restore the connected account's access, or connect an
        # account that has it. The red names the pull that raised it
        # (migration 331), same rule as populate_failed.
        pull_id = str(pd_pull_id) if pd_pull_id else ""
        raised_at = pd_raised_at
        if raised_at is not None and getattr(raised_at, "tzinfo", None) is None:
            raised_at = raised_at.replace(tzinfo=timezone.utc)
        parts = [pull_id] if pull_id else []
        if pull_id and raised_at is not None:
            parts.append(raised_at.date().isoformat())
        which = f" (collection {', '.join(parts)})" if parts else ""
        alert = {
            "code": "provider_denied",
            "severity": "error",
            "message": (
                f"The provider refuses this connection's access to the data it "
                f"collects{which}. Restore the connected account's access at the "
                "provider, or reconnect with an account that has it; the next "
                "successful collection clears this on its own"
            ),
        }
        if pull_id:
            alert["pull_id"] = pull_id
        if raised_at is not None:
            alert["raised_at"] = raised_at.isoformat()
        alerts.append(alert)

    elif status == "populate_failed":
        # Story 3.5 (AC6): populate_failed alert (HG-2: distinct from auth_expired).
        #
        # AI-302: THE ALERT NAMES THE PULL THAT RAISED THE RED. The flag is
        # sticky and closes every Datastream behind the authorization, and until
        # migration 276 the row it comes from said only `populate_failed` -- no
        # pull, no date -- which is what made five days of silence illegible,
        # twice. The message quotes the same identity `queue.py`'s
        # `connection_unhealthy` refusal quotes, and it names the GESTURE
        # (open that collection, then re-verify the reporting account), never
        # the cause. Identity is quoted only when the row HAS it: reds raised
        # before migration 276 carry nothing, and inventing a collection to
        # point at would be worse than the silence it replaces.
        verdict = str(pf_verdict) if pf_verdict else ""
        pull_id = str(pf_pull_id) if pf_pull_id else ""
        # `partial` is still rendered because HISTORICAL reds carry it: since
        # 2026-08-17 (AI-101) a partial verdict no longer raises the sticky red at
        # all -- only `empty` does -- but the rows raised before that day are still
        # in `app.connection_health` and must keep naming what they saw. A new
        # `populate_failed` can only mean "no rows" from now on.
        landed = "too few rows" if verdict == "partial" else "no rows"
        raised_at = pf_raised_at
        if raised_at is not None and getattr(raised_at, "tzinfo", None) is None:
            raised_at = raised_at.replace(tzinfo=timezone.utc)
        parts = [pull_id] if pull_id else []
        if pull_id and raised_at is not None:
            parts.append(raised_at.date().isoformat())
        which = f" (collection {', '.join(parts)})" if parts else ""
        alert: dict = {
            "code": "populate_failed",
            "severity": "error",  # review-3-5 F-3: shell needs it for the RED chip
            "message": (
                f"A collection behind this report landed {landed}{which}, so newer "
                "collections are held back. Open that collection to see what it "
                "returned, then re-verify the reporting account to release it"
            ),
        }
        # Machine-readable identity, so a screen can link to the pull instead of
        # parsing the sentence. Each field appears only when it is known.
        if pull_id:
            alert["pull_id"] = pull_id
        if verdict:
            alert["verdict"] = verdict
        if raised_at is not None:
            alert["raised_at"] = raised_at.isoformat()
        alerts.append(alert)

    elif status == "stale":
        # AC5: stale_since from Nango last_fetched_at (P2 proxy)
        # TODO(Epic-3): replace with pull ledger cadence
        if last_fetched_at is not None:
            lf = last_fetched_at
            if lf.tzinfo is None:
                lf = lf.replace(tzinfo=timezone.utc)
            freshness["stale_since"] = lf.isoformat()
        elif conn_created_at is not None:
            # review-2-5 F-02: a None stale_since is falsy in the shell and the
            # badge silently disappears -- anchor on the connection's creation
            # time (never fetched => stale since it exists).
            ca = conn_created_at
            if ca.tzinfo is None:
                ca = ca.replace(tzinfo=timezone.utc)
            freshness["stale_since"] = ca.isoformat()
        else:
            freshness["stale_since"] = now.isoformat()

    elif status == "ok":
        # AC7: cadence check independent of Nango's own stale classification
        stale_cadence_seconds = cadence_hours * 3600
        if last_fetched_at is not None:
            lf = last_fetched_at
            if lf.tzinfo is None:
                lf = lf.replace(tzinfo=timezone.utc)
            age_seconds = (now - lf).total_seconds()
            if age_seconds > stale_cadence_seconds:
                # TODO(Epic-3): replace with manifest cadence_hours per connector
                freshness["stale_since"] = lf.isoformat()
        else:
            # Never pulled -- if connection is older than 2h, mark stale
            # TODO(Epic-3): replace with manifest cadence_hours
            if conn_created_at is not None:
                ca = conn_created_at
                if ca.tzinfo is None:
                    ca = ca.replace(tzinfo=timezone.utc)
                if (now - ca).total_seconds() > 2 * 3600:
                    # review-2-5 F-02: truthy anchor -- stale since creation+2h
                    from datetime import timedelta  # noqa: PLC0415

                    freshness["stale_since"] = (ca + timedelta(hours=2)).isoformat()

    return envelope
