"""Story 49.2 — the one Master Data authority, read out of the migration itself.

Migration 143 extends Story 48.2's generic core rather than replacing it. These
tests pin the four decisions that are easy to undo by accident, and one defect
that was already live:

* a registry DECLARES whether it versions as a whole (Country) or per identity
  (Products) — collapsing that back into one behaviour breaks one of the two;
* an organization-scoped row has no Project, and the link to its registry is
  still enforced — a composite foreign key stops being checked the moment one
  of its columns is NULL, which is how the scope change could have opened a hole;
* an object type is immutable, because an object version pins one;
* an alias carries an explicit SKOS relation, so "the same thing" and "related"
  cannot be confused;
* the four guarded Master Data tables 140 introduced yield to an RGPD erasure.

The migration text is the subject under test on purpose. The live invariants
were separately proven against the real database with an auto-rollback probe
(recorded in the story's Dev Agent Record); what a repository test can hold in
place afterwards is that the SQL still says what it said.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
MIGRATIONS = REPO_ROOT / "infra" / "nango" / "migrations"
MIGRATION = MIGRATIONS / "143_master_data_one_authority.sql"
GENERIC_CORE = MIGRATIONS / "140_master_data_registries.sql"


@pytest.fixture(scope="module")
def sql() -> str:
    return MIGRATION.read_text(encoding="utf-8")


def statements_only(text: str) -> str:
    """The SQL with its prose removed.

    An assertion that a word is ABSENT has to read the statements, not the file:
    this migration explains at length why the core must not know the word
    `competitor`, and a naive scan finds it in that very explanation.
    """
    return "\n".join(re.sub(r"--.*$", "", line) for line in text.splitlines())


def test_the_registry_declares_its_version_scope_and_keeps_140_as_the_default(sql: str):
    assert "ADD COLUMN IF NOT EXISTS version_scope TEXT NOT NULL DEFAULT 'registry'" in sql
    assert "CHECK (version_scope IN ('registry', 'node'))" in sql
    # Defaulting to 'registry' is what leaves Country untouched: it is 140's
    # behaviour, and this migration must not silently re-shape a live grouping.
    assert "DEFAULT 'node'" not in sql


def test_current_and_numbered_versions_are_constrained_at_both_scopes(sql: str):
    # 140's single rule cannot survive node scope: every Product would compete
    # for one `current` slot. It is replaced by one rule per scope, not removed.
    assert "DROP INDEX IF EXISTS app.uq_master_data_versions_single_current" in sql
    for index, predicate in [
        ("uq_master_data_versions_current_registry", "status = 'current' AND node_id IS NULL"),
        ("uq_master_data_versions_current_node", "status = 'current' AND node_id IS NOT NULL"),
        ("uq_master_data_versions_number_registry", "node_id IS NULL"),
        ("uq_master_data_versions_number_node", "node_id IS NOT NULL"),
    ]:
        assert index in sql, index
        assert predicate in sql, predicate


def test_the_dropped_unique_constraint_is_found_by_its_columns_not_by_a_guessed_name(sql: str):
    # Postgres truncates generated constraint names at 63 characters and this
    # one is over; naming it literally would have silently dropped nothing.
    assert "master_data_object_versions_project_id_registry_id_version_key" not in sql
    assert "con.contype = 'u'" in sql
    assert "ARRAY['project_id', 'registry_id', 'version_number']" in sql
    # `attname` is `name`, not `text`; the cast is what makes the comparison run.
    assert "att.attname::text" in sql


def test_organization_scope_exists_and_a_project_scope_still_requires_a_project(sql: str):
    assert "CHECK (scope IN ('platform', 'organization', 'project'))" in sql
    assert "CHECK ((scope = 'project') = (project_id IS NOT NULL))" in sql
    for table in ("master_data_registries", "master_data_nodes", "master_data_object_versions"):
        assert f"ALTER TABLE app.{table} ALTER COLUMN project_id DROP NOT NULL" in sql
    # A consumer always lives in one Project, whatever the scope of what it uses.
    assert "ALTER TABLE app.master_data_used_by ALTER COLUMN project_id DROP NOT NULL" not in sql


def test_every_composite_link_weakened_by_a_null_project_gains_a_single_column_key():
    """The MATCH SIMPLE hole, stated as a property rather than a list.

    140 links its rows through `(project_id, <target>)`. A composite foreign key
    is MATCH SIMPLE: with `project_id` NULL the whole key stops being checked.
    Making project_id nullable therefore has to be paid for, on every one of
    those links, with a key that does not depend on it.
    """
    core = GENERIC_CORE.read_text(encoding="utf-8")
    sql = MIGRATION.read_text(encoding="utf-8")

    relaxed = set(re.findall(r"ALTER TABLE app\.(\w+) ALTER COLUMN project_id DROP NOT NULL", sql))
    assert relaxed == {
        "master_data_registries",
        "master_data_nodes",
        "master_data_object_versions",
        "master_data_memberships",
    }

    # Every composite FK declared in 140 whose owner just lost its NOT NULL.
    composite = re.findall(r"FOREIGN KEY \(project_id, (\w+)\)", core)
    assert set(composite) >= {"registry_id", "parent_node_id", "child_node_id", "version_id"}

    for column in {"registry_id", "node_id", "parent_node_id", "child_node_id", "version_id"}:
        assert f"FOREIGN KEY ({column}) REFERENCES app." in sql, column


def test_object_types_are_extensible_and_immutable(sql: str):
    assert "CREATE TABLE IF NOT EXISTS app.master_data_type_versions" in sql
    assert "property_schema JSONB NOT NULL" in sql
    assert "relationship_definitions JSONB NOT NULL" in sql
    # A client type is data, never an enum or a navigation change.
    assert "CHECK (object_kind ~ '^[a-z][a-z0-9_]{1,39}$')" in sql
    assert "trg_master_data_types_immutable" in sql
    assert "master data type versions are immutable" in sql
    # An object version pins the schema it validated against, so a historical
    # read resolves the type of its own time.
    assert "ADD COLUMN IF NOT EXISTS type_version_id TEXT" in sql


def test_an_alias_must_say_which_kind_of_claim_it_is_making(sql: str):
    assert "CREATE TABLE IF NOT EXISTS app.master_data_aliases" in sql
    for relation in ("exact", "close", "broader", "narrower", "related", "negative"):
        assert f"'{relation}'" in sql, relation
    assert "relation TEXT NOT NULL" in sql
    # Only `exact` claims sameness, so only `exact` may collide.
    assert "uq_master_data_aliases_live_exact" in sql
    assert "WHERE relation = 'exact' AND retired_at IS NULL AND effective_to IS NULL" in sql
    # A contradiction is recorded, never resolved by write order.
    assert "conflict_state" in sql
    assert "CHECK (conflict_state IN ('none', 'collision', 'contradiction'))" in sql


def test_an_organization_object_is_reused_by_a_project_rather_than_copied(sql: str):
    assert "CREATE TABLE IF NOT EXISTS app.master_data_project_associations" in sql
    assert "project_role TEXT NOT NULL" in sql
    # Opaque to the core: 'competitor' and 'own_brand' are the Competitor
    # capability's roles, and this table must not know them.
    ddl = statements_only(sql)
    assert "'competitor'" not in ddl
    assert "'own_brand'" not in ddl
    assert "uq_master_data_association_live" in sql


def test_story_45_1_identities_are_preserved_and_no_row_is_guessed(sql: str):
    assert "CHECK (id ~ '^(mdnode|bd|bcl)_[0-9A-HJKMNP-TV-Z]{26}$')" in sql
    # The Implementation Gate forbids guessing which classification is a Product
    # and which is an Activity, so no backfill of those rows happens in DDL.
    ddl = statements_only(sql)
    assert "INSERT INTO app.master_data_nodes" not in ddl
    assert "FROM app.mdm_business_classifications" not in ddl


def test_the_guarded_tables_140_added_now_yield_to_an_rgpd_erasure(sql: str):
    """The live defect this migration also repairs.

    Migration 099 requires every protective DELETE trigger on an org-scoped
    table to yield to `app.rgpd_erasure`, through an explicit allowlist. 140 added
    guarded tables and joined none, so `org_purge` — which walks the FK graph and
    therefore reaches them — would have raised on any organization holding one
    published Master Data version.
    """
    assert "rgpd_erasure" in sql
    assert "'master_data_object_versions'" in sql
    assert "'master_data_memberships'" in sql
    # Platform-scoped reference data belongs to no tenant: 099 reasoned the same
    # way about import_templates, and vocabularies/presets stay out for it.
    hatch = sql[sql.index("target_tables CONSTANT") : sql.index("guard CONSTANT")]
    assert "vocabulary" not in hatch
    assert "preset" not in hatch


def test_the_migration_is_registered_in_the_manifest_and_never_re_edits_an_applied_one():
    manifest = json.loads((MIGRATIONS / "manifest.json").read_text(encoding="utf-8"))
    entries = {entry["identifier"]: entry["filename"] for entry in manifest["migrations"]}
    assert entries["143"] == "143_master_data_one_authority.sql"
    # 140 and 141 are applied. This story corrects forward; it does not re-open
    # them, which is what the repository's immutability rule requires.
    assert entries["140"] == "140_master_data_registries.sql"
    assert entries["141"] == "141_master_data_generic_core.sql"
    sql = MIGRATION.read_text(encoding="utf-8")
    assert "DROP TABLE" not in sql.upper()
    assert "DELETE FROM" not in sql.upper()
