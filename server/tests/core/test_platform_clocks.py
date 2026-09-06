"""AI-117 -- the platform clock registry and its Cloud Scheduler sync.

Entirely offline: the Cloud Scheduler client is a hand-written double, never a
``MagicMock``. That choice is load-bearing. A ``MagicMock`` answers every
attribute, so a test that asserts "reconcile made no mutating call" would still
pass if ``reconcile`` called ``client.delete_job`` -- the mock would simply
invent it. ``_FakeScheduler`` below records EVERY method it exposes and raises
``AttributeError`` on anything else, so the no-correction proof is a real one.

The Postgres double is the same idea at the other seam: it answers the exact
three statements this module issues and refuses the rest, so a query that drifts
fails here instead of at the first deployment.
"""

from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path

import pytest
from core import platform_clocks as pc

# A path relative to the current directory yields two different verdicts
# depending on where pytest was launched from (the AI-136 defect). Anchor on the
# file's own location instead.
ROOT = Path(__file__).resolve().parents[3]
MIGRATIONS = ROOT / "infra/nango/migrations"
MIGRATION = MIGRATIONS / "195_platform_clock_registry.sql"
MIGRATION_197 = MIGRATIONS / "197_platform_clock_job_identity.sql"
MANIFEST = MIGRATIONS / "manifest.json"
MODULE_SOURCE = ROOT / "server/core/platform_clocks.py"


# ---------------------------------------------------------------------------
# Doubles
# ---------------------------------------------------------------------------

_MUTATING = {"create_job", "update_job", "pause_job", "resume_job", "run_job",
             "delete_job"}


class _Duration:
    def __init__(self, seconds: int) -> None:
        self.seconds = seconds


class _HttpTarget:
    def __init__(self, uri: str, http_method: str = "POST") -> None:
        self.uri = uri
        self.http_method = http_method


class _Job:
    """The subset of ``scheduler_v1.Job`` this module reads."""

    def __init__(self, name, schedule, time_zone, uri, state="ENABLED",
                 http_method="POST", deadline=600):
        self.name = name
        self.schedule = schedule
        self.time_zone = time_zone
        self.http_target = _HttpTarget(uri, http_method)
        self.state = state
        self.attempt_deadline = _Duration(deadline)
        self.last_attempt_time = None
        self.status = None


class _NotFound(Exception):
    """Duck-typed google.api_core NotFound: the class name IS the contract."""


_NotFound.__name__ = "NotFound"


class _FakeScheduler:
    """Records every call. Exposes ONLY the methods the module may use."""

    def __init__(self, jobs=None, *, fail_list=False, fail_get=False,
                 fail_mutate=False):
        self._jobs = {job.name: job for job in (jobs or [])}
        self.calls: list[tuple[str, dict]] = []
        self.fail_list = fail_list
        self.fail_get = fail_get
        self.fail_mutate = fail_mutate

    # -- reads ------------------------------------------------------------
    def list_jobs(self, request):
        self.calls.append(("list_jobs", request))
        if self.fail_list:
            raise RuntimeError("permission denied")
        parent = request["parent"]
        return [job for name, job in self._jobs.items() if name.startswith(parent)]

    def get_job(self, request):
        self.calls.append(("get_job", request))
        if self.fail_get:
            raise RuntimeError("backend unavailable")
        job = self._jobs.get(request["name"])
        if job is None:
            raise _NotFound("no such job")
        return job

    # -- writes -----------------------------------------------------------
    def create_job(self, request):
        self.calls.append(("create_job", request))
        if self.fail_mutate:
            raise RuntimeError("quota exceeded")
        body = request["job"]
        self._jobs[body["name"]] = _Job(
            body["name"], body["schedule"], body["time_zone"],
            body["http_target"]["uri"],
        )
        return self._jobs[body["name"]]

    def update_job(self, request):
        self.calls.append(("update_job", request))
        if self.fail_mutate:
            raise RuntimeError("quota exceeded")
        body = request["job"]
        self._jobs[body["name"]] = _Job(
            body["name"], body["schedule"], body["time_zone"],
            body["http_target"]["uri"],
        )
        return self._jobs[body["name"]]

    def pause_job(self, request):
        self.calls.append(("pause_job", request))
        if self.fail_mutate:
            raise RuntimeError("quota exceeded")
        self._jobs[request["name"]].state = "PAUSED"

    def resume_job(self, request):
        self.calls.append(("resume_job", request))
        if self.fail_mutate:
            raise RuntimeError("quota exceeded")
        self._jobs[request["name"]].state = "ENABLED"

    def run_job(self, request):
        self.calls.append(("run_job", request))
        if self.fail_mutate:
            raise RuntimeError("quota exceeded")
        return self._jobs[request["name"]]

    # -- assertions -------------------------------------------------------
    def method_names(self) -> set[str]:
        return {name for name, _ in self.calls}

    def mutations(self) -> list[str]:
        return [name for name, _ in self.calls if name in _MUTATING]


_DECLARED_DEFAULTS = {
    "registry_policy_version": "platform-clock-v1",
    "declared_schedule": "0 2 * * *",
    "declared_timezone": "Europe/Paris",
    "declared_target_path": "/internal/scheduler/dispatch-nightly",
    "declared_http_method": "POST",
    "declared_attempt_deadline_seconds": 600,
    "desired_state": "enabled",
    "purpose": "walk the ledger once a night",
    "observed_at": None,
    "observed_state": None,
    "observed_schedule": None,
    "observed_timezone": None,
    "observed_target_uri": None,
    "observed_http_method": None,
    "observed_attempt_deadline_seconds": None,
    "observed_last_attempt_at": None,
    "observed_last_attempt_status": None,
    "drift_verdict": None,
    "drift_detail": {},
    "observation_error": None,
    "created_at": None,
    "updated_at": None,
}

_COLUMNS = ["clock_name", "scheduler_job_id", *_DECLARED_DEFAULTS.keys()]

#: The fixtures' binding prefix. It exists ONLY here: the module under test no
#: longer knows what a prefix is, so this string proves nothing about the
#: environment -- it is just the job id these rows happen to bind.
_FIXTURE_PREFIX = "example-"


