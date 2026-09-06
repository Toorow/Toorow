"""An Analyze execution records the Output it consumed. Migration 138's table.

WHY THIS FILE EXISTS. `app.datastream_output_used_by` shipped with story 47.5 and
has had, ever since, **three readers and no writer**:

  * the Datastream Workbench's `Used by` panel (`datastream_workbench.py`, the
    Outputs tab);
  * the Overview's `downstream_count`;
  * `capability_proposals._fan_out`, which reported `state: "observed"` with a
    count of zero -- a sentence that reads as *nothing depends on this Output*
    rather than *nobody ever recorded it*.

The ruling of 2026-08-12 settled that only the CONSUMER may write the row, never
the publication path, and a conformance test
(`tests/core/test_publication_writes_no_consumer.py`) pinned that. But the
consumer did not write it either, so the table was structurally empty and the
panel was empty by construction. The conformance test's own docstring named the
address the writer should have -- `query_execution`, at the read -- and
instructed that it be NARROWED rather than deleted when the writer landed. It has
been.

WHAT THIS PROVES, in order of value:

  1. the write happens where the consumption happens -- `run_execution`'s plan,
     the moment a `relation_ref` becomes a physical relation;
  2. THE READ<->WRITE LOOP CLOSES: the row the execution writes is the row the
     Workbench's `Used by` panel serves. Three readers with no writer and a
     writer no reader sees are the same defect;
  3. it is idempotent -- an execution is retried, and the table's trigger refuses
     UPDATE and DELETE outright, so a second run must conflict into nothing
     rather than raise;
  4. it never costs a Result -- an unresolvable Output records nothing and
     raises nothing.

Skipped without TEST_POSTGRES_DSN:
    python scripts/disposable_postgres.py up
"""

from __future__ import annotations

import os

import psycopg
import pytest

_DSN = os.environ.get("TEST_POSTGRES_DSN", "")

pytestmark = pytest.mark.skipif(not _DSN, reason="Requires TEST_POSTGRES_DSN")


@pytest.fixture()
def conn():
    connection = psycopg.connect(_DSN)
    try:
        yield connection
    finally:
        # Nothing is committed by this suite: the writer joins the CALLER's
        # transaction on purpose (a Result that rolls back must not leave a
        # dependency claiming to exist), so a rollback undoes the whole chain.
        connection.rollback()
        connection.close()


@pytest.fixture()
def published(conn):
    """A Datastream with one published Output version, built through the schema."""
    from tests.integration.epic66_fixtures import (
        make_canonical_field,
        make_datastream,
        make_project,
        publish_output,
    )

    org_id, project_id = make_project(conn, "consumption")
    # `bindings` is what makes `make_datastream` PUBLISH a plan and a mapping
    # version, and `publish_output` needs the plan version to hang the Output
    # off. A Datastream built without them has nothing to publish, which is a
    # real refusal of this schema rather than a fixture detail.
    day = make_canonical_field(conn, project_id, "day", value_type="date")
    spend = make_canonical_field(
        conn, project_id, "spend", kind="metric", value_type="money"
    )
    datastream_id = make_datastream(
        conn,
        org_id,
        project_id,
        "Consumed stream",
        bindings={day: ("date", "confirmed"), spend: ("spend_micros", "confirmed")},
    )
    version_id = publish_output(
        conn, org_id, project_id, datastream_id, "consumption_relation"
    )
    with conn.cursor() as cur:
        cur.execute(
            "SELECT output_id FROM app.datastream_output_versions WHERE id = %s",
            (version_id,),
        )
        output_id = cur.fetchone()[0]
        # The Workbench reads through `app.project_flux`, not `app.datastreams`
        # directly: its base record INNER JOINs the membership table, so a
        # Datastream absent from it is `WorkbenchNotFound` however well published.
        cur.execute(
            "INSERT INTO app.project_flux (project_id, flux_id, org_id) "
            "VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
            (project_id, datastream_id, org_id),
        )
    return {
        "org_id": org_id,
        "project_id": project_id,
        "datastream_id": datastream_id,
        "output_version_id": version_id,
        "output_id": output_id,
    }


def _plan(published: dict) -> dict:
    """The two keys `resolve_physical_plan` now carries out of its own SELECT.

    They were the whole blocker: the resolver read `relation_ref, execution_id,
    publication_log_id, created_at` and discarded `id` and `output_id`, which are
    exactly the two NOT NULL foreign keys the row needs.
    """
    return {
        "output_version_id": published["output_version_id"],
        "output_id": published["output_id"],
        "datastream_id": published["datastream_id"],
    }


def _rows(cur, published: dict) -> list[tuple]:
    cur.execute(
        "SELECT consumer_kind, consumer_ref, consumer_version_ref, owner_href "
        "FROM app.datastream_output_used_by WHERE output_version_id = %s",
        (published["output_version_id"],),
    )
    return cur.fetchall()


