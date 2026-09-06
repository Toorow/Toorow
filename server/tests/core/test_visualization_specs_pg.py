"""Story 50.4 -- AC6, proved against a real PostgreSQL and never against a mock.

Every property below is enforced by a CHECK, a UNIQUE, a composite foreign key, a
trigger or an RLS policy in migration 156. A mocked cursor accepts all of them
happily, which is exactly how a schema promise becomes a comment.

TWO THINGS THIS FILE REFUSES TO DO.

  * It never SKIPS. Story 50.1's `test_query_results_constraints_pg.py` reuses
    whatever published Semantic View the database already carries and skips when
    there is none -- and a skip is not a pass. Every test here builds its whole
    chain inside its own transaction, so it runs on a freshly migrated empty
    database and proves what it claims.
  * It never runs as a superuser without saying so. A superuser bypasses RLS, so
    an isolation assertion under one passes vacuously. The RLS test asserts the
    connected role is ordinary and FAILS if it is not, rather than skipping into a
    green that means nothing.

Every test rolls back. Nothing is left behind, even on a disposable database.
"""

from __future__ import annotations

import pytest
from core.visualization_specs import (
    VISUALIZATION_SPEC_CONTRACT_VERSION,
    VISUALIZATION_SPEC_SCHEMA_VERSION,
    VisualizationNotFound,
    create_visualization_spec_version,
    load_visualization,
    load_visualization_spec_version,
    validate_visualization_spec,
)
from ulid import ULID

pytestmark = pytest.mark.usefixtures("live_postgres")

_HASH_A = "a" * 64
_HASH_B = "b" * 64


def _uid(prefix: str) -> str:
    return f"{prefix}_{ULID()}"


#: The presentation every fixture starts from: one measure by one dimension.
def bar_document(**overrides) -> dict:
    document = {
        "spec_contract_version": VISUALIZATION_SPEC_CONTRACT_VERSION,
        "schema_version": VISUALIZATION_SPEC_SCHEMA_VERSION,
        "family": "bar",
        "bindings": {"measure": ["clicks"], "dimension": ["channel"]},
    }
    document.update(overrides)
    return document


