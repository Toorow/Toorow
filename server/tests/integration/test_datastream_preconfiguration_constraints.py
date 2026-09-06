"""Persistence contract checks for Story 47.2 preconfiguration evidence."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
MIGRATION = ROOT / "infra" / "nango" / "migrations" / "134_datastream_preconfiguration.sql"
OBSERVATION_MIGRATION = (
    ROOT / "infra" / "nango" / "migrations" / "135_datastream_setup_observations.sql"
)


def _sql() -> str:
    return MIGRATION.read_text(encoding="utf-8")


def test_migration_declares_four_separate_pre_datastream_identities() -> None:
    sql = _sql()
    assert "CREATE TABLE IF NOT EXISTS app.datastream_setup_drafts" in sql
    assert "CREATE TABLE IF NOT EXISTS app.datastream_setup_draft_revisions" in sql
    assert "CREATE TABLE IF NOT EXISTS app.datastream_preconfiguration_proposals" in sql
    assert "CREATE TABLE IF NOT EXISTS app.datastream_preconfiguration_evidence_refs" in sql
    assert "materialized_datastream_id" in sql
    assert "current_revision_id" in sql
    assert "current_proposal_id" in sql


def test_migration_scopes_versions_and_pointers_to_one_project_draft() -> None:
    sql = _sql()
    assert "UNIQUE (id, project_id)" in sql
    # Migration 030 created this as a unique index.  134 promotes that exact
    # index to a constraint so the composite foreign key can reference it.
    assert "ADD CONSTRAINT uq_datastreams_id_project" in sql
    assert "UNIQUE USING INDEX uq_datastreams_id_project" in sql
    assert "FOREIGN KEY (materialized_datastream_id, project_id)" in sql
    assert "UNIQUE (id, draft_id, project_id)" in sql
    assert "FOREIGN KEY (current_revision_id, id, project_id)" in sql
    assert "FOREIGN KEY (current_proposal_id, id, project_id)" in sql
    assert "FOREIGN KEY (draft_revision_id, draft_id, project_id)" in sql


def test_migration_freezes_revisions_proposals_and_evidence() -> None:
    sql = _sql()
    assert "trg_datastream_setup_draft_revisions_immutable" in sql
    assert "trg_datastream_preconfiguration_proposals_immutable" in sql
    assert "trg_datastream_preconfiguration_evidence_refs_immutable" in sql
    assert "datastream setup draft revisions are immutable" in sql
    assert "datastream preconfiguration proposals are immutable" in sql
    assert "datastream preconfiguration evidence is immutable" in sql


def test_migration_enforces_replay_content_and_bounded_safe_evidence() -> None:
    sql = _sql()
    assert "idempotency_key_hash" in sql
    assert "UNIQUE (project_id, idempotency_key_hash)" in sql
    assert "UNIQUE (draft_id, content_hash)" in sql
    assert "app.safe_preconfiguration_evidence(normalized_operator_input)" in sql
    assert "UNIQUE (draft_id, dependency_fingerprint, content_hash)" in sql
    assert "octet_length(safe_metadata::text) <= 8192" in sql
    assert "app.safe_preconfiguration_evidence" in sql


def test_setup_observations_are_revision_bound_and_immutable() -> None:
    sql = OBSERVATION_MIGRATION.read_text(encoding="utf-8")
    assert "CREATE TABLE app.datastream_setup_observations" in sql
    assert "FOREIGN KEY (draft_revision_id, draft_id, project_id)" in sql
    assert "UNIQUE (draft_id, idempotency_key_hash)" in sql
    assert "trg_datastream_setup_observations_immutable" in sql
    assert "octet_length(coverage::text) <= 4096" in sql


def test_setup_assets_hold_only_refs_and_have_explicit_retention_transition() -> None:
    sql = OBSERVATION_MIGRATION.read_text(encoding="utf-8")
    asset_table = sql.split("CREATE TABLE app.datastream_setup_assets", 1)[1].split(
        "CREATE TABLE app.datastream_setup_observations", 1
    )[0]
    assert "storage_ref TEXT NOT NULL" in asset_table
    assert "content_hash TEXT NOT NULL" in asset_table
    assert "expires_at TIMESTAMPTZ NOT NULL" in asset_table
    assert "cleanup_owner TEXT NOT NULL" in asset_table
    assert "file_bytes" not in asset_table
    assert "OLD.state = 'available' AND NEW.state IN ('expired','quarantined')" in sql
