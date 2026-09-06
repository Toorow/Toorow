"""Story 50.3 -- the lifecycle invariants, proved against a real PostgreSQL.

WHY THESE CANNOT BE UNIT TESTS. Every property below is enforced by a CHECK, a
UNIQUE, a composite foreign key or a trigger in migration 154. A mocked cursor
accepts all of them happily, which is exactly how a schema promise becomes a
comment. The invariant this story turns on -- rerun, save and refresh never
overwrite prior evidence -- is a database property or it is nothing.

WHY THIS FILE SEEDS ITS OWN SEMANTIC VIEW. Story 50.1's `test_query_results_
constraints_pg.py` reuses whatever published Semantic View version the database
already carries and SKIPS when there is none. On a freshly migrated disposable
PostgreSQL there is none, so that file skips -- and a skip is not a pass. Every
test here builds its whole chain (org -> project -> Semantic View -> published
version -> compiled artifact -> Query Spec version -> attempt -> Result) inside
its own transaction, so it runs on an empty database and proves what it claims.

Every test rolls back. Nothing is left behind even on a disposable database.
"""

from __future__ import annotations

import json

import pytest
from ulid import ULID

pytestmark = pytest.mark.usefixtures("live_postgres")

_HASH_A = "a" * 64
_HASH_B = "b" * 64


def _uid(prefix: str) -> str:
    """A prefixed ULID. Several tables CHECK that shape (migration 142), so a
    plain uuid hex would fail the fixture rather than the property under test."""
    return f"{prefix}_{ULID()}"


class Chain:
    """The identity chain a Report/Notebook/Render needs, all of it seeded here."""

    def __init__(self, conn):
        self.conn = conn
        self.org_id = _uid("org")
        self.project_id = _uid("proj")
        self.view_id = _uid("sv")
        self.view_version_id = _uid("svv")
        self.query_spec_id = _uid("qs")
        self.query_spec_version_id = _uid("qsv")
        self.attempt_id = _uid("qea")
        self.result_id = _uid("qr")

    def build(self) -> Chain:
        with self.conn.cursor() as cur:
            cur.execute(
                "INSERT INTO app.organizations (id, name, slug, status, created_by) "
                "VALUES (%s, 'Story 50.3 fixture', %s, 'active', 'test')",
                (self.org_id, self.org_id.replace("_", "-")),
            )
            cur.execute(
                "INSERT INTO app.projects (id, org_id, name, slug, created_by) "
                "VALUES (%s, %s, 'Story 50.3 fixture', %s, 'test')",
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
                """
                INSERT INTO app.semantic_compiled_artifacts
                    (id, project_id, view_version_id, compiler_version, content_hash,
                     queryability_matrix, ossie_projection, ossie_spec_version,
                     toorow_extension_version)
                VALUES (%s, %s, %s, 'test', %s, '{}'::jsonb, '{}'::jsonb, '1', '1')
                """,
                (_uid("sca"), self.project_id, self.view_version_id, _HASH_A),
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
                VALUES (%s, %s, %s, %s, 1, %s, %s, '{}'::jsonb, %s, 'test')
                """,
                (
                    self.query_spec_version_id, self.query_spec_id, self.org_id,
                    self.project_id, self.view_id, self.view_version_id, _HASH_A,
                ),
            )
            cur.execute(
                "INSERT INTO app.query_execution_attempts (id, org_id, project_id, "
                "query_spec_version_id, result_id, requested_by) VALUES (%s,%s,%s,%s,%s,'test')",
                (
                    self.attempt_id, self.org_id, self.project_id,
                    self.query_spec_version_id, self.result_id,
                ),
            )
            cur.execute(
                """
                INSERT INTO app.query_results
                    (id, org_id, project_id, attempt_id, query_spec_version_id, outcome,
                     ai_path_absent_literal, content_hash, started_at, ended_at)
                VALUES (%s, %s, %s, %s, %s, 'success', 'No AI path', %s, NOW(), NOW())
                """,
                (
                    self.result_id, self.org_id, self.project_id, self.attempt_id,
                    self.query_spec_version_id, _HASH_B,
                ),
            )
        return self

    def report(self, *, report_id: str | None = None, version_id: str | None = None) -> tuple:
        """A Report head plus its version 1, with no accepted presentation contract."""
        report_id = report_id or _uid("rep")
        version_id = version_id or _uid("repv")
        with self.conn.cursor() as cur:
            cur.execute(
                "INSERT INTO app.analysis_reports (id, org_id, project_id, label, created_by) "
                "VALUES (%s, %s, %s, 'Fixture report', 'test')",
                (report_id, self.org_id, self.project_id),
            )
            cur.execute(
                """
                INSERT INTO app.analysis_report_versions
                    (id, report_id, org_id, project_id, version_number, label, query_spec_id,
                     query_spec_version_id, presentation_absent_literal, content_hash, created_by)
                VALUES (%s, %s, %s, %s, 1, 'Fixture report', %s, %s,
                        'No accepted presentation contract', %s, 'test')
                """,
                (
                    version_id, report_id, self.org_id, self.project_id,
                    self.query_spec_id, self.query_spec_version_id, _HASH_A,
                ),
            )
            cur.execute(
                "UPDATE app.analysis_reports SET current_version_id = %s WHERE id = %s",
                (version_id, report_id),
            )
        return report_id, version_id

    def notebook(self) -> tuple:
        notebook_id, version_id = _uid("nbk"), _uid("nbkv")
        with self.conn.cursor() as cur:
            cur.execute(
                "INSERT INTO app.analysis_notebooks (id, org_id, project_id, label, created_by) "
                "VALUES (%s, %s, %s, 'Fixture notebook', 'test')",
                (notebook_id, self.org_id, self.project_id),
            )
            cur.execute(
                """
                INSERT INTO app.analysis_notebook_versions
                    (id, notebook_id, org_id, project_id, version_number, label, content_hash,
                     created_by)
                VALUES (%s, %s, %s, %s, 1, 'Fixture notebook', %s, 'test')
                """,
                (version_id, notebook_id, self.org_id, self.project_id, _HASH_A),
            )
            cur.execute(
                "UPDATE app.analysis_notebooks SET current_version_id = %s WHERE id = %s",
                (version_id, notebook_id),
            )
        return notebook_id, version_id


@pytest.fixture()
def chain(live_postgres):
    built = Chain(live_postgres).build()
    yield built
    live_postgres.rollback()


# ---------------------------------------------------------------------------
# The invariant this story turns on: nothing overwrites prior evidence (AC11).
# ---------------------------------------------------------------------------


def test_a_report_version_cannot_be_updated(chain):
    """AC11: editing a Report creates a version. It never rewrites one."""
    _report_id, version_id = chain.report()
    with chain.conn.cursor() as cur, pytest.raises(Exception, match="immutable"):
        cur.execute(
            "UPDATE app.analysis_report_versions SET label = 'rewritten' WHERE id = %s",
            (version_id,),
        )
    chain.conn.rollback()


def test_a_report_version_cannot_be_deleted(chain):
    _report_id, version_id = chain.report()
    with chain.conn.cursor() as cur, pytest.raises(Exception, match="immutable"):
        cur.execute("DELETE FROM app.analysis_report_versions WHERE id = %s", (version_id,))
    chain.conn.rollback()


@pytest.mark.parametrize(
    "table", ["analysis_report_versions", "analysis_notebook_run_blocks", "renders"]
)
def test_evidence_tables_cannot_be_truncated(live_postgres, table):
    """No `chain` fixture here on purpose.

    A seeded transaction leaves deferred foreign-key trigger events pending, and
    PostgreSQL refuses TRUNCATE for THAT reason before the trigger is reached --
    which would make this test pass while proving nothing. Run against the table
    as it stands, CASCADE so the foreign key is not the refusal either, and the
    only thing left that can refuse is the trigger.
    """
    with live_postgres.cursor() as cur, pytest.raises(Exception, match="truncated"):
        cur.execute(f"TRUNCATE app.{table} CASCADE")
    live_postgres.rollback()


def test_a_report_run_reference_is_insert_once(chain):
    """AC3: refresh creates a new Result. It never updates a prior run reference."""
    report_id, version_id = chain.report()
    run_id = _uid("reprun")
    with chain.conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.analysis_report_runs
                (id, report_id, report_version_id, org_id, project_id, query_spec_version_id,
                 result_id, outcome, actor, started_at, ended_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, 'success', 'test', NOW(), NOW())
            """,
            (
                run_id, report_id, version_id, chain.org_id, chain.project_id,
                chain.query_spec_version_id, chain.result_id,
            ),
        )
    with chain.conn.cursor() as cur, pytest.raises(Exception, match="immutable"):
        cur.execute(
            "UPDATE app.analysis_report_runs SET outcome = 'empty' WHERE id = %s", (run_id,)
        )
    chain.conn.rollback()


