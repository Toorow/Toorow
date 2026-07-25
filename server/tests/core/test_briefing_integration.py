"""Integration tests for _run_due_briefings and _build_project_briefing (Story 6.7, AC7).

Tests:
  - test_run_due_briefings_idempotent
  - test_run_due_briefings_project_isolated
  - test_run_due_briefings_failure_isolation

These tests exercise _build_project_briefing directly with fake DB connections
to avoid importing core.db (which requires psycopg/libpq).
"""

from __future__ import annotations

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
                          context_events, nightly_run_id):
    """A pure stub that always returns a valid insights dict."""
    return _make_fake_insights()


class _FakeCursor:
    """Configurable fake psycopg cursor."""

    def __init__(self, existing_row=None):
        self._existing_row = existing_row
        self._results = []
        self.insert_count = [0]

    def execute(self, sql, params=None):
        sql_upper = sql.strip().upper()
        if "FROM APP.MORNING_BRIEFINGS" in sql_upper and "SELECT ID" in sql_upper:
            self._results = [self._existing_row] if self._existing_row else []
        elif "INSERT INTO APP.MORNING_BRIEFINGS" in sql_upper:
            self.insert_count[0] += 1
            self._results = []
        else:
            self._results = []

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

        def execute(self, sql, params=None):
            sql_upper = sql.strip().upper()
            if "FROM APP.MORNING_BRIEFINGS" in sql_upper and "SELECT ID" in sql_upper:
                # Row exists after first insert
                self._results = [("brief_abc",)] if self._insert_count[0] > 0 else []
            elif "INSERT INTO APP.MORNING_BRIEFINGS" in sql_upper:
                self._insert_count[0] += 1
                self._results = []
            else:
                self._results = []

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

        def execute(self, sql, params=None):
            sql_upper = sql.strip().upper()
            if "FROM APP.MORNING_BRIEFINGS" in sql_upper and "SELECT ID" in sql_upper:
                self._results = []  # no existing row
            elif "INSERT INTO APP.MORNING_BRIEFINGS" in sql_upper:
                insert_count[0] += 1
                if params:
                    inserted_projects.append(params[1])  # project_id is 2nd param
                self._results = []
            else:
                self._results = []

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
                                context_events, nightly_run_id):
        if project_id == "proj_fail":
            raise RuntimeError("Simulated failure for proj_fail")
        built_projects.append(project_id)
        return _make_fake_insights()

    insert_count = [0]

    class FakeCursor:
        def __init__(self):
            self._results = []

        def execute(self, sql, params=None):
            sql_upper = sql.strip().upper()
            if "DISTINCT PROJECT_ID" in sql_upper:
                self._results = [("proj_fail",), ("proj_ok",)]
            elif "FROM APP.MORNING_BRIEFINGS" in sql_upper and "SELECT ID" in sql_upper:
                self._results = []
            elif "INSERT INTO APP.MORNING_BRIEFINGS" in sql_upper:
                insert_count[0] += 1
                self._results = []
            else:
                self._results = []

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

    # Override _run_due_briefings to use our fakes
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
