"""Story 70.4 against a real database -- migration 310's table, CHECKs and grants.

``test_analytics_ventilation.py`` proves the engine, which is pure. What it
cannot prove is that the weights survive the round trip: that `NUMERIC(19, 18)`
gives back the same eighteen places the assertion was run on, that the shapes the
model forbids are refused by a CONSTRAINT and not only by Python, that the first
run of a `(key, day, entity)` stands because a UNIQUE index says so, and that
nothing was granted the privilege to rewrite a published split.

The last test is the one worth naming: it re-reads the weights OUT of the
database and re-runs the conservation over them. An assertion at computation time
cannot see a run written half, and that is exactly the failure a stored,
row-by-row weight set can have.

pg-gated: skipped without ``TEST_POSTGRES_DSN``. It runs offline against the
disposable Postgres (``python scripts/disposable_postgres.py up``), which must
carry migration 310.

No real identifier appears: fixtures are minted from a ULID, the actor is
``owner@example.com``.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import psycopg
import pytest
import ulid
from core.analytics_ventilation import (
    ACTION_VENTILATION_RECORDED,
    VentilationBasis,
    VentilationCandidate,
    VentilationGroup,
    VentilationRefused,
    assert_stored_weights_conserve,
    list_ventilation_weights,
    reassemble,
    record_ventilation_weights,
    ventilate,
)

ACTOR = "owner@example.com"
DAY = date(2026, 8, 25)
BASIS = VentilationBasis(volume_name="clicks", declared_version="v1")


@pytest.fixture
def ventilation_scope(pg_conn, inbound_pg_scope):
    """A real pair of Datastreams and a real common key version to split under.

    Written by hand rather than through ``create_common_key``, for the reason the
    70.3 fixture states: what is under test is the FOREIGN KEY and the CHECKs of
    the weights table, and going through the writer would make a column name it
    happens to spell the thing being proved.
    """
    org = inbound_pg_scope["org_id"]
    project = inbound_pg_scope["project_id"]
    left = inbound_pg_scope["datastream_id"]
    suffix = str(ulid.ULID())
    right = f"ds_test_vent_{suffix}"[:40]
    key_id = f"mck_{suffix}"
    version_id = f"mckv_{suffix}"

    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO app.datastreams "
            "(id, project_id, org_id, name, created_by, source_kind, enabled, "
            "schedule_mode, refetch_days, date_window_days, lifecycle_state) "
            "VALUES (%s, %s, %s, 'Ventilation right side', 'system', "
            "'managed_feed', TRUE, 'manual', 3, 30, 'draft') "
            "ON CONFLICT (id) DO NOTHING",
            (right, project, org),
        )
        cur.execute(
            "INSERT INTO app.mdm_common_keys "
            "(id, org_id, project_id, name, status, created_by) "
            "VALUES (%s, %s, %s, 'Day and campaign', 'active', 'system')",
            (key_id, org, project),
        )
        cur.execute(
            "INSERT INTO app.mdm_common_key_versions "
            "(id, common_key_id, org_id, project_id, version_number, components, "
            "content_hash, created_by) "
            "VALUES (%s, %s, %s, %s, 1, "
            "'[{\"canonical_field_id\": \"cf_example_day\"}]'::jsonb, %s, 'system')",
            (version_id, key_id, org, project, "0" * 64),
        )
        cur.execute(
            "UPDATE app.mdm_common_keys SET current_version_id = %s WHERE id = %s",
            (version_id, key_id),
        )
    pg_conn.commit()
    return {
        "org_id": org,
        "project_id": project,
        "left": left,
        "right": right,
        "common_key_version_id": version_id,
    }


def _shares(*volumes, key: str = "key_1", day=DAY):
    outcome = ventilate(
        [
            VentilationGroup.of(
                key,
                day,
                [
                    VentilationCandidate(
                        row_key=f"entity_{chr(ord('a') + index)}", volume=volume
                    )
                    for index, volume in enumerate(volumes)
                ],
            )
        ],
        basis=BASIS,
        capability_state="ready",
    )
    return outcome.shares


def _record(conn, scope, shares, actor: str = ACTOR) -> int:
    return record_ventilation_weights(
        conn,
        org_id=scope["org_id"],
        project_id=scope["project_id"],
        left_datastream_id=scope["left"],
        right_datastream_id=scope["right"],
        common_key_version_id=scope["common_key_version_id"],
        shares=shares,
        actor=actor,
    )


def _read(conn, scope):
    return list_ventilation_weights(
        conn,
        project_id=scope["project_id"],
        left_datastream_id=scope["left"],
        right_datastream_id=scope["right"],
        common_key_version_id=scope["common_key_version_id"],
    )


def _insert(conn, scope, **overrides) -> None:
    row = {
        "id": f"aaw_{ulid.ULID()}",
        "org_id": scope["org_id"],
        "project_id": scope["project_id"],
        "left_datastream_id": scope["left"],
        "right_datastream_id": scope["right"],
        "common_key_version_id": scope["common_key_version_id"],
        "alignment_key": "key_1",
        "activity_date": DAY,
        "right_row_key": "entity_a",
        "volume_name": "clicks",
        "volume_version": "v1",
        "volume_value": Decimal(1),
        "volume_total": Decimal(4),
        "weight": Decimal("0.25"),
        "carries_residual": False,
        "computed_by": ACTOR,
    }
    row.update(overrides)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO app.analytics_alignment_weights "
            "(id, org_id, project_id, left_datastream_id, right_datastream_id, "
            "common_key_version_id, alignment_key, activity_date, right_row_key, "
            "volume_name, volume_version, volume_value, volume_total, weight, "
            "carries_residual, computed_by) "
            "VALUES (%(id)s, %(org_id)s, %(project_id)s, %(left_datastream_id)s, "
            "%(right_datastream_id)s, %(common_key_version_id)s, %(alignment_key)s, "
            "%(activity_date)s, %(right_row_key)s, %(volume_name)s, %(volume_version)s, "
            "%(volume_value)s, %(volume_total)s, %(weight)s, %(carries_residual)s, "
            "%(computed_by)s)",
            row,
        )


# ---------------------------------------------------------------------------
# The round trip, and the conservation measured on the way BACK OUT.
# ---------------------------------------------------------------------------


def test_the_stored_weights_still_conserve_when_they_are_read_back(
    pg_conn, ventilation_scope
) -> None:
    """The check an assertion at computation time cannot perform.

    Eighteen decimal places through `NUMERIC(19, 18)` and back, over a ratio no
    finite decimal writes: if the column rounded, this is where it would show.
    """
    shares = _shares(1, 1, 1)
    assert _record(pg_conn, ventilation_scope, shares) == 3

    stored = _read(pg_conn, ventilation_scope)
    assert len(stored) == 3
    assert sum(share.weight for share in stored) == Decimal(1)
    assert_stored_weights_conserve(stored)
    assert [share.weight for share in stored] == [share.weight for share in shares]
    pg_conn.rollback()


def test_the_split_is_still_reversible_from_the_stored_weights(
    pg_conn, ventilation_scope
) -> None:
    """Reversible means re-derivable from the STORE, not from the run that made it."""
    _record(pg_conn, ventilation_scope, _shares(7, 11, 13))
    stored = _read(pg_conn, ventilation_scope)
    observed = {"conversions": Decimal("1234.56")}
    assert reassemble(stored, observed)["conversions"] == Decimal("1234.56")
    pg_conn.rollback()


def test_the_declared_volume_and_its_version_come_back_on_every_row(
    pg_conn, ventilation_scope
) -> None:
    _record(pg_conn, ventilation_scope, _shares(3, 1))
    stored = _read(pg_conn, ventilation_scope)
    assert {share.volume_name for share in stored} == {"clicks"}
    assert {share.volume_version for share in stored} == {"v1"}
    assert sum(1 for share in stored if share.carries_residual) == 1
    pg_conn.rollback()


def test_a_run_is_recorded_with_its_author_on_the_same_transaction(
    pg_conn, ventilation_scope
) -> None:
    """AD-42: the audit row rides the caller's transaction, so a rollback erases both."""
    _record(pg_conn, ventilation_scope, _shares(1, 3))
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT identity, metadata FROM app.audit_log WHERE action = %s "
            "ORDER BY created_at DESC LIMIT 1",
            (ACTION_VENTILATION_RECORDED,),
        )
        row = cur.fetchone()
    assert row is not None
    identity, metadata = row
    assert identity == ACTOR
    assert metadata["volume_name"] == "clicks"
    assert metadata["shares_written"] == 2
    pg_conn.rollback()


