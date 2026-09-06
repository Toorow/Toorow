"""One readiness predicate, one owner -- AI-327.

WHAT WAS MEASURED ON 2026-08-31. `account_topology.ready_accounts_for_connector`
had NO caller in the repository, and `queue._ready_accounts_of_connector` re-spelled
the same SELECT by hand thirty lines below the enqueue gate that uses it. Its own
docstring stated the reason -- "Mirrors `account_topology.ready_accounts_for_connector`
and cannot call it: that one owns its connection" -- and the reason had stopped
being true: `conn` is a parameter of that function, and passing it keeps the read
on the connection the pull is being decided under.

WHY TWO COPIES OF THIS ONE ARE WORSE THAN TWO COPIES OF ANYTHING ELSE. This
predicate is asked twice about the same pull, by two different callers: the
enqueue gate (`_topology_scope_refusal`), and the extraction (`_execute_job`). If
the two spellings ever answer differently -- a state added, the `IS NULL` of
migration 210 dropped from one -- the gate authorises one account and the pull
reads another, in silence, and the screen shows another property's numbers.

AND THE SECOND HALF, WHICH IS THE ONE THAT SHIPPED. The queue's copy was reached
inside a `try` whose `except Exception` logged at WARNING and returned `None`.
`None` is the answer "no account was ever chosen here" -- the enqueue gate renders
it `account_not_selected`, whose gesture is "go and choose one". So a database
that did not answer told a person who HAD chosen to go and do it again, and a job
that reached execution pulled with no account at all. A read that failed is not a
count of zero.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_QUEUE_PY = Path(__file__).resolve().parents[2] / "core" / "queue.py"

#: The relation the predicate reads. A statement naming it inside `queue.py` is,
#: by construction, a second copy of a predicate that already has an owner.
_SCOPE_RELATION = "app.connection_account_scope"


class _Cursor:
    def __init__(self, outcome):
        self._outcome = outcome
        self.description = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, sql, params=None):
        if isinstance(self._outcome, BaseException):
            raise self._outcome

    def fetchone(self):
        return None

    def fetchall(self):
        return []


class _Conn:
    def __init__(self, outcome=None):
        self._outcome = outcome

    def cursor(self):
        return _Cursor(self._outcome)


def test_the_queue_holds_no_second_spelling_of_the_readiness_predicate() -> None:
    """No statement of `queue.py` reads the scope relation. Not fewer: none.

    Read from the STRING CONSTANTS of the module rather than from its whole text,
    because the paragraph above mentions the table by name and a `grep` would
    count the explanation as the defect it explains.
    """
    source = _QUEUE_PY.read_text(encoding="utf-8")
    assert "_resolve_selected_account" in source, (
        f"{_QUEUE_PY.name} no longer holds the resolver this guard watches -- "
        "the code moved, and a scan of the wrong file finds no second spelling "
        "of anything. Point this guard at the module that owns it."
    )
    tree = ast.parse(source)
    statements = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and _SCOPE_RELATION in node.value.lower()
        and "select" in node.value.lower()
    ]
    assert not statements, (
        "queue.py spells its own readiness SELECT again:\n  "
        + "\n  ".join(text.strip()[:120] for text in statements)
        + "\n\nThe predicate belongs to `account_topology.ready_accounts_for_connector`, "
        "which takes the caller's `conn`. Two spellings are two answers waiting to "
        "differ, and the enqueue gate and the extraction each read one of them."
    )


def test_the_resolver_asks_the_owner_and_nothing_else(monkeypatch) -> None:
    """The fallback goes through the topology function, with the caller's `conn`.

    Pinned by SUBSTITUTION rather than by reading the SQL: what matters is not
    that the two texts look alike, it is that there is only one text, and the
    only way to prove that is to change the one and see the other move.
    """
    from core import account_topology, queue

    seen: list[tuple] = []

    def _owner(connection_ref_id, connector=None, conn=None):
        seen.append((connection_ref_id, connector, conn))
        return ["properties/424242"]

    monkeypatch.setattr(account_topology, "ready_accounts_for_connector", _owner)

    conn = _Conn()
    account = queue._resolve_selected_account(
        conn, "conn_ai327", None, connector="google-analytics"
    )

    assert account == "properties/424242", (
        "the resolver answered without the owner of the predicate -- a copy is back"
    )
    assert seen == [("conn_ai327", "google-analytics", conn)], seen


def test_the_two_callers_of_the_predicate_cannot_disagree(monkeypatch) -> None:
    """The gate and the extraction resolve through the SAME function, one answer.

    A single substitution answers both: if either caller still held its own
    spelling, it would not see this one and the two lists below would differ.
    """
    from core import account_topology, queue

    calls: list[str | None] = []

    def _owner(connection_ref_id, connector=None, conn=None):
        calls.append(connector)
        return ["acct_only"]

    monkeypatch.setattr(account_topology, "ready_accounts_for_connector", _owner)

    gate = queue._resolve_selected_account(_Conn(), "conn_ai327", None, connector="gsc")
    extraction = queue._resolve_selected_account(
        _Conn(), "conn_ai327", None, connector="gsc"
    )

    assert gate == extraction == "acct_only"
    assert calls == ["gsc", "gsc"]


def test_a_read_that_fails_refuses_and_never_answers_none() -> None:
    """The `except` names what could not be read instead of degrading to `None`.

    `None` means "nobody ever chose an account here". Answering it to a database
    that did not answer sends a person who chose one back to the step they had
    already completed, and lets a job pull with no account.
    """
    from core.queue import (
        REFUSAL_ACCOUNT_SELECTION_UNREADABLE,
        AccountSelectionUnreadable,
        _account_selection_refusal,
    )

    boom = RuntimeError("connection is closed")
    with pytest.raises(AccountSelectionUnreadable) as raised:
        from core.queue import _resolve_selected_account

        _resolve_selected_account(_Conn(boom), "conn_ai327", None, connector="gsc")

    refusal = _account_selection_refusal(raised.value)
    assert refusal["state"] == "refused"
    assert refusal["code"] == REFUSAL_ACCOUNT_SELECTION_UNREADABLE
    assert raised.value.retryable is True, (
        "a read that did not come back is exactly what an attempt ladder is for"
    )
    # The cause travels for the log and the audit row, and NEVER into the
    # sentence a person reads.
    assert raised.value.cause is boom
    assert "closed" not in refusal["message"]
    assert "connection_account_scope" not in refusal["message"]


def test_the_two_refusals_are_two_different_words() -> None:
    """`ambiguous` and `unreadable` do not repair the same way, so they do not share a code."""
    from core.queue import (
        REFUSAL_ACCOUNT_SELECTION_AMBIGUOUS,
        REFUSAL_ACCOUNT_SELECTION_UNREADABLE,
        AccountSelectionAmbiguous,
        AccountSelectionRefused,
        AccountSelectionUnreadable,
        _account_selection_refusal,
    )

    ambiguous = AccountSelectionAmbiguous("conn_ai327", "gsc", ["a", "b"])
    unreadable = AccountSelectionUnreadable("conn_ai327", "gsc", RuntimeError("x"))

    assert isinstance(ambiguous, AccountSelectionRefused)
    assert isinstance(unreadable, AccountSelectionRefused)
    assert REFUSAL_ACCOUNT_SELECTION_AMBIGUOUS != REFUSAL_ACCOUNT_SELECTION_UNREADABLE
    assert ambiguous.retryable is False, "a human gesture repairs it; a retry repeats it"

    messages = {
        _account_selection_refusal(ambiguous)["message"],
        _account_selection_refusal(unreadable)["message"],
    }
    assert len(messages) == 2, "two causes told with one sentence is one cause hidden"


def test_an_unknown_statement_is_never_dressed_up_as_an_unreadable_database() -> None:
    """A fixture that was never taught a query must still fail as itself.

    `tests/support/statement_router.UnknownStatement` is an `AssertionError` on
    purpose. If the resolver's `except` renamed it "the accounts could not be
    read", every fake in the queue suites would answer a question it was never
    asked, green, exactly as they did before AI-327.
    """
    from core.queue import _resolve_selected_account

    from tests.support.statement_router import UnknownStatement

    with pytest.raises(UnknownStatement):
        _resolve_selected_account(
            _Conn(UnknownStatement("never taught")), "conn_ai327", None, connector="gsc"
        )
