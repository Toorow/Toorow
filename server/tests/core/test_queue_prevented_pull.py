"""A pull the SOURCE refused is not a pull that returned nothing -- AI-307.

WHAT WAS FALSE IN PRODUCTION. `queue._execute_job` read one key of the pull
envelope::

    row_count = result.get("row_count", 0) if isinstance(result, dict) else 0
    _finish_job(conn, job, DONE, row_count=row_count, actor=requested_by)

so a pull refused BEFORE it ever ran -- `reviews` on a Business Profile project
that is not on the legacy-host allowlist, any surface of a project still at
0 QPM -- was written `done / 0 row`. That is, letter for letter, the row written
by a pull that ran and honestly found nothing. Nobody could tell the two apart,
and the connector had been saying which one it was all along: it returned
`skipped`, `skip_reason` and `message`, and measured 2026-08-21 those keys had NO
reader anywhere in `server/`, `ui/` or `web/`.

AND THE ZERO TRAVELLED. `done / 0` publishes `FACT_PULL_LANDED` with
`row_count: 0`; the verification subscriber counts no rows, files verdict
`empty`; `empty` raises the STICKY `populate_failed` the enqueue gate reads. One
un-granted quota therefore refused every Datastream behind the whole
authorization. That is why the assertions below are about a STATE and about the
ABSENCE of a stored zero, not about a log line.

WHAT THESE TESTS PIN, and each one goes red if the branch in `_execute_job` is
removed:

  1. a prevented envelope is recorded `prevented`, never `done`;
  2. it writes NO `row_count` -- the column keeps its NULL, because `0` is a
     count that was taken and this window took none;
  3. it writes its own audit action, and NOT `pull.completed`;
  4. an honest empty pull is untouched: still `done`, still `row_count = 0`;
  5. the two are therefore two different rows, which is the whole point;
  6. the reader is the SHARED one, so the repair is the class of 39 connectors
     and not the one connector that needed it first;
  7. and (2026-08-25) the SECOND door: a refusal that arrives as a raised error
     rather than as a returned envelope -- which is the only door the DEFAULT
     profile of a connector has, since a pull that must land rows cannot report
     a refusal as a success. Section 7 below carries that measurement.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

import pytest

from tests.support.statement_router import StatementInventory, UnknownStatement, describe

# ---------------------------------------------------------------------------
# The harness: drive the real `_execute_job` against a recording connection.
# ---------------------------------------------------------------------------

# EVERY STATEMENT ONE JOB RUN MAKES. The fake used to answer the connection-ref
# row to ANY read -- `fetchone` returned it unconditionally, with a four-column
# description that had nothing to do with the statement -- so a read of any other
# table came back as a connection reference and the run carried on (AI-317).
_JOB_RUN = StatementInventory(
    "_run_job.Cursor",
    job_write=("update", "pull_jobs"),
    connection_ref=("select id, nango_connection_id", "from app.connection_ref"),
    # THE ACCOUNTS THIS CONSENT VERIFIED FOR THIS CONNECTOR (AI-327). The read
    # has always happened here -- `_resolve_selected_account` -- and the fake was
    # never taught it: `UnknownStatement` fell into that function's "best effort,
    # never fails a pull" `except`, which answered `None`. The resolver refuses
    # instead of degrading now, so the question has to be answered. This subject
    # is about what a REFUSED pull writes on the job row, not about accounts: no
    # row, which says "this consent verified nothing".
    connector_ready_accounts=(
        "select s.account_id, ca.discovered_for_connector",
        "from app.connection_account_scope s",
    ),
)


class _Recorder:
    """Everything the worker wrote on the job row, statement by statement."""

    def __init__(self):
        #: (sql, params) of every UPDATE on `app.pull_jobs`.
        self.job_writes: list[tuple[str, tuple]] = []

    @property
    def terminal_write(self) -> tuple[str, tuple]:
        """The statement that put the window in a state it cannot leave.

        Read from the PARAMETERS, never from the SQL literal: since story 63.1
        `_finish_job` composes its assignment list and binds the state, so a test
        reading the literal would be reading how the statement is spelled.
        """
        from core.pull_job_states import TERMINAL_JOB_STATES

        for sql, params in self.job_writes:
            if any(p in TERMINAL_JOB_STATES for p in params):
                return sql, params
        raise AssertionError(
            "no terminal write on the job row; statements were %r" % (self.job_writes,)
        )

    @property
    def terminal_state(self) -> str:
        from core.pull_job_states import TERMINAL_JOB_STATES

        _sql, params = self.terminal_write
        return next(p for p in params if p in TERMINAL_JOB_STATES)


def _run_job(pull_result=None, *, raises=None, job_id="job_ai307", pull_id="pull_ai307"):
    """Execute one job whose connector returns *pull_result* -- or raises *raises*.

    The second door is the one the DEFAULT profile of a connector uses: a pull
    that must land rows cannot report a refusal as a success, so it raises.
    """
    recorder = _Recorder()
    conn_ref_row = ("conn_ref_ai307", "nango-conn-ai307", "test-provider", "proj-ai307")

    class Cursor:
        def __init__(self):
            self.description = None
            self._one = None

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def execute(self, sql, params=None):
            statement = _JOB_RUN.match(sql)
            if statement == "job_write":
                recorder.job_writes.append((str(sql), tuple(params or ())))
                self.description = None
                self._one = None
                return
            if statement == "connection_ref":
                # DERIVED from the statement rather than restated: the four names
                # used to be typed here, and this fake answered THIS row to every
                # read it was given, including reads of other tables (AI-317).
                self.description = describe(sql)
                self._one = conn_ref_row
                return
            self.description = None
            self._one = None

        def fetchone(self):
            return self._one

        def fetchall(self):
            return []

    class Conn:
        def cursor(self):
            return Cursor()

        def commit(self):
            pass

        def rollback(self):
            pass

        def close(self):
            pass

    @contextmanager
    def _fake_get_connection():
        yield Conn()

    job = {
        "id": job_id,
        "pull_id": pull_id,
        "connection_ref_id": "conn_ref_ai307",
        "date_from": "2026-07-01",
        "date_to": "2026-07-07",
        "requested_by": "test-user",
        "attempt_count": 0,
    }
    audit = MagicMock()
    pull_fn = (
        MagicMock(side_effect=raises)
        if raises is not None
        else MagicMock(return_value=pull_result)
    )

    with patch("core.db.get_connection", new=_fake_get_connection), \
         patch("core.main.get_module_pull_fn", return_value=pull_fn), \
         patch("core.audit.write_audit_row", audit):
        from core.queue import _execute_job  # noqa: PLC0415

        _execute_job(job)

    return recorder, audit


def _prevented_result():
    """What a connector returns when the provider refused the window."""
    from core.pull_envelope import prevented_envelope  # noqa: PLC0415

    return prevented_envelope(
        pull_id="pull_ai307",
        date_from="2026-07-01",
        date_to="2026-07-07",
        reason="reviews_access_pending",
        message=(
            "Reviews are not collected yet: they are served by a separate host "
            "that is allowlisted on its own. Request the reviews allowlist for "
            "this project, then re-ask these dates."
        ),
    )


def _empty_result():
    """What a connector returns when it ran and the source had nothing."""
    return {
        "pull_id": "pull_ai307",
        "row_count": 0,
        "date_from": "2026-07-01",
        "date_to": "2026-07-07",
    }


# ---------------------------------------------------------------------------
# 1-3. The refused window.
# ---------------------------------------------------------------------------


def test_the_job_run_fake_refuses_a_statement_the_worker_never_declared():
    """AI-317: this fake answered the connection reference to EVERY read.

    `fetchone` returned `conn_ref_row` unconditionally, under a four-column
    description typed by hand. Any other read the worker makes -- a schedule
    row, a datastream row -- therefore came back as a connection reference with
    the right arity, and the run went on. Refusing is what makes the fixture say
    which read it was never taught.
    """
    with pytest.raises(UnknownStatement) as raised:
        _JOB_RUN.match("SELECT next_run_at FROM app.datastream_schedule_state WHERE id=%s")
    assert "app.datastream_schedule_state" in str(raised.value)
    assert "connection_ref" in str(raised.value)


def test_a_refused_pull_is_not_recorded_done():
    """THE DEFECT ITSELF. Remove the branch in `_execute_job` and this reads `done`."""
    from core.pull_job_states import DONE, PREVENTED  # noqa: PLC0415

    rec, _audit = _run_job(_prevented_result())
    assert rec.terminal_state == PREVENTED
    assert rec.terminal_state != DONE


def test_a_refused_pull_stores_no_row_count_at_all():
    """NOT `0`. A zero is a count that was taken; this window took none.

    `_finish_job` only adds `row_count = %s` to its assignment list when it is
    given one, so the honest write is the ABSENCE of the assignment -- the column
    keeps whatever NULL it had, and no total can absorb a number nobody measured.
    """
    rec, _audit = _run_job(_prevented_result())
    sql, params = rec.terminal_write
    assert "row_count" not in sql, sql
    assert 0 not in params, params


def test_a_refused_pull_carries_the_reason_and_the_sentence():
    """The connector's own words reach the row, so a screen can say them."""
    rec, _audit = _run_job(_prevented_result())
    _sql, params = rec.terminal_write
    detail = next(p for p in params if isinstance(p, str) and p.startswith("{"))
    parsed = json.loads(detail)
    assert parsed["prevented_reason"] == "reviews_access_pending"
    # THE MESSAGE NAMES THE GESTURE, not the cause. "403" is not actionable;
    # "request the allowlist, then re-ask these dates" is.
    assert "Request the reviews allowlist" in parsed["prevented_message"]
    assert "403" not in parsed["prevented_message"]


