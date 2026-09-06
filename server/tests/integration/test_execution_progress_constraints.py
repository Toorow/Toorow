"""Live-Postgres contract for the progress columns -- story 63.1, migration 218.

What a fake cursor cannot catch: whether the bounds the story names are actually
enforced by the database, whether the monotone UPDATE really refuses to rewind a
counter, whether `days_total` really stays NULL when no window was declared, and
whether `collected` is a state the CHECK accepts at all.

`pg_owner`: the fixture applies migration 218, which is DDL.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest

from tests.conftest import purge_fixture_project

ROOT = Path(__file__).resolve().parents[3]
MIGRATIONS = ROOT / "infra" / "nango" / "migrations"
PROGRESS_MIGRATION = MIGRATIONS / "218_a_running_collection_writes_where_it_is.sql"
#: Story 58.10: the spans of the four ratified steps. Applied by the fixture for
#: the same reason as 218 -- both are idempotent DDL, and a test that assumes a
#: table exists measures the cluster rather than the code.
STEP_MIGRATION = MIGRATIONS / "223_a_step_of_a_run_has_a_duration.sql"

_skip_without_dsn = pytest.mark.skipif(
    not os.environ.get("TEST_POSTGRES_DSN"),
    reason="TEST_POSTGRES_DSN not set -- live Postgres constraint test skipped",
)


def requires_postgres(fn):
    """The DSN gate AND the `pg_owner` marker, on the live tests only.

    Deliberately not a module-level `pytestmark`: the migration-text assertions
    above need no database and no ownership, and marking the whole file would
    have them SKIP on a plain application role -- a file that reports "14
    skipped" while two of its tests could have run is a file that measures the
    connection instead of the code.
    """
    return _skip_without_dsn(pytest.mark.pg_owner(fn))

_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"

_PROGRESS_COLUMNS = (
    "step",
    "day_in_progress",
    "days_done",
    "days_total",
    "rows_written",
    "started_at",
    "progress_updated_at",
)


def _dse() -> str:
    """A FRESH house-style execution id per test.

    A fixed sample id shared with a neighbouring file collided on
    `pk_datastream_executions` the moment both ran against the same disposable
    cluster -- a failure that measures the fixture, not the code.
    """
    body = uuid.uuid4().int
    chars = []
    for _ in range(26):
        body, index = divmod(body, len(_ALPHABET))
        chars.append(_ALPHABET[index])
    return "dse_" + "".join(reversed(chars))


def _id(prefix: str) -> str:
    return f"{prefix}{uuid.uuid4().hex[:12]}"


# ---------------------------------------------------------------------------
# Migration text (runs without Postgres).
# ---------------------------------------------------------------------------


def test_the_migration_declares_the_named_constraint_and_nothing_append_only() -> None:
    sql = PROGRESS_MIGRATION.read_text(encoding="utf-8")
    assert "ck_datastream_executions_progress_bounds" in sql
    assert "ck_datastream_executions_step" in sql
    for column in _PROGRESS_COLUMNS:
        assert f"ADD COLUMN IF NOT EXISTS {column}" in sql
    # The arbitrage: columns on an existing mutable row, never a new append-only
    # table -- which would owe the RGPD hatch and an org_purge edge.
    assert "CREATE TABLE" not in sql
    assert "CREATE TRIGGER" not in sql
    # The join: the queue learns which run its window belongs to.
    assert "ALTER TABLE app.pull_jobs" in sql
    assert "execution_id" in sql


def test_started_at_is_a_column_and_never_derived_from_state_changed_at() -> None:
    """57.8's class: a meaning hung on a column every change moves is a wrong answer."""
    sql = PROGRESS_MIGRATION.read_text(encoding="utf-8")
    assert "ADD COLUMN IF NOT EXISTS started_at          TIMESTAMPTZ" in sql
    assert "started_at = state_changed_at" not in sql
    assert "COALESCE(state_changed_at" not in sql


# ---------------------------------------------------------------------------
# Live Postgres.
# ---------------------------------------------------------------------------


