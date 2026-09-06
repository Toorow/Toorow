"""AI-344 -- the morning briefing says when its context window was NOT READ.

Ledger clauses `context-hub[75]` and `[78]`, both measured red on 2026-09-01.
`context_events.fetch_context_events` stopped answering `[]` for a window it
could not read and raises `ContextEventsUnavailable` instead; the card door
(`cards_mcp`) and the two report doors (`reporting_mcp` / `reports` /
`report_mcp`) carry the resulting `{reason, repair}` on their envelopes. The
briefing did not: `scheduler._build_project_briefing` caught the refusal, logged
it at WARNING, and then called `build_briefing(..., context_events=[])`. The row
written to `app.morning_briefings` was byte-for-byte the row of a quiet week.

Three tests, in the order the defect is repaired:

  (a) the builder takes the payload and puts it in the insights JSON, and the
      briefing's own line then says the window was not read -- in the SAME
      catalogue clause the report's "Why" section already emits, never a second
      sentence for the same fact;
  (b) the scheduler, on a simulated `ContextEventsUnavailable`, writes a row that
      CARRIES it -- read back off the INSERT parameters, which is the row;
  (c) the class guard: the four readers of context events named by `[78]`, by
      name, each with the channel by which "not read" leaves it.

(b) uses the in-process fake connection of `test_briefing_integration.py` rather
than a live Postgres: what is under test is the value the scheduler binds into
the INSERT, and a real server would echo back the same bytes for a slower run.
The store itself is proven on disposable Postgres by
`test_mirror_fetch_context_events.py`.
"""

from __future__ import annotations

import inspect
import json

import pytest

from tests.support.statement_router import StatementInventory, describe

# The scheduler's statements, same inventory (and same match order) as
# `test_briefing_integration.py`: this file drives the very same function.
_BRIEFING = StatementInventory(
    "the briefing fakes",
    existing_briefing=("select id", "from app.morning_briefings"),
    briefing_write="insert into app.morning_briefings",
    business_firings=("from app.alert_firings f", "join app.alert_definitions d"),
    anomaly_firings=("from app.alert_firings", "type = 'anomaly'"),
    mediaplan_firings=("from app.alert_firings", "where type = %s"),
)
_FIRING_READS = ("business_firings", "anomaly_firings", "mediaplan_firings")

_UNAVAILABLE = {
    "reason": "The context events were not read: no mirror is kept on this "
    "deployment and the record could not be reached.",
    "repair": "Point TOOROW_DUCKDB_PATH at a synced mirror, or restore the "
    "connection to the events store.",
}


def _one_anomaly_firing() -> list[dict]:
    """One real firing shape, so the briefing has an insight to be quiet about."""
    return [
        {
            "code": "anomaly",
            "metric": "sessions",
            "connector": "google-analytics",
            "window_date": "2026-08-31",
            "observed_value": 10,
            "expected_value": 2,
            "id": "fire_EXAMPLE",
        }
    ]


# ---------------------------------------------------------------------------
# (a) the builder
# ---------------------------------------------------------------------------


def test_build_briefing_carries_the_unavailable_beside_the_counts():
    """`context_events_unavailable` reaches the insights JSON, next to the counts
    it qualifies -- `alerts_count: 0` on an unread window is not an all-clear."""
    from core.briefing import build_briefing

    payload = build_briefing(
        project_id="proj_EXAMPLE",
        briefing_date="2026-09-01",
        alert_firings=_one_anomaly_firing(),
        rollup={},
        context_events=[],
        nightly_run_id="run_EXAMPLE",
        context_events_unavailable=_UNAVAILABLE,
    )

    assert payload["context_events_unavailable"] == _UNAVAILABLE
    assert set(payload["context_events_unavailable"]) == {"reason", "repair"}
    # It survives the JSONB round-trip -- the row stores text, not a dict.
    assert json.loads(json.dumps(payload))["context_events_unavailable"] == _UNAVAILABLE


