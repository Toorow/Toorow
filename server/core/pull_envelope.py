"""What a pull envelope may say BESIDES its row count -- and the one place that reads it.

WHY THIS MODULE EXISTS. `server/modules/README.md` fixes four keys a pull must
return (`pull_id`, `row_count`, `date_from`, `date_to`) and `queue._execute_job`
read exactly one of them::

    row_count = result.get("row_count", 0) if isinstance(result, dict) else 0
    _finish_job(conn, job, DONE, row_count=row_count, actor=requested_by)

So a pull the provider REFUSED before it ever ran -- a Business Profile project
still at 0 QPM, a legacy host the project is not allowlisted on -- was written
`done / 0 row`, letter for letter the row a pull that ran and found nothing
writes. One connector had already built the envelope that says otherwise
(`google-business-profile`, keys `skipped` / `skip_reason` / `message`) and its
docstring promised "the worker logs an honest zero"; measured 2026-08-21, that
envelope had NO reader anywhere in `server/`, `ui/` or `web/`.

THE FALSE ZERO IS NOT COSMETIC, AND THIS IS WHY THE MODULE IS IN CORE. `done / 0`
publishes `FACT_PULL_LANDED` with `row_count: 0`, the verification subscriber
counts no rows, files verdict `empty`, and `empty` raises the STICKY
`populate_failed` the enqueue gate reads (migration 007). One un-granted quota
therefore refuses every Datastream behind the whole authorization -- the closure
AI-302 measured twice in production, reached through the door AI-302 did not
cover. AI-302 stopped a COUNTER from answering `0` without having read; this
stops a WINDOW from answering `0` without having been allowed to run.

WHY IT IS A CORE CONTRACT AND NOT A CONNECTOR HABIT. 39 connectors face the same
class of provisioning gate -- an approval queue at the provider, an allowlist, a
scope not yet granted. Leaving each of them to invent the shape would give 39
shapes and one reader that understands none. Both halves live here: the module
BUILDS with `prevented_envelope()`, the worker READS with `prevented_by()`, and a
connector still declares its own reasons and its own sentences at home
(`docs/product-architecture/execution-substrate.md`, AI-307).

WHAT A SENTENCE MAY SAY, AND WHY IT MAY BE ECHOED. `app.pull_jobs.error_detail`
is never surfaced raw because a provider error can carry a payload, a stack trace
or an address (`datastream_diagnosis._canonical_error`). A prevented sentence is
NOT that: it is authored in the connector module, from a constant, and never read
off the wire. It is bounded here all the same -- ASCII, one line, 400 characters --
so the rule holds even if a future author interpolates something from a response.

AND IT NAMES THE GESTURE, NEVER THE CAUSE. "403 on the v4 host" is the technical
reason; "ask Google for the reviews allowlist grant on this project, then re-ask
these dates" is the gesture. The second is the only one a person can act on, and
the repository rule (`CLAUDE.md`, "L'ecran") asks for it on every message.

A REFUSAL ALSO ARRIVES AS AN EXCEPTION, AND THAT DOOR WAS SHUT UNTIL 2026-08-25.
`prevented_envelope()` / `prevented_by()` cover the pull that RETURNS -- which a
connector can only do on a profile it is allowed to abandon. The profile that
must land rows cannot return an envelope: an empty day is not a day, so it
RAISES. `google-business-profile` had said exactly that since it shipped
(`connector.py:348-350`, "the core profile does NOT skip ... it raises
permission_denied") and marked the raised error with `precondition` /
`precondition_message` for a reader to find. Measured 2026-08-24, `grep -rn
"precondition" server/ ui/ web/` outside that module returned other subjects
only: the two attributes had NO reader, so the DEFAULT pull of that connector --
`pull_location_daily`, a project still at 0 QPM, i.e. every Business Profile
project on its first day -- was recorded `failed / permission_denied` with
`user_action = "reconnect"`. Reconnecting releases nothing: the grant is a manual
approval at Google. And `failed` is what `scheduler._reschedule_failed_pulls`
re-arms hourly, so the crash loop against a human approval queue that `prevented`
exists to stop was reached through the raising door instead of the returning one.

So `prevented_by_error()` is the second reader, and it is the SAME contract: any
connector may mark ANY raised error with those two attributes, `queue._execute_job`
reads them before it classifies the exception, and the window is recorded
`prevented` with the connector's own sentence -- same envelope, same transport,
same screen. Nothing here is Business-Profile-shaped; that connector is only the
first of the 39 to have needed it.
"""

from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass

logger = logging.getLogger(__name__)

