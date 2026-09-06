"""Story 49.5 — the Evidence index properties that only a real PostgreSQL holds.

An immutability trigger, a partial unique index, a CHECK constraint and a
fail-closed policy are database objects. A mocked cursor proves nothing about
any of them, which is why this file is gated on `TEST_POSTGRES_DSN` and why the
repository's own gate — which FAILS rather than skips when pointed at anything
that is not demonstrably disposable — is the one used here rather than a second
copy of it.

What each block guards, in one line:

* an Evidence Record refuses UPDATE, DELETE and TRUNCATE;
* one source identity indexes once, and a second insert collides;
* one anchor per (Project, producer, owner object, version);
* a link points at exactly one destination, and never at itself;
* an all-zero or non-hex W3C trace id is unstorable;
* retention is a NEW append-only fact, never an edit of the fact it qualifies;
* a quarantine row structurally cannot hold a payload.
"""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
MIGRATION = ROOT / "infra" / "nango" / "migrations" / "149_evidence_reference_index.sql"


# ---------------------------------------------------------------------------
# Structural: what the migration declares, readable without a database.
# ---------------------------------------------------------------------------


def _declarations() -> str:
    """The migration with its prose removed.

    The comments in 149 explain at length what the index must NOT hold, so
    asserting against the raw file would only prove the explanation exists. What
    matters is the DDL.
    """
    return "\n".join(
        line
        for line in MIGRATION.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("--")
    )


def test_migration_declares_the_five_index_tables_and_no_generic_event_store():
    sql = _declarations()
    for table in (
        "app.evidence_records",
        "app.evidence_correlations",
        "app.evidence_links",
        "app.evidence_availability_events",
        "app.evidence_index_watermarks",
        "app.evidence_projection_quarantine",
    ):
        assert f"CREATE TABLE IF NOT EXISTS {table} (" in sql, table
    # One arbitrary JSON event table is exactly what the contract forbids.
    assert "payload JSONB" not in sql
    assert "metadata JSONB" not in sql
    assert "event_body" not in sql


def test_the_index_stores_no_owner_content_and_no_display_label():
    ddl = _declarations()
    # A cached label drifts the moment its owner renames, so there is no column
    # for one. `owner_reference` is built at read time and never persisted.
    assert "label TEXT" not in ddl
    assert "display_name" not in ddl
    assert "raw_" not in ddl
    assert "sample" not in ddl
    assert "url" not in ddl.lower()


def test_immutability_and_truncate_blocks_cover_every_evidence_table():
    sql = _declarations()
    for table in ("records", "correlations", "links"):
        assert f"trg_evidence_{table}_immutable" in sql, table
        assert f"trg_evidence_{table}_block_truncate" in sql, table
    assert "trg_evidence_availability_append_only" in sql
    assert "trg_evidence_availability_block_truncate" in sql


def test_rls_is_enabled_forced_and_fail_closed_on_all_four_index_tables():
    sql = _declarations()
    for table in (
        "app.evidence_records",
        "app.evidence_correlations",
        "app.evidence_links",
        "app.evidence_availability_events",
    ):
        assert f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY;" in sql, table
        assert f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY;" in sql, table
    assert sql.count("epic36_has_resource_access(org_id, 'project', project_id)") >= 8


def test_the_rgpd_erasure_hatch_survives_the_new_immutability_triggers():
    """Every guard carries the documented escape hatch (migrations 098/099).

    Without it an organization could no longer be erased, and the failure would
    only appear the day someone tried.
    """
    sql = _declarations()
    assert sql.count("current_setting('app.rgpd_erasure', true) IS DISTINCT FROM 'on'") >= 4


def test_the_quarantine_table_structurally_cannot_hold_a_payload():
    sql = _declarations()
    block = sql.split("app.evidence_projection_quarantine (")[1].split(");")[0]
    assert "error_class" in block
    assert "JSONB" not in block
    assert "payload" not in block


def test_the_migration_is_registered_in_the_manifest():
    import json

    manifest = json.loads(
        (ROOT / "infra" / "nango" / "migrations" / "manifest.json").read_text(encoding="utf-8")
    )
    entries = {entry["identifier"]: entry["filename"] for entry in manifest["migrations"]}
    assert entries["149"] == "149_evidence_reference_index.sql"


