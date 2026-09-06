"""Migration 153, replayed against a live PostgreSQL rather than read.

WHY THIS FILE EXISTS. The four Epic 51 suites are almost entirely mocked, and
the first delivery said so instead of hiding it: triggers, RLS and the composite
foreign keys were "written and read, not replayed". A mocked cursor cannot
refuse an INSERT, so every immutability claim in those suites is a claim about
Python, not about the database that is supposed to be the last line.

This suite makes the difference concrete. It runs as the ordinary `connector`
role -- never a superuser, for whom RLS is not enforced and every isolation
assertion would pass vacuously (`scripts/disposable_postgres.py`, trap 1).

TWO TRAPS THIS FILE ENCODES, both of which have already cost a session a green
run that proved nothing:

1. **A refused statement aborts the whole transaction** in psycopg 3. Asserting
   two refusals in a row without a SAVEPOINT makes the second fail with
   `InFailedSqlTransaction` -- which is still an exception, so a naive
   `pytest.raises` passes and proves nothing about the constraint it names.
   Every refusal below runs inside `conn.transaction()`, which is a savepoint.
2. **`live_postgres` rolls back on teardown.** Nothing here may depend on a
   previous test's rows; each builds its own chain.
"""

from __future__ import annotations

import datetime as _dt

import pytest
from ulid import ULID

psycopg = pytest.importorskip("psycopg")

_HASH = "a" * 64

_RENDER_ABSENCE = (
    '[{"pin_family": "render", "reason_code": "render_owner_not_delivered",'
    ' "owner": "stories 50.4 / 50.5 / 50.7"}]'
)


def _uid(prefix: str) -> str:
    """A prefixed ULID: several tables CHECK that shape, so a bare uuid would
    fail the fixture instead of the property under test."""
    return f"{prefix}_{ULID()}"


class Chain:
    """Everything migration 153 needs upstream of an Evaluation Run."""

    def __init__(self, conn):
        self.conn = conn
        self.org_id = _uid("org")
        self.project_id = _uid("proj")
        self.view_id = _uid("sv")
        self.view_version_id = _uid("svv")
        self.profile_id = _uid("erp")
        self.context_set_id = _uid("ecvs")

    def build(self) -> "Chain":
        with self.conn.cursor() as cur:
            cur.execute(
                "INSERT INTO app.organizations (id, name, slug, status, created_by) "
                "VALUES (%s, 'Story 51 fixture', %s, 'active', 'test')",
                (self.org_id, self.org_id.replace("_", "-")),
            )
            cur.execute(
                "INSERT INTO app.projects (id, org_id, name, slug, created_by) "
                "VALUES (%s, %s, 'Story 51 fixture', %s, 'test')",
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
                (self.view_version_id, self.view_id, self.project_id, _HASH, _HASH),
            )
            cur.execute(
                "INSERT INTO app.evaluation_run_profiles "
                "(id, org_id, project_id, name, evidence_mode, created_by) "
                "VALUES (%s, %s, %s, 'fixture profile', 'offline', 'test')",
                (self.profile_id, self.org_id, self.project_id),
            )
            cur.execute(
                "INSERT INTO app.evaluation_context_version_sets "
                "(id, org_id, project_id, content_hash) VALUES (%s, %s, %s, %s)",
                (self.context_set_id, self.org_id, self.project_id, _HASH),
            )
        return self

    def open_run(self, run_id: str | None = None, **overrides) -> str:
        run_id = run_id or _uid("erun")
        values = {
            "id": run_id,
            "org_id": self.org_id,
            "project_id": self.project_id,
            "run_profile_id": self.profile_id,
            "evidence_mode": "offline",
            "semantic_view_id": self.view_id,
            "semantic_view_version_id": self.view_version_id,
            "context_version_set_id": self.context_set_id,
            "model_ref": "model-example-1",
            # `ck_evaluation_runs_catalog_version` demands a 64-hex digest, not a
            # label: the tool catalog is pinned by CONTENT, so a name that could
            # be reused for a different catalog is not a pin.
            "tool_catalog_version": _HASH,
            "data_snapshot_hash": _HASH,
            "as_of": _dt.date(2026, 7, 31),
            "created_by": "test",
        }
        values.update(overrides)
        columns = ", ".join(values)
        placeholders = ", ".join(["%s"] * len(values))
        with self.conn.cursor() as cur:
            cur.execute(
                f"INSERT INTO app.evaluation_runs ({columns}) VALUES ({placeholders})",
                tuple(values.values()),
            )
        return run_id

    def finalize(self, run_id: str, *, pins: str = _RENDER_ABSENCE) -> None:
        """`ck_evaluation_runs_finalized_is_complete` demands all three: a run
        frozen without an end, a content hash and a question-set fingerprint
        would be evidence nobody can reproduce."""
        with self.conn.cursor() as cur:
            cur.execute(
                """
                UPDATE app.evaluation_runs
                   SET lifecycle = 'finalized',
                       ended_at = now(),
                       content_hash = %s,
                       question_set_fingerprint = %s,
                       unresolved_pins = %s::jsonb
                 WHERE id = %s
                """,
                (_HASH, _HASH, pins, run_id),
            )