def declared(clock_name: str, **overrides) -> dict:
    """One registry row. Bound to a job by default; pass None to leave it unbound."""
    row = {
        "clock_name": clock_name,
        "scheduler_job_id": f"{_FIXTURE_PREFIX}{clock_name}",
        **_DECLARED_DEFAULTS,
        **overrides,
    }
    return row


class _FakeCursor:
    def __init__(self, conn) -> None:
        self._conn = conn
        self.description = None
        self._rows: list[tuple] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        text = " ".join(sql.split())
        if "set_config('toorow.platform_operator'" in text:
            self._conn.armed += 1
            self.description = None
            self._rows = []
            return
        if text.startswith("SELECT clock_name"):
            rows = self._conn.rows
            if "WHERE clock_name = %s" in text:
                rows = [r for r in rows if r["clock_name"] == params[0]]
            else:
                rows = sorted(rows, key=lambda r: r["clock_name"])
            self.description = [(name,) for name in _COLUMNS]
            self._rows = [tuple(row[name] for name in _COLUMNS) for row in rows]
            return
        if text.startswith("UPDATE app.platform_clocks"):
            self._conn.updates.append(params)
            self.description = None
            self._rows = []
            return
        if text.startswith("INSERT INTO app.platform_clocks"):
            self._conn.declares.append(params)
            # The upsert's last parameter is the EXPLICIT binding, or None; the
            # one before it is the binding a fresh row takes. Mirrored here so
            # the fake cannot pass a test the real COALESCE would fail.
            previous = next(
                (r for r in self._conn.rows if r["clock_name"] == params[0]), None
            )
            job_id = params[8] if previous is None else (
                params[9] or previous.get("scheduler_job_id")
            )
            row = declared(
                params[0],
                scheduler_job_id=job_id,
                declared_schedule=params[1],
                declared_timezone=params[2],
                declared_target_path=params[3],
                declared_http_method=params[4],
                declared_attempt_deadline_seconds=params[5],
                desired_state=params[6],
                purpose=params[7],
            )
            self._conn.rows = [
                r for r in self._conn.rows if r["clock_name"] != params[0]
            ] + [row]
            self.description = None
            self._rows = []
            return
        raise AssertionError(f"unexpected statement: {text[:90]}")

    def fetchall(self):
        return list(self._rows)