def test_build_briefing_omits_the_marker_when_the_window_was_read():
    """A read window carries no marker at all. An always-present `null` would make
    "read, and empty" and "not read" the same shape again, which is the defect."""
    from core.briefing import build_briefing

    payload = build_briefing(
        project_id="proj_EXAMPLE",
        briefing_date="2026-09-01",
        alert_firings=_one_anomaly_firing(),
        rollup={},
        context_events=[],
        nightly_run_id="run_EXAMPLE",
    )

    assert "context_events_unavailable" not in payload


def test_the_briefing_line_says_not_read_and_never_reads_as_a_quiet_week():
    """The sentence the assistant gets. It is the catalogue's `context_unavailable`
    -- the clause the report's "Why" section already emits for the same state --
    and it is NOT `context_missing`, which claims the window was read."""
    from core.narrative_phrases import phrase
    from core.reporting_mcp import _describe_context_events_unread

    line = _describe_context_events_unread(_UNAVAILABLE)

    assert line is not None
    assert phrase("context_unavailable") in line
    assert phrase("context_missing") not in line
    # The reason and the repair ride the envelope, never the sentence.
    assert _UNAVAILABLE["reason"] not in line
    assert _UNAVAILABLE["repair"] not in line
    # A window that WAS read gets no line: the budget is not spent saying nothing.
    assert _describe_context_events_unread(None) is None
    assert _describe_context_events_unread({}) is None


# ---------------------------------------------------------------------------
# (b) the scheduler: the row that gets written
# ---------------------------------------------------------------------------


class _RecordingCursor:
    """The scheduler's cursor. Keeps every INSERT's parameters."""

    def __init__(self, writes: list):
        self._writes = writes
        self._results: list = []
        self.description = None

    def execute(self, sql, params=None):
        statement = _BRIEFING.match(sql)
        if statement == "existing_briefing":
            self._results = []
            self.description = describe(sql)
        elif statement == "briefing_write":
            self._writes.append(params)
            self._results = []
            self.description = None
        else:
            assert statement in _FIRING_READS
            self._results = []
            self.description = describe(sql)

    def fetchone(self):
        return self._results[0] if self._results else None

    def fetchall(self):
        return list(self._results)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass


