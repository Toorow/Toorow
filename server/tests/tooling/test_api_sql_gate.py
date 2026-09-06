"""Criterion 4 ratchet: the set of `*_api.py` modules that execute SQL may only shrink.

`docs/product-architecture/module-boundaries.md` says a route module *"contains
business logic rather than request parsing, authorization and a call into a
service module"*. AD-43 named the repair and delivered its first step on
2026-08-12 -- the 877 lines of service logic that were mounted by nothing left
`admin_api.py`. Step three, *a handler parses, authorizes and calls a service*,
was never done, and nothing in the repository was counting: **72 of the 103
`*_api.py` modules execute SQL** (AI-330, measured 2026-08-30).

This file does not remove them. It makes the number stop drifting. A count
somebody has to remember to re-run is a paragraph, so the check lives here,
where `make test` runs it.

Three tripwires, and the first is the one the census can fail on its own:

1. the walk must still reach the scope. A census that reads an empty tree finds
   no SQL and reports the debt cleared -- criterion 13's failure mode, applied
   to this instrument;
2. a module that executes SQL and is not frozen is refused the day it lands,
   which is the only day the repair is free;
3. a frozen module that no longer executes SQL is refused too. The ratchet turns
   ONE WAY: the recorded count falls WITH the measurement, in the same commit,
   so the baseline can never become a record of debt nobody has.

The repair for a refusal is never "add it to the list". It is AD-43's own worked
example: the statement moves into a service module the handler calls, and the
handler keeps parsing and authorization. `core/org_lifecycle.py`,
`core/connection_revocation.py` and `core/verification_prefs.py` are the three
that already made that trip.
"""

from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SCRIPTS_DIR = _REPO_ROOT / "scripts"

if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

import api_sql_census  # noqa: E402

#: Measured 2026-08-31 by `python scripts/api_sql_census.py`. The floor of the
#: population, not a target: 103 route modules were in scope, and a census that
#: finds fewer is measuring something else.
API_MODULES = api_sql_census.API_MODULES_AT_2026_08_31

#: The ceiling the ratchet enforces. It is the length of the frozen set, read
#: from the set rather than typed again -- two copies of a number drift.
SQL_EXECUTING_CEILING = len(api_sql_census.SQL_EXECUTING_API_MODULES_AT_2026_08_31)


def _census():
    return api_sql_census.census()


def test_the_census_still_reaches_every_route_module():
    """The instrument must not measure its own copy: an empty scan finds no SQL."""

    modules = _census()
    assert len(modules) >= API_MODULES, (
        f"the census scanned {len(modules)} `*_api.py` modules, {API_MODULES} "
        "were in server/core on 2026-08-31. The route modules moved or the walk "
        "broke, so every verdict below is about nothing."
    )


def test_every_api_module_is_inside_the_declared_scope():
    """A guard whose scope stops before a directory goes quiet, not red."""

    drift = api_sql_census.out_of_scope_modules()
    assert not drift, (
        "a `*_api.py` module lives outside server/core, so this census does not "
        "cover it and its silence would read as health:\n  " + "\n  ".join(drift)
    )


def test_no_new_route_module_executes_sql():
    modules = _census()
    live = api_sql_census.executing_names(modules)
    new = sorted(live - api_sql_census.SQL_EXECUTING_API_MODULES_AT_2026_08_31)
    assert len(live) <= SQL_EXECUTING_CEILING and not new, (
        f"{len(live)} `*_api.py` modules execute SQL (ceiling "
        f"{SQL_EXECUTING_CEILING}); {len(new)} are not frozen:\n  "
        + "\n  ".join(new)
        + "\nCriterion 4: a route module parses, authorizes and calls a SERVICE. "
        "Move the statement into a service module -- AD-43's first step is the "
        "worked example -- rather than adding a line to the baseline."
    )


