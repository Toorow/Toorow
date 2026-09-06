"""What a governed Datastream change is frozen against, and whether it moved.

WHY THIS FILE EXISTS (amendment 4 of the 2026-08-11 review). Until 2026-08-12,
`datastream_change.prepare_change` read ONE thing: the two pointers on
`app.datastreams`, through an INNER JOIN, on a row required to be `active`. The
Mapping tab meanwhile became editable on the HEAD version when no pointer is in
force, because every editing control was otherwise unreachable. Measured on the
live base on 2026-08-12: 8 non-archived Datastreams, 1 `active` carrying both
pointers, 7 `draft`, 6 of them with NO mapping pointer. The screen offered a
governed change on the majority of the fleet and the seam refused every one --
a control that looks like it works.

So a base is a PAIR: the version, and whether that version is IN FORCE. Both are
real version ids; what differs is what the confirmation must re-check, and
`require_base_unmoved` is where that difference lives.

NOTHING HERE ACTIVATES ANYTHING. `docs/product-architecture/governance.md` --
"The head is a POINTER, not a status" -- keeps the pointer move in governed
publication, whose single writer is
`datastream_activation.publish_activate_mutation`. Reading the head as a base is
a read; it does not make the head live, and no function below writes to
`app.datastreams`.

It is a module of its own because `datastream_change.py` stood at 991 lines with
this block inside it, and because "which version governs, and has it moved" is
one responsibility with one set of tests.
"""

from __future__ import annotations

from typing import Any


class DatastreamChangeError(ValueError):
    """The refusal vocabulary shared by the whole change seam.

    Defined here rather than in `datastream_change` so that this module needs no
    import of its caller. `datastream_change` re-exports it, so
    `from core.datastream_change import DatastreamChangeError` -- what every
    existing caller and test writes -- keeps naming this exact class.
    """

    code = "invalid_datastream_change"


#: The lifecycle states in which a governed change may be prepared.
CHANGEABLE_LIFECYCLE_STATES = frozenset({"draft", "active", "paused"})

#: axis -> (append-only ledger, the column carrying its full document).
VERSION_LEDGERS: dict[str, tuple[str, str]] = {
    "plan": ("app.datastream_plan_versions", "normalized_payload"),
    "mapping": ("app.datastream_mapping_versions", "mapping_payload"),
}

#: What a person does next when the base of their review is no longer the base.
_REPREPARE = "reopen the Datastream and prepare the change again"


def require_changeable(lifecycle_state: str | None, archived_at: Any) -> None:
    """Which Datastreams accept a governed change, and which say why they do not.

    `lifecycle_state='active'` was the whole test, and it made
    `Prepare mapping change` a control that refuses on 7 of the 8 non-archived
    Datastreams of the live base. A DRAFT is exactly where a mapping is composed
    (amendment 4), so a draft is in.

    ARCHIVED IS OUT, and it is read from `archived_at`, never from the enum: the
    soft archive of `core/datastreams.py` sets `archived_at` and `enabled=FALSE`
    and LEAVES `lifecycle_state` where it was -- all 65 archived rows of the live
    base still read `draft`, so an enum test would have let every one of them
    through. The sentence is the one the Workbench already gives an archived
    Datastream asked to start: it is restored, it is not driven from here.
    """
    if archived_at is not None or (lifecycle_state or "").lower() == "archived":
        raise DatastreamChangeError("An archived Datastream is restored before it is changed")
    if (lifecycle_state or "").lower() not in CHANGEABLE_LIFECYCLE_STATES:
        raise DatastreamChangeError("This Datastream is not in a state that accepts a change")


def head_version(cur, axis: str, *, datastream_id: str, project_id: str) -> tuple[str, Any] | None:
    """The most recent recorded version of one axis, with its document.

    `version_number` is allocated as `MAX(...)+1` under the same `app.datastreams`
    row lock both callers hold, so "most recent" is one row and not a tie.
    """
    table, payload_column = VERSION_LEDGERS[axis]
    cur.execute(
        f"SELECT id,{payload_column} FROM {table} "  # noqa: S608 -- literal, from VERSION_LEDGERS
        "WHERE datastream_id=%s AND project_id=%s ORDER BY version_number DESC LIMIT 1",
        (datastream_id, project_id),
    )
    return cur.fetchone()


