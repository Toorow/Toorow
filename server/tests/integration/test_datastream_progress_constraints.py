"""Live-Postgres contract for the progress read -- story 63.2.

What a fake cursor cannot catch, and what this route is judged on:

  * TWO Datastreams, each with its own run in flight -- each call answers ITS
    OWN. A read filtered on `datastream_id` alone would still pass a mocked
    test; only a real second row proves the pair.
  * A Datastream read under a project that does not own it is `not_found`, and
    the run it holds is never disclosed.
  * The statement really is an INDEX PROBE of `uq_datastream_executions_active`,
    not a scan of a table that now grows by one row per Datastream per night.

`pg_owner`: the fixture applies migration 218, which is DDL.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import date
from pathlib import Path

import pytest
from core.datastream_progress_api import (
    IDLE_FIELDS,
    IDLE_LAST_RUN_FAILED,
    IDLE_LAST_RUN_SUCCEEDED,
    IDLE_NEVER_RAN,
    PROGRESS_SQL,
    DatastreamNotFound,
    read_active_progress,
    read_idle_reason,
)

from tests.conftest import purge_fixture_project

ROOT = Path(__file__).resolve().parents[3]
MIGRATIONS = ROOT / "infra" / "nango" / "migrations"
PROGRESS_MIGRATION = MIGRATIONS / "218_a_running_collection_writes_where_it_is.sql"

_skip_without_dsn = pytest.mark.skipif(
    not os.environ.get("TEST_POSTGRES_DSN"),
    reason="TEST_POSTGRES_DSN not set -- live Postgres constraint test skipped",
)


def requires_postgres(fn):
    """The DSN gate AND the `pg_owner` marker, on the live tests only."""
    return _skip_without_dsn(pytest.mark.pg_owner(fn))


_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def _dse() -> str:
    """A FRESH house-style execution id per test, so two files never collide."""
    body = uuid.uuid4().int
    chars = []
    for _ in range(26):
        body, index = divmod(body, len(_ALPHABET))
        chars.append(_ALPHABET[index])
    return "dse_" + "".join(reversed(chars))


def _id(prefix: str) -> str:
    return f"{prefix}{uuid.uuid4().hex[:12]}"


# ---------------------------------------------------------------------------
# The statement itself (runs without Postgres).
# ---------------------------------------------------------------------------


def test_the_statement_carries_both_halves_of_the_scope() -> None:
    """53.1's rule, on the route polled the most: never the datastream id alone."""
    assert "d.project_id = %s" in PROGRESS_SQL
    assert "run.project_id = d.project_id" in PROGRESS_SQL


def test_the_executions_table_is_never_sorted_by_the_polled_statement() -> None:
    """That table grows by a row per Datastream per night; the probe may not sort it.

    Every ordering it carries is inside a pull-job LATERAL and every one is
    BOUNDED by a LIMIT of its own: the window in flight out of the run's OWN
    jobs, behind `idx_pull_jobs_execution`; the ROWS story 63.4 reads its rate
    from, behind `idx_pull_jobs_datastream_completed` (migration 219); and the
    runs those rows are grouped into. None of them touches the executions table.
    """
    import re

    from core.datastream_progress_estimate import HISTORY_RUNS, HISTORY_WINDOWS

    assert [" ".join(line.split()) for line in re.findall(r"ORDER BY .+", PROGRESS_SQL)] == [
        "ORDER BY j.date_from ASC, j.id ASC",
        "ORDER BY j.completed_at DESC",
        "ORDER BY max(recent.completed_at) DESC",
    ]
    assert PROGRESS_SQL.upper().count("LIMIT") == 3
    assert f"LIMIT {HISTORY_WINDOWS}" in PROGRESS_SQL
    assert f"LIMIT {HISTORY_RUNS}" in PROGRESS_SQL
    # The row bound comes BEFORE the aggregate, or it bounds nothing that is read.
    assert PROGRESS_SQL.index(f"LIMIT {HISTORY_WINDOWS}") < PROGRESS_SQL.index("GROUP BY")
    assert not re.search(r"ORDER BY[^\n]*\brun\.", PROGRESS_SQL)


