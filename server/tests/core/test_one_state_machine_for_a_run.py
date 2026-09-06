"""Every writer of a run's state crosses `advance_state` -- AI-223.

`tests/conformance/test_one_state_machine_for_a_run.py` refuses a private
`UPDATE ... SET state`; it reads text and cannot say what the code does instead.
These tests say it: each converted writer is run against a connection double
with the machine SPIED, and what is asserted is the transition it asked for --
`(expected_current_state, new_state)` -- in order.

Spying the machine rather than letting it run is deliberate. The machine's own
behaviour (the lock, the refusal, `state_changed_at`, the closed spans, the
audit row) is proven once, where it lives, in `test_datastream_publication.py`.
Proving it again per caller would measure the machine five times and the wiring
never. `publish_activate_mutation` is proven UNSPIED in
`test_datastream_activation.py::test_publish_activate_moves_the_run_through_the_
one_state_machine`, because that writer is the one whose private UPDATEs lost
`state_changed_at` and its repair had to be visible end to end.
"""

from __future__ import annotations

import inspect

import pytest
from core import datastream_publication as pub

from tests.support.statement_router import (
    StatementInventory,
    UnknownStatement,
    describe,
)

_HASH = "a" * 64
_EVIDENCE = {
    "candidate_content_fingerprint": "c" * 64,
    "candidate_schema_fingerprint": "s" * 64,
    "dispatch_bundle_fingerprint": "b" * 64,
    "landing_relation": "raw.ds_1__cand_dse_1",
}


# EVERY statement the four converted writers issue on a path these tests walk,
# NAMED -- AI-317. Five of them (the whole atomic group of `commit_publication`
# and the two dispatch UPDATEs) used to land in an `else` that answered no rows
# and left the PREVIOUS statement's `description` in place. Nothing read those
# answers, so nothing was wrong today; what was wrong is that a rewritten
# `INSERT ... RETURNING`, or a pointer swap that starts reading its own row,
# would have gone on being answered `None` by a fake that had never been told
# about it.
#
# Declaration order is first match wins: `commit_pointer_lock` is declared
# before `reconcile_pointer` because the reconciliation read's fragment is a
# prefix of the locking one. The `FROM app.datastreams ... FOR UPDATE` branch
# the old chain ended on was dead for exactly that reason -- the lock never
# reached it -- and it is gone rather than renamed.
_RUN = StatementInventory(
    "_Cursor (datastream_publication run writers)",
    execution_row="select id, datastream_id, project_id, plan_version_id",
    commit_pointer_lock=("select current_published_execution_id", "for update"),
    reconcile_pointer="select current_published_execution_id",
    publication_log_probe="select 1 from app.datastream_publication_log",
    promotion_execution_lock="select datastream_id, state, content_hash, row_count",
    commit_execution_lock="select datastream_id, plan_version_id, mapping_version_id",
    dispatch_evidence_lock="select state, candidate_content_fingerprint",
    publication_log_insert="insert into app.datastream_publication_log",
    pointer_swap="update app.datastreams set current_published_execution_id",
    outbox_insert="insert into app.datastream_outbox",
    dispatch_promoting=("update app.managed_file_dispatches", "set state = 'promoting'"),
    dispatch_published=("update app.managed_file_dispatches", "set state = 'published'"),
)

# The statements that genuinely return NO RESULT SET. `description = None` is
# what psycopg reserves for exactly those; everywhere else it is DERIVED from
# the statement, so a moved projection cannot keep agreeing with a hand-written
# column list nothing reads. `_row_to_execution` reads `cur.description`, which
# is why that list mattered here at all.
_NO_RESULT_SET = frozenset(
    {
        "publication_log_insert",
        "pointer_swap",
        "outbox_insert",
        "dispatch_promoting",
        "dispatch_published",
    }
)


class _Cursor:
    def __init__(self, conn) -> None:
        self._conn = conn
        self.description = None
        self._result = None
        self.rowcount = 1

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        s = " ".join(sql.split())
        self._conn.executed.append(s)
        statement = _RUN.match(s)
        self.description = None if statement in _NO_RESULT_SET else describe(s)
        match statement:
            case "execution_row":
                self._result = (
                    "dse_1", "ds_1", "proj_1", "dsp_1", "dmap_1", {}, self._conn.state,
                    None, _HASH, 100, None, None, None, "operator", None, None,
                )
            case "commit_pointer_lock" | "reconcile_pointer":
                self._result = (self._conn.pointer,)
            case "publication_log_probe":
                self._result = (1,) if self._conn.has_log else None
            case "promotion_execution_lock":
                self._result = ("ds_1", self._conn.state, _HASH, 100)
            case "dispatch_evidence_lock":
                self._result = (
                    "ready",
                    _EVIDENCE["candidate_content_fingerprint"],
                    _EVIDENCE["candidate_schema_fingerprint"],
                    _EVIDENCE["landing_relation"],
                    100,
                    _EVIDENCE["dispatch_bundle_fingerprint"],
                    {"status": "passed"},
                )
            case "commit_execution_lock":
                self._result = ("ds_1", "dsp_1", "dmap_1", self._conn.state, _HASH, 100)
            case (
                "publication_log_insert"
                | "pointer_swap"
                | "outbox_insert"
                | "dispatch_promoting"
                | "dispatch_published"
            ):
                # Writes the product reads only through `rowcount`.
                self.rowcount = 1
                self._result = None
            case _:  # pragma: no cover - a name added to the inventory, unanswered
                raise _RUN.unknown(s)

    def fetchone(self):
        return self._result