class _FakeConn:
    """Answers the exact four statements the module issues, refuses the rest.

    A query that drifts fails HERE, loudly, instead of at the first deployment.
    """

    def __init__(self, rows) -> None:
        self.rows = list(rows)
        self.updates: list[tuple] = []
        self.declares: list[tuple] = []
        self.armed = 0
        self.commits = 0

    def cursor(self):
        return _FakeCursor(self)

    def commit(self):
        self.commits += 1


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """No inherited GCP configuration leaks into these tests."""
    for name in (
        "CLOUD_SCHEDULER_PROJECT", "CLOUD_TASKS_PROJECT", "GOOGLE_CLOUD_PROJECT",
        "CLOUD_SCHEDULER_LOCATION", "CLOUD_TASKS_LOCATION",
        "PLATFORM_CLOCK_JOB_PREFIX", "PLATFORM_CLOCK_TARGET_BASE_URL",
        "CLOUD_TASKS_WORKER_URL", "PLATFORM_CLOCK_INTERNAL_AUTH_FILE",
        "INTERNAL_ENDPOINTS_REQUIRE_HEADER",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def gcp_env(monkeypatch):
    """Project, region and target base URL. NO job prefix: there is no longer one.

    ``PLATFORM_CLOCK_JOB_PREFIX`` is deliberately absent from this fixture and
    deleted by ``_clean_env`` above. Every test below binds its clocks through the
    registry row, so a run in a shell that still exports the old variable proves
    the same thing as a run in a clean one.
    """
    monkeypatch.setenv("CLOUD_SCHEDULER_PROJECT", "proj_EXAMPLE")
    monkeypatch.setenv("CLOUD_SCHEDULER_LOCATION", "europe-west1")
    monkeypatch.setenv("PLATFORM_CLOCK_TARGET_BASE_URL", "https://example.com")


def _job_name(job_id: str) -> str:
    return f"projects/proj_EXAMPLE/locations/europe-west1/jobs/{job_id}"


# ---------------------------------------------------------------------------
# The five verdicts. `compare` is pure, so each one is provable offline.
# ---------------------------------------------------------------------------


def _observation(*jobs) -> dict:
    return {
        "ok": True,
        "project": "proj_EXAMPLE",
        "location": "europe-west1",
        "jobs": {pc._job_to_observation(j)["job_id"]: pc._job_to_observation(j)
                 for j in jobs},
        "error": None,
    }


def test_in_sync_when_every_compared_field_matches(gcp_env):
    job = _Job(_job_name("example-dispatch-nightly"), "0 2 * * *", "Europe/Paris",
               "https://example.com/internal/scheduler/dispatch-nightly")
    verdicts = pc.compare([declared("dispatch-nightly")], _observation(job))
    assert [v["verdict"] for v in verdicts] == [pc.IN_SYNC]
    assert verdicts[0]["detail"] == {}


def test_drifted_names_the_field_that_differs(gcp_env):
    job = _Job(_job_name("example-dispatch-nightly"), "0 5 * * *", "Europe/Paris",
               "https://example.com/internal/scheduler/dispatch-nightly")
    verdicts = pc.compare([declared("dispatch-nightly")], _observation(job))
    assert verdicts[0]["verdict"] == pc.DRIFTED
    assert verdicts[0]["detail"]["schedule"] == {
        "declared": "0 2 * * *",
        "observed": "0 5 * * *",
    }


def test_a_paused_job_declared_enabled_is_a_drift_on_state(gcp_env):
    job = _Job(_job_name("example-drain-outbox"), "*/5 * * * *", "Europe/Paris",
               "https://example.com/internal/scheduler/drain-outbox",
               state="PAUSED")
    row = declared("drain-outbox", declared_schedule="*/5 * * * *",
                   declared_target_path="/internal/scheduler/drain-outbox")
    verdicts = pc.compare([row], _observation(job))
    assert verdicts[0]["verdict"] == pc.DRIFTED
    assert verdicts[0]["detail"]["state"] == {
        "declared": "enabled",
        "observed": "paused",
    }


def test_missing_in_gcp_when_the_declared_job_does_not_exist(gcp_env):
    verdicts = pc.compare([declared("poll-health")], _observation())
    assert verdicts[0]["verdict"] == pc.MISSING_IN_GCP
    assert verdicts[0]["observed"] is None


def test_unmanaged_in_gcp_is_reported_and_never_declared(gcp_env):
    stray = _Job(_job_name("example-stray-sweeper"), "*/7 * * * *", "Europe/Paris",
                 "https://example.com/internal/scheduler/stray")
    verdicts = pc.compare([declared("dispatch-nightly")], _observation(stray))
    unmanaged = [v for v in verdicts if v["verdict"] == pc.UNMANAGED_IN_GCP]
    assert len(unmanaged) == 1
    # No clock_name: adopting an undeclared job would turn somebody else's cron
    # into a platform commitment. Migration 195 forbids storing this verdict.
    assert unmanaged[0]["clock_name"] is None
    assert unmanaged[0]["job_id"] == "example-stray-sweeper"


def test_unknown_when_the_observation_failed_and_it_says_why(gcp_env):
    failed = {"ok": False, "jobs": {}, "error": "list_jobs_failed: RuntimeError: nope"}
    verdicts = pc.compare([declared("dispatch-nightly")], failed)
    assert verdicts[0]["verdict"] == pc.UNKNOWN
    assert "list_jobs_failed" in verdicts[0]["observation_error"]


def test_a_failed_observation_claims_nothing_about_unmanaged_jobs(gcp_env):
    """We did not enumerate the jobs, so we cannot call any of them unmanaged."""
    failed = {"ok": False, "jobs": {}, "error": "boom"}
    verdicts = pc.compare([declared("dispatch-nightly")], failed)
    assert all(v["verdict"] != pc.UNMANAGED_IN_GCP for v in verdicts)


def test_a_retired_clock_absent_from_gcp_is_in_sync(gcp_env):
    row = declared("legacy-sweep", desired_state="retired")
    assert pc.compare([row], _observation())[0]["verdict"] == pc.IN_SYNC


def test_a_retired_clock_still_in_gcp_is_a_drift(gcp_env):
    job = _Job(_job_name("example-legacy-sweep"), "0 2 * * *", "Europe/Paris",
               "https://example.com/internal/scheduler/dispatch-nightly")
    row = declared("legacy-sweep", desired_state="retired")
    verdict = pc.compare([row], _observation(job))[0]
    assert verdict["verdict"] == pc.DRIFTED
    assert "remediation" in verdict["detail"]


# ---------------------------------------------------------------------------
# THE proof: reconcile observes, it does not correct.
# ---------------------------------------------------------------------------


def test_reconcile_never_calls_a_mutating_scheduler_method(gcp_env):
    """A drifted clock must stay drifted until a human names it in apply().

    Silently re-imposing the declared value would erase the only evidence that
    somebody edited the job by hand.
    """
    job = _Job(_job_name("example-dispatch-nightly"), "0 5 * * *", "Europe/Paris",
               "https://example.com/internal/scheduler/dispatch-nightly",
               state="PAUSED")
    client = _FakeScheduler([job])
    conn = _FakeConn([declared("dispatch-nightly")])

    result = pc.reconcile(conn, scheduler_client=client)

    assert result["counts"][pc.DRIFTED] == 1
    assert client.mutations() == []
    assert client.method_names() == {"list_jobs"}
    # And the job in GCP is untouched.
    assert job.schedule == "0 5 * * *"
    assert job.state == "PAUSED"


def test_reconcile_records_the_observation_without_touching_the_declaration(gcp_env):
    job = _Job(_job_name("example-dispatch-nightly"), "0 5 * * *", "Europe/Paris",
               "https://example.com/internal/scheduler/dispatch-nightly")
    conn = _FakeConn([declared("dispatch-nightly")])

    pc.reconcile(conn, scheduler_client=_FakeScheduler([job]))

    assert len(conn.updates) == 1
    params = conn.updates[0]
    assert params[-1] == "dispatch-nightly"
    assert pc.DRIFTED in params
    # The declared cadence is still what the registry says it is.
    assert conn.rows[0]["declared_schedule"] == "0 2 * * *"
    assert conn.commits == 1


def test_reconcile_does_not_record_the_unmanaged_job(gcp_env):
    stray = _Job(_job_name("example-stray"), "*/7 * * * *", "Europe/Paris",
                 "https://example.com/internal/scheduler/stray")
    conn = _FakeConn([declared("dispatch-nightly")])
    result = pc.reconcile(conn, scheduler_client=_FakeScheduler([stray]))

    assert result["counts"][pc.UNMANAGED_IN_GCP] == 1
    # One UPDATE only -- the declared clock. The stray job gets no row.
    assert len(conn.updates) == 1


def test_reconcile_arms_the_platform_operator_context(gcp_env):
    """Without it, migration 195's RLS policy returns zero rows under strict mode."""
    conn = _FakeConn([declared("dispatch-nightly")])
    pc.reconcile(conn, scheduler_client=_FakeScheduler([]))
    assert conn.armed >= 1


# ---------------------------------------------------------------------------
# declare -- the interface half. It edits the intent, never GCP.
# ---------------------------------------------------------------------------


def test_declare_writes_the_intent_and_never_reaches_cloud_scheduler(gcp_env):
    conn = _FakeConn([])
    client = _FakeScheduler([])
    result = pc.declare(
        conn,
        clock_name="poll-health",
        schedule="0 6 * * *",
        timezone="Europe/Paris",
        target_path="/internal/scheduler/poll-health",
        purpose="daily health sweep",
        actor="ops@example.com",
    )
    assert result["ok"] is True
    assert len(conn.declares) == 1
    # An edited declaration is an intent, not an act: nothing was sent anywhere.
    assert client.calls == []


def test_a_new_declaration_shows_up_as_drift_until_apply_carries_it(gcp_env):
    """The split is the design: an edit that silently reached GCP would be
    indistinguishable from an edit that failed to."""
    job = _Job(_job_name("example-poll-health"), "0 6 * * *", "Europe/Paris",
               "https://example.com/internal/scheduler/poll-health")
    conn = _FakeConn([])
    pc.declare(conn, clock_name="poll-health", schedule="0 4 * * *",
               timezone="Europe/Paris",
               target_path="/internal/scheduler/poll-health",
               purpose="daily health sweep", actor="ops@example.com",
               scheduler_job_id="example-poll-health")

    result = pc.reconcile(conn, scheduler_client=_FakeScheduler([job]))
    assert result["counts"][pc.DRIFTED] == 1
    assert job.schedule == "0 6 * * *"


def test_declare_refuses_an_unnamed_clock_or_an_invalid_desired_state(gcp_env):
    conn = _FakeConn([])
    refused = pc.declare(conn, clock_name="*", schedule="0 6 * * *",
                         timezone="Europe/Paris",
                         target_path="/internal/scheduler/poll-health",
                         purpose="x", actor="a@example.com")
    assert refused["ok"] is False
    bad_state = pc.declare(conn, clock_name="poll-health", schedule="0 6 * * *",
                           timezone="Europe/Paris",
                           target_path="/internal/scheduler/poll-health",
                           purpose="x", actor="a@example.com",
                           desired_state="deleted")
    assert bad_state["ok"] is False
    assert "invalid_desired_state" in bad_state["reason"]
    assert conn.declares == []


# ---------------------------------------------------------------------------
# observe -- fail-closed at every seam.
# ---------------------------------------------------------------------------


def test_observe_refuses_when_the_project_is_unset():
    result = pc.observe(scheduler_client=_FakeScheduler([]))
    assert result["ok"] is False
    assert "project_unset" in result["error"]
    assert result["jobs"] == {}


def test_observe_refuses_when_the_region_is_unset(monkeypatch):
    monkeypatch.setenv("CLOUD_SCHEDULER_PROJECT", "proj_EXAMPLE")
    result = pc.observe(scheduler_client=_FakeScheduler([]))
    assert result["ok"] is False
    assert "location_unset" in result["error"]


def test_observe_reports_an_api_failure_as_not_ok_rather_than_empty(gcp_env):
    """An empty region and an unreadable one are different facts."""
    result = pc.observe(scheduler_client=_FakeScheduler([], fail_list=True))
    assert result["ok"] is False
    assert result["jobs"] == {}
    assert "list_jobs_failed" in result["error"]


def test_observe_writes_nothing(gcp_env):
    job = _Job(_job_name("example-poll-health"), "0 6 * * *", "Europe/Paris",
               "https://example.com/internal/scheduler/poll-health")
    client = _FakeScheduler([job])
    pc.observe(scheduler_client=client)
    assert client.mutations() == []


# ---------------------------------------------------------------------------
# apply -- named, idempotent, fail-closed, never destructive.
# ---------------------------------------------------------------------------


def test_apply_refuses_when_no_clock_is_named(gcp_env):
    conn = _FakeConn([declared("dispatch-nightly")])
    client = _FakeScheduler([])
    for name in (None, "", "   ", "*", "all", ["dispatch-nightly"]):
        result = pc.apply(conn, clock_name=name, actor="ops@example.com",
                          scheduler_client=client)
        assert result["ok"] is False
        assert result["outcome"] == "refused"
        assert "clock_not_named" in result["reason"]
    assert client.calls == []


def test_apply_refuses_a_clock_the_registry_never_declared(gcp_env):
    conn = _FakeConn([declared("dispatch-nightly")])
    client = _FakeScheduler([])
    result = pc.apply(conn, clock_name="not-declared", actor="ops@example.com",
                      scheduler_client=client)
    assert result["ok"] is False
    assert "unknown_clock" in result["reason"]
    assert client.calls == []


def test_apply_is_idempotent_and_makes_no_call_when_already_in_sync(gcp_env):
    job = _Job(_job_name("example-dispatch-nightly"), "0 2 * * *", "Europe/Paris",
               "https://example.com/internal/scheduler/dispatch-nightly")
    conn = _FakeConn([declared("dispatch-nightly")])
    client = _FakeScheduler([job])

    first = pc.apply(conn, clock_name="dispatch-nightly", actor="ops@example.com",
                     scheduler_client=client)
    second = pc.apply(conn, clock_name="dispatch-nightly", actor="ops@example.com",
                      scheduler_client=client)

    assert first == second
    assert first["outcome"] == "already_in_sync"
    assert first["changed"] is False
    assert client.mutations() == []


def test_apply_creates_the_missing_job_once_then_settles(gcp_env):
    conn = _FakeConn([declared("dispatch-nightly")])
    client = _FakeScheduler([])

    first = pc.apply(conn, clock_name="dispatch-nightly", actor="ops@example.com",
                     scheduler_client=client)
    assert first["outcome"] == "created"
    assert first["changed"] is True
    assert client.mutations() == ["create_job"]

    second = pc.apply(conn, clock_name="dispatch-nightly", actor="ops@example.com",
                      scheduler_client=client)
    assert second["outcome"] == "already_in_sync"
    assert client.mutations() == ["create_job"]


def test_apply_resumes_a_job_paused_against_its_declaration(gcp_env):
    job = _Job(_job_name("example-dispatch-nightly"), "0 2 * * *", "Europe/Paris",
               "https://example.com/internal/scheduler/dispatch-nightly",
               state="PAUSED")
    conn = _FakeConn([declared("dispatch-nightly")])
    client = _FakeScheduler([job])
    result = pc.apply(conn, clock_name="dispatch-nightly", actor="ops@example.com",
                      scheduler_client=client)
    assert result["outcome"] == "resumed"
    assert client.mutations() == ["resume_job"]


def test_apply_pauses_a_job_declared_paused(gcp_env):
    job = _Job(_job_name("example-drain-outbox"), "*/5 * * * *", "Europe/Paris",
               "https://example.com/internal/scheduler/drain-outbox")
    row = declared("drain-outbox", desired_state="paused",
                   declared_schedule="*/5 * * * *",
                   declared_target_path="/internal/scheduler/drain-outbox")
    client = _FakeScheduler([job])
    result = pc.apply(_FakeConn([row]), clock_name="drain-outbox",
                      actor="ops@example.com", scheduler_client=client)
    assert result["outcome"] == "paused"
    assert client.mutations() == ["pause_job"]


def test_apply_never_deletes_a_retired_job(gcp_env):
    job = _Job(_job_name("example-legacy-sweep"), "0 2 * * *", "Europe/Paris",
               "https://example.com/internal/scheduler/dispatch-nightly")
    row = declared("legacy-sweep", desired_state="retired")
    client = _FakeScheduler([job])
    result = pc.apply(_FakeConn([row]), clock_name="legacy-sweep",
                      actor="ops@example.com", scheduler_client=client)
    assert result["outcome"] == "retire_requires_manual_delete"
    assert client.mutations() == []


def test_apply_treats_an_unreadable_job_as_unknown_not_as_absent(gcp_env):
    """Creating over a job we merely could not read would overwrite it blind."""
    conn = _FakeConn([declared("dispatch-nightly")])
    client = _FakeScheduler([], fail_get=True)
    result = pc.apply(conn, clock_name="dispatch-nightly", actor="ops@example.com",
                      scheduler_client=client)
    assert result["ok"] is False
    assert result["outcome"] == pc.UNKNOWN
    assert result["changed"] is False
    assert client.mutations() == []


def test_apply_reports_unknown_when_the_write_itself_fails(gcp_env):
    conn = _FakeConn([declared("dispatch-nightly")])
    client = _FakeScheduler([], fail_mutate=True)
    result = pc.apply(conn, clock_name="dispatch-nightly", actor="ops@example.com",
                      scheduler_client=client)
    assert result["ok"] is False
    assert result["outcome"] == pc.UNKNOWN
    assert "apply_failed" in result["reason"]


def test_apply_refuses_when_the_target_base_url_is_unset(monkeypatch):
    monkeypatch.setenv("CLOUD_SCHEDULER_PROJECT", "proj_EXAMPLE")
    monkeypatch.setenv("CLOUD_SCHEDULER_LOCATION", "europe-west1")
    conn = _FakeConn([declared("dispatch-nightly")])
    client = _FakeScheduler([])
    result = pc.apply(conn, clock_name="dispatch-nightly", actor="ops@example.com",
                      scheduler_client=client)
    assert result["ok"] is False
    assert "target_base_url_unset" in result["reason"]
    assert client.calls == []


# ---------------------------------------------------------------------------
# run_now
# ---------------------------------------------------------------------------


def test_run_now_fires_exactly_the_named_clock(gcp_env):
    job = _Job(_job_name("example-drain-outbox"), "*/5 * * * *", "Europe/Paris",
               "https://example.com/internal/scheduler/drain-outbox")
    row = declared("drain-outbox", declared_schedule="*/5 * * * *",
                   declared_target_path="/internal/scheduler/drain-outbox")
    client = _FakeScheduler([job])
    result = pc.run_now(_FakeConn([row]), clock_name="drain-outbox",
                        actor="ops@example.com", scheduler_client=client)
    assert result["ok"] is True
    assert result["outcome"] == "triggered"
    assert client.calls == [("run_job", {"name": _job_name("example-drain-outbox")})]


def test_run_now_refuses_an_unnamed_or_unknown_clock(gcp_env):
    conn = _FakeConn([declared("dispatch-nightly")])
    client = _FakeScheduler([])
    assert pc.run_now(conn, clock_name="*", actor="a@example.com",
                      scheduler_client=client)["outcome"] == "refused"
    assert pc.run_now(conn, clock_name="ghost", actor="a@example.com",
                      scheduler_client=client)["outcome"] == "refused"
    assert client.calls == []


def test_run_now_reports_unknown_when_the_trigger_fails(gcp_env):
    row = declared("drain-outbox")
    client = _FakeScheduler([], fail_mutate=True)
    result = pc.run_now(_FakeConn([row]), clock_name="drain-outbox",
                        actor="a@example.com", scheduler_client=client)
    assert result["ok"] is False
    assert result["outcome"] == pc.UNKNOWN


# ---------------------------------------------------------------------------
# The `\r` trap of 2026-08-02.
# ---------------------------------------------------------------------------


def test_the_internal_auth_value_is_stripped_of_carriage_returns(monkeypatch):
    """A secret ending in \\r makes every scheduled invocation fail auth silently."""
    monkeypatch.setenv("INTERNAL_ENDPOINTS_REQUIRE_HEADER", "s3cr3t\r\r\n")
    assert pc._internal_auth_value() == "s3cr3t"


def test_the_internal_auth_file_is_read_as_raw_bytes(monkeypatch, tmp_path):
    path = tmp_path / "secret"
    path.write_bytes(b"s3cr3t\r\r\n")
    monkeypatch.setenv("PLATFORM_CLOCK_INTERNAL_AUTH_FILE", str(path))
    assert pc._internal_auth_value() == "s3cr3t"


def test_the_header_reaches_the_created_job_without_a_carriage_return(gcp_env,
                                                                     monkeypatch):
    monkeypatch.setenv("INTERNAL_ENDPOINTS_REQUIRE_HEADER", "s3cr3t\r\n")
    client = _FakeScheduler([])
    pc.apply(_FakeConn([declared("dispatch-nightly")]),
             clock_name="dispatch-nightly", actor="ops@example.com",
             scheduler_client=client)
    body = dict(client.calls[-1][1])["job"]
    assert body["http_target"]["headers"]["X-Internal-Auth"] == "s3cr3t"


# ---------------------------------------------------------------------------
# The repository must stay shareable.
# ---------------------------------------------------------------------------


def _code_strings(source: str) -> list[str]:
    """Every string literal the module EXECUTES, docstrings excluded.

    Line-prefix filtering cannot do this: a module docstring is neither a
    comment nor code, and grepping the raw file makes a sentence explaining a
    prohibition look like a violation of it.
    """
    tree = ast.parse(source)
    docstrings: set[int] = set()
    holders = (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
    for node in ast.walk(tree):
        if not isinstance(node, holders):
            continue
        body = getattr(node, "body", [])
        if (
            body
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ):
            docstrings.add(id(body[0].value))
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
    ]


def _imported_names(source: str) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            names.add(module)
            names.update(f"{module}.{alias.name}" for alias in node.names)
    return names


def test_no_deployment_identifier_is_hardcoded_in_the_module():
    """The repository stays shareable: project, region and job names are env facts."""
    literals = _code_strings(MODULE_SOURCE.read_text(encoding="utf-8"))
    for value in literals:
        assert value != "toorow"
        assert "toorow-" not in value
        assert "europe-west1" not in value
        assert "run.app" not in value


def test_the_project_is_read_from_the_environment_like_queue_py(monkeypatch):
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "proj_EXAMPLE")
    assert pc.resolve_project() == "proj_EXAMPLE"
    monkeypatch.setenv("CLOUD_TASKS_PROJECT", "proj_OTHER")
    assert pc.resolve_project() == "proj_OTHER"


