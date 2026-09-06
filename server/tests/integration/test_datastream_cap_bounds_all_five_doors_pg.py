"""An org at its cap is refused at EVERY door that puts a Datastream on the air.

`organization-settings.md`, "Incomplete if": *the ceiling a person reads is not
the ceiling that is enforced*. The page records why that criterion stayed open:
the read is pinned by tests and by G5-T05 in production, but the AGREEMENT
between the ceiling read and the ceiling enforced "has never been observed end to
end, and observing it would saturate a real organization with indelible plan
versions. It closes on a disposable cluster, not here." This is that cluster.

WHAT WAS MEASURED, 2026-08-31. The number agreed and the REACH did not.
`OrgSettings.tsx#PlanSection` told a person *"the cap bounds creation only"*,
while `trial_enforcement.check_datastream_limit` has FIVE callers -- its own
docstring names them, and `grep -rn "check_datastream_limit" server --include=*.py`
outside `tests/` finds exactly those five:

  * `datastreams.create_datastream`            -- creating one;
  * `datastream_activation.publish_activate_mutation` -- publishing a draft;
  * `schedule_mcp.set_schedule`                -- turning a paused one back on;
  * `dataset_recovery.rollback_dataset`        -- restoring one from a rollback;
  * `datastreams.backfill_datastreams`         -- the maintenance sweep.

A person told "creation only" and then refused when they re-arm a paused
Datastream has been given the wrong ceiling by the one screen that exists to
state it. The sentence is repaired; this file is what makes the repaired sentence
a measured claim rather than a better-worded guess.

WHY FIVE TESTS AND NOT ONE. The 48 existing tests of the guard are unit tests
over a `MagicMock` connection: they prove the RULE and can say nothing about
whether a given act reaches it. Two of the five callers were added on 2026-08-21
precisely because the rule was right and absent from the statement. So each door
is driven here through its REAL function, against a real Postgres, and each
assertion is that the refusal arrives carrying the org's own numbers and the
gesture that repairs.

The org has no `app.org_plan` row on purpose -- `org_entitlements` derives the
trial default for an org nobody upgraded, which is the state every new org is in.
Nothing is committed: `live_postgres` rolls the whole transaction back, and the
one door that commits for itself is handed a connection whose `commit` is inert.
"""

from __future__ import annotations

import contextlib
import os
import sys
from pathlib import Path

import pytest

_SERVER = Path(__file__).resolve().parents[2]
if str(_SERVER) not in sys.path:
    sys.path.insert(0, str(_SERVER))

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

# ONE fixture builder, not a second copy of it. The rollback journey
# (`test_trial_limit_rollback_journey_pg.py`) already composes a trial org, its
# project, a running Datastream and a publication history in the exact shapes
# these constraints accept; re-typing them here would be two worlds drifting
# apart while both stayed green.
from tests.integration.test_trial_limit_rollback_journey_pg import (  # noqa: E402
    _id,
    _make_org_and_project,
    _make_publication_history,
    _make_running_datastream,
    _running_count,
    _ulid_id,
)

#: The trial default `org_entitlements` derives for an org nobody upgraded.
EXPECTED_TRIAL_CAP = 3

#: The sentence the refusal must carry -- the GESTURE, never the column.
REPAIR_GESTURE = "Turn off a Datastream you no longer collect"


# ---------------------------------------------------------------------------
# The world: a trial org saturated at its cap.
# ---------------------------------------------------------------------------


def _saturated_org(conn) -> tuple[str, str]:
    """A trial org running exactly `max_datastreams` Datastreams.

    The cap is READ from the product rather than assumed: an org that resolved to
    unlimited would make every refusal assertion below pass vacuously, which is
    the way this whole family of test goes quietly green.
    """
    from core.org_entitlements import resolve_entitlements

    org_id, project_id = _make_org_and_project(conn)
    cap = resolve_entitlements(org_id)["max_datastreams"]
    assert cap == EXPECTED_TRIAL_CAP, (
        f"this test measures the trial cap and the org resolved to {cap!r}; check "
        "that PLATFORM_DB_URL points at the disposable base"
    )
    for index in range(cap):
        _make_running_datastream(conn, org_id, project_id, f"Saturating stream {index}")
    assert _running_count(conn, org_id) == cap
    return org_id, project_id


