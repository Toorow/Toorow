"""Integration tests for _run_due_briefings and _build_project_briefing (Story 6.7, AC7).

Tests:
  - test_run_due_briefings_idempotent
  - test_run_due_briefings_project_isolated
  - test_run_due_briefings_failure_isolation
  - test_the_fakes_refuse_a_statement_they_were_never_taught

These tests exercise _build_project_briefing directly with fake DB connections
to avoid importing core.db (which requires psycopg/libpq).
"""

from __future__ import annotations

import pytest

from tests.support.statement_router import StatementInventory, UnknownStatement, describe

# ---------------------------------------------------------------------------
# The statements `_build_project_briefing` puts through the connection it is
# HANDED. Four fakes in this file each recognized the same two of them -- the
# idempotency SELECT and the INSERT -- and answered everything else with no
# rows and no `description` attribute at all.
#
# The three alert reads landed in that silence. The scheduler fetches business,
# anomaly and mediaplan firings through the very connection these tests supply
# (scheduler.py:447, 451, 455), and each of those readers asks the cursor for
# `cur.description` (business_alerts.py:519, anomaly_alerts.py:687,
# mediaplan_alerts.py:711). The old fakes had no such attribute, the
# `AttributeError` was swallowed by each reader's own `except Exception`, and
# every run of every test in this file built a briefing over "this project has
# no alert" -- a state the test never chose (AI-317).
#
# Declaration order is match order. The three firing reads share one relation,
# so each fragment names what separates it from the other two rather than being
# widened until it swallows them.
# ---------------------------------------------------------------------------
_BRIEFING = StatementInventory(
    "the briefing fakes",
    existing_briefing=("select id", "from app.morning_briefings"),
    briefing_write="insert into app.morning_briefings",
    business_firings=("from app.alert_firings f", "join app.alert_definitions d"),
    anomaly_firings=("from app.alert_firings", "type = 'anomaly'"),
    mediaplan_firings=("from app.alert_firings", "where type = %s"),
)

# The window these tests run in holds no firing -- a real state of the product,
# not a fabricated row. What the fakes must NOT keep doing is answer it without
# the `description` psycopg would report, because the product reads that first.
_FIRING_READS = ("business_firings", "anomaly_firings", "mediaplan_firings")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_fake_insights():
    """Return a minimal valid insights JSONB dict."""
    return {
        "version": 1,
        "briefing_date": "2026-07-12",
        "insights": [],
        "alerts_count": 0,
        "anomalies_count": 0,
        "build_duration_ms": 5,
    }


def _fake_build_briefing(project_id, briefing_date, alert_firings, rollup,
                          context_events, nightly_run_id, detector_readiness=None,
                          context_events_unavailable=None):
    """A pure stub that always returns a valid insights dict.

    `detector_readiness` is here because the real `build_briefing` has taken it
    since Story 53.8 and the scheduler now passes it (CAV-13). A double that does
    not accept what the caller sends fails on the ARGUMENT rather than on the
    behaviour it was written to check -- which is what happened when the wiring
    landed, and why the default is `None`: it keeps the stub honest about the
    signature without making these tests about readiness.

    `context_events_unavailable` is here for the same reason since AI-344: the
    scheduler passes it on every call, whether or not the window could be read.
    What it CARRIES is checked against the real builder in
    `test_briefing_context_unavailable.py`.
    """
    return _make_fake_insights()


class _FakeCursor:
    """Configurable fake psycopg cursor."""

    def __init__(self, existing_row=None):
        self._existing_row = existing_row
        self._results = []
        self.description = None
        self.insert_count = [0]

    def execute(self, sql, params=None):
        statement = _BRIEFING.match(sql)
        if statement == "existing_briefing":
            self._results = [self._existing_row] if self._existing_row else []
            self.description = describe(sql)
        elif statement == "briefing_write":
            self.insert_count[0] += 1
            self._results = []
            self.description = None  # an INSERT with no RETURNING reports none
        else:  # the three firing reads -- no firing in the window
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


class _FakeConn:
    """Fake DB connection that tracks inserts."""

    def __init__(self, existing_row=None):
        self._cursor = _FakeCursor(existing_row=existing_row)
        self.committed = False

    def cursor(self):
        return self._cursor

    def commit(self):
        self.committed = True

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass


# ---------------------------------------------------------------------------
# test_run_due_briefings_idempotent
# ---------------------------------------------------------------------------

def test_run_due_briefings_idempotent():
    """Run _build_project_briefing twice -> only one row inserted (ON CONFLICT DO NOTHING).

    After the first run, the second run detects the existing row and skips.
    """
    from core.scheduler import _build_project_briefing

    insert_count = [0]

    class TrackingCursor:
        """Cursor that tracks inserts and simulates a row after the first insert."""

        def __init__(self, _insert_count):
            self._insert_count = _insert_count
            self._results = []
            self.description = None

        def execute(self, sql, params=None):
            statement = _BRIEFING.match(sql)
            if statement == "existing_briefing":
                # Row exists after first insert
                self._results = [("brief_abc",)] if self._insert_count[0] > 0 else []
                self.description = describe(sql)
            elif statement == "briefing_write":
                self._insert_count[0] += 1
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

    class TrackingConn:
        def cursor(self):
            return TrackingCursor(insert_count)

        def commit(self):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

    def fake_get_conn():
        return TrackingConn()

    # First call: should insert
    _build_project_briefing(
        project_id="default",
        nightly_run_id="nrun_1",
        get_connection=fake_get_conn,
        build_briefing=_fake_build_briefing,
    )
    assert insert_count[0] == 1, f"Expected 1 insert after first run, got {insert_count[0]}"

    # Second call: should detect existing row and skip
    _build_project_briefing(
        project_id="default",
        nightly_run_id="nrun_2",
        get_connection=fake_get_conn,
        build_briefing=_fake_build_briefing,
    )
    assert insert_count[0] == 1, (
        f"Expected still 1 insert after second run (idempotent), got {insert_count[0]}"
    )