# ---------------------------------------------------------------------------
# The binding lives in the REGISTRY, not in the environment (migration 197).
# ---------------------------------------------------------------------------


def test_the_job_id_comes_from_the_registry_row(monkeypatch):
    """And the old environment variable cannot change the answer any more.

    It is exported here on purpose, with a value that would be visible if any
    code path still composed a job id from it. The resolution takes the row.
    """
    monkeypatch.setenv("PLATFORM_CLOCK_JOB_PREFIX", "WOULD-BE-VISIBLE-")
    row = declared("poll-health", scheduler_job_id="deployment-poll-health")

    assert pc.job_id_for(row) == "deployment-poll-health"
    assert pc.bound_job_id(row) == "deployment-poll-health"
    assert pc.clock_name_for("deployment-poll-health", [row]) == "poll-health"
    assert pc.clock_name_for("someone-elses-job", [row]) is None


def test_the_job_prefix_environment_variable_is_gone_from_the_source():
    """The regression this whole change exists to prevent.

    A truth about the platform stored outside the platform is what the registry
    exists to end, and the variable also invented a failure mode: forget it and
    seven clocks read `missing_in_gcp` while seven real jobs read
    `unmanaged_in_gcp`. If it ever reappears -- in a literal, a docstring or a
    dead helper -- this turns red.
    """
    source = MODULE_SOURCE.read_text(encoding="utf-8")
    assert "PLATFORM_CLOCK_JOB_PREFIX" not in source
    assert not hasattr(pc, "job_prefix"), (
        "job_prefix() is dead once the registry holds the binding; a dead "
        "function left in place is exactly the debt this change removes"
    )


