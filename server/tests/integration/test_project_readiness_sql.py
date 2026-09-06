"""`compose_project_readiness` against a REAL schema.

WHY THIS FILE EXISTS. `compose_project_readiness` had **no test at all**. Only
`compose_project_readiness_from_evidence` -- the pure half, which takes evidence
ids as arguments and touches no database -- was covered. So the four SQL
statements that produce those ids were never executed by anything, and one of
them named a column that does not exist:

    SELECT execution_id FROM app.datastream_publication_log
    WHERE datastream_id=%s AND state='succeeded' ...

`state` is a column of `app.datastream_executions`. On the publication log it
raises `UndefinedColumn`, so **every** Getting Started read in production
answered 503 -- measured 2026-08-04 on `proj_01KYJ0NP...` from the Cloud Run log,
and only after a `logger.exception` was added to the route, because the handler
had been discarding the exception.

A green suite meant nothing here: the tested half cannot fail on a schema, and
the half that can was not called. This test calls it.

It is pg-gated, like every other schema contract in this directory. On a machine
with no `TEST_POSTGRES_DSN` it skips -- and a skip is not a pass, which is the
whole reason the repair was also verified by running the repaired statement
against production's own schema before shipping.
"""

from __future__ import annotations

import os
import uuid

import pytest
from ulid import ULID

requires_postgres = pytest.mark.skipif(
    not os.environ.get("TEST_POSTGRES_DSN"),
    reason="TEST_POSTGRES_DSN not set -- live Postgres schema test skipped",
)


def _id(prefix: str) -> str:
    return f"{prefix}{uuid.uuid4().hex[:12]}"


@requires_postgres
def test_readiness_reads_every_component_against_the_real_schema() -> None:
    """The four statements run, and the composed readiness is coherent.

    The assertion that matters is not the values -- it is that NONE of the four
    raises. A statement naming a column the table does not have fails here and
    nowhere else.
    """
    import psycopg
    from core.project_readiness import READINESS_COMPONENTS, compose_project_readiness

    dsn = os.environ["TEST_POSTGRES_DSN"]
    org_id, project_id = _id("org_"), _id("proj_")

    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(
                # `created_by` became NOT NULL after this fixture was written, so
                # both tests failed at their FIRST insert -- before a line of
                # `compose_project_readiness` ran. A schema contract that cannot
                # reach the code it contracts proves nothing.
                "INSERT INTO app.organizations (id, name, slug, created_by) "
                "VALUES (%s, %s, %s, %s)",
                (
                    org_id,
                    "Readiness probe",
                    org_id.replace("_", "-").lower(),
                    "owner@example.com",
                ),
            )
            cur.execute(
                "INSERT INTO app.projects (id, org_id, name, slug, created_by, status) "
                "VALUES (%s, %s, %s, %s, %s, 'active')",
                (
                    project_id,
                    org_id,
                    "Readiness probe",
                    project_id.replace("_", "-").lower(),
                    "owner@example.com",
                ),
            )

        readiness = compose_project_readiness(project_id, conn)

        assert readiness["schema_version"] == "project-readiness.v2"
        for component in READINESS_COMPONENTS:
            # No evidence was inserted, so every component is blocked. What is
            # being proven is that each one was ASKED, not what it answered.
            assert readiness[component]["state"] == "blocked"

        conn.rollback()