@pytest.fixture
def progress_db(live_postgres):
    """Apply migration 218 and seed one datastream with one execution."""
    conn = live_postgres
    with conn.cursor() as cur:
        cur.execute(PROGRESS_MIGRATION.read_text(encoding="utf-8"))
        cur.execute(STEP_MIGRATION.read_text(encoding="utf-8"))
    conn.commit()

    project_id = _id("proj_")
    ds_id = _id("ds_")
    plan_id = _id("dsp_")
    mapping_id = _id("dmap_")
    exec_id = _dse()

    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.projects (id, name, slug, created_by, org_id)
            VALUES (%s, %s, %s, 'story-63.1-test', 'org_test_fixture')
            """,
            (project_id, project_id, project_id),
        )
        cur.execute(
            """
            INSERT INTO app.datastreams
                (id, project_id, name, module_name, source_kind, enabled, created_by, org_id)
            VALUES (%s, %s, 'DS', 'generic', 'connector_pull', FALSE, 'test', 'org_test_fixture')
            """,
            (ds_id, project_id),
        )
        # The Workbench reads through `app.project_flux`: without this row
        # `_read_base_record` answers "not found" and the tab tests below would
        # be measuring the fixture rather than the query.
        cur.execute(
            """
            INSERT INTO app.project_flux (project_id, flux_id, org_id)
            VALUES (%s, %s, 'org_test_fixture')
            """,
            (project_id, ds_id),
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
                    '1', TRUE, '{}'::jsonb, '{}'::jsonb, repeat('d', 64), 'test')
            """,
            (mapping_id, ds_id, project_id, plan_id),
        )
        cur.execute(
            """
            INSERT INTO app.datastream_executions
                (id, datastream_id, project_id, plan_version_id, mapping_version_id,
                 projection_plan_ref, state, created_by)
            VALUES (%s, %s, %s, %s, %s, '{}'::jsonb, 'loading', 'test')
            """,
            (exec_id, ds_id, project_id, plan_id, mapping_id),
        )
    conn.commit()
    yield conn, {
        "project_id": project_id,
        "ds_id": ds_id,
        "execution_id": exec_id,
        # The active versions are NOT written onto `app.datastreams` here, so
        # `open_collection_run` cannot resolve them on its own -- the tests that
        # open a real run hand them over, which is the same door the dispatchers
        # use when they already know them.
        "plan_version_id": plan_id,
        "mapping_version_id": mapping_id,
    }

    conn.rollback()
    with conn.cursor() as cur:
        # BY DATASTREAM, NOT BY THE ONE EXECUTION THE FIXTURE MINTED. Since story
        # 58.10 several tests open a REAL second run through
        # `open_collection_run` -- that is the point, it is the entry the
        # production path uses -- and a teardown naming a single id would leave
        # those rows in the cluster forever.
        cur.execute("DELETE FROM app.pull_jobs WHERE datastream_id = %s", (ds_id,))
        # Everything that holds an execution by `ON DELETE RESTRICT` goes first,
        # in the order the keys point: a published run (this file publishes one,
        # through `commit_publication`) is referenced by its log, its outbox
        # event and the Datastream's own current pointer.
        cur.execute(
            "UPDATE app.datastreams SET current_published_execution_id = NULL WHERE id = %s",
            (ds_id,),
        )
        # The publication log is APPEND-ONLY BY TRIGGER, like the plan and
        # mapping versions below: the fixture is the owner precisely so it can
        # lift the guard for its own rows and put it straight back. Leaving them
        # would pin the execution forever through `ON DELETE RESTRICT`.
        cur.execute("ALTER TABLE app.datastream_publication_log DISABLE TRIGGER USER")
        cur.execute("ALTER TABLE app.datastream_outbox DISABLE TRIGGER USER")
        try:
            cur.execute("DELETE FROM app.datastream_outbox WHERE datastream_id = %s", (ds_id,))
            cur.execute(
                "DELETE FROM app.datastream_publication_log WHERE datastream_id = %s", (ds_id,)
            )
        finally:
            cur.execute("ALTER TABLE app.datastream_outbox ENABLE TRIGGER USER")
            cur.execute("ALTER TABLE app.datastream_publication_log ENABLE TRIGGER USER")
        cur.execute(
            "DELETE FROM app.datastream_execution_step_evidence WHERE datastream_id = %s",
            (ds_id,),
        )
        cur.execute("DELETE FROM app.datastream_executions WHERE datastream_id = %s", (ds_id,))
        # Plan and mapping versions are immutable BY TRIGGER (migration 030 /
        # 032): the fixture is the owner precisely so it can lift the guard for
        # its own rows and put it straight back. Disabling it for the delete is
        # not a workaround around the invariant -- it is the invariant, and the
        # fixture would otherwise leave test rows in the cluster forever.
        cur.execute("ALTER TABLE app.datastream_mapping_versions DISABLE TRIGGER USER")
        cur.execute("ALTER TABLE app.datastream_plan_versions DISABLE TRIGGER USER")
        try:
            cur.execute(
                "DELETE FROM app.datastream_mapping_versions WHERE id = %s", (mapping_id,)
            )
            cur.execute(
                "DELETE FROM app.datastream_plan_versions WHERE id = %s", (plan_id,)
            )
        finally:
            cur.execute("ALTER TABLE app.datastream_plan_versions ENABLE TRIGGER USER")
            cur.execute("ALTER TABLE app.datastream_mapping_versions ENABLE TRIGGER USER")
        cur.execute("DELETE FROM app.datastreams WHERE id = %s", (ds_id,))
        cur.execute(
            "DELETE FROM app.connection_ref WHERE project_id = %s", (project_id,)
        )
        # Creating a project arms side tables (capabilities, preferences...) by
        # trigger. Enumerating them by hand would rot; ask the catalog which
        # tables reference app.projects and clear this project out of each.
        cur.execute(
            """
            SELECT DISTINCT c.conrelid::regclass::text, a.attname
            FROM pg_constraint c
            JOIN unnest(c.conkey) WITH ORDINALITY AS k(attnum, ord) ON TRUE
            JOIN pg_attribute a ON a.attrelid = c.conrelid AND a.attnum = k.attnum
            WHERE c.contype = 'f'
              AND c.confrelid = 'app.projects'::regclass
              AND array_length(c.conkey, 1) = 1
            """
        )
        for table, column in cur.fetchall():
            cur.execute(f"DELETE FROM {table} WHERE {column} = %s", (project_id,))
        # AI-291: le graphe prend le relais si une table gouvernee
        # ajoutee depuis retient le projet en ON DELETE RESTRICT.
        purge_fixture_project(cur.connection, project_id)
    conn.commit()