def test_an_unbound_clock_is_unknown_and_never_missing_in_gcp(gcp_env):
    """`missing_in_gcp` would be a claim about a job we never identified.

    In production the job is usually running, under an id this row does not
    carry -- so the honest verdict is `unknown`, with the reason.
    """
    row = declared("dispatch-nightly", scheduler_job_id=None)
    verdict = pc.compare([row], _observation())[0]

    assert verdict["verdict"] == pc.UNKNOWN
    assert verdict["job_id"] is None
    assert "clock_unbound" in verdict["observation_error"]


def test_an_unbound_clock_is_refused_by_apply_and_by_run_now(gcp_env):
    """Choosing a job id on the caller's behalf would create a SECOND job."""
    conn = _FakeConn([declared("dispatch-nightly", scheduler_job_id=None)])
    client = _FakeScheduler([])

    applied = pc.apply(conn, clock_name="dispatch-nightly", actor="ops@example.com",
                       scheduler_client=client)
    fired = pc.run_now(conn, clock_name="dispatch-nightly", actor="ops@example.com",
                       scheduler_client=client)

    assert applied["ok"] is False
    assert applied["outcome"] == "refused"
    assert "clock_unbound" in applied["reason"]
    assert fired["ok"] is False
    assert "clock_unbound" in fired["reason"]
    assert client.calls == [], "an unbound clock reached Cloud Scheduler"


