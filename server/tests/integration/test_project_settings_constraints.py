"""Persistence contract checks for Story 46.3 Project Settings."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
MIGRATION = ROOT / "infra" / "nango" / "migrations" / "131_project_settings_control_plane.sql"
SIXTH_CAPABILITY = (
    ROOT
    / "infra"
    / "nango"
    / "migrations"
    / "243_a_project_carries_its_sixth_capability.sql"
)


def test_migration_declares_project_settings_control_plane() -> None:
    sql = MIGRATION.read_text(encoding="utf-8")
    assert "CREATE TABLE IF NOT EXISTS app.project_configuration_versions" in sql
    assert "CREATE TABLE IF NOT EXISTS app.project_capabilities" in sql
    assert "CREATE TABLE IF NOT EXISTS app.project_change_sets" in sql
    assert "CREATE TABLE IF NOT EXISTS app.project_change_set_references" in sql
    assert "DROP COLUMN IF EXISTS currency" in sql
    assert "DROP COLUMN IF EXISTS timezone" in sql
    assert "CHECK (availability IN ('always_present', 'optional'))" in sql
    assert "CHECK (capability_key IN (" in sql
    assert "currency_fx" in sql
    assert "reporting_timezone" in sql
    assert "trg_project_configuration_versions_immutable" in sql
    assert "trg_project_change_sets_prepared_immutable" in sql
    assert "seed_project_capabilities" in sql


def test_migration_243_opens_both_checks_to_the_sixth_capability() -> None:
    """Story 61.5: `placement_mapping` enters BOTH CHECKs, under their real names.

    Migration 131 declared its two capability CHECKs inline, so PostgreSQL named
    them itself: `project_capabilities_capability_key_check` and
    `project_capabilities_check1`, read with `pg_get_constraintdef` on the
    disposable database. A migration that guessed a name would drop nothing and
    add a second constraint beside the one still refusing the key.

    The second CHECK is the one the plan forgets: it re-enumerates every key
    beside its availability, so a key accepted by the first and absent from the
    second is refused anyway.
    """
    sql = SIXTH_CAPABILITY.read_text(encoding="utf-8")

    assert "DROP CONSTRAINT IF EXISTS project_capabilities_capability_key_check;" in sql
    assert "DROP CONSTRAINT IF EXISTS project_capabilities_check1;" in sql
    assert "ADD CONSTRAINT project_capabilities_capability_key_check" in sql
    assert "ADD CONSTRAINT project_capabilities_check1" in sql

    key_check = sql.split("ADD CONSTRAINT project_capabilities_capability_key_check", 1)[1]
    key_check = key_check.split(";", 1)[0]
    for key in (
        "country",
        "currency_fx",
        "reporting_timezone",
        "tax_fees",
        "competitors",
        "placement_mapping",
    ):
        assert f"'{key}'" in key_check

    # `optional`, because `project-settings.md` ratified it on 2026-08-05:
    # "Placement Mapping | Optional and Disabled by default". The pairing CHECK
    # is what makes `always_present` unwritable for this key.
    pairing = sql.split("ADD CONSTRAINT project_capabilities_check1", 1)[1].split(";", 1)[0]
    always_present, optional = pairing.split("OR", 1)
    assert "placement_mapping" not in always_present
    assert "placement_mapping" in optional
    assert "availability = 'optional'" in optional

    # The third CHECK -- `NOT (always_present AND disabled)` -- is untouched:
    # `optional` + `disabled` is already what this capability is born as.
    assert "project_capabilities_check;" not in sql


def test_migration_243_replays_the_seed_so_no_project_is_left_at_five() -> None:
    """The reprise lives in the migration, and `seed_project_capabilities` stays
    the only writer.

    Nothing in `server/core` ever INSERTs into `app.project_capabilities`
    (`grep -rn "INSERT INTO app.project_capabilities" server/core` -> nothing):
    prepare and confirm both UPDATE. So an activation aimed at a row that does
    not exist updates zero rows, raises nothing and reports success. The backfill
    is what makes that UPDATE touch exactly one row.
    """
    sql = SIXTH_CAPABILITY.read_text(encoding="utf-8")

    seed = sql.split("CREATE OR REPLACE FUNCTION app.seed_project_capabilities", 1)[1]
    assert "'placement_mapping', 'optional', 'disabled'" in seed
    assert "ON CONFLICT (project_id, capability_key) DO NOTHING" in seed
    # The same replay migration 131 ran for the five, for the sixth.
    assert "SELECT app.seed_project_capabilities(id) FROM app.projects;" in sql


def test_migration_243_poses_no_trigger_and_therefore_needs_no_erasure_hatch() -> None:
    """Written down so nobody looks for the hatch AI-258 is about.

    `099_rgpd_erasure_trigger_guards.sql` exists because an append-only journal
    guarded by a trigger cannot be erased. This migration installs no trigger at
    all: two CHECK constraints, one function body and one backfill.
    `app.project_capabilities` stays a mutable posture table that `core/org_purge.py`
    reaches through the FK graph, with no table list to maintain. AI-258 remains
    open and is not this migration's business.
    """
    sql = SIXTH_CAPABILITY.read_text(encoding="utf-8")

    assert "CREATE TRIGGER" not in sql
    assert "CREATE OR REPLACE TRIGGER" not in sql
    assert "BEFORE UPDATE" not in sql


def test_migration_never_seeds_suggestions_as_confirmed_defaults() -> None:
    sql = MIGRATION.read_text(encoding="utf-8")
    assert "canonical_currency_confirmation_status" in sql
    assert "reporting_timezone_confirmation_status" in sql
    assert "DEFAULT 'unconfirmed'" in sql


def test_a_preference_origin_is_no_longer_born_a_suggestion() -> None:
    """AI-77/AI-81: this assertion used to be `"DEFAULT 'suggestion'" in sql`.

    That pinned the defect as a contract -- it asserted that every Project
    default is *born* claiming a source suggested it, when nothing had. Migration
    131 is applied and immutable, so 152 corrects it forward; this test now reads
    the correction instead of guarding what it replaced.

    The behavioural proof lives in
    `server/tests/core/test_project_provenance.py`, which drives the decision
    that actually runs and the parameters that reach the INSERT. This one only
    pins the schema so the column default cannot drift back.
    """
    correction = (
        ROOT
        / "infra"
        / "nango"
        / "migrations"
        / "152_project_preference_origin_is_earned.sql"
    ).read_text(encoding="utf-8")

    assert "ALTER COLUMN canonical_currency_origin SET DEFAULT 'default'" in correction
    assert "ALTER COLUMN reporting_timezone_origin SET DEFAULT 'default'" in correction
    # And the storage layer, not just Python, now bounds what an origin may claim.
    assert "chk_project_preferences_currency_origin" in correction
    assert "chk_project_preferences_timezone_origin" in correction
    assert "'operator', 'suggestion', 'default'" in correction


def test_project_crud_no_longer_reads_or_writes_legacy_default_columns() -> None:
    source = (ROOT / "server" / "core" / "admin_api.py").read_text(encoding="utf-8")
    assert "p.currency, p.timezone" not in source
    assert "status, currency, timezone" not in source
    assert "SET currency = %s" not in source
    assert "SET timezone = %s" not in source


def test_static_consumer_class_uses_only_confirmed_configuration_defaults() -> None:
    """Money and FX consumers read a confirmed version, never a stored default.

    Repaired 2026-07-31 (review-epic-48.md). This guard read
    `server/core/money_api.py`, a file that has never existed in this repository
    -- so it raised FileNotFoundError on every run and proved nothing about the
    class it names. It was introduced by 85b1deb, the same in-flight checkpoint
    that dropped three Route lines and left their handlers (46.4 C-8).

    The real money read authority is `core.money_policy`, and the guarantee
    worth pinning is the one it actually makes: an unconfirmed Project raises a
    typed gap instead of resolving to a stored currency.
    """
    projects = (ROOT / "server" / "core" / "projects_api.py").read_text(encoding="utf-8")
    fixture = (ROOT / "server" / "tests" / "isolation" / "conftest.py").read_text(encoding="utf-8")
    money = (ROOT / "server" / "core" / "money_policy.py").read_text(encoding="utf-8")
    fx = (ROOT / "server" / "core" / "fx_helper.py").read_text(encoding="utf-8")
    # This used to assert the PATCH route was absent. It is present again, and
    # correctly so: f89104b restored it because 85b1deb had deleted the Route
    # line while leaving the handler, and no Project could be updated at all
    # (46.4 C-8). What Story 46.3 actually requires is narrower and true -- the
    # route updates name, description and verification source, and must never
    # write a Project default, which belongs to the Change Set lifecycle.
    patch_handler = projects[projects.index("async def _patch_project") :][:20000]
    assert "canonical_currency" not in patch_handler
    assert "reporting_timezone" not in patch_handler
    assert "currency, timezone, created_by" not in fixture
    assert "active_configuration_version_id" in fx
    # The money policy resolves from the active version and refuses otherwise.
    assert "def resolve_money_policy" in money
    assert "money_policy_unconfirmed" in money
    assert "raise PolicyGap" in money
    # Neither consumer falls back to the stored preference row.
    assert "SELECT canonical_currency FROM app.project_preferences" not in money
    assert "SELECT canonical_currency FROM app.project_preferences" not in fx


def test_migration_scopes_versions_and_freezes_owner_references() -> None:
    sql = MIGRATION.read_text(encoding="utf-8")
    assert "FOREIGN KEY (project_id, previous_version_id)" in sql
    assert "FOREIGN KEY (project_id, rollback_version_id)" in sql
    assert "FOREIGN KEY (project_id, activated_version_id)" in sql
    assert "trg_project_change_set_references_immutable" in sql


def test_legacy_settings_surface_and_routes_are_removed() -> None:
    page = ROOT / "ui" / "admin" / "src" / "shell" / "pages" / "ProjectSettings.tsx"
    css = ROOT / "ui" / "admin" / "src" / "shell" / "pages" / "project-settings.css"
    source = page.read_text(encoding="utf-8")
    admin = (ROOT / "server" / "core" / "admin_api.py").read_text(encoding="utf-8")
    assert not css.exists()
    assert "geography/preview" not in source
    assert "vocabularies" not in source
    assert "ps-" not in source
    assert "@mui" not in source
    assert "project-settings.css" not in source
    assert "endpoint=_preview_project_geography" not in admin
    assert "endpoint=_confirm_project_geography_preview" not in admin
