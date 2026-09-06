"""Story 60.1 -- the client's own value mapping tables: the store.

Offline (no DB): the import parser, which is PURE -- a line that does not carry
exactly two columns is REJECTED AND NAMED, never dropped, because an import that
silently skips a line presents a partial vocabulary as a complete one.

Live Postgres (skipped when TEST_POSTGRES_DSN is unset): the real DDL of
migration 235, the CRUD of a table and of its pairs, the unicity of a source
value inside a table, both accepted scopes, and the refusal of PLATFORM. Pattern
calque sur test_dimension_conformance.py.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

from core import value_mapping_tables as store  # noqa: E402

_REPO_ROOT = Path(__file__).resolve().parents[3]
MIGRATION_235 = (
    _REPO_ROOT
    / "infra"
    / "nango"
    / "migrations"
    / "235_a_client_value_table_names_the_streams_it_serves.sql"
)


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
                "VALUES (%s, 'Story 60.1 fixture', %s, 'active', 'owner@example.com')",
                (org_id, org_id.replace("_", "-")),
            )
            cur.execute(
                "INSERT INTO app.projects (id, org_id, name, slug, created_by) "
                "VALUES (%s, %s, 'Story 60.1 fixture', %s, 'owner@example.com')",
                (project_id, org_id, project_id.replace("_", "-")),
            )
            for index, datastream_id in enumerate(datastreams, start=1):
                cur.execute(
                    "INSERT INTO app.datastreams "
                    "(id, project_id, org_id, name, module_name, enabled) "
                    "VALUES (%s, %s, %s, %s, 'example_connector', TRUE)",
                    (
                        datastream_id,
                        project_id,
                        org_id,
                        f"Story 60.1 fixture stream {index}",
                    ),
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


# ===========================================================================
# Offline -- the import parser (PURE)
# ===========================================================================


def test_two_columns_are_accepted_with_a_comma_a_semicolon_or_a_tab():
    for text in ("a,b\nc,d", "a;b\nc;d", "a\tb\nc\td"):
        pairs, rejected = store.parse_pairs(text)
        assert pairs == [("a", "b"), ("c", "d")], text
        assert rejected == []


def test_the_declared_header_is_not_imported_as_a_pair():
    pairs, rejected = store.parse_pairs("source_value,canonical_value\na,b")
    assert pairs == [("a", "b")]
    assert rejected == []


def test_a_line_without_two_columns_is_rejected_and_NAMED():
    """The whole point: it is refused with its line number, not skipped."""
    pairs, rejected = store.parse_pairs("a,b\nlonely\nc,d")
    assert pairs == [("a", "b"), ("c", "d")]
    assert [(row.line, row.reason) for row in rejected] == [(2, "not_two_columns")]
    assert rejected[0].raw == "lonely"


def test_a_three_column_line_is_rejected_too():
    _, rejected = store.parse_pairs("a,b,c")
    assert [(row.line, row.reason) for row in rejected] == [(1, "not_two_columns")]


def test_an_empty_cell_is_named_by_the_side_that_is_empty():
    _, rejected = store.parse_pairs('"",b\na,""')
    assert [row.reason for row in rejected] == ["empty_source_value", "empty_canonical_value"]


def test_the_same_source_value_twice_in_one_file_is_rejected_not_overwritten():
    pairs, rejected = store.parse_pairs("a,b\na,c")
    assert pairs == [("a", "b")]
    assert [(row.line, row.reason) for row in rejected] == [(2, "duplicate_source_value")]


def test_a_blank_line_claims_nothing_and_is_not_a_rejection():
    pairs, rejected = store.parse_pairs("a,b\n\n\nc,d\n")
    assert pairs == [("a", "b"), ("c", "d")]
    assert rejected == []


def test_platform_is_not_a_scope_of_a_client_table():
    with pytest.raises(store.InvalidValueMappingScope):
        store.validate_scope("PLATFORM", None, None)
    with pytest.raises(store.InvalidValueMappingScope):
        store.validate_scope("PLATFORM", "org_EXAMPLE", None)
    assert store.VALID_SCOPES == {"ORG", "PROJECT"}


def test_org_and_project_scopes_are_the_only_two_accepted():
    store.validate_scope("ORG", "org_EXAMPLE", None)
    store.validate_scope("PROJECT", "org_EXAMPLE", "proj_EXAMPLE")
    with pytest.raises(store.InvalidValueMappingScope):
        store.validate_scope("ORG", "org_EXAMPLE", "proj_EXAMPLE")
    with pytest.raises(store.InvalidValueMappingScope):
        store.validate_scope("PROJECT", "org_EXAMPLE", None)


def test_no_client_vocabulary_is_shipped_in_the_module():
    """Same interdiction as `geographic_conformance.py:20-22`.

    A default alias baked into the code would be a decision taken for a client
    who never made it, and it would be invisible in the surface that is supposed
    to own the vocabulary.
    """
    containers = {
        name
        for name, value in vars(store).items()
        if not name.startswith("__")
        and isinstance(value, (dict, list, set, frozenset, tuple))
    }
    # Exactly four, all of them syntax and none of them vocabulary: the accepted
    # scopes, the separators an import may use, and the two column names a header
    # may declare. Any fifth container would be a decision taken for a client who
    # never made it.
    assert containers == {"VALID_SCOPES", "_DELIMITERS", "_HEADER"}, containers


# ===========================================================================
# Live Postgres -- the real DDL and the real CRUD
# ===========================================================================


@pg_available
@pytest.mark.skipif(
    not os.environ.get("TEST_POSTGRES_OWNER_DSN"),
    reason=(
        "the DDL is owned by the migration role, not by the application role: "
        "`python scripts/disposable_postgres.py env` exports TEST_POSTGRES_OWNER_DSN"
    ),
)
def test_the_ddl_exists_and_replays_without_error():
    """Replayed as the OWNER, which is the role migrations run under.

    `connector` is the application role and owns nothing; a COMMENT ON or a
    CREATE TRIGGER under it would fail for a reason that has nothing to do with
    this migration.
    """
    import psycopg
    from core.db import get_connection

    with psycopg.connect(os.environ["TEST_POSTGRES_OWNER_DSN"]) as owner:
        with owner.cursor() as cur:
            cur.execute(MIGRATION_235.read_text(encoding="utf-8"))
        owner.commit()

    with get_connection() as conn:
        with conn.cursor() as cur:
            # The version ledger `app.value_mapping_table_versions` is migration
            # 242 (story 60.5) and is excluded on purpose: this assertion is
            # about the EXACT surface 235 creates, and widening it to whatever
            # matches the prefix would stop measuring that.
            cur.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'app' AND table_name LIKE 'value_mapping%%' "
                "AND table_name NOT LIKE '%%_versions'"
            )
            tables = {row[0] for row in cur.fetchall()}
    assert tables == {
        "value_mapping_tables",
        "value_mapping_entries",
        "value_mapping_assignments",
    }


@pg_available
def test_an_entry_carries_no_status_column():
    """Arbitrage 4, held by the schema and not only by a comment.

    `proposed/confirmed/rejected` belongs to the semi-automatic suggestions of
    migration 052: `conform_value` resolves confirmed rows only, so a pair a
    client imported would apply to nothing at all.
    """
    from core.db import get_connection

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = 'app' AND table_name = 'value_mapping_entries'"
            )
            columns = {row[0] for row in cur.fetchall()}
    assert "status" not in columns
    assert {"source_value", "canonical_value"} <= columns


@pg_available
def test_a_table_is_created_read_renamed_and_deleted(fixture_org):
    from core.db import get_connection

    with get_connection() as conn:
        created = store.create_table(
            conn,
            org_id=fixture_org["org_id"],
            project_id=fixture_org["project_id"],
            scope_level="PROJECT",
            name="Campaign vocabulary",
            description="What the team calls a product line.",
            identity="owner@example.com",
        )
        conn.commit()
        assert created["id"].startswith("vmt_")
        assert created["scope_level"] == "PROJECT"
        assert created["entry_count"] == 0
        assert created["datastream_count"] == 0

        listed = store.list_tables(
            conn, org_id=fixture_org["org_id"], project_id=fixture_org["project_id"]
        )
        assert [row["id"] for row in listed] == [created["id"]]

        renamed = store.update_table(
            conn,
            table_id=created["id"],
            org_id=fixture_org["org_id"],
            name="Product lines",
            description=None,
            identity="owner@example.com",
        )
        conn.commit()
        assert renamed["name"] == "Product lines"

        store.delete_table(
            conn,
            table_id=created["id"],
            org_id=fixture_org["org_id"],
            identity="owner@example.com",
        )
        conn.commit()
        with pytest.raises(store.ValueMappingNotFound):
            store.get_table(conn, table_id=created["id"], org_id=fixture_org["org_id"])


@pg_available
def test_an_org_scoped_table_is_visible_from_the_project_and_carries_no_project(fixture_org):
    from core.db import get_connection

    with get_connection() as conn:
        table = store.create_table(
            conn,
            org_id=fixture_org["org_id"],
            project_id=fixture_org["project_id"],
            scope_level="ORG",
            name="Organization vocabulary",
            description=None,
            identity="owner@example.com",
        )
        conn.commit()
        assert table["project_id"] is None
        assert table["scope_level"] == "ORG"
        visible = store.list_tables(
            conn, org_id=fixture_org["org_id"], project_id=fixture_org["project_id"]
        )
        assert table["id"] in {row["id"] for row in visible}


@pg_available
def test_the_platform_scope_never_reaches_the_store(fixture_org):
    from core.db import get_connection

    with get_connection() as conn:
        with pytest.raises(store.InvalidValueMappingScope):
            store.create_table(
                conn,
                org_id=fixture_org["org_id"],
                project_id=None,
                scope_level="PLATFORM",
                name="Platform vocabulary",
                description=None,
                identity="owner@example.com",
            )
        conn.rollback()


@pg_available
def test_two_tables_of_one_scope_cannot_share_a_name(fixture_org):
    from core.db import get_connection

    with get_connection() as conn:
        store.create_table(
            conn,
            org_id=fixture_org["org_id"],
            project_id=fixture_org["project_id"],
            scope_level="PROJECT",
            name="Product lines",
            description=None,
            identity="owner@example.com",
        )
        conn.commit()
        with pytest.raises(store.ValueMappingConflict):
            store.create_table(
                conn,
                org_id=fixture_org["org_id"],
                project_id=fixture_org["project_id"],
                scope_level="PROJECT",
                name="product LINES",
                description=None,
                identity="owner@example.com",
            )
        conn.rollback()


@pg_available
def test_one_live_pair_per_source_value(fixture_org):
    from core.db import get_connection

    with get_connection() as conn:
        table = store.create_table(
            conn,
            org_id=fixture_org["org_id"],
            project_id=fixture_org["project_id"],
            scope_level="PROJECT",
            name="Product lines",
            description=None,
            identity="owner@example.com",
        )
        store.add_entry(
            conn,
            table_id=table["id"],
            org_id=fixture_org["org_id"],
            source_value="raw-value-1",
            canonical_value="Line A",
            identity="owner@example.com",
        )
        conn.commit()
        with pytest.raises(store.ValueMappingConflict):
            store.add_entry(
                conn,
                table_id=table["id"],
                org_id=fixture_org["org_id"],
                source_value="raw-value-1",
                canonical_value="Line B",
                identity="owner@example.com",
            )
        conn.rollback()

        entries = store.list_entries(conn, table_id=table["id"])
        assert [(e["source_value"], e["canonical_value"]) for e in entries] == [
            ("raw-value-1", "Line A")
        ]


@pg_available
def test_an_entry_is_edited_and_removed(fixture_org):
    from core.db import get_connection

    with get_connection() as conn:
        table = store.create_table(
            conn,
            org_id=fixture_org["org_id"],
            project_id=fixture_org["project_id"],
            scope_level="PROJECT",
            name="Product lines",
            description=None,
            identity="owner@example.com",
        )
        entry = store.add_entry(
            conn,
            table_id=table["id"],
            org_id=fixture_org["org_id"],
            source_value="raw-value-1",
            canonical_value="Line A",
            identity="owner@example.com",
        )
        updated = store.update_entry(
            conn,
            table_id=table["id"],
            org_id=fixture_org["org_id"],
            entry_id=entry["id"],
            canonical_value="Line B",
            identity="owner@example.com",
        )
        conn.commit()
        assert updated["canonical_value"] == "Line B"
        assert updated["source_value"] == "raw-value-1"

        store.delete_entry(
            conn,
            table_id=table["id"],
            org_id=fixture_org["org_id"],
            entry_id=entry["id"],
            identity="owner@example.com",
        )
        conn.commit()
        assert store.list_entries(conn, table_id=table["id"]) == []


@pg_available
def test_an_import_writes_the_good_lines_and_names_the_bad_one(fixture_org):
    """The refusal is reported WITH the result, so nothing is partial in silence."""
    from core.db import get_connection

    with get_connection() as conn:
        table = store.create_table(
            conn,
            org_id=fixture_org["org_id"],
            project_id=fixture_org["project_id"],
            scope_level="PROJECT",
            name="Product lines",
            description=None,
            identity="owner@example.com",
        )
        conn.commit()
        result = store.import_pairs(
            conn,
            table_id=table["id"],
            org_id=fixture_org["org_id"],
            text="raw-1,Line A\nlonely\nraw-2,Line B\n",
            identity="owner@example.com",
        )
        conn.commit()

    assert result["imported_count"] == 2
    assert result["rejected_count"] == 1
    assert result["rejected"][0]["line"] == 2
    assert result["rejected"][0]["reason"] == "not_two_columns"


@pg_available
def test_an_import_never_overwrites_a_pair_the_client_typed(fixture_org):
    from core.db import get_connection

    with get_connection() as conn:
        table = store.create_table(
            conn,
            org_id=fixture_org["org_id"],
            project_id=fixture_org["project_id"],
            scope_level="PROJECT",
            name="Product lines",
            description=None,
            identity="owner@example.com",
        )
        store.add_entry(
            conn,
            table_id=table["id"],
            org_id=fixture_org["org_id"],
            source_value="raw-1",
            canonical_value="Line A",
            identity="owner@example.com",
        )
        conn.commit()
        result = store.import_pairs(
            conn,
            table_id=table["id"],
            org_id=fixture_org["org_id"],
            text="raw-1,Line Z\n",
            identity="owner@example.com",
        )
        conn.commit()
        entries = store.list_entries(conn, table_id=table["id"])

    assert result["imported_count"] == 0
    assert result["rejected"][0]["reason"] == "already_present"
    assert entries[0]["canonical_value"] == "Line A"


@pg_available
def test_a_table_of_another_org_is_not_found_rather_than_refused(fixture_org):
    """Existence-hiding at the store level too: a wrong org gets `not found`."""
    from core.db import get_connection

    with get_connection() as conn:
        table = store.create_table(
            conn,
            org_id=fixture_org["org_id"],
            project_id=fixture_org["project_id"],
            scope_level="PROJECT",
            name="Product lines",
            description=None,
            identity="owner@example.com",
        )
        conn.commit()
        with pytest.raises(store.ValueMappingNotFound):
            store.get_table(conn, table_id=table["id"], org_id=_uid("org"))


@pg_available
def test_every_mutation_leaves_an_audit_row(fixture_org):
    from core.db import get_connection

    with get_connection() as conn:
        table = store.create_table(
            conn,
            org_id=fixture_org["org_id"],
            project_id=fixture_org["project_id"],
            scope_level="PROJECT",
            name="Product lines",
            description=None,
            identity="owner@example.com",
        )
        store.add_entry(
            conn,
            table_id=table["id"],
            org_id=fixture_org["org_id"],
            source_value="raw-1",
            canonical_value="Line A",
            identity="owner@example.com",
        )
        conn.commit()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT action FROM app.metric_semantics_audit "
                "WHERE org_id = %s ORDER BY created_at",
                (fixture_org["org_id"],),
            )
            actions = [row[0] for row in cur.fetchall()]
    assert actions == ["value_mapping_table.created", "value_mapping_entry.created"]


# ===========================================================================
# S4 -- l'apercu, et la seule propriete qui compte : il dit la MEME chose
# ===========================================================================


@pg_available
def test_the_preview_classifies_exactly_what_the_import_writes(fixture_org):
    """Le defaut qu'un apercu introduit : un SECOND avis sur le meme fichier.

    `unresolved-values.md` S4 demande un apercu avant l'ecriture. La facon dont
    un apercu se casse n'est pas de planter -- c'est de diverger : il annonce 12
    acceptees, l'import en ecrit 11, et personne ne le voit avant la troisieme
    fois. Ce test joue LE MEME texte contre les deux et compare les verdicts
    ligne par ligne, ce qui est la seule preuve qu'il n'y a qu'une classification.
    """
    from core.db import get_connection

    text = "\n".join(
        [
            "source_value,canonical_value",
            "FR - Paris,France",       # accepte
            "FR - Lyon,France",        # accepte
            "DE - Berlin",             # not_two_columns
            "IT - Rome,",              # empty_canonical_value
            "FR - Paris,France",       # duplique DANS le fichier
            "already,there",           # deja dans la table
        ]
    )
    with get_connection() as conn:
        table = store.create_table(
            conn,
            org_id=fixture_org["org_id"],
            project_id=fixture_org["project_id"],
            scope_level="PROJECT",
            name="S4 preview",
            description="S4 preview fixture",
            identity="owner@example.com",
        )
        store.add_entry(
            conn,
            table_id=table["id"],
            org_id=fixture_org["org_id"],
            source_value="already",
            canonical_value="Elsewhere",
            identity="owner@example.com",
        )
        conn.commit()

        preview = store.preview_import(
            conn, table_id=table["id"], org_id=fixture_org["org_id"], text=text
        )
        # L'APERCU N'A RIEN ECRIT -- la question posee avant l'import.
        assert len(store.list_entries(conn, table_id=table["id"])) == 1

        written = store.import_pairs(
            conn,
            table_id=table["id"],
            org_id=fixture_org["org_id"],
            text=text,
            identity="owner@example.com",
        )
        conn.commit()

    assert preview["would_import_count"] == written["imported_count"]
    assert preview["rejected_count"] == written["rejected_count"]
    # Ligne par ligne, motif par motif : deux comptes egaux peuvent recouvrir
    # deux classifications differentes.
    assert [(r["line"], r["reason"]) for r in preview["rejected"]] == [
        (r["line"], r["reason"]) for r in written["rejected"]
    ]
    assert [p["source_value"] for p in preview["would_import"]] == [
        e["source_value"] for e in written["imported"]
    ]


@pg_available
def test_the_preview_names_the_three_reasons_the_target_names(fixture_org):
    """S4 nomme trois motifs. Un quatrieme silencieux serait une ligne perdue."""
    from core.db import get_connection

    text = "\n".join(["a,b", "solo", "c,", "a,b"])
    with get_connection() as conn:
        table = store.create_table(
            conn,
            org_id=fixture_org["org_id"],
            project_id=fixture_org["project_id"],
            scope_level="PROJECT",
            name="S4 reasons",
            description="S4 preview fixture",
            identity="owner@example.com",
        )
        conn.commit()
        preview = store.preview_import(
            conn, table_id=table["id"], org_id=fixture_org["org_id"], text=text
        )
    reasons = {r["reason"] for r in preview["rejected"]}
    assert "not_two_columns" in reasons
    assert "empty_canonical_value" in reasons
    # Le doublon INTERNE au fichier est attrape par l'apercu comme par l'import.
    assert "already_present" in reasons or "duplicate_source_value" in reasons
    assert preview["would_import_count"] == 1


@pg_available
def test_a_preview_that_writes_nothing_carries_no_impact_rather_than_a_zero(fixture_org):
    """AD-9 : un fichier integralement rejete ne rend pas `0 Datastreams`."""
    from core.db import get_connection

    with get_connection() as conn:
        table = store.create_table(
            conn,
            org_id=fixture_org["org_id"],
            project_id=fixture_org["project_id"],
            scope_level="PROJECT",
            name="S4 empty",
            description="S4 preview fixture",
            identity="owner@example.com",
        )
        conn.commit()
        preview = store.preview_import(
            conn, table_id=table["id"], org_id=fixture_org["org_id"], text="solo\nalso"
        )
    assert preview["would_import_count"] == 0
    assert preview["impact"] is None


@pg_available
def test_the_preview_states_the_ceiling_instead_of_letting_the_click_discover_it(
    fixture_org,
):
    """`import_pairs` LEVE au-dela du plafond. Un apercu doit le DIRE avant."""
    from core.db import get_connection

    text = "\n".join(f"v{i},c{i}" for i in range(store.MAX_IMPORT_ROWS + 1))
    with get_connection() as conn:
        table = store.create_table(
            conn,
            org_id=fixture_org["org_id"],
            project_id=fixture_org["project_id"],
            scope_level="PROJECT",
            name="S4 ceiling",
            description="S4 preview fixture",
            identity="owner@example.com",
        )
        conn.commit()
        preview = store.preview_import(
            conn, table_id=table["id"], org_id=fixture_org["org_id"], text=text
        )
    assert preview["over_limit"] is True
    assert preview["limit"] == store.MAX_IMPORT_ROWS
