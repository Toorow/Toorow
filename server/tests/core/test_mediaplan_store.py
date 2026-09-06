"""toorow -- Live-Postgres tests for mediaplan_store.py (Story 22.1).

Runs against the opt-in test Postgres (TEST_POSTGRES_DSN) so the append-only
triggers, the one-active-pointer partial index, the allocation immutability guard,
and the SUM(allocations)=budget invariant are proven against the REAL schema
(migration 040). Skips when Postgres is unreachable.

Covers: atomic publication, invariant proven in DB, immutability (UPDATE of a
published version blocked; DELETE of a published version's allocations blocked),
two overlapping active plans coexisting, deterministic re-publication, diff.
"""

from __future__ import annotations

import os
import uuid
from decimal import Decimal

import pytest

from tests.conftest import purge_fixture_project

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")


def _pg_reachable() -> bool:
    if not os.environ.get("TEST_POSTGRES_DSN"):
        return False
    try:
        import psycopg

        with psycopg.connect(os.environ["TEST_POSTGRES_DSN"], connect_timeout=2) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
        return True
    except Exception:
        return False


pg_available = pytest.mark.skipif(not _pg_reachable(), reason="platform Postgres not reachable")


def _connect():
    import psycopg

    return psycopg.connect(os.environ["TEST_POSTGRES_DSN"])


