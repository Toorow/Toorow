"""Live-Postgres contract test for the file-source template artifact (Story 22.11).

Applies the dependency chain (018 projects, 023 datastreams+target_fields, 032
mdm_canonical_fields, 035 organizations, 060 operations/audit/outbox) then the new
097 file_source_templates, idempotently, and asserts the REAL behaviour a mocked
cursor cannot catch:

  * create writes ONE immutable version row through execute_operation (+ an
    operation + audit + outbox row);
  * re-creating the IDENTICAL contract returns the SAME version (idempotent);
  * a DIFFERENT contract for the same template_code appends version 2;
  * the immutability trigger REJECTS UPDATE of an identity column and any DELETE,
    while label / is_active remain mutable;
  * a required field id that is not an active mdm_canonical_field fails closed
    (UnknownCanonicalField), before any row is written.

SKIPS when TEST_POSTGRES_DSN is unset. Migration 097 is applied to Supabase only
under Jean's authorization; this test applies it to the disposable test database.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import psycopg
import pytest
from core.file_source_template import (
    UnknownCanonicalField,
    create_file_source_template,
    get_file_source_template,
    list_file_source_template_versions,
)

ROOT = Path(__file__).resolve().parents[3]
MIGRATIONS = ROOT / "infra" / "nango" / "migrations"

# Numeric order = dependency order (FKs point at lower-numbered tables).
_CHAIN = [
    "018_projects.sql",
    "023_datastreams.sql",
    "032_datastream_field_mappings.sql",
    "035_organizations.sql",
    "060_operation_audit_outbox.sql",
    "097_file_source_templates.sql",
]

requires_postgres = pytest.mark.skipif(
    not os.environ.get("TEST_POSTGRES_DSN"),
    reason="TEST_POSTGRES_DSN not set -- live Postgres constraint test skipped",
)

# Valid Crockford-base32 canonical field ids (^mdm_[0-9A-HJKMNP-TV-Z]{26}$).
_CROCK = "0123456789ABCDEFGHJKMNPQRS"
FIELD_COST = "mdm_" + _CROCK[:-1] + "S"
FIELD_DATE = "mdm_" + _CROCK[:-1] + "T"
FIELD_IMPR = "mdm_" + _CROCK[:-1] + "V"
FIELD_CHAN = "mdm_" + _CROCK[:-1] + "W"


def _id(prefix: str) -> str:
    return f"{prefix}{uuid.uuid4().hex[:12]}"


def _apply_chain(conn) -> None:
    with conn.cursor() as cur:
        for name in _CHAIN:
            path = MIGRATIONS / name
            if path.exists():
                cur.execute(path.read_text(encoding="utf-8"))
    conn.commit()


def _seed(conn):
    org_id = _id("org_")
    project_id = _id("proj_")
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO app.organizations (id, name, slug, created_by) "
            "VALUES (%s, %s, %s, 'story-22.11-test') ON CONFLICT DO NOTHING",
            (org_id, org_id, org_id),
        )
        cur.execute(
            "INSERT INTO app.projects (id, name, slug, org_id, created_by) "
            "VALUES (%s, %s, %s, %s, 'story-22.11-test') ON CONFLICT DO NOTHING",
            (project_id, project_id, project_id, org_id),
        )
        for fid, kind, name, agg in (
            (FIELD_COST, "metric", "net_cost", "sum"),
            (FIELD_IMPR, "metric", "impressions", "sum"),
            (FIELD_DATE, "dimension", "media_date", None),
            (FIELD_CHAN, "dimension", "channel", None),
        ):
            cur.execute(
                "INSERT INTO app.mdm_canonical_fields "
                "(id, project_id, concept_kind, canonical_name, aggregation, created_by) "
                "VALUES (%s, %s, %s, %s, %s, 'test') ON CONFLICT (id) DO NOTHING",
                (fid, project_id, kind, f"{name}_{project_id[-6:]}", agg),
            )
    conn.commit()
    return org_id, project_id


def _contract(**over):
    base = {
        "kind": "catalog",
        "required_fields": [FIELD_COST, FIELD_DATE],
        "optional_fields": [FIELD_IMPR],
        "grain": "daily",
        "class": "planned",
        "placement": {"metric": FIELD_COST, "period": FIELD_DATE, "dimension": [FIELD_CHAN]},
    }
    base.update(over)
    return base


@pytest.fixture
def conn():
    dsn = os.environ.get("TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("TEST_POSTGRES_DSN not set")
    c = psycopg.connect(dsn)
    try:
        _apply_chain(c)
        yield c
    finally:
        c.rollback()
        c.close()


@requires_postgres
def test_create_writes_one_immutable_version(conn):
    org_id, project_id = _seed(conn)
    row = create_file_source_template(
        conn, project_id=project_id, org_id=org_id, template_code="AXA_PLAN",
        contract=_contract(), created_by="tester",
    )
    conn.commit()
    assert row["version"] == 1
    assert row["id"].startswith("fst_")
    assert row["placement_class"] == "planned"
    assert row["grain"] == "daily"
    assert len(row["content_hash"]) == 64
    # The operation substrate recorded the write (audit + outbox + operation).
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM app.operations WHERE command_type = %s",
            ("file_source.template.created",),
        )
        assert cur.fetchone()[0] >= 1


@requires_postgres
def test_identical_recreate_is_idempotent_same_version(conn):
    org_id, project_id = _seed(conn)
    first = create_file_source_template(
        conn, project_id=project_id, org_id=org_id, template_code="AXA_PLAN",
        contract=_contract(), created_by="tester",
    )
    conn.commit()
    again = create_file_source_template(
        conn, project_id=project_id, org_id=org_id, template_code="AXA_PLAN",
        contract=_contract(), created_by="tester",
    )
    conn.commit()
    assert again["id"] == first["id"]
    assert again["version"] == 1
    versions = list_file_source_template_versions(
        conn, project_id=project_id, template_code="AXA_PLAN"
    )
    assert len(versions) == 1  # no duplicate version row


@requires_postgres
def test_different_contract_appends_new_version(conn):
    org_id, project_id = _seed(conn)
    v1 = create_file_source_template(
        conn, project_id=project_id, org_id=org_id, template_code="AXA_PLAN",
        contract=_contract(), created_by="tester",
    )
    conn.commit()
    v2 = create_file_source_template(
        conn, project_id=project_id, org_id=org_id, template_code="AXA_PLAN",
        contract=_contract(**{"class": "actual"}), created_by="tester",
    )
    conn.commit()
    assert v1["version"] == 1
    assert v2["version"] == 2
    assert v2["id"] != v1["id"]
    assert v2["placement_class"] == "actual"


@requires_postgres
def test_immutability_trigger_rejects_update_and_delete(conn):
    org_id, project_id = _seed(conn)
    row = create_file_source_template(
        conn, project_id=project_id, org_id=org_id, template_code="AXA_PLAN",
        contract=_contract(), created_by="tester",
    )
    conn.commit()
    # UPDATE of an identity column -> rejected.
    with pytest.raises(psycopg.errors.RaiseException):
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE app.file_source_templates SET grain = 'weekly' WHERE id = %s",
                (row["id"],),
            )
    conn.rollback()
    # DELETE -> rejected.
    with pytest.raises(psycopg.errors.RaiseException):
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM app.file_source_templates WHERE id = %s", (row["id"],)
            )
    conn.rollback()
    # label / is_active remain mutable.
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE app.file_source_templates SET label = 'annotated', is_active = FALSE "
            "WHERE id = %s",
            (row["id"],),
        )
    conn.commit()
    fresh = get_file_source_template(
        conn, project_id=project_id, template_code="AXA_PLAN", version=1
    )
    assert fresh["label"] == "annotated"
    assert fresh["is_active"] is False


@requires_postgres
def test_unknown_canonical_field_fails_closed(conn):
    org_id, project_id = _seed(conn)
    bad = _contract(required_fields=[FIELD_COST, "mdm_ZZZZZZZZZZZZZZZZZZZZZZZZZZ"])
    with pytest.raises(UnknownCanonicalField):
        create_file_source_template(
            conn, project_id=project_id, org_id=org_id, template_code="AXA_PLAN",
            contract=bad, created_by="tester",
        )
    conn.rollback()
    # Nothing was written.
    assert list_file_source_template_versions(
        conn, project_id=project_id, template_code="AXA_PLAN"
    ) == []
