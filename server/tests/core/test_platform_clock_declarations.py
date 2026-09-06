"""AI-117 -- populating the platform clock registry, and the clock that watches it.

THREE THINGS ARE PROVED HERE, each because its opposite is a defect this
repository has already paid for:

  1. the seven declarations and the seven `create_job` lines of
     `infra/gcp/provision_ad36_substrate.sh` are THE SAME LIST. A declaration
     with no job reads `missing_in_gcp` forever; a job with no declaration is
     `unmanaged_in_gcp`. Both are silent until somebody opens the screen, so the
     two lists are diffed here rather than trusted;
  2. re-running the population script declares NOTHING a second time, and in
     particular does not overwrite a cadence a human edited since. An upsert on
     every run would destroy the only evidence that the edit ever happened --
     the same destruction `reconcile()` is forbidden from performing against
     Cloud Scheduler;
  3. `/internal/scheduler/reconcile-clocks` OBSERVES and records. It never
     applies. The Cloud Scheduler double below exposes only the methods the
     module may call and records every one of them, so "made no mutating call"
     is a real assertion -- a `MagicMock` would have invented `delete_job` and
     the test would have passed while the endpoint deleted a job.

Entirely offline: no GCP, no Postgres, no network.
"""

from __future__ import annotations

import asyncio
import json
import re
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from starlette.requests import Request

# Anchored on this file. A path relative to the working directory yields a
# different verdict depending on where pytest was launched from (AI-136).
ROOT = Path(__file__).resolve().parents[3]
PROVISIONER = ROOT / "infra/gcp/provision_ad36_substrate.sh"

if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import declare_platform_clocks as dpc  # noqa: E402
from core import (  # noqa: E402
    admin_api,  # noqa: E402
    internal_api,  # AD-43 : le handler vit chez son sujet
)
from core import platform_clocks as pc  # noqa: E402

_INTERNAL_SECRET = "test-internal-secret"


async def _authorized(_request):
    """Stand in for the gate in tests whose subject is NOT the gate."""
    return None


# ---------------------------------------------------------------------------
# Doubles. Hand-written on purpose -- see the module docstring.
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
    """The subset of `scheduler_v1.Job` the module reads."""

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


class _FakeScheduler:
    """Records every call. Exposes ONLY the methods the module may use.

    Anything else raises `AttributeError`, so a mutating call this endpoint must
    never make cannot be silently absorbed.
    """

    def __init__(self, jobs=None) -> None:
        self._jobs = {job.name: job for job in (jobs or [])}
        self.calls: list[str] = []

    def list_jobs(self, request):
        self.calls.append("list_jobs")
        parent = request["parent"]
        return [job for name, job in self._jobs.items() if name.startswith(parent)]

    def mutations(self) -> list[str]:
        return [name for name in self.calls if name in _MUTATING]