class _RecordingConn:
    def __init__(self, writes: list):
        self._writes = writes

    def cursor(self):
        return _RecordingCursor(self._writes)

    def commit(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass


def _run_scheduler_briefing(monkeypatch, *, fetch) -> dict:
    """Run `_build_project_briefing` over the fakes; return the insights written."""
    from core import context_events as context_events_module
    from core.briefing import build_briefing
    from core.scheduler import _build_project_briefing

    monkeypatch.setattr(context_events_module, "fetch_context_events", fetch)

    writes: list = []
    _build_project_briefing(
        project_id="proj_EXAMPLE",
        nightly_run_id="run_EXAMPLE",
        get_connection=lambda: _RecordingConn(writes),
        build_briefing=build_briefing,
    )
    assert len(writes) == 1, f"expected exactly one briefing row, got {len(writes)}"
    # (id, project_id, briefing_date, insights_json, nightly_run_id)
    return json.loads(writes[0][3])


def test_the_written_row_carries_the_unavailable_when_no_store_can_serve(monkeypatch):
    """`ContextEventsUnavailable` out of the fetch -> the row written to
    `app.morning_briefings` says the window was not read. A WARNING in the log is
    not a reader: the ledger clause is about the ROW."""
    from core.context_events import ContextEventsUnavailable

    def _no_store(*a, **kw):
        raise ContextEventsUnavailable(_UNAVAILABLE["reason"], _UNAVAILABLE["repair"])

    insights = _run_scheduler_briefing(monkeypatch, fetch=_no_store)

    assert insights["context_events_unavailable"] == _UNAVAILABLE


def test_the_written_row_carries_no_marker_when_the_window_was_read(monkeypatch):
    """The same path with a store that answered: `[]` here means read-and-empty,
    and the row must not claim otherwise."""
    insights = _run_scheduler_briefing(monkeypatch, fetch=lambda *a, **kw: [])

    assert "context_events_unavailable" not in insights


# ---------------------------------------------------------------------------
# (c) the class guard -- the four readers, by name
# ---------------------------------------------------------------------------

#: Ledger clause `context-hub[78]`: "one of the four readers of context events
#: (the fetch, the candidate-cause walk, the deployment markers, the briefing
#: through the fetch) follows a different rule from the other three". The briefing
#: was the one that diverged. This table names them so a fifth reader added later
#: is added HERE, and so removing any one channel fails on the reader's name
#: rather than somewhere downstream. The card door is listed with them: it reads
#: the same events through the same fetch and carries the same key.
_READERS_WITH_A_SIGNATURE_CHANNEL = {
    "the briefing (core.briefing.build_briefing)": (
        "core.briefing",
        "build_briefing",
    ),
    "the report (core.reports.build_summary)": (
        "core.reports",
        "build_summary",
    ),
    "the card (core.cards.get_card)": (
        "core.cards",
        "get_card",
    ),
    # AI-350, the fifth path and the last one without a channel. It is not a new
    # READER of context events -- it is the ZERO-ROW branch of the daily report,
    # which hands the events to the legacy rollup summarizer instead of to the
    # narrative builder. Same events, same window, a different composer, and that
    # composer had no notion of "not read": measured 2026-09-01 on disposable
    # Postgres, `get_daily_report` printed "aucun événement connu" for a project
    # whose record held five live events, with `meta.context_events_unavailable`
    # set on the same response.
    "the empty daily report (core.summarizer.build_daily_report_summary)": (
        "core.summarizer",
        "build_daily_report_summary",
    ),
}

_MARKER = "context_events_unavailable"


@pytest.mark.parametrize("reader", sorted(_READERS_WITH_A_SIGNATURE_CHANNEL))
def test_every_reader_takes_the_unavailable_by_the_same_name(reader):
    """Each builder accepts the `{reason, repair}` under ONE name. Three of them
    took it before AI-344; the briefing is the fourth, and a reader that names it
    differently is a reader its caller cannot feed."""
    import importlib

    module_name, attribute = _READERS_WITH_A_SIGNATURE_CHANNEL[reader]
    func = getattr(importlib.import_module(module_name), attribute)
    parameters = inspect.signature(func).parameters

    assert _MARKER in parameters, (
        f"{reader} has no channel by which an unread window could reach its "
        f"output: {sorted(parameters)}"
    )
    assert parameters[_MARKER].default is None, (
        f"{reader} must default to None -- a reader that REQUIRES the marker "
        f"forces every caller to assert something about a read it did not do"
    )


def test_the_fetch_refuses_rather_than_answering_an_empty_window():
    """The first reader. It is the one that DECIDES the state, so it carries the
    payload as an exception rather than as a parameter."""
    from core.context_events import ContextEventsUnavailable

    unavailable = ContextEventsUnavailable("not read", "the repair")

    assert set(unavailable.payload) == {"reason", "repair"}
    assert unavailable.payload == {"reason": "not read", "repair": "the repair"}


def test_the_candidate_cause_walk_carries_the_unavailable_on_its_pairing(monkeypatch):
    """The second reader. It keeps its verdict, so the payload rides the pairing
    descriptor instead of the return value."""
    from datetime import date as _date

    from core import anomaly_alerts

    monkeypatch.delenv("TOOROW_DUCKDB_PATH", raising=False)
    labels, pairing = anomaly_alerts._fetch_context_events_for_anomaly(
        "proj_EXAMPLE",
        _date(2026, 8, 31),
        None,
        connector="google-analytics",
        metric="sessions",
        record_connection=None,
    )

    assert labels == []
    assert set(pairing["unavailable"]) == {"reason", "repair"}


# ---------------------------------------------------------------------------
# (d) AI-350 -- the zero-row branch of the daily report, and the one composer
# ---------------------------------------------------------------------------


def _daily_report_over_no_rows(monkeypatch, *, unavailable: dict | None) -> str:
    """`get_daily_report` on a window the warehouse has no row for.

    The warehouse and the context fetch are the only two seams stubbed: what is
    under test is which sentence the MODEL CHANNEL carries, and both stubs state
    a real deployment state -- an unseeded mart, and (for `unavailable`) the
    Cloud Run shape of AI-344 where no mirror is kept and the record cannot be
    reached. Proven end to end on disposable Postgres by the AI-350 walk.
    """
    from core import reporting_mcp
    from core.context_events import ContextEventsUnavailable

    monkeypatch.setattr(reporting_mcp.warehouse, "query_daily_report", lambda *a, **kw: [])
    monkeypatch.setattr("core.main._resolve_project", lambda p, identity=None: p)
    monkeypatch.setattr("core.main._refuse_unless_project_scope", lambda *a, **kw: None)

    def _fetch(*a, **kw):
        if unavailable is None:
            return []
        raise ContextEventsUnavailable(unavailable["reason"], unavailable["repair"])

    monkeypatch.setattr("core.main._fetch_context_events", _fetch)

    result = reporting_mcp.get_daily_report(
        project_id="proj_EXAMPLE",
        date_range={"start": "2026-08-01", "end": "2026-08-31"},
    )
    assert result.is_error is False, "an unread window never turns the report into an error"
    return result.content[0].text


def test_the_empty_daily_report_says_the_window_was_not_read(monkeypatch):
    """THE AI-350 DEFECT. Zero rows plus an unread context window used to print the
    legacy empty state -- "Contexte : aucun événement connu pour cette période" --
    while `meta.context_events_unavailable` on the same response said the events
    were never read. The sentence is the catalogue's, not a second wording."""
    from core.narrative_phrases import phrase

    text = _daily_report_over_no_rows(monkeypatch, unavailable=_UNAVAILABLE)

    assert phrase("context_unavailable") in text
    # It must not read as calm: neither the rollup summary's "aucun événement
    # connu" nor the narrative's "contexte manquant" -- both claim a read.
    assert phrase("summary_context_none") not in text
    assert phrase("context_missing") not in text
    # The reason and the repair ride the envelope; the sentence stays a sentence.
    assert _UNAVAILABLE["reason"] not in text
    assert _UNAVAILABLE["repair"] not in text


def test_the_empty_daily_report_still_says_quiet_when_the_window_was_read(monkeypatch):
    """The other half, and the one a careless repair breaks: a window that WAS
    read and holds nothing keeps saying so. "Not read" must not swallow "empty"."""
    from core.narrative_phrases import phrase

    text = _daily_report_over_no_rows(monkeypatch, unavailable=None)

    assert phrase("summary_context_none") in text
    assert phrase("context_unavailable") not in text


def test_the_absence_fork_is_composed_in_one_place():
    """The class, not the instance. Every reader that holds no context event asks
    `narrative.context_absence_line` which of the two absences it is looking at,
    so a sixth branch cannot re-decide it -- which is exactly how the zero-row
    branch of the daily report drifted away from the four readers of AI-344."""
    from core.narrative import context_absence_line
    from core.narrative_phrases import phrase

    assert context_absence_line(_UNAVAILABLE, read_and_empty="read") == phrase(
        "context_unavailable"
    )
    # Read-and-empty gives the CALLER's own wording back, untouched: the narrative
    # and the rollup summary have always worded that fact differently.
    assert context_absence_line(None, read_and_empty="read") == "read"
    assert context_absence_line({}, read_and_empty="read") == "read"
    assert context_absence_line("not a payload", read_and_empty="read") == "read"
    # A caller that says nothing for a read window still says nothing.
    assert context_absence_line(None, read_and_empty=None) is None
