"""Story 70.3 against a real database -- migration 309's row, table and CHECKs.

``test_analytics_alignment.py`` proves the cascade, which is pure. What it cannot
prove is that the database accepts these writes and refuses the shapes the model
forbids: the seventh capability row exists on a real Project, an arbitration that
names no side is rejected by a CONSTRAINT and not by Python, and the first
decision on a row stands because a UNIQUE index says so.

It also runs :func:`core.analytics_alignment.resolve_dependencies` against real
SQL. That read spans four modules and four statements, and a stubbed connection
cannot tell whether any of the four columns it names still exists.

pg-gated: skipped without ``TEST_POSTGRES_DSN``. It runs offline against the
disposable Postgres (``python scripts/disposable_postgres.py up``), which must
carry migration 309.

No real identifier appears: fixtures are minted from a ULID, the actors are
``owner@example.com`` and ``second@example.com``.
"""

from __future__ import annotations

import psycopg
import pytest
import ulid
from core.analytics_alignment import (
    DECISION_ACCEPTED,
    DECISION_ARBITRATED,
    DEPENDENCY_COMMON_KEY,
    DEPENDENCY_CURRENCY_FX,
    AlignmentDecision,
    list_alignment_decisions,
    record_alignment_decision,
    resolve_dependencies,
)
from core.project_capability_states import (
    CAPABILITY_AVAILABILITY,
    PROJECT_CAPABILITY_KEYS,
    read_capability_state,
)

ACTOR = "owner@example.com"
SECOND_ACTOR = "second@example.com"
CAPABILITY_KEY = "analytics_alignment"


@pytest.fixture
def alignment_scope(pg_conn, inbound_pg_scope):
    """A real pair of Datastreams and a real common key version to cross on.

    Written by hand rather than through ``create_common_key``: what is under test
    is the FOREIGN KEY and the CHECKs of the decisions table, and going through
    the writer would make a column name it happens to spell the thing being
    proved.
    """
    org = inbound_pg_scope["org_id"]
    project = inbound_pg_scope["project_id"]
    left = inbound_pg_scope["datastream_id"]
    suffix = str(ulid.ULID())
    right = f"ds_test_align_{suffix}"[:40]
    # `mck_<ULID>` / `mckv_<ULID>`, exactly: migration 258 CHECKs both shapes with
    # a Crockford-alphabet regex, so a made-up prefix fails on the INSERT rather
    # than on the thing under test.
    key_id = f"mck_{suffix}"
    version_id = f"mckv_{suffix}"

    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO app.datastreams "
            "(id, project_id, org_id, name, created_by, source_kind, enabled, "
            "schedule_mode, refetch_days, date_window_days, lifecycle_state) "
            "VALUES (%s, %s, %s, 'Alignment right side', 'system', "
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
            # One component, because migration 258 CHECKs 1..8: a key that
            # declares nothing is refused by the database as well as by
            # `covering_key_versions`.
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


def _insert(conn, scope: dict, **overrides) -> None:
    row = {
        "id": f"aad_{ulid.ULID()}",
        "org_id": scope["org_id"],
        "project_id": scope["project_id"],
        "left_datastream_id": scope["left"],
        "right_datastream_id": scope["right"],
        "common_key_version_id": scope["common_key_version_id"],
        "left_row_key": "row_1",
        "decision": DECISION_ARBITRATED,
        "right_row_key": "r1",
        "reason": None,
        "decided_by": ACTOR,
    }
    row.update(overrides)
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO app.analytics_alignment_decisions "
            "(id, org_id, project_id, left_datastream_id, right_datastream_id, "
            "common_key_version_id, left_row_key, decision, right_row_key, reason, decided_by) "
            "VALUES (%(id)s, %(org_id)s, %(project_id)s, %(left_datastream_id)s, "
            "%(right_datastream_id)s, %(common_key_version_id)s, %(left_row_key)s, "
            "%(decision)s, %(right_row_key)s, %(reason)s, %(decided_by)s)",
            row,
        )


# ---------------------------------------------------------------------------
# The seventh capability row.
# ---------------------------------------------------------------------------


