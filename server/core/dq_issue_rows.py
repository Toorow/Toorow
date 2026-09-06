"""The faulty rows of an anomaly, REPLAYED on demand -- story 59.1, epic 59.

NOTHING IS STORED, AND THAT IS A DECISION, NOT A GAP (Jean, 2026-08-07). Migration
145 already wrote it of ``app.dq_evaluations.observed`` -- "bounded, masked, and
NOT the rows themselves" -- so there is no sample table to read, no client data
retained, and no organization-erasure escape hatch to add. When somebody unfolds
an issue, the monitor's condition is replayed over the window the run was judged
on and the rows come back AS THEY ARE NOW.

Two consequences the screen carries and this module makes readable:

* the unfolding COSTS A READ of the warehouse, so it is explicit and never
  automatic for every run of a list. It is a route of its own
  (``…/workbench/runs/{execution_id}/anomalies/{issue_id}/rows``) precisely so
  that the run list cannot pay for it;
* the rows are TODAY'S, not the detection's. ``replayed_at`` says when they were
  read, and the console says the sentence. A replay that finds nothing is
  information -- ``NOTHING_MATCHES_NOW`` -- and never "no rows were returned":
  the first says the condition no longer holds, the second says the reading
  failed.

THE PROFILE DECLARES WHETHER IT HAS ROWS TO REPLAY; NO SCREEN HOLDS A LIST OF
PROFILE NAMES. A zero-row finding has no faulty row BY NATURE -- it is the
absence of rows -- exactly like a missed arrival time, and a console holding
``["zero_rows", "timeliness"]`` would give the eighth profile the wrong default
in silence. That is the class defect story 59.4 paid for five times, so the
answer travels on the payload: :data:`REPLAY_PROFILES` is keyed by the same names
as ``dq_monitors.CHECK_PROFILES``, and a profile this build does not know answers
``replayable_rows = False`` with a reason that names the gap rather than claiming
there is nothing to show.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Callable, Mapping

from core import dq_monitor_registry

logger = logging.getLogger(__name__)


class IssueNotFound(LookupError):
    """The issue is not this Project's, or not this Datastream's, or not this run's."""


class InvalidIssueStatus(ValueError):
    """The asked-for transition is not one this schema can hold."""


# ---------------------------------------------------------------------------
# What each check profile has to show.
# ---------------------------------------------------------------------------

#: The finding is not about rows at all: no row of the window is at fault.
NO_FAULTY_ROW_BY_NATURE = "no_faulty_row_by_nature"
#: The finding IS about rows, and this build cannot replay them yet. Said as the
#: gap it is -- "there is nothing to show" would be a different, false sentence.
NO_REPLAY_IMPLEMENTED = "no_replay_implemented"
#: A `check_profile` neither `CHECK_PROFILES` nor this registry names.
UNKNOWN_PROFILE = "unknown_check_profile"

#: The evaluation that judged this run is what carries the window and the
#: measurement; without it the condition cannot be replayed over the right days,
#: and inventing a window would replay a condition nobody applied.
NO_EVALUATION_FOR_RUN = "no_evaluation_for_run"
#: The issue's root cause is a fingerprint, and no field of the evaluation
#: reproduces it. Answered rather than guessed at: replaying the wrong field
#: would hand back rows that are not the anomaly's.
FIELD_UNIDENTIFIABLE = "field_unidentifiable"
#: The evaluation named no relation, so there is no address to read.
RELATION_UNKNOWN = "relation_unknown"

#: The replay ran and the condition no longer holds. NOT an error, and never the
#: same sentence as an unreadable relation.
NOTHING_MATCHES_NOW = "nothing_matches_now"

