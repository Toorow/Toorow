"""Migration 196 stores ONE platform-scoped operating procedure, and no leak.

Two properties are worth a test rather than a note.

1. **Scope and idempotence.** The procedure is `project_id IS NULL` so every
   project reads it (`core/context_search.py`: "Procedures (platform + project
   scope, active only)"), and a replay must not duplicate it. A migration that
   duplicates on replay is discovered on the second environment, not the first.

2. **The repository stays shareable.** This row is documentation written from
   production measurements, which is exactly the shape of content that leaks a
   real project id, connection id or address. The check runs on the whole file,
   comments included -- that is where the previous leak lived.

There is also a content gate: the body must keep NAMING the three closed doors.
The failure mode this guards against is a later edit that smooths the document
into something that reads complete while the chain is still shut -- which is the
lie the document exists to prevent.

Paths are anchored on ``Path(__file__).resolve().parents[3]`` (the model is
``server/tests/core/test_mapping_writes_are_governed.py:32``). A test that
resolves artifacts against the working directory returns two different verdicts
for the same tree depending on where pytest was launched.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
MIGRATIONS = ROOT / "infra" / "nango" / "migrations"
MIGRATION_196 = MIGRATIONS / "196_platform_operating_procedure.sql"
MIGRATION_031 = MIGRATIONS / "031_context_layer.sql"
MANIFEST = MIGRATIONS / "manifest.json"

PROCEDURE_ID = "proc_0000000DATASTREAMTESTSPATH"
PROCEDURE_NAME = "platform-operating-procedure"


def _sql() -> str:
    return MIGRATION_196.read_text(encoding="utf-8")


def _dollar_block(tag: str) -> str:
    """Return the text of the ``$tag$ ... $tag$`` block in the migration."""
    match = re.search(rf"\${tag}\$(.*?)\${tag}\$", _sql(), flags=re.DOTALL)
    assert match is not None, f"migration 196 has no ${tag}$ block"
    return match.group(1)


# ---------------------------------------------------------------------------
# The catalog
# ---------------------------------------------------------------------------


def test_the_migration_exists_and_is_named_for_the_catalog():
    assert MIGRATION_196.is_file(), f"missing: {MIGRATION_196}"
    # scripts/check_migration_catalog.py: ^(\d{3})_[a-z0-9][a-z0-9_]*\.sql$
    assert re.fullmatch(
        r"\d{3}_[a-z0-9][a-z0-9_]*\.sql", MIGRATION_196.name
    ), MIGRATION_196.name


def test_the_migration_is_registered_in_the_manifest_with_its_checksum():
    """The manifest freezes each migration's sha256; drift blocks the runner."""
    document = json.loads(MANIFEST.read_text(encoding="utf-8"))
    entries = {entry["filename"]: entry for entry in document["migrations"]}
    hint = (
        "run: python scripts/check_migration_catalog.py --write-manifest "
        "(only for reviewed SQL)"
    )
    assert MIGRATION_196.name in entries, f"196 absent from manifest.json -- {hint}"

    # Same canonical checksum the catalog script computes (newlines normalised).
    text = _sql().replace("\r\n", "\n").replace("\r", "\n")
    expected = hashlib.sha256(text.encode("utf-8")).hexdigest()
    assert entries[MIGRATION_196.name]["sha256"] == expected, (
        f"manifest checksum drift for {MIGRATION_196.name} -- {hint}"
    )


# ---------------------------------------------------------------------------
# One row, platform scope, active, replay-proof -- read from the SQL itself
# ---------------------------------------------------------------------------


def test_the_migration_inserts_exactly_one_procedure_and_one_version():
    sql = _sql()
    assert sql.count("INSERT INTO app.procedures (") == 1
    assert sql.count("INSERT INTO app.procedures_versions (") == 1
    # Nothing else is written, created or dropped.
    for forbidden in ("CREATE TABLE", "DROP TABLE", "ALTER TABLE", "DELETE FROM"):
        assert forbidden not in sql, f"196 must not {forbidden}"


def test_the_procedure_is_platform_scoped_and_active():
    sql = _sql()
    # Both INSERTs bind project_id positionally as a literal NULL.
    assert sql.count("v_proc_id, NULL, v_name") == 2, (
        "both the procedure and its version 1 must carry project_id = NULL"
    )
    assert "'active'" in sql
    assert f"'{PROCEDURE_NAME}'" in sql


def test_the_identity_is_a_fixed_constant_not_a_generated_id():
    """A generated id inserts a second row on the second run, by construction."""
    sql = _sql()
    assert f"'{PROCEDURE_ID}'" in sql
    for generator in ("gen_random_uuid", "uuid_generate", "ULID()", "random()"):
        assert generator not in sql, f"196 must not mint an id with {generator}"


