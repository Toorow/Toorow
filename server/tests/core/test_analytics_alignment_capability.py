"""Story 70.3 -- the SEVENTH capability: declared everywhere, and its compiler.

Two things are proved here, and they are different failures.

THE DECLARATION. A capability key lives in six tables that must agree
-- `CAPABILITY_SPECS`, `PROJECT_CAPABILITY_KEYS`, `CAPABILITY_AVAILABILITY`,
`GOVERNANCE_SECTION_BY_CAPABILITY`, the Workbench tab map and the two label
tables. Story 61.5 measured what a hole costs: `governance_owner_reference`
RAISES on a key it does not know and is called for every key of
`CAPABILITY_SPECS` on every Datastream, so one missing entry takes down the
Workbench projection of every Datastream on every Project. This file walks the
key across all of them instead of trusting six edits.

THE COMPILER. Off adds nothing -- and that is checkable, not promised: the three
columns are named in NO payload a disabled capability produces. On, every unmet
dependency is a blocker with the gesture that meets it.

No real identifier: `proj_EXAMPLE`, `ds_EXAMPLE_*`, `owner@example.com`.
"""

from __future__ import annotations

import pytest
from core.analytics_alignment import (
    ADDED_COLUMNS,
    ANALYTICS_ALIGNMENT_CAPABILITY_KEY,
    DEPENDENCY_APPROVED_RELATIONSHIP,
    DEPENDENCY_COMMON_KEY,
    DEPENDENCY_CURRENCY_FX,
    NO_ALIGNABLE_PAIR,
    AlignmentDependencies,
    MissingDependency,
)
from core.capability_proposals import (
    NOT_APPLICABLE,
    CompileContext,
    compiler_for,
    governance_owner_reference,
)
from core.datastream_preconfiguration import CAPABILITY_LABELS
from core.datastream_workbench import CAPABILITY_TABS
from core.project_capability_states import (
    CAPABILITY_AVAILABILITY,
    PROJECT_CAPABILITY_KEYS,
)
from core.project_settings import CAPABILITY_SPECS

KEY = ANALYTICS_ALIGNMENT_CAPABILITY_KEY
PROJECT = "proj_EXAMPLE"
ORG = "org_EXAMPLE"
LEFT_DS = "ds_EXAMPLE_media"
RIGHT_DS = "ds_EXAMPLE_analytics"
LONE_DS = "ds_EXAMPLE_alone"


# ---------------------------------------------------------------------------
# The declaration, walked rather than trusted.
# ---------------------------------------------------------------------------


def test_the_seventh_key_is_last_and_is_the_same_word_in_every_table() -> None:
    """LAST, and that position is load-bearing.

    `read_project_settings` ranks its rows with a CASE and
    `validate_capability_ledger` compares the result to `CAPABILITY_SPECS`. A key
    appended to one list and inserted in the middle of the other makes the
    envelope raise `capability keys are ... unordered` on some reads and not
    others -- Project Settings dead by intermittence, which is worse than dead.
    """
    assert list(CAPABILITY_SPECS)[-1] == KEY
    assert PROJECT_CAPABILITY_KEYS[-1] == KEY
    assert list(CAPABILITY_SPECS) == list(PROJECT_CAPABILITY_KEYS)
    assert len(CAPABILITY_SPECS) == 7


def test_the_capability_is_optional_and_says_so_in_both_places() -> None:
    """Off by default, and the database can hold nothing else.

    The pairing CHECK of migration 309 puts the key in the `optional` member, so
    a table that declared it `always_present` would be a Python promise the
    database refuses -- and the refusal would surface as a constraint violation
    on activation rather than as a wrong default.
    """
    assert CAPABILITY_SPECS[KEY]["availability"] == "optional"
    assert CAPABILITY_AVAILABILITY[KEY] == "optional"


def test_currency_fx_is_declared_as_a_capability_dependency() -> None:
    """The one dependency this list CAN express, and it is expressed."""
    assert CAPABILITY_SPECS[KEY]["dependencies"] == ["currency_fx"]


def test_the_capability_resolves_a_governance_owner_instead_of_raising() -> None:
    """The hole story 61.5 named: one missing entry takes down every Workbench."""
    reference = governance_owner_reference(KEY, object_type="rule-set")
    assert reference["workspace"] == "governance"
    assert reference["section"] == "master-data"


def test_the_capability_opens_no_tab_and_says_so_with_none() -> None:
    """`None` is the answer, not an omission.

    Analytics Alignment adds COLUMNS, not a tab. Leaving it out of the map would
    make the Overview show six modules out of seven; declaring a tab it does not
    open would draw an empty band on every Workbench.
    """
    tabs = dict(CAPABILITY_TABS)
    assert set(tabs) == set(PROJECT_CAPABILITY_KEYS)
    assert tabs[KEY] is None