def resolve_base(
    cur, axis: str, *, datastream_id: str, project_id: str, pointer: str | None
) -> tuple[str, Any, bool]:
    """The version this change is frozen against, and whether it is IN FORCE.

    Returns `(version_id, document, pointer_in_force)`. A POINTER WINS whenever
    there is one -- widening the base must never take a change off the version
    that actually governs the data. With no pointer, the base is the head of the
    ledger, which is exactly what the Mapping tab lets a person edit.

    An axis with NO recorded version at all is refused, and the refusal names the
    gesture that records the first one rather than inventing an empty document to
    diff against. Measured on the live base on 2026-08-12: 2 of the 8 are in that
    state, both `draft` `managed_feed` with zero plan and zero mapping versions.
    """
    table, payload_column = VERSION_LEDGERS[axis]
    if pointer:
        cur.execute(
            f"SELECT {payload_column} FROM {table} "  # noqa: S608 -- literal, from VERSION_LEDGERS
            "WHERE id=%s AND datastream_id=%s AND project_id=%s",
            (pointer, datastream_id, project_id),
        )
        found = cur.fetchone()
        if found is None:
            raise DatastreamChangeError(
                f"The {axis} version in force could not be read; {_REPREPARE}"
            )
        return pointer, found[0], True
    head = head_version(cur, axis, datastream_id=datastream_id, project_id=project_id)
    if head is None:
        raise DatastreamChangeError(
            f"This Datastream has no recorded {axis} version to change yet; "
            "finish its setup in the wizard, which records the first one"
        )
    return head[0], head[1], False


def require_base_unmoved(
    cur,
    axis: str,
    *,
    datastream_id: str,
    project_id: str,
    expected_id: str,
    pointer_in_force: bool,
    current_pointer: str | None,
) -> None:
    """THE OPTIMISTIC LOCK, and what it compares when no pointer existed at prepare.

    POINTER IN FORCE AT PREPARE -- unchanged, and it must stay unchanged: the
    review was frozen against the version that governs the data, so the version
    that governs the data at confirmation must be that exact one. A publication
    that moved it underneath refuses. This is the guarantee that existed before
    amendment 4, and not a line below weakens it.

    NO POINTER AT PREPARE -- the review was frozen against the head of the ledger
    AND against the fact that nothing was live. Two things falsify it, and both
    refuse:

      A PUBLICATION HAPPENED. `current_<axis>_version_id` is no longer NULL, so a
      version IS live now -- possibly the very version this review is based on.
      "Nothing is in force" was part of what the person read, and the rollback
      path they were shown ("no version becomes live") stopped being true. This
      is the case amendment 4 could most easily have let land silently, and it is
      why `expected_<axis>_pointer_in_force` is stored rather than re-derived: a
      confirmation that re-derived it would find a pointer, believe the
      preparation had always been made against one, compare it to the base, find
      them equal when the publication activated that very head -- and land.

      A NEWER VERSION WAS RECORDED. The head moved, so the document the person
      diffed against is superseded and their `before_hash` describes a version
      the screen would no longer show. Two concurrent preparations on a
      pointerless Datastream are exactly this; without the check the second would
      append on top of a base nobody reviewed.

    A pointer that DISAPPEARED between prepare and confirm is caught by the first
    branch -- `current_pointer != expected_id` with `current_pointer` NULL.
    """
    if pointer_in_force:
        if current_pointer != expected_id:
            raise DatastreamChangeError(
                f"The {axis} version in force moved after this review; {_REPREPARE}"
            )
        return
    if current_pointer is not None:
        raise DatastreamChangeError(
            f"A {axis} version was published while this review was open; {_REPREPARE}"
        )
    head = head_version(cur, axis, datastream_id=datastream_id, project_id=project_id)
    if head is None or head[0] != expected_id:
        raise DatastreamChangeError(
            f"A newer {axis} version was recorded after this review; {_REPREPARE}"
        )


def base_version_statement(version_id: str, pointer_in_force: bool) -> dict[str, Any]:
    """What the person confirming is told about the version they are changing.

    `in_force` and `head_of_ledger` are two different facts about a Datastream,
    and the second carries a consequence the first does not: the change is being
    composed beside a version nothing is running, and confirming still makes
    nothing live.
    """
    if pointer_in_force:
        return {"state": "in_force", "version_id": version_id}
    return {
        "state": "head_of_ledger",
        "version_id": version_id,
        "reason": "no version is in force; the most recent recorded version is the base, "
        "and confirming this change does not make it live",
    }


def rollback_path(
    plan_version_id: str,
    plan_in_force: bool,
    mapping_version_id: str,
    mapping_in_force: bool,
) -> dict[str, Any]:
    """The way back, stated for the pointers that exist and for no others.

    "The live pointers do not move" is true of NOTHING on a Datastream with no
    pointer in force, and a promise about an object that does not exist is the
    shape of reassurance this seam exists to refuse.
    """
    if plan_in_force and mapping_in_force:
        return {
            "state": "active_versions_stay_in_force",
            "plan_version_id": plan_version_id,
            "mapping_version_id": mapping_version_id,
            "reason": "this change appends without activating; the live pointers do not move",
        }
    return {
        "state": "no_version_becomes_live",
        "plan_version_id": plan_version_id if plan_in_force else None,
        "mapping_version_id": mapping_version_id if mapping_in_force else None,
        "reason": "this change appends without activating; an axis with no version in force "
        "keeps none, and publication stays the only step that makes one live",
    }