# ---------------------------------------------------------------------------
# The first run of a (key, day, entity) stands.
# ---------------------------------------------------------------------------


def test_a_second_run_over_a_refetched_volume_never_rewrites_the_published_split(
    pg_conn, ventilation_scope
) -> None:
    """The shape migration 245 chose and 309 reused, applied to a weight.

    A client was shown a figure. Re-splitting it under a volume that has since
    been corrected would change a number nobody can re-derive afterwards, so the
    second run writes nothing and the store keeps the first.
    """
    assert _record(pg_conn, ventilation_scope, _shares(1, 3)) == 2
    assert [share.weight for share in _read(pg_conn, ventilation_scope)] == [
        Decimal("0.25"),
        Decimal("0.75"),
    ]

    assert _record(pg_conn, ventilation_scope, _shares(3, 1)) == 0
    assert [share.weight for share in _read(pg_conn, ventilation_scope)] == [
        Decimal("0.25"),
        Decimal("0.75"),
    ]
    pg_conn.rollback()


def test_a_second_run_with_an_ENLARGED_candidate_set_never_lands_a_split_past_one(
    pg_conn, ventilation_scope
) -> None:
    """The additive re-run, the exact defect the review proved.

    run1 splits (a, b). run2 sees a THIRD entity c answer the same coarse key that
    day -- the world of a re-fetched volume. c is a new `right_row_key`, so it does
    NOT conflict on the per-row unique index: `ON CONFLICT DO NOTHING` would insert
    it on top of a finished split and leave the (key, day) summing to 1.5. The
    group is written all-or-nothing instead: because (key, day) already carries a
    and b, run2 writes NONE of it, and the store never holds a split past one.
    """
    assert _record(pg_conn, ventilation_scope, _shares(1, 3)) == 2

    # run2: three candidates for the same (key, day). Weights would be a valid
    # split of three on their own -- the in-memory pre-check passes -- but the
    # (key, day) is already split, so nothing of it is written.
    assert _record(pg_conn, ventilation_scope, _shares(1, 1, 2)) == 0

    stored = _read(pg_conn, ventilation_scope)
    assert [share.right_row_key for share in stored] == ["entity_a", "entity_b"]
    assert sum(share.weight for share in stored) == Decimal("1")
    # And a re-read of the store conserves: the assertion that would forever raise
    # on a 1.5 split holds instead.
    assert_stored_weights_conserve(stored)
    pg_conn.rollback()