class Chain:
    """org -> project -> Semantic View -> published version -> Query Spec version.

    Seeded here in full, so this file runs on an empty database.
    """

    def __init__(self, conn, *, org_id: str | None = None):
        self.conn = conn
        self.org_id = org_id or _uid("org")
        self.project_id = _uid("proj")
        self.view_id = _uid("sv")
        self.view_version_id = _uid("svv")
        self.query_spec_id = _uid("qs")
        self.query_spec_version_id = _uid("qsv")

    #: What the Query Spec version SELECTED. `check_shape_compatibility` reads
    #: exactly this, so a binding outside it is an unknown member.
    SPEC = {
        "contract_version": "query-spec.v1",
        "measures": [{"id": "clicks", "version_id": "mv_1"}],
        "dimensions": [{"id": "channel", "version_id": "dv_1"}],
        "grain": "day",
        "comparison": "none",
        "row_limit": 1000,
    }

    def build(self, *, seed_org: bool = True) -> Chain:
        import json

        with self.conn.cursor() as cur:
            if seed_org:
                cur.execute(
                    "INSERT INTO app.organizations (id, name, slug, status, created_by) "
                    "VALUES (%s, 'Story 50.4 fixture', %s, 'active', 'test')",
                    (self.org_id, self.org_id.replace("_", "-")),
                )
            cur.execute(
                "INSERT INTO app.projects (id, org_id, name, slug, created_by) "
                "VALUES (%s, %s, 'Story 50.4 fixture', %s, 'test')",
                (self.project_id, self.org_id, self.project_id.replace("_", "-")),
            )
            cur.execute(
                "INSERT INTO app.semantic_views (id, project_id, name, created_by) "
                "VALUES (%s, %s, 'fixture_view', 'test')",
                (self.view_id, self.project_id),
            )
            cur.execute(
                """
                INSERT INTO app.semantic_view_versions
                    (id, view_id, project_id, version_number, status, name, label,
                     dependency_fingerprint, content_hash, created_by)
                VALUES (%s, %s, %s, 1, 'published', 'fixture_view', 'Fixture view',
                        %s, %s, 'test')
                """,
                (self.view_version_id, self.view_id, self.project_id, _HASH_A, _HASH_A),
            )
            cur.execute(
                "INSERT INTO app.query_specs (id, org_id, project_id, semantic_view_id, "
                "created_by) VALUES (%s, %s, %s, %s, 'test')",
                (self.query_spec_id, self.org_id, self.project_id, self.view_id),
            )
            cur.execute(
                """
                INSERT INTO app.query_spec_versions
                    (id, query_spec_id, org_id, project_id, version_number, semantic_view_id,
                     semantic_view_version_id, spec, content_hash, created_by)
                VALUES (%s, %s, %s, %s, 1, %s, %s, %s::jsonb, %s, 'test')
                """,
                (
                    self.query_spec_version_id,
                    self.query_spec_id,
                    self.org_id,
                    self.project_id,
                    self.view_id,
                    self.view_version_id,
                    json.dumps(self.SPEC),
                    _HASH_A,
                ),
            )
        return self

    def next_query_spec_version(self) -> str:
        """A genuine QUERY change: a second immutable Query Spec version."""
        import json

        version_id = _uid("qsv")
        spec = dict(self.SPEC)
        spec["dimensions"] = [
            {"id": "channel", "version_id": "dv_1"},
            {"id": "country", "version_id": "dv_2"},
        ]
        with self.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO app.query_spec_versions
                    (id, query_spec_id, org_id, project_id, version_number, semantic_view_id,
                     semantic_view_version_id, spec, content_hash, predecessor_version_id,
                     created_by)
                VALUES (%s, %s, %s, %s, 2, %s, %s, %s::jsonb, %s, %s, 'test')
                """,
                (
                    version_id,
                    self.query_spec_id,
                    self.org_id,
                    self.project_id,
                    self.view_id,
                    self.view_version_id,
                    json.dumps(spec),
                    _HASH_B,
                    self.query_spec_version_id,
                ),
            )
        return version_id

    def other_query_spec_version(self) -> tuple[str, str]:
        """A SECOND Query Spec in the same Project, with its own version 1.

        Not another version of the same Query Spec -- another QUESTION. Re-pinning
        a Visualization to this is the F1 defect: version N+1 would answer it while
        `app.visualizations.query_spec_id` still advertised the first one.
        """
        import json

        query_spec_id = _uid("qs")
        version_id = _uid("qsv")
        with self.conn.cursor() as cur:
            cur.execute(
                "INSERT INTO app.query_specs (id, org_id, project_id, semantic_view_id, "
                "created_by) VALUES (%s, %s, %s, %s, 'test')",
                (query_spec_id, self.org_id, self.project_id, self.view_id),
            )
            cur.execute(
                """
                INSERT INTO app.query_spec_versions
                    (id, query_spec_id, org_id, project_id, version_number, semantic_view_id,
                     semantic_view_version_id, spec, content_hash, created_by)
                VALUES (%s, %s, %s, %s, 1, %s, %s, %s::jsonb, %s, 'test')
                """,
                (
                    version_id,
                    query_spec_id,
                    self.org_id,
                    self.project_id,
                    self.view_id,
                    self.view_version_id,
                    json.dumps(self.SPEC),
                    _HASH_B,
                ),
            )
        return query_spec_id, version_id

    def visualization(self, document: dict | None = None) -> dict:
        validated = validate_visualization_spec(
            self.conn,
            project_id=self.project_id,
            query_spec_version_id=self.query_spec_version_id,
            payload=document or bar_document(),
        )
        return create_visualization_spec_version(
            self.conn,
            org_id=self.org_id,
            project_id=self.project_id,
            validated=validated,
            actor="test",
            name="Fixture visualization",
        )


@pytest.fixture()
def chain(live_postgres):
    built = Chain(live_postgres).build()
    yield built
    live_postgres.rollback()


# ---------------------------------------------------------------------------
# The migration actually landed, and landed with its guarantees.
# ---------------------------------------------------------------------------


def test_pg_the_two_tables_exist_with_forced_row_level_security(live_postgres):
    with live_postgres.cursor() as cur:
        cur.execute(
            """
            SELECT relname, relrowsecurity, relforcerowsecurity
            FROM pg_class WHERE relnamespace = 'app'::regnamespace
              AND relname IN ('visualizations', 'visualization_spec_versions')
            ORDER BY relname
            """
        )
        rows = cur.fetchall()
    assert len(rows) == 2, "migration 156 has not been applied to this database"
    for name, enabled, forced in rows:
        assert enabled, f"{name} has no row level security"
        # FORCE matters: without it the table owner is silently exempt, and a
        # deployment that connects as owner reads every Project.
        assert forced, f"{name} does not FORCE row level security"


def test_pg_the_version_table_reuses_migration_151s_immutability_function(live_postgres):
    """One immutability policy per epic. Two functions is two policies.

    THE QUESTION IS "WHICH FUNCTIONS CAN REFUSE A WRITE", not "which functions are
    attached". This used to read every non-internal trigger on the table, which
    made it fail the day migration 333 attached an AFTER INSERT trigger that
    refuses nothing -- it registers the new version in the pin registry the two
    presentation foreign keys judge. An AFTER INSERT trigger is not an immutability
    policy and cannot become one: it fires after the row is in, and it has no
    branch that raises. So the scope is narrowed to the guards -- BEFORE, on
    UPDATE, DELETE or TRUNCATE -- which is what the sentence above always meant.
    """
    with live_postgres.cursor() as cur:
        cur.execute(
            """
            SELECT p.proname
            FROM pg_trigger t JOIN pg_proc p ON p.oid = t.tgfoid
            WHERE t.tgrelid = 'app.visualization_spec_versions'::regclass
              AND NOT t.tgisinternal
              -- `pg_trigger.tgtype` bits, from PostgreSQL's own catalog:
              -- 1 ROW, 2 BEFORE, 4 INSERT, 8 DELETE, 16 UPDATE, 32 TRUNCATE.
              AND (t.tgtype & 2) <> 0
              AND (t.tgtype & (8 | 16 | 32)) <> 0
            """
        )
        functions = {r[0] for r in cur.fetchall()}
    assert functions == {"reject_analytical_evidence_mutation"}
    #  And the narrowing did not empty the question: something IS guarding.
    assert functions, "no BEFORE UPDATE/DELETE/TRUNCATE guard is attached at all"


# ---------------------------------------------------------------------------
# AC6 -- insert-once.
# ---------------------------------------------------------------------------


def test_pg_a_visualization_spec_version_cannot_be_updated(chain):
    created = chain.visualization()
    with chain.conn.cursor() as cur, pytest.raises(Exception, match="immutable"):
        cur.execute(
            "UPDATE app.visualization_spec_versions SET family = 'line' WHERE id = %s",
            (created["id"],),
        )
    chain.conn.rollback()


def test_pg_a_visualization_spec_version_cannot_be_deleted(chain):
    created = chain.visualization()
    with chain.conn.cursor() as cur, pytest.raises(Exception, match="immutable"):
        cur.execute("DELETE FROM app.visualization_spec_versions WHERE id = %s", (created["id"],))
    chain.conn.rollback()


@pytest.mark.pg_owner
def test_pg_the_version_table_cannot_be_truncated(chain):
    """TRUNCATE is statement-level: a row trigger never sees it, and a table whose
    rows are insert-once but whose contents can be emptied in one statement is not
    immutable. Every referring table is named because a foreign key would otherwise
    refuse first, which would leave the statement trigger untested.

    `app.renders` joined that list on 2026-07-31: migration 158 added
    `fk_renders_visualization_spec_version`, the project-scoped constraint that
    migration 154 had put inside a `DO` block which could only ever fire on a
    database where the table already existed -- so it never fired at all. With the
    constraint finally real, PostgreSQL refuses this TRUNCATE for the foreign key
    rather than for immutability, and the assertion below would pass on a schema
    whose trigger had been dropped. Naming `renders` is what keeps this test about
    the trigger.

    IT NEEDS THE OWNER, and it always did -- `pg_owner`, added 2026-08-31. TRUNCATE
    is its own table privilege, no migration in this repository GRANTs it
    (`grep -rn "GRANT.*TRUNCATE" infra/nango/migrations/*.sql` answers nothing),
    and migration 207's ALTER DEFAULT PRIVILEGES hands the application role SELECT,
    INSERT, UPDATE and DELETE only. So on a cluster built from the migrations and
    nothing else this test dies on 42501 -- permission denied -- before it ever
    reaches the statement trigger it exists to measure. It passed for months on a
    long-lived disposable cluster that had been widened by hand, and reddened the
    day one was rebuilt from zero (`disposable_postgres.py down` then `up`, 333
    migrations, 0 already applied). A refusal from the privilege layer is not a
    refusal from the trigger, and only one of the two is the invariant."""
    chain.visualization()
    with chain.conn.cursor() as cur:
        # The head's `current_version_id` foreign key is DEFERRABLE INITIALLY
        # DEFERRED, so its check is still queued here and PostgreSQL refuses to
        # TRUNCATE a table with pending trigger events. Flushing it first is what
        # lets the statement reach the immutability trigger under test.
        cur.execute("SET CONSTRAINTS ALL IMMEDIATE")
        # The referrers are COMPUTED, not listed. Naming them by hand made this
        # test break twice in one day as migrations 158 and 160 each added one,
        # and -- worse -- a hand-written list can go stale into a green: once
        # PostgreSQL refuses for a foreign key, the assertion below passes on a
        # schema whose immutability trigger has been dropped. Closing the
        # referrer set transitively is what keeps the trigger the only thing
        # left that can refuse.
        cur.execute(
            """
            WITH RECURSIVE referrer(oid) AS (
                SELECT c.oid FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
                 WHERE n.nspname = 'app'
                   AND c.relname IN ('visualizations', 'visualization_spec_versions')
                UNION
                SELECT con.conrelid FROM pg_constraint con
                  JOIN referrer r ON con.confrelid = r.oid
                 WHERE con.contype = 'f'
            )
            SELECT string_agg(format('%s.%s', n.nspname, c.relname), ', ')
              FROM referrer r JOIN pg_class c ON c.oid = r.oid
              JOIN pg_namespace n ON n.oid = c.relnamespace
            """
        )
        targets = cur.fetchone()[0]
        assert "visualization_spec_versions" in targets
        with pytest.raises(Exception, match="immutable"):
            cur.execute(f"TRUNCATE {targets}")
    chain.conn.rollback()


def test_pg_rgpd_erasure_is_the_single_exception(chain):
    """The same escape hatch migrations 098/099/150/151 use, and no other."""
    created = chain.visualization()
    with chain.conn.cursor() as cur:
        cur.execute(
            "UPDATE app.visualizations SET current_version_id = NULL WHERE id = %s",
            (created["visualization_id"],),
        )
        cur.execute("SET LOCAL app.rgpd_erasure = 'on'")
        cur.execute("DELETE FROM app.visualization_spec_versions WHERE id = %s", (created["id"],))
        cur.execute(
            "SELECT COUNT(*) FROM app.visualization_spec_versions WHERE id = %s", (created["id"],)
        )
        assert cur.fetchone()[0] == 0
    chain.conn.rollback()


# ---------------------------------------------------------------------------
# AC6 -- the Project boundary, enforced by composite foreign keys.
# ---------------------------------------------------------------------------


def test_pg_a_version_cannot_point_at_another_projects_query_spec_version(live_postgres):
    """Even if the application layer is wrong. This is the whole point of the
    composite key: `(query_spec_version_id, org_id, project_id)` has to resolve."""
    mine = Chain(live_postgres).build()
    theirs = Chain(live_postgres, org_id=mine.org_id).build(seed_org=False)
    created = mine.visualization()

    with live_postgres.cursor() as cur, pytest.raises(Exception) as exc:
        cur.execute(
            """
            INSERT INTO app.visualization_spec_versions
                (id, visualization_id, org_id, project_id, version_number, query_spec_id,
                 query_spec_version_id, spec_contract_version, schema_version, family, spec,
                 content_hash, predecessor_version_id, created_by)
            VALUES (%s, %s, %s, %s, 2, %s, %s, %s, 1, 'bar',
                    '{"spec_contract_version":"visualization-spec.v1","schema_version":1,
                      "family":"bar","accessibility":{"table_fallback":"required"}}'::jsonb,
                    %s, %s, 'test')
            """,
            (
                _uid("vsv"),
                created["visualization_id"],
                mine.org_id,
                mine.project_id,
                mine.query_spec_id,
                theirs.query_spec_version_id,
                VISUALIZATION_SPEC_CONTRACT_VERSION,
                _HASH_B,
                created["id"],
            ),
        )
    assert "fk_visualization_spec_versions_query_spec_version" in str(exc.value)
    live_postgres.rollback()


def test_pg_a_head_cannot_point_at_another_projects_query_spec(live_postgres):
    mine = Chain(live_postgres).build()
    theirs = Chain(live_postgres, org_id=mine.org_id).build(seed_org=False)
    with live_postgres.cursor() as cur, pytest.raises(Exception) as exc:
        cur.execute(
            "INSERT INTO app.visualizations (id, org_id, project_id, query_spec_id, created_by) "
            "VALUES (%s, %s, %s, %s, 'test')",
            (_uid("vis"), mine.org_id, mine.project_id, theirs.query_spec_id),
        )
    assert "fk_visualizations_query_spec" in str(exc.value)
    live_postgres.rollback()


# ---------------------------------------------------------------------------
# AC6 -- the database mirrors the outer shape, so psql meets the same contract.
# ---------------------------------------------------------------------------


def _insert_raw(
    chain: Chain,
    *,
    family: str = "bar",
    contract: str | None = None,
    schema_version: int = 1,
    spec: str | None = None,
):
    contract = contract or VISUALIZATION_SPEC_CONTRACT_VERSION
    spec = spec or (
        '{"spec_contract_version":"'
        + contract
        + '","schema_version":'
        + str(schema_version)
        + ',"family":"'
        + family
        + '","accessibility":{"table_fallback":"required"}}'
    )
    created = chain.visualization()
    with chain.conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.visualization_spec_versions
                (id, visualization_id, org_id, project_id, version_number, query_spec_id,
                 query_spec_version_id, spec_contract_version, schema_version, family, spec,
                 content_hash, predecessor_version_id, created_by)
            VALUES (%s, %s, %s, %s, 2, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, 'test')
            """,
            (
                _uid("vsv"),
                created["visualization_id"],
                chain.org_id,
                chain.project_id,
                chain.query_spec_id,
                chain.query_spec_version_id,
                contract,
                schema_version,
                family,
                spec,
                _HASH_B,
                created["id"],
            ),
        )