def test_a_refused_pull_is_audited_as_refused_never_as_completed():
    from core.queue import (  # noqa: PLC0415
        ACTION_PULL_COMPLETED,
        ACTION_PULL_PREVENTED,
    )

    _rec, audit = _run_job(_prevented_result())
    written = [str(call) for call in audit.call_args_list]
    assert any(ACTION_PULL_PREVENTED in row for row in written), written
    assert not any(ACTION_PULL_COMPLETED in row for row in written), written


# ---------------------------------------------------------------------------
# 4-5. The honest zero, and the fact that they are two different rows.
# ---------------------------------------------------------------------------


def test_an_honest_empty_pull_is_still_done_with_a_measured_zero():
    """The other half of the contract: this behaviour must NOT have moved.

    A window that ran and found nothing DID take a count, and that count is 0. It
    stays `done`, it stores its zero, and it keeps every downstream consumer.
    """
    from core.pull_job_states import DONE  # noqa: PLC0415

    rec, audit = _run_job(_empty_result())
    assert rec.terminal_state == DONE
    sql, params = rec.terminal_write
    assert "row_count" in sql, sql
    assert 0 in params

    from core.queue import ACTION_PULL_COMPLETED  # noqa: PLC0415

    assert any(ACTION_PULL_COMPLETED in str(c) for c in audit.call_args_list)