def _set(conn, execution_id: str, **columns):
    assignments = ", ".join(f"{name} = %s" for name in columns)
    with conn.cursor() as cur:
        cur.execute(
            f"UPDATE app.datastream_executions SET {assignments} WHERE id = %s",
            (*columns.values(), execution_id),
        )


@requires_postgres
def test_a_fresh_execution_carries_no_progress_at_all(progress_db) -> None:
    """Vide: nothing has run yet, so every column is NULL -- never a 0."""
    conn, ids = progress_db
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {', '.join(_PROGRESS_COLUMNS)} FROM app.datastream_executions "
            "WHERE id = %s",
            (ids["execution_id"],),
        )
        assert cur.fetchone() == (None,) * len(_PROGRESS_COLUMNS)


@requires_postgres
def test_the_named_constraint_refuses_days_done_beyond_the_total(progress_db) -> None:
    conn, ids = progress_db
    with pytest.raises(Exception) as excinfo:
        _set(conn, ids["execution_id"], days_total=24, days_done=25)
    assert "ck_datastream_executions_progress_bounds" in str(excinfo.value)
    conn.rollback()


@requires_postgres
@pytest.mark.parametrize(
    "columns",
    [
        {"days_done": -1},
        {"rows_written": -1},
        {"days_total": 3, "days_done": 4},
    ],
)
def test_the_named_constraint_refuses_every_bound_the_story_names(
    progress_db, columns
) -> None:
    conn, ids = progress_db
    with pytest.raises(Exception) as excinfo:
        _set(conn, ids["execution_id"], **columns)
    assert "ck_datastream_executions_progress_bounds" in str(excinfo.value)
    conn.rollback()


@requires_postgres
def test_a_total_with_no_days_done_is_allowed_and_stays_null(progress_db) -> None:
    """days_total declared, nothing collected yet: a legal, readable state."""
    conn, ids = progress_db
    _set(conn, ids["execution_id"], days_total=24)
    conn.commit()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT days_total, days_done FROM app.datastream_executions WHERE id = %s",
            (ids["execution_id"],),
        )
        assert cur.fetchone() == (24, None)


@requires_postgres
def test_the_step_is_the_ratified_vocabulary_and_nothing_else(progress_db) -> None:
    conn, ids = progress_db
    for step in ("Collect", "Map", "Check", "Publish"):
        _set(conn, ids["execution_id"], step=step)
    conn.commit()
    for word in ("Fetch", "Enrich", "Load", "collect"):
        with pytest.raises(Exception) as excinfo:
            _set(conn, ids["execution_id"], step=word)
        assert "ck_datastream_executions_step" in str(excinfo.value)
        conn.rollback()


@requires_postgres
def test_collected_is_a_state_the_database_accepts_and_the_others_still_are(
    progress_db,
) -> None:
    conn, ids = progress_db
    for state in ("collected", "published", "failed", "cancelled", "loading"):
        _set(conn, ids["execution_id"], state=state)
    conn.commit()
    with pytest.raises(Exception):
        _set(conn, ids["execution_id"], state="collecting")
    conn.rollback()


# ---------------------------------------------------------------------------
# The writer, against the real constraints.
# ---------------------------------------------------------------------------