def test_pg_a_direct_insert_cannot_invent_an_eighth_family(chain):
    with pytest.raises(Exception) as exc:
        _insert_raw(chain, family="sankey")
    assert "ck_visualization_spec_versions_family" in str(exc.value)
    chain.conn.rollback()


def test_pg_forward_family_constraint_accepts_waterfall(chain):
    _insert_raw(chain, family="waterfall")
    with chain.conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM app.visualization_spec_versions WHERE family = 'waterfall'"
        )
        assert cur.fetchone()[0] == 1
    chain.conn.rollback()


def test_pg_a_direct_insert_cannot_claim_another_contract_version(chain):
    with pytest.raises(Exception) as exc:
        _insert_raw(chain, contract="visualization-spec.v2")
    assert "ck_visualization_spec_versions_contract" in str(exc.value)
    chain.conn.rollback()


def test_pg_a_direct_insert_cannot_switch_off_the_table_fallback(chain):
    """AC8 at the layer that cannot be argued with."""
    with pytest.raises(Exception) as exc:
        _insert_raw(
            chain,
            spec='{"spec_contract_version":"visualization-spec.v1","schema_version":1,'
            '"family":"bar","accessibility":{"table_fallback":"optional"}}',
        )
    assert "ck_visualization_spec_versions_document_pins" in str(exc.value)
    chain.conn.rollback()