@pytest.fixture
def chain(live_postgres):
    return Chain(live_postgres).build()


# ===========================================================================
# The finalization guard -- the defect the first delivery shipped and repaired.
# ===========================================================================


def test_a_run_cannot_finalize_while_the_render_pin_is_unresolved_and_unrecorded(
    live_postgres, chain
):
    """The rule, stated as the rule: while `render_runtime_version` is NULL, the
    absence must be RECORDED. Silence is what this refuses."""
    run_id = chain.open_run()
    with pytest.raises(psycopg.errors.CheckViolation), live_postgres.transaction():
        with live_postgres.cursor() as cur:
            cur.execute(
                "UPDATE app.evaluation_runs SET lifecycle = 'finalized' WHERE id = %s",
                (run_id,),
            )


def test_a_run_finalizes_once_the_render_absence_is_recorded(live_postgres, chain):
    run_id = chain.open_run()
    chain.finalize(run_id)
    with live_postgres.cursor() as cur:
        cur.execute("SELECT lifecycle FROM app.evaluation_runs WHERE id = %s", (run_id,))
        assert cur.fetchone()[0] == "finalized"


def test_the_finalization_guard_keys_on_the_render_pin_not_on_an_empty_list(live_postgres):
    """What this proves, and what it cannot -- said plainly.

    The first delivery's guard refused an EMPTY `unresolved_pins` outright. That
    was true today only by accident, and it made the target state unreachable:
    once stories 50.4/50.5/50.7 land a Render and every pin resolves, the honest
    list IS empty, and no run could have finalized again without a migration to
    unblock it. The rule is now keyed on the pin it is actually about.

    CANNOT BE EXERCISED AS A ROW TODAY, and pretending otherwise would be the
    dishonest version of this test: `ck_evaluation_runs_render_unpinned` holds
    `render_runtime_version` at NULL, so the resolved-pin branch is unreachable
    until that CHECK is lifted by the story that delivers the Render. What is
    checkable now is that lifting the CHECK will be ENOUGH -- that the trigger
    reads the column rather than the list length, so nobody has to remember to
    come back and edit it.
    """
    with live_postgres.cursor() as cur:
        cur.execute(
            "SELECT prosrc FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
            "WHERE n.nspname = 'app' AND p.proname = 'reject_evaluation_run_rewrite'"
        )
        source = cur.fetchone()[0]

        cur.execute(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conrelid = 'app.evaluation_runs'::regclass "
            "AND conname = 'ck_evaluation_runs_render_unpinned'"
        )
        check = cur.fetchone()

    assert "render_runtime_version IS NULL" in source, (
        "the guard no longer keys on the render pin: an empty unresolved_pins "
        "would become unfinalizable again the day a Render exists"
    )
    assert "jsonb_array_length(NEW.unresolved_pins) = 0" not in source, (
        "the blanket empty-list refusal is back, and it makes the target state unreachable"
    )
    assert check is None, (
        "ck_evaluation_runs_render_unpinned is back. Migration 166 removed it once "
        "Stories 50.4/50.5 delivered the rendered artifact; restoring it would make "
        "the resolved-pin branch unreachable again"
    )