def test_replay_is_guarded_on_both_uniqueness_keys():
    """Two ways a duplicate could appear: the id, and the platform-scope NAME.

    `uq_procedures_name_platform` (migration 031) covers non-archived rows only,
    so a guard on the id alone would still raise on a name collision.
    """
    sql = _sql()
    assert "WHERE id = v_proc_id" in sql
    assert "project_id IS NULL AND name = v_name AND status <> 'archived'" in sql
    # And the table itself may be absent on a partially built schema.
    assert "to_regclass('app.procedures')" in sql


def test_the_migration_writes_no_audit_row_and_no_owner_column():
    """Deliberate: a migration is not an identity, and 118 may not have run."""
    sql = _sql()
    # (the header names the table to explain the choice -- what must be absent
    #  is the write, not the word)
    assert "INSERT INTO app.audit_log" not in sql
    # The INSERT column lists must not name `owner` (added by migration 118):
    # naming it would make this file unrunnable against a 031-only schema.
    insert_lists = re.findall(
        r"INSERT INTO app\.procedures(?:_versions)? \((.*?)\)", sql, flags=re.DOTALL
    )
    assert len(insert_lists) == 2
    for columns in insert_lists:
        assert "owner" not in columns


# ---------------------------------------------------------------------------
# The stored payload
# ---------------------------------------------------------------------------


def test_frontmatter_passes_the_platform_validator():
    """The same validator `context_store.create_procedure` runs, on the real text."""
    from core.context_store import validate_procedure_frontmatter  # noqa: PLC0415

    parsed = validate_procedure_frontmatter(_dollar_block("procfm"))

    assert parsed["name"] == PROCEDURE_NAME
    assert parsed["description"].strip()
    # tool_bindings are normalised and ordered by the validator; every bound tool
    # must be a real MCP tool name, so a rename is caught here.
    steps = [binding["step"] for binding in parsed["tool_bindings"]]
    assert steps == sorted(steps) and len(set(steps)) == len(steps)


@pytest.mark.parametrize(
    "tool",
    [
        "get_datastream_readiness",
        "inspect_mapping",
        "propose_mapping_correction",
        "test_mapping_candidate",
        "prepare_datastream_recovery",
        "confirm_datastream_recovery",
        "set_datastream_schedule",
        "analyze_result",
        "list_platform_clocks",
        "get_procedure",
    ],
)
def test_every_bound_tool_is_defined_somewhere_in_core(tool: str):
    """A binding that names a tool nobody defines sends a reader down a dead end."""
    core = ROOT / "server" / "core"
    needle = f"def {tool}("
    assert any(
        needle in path.read_text(encoding="utf-8", errors="ignore")
        for path in core.glob("*.py")
    ), f"tool_bindings names {tool}, which no core module defines"


def test_the_body_keeps_naming_the_three_closed_doors():
    """A document that stops naming what is shut reads complete and lies."""
    body = _dollar_block("procbody")
    for marker in (
        "dispatch_not_available",
        "multi_tool_credential_needs_a_datastream",
        "current_published_execution_id",
    ):
        assert marker in body, f"the body no longer names {marker}"
    # ...and it stays dated, because an undated measurement cannot be refuted.
    assert "2026-08-02" in body


def test_the_body_keeps_both_measurement_traps():
    body = _dollar_block("procbody")
    assert "--basetemp" in body
    assert "MAX_PATH" in body
    assert "Path(__file__).resolve().parents[3]" in body


# ---------------------------------------------------------------------------
# The repository stays shareable
# ---------------------------------------------------------------------------

#: Real ids are ULID-shaped; the length floor keeps `conn_ref`-style words out.
_PROD_ID = re.compile(r"\b(?:proj|conn|ds|org|top|sctx|proc)_[A-Za-z0-9]{8,}\b")
_EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_URL_HOST = re.compile(r"https?://([A-Za-z0-9.-]+)")

_ALLOWED_ID_PREFIXES = (PROCEDURE_ID,)
_ALLOWED_EMAIL_DOMAINS = ("example.com", "example.org", "example.invalid")
_ALLOWED_HOSTS = ("example.com", "example.org", "example.invalid")


def test_the_migration_carries_no_production_identifier():
    """Comments and fixtures included -- that is where the previous leak lived."""
    text = _sql()

    leaked_ids = [
        found
        for found in _PROD_ID.findall(text)
        if not any(found.startswith(allowed) for allowed in _ALLOWED_ID_PREFIXES)
    ]
    assert leaked_ids == [], f"production-shaped identifiers: {sorted(set(leaked_ids))}"

    leaked_mail = [
        found
        for found in _EMAIL.findall(text)
        if not found.lower().endswith(_ALLOWED_EMAIL_DOMAINS)
    ]
    assert leaked_mail == [], f"real addresses: {sorted(set(leaked_mail))}"

    leaked_hosts = [
        host
        for host in _URL_HOST.findall(text)
        if not host.lower().endswith(_ALLOWED_HOSTS)
    ]
    assert leaked_hosts == [], f"external hosts: {sorted(set(leaked_hosts))}"