def test_pg_a_direct_insert_cannot_disagree_with_its_own_document(chain):
    with pytest.raises(Exception) as exc:
        _insert_raw(
            chain,
            family="line",
            spec='{"spec_contract_version":"visualization-spec.v1","schema_version":1,'
            '"family":"bar","accessibility":{"table_fallback":"required"}}',
        )
    assert "ck_visualization_spec_versions_document_pins" in str(exc.value)
    chain.conn.rollback()


def test_pg_version_one_carries_no_predecessor_and_later_ones_must(chain):
    created = chain.visualization()
    assert created["version_number"] == 1
    with chain.conn.cursor() as cur:
        cur.execute(
            "SELECT predecessor_version_id FROM app.visualization_spec_versions WHERE id = %s",
            (created["id"],),
        )
        assert cur.fetchone()[0] is None
        with pytest.raises(Exception) as exc:
            cur.execute(
                """
                INSERT INTO app.visualization_spec_versions
                    (id, visualization_id, org_id, project_id, version_number, query_spec_id,
                     query_spec_version_id, spec_contract_version, schema_version, family, spec,
                     content_hash, created_by)
                VALUES (%s, %s, %s, %s, 2, %s, %s, %s, 1, 'bar',
                        '{"spec_contract_version":"visualization-spec.v1","schema_version":1,
                          "family":"bar","accessibility":{"table_fallback":"required"}}'::jsonb,
                        %s, 'test')
                """,
                (
                    _uid("vsv"),
                    created["visualization_id"],
                    chain.org_id,
                    chain.project_id,
                    chain.query_spec_id,
                    chain.query_spec_version_id,
                    VISUALIZATION_SPEC_CONTRACT_VERSION,
                    _HASH_B,
                ),
            )
    assert "ck_visualization_spec_versions_lineage" in str(exc.value)
    chain.conn.rollback()