# ---------------------------------------------------------------------------
# Live: the properties that need the database itself.
# ---------------------------------------------------------------------------


@pytest.fixture()
def conn(live_postgres):
    """The repository's own gated connection, not a second one.

    `server/tests/conftest.py` already refuses a DSN that is not demonstrably
    disposable and FAILS rather than skips when a writing test is pointed at a
    real database. Re-implementing that guard here would be a second copy of the
    one rule this repository has already been burned by getting wrong.
    """
    return live_postgres


@pytest.fixture()
def scope(conn):
    """An organization and a Project that exist only for this test."""
    from ulid import ULID

    org_id = f"org_{ULID()}"
    project_id = f"proj_{ULID()}"
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO app.organizations (id, name, slug, status, created_by) "
            "VALUES (%s, %s, %s, 'active', %s)",
            (org_id, "Evidence index test", org_id.lower(), "test@example.com"),
        )
        cur.execute(
            "INSERT INTO app.projects (id, org_id, name, slug, status, created_by) "
            "VALUES (%s, %s, %s, %s, 'active', %s)",
            (project_id, org_id, "Evidence index test", project_id.lower(), "test@example.com"),
        )
    return org_id, project_id


def _insert_record(conn, org_id: str, project_id: str, **overrides) -> str:
    from ulid import ULID

    record_id = overrides.pop("id", f"evr_{ULID()}")
    values = {
        "id": record_id,
        "org_id": org_id,
        "project_id": project_id,
        "record_kind": "evidence_trace",
        "producer": "data_execution",
        "owner_workspace": "data",
        "owner_object_type": "datastream-execution",
        "owner_object_id": "dse_1",
        "owner_version_id": None,
        "is_anchor": True,
        "occurred_at": "2026-07-30T09:00:00Z",
        "observed_at": None,
        "source_identity_key": f"data_execution|{record_id}",
        "source_payload_hash": "a" * 64,
        "integrity_hash": None,
        "redaction_class": "reference_only",
    }
    values.update(overrides)
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.evidence_records (
                id, org_id, project_id, record_kind, producer, owner_workspace,
                owner_object_type, owner_object_id, owner_version_id, is_anchor,
                occurred_at, observed_at, source_identity_key, source_payload_hash,
                integrity_hash, redaction_class
            ) VALUES (
                %(id)s, %(org_id)s, %(project_id)s, %(record_kind)s, %(producer)s,
                %(owner_workspace)s, %(owner_object_type)s, %(owner_object_id)s,
                %(owner_version_id)s, %(is_anchor)s, %(occurred_at)s, %(observed_at)s,
                %(source_identity_key)s, %(source_payload_hash)s, %(integrity_hash)s,
                %(redaction_class)s
            )
            """,
            values,
        )
    return record_id


def test_an_indexed_record_refuses_update_and_delete(conn, scope):
    import psycopg

    org_id, project_id = scope
    record_id = _insert_record(conn, org_id, project_id)

    with pytest.raises(psycopg.errors.RaiseException), conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE app.evidence_records SET occurred_at = NOW() WHERE id = %s", (record_id,)
            )
    with pytest.raises(psycopg.errors.RaiseException), conn.transaction():
        with conn.cursor() as cur:
            cur.execute("DELETE FROM app.evidence_records WHERE id = %s", (record_id,))
    conn.rollback()


def test_the_same_source_identity_indexes_once(conn, scope):
    import psycopg

    org_id, project_id = scope
    _insert_record(conn, org_id, project_id, source_identity_key="data_execution|dse_7")
    with pytest.raises(psycopg.errors.UniqueViolation), conn.transaction():
        _insert_record(conn, org_id, project_id, source_identity_key="data_execution|dse_7")
    conn.rollback()


def test_one_anchor_per_producer_owner_and_version(conn, scope):
    import psycopg

    org_id, project_id = scope
    _insert_record(conn, org_id, project_id, owner_object_id="dse_9")
    with pytest.raises(psycopg.errors.UniqueViolation):
        # A different source identity claiming the SAME anchor identity would
        # give one owner artifact two traces.
        _insert_record(conn, org_id, project_id, owner_object_id="dse_9")
    conn.rollback()


def test_only_a_trace_record_may_anchor(conn, scope):
    import psycopg

    org_id, project_id = scope
    with pytest.raises(psycopg.errors.CheckViolation), conn.transaction():
        _insert_record(conn, org_id, project_id, record_kind="object_version", is_anchor=True)
    conn.rollback()


def test_a_link_needs_exactly_one_destination_and_is_never_its_own(conn, scope):
    import psycopg
    from ulid import ULID

    org_id, project_id = scope
    record_id = _insert_record(conn, org_id, project_id)

    with pytest.raises(psycopg.errors.CheckViolation), conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO app.evidence_links
                    (id, org_id, project_id, from_record_id, relation)
                VALUES (%s, %s, %s, %s, 'derives_from')
                """,
                (f"evl_{ULID()}", org_id, project_id, record_id),
            )
    with pytest.raises(psycopg.errors.CheckViolation), conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO app.evidence_links
                    (id, org_id, project_id, from_record_id, relation, to_record_id)
                VALUES (%s, %s, %s, %s, 'derives_from', %s)
                """,
                (f"evl_{ULID()}", org_id, project_id, record_id, record_id),
            )
    conn.rollback()


def test_an_invalid_w3c_trace_id_is_unstorable(conn, scope):
    import psycopg
    from ulid import ULID

    org_id, project_id = scope
    record_id = _insert_record(conn, org_id, project_id)

    for invalid in ("0" * 32, "A" * 32, "abc"):
        with pytest.raises(psycopg.errors.CheckViolation), conn.transaction():
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO app.evidence_correlations
                        (id, org_id, project_id, record_id, correlation_kind, correlation_id)
                    VALUES (%s, %s, %s, %s, 'w3c_trace', %s)
                    """,
                    (f"evc_{ULID()}", org_id, project_id, record_id, invalid),
                )
        conn.rollback()