def test_a_run_whose_weights_do_not_conserve_is_refused_before_the_first_insert(
    pg_conn, ventilation_scope
) -> None:
    """Refused whole, never written half."""
    shares = _shares(1, 3)
    broken = (shares[0],)  # one share of a two-share key: 0.25, not 1
    with pytest.raises(VentilationRefused) as excinfo:
        _record(pg_conn, ventilation_scope, broken)
    assert excinfo.value.code == "weights_do_not_sum_to_one"
    assert _read(pg_conn, ventilation_scope) == []
    pg_conn.rollback()


def test_a_run_with_no_author_is_refused(pg_conn, ventilation_scope) -> None:
    with pytest.raises(VentilationRefused) as excinfo:
        _record(pg_conn, ventilation_scope, _shares(1, 3), actor="   ")
    assert excinfo.value.code == "ventilation_without_an_author"
    pg_conn.rollback()


# ---------------------------------------------------------------------------
# The CHECKs. Python refuses these at construction; the database refuses them to
# EVERY writer, including one that has not been written yet.
# ---------------------------------------------------------------------------


def test_a_weight_outside_zero_and_one_is_refused(pg_conn, ventilation_scope) -> None:
    with pytest.raises(psycopg.errors.CheckViolation):
        _insert(pg_conn, ventilation_scope, weight=Decimal("1.5"))
    pg_conn.rollback()


