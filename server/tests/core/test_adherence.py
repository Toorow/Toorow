"""Unit tests for the measured pre-query gate (Story 11.6, AD-18).

Covers core.adherence directly (session-key definition, both call orders, the
one-line pointer + <=30-line cap, and the non-blocking / never-raises contract).
The MCP-layer seam is exercised separately in
server/tests/integration/test_pre_query_gate_seams.py (AI-56).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from core import adherence


@pytest.fixture(autouse=True)
def _reset_adherence():
    adherence.reset_for_tests()
    yield
    adherence.reset_for_tests()


# ---------------------------------------------------------------------------
# The "same session" key: the trace when the host sends one, otherwise the
# CONSULT that opened the exchange -- never a wall-clock bucket (AI-316).
# ---------------------------------------------------------------------------


def test_session_key_prefers_trace_id_when_present():
    key, kind = adherence.session_key(trace_id="a" * 32)
    assert kind == "trace"
    assert key == "trace:" + "a" * 32


def test_session_key_falls_back_to_time_window_without_trace():
    key, kind = adherence.session_key(trace_id=None)
    assert kind == "time_window"
    assert key.startswith("tw:")


def test_the_pairing_slot_carries_no_clock_at_all():
    """AI-316. The slot is WHERE two calls meet, not WHEN.

    While a wall-clock bucket lived in this key, the verdict depended on where
    the boundary fell rather than on how long the caller waited: the same corpus
    against the same database scored PASS=21 FAIL=1 and then PASS=22 FAIL=0
    (measured 2026-08-24). A slot that cannot change with the clock cannot do
    that.
    """
    key, _ = adherence.session_key(trace_id=None, project_id="projA", identity="person_a")

    assert key == "tw:person_a:projA"


def test_a_consult_and_its_query_pair_across_a_wall_clock_boundary(
    _no_sinks, monkeypatch
):
    """THE AI-316 RED. 200 milliseconds apart, astride a 300s boundary.

    Under the bucket this was two sessions -- ``1199.9 // 300`` is 3 and
    ``1200.1 // 300`` is 4 -- so the query read as not adherent although the
    consult that preceded it was a fifth of a second old. Restore the bucket and
    this test goes red, which is the only thing that makes it a guard.
    """
    monkeypatch.setenv("ADHERENCE_SESSION_WINDOW_SECONDS", "300")

    adherence.mark_context_call(
        "search_context", trace_id=None, project_id="p", identity="a", now=1199.9
    )
    verdict = adherence.record_data_query(
        "get_report", project_id="p", trace_id=None, identity="a", now=1200.1
    )

    assert verdict["adherent"] is True
    assert verdict["context_tool"] == "search_context"


def test_the_recorded_key_names_the_consult_that_opened_the_exchange(
    _no_sinks, monkeypatch
):
    """Two questions replayed back to back are two exchanges, not one bucket.

    The recorded `session_key` is what a reader of `app.query_adherence` has to
    tell one exchange from the next. A bucket gave every question of a five
    minute replay the SAME key; the consult that opened the pair gives each its
    own, which is the closest thing to a question identifier the data permits.
    """
    monkeypatch.setenv("ADHERENCE_SESSION_WINDOW_SECONDS", "300")

    adherence.mark_context_call(
        "search_context", trace_id=None, project_id="p", identity="a", now=1000.0
    )
    first = adherence.record_data_query(
        "get_report", project_id="p", trace_id=None, identity="a", now=1000.5
    )
    adherence.mark_context_call(
        "search_context", trace_id=None, project_id="p", identity="a", now=1010.0
    )
    second = adherence.record_data_query(
        "get_report", project_id="p", trace_id=None, identity="a", now=1010.5
    )

    assert first["adherent"] is True and second["adherent"] is True
    assert first["session_key"] != second["session_key"]
    # Both still carry the caller and the project, which is what keeps two
    # operators and two tenants out of each other's exchanges.
    for key in (first["session_key"], second["session_key"]):
        assert key.startswith("tw:a:p@")


def test_a_query_with_no_consult_never_borrows_the_key_of_one(_no_sinks, monkeypatch):
    monkeypatch.setenv("ADHERENCE_SESSION_WINDOW_SECONDS", "300")

    adherence.mark_context_call(
        "search_context", trace_id=None, project_id="p", identity="a", now=1000.0
    )
    paired = adherence.record_data_query(
        "get_report", project_id="p", trace_id=None, identity="a", now=1000.5
    )
    # Well past the freshness of that consult: its own exchange, not that one.
    alone = adherence.record_data_query(
        "get_report", project_id="p", trace_id=None, identity="a", now=2000.0
    )

    assert alone["adherent"] is False
    assert alone["session_key"] != paired["session_key"]


def test_a_consult_older_than_the_window_no_longer_pairs(_no_sinks, monkeypatch):
    """Elapsed, and bounded. Freshness is the whole claim the basis makes."""
    monkeypatch.setenv("ADHERENCE_SESSION_WINDOW_SECONDS", "300")

    adherence.mark_context_call(
        "search_context", trace_id=None, project_id="p", identity="a", now=1000.0
    )
    just_inside = adherence.record_data_query(
        "get_report", project_id="p", trace_id=None, identity="a", now=1299.0
    )
    just_outside = adherence.record_data_query(
        "get_report", project_id="p", trace_id=None, identity="a", now=1301.0
    )

    assert just_inside["adherent"] is True
    assert just_outside["adherent"] is False


# ---------------------------------------------------------------------------
# Adherence recorded in BOTH call orders (verdict by value). We patch the two
# sinks so no Langfuse / Postgres is required (degrade-clean, AI-13 N/A).
# ---------------------------------------------------------------------------


@pytest.fixture
def _no_sinks(monkeypatch):
    """Silence the trace + Postgres sinks so we assert the pure verdict."""
    monkeypatch.setattr(adherence, "_record_trace_attributes", lambda *a, **k: None)
    monkeypatch.setattr(adherence, "_record_postgres_fallback", lambda *a, **k: None)


def test_context_then_data_is_adherent(_no_sinks):
    trace = "b" * 32
    adherence.mark_context_call("search_context", trace_id=trace)
    verdict = adherence.record_data_query(
        "get_report", project_id="projA", trace_id=trace
    )
    assert verdict["adherent"] is True
    assert verdict["context_tool"] == "search_context"
    assert verdict["data_tool"] == "get_report"
    assert verdict["session_kind"] == "trace"


def test_data_only_is_not_adherent(_no_sinks):
    trace = "c" * 32
    verdict = adherence.record_data_query(
        "get_card", project_id="projA", trace_id=trace
    )
    assert verdict["adherent"] is False
    assert verdict["context_tool"] is None


def test_data_then_context_is_not_adherent_for_that_query(_no_sinks):
    """A context call AFTER the data query does not retroactively make it adherent."""
    trace = "d" * 32
    v1 = adherence.record_data_query("get_daily_report", project_id="p", trace_id=trace)
    adherence.mark_context_call("get_procedure", trace_id=trace)
    assert v1["adherent"] is False  # the recorded verdict for the earlier query


def test_different_sessions_do_not_leak_adherence(_no_sinks):
    adherence.mark_context_call("search_context", trace_id="e" * 32)
    verdict = adherence.record_data_query(
        "get_report", project_id="p", trace_id="f" * 32  # different trace/session
    )
    assert verdict["adherent"] is False


def test_time_window_session_adherence(_no_sinks, monkeypatch):
    monkeypatch.setenv("ADHERENCE_SESSION_WINDOW_SECONDS", "300")
    # No trace id -> time-window session. Same window + same project -> adherent.
    adherence.mark_context_call(
        "search_context", trace_id=None, project_id="p", now=1000.0
    )
    verdict = adherence.record_data_query(
        "get_card", project_id="p", trace_id=None, now=1100.0
    )
    assert verdict["adherent"] is True


def test_time_window_key_is_scoped_by_project():
    """The time-window key folds in project_id so two projects don't collide.

    Asserted as the property rather than the literal prefix: Story 49.6 added the
    identity dimension in front of the project, and a test pinned to the exact
    string would have to be rewritten for every dimension the key gains while the
    invariant it protects never changes.
    """
    ka, _ = adherence.session_key(trace_id=None, project_id="projA")
    kb, _ = adherence.session_key(trace_id=None, project_id="projB")
    assert ka != kb
    assert ka.startswith("tw:") and kb.startswith("tw:")
    assert "projA" in ka and "projB" in kb


def test_time_window_key_is_scoped_by_caller_identity():
    """`context-hub.md` [6]: adherence must not be recorded for a caller that did
    not itself consult context.

    Before Story 49.6 the key folded in the project and stopped there, so two
    operators of ONE project in the same wall-clock window shared a bucket: one
    running `search_context` made the other's data query count as adherent.
    """
    ka, _ = adherence.session_key(
        trace_id=None, project_id="projA", identity="person_a"
    )
    kb, _ = adherence.session_key(
        trace_id=None, project_id="projA", identity="person_b"
    )

    assert ka != kb


def test_an_absent_identity_degrades_instead_of_keying_on_the_word_none():
    """A caller with no identity must not collapse into a bucket named "None"."""
    key, _ = adherence.session_key(trace_id=None, project_id="projA")

    assert "None" not in key


def test_one_operators_context_call_does_not_make_anothers_query_adherent(
    _no_sinks, monkeypatch
):
    """The end-to-end shape of criterion [6], not just the key derivation."""
    monkeypatch.setenv("ADHERENCE_SESSION_WINDOW_SECONDS", "300")
    adherence.reset_for_tests()

    adherence.mark_context_call(
        "search_context", trace_id=None, project_id="projA",
        identity="person_a", now=1000.0,
    )
    verdict = adherence.record_data_query(
        "get_report", project_id="projA", trace_id=None,
        identity="person_b", now=1010.0,
    )

    assert verdict["adherent"] is False

    # And the operator who DID consult is still measured as adherent.
    same = adherence.record_data_query(
        "get_report", project_id="projA", trace_id=None,
        identity="person_a", now=1010.0,
    )
    assert same["adherent"] is True


def test_cross_project_time_window_does_not_leak_adherence(_no_sinks, monkeypatch):
    """[tw session key] Tracing OFF, same window: project A's context call must NOT
    make project B's data query adherent (the _context_seen map is process-global,
    so the time-window key MUST include project_id to isolate tenants)."""
    monkeypatch.setenv("ADHERENCE_SESSION_WINDOW_SECONDS", "300")
    # Project A consults context (no trace -> time-window session).
    adherence.mark_context_call(
        "search_context", trace_id=None, project_id="projA", now=1000.0
    )
    # Project B queries data in the SAME wall-clock window, no context of its own.
    verdict = adherence.record_data_query(
        "get_card", project_id="projB", trace_id=None, now=1100.0
    )
    assert verdict["adherent"] is False  # no cross-tenant leak
    assert verdict["context_tool"] is None
    # Project A's own data query in the same window is still adherent.
    verdict_a = adherence.record_data_query(
        "get_report", project_id="projA", trace_id=None, now=1150.0
    )
    assert verdict_a["adherent"] is True


# ---------------------------------------------------------------------------
# The one-line pointer (AD-1: ~1 line, never a dump) + <=30-line cap preserved.
# ---------------------------------------------------------------------------


def test_pointer_none_when_adherent():
    defs = {"clicks": {"definition": "Clics", "direction": "up_good"}}
    assert adherence.build_context_pointer(defs, adherent=True) is None


def test_pointer_none_when_no_definitions():
    assert adherence.build_context_pointer(None, adherent=False) is None
    assert adherence.build_context_pointer({}, adherent=False) is None


def test_pointer_is_single_line_and_mentions_search_context():
    defs = {
        "clicks": {"definition": "Clics"},
        "impressions": {"definition": "Impr."},
        "conversions": {"definition": "Conv."},
        "cost": {"definition": "Cout"},
    }
    pointer = adherence.build_context_pointer(defs, adherent=False)
    assert pointer is not None
    assert "\n" not in pointer  # EXACTLY one line (AD-1)
    assert "search_context" in pointer
    # It POINTS, it does not DUMP: at most 3 terms named + an ellipsis, never the
    # definition texts themselves.
    assert "Clics" not in pointer
    assert pointer.count(",") <= 2  # <=3 terms listed


def test_append_pointer_preserves_30_line_cap():
    # A summary already AT the 30-line cap.
    summary = "\n".join(f"line {i}" for i in range(30))
    pointer = "Astuce contexte : appelez search_context."
    out = adherence.append_pointer_within_cap(summary, pointer)
    lines = out.split("\n")
    assert len(lines) <= 30, f"cap broken: {len(lines)} lines"
    # The pointer survives (tail trimmed, not the pointer).
    assert lines[-1] == pointer


def test_append_pointer_noop_when_pointer_falsy():
    summary = "one\ntwo"
    assert adherence.append_pointer_within_cap(summary, None) == summary
    assert adherence.append_pointer_within_cap(summary, "") == summary


def test_append_pointer_under_cap_keeps_all_lines():
    summary = "a\nb\nc"
    pointer = "ptr"
    out = adherence.append_pointer_within_cap(summary, pointer)
    assert out.split("\n") == ["a", "b", "c", "ptr"]


# ---------------------------------------------------------------------------
# Non-blocking / never raises: sink failures must not surface (AD-18).
# ---------------------------------------------------------------------------


def test_record_data_query_never_raises_on_sink_failure(monkeypatch):
    def _boom(*a, **k):
        raise RuntimeError("langfuse down")

    monkeypatch.setattr(adherence, "_record_trace_attributes", _boom)
    monkeypatch.setattr(adherence, "_record_postgres_fallback", _boom)
    # record_data_query catches sink errors internally; assert it returns a verdict.
    verdict = adherence.record_data_query("get_report", project_id="p", trace_id="1" * 32)
    assert verdict["data_tool"] == "get_report"
    assert verdict["adherent"] is False


def test_mark_context_call_never_raises(monkeypatch):
    # Force session_key to blow up; mark must swallow it.
    monkeypatch.setattr(
        adherence, "session_key", lambda **k: (_ for _ in ()).throw(RuntimeError("x"))
    )
    adherence.mark_context_call("search_context", trace_id=None)  # no raise


# ---------------------------------------------------------------------------
# AI-84 -- le chemin OBSERVE, a cote du booleen.
# ---------------------------------------------------------------------------
#
# `adherent` repond « quelque chose a-t-il ete consulte avant celle-ci ». En
# mode `time_window`, cette reponse vient d'une fenetre de 300 secondes : une
# INFERENCE, et le docstring du module le dit lui-meme. Le chemin d'IA sait QUOI
# a ete consulte, enregistre sous le MEME trace id -- donc quand un trace existe
# il n'y a plus rien a inferer.
#
# Ce que ces tests tiennent : que le chemin arrive A COTE et ne REMPLACE jamais
# le booleen. Un lecteur doit pouvoir distinguer une adherence mesuree d'une
# adherence inferee ; une substitution silencieuse les rendrait identiques.


def test_the_basis_says_time_window_when_nothing_was_recorded(monkeypatch):
    """Sans trace : le verdict AVOUE que sa base est une fenetre horaire."""
    from core import adherence

    adherence.reset_for_tests()
    monkeypatch.setattr(adherence, "observed_path_for", lambda *a, **kw: None)

    verdict = adherence.record_data_query(
        "get_daily_report", project_id="proj_EXAMPLE", trace_id=None, identity="person_A"
    )

    assert verdict["observed_path"] is None
    assert verdict["adherence_basis"] == "time_window_inference"


def test_a_trace_without_a_recorded_path_is_not_called_observed(monkeypatch):
    """La moitie qu'on oublie : un trace ne suffit PAS.

    Un trace existe des que le client en envoie un ; cela ne dit rien de ce qui a
    ete consulte. Appeler cette base `observed_ai_path` ferait passer une session
    tracee pour une session dont on a lu le chemin.
    """
    from core import adherence

    adherence.reset_for_tests()
    monkeypatch.setattr(adherence, "observed_path_for", lambda *a, **kw: None)

    verdict = adherence.record_data_query(
        "get_daily_report", project_id="proj_EXAMPLE", trace_id="a" * 32
    )

    assert verdict["adherence_basis"] == "trace_session"


def test_a_recorded_path_travels_beside_the_boolean(monkeypatch):
    """Le chemin arrive, ET le booleen reste lisible tel quel."""
    from core import adherence

    adherence.reset_for_tests()
    path = {
        "step_count": 2,
        "steps": [
            {"kind": "context", "object_type": "context-topic", "object_id": "ct_1",
             "tool": "search_context"},
            {"kind": "data", "object_type": None, "object_id": None,
             "tool": "get_daily_report"},
        ],
    }
    monkeypatch.setattr(adherence, "observed_path_for", lambda *a, **kw: path)

    verdict = adherence.record_data_query(
        "get_daily_report", project_id="proj_EXAMPLE", trace_id="b" * 32
    )

    assert verdict["observed_path"] == path
    assert verdict["adherence_basis"] == "observed_ai_path"
    # Le booleen n'est PAS remplace : il garde sa propre reponse, qui est celle
    # de la session, pas celle du chemin.
    assert "adherent" in verdict


def test_an_unreadable_path_never_fails_the_data_query(monkeypatch):
    """AD-18 : mesure, jamais application.

    Une base qui ne repond pas doit rendre `observed_path: None`, pas lever --
    sinon la mesure de l'adherence casserait la requete qu'elle observe.
    """
    from core import adherence

    adherence.reset_for_tests()

    def _boom(*_a, **_kw):
        raise RuntimeError("platform database unreachable")

    monkeypatch.setattr("core.db.get_connection", _boom)

    assert adherence.observed_path_for("proj_EXAMPLE", "c" * 32) is None

    verdict = adherence.record_data_query(
        "get_daily_report", project_id="proj_EXAMPLE", trace_id="c" * 32
    )
    assert verdict["observed_path"] is None
    assert verdict["adherence_basis"] == "trace_session"


# ---------------------------------------------------------------------------
# The READER, and the one merge the ratified amendment forbids.
#
# `analyze-and-test.md:1330` -- "A trace-identified session and a wall-clock
# inference are reported apart and never merged", and its Incomplete if closes
# on "an inferred session is pooled with an observed one".
#
# The overview carried that merge twice: a top-level `adherent` /
# `share_adherent` summed across every `session_kind`, and a `by_data_tool`
# bucket keyed on the tool alone. These tests read the payload with a fake
# cursor -- no PostgreSQL, so the guard runs on every machine rather than only
# where a DSN happens to be exported.
# ---------------------------------------------------------------------------

_T0 = datetime(2026, 8, 24, 9, 0, tzinfo=timezone.utc)


class _FakeCursor:
    """Answers the four statements `adherence_overview` issues, and no other.

    The fourth (2026-09-01) counts the data-tool calls the AI Path recorder saw
    in the window -- `app.ai_path_steps` -- and it is what lets an empty window
    say whether a data question was asked at all.
    """

    def __init__(self, rows, context_rows, observed_calls=0):
        self._rows = rows
        self._context_rows = context_rows
        self._observed_calls = observed_calls
        self._pending: list = []
        self.observed_params = None

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def execute(self, sql, _params=None):
        if "make_interval" in sql:
            self._pending = [(_T0 - timedelta(days=30), _T0)]
        elif "GROUP BY data_tool" in sql:
            self._pending = list(self._rows)
        elif "GROUP BY context_tool" in sql:
            self._pending = list(self._context_rows)
        elif "app.ai_path_steps" in sql:
            self.observed_params = _params
            self._pending = [(self._observed_calls,)]
        else:  # pragma: no cover -- a fifth statement must be noticed, not guessed
            raise AssertionError(f"unexpected statement: {sql}")

    def fetchone(self):
        return self._pending[0]

    def fetchall(self):
        return list(self._pending)


class _FakeConn:
    def __init__(self, rows, context_rows=(), observed_calls=0):
        self._rows = rows
        self._context_rows = context_rows
        self._observed_calls = observed_calls
        self.cursors: list = []

    def cursor(self):
        cursor = _FakeCursor(self._rows, self._context_rows, self._observed_calls)
        self.cursors.append(cursor)
        return cursor


#: Three trace rows and one time_window row, all on the same data tool. Pooled
#: they read "3 of 4"; that string is exactly what must not be constructible.
_MIXED_ROWS = [
    ("get_report", "trace", True, 2, _T0, _T0),
    ("get_report", "trace", False, 1, _T0, _T0),
    ("get_report", "time_window", True, 1, _T0, _T0),
]


def test_no_reported_figure_merges_an_inferred_session_with_an_observed_one():
    """The FIGURE, not the labels.

    The guard this replaces asserted that both basis labels rendered -- which
    they already did, on a payload whose headline was the pooled total. A guard
    that measures its own weaker version is not a guard. This one names the
    pooled numbers and refuses them: put `total` back and it goes red on
    `adherent`, key `by_data_tool` on the tool alone and it goes red on the
    bucket of four.
    """
    payload = adherence.adherence_overview(
        _FakeConn(_MIXED_ROWS, [("search_context", 3)]), project_id="proj_EXAMPLE"
    )

    # No pooled adherence claim anywhere at the top level. `observations` stays,
    # because a count of measurements carries no verdict and no share can be
    # built from it.
    assert payload["observations"] == 4
    for pooled in ("adherent", "not_adherent", "share_adherent"):
        assert pooled not in payload, f"a pooled {pooled} is back at the top level"

    # Every counted bucket stands on ONE basis and says which.
    for bucket in payload["by_data_tool"] + payload["by_basis"]:
        assert bucket["basis"] in {"observed_session", "inferred_window"}
        assert bucket["means"]
        assert bucket["observations"] < 4, (
            "a bucket of four spans both bases: the inference is merged in"
        )


def test_the_same_data_tool_is_reported_once_per_basis():
    payload = adherence.adherence_overview(
        _FakeConn(_MIXED_ROWS, []), project_id="proj_EXAMPLE"
    )

    split = {
        (row["data_tool"], row["basis"]): (row["observations"], row["adherent"])
        for row in payload["by_data_tool"]
    }
    assert split == {
        ("get_report", "observed_session"): (3, 2),
        ("get_report", "inferred_window"): (1, 1),
    }
    # Each share sits beside the denominator it came from, never beside a total.
    shares = {row["basis"]: row["share_adherent"] for row in payload["by_data_tool"]}
    assert shares["observed_session"] == pytest.approx(2 / 3, abs=1e-4)
    assert shares["inferred_window"] == 1.0


def test_the_basis_totals_are_the_only_totals():
    payload = adherence.adherence_overview(
        _FakeConn(_MIXED_ROWS, []), project_id="proj_EXAMPLE"
    )

    totals = {row["basis"]: row for row in payload["by_basis"]}
    assert totals["observed_session"]["observations"] == 3
    assert totals["observed_session"]["adherent"] == 2
    assert totals["inferred_window"]["observations"] == 1
    assert totals["inferred_window"]["adherent"] == 1
    # And they are NOT added up for the reader anywhere in the payload.
    assert sum(row["observations"] for row in payload["by_basis"]) == payload["observations"]


def test_an_empty_window_still_says_why_and_names_the_gesture():
    payload = adherence.adherence_overview(_FakeConn([], []), project_id="proj_EXAMPLE")

    assert payload["observations"] == 0
    assert payload["by_data_tool"] == [] and payload["by_basis"] == []
    empty = payload["empty_state"]
    assert empty["headline"] == "Nothing recorded yet."
    assert "ask it one question" in empty["next_step"]


# ---------------------------------------------------------------------------
# 2026-09-01 -- the Analyze path is measured like the report and card tools.
#
# Three real Analyze sessions ran `get_procedure` -> `get_knowledge` ->
# `execute_analyze_query_spec` against the deployed server and
# `get_context_adherence` answered "No adherence observation": the gate measured
# three tools and the Analyze door was not one of them, and `get_knowledge` --
# the second call of every session -- was not a consult it counted. These tests
# hold the repaired perimeter by value (`analyze-and-test.md`, amendment of
# 2026-09-01); `tests/conformance/test_every_data_question_is_measured.py`
# holds it against the inventory.
# ---------------------------------------------------------------------------

_ANALYZE_DATA_TOOLS = (
    "execute_analyze_query_spec",
    "explore_analyze_query",
    "analyze_result",
    "render_analyze_result",
)


def test_an_analyze_execution_after_a_knowledge_consult_is_adherent_on_the_trace(
    _no_sinks, monkeypatch
):
    """The exact sequence of the three observed sessions, under one trace."""
    trace = "9" * 32
    steps = {
        "step_count": 3,
        "steps": [
            {"kind": "tool_call", "object_type": None, "object_id": None,
             "tool": "get_procedure"},
            {"kind": "tool_call", "object_type": None, "object_id": None,
             "tool": "get_knowledge"},
            {"kind": "tool_call", "object_type": None, "object_id": None,
             "tool": "execute_analyze_query_spec"},
        ],
    }
    monkeypatch.setattr(adherence, "observed_path_for", lambda *a, **kw: steps)

    adherence.mark_context_call("get_procedure", trace_id=trace, project_id="proj_EXAMPLE")
    adherence.mark_context_call("get_knowledge", trace_id=trace, project_id="proj_EXAMPLE")
    verdict = adherence.record_data_query(
        "execute_analyze_query_spec", project_id="proj_EXAMPLE", trace_id=trace
    )

    assert verdict["adherent"] is True
    assert verdict["context_tool"] == "get_knowledge"
    assert verdict["session_kind"] == "trace"
    assert verdict["data_tool"] == "execute_analyze_query_spec"
    # The path is read under the SAME trace, so the basis is the observed path,
    # not the inference -- the reason the Analyze hook records after commit.
    assert verdict["adherence_basis"] == "observed_ai_path"
    assert verdict["observed_path"] == steps


@pytest.mark.parametrize("data_tool", _ANALYZE_DATA_TOOLS + ("get_datastream_report",))
def test_a_data_question_with_no_consult_is_recorded_non_adherent(
    _no_sinks, monkeypatch, data_tool
):
    """Recorded -- with its name -- and never blocked. The measure, not the gate."""
    monkeypatch.setattr(adherence, "observed_path_for", lambda *a, **kw: None)

    verdict = adherence.record_data_query(
        data_tool, project_id="proj_EXAMPLE", trace_id="8" * 32
    )

    assert verdict["adherent"] is False
    assert verdict["context_tool"] is None
    assert verdict["data_tool"] == data_tool
    assert verdict["adherence_basis"] == "trace_session"


def test_get_knowledge_counts_as_a_consult_in_the_time_window_too(_no_sinks, monkeypatch):
    monkeypatch.setenv("ADHERENCE_SESSION_WINDOW_SECONDS", "300")

    adherence.mark_context_call(
        "get_knowledge", trace_id=None, project_id="proj_EXAMPLE",
        identity="person_a", now=1000.0,
    )
    verdict = adherence.record_data_query(
        "explore_analyze_query", project_id="proj_EXAMPLE", trace_id=None,
        identity="person_a", now=1010.0,
    )

    assert verdict["adherent"] is True
    assert verdict["context_tool"] == "get_knowledge"
    assert verdict["session_kind"] == "time_window"


def test_the_measured_lists_name_the_analyze_tools_and_the_knowledge_consult():
    """What `get_context_adherence` and the console read as the perimeter."""
    payload = adherence.adherence_overview(_FakeConn([], []), project_id="proj_EXAMPLE")

    for tool in _ANALYZE_DATA_TOOLS + ("get_datastream_report",):
        assert tool in payload["measured_data_tools"], f"{tool} is not measured"
    for tool in ("get_daily_report", "get_report", "get_card"):
        assert tool in payload["measured_data_tools"], f"{tool} fell out of the measure"
    assert "get_knowledge" in payload["measured_context_tools"]
    assert payload["measured_data_tools"] == list(adherence.DATA_TOOLS)
    assert payload["measured_context_tools"] == list(adherence.CONTEXT_TOOLS)


def test_the_observed_calls_are_counted_over_the_measured_data_tools_only():
    """The fourth statement asks for the measured tools, by name, as one list."""
    conn = _FakeConn([], [], observed_calls=0)
    adherence.adherence_overview(conn, project_id="proj_EXAMPLE")

    asked = [c.observed_params for c in conn.cursors if c.observed_params is not None]
    assert asked, "the overview never counted the observed data-tool calls"
    project, _window_from, tools = asked[0]
    assert project == "proj_EXAMPLE"
    assert tools == list(adherence.DATA_TOOLS)


def test_an_empty_window_with_no_question_keeps_its_quiet_sentence():
    payload = adherence.adherence_overview(
        _FakeConn([], [], observed_calls=0), project_id="proj_EXAMPLE"
    )

    assert payload["observations"] == 0
    assert payload["data_tool_calls_observed"]["count"] == 0
    assert payload["empty_state"]["headline"] == "Nothing recorded yet."
    assert "ask it one question" in payload["empty_state"]["next_step"]


def test_an_empty_window_with_unmeasured_questions_says_so_and_names_the_gesture():
    """The reading of 2026-09-01, made distinguishable from a quiet project.

    Three questions were asked and the window read "Nothing recorded yet" -- the
    same sentence a project nobody had asked anything would get. The two call
    for opposite gestures, so they are two states.
    """
    payload = adherence.adherence_overview(
        _FakeConn([], [], observed_calls=3), project_id="proj_EXAMPLE"
    )

    assert payload["observations"] == 0
    assert payload["data_tool_calls_observed"]["count"] == 3
    empty = payload["empty_state"]
    assert empty["headline"] == "Data questions were asked here, and none was measured."
    assert "3 call(s)" in empty["detail"]
    assert "Ask one question again" in empty["next_step"]
    # Never a table name, never a deployment state, in what a person reads.
    for text in empty.values():
        assert "ai_path_steps" not in text and "query_adherence" not in text
        assert "deploy" not in text.lower()


def test_a_measured_window_carries_the_observed_count_without_merging_it():
    """`data_tool_calls_observed` sits beside the buckets and enters no share."""
    payload = adherence.adherence_overview(
        _FakeConn(_MIXED_ROWS, [], observed_calls=9), project_id="proj_EXAMPLE"
    )

    assert payload["observations"] == 4
    assert payload["data_tool_calls_observed"]["count"] == 9
    assert payload["empty_state"] is None
    assert sum(row["observations"] for row in payload["by_basis"]) == 4


# ---------------------------------------------------------------------------
# One trace source for both halves of the pair.
# ---------------------------------------------------------------------------


def test_the_exchange_trace_prefers_the_clients_traceparent(monkeypatch):
    """The consult mark and the data verdict read the SAME id, and it is the
    one the AI Path recorder keyed the observed path on."""
    from core import ai_path_recorder, tracing

    monkeypatch.setattr(ai_path_recorder, "current_call_trace_id", lambda: "a" * 32)
    monkeypatch.setattr(tracing, "current_trace_id_hex", lambda: "b" * 32)

    assert adherence.current_exchange_trace_id() == "a" * 32


def test_the_exchange_trace_falls_back_to_the_active_span(monkeypatch):
    from core import ai_path_recorder, tracing

    monkeypatch.setattr(ai_path_recorder, "current_call_trace_id", lambda: None)
    monkeypatch.setattr(tracing, "current_trace_id_hex", lambda: "b" * 32)

    assert adherence.current_exchange_trace_id() == "b" * 32


def test_the_exchange_trace_is_none_when_nobody_named_one(monkeypatch):
    from core import ai_path_recorder, tracing

    monkeypatch.setattr(ai_path_recorder, "current_call_trace_id", lambda: None)
    monkeypatch.setattr(tracing, "current_trace_id_hex", lambda: None)

    assert adherence.current_exchange_trace_id() is None


def test_the_exchange_trace_never_raises(monkeypatch):
    from core import ai_path_recorder, tracing

    def _boom():
        raise RuntimeError("no context")

    monkeypatch.setattr(ai_path_recorder, "current_call_trace_id", _boom)
    monkeypatch.setattr(tracing, "current_trace_id_hex", _boom)

    assert adherence.current_exchange_trace_id() is None