def test_a_run_that_pins_a_renderer_runtime_finalizes_with_an_empty_pin_list(
    live_postgres, chain
):
    """The branch that was unreachable, and that migration 157 was written for.

    Its predecessor test said, in as many words: *"if lifting the CHECK is
    deliberate, the resolved-pin branch is now reachable and deserves a real
    row-level test here"*. Migration 166 lifted it. This is that test.

    It is the whole point of how 157 phrased the guard. Had the guard kept
    refusing an EMPTY `unresolved_pins`, this row would be unfinalizable today and
    someone would have had to ship a migration to unblock the product becoming
    correct. Keyed on the column instead, lifting the CHECK was enough and no
    trigger was touched.
    """
    build_id = "kpi/toorow-kpi@1.0.0"
    with live_postgres.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.renderer_runtime_builds
                (id, runtime_build, family, renderer_id, theme_version,
                 formatter_version, responsive_profiles, git_sha)
            VALUES (%s, %s, 'kpi', 'toorow-kpi', '1.0.0', '1.0.0',
                    ARRAY['console'], %s)
            ON CONFLICT (id) DO NOTHING
            """,
            (build_id, "@toorow/card-shell/viz@1.0.0+abcdef1", "abcdef1"),
        )

    run_id = chain.open_run(render_runtime_version=build_id)
    chain.finalize(run_id, pins="[]")

    with live_postgres.cursor() as cur:
        cur.execute(
            "SELECT lifecycle, unresolved_pins, render_runtime_version "
            "FROM app.evaluation_runs WHERE id = %s",
            (run_id,),
        )
        lifecycle, pins, runtime = cur.fetchone()

    assert lifecycle == "finalized"
    assert pins == [], "an empty list is the truth once every pin resolves"
    assert runtime == build_id


def test_a_run_cannot_pin_a_renderer_runtime_that_does_not_exist(live_postgres, chain):
    """Fillable is not the same as free-form: the pin references a real build."""
    with pytest.raises(psycopg.errors.ForeignKeyViolation), live_postgres.transaction():
        chain.open_run(render_runtime_version="kpi/invented@9.9.9")


def test_a_finalized_run_cannot_be_modified_again(live_postgres, chain):
    run_id = chain.open_run()
    chain.finalize(run_id)
    with pytest.raises(psycopg.Error), live_postgres.transaction():
        with live_postgres.cursor() as cur:
            cur.execute(
                "UPDATE app.evaluation_runs SET model_ref = 'rewritten' WHERE id = %s", (run_id,)
            )


def test_finalization_may_not_rewrite_the_pinned_subject_or_environment(live_postgres, chain):
    """Freezing and rewriting in the same statement is the interesting attack."""
    run_id = chain.open_run()
    with pytest.raises(psycopg.Error), live_postgres.transaction():
        with live_postgres.cursor() as cur:
            cur.execute(
                """
                UPDATE app.evaluation_runs
                   SET lifecycle = 'finalized', ended_at = now(), content_hash = %s,
                       question_set_fingerprint = %s, unresolved_pins = %s::jsonb,
                       model_ref = 'swapped-during-freeze'
                 WHERE id = %s
                """,
                (_HASH, _HASH, _RENDER_ABSENCE, run_id),
            )


def test_an_evaluation_run_can_never_be_deleted(live_postgres, chain):
    run_id = chain.open_run()
    with pytest.raises(psycopg.Error), live_postgres.transaction():
        with live_postgres.cursor() as cur:
            cur.execute("DELETE FROM app.evaluation_runs WHERE id = %s", (run_id,))


# ===========================================================================
# Cross-project isolation, enforced by the composite keys and not by Python.
# ===========================================================================


def test_a_run_cannot_borrow_a_profile_from_another_project(live_postgres, chain):
    """The composite FK is the whole point: a bare `run_profile_id` would let a
    caller pass an id it does not own and the row would be accepted."""
    other = Chain(live_postgres).build()
    with pytest.raises(psycopg.errors.ForeignKeyViolation), live_postgres.transaction():
        chain.open_run(run_profile_id=other.profile_id)


def test_a_run_cannot_borrow_a_semantic_view_version_from_another_project(live_postgres, chain):
    other = Chain(live_postgres).build()
    with pytest.raises(psycopg.errors.ForeignKeyViolation), live_postgres.transaction():
        chain.open_run(
            semantic_view_id=other.view_id, semantic_view_version_id=other.view_version_id
        )


# ===========================================================================
# A baseline is approved, and never advances on its own.
# ===========================================================================


def _finalized_run(chain, live_postgres) -> str:
    run_id = chain.open_run()
    chain.finalize(run_id)
    return run_id


def _approve(chain, live_postgres, run_id: str) -> str:
    baseline_id = _uid("ebl")
    with live_postgres.cursor() as cur:
        cur.execute(
            "INSERT INTO app.evaluation_baselines "
            "(id, org_id, project_id, run_profile_id, run_id, approved_by, approval_reason) "
            "VALUES (%s, %s, %s, %s, %s, 'test', 'fixture approval')",
            (baseline_id, chain.org_id, chain.project_id, chain.profile_id, run_id),
        )
    return baseline_id


def test_a_baseline_cannot_be_approved_on_an_unfinalized_run(live_postgres, chain):
    run_id = chain.open_run()
    with pytest.raises(psycopg.Error), live_postgres.transaction():
        _approve(chain, live_postgres, run_id)


def test_a_baseline_is_superseded_never_repointed(live_postgres, chain):
    """`analyze-and-test.md`: a baseline never advances automatically. Repointing
    it at another run would move history rather than record that it moved."""
    first = _approve(chain, live_postgres, _finalized_run(chain, live_postgres))
    second_run = _finalized_run(chain, live_postgres)

    with pytest.raises(psycopg.Error), live_postgres.transaction():
        with live_postgres.cursor() as cur:
            cur.execute(
                "UPDATE app.evaluation_baselines SET run_id = %s WHERE id = %s",
                (second_run, first),
            )


def test_a_profile_can_actually_receive_a_second_baseline(live_postgres, chain):
    """The successor must be REACHABLE, and it was not.

    Migration 153 held two guards that were each right alone and deadlocked
    together: a partial unique index allowing one active baseline per profile,
    and a self-referencing foreign key that was NOT deferrable. Both of the only
    two possible orders were refused -- inserting the successor while the
    incumbent is active (UniqueViolation), and superseding the incumbent toward
    a successor that does not exist yet (ForeignKeyViolation). The first
    baseline a profile received was the last one it could ever have.

    Migration 157 defers the self-FK to COMMIT, which opens exactly one legal
    path: supersede then insert, in one transaction. This test is the proof, and
    it also checks the invariant the index exists for -- one active baseline at
    the end, not two.
    """
    incumbent = _approve(chain, live_postgres, _finalized_run(chain, live_postgres))
    successor_run = _finalized_run(chain, live_postgres)
    successor = _uid("ebl")

    with live_postgres.cursor() as cur:
        cur.execute(
            "UPDATE app.evaluation_baselines SET superseded_by_baseline_id = %s WHERE id = %s",
            (successor, incumbent),
        )
        cur.execute(
            "INSERT INTO app.evaluation_baselines "
            "(id, org_id, project_id, run_profile_id, run_id, approved_by, approval_reason) "
            "VALUES (%s, %s, %s, %s, %s, 'test', 'succeeds the incumbent')",
            (successor, chain.org_id, chain.project_id, chain.profile_id, successor_run),
        )
        cur.execute(
            "SELECT count(*) FROM app.evaluation_baselines "
            "WHERE run_profile_id = %s AND superseded_by_baseline_id IS NULL",
            (chain.profile_id,),
        )
        assert cur.fetchone()[0] == 1, "exactly one baseline stays active per profile"


def test_the_deferred_key_still_refuses_a_successor_that_is_never_inserted(live_postgres, chain):
    """Deferring changes WHEN the reference is checked, never WHETHER.

    The check lands at COMMIT, so this cannot be written with a nested
    `conn.transaction()` -- that is a SAVEPOINT, and releasing it verifies
    nothing. A test that used one would pass while proving the opposite of its
    name. Hence a separate connection with a real commit.
    """
    import os

    incumbent = _approve(chain, live_postgres, _finalized_run(chain, live_postgres))
    live_postgres.commit()

    ghost = _uid("ebl")
    other = psycopg.connect(os.environ["TEST_POSTGRES_DSN"])
    try:
        with other.cursor() as cur:
            cur.execute(
                "UPDATE app.evaluation_baselines SET superseded_by_baseline_id = %s WHERE id = %s",
                (ghost, incumbent),
            )
        with pytest.raises(psycopg.errors.ForeignKeyViolation):
            other.commit()
    finally:
        other.rollback()
        other.close()


def test_a_baseline_can_never_be_deleted(live_postgres, chain):
    baseline_id = _approve(chain, live_postgres, _finalized_run(chain, live_postgres))
    with pytest.raises(psycopg.Error), live_postgres.transaction():
        with live_postgres.cursor() as cur:
            cur.execute("DELETE FROM app.evaluation_baselines WHERE id = %s", (baseline_id,))


# ===========================================================================
# The Render pin is declared and held empty -- by the database, not by a comment.
# ===========================================================================


@pytest.mark.parametrize(
    "table,column",
    [
        ("evaluation_run_cases", "render_ref"),
        ("observed_cohort_members", "render_ref"),
        ("feedback_annotations", "render_ref"),
        ("golden_question_versions", "expected_render_ref"),
    ],
)
def test_every_render_pin_column_exists_and_is_held_null_by_a_check(live_postgres, table, column):
    """What each render pin must be, now that the rendered artifact exists.

    Migration 153 held all of them NULL because `app.renders` did not exist.
    Migration 166 split them, and the split is the point:

      * the three EVIDENCE pins (a run case, a cohort member, a feedback
        annotation) now carry a composite FOREIGN KEY to
        `app.renders (id, org_id, project_id)` -- referencing another owner's
        object, never minting one;
      * `golden_question_versions.expected_render_ref` stays held NULL, and NOT
        because the object is missing: the ratified Golden Question contract has
        no expected-rendered-artifact field. A specification does not pin an
        instance a run produces.
    """
    with live_postgres.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_schema='app' AND table_name=%s AND column_name=%s",
            (table, column),
        )
        assert cur.fetchone(), f"app.{table}.{column} is not declared"

        cur.execute(
            "SELECT coalesce(string_agg(pg_get_constraintdef(oid), ' '), '') "
            "FROM pg_constraint WHERE conrelid = %s::regclass AND contype = 'c'",
            (f"app.{table}",),
        )
        checks = cur.fetchone()[0].lower()
        cur.execute(
            "SELECT coalesce(string_agg(pg_get_constraintdef(oid), ' '), '') "
            "FROM pg_constraint WHERE conrelid = %s::regclass AND contype = 'f'",
            (f"app.{table}",),
        )
        keys = cur.fetchone()[0].lower()

    if table == "golden_question_versions":
        assert column in checks, (
            f"app.{table}.{column} is no longer held NULL. The Golden Question "
            "contract has no expected-rendered-artifact field, so filling it would "
            "invent a product decision -- see migration 166"
        )
        return

    assert "references app.renders" in keys and column in keys, (
        f"app.{table}.{column} has no foreign key to app.renders. Since migration "
        "166 an evidence pin REFERENCES the governed object; being held NULL was "
        "only correct while that object did not exist"
    )
    assert "org_id" in keys and "project_id" in keys, (
        f"app.{table}.{column} references app.renders without its Project scope: a "
        "bare id lets a row point at another Project's Render"
    )


def test_migration_153_created_no_render_object_of_its_own():
    """The line this epic held -- stated about the migration, not the database.

    `app.renders` now EXISTS: the Epic 50 session landed it while Epic 51 was in
    flight, which is the outcome everyone wanted. The claim worth defending was
    never "no Render table exists anywhere"; it was that the EVALUATION layer did
    not invent one to fill a hole it was told to leave empty. So the assertion
    reads migration 153's own text: an epic that fabricates an identity it does
    not own would have to write it here.

    Its arrival is also the trigger for the follow-up work: once 50.4/50.5/50.7
    can pin a Render, the CHECKs above are lifted by their successor migration
    and the `unverifiable / render_owner_not_delivered` verdicts stop being the
    honest answer.
    """
    import pathlib
    import re

    sql = (
        pathlib.Path(__file__).resolve().parents[3]
        / "infra" / "nango" / "migrations" / "153_product_evaluation_evidence.sql"
    ).read_text(encoding="utf-8")

    assert not re.search(r"create\s+table[^;]*\brenders\b", sql, re.IGNORECASE), (
        "migration 153 creates a Render table: the evaluation layer must not own "
        "that identity -- stories 50.4/50.5/50.7 do"
    )
    assert "render_id" not in sql, (
        "migration 153 mints a `render_id`: the pin is declared as a reference to "
        "an object another story owns, never as an identity this one invents"
    )


# ===========================================================================
# RLS is on, forced, and the role running these tests is not exempt from it.
# ===========================================================================

_EVIDENCE_TABLES = (
    "golden_questions",
    "golden_question_versions",
    "golden_question_reference_paths",
    "observed_cohorts",
    "observed_cohort_members",
    "golden_question_proposals",
    "evaluation_run_profiles",
    "evaluation_context_version_sets",
    "evaluation_context_version_set_entries",
    "evaluation_runs",
    "evaluation_run_cases",
    "evaluation_case_dimension_verdicts",
    "evaluation_path_comparisons",
    "evaluation_baselines",
    "evaluation_comparisons",
    "evaluation_gate_decisions",
    "feedback_annotations",
    "feedback_reviews",
    "feedback_review_versions",
)


def test_the_role_running_this_suite_is_not_exempt_from_rls(live_postgres):
    """Without this, every isolation assertion above is vacuous.

    RLS is not applied to a superuser or to a role with BYPASSRLS, so a suite run
    as `postgres` proves nothing and says nothing about it.
    """
    with live_postgres.cursor() as cur:
        cur.execute(
            "SELECT rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user"
        )
        is_super, bypasses = cur.fetchone()
    assert not is_super, "these tests must not run as a superuser: RLS would not apply"
    assert not bypasses, "the role has BYPASSRLS: every isolation assertion would pass vacuously"


def test_every_evidence_table_has_rls_enabled_and_forced(live_postgres):
    """ENABLE alone is not enough: the table OWNER bypasses it without FORCE."""
    with live_postgres.cursor() as cur:
        cur.execute(
            """
            SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity
              FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
             WHERE n.nspname = 'app' AND c.relname = ANY(%s)
            """,
            (list(_EVIDENCE_TABLES),),
        )
        rows = cur.fetchall()

    found = {name for name, _, _ in rows}
    assert found == set(_EVIDENCE_TABLES), f"missing tables: {set(_EVIDENCE_TABLES) - found}"
    unprotected = [name for name, enabled, forced in rows if not (enabled and forced)]
    assert not unprotected, f"RLS not enabled+forced on: {unprotected}"


def test_every_evidence_table_carries_at_least_one_policy(live_postgres):
    """RLS with no policy denies everything, which reads as "secure" while the
    surface is simply broken. Both halves are checked, never one."""
    with live_postgres.cursor() as cur:
        cur.execute(
            "SELECT tablename, count(*) FROM pg_policies "
            "WHERE schemaname = 'app' AND tablename = ANY(%s) GROUP BY tablename",
            (list(_EVIDENCE_TABLES),),
        )
        counted = dict(cur.fetchall())
    missing = [t for t in _EVIDENCE_TABLES if counted.get(t, 0) < 1]
    assert not missing, f"RLS is on with no policy on: {missing}"


# ===========================================================================
# `latest` is refused by the database, not only by the service.
# ===========================================================================


@pytest.mark.parametrize("token", ["latest", "LATEST", " current ", "head"])
def test_the_database_itself_refuses_a_moving_version_pin(live_postgres, token):
    with live_postgres.cursor() as cur:
        cur.execute("SELECT app.is_exact_version_pin(%s)", (token,))
        assert cur.fetchone()[0] is False


def test_the_database_accepts_an_exact_version_pin(live_postgres):
    with live_postgres.cursor() as cur:
        cur.execute("SELECT app.is_exact_version_pin(%s)", ("svv_example_1",))
        assert cur.fetchone()[0] is True


# ===========================================================================
# Story 51.4 -- le gel d'appartenance a `resolved_at`, contre PostgreSQL
# ===========================================================================
#
# POURQUOI CES TESTS EXISTENT. La review du 2026-07-31 a renvoye 51.4 sur une
# reserve que personne n'avait mesuree : `test_trace_observation.py` collecte 92
# tests et ZERO n'atteint une base vivante
# (`grep -cE 'live_postgres|TEST_POSTGRES_DSN|psycopg\.connect'` -> 0). Le gel
# d'appartenance et le denominateur des agregats etaient donc prouves UNIQUEMENT
# en memoire. Ce n'etait pas un echec -- le rejeu etait vert. C'etait une absence :
# il n'y avait rien a rejouer la ou un trigger peut contredire le code.
#
# Ce que le gel promet, et que seule la base peut tenir : une fois la cohorte
# resolue, son appartenance ne bouge plus. Sinon une erasure ulterieure ameliore
# en silence un pourcentage historique -- c'est ecrit en toutes lettres au-dessus
# de `member_count` dans la migration 153.


def _cohort(chain, live_postgres, *, member_count: int = 1) -> str:
    """Une cohorte resolue, avec son `member_count` fige a `resolved_at`."""
    cohort_id = _uid("ocoh")
    with live_postgres.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.observed_cohorts
                (id, org_id, project_id, window_start, window_end,
                 resolved_at, member_count, filter_hash, content_hash, created_by)
            VALUES (%s, %s, %s, now() - interval '7 days', now(),
                    now(), %s, %s, %s, 'test')
            """,
            (cohort_id, chain.org_id, chain.project_id, member_count, _HASH, _HASH),
        )
    return cohort_id