def test_a_negative_volume_is_refused(pg_conn, ventilation_scope) -> None:
    with pytest.raises(psycopg.errors.CheckViolation):
        _insert(pg_conn, ventilation_scope, volume_value=Decimal(-1))
    pg_conn.rollback()


def test_a_total_of_zero_is_refused_because_nothing_is_proportional_to_it(
    pg_conn, ventilation_scope
) -> None:
    with pytest.raises(psycopg.errors.CheckViolation):
        _insert(pg_conn, ventilation_scope, volume_total=Decimal(0), volume_value=Decimal(0))
    pg_conn.rollback()


def test_a_share_larger_than_the_total_it_was_taken_over_is_refused(
    pg_conn, ventilation_scope
) -> None:
    with pytest.raises(psycopg.errors.CheckViolation):
        _insert(pg_conn, ventilation_scope, volume_value=Decimal(9), volume_total=Decimal(4))
    pg_conn.rollback()


def test_a_split_that_names_no_volume_is_refused(pg_conn, ventilation_scope) -> None:
    """`aucun volume par defaut implicite`, said by a CONSTRAINT."""
    with pytest.raises(psycopg.errors.CheckViolation):
        _insert(pg_conn, ventilation_scope, volume_name="   ")
    pg_conn.rollback()


def test_a_declared_volume_with_no_version_is_refused(pg_conn, ventilation_scope) -> None:
    with pytest.raises(psycopg.errors.CheckViolation):
        _insert(pg_conn, ventilation_scope, volume_version="")
    pg_conn.rollback()


def test_a_datastream_split_against_itself_is_refused(pg_conn, ventilation_scope) -> None:
    with pytest.raises(psycopg.errors.CheckViolation):
        _insert(
            pg_conn, ventilation_scope, right_datastream_id=ventilation_scope["left"]
        )
    pg_conn.rollback()


def test_a_split_with_no_author_is_refused_by_the_database_too(
    pg_conn, ventilation_scope
) -> None:
    with pytest.raises(psycopg.errors.CheckViolation):
        _insert(pg_conn, ventilation_scope, computed_by=" ")
    pg_conn.rollback()


# ---------------------------------------------------------------------------
# Nothing may rewrite a published split.
# ---------------------------------------------------------------------------


def test_the_writer_role_was_granted_no_update_on_a_published_split(
    pg_conn, ventilation_scope
) -> None:
    """A dated split is not a mutable field.

    Re-splitting a key under a corrected volume is a gesture no story has opened,
    and the privilege is absent rather than lying about for one to arrive without
    one. Migration 310's REVOKE is what says so: migration 207's
    `ALTER DEFAULT PRIVILEGES` hands every new `app.` table all four privileges,
    so a narrow GRANT alone states something the database does not do.
    """
    _record(pg_conn, ventilation_scope, _shares(1, 3))
    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        with pg_conn.cursor() as cur:
            cur.execute(
                "UPDATE app.analytics_alignment_weights SET weight = 0.5 "
                "WHERE project_id = %s",
                (ventilation_scope["project_id"],),
            )
    pg_conn.rollback()