_MESSAGES = {
    NO_FAULTY_ROW_BY_NATURE: (
        "This monitor's finding is not about particular rows: there is no faulty row "
        "to replay, so nothing is exported and no table is shown."
    ),
    NO_REPLAY_IMPLEMENTED: (
        "This monitor's finding is about rows, and this build carries no replay for "
        "its profile yet. That is a gap, not an absence of faulty rows."
    ),
    UNKNOWN_PROFILE: (
        "This build does not know this check profile, so it cannot say which rows "
        "the finding is about."
    ),
    NO_EVALUATION_FOR_RUN: (
        "No evaluation of this monitor names this run, so the window the condition "
        "was judged on is unknown and replaying it over an invented window would "
        "answer about days nobody checked."
    ),
    FIELD_UNIDENTIFIABLE: (
        "The field this issue is about could not be recovered from the evaluation "
        "that opened it, so no condition can be replayed."
    ),
    RELATION_UNKNOWN: (
        "The evaluation named no relation, so there is no address to read the rows "
        "back from."
    ),
    NOTHING_MATCHES_NOW: (
        "Nothing matches this condition now. The rows are read at the moment you "
        "unfold them, so an anomaly that has since been repaired at the source "
        "returns none."
    ),
}


def message_for(reason: str | None) -> str | None:
    """The sentence of a reason of this module, or the reader's own."""
    if not reason:
        return None
    if reason in _MESSAGES:
        return _MESSAGES[reason]
    from core import collected_mapped_reader  # noqa: PLC0415

    return collected_mapped_reader.message_for(reason)


@dataclass(frozen=True)
class ReplayProfile:
    """What one check profile can hand back, and why when it can hand back nothing."""

    check_profile: str
    replayable_rows: bool
    #: Why there are no rows, when there are none. `None` exactly when
    #: `replayable_rows` is True.
    absence_reason: str | None = None
    replay: Callable[..., dict[str, Any]] | None = None

    def as_payload(self) -> dict[str, Any]:
        return {
            "check_profile": self.check_profile,
            "replayable_rows": self.replayable_rows,
            "rows_absence_reason": self.absence_reason,
            "rows_absence_message": message_for(self.absence_reason),
        }


def _replay_null_rate(
    conn,
    *,
    project_id: str,
    datastream_id: str,
    execution_id: str,
    issue: Mapping[str, Any],
    limit: int,
) -> dict[str, Any]:
    """The rows of the run's window whose required field is NULL, as they are now.

    THE FIELD IS RECOVERED, NEVER GUESSED. ``app.dq_issues`` stores a
    ``root_cause_fingerprint`` and not the field: ``dq_null_rate`` builds it as
    ``content_hash({monitor_id, field})``, so every candidate field of the
    evaluation is hashed back and the one that reproduces the fingerprint is the
    issue's. No match is answered (:data:`FIELD_UNIDENTIFIABLE`), because
    replaying a neighbouring field would return rows that are not this anomaly's.

    THE WINDOW IS THE EVALUATION'S. ``app.dq_evaluations`` is append-only and
    carries the run (migration 222) together with ``window_start`` /
    ``window_end`` and the relation it read. The issue itself carries none of
    that: its run MOVES with ``last_seen_at`` (``dq_governance.open_issue``), so
    the evaluation is the only row that says which days this run was judged on.
    """
    from core import collected_mapped_reader, dq_null_rate  # noqa: PLC0415

    monitor_id = str(issue.get("monitor_id") or "")
    evaluation = _evaluation_of_run(
        conn,
        project_id=project_id,
        datastream_id=datastream_id,
        execution_id=execution_id,
        monitor_id=monitor_id,
    )
    if evaluation is None:
        return _no_rows(NO_EVALUATION_FOR_RUN)

    observed = _json_object(evaluation.get("observed"))
    candidates: list[str] = []
    for finding in observed.get("findings") or []:
        if isinstance(finding, Mapping) and finding.get("field"):
            candidates.append(str(finding["field"]))
    for field in observed.get("required_fields") or []:
        if str(field) not in candidates:
            candidates.append(str(field))

    fingerprint = str(issue.get("root_cause_fingerprint") or "")
    field = next(
        (
            candidate
            for candidate in candidates
            if dq_null_rate.root_cause_fingerprint(monitor_id, candidate) == fingerprint
        ),
        None,
    )
    if field is None:
        return _no_rows(FIELD_UNIDENTIFIABLE, window=_window_of(evaluation))

    relation = str(observed.get("relation") or "")
    if not relation:
        return _no_rows(RELATION_UNKNOWN, window=_window_of(evaluation))

    window = _window_of(evaluation)
    try:
        description = collected_mapped_reader.describe_relation(
            project_id=project_id,
            relation=relation,
            zone=collected_mapped_reader.ZONE_COLLECTED,
        )
        read = collected_mapped_reader.read_rows(
            description=description,
            project_id=project_id,
            start=window["start"],
            end=window["end"],
            limit=limit,
            null_field=field,
        )
    except Exception as exc:  # noqa: BLE001 -- an unreadable relation is not "no rows"
        logger.warning("dq_issue_rows: replay_failed relation=%s: %s", relation, exc)
        read = {"readable": False, "reason": collected_mapped_reader.WAREHOUSE_UNAVAILABLE}

    if not read.get("readable"):
        return _no_rows(
            str(read.get("reason") or collected_mapped_reader.WAREHOUSE_UNAVAILABLE),
            window=window,
            field=field,
            relation=relation,
        )

    rows = list(read.get("rows") or [])
    return {
        "rows": rows,
        "columns": list(read.get("columns") or []),
        "row_count": len(rows),
        "truncated": bool(read.get("truncated")),
        "masked_fields": list(read.get("masked_fields") or []),
        "field": field,
        "relation": relation,
        "window": window,
        # A reading that RAN. `reason` stays None -- the note is what says the
        # condition no longer holds, and it is not a failure.
        "reason": None,
        "message": None,
        "note": NOTHING_MATCHES_NOW if not rows else None,
        "note_message": message_for(NOTHING_MATCHES_NOW) if not rows else None,
    }


