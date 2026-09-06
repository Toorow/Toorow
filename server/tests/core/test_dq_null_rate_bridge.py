"""Story 59.3: the bridge from a firing to a governed issue, on a live Postgres.

WHAT ONLY A DATABASE CAN PROVE HERE, and each one was a defect before it was a
test:

1. **An issue names its run.** `app.dq_issues` gained `datastream_id` and
   `execution_id` in migration 222 and NOTHING wrote them -- neither
   `record_evaluation` nor `open_issue`. The composite foreign key and
   `ck_dq_issues_execution_needs_datastream` are what say the pair is readable,
   and a mocked cursor agrees with any pair at all.
2. **A recurrence moves the run.** The recurrence branch of `open_issue` updated
   `last_seen_at` alone, so an issue stayed pinned to the FIRST run that saw it
   and the per-run read lost it every later night. Proving it needs two real
   nights against `uq_dq_issues_open_root_cause`.
3. **Several runs covered the same day**, so "the run" is a choice, and the order
   that makes it has to be exercised against a real planner rather than asserted
   about a string. 60 of 571 (Datastream, day) pairs carry more than one covering
   run today, so a seeded fixture is what reaches the branch at all.
4. **An unresolvable run writes NULL**, never a fabricated id.

THE ORDER THESE TESTS ASSERT CHANGED IN STORY 59.4, and three of them had to be
rewritten rather than left green. This module used to carry a query of its own,
ordered `completed_at DESC NULLS LAST, id DESC` and restricted to `done` pulls;
`resolve_execution_id` now delegates to `extract_ledger.execution_for_day`, whose
order is the ledger's own `enqueued_at DESC` over pulls in ANY state -- the order
every run and coverage screen already displays. The old tests stayed green only
because their fixtures inserted rows in an order where both rules agreed: a test
whose subject was deleted and which still passes is proving something about its
own insert sequence.

**Every ordering fixture below therefore writes `enqueued_at` explicitly.**
Relying on `now()` at insert time makes the assertion depend on statement order
rather than on the column the resolver reads, which is the defect it is meant to
catch.

`app.pull_jobs.execution_id` was NULL on 130 rows of 130 on the disposable base
and 6 of 6 on preprod when this was written: `queue.py` has filled it since story
63.1 and those rows are simply older. **Every fixture here therefore seeds
`pull_jobs` WITH an `execution_id`** -- one that omits it exercises a path today's
data never reaches.

`pg_owner`: the teardown deletes from `app.dq_issue_events`, which is append-only
BY TRIGGER, so the fixture lifts its own guard and puts it straight back. Under
the application role this file SKIPS, and a skip is not a pass.
"""

from __future__ import annotations

import os
import uuid
from datetime import date, timedelta

import pytest

from tests.conftest import purge_fixture_project

TEST_ORG_ID = "org_test_fixture"
WINDOW = date(2026, 8, 6)

_skip_without_dsn = pytest.mark.skipif(
    not os.environ.get("TEST_POSTGRES_DSN"),
    reason="TEST_POSTGRES_DSN not set -- the governance bridge needs a live Postgres",
)

pytestmark = [_skip_without_dsn, pytest.mark.pg_owner]

_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def _ulid_body() -> str:
    body = uuid.uuid4().int
    chars = []
    for _ in range(26):
        body, index = divmod(body, len(_ALPHABET))
        chars.append(_ALPHABET[index])
    return "".join(reversed(chars))


def _id(prefix: str) -> str:
    return f"{prefix}{uuid.uuid4().hex[:12]}"