def test_the_two_zeros_are_two_different_rows():
    """The sentence of the whole story, as one assertion."""
    prevented, _ = _run_job(_prevented_result())
    empty, _ = _run_job(_empty_result())
    assert prevented.terminal_state != empty.terminal_state
    assert prevented.terminal_write[1] != empty.terminal_write[1]


# ---------------------------------------------------------------------------
# 6. The reader is the class's, not one connector's.
# ---------------------------------------------------------------------------


def test_the_reader_accepts_the_spelling_that_shipped_before_it_existed():
    """`google-business-profile` shipped `skipped` / `skip_reason`.

    An author copying that connector writes them by reflex, and a reader that
    ignored them would restore the exact silence this story closed.
    """
    from core.pull_envelope import prevented_by  # noqa: PLC0415

    legacy = {
        "pull_id": "p",
        "row_count": 0,
        "date_from": "2026-07-01",
        "date_to": "2026-07-07",
        "skipped": True,
        "skip_reason": "google_access_pending",
        "message": "Request Business Profile quota for this project, then re-ask.",
    }
    read = prevented_by(legacy, module_name="legacy-connector")
    assert read is not None
    assert read.reason == "google_access_pending"


def test_an_ordinary_pull_is_never_read_as_prevented():
    from core.pull_envelope import prevented_by  # noqa: PLC0415

    for result in (
        {"pull_id": "p", "row_count": 12},
        {"pull_id": "p", "row_count": 0},
        {"pull_id": "p", "row_count": 0, "prevented": False},
        None,
        "not a dict",
        42,
    ):
        assert prevented_by(result) is None, result


def test_an_envelope_that_claims_rows_AND_a_refusal_keeps_its_rows():
    """Fail-closed, and the rows win: something landed, so nothing may be lost.

    The claim is a bug in that connector, so it is refused and logged -- but the
    pull is recorded as the landing it was.
    """
    from core.pull_envelope import prevented_by  # noqa: PLC0415

    contradiction = {
        "pull_id": "p",
        "row_count": 112,
        "prevented": True,
        "prevented_reason": "google_access_pending",
        "message": "Request the quota, then re-ask these dates.",
    }
    assert prevented_by(contradiction, module_name="broken") is None


def test_a_malformed_refusal_still_fails_toward_the_refusal_and_not_toward_done():
    """`None` is NOT the neutral answer here -- it is the false zero, restored.

    This assertion used to read `is None`, and pinned the degradation instead of
    refusing it: answering None sends the window straight back to
    `_finish_job(DONE, row_count=0)`, i.e. the exact `done / 0` the module exists
    to remove, with `empty` -> sticky `populate_failed` behind it. What the
    connector ASSERTED by setting the key is that the source did not allow the
    window; a missing token or a missing sentence is a bug in its metadata, not
    evidence that the pull ran. The assertion is kept, the defect is carried
    where a person can read it, and no connector that never claims a refusal is
    touched (`test_an_ordinary_pull_is_never_read_as_prevented`).
    """
    from core.pull_envelope import (  # noqa: PLC0415
        INCOMPLETE_MESSAGE,
        INCOMPLETE_REASON,
        prevented_by,
    )

    for bad, expected_reason in (
        # no reason, no sentence
        ({"row_count": 0, "prevented": True}, INCOMPLETE_REASON),
        # a token that is not an identifier
        ({"row_count": 0, "prevented": True,
          "prevented_reason": "Reviews Pending!"}, INCOMPLETE_REASON),
        # a usable token, and nothing to say: the token is KEPT, and the sentence
        # is the half that degrades.
        ({"row_count": 0, "prevented": True,
          "prevented_reason": "ok", "message": "  "}, "ok"),
    ):
        read = prevented_by(bad, module_name="broken")
        assert read is not None, bad
        assert read.reason == expected_reason, bad
        # A sentence that names a gesture the person in front of the screen
        # actually has -- never a blank where an instruction belongs.
        assert read.message == INCOMPLETE_MESSAGE, bad