def _no_rows(
    reason: str,
    *,
    window: dict[str, str] | None = None,
    field: str | None = None,
    relation: str | None = None,
) -> dict[str, Any]:
    """A replay that produced NO reading. `rows` is None, never an empty list.

    The distinction is the whole point: `[]` means the condition was tested and
    matched nothing, `None` means nothing was tested. A screen given `[]` for both
    would print "nothing matches this condition now" over a warehouse it never
    reached.
    """
    return {
        "rows": None,
        "columns": [],
        "row_count": None,
        "truncated": False,
        "masked_fields": [],
        "field": field,
        "relation": relation,
        "window": window,
        "reason": reason,
        "message": message_for(reason),
        "note": None,
        "note_message": None,
    }


#: The ONE thing written here: which profile has a replay, and what runs it.
#: Everything else about a monitor is read from `dq_monitor_registry`.
_REPLAY_IMPLEMENTATIONS: dict[str, Callable[..., dict[str, Any]]] = {
    "null_rate": _replay_null_rate,
}


def _profile_for(monitor) -> ReplayProfile:
    """The three states, decided by the registry and by what is implemented.

    The `no_replay_implemented` entries are deliberately NOT folded into the
    `no_faulty_row_by_nature` ones: a duplicated row and a rejected date ARE
    rows, and telling a person there is nothing to show would be a false
    sentence about their data rather than a true one about this build.
    """
    if not monitor.has_faulty_rows:
        return ReplayProfile(monitor.key, False, NO_FAULTY_ROW_BY_NATURE)
    implementation = _REPLAY_IMPLEMENTATIONS.get(monitor.key)
    if implementation is None:
        return ReplayProfile(monitor.key, False, NO_REPLAY_IMPLEMENTED)
    return ReplayProfile(monitor.key, True, None, implementation)


#: One entry per DISPATCHED monitor, DERIVED and not restated. It used to be a
#: hand-written dict of seven keys whose parity with `CHECK_PROFILES` was
#: asserted by one test in another file. That is the shape
#: `test_no_layer_redeclares_the_dq_type_list` exists to refuse, and its own
#: docstring says why: an equality proves one list matches today, and proves
#: nothing about a second list standing next to it. Adding a monitor to the
#: registry now reaches this table on its own, carrying the answer its
#: `has_faulty_rows` field was required to state.
#:
#: Which profile sits on which side is READ from here, never recited -- a
#: paragraph that lists them is a paragraph that inverts them:
#:
#:     collections.Counter(p.absence_reason for p in REPLAY_PROFILES.values())
REPLAY_PROFILES: dict[str, ReplayProfile] = {
    key: _profile_for(dq_monitor_registry.BY_KEY[key])
    for key in dq_monitor_registry.DISPATCHED_KEYS
}


