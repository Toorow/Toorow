"""AI-117 -- the platform clock registry, and its sync with Cloud Scheduler.

WHY THIS MODULE EXISTS. Five Cloud Scheduler jobs drive the whole platform
(dispatch-nightly, dispatch-hourly, reconcile-queues, poll-health,
drain-outbox). Until this module they existed ONLY in GCP: created once by
``infra/gcp/provision_ad36_substrate.sh``, unknown to the database, unreadable
from the product, un-editable without the Google console. Jean's requirement,
literal: "on doit avoir une sync entre l'interface et les cloud schedule".

The half that survives a restart is ``app.platform_clocks`` (migration 195).
This module is the other half: it OBSERVES what Cloud Scheduler holds, COMPARES
it to what the registry declares, and -- only when a human names one clock --
APPLIES the declaration.

THE LINE THIS MODULE DOES NOT CROSS. A platform clock is not a Datastream
cadence. The cadence of a Datastream is a product setting, it already exists in
``app.datastream_schedule_state`` and it is already editable through
``core/schedule_mcp.py``. Nothing here reads, writes or references either.

FOUR PROPERTIES, each written because its opposite is a defect this repository
has already paid for:

  1. ``observe`` and ``reconcile`` NEVER mutate Cloud Scheduler. They call
     ``list_jobs`` / ``get_job`` and nothing else. Silently re-imposing the
     declared value would destroy the only evidence that somebody edited a job
     by hand -- and a drift you cannot see is worse than a drift.
  2. ``apply`` refuses to act unless the caller NAMES the clock. There is no
     "apply everything": a single call must never be able to rewrite five
     clocks at once.
  3. FAIL-CLOSED everywhere. An unreachable API, a missing client library, an
     absent project, a permission error -- each yields the verdict ``unknown``
     WITH the reason, never a guess. An uncertainty at an authorization or
     execution seam is never a yes.
  4. NO production identifier is written here. The GCP project comes from
     ``CLOUD_SCHEDULER_PROJECT`` / ``CLOUD_TASKS_PROJECT`` /
     ``GOOGLE_CLOUD_PROJECT`` exactly as ``core/queue.py`` reads it, and the
     region from ``CLOUD_SCHEDULER_LOCATION`` / ``CLOUD_TASKS_LOCATION``. The
     repository stays shareable.
  5. The job a clock drives is READ FROM THE REGISTRY, never composed from the
     environment. Until migration 197 it was a job-name prefix read from an
     environment variable and glued onto the clock name -- a truth about the
     platform stored outside the platform, which is the exact failure this
     registry exists to end, plus a free failure mode: forget the variable in one
     deployment and the seven declared clocks read ``missing_in_gcp`` while the
     seven real jobs read ``unmanaged_in_gcp``, fourteen false lines whose cause
     appears on no screen. Migration 197 holds the whole story. A row that binds
     no job is UNBOUND and answered with ``unknown``, never ``missing_in_gcp``.

THE ``\\r`` TRAP, recorded because it cost a day on 2026-08-02. The platform
secret was created on Windows with ``print()``, so its stored value ended
``\\r\\r\\n``. Every Scheduler job then carried ``X-Internal-Auth: <secret>\\r``
and EVERY invocation would have failed authentication -- a symptom that reads
like a broken substrate rather than a mis-provisioned one. Anything this module
reads or writes as that header goes through ``_internal_auth_value``, which
handles raw bytes and strips CR/LF.
"""

from __future__ import annotations

import logging
import os
import re
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Verdict vocabulary. One name per concept; these strings are also the CHECK
# constraint vocabulary of migration 195, so they cannot drift apart silently.
# ---------------------------------------------------------------------------

IN_SYNC = "in_sync"
DRIFTED = "drifted"
MISSING_IN_GCP = "missing_in_gcp"
UNMANAGED_IN_GCP = "unmanaged_in_gcp"
UNKNOWN = "unknown"

VERDICTS = (IN_SYNC, DRIFTED, MISSING_IN_GCP, UNMANAGED_IN_GCP, UNKNOWN)

REGISTRY_POLICY_VERSION = "platform-clock-v1"

#: The columns `reconcile` writes. Declared here so a reader can see, in one
#: place, that the DECLARED half is never among them.
_OBSERVED_COLUMNS = (
    "observed_at",
    "observed_state",
    "observed_schedule",
    "observed_timezone",
    "observed_target_uri",
    "observed_http_method",
    "observed_attempt_deadline_seconds",
    "observed_last_attempt_at",
    "observed_last_attempt_status",
    "drift_verdict",
    "drift_detail",
    "observation_error",
)

_CLOCK_NAME_MAX = 61


