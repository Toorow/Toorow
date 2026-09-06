"""The journey that walked around the trial cap: stop one, then roll it back.

THE DEFECT THIS FILE EXISTS FOR. `app.datastreams.enabled` is the column the
trial allowance counts (`trial_enforcement._count_active_datastreams`:
`enabled = TRUE AND archived_at IS NULL`). Stopping a Datastream writes
`enabled = FALSE` and LEAVES `lifecycle_state = 'active'` and `archived_at`
NULL (`schedule_mcp.set_schedule`), so a stopped Datastream is not counted --
its allowance goes back to the org. `dataset_recovery.rollback_dataset` writes
`lifecycle_state = 'active', enabled = TRUE` and, until 2026-08-21, asked the
allowance nothing. So:

    3 running (cap = 3) -> stop one (2) -> activate a fourth (3)
                        -> roll the stopped one back (4)

Four running on a plan of three, through two doors that each answered
correctly. The tracker closed this as harmless on the argument that "the state
is already counted"; that argument is true for an ARCHIVED Datastream and false
for a STOPPED one, and stopping is the ordinary gesture the console offers.

WHY THE JOURNEY AND NOT A UNIT TEST OF THE GUARD. A unit test of
`check_datastream_limit` was already green while the cap was walkable: what was
missing was not the rule but its presence in one statement. Only the sequence
proves it, so the sequence is what is written here -- with the real acts
(`set_schedule`, `create_datastream`, `rollback_dataset`), the real counter, and
a real Postgres.

The org has no `app.org_plan` row on purpose: `org_entitlements.get_org_plan`
derives the trial default (`max_datastreams = 3`) for an org that was never
explicitly upgraded, which is the state every new org is in. The first
assertion of the journey pins that number rather than assuming it.

Nothing is committed: `live_postgres` rolls the whole transaction back.
"""

from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

import pytest

_SERVER = Path(__file__).resolve().parents[2]
if str(_SERVER) not in sys.path:
    sys.path.insert(0, str(_SERVER))

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def _id(prefix: str) -> str:
    return f"{prefix}{uuid.uuid4().hex[:12]}"


def _ulid_id(prefix: str) -> str:
    """A ULID-shaped id -- the shape `ck_datastream_executions_id` and friends want."""
    import random

    return prefix + "".join(random.choice(_CROCKFORD) for _ in range(26))


# ---------------------------------------------------------------------------
# The world: one trial org, one project, three RUNNING Datastreams, and enough
# publication history on the first of them for a rollback to have a target.
# ---------------------------------------------------------------------------


def _make_org_and_project(conn) -> tuple[str, str]:
    org_id, project_id = _id("org_"), _id("proj_")
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO app.organizations (id, name, slug, created_by) VALUES (%s,%s,%s,%s)",
            (org_id, "Trial Cap Journey", org_id.lower(), "owner@example.com"),
        )
        cur.execute(
            "INSERT INTO app.projects (id, org_id, name, slug, created_by) "
            "VALUES (%s,%s,%s,%s,%s)",
            (project_id, org_id, "Trial Cap Journey", project_id.lower(), "owner@example.com"),
        )
    return org_id, project_id


