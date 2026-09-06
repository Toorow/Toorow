from __future__ import annotations

import pytest
from core import getting_started
from core.getting_started import (
    _TASKS,
    _route,
    _state_for,
    read_getting_started,
    reconcile_project_journey,
)
from core.project_readiness import (
    READINESS_COMPONENTS,
    compose_project_readiness_from_evidence,
)


def test_getting_started_route_is_canonical_and_scope_complete():
    assert _route("org-1", "proj-1") == "/org/org-1/project/proj-1/getting-started"


def test_journey_state_is_derived_only_from_shared_readiness():
    readiness = compose_project_readiness_from_evidence(
        active_configuration_version_id="cfg-1",
        source_id=None,
        datastream_id="ds-1",
        first_value_id=None,
    )

    assert _state_for("project_foundation", readiness) == "completed"
    assert _state_for("source", readiness) == "blocked"
    assert _state_for("datastream", readiness) == "completed"
    assert _state_for("first_value", readiness) == "blocked"


def test_a_step_readiness_cannot_judge_is_not_given_a_state():
    """The defect that made the screen 503 on every production project.

    `app.setup_tasks.state` allows exactly seven values. `_state_for` answered
    the literal "unknown" for any step with no readiness component, the caller
    wrote it, and the CHECK constraint refused it -- so the whole GET failed.

    Three persisted step keys are in that case. None of them may produce a
    state; readiness having no opinion is not an opinion.
    """
    persisted_states = {"ready", "waiting", "blocked", "completed", "failed", "expired", "revoked"}
    readiness = compose_project_readiness_from_evidence(
        active_configuration_version_id="cfg-1",
        source_id="source-1",
        datastream_id="ds-1",
        first_value_id="run-1",
    )

    for step_key in ("invitation_accepted", "project_access", "host_connection"):
        assert _state_for(step_key, readiness) is None

    for step_key in ("project_foundation", "source", "datastream", "first_value",
                     "source_authorization", "first_report"):
        assert _state_for(step_key, readiness) in persisted_states


def test_readiness_version_is_deterministic_and_shared():
    evidence = dict(
        active_configuration_version_id="cfg-1",
        source_id="source-1",
        datastream_id="ds-1",
        first_value_id="run-1",
        governance_evidence_date="2026-08-16",
        governance_unresolved_issues=0,
    )
    overview = compose_project_readiness_from_evidence(**evidence)
    getting_started = compose_project_readiness_from_evidence(**evidence)

    assert overview["version"] == getting_started["version"]
    assert overview["schema_version"] == "project-readiness.v2"
    assert all(overview[key]["state"] == "ready" for key in READINESS_COMPONENTS)


def test_governance_is_not_ready_just_because_nothing_fired():
    """Zero out of zero is not an all-clear.

    A Project whose monitors have never evaluated anything has no unresolved
    quality issue -- trivially. Calling that `ready` would complete the Governance
    step of every brand-new Project on the strength of an absence, which is the
    exact shape of "unknown presented as healthy" that `overview.md:118` forbids.
    A day the monitors could run is the denominator.
    """
    never_evaluated = compose_project_readiness_from_evidence(
        active_configuration_version_id="cfg-1",
        source_id="source-1",
        datastream_id="ds-1",
        first_value_id="run-1",
        governance_evidence_date=None,
        governance_unresolved_issues=0,
    )
    assert never_evaluated["governance"]["state"] == "blocked"
    assert never_evaluated["governance"]["evidence_ref"] is None

    evaluated_but_open = compose_project_readiness_from_evidence(
        active_configuration_version_id="cfg-1",
        source_id="source-1",
        datastream_id="ds-1",
        first_value_id="run-1",
        governance_evidence_date="2026-08-16",
        governance_unresolved_issues=2,
    )
    assert evaluated_but_open["governance"]["state"] == "blocked"


def test_the_governance_step_can_now_be_completed():
    """The structural 4-of-5 cap.

    `_STEP_READINESS` had no entry for `governance`, so `_state_for` returned None,
    the step kept its persisted `blocked` forever and no Project could ever pass
    80%. A step no evidence can complete is not a step.
    """
    ready = compose_project_readiness_from_evidence(
        active_configuration_version_id="cfg-1",
        source_id="source-1",
        datastream_id="ds-1",
        first_value_id="run-1",
        governance_evidence_date="2026-08-16",
        governance_unresolved_issues=0,
    )
    assert _state_for("governance", ready) == "completed"
    assert [_state_for(step[0], ready) for step in _TASKS] == ["completed"] * 5


# --------------------------------------------------------------------------
# A READ DOES NOT WRITE -- amendment of 2026-08-17 to `overview.md`.
# --------------------------------------------------------------------------


class _RecordingCursor:
    """Records every statement and answers from a queue keyed on the SQL verb."""

    def __init__(self, recorder, answers):
        self.recorder = recorder
        self.answers = answers
        self._last = None
        # Every conditional write in `reconcile_project_journey` asks whether it
        # actually changed a row before journaling an event. One is the honest
        # answer for a statement this fake accepted.
        self.rowcount = 1

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def execute(self, sql, params=None):
        self.recorder.append((" ".join(str(sql).split()), params))
        self._last = str(sql)

    def _answer(self):
        for marker, value in self.answers:
            if marker in (self._last or ""):
                return value
        return None

    def fetchone(self):
        value = self._answer()
        return value[0] if isinstance(value, list) and value else value

    def fetchall(self):
        value = self._answer()
        return value if isinstance(value, list) else []