def test_a_real_project_carries_all_seven_capability_rows(pg_conn, inbound_pg_scope) -> None:
    """The failure migration 243 named, one capability later.

    No statement in ``server/core`` ever INSERTs into ``app.project_capabilities``:
    prepare and confirm both UPDATE, and neither reads the row count. On a
    Project whose seventh row does not exist, turning Analytics Alignment on
    updates zero rows, raises nothing, mints its Configuration Version and
    reports success. Only a real row can prove the seed wrote one.
    """
    project = inbound_pg_scope["project_id"]
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT capability_key, availability, state FROM app.project_capabilities "
            "WHERE project_id = %s ORDER BY capability_key",
            (project,),
        )
        rows = {key: (availability, state) for key, availability, state in cur.fetchall()}
    assert set(rows) == set(PROJECT_CAPABILITY_KEYS)
    availability, state = rows[CAPABILITY_KEY]
    assert availability == CAPABILITY_AVAILABILITY[CAPABILITY_KEY] == "optional"
    # Off by default, which is what the card says and what the CHECK permits.
    assert state == "disabled"
    assert read_capability_state(
        pg_conn, project_id=project, capability_key=CAPABILITY_KEY
    ) == "disabled"


def test_the_database_refuses_an_eighth_capability_key(pg_conn, inbound_pg_scope) -> None:
    """The CHECK is the closed vocabulary, and it is closed in the database too."""
    with pytest.raises(psycopg.errors.CheckViolation):
        with pg_conn.cursor() as cur:
            cur.execute(
                "INSERT INTO app.project_capabilities "
                "(project_id, capability_key, availability, state) "
                "VALUES (%s, 'analytics_alignments', 'optional', 'disabled')",
                (inbound_pg_scope["project_id"],),
            )
    pg_conn.rollback()


def test_the_seventh_capability_may_never_be_written_always_present(
    pg_conn, inbound_pg_scope
) -> None:
    """The pairing CHECK, which is the one that is easy to miss."""
    with pytest.raises(psycopg.errors.CheckViolation):
        with pg_conn.cursor() as cur:
            cur.execute(
                "UPDATE app.project_capabilities SET availability = 'always_present' "
                "WHERE project_id = %s AND capability_key = %s",
                (inbound_pg_scope["project_id"], CAPABILITY_KEY),
            )
    pg_conn.rollback()


# ---------------------------------------------------------------------------
# The decisions table.
# ---------------------------------------------------------------------------


def test_an_arbitration_that_names_no_side_is_refused_by_the_database(
    pg_conn, alignment_scope
) -> None:
    """Python refuses it at construction; the CHECK refuses it to every writer."""
    with pytest.raises(psycopg.errors.CheckViolation):
        _insert(pg_conn, alignment_scope, right_row_key=None)
    pg_conn.rollback()


def test_an_acceptance_that_quietly_names_a_side_is_refused(pg_conn, alignment_scope) -> None:
    """Accepting states that NOTHING answers this row. A side would say the opposite."""
    with pytest.raises(psycopg.errors.CheckViolation):
        _insert(pg_conn, alignment_scope, decision=DECISION_ACCEPTED, right_row_key="r1")
    pg_conn.rollback()


def test_a_decision_with_no_author_is_refused(pg_conn, alignment_scope) -> None:
    with pytest.raises(psycopg.errors.CheckViolation):
        _insert(pg_conn, alignment_scope, decided_by="   ")
    pg_conn.rollback()


def test_a_datastream_aligned_with_itself_is_refused(pg_conn, alignment_scope) -> None:
    with pytest.raises(psycopg.errors.CheckViolation):
        _insert(pg_conn, alignment_scope, right_datastream_id=alignment_scope["left"])
    pg_conn.rollback()


def test_a_third_decision_word_is_refused(pg_conn, alignment_scope) -> None:
    """Two acts, two words. A third would be a state nobody designed a screen for."""
    with pytest.raises(psycopg.errors.CheckViolation):
        _insert(pg_conn, alignment_scope, decision="declined")
    pg_conn.rollback()


