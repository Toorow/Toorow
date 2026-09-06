"""The four cases of the optimistic lock, stated once and cheaply.

WHY THIS FILE EXISTS. Amendment 4 of the 2026-08-11 review made the Mapping tab
editable on the HEAD version when no mapping pointer is in force. The seam was
not moved with it, so `prepare_change` refused every Datastream missing a pointer
or not `active` -- 7 of the 8 non-archived Datastreams of the live base measured
on 2026-08-12. Widening it is only safe if the guarantee the pointers carried
survives, and that guarantee is a 2x2:

                          | pointer moved / a version was published | did not move
    pointer at prepare    | REFUSE (unchanged since story 38.17)    | confirm
    no pointer at prepare | REFUSE (new, and the one that could     | confirm, only
                          | most easily have landed silently)       | if the head is
                          |                                         | still the base

`tests/integration/test_datastream_change_head_base_pg.py` runs all four against
a real schema, real rows and real refusals -- that is the file that proves the
behaviour. This one pins the DECISION on its own, without a database, so the
matrix is re-run on every change of this module in under a second and a fifth
case cannot be added without a line here.
"""

from __future__ import annotations

import pytest
from core.datastream_change_base import (
    DatastreamChangeError,
    base_version_statement,
    require_base_unmoved,
    require_changeable,
    resolve_base,
    rollback_path,
)


class _Cursor:
    """A cursor that answers with the head row it was built with, once per query.

    It records every statement, so a test can assert that a branch which must not
    reach the ledger did not reach it -- "refused" and "refused before asking"
    are different facts about a lock.
    """

    def __init__(self, head: tuple[str, object] | None = None) -> None:
        self.head = head
        self.statements: list[str] = []

    def execute(self, sql: str, params: tuple = ()) -> None:  # noqa: ARG002
        self.statements.append(" ".join(sql.split()))

    def fetchone(self):
        return self.head


# ---------------------------------------------------------------------------
# The 2x2 itself.
# ---------------------------------------------------------------------------


def test_a_base_in_force_that_did_not_move_is_confirmable() -> None:
    cur = _Cursor()
    require_base_unmoved(
        cur,
        "mapping",
        datastream_id="ds_1",
        project_id="proj_1",
        expected_id="dmap_1",
        pointer_in_force=True,
        current_pointer="dmap_1",
    )
    assert cur.statements == [], (
        "an in-force base is decided by the pointer alone; reading the ledger here "
        "would make the head a second authority over a version already in force"
    )


@pytest.mark.parametrize(
    ("current_pointer", "why"),
    [
        ("dmap_2", "a publication moved the pointer to another version"),
        (None, "a rollback left the Datastream with no version in force at all"),
    ],
)
def test_a_base_in_force_that_moved_is_refused(current_pointer, why) -> None:
    """The guarantee that existed before amendment 4, unweakened."""
    with pytest.raises(DatastreamChangeError) as refusal:
        require_base_unmoved(
            _Cursor(),
            "mapping",
            datastream_id="ds_1",
            project_id="proj_1",
            expected_id="dmap_1",
            pointer_in_force=True,
            current_pointer=current_pointer,
        )
    assert "moved after this review" in str(refusal.value), why
    assert "prepare the change again" in str(refusal.value), (
        "a refusal names the gesture that repairs it"
    )


def test_a_head_base_with_nothing_published_and_nothing_appended_is_confirmable() -> None:
    """The repair: the case that used to be unreachable is the ordinary one."""
    require_base_unmoved(
        _Cursor(head=("dmap_1", {})),
        "mapping",
        datastream_id="ds_1",
        project_id="proj_1",
        expected_id="dmap_1",
        pointer_in_force=False,
        current_pointer=None,
    )