class PlatformClockError(RuntimeError):
    """A refusal this module makes on purpose, with a machine-readable code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


# ---------------------------------------------------------------------------
# Environment. Read, never hardcoded (the repository must stay shareable).
# ---------------------------------------------------------------------------


def _env(name: str) -> str:
    return (os.environ.get(name) or "").strip()


def resolve_project() -> str:
    """The GCP project holding the clocks, or a refusal naming the variable.

    The same precedence ``core/queue.py`` already uses, so a deployment does not
    have to configure a second project for the same GCP account.
    """
    project = _env("CLOUD_SCHEDULER_PROJECT") or _env("CLOUD_TASKS_PROJECT")
    project = project or _env("GOOGLE_CLOUD_PROJECT")
    if not project:
        raise PlatformClockError(
            "project_unset",
            "CLOUD_SCHEDULER_PROJECT (or CLOUD_TASKS_PROJECT / "
            "GOOGLE_CLOUD_PROJECT) is required to reach Cloud Scheduler",
        )
    return project


def resolve_location() -> str:
    """The Cloud Scheduler region, or a refusal naming the variable."""
    location = _env("CLOUD_SCHEDULER_LOCATION") or _env("CLOUD_TASKS_LOCATION")
    if not location:
        raise PlatformClockError(
            "location_unset",
            "CLOUD_SCHEDULER_LOCATION (or CLOUD_TASKS_LOCATION) is required to "
            "reach Cloud Scheduler",
        )
    return location


# ---------------------------------------------------------------------------
# The binding: which Cloud Scheduler job a declared clock drives.
#
# It is a REGISTRY value (migration 197, column ``scheduler_job_id``), never an
# environment value and never a heuristic on job names. See property 5 of the
# module docstring for what the environment version cost.
#
# NULL means UNBOUND, and unbound is a first-class, readable state: the registry
# does not know which job the clock drives, and it says exactly that. It is NOT
# resolved by observation -- picking the GCP job whose id ends with the clock
# name is adoption, and adoption is what migration 195's
# ``ck_platform_clocks_never_declares_unmanaged`` exists to forbid.
# ---------------------------------------------------------------------------

#: The registry column holding the binding, named once so a rename is one edit.
JOB_ID_COLUMN = "scheduler_job_id"

#: Cloud Scheduler's own rule for a job id, mirrored from migration 197's CHECK:
#: a letter, then up to 499 letters, digits, hyphens or underscores. The length
#: is a separate test on BOTH sides because Postgres caps a regex repetition
#: count at 255 -- `{0,499}` is an invalid regular expression there, and one that
#: only fails when a value is finally written. Spelling the two rules the same
#: way here is what keeps the module's refusal and the constraint from drifting.
_JOB_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")
_JOB_ID_MAX = 500

#: Why an unbound clock cannot be observed. Stored as `observation_error`, so the
#: reason survives the process that produced it.
UNBOUND_REASON = (
    "clock_unbound: this clock binds no Cloud Scheduler job id, so there is "
    "nothing to observe. The registry does not guess which job a row drives."
)


def bound_job_id(declared) -> str | None:
    """The job id this registry row binds, or None when the row binds nothing."""
    if not isinstance(declared, dict):
        return None
    value = declared.get(JOB_ID_COLUMN)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def job_id_for(declared) -> str:
    """The Cloud Scheduler job id of ONE declared clock, read from its own row.

    Takes the ROW, not the name, and that signature is the point: there is no
    function from a clock name to a job id any more, so no caller can rebuild one
    out of an environment variable by accident.

    Refuses rather than guessing. Falling back to the clock name would invent a
    second binding nobody chose, and the symptom -- a clock reported
    ``missing_in_gcp`` while its job runs fine under another id -- would appear
    nowhere near the cause.
    """
    bound = bound_job_id(declared)
    if bound is None:
        raise PlatformClockError("clock_unbound", UNBOUND_REASON)
    return bound


def bound_index(declared_rows) -> dict[str, str]:
    """``{job_id: clock_name}`` for every declared clock that binds a job."""
    index: dict[str, str] = {}
    for row in declared_rows:
        bound = bound_job_id(row)
        if bound is not None:
            index[bound] = row.get("clock_name")
    return index


def clock_name_for(job_id: str, declared_rows) -> str | None:
    """Which declared clock binds this job id, or None -- the `unmanaged` test.

    Resolved against the registry, never against a prefix. A prefix test answers
    "does this job's id LOOK like one of ours", which adopts a stranger's cron
    whose id happens to start the same way and misses our own job the day
    somebody renames it. The registry answers "does any row CLAIM this job",
    which is the question, and it keeps working in a deployment whose jobs share
    no common prefix at all.
    """
    return bound_index(declared_rows).get((job_id or "").strip())


def _target_base_url() -> str:
    """The service base URL a clock's HTTP target is built from."""
    base = _env("PLATFORM_CLOCK_TARGET_BASE_URL") or _env("CLOUD_TASKS_WORKER_URL")
    if not base:
        raise PlatformClockError(
            "target_base_url_unset",
            "PLATFORM_CLOCK_TARGET_BASE_URL (or CLOUD_TASKS_WORKER_URL) is "
            "required before a clock can be created or updated in GCP",
        )
    return base.rstrip("/")


def _internal_auth_value() -> str | None:
    """The ``X-Internal-Auth`` value, read as raw bytes and stripped of CR/LF.

    See the module docstring: a value stored with a trailing ``\\r`` makes every
    scheduled invocation fail authentication while every dashboard stays green.
    ``PLATFORM_CLOCK_INTERNAL_AUTH_FILE`` is read in binary on purpose -- a text
    read on Windows would re-introduce the newline translation that caused it.
    """
    path = _env("PLATFORM_CLOCK_INTERNAL_AUTH_FILE")
    if path:
        try:
            with open(path, "rb") as handle:
                raw = handle.read()
        except OSError as exc:
            raise PlatformClockError(
                "internal_auth_unreadable",
                f"internal auth file could not be read: {type(exc).__name__}",
            ) from exc
        return raw.decode("utf-8", "strict").strip("\r\n").strip() or None
    value = os.environ.get("INTERNAL_ENDPOINTS_REQUIRE_HEADER")
    if value is None:
        return None
    return value.strip("\r\n").strip() or None


# ---------------------------------------------------------------------------
# Resource names. Built as literals rather than through client helpers so the
# result is identical under a real client and under a test double -- a mock
# that invents `common_location_path` would make the test prove nothing.
# ---------------------------------------------------------------------------


def _location_path(project: str, location: str) -> str:
    return f"projects/{project}/locations/{location}"


def _job_path(project: str, location: str, job_id: str) -> str:
    return f"{_location_path(project, location)}/jobs/{job_id}"


def _scheduler_client(scheduler_client=None):
    """Return the injected client, or build one. ImportError is a refusal."""
    if scheduler_client is not None:
        return scheduler_client
    try:
        from google.cloud import scheduler_v1  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover -- exercised via the refusal test
        raise PlatformClockError(
            "client_unavailable",
            "google-cloud-scheduler is not installed; declare it in "
            "server/pyproject.toml before observing platform clocks",
        ) from exc
    return scheduler_v1.CloudSchedulerClient()