def test_pg_an_oversized_document_is_refused_by_the_database(chain):
    created = chain.visualization()
    padding = "x" * 40_000
    with chain.conn.cursor() as cur, pytest.raises(Exception) as exc:
        cur.execute(
            """
            INSERT INTO app.visualization_spec_versions
                (id, visualization_id, org_id, project_id, version_number, query_spec_id,
                 query_spec_version_id, spec_contract_version, schema_version, family, spec,
                 content_hash, predecessor_version_id, created_by)
            VALUES (%s, %s, %s, %s, 2, %s, %s, %s, 1, 'bar',
                    jsonb_build_object(
                      'spec_contract_version', 'visualization-spec.v1',
                      'schema_version', 1, 'family', 'bar',
                      'accessibility', jsonb_build_object('table_fallback', 'required'),
                      'padding', %s::text),
                    %s, %s, 'test')
            """,
            (
                _uid("vsv"),
                created["visualization_id"],
                chain.org_id,
                chain.project_id,
                chain.query_spec_id,
                chain.query_spec_version_id,
                VISUALIZATION_SPEC_CONTRACT_VERSION,
                padding,
                _HASH_B,
                created["id"],
            ),
        )
    assert "ck_visualization_spec_versions_size" in str(exc.value)
    chain.conn.rollback()