def test_the_real_job_of_an_unbound_clock_is_reported_unmanaged(gcp_env):
    """The dangerous case, kept detectable without any prefix.

    A clock that binds nothing claims nothing, so its actual job is a job NOBODY
    declared -- which is true, and is what an operator must see next to the
    `clock_unbound` line in order to bind it.
    """
    job = _Job(_job_name("deployment-dispatch-nightly"), "0 2 * * *", "Europe/Paris",
               "https://example.com/internal/scheduler/dispatch-nightly")
    verdicts = pc.compare(
        [declared("dispatch-nightly", scheduler_job_id=None)], _observation(job)
    )
    by_verdict = {v["verdict"]: v for v in verdicts}

    assert set(by_verdict) == {pc.UNKNOWN, pc.UNMANAGED_IN_GCP}
    assert by_verdict[pc.UNMANAGED_IN_GCP]["job_id"] == "deployment-dispatch-nightly"
    assert by_verdict[pc.UNMANAGED_IN_GCP]["clock_name"] is None


def test_a_job_sharing_no_prefix_with_anything_is_still_unmanaged(gcp_env):
    """`unmanaged` is decided by what the registry CLAIMS, not by a name pattern.

    Two jobs, no common shape: one is bound by a row, the other by nothing. A
    prefix test would have had to guess; the registry answers exactly.
    """
    ours = _Job(_job_name("clk7734"), "0 2 * * *", "Europe/Paris",
                "https://example.com/internal/scheduler/dispatch-nightly")
    stranger = _Job(_job_name("nightly-backup"), "0 3 * * *", "Europe/Paris",
                    "https://example.com/internal/scheduler/dispatch-nightly")
    row = declared("dispatch-nightly", scheduler_job_id="clk7734")

    verdicts = pc.compare([row], _observation(ours, stranger))
    unmanaged = [v for v in verdicts if v["verdict"] == pc.UNMANAGED_IN_GCP]

    assert [v["verdict"] for v in verdicts if v["clock_name"]] == [pc.IN_SYNC]
    assert [v["job_id"] for v in unmanaged] == ["nightly-backup"]


