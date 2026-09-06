"""toorow -- Unit tests for context_store.py (Story 11.1)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from core.context_store import (
    MAX_BODY_MD_BYTES,
    MAX_FRONTMATTER_BYTES,
    DuplicateProcedureNameError,
    NotArchivedContextError,
    PayloadTooLargeError,
    archive_procedure,
    archive_topic,
    create_procedure,
    create_topic,
    get_procedure,
    get_topic,
    list_schema_docs,
    list_topics,
    restore_procedure,
    restore_topic,
    update_procedure,
    update_topic,
    validate_procedure_frontmatter,
)


def test_validate_procedure_frontmatter_success():
    yaml_text = """
name: my_procedure
description: A helpful procedure for analytics.
keywords:
  - analytics
evidence:
  - "Must have 10 rows"
"""
    parsed = validate_procedure_frontmatter(yaml_text)
    assert parsed["name"] == "my_procedure"
    assert parsed["description"] == "A helpful procedure for analytics."
    assert parsed["keywords"] == ["analytics"]
    assert parsed["evidence"] == ["Must have 10 rows"]


def test_validate_procedure_frontmatter_rejects_unsupported_keys():
    yaml_text = """
name: my_procedure
description: A helpful procedure.
unsupported_key: true
"""
    with pytest.raises(
        ValueError, match="YAML frontmatter contains unsupported keys: unsupported_key"
    ):
        validate_procedure_frontmatter(yaml_text)


def test_validate_procedure_frontmatter_failures():
    with pytest.raises(ValueError, match="name"):
        validate_procedure_frontmatter("description: hello")

    with pytest.raises(ValueError, match="name"):
        validate_procedure_frontmatter("name: '   '\ndescription: hello")

    with pytest.raises(ValueError, match="description"):
        validate_procedure_frontmatter("name: my_proc")

    with pytest.raises(ValueError, match="empty"):
        validate_procedure_frontmatter("")


def test_validate_procedure_frontmatter_normalized_skill_metadata():
    parsed = validate_procedure_frontmatter(
        """
name: campaign_review
description: Review paid-media pacing.
keywords:
  - growth-ops
tool_bindings:
  - step: 1
    tool: get_daily_report
    viz_tag: card_kpi_hero
  - step: 3
    tool: get_kpi_movers
mdm_tags:
  - spend
  - conversions