def test_a_stable_head_cannot_walk_its_pointer_backwards(chain):
    """AC11: no action silently rebinds a prior version to `current`."""
    report_id, first = chain.report()
    second = _uid("repv")
    with chain.conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.analysis_report_versions
                (id, report_id, org_id, project_id, version_number, label, query_spec_id,
                 query_spec_version_id, presentation_absent_literal, content_hash,
                 predecessor_version_id, created_by)
            VALUES (%s, %s, %s, %s, 2, 'v2', %s, %s,
                    'No accepted presentation contract', %s, %s, 'test')
            """,
            (
                second, report_id, chain.org_id, chain.project_id, chain.query_spec_id,
                chain.query_spec_version_id, _HASH_B, first,
            ),
        )
        cur.execute(
            "UPDATE app.analysis_reports SET current_version_id = %s WHERE id = %s",
            (second, report_id),
        )
    # Forward worked. Backward must not.
    with chain.conn.cursor() as cur, pytest.raises(Exception, match="NEWER version"):
        cur.execute(
            "UPDATE app.analysis_reports SET current_version_id = %s WHERE id = %s",
            (first, report_id),
        )
    chain.conn.rollback()


def test_a_report_head_cannot_be_deleted_only_archived(chain):
    report_id, _version = chain.report()
    with chain.conn.cursor() as cur, pytest.raises(Exception, match="archive"):
        cur.execute("DELETE FROM app.analysis_reports WHERE id = %s", (report_id,))
    chain.conn.rollback()


def test_version_two_must_name_its_predecessor(chain):
    """A revision that loses its lineage is indistinguishable from a fresh intent."""
    report_id, _first = chain.report()
    with chain.conn.cursor() as cur, pytest.raises(Exception, match="lineage"):
        cur.execute(
            """
            INSERT INTO app.analysis_report_versions
                (id, report_id, org_id, project_id, version_number, label, query_spec_id,
                 query_spec_version_id, presentation_absent_literal, content_hash, created_by)
            VALUES (%s, %s, %s, %s, 2, 'orphan', %s, %s,
                    'No accepted presentation contract', %s, 'test')
            """,
            (
                _uid("repv"), report_id, chain.org_id, chain.project_id, chain.query_spec_id,
                chain.query_spec_version_id, _HASH_B,
            ),
        )
    chain.conn.rollback()


# ---------------------------------------------------------------------------
# The presentation contract has exactly two honest shapes (AC2).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "literal", [None, "deferred", "legacy", "", "no accepted presentation contract"]
)
def test_a_report_version_states_its_presentation_or_the_exact_literal(chain, literal):
    """No third state: not null, not `deferred`, not a differently worded literal."""
    report_id, _version = chain.report()
    with chain.conn.cursor() as cur, pytest.raises(Exception, match="presentation_is_honest"):
        cur.execute(
            """
            INSERT INTO app.analysis_report_versions
                (id, report_id, org_id, project_id, version_number, label, query_spec_id,
                 query_spec_version_id, presentation_absent_literal, content_hash,
                 predecessor_version_id, created_by)
            VALUES (%s, %s, %s, %s, 2, 'v2', %s, %s, %s, %s,
                    (SELECT current_version_id FROM app.analysis_reports WHERE id = %s), 'test')
            """,
            (
                _uid("repv"), report_id, chain.org_id, chain.project_id, chain.query_spec_id,
                chain.query_spec_version_id, literal, _HASH_B, report_id,
            ),
        )
    chain.conn.rollback()


@pytest.mark.parametrize("placeholder", ["current", "latest", "deferred", "legacy"])
def test_a_presentation_pin_may_not_be_a_placeholder_word(chain, placeholder):
    report_id, _version = chain.report()
    with chain.conn.cursor() as cur, pytest.raises(Exception, match="presentation_is_honest"):
        cur.execute(
            """
            INSERT INTO app.analysis_report_versions
                (id, report_id, org_id, project_id, version_number, label, query_spec_id,
                 query_spec_version_id, presentation_kind, presentation_version_id,
                 content_hash, predecessor_version_id, created_by)
            VALUES (%s, %s, %s, %s, 2, 'v2', %s, %s, 'visualization_spec_version', %s, %s,
                    (SELECT current_version_id FROM app.analysis_reports WHERE id = %s), 'test')
            """,
            (
                _uid("repv"), report_id, chain.org_id, chain.project_id, chain.query_spec_id,
                chain.query_spec_version_id, placeholder, _HASH_B, report_id,
            ),
        )
    chain.conn.rollback()


# ---------------------------------------------------------------------------
# Notebook Runs (AC6, AC7).
# ---------------------------------------------------------------------------


def _accept_run(chain, notebook_id, version_id, key="k1"):
    run_id = _uid("nbkrun")
    with chain.conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.analysis_notebook_runs
                (id, notebook_id, notebook_version_id, org_id, project_id, idempotency_key,
                 dispatch_source, actor)
            VALUES (%s, %s, %s, %s, %s, %s, 'manual', 'test')
            """,
            (run_id, notebook_id, version_id, chain.org_id, chain.project_id, key),
        )
    return run_id


def test_one_idempotency_key_admits_exactly_one_run(chain):
    """AC7: retry returns the original Run rather than duplicating it blindly."""
    notebook_id, version_id = chain.notebook()
    _accept_run(chain, notebook_id, version_id, key="due-2026-07-31")
    with chain.conn.cursor() as cur, pytest.raises(Exception):
        cur.execute(
            """
            INSERT INTO app.analysis_notebook_runs
                (id, notebook_id, notebook_version_id, org_id, project_id, idempotency_key,
                 dispatch_source, actor)
            VALUES (%s, %s, %s, %s, %s, 'due-2026-07-31', 'scheduled', 'test')
            """,
            (_uid("nbkrun"), notebook_id, version_id, chain.org_id, chain.project_id),
        )
    chain.conn.rollback()


def test_a_run_cannot_be_rebound_to_another_notebook_version(chain):
    """AC7: editing the Notebook does not rewrite a previous Run."""
    notebook_id, version_id = chain.notebook()
    run_id = _accept_run(chain, notebook_id, version_id)
    second = _uid("nbkv")
    with chain.conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.analysis_notebook_versions
                (id, notebook_id, org_id, project_id, version_number, label, content_hash,
                 predecessor_version_id, created_by)
            VALUES (%s, %s, %s, %s, 2, 'v2', %s, %s, 'test')
            """,
            (second, notebook_id, chain.org_id, chain.project_id, _HASH_B, version_id),
        )
    with chain.conn.cursor() as cur, pytest.raises(Exception, match="rebound"):
        cur.execute(
            "UPDATE app.analysis_notebook_runs SET notebook_version_id = %s WHERE id = %s",
            (second, run_id),
        )
    chain.conn.rollback()


def test_a_terminal_run_cannot_be_reopened(chain):
    notebook_id, version_id = chain.notebook()
    run_id = _accept_run(chain, notebook_id, version_id)
    with chain.conn.cursor() as cur:
        cur.execute(
            "UPDATE app.analysis_notebook_runs SET state='terminal', outcome='succeeded', "
            "terminal_at=NOW() WHERE id = %s",
            (run_id,),
        )
    with chain.conn.cursor() as cur, pytest.raises(Exception, match="reopened"):
        cur.execute(
            "UPDATE app.analysis_notebook_runs SET state='running' WHERE id = %s", (run_id,)
        )
    chain.conn.rollback()


def test_a_terminal_run_must_state_an_outcome(chain):
    notebook_id, version_id = chain.notebook()
    run_id = _accept_run(chain, notebook_id, version_id)
    with chain.conn.cursor() as cur, pytest.raises(Exception, match="terminal_is_complete"):
        cur.execute(
            "UPDATE app.analysis_notebook_runs SET state='terminal', terminal_at=NOW() "
            "WHERE id = %s",
            (run_id,),
        )
    chain.conn.rollback()


@pytest.mark.parametrize("literal", [None, "deferred", "latest", "no render"])
def test_a_run_block_names_a_render_or_the_exact_literal(chain, literal):
    """AC6: no `envelope_ref='deferred'`, no null standing in for a missing Render."""
    notebook_id, version_id = chain.notebook()
    run_id = _accept_run(chain, notebook_id, version_id)
    with chain.conn.cursor() as cur, pytest.raises(Exception, match="render_is_honest"):
        cur.execute(
            """
            INSERT INTO app.analysis_notebook_run_blocks
                (id, run_id, org_id, project_id, block_key, position, block_type,
                 query_spec_version_id, result_id, render_absent_literal, status,
                 started_at, ended_at)
            VALUES (%s, %s, %s, %s, 'b1', 1, 'query', %s, %s, %s, 'succeeded', NOW(), NOW())
            """,
            (
                _uid("nbkrb"), run_id, chain.org_id, chain.project_id,
                chain.query_spec_version_id, chain.result_id, literal,
            ),
        )
    chain.conn.rollback()


def test_a_run_block_is_insert_once(chain):
    notebook_id, version_id = chain.notebook()
    run_id = _accept_run(chain, notebook_id, version_id)
    block_id = _uid("nbkrb")
    with chain.conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.analysis_notebook_run_blocks
                (id, run_id, org_id, project_id, block_key, position, block_type,
                 query_spec_version_id, result_id, render_absent_literal, status,
                 started_at, ended_at)
            VALUES (%s, %s, %s, %s, 'b1', 1, 'query', %s, %s, 'No Render', 'succeeded',
                    NOW(), NOW())
            """,
            (
                block_id, run_id, chain.org_id, chain.project_id,
                chain.query_spec_version_id, chain.result_id,
            ),
        )
    with chain.conn.cursor() as cur, pytest.raises(Exception, match="immutable"):
        cur.execute(
            "UPDATE app.analysis_notebook_run_blocks SET status='failed' WHERE id = %s",
            (block_id,),
        )
    chain.conn.rollback()