def _seed_project(conn) -> str:
    project_id = f"proj-mp-{uuid.uuid4().hex[:8]}"
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.projects (id, name, slug, status, created_by, org_id)
            VALUES (%s, %s, %s, 'active', 'system', 'org_test_fixture')
            """,
            (project_id, "MP Test", project_id),
        )
    conn.commit()
    return project_id


def _drop_project(conn, project_id: str) -> None:
    conn = _connect()
    with conn.cursor():
        # THE WHOLE TREE, THROUGH THE PRODUCTION GRAPH -- not a hand-written list.
        #
        # Three things were wrong here at once, and each is a class this harness
        # already has an answer for:
        #
        #   * `ALTER TABLE ... DISABLE TRIGGER USER` requires OWNING the table,
        #     and fixtures connect as a role that owns nothing. The erasure flag
        #     `purge_fixture_project` sets is what the immutability triggers
        #     yield to (migration 099, extended by 264), so nothing needs
        #     disabling.
        #   * `DELETE FROM app.audit_log` is refused, CORRECTLY: 099 excludes it
        #     from the escape hatch on purpose -- the audit log is the durable
        #     trace OF an erasure and must survive it. The fixture asked for
        #     something the product forbids, and got a refusal it read as a bug.
        #   * the per-table list loses the race to the next governed table.
        purge_fixture_project(conn, project_id)
    conn.commit()



_LINES = [
    {
        "line_key": "digital/meta",
        "label": "Meta prospecting",
        "channel": "Meta",
        "start_date": "2026-03-15",
        "end_date": "2026-04-28",
        "budget": "10000.00",
    },
    {
        "line_key": "tv/national",
        "label": "TV national",
        "channel": "TV",
        "start_date": "2026-03-01",
        "end_date": "2026-03-31",
        "budget": "5000.00",
        "is_plan_only": True,
    },
]


@pg_available
def test_publish_materialises_and_invariant_holds():
    from core.mediaplan_store import (
        create_plan,
        create_version_with_lines,
        publish_version,
    )

    conn = _connect()
    project_id = _seed_project(conn)
    try:
        plan = create_plan(conn, project_id=project_id, name="Plan A", created_by="tester")
        version = create_version_with_lines(
            conn, plan_id=plan["id"], lines=_LINES, source_note="import.xlsx", created_by="tester"
        )
        conn.commit()
        assert version["status"] == "candidate"
        assert version["is_active"] is False

        published = publish_version(conn, version_id=version["id"], published_by="tester")
        conn.commit()
        assert published["status"] == "published"
        assert published["is_active"] is True

        # Invariant proven in DB: SUM(amount) per line == line budget.
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT l.budget, SUM(a.amount)
                FROM app.media_plan_lines l
                JOIN app.plan_allocation_daily a ON a.line_id = l.id
                WHERE l.version_id = %s
                GROUP BY l.id, l.budget
                """,
                (version["id"],),
            )
            rows = cur.fetchall()
        assert rows, "no allocations materialised"
        for budget, total in rows:
            assert Decimal(budget) == Decimal(total)

        # 45 daily rows for the digital line; plan_only line ALSO gets allocations.
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT COUNT(*) FROM app.plan_allocation_daily a
                JOIN app.media_plan_lines l ON l.id = a.line_id
                WHERE l.version_id = %s AND l.line_key = 'digital/meta'
                """,
                (version["id"],),
            )
            assert cur.fetchone()[0] == 45
    finally:
        _drop_project(conn, project_id)
        conn.close()


@pg_available
def test_published_version_immutable_and_allocations_frozen():
    import psycopg
    from core.mediaplan_store import (
        create_plan,
        create_version_with_lines,
        publish_version,
    )

    conn = _connect()
    project_id = _seed_project(conn)
    try:
        plan = create_plan(conn, project_id=project_id, name="Plan Imm", created_by="tester")
        version = create_version_with_lines(
            conn, plan_id=plan["id"], lines=_LINES, created_by="tester"
        )
        publish_version(conn, version_id=version["id"], published_by="tester")
        conn.commit()

        # UPDATE of a forbidden column on a published version -> trigger raises.
        with pytest.raises(psycopg.errors.RaiseException):
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE app.media_plan_versions SET source_note = 'x' WHERE id = %s",
                    (version["id"],),
                )
        conn.rollback()

        # DELETE of a published version's allocations -> trigger raises.
        with pytest.raises(psycopg.errors.RaiseException):
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM app.plan_allocation_daily WHERE version_id = %s",
                    (version["id"],),
                )
        conn.rollback()

        # DELETE of the published version row -> trigger raises.
        with pytest.raises(psycopg.errors.RaiseException):
            with conn.cursor() as cur:
                cur.execute("DELETE FROM app.media_plan_versions WHERE id = %s", (version["id"],))
        conn.rollback()
    finally:
        _drop_project(conn, project_id)
        conn.close()


@pg_available
def test_pointer_flips_atomically_on_republish():
    from core.mediaplan_store import (
        create_plan,
        create_version_with_lines,
        publish_version,
    )

    conn = _connect()
    project_id = _seed_project(conn)
    try:
        plan = create_plan(conn, project_id=project_id, name="Plan V", created_by="tester")
        v1 = create_version_with_lines(conn, plan_id=plan["id"], lines=_LINES, created_by="tester")
        publish_version(conn, version_id=v1["id"], published_by="tester")
        conn.commit()

        lines_v2 = [dict(_LINES[0], budget="12000.00"), dict(_LINES[1])]
        v2 = create_version_with_lines(
            conn, plan_id=plan["id"], lines=lines_v2, created_by="tester"
        )
        publish_version(conn, version_id=v2["id"], published_by="tester")
        conn.commit()

        # Exactly one active version, and it is v2.
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id FROM app.media_plan_versions WHERE plan_id = %s AND is_active",
                (plan["id"],),
            )
            active = cur.fetchall()
        assert len(active) == 1
        # `str()` because psycopg returns a UUID OBJECT for a uuid column while
        # the store hands back its text form. Comparing the two raw is a false
        # red about types, not about which version is active.
        assert str(active[0][0]) == v2["id"]

        # v1 still readable & intact (immutable): its allocation still sums to 10000.
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT SUM(a.amount) FROM app.plan_allocation_daily a
                JOIN app.media_plan_lines l ON l.id = a.line_id
                WHERE l.version_id = %s AND l.line_key = 'digital/meta'
                """,
                (v1["id"],),
            )
            assert Decimal(cur.fetchone()[0]) == Decimal("10000.00")
    finally:
        _drop_project(conn, project_id)
        conn.close()