def _make_running_datastream(conn, org_id: str, project_id: str, label: str) -> str:
    """A Datastream as activation leaves it: `lifecycle_state='active'`, `enabled=TRUE`."""
    ds_id = _id("ds_")
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.datastreams
                (id, project_id, org_id, name, module_name, source_kind, enabled,
                 lifecycle_state, schedule_mode, created_by)
            VALUES (%s,%s,%s,%s,'generic','connector_pull',TRUE,'active','nightly',%s)
            """,
            (ds_id, project_id, org_id, label, "owner@example.com"),
        )
    return ds_id


def _make_publication_history(conn, project_id: str, ds_id: str) -> tuple[str, str]:
    """Two published executions + their append-only log rows; return (prior, current).

    The live pointer is left on *current*, so a rollback has a strictly older
    retained target inside its window -- the only shape `rollback_dataset` will
    act on.
    """
    plan_id, mapping_id = _id("dsp_"), _id("dmap_")
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.datastream_plan_versions
                (id, datastream_id, project_id, version_number, contract_version,
                 source_kind, writer_kind, destination_policy, normalized_payload,
                 content_hash, idempotency_key_hash, created_by)
            VALUES (%s,%s,%s,1,'1','connector_pull','toorow','managed_raw','{}'::jsonb,
                    repeat('a',64), repeat('b',64), 'owner@example.com')
            """,
            (plan_id, ds_id, project_id),
        )
        cur.execute(
            """
            INSERT INTO app.datastream_mapping_versions
                (id, datastream_id, project_id, version_number, mapping_contract_version,
                 source_schema_hash, plan_version_id, content_hash, ossie_spec_version,
                 toorow_extension_version, executable, mapping_payload, ossie_projection,
                 idempotency_key_hash, created_by)
            VALUES (%s,%s,%s,1,'1',repeat('a',64),%s,repeat('c',64),'0.1.1','1',TRUE,
                    '{}'::jsonb,'{}'::jsonb,repeat('d',64),'owner@example.com')
            """,
            (mapping_id, ds_id, project_id, plan_id),
        )

    executions = []
    for content, rows in (("a" * 64, 90), ("b" * 64, 100)):
        exec_id = _ulid_id("dse_")
        log_id = _ulid_id("dplog_")
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO app.datastream_executions
                    (id, datastream_id, project_id, plan_version_id, mapping_version_id,
                     projection_plan_ref, state, content_hash, row_count, created_by)
                VALUES (%s,%s,%s,%s,%s,'{}'::jsonb,'published',%s,%s,'owner@example.com')
                """,
                (exec_id, ds_id, project_id, plan_id, mapping_id, content, rows),
            )
            cur.execute(
                """
                INSERT INTO app.datastream_publication_log
                    (id, execution_id, datastream_id, project_id, plan_version_id,
                     mapping_version_id, content_hash, row_count, published_by,
                     rollback_deadline, retained)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'owner@example.com',
                        NOW() + INTERVAL '30 days', TRUE)
                """,
                (log_id, exec_id, ds_id, project_id, plan_id, mapping_id, content, rows),
            )
        executions.append(exec_id)

    prior, current = executions
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE app.datastreams
            SET current_published_execution_id = %s,
                current_plan_version_id = %s,
                current_mapping_version_id = %s
            WHERE id = %s AND project_id = %s
            """,
            (current, plan_id, mapping_id, ds_id, project_id),
        )
    return prior, current


def _running_count(conn, org_id: str) -> int:
    """The SAME number the refusal compares against -- read through the product."""
    from core.trial_enforcement import _count_active_datastreams

    return _count_active_datastreams(org_id, conn)


# ---------------------------------------------------------------------------
# The journey.
# ---------------------------------------------------------------------------