def test_the_fixed_procedure_id_is_not_mistaken_for_a_real_one():
    """It must be visibly constant, so nobody reads it as a captured prod id."""
    assert PROCEDURE_ID in _sql()
    suffix = PROCEDURE_ID.removeprefix("proc_")
    assert len(suffix) == 26, "keep it ULID-shaped so tooling that assumes 26 holds"
    assert suffix.startswith("0000000"), "the leading run of zeros is the tell"


# ---------------------------------------------------------------------------
# pg-gated: apply it for real, twice
# ---------------------------------------------------------------------------


def _pg_reachable() -> bool:
    dsn = os.environ.get("TEST_POSTGRES_DSN")
    if not dsn:
        return False
    try:
        import psycopg  # noqa: PLC0415

        with psycopg.connect(dsn, connect_timeout=3) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
        return True
    except Exception:
        return False


pg_available = pytest.mark.skipif(
    not _pg_reachable(), reason="TEST_POSTGRES_DSN not set/reachable -- skip live PG"
)


@pg_available
@pytest.mark.pg_owner
def test_live_migration_inserts_one_platform_procedure_and_replays_clean(monkeypatch):
    """Applied twice: still one row, still the SAME versions, still platform scope.

    Nothing is cleaned up afterwards. The row IS the intended end state of the
    migration, and `app.procedures_versions` is append-only (031 installs a
    BEFORE DELETE trigger that raises), so deleting the procedure would leave an
    undeletable version 1 behind and make the next run fail on its primary key.

    THE VERSION ASSERTION IS A BEFORE/AFTER, NOT THE LITERAL `[1]`. It read
    `== [1]`, which stopped being true when migration 203
    (`keywords_are_not_governed_metrics`) EDITED the platform procedure and
    appended version 2 as `system:migration-203` -- a legitimate governed edit
    that this test then reported as "a replay appended a second version". The
    subject here is the IDEMPOTENCE of 196, so what it must compare is the list
    before the two replays against the list after: that is true whatever later
    migrations legitimately append, and it still fails the moment a replay adds
    one of its own.
    """
    monkeypatch.setenv("PLATFORM_DB_URL", os.environ["TEST_POSTGRES_DSN"])
    from core.db import get_connection  # noqa: PLC0415

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(MIGRATION_031.read_text(encoding="utf-8"))
        conn.commit()

        with conn.cursor() as cur:
            cur.execute(
                "SELECT version_number FROM app.procedures_versions "
                "WHERE procedure_id = %s ORDER BY version_number",
                (PROCEDURE_ID,),
            )
            versions_before = [row[0] for row in cur.fetchall()]

        for _ in range(2):
            with conn.cursor() as cur:
                cur.execute(_sql())
            conn.commit()

        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, project_id, name, status, created_by,
                       length(body_md), length(frontmatter_yaml)
                FROM app.procedures
                WHERE name = %s
                """,
                (PROCEDURE_NAME,),
            )
            rows = cur.fetchall()
            assert len(rows) == 1, f"replay duplicated the procedure: {rows}"
            proc_id, project_id, name, status, created_by, body_len, fm_len = rows[0]
            assert proc_id == PROCEDURE_ID
            assert project_id is None, "platform scope is what makes it readable"
            assert name == PROCEDURE_NAME
            assert status == "active"
            assert created_by == "migration-196"
            assert body_len > 2000 and fm_len > 100

            cur.execute(
                "SELECT version_number FROM app.procedures_versions "
                "WHERE procedure_id = %s ORDER BY version_number",
                (proc_id,),
            )
            versions_after = [row[0] for row in cur.fetchall()]
            assert versions_after and versions_after[0] == 1, (
                f"the platform procedure has no version 1: {versions_after}"
            )
            assert versions_after == (versions_before or [1]), (
                f"a replay appended a version: {versions_before} -> {versions_after}"
            )

            # The platform row resolves from a project that never defined it --
            # the whole point of project_id IS NULL.
            cur.execute(
                """
                SELECT id FROM app.procedures
                WHERE status = 'active'
                  AND name = %s
                  AND (project_id IS NULL OR project_id = %s)
                ORDER BY (project_id IS NULL) ASC
                LIMIT 1
                """,
                (PROCEDURE_NAME, "proj_no_such_project"),
            )
            assert cur.fetchone()[0] == PROCEDURE_ID
        conn.commit()