@requires_postgres
def test_a_published_datastream_makes_first_value_ready() -> None:
    """A row in the publication log IS the first value.

    `datastream_publication.py:1605` states the rule the repaired statement now
    follows: *"the log, pointer and outbox are written in one transaction, so
    the log is sufficient commit-evidence"*. No status predicate belongs here --
    the row's existence is the evidence.
    """
    import psycopg
    from core.project_readiness import compose_project_readiness

    dsn = os.environ["TEST_POSTGRES_DSN"]
    org_id, project_id = _id("org_"), _id("proj_")
    datastream_id, execution_id = _id("ds_"), f"dse_{ULID()}"
    plan_version_id, mapping_version_id = _id("plan_"), _id("map_")

    with psycopg.connect(dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(
                # `created_by` became NOT NULL after this fixture was written, so
                # both tests failed at their FIRST insert -- before a line of
                # `compose_project_readiness` ran. A schema contract that cannot
                # reach the code it contracts proves nothing.
                "INSERT INTO app.organizations (id, name, slug, created_by) "
                "VALUES (%s, %s, %s, %s)",
                (
                    org_id,
                    "Readiness probe",
                    org_id.replace("_", "-").lower(),
                    "owner@example.com",
                ),
            )
            cur.execute(
                "INSERT INTO app.projects (id, org_id, name, slug, created_by, status) "
                "VALUES (%s, %s, %s, %s, %s, 'active')",
                (
                    project_id,
                    org_id,
                    "Readiness probe",
                    project_id.replace("_", "-").lower(),
                    "owner@example.com",
                ),
            )
            cur.execute(
                "INSERT INTO app.datastreams (id, org_id, project_id, name, enabled) "
                "VALUES (%s, %s, %s, %s, TRUE)",
                (datastream_id, org_id, project_id, "Readiness probe stream"),
            )
            # THE WHOLE PUBLICATION CHAIN, because the schema requires it. Plan
            # version, mapping version and execution are the parents the log
            # points at; a fixture that skipped them stopped compiling the day
            # those foreign keys landed, and the test had been failing on its
            # first INSERT ever since -- never reaching the code it contracts.
            cur.execute(
                "INSERT INTO app.datastream_plan_versions "
                "(id, datastream_id, project_id, version_number, contract_version, "
                " source_kind, writer_kind, destination_policy, normalized_payload, "
                " content_hash, idempotency_key_hash, created_by) "
                "VALUES (%s, %s, %s, 1, 'v1', 'connector_pull', 'toorow', 'managed_raw', "
                " '{}'::jsonb, %s, %s, %s)",
                (
                    plan_version_id,
                    datastream_id,
                    project_id,
                    "0" * 64,
                    "1" * 64,
                    "owner@example.com",
                ),
            )
            cur.execute(
                "INSERT INTO app.datastream_mapping_versions "
                "(id, datastream_id, project_id, version_number, mapping_contract_version, "
                " source_schema_hash, plan_version_id, content_hash, ossie_spec_version, "
                " toorow_extension_version, mapping_payload, ossie_projection, "
                " idempotency_key_hash, created_by) "
                "VALUES (%s, %s, %s, 1, 'v1', %s, %s, %s, 'v1', 'v1', '{}'::jsonb, "
                " '{}'::jsonb, %s, %s)",
                (
                    mapping_version_id,
                    datastream_id,
                    project_id,
                    "2" * 64,
                    plan_version_id,
                    "3" * 64,
                    "4" * 64,
                    "owner@example.com",
                ),
            )
            # The log references a real execution. Inserting the parent row is
            # what makes the publication evidence in this fixture the same shape
            # production writes -- a dangling id would prove a different thing.
            cur.execute(
                "INSERT INTO app.datastream_executions "
                "(id, datastream_id, project_id, plan_version_id, mapping_version_id, "
                " created_by, state) VALUES (%s, %s, %s, %s, %s, %s, 'published')",
                (
                    execution_id,
                    datastream_id,
                    project_id,
                    plan_version_id,
                    mapping_version_id,
                    "owner@example.com",
                ),
            )
            cur.execute(
                "INSERT INTO app.datastream_publication_log "
                "(id, execution_id, datastream_id, project_id, plan_version_id, "
                " mapping_version_id, content_hash, row_count, published_by, published_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())",
                (
                    f"dplog_{ULID()}",
                    execution_id,
                    datastream_id,
                    project_id,
                    plan_version_id,
                    mapping_version_id,
                    "0" * 64,
                    1,
                    "owner@example.com",
                ),
            )

        readiness = compose_project_readiness(project_id, conn)

        assert readiness["datastream"]["state"] == "ready"
        assert readiness["first_value"]["state"] == "ready"
        assert readiness["first_value"]["evidence_ref"] == execution_id

        conn.rollback()