def test_a_failed_block_may_not_point_at_an_older_result(chain):
    """A block that failed names no Result. Pointing at one would fabricate an answer."""
    notebook_id, version_id = chain.notebook()
    run_id = _accept_run(chain, notebook_id, version_id)
    with chain.conn.cursor() as cur, pytest.raises(Exception, match="result_matches_status"):
        cur.execute(
            """
            INSERT INTO app.analysis_notebook_run_blocks
                (id, run_id, org_id, project_id, block_key, position, block_type,
                 query_spec_version_id, result_id, render_absent_literal, status, limitation,
                 started_at, ended_at)
            VALUES (%s, %s, %s, %s, 'b1', 1, 'query', %s, %s, 'No Render: block failed',
                    'failed', 'boom', NOW(), NOW())
            """,
            (
                _uid("nbkrb"), run_id, chain.org_id, chain.project_id,
                chain.query_spec_version_id, chain.result_id,
            ),
        )
    chain.conn.rollback()


def test_a_narrative_block_may_not_claim_it_renders(chain):
    notebook_id, version_id = chain.notebook()
    with chain.conn.cursor() as cur, pytest.raises(Exception, match="narrative_does_not_render"):
        cur.execute(
            """
            INSERT INTO app.analysis_notebook_version_blocks
                (id, notebook_version_id, notebook_id, org_id, project_id, block_key, position,
                 block_type, presentation_absent_literal, renders, content_hash)
            VALUES (%s, %s, %s, %s, %s, 'intro', 1, 'narrative',
                    'No accepted presentation contract', TRUE, %s)
            """,
            (
                _uid("nbkb"), version_id, notebook_id, chain.org_id, chain.project_id, _HASH_A,
            ),
        )
    chain.conn.rollback()


def test_a_query_block_may_not_also_pin_a_report_version(chain):
    notebook_id, version_id = chain.notebook()
    _report_id, report_version_id = chain.report()
    with chain.conn.cursor() as cur, pytest.raises(Exception, match="blocks_input"):
        cur.execute(
            """
            INSERT INTO app.analysis_notebook_version_blocks
                (id, notebook_version_id, notebook_id, org_id, project_id, block_key, position,
                 block_type, query_spec_version_id, report_version_id,
                 presentation_absent_literal, renders, content_hash)
            VALUES (%s, %s, %s, %s, %s, 'both', 1, 'query', %s, %s,
                    'No accepted presentation contract', FALSE, %s)
            """,
            (
                _uid("nbkb"), version_id, notebook_id, chain.org_id, chain.project_id,
                chain.query_spec_version_id, report_version_id, _HASH_A,
            ),
        )
    chain.conn.rollback()


# ---------------------------------------------------------------------------
# The canonical Render (AC8).
# ---------------------------------------------------------------------------


_COMPLETE_RENDER_PINS = {
    "renderer_adapter": "echarts",
    "renderer_build_id": "rb_fixture",
    "runtime_build_id": "rt_fixture",
    "theme_version": "theme_fixture",
    "formatter_version": "fmt_fixture",
    "responsive_profile": "desktop_wide",
}


def _visualization_spec_version(chain) -> str:
    """Seed one real Visualization Spec version (Story 50.4, migration 156).

    It used to be the string `vsv_fixture`, which was enough while `app.renders`
    had no foreign key to that table -- and it was enough only because the foreign
    key migration 154 CLAIMED to add was never created. Migration 158 adds it, so
    the fixture now has to seed the row it points at, and that is the point: the
    pin is a reference, not a shape.
    """
    visualization_id, version_id = _uid("viz"), _uid("vsv")
    with chain.conn.cursor() as cur:
        cur.execute(
            "INSERT INTO app.visualizations (id, org_id, project_id, query_spec_id, "
            "name, created_by) VALUES (%s, %s, %s, %s, 'Fixture visualization', 'test')",
            (visualization_id, chain.org_id, chain.project_id, chain.query_spec_id),
        )
        cur.execute(
            """
            INSERT INTO app.visualization_spec_versions
                (id, visualization_id, org_id, project_id, version_number, query_spec_id,
                 query_spec_version_id, spec_contract_version, schema_version, family,
                 spec, content_hash, created_by)
            VALUES (%s, %s, %s, %s, 1, %s, %s, 'visualization-spec.v1', 1, 'table',
                    %s::jsonb, %s, 'test')
            """,
            (
                version_id, visualization_id, chain.org_id, chain.project_id,
                chain.query_spec_id, chain.query_spec_version_id,
                '{"spec_contract_version": "visualization-spec.v1", "schema_version": 1,'
                ' "family": "table", "accessibility": {"table_fallback": "required"}}',
                _HASH_A,
            ),
        )
    return version_id


_RENDER_INSERT = """
            INSERT INTO app.renders
                (id, org_id, project_id, result_id, result_content_hash,
                 visualization_spec_version_id, renderer_adapter, renderer_build_id,
                 runtime_build_id, theme_version, formatter_version, responsive_profile,
                 display_state, evidence_manifest, creation_surface, origin_kind,
                 content_hash, created_by)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s::jsonb, %s::jsonb, 'explore', 'explore', %s, 'test')
"""


def _render_row(chain, *, visualization_spec_version_id: str, **overrides) -> tuple:
    """The parameter tuple for `_RENDER_INSERT`, with every pin complete."""
    pins = {**_COMPLETE_RENDER_PINS, **overrides}
    return (
        _uid("rnd"), chain.org_id, chain.project_id, chain.result_id, _HASH_B,
        visualization_spec_version_id, pins["renderer_adapter"],
        pins["renderer_build_id"], pins["runtime_build_id"], pins["theme_version"],
        pins["formatter_version"], pins["responsive_profile"],
        overrides.get("display_state", "{}"),
        overrides.get("evidence_manifest", '{"semantic_view_version_id": "svv_x"}'),
        _HASH_A,
    )