"""
    )

    assert parsed["keywords"] == ["growth-ops"]
    assert parsed["tool_bindings"] == [
        {"step": 1, "tool": "get_daily_report", "viz_tag": "card_kpi_hero"},
        {"step": 3, "tool": "get_kpi_movers"},
    ]
    assert parsed["mdm_tags"] == ["spend", "conversions"]


@pytest.mark.parametrize(
    ("yaml_suffix", "message"),
    [
        ("tool_bindings: invalid", "must be a list"),
        ("tool_bindings:\n  - step: 0\n    tool: report", "positive integer"),
        ("tool_bindings:\n  - step: 1\n    tool: ''", "non-empty string"),
        (
            "tool_bindings:\n  - step: 2\n    tool: second\n  - step: 1\n    tool: first",
            "ordered by step",
        ),
        ("mdm_tags:\n  - spend\n  - spend", "duplicate value"),
        ("evidence:\n  - dup\n  - dup", "duplicate value"),
        ("evidence_requirements: invalid", "must be a list"),
    ],
)
def test_validate_procedure_frontmatter_rejects_invalid_skill_metadata(
    yaml_suffix: str, message: str
):
    with pytest.raises(ValueError, match=message):
        validate_procedure_frontmatter(
            f"name: skill\ndescription: governed skill\n{yaml_suffix}\n"
        )


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

    with pytest.raises(ValueError, match="owner"):
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


# ---------------------------------------------------------------------------
# The way back (2026-08-18). An archive was a version, never a deletion, so
# restoring it is a version too -- and it must leave the same trail archiving
# leaves, or "restored" becomes a fact nothing can reconstruct.
# ---------------------------------------------------------------------------

_TOPIC_COLS = [
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

_PROCEDURE_COLS = [
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


def _topic_row(status: str) -> tuple:
    return (
        "top_123",
        "proj_A",
        "Title",
        "body",
        status,
        None,
        "user_1",
        "2026-07-20T10:00:00Z",
        "2026-07-20T10:15:00Z",
    )


def _procedure_row(status: str) -> tuple:
    return (
        "proc_x",
        "proj_A",
        "proc_x",
        "desc",
        "name: proc_x\ndescription: desc",
        "",
        status,
        None,
        "user_1",
        "2026-07-20T12:00:00Z",
        "2026-07-20T12:05:00Z",
    )


def test_restore_topic_brings_it_back_and_appends_a_version():
    conn = MagicMock()
    cur = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur

    cur.fetchone.side_effect = [
        _topic_row("archived"),  # SELECT FOR UPDATE
        (2,),  # MAX version_number
        _topic_row("active"),  # UPDATE RETURNING
    ]
    cur.description = _TOPIC_COLS

    restored = restore_topic(conn, topic_id="top_123", changed_by="user_1")

    assert restored["status"] == "active"
    # Same version semantics as archive: the next number, never a rewrite.
    assert restored["version_number"] == 3
    version_sql = [
        call[0][0] for call in cur.execute.call_args_list
        if "app.context_topics_versions" in call[0][0] and "INSERT" in call[0][0]
    ]
    assert len(version_sql) == 1, "a restore appends exactly one version row"


def test_restore_topic_writes_its_own_audit_action():
    """`context_topic.restored` -- not `updated`, or the trail cannot say what happened."""
    conn = MagicMock()
    cur = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    cur.fetchone.side_effect = [_topic_row("archived"), (2,), _topic_row("active")]
    cur.description = _TOPIC_COLS

    with patch("core.context_store.insert_audit_row") as audit:
        restore_topic(conn, topic_id="top_123", changed_by="user_1")

    assert audit.call_count == 1
    assert audit.call_args.kwargs["action"] == "context_topic.restored"
    assert audit.call_args.kwargs["metadata"]["version_number"] == 3


def test_restore_topic_refuses_a_topic_that_is_not_archived():
    conn = MagicMock()
    cur = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    cur.fetchone.side_effect = [_topic_row("active"), (2,)]
    cur.description = _TOPIC_COLS

    with pytest.raises(NotArchivedContextError, match="nothing to restore"):
        restore_topic(conn, topic_id="top_123", changed_by="user_1")


def test_restore_topic_honours_the_expected_version_precondition():
    from core.context_store import StaleContextVersionError

    conn = MagicMock()
    cur = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    cur.fetchone.side_effect = [_topic_row("archived"), (5,)]
    cur.description = _TOPIC_COLS

    with pytest.raises(StaleContextVersionError):
        restore_topic(conn, topic_id="top_123", changed_by="user_1", expected_version=2)


def test_archived_topic_still_refuses_an_ordinary_patch():
    """The way back is a door, not a hole: PATCH is unchanged."""
    from core.context_store import ArchivedContextError

    conn = MagicMock()
    cur = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    cur.fetchone.side_effect = [_topic_row("archived"), (2,)]
    cur.description = _TOPIC_COLS

    with pytest.raises(ArchivedContextError):
        update_topic(conn, topic_id="top_123", patch={"title": "New"}, changed_by="user_1")


def test_restore_procedure_brings_it_back_with_its_own_audit_action():
    conn = MagicMock()
    cur = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    cur.fetchone.side_effect = [
        _procedure_row("archived"),  # SELECT FOR UPDATE
        None,  # no ACTIVE row holds this name
        (2,),  # MAX version_number
        _procedure_row("active"),  # UPDATE RETURNING
    ]
    cur.description = _PROCEDURE_COLS

    with patch("core.context_store.insert_audit_row") as audit:
        restored = restore_procedure(conn, procedure_id="proc_x", changed_by="user_1")

    assert restored["status"] == "active"
    assert restored["version_number"] == 3
    assert audit.call_args.kwargs["action"] == "procedure.restored"


def test_restore_procedure_refuses_when_the_name_was_taken_meanwhile():
    """The partial unique index covers non-archived rows only."""
    conn = MagicMock()
    cur = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    cur.fetchone.side_effect = [
        _procedure_row("archived"),
        (1,),  # an ACTIVE row already carries this name
    ]
    cur.description = _PROCEDURE_COLS

    with pytest.raises(DuplicateProcedureNameError):
        restore_procedure(conn, procedure_id="proc_x", changed_by="user_1")


def test_restore_procedure_refuses_a_procedure_that_is_not_archived():
    conn = MagicMock()
    cur = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    cur.fetchone.side_effect = [_procedure_row("active"), None, (2,)]
    cur.description = _PROCEDURE_COLS

    with pytest.raises(NotArchivedContextError, match="nothing to restore"):
        restore_procedure(conn, procedure_id="proc_x", changed_by="user_1")


def test_archive_procedure_is_still_reachable_and_unchanged():
    conn = MagicMock()
    cur = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    cur.fetchone.side_effect = [
        _procedure_row("active"),
        (1,),
        _procedure_row("archived"),
    ]
    cur.description = _PROCEDURE_COLS

    with patch("core.context_store.insert_audit_row") as audit:
        archived = archive_procedure(conn, procedure_id="proc_x", changed_by="user_1")

    assert archived["status"] == "archived"
    assert audit.call_args.kwargs["action"] == "procedure.archived"


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



# --- Payload size limits (payload-limits debt) ------------------------------
#
# No HTTP middleware bounds request bodies, so the store is the gate: it must
# refuse oversized body_md / frontmatter_yaml BEFORE yaml parsing and the
# Postgres INSERT, for the REST and MCP entry points alike.


def test_validate_procedure_frontmatter_rejects_oversized_yaml():
    yaml_text = "name: p\ndescription: " + "d" * MAX_FRONTMATTER_BYTES + "\n"
    with pytest.raises(PayloadTooLargeError, match="frontmatter_yaml"):
        validate_procedure_frontmatter(yaml_text)


def test_validate_procedure_frontmatter_accepts_yaml_just_under_limit():
    prefix = "name: p\ndescription: "
    description = "d" * (MAX_FRONTMATTER_BYTES - len(prefix) - 1)
    parsed = validate_procedure_frontmatter(prefix + description)
    assert parsed["description"] == description


def test_create_topic_rejects_oversized_body():
    conn = MagicMock()
    with pytest.raises(PayloadTooLargeError, match="body_md"):
        create_topic(
            conn,
            project_id="proj_A",
            title="My Topic",
            body_md="b" * (MAX_BODY_MD_BYTES + 1),
            created_by="user_1",
        )
    # The refusal lands before any SQL is emitted.
    conn.cursor.assert_not_called()


def test_create_topic_accepts_body_just_under_limit():
    conn = MagicMock()
    cur = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur

    cur.fetchone.return_value = (
        "top_03HX",
        "proj_A",
        "My Topic",
        "body text",
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
        title="My Topic",
        body_md="b" * MAX_BODY_MD_BYTES,
        created_by="user_1",
    )
    assert topic["id"].startswith("top_")


def test_update_topic_rejects_oversized_body_patch():
    conn = MagicMock()
    with pytest.raises(PayloadTooLargeError, match="body_md"):
        update_topic(
            conn,
            topic_id="top_123",
            patch={"body_md": "b" * (MAX_BODY_MD_BYTES + 1)},
            changed_by="user_1",
        )
    conn.cursor.assert_not_called()


def test_create_procedure_rejects_oversized_body():
    conn = MagicMock()
    with pytest.raises(PayloadTooLargeError, match="body_md"):
        create_procedure(
            conn,
            project_id="proj_A",
            frontmatter_yaml="name: p\ndescription: governed skill\n",
            body_md="b" * (MAX_BODY_MD_BYTES + 1),
            created_by="user_1",
        )
    conn.cursor.assert_not_called()


def test_update_procedure_rejects_oversized_body_patch():
    conn = MagicMock()
    with pytest.raises(PayloadTooLargeError, match="body_md"):
        update_procedure(
            conn,
            procedure_id="proc_123",
            patch={"body_md": "b" * (MAX_BODY_MD_BYTES + 1)},
            changed_by="user_1",
        )
    conn.cursor.assert_not_called()


def test_a_required_step_names_its_tool_or_is_refused() -> None:
    """2026-09-05: a required step is one the recorder can observe."""
    import pytest

    from core.context_store import validate_procedure_frontmatter

    def frontmatter(*step_lines: str) -> str:
        return chr(10).join(["name: s", "description: d", "steps:", *step_lines]) + chr(10)

    ok = validate_procedure_frontmatter(
        frontmatter(
            "  - step: 1", "    action: analyze", "    label: Run it", "    tool: execute_analyze_query_spec", "    required: true",
            "  - step: 2", "    action: read", "    label: Read a target", "    target: the catalogue",
        )
    )
    assert ok["steps"][0]["required"] is True and "required" not in ok["steps"][1]
    with pytest.raises(ValueError, match="required step is one the recorder can observe"):
        validate_procedure_frontmatter(
            frontmatter("  - step: 1", "    action: read", "    label: Read a target", "    target: the catalogue", "    required: true")
        )
    with pytest.raises(ValueError, match="required must be true or false"):
        validate_procedure_frontmatter(
            frontmatter("  - step: 1", "    action: analyze", "    label: Run it", "    tool: t", "    required: yes please")
        )