#: The key that says "this window was not allowed to run". Truthy and nothing
#: else: a connector that omits it has returned an ordinary pull.
PREVENTED_KEY = "prevented"
#: The machine token naming WHICH gate refused. Stable, lowercase, connector-owned.
PREVENTED_REASON_KEY = "prevented_reason"
#: The sentence a person reads. Connector-authored, bounded by this module.
PREVENTED_MESSAGE_KEY = "message"

#: The historic spelling, shipped by `google-business-profile` before this module
#: existed. READ, never written: an author copying that connector writes
#: `skipped` by reflex, and a reader that ignored it would restore the exact
#: silence AI-307 closed. `prevented_envelope` is the only writer, and it writes
#: the canonical key alone.
LEGACY_PREVENTED_KEY = "skipped"
LEGACY_PREVENTED_REASON_KEY = "skip_reason"

#: The two attributes a connector sets on a RAISED error to say the same thing:
#: "this refusal is a provisioning gate, not a fault". They are spelled here
#: because the reader is here -- a connector author sets what `prevented_by_error`
#: reads, and neither side has to know the other's file.
PRECONDITION_ATTR = "precondition"
PRECONDITION_MESSAGE_ATTR = "precondition_message"

#: A reason is an identifier, not prose: it is compared, logged and stored.
_REASON_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")

#: What fits on one line of a row that also carries a date range and a control.
MESSAGE_MAX_CHARS = 400

#: The reason a window carries when its connector claimed a refusal and named
#: neither a gate nor a gesture. RESERVED to this module: no connector may author
#: it, and `_REASON_RE` accepts it so the stored row is readable like any other.
INCOMPLETE_REASON = "envelope_incomplete"

#: What the console says over such a window. It names a gesture, because the
#: person in front of the screen has one -- and it does NOT invent the grant,
#: because the connector never said which one it is.
INCOMPLETE_MESSAGE = (
    "The source did not allow this collection. This connector did not name the "
    "access that releases it, so there is nothing to request yet -- report this "
    "connector."
)


class PullEnvelopeError(ValueError):
    """A connector built a prevented envelope that cannot be recorded."""


@dataclass(frozen=True)
class PreventedPull:
    """A window the source did not allow to run, and what to say about it."""

    #: The connector's machine token, e.g. ``reviews_access_pending``.
    reason: str
    #: The sentence, already bounded. Names the gesture that releases the gate.
    message: str


def _ascii_one_line(text: str) -> str:
    """Fold *text* to a single line of ASCII (AI-03), never raising on input.

    Accented characters are decomposed and their marks dropped rather than
    replaced by ``?``: a sentence is still readable without its accents, and it
    is not readable as ``Acc?s refus?``.
    """
    folded = unicodedata.normalize("NFKD", text)
    ascii_text = folded.encode("ascii", "ignore").decode("ascii")
    return " ".join(ascii_text.split())


def _bounded_sentence(value) -> str:
    """A sentence folded to one ASCII line and clamped -- or "" for a non-sentence.

    THE TYPE IS CHECKED BEFORE ANYTHING IS SAID, and that is the repair. All
    three call sites read `_ascii_one_line(str(value))`, so a value that was not
    a string was STRINGIFIED rather than refused: measured 2026-08-21,
    ``prevented_pair('{"prevented_reason": "gate", "prevented_message": {"a": 1}}')``
    answered ``(gate, "{'a': 1}")`` -- a Python `repr` published by the one
    boundary whose docstring promises the bound holds "including on rows written
    before those bounds existed". A dict is not a sentence; it is a malformed
    field, and a malformed field degrades to the reserved sentence like every
    other one.

    The clamp lives here too, once. It was written out three times, and E36-NFR01
    holds only if every publisher applies it -- three copies is two chances to
    drop one.
    """
    if not isinstance(value, str):
        return ""
    sentence = _ascii_one_line(value)
    if len(sentence) > MESSAGE_MAX_CHARS:
        sentence = sentence[: MESSAGE_MAX_CHARS - 3].rstrip() + "..."
    return sentence


