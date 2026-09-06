"""Story 59.4: the zero-row finding becomes a governed issue, on a live Postgres.

WHAT ONLY A DATABASE CAN PROVE HERE:

1. **The check reads a REAL ledger.** `verdict = 'empty'` exists on preprod (1 row,
   measured 2026-08-08) and NOWHERE on the disposable base (0 of 62
   verifications), so the positive case of this monitor is reachable only from a
   seeded fixture. A mocked ledger agrees with any shape; only Postgres proves
   that `verification.py`'s `empty` verdict travels through
   `extract_ledger._day_to_ledger_entry` and comes out as the day status this
   check keys on.
2. **One issue per flux, and the second empty window is a RECURRENCE.** Arbitrage
   7's fingerprint is `(monitor,)`, and what makes that a bound rather than an
   intention is `uq_dq_issues_open_root_cause`. Two nights against a real unique
   index is the only way to see it hold.
3. **The recurrence MOVES the run.** `app.dq_issues.execution_id` is the run that
   saw the anomaly last; the earlier one survives on its own append-only
   `app.dq_evaluations` row. Both halves are migration 222's and both need the
   composite foreign key to be real.
4. **An unresolvable run writes NULL**, never a fabricated id, with
   `ck_dq_issues_execution_needs_datastream` still satisfied.
5. **A day inside `window_offset_days` writes nothing at all.** The column holds 1
   on 1421 rows of 1421 and on 47 of 47 in preprod, so the offset case is
   unreachable from data on both bases: one Datastream here is seeded at 3.

`app.pull_jobs.execution_id` was NULL on 130 rows of 130 on the disposable base
when this was written, so every fixture seeds it explicitly -- one that omits it
exercises a path today's data never reaches.

`pg_owner`: the teardown deletes from `app.dq_issue_events`, which is append-only
BY TRIGGER, so the fixture lifts its own guard and puts it straight back. Under
the application role this file SKIPS, and a skip is not a pass.
"""

from __future__ import annotations

import os
import uuid
from datetime import date, datetime, timedelta, timezone

import pytest

from tests.conftest import purge_fixture_project

TEST_ORG_ID = "org_test_fixture"
#: The judged day. `NOW` is the day after, so an offset of 1 makes WINDOW the last
#: fetchable day and the offset guard is the only thing that can move it.
WINDOW = date(2026, 8, 6)
NOW = datetime(2026, 8, 7, 10, 0, tzinfo=timezone.utc)