class _Conn:
    def __init__(self, *, state: str, pointer: str | None = None, has_log: bool = False):
        self.state = state
        self.pointer = pointer
        self.has_log = has_log
        self.executed: list[str] = []
        self.committed = False

    def cursor(self):
        return _Cursor(self)

    def commit(self):
        self.committed = True

    def rollback(self):
        pass


@pytest.fixture()
def machine(monkeypatch):
    """Spy on the ONE state machine and report the transitions asked of it."""
    asked: list[tuple[str | None, str]] = []
    metadata: list[dict | None] = []

    def _spy(execution_id, expected_current_state, new_state, actor, conn, **kwargs):
        asked.append((expected_current_state, new_state))
        metadata.append(kwargs.get("audit_metadata"))
        conn.state = new_state
        return {"id": execution_id, "state": new_state}

    monkeypatch.setattr(pub, "advance_state", _spy)
    _spy.asked = asked  # type: ignore[attr-defined]
    _spy.metadata = metadata  # type: ignore[attr-defined]
    return _spy


def test_the_fake_refuses_a_statement_it_was_never_taught() -> None:
    """AI-317: an unrecognised statement must NAME itself, not answer no rows.

    The statement below is the replay probe `commit_publication` issues when the
    execution it is asked to publish is ALREADY `published` -- a real query of
    this same module, on a branch these tests never walk. The old `else`
    answered it `None` with the previous statement's `description`, which is
    indistinguishable from "no such dispatch": the fake would have decided a
    branch instead of admitting it had never been taught it.
    """
    cursor = _Cursor(_Conn(state=pub.STATE_PUBLISHED))

    with pytest.raises(UnknownStatement) as raised:
        cursor.execute(
            "SELECT state FROM app.managed_file_dispatches "
            "WHERE id = %s AND execution_id = %s "
            "AND datastream_id = %s AND project_id = %s"
        )

    message = str(raised.value)
    assert "from app.managed_file_dispatches" in message
    assert "dispatch_evidence_lock" in message


def _no_private_state_write(conn: _Conn) -> None:
    """Nothing on this connection set `state` behind the machine's back."""
    for sql in conn.executed:
        if sql.upper().startswith("UPDATE APP.DATASTREAM_EXECUTIONS"):
            head = sql.upper().split(" WHERE ")[0]
            assert "STATE =" not in head and "STATE=" not in head, sql


# ---------------------------------------------------------------------------
# commit_publication -- the DOMINANT path that ends a successful run.
# ---------------------------------------------------------------------------


def test_commit_publication_asks_the_machine_for_both_steps(machine) -> None:
    conn = _Conn(state=pub.STATE_READY, pointer="dse_prior")

    pub.commit_publication("dse_1", "proj_1", "operator", conn, connection_factory=object)

    assert machine.asked == [
        (pub.STATE_READY, pub.STATE_PUBLISHING),
        (pub.STATE_PUBLISHING, pub.STATE_PUBLISHED),
    ]
    _no_private_state_write(conn)
    assert conn.committed is True


def test_the_publication_audit_evidence_rides_on_the_terminal_transition(machine) -> None:
    """One state change, one audit row -- and it still carries every field.

    This function used to write a second, richer audit row of its own next to its
    private UPDATE. Two rows for one publication would have been the cost of the
    conversion; `audit_metadata` is what avoids paying it.
    """
    conn = _Conn(state=pub.STATE_READY, pointer="dse_prior")

    result = pub.commit_publication(
        "dse_1", "proj_1", "operator", conn, connection_factory=object
    )

    evidence = machine.metadata[-1]
    assert evidence["prior_execution_id"] == "dse_prior"
    assert evidence["content_hash"] == _HASH
    assert evidence["row_count"] == 100
    # The id it names is the log row this same transaction wrote.
    assert evidence["publication_log_id"] == result["publication_log_id"]
    assert evidence["publication_log_id"].startswith("dplog_")


def test_a_managed_file_publication_does_not_re_enter_publishing(machine) -> None:
    """It committed that phase before the warehouse promotion; asking twice would
    be the machine refusing a transition out of a state the run already left."""
    conn = _Conn(state=pub.STATE_PUBLISHING, pointer="dse_prior")

    pub.commit_publication(
        "dse_1",
        "proj_1",
        "operator",
        conn,
        connection_factory=object,
        dispatch_id="mfd_1",
        candidate_evidence=_EVIDENCE,
    )

    assert machine.asked == [(pub.STATE_PUBLISHING, pub.STATE_PUBLISHED)]
    _no_private_state_write(conn)