def prevented_envelope(
    *,
    pull_id: str,
    date_from: str,
    date_to: str,
    reason: str,
    message: str,
) -> dict:
    """Build the envelope of a window the source did not allow to run.

    Carries the four keys every pull owes (`server/modules/README.md`) so the
    contract conformance suite stays satisfied, plus the two this module owns.

    ``row_count`` is 0 HERE and NULL in the database, and the two are not in
    conflict: the envelope is the shape a pull returns, and every reader of it
    expects the key to exist. What must never become a stored `0` is the COUNT,
    and `queue._execute_job` is what refuses to store it -- a prevented window
    calls `_finish_job(..., row_count=None)` and the column keeps its NULL.

    Raises `PullEnvelopeError` on an unusable reason or an empty sentence: a
    prevented window that cannot say which gate stopped it, or cannot say what
    to do, is exactly the silence this module exists to remove, and failing the
    pull is more honest than recording an unreadable skip.
    """
    if not _REASON_RE.match(str(reason or "")):
        raise PullEnvelopeError(
            f"prevented reason {reason!r} is not a lowercase identifier"
        )
    sentence = _bounded_sentence(message)
    if not sentence:
        raise PullEnvelopeError(f"prevented reason {reason!r} carries no sentence")
    return {
        "pull_id": pull_id,
        "row_count": 0,
        "date_from": date_from,
        "date_to": date_to,
        PREVENTED_KEY: True,
        PREVENTED_REASON_KEY: reason,
        PREVENTED_MESSAGE_KEY: sentence,
    }


def prevented_by(result, *, module_name: str = "") -> PreventedPull | None:
    """Read a prevented envelope, or None when *result* is an ordinary pull.

    THE ONE READER. `queue._execute_job` calls it once, between the pull and the
    terminal write, which is what makes this a repair of the whole connector
    class rather than of the connector that needed it first.

    ONE CONTRADICTION FAILS TOWARD `done`, AND IT IS THE ONE WHERE ROWS EXIST.
    An envelope that declares itself prevented AND reports rows is not a skip:
    something landed, and dropping it would lose data that exists. The claim is
    refused, the pull is recorded `done` with its rows, and the contradiction is
    logged at WARNING naming the connector -- the envelope is a bug in that
    module and somebody has to be able to find it.

    EVERY OTHER MALFORMED ENVELOPE FAILS TOWARD THE REFUSAL. This reader first
    shipped answering None for an envelope with no reason or no sentence, and
    "None" is not neutral here: it hands the window straight back to
    `_finish_job(DONE, row_count=0)`, which is the false zero this module exists
    to remove, with `empty` -> sticky `populate_failed` behind it. What the
    connector ASSERTED by setting the key is that the source did not allow the
    window; a missing token or a missing sentence is a bug in its METADATA, not
    evidence that the pull ran. So the assertion is kept, `INCOMPLETE_REASON`
    carries the defect where a person can read it, and the WARNING still names
    the connector. Nothing re-arms: `prevented` is in `ATTEMPTED_JOB_STATES`.
    """
    if not isinstance(result, dict):
        return None

    claimed = bool(result.get(PREVENTED_KEY) or result.get(LEGACY_PREVENTED_KEY))
    if not claimed:
        return None

    landed = result.get("row_count")
    # A COUNT IS A NUMBER, AND THE EXCEPTION HANGS ON IT. This read
    # `int(result.get("row_count") or 0)`, and `int()` accepts `"5"` and `True`:
    # measured 2026-08-21, `{"prevented": True, "row_count": "5"}` and
    # `{"prevented": True, "row_count": True}` both answered None, i.e. took the
    # one exit this module keeps toward `done` -- and `queue._execute_job` then
    # wrote `row_count="5"` into the column verbatim. The exception exists
    # because ROWS EXIST and losing them is worse than either state; a string is
    # not proof that anything landed, it is one more malformed field, and every
    # malformed field fails toward the refusal. `bool` is excluded explicitly
    # because it IS an `int` in Python and `True` is not a row.
    if isinstance(landed, bool) or not isinstance(landed, (int, float)):
        landed = 0
    if landed > 0:
        logger.warning(
            "pull_envelope: prevented_envelope_claims_rows module=%s row_count=%d "
            "-- recorded as a landed pull; fix the connector",
            module_name or "unknown",
            int(landed),
        )
        return None

    return _prevented_claim(
        result.get(PREVENTED_REASON_KEY) or result.get(LEGACY_PREVENTED_REASON_KEY),
        result.get(PREVENTED_MESSAGE_KEY),
        module_name=module_name,
        kind="prevented_envelope",
    )


