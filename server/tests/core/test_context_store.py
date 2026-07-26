"""toorow -- Unit tests for context_store.py (Story 11.1)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from core.context_store import (
    DuplicateProcedureNameError,
    archive_topic,
    create_procedure,
    create_topic,
    get_procedure,
    get_topic,
    list_schema_docs,
    list_topics,
    update_procedure,
    update_topic,
    validate_procedure_frontmatter,
)


def test_validate_procedure_frontmatter_success():
    yaml_text = """
name: my_procedure
description: A helpful procedure for analytics.
extra_setting: true
"""
    parsed = validate_procedure_frontmatter(yaml_text)
    assert parsed["name"] == "my_procedure"
    assert parsed["description"] == "A helpful procedure for analytics."
    assert parsed["extra_setting"] is True


def test_validate_procedure_frontmatter_failures():
    with pytest.raises(ValueError, match="name"):
        validate_procedure_frontmatter("description: hello")

    with pytest.raises(ValueError, match="name"):
        validate_procedure_frontmatter("name: '   '\ndescription: hello")

    with pytest.raises(ValueError, match="description"):
        validate_procedure_frontmatter("name: my_proc")

    with pytest.raises(ValueError, match="vide"):
        validate_procedure_frontmatter("")


def test_create_topic_mocked():
    conn = MagicMock()
    cur = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur

    cur.fetchone.return_value = (
        "top_01HX",
        "proj_A",
        "My Topic",
        "body text",
        "active",
        "ann@toorow.com",
        "user_1",
        "2026-07-20T10:00:00Z",
        "2026-07-20T10:00:00Z",
    )
    cur.description = [
        ("id",),
        ("project_id",),
        ("title",),
        ("body_md",),
        ("status",),
        ("owner",),
        ("created_by",),
        ("created_at",),
        ("updated_at",),
    ]

    topic = create_topic(
        conn,
        project_id="proj_A",
        title="My Topic",
        body_md="body text",
        owner="ann@toorow.com",
        created_by="user_1",
    )

    assert topic["id"].startswith("top_")
    assert topic["title"] == "My Topic"
    assert topic["version_number"] == 1
    assert topic["project_id"] == "proj_A"
    assert topic["owner"] == "ann@toorow.com"

    # The version-append INSERT must carry `owner` too (Story 44.11) -- assert
    # against the actual SQL emitted, not just the returned dict, so a future
    # column-list regression on the versions table is caught.
    version_insert_sql, version_insert_params = cur.execute.call_args_list[1][0]
    assert "app.context_topics_versions" in version_insert_sql
    assert "owner" in version_insert_sql
    assert "ann@toorow.com" in version_insert_params


def test_create_topic_owner_defaults_to_none_when_unset():
    conn = MagicMock()
    cur = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur

    cur.fetchone.return_value = (
        "top_02HX",
        "proj_A",
        "No Owner Topic",
        "body",
        "active",
        None,
        "user_1",
        "2026-07-20T10:00:00Z",
        "2026-07-20T10:00:00Z",
    )
    cur.description = [
        ("id",),
        ("project_id",),
        ("title",),
        ("body_md",),
        ("status",),
        ("owner",),
        ("created_by",),
        ("created_at",),
        ("updated_at",),
    ]

    topic = create_topic(
        conn,
        project_id="proj_A",
        title="No Owner Topic",
        body_md="body",
        owner="   ",  # whitespace-only -- must collapse to None (_clean_owner rule)
        created_by="user_1",
    )
    assert topic["owner"] is None

    insert_sql, insert_params = cur.execute.call_args_list[0][0]
    assert "app.context_topics" in insert_sql
    # The cleaned (None, not "   ") owner value is what actually got sent to the DB.
    assert insert_params[4] is None


def test_update_topic_version_counter_increment():
    conn = MagicMock()
    cur = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur

    # First update: current topic version max is 1
    cur.fetchone.side_effect = [
        (
            "top_123",
            "proj_A",
            "Initial Title",
            "body",
            "active",
            None,
            "user_1",
            "2026-07-20T10:00:00Z",
            "2026-07-20T10:00:00Z",
        ),  # SELECT FOR UPDATE
        (1,),  # MAX version_number
        (
            "top_123",
            "proj_A",
            "Updated Title 1",
            "body",
            "active",
            None,
            "user_1",
            "2026-07-20T10:00:00Z",
            "2026-07-20T10:05:00Z",
        ),  # UPDATE RETURNING
    ]
    cur.description = [
        ("id",),
        ("project_id",),
        ("title",),
        ("body_md",),
        ("status",),
        ("owner",),
        ("created_by",),
        ("created_at",),
        ("updated_at",),
    ]

    t1 = update_topic(
        conn, topic_id="top_123", patch={"title": "Updated Title 1"}, changed_by="user_1"
    )
    assert t1["version_number"] == 2

    # Second update: current max version is 2
    cur.fetchone.side_effect = [
        (
            "top_123",
            "proj_A",
            "Updated Title 1",
            "body",
            "active",
            None,
            "user_1",
            "2026-07-20T10:00:00Z",
            "2026-07-20T10:05:00Z",
        ),
        (2,),
        (
            "top_123",
            "proj_A",
            "Updated Title 2",
            "body",
            "active",
            None,
            "user_1",
            "2026-07-20T10:00:00Z",
            "2026-07-20T10:10:00Z",
        ),
    ]

    t2 = update_topic(
        conn, topic_id="top_123", patch={"title": "Updated Title 2"}, changed_by="user_1"
    )
    assert t2["version_number"] == 3


def test_update_topic_patch_owner_round_trips():
    """PATCHing `owner` updates the live row AND the appended version row
    (Story 44.11) -- both are asserted directly against the emitted SQL, not
    just the returned dict, since the version-append INSERT enumerates
    columns by hand and is exactly the place a future edit could silently
    drop the new column again."""
    conn = MagicMock()
    cur = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur

    cur.fetchone.side_effect = [
        (
            "top_own",
            "proj_A",
            "Title",
            "body",
            "active",
            None,
            "user_1",
            "2026-07-20T10:00:00Z",
            "2026-07-20T10:00:00Z",
        ),  # SELECT FOR UPDATE (no explicit owner yet)
        (1,),  # MAX version_number
        (
            "top_own",
            "proj_A",
            "Title",
            "body",
            "active",
            "bob@toorow.com",
            "user_1",
            "2026-07-20T10:00:00Z",
            "2026-07-20T10:05:00Z",
        ),  # UPDATE RETURNING
    ]
    cur.description = [
        ("id",),
        ("project_id",),
        ("title",),
        ("body_md",),
        ("status",),
        ("owner",),
        ("created_by",),
        ("created_at",),
        ("updated_at",),
    ]

    updated = update_topic(
        conn, topic_id="top_own", patch={"owner": "  bob@toorow.com  "}, changed_by="user_1"
    )
    assert updated["owner"] == "bob@toorow.com"

    update_sql, update_params = cur.execute.call_args_list[2][0]
    assert "UPDATE app.context_topics" in update_sql
    assert "owner" in update_sql
    assert "bob@toorow.com" in update_params  # trimmed before hitting the DB

    version_insert_sql, version_insert_params = cur.execute.call_args_list[3][0]
    assert "app.context_topics_versions" in version_insert_sql
    assert "owner" in version_insert_sql
    assert "bob@toorow.com" in version_insert_params


def test_update_topic_patch_owner_explicit_null_clears_it():
    conn = MagicMock()
    cur = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur

    cur.fetchone.side_effect = [
        (
            "top_own2",
            "proj_A",
            "Title",
            "body",
            "active",
            "bob@toorow.com",
            "user_1",
            "2026-07-20T10:00:00Z",
            "2026-07-20T10:00:00Z",
        ),
        (1,),
        (
            "top_own2",
            "proj_A",
            "Title",
            "body",
            "active",
            None,
            "user_1",
            "2026-07-20T10:00:00Z",
            "2026-07-20T10:05:00Z",
        ),
    ]
    cur.description = [
        ("id",),
        ("project_id",),
        ("title",),
        ("body_md",),
        ("status",),
        ("owner",),
        ("created_by",),
        ("created_at",),
        ("updated_at",),
    ]

    updated = update_topic(conn, topic_id="top_own2", patch={"owner": None}, changed_by="user_1")
    assert updated["owner"] is None


def test_update_topic_owner_invalid_type_rejected():
    conn = MagicMock()
    cur = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    cur.fetchone.return_value = (
        "top_own3",
        "proj_A",
        "Title",
        "body",
        "active",
        None,
        "user_1",
        "2026-07-20T10:00:00Z",
        "2026-07-20T10:00:00Z",
    )
    cur.description = [
        ("id",),
        ("project_id",),
        ("title",),
        ("body_md",),
        ("status",),
        ("owner",),
        ("created_by",),
        ("created_at",),
        ("updated_at",),
    ]

    with pytest.raises(ValueError, match="propriétaire"):
        update_topic(conn, topic_id="top_own3", patch={"owner": 123}, changed_by="user_1")


def test_archive_topic_mocked():
    conn = MagicMock()
    cur = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur

    cur.fetchone.side_effect = [
        (
            "top_123",
            "proj_A",
            "Title",
            "body",
            "active",
            None,
            "user_1",
            "2026-07-20T10:00:00Z",
            "2026-07-20T10:00:00Z",
        ),
        (1,),
        (
            "top_123",
            "proj_A",
            "Title",
            "body",
            "archived",
            None,
            "user_1",
            "2026-07-20T10:00:00Z",
            "2026-07-20T10:15:00Z",
        ),
    ]
    cur.description = [
        ("id",),
        ("project_id",),
        ("title",),
        ("body_md",),
        ("status",),
        ("owner",),
        ("created_by",),
        ("created_at",),
        ("updated_at",),
    ]

    archived = archive_topic(conn, topic_id="top_123", changed_by="user_1")
    assert archived["status"] == "archived"
    assert archived["version_number"] == 2


def test_list_topics_scope_filtering():
    conn = MagicMock()
    cur = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur

    # Mock DB returning 2 items (proj_A + platform)
    cur.fetchall.return_value = [
        (
            "top_1",
            "proj_A",
            "Project A Topic",
            "body",
            "active",
            "ann@toorow.com",
            "user_1",
            "2026-07-20T10:00:00Z",
            "2026-07-20T10:00:00Z",
            1,
        ),
        (
            "top_2",
            None,
            "Platform Topic",
            "body",
            "active",
            None,
            "user_1",
            "2026-07-20T09:00:00Z",
            "2026-07-20T09:00:00Z",
            1,
        ),
    ]
    cur.description = [
        ("id",),
        ("project_id",),
        ("title",),
        ("body_md",),
        ("status",),
        ("owner",),
        ("created_by",),
        ("created_at",),
        ("updated_at",),
        ("version_number",),
    ]

    topics = list_topics(conn, project_id="proj_A")
    assert len(topics) == 2
    assert {t["id"] for t in topics} == {"top_1", "top_2"}
    by_id = {t["id"]: t for t in topics}
    assert by_id["top_1"]["owner"] == "ann@toorow.com"
    assert by_id["top_2"]["owner"] is None


def test_get_topic_cross_scope():
    conn = MagicMock()
    cur = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur

    # Mock DB row owned by proj_B
    cur.fetchone.return_value = (
        "top_proj_b",
        "proj_B",
        "B Topic",
        "body",
        "active",
        None,
        "user_1",
        "2026-07-20T10:00:00Z",
        "2026-07-20T10:00:00Z",
        1,
    )
    cur.description = [
        ("id",),
        ("project_id",),
        ("title",),
        ("body_md",),
        ("status",),
        ("owner",),
        ("created_by",),
        ("created_at",),
        ("updated_at",),
        ("version_number",),
    ]

    # Caller from proj_A requests proj_B topic -> returns None
    result = get_topic(conn, topic_id="top_proj_b", caller_project_id="proj_A")
    assert result is None


def test_create_procedure_duplicate_name():
    """Pre-check removed; duplicate now raised by DB UniqueViolation catch (Fix 2).

    We raise psycopg.errors.UniqueViolation from cur.execute to simulate the partial
    unique index blocking a duplicate active-name insert. The store catches it and
    re-raises as DuplicateProcedureNameError.
    """
    import psycopg.errors

    conn = MagicMock()
    cur = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur

    # Build a UniqueViolation via subclass instantiation with no args (psycopg allows this).
    exc = psycopg.errors.UniqueViolation.__new__(psycopg.errors.UniqueViolation)
    cur.execute.side_effect = exc

    fm = "name: duplicate_proc\ndescription: test"
    with pytest.raises(DuplicateProcedureNameError):
        create_procedure(conn, project_id="proj_A", frontmatter_yaml=fm, created_by="user_1")


def test_create_procedure_archived_name_reuse_allowed():
    """Creating a procedure with an archived-name does NOT raise
    DuplicateProcedureNameError (Fix 2).

    With the pre-check removed, the DB's partial index (WHERE status != 'archived') allows
    reuse. We simulate DB returning the new row without UniqueViolation.
    """
    conn = MagicMock()
    cur = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur

    cur.fetchone.return_value = (
        "proc_new",
        "proj_A",
        "reused_name",
        "New description",
        "name: reused_name\ndescription: New description",
        "",
        "active",
        None,
        "user_1",
        "2026-07-20T12:00:00Z",
        "2026-07-20T12:00:00Z",
    )
    cur.description = [
        ("id",),
        ("project_id",),
        ("name",),
        ("description",),
        ("frontmatter_yaml",),
        ("body_md",),
        ("status",),
        ("owner",),
        ("created_by",),
        ("created_at",),
        ("updated_at",),
    ]

    fm = "name: reused_name\ndescription: New description"
    # Should NOT raise — archived name reuse is permitted
    proc = create_procedure(conn, project_id="proj_A", frontmatter_yaml=fm, created_by="user_1")
    assert proc["name"] == "reused_name"
    assert proc["status"] == "active"
    assert proc["version_number"] == 1
    assert proc["owner"] is None


def test_create_procedure_with_owner():
    conn = MagicMock()
    cur = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur

    cur.fetchone.return_value = (
        "proc_owned",
        "proj_A",
        "owned_proc",
        "Has an owner",
        "name: owned_proc\ndescription: Has an owner",
        "",
        "active",
        "carol@toorow.com",
        "user_1",
        "2026-07-20T12:00:00Z",
        "2026-07-20T12:00:00Z",
    )
    cur.description = [
        ("id",),
        ("project_id",),
        ("name",),
        ("description",),
        ("frontmatter_yaml",),
        ("body_md",),
        ("status",),
        ("owner",),
        ("created_by",),
        ("created_at",),
        ("updated_at",),
    ]

    fm = "name: owned_proc\ndescription: Has an owner"
    proc = create_procedure(
        conn,
        project_id="proj_A",
        frontmatter_yaml=fm,
        owner="carol@toorow.com",
        created_by="user_1",
    )
    assert proc["owner"] == "carol@toorow.com"

    version_insert_sql, version_insert_params = cur.execute.call_args_list[1][0]
    assert "app.procedures_versions" in version_insert_sql
    assert "owner" in version_insert_sql
    assert "carol@toorow.com" in version_insert_params


def test_update_procedure_patch_owner_round_trips():
    conn = MagicMock()
    cur = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur

    fm = "name: proc_x\ndescription: desc"
    cur.fetchone.side_effect = [
        (
            "proc_x",
            "proj_A",
            "proc_x",
            "desc",
            fm,
            "",
            "active",
            None,
            "user_1",
            "2026-07-20T12:00:00Z",
            "2026-07-20T12:00:00Z",
        ),  # SELECT FOR UPDATE
        (1,),  # MAX version_number
        (
            "proc_x",
            "proj_A",
            "proc_x",
            "desc",
            fm,
            "",
            "active",
            "dave@toorow.com",
            "user_1",
            "2026-07-20T12:00:00Z",
            "2026-07-20T12:05:00Z",
        ),  # UPDATE RETURNING
    ]
    cur.description = [
        ("id",),
        ("project_id",),
        ("name",),
        ("description",),
        ("frontmatter_yaml",),
        ("body_md",),
        ("status",),
        ("owner",),
        ("created_by",),
        ("created_at",),
        ("updated_at",),
    ]

    updated = update_procedure(
        conn, procedure_id="proc_x", patch={"owner": "dave@toorow.com"}, changed_by="user_1"
    )
    assert updated["owner"] == "dave@toorow.com"

    update_sql, update_params = cur.execute.call_args_list[2][0]
    assert "UPDATE app.procedures" in update_sql
    assert "owner" in update_sql
    assert "dave@toorow.com" in update_params

    version_insert_sql, version_insert_params = cur.execute.call_args_list[3][0]
    assert "app.procedures_versions" in version_insert_sql
    assert "owner" in version_insert_sql
    assert "dave@toorow.com" in version_insert_params


def test_get_procedure_returns_owner_column():
    conn = MagicMock()
    cur = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur

    cur.fetchone.return_value = (
        "proc_y",
        "proj_A",
        "proc_y",
        "desc",
        "name: proc_y\ndescription: desc",
        "",
        "active",
        "erin@toorow.com",
        "user_1",
        "2026-07-20T12:00:00Z",
        "2026-07-20T12:00:00Z",
        1,
    )
    cur.description = [
        ("id",),
        ("project_id",),
        ("name",),
        ("description",),
        ("frontmatter_yaml",),
        ("body_md",),
        ("status",),
        ("owner",),
        ("created_by",),
        ("created_at",),
        ("updated_at",),
        ("version_number",),
    ]

    proc = get_procedure(conn, procedure_id="proc_y")
    assert proc["owner"] == "erin@toorow.com"


# ---------------------------------------------------------------------------
# Story 44.3: list_schema_docs
# ---------------------------------------------------------------------------


def test_list_schema_docs_maps_relation_to_title_source_and_resolves_version():
    """list_schema_docs SELECTs app.schema_context real columns (relation, body_md,
    generated_at) and resolves version_number via a scalar subquery over
    schema_context_versions. Unlike topics/procedures, schema_context_versions stores
    PRE-update snapshots (upsert_schema_context_doc appends the OLD body before
    overwriting), so the live doc's version is MAX(history)+1, not MAX(history) with a
    GROUP BY. Assert the emitted SQL actually uses the '+ 1' scalar-subquery form (this
    is what catches the off-by-one regression, not just the returned value)."""
    conn = MagicMock()
    cur = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur

    cur.fetchall.return_value = [
        (
            "sctx_1",
            "proj_A",
            "fact_ga4_sessions",
            "columns",
            "session_id BIGINT, ...",
            "2026-07-20T08:00:00Z",
            "2026-07-20T08:00:00Z",
            2,
        ),
    ]
    cur.description = [
        ("id",),
        ("project_id",),
        ("relation",),
        ("doc_kind",),
        ("body_md",),
        ("generated_at",),
        ("created_at",),
        ("version_number",),
    ]

    docs = list_schema_docs(conn, project_id="proj_A")
    assert len(docs) == 1
    doc = docs[0]
    assert doc["relation"] == "fact_ga4_sessions"
    assert doc["body_md"] == "session_id BIGINT, ..."
    assert doc["version_number"] == 2

    # Query filters on the caller's project_id (schema_context.project_id is NOT NULL,
    # so there is no platform-scope branch to test here — unlike topics/procedures).
    executed_sql, executed_params = cur.execute.call_args[0]
    assert "app.schema_context" in executed_sql
    assert executed_params == ("proj_A",)

    # Off-by-one guard (Finding 1, 44.3): the version_number column MUST be computed via
    # the scalar-subquery "+ 1" form, not a topics/procedures-style
    # "COALESCE(MAX(v.version_number), 1)" GROUP BY, which would under-count by one
    # against the PRE-update snapshot semantics of schema_context_versions.
    normalized_sql = " ".join(executed_sql.split())
    assert "0) + 1" in normalized_sql, (
        "expected a 'COALESCE(MAX(...), 0) + 1' scalar-subquery version_number, "
        f"got SQL: {normalized_sql}"
    )
    assert "GROUP BY" not in normalized_sql.upper(), (
        "list_schema_docs must not GROUP BY body_md (perf finding 4 / 44.3)"
    )


def test_list_schema_docs_empty_project_returns_empty_list():
    conn = MagicMock()
    cur = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    cur.fetchall.return_value = []
    cur.description = [
        ("id",),
        ("project_id",),
        ("relation",),
        ("doc_kind",),
        ("body_md",),
        ("generated_at",),
        ("created_at",),
        ("version_number",),
    ]

    docs = list_schema_docs(conn, project_id="proj_empty")
    assert docs == []