def test_a_repaired_module_must_leave_the_baseline():
    """The ratchet turns one way: a frozen module that stopped is refused too."""

    modules = _census()
    live = api_sql_census.executing_names(modules)
    stale = sorted(api_sql_census.SQL_EXECUTING_API_MODULES_AT_2026_08_31 - live)
    assert not stale, (
        f"{len(stale)} frozen module(s) no longer execute SQL. Delete them from "
        "SQL_EXECUTING_API_MODULES_AT_2026_08_31 in scripts/api_sql_census.py "
        "in the commit that repaired them, so the recorded count falls with the "
        "measurement:\n  " + "\n  ".join(stale)
    )


def test_the_exemption_stays_bounded():
    """`admin_api.py` may hold the auth/scope seam, not a new query under its cover."""

    modules = _census()
    exempt = [m for m in modules if m.exempt]
    assert len(exempt) == 1 and exempt[0].name == api_sql_census.EXEMPT, (
        "the single exemption is admin_api.py, the assembler AD-40 left holding "
        f"the auth/scope seam; the census found {[m.name for m in exempt]}."
    )
    assert exempt[0].statements <= api_sql_census.EXEMPT_STATEMENT_CEILING, (
        f"admin_api.py holds {exempt[0].statements} statements, "
        f"{api_sql_census.EXEMPT_STATEMENT_CEILING} when it was exempted."
    )


# --------------------------------------------------------------------------- #
# The gate must be able to go RED -- in both directions, shown rather than hoped
# --------------------------------------------------------------------------- #


def test_the_gate_names_a_seventy_third_module():
    """A NEW route module that executes SQL is refused, by name."""

    modules = _census()
    intruder = api_sql_census.ApiModule(
        name="synthetic_intruder_api.py",
        path="server/core/synthetic_intruder_api.py",
        statements=3,
        lines=120,
        verbs={"select": 3},
        receivers=["cur"],
        exempt=False,
    )
    failures = api_sql_census.gate_failures((*modules, intruder))
    assert any("synthetic_intruder_api.py" in f for f in failures), (
        "the ratchet did not name a 73rd SQL-executing route module: " f"{failures}"
    )


def test_the_gate_names_a_module_that_was_repaired_without_being_decremented():
    """A frozen module whose SQL is gone is refused until its line goes too."""

    modules = _census()
    repaired = "jobs_api.py"
    assert repaired in api_sql_census.SQL_EXECUTING_API_MODULES_AT_2026_08_31
    mutated = tuple(
        replace(m, statements=0, verbs={}, receivers=[]) if m.name == repaired else m
        for m in modules
    )
    failures = api_sql_census.gate_failures(mutated)
    assert any(repaired in f and "no longer execute SQL" in f for f in failures), (
        "the ratchet did not refuse a baseline line whose module was repaired: "
        f"{failures}"
    )


def test_the_gate_refuses_a_seam_that_grew_a_query():
    """The exemption is bounded, and the bound bites."""

    modules = _census()
    mutated = tuple(
        replace(m, statements=api_sql_census.EXEMPT_STATEMENT_CEILING + 1)
        if m.exempt
        else m
        for m in modules
    )
    failures = api_sql_census.gate_failures(mutated)
    assert any(api_sql_census.EXEMPT in f and "exemption covers" in f for f in failures)


def test_the_gate_refuses_a_census_that_scanned_nothing():
    """Criterion 13, applied to this instrument: an empty walk must be red."""

    failures = api_sql_census.gate_failures(())
    assert any("about nothing" in f for f in failures), failures


def test_the_gate_refuses_an_unknown_cursor_receiver():
    """A second way of reaching the database must be declared, not silently missed."""

    modules = _census()
    mutated = tuple(
        replace(m, receivers=["bigquery_client"]) if m.name == "jobs_api.py" else m
        for m in modules
    )
    failures = api_sql_census.gate_failures(mutated)
    assert any("bigquery_client" in f for f in failures), failures


def test_the_gate_is_green_on_the_tree_as_it_stands():
    """The whole point: today's tree passes, so a red tomorrow means a change."""

    assert api_sql_census.gate_failures(_census()) == []