def test_retention_appends_a_fact_and_never_edits_the_one_it_qualifies(conn, scope):
    import psycopg
    from core.evidence_index import record_availability
    from ulid import ULID

    org_id, project_id = scope
    record_id = _insert_record(conn, org_id, project_id)

    event_id = record_availability(
        conn,
        org_id=org_id,
        project_id=project_id,
        record_id=record_id,
        availability="retained_away",
        reason_code="owner_retention_window",
        actor="test@example.com",
    )
    assert event_id.startswith("eva_")

    with conn.cursor() as cur:
        cur.execute("SELECT occurred_at FROM app.evidence_records WHERE id = %s", (record_id,))
        assert cur.fetchone() is not None  # untouched, and still readable

    with pytest.raises(psycopg.errors.RaiseException), conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE app.evidence_availability_events"
                " SET availability = 'available' WHERE id = %s",
                (event_id,),
            )
    # A tombstone carries a reason CODE and nowhere to put owner content.
    with pytest.raises(psycopg.errors.CheckViolation), conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO app.evidence_availability_events
                    (id, org_id, project_id, record_id, availability, reason_code, created_by)
                VALUES (%s, %s, %s, %s, 'retained_away', 'Not A Code', 'test@example.com')
                """,
                (f"eva_{ULID()}", org_id, project_id, record_id),
            )
    conn.rollback()


def test_a_correlation_or_link_cannot_bridge_two_tenants(conn, scope):
    """The composite foreign key is the guard, not a convention.

    A child row claiming another organization's scope fails at insert rather
    than becoming an edge that crosses a tenant boundary.
    """
    import psycopg
    from ulid import ULID

    org_id, project_id = scope
    record_id = _insert_record(conn, org_id, project_id)

    with pytest.raises(psycopg.errors.ForeignKeyViolation), conn.transaction():
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO app.evidence_correlations
                    (id, org_id, project_id, record_id, correlation_kind, correlation_id)
                VALUES (%s, %s, %s, %s, 'operation', 'op_1')
                """,
                (f"evc_{ULID()}", "org_SOMEONE_ELSE", project_id, record_id),
            )
    conn.rollback()