def test_a_person_reads_a_name_and_never_the_machine_key() -> None:
    assert CAPABILITY_LABELS[KEY] == "Analytics Alignment"
    assert set(CAPABILITY_LABELS) == set(PROJECT_CAPABILITY_KEYS)


# ---------------------------------------------------------------------------
# The compiler.
# ---------------------------------------------------------------------------


class _StubConn:
    """Nothing here reads a connection: the collaborators are patched by name."""


def _context(requested_state: str, evidence: dict) -> CompileContext:
    return CompileContext(
        org_id=ORG,
        project_id=PROJECT,
        capability_key=KEY,
        change_set_id="pcset_EXAMPLE",
        intent={"capabilities": {KEY: requested_state}},
        requested_state=requested_state,
        project_evidence=evidence,
        actor="owner@example.com",
    )


_PAIR = {
    "left_datastream_id": LEFT_DS,
    "right_datastream_id": RIGHT_DS,
    "common_key_version_id": "mdmckv_EXAMPLE",
    "relationship_name": "media_to_analytics",
    "view_version_id": "svv_EXAMPLE",
}


def _patch_resolve(monkeypatch, dependencies: AlignmentDependencies) -> None:
    import core.analytics_alignment as module

    monkeypatch.setattr(
        module,
        "resolve_dependencies",
        lambda conn, *, project_id, left_datastream_id, right_datastream_id: dependencies,
    )


def test_a_disabled_capability_names_none_of_its_three_columns() -> None:
    """OFF ADDS NOTHING, and this is the check rather than the promise.

    "Eteinte, la capacite n'apparait nulle part -- ni onglet, ni panneau, ni
    colonne". A payload that mentioned the columns while the switch was off would
    be a panel by another name.
    """
    compiler = compiler_for(KEY)
    assessment = compiler.assess(
        _StubConn(),
        context=_context("disabled", {"alignable_pairs": [_PAIR]}),
        datastream={"id": LEFT_DS},
    )
    assert assessment.applicability == NOT_APPLICABLE
    assert assessment.coverage_state == NOT_APPLICABLE
    rendered = repr(assessment.detected_support_selection)
    for column in ADDED_COLUMNS:
        assert column not in rendered


def test_a_datastream_no_approved_pair_can_reach_leaves_the_denominator() -> None:
    """`not_applicable`, never `unavailable`.

    `aggregate_coverage` takes only `not_applicable` out of the denominator. A
    Datastream nothing can be crossed with would otherwise lower the coverage
    percentage of the whole Project for a question that was never asked of it.
    """
    compiler = compiler_for(KEY)
    assessment = compiler.assess(
        _StubConn(),
        context=_context("enabled", {"alignable_pairs": [_PAIR]}),
        datastream={"id": LONE_DS},
    )
    assert assessment.applicability == NOT_APPLICABLE
    assert assessment.reason == NO_ALIGNABLE_PAIR
    # Named at `None` and never at `0`: nothing counted columns here.
    assert assessment.detected_support_selection["added_columns"] is None


def test_a_project_with_no_alignable_pair_at_all_refuses_activation_with_a_gesture() -> None:
    """The 70.3 defect: on a Project where NO pair is alignable, every Datastream
    was `not_applicable` with zero blockers, so the Change Set landed `prepared`
    and the capability activated `ready` -- the three declared-BLOCKING
    dependencies blocking nothing in the exact state where none is met.

    Now the requested activation is refused with a blocker naming the gesture.
    Distinct from the denominator case above: there a pair EXISTS elsewhere; here
    there is none at all.
    """
    compiler = compiler_for(KEY)
    assessment = compiler.assess(
        _StubConn(),
        context=_context("enabled", {"alignable_pairs": [], "currency_fx_state": "ready"}),
        datastream={"id": LONE_DS},
    )
    assert assessment.applicability == "applicable"
    assert assessment.coverage_state == "unavailable"
    assert assessment.detected_support_selection["added_columns"] is None
    codes = {blocker.code for blocker in assessment.blockers}
    assert "alignable_pair" in codes
    # The gesture, not just the cause: every blocker carries an act.
    assert all(blocker.message for blocker in assessment.blockers)


def test_no_alignable_pair_and_fx_absent_names_both_missing_dependencies() -> None:
    """Currency & FX is read on this path too -- before the fix it was never
    consulted when there was no pair, so the money dependency went unnamed exactly
    when nothing else was met either."""
    compiler = compiler_for(KEY)
    assessment = compiler.assess(
        _StubConn(),
        context=_context("enabled", {"alignable_pairs": [], "currency_fx_state": "disabled"}),
        datastream={"id": LONE_DS},
    )
    codes = {blocker.code for blocker in assessment.blockers}
    assert "alignable_pair" in codes
    assert DEPENDENCY_CURRENCY_FX in codes


