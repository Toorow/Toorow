"""Changing a Datastream's data role is governed — amendment 9, 2026-08-11 review.

« Le rôle et le mode s'éditent depuis le Workbench, par le même changement
gouverné que le reste : un changement préparé, une confirmation qui nomme ce qui
bouge en aval, une version. »

WHAT THIS FILE HOLDS, and why each of these is a real hole rather than a
hypothetical one.

  * THE DOOR EXISTED AND WAS UNGOVERNED. The review that raised this defect
    grepped `UPDATE … SET data_role` and found one writer. `update_datastream`
    builds its `SET` clause at runtime, so the grep could not see it -- and
    `data_role` has been in its allowed set all along. Anybody could move a
    Datastream out of the fee ladder with one field, with no base and no
    sentence.
  * THE BASE DECIDES A RACE, so it is read UNDER THE ROW LOCK. Two screens on one
    Datastream were arbitrated by whoever committed last, and the loser was told
    nothing -- the same defect `master_data_commands` settled for a rename.
  * THE DOWNSTREAM IS A PAIR, not a role. `fee_tax_source_types._AGREEMENT` maps
    (manifest category, role) -> source type, and only five pairs agree; anything
    else is `UNKNOWN`, which is EXCLUDED from the cost cascade. So the sentence a
    person must read before the click is per role, and it is computed from the
    ladder's own table rather than written twice.

Against a REAL schema, because every one of those is a fact about rows: a fake
cursor would answer whatever this file felt like, and the lock, the CHECK of
migration 093 and the audit insert are exactly what it could not prove.
"""

from __future__ import annotations

import os
import uuid

import pytest

_skip_without_dsn = pytest.mark.skipif(
    not os.environ.get("TEST_POSTGRES_DSN"),
    reason="TEST_POSTGRES_DSN not set -- live data-role governance walk skipped",
)

AUTHOR = "amendment-9-test"
IDENTITY = "person_01J8ZC4Q0N7R2K3W5X6Y7Z8A9C"


def _id(prefix: str) -> str:
    return f"{prefix}{uuid.uuid4().hex[:12]}"


@pytest.fixture
def a_stream(live_postgres):
    """One org, one project, one `meta-ads` Datastream carrying a role.

    `meta-ads` because its manifest declares `public_catalog.category`, which is
    the first half of the pair the fee ladder resolves. A connector with no
    category would make every option answer the same sentence and prove nothing
    about the agreement table.
    """
    conn = live_postgres
    org_id, project_id, ds_id = _id("org_"), _id("proj_"), _id("ds_")
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO app.organizations (id, name, slug, created_by, status)"
            " VALUES (%s, %s, %s, %s, 'active')",
            (org_id, org_id, org_id, AUTHOR),
        )
        cur.execute(
            "INSERT INTO app.projects (id, name, slug, created_by, org_id, status)"
            " VALUES (%s, %s, %s, %s, %s, 'active')",
            (project_id, project_id, project_id, AUTHOR, org_id),
        )
        cur.execute(
            "INSERT INTO app.datastreams (id, project_id, name, module_name,"
            " source_kind, enabled, created_by, org_id, data_role)"
            " VALUES (%s, %s, 'Meta Ads', 'meta-ads', 'connector_pull', TRUE, %s, %s, 'Spend')",
            (ds_id, project_id, AUTHOR, org_id),
        )
    try:
        yield {"conn": conn, "org_id": org_id, "project_id": project_id, "ds_id": ds_id}
    finally:
        conn.rollback()


def _record(ids) -> dict:
    with ids["conn"].cursor() as cur:
        cur.execute(
            "SELECT data_role, source_kind, module_name FROM app.datastreams WHERE id=%s",
            (ids["ds_id"],),
        )
        row = cur.fetchone()
    return {"data_role": row[0], "source_kind": row[1], "module_name": row[2]}


@_skip_without_dsn
def test_the_review_states_the_ladder_effect_of_every_role_it_offers(a_stream) -> None:
    """The confirmation's own content, resolved from the ladder's table.

    NOT ONE WARNING FOR SEVEN ROLES. `_AGREEMENT` is a table of PAIRS, so what a
    role does depends on the connector's category; a screen handed one generic
    sentence would make the person guess which of the seven it applied to.
    """
    from core.datastream_data_role import read_role_change
    from core.datastreams import DATA_ROLES

    review = read_role_change(
        a_stream["conn"],
        project_id=a_stream["project_id"],
        datastream_id=a_stream["ds_id"],
        record=_record(a_stream),
    )

    assert review["current"] == "Spend"
    # THE BASE IS PUBLISHED, so the console never composes one of its own.
    assert review["expected_data_role"] == "Spend"
    assert [option["value"] for option in review["options"]] == list(DATA_ROLES)
    assert sum(1 for option in review["options"] if option["is_current"]) == 1

    by_value = {option["value"]: option for option in review["options"]}
    # The pairing in force, read off the manifest and not asserted here.
    assert review["category"], "meta-ads declares no manifest category on this build"
    assert review["in_cost_cascade"] is True
    # And the move that costs something: a role the category does not agree with
    # takes this Datastream out of the cascade, and the sentence says so.
    leaving = [
        option for option in review["options"]
        if not option["in_cost_cascade"] and not option["is_current"]
    ]
    assert leaving, "no offered role leaves the cascade; the agreement table changed"
    assert "cost cascade" in leaving[0]["effect"]
    assert by_value["Spend"]["effect"].startswith("This is the pairing in force")

    # THE OTHER HALF OF THE AMENDMENT SAYS WHAT IT IS. A silence here reads as a
    # missing feature; this reads as a decision with a gesture.
    assert review["mode_and_connector"]["changeable"] is False
    assert review["mode_and_connector"]["gesture"]