def _path(chain, live_postgres) -> str:
    path_id = _uid("aip")
    with live_postgres.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.ai_paths
                (id, org_id, project_id, lifecycle, outcome, ended_at, content_hash,
                 actor, policy_snapshot_hash)
            VALUES (%s, %s, %s, 'finalized', 'succeeded', now(), %s,
                    'fixture-actor', %s)
            """,
            (path_id, chain.org_id, chain.project_id, _HASH, _HASH),
        )
    return path_id


def _member(chain, live_postgres, cohort_id: str, path_id: str) -> str:
    member_id = _uid("ocm")
    with live_postgres.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.observed_cohort_members
                (id, cohort_id, org_id, project_id, ai_path_id,
                 path_evidence_state, observed_at)
            VALUES (%s, %s, %s, %s, %s, 'observed', now())
            """,
            (member_id, cohort_id, chain.org_id, chain.project_id, path_id),
        )
    return member_id


def test_a_resolved_cohort_member_can_never_be_removed(live_postgres, chain):
    """Le coeur du gel : retirer un membre changerait un denominateur historique.

    C'est la moitie que le code ne peut pas tenir seul -- un DELETE direct ne
    passe par aucune fonction Python.
    """
    cohort_id = _cohort(chain, live_postgres)
    member_id = _member(chain, live_postgres, cohort_id, _path(chain, live_postgres))
    live_postgres.commit()

    with pytest.raises(psycopg.errors.IntegrityConstraintViolation, match="insert-once"):
        with live_postgres.cursor() as cur:
            cur.execute(
                "DELETE FROM app.observed_cohort_members WHERE id = %s", (member_id,)
            )
    live_postgres.rollback()


