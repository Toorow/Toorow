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
            INSERT INTO app.projects (id, name, slug, status, currency, timezone, created_by,
                org_id)
            VALUES (%s, %s, %s, 'active', 'EUR', 'Europe/Paris', 'system', 'org_test_fixture')
            """,
            (project_id, "MP Test", project_id),
        )
    conn.commit()
    return project_id


def _drop_project(conn, project_id: str) -> None:
    """Tear down a test plan tree.

    Published versions/allocations are immutable by trigger; the migration owner
    (the `connector` role that ran migration 040) may DISABLE TRIGGER on the
    tables it owns for this maintenance path only, then re-enable them.
    """
    with conn.cursor() as cur:
        cur.execute("ALTER TABLE app.plan_allocation_daily DISABLE TRIGGER USER")
        cur.execute("ALTER TABLE app.media_plan_versions DISABLE TRIGGER USER")
        try:
            cur.execute(
                """
                DELETE FROM app.plan_allocation_daily WHERE version_id IN (
                    SELECT v.id FROM app.media_plan_versions v
                    JOIN app.media_plans p ON p.id = v.plan_id WHERE p.project_id = %s
                )
                """,
                (project_id,),
            )
            cur.execute(
                """
                DELETE FROM app.media_plan_lines WHERE version_id IN (
                    SELECT v.id FROM app.media_plan_versions v
                    JOIN app.media_plans p ON p.id = v.plan_id WHERE p.project_id = %s
                )
                """,
                (project_id,),
            )
            cur.execute(
                """
                DELETE FROM app.media_plan_versions WHERE plan_id IN (
                    SELECT id FROM app.media_plans WHERE project_id = %s
                )
                """,
                (project_id,),
            )
            cur.execute("DELETE FROM app.media_plans WHERE project_id = %s", (project_id,))
            cur.execute("DELETE FROM app.audit_log WHERE identity = 'tester'")
            cur.execute("DELETE FROM app.projects WHERE id = %s", (project_id,))
        finally:
            cur.execute("ALTER TABLE app.plan_allocation_daily ENABLE TRIGGER USER")
            cur.execute("ALTER TABLE app.media_plan_versions ENABLE TRIGGER USER")
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
        assert active[0][0] == v2["id"]

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
            active_plans = {r[0] for r in cur.fetchall()}
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
