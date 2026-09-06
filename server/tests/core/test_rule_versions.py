"""Story 60.5 -- a transformation rule remembers what it was.

Offline (no DB): the body builders and the hash, which are PURE. Two equal bodies
built in two different orders must hash the same, or the UNIQUE content
constraint of migration 242 would refuse nothing; and `enabled` must stay OUT of
a cleanup rule's body, or the third toggle of a rule switched off and back on
would collide with the first.

Live Postgres (skipped when TEST_POSTGRES_DSN is unset): the real DDL of
migration 242 -- an edit appends a version and THE OLD BODY IS STILL READABLE,
the numbers are monotone, a body the object already carried moves the head back
instead of writing a second row, and a hand-written UPDATE or DELETE of a
recorded version is refused by the trigger.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

from core import cleanup_rules as rules  # noqa: E402
from core import rule_versions as ledger  # noqa: E402
from core import value_mapping_tables as tables  # noqa: E402

_REPO_ROOT = Path(__file__).resolve().parents[3]
MIGRATION_242 = (
    _REPO_ROOT
    / "infra"
    / "nango"
    / "migrations"
    / "242_a_transformation_rule_remembers_what_it_was.sql"
)

IDENTITY = "owner@example.com"


def _pg_reachable() -> bool:
    if not os.environ.get("TEST_POSTGRES_DSN"):
        return False
    try:
        import psycopg

        with psycopg.connect(os.environ["TEST_POSTGRES_DSN"], connect_timeout=2) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
        return True
    except Exception:
        return False


pg_available = pytest.mark.skipif(not _pg_reachable(), reason="platform Postgres not reachable")


def _uid(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


@pytest.fixture
def fixture_org(request):
    """One org, one project and two Datastreams, dropped at the end of the test."""
    from core.db import get_connection

    org_id, project_id = _uid("org"), _uid("proj")
    datastreams = [_uid("ds"), _uid("ds")]
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO app.organizations (id, name, slug, status, created_by) "
                "VALUES (%s, 'Story 60.5 fixture', %s, 'active', %s)",
                (org_id, org_id.replace("_", "-"), IDENTITY),
            )
            cur.execute(
                "INSERT INTO app.projects (id, org_id, name, slug, created_by) "
                "VALUES (%s, %s, 'Story 60.5 fixture', %s, %s)",
                (project_id, org_id, project_id.replace("_", "-"), IDENTITY),
            )
            for index, datastream_id in enumerate(datastreams, start=1):
                cur.execute(
                    "INSERT INTO app.datastreams "
                    "(id, project_id, org_id, name, module_name, enabled) "
                    "VALUES (%s, %s, %s, %s, 'example_connector', TRUE)",
                    (datastream_id, project_id, org_id, f"Story 60.5 stream {index}"),
                )
        conn.commit()
    yield {"org_id": org_id, "project_id": project_id, "datastreams": datastreams}
    # Torn down through the repository's own eraser, which walks the FOREIGN KEY
    # GRAPH: a hand-written DELETE list leaves whatever a trigger created behind
    # (creating a Project mints its capabilities) and fails on their FK.
    from tests.conftest import purge_fixture_org

    with get_connection() as conn:
        purge_fixture_org(conn, org_id)
        conn.commit()


# ---------------------------------------------------------------------------
# Offline: the body and the hash.
# ---------------------------------------------------------------------------


def test_the_hash_is_a_property_of_the_body_not_of_the_dict_that_carried_it():
    left = ledger.value_table_body(
        {"name": "Product lines", "description": None, "scope_level": "PROJECT"},
        [("raw-b", "Line B"), ("raw-a", "Line A")],
    )
    right = ledger.value_table_body(
        {"scope_level": "PROJECT", "name": "Product lines", "description": None},
        [("raw-a", "Line A"), ("raw-b", "Line B")],
    )
    assert left == right
    assert ledger.content_hash(left) == ledger.content_hash(right)
    # And sorted, so a cursor order can never change the identity of a body.
    assert left["pairs"] == [["raw-a", "Line A"], ["raw-b", "Line B"]]


def test_a_cleanup_rule_body_excludes_enabled():
    """Migration 242 states the exclusion; this is the code half of it.

    `enabled` is the rule's lifecycle. Hashing it would make the UNIQUE content
    constraint treat "switched back on" as a body already recorded, and the third
    toggle of a rule would collide with the first.
    """
    on = ledger.cleanup_rule_body(
        {
            "name": "Drop test campaigns",
            "source_field": "campaign_name",
            "rule_kind": "exclude_row",
            "pattern": "_TEST_",
            "datastream_id": None,
            "enabled": True,
        }
    )
    off = ledger.cleanup_rule_body(
        {
            "name": "Drop test campaigns",
            "source_field": "campaign_name",
            "rule_kind": "exclude_row",
            "pattern": "_TEST_",
            "datastream_id": None,
            "enabled": False,
        }
    )
    assert "enabled" not in on
    assert ledger.content_hash(on) == ledger.content_hash(off)


def test_the_no_backfill_sentence_is_generic_and_lives_at_its_reader():
    """Arbitrage 8, corrected: SPLIT at the source, never reused whole.

    `NO_BACKFILL_STATEMENT` says two things — one generic ("nothing collected is
    rewritten") and one geographic ("retained country data is reclassified
    semantically"). Rendered on a table of campaign names the second is false
    twice, so story 60.5 takes the generic half only.

    The composed sentence went with `geographic_change` when that module was
    retired on 2026-08-17 -- it had no reader but that module. What survives is
    the generic half, at its one production reader, and what this case still
    holds is the property that made the split necessary.
    """
    from core.rule_versions import NO_BACKFILL_FACT

    # The half this module renders claims nothing about geography.
    for word in ("country", "Regrouping", "reclassified"):
        assert word not in NO_BACKFILL_FACT


def test_an_unknown_family_has_no_ledger_and_says_so():
    with pytest.raises(ledger.UnknownRuleFamily):
        ledger.ledger_for("semantic-concept")


def test_the_two_families_are_the_only_ones_registered():
    """Arbitrage 3: one ledger per family, and exactly two families."""
    assert set(ledger.LEDGERS) == {ledger.KIND_VALUE_TABLE, ledger.KIND_CLEANUP_RULE}
    assert ledger.LEDGERS[ledger.KIND_VALUE_TABLE].table == "value_mapping_table_versions"
    assert ledger.LEDGERS[ledger.KIND_CLEANUP_RULE].table == "cleanup_rule_versions"


def test_no_execution_engine_is_declared_by_this_story():
    """The current engine registry, held by code rather than by a sentence.

    This test predates the bounded-reprocess engine. Keep the historic node id so
    the former gap remains visible, but assert the independently registered
    runtime that now services it.
    """
    from core.run_origins import has_engine

    assert has_engine("bounded_reprocess") is True
    assert has_engine("mapping_change") is True


# ---------------------------------------------------------------------------
# Live Postgres: the ledger itself.
# ---------------------------------------------------------------------------


@pg_available
def test_the_two_ledgers_exist_with_their_unique_constraints():
    from core.db import get_connection

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'app' AND table_name IN "
                "('value_mapping_table_versions', 'cleanup_rule_versions')"
            )
            found = {row[0] for row in cur.fetchall()}
            cur.execute(
                "SELECT conname FROM pg_constraint WHERE conname IN "
                "('uq_value_mapping_table_version', 'uq_value_mapping_table_version_content', "
                "'uq_cleanup_rule_version', 'uq_cleanup_rule_version_content')"
            )
            constraints = {row[0] for row in cur.fetchall()}
    assert found == {"value_mapping_table_versions", "cleanup_rule_versions"}
    assert constraints == {
        "uq_value_mapping_table_version",
        "uq_value_mapping_table_version_content",
        "uq_cleanup_rule_version",
        "uq_cleanup_rule_version_content",
    }


@pg_available
@pytest.mark.skipif(
    not os.environ.get("TEST_POSTGRES_OWNER_DSN"),
    reason=(
        "the DDL is owned by the migration role, not by the application role: "
        "`python scripts/disposable_postgres.py env` exports TEST_POSTGRES_OWNER_DSN"
    ),
)
def test_the_ddl_replays_without_error():
    import psycopg

    with psycopg.connect(os.environ["TEST_POSTGRES_OWNER_DSN"]) as owner:
        with owner.cursor() as cur:
            cur.execute(MIGRATION_242.read_text(encoding="utf-8"))
        owner.commit()


@pg_available
def test_editing_a_pair_appends_a_version_and_the_old_body_is_still_readable(fixture_org):
    """AC2, and the whole point of the story.

    What the value became yesterday used to be overwritten by an UPDATE in place.
    After this, version 2 holds the new body and version 1 still holds the old
    one -- readable, dated and attributed.
    """
    from core.db import get_connection

    with get_connection() as conn:
        table = tables.create_table(
            conn,
            org_id=fixture_org["org_id"],
            project_id=fixture_org["project_id"],
            scope_level="PROJECT",
            name="Product lines",
            description=None,
            identity=IDENTITY,
        )
        entry = tables.add_entry(
            conn,
            table_id=table["id"],
            org_id=fixture_org["org_id"],
            source_value="raw-value-1",
            canonical_value="Line A",
            identity=IDENTITY,
        )
        tables.update_entry(
            conn,
            table_id=table["id"],
            org_id=fixture_org["org_id"],
            entry_id=entry["id"],
            canonical_value="Line B",
            identity=IDENTITY,
        )
        conn.commit()

        history = ledger.list_versions(
            conn, kind=ledger.KIND_VALUE_TABLE, object_id=table["id"]
        )

    # Creation, the added pair, the edited pair. Newest first, numbers monotone.
    assert [row["version_number"] for row in history] == [3, 2, 1]
    assert history[0]["body"]["pairs"] == [["raw-value-1", "Line B"]]
    # THE OLD BODY IS STILL THERE.
    assert history[1]["body"]["pairs"] == [["raw-value-1", "Line A"]]
    assert history[2]["body"]["pairs"] == []
    # And the chain says what preceded what, from the store's own sequence.
    assert history[0]["predecessor_version_id"] == history[1]["id"]
    assert history[1]["predecessor_version_id"] == history[2]["id"]
    assert history[2]["predecessor_version_id"] is None
    assert all(row["created_by"] == IDENTITY for row in history)


@pg_available
def test_a_body_the_table_already_carried_moves_the_head_back_and_records_nothing(fixture_org):
    """`uq_*_version_content` read as the IDENTITY of a body, not as a refusal.

    A DQ Monitor version is a published policy and republishing it is a no-op, so
    145 can refuse it outright. A pair a client edits back to what it was is a
    legitimate act, and a ledger that refused it would forbid an undo. The head
    pointer moves back; no second row is written.
    """
    from core.db import get_connection

    with get_connection() as conn:
        table = tables.create_table(
            conn,
            org_id=fixture_org["org_id"],
            project_id=fixture_org["project_id"],
            scope_level="PROJECT",
            name="Product lines",
            description=None,
            identity=IDENTITY,
        )
        entry = tables.add_entry(
            conn,
            table_id=table["id"],
            org_id=fixture_org["org_id"],
            source_value="raw-value-1",
            canonical_value="Line A",
            identity=IDENTITY,
        )
        tables.update_entry(
            conn,
            table_id=table["id"],
            org_id=fixture_org["org_id"],
            entry_id=entry["id"],
            canonical_value="Line B",
            identity=IDENTITY,
        )
        tables.update_entry(
            conn,
            table_id=table["id"],
            org_id=fixture_org["org_id"],
            entry_id=entry["id"],
            canonical_value="Line A",
            identity=IDENTITY,
        )
        conn.commit()

        history = ledger.list_versions(
            conn, kind=ledger.KIND_VALUE_TABLE, object_id=table["id"]
        )
        head = ledger.head_version(conn, kind=ledger.KIND_VALUE_TABLE, object_id=table["id"])

    assert [row["version_number"] for row in history] == [3, 2, 1]
    assert head is not None
    assert head["version_number"] == 2
    assert head["body"]["pairs"] == [["raw-value-1", "Line A"]]


@pg_available
def test_an_import_of_many_pairs_is_one_version(fixture_org):
    """Arbitrage 2: a version per TABLE, never per pair."""
    from core.db import get_connection

    with get_connection() as conn:
        table = tables.create_table(
            conn,
            org_id=fixture_org["org_id"],
            project_id=fixture_org["project_id"],
            scope_level="PROJECT",
            name="Product lines",
            description=None,
            identity=IDENTITY,
        )
        result = tables.import_pairs(
            conn,
            table_id=table["id"],
            org_id=fixture_org["org_id"],
            text="raw-1,Line A\nraw-2,Line B\nraw-3,Line C\n",
            identity=IDENTITY,
        )
        conn.commit()
        history = ledger.list_versions(
            conn, kind=ledger.KIND_VALUE_TABLE, object_id=table["id"]
        )

    assert result["imported_count"] == 3
    assert [row["version_number"] for row in history] == [2, 1]
    assert len(history[0]["body"]["pairs"]) == 3


@pg_available
def test_a_recorded_version_cannot_be_updated_or_deleted_by_hand(fixture_org):
    """The trigger of migration 242, not a comment about it."""
    import psycopg
    from core.db import get_connection

    with get_connection() as conn:
        table = tables.create_table(
            conn,
            org_id=fixture_org["org_id"],
            project_id=fixture_org["project_id"],
            scope_level="PROJECT",
            name="Product lines",
            description=None,
            identity=IDENTITY,
        )
        conn.commit()
        head = ledger.head_version(conn, kind=ledger.KIND_VALUE_TABLE, object_id=table["id"])
        assert head is not None

        with pytest.raises(psycopg.errors.IntegrityConstraintViolation):
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE app.value_mapping_table_versions SET body = '{}'::jsonb "
                    "WHERE id = %s",
                    (head["id"],),
                )
        conn.rollback()

        with pytest.raises(psycopg.errors.IntegrityConstraintViolation):
            with conn.cursor() as cur:
                cur.execute(
                    "DELETE FROM app.value_mapping_table_versions WHERE id = %s",
                    (head["id"],),
                )
        conn.rollback()

        # And the version is still exactly what it was.
        again = ledger.head_version(conn, kind=ledger.KIND_VALUE_TABLE, object_id=table["id"])
        assert again is not None
        assert again["content_hash"] == head["content_hash"]


@pg_available
def test_deleting_the_table_takes_its_history_with_it(fixture_org):
    """A version dies with its object, and only with it.

    The trigger allows a DELETE exactly when the parent row is already gone --
    which is true inside ON DELETE CASCADE and false for every hand-written
    DELETE, as the test above proves.
    """
    from core.db import get_connection

    with get_connection() as conn:
        table = tables.create_table(
            conn,
            org_id=fixture_org["org_id"],
            project_id=fixture_org["project_id"],
            scope_level="PROJECT",
            name="Product lines",
            description=None,
            identity=IDENTITY,
        )
        conn.commit()
        tables.delete_table(
            conn, table_id=table["id"], org_id=fixture_org["org_id"], identity=IDENTITY
        )
        conn.commit()
        assert (
            ledger.list_versions(conn, kind=ledger.KIND_VALUE_TABLE, object_id=table["id"]) == []
        )


@pg_available
def test_editing_a_pattern_appends_a_version_and_a_toggle_does_not(fixture_org):
    """The cleanup rule half, and the exclusion of `enabled` proven live."""
    from core.db import get_connection

    with get_connection() as conn:
        rule = rules.create_rule(
            conn,
            org_id=fixture_org["org_id"],
            project_id=fixture_org["project_id"],
            datastream_id=None,
            name="Drop test campaigns",
            source_field="campaign_name",
            rule_kind="exclude_row",
            pattern="_TEST_",
            identity=IDENTITY,
            dry_run_state="not_attempted",
        )
        rules.update_rule(
            conn,
            rule_id=rule["id"],
            project_id=fixture_org["project_id"],
            identity=IDENTITY,
            pattern="_TEST_|_STAGING_",
        )
        conn.commit()
        after_edit = ledger.list_versions(
            conn, kind=ledger.KIND_CLEANUP_RULE, object_id=rule["id"]
        )

        rules.update_rule(
            conn,
            rule_id=rule["id"],
            project_id=fixture_org["project_id"],
            identity=IDENTITY,
            enabled=False,
        )
        conn.commit()
        after_toggle = ledger.list_versions(
            conn, kind=ledger.KIND_CLEANUP_RULE, object_id=rule["id"]
        )

    assert [row["version_number"] for row in after_edit] == [2, 1]
    assert after_edit[0]["body"]["pattern"] == "_TEST_|_STAGING_"
    assert after_edit[1]["body"]["pattern"] == "_TEST_"
    # A toggle is lifecycle, not content: no third row.
    assert [row["version_number"] for row in after_toggle] == [2, 1]


@pg_available
def test_the_fan_out_names_the_datastreams_of_a_project_wide_rule(fixture_org):
    """Arbitrage 7. `datastream_id` NULL means every Datastream of the Project,
    and "six flows change tonight" is not an answer until the six are named."""
    from core.db import get_connection

    with get_connection() as conn:
        rule = rules.create_rule(
            conn,
            org_id=fixture_org["org_id"],
            project_id=fixture_org["project_id"],
            datastream_id=None,
            name="Drop test campaigns",
            source_field="campaign_name",
            rule_kind="exclude_row",
            pattern="_TEST_",
            identity=IDENTITY,
            dry_run_state="not_attempted",
        )
        conn.commit()
        fan_out = ledger.read_fan_out(
            conn,
            kind=ledger.KIND_CLEANUP_RULE,
            object_id=rule["id"],
            project_id=fixture_org["project_id"],
        )

    assert fan_out["impact_state"] == "known"
    assert fan_out["datastream_count"] == 2
    assert fan_out["reach"] == "project"
    assert sorted(entry["datastream_name"] for entry in fan_out["datastreams"]) == [
        "Story 60.5 stream 1",
        "Story 60.5 stream 2",
    ]


@pg_available
def test_a_fan_out_that_could_not_be_read_is_unknown_and_never_zero(fixture_org):
    """`impact_state: "unknown"` with NO count. A zero would say "nothing
    depends on this", which is the one thing that was not observed."""

    class _Exploding:
        def cursor(self, *args, **kwargs):
            raise RuntimeError("the store is unreachable")

    fan_out = ledger.read_fan_out(
        _Exploding(), kind=ledger.KIND_VALUE_TABLE, object_id="vmt_EXAMPLE"
    )
    assert fan_out["impact_state"] == "unknown"
    assert fan_out["datastream_count"] is None
    assert fan_out["datastreams"] == []


@pg_available
def test_the_preview_names_the_version_the_hash_the_streams_and_the_scope(fixture_org):
    """The confirmation shows the COUNT BEFORE the act, and the hash it shows is
    the hash the write computes -- one function, called by both."""
    from core.db import get_connection

    with get_connection() as conn:
        table = tables.create_table(
            conn,
            org_id=fixture_org["org_id"],
            project_id=fixture_org["project_id"],
            scope_level="PROJECT",
            name="Product lines",
            description=None,
            identity=IDENTITY,
        )
        tables.create_assignment(
            conn,
            table_id=table["id"],
            org_id=fixture_org["org_id"],
            datastream_id=fixture_org["datastreams"][0],
            source_field="campaign_name",
            identity=IDENTITY,
        )
        conn.commit()

        proposed = ledger.value_table_body(
            {"name": "Product lines", "description": None, "scope_level": "PROJECT"},
            [("raw-1", "Line A")],
        )
        preview = ledger.preview_change(
            conn,
            kind=ledger.KIND_VALUE_TABLE,
            object_id=table["id"],
            body=proposed,
            project_id=fixture_org["project_id"],
        )

    from core.rule_versions import NO_BACKFILL_FACT

    assert preview["history_state"] == "available"
    assert preview["current_version_number"] == 1
    assert preview["next_version_number"] == 2
    assert preview["content_hash"] == ledger.content_hash(proposed)
    assert preview["unchanged"] is False
    assert preview["impact_state"] == "known"
    assert preview["datastream_count"] == 1
    assert preview["datastreams"][0]["datastream_name"] == "Story 60.5 stream 1"
    assert preview["datastreams"][0]["source_field"] == "campaign_name"
    # Imported, never re-typed — and the GENERIC half only: the geographic half
    # of that statement would be false about a table of campaign names.
    assert preview["backfill_statement"] == NO_BACKFILL_FACT