def _insert_render(chain, **overrides):
    pins = {**_COMPLETE_RENDER_PINS, **overrides}
    spec_version_id = overrides.get(
        "visualization_spec_version_id", _visualization_spec_version(chain)
    )
    render_id = _uid("rnd")
    with chain.conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.renders
                (id, org_id, project_id, result_id, result_content_hash,
                 visualization_spec_version_id, renderer_adapter, renderer_build_id,
                 runtime_build_id, theme_version, formatter_version, responsive_profile,
                 display_state, evidence_manifest, creation_surface, origin_kind,
                 content_hash, created_by)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s::jsonb, %s::jsonb, 'explore', 'explore', %s, 'test')
            """,
            (
                render_id, chain.org_id, chain.project_id, chain.result_id, _HASH_B,
                spec_version_id, pins["renderer_adapter"],
                pins["renderer_build_id"], pins["runtime_build_id"], pins["theme_version"],
                pins["formatter_version"], pins["responsive_profile"],
                overrides.get("display_state", "{}"),
                overrides.get("evidence_manifest", '{"semantic_view_version_id": "svv_x"}'),
                _HASH_A,
            ),
        )
    return render_id


def test_a_complete_render_is_accepted_and_then_frozen(chain):
    """The positive case, so the negatives below prove a CHECK and not a typo."""
    render_id = _insert_render(chain)
    with chain.conn.cursor() as cur, pytest.raises(Exception, match="immutable"):
        cur.execute(
            "UPDATE app.renders SET responsive_profile = 'mobile' WHERE id = %s", (render_id,)
        )
    chain.conn.rollback()


def test_a_render_cannot_be_deleted(chain):
    render_id = _insert_render(chain)
    with chain.conn.cursor() as cur, pytest.raises(Exception, match="immutable"):
        cur.execute("DELETE FROM app.renders WHERE id = %s", (render_id,))
    chain.conn.rollback()


@pytest.mark.parametrize(
    "pin",
    [
        "visualization_spec_version_id",
        "renderer_adapter",
        "renderer_build_id",
        "runtime_build_id",
        "theme_version",
        "formatter_version",
        "responsive_profile",
    ],
)
@pytest.mark.parametrize("placeholder", ["current", "latest", "deferred", "legacy", ""])
def test_no_replay_pin_may_be_a_placeholder(chain, pin, placeholder):
    """The Implementation Gate, enforced: never `legacy`, `current`, `deferred`, `latest`."""
    with pytest.raises(Exception, match="pins_are_exact"):
        _insert_render(chain, **{pin: placeholder})
    chain.conn.rollback()


def test_a_render_may_not_carry_an_empty_evidence_manifest(chain):
    with pytest.raises(Exception, match="evidence_manifest_not_empty"):
        _insert_render(chain, evidence_manifest="{}")
    chain.conn.rollback()


@pytest.mark.parametrize("key", ["query", "query_spec", "sql", "rerun", "refresh", "tool_args"])
def test_a_render_carries_no_live_query_instruction(chain, key):
    """AC8: a Render grants no rerun authority, and display state is not a loophole."""
    with pytest.raises(Exception, match="carries_no_query"):
        _insert_render(chain, display_state='{"%s": "select 1"}' % key)
    chain.conn.rollback()


def test_a_render_claiming_a_report_origin_must_name_the_run(chain):
    with chain.conn.cursor() as cur, pytest.raises(Exception, match="origin_reference"):
        cur.execute(
            """
            INSERT INTO app.renders
                (id, org_id, project_id, result_id, result_content_hash,
                 visualization_spec_version_id, renderer_adapter, renderer_build_id,
                 runtime_build_id, theme_version, formatter_version, responsive_profile,
                 evidence_manifest, creation_surface, origin_kind, content_hash, created_by)
            VALUES (%s, %s, %s, %s, %s, 'vsv_x', 'echarts', 'rb', 'rt', 'th', 'fm', 'wide',
                    '{"a":1}'::jsonb, 'report', 'report_run', %s, 'test')
            """,
            (
                _uid("rnd"), chain.org_id, chain.project_id, chain.result_id, _HASH_B, _HASH_A,
            ),
        )
    chain.conn.rollback()


def test_the_detail_route_says_whether_a_render_can_still_be_replayed(chain):
    """`replayable` is not a list-only field, and it is not derivable client-side.

    `RenderDetail extends RenderSummary` on the TypeScript side, so every reader of
    a Render's detail page could type `render.replayable` and compile. `get_render`
    did not return the key: the value read was `undefined` — falsy, and therefore
    INDISTINGUISHABLE from a Render whose runtime material had been retired. The
    Console fixture carried `replayable: true` by hand, so no test saw it.

    Both states are asserted here, from the same source of truth `list_renders`
    uses: the presence of an audited retention action.
    """
    from core import analyze_artifacts as svc

    render_id = _insert_render(chain)
    fresh = svc.get_render(
        chain.conn, org_id=chain.org_id, project_id=chain.project_id, render_id=render_id
    )
    assert fresh["replayable"] is True
    assert fresh["retention_actions"] == []

    with chain.conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.render_retention_actions
                (id, render_id, org_id, project_id, action, reason, policy_ref, actor)
            VALUES (%s, %s, %s, %s, 'runtime_unavailable', 'renderer build removed',
                    'policy_fixture', 'test')
            """,
            (_uid("rra"), render_id, chain.org_id, chain.project_id),
        )
    retired = svc.get_render(
        chain.conn, org_id=chain.org_id, project_id=chain.project_id, render_id=render_id
    )
    assert retired["replayable"] is False
    # The same rule the collection applies, so the two views cannot disagree.
    listed = svc.list_renders(chain.conn, org_id=chain.org_id, project_id=chain.project_id)
    same = [r for r in listed["renders"] if r["id"] == render_id]
    assert same and same[0]["replayable"] is False
    chain.conn.rollback()