def test_rollback_of_a_stopped_datastream_cannot_exceed_the_trial_cap(live_postgres) -> None:
    """3 running -> stop one -> activate a fourth -> roll the stopped one back.

    Every step is a real act. The last one is the statement that used to write
    `enabled = TRUE` without asking anything, and the assertion is that it now
    refuses -- with the org's own numbers, and with nothing half-written.
    """
    from core.dataset_recovery import rollback_dataset
    from core.datastreams import create_datastream
    from core.org_entitlements import resolve_entitlements
    from core.schedule_mcp import set_schedule
    from core.trial_enforcement import TrialDatastreamLimitError

    conn = live_postgres
    org_id, project_id = _make_org_and_project(conn)

    # THE INSTRUMENT FIRST. If the plan did not resolve to the bounded trial
    # default, every assertion below would pass vacuously.
    cap = resolve_entitlements(org_id)["max_datastreams"]
    assert cap == 3, (
        f"this journey measures the trial cap and the org resolved to {cap!r}; "
        "check that PLATFORM_DB_URL points at the disposable base"
    )

    ds_stopped = _make_running_datastream(conn, org_id, project_id, "Spend -- daily")
    _make_running_datastream(conn, org_id, project_id, "Performance -- daily")
    _make_running_datastream(conn, org_id, project_id, "Revenue -- daily")
    prior, current = _make_publication_history(conn, project_id, ds_stopped)
    assert _running_count(conn, org_id) == 3

    # 1) STOP one. `lifecycle_state` stays 'active' and `archived_at` stays NULL --
    #    this is exactly why the allowance comes back.
    set_schedule(
        conn,
        project_id=project_id,
        datastream_id=ds_stopped,
        enabled=False,
        identity="owner@example.com",
    )
    assert _running_count(conn, org_id) == 2
    with conn.cursor() as cur:
        cur.execute(
            "SELECT enabled, lifecycle_state, archived_at FROM app.datastreams WHERE id = %s",
            (ds_stopped,),
        )
        assert cur.fetchone() == (False, "active", None), (
            "a stopped Datastream is still 'active' with no archived_at -- if this "
            "ever changes, the hole this test guards has moved somewhere else"
        )

    # 2) The freed allowance is really spendable: a fourth Datastream goes live.
    #    Creation is used here rather than publish-activate because the two are
    #    the same act to the counter -- both call `check_datastream_limit` and
    #    both end at `enabled = TRUE`. What matters to this journey is that the
    #    org is back at its cap by a LEGITIMATE act.
    create_datastream(
        {"name": "Context -- daily", "module_name": "generic", "enabled": True},
        project_id,
        "owner@example.com",
        conn,
    )
    assert _running_count(conn, org_id) == 3

    # 3) THE STEP THAT USED TO OVERSHOOT. Rolling the stopped Datastream back
    #    restores `enabled = TRUE` -- a fourth running Datastream on a plan of
    #    three.
    with pytest.raises(TrialDatastreamLimitError) as refusal:
        rollback_dataset(
            conn,
            datastream_id=ds_stopped,
            project_id=project_id,
            actor="owner@example.com",
            target_execution_id=prior,
            manage_transaction=False,
        )

    assert refusal.value.limit == 3
    assert refusal.value.current == 3
    # THE SENTENCE NAMES THE GESTURE, not the column and not the count table.
    assert "Turn off a Datastream you no longer collect" in refusal.value.message

    # 4) NOTHING HALF-WRITTEN. The refusal fires before the swap, so the pointer,
    #    the stopped flag and the publication log are all as they were.
    assert _running_count(conn, org_id) == 3
    with conn.cursor() as cur:
        cur.execute(
            "SELECT enabled, current_published_execution_id FROM app.datastreams "
            "WHERE id = %s AND project_id = %s",
            (ds_stopped, project_id),
        )
        assert cur.fetchone() == (False, current)
        cur.execute(
            "SELECT COUNT(*) FROM app.datastream_publication_log WHERE datastream_id = %s",
            (ds_stopped,),
        )
        assert cur.fetchone()[0] == 2, "a refused rollback appended a publication row"


def test_rollback_of_a_running_datastream_at_the_cap_is_not_refused(live_postgres) -> None:
    """Recovery must stay available to a saturated org.

    A Datastream that is ALREADY running changes no count when its pointer moves
    back, so the guard must skip it. Refusing here would freeze the last
    Datastream of every trial org on whatever version it happens to carry --
    and a rollback is the act a saturated org needs most.
    """
    from core.dataset_recovery import rollback_dataset

    conn = live_postgres
    org_id, project_id = _make_org_and_project(conn)
    ds_running = _make_running_datastream(conn, org_id, project_id, "Spend -- daily")
    _make_running_datastream(conn, org_id, project_id, "Performance -- daily")
    _make_running_datastream(conn, org_id, project_id, "Revenue -- daily")
    prior, current = _make_publication_history(conn, project_id, ds_running)
    assert _running_count(conn, org_id) == 3

    result = rollback_dataset(
        conn,
        datastream_id=ds_running,
        project_id=project_id,
        actor="owner@example.com",
        target_execution_id=prior,
        manage_transaction=False,
    )

    assert result["rolled_back_from"] == current
    assert result["rolled_back_to"] == prior
    assert _running_count(conn, org_id) == 3