def test_a_project_with_no_pair_but_capability_off_still_adds_nothing() -> None:
    """The refusal is for a REQUESTED activation only. Off stays off, no blocker,
    no column -- the disabled branch returns before any of this."""
    compiler = compiler_for(KEY)
    assessment = compiler.assess(
        _StubConn(),
        context=_context("disabled", {"alignable_pairs": []}),
        datastream={"id": LONE_DS},
    )
    rendered = repr(assessment.detected_support_selection)
    for column in ADDED_COLUMNS:
        assert column not in rendered
    assert assessment.blockers == ()


def test_an_activation_blocked_names_every_missing_dependency_with_its_gesture(
    monkeypatch,
) -> None:
    _patch_resolve(
        monkeypatch,
        AlignmentDependencies(
            left_datastream_id=LEFT_DS,
            right_datastream_id=RIGHT_DS,
            currency_fx_state="disabled",
            missing=(
                MissingDependency(
                    code=DEPENDENCY_COMMON_KEY, reason="no key", gesture="Declare the common key."
                ),
                MissingDependency(
                    code=DEPENDENCY_CURRENCY_FX,
                    reason="no fx",
                    gesture="Confirm the Money Policy.",
                ),
            ),
        ),
    )
    compiler = compiler_for(KEY)
    assessment = compiler.assess(
        _StubConn(),
        context=_context("enabled", {"alignable_pairs": [_PAIR]}),
        datastream={"id": LEFT_DS},
    )
    assert assessment.applicability == "applicable"
    assert assessment.coverage_state == "unavailable"
    assert [blocker.code for blocker in assessment.blockers] == [
        DEPENDENCY_COMMON_KEY,
        DEPENDENCY_CURRENCY_FX,
    ]
    assert [blocker.message for blocker in assessment.blockers] == [
        "Declare the common key.",
        "Confirm the Money Policy.",
    ]
    assert assessment.repair is not None


def test_a_missing_relationship_is_a_blocker_of_its_own(monkeypatch) -> None:
    """A declaration of MEANING is not a permission to EXECUTE."""
    _patch_resolve(
        monkeypatch,
        AlignmentDependencies(
            left_datastream_id=LEFT_DS,
            right_datastream_id=RIGHT_DS,
            common_key_version_ids=("mdmckv_EXAMPLE",),
            currency_fx_state="ready",
            missing=(
                MissingDependency(
                    code=DEPENDENCY_APPROVED_RELATIONSHIP,
                    reason="nobody approved crossing on it",
                    gesture="Publish the Semantic View that carries the relationship.",
                ),
            ),
        ),
    )
    compiler = compiler_for(KEY)
    assessment = compiler.assess(
        _StubConn(),
        context=_context("enabled", {"alignable_pairs": [_PAIR]}),
        datastream={"id": RIGHT_DS},
    )
    assert [blocker.code for blocker in assessment.blockers] == [
        DEPENDENCY_APPROVED_RELATIONSHIP
    ]


def test_the_three_columns_are_named_only_once_every_dependency_is_met(monkeypatch) -> None:
    _patch_resolve(
        monkeypatch,
        AlignmentDependencies(
            left_datastream_id=LEFT_DS,
            right_datastream_id=RIGHT_DS,
            common_key_version_ids=("mdmckv_EXAMPLE",),
            relationships=({"relationship_name": "media_to_analytics"},),
            currency_fx_state="ready",
        ),
    )
    compiler = compiler_for(KEY)
    assessment = compiler.assess(
        _StubConn(),
        context=_context("enabled", {"alignable_pairs": [_PAIR]}),
        datastream={"id": LEFT_DS},
    )
    assert assessment.coverage_state == "complete"
    assert assessment.detected_support_selection["added_columns"] == list(ADDED_COLUMNS)
    assert assessment.detected_support_selection["paired_with"] == RIGHT_DS


def test_the_project_evidence_states_the_absence_rather_than_an_empty_block(
    monkeypatch,
) -> None:
    """A Project-wide read never has to invent what `[]` meant."""
    import core.analytics_alignment as module
    import core.project_capability_states as states

    monkeypatch.setattr(module, "alignable_pairs", lambda conn, *, project_id: [])
    monkeypatch.setattr(
        states, "read_capability_state", lambda conn, *, project_id, capability_key: "draft"
    )
    evidence = compiler_for(KEY).project_evidence(_StubConn(), project_id=PROJECT, org_id=ORG)
    assert evidence["alignable_pairs"] == []
    assert evidence["reason"] == NO_ALIGNABLE_PAIR
    assert evidence["currency_fx_state"] == "draft"


@pytest.mark.parametrize("key", list(CAPABILITY_SPECS))
def test_every_declared_capability_still_has_a_registered_compiler(key: str) -> None:
    """`compiler_for` raises the moment an activation is requested without one."""
    assert compiler_for(key).capability_key == key