_skip_without_dsn = pytest.mark.skipif(
    not os.environ.get("TEST_POSTGRES_DSN"),
    reason="TEST_POSTGRES_DSN not set -- the zero-row bridge needs a live Postgres",
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
    """One project, two Datastreams -- one at offset 1, one at offset 3 -- two runs.

    `core.db.get_connection` is pointed at the SAME database as the test's own
    connection: the bridge deliberately opens its own short-lived connection so a
    governed write cannot abort the nightly sweep's shared one, and a test that let
    it wander off to another DSN would be measuring nothing.
    """
    conn = live_postgres
    monkeypatch.setenv("PLATFORM_DB_URL", os.environ["TEST_POSTGRES_DSN"])

    project_id = _id("proj_")
    ds_id = _id("ds_")
    offset_ds_id = _id("ds_")
    plan_id = _id("dsp_")
    mapping_id = _id("dmap_")
    connection_id = _id("cref_")
    first_run = f"dse_{_ulid_body()}"
    second_run = f"dse_{_ulid_body()}"

    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO app.projects (id, name, slug, created_by, org_id) "
            "VALUES (%s, %s, %s, 'story-59.4-test', %s)",
            (project_id, project_id, project_id.replace("_", "-").lower(), TEST_ORG_ID),
        )
        cur.execute(
            "INSERT INTO app.connection_ref "
            "  (id, provider, nango_connection_id, project_id, owner_org_id, owner_identity) "
            "VALUES (%s, 'ga4', %s, %s, %s, 'owner@example.com')",
            (connection_id, connection_id, project_id, TEST_ORG_ID),
        )
        for stream_id, name, offset in (
            (ds_id, "Zero rows fixture", 1),
            # The offset case, unreachable from data on both bases.
            (offset_ds_id, "Zero rows fixture at offset 3", 3),
        ):
            cur.execute(
                """
                INSERT INTO app.datastreams
                    (id, project_id, name, module_name, report_profile_id, source_kind,
                     connection_ref_id, enabled, created_by, org_id, window_offset_days)
                VALUES (%s, %s, %s, 'ga4', 'pages_daily_landing', 'connector_pull',
                        %s, TRUE, 'test', %s, %s)
                """,
                (stream_id, project_id, name, connection_id, TEST_ORG_ID, offset),
            )
            cur.execute(
                "INSERT INTO app.project_flux (project_id, flux_id, org_id) "
                "VALUES (%s, %s, %s)",
                (project_id, stream_id, TEST_ORG_ID),
            )
        # `app.datastream_executions.plan_version_id` and `mapping_version_id` are
        # NOT NULL: a run is a run OF a published plan, and the fixture cannot
        # pretend otherwise.
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
                    '1', TRUE, '{"grain": ["date"]}'::jsonb, '{}'::jsonb,
                    repeat('d', 64), 'test')
            """,
            (mapping_id, ds_id, project_id, plan_id),
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
        "offset_datastream_id": offset_ds_id,
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
                "UPDATE app.dq_monitors SET current_version_id = NULL, "
                "pending_version_id = NULL, last_known_good_version_id = NULL "
                "WHERE project_id = %s",
                "DELETE FROM app.dq_monitor_versions WHERE project_id = %s",
                "DELETE FROM app.dq_monitors WHERE project_id = %s",
            ):
                cur.execute(statement, (project_id,))
            cur.execute(
                "DELETE FROM app.alert_firings WHERE project_id = %s", (project_id,)
            )
            cur.execute(
                "DELETE FROM app.pull_verifications WHERE connection_ref_id = %s",
                (connection_id,),
            )
            cur.execute(
                "DELETE FROM app.pull_jobs WHERE connection_ref_id = %s", (connection_id,)
            )
            cur.execute(
                "DELETE FROM app.datastream_executions WHERE project_id = %s", (project_id,)
            )
            cur.execute("DELETE FROM app.project_flux WHERE project_id = %s", (project_id,))
            cur.execute(
                "DELETE FROM app.datastream_mapping_versions WHERE project_id = %s",
                (project_id,),
            )
            cur.execute(
                "DELETE FROM app.datastream_plan_versions WHERE project_id = %s",
                (project_id,),
            )
            cur.execute("DELETE FROM app.datastreams WHERE project_id = %s", (project_id,))
            cur.execute("DELETE FROM app.connection_ref WHERE project_id = %s", (project_id,))
            # `trg_projects_seed_capabilities` fills this on INSERT, so a teardown
            # that never wrote it still has to clear it.
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


def _seed_pull(
    conn,
    context,
    *,
    verdict: str,
    execution_id: str | None,
    date_from: date,
    date_to: date | None = None,
    datastream_id: str | None = None,
    state: str = "done",
    enqueued_at: str | None = None,
    actual_rows: int | None = None,
) -> str:
    """One `app.pull_jobs` row and its `app.pull_verifications` verdict.

    `verdict = 'empty'` does not exist on the disposable base (0 of 62) and exists
    once on preprod: the shape is real, and seeding it here is what makes the
    positive case of this monitor reachable at all.
    """
    pull_id = _id("pull_")
    rows = actual_rows if actual_rows is not None else (0 if verdict == "empty" else 42)
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.pull_jobs
                (id, pull_id, connection_ref_id, datastream_id, execution_id,
                 date_from, date_to, state, requested_by, enqueued_at, completed_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'test',
                    COALESCE(%s::timestamptz, now()), now())
            """,
            (
                _id("pjob_"),
                pull_id,
                context["connection_ref_id"],
                datastream_id or context["datastream_id"],
                execution_id,
                date_from,
                date_to or date_from,
                state,
                enqueued_at,
            ),
        )
        cur.execute(
            """
            INSERT INTO app.pull_verifications
                (id, pull_id, connection_ref_id, expected_rows, actual_rows,
                 completeness_ratio, verdict)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (
                _id("pver_"),
                pull_id,
                context["connection_ref_id"],
                rows,
                rows,
                1.0,
                verdict,
            ),
        )
    conn.commit()
    return pull_id


def _seed_activity(conn, context, *, days: int, datastream_id: str | None = None) -> None:
    """*days* separate `ok` days in the lookback -- the evidence of production."""
    for offset in range(1, days + 1):
        _seed_pull(
            conn,
            context,
            verdict="ok",
            execution_id=None,
            date_from=WINDOW - timedelta(days=offset + 1),
            datastream_id=datastream_id,
        )


def _evaluate(ds_row, conn, window_date: date = WINDOW):
    from core import dq_monitors

    return dq_monitors._check_zero_rows(ds_row, conn, window_date, NOW)


def _ds_row(conn, project_id: str, datastream_id: str) -> dict:
    """The Datastream as the sweep projects it -- `window_offset_days` included."""
    from core.dq_monitors import _fetch_enabled_datastreams

    rows = [
        ds
        for ds in _fetch_enabled_datastreams(conn, project_id)
        if ds["id"] == datastream_id
    ]
    assert rows, "the fixture's Datastream must be visible to the sweep's own read"
    return rows[0]


def _issues(conn, project_id: str) -> list[tuple]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, datastream_id, execution_id, severity, status, "
            "       root_cause_fingerprint "
            "FROM app.dq_issues WHERE project_id = %s",
            (project_id,),
        )
        return cur.fetchall()


def _evaluations(conn, project_id: str) -> list[tuple]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, outcome, datastream_id, execution_id, window_start, window_end "
            "FROM app.dq_evaluations WHERE project_id = %s ORDER BY evaluated_at",
            (project_id,),
        )
        return cur.fetchall()


# ---------------------------------------------------------------------------
# Which run a day belongs to -- the SHARED resolver (arbitrage 4).
# ---------------------------------------------------------------------------


def test_the_run_of_a_day_is_the_one_the_ledger_names(bridge):
    """One resolver for the whole epic: `extract_ledger.execution_for_day`.

    Story 59.3 carried its own ordering, `completed_at DESC NULLS LAST, id DESC`.
    It is populated on preprod (6 rows of 6) and NULL on the whole disposable base
    (130 of 130), so on the base its test ran against an order that degenerated to
    `id DESC`. "Which run saw this" has one answer or story 59.1 reads two.
    """
    from core.dq_null_rate import resolve_execution_id
    from core.extract_ledger import execution_for_day

    conn, context = bridge
    _seed_pull(
        conn,
        context,
        verdict="empty",
        execution_id=context["first_run"],
        date_from=WINDOW,
    )

    assert execution_for_day(conn, context["datastream_id"], WINDOW) == context["first_run"]
    # 59.3 now answers through the same function, and answers the same thing.
    assert (
        resolve_execution_id(
            conn,
            project_id=context["project_id"],
            datastream_id=context["datastream_id"],
            window_date=WINDOW,
        )
        == context["first_run"]
    )


def test_a_day_no_pull_covered_resolves_to_nothing(bridge):
    from core.extract_ledger import execution_for_day

    conn, context = bridge
    _seed_pull(
        conn,
        context,
        verdict="ok",
        execution_id=context["first_run"],
        date_from=WINDOW - timedelta(days=10),
        date_to=WINDOW - timedelta(days=8),
    )

    assert execution_for_day(conn, context["datastream_id"], WINDOW) is None


# ---------------------------------------------------------------------------
# The empty window becomes an issue.
# ---------------------------------------------------------------------------


def test_an_empty_window_opens_one_issue_naming_its_run(bridge):
    conn, context = bridge
    _seed_activity(conn, context, days=4)
    _seed_pull(
        conn,
        context,
        verdict="empty",
        execution_id=context["first_run"],
        date_from=WINDOW,
    )

    verdict = _evaluate(_ds_row(conn, context["project_id"], context["datastream_id"]), conn)

    assert bool(verdict) is True, verdict.detail
    issues = _issues(conn, context["project_id"])
    assert len(issues) == 1
    _issue_id, datastream_id, execution_id, _severity, status, _fingerprint = issues[0]
    assert datastream_id == context["datastream_id"]
    assert execution_id == context["first_run"]
    assert status == "open"

    evaluations = _evaluations(conn, context["project_id"])
    assert len(evaluations) == 1
    assert evaluations[0][1] == "fail"
    assert evaluations[0][3] == context["first_run"]


def test_a_second_empty_window_recurs_and_moves_the_run(bridge):
    """Arbitrage 7, against the real `uq_dq_issues_open_root_cause`.

    The fingerprint is `(monitor,)`, so the second empty window of the same
    Datastream is a recurrence -- one issue, its run moved to the night that saw it
    last, and the earlier run surviving on its own append-only evaluation.
    """
    conn, context = bridge
    _seed_activity(conn, context, days=4)
    # Night one: the window of 2026-08-05, seen by the first run.
    _seed_pull(
        conn,
        context,
        verdict="empty",
        execution_id=context["first_run"],
        date_from=WINDOW - timedelta(days=1),
        enqueued_at="2026-08-06 02:00:00+00",
    )
    ds_row = _ds_row(conn, context["project_id"], context["datastream_id"])
    assert bool(_evaluate(ds_row, conn, WINDOW - timedelta(days=1))) is True

    # Night two: the NEXT window, empty again, seen by another run.
    _seed_pull(
        conn,
        context,
        verdict="empty",
        execution_id=context["second_run"],
        date_from=WINDOW,
        enqueued_at="2026-08-07 02:00:00+00",
    )
    assert bool(_evaluate(ds_row, conn)) is True

    issues = _issues(conn, context["project_id"])
    assert len(issues) == 1, "one flux, one open issue -- the second window recurs"
    assert issues[0][2] == context["second_run"], "the run moves with the last sighting"

    # The earlier run is not lost: it survives on its own append-only evaluation,
    # which is what makes the overwrite above honest rather than destructive.
    runs = [row[3] for row in _evaluations(conn, context["project_id"])]
    assert runs == [context["first_run"], context["second_run"]]

    with conn.cursor() as cur:
        cur.execute(
            "SELECT event_kind FROM app.dq_issue_events WHERE project_id = %s "
            "ORDER BY occurred_at",
            (context["project_id"],),
        )
        events = [row[0] for row in cur.fetchall()]
    assert events.count("observed") == 2


def test_an_empty_window_whose_pull_names_no_run_writes_null(bridge):
    """NULL is a real answer, and `ck_dq_issues_execution_needs_datastream` holds.

    `app.pull_jobs.execution_id` is NULL on 130 rows of 130 of the disposable base;
    inventing an id to fill the column would put an anomaly under a collection that
    never produced it.
    """
    conn, context = bridge
    _seed_activity(conn, context, days=4)
    _seed_pull(conn, context, verdict="empty", execution_id=None, date_from=WINDOW)

    verdict = _evaluate(_ds_row(conn, context["project_id"], context["datastream_id"]), conn)

    assert bool(verdict) is True, verdict.detail
    issues = _issues(conn, context["project_id"])
    assert len(issues) == 1
    assert issues[0][1] == context["datastream_id"]
    assert issues[0][2] is None


# ---------------------------------------------------------------------------
# The three silences, on real rows.
# ---------------------------------------------------------------------------


def test_an_empty_window_without_history_opens_no_issue(bridge):
    """`not_applicable` naming the days found -- never a pass, never a firing."""
    from core import dq_monitors, dq_zero_rows

    conn, context = bridge
    _seed_activity(conn, context, days=2)
    _seed_pull(
        conn, context, verdict="empty", execution_id=context["first_run"], date_from=WINDOW
    )

    verdict = _evaluate(_ds_row(conn, context["project_id"], context["datastream_id"]), conn)

    assert bool(verdict) is False
    assert verdict.status == dq_monitors.STATUS_NOT_APPLICABLE
    assert verdict.detail["reason"] == dq_zero_rows.NOT_HISTORICALLY_ACTIVE
    assert verdict.detail["active_days"] == 2
    assert _issues(conn, context["project_id"]) == []
    # But the night IS recorded: "watched and quiet" must be tellable from
    # "watched by nobody" (`epic-59:114-115`).
    evaluations = _evaluations(conn, context["project_id"])
    assert len(evaluations) == 1
    assert evaluations[0][1] == "not_applicable"


def test_a_day_inside_the_offset_writes_nothing_at_all(bridge):
    """The Datastream at `window_offset_days = 3`, and the reason it is seeded.

    Its last fetchable day is 2026-08-04; WINDOW is two days inside the shadow. No
    issue, no evaluation, and no governed monitor -- nothing was measured, so there
    is nothing for a monitor to assert.
    """
    from core import dq_monitors, dq_zero_rows

    conn, context = bridge
    _seed_activity(conn, context, days=4, datastream_id=context["offset_datastream_id"])
    _seed_pull(
        conn,
        context,
        verdict="empty",
        execution_id=context["first_run"],
        date_from=WINDOW,
        datastream_id=context["offset_datastream_id"],
    )

    ds_row = _ds_row(conn, context["project_id"], context["offset_datastream_id"])
    assert ds_row["window_offset_days"] == 3
    verdict = _evaluate(ds_row, conn)

    assert bool(verdict) is False
    assert verdict.status == dq_monitors.STATUS_NOT_APPLICABLE
    assert verdict.detail["reason"] == dq_zero_rows.INSIDE_EXTRACTION_OFFSET
    assert verdict.detail["last_fetchable_date"] == "2026-08-04"
    assert _issues(conn, context["project_id"]) == []
    assert _evaluations(conn, context["project_id"]) == []
    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM app.dq_monitors WHERE project_id = %s",
            (context["project_id"],),
        )
        assert cur.fetchone()[0] == 0


def test_a_window_that_carried_rows_opens_no_issue(bridge):
    conn, context = bridge
    _seed_activity(conn, context, days=4)
    _seed_pull(
        conn, context, verdict="ok", execution_id=context["first_run"], date_from=WINDOW
    )

    verdict = _evaluate(_ds_row(conn, context["project_id"], context["datastream_id"]), conn)

    assert bool(verdict) is False
    assert _issues(conn, context["project_id"]) == []
    evaluations = _evaluations(conn, context["project_id"])
    assert len(evaluations) == 1
    assert evaluations[0][1] == "pass"


# ---------------------------------------------------------------------------
# The firing of world A, on the real table.
# ---------------------------------------------------------------------------


def test_the_firing_carries_the_window_it_speaks_of(bridge):
    """Arbitrage 5, on `app.alert_firings` itself.

    `write_infra_firing` wrote `window_date = date.today()` for every one of the
    2423 rows already in the table. A finding about the 6th, written on the 8th,
    said the 8th.
    """
    conn, context = bridge
    _seed_activity(conn, context, days=4)
    pull_id = _seed_pull(
        conn, context, verdict="empty", execution_id=context["first_run"], date_from=WINDOW
    )

    _evaluate(_ds_row(conn, context["project_id"], context["datastream_id"]), conn)

    with conn.cursor() as cur:
        cur.execute(
            "SELECT window_date, observed_value, threshold, pull_ids, type, severity "
            "FROM app.alert_firings WHERE project_id = %s",
            (context["project_id"],),
        )
        rows = cur.fetchall()
    assert len(rows) == 1
    window_date, observed_value, threshold, pull_ids, alert_type, severity = rows[0]
    assert window_date == WINDOW
    assert float(observed_value) == 0.0
    assert float(threshold) == 1.0
    assert pull_ids == [pull_id]
    assert alert_type == "dq_zero_rows"
    assert severity == "warning"