def _enqueue_window(conn, ids, job_index: int, date_from: str, date_to: str, state: str,
                    row_count: int | None) -> None:
    """A pull job bound to the run. connection_ref_id is nullable-free, so seed one."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM app.connection_ref WHERE project_id = %s LIMIT 1",
            (ids["project_id"],),
        )
        row = cur.fetchone()
        if row is None:
            conn_ref = _id("cref_")
            cur.execute(
                """
                INSERT INTO app.connection_ref
                    (id, provider, nango_connection_id, project_id, status, enabled,
                     owner_org_id, owner_identity)
                VALUES (%s, 'generic', %s, %s, 'active', TRUE,
                        'org_test_fixture', 'owner@example.com')
                """,
                (conn_ref, conn_ref, ids["project_id"]),
            )
            ids["connection_ref_id"] = conn_ref
        else:
            ids["connection_ref_id"] = row[0]
        cur.execute(
            """
            INSERT INTO app.pull_jobs
                (id, pull_id, connection_ref_id, date_from, date_to, state,
                 requested_by, datastream_id, execution_id, row_count)
            VALUES (%s, %s, %s, %s, %s, %s, 'test', %s, %s, %s)
            """,
            (
                _id("job_"),
                _id("pull_"),
                ids["connection_ref_id"],
                date_from,
                date_to,
                state,
                ids["ds_id"],
                ids["execution_id"],
                row_count,
            ),
        )
    conn.commit()


@requires_postgres
def test_the_writer_is_monotone_against_the_real_row(progress_db) -> None:
    """A replay proposing less does not rewind what was already measured."""
    from core.execution_progress import record_window_progress

    conn, ids = progress_db
    _set(conn, ids["execution_id"], days_total=31)
    conn.commit()

    _enqueue_window(conn, ids, 0, "2026-07-01", "2026-07-10", "done", 500)
    record_window_progress(
        conn, execution_id=ids["execution_id"], project_id=ids["project_id"]
    )
    conn.commit()

    with conn.cursor() as cur:
        cur.execute(
            "SELECT days_done, rows_written FROM app.datastream_executions WHERE id = %s",
            (ids["execution_id"],),
        )
        assert cur.fetchone() == (10, 500)

    # A rewind attempt: the job's own numbers are lowered under the writer's feet.
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE app.pull_jobs SET date_to = %s, row_count = %s WHERE execution_id = %s",
            ("2026-07-02", 1, ids["execution_id"]),
        )
    conn.commit()
    record_window_progress(
        conn, execution_id=ids["execution_id"], project_id=ids["project_id"]
    )
    conn.commit()

    with conn.cursor() as cur:
        cur.execute(
            "SELECT days_done, rows_written FROM app.datastream_executions WHERE id = %s",
            (ids["execution_id"],),
        )
        assert cur.fetchone() == (10, 500)


@requires_postgres
def test_a_finished_run_becomes_collected_and_blocks_no_later_publish(
    progress_db,
) -> None:
    from core.datastream_publication import ACTIVE_STATES
    from core.execution_progress import record_and_close

    conn, ids = progress_db
    _set(conn, ids["execution_id"], days_total=10)
    conn.commit()
    _enqueue_window(conn, ids, 0, "2026-07-01", "2026-07-10", "done", 7)

    record_and_close(conn, execution_id=ids["execution_id"], actor="test")
    conn.commit()

    with conn.cursor() as cur:
        cur.execute(
            "SELECT state, days_done, rows_written, step FROM app.datastream_executions "
            "WHERE id = %s",
            (ids["execution_id"],),
        )
        state, days_done, rows_written, step = cur.fetchone()
    assert state == "collected"
    assert state not in ACTIVE_STATES
    assert (days_done, rows_written, step) == (10, 7, "Collect")


@requires_postgres
def test_every_date_like_column_of_the_table_survives_serialisation(progress_db) -> None:
    """Reads the SCHEMA, so a date column added later fails here, not in production.

    `admin_api` renders execution payloads with the standard `JSONResponse`; a
    `datetime` left in the dict raises inside `render()`, outside the handler's
    try/except, and answers a bare 500 that logs nothing. The guard cannot be a
    list of column names -- that list IS the defect.
    """
    from core.datastream_publication import _row_to_execution

    conn, ids = progress_db
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT column_name, data_type
            FROM information_schema.columns
            WHERE table_schema = 'app' AND table_name = 'datastream_executions'
            ORDER BY ordinal_position
            """
        )
        schema = cur.fetchall()
    temporal = [
        name for name, kind in schema
        if kind.startswith(("timestamp", "date", "time"))
    ]
    assert temporal, "the execution table has no temporal column -- read the schema"

    _set(
        conn,
        ids["execution_id"],
        started_at="2026-08-05T02:00:00+00:00",
        progress_updated_at="2026-08-05T02:10:00+00:00",
        day_in_progress="2026-08-05",
    )
    conn.commit()

    with conn.cursor() as cur:
        cur.execute(
            f"SELECT {', '.join(name for name, _ in schema)} "
            "FROM app.datastream_executions WHERE id = %s",
            (ids["execution_id"],),
        )
        record = _row_to_execution(cur, cur.fetchone())

    from datetime import date as _date
    from datetime import datetime as _datetime
    from datetime import time as _time

    unserialised = [
        name for name in temporal
        if isinstance(record.get(name), (_datetime, _date, _time))
    ]
    assert unserialised == [], (
        f"columns left as raw temporals in the execution payload: {unserialised}"
    )
    # And the columns that DID carry a value came back as strings, not None.
    for name in ("started_at", "progress_updated_at", "day_in_progress"):
        assert isinstance(record[name], str), name


@requires_postgres
def test_a_failed_run_keeps_the_progress_it_had_written(progress_db) -> None:
    """Casse: a partial run and a run that never started are not the same absence."""
    from core.execution_progress import record_and_close

    conn, ids = progress_db
    _set(conn, ids["execution_id"], days_total=20)
    conn.commit()
    _enqueue_window(conn, ids, 0, "2026-07-01", "2026-07-10", "done", 42)
    _enqueue_window(conn, ids, 1, "2026-07-11", "2026-07-20", "dead_letter", None)

    record_and_close(conn, execution_id=ids["execution_id"], actor="test")
    conn.commit()

    with conn.cursor() as cur:
        cur.execute(
            "SELECT state, days_done, rows_written, error_code "
            "FROM app.datastream_executions WHERE id = %s",
            (ids["execution_id"],),
        )
        state, days_done, rows_written, error_code = cur.fetchone()
    assert state == "failed"
    assert (days_done, rows_written) == (10, 42)
    assert error_code == "collection_window_failed"


# ---------------------------------------------------------------------------
# The Workbench, against real rows. Source assertions cannot prove a SQL clause
# selects what it claims to.
# ---------------------------------------------------------------------------


