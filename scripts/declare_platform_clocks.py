"""AI-117 -- declare this deployment's platform clocks into the registry.

WHY THIS SCRIPT EXISTS. `app.platform_clocks` (migration 195) and
`core/platform_clocks.py` shipped together, and they shipped EMPTY:
`list_declared` returns `[]` until somebody calls `declare()`. An empty registry
is the worst of the three possible states -- worse than having no registry at
all -- because the screen then renders zero rows, zero drift and zero error,
which reads exactly like a platform whose clocks are all in sync. This script is
what fills it.

SEVEN CLOCKS, AND TWO OF THEM ARE HOLES THE REGISTRY MADE VISIBLE. Five were
provisioned by `infra/gcp/provision_ad36_substrate.sh` (dispatch-nightly,
dispatch-hourly, reconcile-queues, poll-health, drain-outbox). The other two
were measured on 2026-08-02:

  * `run-dq-monitors` -- the endpoint is SERVED by `core/admin_api.py` and has
    NO Cloud Scheduler job at all. So it has never run, and nothing anywhere
    said so: every automatic quality alert on the platform, including the
    arrival monitor that tells an operator a delivered file did not come
    (AI-113), was emitted by nobody.
  * `reconcile-clocks` -- the clock that watches the clocks. Without it,
    `observed_*` is never refreshed and the screen shows an observation that
    ages in silence, which is precisely the defect the registry exists to end.

IDEMPOTENCE, AND WHY IT IS NOT "CALL declare() EVERY TIME".
`platform_clocks.declare()` is an UPSERT. Re-running it unconditionally would
overwrite `declared_schedule` with the repository's value, so a human who had
edited a cadence through `PATCH /api/platform/clocks/{name}` would find the edit
gone -- with no trace that it had ever existed. That is exactly the destruction
the registry forbids `reconcile()` from performing against Cloud Scheduler,
applied to the other half of the pair. This script therefore refuses it too:

  * a clock with NO row is declared;
  * a clock that already HAS a row is left alone -- and when its stored
    declaration differs from this file's, the difference is PRINTED rather than
    resolved. A divergence between the repository and the registry is a
    reconciliation for a human to make, not a value for a script to pick;
  * `--force <clock-name>` re-imposes this file's declaration on ONE explicitly
    named clock. There is no `--force all`, for the reason
    `platform_clocks._validate_clock_name` already states: a single mistaken
    call must never be able to rewrite seven clocks at once.

Consequence worth stating: this script is NOT how a cadence is changed. Editing
a schedule below and re-running does nothing, on purpose. Changing a cadence is
either `--force` on that one clock, or the PATCH route -- both of which leave a
named actor behind.

DECLARING A CLOCK CHANGES NOTHING IN CLOUD SCHEDULER. A newly declared clock
reads `missing_in_gcp` until either `provision_ad36_substrate.sh` creates the
job or somebody POSTs `/api/platform/clocks/{name}/apply`. That gap is the
design: a declaration and a running job are two facts, and this script only ever
writes the first one.

No production identifier appears here. The GCP project, the region and the job
prefix are environment facts read by `core/platform_clocks.py`; this file holds
only canonical short names, cron expressions and internal paths.

Usage:
    uv run python scripts/declare_platform_clocks.py            # apply
    uv run python scripts/declare_platform_clocks.py --dry-run
    uv run python scripts/declare_platform_clocks.py --json
    uv run python scripts/declare_platform_clocks.py --force reconcile-clocks
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# Anchored on this file, never on the current directory: a path relative to the
# working directory yields a different answer depending on where the script was
# launched from (the AI-136 defect).
_ROOT = Path(__file__).resolve().parents[1]
for _path in (_ROOT / "server",):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

#: Europe/Paris everywhere, matching `SCHEDULER_TIMEZONE`'s default and the
#: `--time-zone` the provisioner passes. Two clocks in two zones would make
#: "02:00" mean two different instants on the same platform.
DEFAULT_TIMEZONE = "Europe/Paris"

#: The Cloud Scheduler attempt deadline the provisioner already uses. Kept in
#: one place so the declared value and the created job cannot drift by a typo.
DEFAULT_ATTEMPT_DEADLINE_SECONDS = 600

#: Written to the log line `declare()` emits. Names the SCRIPT, not a person:
#: attributing a bulk declaration to a human would be a lie in an audit trail.
ACTOR = "script:declare_platform_clocks"


@dataclass(frozen=True)
class ClockDeclaration:
    """One row this repository intends `app.platform_clocks` to hold."""

    clock_name: str
    schedule: str
    target_path: str
    purpose: str
    timezone: str = DEFAULT_TIMEZONE
    http_method: str = "POST"
    attempt_deadline_seconds: int = DEFAULT_ATTEMPT_DEADLINE_SECONDS
    desired_state: str = "enabled"


#: THE SEVEN. Every entry must have a counterpart `create_job` line in
#: `infra/gcp/provision_ad36_substrate.sh` -- a declaration with no job is a
#: clock that reads `missing_in_gcp` forever, and a job with no declaration is
#: reported `unmanaged_in_gcp`. The test suite diffs the two lists.
DECLARATIONS: tuple[ClockDeclaration, ...] = (
    ClockDeclaration(
        clock_name="dispatch-nightly",
        schedule="0 2 * * *",
        target_path="/internal/scheduler/dispatch-nightly",
        purpose=(
            "Walk the ledger once a night and enqueue every armed Datastream. "
            "The single tick the whole recurring-retrieval chain hangs from."
        ),
    ),
    ClockDeclaration(
        clock_name="dispatch-hourly",
        schedule="0 * * * *",
        target_path="/internal/scheduler/dispatch-hourly",
        purpose=(
            "The hourly branch of the same loop, for Datastreams whose cadence "
            "is finer than a day."
        ),
    ),
    ClockDeclaration(
        clock_name="reconcile-queues",
        schedule="*/10 * * * *",
        target_path="/internal/scheduler/reconcile-queues",
        purpose=(
            "Re-dispatch work the ledger shows pending with no live task. "
            "It re-dispatches and never executes: the line between a "
            "reconciliation and a second worker."
        ),
    ),
    ClockDeclaration(
        clock_name="poll-health",
        schedule="0 6 * * *",
        target_path="/internal/scheduler/poll-health",
        purpose=(
            "Daily backstop behind the event-driven health refresh: catches a "
            "token revoked or a scope withdrawn at the provider, which no flow "
            "of ours can observe."
        ),
    ),
    ClockDeclaration(
        clock_name="drain-outbox",
        schedule="*/5 * * * *",
        target_path="/internal/scheduler/drain-outbox",
        purpose=(
            "Take the facts OUT of the outbox they are written into with the "
            "mutation they describe. Nothing else delivers them."
        ),
    ),
    # ── The two that had no job at all ───────────────────────────────────────
    ClockDeclaration(
        clock_name="run-dq-monitors",
        # WHY */15. The tick carries no due-ness of its own: `run_dq_monitors`
        # re-reads the ledger and each monitor decides. So the only thing this
        # period sets is the RESOLUTION of lateness detection -- the arrival
        # monitor compares elapsed time against `expected_interval_minutes * 2`
        # (core/dq_monitors.py), and a deadline can only be noticed on the next
        # tick. At */15 an alert is at most a quarter-hour late; at "0 * * * *"
        # a feed expected every 15 minutes would be reported an hour after it
        # was already twice overdue. Not finer, because a sweep evaluates five
        # monitors across every enabled Datastream of every project: it is the
        # most expensive of the seven, unlike drain-outbox (*/5), which reads
        # one small table. 96 invocations a day is nothing against the cost
        # posture; 288 evaluations of the whole ledger would not be.
        schedule="*/15 * * * *",
        target_path="/internal/scheduler/run-dq-monitors",
        purpose=(
            "Evaluate the five data-quality monitors across every enabled "
            "Datastream. Had NO Cloud Scheduler job until 2026-08-02, so every "
            "automatic quality alert on the platform was emitted by nobody."
        ),
    ),
    ClockDeclaration(
        clock_name="reconcile-clocks",
        # WHY HOURLY, AND WHY MINUTE 17. Hourly because this clock only has to
        # be fresh enough that a hand-edit, a paused job or a job missing from
        # a new environment is noticed before it costs a day -- not fresh enough
        # to catch it within the minute. It is also the cheapest of the seven:
        # one `list_jobs` plus seven single-row UPDATEs.
        # Minute 17 rather than 0 because every other clock fires on a minute
        # divisible by 5 (0, */5, */10, */15, and both dailies on the hour). An
        # observation taken on those minutes samples Cloud Scheduler while it is
        # dispatching, so `observed_state` and `last_attempt_status` would be
        # read mid-flight and a perfectly healthy job could be recorded in a
        # transient state. 17 is coprime with 5, so this tick never lands on
        # another clock's.
        schedule="17 * * * *",
        target_path="/internal/scheduler/reconcile-clocks",
        purpose=(
            "The clock that watches the clocks: observe Cloud Scheduler, record "
            "the drift verdict of every declared clock, correct nothing. Without "
            "it the observed half of this registry ages in silence."
        ),
    ),
)

#: (this file's field, registry column). `purpose` is deliberately absent: it is
#: prose, and a reworded sentence is not a drift worth reporting.
_COMPARED_FIELDS: tuple[tuple[str, str], ...] = (
    ("schedule", "declared_schedule"),
    ("timezone", "declared_timezone"),
    ("target_path", "declared_target_path"),
    ("http_method", "declared_http_method"),
    ("attempt_deadline_seconds", "declared_attempt_deadline_seconds"),
    ("desired_state", "desired_state"),
)

DECLARED = "declared"
REDECLARED = "redeclared"
KEPT = "kept"
KEPT_DIVERGENT = "kept_divergent"
FAILED = "failed"


def clock_names() -> tuple[str, ...]:
    """The seven canonical short names, in declaration order."""
    return tuple(entry.clock_name for entry in DECLARATIONS)


def _divergence(entry: ClockDeclaration, row: dict) -> dict[str, dict]:
    """Fields where the stored declaration differs from this file's.

    Same shape as `platform_clocks.compare`'s detail, on purpose: a reader who
    has seen one of the two has seen both.
    """
    detail: dict[str, dict] = {}
    for field_name, column in _COMPARED_FIELDS:
        want = getattr(entry, field_name)
        got = row.get(column)
        if isinstance(want, str) and isinstance(got, str):
            same = want.strip() == got.strip()
        else:
            same = want == got
        if not same:
            detail[field_name] = {"repository": want, "registry": got}
    return detail


def plan(conn, *, force: tuple[str, ...] = ()) -> list[dict[str, Any]]:
    """Decide, per clock, what a run would do. Reads the registry, writes nothing.

    Separated from `populate` so the decision is provable without a database
    write, and so `--dry-run` and the real run cannot disagree about what was
    about to happen.
    """
    from core.platform_clocks import list_declared  # noqa: PLC0415

    existing = {row["clock_name"]: row for row in list_declared(conn)}
    forced = set(force)

    decisions: list[dict[str, Any]] = []
    for entry in DECLARATIONS:
        row = existing.get(entry.clock_name)
        if row is None:
            action, detail = DECLARED, {}
        elif entry.clock_name in forced:
            action, detail = REDECLARED, _divergence(entry, row)
        else:
            detail = _divergence(entry, row)
            action = KEPT_DIVERGENT if detail else KEPT
        decisions.append(
            {
                "clock_name": entry.clock_name,
                "action": action,
                "divergence": detail,
                "schedule": entry.schedule,
                "target_path": entry.target_path,
            }
        )
    return decisions


def populate(
    conn, *, force: tuple[str, ...] = (), dry_run: bool = False
) -> dict[str, Any]:
    """Declare the missing clocks. Never overwrites an existing declaration.

    The only writes are `declare()` calls for clocks that have NO row, plus the
    clocks explicitly named in *force*. Everything else is reported and left
    exactly as it is -- see the module docstring for why.
    """
    from core.platform_clocks import declare  # noqa: PLC0415

    decisions = plan(conn, force=force)

    for decision in decisions:
        if decision["action"] not in (DECLARED, REDECLARED) or dry_run:
            continue
        entry = next(
            e for e in DECLARATIONS if e.clock_name == decision["clock_name"]
        )
        outcome = declare(
            conn,
            clock_name=entry.clock_name,
            schedule=entry.schedule,
            timezone=entry.timezone,
            target_path=entry.target_path,
            purpose=entry.purpose,
            actor=ACTOR,
            desired_state=entry.desired_state,
            http_method=entry.http_method,
            attempt_deadline_seconds=entry.attempt_deadline_seconds,
        )
        if not outcome.get("ok"):
            decision["action"] = FAILED
            decision["reason"] = outcome.get("reason")

    counts: dict[str, int] = {}
    for decision in decisions:
        counts[decision["action"]] = counts.get(decision["action"], 0) + 1

    return {
        "decisions": decisions,
        "counts": counts,
        "dry_run": dry_run,
        "forced": sorted(force),
        # Stated in the payload rather than only in a docstring: whoever reads
        # this must be able to see that no Cloud Scheduler job was touched.
        "cloud_scheduler_untouched": True,
    }


def _render(report: dict[str, Any]) -> None:
    prefix = "would " if report["dry_run"] else ""
    for decision in report["decisions"]:
        action = decision["action"]
        if action in (DECLARED, REDECLARED):
            print(f"[{prefix}{action}] {decision['clock_name']} -- "
                  f"{decision['schedule']} {decision['target_path']}")
        elif action == KEPT:
            print(f"[kept]     {decision['clock_name']} -- already declared, "
                  "identical to this repository")
        elif action == KEPT_DIVERGENT:
            print(f"[DIVERGES] {decision['clock_name']} -- left untouched; the "
                  "registry and this repository disagree:")
            for field_name, pair in decision["divergence"].items():
                print(f"           {field_name}: repository={pair['repository']!r} "
                      f"registry={pair['registry']!r}")
        else:
            print(f"[FAILED]   {decision['clock_name']} -- "
                  f"{decision.get('reason')}")

    print()
    print("counts: " + ", ".join(
        f"{name}={count}" for name, count in sorted(report["counts"].items())
    ))
    if report["counts"].get(KEPT_DIVERGENT):
        print(
            "A divergence is a reconciliation for a human, not a value for this "
            "script to pick. Either edit the declaration in the product (PATCH "
            "/api/platform/clocks/{name}) or re-impose this repository's value "
            "on ONE named clock with --force <clock-name>."
        )
    if report["counts"].get(DECLARED) or report["counts"].get(REDECLARED):
        print(
            "Declared clocks read `missing_in_gcp` until the job exists. Create "
            "it with `bash infra/gcp/provision_ad36_substrate.sh`, or POST "
            "/api/platform/clocks/{name}/apply."
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="decide and print, write nothing",
    )
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument(
        "--force",
        action="append",
        metavar="CLOCK_NAME",
        default=[],
        help=(
            "re-impose this repository's declaration on ONE named clock, "
            "destroying whatever the registry holds for it. Repeatable, one "
            "name at a time; a wildcard is refused."
        ),
    )
    args = parser.parse_args(argv)

    known = set(clock_names())
    unknown = [name for name in args.force if name not in known]
    if unknown:
        # Refuses '*', 'all' and a typo by the same rule: --force names a clock
        # this file declares, or it names nothing.
        print(
            "--force names ONE declared clock at a time; refused: "
            + ", ".join(repr(name) for name in unknown),
            file=sys.stderr,
        )
        print("declared clocks: " + ", ".join(sorted(known)), file=sys.stderr)
        return 2

    from core.db import get_connection  # noqa: PLC0415

    with get_connection() as conn:
        report = populate(conn, force=tuple(args.force), dry_run=args.dry_run)

    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        _render(report)

    return 1 if report["counts"].get(FAILED) else 0


if __name__ == "__main__":
    raise SystemExit(main())