def test_a_head_base_refuses_once_a_publication_has_happened() -> None:
    """THE case a re-derived flag would have let land silently.

    The publication activated the very version this review is based on, so a
    confirmation that re-read "is there a pointer, and does it equal my expected
    id" would find yes and yes, and append. What the person read was "no version
    is in force, and confirming makes nothing live"; that stopped being true, so
    the answer is a refusal and not a silent append.
    """
    cur = _Cursor(head=("dmap_1", {}))
    with pytest.raises(DatastreamChangeError) as refusal:
        require_base_unmoved(
            cur,
            "mapping",
            datastream_id="ds_1",
            project_id="proj_1",
            expected_id="dmap_1",
            pointer_in_force=False,
            current_pointer="dmap_1",  # the base itself, now live
        )
    assert "published while this review was open" in str(refusal.value)
    assert cur.statements == [], "a published pointer settles it; the head is not consulted"


def test_a_head_base_refuses_once_a_newer_version_has_been_recorded() -> None:
    """Two concurrent preparations on a pointerless Datastream are exactly this."""
    with pytest.raises(DatastreamChangeError) as refusal:
        require_base_unmoved(
            _Cursor(head=("dmap_2", {})),
            "mapping",
            datastream_id="ds_1",
            project_id="proj_1",
            expected_id="dmap_1",
            pointer_in_force=False,
            current_pointer=None,
        )
    assert "newer mapping version was recorded" in str(refusal.value)


def test_a_head_base_refuses_when_the_ledger_has_gone_empty() -> None:
    """`head is None` is not "unchanged"; it is a base that no longer exists."""
    with pytest.raises(DatastreamChangeError):
        require_base_unmoved(
            _Cursor(head=None),
            "plan",
            datastream_id="ds_1",
            project_id="proj_1",
            expected_id="dsp_1",
            pointer_in_force=False,
            current_pointer=None,
        )


# ---------------------------------------------------------------------------
# Which Datastreams accept a change at all.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("state", ["draft", "active", "paused"])
def test_a_draft_or_paused_datastream_accepts_a_change(state) -> None:
    """`draft` is the state amendment 4 is about, and 7 of 8 live rows are in it."""
    require_changeable(state, None)


def test_an_archived_datastream_is_refused_by_its_timestamp_not_by_its_enum() -> None:
    """The soft archive LEAVES `lifecycle_state` alone.

    Measured on the live base on 2026-08-12: 65 archived Datastreams, every one
    of them still reading `lifecycle_state = 'draft'`. A test on the enum would
    have admitted all 65 the moment `draft` became changeable.
    """
    with pytest.raises(DatastreamChangeError) as refusal:
        require_changeable("draft", "2026-08-01T00:00:00Z")
    assert "restored" in str(refusal.value)

    with pytest.raises(DatastreamChangeError):
        require_changeable("archived", None)


def test_an_axis_with_no_recorded_version_names_the_gesture_that_records_one() -> None:
    """2 of the 8 live Datastreams have zero plan and zero mapping versions.

    An empty document to diff against would be fabricated evidence; the refusal
    names the wizard instead.
    """
    with pytest.raises(DatastreamChangeError) as refusal:
        resolve_base(
            _Cursor(head=None),
            "mapping",
            datastream_id="ds_1",
            project_id="proj_1",
            pointer=None,
        )
    assert "no recorded mapping version" in str(refusal.value)
    assert "wizard" in str(refusal.value)


# ---------------------------------------------------------------------------
# What the person confirming is told.
# ---------------------------------------------------------------------------


def test_the_review_tells_the_two_bases_apart_in_words() -> None:
    assert base_version_statement("dmap_1", True) == {
        "state": "in_force",
        "version_id": "dmap_1",
    }
    head = base_version_statement("dmap_1", False)
    assert head["state"] == "head_of_ledger"
    assert "does not make it live" in head["reason"]


def test_the_rollback_path_never_promises_pointers_that_do_not_exist() -> None:
    """ "The live pointers do not move" is true of nothing when nothing is live."""
    both = rollback_path("dsp_1", True, "dmap_1", True)
    assert both["state"] == "active_versions_stay_in_force"
    assert both["mapping_version_id"] == "dmap_1"

    mixed = rollback_path("dsp_1", True, "dmap_1", False)
    assert mixed["state"] == "no_version_becomes_live"
    assert mixed["plan_version_id"] == "dsp_1"
    assert mixed["mapping_version_id"] is None, (
        "naming a mapping version to roll back to, when none is in force, promises "
        "a return to a state that never existed"
    )