@requires_postgres
def test_a_nightly_collection_never_becomes_a_candidate_to_review(progress_db) -> None:
    """The clause, exercised. A run in flight AND a finished one, both refused.

    From story 63.1 the newest execution of a scheduled Datastream is its
    nightly retrieval. A candidate probe that only excluded terminal states
    would put a permanent "Review candidate" on every scheduled Datastream for
    the length of every run.
    """
    from core.datastream_workbench import _read_base_record
    from core.execution_progress import COLLECTION_PLAN_KIND

    conn, ids = progress_db
    with conn.cursor() as cur:
        cur.execute(
            """UPDATE app.datastream_executions
                  SET projection_plan_ref = %s::jsonb, state = 'loading'
                WHERE id = %s""",
            (f'{{"executable": true, "kind": "{COLLECTION_PLAN_KIND}"}}', ids["execution_id"]),
        )
    conn.commit()

    record = _read_base_record(conn, ids["project_id"], ids["ds_id"])
    assert record["candidate_execution_id"] is None, (
        "a recurring collection in flight was offered as a candidate to review"
    )
    assert record["latest_execution_id"] == ids["execution_id"]
    # Nothing has finished yet, so health is unknown -- not Healthy, not Blocked.
    assert record["latest_terminal_run_state"] is None

    with conn.cursor() as cur:
        cur.execute(
            "UPDATE app.datastream_executions SET state = 'collected' WHERE id = %s",
            (ids["execution_id"],),
        )
    conn.commit()
    record = _read_base_record(conn, ids["project_id"], ids["ds_id"])
    assert record["candidate_execution_id"] is None
    assert record["latest_terminal_run_state"] == "collected"


@requires_postgres
def test_a_real_publication_candidate_is_still_offered(progress_db) -> None:
    """The refusal above must not have swallowed the case it exists beside."""
    from core.datastream_workbench import _read_base_record

    conn, ids = progress_db
    with conn.cursor() as cur:
        cur.execute(
            """UPDATE app.datastream_executions
                  SET projection_plan_ref = '{"executable": true}'::jsonb, state = 'ready'
                WHERE id = %s""",
            (ids["execution_id"],),
        )
    conn.commit()
    record = _read_base_record(conn, ids["project_id"], ids["ds_id"])
    assert record["candidate_execution_id"] == ids["execution_id"]


@requires_postgres
def test_the_thirty_day_rate_counts_a_collection_as_a_success(progress_db) -> None:
    """The number a person reads, computed on real rows.

    Counting only `published` put a failed collection in the denominator and a
    successful one nowhere, so the rate of any Datastream that collects without
    publishing fell toward 0%.
    """
    from core.datastream_workbench import read_tab

    conn, ids = progress_db
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE app.datastream_executions SET state = 'collected' WHERE id = %s",
            (ids["execution_id"],),
        )
    conn.commit()

    payload = read_tab(
        conn, project_id=ids["project_id"], datastream_id=ids["ds_id"], tab="overview"
    )
    run_success = payload["evidence"]["run_success"]
    assert run_success["terminal_count"] == 1
    assert run_success["succeeded_count"] == 1, (
        "a collected run counted as terminal but not as a success: the rate reads 0%"
    )
    assert "published_count" not in run_success


# ---------------------------------------------------------------------------
# Story 58.10 -- a step of a run has a duration, and it is written where `step`
# is written. Against the real UNIQUE and the real CHECK: a fake cursor cannot
# prove that a replayed window does not duplicate a row.
# ---------------------------------------------------------------------------


def _spans(conn, execution_id: str) -> list[tuple]:
    with conn.cursor() as cur:
        cur.execute(
            """SELECT step, started_at, ended_at
                 FROM app.datastream_execution_step_evidence
                WHERE execution_id = %s
                ORDER BY started_at, step""",
            (execution_id,),
        )
        return cur.fetchall()


def _open_a_collection(conn, ids, *, windows=None):
    """Open a REAL collection run through the production entry point.

    The fixture's own execution is `loading`, which holds
    `uq_datastream_executions_active`; it is retired first so
    `open_collection_run` can mint the run whose span this file measures.
    Reaching for the entry point rather than writing `step` by hand is the
    whole lesson of the first cut: a span opened anywhere but at the ENTRY
    measures the bookkeeping instead of the work.
    """
    from core.execution_progress import open_collection_run

    with conn.cursor() as cur:
        cur.execute(
            "UPDATE app.datastream_executions SET state = 'collected' WHERE id = %s",
            (ids["execution_id"],),
        )
    conn.commit()
    execution = open_collection_run(
        conn,
        datastream_id=ids["ds_id"],
        project_id=ids["project_id"],
        plan_version_id=ids["plan_version_id"],
        mapping_version_id=ids["mapping_version_id"],
        windows=windows or [{"date_from": "2026-07-01", "date_to": "2026-07-10"}],
        actor="test",
        idempotency_key=_id("idem_"),
        origin="scheduler_nightly",
    )
    conn.commit()
    assert execution is not None, "no collection run was opened -- the fixture, not the code"
    ids["collection_id"] = str(execution["id"])
    return ids["collection_id"]