# ---------------------------------------------------------------------------
# begin_managed_file_promotion -- the durable claim on cross-store promotion.
# ---------------------------------------------------------------------------


def test_begin_managed_file_promotion_asks_the_machine(machine) -> None:
    conn = _Conn(state=pub.STATE_READY)

    pub.begin_managed_file_promotion(
        "dse_1", "proj_1", "operator", conn,
        dispatch_id="mfd_1", candidate_evidence=_EVIDENCE,
    )

    assert machine.asked == [(pub.STATE_READY, pub.STATE_PUBLISHING)]
    _no_private_state_write(conn)


# ---------------------------------------------------------------------------
# reconcile_execution / _reconcile_fail_closed -- the cross-store repair.
# ---------------------------------------------------------------------------


def test_reconciling_to_published_replays_the_lost_transition(machine) -> None:
    """The atomic writes landed; only the state advance was lost. Replaying it
    through the machine is what closes the spans the interruption left open."""
    conn = _Conn(state=pub.STATE_PUBLISHING, pointer="dse_1", has_log=True)

    out = pub.reconcile_execution("dse_1", "proj_1", conn)

    assert out == {
        "resolved": True,
        "final_state": pub.STATE_PUBLISHED,
        "action_taken": "resolved_to_published",
    }
    assert machine.asked == [(pub.STATE_PUBLISHING, pub.STATE_PUBLISHED)]
    _no_private_state_write(conn)


def test_reconciling_fail_closed_asks_the_machine_from_the_state_it_read(machine) -> None:
    conn = _Conn(state=pub.STATE_LOADING)

    out = pub.reconcile_execution("dse_1", "proj_1", conn)

    assert out["action_taken"] == "reconciliation_inconclusive"
    assert machine.asked == [(pub.STATE_LOADING, pub.STATE_FAILED)]
    assert machine.metadata[-1] == {"reason": "reconciliation_inconclusive"}
    _no_private_state_write(conn)


def test_no_commit_evidence_fails_the_run_and_names_why(machine) -> None:
    conn = _Conn(state=pub.STATE_PUBLISHING, pointer="dse_other", has_log=False)

    out = pub.reconcile_execution("dse_1", "proj_1", conn)

    assert out["action_taken"] == "resolved_to_failed"
    assert machine.asked == [(pub.STATE_PUBLISHING, pub.STATE_FAILED)]
    assert machine.metadata[-1] == {"reason": "no_commit_evidence"}


def test_a_terminal_run_is_reported_resolved_and_the_machine_is_not_asked(machine) -> None:
    """Idempotence, and the reason the fail-closed branch reads the state first:
    the machine refuses a transition out of a terminal state, correctly."""
    conn = _Conn(state=pub.STATE_PUBLISHED)

    out = pub.reconcile_execution("dse_1", "proj_1", conn)

    assert out == {
        "resolved": True,
        "final_state": pub.STATE_PUBLISHED,
        "action_taken": "already_terminal",
    }
    assert machine.asked == []


def test_the_fail_closed_branch_writes_nothing_for_a_run_that_already_ended(machine) -> None:
    """Called directly, past `reconcile_execution`'s own early return.

    The private UPDATE this replaced matched no row in this case -- and STILL
    wrote an audit row saying the run had been failed. An audit row for a state
    change that did not happen is worse than none.
    """
    conn = _Conn(state=pub.STATE_CANCELLED)

    pub._reconcile_fail_closed("dse_1", "proj_1", conn)

    assert machine.asked == []
    _no_private_state_write(conn)
    assert conn.committed is True


# ---------------------------------------------------------------------------
# The machine's vocabulary is closed, and the converted writers stay inside it.
# ---------------------------------------------------------------------------


def test_every_transition_the_converted_writers_ask_for_is_a_declared_edge() -> None:
    """AI-223 added NO transition: all six private UPDATEs asked for edges the
    machine already declared. Written down so the next conversion that DOES need
    a new edge has to add it here, next to its rule, instead of writing around it.
    """
    asked = [
        (pub.STATE_READY, pub.STATE_PUBLISHING),      # activation, promotion, commit
        (pub.STATE_PUBLISHING, pub.STATE_PUBLISHED),  # activation, commit, reconcile
        (pub.STATE_PUBLISHING, pub.STATE_FAILED),     # reconcile, fail closed
        (pub.STATE_LOADING, pub.STATE_FAILED),        # fail closed, inconclusive
    ]
    for current, new_state in asked:
        assert pub.is_valid_transition(current, new_state), f"{current} -> {new_state}"
    # And the machine still refuses the shortcut none of them may take.
    assert pub.is_valid_transition(pub.STATE_READY, pub.STATE_PUBLISHED) is False


def test_the_audit_metadata_may_not_restate_what_the_row_did() -> None:
    """A caller enriches the audit row; it cannot rewrite the transition in it."""
    body = inspect.getsource(pub.advance_state)
    merged = body.index("**(audit_metadata or {})")
    for owned in ('"from_state"', '"to_state"', '"execution_id"'):
        assert body.index(owned) > merged, owned
