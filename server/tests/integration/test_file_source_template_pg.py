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
import random
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
from tests.migration_ledger import apply_migrations_absent_from_the_ledger  # noqa: E402

MIGRATIONS = ROOT / "infra" / "nango" / "migrations"

# Numeric order = dependency order (FKs point at lower-numbered tables).
_CHAIN = [
    "018_projects.sql",
    "023_datastreams.sql",
    "032_datastream_field_mappings.sql",
    "035_organizations.sql",
    "060_operation_audit_outbox.sql",
    "097_file_source_templates.sql",
    # 208 : l'echappatoire de nettoyage d'identifiant. Elle vient APRES la 097
    # parce qu'elle remplace sa fonction de trigger -- l'ordre est le contrat.
    "208_a_client_name_must_be_able_to_leave_an_append_only_row.sql",
]

requires_postgres = pytest.mark.skipif(
    not os.environ.get("TEST_POSTGRES_DSN"),
    reason="TEST_POSTGRES_DSN not set -- live Postgres constraint test skipped",
)

# Valid Crockford-base32 canonical field ids (^mdm_[0-9A-HJKMNP-TV-Z]{26}$).
#
# UNIQUE PER RUN, AND THAT IS THE POINT. These four were fixed constants, while
# `_seed` inserts them `ON CONFLICT (id) DO NOTHING` and every test builds a NEW
# `project_id`. So the FIRST run of this file ever executed bound the four ids to
# a project that no longer exists, `DO NOTHING` kept that binding, and
# `_assert_required_fields_registered` -- which asks for `project_id = %s OR
# project_id IS NULL` -- refused them for every run afterwards. Measured on the
# shared base: the four rows still carry `proj_e67d2fc6e754`, and six of the
# eight tests here died on `UnknownCanonicalField` for that reason alone. A
# fixture whose second run cannot pass is a criterion nobody can measure, which
# is exactly the state `file-source-ingestion[20]` was in.
_CROCKFORD_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def _field_id() -> str:
    return "mdm_" + "".join(random.choice(_CROCKFORD_ALPHABET) for _ in range(26))


FIELD_COST = _field_id()
FIELD_DATE = _field_id()
FIELD_IMPR = _field_id()
FIELD_CHAN = _field_id()


def _id(prefix: str) -> str:
    return f"{prefix}{uuid.uuid4().hex[:12]}"


def _apply_chain(conn) -> None:
    """Ne rejouer que ce que le ledger ne porte pas -- voir `tests.migration_ledger`.

    Deux raisons mesurees, pas une. `035_organizations.sql` echouait ici sur
    `app.project_members` contre une base deja migree ; et `032` comme `097` sont
    ANTERIEURES a la `099`/`264`, donc les rejouer recree
    `trg_datastream_mapping_versions_immutable` et
    `trg_file_source_template_immutable` SANS la clause `rgpd_erasure`.
    """
    apply_migrations_absent_from_the_ledger(conn, [MIGRATIONS / name for name in _CHAIN])


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
        # `value_type` is NOT NULL since migration 241 and has no default: each
        # field states the type its role implies.
        for fid, kind, name, agg, value_type in (
            (FIELD_COST, "metric", "net_cost", "sum", "money"),
            (FIELD_IMPR, "metric", "impressions", "sum", "integer"),
            (FIELD_DATE, "dimension", "media_date", None, "date"),
            (FIELD_CHAN, "dimension", "channel", None, "string"),
        ):
            # DO UPDATE, not DO NOTHING, and the difference is the whole file.
            # An id is globally unique while every test here seeds a NEW project,
            # so the second `_seed` of a run found the four ids already bound to
            # the FIRST test's project. `DO NOTHING` kept that binding, and
            # `_assert_required_fields_registered` -- `project_id = %s OR
            # project_id IS NULL` -- then refused them. Six of the eight tests
            # died there, and re-running the file could not repair it, because
            # the stale rows outlive the run. Re-pointing the row at the project
            # asking for it is what the fixture always meant.
            cur.execute(
                "INSERT INTO app.mdm_canonical_fields "
                "(id, project_id, concept_kind, canonical_name, aggregation, "
                " value_type, created_by) "
                "VALUES (%s, %s, %s, %s, %s, %s, 'test') "
                "ON CONFLICT (id) DO UPDATE SET "
                "  project_id = EXCLUDED.project_id, "
                "  canonical_name = EXCLUDED.canonical_name, "
                "  status = 'active'",
                (fid, project_id, kind, f"{name}_{project_id[-6:]}", agg, value_type),
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
        conn, project_id=project_id, org_id=org_id, template_code="EXAMPLE_PLAN",
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
        conn, project_id=project_id, org_id=org_id, template_code="EXAMPLE_PLAN",
        contract=_contract(), created_by="tester",
    )
    conn.commit()
    again = create_file_source_template(
        conn, project_id=project_id, org_id=org_id, template_code="EXAMPLE_PLAN",
        contract=_contract(), created_by="tester",
    )
    conn.commit()
    assert again["id"] == first["id"]
    assert again["version"] == 1
    versions = list_file_source_template_versions(
        conn, project_id=project_id, template_code="EXAMPLE_PLAN"
    )
    assert len(versions) == 1  # no duplicate version row


