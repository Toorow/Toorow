"""Static contracts for Epic 22's additive confirmation migration."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
MIGRATION = ROOT / "infra/nango/migrations/188_file_source_template_confirmation.sql"
MANIFEST = ROOT / "infra/nango/migrations/manifest.json"


def canonical_checksum(path: Path) -> str:
    text = "\n".join(path.read_text(encoding="utf-8").splitlines()) + "\n"
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def test_manifest_pins_the_exact_confirmation_migration():
    entries = json.loads(MANIFEST.read_text(encoding="utf-8"))["migrations"]
    entry = next(item for item in entries if item["identifier"] == "188")
    assert entry == {
        "identifier": "188",
        "filename": MIGRATION.name,
        "sha256": canonical_checksum(MIGRATION),
    }


def test_confirmation_migration_is_additive_and_does_not_erase_the_orphan():
    sql = MIGRATION.read_text(encoding="utf-8").lower()
    assert "create table if not exists app.file_source_template_confirmations" in sql
    assert "unique (template_id, mapping_version_id)" in sql
    assert "foreign key (project_id)" in sql
    assert "not valid" in sql
    assert "delete from app.file_source_templates" not in sql


def test_each_template_mapping_proof_is_bound_to_a_human_operation():
    sql = MIGRATION.read_text(encoding="utf-8")
    assert "file_source.template.gate_confirmed" in sql
    assert "operation_row.confirmation_mode IS DISTINCT FROM 'human'" in sql
    assert "operation_row.state IS DISTINCT FROM 'pending'" in sql
    assert "template_content_hash" in sql
    assert "sample_content_hash" in sql
    assert "mapping_version_id" in sql
    assert "plan_version_id" in sql


def test_orphan_delete_requires_the_exact_pending_human_operation():
    sql = MIGRATION.read_text(encoding="utf-8")
    assert "app.file_source_erasure_operation_id" in sql
    assert "file_source.template.orphan_erased" in sql
    assert "erasure_operation.state IS DISTINCT FROM 'pending'" in sql
