"""A profile's declared landing is the one its pull writes to -- AI-311.

WHY A SECOND GUARD BESIDE `test_profile_relations_declared.py`, AND NOT A LINE
INSIDE IT. That file's second refusal is "a `raw_relation` no `connector.py` of
that module creates". It asks whether the MODULE creates the relation named. This
one asks whether the PROFILE writes to it. For the 32 modules that create exactly
one raw relation the two questions have the same answer; for the 7 that create
several, they do not -- and one of them was wrong.

The instance, found under AI-310: `youtube-analytics` declared `channel_snapshot`
on `raw_youtube_breakdown`, a relation the module does create, while
`pull_channel_snapshot` calls `_insert_raw_rows`, which writes
`raw_youtube_daily`. `report profiles carrying the pair : 140/140`, before the
repair and after it. A guard that is green on the defect it was written to catch
is the thing this file exists to stop happening twice.

WHAT IT REFUSES, AND WHAT IT REFUSES TO CLAIM. Only a PROVEN disagreement fails:
a callable traced to exactly one relation that is not the one declared. A trace
that cannot answer -- a landing helper writing several relations with the target
passed in, a profile naming no callable -- is counted and printed, never judged.
The coverage is printed on every run for the same reason the evals summary prints
"not measured": a guard whose reach shrinks in silence is a guard that stops
guarding without anybody reading a red line.
"""

from __future__ import annotations

import json
from pathlib import Path

from core.profile_landing_trace import (
    traced_profile_landings,
    wrong_addresses,
)


def test_no_profile_declares_a_landing_its_pull_does_not_write(capsys) -> None:
    records = traced_profile_landings()
    traced = [record for record in records if record["traced_relation"]]
    wrong = wrong_addresses()

    with capsys.disabled():
        print()
        print(
            f"  profile landings traced from the pull : {len(traced)}/{len(records)}"
            f"  (wrong addresses: {len(wrong)})"
        )

    assert not wrong, (
        "report profile(s) declaring a landing their own pull does not write:\n  "
        + "\n  ".join(
            f"{record['connector']}/{record['report_profile_id']}: declares "
            f"{record['declared_relation']}, {record['callable']} writes "
            f"{record['traced_relation']}"
            for record in wrong
        )
        + "\n\nThe declaration is the address a reader is served from. Point it at "
        "the relation the pull writes, or change where the pull writes -- but the "
        "two have to be the same relation, or somebody is served another profile's "
        "rows under this one's name."
    )


def test_the_reach_of_this_guard_is_stated_rather_than_assumed(capsys) -> None:
    """How many profiles it can speak for, and why the rest are silent.

    Not a threshold. A number that only goes up is a ratchet somebody has to
    maintain; what matters here is that the silence is COUNTED and named, so a
    change that halves the reach is visible in the output of the run that made
    it rather than in an incident three weeks later.
    """
    records = traced_profile_landings()
    undetermined: dict[str, int] = {}
    for record in records:
        reason = record["undetermined_reason"]
        if reason:
            undetermined[reason] = undetermined.get(reason, 0) + 1

    with capsys.disabled():
        print("  undetermined, by cause :")
        for reason, count in sorted(undetermined.items()):
            print(f"      {count:3d}  {reason}")

    assert records, "no report profile was read at all -- the sweep found nothing"
    # Every silence carries one of the declared causes: an unnamed one would be a
    # profile this module dropped without saying so.
    assert set(undetermined) <= {
        "callable_not_declared",
        "callable_absent_from_module",
        "no_landing_reached",
        "several_landings_reached",
        "non_warehouse_landing",
    }


def test_a_profile_landing_outside_the_warehouse_is_not_a_wrong_address() -> None:
    """`raw_relation: null` is a decision, and this guard must not read it as a bug.

    The event profiles land in `app.context_events`, in Postgres. The trace says so
    as an ANSWER (`non_warehouse_landing`), not as a failure to look, and the
    comparison against a declared relation never happens.

    THIS TEST FAILED ON ITS FIRST RUN, AND THE GUARD WAS THE THING THAT WAS WRONG.
    It reported `brevo/transactional_events` as declaring `null` while writing
    `raw_brevo_daily`. Read: that callable reaches `_pull_profile`, a DISPATCHER,
    and the branch its own profile takes is `_pull_transactional_events_to_context`
    -- the docstring of which says in full "Landing: app.context_events (Postgres)
    via core.context_events, NOT raw_brevo_daily". Collecting only `raw_*` names
    saw one relation and answered with a confidence it had no right to. A tracer
    that reaches a branch point must say `several_landings_reached`.
    """
    records = traced_profile_landings()
    declared_null = [
        record
        for record in records
        if record["declared_relation"] is None and record["traced_relation"]
    ]
    assert not declared_null, (
        "profile(s) declaring no warehouse landing while their pull writes one:\n  "
        + "\n  ".join(
            f"{record['connector']}/{record['report_profile_id']} writes "
            f"{record['traced_relation']}"
            for record in declared_null
        )
    )


# ---------------------------------------------------------------------------
# TEETH -- the proof this file is not green by inertia.
#
# `test_profile_relations_declared.py` read `140/140` straight through the defect
# this guard exists for, so "it passes on the repository" is not evidence. A
# module of two files is written TWICE, one word apart: once declaring the
# relation its pull writes, once declaring another. Only the second may be named.
# ---------------------------------------------------------------------------

#: A connector whose landing is one `INSERT INTO` reached through one helper --
#: the shape every module in this repository uses, reduced to what is traced.
_PROBE_CONNECTOR = (
    '_LANDING_SQL = "INSERT INTO raw_probe_daily (day, rows) VALUES (?, ?)"\n'
    "\n\n"
    "def _land(sql, rows):\n"
    "    return len(rows)\n"
    "\n\n"
    "def pull_daily(**kwargs):\n"
    "    return _land(_LANDING_SQL, [])\n"
)


def _probe_tree(root: Path, declared_relation: str) -> str:
    """One connector declaring *declared_relation*, whose pull writes `raw_probe_daily`."""
    module = root / "modules" / "probe"
    module.mkdir(parents=True, exist_ok=True)
    (module / "connector.py").write_text(_PROBE_CONNECTOR, encoding="utf-8")
    (module / "manifest.json").write_text(
        json.dumps(
            {
                "name": "probe",
                "report_profiles": [
                    {
                        "id": "daily",
                        "raw_relation": declared_relation,
                        "staging_relation": None,
                    }
                ],
                "source_capabilities": {
                    "reports": [{"id": "daily", "dispatch": {"callable": "pull_daily"}}]
                },
            }
        ),
        encoding="utf-8",
    )
    return str(root / "modules")


def test_the_guard_goes_red_on_a_declaration_that_is_deliberately_wrong(tmp_path) -> None:
    """A wrong address is NAMED, and a right one is left alone.

    Both directions in one test on purpose: a guard that reports everything is as
    useless as one that reports nothing, and only the pair distinguishes them.
    The message has to carry BOTH relations, or it does not say what to change.
    """
    honest = _probe_tree(tmp_path / "honest", "raw_probe_daily")
    assert wrong_addresses(honest) == [], (
        "a declaration pointing at the relation its own pull writes was reported "
        "as wrong -- then it is the trace that is broken, not the manifest"
    )

    mutated = _probe_tree(tmp_path / "mutated", "raw_probe_other_daily")
    caught = wrong_addresses(mutated)
    assert len(caught) == 1, caught
    assert caught[0]["declared_relation"] == "raw_probe_other_daily"
    assert caught[0]["traced_relation"] == "raw_probe_daily"
    assert caught[0]["callable"] == "pull_daily"