# ---------------------------------------------------------------------------
# Live Postgres.
# ---------------------------------------------------------------------------


def _seed_project(cur, *, state: str):
    """One project, one Datastream, one execution in `state`. Returns the ids."""
    project_id = _id("proj_")
    ds_id = _id("ds_")
    plan_id = _id("dsp_")
    mapping_id = _id("dmap_")
    exec_id = _dse()

    cur.execute(
        """
        INSERT INTO app.projects (id, name, slug, created_by, org_id)
        VALUES (%s, %s, %s, 'story-63.2-test', 'org_test_fixture')
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
        VALUES (%s, %s, %s, %s, %s, '{}'::jsonb, %s, 'test')
        """,
        (exec_id, ds_id, project_id, plan_id, mapping_id, state),
    )
    return {
        "project_id": project_id,
        "ds_id": ds_id,
        "plan_id": plan_id,
        "mapping_id": mapping_id,
        "execution_id": exec_id,
    }


def _seed_window(cur, ids, *, date_from, date_to, state, row_count=None):
    """One pull job bound to the run: a window, with its state and what it landed."""
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


def _drop_project(cur, ids):
    cur.execute("DELETE FROM app.pull_jobs WHERE execution_id = %s", (ids["execution_id"],))
    cur.execute("DELETE FROM app.datastream_executions WHERE id = %s", (ids["execution_id"],))
    # Plan and mapping versions are immutable BY TRIGGER (migrations 030 / 032):
    # the fixture is the owner precisely so it can lift the guard for its own
    # rows and put it straight back, rather than leave test rows forever.
    cur.execute("ALTER TABLE app.datastream_mapping_versions DISABLE TRIGGER USER")
    cur.execute("ALTER TABLE app.datastream_plan_versions DISABLE TRIGGER USER")
    try:
        cur.execute(
            "DELETE FROM app.datastream_mapping_versions WHERE id = %s", (ids["mapping_id"],)
        )
        cur.execute("DELETE FROM app.datastream_plan_versions WHERE id = %s", (ids["plan_id"],))
    finally:
        cur.execute("ALTER TABLE app.datastream_plan_versions ENABLE TRIGGER USER")
        cur.execute("ALTER TABLE app.datastream_mapping_versions ENABLE TRIGGER USER")
    cur.execute("DELETE FROM app.datastreams WHERE id = %s", (ids["ds_id"],))
    cur.execute("DELETE FROM app.connection_ref WHERE project_id = %s", (ids["project_id"],))
    # Creating a project arms side tables by trigger; ask the catalog which
    # tables reference app.projects rather than enumerate them by hand.
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
        cur.execute(f"DELETE FROM {table} WHERE {column} = %s", (ids["project_id"],))
    # AI-291: le graphe prend le relais si une table gouvernee
    # ajoutee depuis retient le projet en ON DELETE RESTRICT.
    purge_fixture_project(cur.connection, ids["project_id"])


@pytest.fixture
def two_running_fluxes(live_postgres):
    """Two projects, two Datastreams, one run in flight each -- plus one at rest."""
    conn = live_postgres
    with conn.cursor() as cur:
        cur.execute(PROGRESS_MIGRATION.read_text(encoding="utf-8"))
    conn.commit()

    with conn.cursor() as cur:
        first = _seed_project(cur, state="loading")
        second = _seed_project(cur, state="validating")
        # A third flux whose only run has finished: nothing is moving there.
        at_rest = _seed_project(cur, state="published")
        cur.execute(
            """
            UPDATE app.datastream_executions
            SET step = 'Collect', day_in_progress = DATE '2026-07-12',
                days_done = 12, days_total = 30, rows_written = 4218,
                started_at = NOW(), progress_updated_at = NOW()
            WHERE id = %s
            """,
            (first["execution_id"],),
        )
        # Three windows of a two-year catch-up: one landed, one being collected,
        # one still queued. This is the shape that produces a plateau.
        _seed_window(
            cur, first, date_from="2026-06-01", date_to="2026-06-30",
            state="done", row_count=4218,
        )
        _seed_window(
            cur, first, date_from="2026-07-01", date_to="2026-07-31", state="running"
        )
        _seed_window(
            cur, first, date_from="2026-08-01", date_to="2026-08-31", state="queued"
        )
    conn.commit()

    yield conn, first, second, at_rest

    conn.rollback()
    with conn.cursor() as cur:
        for ids in (first, second, at_rest):
            _drop_project(cur, ids)
    conn.commit()