def test_a_resolved_cohort_member_can_never_be_reclassified(live_postgres, chain):
    """L'autre moitie, plus insidieuse que la suppression.

    Repasser un membre de `observed` a autre chose ne change pas le denominateur
    mais change le NUMERATEUR : la couverture s'ameliore sans qu'aucune observation
    nouvelle n'ait eu lieu.
    """
    cohort_id = _cohort(chain, live_postgres)
    member_id = _member(chain, live_postgres, cohort_id, _path(chain, live_postgres))
    live_postgres.commit()

    with pytest.raises(psycopg.errors.IntegrityConstraintViolation, match="insert-once"):
        with live_postgres.cursor() as cur:
            cur.execute(
                "UPDATE app.observed_cohort_members SET path_evidence_state = %s "
                "WHERE id = %s",
                ("unavailable", member_id),
            )
    live_postgres.rollback()


def test_the_stored_member_count_cannot_be_rewritten_after_resolution(
    live_postgres, chain
):
    """`member_count` est STOCKE plutot que compte a la lecture, exactement pour
    qu'une erasure ulterieure n'ameliore pas un pourcentage historique. Un
    denominateur reinscriptible annulerait ce choix."""
    cohort_id = _cohort(chain, live_postgres, member_count=10)
    live_postgres.commit()

    with pytest.raises(psycopg.errors.IntegrityConstraintViolation, match="insert-once"):
        with live_postgres.cursor() as cur:
            cur.execute(
                "UPDATE app.observed_cohorts SET member_count = 3 WHERE id = %s",
                (cohort_id,),
            )
    live_postgres.rollback()