def test_the_writer_role_KEEPS_delete_because_the_erasure_needs_it(pg_conn) -> None:
    """DELETE is kept, and that is not an oversight -- it is the right to erasure.

    This table is an ON DELETE CASCADE child of `app.organizations`, which is
    scope (b) of `test_the_erasure_hatch_is_a_privilege_too.py`. PostgreSQL checks
    the TABLE PRIVILEGE before any trigger, so a role with no DELETE turns a
    customer's erasure request into a 42501 on a statement nothing gets to
    explain -- the failure migration 198 found on `app.org_plan_history` and 277
    stated the final posture for. Revoking DELETE here was tried first and the
    ratified guard refused it.
    """
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT has_table_privilege('connector', 'app.analytics_alignment_weights', %s)",
            ("DELETE",),
        )
        assert cur.fetchone()[0] is True
        cur.execute(
            "SELECT has_table_privilege('connector', 'app.analytics_alignment_weights', %s)",
            ("UPDATE",),
        )
        assert cur.fetchone()[0] is False
    pg_conn.rollback()


def test_the_weights_of_one_pair_do_not_leak_into_another_key_version(
    pg_conn, ventilation_scope
) -> None:
    _record(pg_conn, ventilation_scope, _shares(1, 3))
    other = list_ventilation_weights(
        pg_conn,
        project_id=ventilation_scope["project_id"],
        left_datastream_id=ventilation_scope["left"],
        right_datastream_id=ventilation_scope["right"],
        common_key_version_id="mckv_a_version_nobody_declared",
    )
    assert other == []
    pg_conn.rollback()


def test_the_row_level_security_policy_is_armed_on_the_table(pg_conn) -> None:
    """Migration 273's ratchet: an org-scoped table that skips it leaves the sweep silent."""
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT relrowsecurity, relforcerowsecurity "
            "FROM pg_class WHERE oid = 'app.analytics_alignment_weights'::regclass"
        )
        enabled, forced = cur.fetchone()
    assert enabled and forced


def _lock_key(scope, key: str, day) -> str:
    """The advisory-lock string `record_ventilation_weights` derives, verbatim."""
    return "\x1f".join(
        str(part)
        for part in (
            scope["project_id"],
            scope["left"],
            scope["right"],
            scope["common_key_version_id"],
            key,
            day,
        )
    )


def test_two_concurrent_writers_of_one_key_day_cannot_both_hold_the_lock(
    pg_conn, ventilation_scope
):
    """The write-skew a post-write re-read could not close, closed by the lock.

    Two runs writing DISJOINT right_row_keys to one (key, day) never conflict on
    the per-row unique index, so under READ COMMITTED both used to insert a full
    split and commit a (key, day) summing to 2. The advisory transaction lock
    serialises them: two writers of the SAME (key, day) cannot both hold it, so
    the second blocks until the first commits and then sees its rows. A different
    (key, day) hashes elsewhere and never contends.
    """
    import os

    same = _lock_key(ventilation_scope, "key_1", DAY)
    other = _lock_key(ventilation_scope, "key_2", DAY)

    # Connection A takes the lock for (key_1, DAY) and holds it (no commit).
    with pg_conn.cursor() as cur_a:
        cur_a.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (same,))

        other_conn = psycopg.connect(os.environ["TEST_POSTGRES_DSN"])
        try:
            with other_conn.cursor() as cur_b:
                # A second writer of the SAME (key, day) cannot acquire it.
                cur_b.execute(
                    "SELECT pg_try_advisory_xact_lock(hashtextextended(%s, 0))", (same,)
                )
                assert cur_b.fetchone()[0] is False
                # A writer of a DIFFERENT (key, day) is never blocked.
                cur_b.execute(
                    "SELECT pg_try_advisory_xact_lock(hashtextextended(%s, 0))", (other,)
                )
                assert cur_b.fetchone()[0] is True
            other_conn.rollback()
        finally:
            other_conn.close()
    pg_conn.rollback()