@requires_postgres
def test_each_flux_answers_its_own_run(two_running_fluxes) -> None:
    conn, first, second, _ = two_running_fluxes

    left = read_active_progress(
        conn, project_id=first["project_id"], datastream_id=first["ds_id"]
    )
    right = read_active_progress(
        conn, project_id=second["project_id"], datastream_id=second["ds_id"]
    )

    assert left is not None and right is not None
    assert left["execution_id"] == first["execution_id"]
    assert right["execution_id"] == second["execution_id"]
    assert left["execution_id"] != right["execution_id"]
    # And the numbers written by 63.1 come back as they were measured.
    assert (left["days_done"], left["days_total"], left["rows_written"]) == (12, 30, 4218)
    assert left["step"] == "Collect"
    assert str(left["day_in_progress"]) == "2026-07-12"
    # The run at rest carries none of them: absence, not zero.
    assert (right["days_done"], right["days_total"], right["rows_written"]) == (
        None,
        None,
        None,
    )


@requires_postgres
def test_the_window_in_flight_and_its_size_come_from_the_run_s_own_jobs(
    two_running_fluxes,
) -> None:
    """What makes a plateau readable, derived -- no new column anywhere."""
    conn, first, _, _ = two_running_fluxes

    progress = read_active_progress(
        conn, project_id=first["project_id"], datastream_id=first["ds_id"]
    )

    assert progress is not None
    assert (progress["windows_done"], progress["windows_total"]) == (1, 3)
    assert progress["window_in_progress"] == {
        "date_from": date(2026, 7, 1),
        "date_to": date(2026, 7, 31),
        # 31 days landed by ONE provider call: the day count cannot move until
        # this window finishes, and that is the sentence a screen owes a person.
        "days": 31,
        # The per-window chronometer (migration 006), which story 63.4 needs to
        # see that a window has outrun what this Datastream usually spends. This
        # fixture enqueued the job without dequeuing it, so it has not started.
        "started_at": None,
        "completed_at": None,
    }


@requires_postgres
def test_a_run_with_no_window_declared_none_rather_than_zero(two_running_fluxes) -> None:
    conn, _, second, _ = two_running_fluxes

    progress = read_active_progress(
        conn, project_id=second["project_id"], datastream_id=second["ds_id"]
    )

    assert progress is not None
    assert progress["windows_total"] is None
    assert progress["windows_done"] is None
    assert progress["window_in_progress"] is None


@requires_postgres
def test_a_terminal_run_leaves_nothing_active_to_report(two_running_fluxes) -> None:
    """The answer that stops the poll, proven against the real index predicate."""
    conn, _, _, at_rest = two_running_fluxes

    assert (
        read_active_progress(
            conn, project_id=at_rest["project_id"], datastream_id=at_rest["ds_id"]
        )
        is None
    )


# ---------------------------------------------------------------------------
# Why the poll stopped -- three sentences, against the real state machine.
# ---------------------------------------------------------------------------


@requires_postgres
def test_a_run_that_finished_well_says_so_when_the_poll_stops(two_running_fluxes) -> None:
    conn, _, _, at_rest = two_running_fluxes

    idle = read_idle_reason(
        conn, project_id=at_rest["project_id"], datastream_id=at_rest["ds_id"]
    )

    assert idle["reason"] == IDLE_LAST_RUN_SUCCEEDED
    assert idle["execution_id"] == at_rest["execution_id"]
    assert idle["state"] == "published"
    assert idle["ended_at"] is not None