@pg_available
def test_two_overlapping_active_plans_coexist():
    from core.mediaplan_store import (
        create_plan,
        create_version_with_lines,
        publish_version,
    )

    conn = _connect()
    project_id = _seed_project(conn)
    try:
        plan_a = create_plan(conn, project_id=project_id, name="Plan Over A", created_by="tester")
        plan_b = create_plan(conn, project_id=project_id, name="Plan Over B", created_by="tester")
        va = create_version_with_lines(
            conn, plan_id=plan_a["id"], lines=_LINES, created_by="tester"
        )
        vb = create_version_with_lines(
            conn, plan_id=plan_b["id"], lines=_LINES, created_by="tester"
        )
        publish_version(conn, version_id=va["id"], published_by="tester")
        publish_version(conn, version_id=vb["id"], published_by="tester")
        conn.commit()

        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT plan_id FROM app.media_plan_versions
                WHERE plan_id IN (%s, %s) AND is_active
                """,
                (plan_a["id"], plan_b["id"]),
            )
            # `str()` for the same reason as above: a uuid column comes back as a
            # UUID object, and a set of those never equals a set of strings.
            active_plans = {str(r[0]) for r in cur.fetchall()}
        assert active_plans == {plan_a["id"], plan_b["id"]}
    finally:
        _drop_project(conn, project_id)
        conn.close()


@pg_available
def test_republish_deterministic_same_allocations():
    from core.mediaplan_store import (
        create_plan,
        create_version_with_lines,
        publish_version,
    )

    conn = _connect()
    project_id = _seed_project(conn)
    try:
        plan = create_plan(conn, project_id=project_id, name="Plan Det", created_by="tester")
        v1 = create_version_with_lines(conn, plan_id=plan["id"], lines=_LINES, created_by="tester")
        publish_version(conn, version_id=v1["id"], published_by="tester")
        v2 = create_version_with_lines(conn, plan_id=plan["id"], lines=_LINES, created_by="tester")
        publish_version(conn, version_id=v2["id"], published_by="tester")
        conn.commit()

        # Same input lines -> byte-identical (day, amount) sequence per line_key.
        def _alloc(version_id):
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT l.line_key, a.day, a.amount
                    FROM app.plan_allocation_daily a
                    JOIN app.media_plan_lines l ON l.id = a.line_id
                    WHERE l.version_id = %s
                    ORDER BY l.line_key, a.day
                    """,
                    (version_id,),
                )
                return [(k, d.isoformat(), str(amt)) for k, d, amt in cur.fetchall()]

        assert _alloc(v1["id"]) == _alloc(v2["id"])
    finally:
        _drop_project(conn, project_id)
        conn.close()


@pg_available
def test_diff_versions_added_removed_changed():
    from core.mediaplan_store import create_plan, create_version_with_lines, diff_versions

    conn = _connect()
    project_id = _seed_project(conn)
    try:
        plan = create_plan(conn, project_id=project_id, name="Plan Diff", created_by="tester")
        v1 = create_version_with_lines(conn, plan_id=plan["id"], lines=_LINES, created_by="tester")
        # v2: change budget of digital, drop TV, add radio.
        lines_v2 = [
            dict(_LINES[0], budget="11000.00"),
            {
                "line_key": "radio/nat",
                "label": "Radio",
                "channel": "Radio",
                "start_date": "2026-04-01",
                "end_date": "2026-04-30",
                "budget": "2000.00",
            },
        ]
        v2 = create_version_with_lines(
            conn, plan_id=plan["id"], lines=lines_v2, created_by="tester"
        )
        conn.commit()

        diff = diff_versions(conn, version_a=v1["id"], version_b=v2["id"])
        assert {ln["line_key"] for ln in diff["added"]} == {"radio/nat"}
        assert {ln["line_key"] for ln in diff["removed"]} == {"tv/national"}
        changed_keys = {c["line_key"] for c in diff["changed"]}
        assert changed_keys == {"digital/meta"}
        assert "budget" in diff["changed"][0]["changes"]
    finally:
        _drop_project(conn, project_id)
        conn.close()