def _is_not_found(exc: BaseException) -> bool:
    """True for the 'this job does not exist' failure, false for every other.

    Deliberately duck-typed: importing ``google.api_core.exceptions`` here would
    make an absent optional dependency turn a clean ``missing_in_gcp`` into an
    ImportError, i.e. a configuration problem reported as a code problem.
    """
    if type(exc).__name__ == "NotFound":
        return True
    return getattr(exc, "code", None) == 404


# ---------------------------------------------------------------------------
# Reading a Cloud Scheduler Job into a plain dict.
# ---------------------------------------------------------------------------

_STATE_NAMES = {
    "STATE_UNSPECIFIED": UNKNOWN,
    "ENABLED": "enabled",
    "PAUSED": "paused",
    "DISABLED": "disabled",
    "UPDATE_FAILED": "update_failed",
}


def _normalize_state(raw) -> str:
    """Map a Job.State (enum, int or string) onto the registry vocabulary."""
    name = getattr(raw, "name", None)
    if name is None:
        name = str(raw) if raw is not None else ""
    return _STATE_NAMES.get(str(name).upper(), UNKNOWN)


def _duration_seconds(raw) -> int | None:
    """Seconds from a protobuf Duration, an int, or None."""
    if raw is None:
        return None
    seconds = getattr(raw, "seconds", None)
    if seconds is not None:
        return int(seconds)
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _job_to_observation(job) -> dict:
    """Everything the registry compares, read defensively off one Job."""
    name = str(getattr(job, "name", "") or "")
    job_id = name.rsplit("/", 1)[-1] if name else ""
    target = getattr(job, "http_target", None)
    uri = str(getattr(target, "uri", "") or "") if target is not None else ""
    method = getattr(target, "http_method", None) if target is not None else None
    status = getattr(job, "status", None)
    status_text = None
    if status is not None:
        message = getattr(status, "message", None)
        code = getattr(status, "code", None)
        if message:
            status_text = str(message)[:500]
        elif code not in (None, 0):
            status_text = f"code={code}"
    return {
        "job_id": job_id,
        "resource_name": name,
        "state": _normalize_state(getattr(job, "state", None)),
        "schedule": str(getattr(job, "schedule", "") or "") or None,
        "timezone": str(getattr(job, "time_zone", "") or "") or None,
        "target_uri": uri or None,
        "target_path": urlsplit(uri).path if uri else None,
        "http_method": (
            str(getattr(method, "name", method) or "").upper() or None
            if method is not None
            else None
        ),
        "attempt_deadline_seconds": _duration_seconds(
            getattr(job, "attempt_deadline", None)
        ),
        "last_attempt_at": getattr(job, "last_attempt_time", None) or None,
        "last_attempt_status": status_text,
    }


# ---------------------------------------------------------------------------
# The registry -- reads only. `arm_platform_clock_access` is what opens the RLS
# policy of migration 195 for a platform-scoped session.
# ---------------------------------------------------------------------------


def arm_platform_clock_access(conn) -> None:
    """Declare platform-operator context for the current transaction.

    ``app.platform_clocks`` has no ``org_id``, so the Epic-36 membership gate
    cannot decide access to it. Its policy opens only for a session that has
    declared this flag; a session that forgets sees zero rows rather than
    another tenant's. Transaction-local (``set_config(..., true)``), so it can
    never leak onto a pooled connection.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT set_config('toorow.platform_operator', 'on', true)")


_DECLARED_SELECT = """
    SELECT clock_name,
           scheduler_job_id,
           registry_policy_version,
           declared_schedule,
           declared_timezone,
           declared_target_path,
           declared_http_method,
           declared_attempt_deadline_seconds,
           desired_state,
           purpose,
           observed_at,
           observed_state,
           observed_schedule,
           observed_timezone,
           observed_target_uri,
           observed_http_method,
           observed_attempt_deadline_seconds,
           observed_last_attempt_at,
           observed_last_attempt_status,
           drift_verdict,
           drift_detail,
           observation_error,
           created_at,
           updated_at
      FROM app.platform_clocks