# ---------------------------------------------------------------------------
# AC6 -- RLS, under the ORDINARY role. A superuser proves nothing here.
# ---------------------------------------------------------------------------


def test_pg_an_identity_without_a_project_grant_reads_nothing(chain):
    chain.visualization()
    with chain.conn.cursor() as cur:
        cur.execute("SELECT usesuper FROM pg_user WHERE usename = current_user")
        row = cur.fetchone()
    # A FAILURE, not a skip: a skip here would leave a green suite that never
    # tested isolation at all (`scripts/disposable_postgres.py:28-30`).
    assert row is not None and not row[0], (
        "connected as a superuser: row level security is not applied, so every "
        "assertion below would pass while proving nothing"
    )
    with chain.conn.cursor() as cur:
        cur.execute("SET LOCAL toorow.enforce_epic36 = 'on'")
        cur.execute(
            "SELECT COUNT(*) FROM app.visualizations WHERE project_id = %s", (chain.project_id,)
        )
        assert cur.fetchone()[0] == 0
        cur.execute(
            "SELECT COUNT(*) FROM app.visualization_spec_versions WHERE project_id = %s",
            (chain.project_id,),
        )
        assert cur.fetchone()[0] == 0
    chain.conn.rollback()


# ---------------------------------------------------------------------------
# AC6 -- foreign, denied and nonexistent are one answer.
# ---------------------------------------------------------------------------


def test_pg_foreign_denied_and_absent_ids_raise_the_same_exception(live_postgres):
    mine = Chain(live_postgres).build()
    theirs = Chain(live_postgres, org_id=mine.org_id).build(seed_org=False)
    foreign = theirs.visualization()

    for visualization_id in (foreign["visualization_id"], "vis_does_not_exist", ""):
        with pytest.raises(VisualizationNotFound):
            load_visualization(
                live_postgres,
                org_id=mine.org_id,
                project_id=mine.project_id,
                visualization_id=visualization_id,
            )
    for version_id in (foreign["id"], "vsv_does_not_exist", ""):
        with pytest.raises(VisualizationNotFound):
            load_visualization_spec_version(
                live_postgres,
                org_id=mine.org_id,
                project_id=mine.project_id,
                visualization_spec_version_id=version_id,
            )
    live_postgres.rollback()


def test_pg_a_foreign_query_spec_version_pin_is_not_found_not_refused(live_postgres):
    """The pin does not resolve inside this Project, so it answers exactly like an
    absent one. A distinguishable answer would be a tenant-enumeration oracle."""
    mine = Chain(live_postgres).build()
    theirs = Chain(live_postgres, org_id=mine.org_id).build(seed_org=False)
    with pytest.raises(VisualizationNotFound):
        validate_visualization_spec(
            live_postgres,
            project_id=mine.project_id,
            query_spec_version_id=theirs.query_spec_version_id,
            payload=bar_document(),
        )
    live_postgres.rollback()


# ---------------------------------------------------------------------------
# The round trip.
# ---------------------------------------------------------------------------


def test_pg_a_stored_version_reads_back_with_both_version_keys(chain):
    created = chain.visualization()
    stored = load_visualization_spec_version(
        chain.conn,
        org_id=chain.org_id,
        project_id=chain.project_id,
        visualization_spec_version_id=created["id"],
    )
    assert stored["spec_contract_version"] == VISUALIZATION_SPEC_CONTRACT_VERSION
    assert stored["schema_version"] == VISUALIZATION_SPEC_SCHEMA_VERSION
    assert stored["family"] == "bar"
    assert stored["spec"]["accessibility"]["table_fallback"] == "required"
    assert stored["proposed_by"] == "person"
    chain.conn.rollback()


def test_pg_the_head_advances_to_the_new_version(chain):
    created = chain.visualization()
    head = load_visualization(
        chain.conn,
        org_id=chain.org_id,
        project_id=chain.project_id,
        visualization_id=created["visualization_id"],
    )
    assert head["current_version_id"] == created["id"]
    assert [v["version_number"] for v in head["versions"]] == [1]
    chain.conn.rollback()


