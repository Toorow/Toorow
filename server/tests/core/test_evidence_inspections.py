"""Story 55.2 AC4/AC6 -- an inspection is recorded, and recording never breaks.

Two halves, and the split is deliberate.

The FIRST half needs no database. It reads
`infra/nango/migrations/175_an_inspected_branch_leaves_a_trace.sql` and compares
its CHECK vocabularies with the module constants character for character. A
Python constant and a database CHECK that drift produce a row nobody can write
and an error nobody can read; pinning them against each other is the only way one
of them cannot move alone.

The SECOND half needs a real PostgreSQL, because what it proves is not Python:
append-only refusal, the RGPD erasure hatch, the RLS floor and the two count
constraints are database behaviour, and a mocked cursor would assert that the
test author remembered them, not that they exist.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from core import evidence_inspections

_REPO_ROOT = Path(__file__).resolve().parents[3]
_MIGRATION = (
    _REPO_ROOT / "infra" / "nango" / "migrations"
    / "175_an_inspected_branch_leaves_a_trace.sql"
)


def _check_values(column: str) -> set[str]:
    """The literals of a `column TEXT NOT NULL CHECK (column IN (...))` clause."""
    sql = _MIGRATION.read_text(encoding="utf-8")
    match = re.search(
        rf"{column}\s+TEXT\s+NOT\s+NULL\s+CHECK\s*\(\s*{column}\s+IN\s*\((.*?)\)\s*\)",
        sql,
        re.DOTALL,
    )
    assert match, f"no CHECK ... IN (...) found for {column!r} in migration 175"
    return set(re.findall(r"'([a-z_]+)'", match.group(1)))


# ---------------------------------------------------------------------------
# The vocabulary is one vocabulary, not two that look alike.
# ---------------------------------------------------------------------------


def test_the_migration_exists_and_is_the_one_this_story_wrote():
    assert _MIGRATION.is_file(), _MIGRATION


@pytest.mark.parametrize(
    ("column", "constant"),
    [
        ("kind", "KINDS"),
        ("surface", "SURFACES"),
        ("displayed_state", "DISPLAYED_STATES"),
    ],
)
def test_every_closed_vocabulary_matches_the_database_check(column, constant):
    assert set(getattr(evidence_inspections, constant)) == _check_values(column), (
        f"{constant} and the CHECK on {column} disagree; one of them can write a "
        "row the other refuses"
    )


def test_the_mcp_app_surface_is_declarable_even_though_nothing_writes_it_yet():
    """The value stays in the vocabulary, and the absence of its writer is named.

    Removing `mcp_app` because no caller exists would erase the inventory of what
    remains -- and it is the surface `mcp_app_behavior` is named after.
    """
    assert evidence_inspections.SURFACE_MCP_APP in evidence_inspections.SURFACES
    assert "mcp_app" in _MIGRATION.read_text(encoding="utf-8")


def test_only_a_listing_may_carry_a_count():
    """AC3, one level up: an absence is never a zero."""
    for state in evidence_inspections.DISPLAYED_STATES:
        if state == evidence_inspections.STATE_REQUIRING_COUNT:
            continue
        with pytest.raises(evidence_inspections.InspectionRefused):
            evidence_inspections.validate_inspection(
                org_id="org_x", project_id="proj_EXAMPLE", actor="owner@example.com",
                kind=evidence_inspections.KIND_SUBTREE_EXPANDED,
                surface=evidence_inspections.SURFACE_CONSOLE,
                displayed_state=state, branches_listed=0,
            )


def test_a_listing_without_a_count_is_refused():
    with pytest.raises(evidence_inspections.InspectionRefused):
        evidence_inspections.validate_inspection(
            org_id="org_x", project_id="proj_EXAMPLE", actor="owner@example.com",
            kind=evidence_inspections.KIND_SUBTREE_EXPANDED,
            surface=evidence_inspections.SURFACE_CONSOLE,
            displayed_state=evidence_inspections.STATE_BRANCHES_LISTED,
        )


def test_branches_not_recorded_is_a_state_of_its_own_and_carries_no_count():
    row = evidence_inspections.validate_inspection(
        org_id="org_x", project_id="proj_EXAMPLE", actor="owner@example.com",
        kind=evidence_inspections.KIND_SUBTREE_EXPANDED,
        surface=evidence_inspections.SURFACE_CONSOLE,
        displayed_state=evidence_inspections.STATE_BRANCHES_NOT_RECORDED,
    )
    assert row["branches_listed"] is None
    assert row["displayed_state"] != evidence_inspections.STATE_NO_BRANCH_JUDGED


def test_a_step_ordinal_without_a_walk_is_refused():
    with pytest.raises(evidence_inspections.InspectionRefused):
        evidence_inspections.validate_inspection(
            org_id="org_x", project_id="proj_EXAMPLE", actor="owner@example.com",
            kind=evidence_inspections.KIND_SUBTREE_EXPANDED,
            surface=evidence_inspections.SURFACE_CONSOLE,
            displayed_state=evidence_inspections.STATE_NO_BRANCH_JUDGED,
            step_ordinal=3,
        )


@pytest.mark.parametrize("bad", ["expanded", "", None, "BRANCH_SUBTREE_EXPANDED"])
def test_an_invented_kind_is_refused(bad):
    with pytest.raises(evidence_inspections.InspectionRefused):
        evidence_inspections.validate_inspection(
            org_id="org_x", project_id="proj_EXAMPLE", actor="owner@example.com",
            kind=bad, surface=evidence_inspections.SURFACE_CONSOLE,
            displayed_state=evidence_inspections.STATE_NO_BRANCH_JUDGED,
        )


def test_a_boolean_is_not_an_integer_count():
    """`True` is an `int` in Python. A count of `True` branches is not a count."""
    with pytest.raises(evidence_inspections.InspectionRefused):
        evidence_inspections.validate_inspection(
            org_id="org_x", project_id="proj_EXAMPLE", actor="owner@example.com",
            kind=evidence_inspections.KIND_SUBTREE_EXPANDED,
            surface=evidence_inspections.SURFACE_CONSOLE,
            displayed_state=evidence_inspections.STATE_BRANCHES_LISTED,
            branches_listed=True,
        )


# ---------------------------------------------------------------------------
# AC6 -- observing never breaks the observed.
# ---------------------------------------------------------------------------


class _ExplodingConnection:
    def cursor(self):
        raise RuntimeError("the database is gone")


def test_record_inspection_swallows_a_dead_connection_and_returns_none(caplog):
    assert evidence_inspections.record_inspection(
        _ExplodingConnection(),
        org_id="org_x", project_id="proj_EXAMPLE", actor="owner@example.com",
        kind=evidence_inspections.KIND_SUBTREE_EXPANDED,
        surface=evidence_inspections.SURFACE_CONSOLE,
        displayed_state=evidence_inspections.STATE_NO_BRANCH_JUDGED,
    ) is None


def test_record_inspection_swallows_a_refused_vocabulary_too():
    assert evidence_inspections.record_inspection(
        _ExplodingConnection(),
        org_id="org_x", project_id="proj_EXAMPLE", actor="owner@example.com",
        kind="invented", surface=evidence_inspections.SURFACE_CONSOLE,
        displayed_state=evidence_inspections.STATE_NO_BRANCH_JUDGED,
    ) is None


def test_insert_inspection_is_the_strict_half_and_still_raises():
    """Two functions, not a flag: a validator that sometimes raises is useless."""
    with pytest.raises(evidence_inspections.InspectionRefused):
        evidence_inspections.insert_inspection(
            _ExplodingConnection(),
            org_id="org_x", project_id="proj_EXAMPLE", actor="owner@example.com",
            kind="invented", surface=evidence_inspections.SURFACE_CONSOLE,
            displayed_state=evidence_inspections.STATE_NO_BRANCH_JUDGED,
        )


def test_no_reraise_switch_exists():
    """A flag that turns the never-raising writer into a raising one would be
    left on eventually. Its absence is the guarantee."""
    import inspect

    signature = inspect.signature(evidence_inspections.record_inspection)
    assert "reraise" not in signature.parameters
    assert "raise_on_error" not in signature.parameters


# ---------------------------------------------------------------------------
# The database half. Skipped without a disposable PostgreSQL, and a skip is
# stated as a skip -- these four properties are not provable in Python.
# ---------------------------------------------------------------------------


@pytest.fixture
def inspection_scope(pg_conn, test_org):
    import ulid as _ulid

    suffix = str(_ulid.ULID())
    project_id = f"proj_test_evi_{suffix}"[:40]
    with pg_conn.cursor() as cur:
        cur.execute(
            "INSERT INTO app.projects (id, org_id, name, slug, status, created_by) "
            "VALUES (%s, %s, 'Inspection pg fixture', %s, 'active', 'system') "
            "ON CONFLICT (id) DO NOTHING",
            (project_id, test_org, f"inspection-pg-{suffix}"[:60]),
        )
    pg_conn.commit()
    return {"org_id": test_org, "project_id": project_id}


def _row(scope, **overrides):
    base = dict(
        org_id=scope["org_id"],
        project_id=scope["project_id"],
        actor="owner@example.com",
        kind=evidence_inspections.KIND_SUBTREE_EXPANDED,
        surface=evidence_inspections.SURFACE_CONSOLE,
        displayed_state=evidence_inspections.STATE_BRANCHES_LISTED,
        branches_listed=4,
    )
    base.update(overrides)
    return base


def test_pg_an_inspection_lands_with_its_four_facts(pg_conn, inspection_scope):
    inspection_id = evidence_inspections.insert_inspection(
        pg_conn, **_row(inspection_scope, result_ref="res_example")
    )
    pg_conn.commit()
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT result_ref, actor, displayed_state, branches_listed, "
            "       occurred_at IS NOT NULL "
            "  FROM app.evidence_inspections WHERE id = %s",
            (inspection_id,),
        )
        row = cur.fetchone()
    assert row == (
        "res_example",
        "owner@example.com",
        evidence_inspections.STATE_BRANCHES_LISTED,
        4,
        True,
    )


def test_pg_a_count_beside_an_absence_is_refused_by_the_database_too(
    pg_conn, inspection_scope
):
    """The module refuses first; this proves the floor underneath it is real."""
    import psycopg

    inspection_id = f"evi_{__import__('ulid').ULID()}"
    with pytest.raises(psycopg.errors.CheckViolation):
        with pg_conn.cursor() as cur:
            cur.execute(
                "INSERT INTO app.evidence_inspections "
                "(id, org_id, project_id, kind, surface, displayed_state, "
                " branches_listed, actor) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    inspection_id,
                    inspection_scope["org_id"],
                    inspection_scope["project_id"],
                    evidence_inspections.KIND_SUBTREE_EXPANDED,
                    evidence_inspections.SURFACE_CONSOLE,
                    evidence_inspections.STATE_BRANCHES_NOT_RECORDED,
                    0,
                    "owner@example.com",
                ),
            )
    pg_conn.rollback()


def test_pg_an_inspection_cannot_be_edited_or_deleted(pg_conn, inspection_scope):
    import psycopg

    inspection_id = evidence_inspections.insert_inspection(
        pg_conn, **_row(inspection_scope)
    )
    pg_conn.commit()

    with pytest.raises(psycopg.Error):
        with pg_conn.cursor() as cur:
            cur.execute(
                "UPDATE app.evidence_inspections SET actor = 'someone@example.com' "
                "WHERE id = %s",
                (inspection_id,),
            )
    pg_conn.rollback()

    with pytest.raises(psycopg.Error):
        with pg_conn.cursor() as cur:
            cur.execute(
                "DELETE FROM app.evidence_inspections WHERE id = %s", (inspection_id,)
            )
    pg_conn.rollback()


def test_pg_the_rgpd_erasure_hatch_reaches_the_row(pg_conn, inspection_scope):
    """Without it, an organization that ever inspected anything is unerasable.

    Migration 169 exists because `app.file_source_templates` was born without
    this hatch. This table is born with it, and this test is why that claim is
    not just a comment.
    """
    inspection_id = evidence_inspections.insert_inspection(
        pg_conn, **_row(inspection_scope)
    )
    pg_conn.commit()

    with pg_conn.cursor() as cur:
        cur.execute("SET LOCAL app.rgpd_erasure = 'on'")
        cur.execute(
            "DELETE FROM app.evidence_inspections WHERE id = %s", (inspection_id,)
        )
        assert cur.rowcount == 1
    pg_conn.commit()

    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM app.evidence_inspections WHERE id = %s",
            (inspection_id,),
        )
        assert cur.fetchone()[0] == 0


# ---------------------------------------------------------------------------
# The READ (2026-08-17). Until it existed, two surfaces wrote this table and no
# production SELECT touched it -- a usage signal kept and read by nothing, which
# `context-hub.md` names as a defect in its own words. These pin the three
# honesty properties, not the SQL: the shape may change, the promises may not.
# ---------------------------------------------------------------------------


def test_the_summary_reads_and_never_writes():
    """Pure, and worth its own test: the table is append-only, so a read that
    slipped a write past would be a row nobody can take back."""
    import inspect

    # The docstring is removed first: it NAMES the writes it refuses, and a
    # guard that matched its own prose would go red on the comment that explains
    # it.
    source = inspect.getsource(evidence_inspections.summarize_inspections)
    source = source.replace(evidence_inspections.summarize_inspections.__doc__ or "", "")
    source += evidence_inspections._SUMMARY_SQL
    for forbidden in ("INSERT", "UPDATE ", "DELETE", "commit()", "TRUNCATE"):
        assert forbidden not in source, f"the summary read writes: {forbidden}"


def test_pg_the_four_displayed_states_stay_four(pg_conn, inspection_scope):
    """The whole point of the column, restated on the way OUT.

    `no_branch_judged` is an honest zero; `branches_not_recorded` and
    `unavailable` say "we cannot know what was judged". A summary that added them
    up would undo, in the read, exactly what
    `ck_evidence_inspections_absence_counts_nothing` protects in the write.
    """
    evidence_inspections.insert_inspection(pg_conn, **_row(inspection_scope))
    for state in (
        evidence_inspections.STATE_NO_BRANCH_JUDGED,
        evidence_inspections.STATE_BRANCHES_NOT_RECORDED,
        evidence_inspections.STATE_UNAVAILABLE,
    ):
        evidence_inspections.insert_inspection(
            pg_conn, **_row(inspection_scope, displayed_state=state, branches_listed=None)
        )
    pg_conn.commit()

    summary = evidence_inspections.summarize_inspections(
        pg_conn, project_id=inspection_scope["project_id"]
    )

    assert summary["displayed_states"] == {
        evidence_inspections.STATE_BRANCHES_LISTED: 1,
        evidence_inspections.STATE_NO_BRANCH_JUDGED: 1,
        evidence_inspections.STATE_BRANCHES_NOT_RECORDED: 1,
        evidence_inspections.STATE_UNAVAILABLE: 1,
    }
    # And every kind of the closed vocabulary is present, zeros included: a key
    # that is merely absent cannot be told from a key that was never possible.
    assert set(summary["kinds"]) == set(evidence_inspections.KINDS)
    assert summary["kinds"][evidence_inspections.KIND_SUBTREE_EXPANDED] == 4
    assert summary["inspections_total"] == 4
    assert summary["window_truncated"] is False


def test_pg_a_total_the_query_could_not_prove_is_none_not_zero(pg_conn, inspection_scope):
    """A bounded scan that reports its ceiling as a total reads as complete."""
    for _ in range(3):
        evidence_inspections.insert_inspection(pg_conn, **_row(inspection_scope))
    pg_conn.commit()

    summary = evidence_inspections.summarize_inspections(
        pg_conn, project_id=inspection_scope["project_id"], row_limit=2
    )

    assert summary["window_truncated"] is True
    assert summary["inspections_total"] is None
    # What it actually read stays a number -- every bucket is a count over
    # exactly those rows, and hiding that would make the summary unreadable.
    assert summary["inspections_scanned"] == 2
    assert summary["scan_row_limit"] == 2
    assert sum(summary["displayed_states"].values()) == 2


def test_pg_a_project_with_no_inspection_gets_an_empty_summary_not_an_error(
    pg_conn, inspection_scope
):
    """Nobody has opened a branch yet is a fact, and it has an answer."""
    summary = evidence_inspections.summarize_inspections(
        pg_conn, project_id=inspection_scope["project_id"]
    )

    assert summary["inspections_scanned"] == 0
    # Proven, because the whole window was read: a zero here IS the fact.
    assert summary["inspections_total"] == 0
    assert summary["window_truncated"] is False
    assert summary["top_steps"] == []
    assert set(summary["displayed_states"]) == set(evidence_inspections.DISPLAYED_STATES)
    assert all(count == 0 for count in summary["displayed_states"].values())


def test_pg_the_most_opened_step_is_named_and_counted(pg_conn, inspection_scope):
    """The product question: WHICH branches do people open most."""
    from core.ai_paths import begin_path

    path = begin_path(
        pg_conn,
        org_id=inspection_scope["org_id"],
        project_id=inspection_scope["project_id"],
        actor="owner@example.com",
    )
    for ordinal, times in ((1, 1), (2, 3)):
        for _ in range(times):
            evidence_inspections.insert_inspection(
                pg_conn,
                **_row(inspection_scope, ai_path_id=path["id"], step_ordinal=ordinal),
            )
    pg_conn.commit()

    summary = evidence_inspections.summarize_inspections(
        pg_conn, project_id=inspection_scope["project_id"]
    )

    assert summary["top_steps"][0] == {
        "ai_path_id": path["id"],
        "step_ordinal": 2,
        "inspections": 3,
    }
    assert len(summary["top_steps"]) == 2


def test_pg_the_summary_stops_at_the_project_it_was_asked_about(pg_conn, inspection_scope):
    """One Project's usage never counts another's -- the aggregate is scoped in
    the WHERE, not filtered afterwards."""
    evidence_inspections.insert_inspection(pg_conn, **_row(inspection_scope))
    pg_conn.commit()

    summary = evidence_inspections.summarize_inspections(pg_conn, project_id="proj_EXAMPLE")

    assert summary["inspections_scanned"] == 0


def test_pg_row_level_security_is_enabled_and_forced(pg_conn):
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT relrowsecurity, relforcerowsecurity FROM pg_class "
            " WHERE oid = 'app.evidence_inspections'::regclass"
        )
        assert cur.fetchone() == (True, True), (
            "RLS is the floor. Without FORCE, a deployment connecting as the "
            "table owner is silently exempt from it."
        )


def test_pg_migration_168s_finding_is_no_longer_written_anywhere(pg_conn):
    """AC5, verified against the DATABASE rather than against the .sql file.

    The sentence migration 168 recorded -- that no table records an interaction --
    is what a reader acts on. It must not survive on the constraint while this
    story ships the table.
    """
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT obj_description(oid, 'pg_constraint') FROM pg_constraint "
            " WHERE conname = 'ck_evaluation_case_verdicts_mcp_evaluator_absent'"
        )
        row = cur.fetchone()
    assert row and row[0], "the constraint or its comment is missing"
    comment = row[0]
    assert "no table records it" not in comment
    # THE TABLE IS NAMED AS A THING, NOT AS AN IDENTIFIER, and this assertion
    # followed rather than fought the migration that decided so. It read
    # `"app.evidence_inspections" in comment` -- the spelling migration 175 used
    # on 2026-08-01. Migration 177 rewrote the whole comment HOURS LATER the same
    # day (`ea596c8e`), reducing the two reasons to one and writing "nothing forms
    # a verdict from an inspection", which says the same thing in the reader's
    # words. 175 and 177 are both applied, so neither comment can be edited back;
    # what the test may hold is the CLAIM, and the claim is that the constraint
    # points at the interaction record. That the relation itself exists is proven
    # against the catalog by `test_pg_the_interaction_relation_the_dimension_
    # waited_for_now_exists` below, so nothing is lost by not spelling it here.
    assert "inspection" in comment
    # The guard stays, and every remaining reason is named on it.
    assert "evaluation_runs.py" in comment
    assert "mcp_app" in comment


def test_pg_the_interaction_relation_the_dimension_waited_for_now_exists(pg_conn):
    """The exact query migration 168 ran, re-run. It returned zero rows; now it
    must not."""
    with pg_conn.cursor() as cur:
        cur.execute(
            "SELECT table_name FROM information_schema.tables "
            " WHERE table_schema = 'app' "
            "   AND (table_name LIKE '%%interaction%%' OR table_name LIKE '%%view_tool%%' "
            "        OR table_name LIKE '%%drill%%' OR table_name LIKE '%%inspection%%')"
        )
        names = {r[0] for r in cur.fetchall()}
    assert "evidence_inspections" in names