class _RecordingConnection:
    def __init__(self, answers):
        self.statements: list = []
        self.answers = answers

    def cursor(self):
        return _RecordingCursor(self.statements, self.answers)


_WRITE_VERBS = ("INSERT", "UPDATE", "DELETE", "TRUNCATE")

_JOURNEY_ROW = ("setup_1", "active", None, None, "org-1")
_TASK_ROWS = [
    (
        f"task_{key}", key, title, "blocked", "invited_operator", None, actor,
        None, None, None, {}, None, "none",
    )
    for key, title, actor, _readiness, _handoff in _TASKS
]


def _readiness_all_ready():
    return compose_project_readiness_from_evidence(
        active_configuration_version_id="cfg-1",
        source_id="source-1",
        datastream_id="ds-1",
        first_value_id="run-1",
        governance_evidence_date="2026-08-16",
        governance_unresolved_issues=0,
    )


@pytest.fixture
def _shared_readiness(monkeypatch):
    monkeypatch.setattr(
        getting_started, "compose_project_readiness", lambda *_a: _readiness_all_ready()
    )
    monkeypatch.setattr(
        getting_started,
        "_read_project_identity",
        lambda *_a: {
            "id": "proj_EXAMPLE",
            "name": "Demo Project",
            "organization": {"id": "org-1", "name": "Demo Org"},
        },
    )


def test_reading_getting_started_issues_no_write_statement(_shared_readiness):
    """The measured defect: `GET /getting-started` created, updated and inserted.

    The capability that route demands is `view`, so the first person to OPEN the
    page became the journey's `operator_identity`. Nothing below may write.
    """
    conn = _RecordingConnection(
        [("FROM app.setup_journeys", [_JOURNEY_ROW]), ("FROM app.setup_tasks", _TASK_ROWS)]
    )

    result = read_getting_started(conn, project_id="proj_EXAMPLE")

    executed = [sql for sql, _params in conn.statements]
    assert executed, "the read must at least look"
    for sql in executed:
        assert not any(sql.upper().startswith(verb) for verb in _WRITE_VERBS), sql
    # And the derivation still answers, without having persisted it.
    assert result["journey"]["progress"] == {"completed": 5, "total": 5, "percent": 100}


def test_a_read_derives_the_terminal_state_the_journal_has_not_written_yet(_shared_readiness):
    """Display follows evidence; the journal follows an explicit gesture.

    The persisted rows here still say `blocked` and `active` -- exactly the state a
    Project is in between the moment its evidence lands and the next authorized
    gesture. The screen must read the truth anyway, and say that the trail is
    behind rather than pretend it is not.
    """
    conn = _RecordingConnection(
        [("FROM app.setup_journeys", [_JOURNEY_ROW]), ("FROM app.setup_tasks", _TASK_ROWS)]
    )

    result = read_getting_started(conn, project_id="proj_EXAMPLE")

    assert result["journey"]["state"] == "verified"
    assert result["materialization"]["journal_behind"] is True


def test_a_project_without_a_journey_names_the_gesture_that_creates_one(_shared_readiness):
    """An empty list says why, and names the gesture that fills it."""
    conn = _RecordingConnection([("FROM app.setup_journeys", [])])

    result = read_getting_started(conn, project_id="proj_EXAMPLE")

    assert result["journey"]["state"] == "not_started"
    assert result["materialization"]["state"] == "pending"
    assert result["materialization"]["action"] == "start_journey"
    assert result["tasks"] == []
    for sql, _params in conn.statements:
        assert not any(sql.upper().startswith(verb) for verb in _WRITE_VERBS), sql


def test_the_envelope_carries_names_so_no_screen_has_to_print_an_identifier(_shared_readiness):
    """The eyebrow read "Project coordination · proj_01K…" because the payload
    gave the screen nothing else to say."""
    conn = _RecordingConnection(
        [("FROM app.setup_journeys", [_JOURNEY_ROW]), ("FROM app.setup_tasks", _TASK_ROWS)]
    )

    result = read_getting_started(conn, project_id="proj_EXAMPLE")

    assert result["project"]["name"] == "Demo Project"
    assert result["project"]["organization"]["name"] == "Demo Org"


def test_the_completion_transition_is_written_by_the_explicit_reconciliation(monkeypatch):
    """`grep "UPDATE app.setup_journeys" server` returned NOTHING before this."""
    monkeypatch.setattr(
        getting_started, "compose_project_readiness", lambda *_a: _readiness_all_ready()
    )
    monkeypatch.setattr(
        getting_started, "bootstrap_project_journey", lambda *_a, **_k: "setup_1"
    )
    monkeypatch.setattr(getting_started, "read_getting_started", lambda *_a, **_k: {})
    conn = _RecordingConnection(
        [("FROM app.setup_tasks", [(row[0], row[1], row[3]) for row in _TASK_ROWS])]
    )

    reconcile_project_journey(conn, project_id="proj_EXAMPLE", actor="person-1")

    updates = [
        sql
        for sql, _p in conn.statements
        if sql.upper().startswith("UPDATE APP.SETUP_JOURNEYS")
    ]
    assert len(updates) == 1
    assert "state='verified'" in updates[0]
    assert "completed_at=COALESCE(completed_at, NOW())" in updates[0]
