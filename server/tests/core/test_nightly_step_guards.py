"""A nightly step that is OFF by default must say so at the deployment -- 2026-08-08.

WHY THIS EXISTS. Measured that day: the deployed service carried 36 environment
variables and not one of them was a step guard, so six of the eleven steps of
`run_nightly_steps` had never run. Not by a decision -- by an
`os.environ.get(..., "false")` written years apart across 2400 lines. "Off because
nobody ever said on" reads exactly like "off on purpose", and nothing in the
repository could tell them apart.

The repair was in the code: every step is DATA-DRIVEN -- with no Datastream, no
threshold and no notebook, each reads an empty set and does nothing -- so a guard
that must be armed at install protects against nothing and costs a chance to
forget. Those defaults are now "true".

What is left to police is the exception: a step deliberately OFF. That is a real
decision, it has a reason, and the reason belongs where someone will read it. So
the rule this file enforces is narrow and permanent:

    a guard whose code default is "false" must be named in deploy.sh.

A guard defaulting to "true" needs no declaration -- there is nothing to remember.
"""

from __future__ import annotations

import re
from pathlib import Path

_REPO = Path(__file__).resolve().parents[3]
_SCHEDULER = _REPO / "server" / "core" / "scheduler.py"
_DEPLOY = _REPO / "infra" / "scripts" / "deploy.sh"

# Guards read by the scheduler that are NOT nightly-step switches. Each is
# excluded for a stated reason -- an unexplained exclusion is how the list rots.
_NOT_STEP_GUARDS = {
    # The in-process daemon thread, and what keeps the scheduler out of CI
    # (HG-5). Deliberately off: the clocks are external (AD-36) and at
    # --min-instances=0 no thread can run between two requests.
    "SCHEDULER_ENABLED",
    # Belongs to that same in-process loop; the external `dispatch-hourly` job is
    # what drives the frequent tick in a managed deploy.
    "SCHEDULER_HOURLY_ENABLED",
    # An hourly step whose schedules live per row in
    # `app.managed_feed_sync_schedule`; it is armed by data, not by a deployment.
    "MANAGED_FEED_SYNC_ENABLED",
    # Gates the mirror sync inside dispatch, not a step of `run_nightly_steps`.
    "SYNC_ENABLED",
}

_GUARD = re.compile(r'environ\.get\(\s*"([A-Z0-9_]*ENABLED)"\s*,\s*"(true|false)"')


def _step_guards() -> dict[str, str]:
    """{guard name: its code default} for every nightly-step guard."""
    found = dict(_GUARD.findall(_SCHEDULER.read_text(encoding="utf-8")))
    return {name: default for name, default in found.items() if name not in _NOT_STEP_GUARDS}


def _declared_at_deploy() -> set[str]:
    return set(
        re.findall(
            r"([A-Z0-9_]*ENABLED)=(?:true|false)",
            _DEPLOY.read_text(encoding="utf-8"),
        )
    )


def test_a_step_that_is_off_by_default_is_named_at_the_deployment():
    guards = _step_guards()
    assert guards, "the regex stopped matching the scheduler -- fix the test, not the list"

    off_by_default = {name for name, default in guards.items() if default == "false"}
    undeclared = sorted(off_by_default - _declared_at_deploy())

    assert not undeclared, (
        f"these nightly steps default to OFF and deploy.sh never names them: {undeclared}. "
        "A step nobody arms is a step nobody runs, and nothing says so. Either give it a "
        "default of `true` -- correct whenever the step is data-driven and no-ops on an "
        "empty platform -- or name it in --update-env-vars with the reason it stays off."
    )


def test_no_step_has_to_be_armed_at_install():
    """A fresh install configures nothing. Every off default owes a reason.

    Not a style rule: the whole class of defect this file exists for is a step
    that only runs if someone remembered. The cache and the schema profiler both
    read a DuckDB file that does not survive `--min-instances=0`, so neither can
    run here at all. DBT_NIGHTLY_ENABLED is the third, and its reason is now
    NARROWER than the other two (2026-08-22): the code default stays "false"
    because a self-hosted image is not required to carry dbt, but THIS
    deployment's image carries it since 2026-08-17 and `deploy.sh` therefore
    arms the step with `DBT_NIGHTLY_ENABLED=true`. Nobody arms it by hand -- a
    hand-armed flag was silently reset by the next deploy of this same script,
    four nightlies in a row.
    """
    off_by_default = sorted(
        name for name, default in _step_guards().items() if default == "false"
    )
    assert off_by_default == [
        "DBT_NIGHTLY_ENABLED",
        "SCHEMA_CONTEXT_ENABLED",
        "TOOROW_CACHE_ENABLED",
    ], (
        "the set of steps that must be armed by hand changed: "
        f"{off_by_default}. Adding one means a fresh install now has something to "
        "remember -- say why in deploy.sh and here, or make the step data-driven."
    )


def test_the_deployment_declares_no_guard_the_scheduler_ignores():
    """The list must not rot in the other direction either.

    A guard declared at the deployment and read by nobody is a promise the
    platform does not keep -- the same failure, mirrored.
    """
    orphans = sorted(_declared_at_deploy() - set(_step_guards()) - _NOT_STEP_GUARDS)
    assert not orphans, (
        f"deploy.sh declares guards the scheduler never reads: {orphans}. "
        "Either the step was removed, or the name is misspelled."
    )