@requires_postgres
def test_different_contract_appends_new_version(conn):
    org_id, project_id = _seed(conn)
    v1 = create_file_source_template(
        conn, project_id=project_id, org_id=org_id, template_code="EXAMPLE_PLAN",
        contract=_contract(), created_by="tester",
    )
    conn.commit()
    v2 = create_file_source_template(
        conn, project_id=project_id, org_id=org_id, template_code="EXAMPLE_PLAN",
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
        conn, project_id=project_id, org_id=org_id, template_code="EXAMPLE_PLAN",
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
        conn, project_id=project_id, template_code="EXAMPLE_PLAN", version=1
    )
    assert fresh["label"] == "annotated"
    assert fresh["is_active"] is False


@requires_postgres
def test_unknown_canonical_field_fails_closed(conn):
    org_id, project_id = _seed(conn)
    bad = _contract(required_fields=[FIELD_COST, "mdm_ZZZZZZZZZZZZZZZZZZZZZZZZZZ"])
    with pytest.raises(UnknownCanonicalField):
        create_file_source_template(
            conn, project_id=project_id, org_id=org_id, template_code="EXAMPLE_PLAN",
            contract=bad, created_by="tester",
        )
    conn.rollback()
    # Nothing was written.
    assert list_file_source_template_versions(
        conn, project_id=project_id, template_code="EXAMPLE_PLAN"
    ) == []


# ---------------------------------------------------------------------------
# Migration 208 -- l'echappatoire de nettoyage d'identifiant
# ---------------------------------------------------------------------------
#
# POURQUOI ELLE EXISTE. Mesure du 2026-08-04 en production : la table portait UNE
# ligne, ecrite par la fixture de ce fichier meme (`created_by='tester'`), et son
# `template_code` etait le nom d'un CLIENT REEL. Elle ne pouvait plus partir : la
# 097 refuse le DELETE et gele `template_code`, et la 099 exclut cette table de
# l'echappatoire `app.rgpd_erasure` -- deliberement, parce que ce n'est pas une
# donnee de tenant.
#
# La 208 ouvre donc une porte etroite : changer l'IDENTIFIANT, jamais l'histoire.
# Ces trois tests sont la pour qu'elle reste etroite. Une echappatoire dont
# personne ne verifie les bords devient l'echappatoire de tout.


@requires_postgres
def test_the_scrub_hatch_renames_the_identifier_and_keeps_the_row(conn):
    org_id, project_id = _seed(conn)
    row = create_file_source_template(
        conn, project_id=project_id, org_id=org_id, template_code="CLIENT_NAME_HERE",
        contract=_contract(), created_by="tester",
    )
    conn.commit()

    with conn.cursor() as cur:
        cur.execute("SET LOCAL app.identifier_scrub = 'on'")
        cur.execute(
            """
            UPDATE app.file_source_templates
               SET template_code = 'EXAMPLE_PLAN',
                   idempotency_key_hash = encode(sha256(convert_to(
                       'file_source_template:' || project_id || ':EXAMPLE_PLAN:' || content_hash,
                       'UTF8')), 'hex')
             WHERE id = %s
            """,
            (row["id"],),
        )
        cur.execute(
            "SELECT template_code, created_by FROM app.file_source_templates WHERE id = %s",
            (row["id"],),
        )
        code, created_by = cur.fetchone()
    conn.commit()

    assert code == "EXAMPLE_PLAN"
    # La LIGNE survit -- c'est le nom qui part, pas la version de template.
    assert created_by == "tester"


@requires_postgres
def test_the_identifier_stays_frozen_without_the_hatch(conn):
    org_id, project_id = _seed(conn)
    row = create_file_source_template(
        conn, project_id=project_id, org_id=org_id, template_code="EXAMPLE_PLAN",
        contract=_contract(), created_by="tester",
    )
    conn.commit()
    with pytest.raises(Exception, match="immutable"):
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE app.file_source_templates SET template_code = %s WHERE id = %s",
                ("SNEAKY", row["id"]),
            )
    conn.rollback()


@requires_postgres
def test_the_hatch_does_not_unfreeze_history_nor_allow_a_delete(conn):
    """Le point qui fait qu'une echappatoire reste une echappatoire.

    Sous `app.identifier_scrub`, `created_by`, `contract`, `content_hash` et le
    DELETE restent refuses. Un nettoyage d'identifiant qui pourrait reecrire
    l'auteur ou effacer la ligne ne serait plus un nettoyage : ce serait la
    reecriture d'histoire que la 097 existe pour empecher.
    """
    org_id, project_id = _seed(conn)
    row = create_file_source_template(
        conn, project_id=project_id, org_id=org_id, template_code="EXAMPLE_PLAN",
        contract=_contract(), created_by="tester",
    )
    conn.commit()

    for column, value in (("created_by", "someone_else"), ("content_hash", "f" * 64)):
        with pytest.raises(Exception, match="immutable"):
            with conn.cursor() as cur:
                cur.execute("SET LOCAL app.identifier_scrub = 'on'")
                cur.execute(
                    f"UPDATE app.file_source_templates SET {column} = %s WHERE id = %s",
                    (value, row["id"]),
                )
        conn.rollback()

    with pytest.raises(Exception, match="append-only"):
        with conn.cursor() as cur:
            cur.execute("SET LOCAL app.identifier_scrub = 'on'")
            cur.execute("DELETE FROM app.file_source_templates WHERE id = %s", (row["id"],))
    conn.rollback()