def _prevented_claim(
    reason, message, *, module_name: str, kind: str
) -> PreventedPull:
    """Turn a connector's CLAIMED refusal into a publishable one -- both readers.

    The claim has already been established by the caller (a truthy key on an
    envelope, a truthy attribute on an error). What is left is the same in both
    cases: two halves that may each be missing or malformed, and a bound that must
    hold on whatever is handed back.

    THE TWO HALVES DEGRADE INDEPENDENTLY, and that is not a detail. The reason is
    a MACHINE token -- compared, logged, stored -- and nobody reads it on a
    screen; the sentence is the only half a person acts on. Folding a bad token
    and a good sentence into one "incomplete" outcome would throw away a
    perfectly usable gesture over a typo in a word no reader ever sees.

    *kind* names the door in the log event key (`prevented_envelope_*` /
    `prevented_error_*`), because "which connector returned a bad envelope" and
    "which connector raised a bad precondition" are two different repairs.
    """
    reason = reason if isinstance(reason, str) else ""
    if not _REASON_RE.match(reason) or reason == INCOMPLETE_REASON:
        logger.warning(
            "pull_envelope: %s_without_reason module=%s reason=%r "
            "-- recorded prevented under %s; fix the connector",
            kind,
            module_name or "unknown",
            reason,
            INCOMPLETE_REASON,
        )
        reason = INCOMPLETE_REASON

    sentence = _bounded_sentence(message)
    if not sentence:
        logger.warning(
            "pull_envelope: %s_without_sentence module=%s reason=%s "
            "-- recorded prevented with the reserved sentence; fix the connector",
            kind,
            module_name or "unknown",
            reason,
        )
        sentence = INCOMPLETE_MESSAGE

    return PreventedPull(reason=reason, message=sentence)


def prevented_by_error(exc, *, module_name: str = "") -> PreventedPull | None:
    """Read a precondition off a RAISED error, or None for an ordinary failure.

    THE SECOND READER, AND THE ONE THE DEFAULT PULL NEEDED. `prevented_by` can
    only see a pull that RETURNED, which a connector may only do on a profile it
    is allowed to abandon. The profile that must land rows raises instead -- an
    empty day reported as a success would fabricate a day -- so its refusal
    reached `except ConnectorError` and was classified `permission_denied /
    reconnect / failed`. Three wrong words at once: nothing is denied to this
    credential, reconnecting releases nothing, and `failed` is what
    `scheduler._reschedule_failed_pulls` re-arms every hour against a grant only a
    human at the provider can give.

    WHAT THE CONNECTOR ASSERTS, AND WHAT IT DOES NOT. Setting `precondition` says
    "the source refused this window because an access has not been provisioned",
    and nothing else: the error keeps its class, its status and its payload, and
    every error that carries no precondition takes the failure path untouched.
    A connector that never sets the attribute cannot be changed by this reader.

    Degradation is `prevented_by`'s, for the same reason: a missing token or a
    missing sentence is a bug in the connector's METADATA, not evidence that the
    window was allowed to run. There is no counterpart here to the "claims rows"
    exception -- an exception landed no rows, so nothing exists to be lost.
    """
    claimed = getattr(exc, PRECONDITION_ATTR, None)
    if not claimed:
        return None
    return _prevented_claim(
        claimed,
        getattr(exc, PRECONDITION_MESSAGE_ATTR, None),
        module_name=module_name,
        kind="prevented_error",
    )


def prevented_pair(error_detail) -> tuple[str | None, str | None]:
    """Read (reason, sentence) back off a stored PREVENTED window -- the ONE reader.

    `queue._execute_job` writes the two keys into `app.pull_jobs.error_detail` as
    JSON; three surfaces read them back (the MCP diagnosis timeline, the extract
    ledger, and the day grid that publishes from it). Parsing them in three
    places would put the E36-NFR01 bound in three places too, which is one edit
    away from a surface that echoes a provider blob because it forgot a clamp.

    WHY ECHOING THIS DETAIL IS NOT A HOLE IN E36-NFR01. The invariant forbids
    echoing `error_detail` because a FAILED window's detail carries provider
    payloads, stack traces, SQL and addresses. A prevented window's detail
    carries neither: it is authored in a connector module as a constant, never
    read off the wire, and bounded at construction. The clamps are applied AGAIN
    here so the guarantee holds at the boundary that publishes and not only at
    the one that wrote -- including on rows written before those bounds existed.

    Answers (None, None) for anything unparseable: an absence a screen can say
    is honest, a half-parsed sentence is not.
    """
    import json as _json  # noqa: PLC0415

    if not error_detail or not isinstance(error_detail, str):
        return None, None
    try:
        parsed = _json.loads(error_detail)
    except (ValueError, TypeError):
        return None, None
    if not isinstance(parsed, dict):
        return None, None

    reason = parsed.get("prevented_reason")
    reason = reason if isinstance(reason, str) and _REASON_RE.match(reason) else None

    sentence = _bounded_sentence(parsed.get("prevented_message")) or None
    return reason, sentence
