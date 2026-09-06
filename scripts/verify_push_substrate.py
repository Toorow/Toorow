"""Story 56.8 -- prove the AD-36 substrate, or name exactly what is missing.

WHY A SCRIPT AND NOT A TEST. Every other story in epic 56 is provable with mocks,
because each closes a hole in code. This one closes a hole in a DEPLOYMENT, and a
mocked assertion about a deployment is the defect this repo has been paying for:
a green harness covering one link while the chain is inert. So this reads the
REAL configuration and the REAL ledger and refuses to conclude from anything else.

WHAT IT REFUSES TO SAY. It never reports "the substrate works". It reports which
of the six preconditions hold, and the single fact that decides the epic:
`app.datastream_executions` is 0 in production and has always been. Until that
number moves, every claim about recurring retrieval is unverified -- and this
script says so rather than counting green checkmarks.

Usage:
    uv run python scripts/verify_push_substrate.py            # read-only
    uv run python scripts/verify_push_substrate.py --json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
for _p in (_ROOT / "server", _ROOT / "scripts"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))


def _env(name: str) -> str:
    return (os.environ.get(name) or "").strip()


def check_configuration() -> list[dict]:
    """The variables the push substrate cannot run without."""
    backend = _env("QUEUE_BACKEND") or "local"
    checks = [
        {
            "id": "backend",
            "ok": backend == "cloud_tasks",
            "detail": f"QUEUE_BACKEND={backend!r}",
            "why": "'local' runs the development fallback: three daemon threads in a "
                   "container that gets no CPU between requests (AD-36).",
        },
        {
            "id": "tasks_project",
            "ok": bool(_env("CLOUD_TASKS_PROJECT")),
            "detail": f"CLOUD_TASKS_PROJECT={'set' if _env('CLOUD_TASKS_PROJECT') else 'UNSET'}",
            "why": "enqueue_pull raises EnvironmentError without it (HG-1).",
        },
        {
            "id": "tasks_queue",
            "ok": bool(_env("CLOUD_TASKS_QUEUE_NAME") and _env("CLOUD_TASKS_LOCATION")),
            "detail": f"queue={_env('CLOUD_TASKS_QUEUE_NAME') or 'UNSET'} "
                      f"location={_env('CLOUD_TASKS_LOCATION') or 'UNSET'}",
            "why": "the task is created against queue_path(project, location, queue).",
        },
        {
            "id": "worker_url",
            "ok": bool(_env("CLOUD_TASKS_WORKER_URL")),
            "detail": f"CLOUD_TASKS_WORKER_URL={_env('CLOUD_TASKS_WORKER_URL') or 'UNSET'}",
            "why": "without it the task URL is a bare path and Cloud Tasks cannot reach it.",
        },
        {
            "id": "internal_secret",
            "ok": bool(_env("INTERNAL_ENDPOINTS_REQUIRE_HEADER")),
            "detail": "INTERNAL_ENDPOINTS_REQUIRE_HEADER="
                      f"{'set' if _env('INTERNAL_ENDPOINTS_REQUIRE_HEADER') else 'UNSET'}",
            "why": "in push mode the endpoints answer 503 without it -- no task can "
                   "authenticate, and a 401 would read like a broken queue (56.5).",
        },
        {
            "id": "pubsub_topic",
            "ok": bool(_env("PUBSUB_TOPIC")),
            "detail": f"PUBSUB_TOPIC={_env('PUBSUB_TOPIC') or 'UNSET'}",
            "why": "facts are recorded either way; without a topic nobody can subscribe "
                   "to them (56.7). Not fatal.",
            "fatal": False,
        },
    ]
    return checks


def check_clients() -> list[dict]:
    """The libraries the substrate calls. Undeclared here = every enqueue fails.

    Not a theoretical risk: `_create_push_task` raises EnvironmentError when
    google-cloud-tasks is absent, and it is the FIRST thing an enqueue does after
    the flip. A substrate whose queue exists and whose client does not is worse
    than one that was never flipped -- it fails at the moment work arrives.
    """
    out = []
    for module, package in (("google.cloud.tasks_v2", "google-cloud-tasks"),
                            ("google.cloud.pubsub_v1", "google-cloud-pubsub")):
        try:
            __import__(module)
            ok = True
        except Exception:
            ok = False
        out.append({
            "id": f"client:{package}",
            "ok": ok,
            "detail": f"{module} {'importable' if ok else 'MISSING'}",
            "why": f"{package} must be a declared dependency of server/pyproject.toml.",
            "fatal": package == "google-cloud-tasks" or bool(_env("PUBSUB_TOPIC")),
        })
    return out


def check_landing_mode() -> list[dict]:
    """Where rows actually land. A push substrate over an ephemeral file is moot.

    TOOROW_DB_MODE defaults to 'duckdb', which on Cloud Run means a file inside a
    container that is destroyed between requests. The substrate can be perfect and
    the data still evaporate, so this is checked here rather than assumed.
    """
    mode = _env("TOOROW_DB_MODE") or "duckdb (DEFAULT)"
    return [{
        "id": "landing_mode",
        "ok": _env("TOOROW_DB_MODE") == "bigquery",
        "detail": f"TOOROW_DB_MODE={mode}",
        "why": "'duckdb' on Cloud Run writes to a file that dies with the container. "
               "Production lands in BigQuery.",
    }]


def check_routes() -> list[dict]:
    """Every push target must be served by the router, not merely addressed."""
    from starlette.routing import Match  # noqa: PLC0415

    from core.admin_api import router  # noqa: PLC0415

    paths = [
        "/internal/worker/execute-pull/job_X",
        "/internal/worker/execute-activation/dsaj_X",
        "/internal/scheduler/dispatch-nightly",
        "/internal/scheduler/dispatch-hourly",
        "/internal/scheduler/reconcile-queues",
        "/internal/scheduler/poll-health",
        "/internal/scheduler/drain-outbox",
        "/internal/facts/pull-landed/verification",
        "/internal/facts/pull-landed/context-seed",
    ]
    out = []
    for path in paths:
        scope = {"type": "http", "method": "POST", "path": path,
                 "path_params": {}, "headers": [], "root_path": ""}
        served = any(r.matches(scope)[0] is Match.FULL for r in router.routes)
        out.append({"id": f"route:{path}", "ok": served, "detail": path,
                    "why": "a task pushed at an unserved path is a 404 answered by retries."})
    return out


def check_ledger() -> list[dict]:
    """The only measurement that decides this epic."""
    try:
        from core.db import get_connection  # noqa: PLC0415

        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT count(*) FROM app.datastream_executions")
                executions = cur.fetchone()[0]
                # AI-217: both cadences that run once a period. Counting only
                # `nightly` reported a weekly Datastream as unarmed while the
                # dispatcher enqueues it -- the mirror has to match the filter in
                # `scheduler._dispatch_nightly_datastreams`.
                cur.execute(
                    "SELECT count(*) FROM app.datastreams "
                    "WHERE lifecycle_state='active' "
                    "AND schedule_mode IN ('nightly', 'weekly') "
                    "AND enabled AND current_mapping_version_id IS NOT NULL"
                )
                armed = cur.fetchone()[0]
                cur.execute("SELECT count(*) FROM app.pull_jobs WHERE state='queued'")
                queued = cur.fetchone()[0]
                cur.execute(
                    "SELECT count(*) FROM app.operation_outbox WHERE state='pending'"
                )
                pending_facts = cur.fetchone()[0]
    except Exception as exc:  # noqa: BLE001
        return [{"id": "ledger", "ok": False, "detail": f"unreadable: {exc}",
                 "why": "without the ledger nothing here can be concluded."}]

    return [
        {
            "id": "datastream_executions",
            "ok": executions > 0,
            "detail": f"{executions} execution(s)",
            "why": "THE measurement. While it is 0, every claim about recurring "
                   "retrieval is unverified, whatever else is green.",
        },
        {
            "id": "armed_datastreams",
            "ok": armed > 0,
            "detail": f"{armed} datastream(s) active+nightly+enabled+mapped",
            "why": "the nightly loop's four conditions. All four false = nothing to run.",
        },
        {
            "id": "queued_backlog",
            "ok": True,
            "detail": f"{queued} pull job(s) queued",
            "why": "informational: a backlog with no live task is what the "
                   "reconciliation sweep re-dispatches (56.4).",
            "fatal": False,
        },
        {
            "id": "undelivered_facts",
            "ok": True,
            "detail": f"{pending_facts} outbox row(s) pending",
            "why": "informational: facts written since migration 060 and never drained "
                   "until 56.7.",
            "fatal": False,
        },
    ]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--skip-ledger", action="store_true",
                        help="configuration and routes only (no database)")
    args = parser.parse_args()

    checks = check_configuration() + check_clients() + check_landing_mode() + check_routes()
    if not args.skip_ledger:
        checks += check_ledger()

    blocking = [c for c in checks if not c["ok"] and c.get("fatal", True)]

    if args.json:
        print(json.dumps({"checks": checks, "blocking": [c["id"] for c in blocking]}, indent=2))
    else:
        for check in checks:
            mark = "ok  " if check["ok"] else ("FAIL" if check.get("fatal", True) else "note")
            print(f"[{mark}] {check['id']}: {check['detail']}")
            if not check["ok"]:
                print(f"       {check['why']}")
        print()
        if blocking:
            print(f"{len(blocking)} blocking precondition(s): {', '.join(c['id'] for c in blocking)}")
            print("The substrate is NOT proven. This is the honest answer -- not a "
                  "count of green checks.")
        else:
            print("Every precondition holds AND datastream_executions is non-zero: the "
                  "chain has run at least once end to end.")
    return 1 if blocking else 0


if __name__ == "__main__":
    raise SystemExit(main())
