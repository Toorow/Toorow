"""toorow -- Unit tests for context_store.py (Story 11.1)."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from core.context_store import (
    DuplicateProcedureNameError,
    archive_topic,
    create_procedure,
    create_topic,
    get_topic,
    list_topics,
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
        ("created_by",),
        ("created_at",),
        ("updated_at",),
    ]

    topic = create_topic(
        conn,
        project_id="proj_A",
        title="My Topic",
        body_md="body text",
        created_by="user_1",
    )

    assert topic["id"].startswith("top_")
    assert topic["title"] == "My Topic"
    assert topic["version_number"] == 1
    assert topic["project_id"] == "proj_A"


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
            "user_1",
            "2026-07-20T10:00:00Z",
            "2026-07-20T10:10:00Z",
        ),
    ]

    t2 = update_topic(
        conn, topic_id="top_123", patch={"title": "Updated Title 2"}, changed_by="user_1"
    )
    assert t2["version_number"] == 3


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
        ("created_by",),
        ("created_at",),
        ("updated_at",),
        ("version_number",),
    ]

    topics = list_topics(conn, project_id="proj_A")
    assert len(topics) == 2
    assert {t["id"] for t in topics} == {"top_1", "top_2"}


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