def profile_replay(check_profile: str | None) -> ReplayProfile:
    """What this profile can replay. An unknown one is NAMED, never defaulted quietly."""
    name = str(check_profile or "")
    known = REPLAY_PROFILES.get(name)
    if known is not None:
        return known
    return ReplayProfile(name, False, UNKNOWN_PROFILE)


# ---------------------------------------------------------------------------
# The replay itself.
# ---------------------------------------------------------------------------

_ISSUE_COLUMNS = (
    "id",
    "monitor_id",
    "status",
    "severity",
    "root_cause_fingerprint",
    "execution_id",
    "monitor_label",
    "check_profile",
)


def _read_issue(conn, *, project_id: str, datastream_id: str, issue_id: str) -> dict[str, Any]:
    """The issue, scoped to the Project AND the Datastream, or :class:`IssueNotFound`.

    NON-DISCLOSING BY CONSTRUCTION: an issue of another Project and an issue that
    never existed take the same branch and produce the same sentence. The scope is
    in the `WHERE` below, not in a comparison afterwards -- a row read wide and
    rejected later is a row that was read.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT i.id, i.monitor_id, i.status, i.severity, i.root_cause_fingerprint,
                   i.execution_id, m.label AS monitor_label, v.check_profile
              FROM app.dq_issues i
              LEFT JOIN app.dq_monitors m
                ON m.id = i.monitor_id AND m.project_id = i.project_id
              LEFT JOIN app.dq_monitor_versions v
                ON v.id = m.current_version_id AND v.monitor_id = m.id
             WHERE i.id = %s AND i.project_id = %s AND i.datastream_id = %s
            """,
            (issue_id, project_id, datastream_id)
        )
        row = cur.fetchone()
    if row is None:
        raise IssueNotFound("this anomaly is not on this Datastream")
    return dict(zip(_ISSUE_COLUMNS, row))