def test_an_execution_records_the_output_it_read(conn, published):
    """The consumer writes, and it writes its own identity.

    `consumer_ref` is the Result id and `owner_href` addresses that Result -- the
    two things the conformance test's docstring asked to be checked at the moment
    the guard was narrowed.
    """
    from core.query_execution import record_output_consumption

    record_output_consumption(
        conn,
        project_id=published["project_id"],
        plan=_plan(published),
        result_id="qr_CONSUMPTION_EXAMPLE",
        query_spec_version_id="qsv_EXAMPLE",
    )

    with conn.cursor() as cur:
        rows = _rows(cur, published)
    assert len(rows) == 1, f"the consumption was not recorded: {rows!r}"
    kind, ref, version_ref, href = rows[0]
    assert kind == "result", (
        "`delivery` is an Output kind and never a consumer this product serves; "
        "a Semantic View does not read an Output, an execution against it does"
    )
    assert ref == "qr_CONSUMPTION_EXAMPLE"
    assert version_ref == "qsv_EXAMPLE"
    assert href.endswith("/analyze/results/qr_CONSUMPTION_EXAMPLE"), href
    assert published["project_id"] in href, (
        "the address must be Project-scoped, like every other Analyze address"
    )


def test_a_retried_execution_does_not_raise_and_does_not_duplicate(conn, published):
    """Idempotent on the primary key, because the row can never be updated.

    Migration 138 puts a BEFORE UPDATE OR DELETE trigger on this table -- Workbench
    evidence is immutable. `ON CONFLICT DO UPDATE` would therefore fire it and
    abort the whole transaction, taking the Result with it. `DO NOTHING` is the
    only clause the schema permits, and this is the test that would catch the
    other one.
    """
    from core.query_execution import record_output_consumption

    for _ in range(3):
        record_output_consumption(
            conn,
            project_id=published["project_id"],
            plan=_plan(published),
            result_id="qr_RETRIED_EXAMPLE",
            query_spec_version_id="qsv_EXAMPLE",
        )

    with conn.cursor() as cur:
        rows = _rows(cur, published)
    assert len(rows) == 1, f"a retried execution duplicated its dependency: {rows!r}"


def test_two_results_are_two_consumers_of_one_output(conn, published):
    """The panel counts consumers, so the key must let a second one in.

    The primary key is `(output_version_id, consumer_kind, consumer_ref)`: same
    Output, different Result, two rows. A key that collapsed them would make the
    panel under-report exactly as badly as the empty table did.
    """
    from core.query_execution import record_output_consumption

    for result_id in ("qr_FIRST_EXAMPLE", "qr_SECOND_EXAMPLE"):
        record_output_consumption(
            conn,
            project_id=published["project_id"],
            plan=_plan(published),
            result_id=result_id,
            query_spec_version_id="qsv_EXAMPLE",
        )

    with conn.cursor() as cur:
        rows = _rows(cur, published)
    assert len(rows) == 2, f"two Results did not read as two consumers: {rows!r}"


def test_an_unresolved_output_records_nothing_and_raises_nothing(conn, published):
    """An execution that resolved no Output version consumed nothing.

    Silence here is the honest answer rather than a swallowed failure -- and it
    must not cost the Result, which is the whole reason the writer is guarded.
    """
    from core.query_execution import record_output_consumption

    record_output_consumption(
        conn,
        project_id=published["project_id"],
        plan={"datastream_id": published["datastream_id"]},
        result_id="qr_NOTHING_EXAMPLE",
        query_spec_version_id=None,
    )

    with conn.cursor() as cur:
        rows = _rows(cur, published)
    assert rows == [], f"a plan with no Output version wrote a dependency: {rows!r}"


def test_a_failed_write_does_not_poison_the_results_transaction(conn, published):
    """The SAVEPOINT is the point: swallowing without one aborts the whole tx.

    An unguarded failed statement leaves a Postgres transaction in the aborted
    state, so the Result that follows a few lines later would fail too -- the
    exact opposite of "a bookkeeping row must not cost a Result that ran". A
    dangling FK is the cheapest way to make the INSERT fail for real.
    """
    from core.query_execution import record_output_consumption

    record_output_consumption(
        conn,
        project_id=published["project_id"],
        plan={
            "output_version_id": "dsov_DOES_NOT_EXIST",
            "output_id": "dso_DOES_NOT_EXIST",
            "datastream_id": published["datastream_id"],
        },
        result_id="qr_POISON_EXAMPLE",
        query_spec_version_id=None,
    )

    # The transaction must still be usable, which is the whole assertion.
    with conn.cursor() as cur:
        cur.execute("SELECT 1")
        assert cur.fetchone()[0] == 1, "the failed bookkeeping write aborted the tx"


def test_the_workbench_used_by_panel_serves_what_the_execution_wrote(conn, published):
    """THE POINT OF THE WHOLE FILE: three readers finally have their writer.

    Asserted through `read_tab`, the real path the Outputs tab calls, rather than
    a second hand-written SELECT -- a row written where the panel does not look
    would be the same defect as a panel with no row.
    """
    from core.datastream_workbench import read_tab
    from core.query_execution import record_output_consumption

    record_output_consumption(
        conn,
        project_id=published["project_id"],
        plan=_plan(published),
        result_id="qr_PANEL_EXAMPLE",
        query_spec_version_id="qsv_EXAMPLE",
    )

    payload = read_tab(
        conn,
        project_id=published["project_id"],
        datastream_id=published["datastream_id"],
        tab="outputs",
    )
    used_by = (payload.get("evidence") or {}).get("used_by") or []
    refs = {row.get("consumer_ref") for row in used_by}
    assert "qr_PANEL_EXAMPLE" in refs, (
        f"the Used by panel does not serve the consumption recorded: {used_by!r}"
    )