# ---------------------------------------------------------------------------
# F1 -- a version cannot name a Query Spec its head does not carry.
#
# Migration 164 widened the head foreign key to (visualization_id, query_spec_id,
# org_id, project_id) against a matching UNIQUE on `app.visualizations`. These
# tests bypass the Python service and write raw SQL, because that is the caller
# the database layer exists for: `server/core/visualization_specs.py` refuses the
# mismatch too, and a test that only exercised the service would leave a psql
# session, a repair script or a second service free to do it.
# ---------------------------------------------------------------------------


def test_pg_the_head_key_carries_the_query_spec(live_postgres):
    """The constraint is the widened one, read off the catalog rather than assumed."""
    with live_postgres.cursor() as cur:
        cur.execute(
            """
            SELECT pg_get_constraintdef(oid) FROM pg_constraint
            WHERE conrelid = 'app.visualization_spec_versions'::regclass
              AND conname = 'fk_visualization_spec_versions_head'
            """
        )
        row = cur.fetchone()
    assert row is not None, "migration 164 has not been applied to this database"
    definition = row[0]
    assert "(visualization_id, query_spec_id, org_id, project_id)" in definition
    assert "app.visualizations(id, query_spec_id, org_id, project_id)" in definition


def test_pg_a_raw_insert_naming_another_query_spec_is_refused_by_the_key(chain):
    """The proof the reviewer reproduced, made unarguable.

    Version 1 is written normally; the raw INSERT for version 2 names a DIFFERENT
    Query Spec. Before migration 164 the row landed, and the head went on
    advertising the first Query Spec to every consumer that resolves it.
    """
    import json

    import psycopg

    created = chain.visualization()
    other_query_spec_id, other_version_id = chain.other_query_spec_version()
    assert other_query_spec_id != chain.query_spec_id

    with pytest.raises(psycopg.errors.ForeignKeyViolation), chain.conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.visualization_spec_versions
                (id, visualization_id, org_id, project_id, version_number, query_spec_id,
                 query_spec_version_id, spec_contract_version, schema_version, family, spec,
                 content_hash, predecessor_version_id, created_by)
            VALUES (%s, %s, %s, %s, 2, %s, %s, %s, %s, 'bar', %s::jsonb, %s, %s, 'test')
            """,
            (
                _uid("vsv"),
                created["visualization_id"],
                chain.org_id,
                chain.project_id,
                other_query_spec_id,
                other_version_id,
                VISUALIZATION_SPEC_CONTRACT_VERSION,
                VISUALIZATION_SPEC_SCHEMA_VERSION,
                json.dumps(created["spec"]),
                "d" * 64,
                created["id"],
            ),
        )
    chain.conn.rollback()


def test_pg_a_raw_insert_naming_the_same_query_spec_still_lands(chain):
    """The mirror. The key refuses a DIFFERENT Query Spec; it must not refuse a
    second VERSION of the same one, which is AC5's legitimate re-pin."""
    import json

    created = chain.visualization()
    new_pin = chain.next_query_spec_version()
    version_id = _uid("vsv")
    with chain.conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.visualization_spec_versions
                (id, visualization_id, org_id, project_id, version_number, query_spec_id,
                 query_spec_version_id, spec_contract_version, schema_version, family, spec,
                 content_hash, predecessor_version_id, created_by)
            VALUES (%s, %s, %s, %s, 2, %s, %s, %s, %s, 'bar', %s::jsonb, %s, %s, 'test')
            """,
            (
                version_id,
                created["visualization_id"],
                chain.org_id,
                chain.project_id,
                chain.query_spec_id,
                new_pin,
                VISUALIZATION_SPEC_CONTRACT_VERSION,
                VISUALIZATION_SPEC_SCHEMA_VERSION,
                json.dumps(created["spec"]),
                "e" * 64,
                created["id"],
            ),
        )
        cur.execute(
            "SELECT query_spec_id, query_spec_version_id "
            "FROM app.visualization_spec_versions WHERE id = %s",
            (version_id,),
        )
        assert cur.fetchone() == (chain.query_spec_id, new_pin)
    chain.conn.rollback()


# ---------------------------------------------------------------------------
# F5 -- AC7's five rail facets are SERVED, or their absence carries its owner.
# ---------------------------------------------------------------------------


def test_pg_member_metadata_carries_all_five_facets_with_an_owner_each(chain):
    from core.visualization_specs import (
        MEMBER_METADATA_FACETS,
        MEMBER_METADATA_OWNERS,
        load_member_presentation_metadata,
    )

    metadata = load_member_presentation_metadata(
        chain.conn,
        project_id=chain.project_id,
        members=[{"id": "clicks", "version_id": "mv_1"}, {"id": "channel", "version_id": "dv_1"}],
    )
    assert set(metadata) == {"clicks", "channel"}
    for member_id, facets in metadata.items():
        assert tuple(facets) == MEMBER_METADATA_FACETS, member_id
        for facet, body in facets.items():
            assert set(body) == {"value", "owner"}
            # Every facet names its owning surface whether or not it has a value.
            # An unowned absence is exactly the silence AC7 forbids.
            assert body["owner"] == MEMBER_METADATA_OWNERS[facet]
    chain.conn.rollback()


def test_pg_member_metadata_is_read_from_the_pinned_concept_version(chain):
    """Served, not invented: the facets come from the row the member's
    `version_id` names, and a member whose concept version does not exist gets
    five owned absences rather than a plausible sentence."""
    from core.visualization_specs import load_member_presentation_metadata

    concept_id = _uid("sc")
    version_id = _uid("scv")
    with chain.conn.cursor() as cur:
        cur.execute(
            "INSERT INTO app.semantic_concepts (id, project_id, kind, name, created_by) "
            "VALUES (%s, %s, 'metric', 'fixture_clicks', 'test')",
            (concept_id, chain.project_id),
        )
        cur.execute(
            """
            INSERT INTO app.semantic_concept_versions
                (id, concept_id, project_id, version_number, status, kind, name, label,
                 definition, value_type, expression, aggregation, additivity_class,
                 allowed_grains, provenance, content_hash, created_by)
            VALUES (%s, %s, %s, 1, 'published', 'metric', 'fixture_clicks',
                    'Fixture clicks', 'Clicks recorded on a governed placement.',
                    'integer', '{"kind": "column", "column": "clicks"}'::jsonb,
                    '{"function": "sum"}'::jsonb, 'additive',
                    ARRAY['day', 'week'], %s::jsonb, %s, 'test')
            """,
            (version_id, concept_id, chain.project_id, '{"recorded_by": "fixture"}', _HASH_A),
        )

    metadata = load_member_presentation_metadata(
        chain.conn,
        project_id=chain.project_id,
        members=[
            {"id": concept_id, "version_id": version_id},
            {"id": "not_a_concept", "version_id": "scv_absent"},
        ],
    )
    served = metadata[concept_id]
    assert served["definition"]["value"] == "Clicks recorded on a governed placement."
    assert served["grain"]["value"] == "day, week"
    assert served["additivity"]["value"] == "additive"
    assert served["provenance_hint"]["value"] == "recorded_by: fixture"
    # No Data Quality monitor targets it, so the state is an owned absence rather
    # than the word "healthy".
    assert served["quality_state"]["value"] is None
    assert served["quality_state"]["owner"] == "Data Quality monitors (Epic 33)"

    absent = metadata["not_a_concept"]
    assert [absent[facet]["value"] for facet in absent] == [None, None, None, None, None]
    for body in absent.values():
        assert body["owner"], "an absence without an owner is the silence AC7 forbids"
    chain.conn.rollback()


def test_pg_the_quality_state_reports_the_worst_monitor_not_the_first(chain):
    """One healthy monitor never cancels a failing one."""
    from core.visualization_specs import load_member_presentation_metadata

    concept_id = _uid("sc")
    with chain.conn.cursor() as cur:
        cur.execute(
            "INSERT INTO app.semantic_concepts (id, project_id, kind, name, created_by) "
            "VALUES (%s, %s, 'metric', 'fixture_clicks', 'test')",
            (concept_id, chain.project_id),
        )
        for state in ("healthy", "failing"):
            cur.execute(
                """
                INSERT INTO app.dq_monitors
                    (id, org_id, project_id, name, label, target_kind, target_id,
                     lifecycle_status, runtime_state, created_by)
                VALUES (%s, %s, %s, %s, %s, 'semantic_concept', %s, 'published', %s, 'test')
                """,
                (
                    f"dqm_{ULID()}",
                    chain.org_id,
                    chain.project_id,
                    f"fixture_{state}",
                    f"Fixture {state}",
                    concept_id,
                    state,
                ),
            )

    metadata = load_member_presentation_metadata(
        chain.conn,
        project_id=chain.project_id,
        members=[{"id": concept_id, "version_id": "scv_absent"}],
    )
    assert metadata[concept_id]["quality_state"]["value"] == "failing"
    chain.conn.rollback()