def _evaluation_of_run(
    conn, *, project_id: str, datastream_id: str, execution_id: str, monitor_id: str
) -> dict[str, Any] | None:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, window_start, window_end, observed, evaluated_at
              FROM app.dq_evaluations
             WHERE project_id = %s AND datastream_id = %s AND execution_id = %s
               AND monitor_id = %s
             ORDER BY evaluated_at DESC
             LIMIT 1
            """,
            (project_id, datastream_id, execution_id, monitor_id)
        )
        row = cur.fetchone()
    if row is None:
        return None
    return dict(
        zip(("id", "window_start", "window_end", "observed", "evaluated_at"), row)
    )


def _window_of(evaluation: Mapping[str, Any]) -> dict[str, str]:
    return {
        "start": _iso_day(evaluation.get("window_start")),
        "end": _iso_day(evaluation.get("window_end")),
    }


def _iso_day(value: Any) -> str:
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    return str(value or "")


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, (str, bytes, bytearray)):
        try:
            decoded = json.loads(value)
        except ValueError:
            return {}
        return decoded if isinstance(decoded, dict) else {}
    return {}


def replay_issue_rows(
    conn,
    *,
    project_id: str,
    datastream_id: str,
    execution_id: str,
    issue_id: str,
    limit: int | None = None,
) -> dict[str, Any]:
    """Replay ONE issue's condition over the window its run was judged on.

    Raises :class:`IssueNotFound` when the issue is not this Datastream's or was
    not found on this run -- the same sentence for both, so the answer discloses
    nothing about another Project's issues.
    """
    from core import collected_mapped_reader  # noqa: PLC0415

    issue = _read_issue(
        conn, project_id=project_id, datastream_id=datastream_id, issue_id=issue_id
    )
    if str(issue.get("execution_id") or "") != str(execution_id):
        raise IssueNotFound("this anomaly was not found on this run")

    profile = profile_replay(issue.get("check_profile"))
    bounded = max(1, min(int(limit or collected_mapped_reader.MAX_ROWS),
                         collected_mapped_reader.MAX_ROWS))
    head = {
        "issue_id": str(issue["id"]),
        "monitor_id": str(issue.get("monitor_id") or ""),
        "monitor_label": issue.get("monitor_label"),
        "execution_id": str(execution_id),
        "status": issue.get("status"),
        "severity": issue.get("severity"),
        # WHEN THE ROWS WERE READ, and it is not when the anomaly was found. The
        # console says the sentence; the payload carries the instant it is about.
        "replayed_at": datetime.now(tz=timezone.utc).isoformat(),
        "row_limit": bounded,
        **profile.as_payload(),
    }
    if profile.replay is None:
        return {**head, **_no_rows(profile.absence_reason or UNKNOWN_PROFILE)}
    body = profile.replay(
        conn,
        project_id=project_id,
        datastream_id=datastream_id,
        execution_id=str(execution_id),
        issue=issue,
        limit=bounded,
    )
    return {**head, **body}


def rows_csv(payload: Mapping[str, Any]) -> bytes:
    """The replayed rows, and NOTHING the replay did not return.

    The header is the reading's own column list and every cell comes from the row
    the reader handed over -- masked values included, as `[MASKED]`, because the
    file must not be able to say more than the screen did. A replay that produced
    no reading at all has no file: the caller refuses before it reaches here.
    """
    import csv  # noqa: PLC0415
    import io  # noqa: PLC0415

    columns = [str(column) for column in payload.get("columns") or []]
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(columns)
    for row in payload.get("rows") or []:
        writer.writerow(["" if row.get(column) is None else row.get(column) for column in columns])
    return output.getvalue().encode("utf-8")


# ---------------------------------------------------------------------------
# The acknowledgement, on `app.dq_issues` and never on `app.alert_firings`.
# ---------------------------------------------------------------------------


def transition_run_issue(
    conn,
    *,
    project_id: str,
    datastream_id: str,
    issue_id: str,
    event_kind: str,
    actor: str,
    reason: str | None = None,
    suppressed_until: date | None = None,
) -> dict[str, Any]:
    """Move ONE issue of this Datastream, then answer the status the DATABASE holds.

    THE OBJECT IS ``app.dq_issues``, WHICH NOTHING MOUNTED COULD REACH.
    ``POST /api/dq/issues/{firing_id}/acknowledge`` updated
    ``app.alert_firings.acknowledged_at`` -- a different table under the same
    word -- and story 49.4 unmounted it for reasons it names
    (`admin_api.py#api/dq`). ``dq_governance.transition_issue`` had zero
    application callers until this one.

    The two refusals happen BEFORE the write and are two different answers:
    an event this schema cannot hold, and a suppression with no end date
    (``ck_dq_issues_suppression``), are `invalid_status`; an issue outside this
    Project or this Datastream is `not_found`, non-disclosing.

    The returned status is READ BACK, never composed from the map above: what the
    console then displays is what the row holds.
    """
    from core.dq_governance import ISSUE_STATUS_FOR_EVENT, transition_issue  # noqa: PLC0415

    kind = str(event_kind or "")
    if kind not in ISSUE_STATUS_FOR_EVENT:
        raise InvalidIssueStatus(
            f"event_kind must be one of {sorted(ISSUE_STATUS_FOR_EVENT)}"
        )
    if kind == "suppressed" and suppressed_until is None:
        raise InvalidIssueStatus(
            "a suppression needs an end date: a permanent one is a silent hole"
        )
    issue = _read_issue(
        conn, project_id=project_id, datastream_id=datastream_id, issue_id=issue_id
    )
    transition_issue(
        conn,
        project_id=project_id,
        issue_id=str(issue["id"]),
        event_kind=kind,
        actor=actor,
        reason=str(reason or f"{kind} from the run this anomaly was found on"),
        suppressed_until=suppressed_until,
    )
    with conn.cursor() as cur:
        cur.execute("SELECT status FROM app.dq_issues WHERE id = %s", (str(issue["id"]),))
        row = cur.fetchone()
    return {"issue_id": str(issue["id"]), "status": str(row[0]) if row else None}