def test_retiring_a_render_is_an_audited_action_not_an_update(chain):
    """AC8: retention is a separate explicit lifecycle action, never a mutation."""
    render_id = _insert_render(chain)
    with chain.conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.render_retention_actions
                (id, render_id, org_id, project_id, action, reason, policy_ref, actor)
            VALUES (%s, %s, %s, %s, 'retention_expired', 'policy window elapsed',
                    'policy_fixture', 'test')
            """,
            (_uid("rra"), render_id, chain.org_id, chain.project_id),
        )
        cur.execute(
            "SELECT COUNT(*) FROM app.renders WHERE id = %s", (render_id,)
        )
        # The Render is still there. Expiry recorded a fact; it did not erase one.
        assert cur.fetchone()[0] == 1
    with chain.conn.cursor() as cur, pytest.raises(Exception, match="immutable"):
        cur.execute(
            "DELETE FROM app.render_retention_actions WHERE render_id = %s", (render_id,)
        )
    chain.conn.rollback()


# ---------------------------------------------------------------------------
# Project scope: composite foreign keys, not bare ids (AC13).
# ---------------------------------------------------------------------------


def test_a_report_version_cannot_pin_another_projects_query_spec(chain, live_postgres):
    other = Chain(live_postgres).build()
    report_id, _version = chain.report()
    with chain.conn.cursor() as cur, pytest.raises(Exception, match="foreign key|fk_"):
        cur.execute(
            """
            INSERT INTO app.analysis_report_versions
                (id, report_id, org_id, project_id, version_number, label, query_spec_id,
                 query_spec_version_id, presentation_absent_literal, content_hash,
                 predecessor_version_id, created_by)
            VALUES (%s, %s, %s, %s, 2, 'cross', %s, %s,
                    'No accepted presentation contract', %s,
                    (SELECT current_version_id FROM app.analysis_reports WHERE id = %s), 'test')
            """,
            (
                _uid("repv"), report_id, chain.org_id, chain.project_id,
                other.query_spec_id, other.query_spec_version_id, _HASH_B, report_id,
            ),
        )
    chain.conn.rollback()


def test_a_render_cannot_pin_another_projects_result(chain, live_postgres):
    other = Chain(live_postgres).build()
    with chain.conn.cursor() as cur, pytest.raises(Exception, match="foreign key|fk_"):
        cur.execute(
            """
            INSERT INTO app.renders
                (id, org_id, project_id, result_id, result_content_hash,
                 visualization_spec_version_id, renderer_adapter, renderer_build_id,
                 runtime_build_id, theme_version, formatter_version, responsive_profile,
                 evidence_manifest, creation_surface, origin_kind, content_hash, created_by)
            VALUES (%s, %s, %s, %s, %s, 'vsv_x', 'echarts', 'rb', 'rt', 'th', 'fm', 'wide',
                    '{"a":1}'::jsonb, 'explore', 'explore', %s, 'test')
            """,
            (
                _uid("rnd"), chain.org_id, chain.project_id, other.result_id, _HASH_B, _HASH_A,
            ),
        )
    chain.conn.rollback()


def test_every_new_table_enforces_row_level_security_on_its_owner(live_postgres):
    """AC13: RLS enabled AND forced. Enabled alone leaves the table owner outside it."""
    expected = {
        "analysis_reports", "analysis_report_versions", "analysis_report_runs",
        "analysis_notebooks", "analysis_notebook_versions",
        "analysis_notebook_version_blocks", "analysis_notebook_runs",
        "analysis_notebook_run_blocks", "analysis_notebook_schedules",
        "renders", "render_retention_actions",
    }
    with live_postgres.cursor() as cur:
        cur.execute(
            """
            SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity
            FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname = 'app' AND c.relname = ANY(%s)
            """,
            (sorted(expected),),
        )
        rows = {r[0]: (r[1], r[2]) for r in cur.fetchall()}
    assert set(rows) == expected, f"missing tables: {expected - set(rows)}"
    for name, (enabled, forced) in rows.items():
        assert enabled, f"{name} has no row level security"
        assert forced, f"{name} does not FORCE row level security"


# ---------------------------------------------------------------------------
# The service, against the real database.
# ---------------------------------------------------------------------------


def test_the_service_writes_a_version_and_advances_the_head(chain):
    from core import analyze_artifacts as svc

    created = svc.create_report(
        chain.conn,
        org_id=chain.org_id,
        project_id=chain.project_id,
        label="Weekly traffic",
        actor="test",
        query_spec_version_id=chain.query_spec_version_id,
    )
    assert created["version_number"] == 1
    # No accepted presentation contract exists in this repository, and the service
    # says so in the exact words the CHECK enforces.
    assert created["presentation"]["presentation_absent_literal"] == (
        "No accepted presentation contract"
    )

    second = svc.create_report_version(
        chain.conn,
        org_id=chain.org_id,
        project_id=chain.project_id,
        report_id=created["report_id"],
        actor="test",
        query_spec_version_id=chain.query_spec_version_id,
        label="Weekly traffic (revised)",
    )
    assert second["version_number"] == 2
    assert second["predecessor_version_id"] == created["id"]

    read = svc.get_report(
        chain.conn, org_id=chain.org_id, project_id=chain.project_id,
        report_id=created["report_id"],
    )
    assert [v["version_number"] for v in read["versions"]] == [2, 1]
    # AC16: the old version's content hash is unchanged by the new one.
    assert read["versions"][1]["content_hash"] == created["content_hash"]
    chain.conn.rollback()


def test_the_render_contract_probe_reports_exactly_the_registries_that_are_absent(chain):
    """AC8: the probe's answer is the DATABASE's answer, never a story number.

    THIS TEST HAS BEEN WRONG TWICE, in the same way both times. It first asserted
    `{"50.4", "50.5"}`; Story 50.4 landed migration 156 and the suite went red, so
    it was narrowed to `{"50.5"}` -- which is the identical mistake with a smaller
    set, and goes red again the day 50.5 lands. An environment's ABSENCE is not an
    invariant of this story, and encoding one makes the next author "fix" a test
    that was never testing anything.

    The invariant is the behaviour: `render_contract_state` reports exactly the
    registries that are not in the catalogue, and `available` is false while any of
    them is missing. That is true on a database where both are absent, where one
    is, and where neither is -- so this test never needs touching again.
    """
    from core import analyze_artifacts as svc

    registries = [svc._VISUALIZATION_SPEC_TABLE, svc._RENDERER_REGISTRY_TABLE]
    absent = [name for name in registries if not svc._table_exists(chain.conn, name)]

    state = svc.render_contract_state(chain.conn)
    assert state["available"] is (not absent)
    assert [m["missing_link"] for m in state["missing"]] == absent
    # Every named contract carries the story that owes it, whichever they are.
    assert all(m["owner_story"] and m["contract"] for m in state["missing"])
    chain.conn.rollback()


def test_create_render_refuses_with_the_reason_that_is_true_of_this_database(chain):
    """AC8: the refusal is derived from the probe, not from a hard-coded world.

    Two honest refusals exist, and which one applies depends on the database:

      * a registry is missing  -> `render_contract_unavailable`, naming exactly the
        contracts the probe reports and no others;
      * every registry exists  -> the contract gate opens and the request is
        refused pin by pin instead, because a payload with only `result_id` is
        still nine pins short.

    Asserting whichever is true here means the day Story 50.5 lands, this test
    keeps testing -- it moves to the second branch instead of going red.
    """
    from core import analyze_artifacts as svc

    state = svc.render_contract_state(chain.conn)
    with pytest.raises(svc.ArtifactRefused) as caught:
        svc.create_render(
            chain.conn, org_id=chain.org_id, project_id=chain.project_id, actor="test",
            payload={"result_id": chain.result_id},
        )
    if state["available"]:
        assert caught.value.code == "incomplete_render"
        assert {r.code for r in caught.value.refusals} == {"missing_pin"}
    else:
        assert caught.value.code == "render_contract_unavailable"
        assert [r.subject for r in caught.value.refusals] == [
            m["contract"] for m in state["missing"]
        ]
    chain.conn.rollback()


def test_a_render_cannot_pin_a_visualization_spec_version_of_another_project(chain):
    """Migration 158: the composite foreign key migration 154 never created.

    154 declared it inside a `DO` block guarded by `to_regclass(...) IS NOT NULL`,
    with a comment saying it "fires the moment that table exists". A `DO` block in
    an applied migration runs ONCE, at apply time, when the table did not exist --
    so it silently did nothing, and Story 50.4's table landing later changed
    nothing. Until 158 the only check on this pin was its SHAPE, which is what let
    a Render pin another Project's Visualization Spec version.

    Proved by inserting a complete, otherwise-valid Render whose spec version
    belongs to a second Project: the foreign key is the only thing that can refuse
    it, because every CHECK on this table passes.
    """
    other = Chain(chain.conn).build()
    foreign_spec_version = _visualization_spec_version(other)

    # Matched on the constraint NAME, not on the message: this host answers in
    # French, and a test that greps an English phrase is a test that passes for the
    # wrong reason on half the machines that run it.
    with chain.conn.cursor() as cur, pytest.raises(
        Exception, match="fk_renders_visualization_spec_version"
    ):
        cur.execute(
            _RENDER_INSERT,
            _render_row(chain, visualization_spec_version_id=foreign_spec_version),
        )
    chain.conn.rollback()


def test_a_render_accepts_a_visualization_spec_version_of_its_own_project(chain):
    """The other half: the constraint refuses the foreign one and admits the right
    one. A foreign key that refuses everything would pass the test above."""
    own = _visualization_spec_version(chain)
    with chain.conn.cursor() as cur:
        cur.execute(_RENDER_INSERT, _render_row(chain, visualization_spec_version_id=own))
        cur.execute("SELECT COUNT(*) FROM app.renders WHERE project_id = %s", (chain.project_id,))
        assert cur.fetchone()[0] == 1
    chain.conn.rollback()


def test_a_foreign_report_is_indistinguishable_from_an_absent_one(chain, live_postgres):
    """AC13: no foreign-ID oracle. Both raise the same exception, with no detail."""
    from core import analyze_artifacts as svc

    other = Chain(live_postgres).build()
    foreign_report, _v = other.report()

    with pytest.raises(svc.ArtifactNotFound) as foreign:
        svc.get_report(
            chain.conn, org_id=chain.org_id, project_id=chain.project_id,
            report_id=foreign_report,
        )
    with pytest.raises(svc.ArtifactNotFound) as absent:
        svc.get_report(
            chain.conn, org_id=chain.org_id, project_id=chain.project_id,
            report_id="rep_does_not_exist",
        )
    assert str(foreign.value) == str(absent.value)
    chain.conn.rollback()


def test_legacy_classification_is_a_read_and_is_restartable(chain):
    """AC12: measured, restartable, and it never fabricates a canonical pin."""
    from core import analyze_artifacts as svc

    first = svc.classify_legacy_artifacts(chain.conn, project_id=chain.project_id)
    second = svc.classify_legacy_artifacts(chain.conn, project_id=chain.project_id)
    assert first == second, "a classification that changes on re-read is not restartable"
    assert first["render_snapshots"]["promotable_to_canonical_render"] == 0
    assert first["render_snapshots"]["classification"] == "legacy_unverifiable"
    assert len(first["render_snapshots"]["missing_pins"]) == 10
    assert first["canonical"]["renders"] == 0
    chain.conn.rollback()


def test_a_notebook_run_pins_its_version_and_a_retry_returns_the_same_run(chain):
    """AC6 + AC7 through the service, end to end, against the real database."""
    from core import analyze_artifacts as svc

    report = svc.create_report(
        chain.conn, org_id=chain.org_id, project_id=chain.project_id, label="Block source",
        actor="test", query_spec_version_id=chain.query_spec_version_id,
    )
    notebook = svc.create_notebook(
        chain.conn,
        org_id=chain.org_id,
        project_id=chain.project_id,
        label="Weekly narrative",
        actor="test",
        blocks=[
            {"block_key": "intro", "block_type": "narrative", "narrative": {"text": "Hello"}},
            {
                "block_key": "traffic",
                "block_type": "query",
                "query_spec_version_id": chain.query_spec_version_id,
            },
            {
                "block_key": "from-report",
                "block_type": "report",
                "report_version_id": report["id"],
            },
        ],
    )
    assert [b["block_key"] for b in notebook["blocks"]] == ["intro", "traffic", "from-report"]
    # No presentation contract exists, so no block claims it renders -- and the
    # Run below will therefore owe an explicit literal, not a Render.
    assert all(b["renders"] is False for b in notebook["blocks"])

    run = svc.run_notebook(
        chain.conn, org_id=chain.org_id, project_id=chain.project_id,
        notebook_id=notebook["notebook_id"], actor="test", idempotency_key="run-1",
    )
    assert run["notebook_version_id"] == notebook["id"]
    assert run["state"] == "terminal"
    assert len(run["blocks"]) == 3
    for block in run["blocks"]:
        assert block["render_id"] is None
        assert block["render_absent_literal"] in {
            "No Render", "No Render: no accepted presentation contract",
        }
    # The two analytical blocks each produced their OWN Result. One Result shared
    # between blocks would be the cached answer AC3 forbids.
    results = [b["result_id"] for b in run["blocks"] if b["result_id"]]
    assert len(results) == 2, results
    assert len(set(results)) == 2, "two blocks shared one Result"

    replay = svc.run_notebook(
        chain.conn, org_id=chain.org_id, project_id=chain.project_id,
        notebook_id=notebook["notebook_id"], actor="test", idempotency_key="run-1",
    )
    assert replay["idempotent_replay"] is True
    assert replay["run_id"] == run["run_id"]

    rerun = svc.run_notebook(
        chain.conn, org_id=chain.org_id, project_id=chain.project_id,
        notebook_id=notebook["notebook_id"], actor="test", idempotency_key="run-2",
    )
    assert rerun["run_id"] != run["run_id"]
    rerun_results = [b["result_id"] for b in rerun["blocks"] if b["result_id"]]
    # AC11: a rerun creates distinct Results. It does not replace the old ones.
    assert not (set(rerun_results) & set(results))

    # The first Run is still readable, unchanged, with its original block pins.
    reread = svc.get_notebook_run(
        chain.conn, org_id=chain.org_id, project_id=chain.project_id, run_id=run["run_id"]
    )
    assert [b["result_id"] for b in reread["blocks"] if b["result_id"]] == results
    chain.conn.rollback()


def test_running_a_report_creates_a_new_result_every_time(chain):
    """AC3: the Report is never treated as a cached answer."""
    from core import analyze_artifacts as svc

    report = svc.create_report(
        chain.conn, org_id=chain.org_id, project_id=chain.project_id, label="Refreshable",
        actor="test", query_spec_version_id=chain.query_spec_version_id,
    )
    first = svc.run_report_version(
        chain.conn, org_id=chain.org_id, project_id=chain.project_id,
        report_version_id=report["id"], actor="test",
    )
    second = svc.run_report_version(
        chain.conn, org_id=chain.org_id, project_id=chain.project_id,
        report_version_id=report["id"], actor="test",
    )
    assert first["result_id"] != second["result_id"]
    assert first["run_id"] != second["run_id"]
    # Both Results exist. Refresh did not replace the first.
    with chain.conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM app.query_results WHERE id = ANY(%s)",
            ([first["result_id"], second["result_id"]],),
        )
        assert cur.fetchone()[0] == 2
    read = svc.get_report(
        chain.conn, org_id=chain.org_id, project_id=chain.project_id,
        report_id=report["report_id"],
    )
    assert len(read["runs"]) == 2
    chain.conn.rollback()


# ---------------------------------------------------------------------------
# The branch that could not produce a valid row (repair of F3).
#
# `run_notebook` had a branch -- a block that renders, executed while both
# downstream registries exist -- that set `render_id` AND `render_absent_literal`
# to NULL. `ck_analysis_notebook_run_blocks_render_is_honest` refuses that row, so
# the first such Notebook Run after Story 50.5 lands would have raised an opaque
# CheckViolation and lost the entire Run transaction, every other block with it.
#
# It was unreachable ONLY because `app.renderer_runtime_builds` does not exist.
# These tests make it reachable, which is the only way to prove the repair: they
# CREATE that table inside the test transaction and roll it back. That forces
# `render_contract_state` true through the real probe rather than by monkeypatching
# it -- a patched probe would prove the test's own stub, not the service.
# ---------------------------------------------------------------------------


def _with_renderer_registry(chain) -> None:
    """Make the Story 50.5 registry exist, for this transaction only.

    DDL is transactional in PostgreSQL, so the rollback every test here already
    performs removes it. Nothing is left behind, and nothing is asserted about the
    real 50.5 schema -- only that `render_contract_state` answers from the
    catalogue, which is what makes this a faithful rehearsal of the day it lands.
    """
    with chain.conn.cursor() as cur:
        cur.execute("CREATE TABLE IF NOT EXISTS app.renderer_runtime_builds (id TEXT PRIMARY KEY)")


def _rendering_notebook(chain, svc):
    """A Notebook whose one analytical block pins an ACCEPTED presentation."""
    return svc.create_notebook(
        chain.conn,
        org_id=chain.org_id,
        project_id=chain.project_id,
        label="Rendered composition",
        actor="test",
        blocks=[
            {
                "block_key": "traffic",
                "block_type": "query",
                "query_spec_version_id": chain.query_spec_version_id,
                "presentation": {
                    "kind": "visualization_spec_version",
                    "version_id": _visualization_spec_version(chain),
                },
            }
        ],
    )


def test_a_block_that_owes_a_render_records_the_debt_instead_of_crashing(chain):
    """The repair: the branch now produces a row the CHECK accepts.

    Before it, this exact sequence raised `CheckViolation` on the INSERT and the
    whole Run was lost. The block does not get a Render -- nothing can mint one
    from a server-side Run yet -- but it says so in exact words, and it names why.
    """
    from core import analyze_artifacts as svc

    _with_renderer_registry(chain)
    assert svc.render_contract_state(chain.conn)["available"] is True

    notebook = _rendering_notebook(chain, svc)
    assert notebook["blocks"][0]["renders"] is True, "the branch under test is not reached"

    run = svc.run_notebook(
        chain.conn, org_id=chain.org_id, project_id=chain.project_id,
        notebook_id=notebook["notebook_id"], actor="test", idempotency_key="rendered-1",
    )
    block = run["blocks"][0]
    assert block["render_id"] is None
    assert block["render_absent_literal"] == svc.NO_RENDER_NOT_DISPATCHED
    assert block["limitation"] == svc.RENDER_NOT_DISPATCHED_LIMITATION
    # And the Run itself completed: the block still produced its Result.
    assert run["state"] == "terminal"
    assert block["result_id"]
    chain.conn.rollback()


def test_the_four_literals_are_exactly_what_the_database_accepts(chain):
    """Service constants and migration 158's CHECK cannot drift apart.

    Each literal is proved by inserting it; a fifth wording is proved refused. A
    constant that the database does not accept is a crash waiting for the branch
    that uses it -- which is precisely how F3 happened.
    """
    from core import analyze_artifacts as svc

    notebook_id, version_id = chain.notebook()
    run_id = _uid("nbkrun")
    with chain.conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.analysis_notebook_runs
                (id, notebook_id, notebook_version_id, org_id, project_id, idempotency_key,
                 dispatch_source, state, actor)
            VALUES (%s, %s, %s, %s, %s, 'literals', 'manual', 'accepted', 'test')
            """,
            (run_id, notebook_id, version_id, chain.org_id, chain.project_id),
        )

    accepted = [
        svc.NO_RENDER,
        svc.NO_RENDER_BLOCK_FAILED,
        svc.NO_RENDER_NO_PRESENTATION,
        svc.NO_RENDER_NOT_DISPATCHED,
    ]
    for position, literal in enumerate(accepted, start=1):
        with chain.conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO app.analysis_notebook_run_blocks
                    (id, run_id, org_id, project_id, block_key, position, block_type,
                     render_absent_literal, status, started_at, ended_at)
                VALUES (%s, %s, %s, %s, %s, %s, 'narrative', %s, 'succeeded', NOW(), NOW())
                """,
                (
                    _uid("nbkrb"), run_id, chain.org_id, chain.project_id,
                    f"block-{position}", position, literal,
                ),
            )
    with chain.conn.cursor() as cur, pytest.raises(Exception, match="render_is_honest"):
        cur.execute(
            """
            INSERT INTO app.analysis_notebook_run_blocks
                (id, run_id, org_id, project_id, block_key, position, block_type,
                 render_absent_literal, status, started_at, ended_at)
            VALUES (%s, %s, %s, %s, 'invented', 99, 'narrative',
                    'No Render for now', 'succeeded', NOW(), NOW())
            """,
            (_uid("nbkrb"), run_id, chain.org_id, chain.project_id),
        )
    chain.conn.rollback()


# ---------------------------------------------------------------------------
# Scheduled dispatch (AC7). The schedule used to be a control nothing read.
# ---------------------------------------------------------------------------


def _scheduled_notebook(chain, svc, *, recurrence="daily", timezone="UTC"):
    notebook = svc.create_notebook(
        chain.conn, org_id=chain.org_id, project_id=chain.project_id,
        label="Nightly composition", actor="test",
        blocks=[
            {
                "block_key": "traffic",
                "block_type": "query",
                "query_spec_version_id": chain.query_spec_version_id,
            }
        ],
    )
    svc.set_notebook_schedule(
        chain.conn, org_id=chain.org_id, project_id=chain.project_id,
        notebook_id=notebook["notebook_id"], recurrence=recurrence, enabled=True,
        actor="test", timezone=timezone,
    )
    return notebook


def test_enabling_a_schedule_computes_when_it_is_next_due(chain):
    """`next_due_at` used to stay NULL forever while the panel said "Enabled: Yes"."""
    from core import analyze_artifacts as svc

    notebook = _scheduled_notebook(chain, svc)
    read = svc.get_notebook(
        chain.conn, org_id=chain.org_id, project_id=chain.project_id,
        notebook_id=notebook["notebook_id"],
    )
    assert read["schedule"]["enabled"] is True
    assert read["schedule"]["next_due_at"] is not None
    assert read["schedule"]["last_dispatched_at"] is None, "nothing has dispatched yet"
    assert read["schedule"]["dispatcher"] == svc.NOTEBOOK_DISPATCHER
    chain.conn.rollback()


def test_a_due_schedule_dispatches_through_the_same_service_as_a_manual_run(chain):
    """AC7: dispatch creates the SAME immutable evidence a manual run creates."""
    from core import analyze_artifacts as svc

    notebook = _scheduled_notebook(chain, svc)
    report = svc.dispatch_due_notebook_schedules(chain.conn, actor="scheduler")

    assert report["considered"] == 1
    assert report["failed"] == []
    assert len(report["dispatched"]) == 1
    dispatched = report["dispatched"][0]
    assert dispatched["notebook_id"] == notebook["notebook_id"]

    run = svc.get_notebook_run(
        chain.conn, org_id=chain.org_id, project_id=chain.project_id,
        run_id=dispatched["run_id"],
    )
    assert run["dispatch_source"] == "scheduled"
    assert run["notebook_version_id"] == notebook["id"]
    assert run["state"] == "terminal"
    # The block evidence a scheduled Run leaves is a real Result, exactly as a
    # manual one leaves. A weaker scheduled path is what this replaces.
    assert run["blocks"][0]["result_id"]

    read = svc.get_notebook(
        chain.conn, org_id=chain.org_id, project_id=chain.project_id,
        notebook_id=notebook["notebook_id"],
    )
    assert read["schedule"]["last_run_id"] == dispatched["run_id"]
    assert read["schedule"]["last_dispatched_at"] is not None
    chain.conn.rollback()


def test_dispatching_twice_in_one_period_completes_the_run_it_already_accepted(chain):
    """AC7: retry returns the ORIGINAL Run rather than duplicating it blindly.

    The nightly step can fire twice -- a redeploy, a retry, an operator. Two Runs
    over the same evidence would be two answers to one scheduled question.
    """
    from datetime import UTC, datetime, timedelta

    from core import analyze_artifacts as svc

    notebook = _scheduled_notebook(chain, svc)
    # Enabling makes it due immediately, so "now" has to be at or after that.
    moment = datetime.now(UTC) + timedelta(minutes=1)
    first = svc.dispatch_due_notebook_schedules(chain.conn, now=moment)
    assert len(first["dispatched"]) == 1
    # Make it due again inside the SAME period, which is what a second nightly
    # call in one day looks like.
    with chain.conn.cursor() as cur:
        cur.execute(
            "UPDATE app.analysis_notebook_schedules SET next_due_at = %s WHERE notebook_id = %s",
            (moment, notebook["notebook_id"]),
        )
    second = svc.dispatch_due_notebook_schedules(chain.conn, now=moment)

    assert second["dispatched"][0]["run_id"] == first["dispatched"][0]["run_id"]
    assert second["dispatched"][0]["idempotent_replay"] is True
    with chain.conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM app.analysis_notebook_runs WHERE notebook_id = %s",
            (notebook["notebook_id"],),
        )
        assert cur.fetchone()[0] == 1, "one scheduled question, one Run"

    # The NEXT period is a different question, and gets its own Run.
    later = svc.dispatch_due_notebook_schedules(chain.conn, now=moment + timedelta(days=1))
    assert later["dispatched"][0]["run_id"] != first["dispatched"][0]["run_id"]
    chain.conn.rollback()


def test_a_disabled_or_archived_notebook_is_never_dispatched(chain):
    from core import analyze_artifacts as svc

    notebook = _scheduled_notebook(chain, svc)
    svc.set_notebook_schedule(
        chain.conn, org_id=chain.org_id, project_id=chain.project_id,
        notebook_id=notebook["notebook_id"], recurrence="daily", enabled=False, actor="test",
    )
    assert svc.dispatch_due_notebook_schedules(chain.conn)["considered"] == 0

    svc.set_notebook_schedule(
        chain.conn, org_id=chain.org_id, project_id=chain.project_id,
        notebook_id=notebook["notebook_id"], recurrence="daily", enabled=True, actor="test",
    )
    with chain.conn.cursor() as cur:
        cur.execute(
            # `archived_by` travels with `archived_at` since migration 285
            # (`ck_analysis_notebooks_archived_pair`): an artifact archived by
            # nobody is a state the gesture cannot produce, so the schema no
            # longer stores it. This seed used to write the half-row.
            "UPDATE app.analysis_notebooks SET archived_at = NOW(), "
            "archived_by = 'owner@example.com' WHERE id = %s",
            (notebook["notebook_id"],),
        )
    assert svc.dispatch_due_notebook_schedules(chain.conn)["considered"] == 0
    chain.conn.rollback()


def test_the_nightly_step_itself_dispatches_against_real_postgresql(chain, monkeypatch):
    """The scheduler's own function, executed -- not asserted from its source.

    `test_the_scheduler_dispatches_through_this_service_and_no_other` reads
    `scheduler.py` and proves the call is written. That is a grep, and a grep
    cannot tell whether the call WORKS. This runs
    `core.scheduler._dispatch_due_canonical_notebooks()` for real, over this
    disposable PostgreSQL, and checks that a Run came out the other end.

    `commit()` is neutralized rather than allowed: the nightly step commits, and
    committing here would leave rows in the shared disposable database and make
    the next measurement taken on it a measurement of this test.
    """
    import contextlib

    from core import analyze_artifacts as svc
    from core import scheduler

    notebook = _scheduled_notebook(chain, svc)

    class NoCommit:
        """The real connection, minus the one call this test must not honour."""

        def __init__(self, wrapped):
            self._wrapped = wrapped

        def __getattr__(self, name):
            return getattr(self._wrapped, name)

        def commit(self):
            return None

    @contextlib.contextmanager
    def borrowed():
        yield NoCommit(chain.conn)

    monkeypatch.setattr("core.db.get_connection", borrowed)
    scheduler._dispatch_due_canonical_notebooks()

    with chain.conn.cursor() as cur:
        cur.execute(
            "SELECT id, dispatch_source, state, outcome FROM app.analysis_notebook_runs "
            "WHERE notebook_id = %s",
            (notebook["notebook_id"],),
        )
        runs = cur.fetchall()
    assert len(runs) == 1, "the nightly step produced no Run"
    assert runs[0][1] == "scheduled"
    assert runs[0][2] == "terminal"
    chain.conn.rollback()


def test_one_failing_notebook_does_not_stop_the_others(chain, monkeypatch):
    """Per-Notebook SAVEPOINT isolation, proved by making one dispatch raise.

    This is the failure mode the legacy loop already has a comment about, and the
    reason it is worth an explicit savepoint: a database error inside one Run
    aborts the whole transaction, so without one, every LATER Notebook fails too --
    silently, in production, at 02:00, with nothing in the record but the first
    error.

    The raise is injected rather than provoked through a broken fixture, because a
    fixture broken enough to raise inside `run_notebook` is also broken enough to
    be filtered out by the due query, which would prove the filter and not the
    isolation.
    """
    from core import analyze_artifacts as svc

    first = _scheduled_notebook(chain, svc)
    second = _scheduled_notebook(chain, svc)
    real_run = svc.run_notebook
    doomed = first["notebook_id"]

    def explode(conn, **kwargs):
        if kwargs["notebook_id"] == doomed:
            # A real database error, so the connection is genuinely in the aborted
            # state a savepoint has to recover from.
            with conn.cursor() as cur:
                cur.execute("SELECT 1 FROM app.this_table_does_not_exist")
        return real_run(conn, **kwargs)

    monkeypatch.setattr(svc, "run_notebook", explode)
    report = svc.dispatch_due_notebook_schedules(chain.conn)

    assert [f["notebook_id"] for f in report["failed"]] == [doomed]
    assert [d["notebook_id"] for d in report["dispatched"]] == [second["notebook_id"]]

    monkeypatch.undo()
    # The failure is recorded on the schedule, and the clock advanced, so a
    # permanently broken Notebook is not retried on every nightly step forever.
    read = svc.get_notebook(
        chain.conn, org_id=chain.org_id, project_id=chain.project_id, notebook_id=doomed
    )
    assert read["schedule"]["last_dispatch_note"]
    assert read["schedule"]["last_run_id"] is None
    chain.conn.rollback()


# ---------------------------------------------------------------------------
# Legacy Notebook definitions and Runs stay READABLE (AC12).
# ---------------------------------------------------------------------------


def test_legacy_notebooks_and_their_runs_remain_readable_with_their_limitations(chain):
    """AC12: readable with their ACTUAL evidence, and never reconstructed.

    They were readable through `NotebooksPanel.tsx` until the canonical screens
    took the Analyze sections and that panel stopped being mounted anywhere. This
    is the route that makes the requirement true again.

    NO SHARE TOKEN IS MINTED HERE ANY MORE, and the change is not a workaround.
    This fixture used to insert `share_token = 'tok_example'` to prove the listing
    reports a share without leaking its token. Story 50.7 then retired mutable
    notebook sharing and migration 162 added `ck_notebooks_share_token_retired`,
    so the INSERT started failing -- correctly. Keeping the row token-less and
    asserting the refusal turns the collision into the proof that the retirement
    reaches the database, rather than only the route table. The
    "reported without leaking" property it used to carry now lives on
    `app.render_shares`, whose grant is never returned to a client
    (`server/tests/core/test_render_shares_api.py`).
    """
    from core import analyze_artifacts as svc

    legacy_id = _uid("nb")
    with chain.conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.notebooks
                (id, project_id, title, report_ref, window_rule, narrative_prompt,
                 created_by, scheduled, schedule_rule)
            VALUES (%s, %s, 'Legacy weekly', 'gsc/position_movements', 'last_30d',
                    'summarize', 'test', TRUE, 'nightly')
            """,
            (legacy_id, chain.project_id),
        )
        for suffix, envelope_ref, inline in (
            ("a", None, '{"structuredContent": {}}'),
            ("b", "deferred", None),
            ("c", None, None),
        ):
            cur.execute(
                """
                INSERT INTO app.notebook_runs
                    (id, notebook_id, summary_text, envelope_ref, envelope_inline, status)
                VALUES (%s, %s, 'a summary', %s, %s::jsonb, 'success')
                """,
                (f"nbrun_{suffix}_{ULID()}", legacy_id, envelope_ref, inline),
            )

    legacy = svc.list_legacy_notebooks(chain.conn, project_id=chain.project_id)
    assert len(legacy) == 1
    entry = legacy[0]
    assert entry["title"] == "Legacy weekly"
    assert entry["report_ref"] == "gsc/position_movements"
    assert entry["classification"] == "legacy_mutable_definition"
    # No legacy notebook can carry a share token any more, and the database is what
    # says so -- not the route table. This is Story 50.7's retirement, proved from
    # the surface that reads the legacy rows.
    assert entry["is_shared"] is False
    # `pytest.raises` OUTSIDE the savepoint on purpose: swallowed inside, the
    # transaction stays aborted and `transaction()` fails trying to RELEASE it.
    with pytest.raises(Exception, match="ck_notebooks_share_token_retired"):
        with chain.conn.transaction():
            with chain.conn.cursor() as cur:
                cur.execute(
                    "UPDATE app.notebooks SET share_token = %s WHERE id = %s",
                    ("tok_example", legacy_id),
                )
    assert "tok_example" not in json.dumps(legacy)

    evidence = {run["evidence"] for run in entry["runs"]}
    assert evidence == {"inline_envelope", "deferred", "absent"}
    for run in entry["runs"]:
        # AC12: no per-block pin is invented for a legacy Run, ever.
        assert run["result_id"] is None
        assert run["render_id"] is None
        assert run["limitation"]

    # A read, so it is restartable by construction.
    assert svc.list_legacy_notebooks(chain.conn, project_id=chain.project_id) == legacy
    chain.conn.rollback()