# ---------------------------------------------------------------------------
# 6bis. The bounds are PROPERTIES of what is published, not a constant compared
#       to itself.
# ---------------------------------------------------------------------------
#
# `assert read.message == INCOMPLETE_MESSAGE` reads like a contract and is a
# tautology: the module hands back its own constant and the test imports the same
# one, so the two sides move together. Measured 2026-08-21, three mutations that
# left the whole suite GREEN at 503 passed:
#
#   * the 400-character clamp removed from the read boundary;
#   * `_ascii_one_line` removed, so a stored sentence keeps its newlines;
#   * `INCOMPLETE_MESSAGE` reduced to "The source did not allow this
#     collection." -- a sentence that names NO gesture, i.e. exactly what this
#     whole module exists to stop being published.
#
# The E36-NFR01 argument in `prevented_pair`'s docstring therefore had no teeth.
# What follows asserts the PROPERTIES a sentence must hold to be publishable, so
# a wording may change freely and a bound may not.

#: The words a gesture is named with here. Not a style rule: `CLAUDE.md` asks
#: every message to name what to DO, and "the source did not allow this
#: collection" names only what happened. A sentence carrying none of these asks
#: the reader to act on nothing.
#: "allow" is NOT one of them, and the omission is the measurement: « the source
#: did not allow this collection » contains it and names nothing to do. A word
#: that appears in the STATE cannot certify the gesture.
_GESTURE_WORDS = (
    "request", "ask", "report", "grant", "enable", "contact", "open", "renew",
    "authorize", "authorise",
)


def _assert_publishable(sentence: str, origin: str) -> None:
    """The four bounds `pull_envelope` promises on everything it publishes."""
    from core.pull_envelope import MESSAGE_MAX_CHARS  # noqa: PLC0415

    assert sentence, f"{origin}: nothing to publish"
    assert sentence.isascii(), f"{origin}: not ASCII -- {sentence!r}"
    assert "\n" not in sentence and "\r" not in sentence, f"{origin}: more than one line"
    assert "  " not in sentence, f"{origin}: folded whitespace left -- {sentence!r}"
    assert len(sentence) <= MESSAGE_MAX_CHARS, f"{origin}: {len(sentence)} characters"
    assert sentence == sentence.strip(), f"{origin}: untrimmed"


def _assert_names_a_gesture(sentence: str, origin: str) -> None:
    lowered = sentence.lower()
    assert any(word in lowered for word in _GESTURE_WORDS), (
        f"{origin} states what happened and names nothing to do: {sentence!r}"
    )


def test_the_reserved_sentence_names_a_gesture_and_holds_the_bounds():
    """The property, on the one sentence no connector authored.

    Reduce it to "The source did not allow this collection." and this reddens --
    which is the mutation the equality assertion below it slept through.
    """
    from core.pull_envelope import INCOMPLETE_MESSAGE  # noqa: PLC0415

    _assert_publishable(INCOMPLETE_MESSAGE, "INCOMPLETE_MESSAGE")
    _assert_names_a_gesture(INCOMPLETE_MESSAGE, "INCOMPLETE_MESSAGE")


def test_every_sentence_the_reader_can_hand_back_holds_the_bounds():
    """Asked of the OUTPUT, over inputs no connector would write on purpose.

    Nine hundred characters, an accented line, a sentence broken over three
    lines, tabs, and a sentence that is not a string at all. Whatever comes out
    of `prevented_by` is what the console renders verbatim.
    """
    from core.pull_envelope import prevented_by  # noqa: PLC0415

    for label, message in (
        ("long", "Request the allowlist. " + ("x" * 900)),
        ("accented", "Request the allowlist for the Café Crème project, then re-ask."),
        ("multiline", "Request the grant,\nthen re-ask these dates.\r\nThank you."),
        ("tabbed", "Request\tthe grant,\t\tthen re-ask."),
        ("not a sentence", {"a": 1}),
        ("empty", "   "),
    ):
        read = prevented_by(
            {"row_count": 0, "prevented": True, "prevented_reason": "gate",
             "message": message},
            module_name="broken",
        )
        assert read is not None, label
        _assert_publishable(read.message, f"prevented_by/{label}")
        _assert_names_a_gesture(read.message, f"prevented_by/{label}")


def test_every_sentence_the_PUBLISHING_boundary_can_hand_back_holds_the_bounds():
    """The same properties on `prevented_pair`, which is the surface boundary.

    It reads rows written by any version of this module -- "including on rows
    written before those bounds existed", says its own docstring -- so the bounds
    are asked of it separately and not inherited from the writer.
    """
    import json as _json  # noqa: PLC0415

    from core.pull_envelope import prevented_pair  # noqa: PLC0415

    for label, stored in (
        ("long", "Request the allowlist for this project. " + ("y" * 900)),
        ("accented", "Request the allowlist for the Café Crème project, then re-ask."),
        ("multiline", "Request the grant,\nthen re-ask these dates."),
    ):
        _reason, sentence = prevented_pair(
            _json.dumps({"prevented_reason": "gate", "prevented_message": stored})
        )
        assert sentence is not None, label
        _assert_publishable(sentence, f"prevented_pair/{label}")