@pg_available
def test_concurrent_first_publish_leaves_exactly_one_active():
    """Review F-4: the one-active-pointer race on FIRST publish (no prior active).

    Two candidates of the SAME plan published from two connections: the demote
    step locks nothing (no active row yet), so the only protection is the partial
    unique index (plan_id) WHERE is_active. The loser must surface the clean
    MediaPlanStateError (409), never a corrupt double-active state.
    """
    import threading
    import time

    from core.mediaplan_store import (
        MediaPlanStateError,
        create_plan,
        create_version_with_lines,
        publish_version,
    )

    conn = _connect()
    project_id = _seed_project(conn)
    conn_a = _connect()
    conn_b = _connect()
    try:
        plan = create_plan(conn, project_id=project_id, name="Plan Race", created_by="tester")
        v1 = create_version_with_lines(conn, plan_id=plan["id"], lines=_LINES, created_by="tester")
        v2 = create_version_with_lines(conn, plan_id=plan["id"], lines=_LINES, created_by="tester")
        conn.commit()

        # A publishes but holds its transaction open (uncommitted active pointer).
        publish_version(conn_a, version_id=v1["id"], published_by="tester")

        loser_errors: list[Exception] = []

        def _publish_b() -> None:
            try:
                publish_version(conn_b, version_id=v2["id"], published_by="tester")
                conn_b.commit()
            except MediaPlanStateError as exc:
                conn_b.rollback()
                loser_errors.append(exc)

        thread = threading.Thread(target=_publish_b)
        thread.start()
        time.sleep(0.5)  # let B reach the index-blocked promote
        conn_a.commit()
        thread.join(timeout=15)

        assert not thread.is_alive(), "publish B deadlocked instead of failing cleanly"
        assert len(loser_errors) == 1, "loser must get MediaPlanStateError (409), not corrupt state"
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM app.media_plan_versions WHERE plan_id = %s AND is_active",
                (plan["id"],),
            )
            assert cur.fetchone()[0] == 1
    finally:
        conn_a.close()
        conn_b.close()
        _drop_project(conn, project_id)
        conn.close()


# ---------------------------------------------------------------------------
# The carrier Datastream (migration 303) -- ratified 2026-08-24.
#
# `file-source-ingestion.md`, amendment "a project carries one or several media
# plans, each on its own carrier Datastream". These three tests are its three
# `Incomplete if` clauses, taken one by one against the real schema -- the only
# place a partial unique index can be proven to mean what its sentence says.
# ---------------------------------------------------------------------------