# ---------------------------------------------------------------------------
# Story 73-1: the Dossier composes frozen Renders into one shareable document.
# ---------------------------------------------------------------------------


def test_a_dossier_composes_renders_and_resolves_their_provenance(chain):
    """The amendment's object: ordered blocks, pinned Renders, provenance at read.

    The resolution is what 73-2's share page and 73-3's PDF will print -- the
    Result and spec-version identities of each figure -- so it must come back
    from the READ, never from a stored copy.
    """
    from core.dossiers import create_dossier, get_dossier  # noqa: PLC0415

    first = _insert_render(chain)
    second = _insert_render(chain)
    created = create_dossier(
        chain.conn,
        org_id=chain.org_id,
        project_id=chain.project_id,
        label="Q3 channel review",
        actor="test",
        blocks=[
            {"kind": "narrative", "text": "What the quarter says."},
            {"kind": "render", "render_id": first},
            {"kind": "render", "render_id": second},
        ],
    )
    assert created["version_number"] == 1

    doc = get_dossier(
        chain.conn,
        org_id=chain.org_id,
        project_id=chain.project_id,
        dossier_id=created["dossier_id"],
    )
    kinds = [block["kind"] for block in doc["current_resolved_blocks"]]
    assert kinds == ["narrative", "render", "render"]
    figure = doc["current_resolved_blocks"][1]
    assert figure["provenance_missing"] is False
    assert figure["provenance"]["result_id"] == chain.result_id
    assert figure["provenance"]["visualization_spec_version_id"]
    assert doc["current_version_id"] == created["version_id"]