# ---------------------------------------------------------------------------
# test_run_due_briefings_project_isolated
# ---------------------------------------------------------------------------

def test_run_due_briefings_project_isolated():
    """Two projects -> two briefing rows built (one per project)."""
    from core.scheduler import _build_project_briefing

    insert_count = [0]
    inserted_projects: list[str] = []

    class PerProjectCursor:
        def __init__(self):
            self._results = []
            self.description = None

        def execute(self, sql, params=None):
            statement = _BRIEFING.match(sql)
            if statement == "existing_briefing":
                self._results = []  # no existing row
                self.description = describe(sql)
            elif statement == "briefing_write":
                insert_count[0] += 1
                if params:
                    inserted_projects.append(params[1])  # project_id is 2nd param
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

    class PerProjectConn:
        def cursor(self):
            return PerProjectCursor()

        def commit(self):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

    def fake_get_conn():
        return PerProjectConn()

    for pid in ["proj_alpha", "proj_beta"]:
        _build_project_briefing(
            project_id=pid,
            nightly_run_id="nrun_test",
            get_connection=fake_get_conn,
            build_briefing=_fake_build_briefing,
        )

    assert insert_count[0] == 2, (
        f"Expected 2 briefing rows (one per project), got {insert_count[0]}"
    )
    assert "proj_alpha" in inserted_projects
    assert "proj_beta" in inserted_projects


# ---------------------------------------------------------------------------
# test_run_due_briefings_failure_isolation
# ---------------------------------------------------------------------------

def test_run_due_briefings_failure_isolation(monkeypatch):
    """build_briefing raises on project_fail -> project_ok is still processed.

    Tests that _run_due_briefings catches per-project errors and continues.
    """
    monkeypatch.setenv("BRIEFING_ENABLED", "true")

    built_projects: list[str] = []

    def build_briefing_raising(project_id, briefing_date, alert_firings, rollup,
                                context_events, nightly_run_id, detector_readiness=None,
                                context_events_unavailable=None):
        if project_id == "proj_fail":
            raise RuntimeError("Simulated failure for proj_fail")
        built_projects.append(project_id)
        return _make_fake_insights()

    insert_count = [0]

    class FakeCursor:
        def __init__(self):
            self._results = []
            self.description = None

        def execute(self, sql, params=None):
            statement = _BRIEFING.match(sql)
            if statement == "existing_briefing":
                self._results = []
                self.description = describe(sql)
            elif statement == "briefing_write":
                insert_count[0] += 1
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

    class FakeConn:
        def cursor(self):
            return FakeCursor()

        def commit(self):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

    from core import scheduler as sched

    # Override _run_due_briefings to use our fakes.
    #
    # The project list is hard-coded here, so `SELECT DISTINCT project_id FROM
    # app.connection_ref` (scheduler.py:357) never reaches the cursor. The old
    # fake carried a branch for it that nothing could ever take; it is not in
    # the inventory, so the day this test drives the real `_run_due_briefings`
    # the fake will say so by name instead of answering two invented projects.
    def fake_run_due_briefings(nightly_run_id: str) -> None:
        """Reproduce _run_due_briefings logic with mocked DB."""
        project_ids = ["proj_fail", "proj_ok"]
        briefings_built = 0
        for project_id in project_ids:
            try:
                sched._build_project_briefing(
                    project_id=project_id,
                    nightly_run_id=nightly_run_id,
                    get_connection=lambda: FakeConn(),
                    build_briefing=build_briefing_raising,
                )
                briefings_built += 1
            except Exception as exc:
                import logging
                logging.getLogger("core.scheduler").warning(
                    "scheduler: briefing_failed: project_id=%s error=%s", project_id, exc
                )

        return briefings_built

    fake_run_due_briefings("nrun_isolation_test")

    # proj_ok should be built; proj_fail should be skipped with error logged
    assert "proj_ok" in built_projects, (
        f"proj_ok must be processed despite proj_fail raising. built={built_projects}"
    )
    assert "proj_fail" not in built_projects, (
        "proj_fail should NOT appear in built_projects (it raised)"
    )
    # Only proj_ok was inserted
    assert insert_count[0] == 1, (
        f"Expected 1 insert (proj_ok only), got {insert_count[0]}"
    )


# ---------------------------------------------------------------------------
# test_the_fakes_refuse_a_statement_they_were_never_taught
# ---------------------------------------------------------------------------

def test_the_fakes_refuse_a_statement_they_were_never_taught():
    """AI-317: a briefing query that moved must not read like "no briefing".

    The four fakes in this file share ONE inventory, so this holds for all of
    them: a statement that is not in it comes back named, carrying the statement
    itself and the neighbours the fake does know, instead of an empty result the
    caller reads as a project with nothing to report.
    """
    cursor = _FakeCursor()

    with pytest.raises(UnknownStatement) as raised:
        cursor.execute(
            "SELECT id FROM app.evening_briefings WHERE project_id = %s",
            ("proj_alpha",),
        )

    message = str(raised.value)
    assert "app.evening_briefings" in message, message
    assert "existing_briefing" in message, message