@requires_postgres
def test_a_run_that_ended_badly_is_a_different_sentence(two_running_fluxes) -> None:
    conn, _, _, at_rest = two_running_fluxes
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE app.datastream_executions
            SET state = 'failed', error_code = 'collection_window_failed',
                state_changed_at = NOW()
            WHERE id = %s
            """,
            (at_rest["execution_id"],),
        )

    idle = read_idle_reason(
        conn, project_id=at_rest["project_id"], datastream_id=at_rest["ds_id"]
    )

    assert idle["reason"] == IDLE_LAST_RUN_FAILED
    assert idle["state"] == "failed"
    assert idle["error_code"] == "collection_window_failed"


@requires_postgres
def test_a_flux_that_never_ran_is_a_third_sentence(two_running_fluxes) -> None:
    """`never_ran` is not `last_run_succeeded` with the fields blanked out."""
    conn, first, _, _ = two_running_fluxes
    # This flux has one execution and it is IN FLIGHT: no terminal run exists.
    idle = read_idle_reason(
        conn, project_id=first["project_id"], datastream_id=first["ds_id"]
    )

    # Les champs sont lus dans `IDLE_FIELDS`, jamais recopies : la story 63.5 en
    # a ajoute trois (`days_done`, `days_total`, `rows_written`) et une liste
    # tenue a la main ici aurait fait echouer le seul test qui dit ce que
    # `never_ran` porte. L'egalite reste STRICTE -- aucune cle en plus, aucune
    # en moins, et toutes nulles sauf la phrase.
    assert idle == {**{field: None for field in IDLE_FIELDS}, "reason": IDLE_NEVER_RAN}
    assert tuple(idle) == IDLE_FIELDS


@requires_postgres
def test_the_idle_read_is_scoped_to_the_project_like_the_progress_read(
    two_running_fluxes,
) -> None:
    """A reason read on the datastream id alone would leak another project's run."""
    conn, _, second, at_rest = two_running_fluxes

    idle = read_idle_reason(
        conn, project_id=second["project_id"], datastream_id=at_rest["ds_id"]
    )

    assert idle["reason"] == IDLE_NEVER_RAN
    assert idle["execution_id"] is None


@requires_postgres
def test_a_flux_read_under_the_wrong_project_discloses_nothing(two_running_fluxes) -> None:
    conn, first, second, _ = two_running_fluxes

    with pytest.raises(DatastreamNotFound):
        read_active_progress(
            conn, project_id=second["project_id"], datastream_id=first["ds_id"]
        )


@requires_postgres
def test_the_read_is_an_index_probe_and_not_a_scan(two_running_fluxes) -> None:
    """`uq_datastream_executions_active` is exactly "the run in flight of this flux".

    Scans are disabled for the plan, because the fixture's handful of rows would
    make a sequential scan cheapest whatever the indexes say. What is being
    measured is whether the partial index is APPLICABLE at all -- it is only if
    Postgres can prove the query's state list implies the index predicate.

    `force_generic_plan` is the point of the test, not a detail: a statement
    executed in a loop is prepared, and a generic plan has no values to reason
    about. Under it, a bound `state = ANY($n)` falls off this index (measured
    2026-08-05: it lands on `idx_datastream_executions_project_state`), which is
    why the state list is generated into the statement from the registry.
    """
    conn, first, _, _ = two_running_fluxes

    with conn.cursor() as cur:
        cur.execute("SET LOCAL enable_seqscan = off")
        cur.execute("SET LOCAL plan_cache_mode = force_generic_plan")
        cur.execute(
            "EXPLAIN (FORMAT JSON) " + PROGRESS_SQL, (first["ds_id"], first["project_id"])
        )
        plan = json.dumps(cur.fetchone()[0])
    conn.rollback()

    assert "uq_datastream_executions_active" in plan, plan
    assert "Seq Scan" not in plan, plan