def test_a_dossier_with_no_render_block_is_a_note_and_is_refused(chain):
    from core.analyze_artifacts import ArtifactRefused  # noqa: PLC0415
    from core.dossiers import create_dossier  # noqa: PLC0415

    with pytest.raises(ArtifactRefused, match="at least one Render"):
        create_dossier(
            chain.conn,
            org_id=chain.org_id,
            project_id=chain.project_id,
            label="Only words",
            actor="test",
            blocks=[{"kind": "narrative", "text": "No figure anywhere."}],
        )


def test_a_dossier_naming_a_render_the_project_does_not_hold_is_refused_by_name(chain):
    from core.analyze_artifacts import ArtifactRefused  # noqa: PLC0415
    from core.dossiers import create_dossier  # noqa: PLC0415

    ghost = _uid("rnd")
    with pytest.raises(ArtifactRefused, match=ghost):
        create_dossier(
            chain.conn,
            org_id=chain.org_id,
            project_id=chain.project_id,
            label="Ghost figure",
            actor="test",
            blocks=[{"kind": "render", "render_id": ghost}],
        )


def test_a_new_dossier_version_succeeds_the_current_and_the_head_advances(chain):
    from core.dossiers import append_dossier_version, create_dossier, get_dossier  # noqa: PLC0415

    first = _insert_render(chain)
    second = _insert_render(chain)
    created = create_dossier(
        chain.conn,
        org_id=chain.org_id,
        project_id=chain.project_id,
        label="Living dossier",
        actor="test",
        blocks=[{"kind": "render", "render_id": first}],
    )
    version2 = append_dossier_version(
        chain.conn,
        org_id=chain.org_id,
        project_id=chain.project_id,
        dossier_id=created["dossier_id"],
        actor="test",
        blocks=[
            {"kind": "render", "render_id": first},
            {"kind": "render", "render_id": second},
        ],
    )
    assert version2["version_number"] == 2
    assert version2["predecessor_version_id"] == created["version_id"]

    doc = get_dossier(
        chain.conn,
        org_id=chain.org_id,
        project_id=chain.project_id,
        dossier_id=created["dossier_id"],
    )
    assert doc["current_version_id"] == version2["version_id"]
    assert [v["version_number"] for v in doc["versions"]] == [2, 1]