_DECLARED_DEFAULTS = {
    "registry_policy_version": "platform-clock-v1",
    "declared_schedule": "0 2 * * *",
    "declared_timezone": "Europe/Paris",
    "declared_target_path": "/internal/scheduler/dispatch-nightly",
    "declared_http_method": "POST",
    "declared_attempt_deadline_seconds": 600,
    "desired_state": "enabled",
    "purpose": "declared by a fixture",
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

#: The job id these fixture rows BIND (migration 197). It is a property of the
#: row, not of the environment: the module under test no longer knows what a job
#: name prefix is.
_FIXTURE_PREFIX = "example-"


def _row(clock_name: str, **overrides) -> dict:
    return {
        "clock_name": clock_name,
        "scheduler_job_id": f"{_FIXTURE_PREFIX}{clock_name}",
        **_DECLARED_DEFAULTS,
        **overrides,
    }


def _row_from_declaration(entry: dpc.ClockDeclaration, **overrides) -> dict:
    """A registry row that matches this repository's declaration exactly.

    The fields are built as a dict and `overrides` is merged LAST, rather than
    forwarded as keywords alongside them. Passing both spellings of the same
    field raised `TypeError: got multiple values for keyword argument`, which is
    exactly what the two interesting tests do -- they take a matching row and
    change one field to make it diverge.
    """
    fields = {
        "declared_schedule": entry.schedule,
        "declared_timezone": entry.timezone,
        "declared_target_path": entry.target_path,
        "declared_http_method": entry.http_method,
        "declared_attempt_deadline_seconds": entry.attempt_deadline_seconds,
        "desired_state": entry.desired_state,
        "purpose": entry.purpose,
    }
    fields.update(overrides)
    return _row(entry.clock_name, **fields)


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
            self.description, self._rows = None, []
            return
        if text.startswith("SELECT clock_name"):
            rows = self._conn.rows
            if "WHERE clock_name = %s" in text:
                rows = [r for r in rows if r["clock_name"] == params[0]]
            else:
                rows = sorted(rows, key=lambda r: r["clock_name"])
            self.description = [(name,) for name in _COLUMNS]
            self._rows = [tuple(r[name] for name in _COLUMNS) for r in rows]
            return
        if text.startswith("INSERT INTO app.platform_clocks"):
            self._conn.declares.append(params)
            # params[8] is the binding a fresh row takes; params[9] is the one the
            # caller EXPLICITLY named, or None -- the upsert's COALESCE keeps the
            # stored binding when it is None, so a re-run never rebinds.
            previous = next(
                (r for r in self._conn.rows if r["clock_name"] == params[0]), None
            )
            row = _row(
                params[0],
                scheduler_job_id=(
                    params[8] if previous is None
                    else (params[9] or previous.get("scheduler_job_id"))
                ),
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
            self.description, self._rows = None, []
            return
        if text.startswith("UPDATE app.platform_clocks"):
            self._conn.updates.append(params)
            self.description, self._rows = None, []
            return
        raise AssertionError(f"unexpected statement: {text[:90]}")

    def fetchall(self):
        return list(self._rows)


class _FakeConn:
    """Answers the exact three statements these paths issue, refuses the rest.

    A query that drifts fails HERE, loudly, instead of at the first deployment.
    """

    def __init__(self, rows=()) -> None:
        self.rows = [dict(r) for r in rows]
        self.declares: list[tuple] = []
        self.updates: list[tuple] = []
        self.armed = 0
        self.commits = 0

    def cursor(self):
        return _FakeCursor(self)

    def commit(self):
        self.commits += 1

    # `with get_connection() as conn:` -- the handler's shape.
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """No inherited GCP or auth configuration leaks into these tests."""
    for name in (
        "CLOUD_SCHEDULER_PROJECT", "CLOUD_TASKS_PROJECT", "GOOGLE_CLOUD_PROJECT",
        "CLOUD_SCHEDULER_LOCATION", "CLOUD_TASKS_LOCATION",
        "PLATFORM_CLOCK_JOB_PREFIX", "PLATFORM_CLOCK_TARGET_BASE_URL",
        "CLOUD_TASKS_WORKER_URL", "PLATFORM_CLOCK_INTERNAL_AUTH_FILE",
        "INTERNAL_ENDPOINTS_REQUIRE_HEADER", "QUEUE_BACKEND",
        "INTERNAL_OIDC_AUDIENCE", "TOOROW_SUPER_ADMINS",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def gcp_env(monkeypatch):
    """Project, region and target base URL only.

    No job-name prefix: since migration 197 the job a clock drives is a registry
    value carried by the row (`scheduler_job_id`), not an environment variable
    glued onto the clock name.
    """
    monkeypatch.setenv("CLOUD_SCHEDULER_PROJECT", "proj_EXAMPLE")
    monkeypatch.setenv("CLOUD_SCHEDULER_LOCATION", "europe-west1")
    monkeypatch.setenv("PLATFORM_CLOCK_TARGET_BASE_URL", "https://example.com")


def _job_name(job_id: str) -> str:
    return f"projects/proj_EXAMPLE/locations/europe-west1/jobs/{job_id}"


# ---------------------------------------------------------------------------
# 1. The registry and the provisioner declare the same seven clocks.
# ---------------------------------------------------------------------------

_CREATE_JOB = re.compile(
    r"^create_job\s+(?P<name>[a-z0-9-]+)\s+\"(?P<schedule>[^\"]+)\"\s+(?P<path>\S+)",
    re.MULTILINE,
)


def _provisioned_jobs() -> dict[str, tuple[str, str]]:
    text = PROVISIONER.read_text(encoding="utf-8")
    return {
        m.group("name"): (m.group("schedule").strip(), m.group("path"))
        for m in _CREATE_JOB.finditer(text)
    }


def test_the_registry_and_the_provisioner_declare_the_same_clocks():
    """One list, two files. A clock in only one of them is silent in production.

    A declaration with no `create_job` line reads `missing_in_gcp` forever; a
    `create_job` line with no declaration is reported `unmanaged_in_gcp` and
    nobody owns it. Neither shows up until somebody opens the screen.
    """
    provisioned = _provisioned_jobs()
    declared = {e.clock_name: (e.schedule, e.target_path) for e in dpc.DECLARATIONS}

    assert set(declared) == set(provisioned), (
        "the registry and provision_ad36_substrate.sh disagree about which "
        f"clocks exist: only-declared={sorted(set(declared) - set(provisioned))} "
        f"only-provisioned={sorted(set(provisioned) - set(declared))}"
    )
    for name, (schedule, path) in declared.items():
        assert provisioned[name] == (schedule, path), (
            f"{name}: declared {schedule!r} {path} but the provisioner creates "
            f"{provisioned[name][0]!r} {provisioned[name][1]}"
        )


def test_the_two_clocks_that_had_no_job_are_declared():
    """The two measured holes, named so a later edit cannot drop them quietly."""
    names = set(dpc.clock_names())
    assert "run-dq-monitors" in names, (
        "the DQ monitor sweep is served by admin_api and had no Cloud Scheduler "
        "job at all: without this declaration nothing reports that it never runs"
    )
    assert "reconcile-clocks" in names, (
        "without the clock that watches the clocks, observed_* is never "
        "refreshed and the screen shows an observation that ages in silence"
    )


def test_every_declared_target_is_actually_served():
    """A clock pointed at an unserved path is a 404 answered by retries, forever."""
    from core.admin_api import router
    from starlette.routing import Match

    for entry in dpc.DECLARATIONS:
        scope = {"type": "http", "method": entry.http_method,
                 "path": entry.target_path, "path_params": {}, "headers": [],
                 "root_path": ""}
        assert any(r.matches(scope)[0] is Match.FULL for r in router.routes), (
            f"{entry.clock_name} targets {entry.target_path}, which no route serves"
        )


def test_no_declaration_carries_a_deployment_identifier():
    """The repository stays shareable: project, region and prefix are env facts."""
    blob = json.dumps([e.__dict__ for e in dpc.DECLARATIONS])
    for forbidden in ("proj_", "conn_", "ds_", "http://", "https://", "@"):
        assert forbidden not in blob, f"{forbidden!r} leaked into a declaration"


# ---------------------------------------------------------------------------
# 2. Population is idempotent, and it never overwrites a human's edit.
# ---------------------------------------------------------------------------


def test_first_run_declares_every_clock():
    conn = _FakeConn()
    report = dpc.populate(conn)

    assert report["counts"] == {dpc.DECLARED: len(dpc.DECLARATIONS)}
    assert len(conn.declares) == len(dpc.DECLARATIONS)
    assert {row["clock_name"] for row in conn.rows} == set(dpc.clock_names())
    assert report["cloud_scheduler_untouched"] is True


def test_a_second_run_declares_nothing():
    """Idempotence, measured as ZERO writes -- not as 'the same end state'.

    An upsert that rewrites seven identical rows every night looks idempotent
    and is not: it stamps `updated_at`, so the registry can no longer say when a
    declaration last actually changed.
    """
    conn = _FakeConn()
    dpc.populate(conn)
    writes_after_first = len(conn.declares)

    report = dpc.populate(conn)

    assert len(conn.declares) == writes_after_first, "the second run wrote again"
    assert report["counts"] == {dpc.KEPT: len(dpc.DECLARATIONS)}


def test_a_hand_edited_cadence_survives_a_re_run():
    """The point of the whole script: an edit made in the product is evidence.

    Silently re-imposing the repository's cadence would destroy the only trace
    that somebody changed it -- the same failure `reconcile()` is forbidden from
    committing against Cloud Scheduler.
    """
    edited = _row_from_declaration(dpc.DECLARATIONS[0], declared_schedule="30 4 * * *")
    conn = _FakeConn([edited])

    report = dpc.populate(conn)

    assert all(params[0] != edited["clock_name"] for params in conn.declares), (
        "the re-run overwrote a cadence a human had edited"
    )
    stored = next(r for r in conn.rows if r["clock_name"] == edited["clock_name"])
    assert stored["declared_schedule"] == "30 4 * * *"

    decision = next(
        d for d in report["decisions"] if d["clock_name"] == edited["clock_name"]
    )
    assert decision["action"] == dpc.KEPT_DIVERGENT
    assert decision["divergence"]["schedule"] == {
        "repository": dpc.DECLARATIONS[0].schedule,
        "registry": "30 4 * * *",
    }, "the divergence must be NAMED, not merely counted"


def test_force_re_imposes_exactly_one_named_clock():
    """`--force` is the deliberate act. It is per clock, and it stays per clock."""
    first, second = dpc.DECLARATIONS[0], dpc.DECLARATIONS[1]
    conn = _FakeConn([
        _row_from_declaration(first, declared_schedule="30 4 * * *"),
        _row_from_declaration(second, declared_schedule="45 * * * *"),
    ])

    report = dpc.populate(conn, force=(first.clock_name,))

    written = [p[0] for p in conn.declares]
    assert first.clock_name in written, "--force did not re-impose the named clock"
    assert second.clock_name not in written, (
        "--force rewrote a clock it was not given: the other divergent row must "
        "survive untouched"
    )
    kept = next(r for r in conn.rows if r["clock_name"] == second.clock_name)
    assert kept["declared_schedule"] == "45 * * * *"
    assert report["forced"] == [first.clock_name]

    actions = {d["clock_name"]: d["action"] for d in report["decisions"]}
    assert actions[first.clock_name] == dpc.REDECLARED
    assert actions[second.clock_name] == dpc.KEPT_DIVERGENT


def test_force_refuses_a_wildcard():
    """There is no `--force all`: one mistaken call must not rewrite seven clocks."""
    for wildcard in ("*", "all", "ALL", "any"):
        assert dpc.main(["--force", wildcard]) == 2, (
            f"--force {wildcard!r} was accepted; a wildcard must be refused"
        )


def test_dry_run_writes_nothing_and_decides_the_same():
    conn = _FakeConn()
    dry = dpc.populate(conn, dry_run=True)

    assert conn.declares == []
    assert conn.commits == 0
    assert dry["counts"] == {dpc.DECLARED: len(dpc.DECLARATIONS)}

    real = dpc.populate(_FakeConn())
    assert [d["action"] for d in dry["decisions"]] == [
        d["action"] for d in real["decisions"]
    ], "--dry-run and the real run disagreed about what was about to happen"


def test_population_arms_the_platform_operator_context():
    """Without it, RLS shows zero rows and the script would declare everything twice."""
    conn = _FakeConn()
    dpc.populate(conn)
    assert conn.armed > 0


# ---------------------------------------------------------------------------
# 3. The clock that watches the clocks: observes, records, never applies.
# ---------------------------------------------------------------------------


def _handler():
    handler = getattr(internal_api, "_reconcile_clocks_internal", None)
    assert handler is not None, (
        "internal_api._reconcile_clocks_internal is missing: apply the handler diff "
        "for POST /internal/scheduler/reconcile-clocks"
    )
    return handler


def _internal_request(headers=None) -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/internal/scheduler/reconcile-clocks",
            "path_params": {},
            "headers": headers or [],
            "query_string": b"",
        }
    )


def _call(request) -> tuple[int, dict]:
    resp = asyncio.run(_handler()(request))
    return resp.status_code, json.loads(bytes(resp.body).decode())


def test_reconcile_clocks_observes_and_never_applies(gcp_env):
    """It records the drift. It does NOT correct it.

    The fake scheduler exposes `list_jobs` and nothing else, so a call to
    `update_job` would raise `AttributeError` rather than be absorbed.
    """
    nightly = dpc.DECLARATIONS[0]
    conn = _FakeConn([_row_from_declaration(nightly)])
    scheduler = _FakeScheduler([
        _Job(
            _job_name(f"example-{nightly.clock_name}"),
            # Drifted on purpose: somebody edited the cadence in the console.
            "0 3 * * *",
            nightly.timezone,
            f"https://example.com{nightly.target_path}",
        )
    ])

    with (
        patch("core.db.get_connection", lambda *a, **k: conn),
        patch.object(pc, "_scheduler_client", lambda client=None: scheduler),
        patch.object(admin_api, "_authorize_internal", _authorized),
    ):
        status, body = _call(_internal_request())

    assert status == 200
    assert scheduler.calls == ["list_jobs"], (
        f"the reconciliation called {scheduler.calls} -- it must only observe"
    )
    assert scheduler.mutations() == [], "the reconciliation corrected the drift"

    assert body["counts"][pc.DRIFTED] == 1
    assert body["drift_is_reported_not_repaired"] is True
    assert body["verdicts"][0]["clock_name"] == nightly.clock_name
    assert body["verdicts"][0]["verdict"] == pc.DRIFTED

    # And the drift is written down, which is what makes it readable tomorrow.
    assert len(conn.updates) == 1
    assert conn.updates[0][8] == pc.DRIFTED
    assert conn.updates[0][11] == nightly.clock_name


def test_reconcile_clocks_reports_unknown_when_gcp_cannot_be_read(gcp_env):
    """An observation that could not run has not proven synchronisation.

    The endpoint still answers 200: a tick that 503s forever on a missing
    credential is noise, and the reason is already recorded per clock as
    `unknown`. What must never happen is `in_sync`.
    """
    nightly = dpc.DECLARATIONS[0]
    conn = _FakeConn([_row_from_declaration(nightly)])

    class _Refusing:
        def list_jobs(self, request):
            raise RuntimeError("permission denied")

    with (
        patch("core.db.get_connection", lambda *a, **k: conn),
        patch.object(pc, "_scheduler_client", lambda client=None: _Refusing()),
        patch.object(admin_api, "_authorize_internal", _authorized),
    ):
        status, body = _call(_internal_request())

    assert status == 200
    assert body["observation_ok"] is False
    assert body["observation_error"]
    assert body["counts"][pc.UNKNOWN] == 1
    assert body["counts"].get(pc.IN_SYNC, 0) == 0


def test_reconcile_clocks_body_survives_a_timestamped_observation(gcp_env):
    """A `datetime` in a JSONResponse is a 500 on the endpoint that must answer.

    `last_attempt_time` is a real Cloud Scheduler field, so the handler cannot
    hand the raw verdicts to `JSONResponse`.
    """
    import datetime as dt

    nightly = dpc.DECLARATIONS[0]
    job = _Job(
        _job_name(f"example-{nightly.clock_name}"),
        nightly.schedule,
        nightly.timezone,
        f"https://example.com{nightly.target_path}",
    )
    job.last_attempt_time = dt.datetime(2026, 8, 2, 2, 0, tzinfo=dt.timezone.utc)

    conn = _FakeConn([_row_from_declaration(nightly)])
    with (
        patch("core.db.get_connection", lambda *a, **k: conn),
        patch.object(pc, "_scheduler_client", lambda client=None: _FakeScheduler([job])),
        patch.object(admin_api, "_authorize_internal", _authorized),
    ):
        status, body = _call(_internal_request())

    assert status == 200
    assert body["counts"][pc.IN_SYNC] == 1


def test_reconcile_clocks_refuses_a_caller_without_the_secret():
    """No secret, no user token: the refusal happens BEFORE anything is observed.

    Nothing patches the gate here. The day this endpoint stops calling
    `_authorize_internal`, this turns red instead of staying green.
    """
    observed: list[str] = []

    async def _unauthenticated(_request):
        return False, ""

    with (
        patch.object(admin_api, "_check_auth", _unauthenticated),
        patch.object(pc, "reconcile", lambda *a, **k: observed.append("ran")),
        patch("core.db.get_connection", lambda *a, **k: _FakeConn()),
    ):
        status, body = _call(_internal_request())

    assert status == 401
    assert body["code"] == "unauthorized"
    assert observed == [], "the endpoint observed Cloud Scheduler before authorizing"


def test_reconcile_clocks_accepts_the_platform_shared_secret(monkeypatch, gcp_env):
    """The positive control: Cloud Scheduler carries no Bearer token, only the header."""
    monkeypatch.setenv("INTERNAL_ENDPOINTS_REQUIRE_HEADER", _INTERNAL_SECRET)
    conn = _FakeConn([_row_from_declaration(dpc.DECLARATIONS[0])])

    with (
        patch("core.db.get_connection", lambda *a, **k: conn),
        patch.object(pc, "_scheduler_client", lambda client=None: _FakeScheduler()),
    ):
        status, body = _call(
            _internal_request([(b"x-internal-auth", _INTERNAL_SECRET.encode())])
        )

    assert status == 200
    assert body["counts"][pc.MISSING_IN_GCP] == 1


def test_reconcile_clocks_is_routed():
    """A tick pushed at an unserved path is a 404 answered by retries."""
    from core.admin_api import router
    from starlette.routing import Match

    for path in ("/internal/scheduler/reconcile-clocks",
                 "/internal/scheduler/run-dq-monitors"):
        scope = {"type": "http", "method": "POST", "path": path,
                 "path_params": {}, "headers": [], "root_path": ""}
        assert any(r.matches(scope)[0] is Match.FULL for r in router.routes), path