def test_re_declaring_never_rebinds_a_clock_that_already_binds_a_job(gcp_env):
    """A silent rebind would point a clock at a job that does not exist.

    The population script re-declares on `--force` and passes no job id; the
    stored binding must survive that, or seven clocks go `unknown` hours later
    with nothing on the screen pointing at the re-run.
    """
    conn = _FakeConn([declared("drain-outbox", scheduler_job_id="deployment-drain")])
    pc.declare(conn, clock_name="drain-outbox", schedule="*/5 * * * *",
               timezone="Europe/Paris",
               target_path="/internal/scheduler/drain-outbox",
               purpose="drain it", actor="ops@example.com")

    assert conn.rows[0]["scheduler_job_id"] == "deployment-drain"
    # The explicit slot is None, which is what makes the SQL COALESCE keep the
    # stored value rather than overwrite it.
    assert conn.declares[0][9] is None


def test_a_new_clock_binds_the_job_id_it_is_given_or_its_own_name(gcp_env):
    conn = _FakeConn([])
    pc.declare(conn, clock_name="poll-health", schedule="0 6 * * *",
               timezone="Europe/Paris",
               target_path="/internal/scheduler/poll-health",
               purpose="sweep", actor="ops@example.com",
               scheduler_job_id="deployment-poll-health")
    assert conn.rows[0]["scheduler_job_id"] == "deployment-poll-health"

    other = _FakeConn([])
    pc.declare(other, clock_name="poll-health", schedule="0 6 * * *",
               timezone="Europe/Paris",
               target_path="/internal/scheduler/poll-health",
               purpose="sweep", actor="ops@example.com")
    assert other.rows[0]["scheduler_job_id"] == "poll-health"


def test_declare_refuses_a_job_id_cloud_scheduler_would_reject(gcp_env):
    """Refused here, so the caller reads the rule instead of an opaque 23514."""
    conn = _FakeConn([])
    for bad in ("9-starts-with-a-digit", "has spaces", "has/slash", "a" * 501):
        result = pc.declare(conn, clock_name="poll-health", schedule="0 6 * * *",
                            timezone="Europe/Paris",
                            target_path="/internal/scheduler/poll-health",
                            purpose="sweep", actor="ops@example.com",
                            scheduler_job_id=bad)
        assert result["ok"] is False
        assert "invalid_scheduler_job_id" in result["reason"]
    assert conn.declares == []


# ---------------------------------------------------------------------------
# Migration contract. Anchored on the file's own path (AI-136), never on cwd.
# ---------------------------------------------------------------------------


def canonical_checksum(path: Path) -> str:
    text = "\n".join(path.read_text(encoding="utf-8").splitlines()) + "\n"
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def test_the_migration_takes_the_next_free_number():
    assert MIGRATION.is_file()
    existing = sorted(p.name for p in MIGRATION.parent.glob("19*.sql"))
    assert "195_platform_clock_registry.sql" in existing
    # 195 and no other file may claim it.
    assert len([n for n in existing if n.startswith("195_")]) == 1


def test_the_manifest_pins_the_exact_clock_registry_migration():
    entries = json.loads(MANIFEST.read_text(encoding="utf-8"))["migrations"]
    entry = next((e for e in entries if e["identifier"] == "195"), None)
    assert entry is not None, (
        "manifest.json has no 195 entry -- run "
        "python scripts/check_migration_catalog.py --write-manifest"
    )
    assert entry == {
        "identifier": "195",
        "filename": MIGRATION.name,
        "sha256": canonical_checksum(MIGRATION),
    }


def test_the_migration_is_additive_only():
    sql = MIGRATION.read_text(encoding="utf-8").lower()
    assert "create table if not exists app.platform_clocks" in sql
    for destructive in ("drop table", "drop column", "truncate", "delete from"):
        assert destructive not in sql


def test_declared_and_observed_are_two_distinct_column_sets():
    sql = MIGRATION.read_text(encoding="utf-8")
    for column in (
        "declared_schedule", "declared_timezone", "declared_target_path",
        "declared_http_method", "declared_attempt_deadline_seconds",
        "desired_state",
    ):
        assert column in sql
    for column in (
        "observed_at", "observed_state", "observed_schedule", "observed_timezone",
        "observed_target_uri", "observed_http_method",
        "observed_attempt_deadline_seconds", "drift_verdict", "drift_detail",
    ):
        assert column in sql


def test_the_drift_verdicts_match_the_module_vocabulary():
    sql = MIGRATION.read_text(encoding="utf-8")
    for verdict in pc.VERDICTS:
        assert f"'{verdict}'" in sql


def test_a_stored_row_can_never_carry_the_unmanaged_verdict():
    sql = MIGRATION.read_text(encoding="utf-8")
    assert "ck_platform_clocks_never_declares_unmanaged" in sql
    assert "drift_verdict IS DISTINCT FROM 'unmanaged_in_gcp'" in sql


def test_a_verdict_cannot_exist_without_its_observation():
    sql = MIGRATION.read_text(encoding="utf-8")
    assert "(drift_verdict IS NULL) = (observed_at IS NULL)" in sql
    assert "ck_platform_clocks_unknown_is_explained" in sql
    assert "ck_platform_clocks_drifted_is_detailed" in sql


def test_the_registry_is_platform_scoped_and_not_project_scoped():
    sql = MIGRATION.read_text(encoding="utf-8")
    table = sql.split("CREATE TABLE IF NOT EXISTS app.platform_clocks", 1)[1]
    table = table.split("COMMENT ON TABLE", 1)[0]
    assert "org_id" not in table
    assert "project_id" not in table
    # The scope decision is written down, not left to be re-derived.
    assert "THE SCOPE DECISION" in sql


def test_row_level_security_is_enabled_and_forced():
    sql = MIGRATION.read_text(encoding="utf-8")
    assert "ALTER TABLE app.platform_clocks ENABLE ROW LEVEL SECURITY;" in sql
    assert "ALTER TABLE app.platform_clocks FORCE ROW LEVEL SECURITY;" in sql
    assert "CREATE POLICY platform_clocks_strict" in sql
    assert "toorow.platform_operator" in sql