def test_a_dossier_version_cannot_be_updated(chain):
    """Immutable per version, like everything else in this chain (migration 340)."""
    from core.dossiers import create_dossier  # noqa: PLC0415

    render_id = _insert_render(chain)
    created = create_dossier(
        chain.conn,
        org_id=chain.org_id,
        project_id=chain.project_id,
        label="Frozen",
        actor="test",
        blocks=[{"kind": "render", "render_id": render_id}],
    )
    with chain.conn.cursor() as cur, pytest.raises(Exception, match="immutable"):
        cur.execute(
            "UPDATE app.analysis_dossier_versions SET label = 'rewritten' WHERE id = %s",
            (created["version_id"],),
        )
    chain.conn.rollback()


def test_a_share_row_carries_exactly_one_target(chain):
    """Migration 341: exactly one of render_id / dossier_version_id, CHECKed.

    Probed at the constraint itself: a full INSERT dies first on the operation
    spine (created_operation_id is NOT NULL with its own FK), which would make
    this test green for the wrong refusal. The definition read here is what
    Postgres enforces on every row, whatever door writes it.
    """
    with chain.conn.cursor() as cur:
        cur.execute(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conname = 'ck_render_shares_one_target' "
            "AND conrelid = 'app.render_shares'::regclass"
        )
        row = cur.fetchone()
    assert row is not None, "ck_render_shares_one_target is not installed"
    definition = row[0]
    assert "render_id" in definition and "dossier_version_id" in definition
    assert "= 1" in definition


def test_the_dossier_share_helpers_read_the_pinned_sequence(chain):
    """73-2: the grant's scope is the version's own blocks, in document order."""
    from core.dossiers import create_dossier  # noqa: PLC0415
    from core.render_shares import (  # noqa: PLC0415
        ShareSession,
        dossier_sequence,
        dossier_version_render_ids,
    )

    first = _insert_render(chain)
    second = _insert_render(chain)
    created = create_dossier(
        chain.conn,
        org_id=chain.org_id,
        project_id=chain.project_id,
        label="Shareable",
        actor="test",
        blocks=[
            {"kind": "render", "render_id": first},
            {"kind": "narrative", "text": "Between the figures."},
            {"kind": "render", "render_id": second},
        ],
    )
    assert (
        dossier_version_render_ids(
            chain.conn,
            org_id=chain.org_id,
            project_id=chain.project_id,
            dossier_version_id=created["version_id"],
        )
        == [first, second]
    )
    sequence = dossier_sequence(
        chain.conn,
        ShareSession(
            share_id="rsh_EXAMPLE",
            org_id=chain.org_id,
            project_id=chain.project_id,
            render_id=None,
            dossier_version_id=created["version_id"],
        ),
    )
    assert sequence["label"] == "Shareable"
    assert [b["kind"] for b in sequence["blocks"]] == ["render", "narrative", "render"]