@_skip_without_dsn
def test_a_change_that_states_no_base_is_refused_before_anything_is_written(a_stream) -> None:
    from core.datastream_data_role import DataRoleChangeRefused, change_data_role

    with pytest.raises(DataRoleChangeRefused) as refused:
        change_data_role(
            a_stream["conn"],
            project_id=a_stream["project_id"],
            datastream_id=a_stream["ds_id"],
            proposed="Context",
            expected=None,
            actor=IDENTITY,
        )

    assert refused.value.code == "expected_data_role_required"
    assert _record(a_stream)["data_role"] == "Spend", "the role moved on a refused change"


@_skip_without_dsn
def test_a_base_that_has_moved_is_refused_and_names_what_the_role_reads_now(a_stream) -> None:
    """The whole reason the base travels: a stale screen must not win silently."""
    from core.datastream_data_role import DataRoleChangeRefused, change_data_role

    with a_stream["conn"].cursor() as cur:
        cur.execute(
            "UPDATE app.datastreams SET data_role='Performance' WHERE id=%s",
            (a_stream["ds_id"],),
        )

    with pytest.raises(DataRoleChangeRefused) as refused:
        change_data_role(
            a_stream["conn"],
            project_id=a_stream["project_id"],
            datastream_id=a_stream["ds_id"],
            proposed="Context",
            # The value a screen loaded before the other change landed.
            expected="Spend",
            actor=IDENTITY,
        )

    assert refused.value.code == "stale_data_role"
    assert "Performance" in str(refused.value), "the refusal does not say what it reads now"
    assert _record(a_stream)["data_role"] == "Performance"


@_skip_without_dsn
def test_a_role_outside_the_seven_is_refused_with_the_list_and_not_by_the_check(a_stream) -> None:
    """Migration 093's CHECK would answer a 500 that names nothing."""
    from core.datastream_data_role import DataRoleChangeRefused, change_data_role
    from core.datastreams import DATA_ROLES

    with pytest.raises(DataRoleChangeRefused) as refused:
        change_data_role(
            a_stream["conn"],
            project_id=a_stream["project_id"],
            datastream_id=a_stream["ds_id"],
            proposed="Whatever",
            expected="Spend",
            actor=IDENTITY,
        )

    assert refused.value.code == "invalid_data_role"
    assert refused.value.details["roles"] == list(DATA_ROLES)


@_skip_without_dsn
def test_the_write_lands_and_the_audit_row_names_what_moved_downstream(a_stream) -> None:
    """A change that only recorded the two words could never explain a cost report.

    The ladder effect is the whole reason this change is governed, so it is what
    the audit row carries -- before AND after, because the pair is what resolved
    and neither half alone says which way it moved.
    """
    from core.datastream_data_role import change_data_role

    outcome = change_data_role(
        a_stream["conn"],
        project_id=a_stream["project_id"],
        datastream_id=a_stream["ds_id"],
        proposed="Context",
        expected="Spend",
        actor=IDENTITY,
    )

    assert outcome["data_role"] == "Context"
    assert outcome["data_role_before"] == "Spend"
    assert outcome["source_type_before"] != outcome["source_type_after"]
    assert outcome["effect"]
    assert _record(a_stream)["data_role"] == "Context"

    with a_stream["conn"].cursor() as cur:
        cur.execute(
            "SELECT metadata FROM app.audit_log"
            " WHERE metadata->>'datastream_id' = %s"
            "   AND metadata->>'operation' = 'datastream_data_role_change'"
            " ORDER BY created_at DESC LIMIT 1",
            (a_stream["ds_id"],),
        )
        row = cur.fetchone()
    assert row is not None, "the governed change left no audit row"
    metadata = row[0]
    assert metadata["data_role_before"] == "Spend"
    assert metadata["data_role_after"] == "Context"
    assert metadata["source_type_before"] == outcome["source_type_before"]
    assert metadata["source_type_after"] == outcome["source_type_after"]


@_skip_without_dsn
def test_changing_a_role_to_the_one_it_already_carries_is_not_a_success(a_stream) -> None:
    """A 200 for a gesture that did nothing is the same lie the rename refuses."""
    from core.datastream_data_role import DataRoleChangeRefused, change_data_role

    with pytest.raises(DataRoleChangeRefused) as refused:
        change_data_role(
            a_stream["conn"],
            project_id=a_stream["project_id"],
            datastream_id=a_stream["ds_id"],
            proposed="Spend",
            expected="Spend",
            actor=IDENTITY,
        )

    assert refused.value.code == "no_change"


@_skip_without_dsn
def test_an_archived_datastream_is_restored_before_its_role_is_changed(a_stream) -> None:
    """The same sentence the change seam gives it, so the product says one thing."""
    from core.datastream_data_role import DataRoleChangeRefused, change_data_role

    with a_stream["conn"].cursor() as cur:
        cur.execute(
            "UPDATE app.datastreams SET archived_at = now() WHERE id=%s",
            (a_stream["ds_id"],),
        )

    with pytest.raises(DataRoleChangeRefused) as refused:
        change_data_role(
            a_stream["conn"],
            project_id=a_stream["project_id"],
            datastream_id=a_stream["ds_id"],
            proposed="Context",
            expected="Spend",
            actor=IDENTITY,
        )

    assert refused.value.code == "archived"
    assert "restored" in str(refused.value)