@pytest.fixture
def bridge(live_postgres, monkeypatch):
    """One project, one Datastream, one run, and the pull job that named it.

    `core.db.get_connection` is pointed at the SAME database as the test's own
    connection: the bridge deliberately opens its own short-lived connection so a
    governed write cannot abort the nightly sweep's shared one, and a test that
    let it wander off to another DSN would be measuring nothing.
    """
    conn = live_postgres
    monkeypatch.setenv("PLATFORM_DB_URL", os.environ["TEST_POSTGRES_DSN"])

    project_id = _id("proj_")
    ds_id = _id("ds_")
    plan_id = _id("dsp_")
    mapping_id = _id("dmap_")
    connection_id = _id("cref_")
    first_run = f"dse_{_ulid_body()}"
    second_run = f"dse_{_ulid_body()}"

    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO app.projects (id, name, slug, created_by, org_id) "
            "VALUES (%s, %s, %s, 'story-59.3-test', %s)",
            (project_id, project_id, project_id.replace("_", "-").lower(), TEST_ORG_ID),
        )
        cur.execute(
            "INSERT INTO app.connection_ref "
            "  (id, provider, nango_connection_id, project_id, owner_org_id, owner_identity) "
            "VALUES (%s, 'ga4', %s, %s, %s, 'owner@example.com')",
            (connection_id, connection_id, project_id, TEST_ORG_ID),
        )
        cur.execute(
            """
            INSERT INTO app.datastreams
                (id, project_id, name, module_name, report_profile_id, source_kind,
                 connection_ref_id, enabled, created_by, org_id)
            VALUES (%s, %s, 'Null rate fixture', 'ga4', 'pages_daily_landing',
                    'connector_pull', %s, TRUE, 'test', %s)
            """,
            (ds_id, project_id, connection_id, TEST_ORG_ID),
        )
        # `_read_base_record` INNER JOINs this table; seeding it keeps the fixture
        # readable by every Workbench path a later story adds here.
        cur.execute(
            "INSERT INTO app.project_flux (project_id, flux_id, org_id) VALUES (%s, %s, %s)",
            (project_id, ds_id, TEST_ORG_ID),
        )
        cur.execute(
            """
            INSERT INTO app.datastream_plan_versions
                (id, datastream_id, project_id, version_number, contract_version,
                 source_kind, writer_kind, destination_policy, normalized_payload,
                 content_hash, idempotency_key_hash, created_by)
            VALUES (%s, %s, %s, 1, '1', 'connector_pull', 'toorow', 'managed_raw',
                    '{}'::jsonb, repeat('a', 64), repeat('b', 64), 'test')
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
            VALUES (%s, %s, %s, 1, '1', repeat('a', 64), %s, repeat('c', 64), '0.1.1',
                    '1', TRUE,
                    '{"grain": ["date", "campaign_id"],
                      "fields": [{"field_id": "campaign_id"}, {"field_id": "country"}]}'::jsonb,
                    '{}'::jsonb, repeat('d', 64), 'test')
            """,
            (mapping_id, ds_id, project_id, plan_id),
        )
        cur.execute(
            "UPDATE app.datastreams SET current_mapping_version_id = %s, "
            "current_plan_version_id = %s WHERE id = %s",
            (mapping_id, plan_id, ds_id),
        )
        for run_id in (first_run, second_run):
            cur.execute(
                """
                INSERT INTO app.datastream_executions
                    (id, datastream_id, project_id, plan_version_id, mapping_version_id,
                     projection_plan_ref, state, created_by)
                VALUES (%s, %s, %s, %s, %s, '{}'::jsonb, 'collected', 'test')
                """,
                (run_id, ds_id, project_id, plan_id, mapping_id),
            )
    conn.commit()

    context = {
        "project_id": project_id,
        "datastream_id": ds_id,
        "connection_ref_id": connection_id,
        "first_run": first_run,
        "second_run": second_run,
    }
    yield conn, context

    conn.rollback()
    with conn.cursor() as cur:
        cur.execute("ALTER TABLE app.dq_issue_events DISABLE TRIGGER USER")
        cur.execute("ALTER TABLE app.dq_evaluations DISABLE TRIGGER USER")
        cur.execute("ALTER TABLE app.dq_monitor_versions DISABLE TRIGGER USER")
        cur.execute("ALTER TABLE app.datastream_mapping_versions DISABLE TRIGGER USER")
        cur.execute("ALTER TABLE app.datastream_plan_versions DISABLE TRIGGER USER")
        try:
            for statement in (
                "DELETE FROM app.dq_issue_events WHERE project_id = %s",
                "DELETE FROM app.dq_issues WHERE project_id = %s",
                "DELETE FROM app.dq_evaluations WHERE project_id = %s",
                # The head points at its versions by a composite key; unpin
                # before deleting them, exactly as the Datastream is unpinned
                # from its plan and mapping versions below.
                "UPDATE app.dq_monitors SET current_version_id = NULL, "
                "pending_version_id = NULL, last_known_good_version_id = NULL "
                "WHERE project_id = %s",
                "DELETE FROM app.dq_monitor_versions WHERE project_id = %s",
                "DELETE FROM app.dq_monitors WHERE project_id = %s",
            ):
                cur.execute(statement, (project_id,))
            cur.execute("DELETE FROM app.pull_jobs WHERE datastream_id = %s", (ds_id,))
            cur.execute(
                "DELETE FROM app.datastream_executions WHERE project_id = %s", (project_id,)
            )
            cur.execute("DELETE FROM app.project_flux WHERE project_id = %s", (project_id,))
            cur.execute(
                "UPDATE app.datastreams SET current_mapping_version_id = NULL, "
                "current_plan_version_id = NULL WHERE id = %s",
                (ds_id,),
            )
            cur.execute(
                "DELETE FROM app.datastream_mapping_versions WHERE project_id = %s",
                (project_id,),
            )
            cur.execute(
                "DELETE FROM app.datastream_plan_versions WHERE project_id = %s", (project_id,)
            )
            cur.execute("DELETE FROM app.datastreams WHERE project_id = %s", (project_id,))
            cur.execute("DELETE FROM app.connection_ref WHERE project_id = %s", (project_id,))
            # `trg_projects_seed_capabilities` fills this table on INSERT, so a
            # teardown that never wrote it still has to clear it.
            cur.execute(
                "DELETE FROM app.project_capabilities WHERE project_id = %s", (project_id,)
            )
            # AI-291: le graphe prend le relais si une table gouvernee
            # ajoutee depuis retient le projet en ON DELETE RESTRICT.
            purge_fixture_project(cur.connection, project_id)
        finally:
            cur.execute("ALTER TABLE app.datastream_plan_versions ENABLE TRIGGER USER")
            cur.execute("ALTER TABLE app.datastream_mapping_versions ENABLE TRIGGER USER")
            cur.execute("ALTER TABLE app.dq_monitor_versions ENABLE TRIGGER USER")
            cur.execute("ALTER TABLE app.dq_evaluations ENABLE TRIGGER USER")
            cur.execute("ALTER TABLE app.dq_issue_events ENABLE TRIGGER USER")
    conn.commit()