def test_the_same_path_cannot_enter_one_cohort_twice(live_postgres, chain):
    """Un double comptage gonfle le denominateur sans nouvelle observation.

    Tenu par `uq_observed_cohort_members_path (cohort_id, ai_path_id)` -- une
    contrainte, pas une garde applicative : le chemin d'insertion n'est pas le
    seul qui existe.
    """
    cohort_id = _cohort(chain, live_postgres)
    path_id = _path(chain, live_postgres)
    _member(chain, live_postgres, cohort_id, path_id)
    live_postgres.commit()

    with pytest.raises(psycopg.errors.UniqueViolation):
        _member(chain, live_postgres, cohort_id, path_id)
    live_postgres.rollback()


def test_a_cohort_cannot_borrow_a_path_from_another_project(live_postgres, chain):
    """Le gel serait sans valeur si l'appartenance pouvait traverser les tenants.

    La FK porte `(ai_path_id, org_id, project_id)` et pas seulement l'id : c'est
    ce triplet qui rend l'emprunt inexprimable.
    """
    cohort_id = _cohort(chain, live_postgres)
    other = Chain(live_postgres).build()
    foreign_path = _path(other, live_postgres)
    live_postgres.commit()

    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        with live_postgres.cursor() as cur:
            cur.execute(
                """
                INSERT INTO app.observed_cohort_members
                    (id, cohort_id, org_id, project_id, ai_path_id,
                     path_evidence_state, observed_at)
                VALUES (%s, %s, %s, %s, %s, 'observed', now())
                """,
                (_uid("ocm"), cohort_id, chain.org_id, chain.project_id, foreign_path),
            )
    live_postgres.rollback()