def _seed_carrier(conn, project_id: str, suffix: str) -> str:
    """A file-source Datastream of this project, and nothing more.

    `source_kind = 'managed_feed'` and `module_name` NULL, which is what
    `create_datastream` writes for a pushed source: the row has to be the shape
    the product writes, or the foreign key it proves is a foreign key to
    something the product never makes.
    """
    datastream_id = f"ds-mp-{suffix}-{uuid.uuid4().hex[:8]}"
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.datastreams
                (id, project_id, name, source_kind, created_by, org_id)
            VALUES (%s, %s, %s, 'managed_feed', 'tester', 'org_test_fixture')
            """,
            (datastream_id, project_id, f"Plan file {suffix} {uuid.uuid4().hex[:6]}"),
        )
    conn.commit()
    return datastream_id


@pg_available
def test_a_second_plan_lives_on_a_second_carrier_and_the_project_holds_both():
    """« a second plan cannot exist in a project because the first one holds the
    only carrier » -- the first `Incomplete if`, refuted against the index.

    THE UNIQUENESS IS PER CARRIER AND NEVER PER PROJECT. A partial unique index
    on `carrier_datastream_id` alone says "one live plan per Datastream"; an
    index that also constrained the project would say something the decision
    explicitly opens. Two plans, two carriers, one project: both stand.
    """
    from core.mediaplan_store import create_plan, get_carrier_plan, list_plans

    conn = _connect()
    project_id = _seed_project(conn)
    try:
        first_carrier = _seed_carrier(conn, project_id, "a")
        second_carrier = _seed_carrier(conn, project_id, "b")

        first = create_plan(
            conn,
            project_id=project_id,
            name="Agency A Q1",
            created_by="tester",
            carrier_datastream_id=first_carrier,
        )
        second = create_plan(
            conn,
            project_id=project_id,
            name="Agency B Q1",
            created_by="tester",
            carrier_datastream_id=second_carrier,
        )
        conn.commit()

        assert first["carrier_datastream_id"] == first_carrier
        assert second["carrier_datastream_id"] == second_carrier
        # THE PROJECT HOLDS BOTH. This is the clause, stated as a count.
        assert {plan["id"] for plan in list_plans(conn, project_id=project_id)} == {
            first["id"],
            second["id"],
        }
        # And each carrier answers with ITS plan, never with the other's.
        assert get_carrier_plan(conn, datastream_id=first_carrier)["id"] == first["id"]
        assert get_carrier_plan(conn, datastream_id=second_carrier)["id"] == second["id"]
    finally:
        _drop_project(conn, project_id)
        conn.close()


@pg_available
def test_one_carrier_carries_one_plan_and_the_refusal_names_the_second_datastream():
    """« two plans with different layouts are forced through one template » --
    refused, and the refusal names the gesture rather than the constraint.

    A carrier is created with the template profile of ITS plan. Letting a second
    plan sit on the same Datastream would be exactly the two-agencies-one-template
    case the decision forbids, so the store refuses -- and the sentence says what
    to do instead, which a unique-violation traceback never would.
    """
    from core.mediaplan_store import MediaPlanCarrierTakenError, create_plan

    conn = _connect()
    project_id = _seed_project(conn)
    try:
        carrier = _seed_carrier(conn, project_id, "solo")
        create_plan(
            conn,
            project_id=project_id,
            name="Agency A Q1",
            created_by="tester",
            carrier_datastream_id=carrier,
        )
        conn.commit()

        with pytest.raises(MediaPlanCarrierTakenError) as raised:
            create_plan(
                conn,
                project_id=project_id,
                name="Agency B Q1",
                created_by="tester",
                carrier_datastream_id=carrier,
            )
        conn.rollback()
        message = str(raised.value)
        # It names the plan already there, and the gesture that repairs.
        assert "Agency A Q1" in message
        assert "second file-source Datastream" in message
        assert raised.value.code == "carrier_already_carries_a_plan"
    finally:
        _drop_project(conn, project_id)
        conn.close()


@pg_available
def test_a_plan_created_without_a_carrier_stays_readable():
    """Nothing is provisioned, and no plan written before the decision breaks.

    `carrier_datastream_id` is NULLABLE on purpose: a plan that arrived by the
    older import path has no carrier, and the console says so rather than
    inventing a Datastream for it -- which is the auto-provisioning the same
    decision forbids in the other direction.
    """
    from core.mediaplan_store import create_plan, get_carrier_plan

    conn = _connect()
    project_id = _seed_project(conn)
    try:
        plan = create_plan(
            conn, project_id=project_id, name="Legacy plan", created_by="tester"
        )
        conn.commit()
        assert plan["carrier_datastream_id"] is None
        # A carrier nobody named answers with nothing, never with the first plan
        # that happens to have none.
        assert get_carrier_plan(conn, datastream_id="") is None
    finally:
        _drop_project(conn, project_id)
        conn.close()