def _assert_names_the_gesture(refusal) -> None:
    """One refusal, one shape -- whichever door raised it."""
    assert refusal.code == "trial_datastream_limit"
    assert refusal.limit == EXPECTED_TRIAL_CAP
    assert refusal.current == EXPECTED_TRIAL_CAP
    assert REPAIR_GESTURE in refusal.message, refusal.message
    # A refusal that names a control the product does not have would be worse
    # than the sentence saying so (`organization-settings.md`, "Refuses").
    assert "upgrade" not in refusal.message.lower()


def _draft_with_a_reviewed_candidate(conn, org_id: str, project_id: str) -> dict:
    """A draft Datastream whose candidate is `ready` -- what publish-activate takes.

    `enabled = FALSE`, so the draft consumes no allowance: this is the state a
    wizard leaves behind, and publishing it is the act that would make one more
    Datastream run.
    """
    ds_id = _id("ds_")
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.datastreams
                (id, project_id, org_id, name, module_name, source_kind, enabled,
                 lifecycle_state, schedule_mode, created_by)
            VALUES (%s,%s,%s,%s,'generic','connector_pull',FALSE,'draft','nightly',%s)
            """,
            (ds_id, project_id, org_id, "The fourth stream", "owner@example.com"),
        )
    plan_id, mapping_id = _id("dsp_"), _id("dmap_")
    execution_id = _ulid_id("dse_")
    content_hash, row_count = "e" * 64, 77
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
        cur.execute(
            """
            INSERT INTO app.datastream_executions
                (id, datastream_id, project_id, plan_version_id, mapping_version_id,
                 projection_plan_ref, state, content_hash, row_count, created_by)
            VALUES (%s,%s,%s,%s,%s,'{}'::jsonb,'ready',%s,%s,'owner@example.com')
            """,
            (execution_id, ds_id, project_id, plan_id, mapping_id, content_hash, row_count),
        )
    return {
        "datastream_id": ds_id,
        "execution_id": execution_id,
        "project_id": project_id,
        "org_id": org_id,
        "content_hash": content_hash,
        "row_count": row_count,
        "expected_current_execution_id": None,
        "plan_version_id": plan_id,
        "mapping_version_id": mapping_id,
    }


# ---------------------------------------------------------------------------
# Door 1 -- creating one.
# ---------------------------------------------------------------------------


def test_creating_a_datastream_at_the_cap_is_refused(live_postgres) -> None:
    from core.datastreams import create_datastream
    from core.trial_enforcement import TrialDatastreamLimitError

    conn = live_postgres
    org_id, project_id = _saturated_org(conn)

    with pytest.raises(TrialDatastreamLimitError) as refusal:
        create_datastream(
            {"name": "The fourth stream", "module_name": "generic", "enabled": True},
            project_id,
            "owner@example.com",
            conn,
        )
    _assert_names_the_gesture(refusal.value)
    assert _running_count(conn, org_id) == EXPECTED_TRIAL_CAP

    # A DRAFT is not refused -- the cap bounds what RUNS, and the screen says so.
    create_datastream(
        {"name": "A draft", "module_name": "generic", "enabled": False},
        project_id,
        "owner@example.com",
        conn,
    )
    assert _running_count(conn, org_id) == EXPECTED_TRIAL_CAP


# ---------------------------------------------------------------------------
# Door 2 -- publishing a draft (the wizard, and the MCP publication tool).
# ---------------------------------------------------------------------------


def test_publishing_a_draft_at_the_cap_is_refused(live_postgres) -> None:
    from core.datastream_activation import publish_activate_mutation
    from core.trial_enforcement import TrialDatastreamLimitError

    conn = live_postgres
    org_id, project_id = _saturated_org(conn)
    review = _draft_with_a_reviewed_candidate(conn, org_id, project_id)

    with pytest.raises(TrialDatastreamLimitError) as refusal:
        publish_activate_mutation(
            conn,
            review=review,
            actor="owner@example.com",
            operation_id=_id("op_"),
        )
    _assert_names_the_gesture(refusal.value)

    # NOTHING HALF-WRITTEN: the guard sits before the first write of the act, so
    # the candidate is still `ready` and the draft is still a draft.
    assert _running_count(conn, org_id) == EXPECTED_TRIAL_CAP
    with conn.cursor() as cur:
        cur.execute(
            "SELECT state FROM app.datastream_executions WHERE id = %s",
            (review["execution_id"],),
        )
        assert cur.fetchone()[0] == "ready"
        cur.execute(
            "SELECT enabled, lifecycle_state, current_published_execution_id "
            "FROM app.datastreams WHERE id = %s",
            (review["datastream_id"],),
        )
        assert cur.fetchone() == (False, "draft", None)


# ---------------------------------------------------------------------------
# Door 3 -- turning a paused Datastream back on (the schedule door, REST + MCP).
# ---------------------------------------------------------------------------


def test_re_arming_a_paused_datastream_at_the_cap_is_refused(live_postgres) -> None:
    """The door the console's old sentence denied existed.

    Pausing frees an allowance (`enabled = FALSE`, `lifecycle_state` untouched),
    so a person can be at the cap with a paused Datastream beside it and be
    refused when they turn it back on. "The cap bounds creation only" told them
    that could not happen.
    """
    from core.datastreams import create_datastream
    from core.schedule_mcp import set_schedule
    from core.trial_enforcement import TrialDatastreamLimitError

    conn = live_postgres
    org_id, project_id = _saturated_org(conn)

    with conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM app.datastreams WHERE project_id = %s ORDER BY id LIMIT 1",
            (project_id,),
        )
        paused = cur.fetchone()[0]

    set_schedule(
        conn,
        project_id=project_id,
        datastream_id=paused,
        enabled=False,
        identity="owner@example.com",
    )
    assert _running_count(conn, org_id) == EXPECTED_TRIAL_CAP - 1

    # The freed allowance is spent by a legitimate act, so the org is back at
    # its cap with a paused Datastream beside it.
    create_datastream(
        {"name": "The fourth stream", "module_name": "generic", "enabled": True},
        project_id,
        "owner@example.com",
        conn,
    )
    assert _running_count(conn, org_id) == EXPECTED_TRIAL_CAP

    with pytest.raises(TrialDatastreamLimitError) as refusal:
        set_schedule(
            conn,
            project_id=project_id,
            datastream_id=paused,
            enabled=True,
            identity="owner@example.com",
        )
    _assert_names_the_gesture(refusal.value)
    assert _running_count(conn, org_id) == EXPECTED_TRIAL_CAP
    with conn.cursor() as cur:
        cur.execute("SELECT enabled FROM app.datastreams WHERE id = %s", (paused,))
        assert cur.fetchone()[0] is False


# ---------------------------------------------------------------------------
# Door 4 -- restoring one from a rollback.
# ---------------------------------------------------------------------------


def test_rolling_a_paused_datastream_back_at_the_cap_is_refused(live_postgres) -> None:
    """The same shape as door 3, through recovery instead of the schedule.

    The rollback JOURNEY is proved next door
    (`test_trial_limit_rollback_journey_pg.py`); what is asserted here is that
    this door belongs to the same census as the other four and answers with the
    same sentence.
    """
    from core.dataset_recovery import rollback_dataset
    from core.datastreams import create_datastream
    from core.schedule_mcp import set_schedule
    from core.trial_enforcement import TrialDatastreamLimitError

    conn = live_postgres
    org_id, project_id = _saturated_org(conn)

    with conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM app.datastreams WHERE project_id = %s ORDER BY id LIMIT 1",
            (project_id,),
        )
        paused = cur.fetchone()[0]
    prior, current = _make_publication_history(conn, project_id, paused)

    set_schedule(
        conn,
        project_id=project_id,
        datastream_id=paused,
        enabled=False,
        identity="owner@example.com",
    )
    create_datastream(
        {"name": "The fourth stream", "module_name": "generic", "enabled": True},
        project_id,
        "owner@example.com",
        conn,
    )
    assert _running_count(conn, org_id) == EXPECTED_TRIAL_CAP

    with pytest.raises(TrialDatastreamLimitError) as refusal:
        rollback_dataset(
            conn,
            datastream_id=paused,
            project_id=project_id,
            actor="owner@example.com",
            target_execution_id=prior,
            manage_transaction=False,
        )
    _assert_names_the_gesture(refusal.value)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT enabled, current_published_execution_id FROM app.datastreams "
            "WHERE id = %s AND project_id = %s",
            (paused, project_id),
        )
        assert cur.fetchone() == (False, current)


# ---------------------------------------------------------------------------
# Door 5 -- the maintenance sweep, which no person is at the other end of.
# ---------------------------------------------------------------------------


def test_the_backfill_sweep_at_the_cap_refuses_the_row_and_not_the_sweep(
    live_postgres, monkeypatch
) -> None:
    """The sweep writes `enabled = TRUE` literally, so it meets the same guard.

    Two properties at once, and the second is why the guard is inside the loop:
    the bounded org's row is REFUSED, and the sweep itself carries on -- one
    saturated org must not leave every org after it unbackfilled.
    """
    conn = live_postgres
    org_id, project_id = _saturated_org(conn)

    connection_id = _id("cref_")
    with conn.cursor() as cur:
        # THE SWEEP HAS NO SCOPE -- it walks every active credential in the
        # database. On a shared disposable cluster that means it walks whatever
        # the last suite left behind, and a seed missing for an unrelated
        # provider aborts the whole pass before this org's row is reached. The
        # census is narrowed to this transaction's own credential, inside a
        # transaction that is rolled back, so what is measured is this door and
        # not the state of the cluster.
        cur.execute(
            "UPDATE app.connection_ref SET status = 'revoked' WHERE status = 'active'"
        )
        cur.execute(
            """
            INSERT INTO app.connection_ref
                (id, project_id, owner_org_id, provider, status, owner_identity,
                 auth_path, nango_connection_id)
            VALUES (%s,%s,%s,'generic','active',%s,'nango',%s)
            """,
            (connection_id, project_id, org_id, "owner@example.com", connection_id),
        )

    # A SECOND ORG, WITH ROOM. Without it "the sweep carries on" is a sentence
    # and not a measurement: a pass that aborted on the refusal would look
    # exactly the same as one that continued, because there would be nothing
    # after it. This one has no Datastream at all, so its row must be created.
    other_org, other_project = _make_org_and_project(conn)
    other_connection = _id("cref_")
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.connection_ref
                (id, project_id, owner_org_id, provider, status, owner_identity,
                 auth_path, nango_connection_id)
            VALUES (%s,%s,%s,'generic','active',%s,'nango',%s)
            """,
            (other_connection, other_project, other_org, "owner@example.com", other_connection),
        )

    # The sweep opens its own connection and commits at the end. It is handed
    # THIS transaction, with `commit` made inert, so the measurement runs against
    # the real statement and still leaves nothing behind.
    class _NoCommit:
        def __init__(self, wrapped):
            self._wrapped = wrapped

        def __getattr__(self, name):
            return getattr(self._wrapped, name)

        def commit(self):  # noqa: D401 -- deliberately inert
            """The enclosing test transaction decides, not the sweep."""

    @contextlib.contextmanager
    def _handed_the_test_transaction():
        yield _NoCommit(conn)

    monkeypatch.setattr("core.db.get_connection", _handed_the_test_transaction)

    from core.datastreams import backfill_datastreams

    summary = backfill_datastreams()

    mine = [error for error in summary["errors"] if project_id in error]
    assert mine, (
        "the saturated org's row was not refused by the sweep; errors were "
        f"{summary['errors']!r}"
    )
    assert mine[0].startswith("trial_limit "), mine[0]
    assert REPAIR_GESTURE in mine[0], mine[0]
    assert summary["skipped"] >= 1
    assert _running_count(conn, org_id) == EXPECTED_TRIAL_CAP

    # ONE ROW REFUSED, NOT THE SWEEP. The org with room got its Datastream in
    # the same pass -- which is the whole reason the guard sits inside the loop
    # instead of in front of it.
    assert summary["created"] >= 1, summary
    assert _running_count(conn, other_org) == 1


# ---------------------------------------------------------------------------
# The census itself: five doors, and the guard knows when a sixth appears.
# ---------------------------------------------------------------------------


def test_the_five_doors_are_the_whole_census(live_postgres) -> None:
    """A sixth caller must arrive with its own test, not quietly.

    This file proves five acts. It cannot prove an act nobody wrote it for, so
    the inventory is COMPUTED from the tree: when a sixth writer starts calling
    the guard, this goes red and names it, rather than the console's sentence
    going quietly wrong again.
    """
    import ast

    core = _SERVER / "core"
    callers: set[str] = set()
    for path in sorted(core.glob("*.py")):
        if path.stem == "trial_enforcement":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
                if name == "check_datastream_limit":
                    callers.add(path.stem)
    assert callers == {
        "datastreams",
        "datastream_activation",
        "schedule_mcp",
        "dataset_recovery",
    }, (
        "the census of modules that bound the datastream cap has changed: "
        f"{sorted(callers)}. Add the new door to this file AND to the sentence "
        "`OrgSettings.tsx#PlanSection` shows a person, which is the criterion "
        "`organization-settings.md` holds open until the two agree."
    )