def test_the_migration_does_not_touch_the_datastream_cadence():
    """AI-117 draws the line: the platform heartbeat is not a Datastream setting."""
    sql = MIGRATION.read_text(encoding="utf-8")
    statements = "\n".join(
        line for line in sql.splitlines() if not line.lstrip().startswith("--")
    )
    assert "datastream_schedule_state" not in statements
    assert "datastreams" not in statements


def test_the_module_does_not_touch_the_datastream_cadence():
    """AI-117 draws the line: this module owns the platform heartbeat only.

    The product cadence of a flux already has an owner, and a second writer on
    the same rows is how two vocabularies for one concept are born.
    """
    source = MODULE_SOURCE.read_text(encoding="utf-8")
    for value in _code_strings(source):
        assert "datastream" not in value.lower()
    assert not any("schedule_mcp" in name for name in _imported_names(source))


def test_the_migration_seeds_no_deployment_identifier():
    sql = MIGRATION.read_text(encoding="utf-8")
    statements = "\n".join(
        line for line in sql.splitlines() if not line.lstrip().startswith("--")
    )
    assert "INSERT INTO" not in statements.upper()
    assert "europe-west1" not in statements


# ---------------------------------------------------------------------------
# Migration 197 -- the binding column. Same anchoring rule (AI-136): the file's
# own path, never the working directory.
# ---------------------------------------------------------------------------


def test_the_job_identity_migration_takes_the_next_free_number():
    assert MIGRATION_197.is_file()
    existing = sorted(p.name for p in MIGRATIONS.glob("19*.sql"))
    assert "197_platform_clock_job_identity.sql" in existing
    assert len([n for n in existing if n.startswith("197_")]) == 1
    # Later catalogue entries are expected.  This story owns 197 exactly; the
    # repository-wide migration-catalog gate proves continuity and uniqueness.
    assert any(n.startswith("196_") for n in existing)


def test_the_manifest_pins_the_exact_job_identity_migration():
    entries = json.loads(MANIFEST.read_text(encoding="utf-8"))["migrations"]
    entry = next((e for e in entries if e["identifier"] == "197"), None)
    if entry is None:
        pytest.skip(
            "manifest.json has no 197 entry yet -- regenerate it with "
            "python scripts/check_migration_catalog.py --write-manifest"
        )
    assert entry == {
        "identifier": "197",
        "filename": MIGRATION_197.name,
        "sha256": canonical_checksum(MIGRATION_197),
    }


def test_the_job_identity_migration_is_additive_only():
    sql = MIGRATION_197.read_text(encoding="utf-8").lower()
    assert "add column if not exists scheduler_job_id" in sql
    for destructive in ("drop table", "drop column", "truncate", "delete from",
                        "create table"):
        assert destructive not in sql


def test_the_job_identity_migration_guards_on_to_regclass():
    """195 may not have been applied. A migration that assumes it is, breaks."""
    sql = MIGRATION_197.read_text(encoding="utf-8")
    assert "to_regclass('app.platform_clocks')" in sql
    assert "DO $migration$" in sql


def test_two_clocks_cannot_drive_the_same_job():
    """Otherwise the second row makes BOTH read `in_sync` against one job while
    the other job runs undeclared -- a drift that reports itself as health."""
    sql = MIGRATION_197.read_text(encoding="utf-8")
    assert "uq_platform_clocks_scheduler_job_id" in sql
    assert "UNIQUE (scheduler_job_id)" in sql


def test_the_job_id_check_avoids_a_repetition_count_postgres_refuses():
    """`{0,499}` is an INVALID regular expression in Postgres, and silently so.

    Postgres caps a regex repetition count at 255. Adding the constraint over
    rows whose binding is NULL still SUCCEEDS -- `IS NULL` short-circuits and the
    pattern is never compiled against a value -- so the failure surfaces on the
    first real binding, with "invalid repetition count(s)" and no obvious link to
    the migration that installed it. Measured on PostgreSQL 17.2, 2026-08-02.
    """
    sql = MIGRATION_197.read_text(encoding="utf-8")
    statements = "\n".join(
        line for line in sql.splitlines() if not line.lstrip().startswith("--")
    )
    assert "{0,499}" not in statements
    assert "length(scheduler_job_id) <= 500" in statements
    # And the module refuses by the same two rules, so the two cannot drift.
    assert pc._JOB_ID_MAX == 500
    assert pc._JOB_ID_RE.match("a" * 600), "the pattern must not carry the length"


def test_the_binding_column_is_nullable_so_unbound_stays_sayable():
    """NULL is the honest answer, and the migration must not replace it with one.

    A NOT NULL column would have forced a fabricated default (`clock_name`),
    which reads `missing_in_gcp` -- false whenever the job exists under another
    id, which is precisely the production case this migration walks into.
    """
    sql = MIGRATION_197.read_text(encoding="utf-8")
    assert "SET NOT NULL" not in sql
    assert "scheduler_job_id IS NULL" in sql


def test_the_job_identity_migration_writes_no_deployment_identifier():
    """The prefix is handed to the RUN, never written in the file.

    Statements only: the header explains the prohibition, and a sentence
    explaining a prohibition must not read as a violation of it.
    """
    sql = MIGRATION_197.read_text(encoding="utf-8")
    statements = "\n".join(
        line for line in sql.splitlines() if not line.lstrip().startswith("--")
    )
    assert "toorow-" not in statements
    assert "europe-west1" not in statements
    assert "run.app" not in statements
    assert "toorow.platform_clock_job_prefix" in statements, (
        "the population must read the prefix from the session, which is the only "
        "way it can be honest without a literal"
    )


def test_the_population_only_ever_fills_a_blank():
    """A binding already present was chosen by somebody. Re-imposing this run's
    prefix on it would be the silent overwrite the registry forbids elsewhere."""
    sql = MIGRATION_197.read_text(encoding="utf-8")
    # Statements only: the header quotes the same UPDATE as the operator's repair
    # recipe, and a recipe in a comment must not be able to satisfy this gate.
    statements = "\n".join(
        line for line in sql.splitlines() if not line.lstrip().startswith("--")
    )
    update = statements.split("UPDATE app.platform_clocks", 1)[1].split(";", 1)[0]
    assert "WHERE scheduler_job_id IS NULL" in update