def _seed_pull_job(
    conn,
    context,
    *,
    execution_id: str | None,
    completed_at: str | None = None,
    enqueued_at: str | None = None,
    date_from: date = WINDOW,
    date_to: date = WINDOW,
    state: str = "done",
) -> str:
    """One `app.pull_jobs` row -- WITH its `execution_id`, and with its own clock.

    `execution_id` is seeded on purpose: it is NULL on 130 rows of 130 of the
    disposable base, so a fixture that omitted it would exercise a path today's
    data never reaches.

    `enqueued_at` IS THE COLUMN THE RESOLVER ORDERS BY since story 59.4. Any test
    about ordering passes it explicitly; leaving it to `now()` would make the
    assertion depend on the order the INSERTs happen to run in, which is exactly
    how the pre-59.4 versions of these tests stayed green after their subject was
    deleted. `completed_at` is kept because the column exists and is populated in
    preprod (6 of 6) -- it simply no longer decides anything here.
    """
    job_id = _id("pjob_")
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.pull_jobs
                (id, pull_id, connection_ref_id, datastream_id, execution_id,
                 date_from, date_to, state, requested_by, completed_at, enqueued_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'test', %s::timestamptz,
                    COALESCE(%s::timestamptz, now()))
            """,
            (
                job_id,
                _id("pull_"),
                context["connection_ref_id"],
                context["datastream_id"],
                execution_id,
                date_from,
                date_to,
                state,
                completed_at,
                enqueued_at,
            ),
        )
    conn.commit()
    return job_id


def _issue_row(conn, project_id: str) -> tuple:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, datastream_id, execution_id, severity, status "
            "FROM app.dq_issues WHERE project_id = %s",
            (project_id,),
        )
        rows = cur.fetchall()
    assert len(rows) == 1, f"expected exactly one issue, found {len(rows)}"
    return rows[0]


# ---------------------------------------------------------------------------
# Which run a day belongs to.
# ---------------------------------------------------------------------------


def test_the_run_of_a_day_is_the_pull_job_that_named_it(bridge):
    from core.dq_null_rate import resolve_execution_id

    conn, context = bridge
    _seed_pull_job(
        conn,
        context,
        execution_id=context["first_run"],
        enqueued_at="2026-08-07 02:00:00+00",
    )

    assert (
        resolve_execution_id(
            conn,
            project_id=context["project_id"],
            datastream_id=context["datastream_id"],
            window_date=WINDOW,
        )
        == context["first_run"]
    )


def test_several_runs_on_one_day_resolve_to_the_LAST_ENQUEUED(bridge):
    """Arbitrage 4, and the two runs are inserted in the order that would hide it.

    60 of 571 (Datastream, day) pairs carry more than one covering run, so the
    branch is real and only a seeded fixture reaches it.

    THE FIXTURE MAKES THE TWO CANDIDATE ORDERS DISAGREE, DETERMINISTICALLY. The
    later-enqueued run is inserted FIRST and given the EARLIER `completed_at`, so
    every rule this repository has ever used picks a different row: insertion
    order picks `second_run` by accident, `completed_at DESC` picks `first_run`,
    and `enqueued_at DESC` -- the ledger's, the one that runs -- picks
    `second_run`. Leaving the two timestamps tied would have left the answer to
    `id DESC` over a uuid4, which is a coin flip and proves nothing either way.
    """
    from core.dq_null_rate import resolve_execution_id

    conn, context = bridge
    _seed_pull_job(
        conn,
        context,
        execution_id=context["second_run"],
        enqueued_at="2026-08-07 06:30:00+00",
        completed_at="2026-08-07 07:00:00+00",
    )
    _seed_pull_job(
        conn,
        context,
        execution_id=context["first_run"],
        enqueued_at="2026-08-07 02:00:00+00",
        completed_at="2026-08-07 23:00:00+00",
    )

    assert (
        resolve_execution_id(
            conn,
            project_id=context["project_id"],
            datastream_id=context["datastream_id"],
            window_date=WINDOW,
        )
        == context["second_run"]
    )


def test_completed_at_decides_nothing_any_more(bridge):
    """REWRITTEN by story 59.4: this test's subject was DELETED, not amended.

    It used to be called "a run that recorded a completion outranks one that did
    not", and it justified the `NULLS LAST` of a query that no longer exists. The
    resolver reads the ledger now, and the ledger orders by `enqueued_at`; whether
    a job wrote a completion is not part of that answer.

    So the fixture is written to make the two rules DISAGREE, which is the only
    arrangement that proves which one runs: the run with NO completion is the
    later-enqueued one, and it is the answer. Under the deleted `completed_at DESC
    NULLS LAST` this assertion is false.
    """
    from core.dq_null_rate import resolve_execution_id

    conn, context = bridge
    _seed_pull_job(
        conn,
        context,
        execution_id=context["first_run"],
        completed_at="2026-08-07 06:30:00+00",
        enqueued_at="2026-08-07 02:00:00+00",
    )
    _seed_pull_job(
        conn,
        context,
        execution_id=context["second_run"],
        completed_at=None,
        enqueued_at="2026-08-07 05:00:00+00",
    )

    assert (
        resolve_execution_id(
            conn,
            project_id=context["project_id"],
            datastream_id=context["datastream_id"],
            window_date=WINDOW,
        )
        == context["second_run"]
    )


def test_the_latest_pull_names_the_run_whatever_state_it_is_in(bridge):
    """REWRITTEN by story 59.4, arbitrage 4, and the rewrite is the point.

    This test used to be called "a pull that did not finish does not name the
    run", and it asserted a rule this repository no longer has ONE of: the
    resolver now reads the ledger, ordered `enqueued_at DESC`
    (`extract_ledger.py:255`), which is the order every screen already displays. A
    still-running collection IS the run the day belongs to -- the ledger says so,
    `CoverageBars` draws it, and the day's own status (`running`) is what tells a
    person it is not finished. Keeping the old assertion would have kept a green
    test for a rule only one module believed in.

    It passed unchanged before this rewrite only because its two pulls were
    enqueued in an order that made both rules agree. That is the shape of a test
    that proves less than it appears to.
    """
    from core.dq_null_rate import resolve_execution_id

    conn, context = bridge
    _seed_pull_job(
        conn,
        context,
        execution_id=context["first_run"],
        completed_at="2026-08-07 02:00:00+00",
        enqueued_at="2026-08-07 02:00:00+00",
    )
    _seed_pull_job(
        conn,
        context,
        execution_id=context["second_run"],
        completed_at=None,
        enqueued_at="2026-08-07 06:30:00+00",
        state="running",
    )

    assert (
        resolve_execution_id(
            conn,
            project_id=context["project_id"],
            datastream_id=context["datastream_id"],
            window_date=WINDOW,
        )
        == context["second_run"]
    )


def test_a_day_no_pull_covered_resolves_to_nothing(bridge):
    from core.dq_null_rate import resolve_execution_id

    conn, context = bridge
    _seed_pull_job(
        conn,
        context,
        execution_id=context["first_run"],
        completed_at="2026-08-07 02:00:00+00",
        date_from=WINDOW - timedelta(days=10),
        date_to=WINDOW - timedelta(days=8),
    )

    assert (
        resolve_execution_id(
            conn,
            project_id=context["project_id"],
            datastream_id=context["datastream_id"],
            window_date=WINDOW,
        )
        is None
    )


def test_a_pull_that_names_no_run_is_null_and_never_a_fabricated_id(bridge):
    """The state of 130 rows of 130. NULL is the honest answer."""
    from core.dq_null_rate import resolve_execution_id

    conn, context = bridge
    _seed_pull_job(conn, context, execution_id=None, completed_at="2026-08-07 02:00:00+00")

    assert (
        resolve_execution_id(
            conn,
            project_id=context["project_id"],
            datastream_id=context["datastream_id"],
            window_date=WINDOW,
        )
        is None
    )


# ---------------------------------------------------------------------------
# The required fields, read from the ACTIVE mapping version.
# ---------------------------------------------------------------------------


def test_the_required_fields_are_the_grain_of_the_current_mapping_version(bridge):
    from core.dq_null_rate import read_required_fields

    conn, context = bridge
    assert read_required_fields(
        conn, project_id=context["project_id"], datastream_id=context["datastream_id"]
    ) == ["date", "campaign_id"]


def test_a_datastream_with_no_current_mapping_version_requires_nothing(bridge):
    from core.dq_null_rate import read_required_fields

    conn, context = bridge
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE app.datastreams SET current_mapping_version_id = NULL WHERE id = %s",
            (context["datastream_id"],),
        )
    conn.commit()

    assert (
        read_required_fields(
            conn, project_id=context["project_id"], datastream_id=context["datastream_id"]
        )
        == []
    )


# ---------------------------------------------------------------------------
# The bridge itself.
# ---------------------------------------------------------------------------


def _derive(context):
    from core.dq_null_rate import derive_monitor

    monitor = derive_monitor(
        org_id=TEST_ORG_ID,
        project_id=context["project_id"],
        datastream_id=context["datastream_id"],
        datastream_name="Null rate fixture",
        threshold=0.0,
    )
    assert monitor and monitor["monitor_id"] and monitor["monitor_version_id"]
    return monitor


def _record(context, monitor, *, execution_id, window_date=WINDOW, outcome="fail", findings=None):
    from core.dq_null_rate import record_verdict

    return record_verdict(
        project_id=context["project_id"],
        monitor_id=monitor["monitor_id"],
        monitor_version_id=monitor["monitor_version_id"],
        datastream_id=context["datastream_id"],
        execution_id=execution_id,
        window_date=window_date,
        outcome=outcome,
        observed={"relation": "raw_ga4_standard_daily"},
        findings=findings if findings is not None else [{"field": "campaign_id"}],
    )


def test_the_issue_carries_its_datastream_and_its_run(bridge):
    conn, context = bridge
    _seed_pull_job(
        conn, context, execution_id=context["first_run"], completed_at="2026-08-07 02:00:00+00"
    )
    monitor = _derive(context)
    written = _record(context, monitor, execution_id=context["first_run"])
    assert [item["recurrence"] for item in written["issues"]] == ["opened"]

    issue_id, datastream_id, execution_id, severity, status = _issue_row(
        conn, context["project_id"]
    )
    assert datastream_id == context["datastream_id"]
    assert execution_id == context["first_run"]
    assert (severity, status) == ("degrading", "open")

    with conn.cursor() as cur:
        cur.execute(
            "SELECT event_kind, evaluation_id FROM app.dq_issue_events WHERE issue_id = %s",
            (issue_id,),
        )
        events = cur.fetchall()
    assert [row[0] for row in events] == ["observed"]
    # `app.dq_issue_events` has NO run column of its own: the evaluation this row
    # points at is the only thing tying the sighting to a run.
    assert events[0][1] == written["evaluation_id"]


def test_the_night_writes_its_own_evaluation_naming_the_run(bridge):
    """The settled half of migration 222, and the reason the issue may move its run.

    Before this, `record_evaluation` was reachable only from the manual route
    (`controls_quality_api`), so the nightly path wrote no evidence at all -- and
    overwriting the issue's run destroyed the earlier one with no survivor.
    """
    conn, context = bridge
    monitor = _derive(context)
    written = _record(context, monitor, execution_id=context["first_run"])

    with conn.cursor() as cur:
        cur.execute(
            "SELECT outcome, datastream_id, execution_id, window_start, window_end, "
            "       total_eligible, failed_count "
            "FROM app.dq_evaluations WHERE id = %s",
            (written["evaluation_id"],),
        )
        row = cur.fetchone()
    assert row[0] == "fail"
    assert row[1] == context["datastream_id"]
    assert row[2] == context["first_run"]
    assert (row[3], row[4]) == (WINDOW, WINDOW)
    assert (row[5], row[6]) == (1, 1)


def test_a_quiet_night_is_recorded_too_and_never_as_a_pass_over_nothing(bridge):
    """A night that measured nothing leaves a row, and that row is not a pass."""
    conn, context = bridge
    monitor = _derive(context)
    quiet = _record(
        context,
        monitor,
        execution_id=context["first_run"],
        window_date=WINDOW - timedelta(days=1),
        outcome="not_applicable",
        findings=[],
    )
    assert quiet["evaluation_id"]
    assert quiet["issues"] == []

    with conn.cursor() as cur:
        cur.execute(
            "SELECT outcome, total_eligible, passed_count, execution_id "
            "FROM app.dq_evaluations WHERE id = %s",
            (quiet["evaluation_id"],),
        )
        outcome, total_eligible, passed, execution_id = cur.fetchone()
    assert (outcome, total_eligible, passed) == ("not_applicable", 0, 0)
    assert execution_id == context["first_run"]
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM app.dq_issues WHERE project_id = %s", (context["project_id"],)
        )
        assert cur.fetchone()[0] == 0


def test_a_second_night_on_the_same_cause_moves_the_run_and_the_first_survives(bridge):
    """Arbitrage 3. One issue per (monitor, field), and the moved run is recoverable."""
    conn, context = bridge
    monitor = _derive(context)
    first_night = _record(
        context,
        monitor,
        execution_id=context["first_run"],
        window_date=WINDOW - timedelta(days=1),
    )
    first_id, _, first_execution, _, _ = _issue_row(conn, context["project_id"])
    assert first_execution == context["first_run"]

    second_night = _record(context, monitor, execution_id=context["second_run"])
    assert [item["recurrence"] for item in second_night["issues"]] == ["appended"]

    second_id, datastream_id, second_execution, _, _ = _issue_row(conn, context["project_id"])
    assert second_id == first_id, "uq_dq_issues_open_root_cause must bound the recurrence"
    assert datastream_id == context["datastream_id"]
    # Before story 59.3 this stayed on `first_run` forever, and the per-run read of
    # 59.1 lost the issue on every later night.
    assert second_execution == context["second_run"]

    with conn.cursor() as cur:
        cur.execute(
            "SELECT event_kind, evaluation_id FROM app.dq_issue_events "
            "WHERE issue_id = %s ORDER BY occurred_at",
            (first_id,),
        )
        events = cur.fetchall()
    assert [row[0] for row in events] == ["observed", "observed"]
    assert [row[1] for row in events] == [
        first_night["evaluation_id"],
        second_night["evaluation_id"],
    ]

    # THE SURVIVOR. The issue no longer names `first_run`; the first night's
    # evaluation still does -- append-only, one row per window.
    with conn.cursor() as cur:
        cur.execute(
            "SELECT window_start, execution_id FROM app.dq_evaluations "
            "WHERE project_id = %s ORDER BY window_start",
            (context["project_id"],),
        )
        history = cur.fetchall()
    assert history == [
        (WINDOW - timedelta(days=1), context["first_run"]),
        (WINDOW, context["second_run"]),
    ]


def test_an_unresolvable_run_writes_null_and_the_check_constraint_holds(bridge):
    conn, context = bridge
    monitor = _derive(context)
    written = _record(context, monitor, execution_id=None)

    _, datastream_id, execution_id, _, _ = _issue_row(conn, context["project_id"])
    assert execution_id is None
    assert datastream_id == context["datastream_id"]

    with conn.cursor() as cur:
        cur.execute(
            "SELECT datastream_id, execution_id FROM app.dq_evaluations WHERE id = %s",
            (written["evaluation_id"],),
        )
        assert cur.fetchone() == (context["datastream_id"], None)


def test_a_run_without_its_datastream_is_refused_rather_than_stored(bridge):
    """`ck_dq_issues_execution_needs_datastream`, said in Python so a caller reads it."""
    from core.dq_governance import DqGovernanceError, open_issue

    conn, context = bridge
    with pytest.raises(DqGovernanceError):
        open_issue(
            conn,
            project_id=context["project_id"],
            monitor_id="dqm_whatever",
            root_cause_fingerprint="a" * 64,
            severity="degrading",
            datastream_id=None,
            execution_id=context["first_run"],
        )
    conn.rollback()


def test_the_monitor_is_derived_once_and_published_on_null_rate(bridge):
    """Arbitrage 5: get-or-create, and the profile is publishable without a migration."""
    from core.dq_null_rate import CHECK_PROFILE, monitor_name

    conn, context = bridge
    first = _derive(context)
    second = _derive(context)
    assert first == second

    with conn.cursor() as cur:
        cur.execute(
            "SELECT name, target_kind, target_id, lifecycle_status, created_by, "
            "       current_version_id "
            "FROM app.dq_monitors WHERE project_id = %s",
            (context["project_id"],),
        )
        rows = cur.fetchall()
    assert len(rows) == 1
    name, target_kind, target_id, lifecycle, created_by, version_id = rows[0]
    assert name == monitor_name(context["datastream_id"])
    assert (target_kind, target_id) == ("datastream", context["datastream_id"])
    assert (lifecycle, created_by) == ("published", "system")

    with conn.cursor() as cur:
        cur.execute(
            "SELECT check_profile, severity, parameters FROM app.dq_monitor_versions "
            "WHERE id = %s",
            (version_id,),
        )
        profile, severity, parameters = cur.fetchone()
    assert (profile, severity) == (CHECK_PROFILE, "degrading")
    assert parameters["thresholds"][CHECK_PROFILE] == 0.0


# ---------------------------------------------------------------------------
# The governed reading of a check that measured nothing -- the class, not this
# monitor. Every profile of the dispatch table passes through it.
# ---------------------------------------------------------------------------


def _published_monitor(conn, context):
    monitor = _derive(context)
    return monitor["monitor_id"], monitor["monitor_version_id"]


def _evaluate_with(conn, context, verdict):
    from unittest.mock import patch

    from core import dq_monitors

    with patch.dict(
        dq_monitors.CHECK_PROFILES,
        {"null_rate": lambda ds, c, day, now, version: verdict},
    ):
        monitor_id, version_id = _published_monitor(conn, context)
        return monitor_id, version_id, dq_monitors.governed_evaluator(
            conn,
            project_id=context["project_id"],
            monitor_id=monitor_id,
            monitor_version_id=version_id,
            window_start=WINDOW,
            window_end=WINDOW,
        )


def test_a_check_that_measured_nothing_is_not_applicable_and_is_storable(bridge):
    """Zero eligible ROWS was rendered as `pass` because a bool cannot say more.

    `governed_evaluator` counted every non-firing check as a pass, so a monitor
    that could not measure anything reported `healthy`. The evaluation is written
    here as well, because the refusal only counts if the database accepts the row
    the honest verdict produces.
    """
    from core.dq_governance import record_evaluation
    from core.dq_monitors import STATUS_NOT_APPLICABLE, CheckVerdict

    conn, context = bridge
    monitor_id, version_id, (outcome, counts, refs, observed) = _evaluate_with(
        conn, context, CheckVerdict(False, STATUS_NOT_APPLICABLE, {"reason": "no_eligible_row"})
    )
    assert outcome == "not_applicable"
    assert (counts.total_eligible, counts.evaluated, counts.passed) == (1, 0, 0)
    assert observed["notes"][0]["reason"] == "no_eligible_row"

    evaluation_id = record_evaluation(
        conn,
        project_id=context["project_id"],
        monitor_id=monitor_id,
        monitor_version_id=version_id,
        outcome=outcome,
        window_start=WINDOW,
        window_end=WINDOW,
        counts=counts,
        dependency_refs=refs,
        observed=observed,
    )
    conn.commit()

    with conn.cursor() as cur:
        cur.execute("SELECT outcome FROM app.dq_evaluations WHERE id = %s", (evaluation_id,))
        assert cur.fetchone()[0] == "not_applicable"
        cur.execute("SELECT runtime_state FROM app.dq_monitors WHERE id = %s", (monitor_id,))
        assert cur.fetchone()[0] == "not_applicable"


def test_a_check_that_could_not_read_its_relation_is_unavailable_and_never_a_pass(bridge):
    from core.dq_monitors import STATUS_UNAVAILABLE, CheckVerdict

    conn, context = bridge
    _monitor_id, _version_id, (outcome, counts, _refs, observed) = _evaluate_with(
        conn, context, CheckVerdict(False, STATUS_UNAVAILABLE, {"relation": "raw_x_ads_daily"})
    )
    assert outcome == "unverifiable"
    assert (counts.passed, counts.unavailable) == (0, 1)
    assert observed["notes"][0]["relation"] == "raw_x_ads_daily"


def test_a_check_that_answers_a_plain_bool_is_unchanged(bridge):
    """The five older profiles say `evaluated` by omission, and must keep passing."""
    conn, context = bridge
    _monitor_id, _version_id, (outcome, counts, _refs, _observed) = _evaluate_with(
        conn, context, False
    )
    assert outcome == "pass"
    assert (counts.total_eligible, counts.evaluated, counts.passed) == (1, 1, 1)