@requires_postgres
def test_collect_opens_where_the_run_declares_it_began(progress_db) -> None:
    """The span starts at the run's own `started_at`, not at a window boundary.

    THE DEFECT THIS PINS. The first cut opened the span in
    `record_window_progress`, which `queue._execute_job` calls ONCE, at the end
    of the window, inside the transaction that then closes the run. `NOW()` is
    `transaction_timestamp()`, so both ends carried one instant and a real
    two-second collection measured `0.000000`.
    """
    conn, ids = progress_db
    execution_id = _open_a_collection(conn, ids)

    spans = _spans(conn, execution_id)
    assert [row[0] for row in spans] == ["Collect"]
    # STILL RUNNING: `ended_at` NULL never means "took no time".
    assert spans[0][2] is None
    with conn.cursor() as cur:
        cur.execute(
            "SELECT started_at FROM app.datastream_executions WHERE id = %s",
            (execution_id,),
        )
        run_started_at = cur.fetchone()[0]
    assert spans[0][1] == run_started_at, (
        "the Collect span does not start where the run says it began"
    )


@requires_postgres
def test_a_finished_collection_measures_a_time_that_actually_elapsed(
    progress_db,
) -> None:
    """The feature itself: `Collect 2 min`, and never `Collect 0 s`.

    The two ends are written in TWO transactions -- the run opening and the
    window landing -- so the elapsed time is the time the collection took.
    `> 0` strictly: `>= 0` is the assertion that let the zero through review.
    """
    import time

    from core.execution_progress import record_and_close

    conn, ids = progress_db
    execution_id = _open_a_collection(conn, ids)

    # The collection itself. Short, but a REAL interval between two commits.
    time.sleep(0.05)
    _enqueue_window(
        conn, {**ids, "execution_id": execution_id}, 0, "2026-07-01", "2026-07-10", "done", 7
    )
    record_and_close(conn, execution_id=execution_id, actor="test")
    conn.commit()

    spans = _spans(conn, execution_id)
    assert [row[0] for row in spans] == ["Collect"]
    step, started_at, ended_at = spans[0]
    assert ended_at is not None, "the run ended and its step is still open"
    elapsed = (ended_at - started_at).total_seconds()
    assert elapsed > 0, f"{step} measured {elapsed}s -- the span did not cover the work"
    assert elapsed >= 0.05


@requires_postgres
def test_a_window_boundary_on_the_step_the_run_is_already_on_writes_no_span(
    progress_db,
) -> None:
    """A replay is not a transition, and it moves nothing.

    `record_window_progress` is called once per window, always with `Collect`.
    The span it must NOT touch is the one `open_collection_run` opened: neither
    a duplicate row (`UNIQUE (execution_id, step)`), nor a start moved forward
    to the end of the window, nor an end written while the run is still going.
    """
    from core.execution_progress import record_window_progress

    conn, ids = progress_db
    execution_id = _open_a_collection(conn, ids)
    opened_at = _spans(conn, execution_id)[0][1]
    _enqueue_window(
        conn, {**ids, "execution_id": execution_id}, 0, "2026-07-01", "2026-07-10", "done", 5
    )

    for _ in range(3):
        record_window_progress(
            conn, execution_id=execution_id, project_id=ids["project_id"], step="Collect"
        )
        conn.commit()

    spans = _spans(conn, execution_id)
    assert len(spans) == 1, "a replayed window duplicated the span of its step"
    assert spans[0][1] == opened_at, "a replayed window moved the start of the span"
    assert spans[0][2] is None, "a replayed window closed a step that is still running"


@requires_postgres
def test_moving_to_another_step_closes_the_one_it_leaves(progress_db) -> None:
    """A REAL transition: `Collect` ends, `Map` opens, and `Map` is still open."""
    from core.execution_progress import record_window_progress

    conn, ids = progress_db
    execution_id = _open_a_collection(conn, ids)
    _enqueue_window(
        conn, {**ids, "execution_id": execution_id}, 0, "2026-07-01", "2026-07-10", "done", 5
    )

    record_window_progress(
        conn, execution_id=execution_id, project_id=ids["project_id"], step="Map"
    )
    conn.commit()

    spans = {row[0]: row for row in _spans(conn, execution_id)}
    assert set(spans) == {"Collect", "Map"}
    assert spans["Collect"][2] is not None, "the step being left was not closed"
    assert (spans["Collect"][2] - spans["Collect"][1]).total_seconds() > 0
    assert spans["Map"][2] is None, "the step being entered must stay open"

    # And replaying the move writes nothing more.
    closed_at = spans["Collect"][2]
    record_window_progress(
        conn, execution_id=execution_id, project_id=ids["project_id"], step="Map"
    )
    conn.commit()
    replayed = {row[0]: row for row in _spans(conn, execution_id)}
    assert len(replayed) == 2
    assert replayed["Collect"][2] == closed_at, "a replay moved the end of a closed span"


@requires_postgres
def test_a_step_with_no_entry_point_is_left_unopened(progress_db) -> None:
    """No entry instant, no span -- and the read model says so.

    The fixture's execution has never declared a step. A window boundary on it
    is a first sighting at the END of the work, not a move: opening a span here
    would time the bookkeeping. Nothing is written, and the track renders its
    named absence rather than a fabricated duration.
    """
    from core.execution_progress import record_window_progress

    conn, ids = progress_db
    _enqueue_window(conn, ids, 0, "2026-07-01", "2026-07-10", "done", 5)

    written = record_window_progress(
        conn, execution_id=ids["execution_id"], project_id=ids["project_id"], step="Collect"
    )
    conn.commit()

    # The progress itself IS written -- only the span is withheld.
    assert written is not None
    assert written["step"] == "Collect"
    assert "previous_step" not in written
    assert _spans(conn, ids["execution_id"]) == []