def test_the_publishing_boundary_refuses_what_is_not_a_sentence():
    """A `repr` is not a sentence, and `str()` before the type check made one.

    Measured 2026-08-21:
    `prevented_pair('{"prevented_reason": "gate", "prevented_message": {"a": 1}}')`
    answered `("gate", "{'a': 1}")` -- a Python dict rendered into the day grid by
    the one boundary whose docstring promises the bound holds at publication.
    """
    import json as _json  # noqa: PLC0415

    from core.pull_envelope import prevented_pair  # noqa: PLC0415

    for not_a_sentence in ({"a": 1}, ["a"], 42, True, None):
        reason, sentence = prevented_pair(
            _json.dumps(
                {"prevented_reason": "gate", "prevented_message": not_a_sentence}
            )
        )
        # The token is readable and kept; the sentence is an ABSENCE, which a
        # screen can say. A half-parsed one it cannot.
        assert reason == "gate", not_a_sentence
        assert sentence is None, not_a_sentence


def test_only_a_NUMBER_of_rows_takes_the_exception_that_still_answers_done():
    """The one exit toward `done`, and what may open it.

    `int(row_count or 0)` accepted `"5"` and `True`, so an envelope that claims
    rows in a value the module refuses to believe anywhere else took the
    exception -- and `queue._execute_job` then wrote `row_count="5"` into the
    column verbatim. Rows that EXIST are what the exception is for; a string is
    not proof that any landed.
    """
    from core.pull_envelope import prevented_by  # noqa: PLC0415

    # A real count keeps its rows and stays `done` -- the exception, untouched.
    assert prevented_by(
        {"row_count": 112, "prevented": True}, module_name="broken"
    ) is None

    for not_a_count in ("5", True, "many", [5], {"n": 5}):
        read = prevented_by(
            {"row_count": not_a_count, "prevented": True,
             "prevented_reason": "gate",
             "message": "Request the grant, then re-ask these dates."},
            module_name="broken",
        )
        assert read is not None, not_a_count
        assert read.reason == "gate", not_a_count

    # And `0` still falls toward the refusal, as it always did.
    assert prevented_by({"row_count": 0, "prevented": True}) is not None


def test_a_bad_reason_token_does_not_throw_away_a_usable_sentence():
    """Les deux moities se degradent SEPAREMENT, et c'est le lecteur qui l'exige.

    The reason is a machine token: compared, logged, stored, and read by nobody
    on a screen. The sentence is the only half a person acts on. Folding both
    into one "incomplete" outcome would discard « Request the reviews allowlist
    for this project » over a typo in a word no reader ever sees.
    """
    from core.pull_envelope import INCOMPLETE_MESSAGE, INCOMPLETE_REASON, prevented_by

    read = prevented_by(
        {
            "row_count": 0,
            "prevented": True,
            "prevented_reason": "Reviews Pending!",
            "message": "Request the reviews allowlist for this project, then re-ask.",
        },
        module_name="broken",
    )
    assert read is not None
    assert read.reason == INCOMPLETE_REASON
    assert read.message.startswith("Request the reviews allowlist")
    assert read.message != INCOMPLETE_MESSAGE


def test_the_only_malformed_envelope_that_still_becomes_done_is_the_one_with_rows():
    """The single exception, and the argument for it: those rows exist.

    Every other degradation goes toward the refusal. This one may not: dropping a
    landing loses data, which is worse than either state being wrong.
    """
    from core.pull_envelope import prevented_by  # noqa: PLC0415

    assert prevented_by(
        {"pull_id": "p", "row_count": 112, "prevented": True}, module_name="broken"
    ) is None


def test_the_builder_refuses_an_unusable_reason_or_an_empty_sentence():
    from core.pull_envelope import PullEnvelopeError, prevented_envelope  # noqa: PLC0415

    with pytest.raises(PullEnvelopeError):
        prevented_envelope(
            pull_id="p", date_from="a", date_to="b", reason="Not An Id", message="x"
        )
    with pytest.raises(PullEnvelopeError):
        prevented_envelope(
            pull_id="p", date_from="a", date_to="b", reason="ok", message=""
        )


# ---------------------------------------------------------------------------
# The connector that needed it first, read through the shared reader.
# ---------------------------------------------------------------------------