"""


def _rows_to_dicts(cur) -> list[dict]:
    columns = [desc[0] for desc in cur.description]
    return [dict(zip(columns, row)) for row in cur.fetchall()]


def list_declared(conn) -> list[dict]:
    """Every declared platform clock, with its last recorded observation.

    Read-only. The caller gets both halves in one row precisely so the drift is
    visible without a second query: a screen that shows only the declared
    cadence would show a clock that is paused in GCP as if it were running.

    ``job_id`` is the binding the row itself carries, and it is None for an
    unbound clock. Reading must never invent one: a list that filled the blank
    would make the gap it exists to show disappear.
    """
    arm_platform_clock_access(conn)
    with conn.cursor() as cur:
        cur.execute(_DECLARED_SELECT + " ORDER BY clock_name")
        rows = _rows_to_dicts(cur)
    for row in rows:
        row["job_id"] = bound_job_id(row)
    return rows


def get_declared(conn, clock_name: str) -> dict | None:
    """One declared clock, or None. Used by ``apply`` to refuse unknown names."""
    arm_platform_clock_access(conn)
    with conn.cursor() as cur:
        cur.execute(_DECLARED_SELECT + " WHERE clock_name = %s", (clock_name,))
        rows = _rows_to_dicts(cur)
    if not rows:
        return None
    rows[0]["job_id"] = bound_job_id(rows[0])
    return rows[0]


_DECLARE_UPSERT = """
    INSERT INTO app.platform_clocks (
        clock_name, declared_schedule, declared_timezone, declared_target_path,
        declared_http_method, declared_attempt_deadline_seconds, desired_state,
        purpose, scheduler_job_id
    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
    ON CONFLICT (clock_name) DO UPDATE SET
        declared_schedule = EXCLUDED.declared_schedule,
        declared_timezone = EXCLUDED.declared_timezone,
        declared_target_path = EXCLUDED.declared_target_path,
        declared_http_method = EXCLUDED.declared_http_method,
        declared_attempt_deadline_seconds =
            EXCLUDED.declared_attempt_deadline_seconds,
        desired_state = EXCLUDED.desired_state,
        purpose = EXCLUDED.purpose,
        -- Re-declaring NEVER rebinds. The last parameter is the binding the
        -- caller EXPLICITLY named, or NULL; an existing clock keeps the job it
        -- already drives. A re-run of the population script that silently
        -- rebound seven clocks to their own names would point every one of them
        -- at a job that does not exist, and the symptom would surface hours
        -- later as `unknown`, nowhere near the cause.
        scheduler_job_id = COALESCE(%s, app.platform_clocks.scheduler_job_id)
"""


def declare(
    conn,
    *,
    clock_name,
    schedule: str,
    timezone: str,
    target_path: str,
    purpose: str,
    actor: str,
    desired_state: str = "enabled",
    http_method: str = "POST",
    attempt_deadline_seconds: int = 600,
    scheduler_job_id: str | None = None,
) -> dict:
    """Write the DECLARED half of one clock. Never touches the observed half.

    This is the "interface" side of the sync Jean asked for: what the platform
    intends. It changes NOTHING in Cloud Scheduler -- declaring a new cadence
    makes the next reconciliation report ``drifted``, and ``apply`` is what
    carries it across. Splitting the two is the whole design: an edit that
    silently reached GCP would be indistinguishable from an edit that failed to.

    THE BINDING. A NEW clock binds the job id given in *scheduler_job_id*, or its
    own name when none is given. That is a CHOICE made at declaration time and
    stored -- ``apply`` then creates exactly that job -- not a guess about an id
    that already exists elsewhere in GCP. An EXISTING clock keeps the job it
    already binds unless this call names a new one: see the upsert above.

    The same one-named-clock discipline as ``apply``: there is no bulk declare.
    """
    try:
        name = _validate_clock_name(clock_name)
    except PlatformClockError as exc:
        return {"ok": False, "clock_name": None, "reason": f"{exc.code}: {exc}"}

    if desired_state not in {"enabled", "paused", "retired"}:
        return {
            "ok": False,
            "clock_name": name,
            "reason": f"invalid_desired_state: {desired_state!r}",
        }

    requested_job_id = (scheduler_job_id or "").strip() or None
    if requested_job_id is not None and (
        not _JOB_ID_RE.match(requested_job_id)
        or len(requested_job_id) > _JOB_ID_MAX
    ):
        # Refused here rather than by migration 197's CHECK, so the caller reads
        # the rule instead of an opaque 23514 three layers down.
        return {
            "ok": False,
            "clock_name": name,
            "reason": f"invalid_scheduler_job_id: {requested_job_id!r}",
        }

    arm_platform_clock_access(conn)
    with conn.cursor() as cur:
        cur.execute(
            _DECLARE_UPSERT,
            (
                name,
                schedule,
                timezone,
                target_path,
                http_method,
                int(attempt_deadline_seconds),
                desired_state,
                purpose,
                requested_job_id or name,
                requested_job_id,
            ),
        )
    conn.commit()
    logger.info(
        "platform_clocks: declared clock=%s desired_state=%s bound=%s actor=%s",
        name,
        desired_state,
        bool(requested_job_id),
        actor,
    )
    return {"ok": True, "clock_name": name, "reason": None}


# ---------------------------------------------------------------------------
# observe -- read GCP, write NOTHING (not the registry, not Cloud Scheduler).
# ---------------------------------------------------------------------------


def observe(*, scheduler_client=None, project: str | None = None,
            location: str | None = None) -> dict:
    """Return what Cloud Scheduler currently holds, or say why we do not know.

    Writes nothing anywhere. The return is deliberately a two-field answer --
    ``ok`` plus ``error`` -- because "we saw no jobs" and "we could not look"
    are different facts, and conflating them is how an empty region reads as a
    healthy one.

    Returns::

        {"ok": bool, "project": str|None, "location": str|None,
         "jobs": {job_id: observation}, "error": str|None}
    """
    try:
        resolved_project = project or resolve_project()
        resolved_location = location or resolve_location()
        client = _scheduler_client(scheduler_client)
    except PlatformClockError as exc:
        logger.warning("platform_clocks: observe_refused code=%s", exc.code)
        return {
            "ok": False,
            "project": project,
            "location": location,
            "jobs": {},
            "error": f"{exc.code}: {exc}",
        }

    parent = _location_path(resolved_project, resolved_location)
    try:
        listed = client.list_jobs(request={"parent": parent})
        jobs = {}
        for job in listed:
            observation = _job_to_observation(job)
            if observation["job_id"]:
                jobs[observation["job_id"]] = observation
    except Exception as exc:  # noqa: BLE001 -- an unreadable API is `unknown`
        logger.warning(
            "platform_clocks: observe_failed parent=%s error=%s",
            parent,
            type(exc).__name__,
        )
        return {
            "ok": False,
            "project": resolved_project,
            "location": resolved_location,
            "jobs": {},
            "error": f"list_jobs_failed: {type(exc).__name__}: {exc}",
        }

    return {
        "ok": True,
        "project": resolved_project,
        "location": resolved_location,
        "jobs": jobs,
        "error": None,
    }


# ---------------------------------------------------------------------------
# compare -- the heart, and a pure function on purpose: no DB, no network, so
# the five verdicts are provable offline.
# ---------------------------------------------------------------------------

#: declared column -> (observed key, human label)
_COMPARED_FIELDS = (
    ("declared_schedule", "schedule", "schedule"),
    ("declared_timezone", "timezone", "timezone"),
    ("declared_target_path", "target_path", "target_path"),
    ("declared_http_method", "http_method", "http_method"),
    ("declared_attempt_deadline_seconds", "attempt_deadline_seconds",
     "attempt_deadline_seconds"),
)


def _compare_one(declared: dict, observed: dict | None) -> tuple[str, dict]:
    """Verdict + detail for ONE declared clock against ONE observation."""
    desired = declared.get("desired_state") or "enabled"

    if desired == "retired":
        # Retired means "must not exist". Present is the drift, and the detail
        # says what a human must do -- this module never deletes a GCP job.
        if observed is None:
            return IN_SYNC, {}
        return DRIFTED, {
            "desired_state": {"declared": "retired", "observed": observed["state"]},
            "remediation": "delete the Cloud Scheduler job by hand; "
                           "apply() never deletes",
        }

    if observed is None:
        return MISSING_IN_GCP, {}

    detail: dict = {}
    for declared_key, observed_key, label in _COMPARED_FIELDS:
        want = declared.get(declared_key)
        got = observed.get(observed_key)
        if isinstance(want, str) and isinstance(got, str):
            match = want.strip() == got.strip()
        else:
            match = want == got
        if not match:
            detail[label] = {"declared": want, "observed": got}

    observed_state = observed.get("state") or UNKNOWN
    if observed_state != desired:
        detail["state"] = {"declared": desired, "observed": observed_state}

    return (DRIFTED, detail) if detail else (IN_SYNC, {})


def compare(declared_rows, observation: dict) -> list[dict]:
    """Compare the declared registry with one observation. Corrects nothing.

    Produces one entry per declared clock plus one per GCP job that no clock
    declares. The unmanaged entries carry ``clock_name = None``: the registry
    DECLARES, and adopting a job it never declared would silently turn somebody
    else's cron into a platform commitment.

    When the observation failed, every declared clock is ``unknown`` with the
    reason, and NO unmanaged entry is produced -- we did not see the list, so we
    cannot claim anything about jobs we did not enumerate.

    A clock that binds NO job is ``unknown`` too, with ``clock_unbound`` as its
    reason. It is deliberately not ``missing_in_gcp``: "the job does not exist"
    would be a claim about a job we never identified, and it is usually false --
    the job runs, under an id this row does not carry.
    """
    declared_rows = list(declared_rows)
    if not observation.get("ok"):
        reason = observation.get("error") or "observation_failed"
        return [
            {
                "clock_name": row["clock_name"],
                "job_id": bound_job_id(row),
                "verdict": UNKNOWN,
                "detail": {},
                "observation_error": reason,
                "observed": None,
            }
            for row in declared_rows
        ]

    jobs = dict(observation.get("jobs") or {})
    # Every job the registry CLAIMS, whether or not that job was found. A job the
    # registry claims is never reported unmanaged, even when it is absent.
    claimed = bound_index(declared_rows)
    results: list[dict] = []

    for row in declared_rows:
        job_id = bound_job_id(row)
        if job_id is None:
            results.append(
                {
                    "clock_name": row["clock_name"],
                    "job_id": None,
                    "verdict": UNKNOWN,
                    "detail": {},
                    "observation_error": UNBOUND_REASON,
                    "observed": None,
                }
            )
            continue
        observed = jobs.get(job_id)
        verdict, detail = _compare_one(row, observed)
        results.append(
            {
                "clock_name": row["clock_name"],
                "job_id": job_id,
                "verdict": verdict,
                "detail": detail,
                "observation_error": None,
                "observed": observed,
            }
        )

    for job_id, observed in sorted(jobs.items()):
        if job_id in claimed:
            continue
        results.append(
            {
                "clock_name": None,
                "job_id": job_id,
                "verdict": UNMANAGED_IN_GCP,
                "detail": {
                    "schedule": {"declared": None, "observed": observed.get("schedule")},
                    "state": {"declared": None, "observed": observed.get("state")},
                },
                "observation_error": None,
                "observed": observed,
            }
        )

    return results


# ---------------------------------------------------------------------------
# reconcile -- observe + compare + record. It NEVER calls a mutating Cloud
# Scheduler method. Recording an observation is not correcting a drift.
# ---------------------------------------------------------------------------


def reconcile(conn, *, scheduler_client=None, persist: bool = True) -> dict:
    """Compare declared against observed, record the verdicts, change nothing.

    "Change nothing" is meant literally about GCP: the only Cloud Scheduler call
    this path makes is ``list_jobs``. It DOES write the observation columns of
    ``app.platform_clocks`` -- which is the opposite of correcting a drift: it
    is what makes the drift readable tomorrow, after this process is gone.

    Returns ``{"verdicts": [...], "observation_ok": bool, "counts": {...}}``.
    """
    declared_rows = list_declared(conn)
    observation = observe(scheduler_client=scheduler_client)
    verdicts = compare(declared_rows, observation)

    if persist:
        _record_observations(conn, verdicts)

    counts: dict[str, int] = {name: 0 for name in VERDICTS}
    for entry in verdicts:
        counts[entry["verdict"]] = counts.get(entry["verdict"], 0) + 1

    if counts.get(UNKNOWN):
        logger.warning(
            '{"event": "platform_clock_reconcile_unknown", "count": %d}',
            counts[UNKNOWN],
        )
    if counts.get(DRIFTED) or counts.get(MISSING_IN_GCP) or counts.get(UNMANAGED_IN_GCP):
        logger.warning(
            '{"event": "platform_clock_drift", "drifted": %d, "missing": %d,'
            ' "unmanaged": %d}',
            counts.get(DRIFTED, 0),
            counts.get(MISSING_IN_GCP, 0),
            counts.get(UNMANAGED_IN_GCP, 0),
        )

    return {
        "verdicts": verdicts,
        "observation_ok": bool(observation.get("ok")),
        "observation_error": observation.get("error"),
        "counts": counts,
        "project": observation.get("project"),
        "location": observation.get("location"),
    }


_OBSERVATION_UPDATE = """
    UPDATE app.platform_clocks
       SET observed_at = NOW(),
           observed_state = %s,
           observed_schedule = %s,
           observed_timezone = %s,
           observed_target_uri = %s,
           observed_http_method = %s,
           observed_attempt_deadline_seconds = %s,
           observed_last_attempt_at = %s,
           observed_last_attempt_status = %s,
           drift_verdict = %s,
           drift_detail = %s::jsonb,
           observation_error = %s
     WHERE clock_name = %s
"""


def _record_observations(conn, verdicts) -> int:
    """Write the OBSERVED half of each declared clock. Never the declared half.

    Unmanaged entries are skipped: they have no row, and creating one would
    make the registry declare a job nobody declared (migration 195 forbids the
    verdict on a stored row for exactly that reason).
    """
    import json  # noqa: PLC0415

    written = 0
    arm_platform_clock_access(conn)
    with conn.cursor() as cur:
        for entry in verdicts:
            if entry["clock_name"] is None:
                continue
            observed = entry.get("observed") or {}
            state = observed.get("state") if observed else None
            if entry["verdict"] == MISSING_IN_GCP:
                state = "absent"
            elif entry["verdict"] == UNKNOWN:
                state = UNKNOWN
            cur.execute(
                _OBSERVATION_UPDATE,
                (
                    state,
                    observed.get("schedule"),
                    observed.get("timezone"),
                    observed.get("target_uri"),
                    observed.get("http_method"),
                    observed.get("attempt_deadline_seconds"),
                    observed.get("last_attempt_at"),
                    observed.get("last_attempt_status"),
                    entry["verdict"],
                    json.dumps(entry["detail"] or {}),
                    entry["observation_error"],
                    entry["clock_name"],
                ),
            )
            written += 1
    conn.commit()
    return written


# ---------------------------------------------------------------------------
# apply -- the only path that writes to Cloud Scheduler. One named clock.
# ---------------------------------------------------------------------------


def _validate_clock_name(clock_name) -> str:
    """Refuse anything that is not one explicitly named clock.

    There is no "apply everything" and there must never be: a single mistaken
    call must not be able to rewrite five clocks at once. A wildcard, a blank
    or a list is a refusal, not an interpretation.
    """
    if not isinstance(clock_name, str):
        raise PlatformClockError(
            "clock_not_named", "apply requires the exact name of ONE clock"
        )
    name = clock_name.strip()
    if not name or name in {"*", "all", "ALL", "any"} or len(name) > _CLOCK_NAME_MAX:
        raise PlatformClockError(
            "clock_not_named",
            "apply requires the exact name of ONE clock; wildcards are refused",
        )
    return name


def _fetch_job(client, name: str):
    """Return the Job, or None when it does not exist. Raises on anything else.

    The distinction matters: absent is a legitimate state that ``apply`` fixes
    by creating; every other failure is an uncertainty, and an uncertainty is
    never treated as absence -- creating over a job we simply could not read
    would overwrite a configuration nobody looked at.
    """
    try:
        return client.get_job(request={"name": name})
    except Exception as exc:  # noqa: BLE001 -- classified immediately below
        if _is_not_found(exc):
            return None
        raise


def _job_payload(declared: dict, name: str) -> dict:
    """The Cloud Scheduler Job body a declaration maps to."""
    uri = f"{_target_base_url()}{declared['declared_target_path']}"
    http_target: dict = {
        "uri": uri,
        "http_method": declared.get("declared_http_method") or "POST",
    }
    secret = _internal_auth_value()
    if secret:
        # Raw value, already stripped of CR/LF: see the module docstring.
        http_target["headers"] = {"X-Internal-Auth": secret}
    return {
        "name": name,
        "schedule": declared["declared_schedule"],
        "time_zone": declared["declared_timezone"],
        "http_target": http_target,
        "attempt_deadline": {
            "seconds": int(declared.get("declared_attempt_deadline_seconds") or 600)
        },
    }


def apply(conn, *, clock_name, actor: str, scheduler_client=None) -> dict:
    """Put ONE named clock's declaration into Cloud Scheduler. Idempotent.

    Idempotent in the strong sense: when the observed job already matches the
    declaration, NO mutating call is made at all and the answer says
    ``already_in_sync`` with ``changed=False``. A "apply" that re-writes an
    identical job on every call is how an audit trail becomes unreadable.

    Never deletes. A ``retired`` declaration whose job still exists is reported
    as ``retire_requires_manual_delete``: destruction is a human act here.

    Returns ``{"ok", "clock_name", "outcome", "changed", "detail", "reason"}``.
    """
    try:
        name = _validate_clock_name(clock_name)
    except PlatformClockError as exc:
        return {
            "ok": False,
            "clock_name": None,
            "outcome": "refused",
            "changed": False,
            "detail": {},
            "reason": f"{exc.code}: {exc}",
        }

    declared = get_declared(conn, name)
    if declared is None:
        return {
            "ok": False,
            "clock_name": name,
            "outcome": "refused",
            "changed": False,
            "detail": {},
            "reason": "unknown_clock: no declaration named this clock",
        }

    try:
        job_id = job_id_for(declared)
    except PlatformClockError as exc:
        # A refusal, not an uncertainty: creating the job under an id we chose
        # for the caller would produce a SECOND job driving the same endpoint.
        return {
            "ok": False,
            "clock_name": name,
            "outcome": "refused",
            "changed": False,
            "detail": {},
            "reason": f"{exc.code}: {exc}",
        }

    try:
        project = resolve_project()
        location = resolve_location()
        client = _scheduler_client(scheduler_client)
        job_name = _job_path(project, location, job_id)
        desired = declared.get("desired_state") or "enabled"
        payload = _job_payload(declared, job_name) if desired != "retired" else None
    except PlatformClockError as exc:
        return {
            "ok": False,
            "clock_name": name,
            "outcome": UNKNOWN,
            "changed": False,
            "detail": {},
            "reason": f"{exc.code}: {exc}",
        }

    try:
        job = _fetch_job(client, job_name)
    except Exception as exc:  # noqa: BLE001 -- unreadable is `unknown`, not absent
        logger.warning(
            "platform_clocks: apply_read_failed clock=%s error=%s",
            name,
            type(exc).__name__,
        )
        return {
            "ok": False,
            "clock_name": name,
            "outcome": UNKNOWN,
            "changed": False,
            "detail": {},
            "reason": f"get_job_failed: {type(exc).__name__}: {exc}",
        }

    observed = _job_to_observation(job) if job is not None else None

    if desired == "retired":
        if observed is None:
            return _applied(name, "already_in_sync", False, actor, {})
        return {
            "ok": True,
            "clock_name": name,
            "outcome": "retire_requires_manual_delete",
            "changed": False,
            "detail": {"observed_state": observed["state"]},
            "reason": "apply never deletes a Cloud Scheduler job",
        }

    verdict, detail = _compare_one(declared, observed)
    if verdict == IN_SYNC:
        return _applied(name, "already_in_sync", False, actor, {})

    try:
        if observed is None:
            client.create_job(
                request={
                    "parent": _location_path(project, location),
                    "job": payload,
                }
            )
            outcome = "created"
            if desired == "paused":
                # create_job lands ENABLED; a declaration of `paused` therefore
                # needs the second call. Stated rather than assumed, because a
                # clock that was supposed to be paused and fires once is a
                # production event.
                client.pause_job(request={"name": job_name})
                outcome = "created_paused"
        else:
            outcome_parts = []
            config_detail = {k: v for k, v in detail.items() if k != "state"}
            if config_detail:
                client.update_job(request={"job": payload})
                outcome_parts.append("updated")
            if "state" in detail:
                if desired == "paused":
                    client.pause_job(request={"name": job_name})
                    outcome_parts.append("paused")
                else:
                    client.resume_job(request={"name": job_name})
                    outcome_parts.append("resumed")
            outcome = "+".join(outcome_parts) or "already_in_sync"
    except Exception as exc:  # noqa: BLE001 -- a half-applied change is `unknown`
        logger.warning(
            "platform_clocks: apply_failed clock=%s error=%s", name, type(exc).__name__
        )
        return {
            "ok": False,
            "clock_name": name,
            "outcome": UNKNOWN,
            "changed": False,
            "detail": detail,
            "reason": f"apply_failed: {type(exc).__name__}: {exc}",
        }

    return _applied(name, outcome, outcome != "already_in_sync", actor, detail)


def _applied(name: str, outcome: str, changed: bool, actor: str, detail: dict) -> dict:
    logger.info(
        "platform_clocks: apply clock=%s outcome=%s changed=%s actor=%s",
        name,
        outcome,
        changed,
        actor,
    )
    return {
        "ok": True,
        "clock_name": name,
        "outcome": outcome,
        "changed": changed,
        "detail": detail,
        "reason": None,
    }


# ---------------------------------------------------------------------------
# run_now -- fire one clock immediately, out of band of its cadence.
# ---------------------------------------------------------------------------


def run_now(conn, *, clock_name, actor: str, scheduler_client=None) -> dict:
    """Trigger ONE named clock now (the API form of ``gcloud ... jobs run``).

    Same naming discipline as ``apply``: a run is an invocation at platform
    scale, so the caller names exactly which one. Firing a clock does not change
    its cadence and writes nothing to the registry -- the evidence of the run is
    the invocation's own status and log line, exactly like a scheduled one.
    """
    try:
        name = _validate_clock_name(clock_name)
    except PlatformClockError as exc:
        return {
            "ok": False,
            "clock_name": None,
            "outcome": "refused",
            "reason": f"{exc.code}: {exc}",
        }

    declared = get_declared(conn, name)
    if declared is None:
        return {
            "ok": False,
            "clock_name": name,
            "outcome": "refused",
            "reason": "unknown_clock: no declaration named this clock",
        }
    if (declared.get("desired_state") or "enabled") == "retired":
        return {
            "ok": False,
            "clock_name": name,
            "outcome": "refused",
            "reason": "retired_clock: a retired clock is not fired on demand",
        }

    try:
        job_id = job_id_for(declared)
    except PlatformClockError as exc:
        # There is no job to fire, and picking one by name would fire somebody
        # else's cron on demand.
        return {
            "ok": False,
            "clock_name": name,
            "outcome": "refused",
            "reason": f"{exc.code}: {exc}",
        }

    try:
        project = resolve_project()
        location = resolve_location()
        client = _scheduler_client(scheduler_client)
    except PlatformClockError as exc:
        return {
            "ok": False,
            "clock_name": name,
            "outcome": UNKNOWN,
            "reason": f"{exc.code}: {exc}",
        }

    job_name = _job_path(project, location, job_id)
    try:
        client.run_job(request={"name": job_name})
    except Exception as exc:  # noqa: BLE001 -- a run whose outcome we cannot read
        logger.warning(
            "platform_clocks: run_now_failed clock=%s error=%s",
            name,
            type(exc).__name__,
        )
        return {
            "ok": False,
            "clock_name": name,
            "outcome": UNKNOWN,
            "reason": f"run_job_failed: {type(exc).__name__}: {exc}",
        }

    logger.info("platform_clocks: run_now clock=%s actor=%s", name, actor)
    return {"ok": True, "clock_name": name, "outcome": "triggered", "reason": None}


# ---------------------------------------------------------------------------
# THE NIGHTLY STEP LEDGER, read -- `execution-substrate.md` "Incomplete if" 2.
#
# The clock registry above answers "did the platform's heartbeat fire?". It
# cannot answer "and did the work inside that beat happen?", because
# `scheduler._run_isolated_step` deliberately never re-raises: a step that
# silently never ran left no row anywhere until migration 325.
#
# It is read HERE, next to the clocks, because it is the same question one level
# down and an operator asking it is already on this screen. Nothing below
# repairs, re-dispatches or closes anything: the same rule the registry follows.
# ---------------------------------------------------------------------------

#: A step whose row was never stamped `started_at`. THE SILENCE THE CLAUSE
#: FORBIDS, now readable: the sequence was declared at dispatch and this one
#: never began.
NEVER_STARTED = "never_started"
#: Started and never closed. A crash, an OOM, a container replaced mid-step --
#: the open row is the record, and it is NOT a failure: nothing judged it.
UNFINISHED = "unfinished"
#: A step of the declared sequence with no row at all for that night. Only
#: reachable when the dispatch-time write itself did not land, and it is a
#: distinct state because "the ledger did not record it" is not "it did not run".
UNRECORDED = "unrecorded"

STEP_STATES = ("succeeded", "failed", UNFINISHED, NEVER_STARTED, UNRECORDED)

#: `nights` MEANS NIGHTS. The inner select used to `GROUP BY run_id, as_of_date`
#: and `LIMIT` that, so the parameter bounded RUNS: a night dispatched twice --
#: the scheduled 02:00 pass and one *Run now* from the very screen this feeds --
#: served only ONE of its two runs at `nights=1`, and asking for two nights could
#: come back with two runs of the SAME night while the previous night, the one a
#: person opened the panel to check, was not in the answer at all. That is the
#: short list of green steps this whole ledger exists to refuse. Bounding the
#: DISTINCT `as_of_date` instead serves every run of the nights asked for, which
#: is what *Last night's steps* names in `execution-substrate.md`.
_RECENT_NIGHTS_SQL = """
    SELECT run_id,
           as_of_date,
           step_name,
           step_ordinal,
           declared_at,
           started_at,
           ended_at,
           outcome,
           error_class
      FROM app.nightly_step_runs
     WHERE as_of_date IN (
           SELECT as_of_date
             FROM app.nightly_step_runs
            GROUP BY as_of_date
            ORDER BY as_of_date DESC
            LIMIT %s
           )
     ORDER BY as_of_date DESC, declared_at DESC, step_ordinal ASC
"""


def _step_state(row: dict) -> str:
    if row["ended_at"] is None:
        return NEVER_STARTED if row["started_at"] is None else UNFINISHED
    return row["outcome"]


def _duration_ms(row: dict) -> int | None:
    """Derived, never stored: a value that can be computed is not a column."""
    if row["started_at"] is None or row["ended_at"] is None:
        return None
    return round((row["ended_at"] - row["started_at"]).total_seconds() * 1000)


def list_nightly_step_runs(conn, *, nights: int = 1) -> dict:
    """The last *nights* nightly runs, one entry per DECLARED step. READ ONLY.

    The declared sequence is `core.scheduler.NIGHTLY_STEPS` and it is applied
    here as well as at dispatch, on purpose: a night whose dispatch-time write
    did not land would otherwise show a SHORT list, and a short list of green
    steps is precisely the silence that reads like health. A declared step with
    no row is reported as `unrecorded` rather than omitted.

    A step the night recorded but the sequence no longer declares is kept too,
    at the end: it ran, and dropping it would rewrite what happened.
    """
    from core.scheduler import NIGHTLY_STEPS  # noqa: PLC0415 -- import cycle.

    arm_platform_clock_access(conn)
    with conn.cursor() as cur:
        cur.execute(_RECENT_NIGHTS_SQL, (max(1, int(nights)),))
        columns = [c.name for c in cur.description]
        rows = [dict(zip(columns, values, strict=True)) for values in cur.fetchall()]

    by_run: dict[str, dict] = {}
    order: list[str] = []
    for row in rows:
        run_id = row["run_id"]
        if run_id not in by_run:
            by_run[run_id] = {"run_id": run_id, "as_of_date": row["as_of_date"], "steps": {}}
            order.append(run_id)
        by_run[run_id]["steps"][row["step_name"]] = row

    runs: list[dict] = []
    for run_id in order:
        entry = by_run[run_id]
        recorded: dict = entry["steps"]
        steps: list[dict] = []
        for ordinal, step_name in enumerate(NIGHTLY_STEPS):
            row = recorded.get(step_name)
            if row is None:
                steps.append(
                    {
                        "step_name": step_name,
                        "step_ordinal": ordinal,
                        "state": UNRECORDED,
                        "started_at": None,
                        "ended_at": None,
                        "duration_ms": None,
                        "error_class": None,
                        "declared": True,
                    }
                )
                continue
            steps.append(
                {
                    "step_name": step_name,
                    "step_ordinal": row["step_ordinal"],
                    "state": _step_state(row),
                    "started_at": row["started_at"],
                    "ended_at": row["ended_at"],
                    "duration_ms": _duration_ms(row),
                    "error_class": row["error_class"],
                    "declared": True,
                }
            )
        for step_name, row in recorded.items():
            if step_name in NIGHTLY_STEPS:
                continue
            steps.append(
                {
                    "step_name": step_name,
                    "step_ordinal": row["step_ordinal"],
                    "state": _step_state(row),
                    "started_at": row["started_at"],
                    "ended_at": row["ended_at"],
                    "duration_ms": _duration_ms(row),
                    "error_class": row["error_class"],
                    # It ran under a sequence this deployment no longer declares.
                    "declared": False,
                }
            )

        counts: dict[str, int] = {}
        for step in steps:
            counts[step["state"]] = counts.get(step["state"], 0) + 1
        runs.append(
            {
                "run_id": run_id,
                "as_of_date": entry["as_of_date"],
                "steps": steps,
                "counts": counts,
                # The one summary a person acts on: everything that is not a
                # closed `succeeded`. Named rather than left to be re-derived by
                # each surface, so REST and MCP cannot count it differently.
                "unresolved": [s["step_name"] for s in steps if s["state"] != "succeeded"],
            }
        )

    return {
        "runs": runs,
        "declared_steps": list(NIGHTLY_STEPS),
        # ZERO NIGHTS IS NOT ZERO PROBLEMS. The surface must render this as its
        # own state -- the nightly has not run since the ledger existed -- and
        # never as a clean night.
        "has_run": bool(runs),
    }