@requires_postgres
def test_advance_state_closes_the_span_of_the_run_it_ends(progress_db) -> None:
    """One of the TWO paths that close a span, proved from outside this module.

    `advance_state` is where the state machine is enforced for the paths that
    use it, and the close lives there. It is NOT universal, and the test below
    is the one that proves the rest.
    """
    from core.datastream_publication import STATE_FAILED, advance_state

    conn, ids = progress_db
    execution_id = _open_a_collection(conn, ids)
    assert _spans(conn, execution_id)[0][2] is None

    advance_state(
        execution_id,
        None,
        STATE_FAILED,
        "test",
        conn,
        project_id=ids["project_id"],
        error_code="dq_gate_failed",
        error_detail="a path that is not execution_progress",
    )
    conn.commit()

    spans = _spans(conn, execution_id)
    assert spans[0][2] is not None, (
        "a terminal transition through advance_state left the step open"
    )
    assert (spans[0][2] - spans[0][1]).total_seconds() > 0


@requires_postgres
def test_a_run_published_without_advance_state_is_not_reported_as_still_running(
    progress_db,
) -> None:
    """THE TEST THAT WOULD HAVE CAUGHT THE FALSE CLAIM -- second review, finding 1.

    THE NAME IS INHERITED AND IS NOW HALF WRONG, deliberately kept: when it was
    written, `commit_publication` advanced `ready -> publishing -> published`
    with its OWN two `UPDATE`s and never called `advance_state`. The first repair
    claimed every terminal transition crossed that seam and wrote it into a
    ratified document; one probe refuted it -- the run reached `published` with
    its `Collect` span still open. AI-223 built the seam the claim assumed:
    `commit_publication` now asks the machine for both steps.

    Two things are asserted, and they are different:

      * the run ends with its span closed -- and it no longer matters which of
        the two writes it, which is the point of the conversion;
      * and even if nothing closed it -- which is the case for every run
        published before AI-223, and for a crash between the state write and the
        close -- the READ model refuses to call the step still running, because
        the run's state says it is not.
    """
    from core.datastream_publication import (
        STATE_READY,
        STATE_VALIDATING,
        advance_state,
        commit_publication,
    )
    from core.datastream_workbench import read_tab

    conn, ids = progress_db
    execution_id = _open_a_collection(conn, ids)
    assert _spans(conn, execution_id)[0][2] is None

    # Walk the run to `ready` with the provenance publication demands, then
    # publish it through the function that owns its own state machine.
    advance_state(execution_id, None, STATE_VALIDATING, "test", conn,
                  project_id=ids["project_id"])
    advance_state(execution_id, STATE_VALIDATING, STATE_READY, "test", conn,
                  project_id=ids["project_id"], content_hash="e" * 64, row_count=42)
    conn.commit()

    commit_publication(
        execution_id, ids["project_id"], "test", conn, connection_factory=lambda: conn
    )

    with conn.cursor() as cur:
        cur.execute(
            "SELECT state FROM app.datastream_executions WHERE id = %s", (execution_id,)
        )
        assert cur.fetchone()[0] == "published"

    spans = _spans(conn, execution_id)
    assert spans[0][2] is not None, (
        "commit_publication ended the run and left its step open"
    )

    # And the read model, which is the half that cannot be bypassed.
    run = next(
        row
        for row in read_tab(
            conn, project_id=ids["project_id"], datastream_id=ids["ds_id"], tab="runs"
        )["evidence"]["runs"]
        if row["id"] == execution_id
    )
    collect = next(span for span in run["steps"] if span["step"] == "Collect")
    assert collect["still_running"] is False


@requires_postgres
def test_an_open_span_on_a_terminal_run_is_never_reported_as_running(
    progress_db,
) -> None:
    """The guarantee that survives the seam, and is not made redundant by it.

    The span is left open ON PURPOSE and the run is put in a terminal state by a
    raw UPDATE -- which is what `datastream_activation`, `reconcile_execution`
    and `_reconcile_fail_closed` did until AI-223, what every run published
    before it left behind, and what a crash between the state write and the span
    close still produces. Nothing closes it, and the payload still refuses to say
    the step is working.
    """
    from core.datastream_workbench import read_tab

    conn, ids = progress_db
    execution_id = _open_a_collection(conn, ids)
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE app.datastream_executions SET state = 'cancelled' WHERE id = %s",
            (execution_id,),
        )
    conn.commit()

    assert _spans(conn, execution_id)[0][2] is None, "the span must stay open for this test"

    run = next(
        row
        for row in read_tab(
            conn, project_id=ids["project_id"], datastream_id=ids["ds_id"], tab="runs"
        )["evidence"]["runs"]
        if row["id"] == execution_id
    )
    collect = next(span for span in run["steps"] if span["step"] == "Collect")
    assert collect["ended_at"] is None
    assert collect["duration_seconds"] is None
    assert collect["still_running"] is False, (
        "an open span under a terminal run was reported as a step still working"
    )

    # And while the run IS moving, the same open span reads the other way.
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE app.datastream_executions SET state = 'loading' WHERE id = %s",
            (execution_id,),
        )
    conn.commit()
    run = next(
        row
        for row in read_tab(
            conn, project_id=ids["project_id"], datastream_id=ids["ds_id"], tab="runs"
        )["evidence"]["runs"]
        if row["id"] == execution_id
    )
    assert next(s for s in run["steps"] if s["step"] == "Collect")["still_running"] is True