def _gbp_connector():
    """The real Business Profile connector, loaded by path (dashed directory)."""
    import importlib.util  # noqa: PLC0415
    import pathlib  # noqa: PLC0415

    path = (
        pathlib.Path(__file__).resolve().parents[2]
        / "modules"
        / "google-business-profile"
        / "connector.py"
    )
    spec = importlib.util.spec_from_file_location("_gbp_ai307", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_every_precondition_of_the_business_profile_connector_is_readable():
    """The class, checked at the one connector that has preconditions today.

    It goes through `prevented_by` -- the worker's own reader -- so a sentence
    this module cannot carry (blank, non-ASCII, or over the bound) fails here and
    not in production.
    """
    from core.pull_envelope import prevented_by  # noqa: PLC0415

    module = _gbp_connector()

    reasons = sorted(module._PRECONDITION_MESSAGES)
    assert reasons, "the connector declares no precondition at all"
    for reason in reasons:
        envelope = module._prevented_envelope("p", "2026-07-01", "2026-07-07", reason)
        read = prevented_by(envelope, module_name="google-business-profile")
        assert read is not None, reason
        assert read.reason == reason
        # A sentence that names a GESTURE, in the vocabulary of the person.
        assert "Request" in read.message or "request" in read.message, read.message


# ---------------------------------------------------------------------------
# What the person reads, on the surface that serves it.
# ---------------------------------------------------------------------------


def test_a_prevented_window_reads_as_a_refusal_and_never_as_an_error():
    """No `error_class`, no `reconnect`: nothing is broken and nothing to retry.

    Mapping it onto the error taxonomy would tell a person to reconnect a
    credential that works perfectly, and mark retryable a window that will be
    refused identically until a grant lands at the provider.
    """
    from core.datastream_diagnosis import _sanitize_pull_event  # noqa: PLC0415
    from core.pull_job_states import PREVENTED  # noqa: PLC0415

    event = _sanitize_pull_event(
        {
            "id": "job_1",
            "pull_id": "pull_1",
            "state": PREVENTED,
            "date_from": "2026-07-01",
            "date_to": "2026-07-07",
            "attempt_count": 1,
            "error_detail": json.dumps(
                {
                    "prevented_reason": "reviews_access_pending",
                    "prevented_message": (
                        "Request the reviews allowlist for this project, then "
                        "re-ask these dates."
                    ),
                }
            ),
        }
    )
    assert event["state"] == PREVENTED
    assert event["error_class"] is None
    assert event["recommended_action"] is None
    assert event["retryable"] is False
    assert event["prevented_reason"] == "reviews_access_pending"
    assert "Request the reviews allowlist" in event["prevented_message"]


def test_a_failed_window_still_maps_through_the_error_taxonomy():
    """The other branch is untouched: a real failure keeps its canonical class."""
    from core.datastream_diagnosis import _sanitize_pull_event  # noqa: PLC0415

    event = _sanitize_pull_event(
        {
            "id": "job_2",
            "pull_id": "pull_2",
            "state": "failed",
            "error_detail": json.dumps(
                {"error_class": "auth_expired", "user_action": "reconnect"}
            ),
        }
    )
    assert event["error_class"] == "auth_expired"
    assert event["recommended_action"] == "reconnect"
    assert event["prevented_reason"] is None


# ---------------------------------------------------------------------------
# The registry: what a prevented window may and may not set in motion.
# ---------------------------------------------------------------------------


def test_a_prevented_window_is_terminal_never_failed_and_never_a_success():
    from core import pull_job_states as states  # noqa: PLC0415

    assert states.is_terminal(states.PREVENTED)
    assert not states.has_failed(states.PREVENTED)
    assert states.PREVENTED not in states.FAILED_JOB_STATES
    assert states.PREVENTED not in states.ACTIVE_JOB_STATES
    assert states.BY_NAME[states.PREVENTED].succeeded is False


def test_a_prevented_window_is_an_attempt_so_the_catch_up_does_not_walk_past_it():
    """`_reschedule_failed_pulls` reads the LATEST attempted window per stream.

    Out of `ATTEMPTED_JOB_STATES`, a prevented window would be invisible to that
    `DISTINCT ON`, which would then find an OLDER `failed` one and re-arm the
    stream every hour against a grant only a human at the provider can give.
    """
    from core import pull_job_states as states  # noqa: PLC0415

    assert states.PREVENTED in states.ATTEMPTED_JOB_STATES
    assert states.PREVENTED not in states.FAILED_JOB_STATES


def test_a_prevented_day_reports_never_fetched_and_keeps_its_own_word():
    """Arbitrage 7 of story 58.1, unchanged: the screen needs both words."""
    from core import pull_job_states as states  # noqa: PLC0415

    assert states.ledger_status(states.PREVENTED) == states.LEDGER_NEVER_FETCHED
    # And the window's own name is what tells the two absences apart.
    assert states.ledger_status(states.CANCELLED) == states.LEDGER_NEVER_FETCHED
    assert states.PREVENTED != states.CANCELLED


# ---------------------------------------------------------------------------
# 7. THE OTHER DOOR -- the refusal that arrives as an EXCEPTION (2026-08-25).
# ---------------------------------------------------------------------------
#
# Everything above covers the pull that RETURNS a prevented envelope, which a
# connector may only do on a profile it is allowed to abandon. The profile that
# must land rows cannot: an empty day reported as a success would fabricate a
# day, so it RAISES. `google-business-profile` said so in its own comment from
# the first commit -- "the core profile does NOT skip ... it raises
# permission_denied" -- and marked the raised error with `precondition` /
# `precondition_message` for somebody to read.
#
# MEASURED 2026-08-24: nobody did. `grep -rn "precondition" server/ ui/ web/`
# outside that module returned other subjects only. So `pull_location_daily` --
# the DEFAULT pull, on a project still at 0 QPM, i.e. every Business Profile
# project on its first day -- was recorded `failed / permission_denied` with
# `user_action = "reconnect"`. Reconnecting releases nothing (the grant is a
# manual approval at Google), and `failed` is exactly what
# `scheduler._reschedule_failed_pulls` re-arms every hour: the crash loop against
# a human approval queue that `prevented` exists to stop, reached through the
# door `prevented` did not cover. The story's DoD line 218 -- "precondition 0-QPM
# surfaced in the UI" -- was ticked over that.
#
# Each test below reddens if `prevented_by_error` is removed from
# `queue._execute_job`.


def _zero_qpm_error():
    """The connector's REAL error for a 403 on the performance surface.

    Built by calling the module's own `_raise_for_status`, never by writing the
    sentence here: a test that spells the message itself would still pass with
    the connector's constant deleted.
    """
    import httpx  # noqa: PLC0415

    module = _gbp_connector()
    response = httpx.Response(
        403,
        request=httpx.Request(
            "GET", "https://businessprofileperformance.example.com/v1/locations/1"
        ),
        json={"error": {"code": 403, "message": "requests per minute exceeded"}},
    )
    try:
        module._raise_for_status(response)
    except Exception as exc:  # noqa: BLE001 -- that is what we are after
        return exc
    raise AssertionError("a 403 on the performance surface no longer raises")


def test_the_default_profile_refusal_is_recorded_prevented_and_never_failed():
    """THE DEFECT ITSELF. Remove the reader in `_execute_job` and this reads `failed`."""
    from core.pull_job_states import FAILED, PREVENTED  # noqa: PLC0415

    rec, _audit = _run_job(raises=_zero_qpm_error())
    assert rec.terminal_state == PREVENTED
    assert rec.terminal_state != FAILED


def test_the_default_profile_refusal_never_tells_a_person_to_reconnect():
    """`permission_denied` carries `user_action = "reconnect"` (pull_errors:158-163).

    On this window all three words are false: the credential works, nothing is
    denied to it, and reconnecting cannot obtain a quota Google grants by hand.
    A prevented row carries no error class at all -- the connector's sentence
    replaces it.
    """
    rec, _audit = _run_job(raises=_zero_qpm_error())
    _sql, params = rec.terminal_write
    detail = next(p for p in params if isinstance(p, str) and p.startswith("{"))
    parsed = json.loads(detail)
    assert "reconnect" not in detail
    assert "error_class" not in parsed
    assert parsed["prevented_reason"] == "google_access_pending"


def test_the_default_profile_refusal_stores_no_row_count_at_all():
    """Same rule as the returning door: `0` is a count, and none was taken.

    Asserted TOGETHER with the state on purpose. Measured while mutating the
    reader out: the `row_count` half alone stays green, because a `failed` row
    writes no count either -- an assertion that cannot fail is not a measurement.
    """
    from core.pull_job_states import PREVENTED  # noqa: PLC0415

    rec, _audit = _run_job(raises=_zero_qpm_error())
    assert rec.terminal_state == PREVENTED
    sql, params = rec.terminal_write
    assert "row_count" not in sql, sql
    assert 0 not in params, params


def test_the_default_profile_refusal_is_audited_as_refused_never_as_failed():
    from core.queue import ACTION_PULL_FAILED, ACTION_PULL_PREVENTED  # noqa: PLC0415

    _rec, audit = _run_job(raises=_zero_qpm_error())
    written = [str(call) for call in audit.call_args_list]
    assert any(ACTION_PULL_PREVENTED in row for row in written), written
    assert not any(ACTION_PULL_FAILED in row for row in written), written


def test_the_two_doors_write_the_same_row():
    """The whole point of the second reader: one product fact, one shape.

    A refused `reviews` day and a refused `location_daily` day are the same
    absence to the person reading the grid. Two transports would be one edit away
    from a screen where only one of them names its grant.
    """
    returned, _ = _run_job(_prevented_result())
    raised, _ = _run_job(raises=_zero_qpm_error())
    assert returned.terminal_state == raised.terminal_state

    def _detail(rec):
        _sql, params = rec.terminal_write
        return json.loads(
            next(p for p in params if isinstance(p, str) and p.startswith("{"))
        )

    assert sorted(_detail(returned)) == sorted(_detail(raised))


def test_an_ordinary_provider_refusal_still_fails_and_keeps_its_class():
    """The other half of the contract: an error that names NO precondition moves.

    A 403 from a surface that is not a provisioning gate is a real refusal, and
    it must keep failing with its canonical class -- otherwise this repair would
    silence every permission error in the product.
    """
    from core.pull_errors import PermissionDeniedError  # noqa: PLC0415
    from core.pull_job_states import PREVENTED  # noqa: PLC0415

    rec, audit = _run_job(raises=PermissionDeniedError(provider_status=403))
    assert rec.terminal_state != PREVENTED
    _sql, params = rec.terminal_write
    detail = next(p for p in params if isinstance(p, str) and p.startswith("{"))
    assert json.loads(detail)["error_class"] == "permission_denied"

    from core.queue import ACTION_PULL_FAILED  # noqa: PLC0415

    assert any(ACTION_PULL_FAILED in str(c) for c in audit.call_args_list)


def test_the_error_reader_answers_none_for_every_error_that_claims_nothing():
    """No attribute, no claim -- 38 connectors are not touched by this reader."""
    from core.pull_envelope import prevented_by_error  # noqa: PLC0415
    from core.pull_errors import (  # noqa: PLC0415
        AuthExpiredError,
        PermissionDeniedError,
        UnclassifiedError,
    )

    for exc in (
        PermissionDeniedError(provider_status=403),
        AuthExpiredError(provider_status=401),
        UnclassifiedError(provider_status=500),
        RuntimeError("boom"),
        ValueError("no location selected"),
    ):
        assert prevented_by_error(exc, module_name="any") is None, exc

    # An attribute that is present and EMPTY is not a claim either.
    blank = PermissionDeniedError(provider_status=403)
    blank.precondition = ""
    assert prevented_by_error(blank, module_name="any") is None


def test_a_malformed_raised_precondition_fails_toward_the_refusal_too():
    """Same degradation as the envelope, and for the same reason.

    What the connector ASSERTED by setting the attribute is that the source did
    not allow the window. A missing sentence or an unusable token is a bug in its
    metadata, not evidence that the window may be retried hourly.
    """
    from core.pull_envelope import (  # noqa: PLC0415
        INCOMPLETE_MESSAGE,
        INCOMPLETE_REASON,
        prevented_by_error,
    )
    from core.pull_errors import PermissionDeniedError  # noqa: PLC0415

    no_sentence = PermissionDeniedError(provider_status=403)
    no_sentence.precondition = "quota_pending"
    read = prevented_by_error(no_sentence, module_name="broken")
    assert read is not None
    assert read.reason == "quota_pending"
    assert read.message == INCOMPLETE_MESSAGE

    bad_token = PermissionDeniedError(provider_status=403)
    bad_token.precondition = "Quota Pending!"
    bad_token.precondition_message = "Request the quota, then re-ask these dates."
    read = prevented_by_error(bad_token, module_name="broken")
    assert read is not None
    assert read.reason == INCOMPLETE_REASON
    assert read.message.startswith("Request the quota")


def test_every_sentence_the_ERROR_reader_hands_back_holds_the_bounds():
    """The published properties, asked of the second reader as of the first."""
    from core.pull_envelope import prevented_by_error  # noqa: PLC0415
    from core.pull_errors import PermissionDeniedError  # noqa: PLC0415

    for label, message in (
        ("long", "Request the quota. " + ("x" * 900)),
        ("accented", "Request the quota for the Café Crème project, then re-ask."),
        ("multiline", "Request the quota,\nthen re-ask these dates.\r\nThanks."),
        ("not a sentence", {"a": 1}),
        ("empty", "   "),
    ):
        exc = PermissionDeniedError(provider_status=403)
        exc.precondition = "gate"
        exc.precondition_message = message
        read = prevented_by_error(exc, module_name="broken")
        assert read is not None, label
        _assert_publishable(read.message, f"prevented_by_error/{label}")
        _assert_names_a_gesture(read.message, f"prevented_by_error/{label}")


# ---------------------------------------------------------------------------
# 8. And the 0-QPM sentence REACHES the screen by the location_daily path.
# ---------------------------------------------------------------------------


def test_the_zero_qpm_sentence_travels_from_the_default_pull_to_the_day_grid():
    """DoD :218, end to end on the server, without one sentence written here.

    The words come from the connector's constant, through the raise, through the
    worker, into `error_detail`, back out through the one publishing boundary and
    onto the day payload the grid renders. `CoverageBars.extractGapSentence`
    renders `prevented_message` verbatim (its own vitest pins that), so the last
    link is the KEY being present on the day with the connector's words in it.
    """
    from datetime import date  # noqa: PLC0415

    from core.extract_ledger import _day_to_ledger_entry  # noqa: PLC0415
    from core.pull_envelope import prevented_pair  # noqa: PLC0415
    from core.pull_job_states import PREVENTED  # noqa: PLC0415

    rec, _audit = _run_job(raises=_zero_qpm_error())
    _sql, params = rec.terminal_write
    stored_detail = next(p for p in params if isinstance(p, str) and p.startswith("{"))

    reason, sentence = prevented_pair(stored_detail)
    assert reason == "google_access_pending"

    entry = _day_to_ledger_entry(
        date(2026, 7, 1),
        {
            "state": PREVENTED,
            "pull_id": "pull_ai307",
            "error_detail": stored_detail,
            "completed_at": "2026-07-02T00:00:00Z",
        },
    )
    assert entry["job_state"] == PREVENTED
    assert entry["prevented_reason"] == "google_access_pending"
    assert entry["prevented_message"] == sentence

    # And it is the connector's OWN sentence -- named at its source, not retyped.
    expected = _gbp_connector()._PRECONDITION_MESSAGES["google_access_pending"]
    assert entry["prevented_message"] == expected
    # It names the gesture that releases the gate, never the status code.
    _assert_names_a_gesture(entry["prevented_message"], "0-QPM sentence")
    assert "403" not in entry["prevented_message"]