def test_the_first_decision_stands_and_the_second_click_reads_it_back(
    pg_conn, alignment_scope
) -> None:
    """A second person never rewrites who decided and when.

    The UNIQUE index plus ``ON CONFLICT DO NOTHING`` is the shape migration 245
    chose for ``app.plan_unmatched_spend_decisions``, and this is the test that
    the writer and the index agree about it.
    """
    first = AlignmentDecision(
        left_row_key="row_1",
        decision=DECISION_ARBITRATED,
        right_row_key="r1",
        decided_by=ACTOR,
        reason="The campaign that ran.",
    )
    stored = record_alignment_decision(
        pg_conn,
        org_id=alignment_scope["org_id"],
        project_id=alignment_scope["project_id"],
        left_datastream_id=alignment_scope["left"],
        right_datastream_id=alignment_scope["right"],
        common_key_version_id=alignment_scope["common_key_version_id"],
        decision=first,
        actor=ACTOR,
    )
    assert stored.decided_by == ACTOR
    assert stored.decided_at is not None

    second = AlignmentDecision(
        left_row_key="row_1",
        decision=DECISION_ARBITRATED,
        right_row_key="r_somewhere_else",
        decided_by=SECOND_ACTOR,
        reason="Changed my mind.",
    )
    again = record_alignment_decision(
        pg_conn,
        org_id=alignment_scope["org_id"],
        project_id=alignment_scope["project_id"],
        left_datastream_id=alignment_scope["left"],
        right_datastream_id=alignment_scope["right"],
        common_key_version_id=alignment_scope["common_key_version_id"],
        decision=second,
        actor=SECOND_ACTOR,
    )
    assert again.decided_by == ACTOR
    assert again.right_row_key == "r1"
    assert again.decided_at == stored.decided_at
    pg_conn.rollback()


def test_the_two_acts_read_back_under_the_key_version_they_were_taken_on(
    pg_conn, alignment_scope
) -> None:
    """A decision belongs to an exact common key version and travels with none other."""
    for decision in (
        AlignmentDecision(
            left_row_key="row_1",
            decision=DECISION_ARBITRATED,
            right_row_key="r1",
            decided_by=ACTOR,
        ),
        AlignmentDecision(
            left_row_key="row_2",
            decision=DECISION_ACCEPTED,
            decided_by=ACTOR,
            reason="Brand campaign, no site tagging.",
        ),
    ):
        record_alignment_decision(
            pg_conn,
            org_id=alignment_scope["org_id"],
            project_id=alignment_scope["project_id"],
            left_datastream_id=alignment_scope["left"],
            right_datastream_id=alignment_scope["right"],
            common_key_version_id=alignment_scope["common_key_version_id"],
            decision=decision,
            actor=ACTOR,
        )
    stored = list_alignment_decisions(
        pg_conn,
        project_id=alignment_scope["project_id"],
        left_datastream_id=alignment_scope["left"],
        right_datastream_id=alignment_scope["right"],
        common_key_version_id=alignment_scope["common_key_version_id"],
    )
    assert {item.left_row_key: item.decision for item in stored} == {
        "row_1": DECISION_ARBITRATED,
        "row_2": DECISION_ACCEPTED,
    }
    assert all(item.decided_by == ACTOR and item.decided_at for item in stored)

    other_version = list_alignment_decisions(
        pg_conn,
        project_id=alignment_scope["project_id"],
        left_datastream_id=alignment_scope["left"],
        right_datastream_id=alignment_scope["right"],
        common_key_version_id="mdmckv_a_version_nobody_declared",
    )
    assert other_version == []
    pg_conn.rollback()


# ---------------------------------------------------------------------------
# The dependency read, against real SQL.
# ---------------------------------------------------------------------------


def test_the_dependency_read_names_the_common_key_and_the_money_on_a_bare_project(
    pg_conn, alignment_scope
) -> None:
    """Four statements over four modules, run for real.

    The Project has a declared common key but no published Datastream mapping
    binding its components, and Currency & FX is `draft` from the seed. So the
    read must name the key AND the money -- and never the relationship, whose
    gesture nobody could take before a key covers the pair.
    """
    result = resolve_dependencies(
        pg_conn,
        project_id=alignment_scope["project_id"],
        left_datastream_id=alignment_scope["left"],
        right_datastream_id=alignment_scope["right"],
    )
    assert not result.satisfied
    assert [item.code for item in result.missing] == [
        DEPENDENCY_COMMON_KEY,
        DEPENDENCY_CURRENCY_FX,
    ]
    assert result.currency_fx_state == "draft"
    assert result.common_key_version_ids == ()
    for item in result.missing:
        assert item.gesture.strip()
    pg_conn.rollback()