@requires_postgres
def test_closing_a_span_never_writes_an_end_before_its_start(progress_db) -> None:
    """Finding 3: the CHECK of migration 223 must not become a 500.

    A stop opened at 10:04 whose in-flight window opens its span at 10:07 would
    write `ended_at < started_at`, `CheckViolation` aborts the caller's whole
    transaction, and the `Stop` gesture answers 500 for a bookkeeping row.
    `GREATEST(started_at, clock_timestamp())` is what keeps it a row.
    """
    from core.execution_progress import close_open_step_spans

    conn, ids = progress_db
    execution_id = _open_a_collection(conn, ids)
    # A span that opens in the future, which is what a clock skew or an
    # interleaved transaction produces at a smaller scale.
    with conn.cursor() as cur:
        cur.execute(
            """UPDATE app.datastream_execution_step_evidence
                  SET started_at = clock_timestamp() + interval '1 hour'
                WHERE execution_id = %s""",
            (execution_id,),
        )
    conn.commit()

    close_open_step_spans(conn, execution_id=execution_id)
    conn.commit()

    step, started_at, ended_at = _spans(conn, execution_id)[0]
    assert ended_at == started_at, f"{step} closed before it started"
    # And the read model calls that zero an ABSENCE, never a fast step.
    assert (ended_at - started_at).total_seconds() == 0


@requires_postgres
def test_the_track_reaches_the_runs_tab_over_real_rows(progress_db) -> None:
    """The payload, read through `read_tab` -- which joins `app.project_flux`.

    The fixture seeds that row (see above); without it `_read_base_record`
    answers "not found" and this test would be measuring the fixture.
    """
    import time

    from core.datastream_workbench import read_tab
    from core.execution_progress import record_window_progress

    conn, ids = progress_db
    execution_id = _open_a_collection(conn, ids)
    _enqueue_window(
        conn, {**ids, "execution_id": execution_id}, 0, "2026-07-01", "2026-07-10", "done", 5
    )
    time.sleep(0.05)
    record_window_progress(
        conn, execution_id=execution_id, project_id=ids["project_id"], step="Map"
    )
    conn.commit()

    evidence = read_tab(
        conn, project_id=ids["project_id"], datastream_id=ids["ds_id"], tab="runs"
    )["evidence"]
    assert evidence["step_vocabulary"] == ["Collect", "Map", "Check", "Publish"]
    run = next(row for row in evidence["runs"] if row["id"] == execution_id)
    steps = {span["step"]: span for span in run["steps"]}
    assert set(steps) == {"Collect", "Map"}
    # A REAL elapsed time on the wire -- `> 0`, which is the assertion the first
    # cut of this test did not make.
    assert steps["Collect"]["duration_seconds"] > 0
    assert steps["Collect"]["duration_seconds"] >= 0.05
    # The step it is on right now has no duration at all -- not a zero.
    assert steps["Map"]["ended_at"] is None
    assert steps["Map"]["duration_seconds"] is None
    # And the anomaly path exists and is empty, which is what it must say:
    # `app.dq_issues` holds 0 rows on both bases (migration 222 added the
    # column, nothing has written one). The empty entry carries `issues` too --
    # the counter and the list it counts are minted together
    # (`core/datastream_workbench.py:1156`), so a run with no anomaly says
    # "none, and here is the empty list" rather than omitting the list.
    assert run["anomalies"] == {"anomalies": 0, "evaluations": 0, "issues": []}


@requires_postgres
def test_a_step_span_is_refused_when_it_ends_before_it_started(progress_db) -> None:
    """Migration 223's CHECK, exercised -- and `ended_at = started_at` is legal.

    The row is legal; what the READ model refuses is calling that zero a
    duration (`STEP_NOT_TIMED`). The database keeps accepting it because a step
    whose two ends are one instant is a row worth storing -- it says the span
    was opened and closed without measuring anything.
    """
    conn, ids = progress_db
    execution_id = _open_a_collection(conn, ids)

    with conn.cursor() as cur:
        cur.execute(
            """UPDATE app.datastream_execution_step_evidence
                  SET ended_at = started_at
                WHERE execution_id = %s""",
            (execution_id,),
        )
    conn.commit()

    with pytest.raises(Exception) as excinfo:
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE app.datastream_execution_step_evidence
                      SET ended_at = started_at - interval '1 second'
                    WHERE execution_id = %s""",
                (execution_id,),
            )
    assert "datastream_execution_step_evidence" in str(excinfo.value)
    conn.rollback()


@requires_postgres
def test_the_runs_tab_is_bounded_and_declares_its_bound(progress_db) -> None:
    from core.datastream_workbench import RUN_LIST_LIMIT, read_tab

    conn, ids = progress_db
    evidence = read_tab(
        conn, project_id=ids["project_id"], datastream_id=ids["ds_id"], tab="runs"
    )["evidence"]
    assert evidence["runs_limit"] == RUN_LIST_LIMIT
    assert evidence["runs_truncated"] is False
    assert len(evidence["runs"]) <= RUN_LIST_LIMIT
