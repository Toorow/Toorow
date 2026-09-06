"""Story 60.3 -- the cleanup rule: bounded, refused, and never a fabricated row.

Offline (no DB): the bound on the pattern is REACHED AND EXCEEDED and the refusal
is read, because a bound that is only declared is a bound nobody proved. The
refusal codes are the `ExpressionError` family of `derived_columns.py:171-218` --
no code is invented here, and none comes from the adaptation sandbox: that path
was not taken (the story's arbitrage 7).

Live Postgres (skipped when TEST_POSTGRES_DSN is unset): the real DDL of
migration 240, whose CHECK carries the SAME length bound as the module. It is
duplicated on purpose -- `125_derived_columns_expressions.sql:89-93` dropped the
1..500 bound of `122:72` together with its column, and a bound that lives in one
place only is a bound the next migration carries away.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

from core import cleanup_rules as store  # noqa: E402
from core.cleanup_rules import ExpressionError  # noqa: E402

_REPO_ROOT = Path(__file__).resolve().parents[3]
MIGRATION_240 = (
    _REPO_ROOT
    / "infra"
    / "nango"
    / "migrations"
    / "240_a_cleanup_rule_stores_a_pattern_not_sql.sql"
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


# ---------------------------------------------------------------------------
# The bound. Reached, then EXCEEDED, and the refusal is read.
# ---------------------------------------------------------------------------


def test_a_pattern_exactly_on_the_bound_is_accepted():
    """The bound is inclusive, so the last accepted length is proven too.

    Without this, a test that only exceeds the bound would still pass against an
    off-by-one that refuses a legal pattern.
    """
    assert store.MAX_PATTERN_LENGTH == 500
    on_the_bound = "a" * store.MAX_PATTERN_LENGTH
    assert store.validate_pattern(on_the_bound) == on_the_bound


def test_a_pattern_over_the_bound_is_refused_with_the_existing_sentence():
    """The refusal names the bound, in the sentence `derived_columns.py:173` uses.

    The code is `ExpressionError` -- the family the story's arbitrage 6 named --
    and not `row_limit_exceeded`, `output_too_large` or `wall_time_exceeded`:
    those belong to the sandbox, and the sandbox is not on this path.
    """
    with pytest.raises(ExpressionError) as refused:
        store.validate_pattern("a" * (store.MAX_PATTERN_LENGTH + 1))
    assert f"exceeds {store.MAX_PATTERN_LENGTH} characters" in str(refused.value)


def test_an_empty_or_multiline_pattern_is_refused_before_anything_is_sent():
    for pattern in ("", "   ", "one\ntwo"):
        with pytest.raises(ExpressionError):
            store.validate_pattern(pattern)


def test_an_invalid_regular_expression_is_refused_with_the_engine_s_words():
    with pytest.raises(ExpressionError) as refused:
        store.validate_pattern("(unclosed")
    assert "not a valid regular expression" in str(refused.value)


@pytest.mark.parametrize(
    "pattern",
    [
        "(?=lookahead)",
        "(?!negative)",
        "(?<=behind)",
        r"(a)\1",
    ],
)
def test_a_construct_RE2_does_not_implement_is_refused_at_save_time(pattern):
    """Both warehouses run RE2. A rule RE2 refuses must fail HERE, not on a screen.

    This is also why the story could drop the ReDoS question rather than bound it
    with a process kill: RE2 is linear by construction, and what makes it linear
    is exactly the absence of these constructs.
    """
    with pytest.raises(ExpressionError) as refused:
        store.validate_pattern(pattern)
    assert "RE2" in str(refused.value)


def test_a_field_that_is_not_a_plain_identifier_is_refused():
    for field in ("", "a field", "`escape`", "x" * 200):
        with pytest.raises(ExpressionError):
            store.validate_field(field)


def test_an_unknown_kind_and_an_unknown_dialect_are_both_refused():
    with pytest.raises(ExpressionError):
        store.validate_kind("delete_everything")
    with pytest.raises(ExpressionError):
        store.compile_rule(
            rule_kind="exclude_row",
            source_field="campaign_name",
            pattern="_TEST_",
            dialect="postgres",
        )


# ---------------------------------------------------------------------------
# The condition, in words. `epic-60:98-99` asks for a sentence, not a regex.
# ---------------------------------------------------------------------------


def test_the_condition_is_a_sentence_a_person_can_read():
    assert store.describe("exclude_row", "campaign_name", "_TEST_") == (
        "Keeps a row only when campaign_name does not match `_TEST_`."
    )
    assert store.describe("keep_row", "campaign_name", "^BRAND") == (
        "Keeps a row only when campaign_name matches `^BRAND`."
    )
    assert store.describe("strip_match", "placement", r"\?utm_.*$") == (
        "Removes from placement every part that matches `\\?utm_.*$`."
    )


# ---------------------------------------------------------------------------
# The compiled SQL. The pattern is a PARAMETER, never a literal.
# ---------------------------------------------------------------------------


def test_the_pattern_never_enters_the_sql_string():
    """A quote in a pattern must not be able to end a literal -- because there is
    no literal. Every dialect binds it, as `warehouse._build_query` requires."""
    hostile = "it's a '; DROP TABLE"
    for dialect in store.DIALECTS:
        compiled = store.compile_rule(
            rule_kind="exclude_row",
            source_field="campaign_name",
            pattern=hostile,
            dialect=dialect,
        )
        assert hostile not in compiled.match_sql
        assert compiled.params == (hostile,)


def test_exclude_and_keep_are_opposite_predicates_and_strip_is_not_a_predicate():
    exclude = store.compile_rule(
        rule_kind="exclude_row", source_field="campaign_name", pattern="_TEST_", dialect="duckdb"
    )
    keep = store.compile_rule(
        rule_kind="keep_row", source_field="campaign_name", pattern="_TEST_", dialect="duckdb"
    )
    strip = store.compile_rule(
        rule_kind="strip_match", source_field="campaign_name", pattern="_TEST_", dialect="duckdb"
    )
    assert exclude.keep_sql == f"NOT ({exclude.match_sql})"
    assert keep.keep_sql == keep.match_sql
    # The effect counts what CHANGES: for keep_row that is the rows it drops.
    assert exclude.affected_sql == exclude.match_sql
    assert keep.affected_sql == f"NOT ({keep.match_sql})"
    # A rule that rewrites a field drops no row and offers no keep predicate.
    assert strip.keep_sql is None
    assert strip.projection_sql is not None
    assert exclude.projection_sql is None


def test_the_dry_run_reads_no_row_and_is_billed_nothing():
    """`WHERE FALSE`, the shape and the motive of `derived_columns.py:227-239`."""
    for dialect in store.DIALECTS:
        sql, params = store.build_dry_run_sql(
            rule_kind="exclude_row",
            source_field="campaign_name",
            pattern="_TEST_",
            dialect=dialect,
            mart_prefix="main_marts.",
        )
        assert sql.endswith("WHERE FALSE")
        assert "main_marts.fact_daily_kpi" in sql
        assert params == ["_TEST_"]


def test_the_effect_query_counts_over_a_bounded_window_and_names_the_field():
    sql, params = store.build_effect_sql(
        rule_kind="exclude_row",
        source_field="campaign_name",
        pattern="_TEST_",
        dialect="duckdb",
        mart_prefix="main_marts.",
        project_id="proj_EXAMPLE",
        start_date="2026-07-10",
        end_date="2026-08-08",
    )
    assert "COUNT(*) AS affected_rows" in sql
    assert "date BETWEEN" in sql
    assert params == ["proj_EXAMPLE", "2026-07-10", "2026-08-08", "campaign_name", "_TEST_"]
    assert store.EFFECT_WINDOW_DAYS == 30


# ---------------------------------------------------------------------------
# The preview. Bounded, and it fabricates nothing.
# ---------------------------------------------------------------------------


def test_the_preview_is_bounded_between_one_and_two_hundred_rows():
    for limit in (0, 201, 1000):
        with pytest.raises(ExpressionError) as refused:
            store.build_preview_sql(
                rule_kind="exclude_row",
                source_field="campaign_name",
                pattern="_TEST_",
                dialect="duckdb",
                mart_prefix="main_marts.",
                project_id="proj_EXAMPLE",
                limit=limit,
            )
        assert "between 1 and 200 rows" in str(refused.value)


def test_the_preview_carries_the_source_value_beside_the_verdict():
    sql, params = store.build_preview_sql(
        rule_kind="strip_match",
        source_field="placement",
        pattern="utm_",
        dialect="bigquery",
        mart_prefix="marts_x.",
        project_id="proj_EXAMPLE",
        limit=5,
    )
    assert "breakdown_value" in sql
    assert "AS becomes" in sql  # what the value becomes, next to what it was
    assert sql.endswith("LIMIT 5")
    # In order of APPEARANCE in the statement, because a positional driver binds
    # by appearance: the two pattern fragments sit in the SELECT list, ahead of
    # the WHERE clause.
    assert params == ["utm_", "utm_", "proj_EXAMPLE", "placement"]


def test_a_warehouse_that_refuses_yields_an_error_and_NOT_ONE_fabricated_row():
    """The refusal of `derived_columns.preview_expression:304-308`, held to.

    A preview that invented values would be worse than no preview, because its
    whole purpose is to be believed.
    """

    def _explode(sql, params):
        raise RuntimeError("the warehouse is unreachable")

    result = store.preview_rule(
        rule_kind="exclude_row",
        source_field="campaign_name",
        pattern="_TEST_",
        dialect="duckdb",
        mart_prefix="main_marts.",
        project_id="proj_EXAMPLE",
        run_query=_explode,
    )
    assert result.ok is False
    assert result.rows == ()
    assert "unreachable" in (result.error or "")


def test_a_preview_that_matched_nothing_is_an_empty_answer_not_an_error():
    result = store.preview_rule(
        rule_kind="exclude_row",
        source_field="campaign_name",
        pattern="_TEST_",
        dialect="duckdb",
        mart_prefix="main_marts.",
        project_id="proj_EXAMPLE",
        run_query=lambda sql, params: [],
    )
    assert result.ok is True
    assert result.rows == ()


# ---------------------------------------------------------------------------
# The reach. Read BEFORE the change, and an unreadable reach is not a zero.
# ---------------------------------------------------------------------------


def test_an_unreadable_reach_RAISES_rather_than_answering_zero():
    class _Exploding:
        def cursor(self):
            raise RuntimeError("the Datastream store is unreachable")

    with pytest.raises(store.CleanupRuleImpactUnavailable):
        store.assess_rule_impact(_Exploding(), project_id="proj_EXAMPLE", datastream_id=None)


# ---------------------------------------------------------------------------
# Live Postgres: the SAME bound, in the schema.
# ---------------------------------------------------------------------------


def test_the_migration_declares_the_same_bound_as_the_module():
    """The two must not drift: the bound was lost once already (125:89-93)."""
    sql = MIGRATION_240.read_text(encoding="utf-8")
    assert f"BETWEEN 1 AND {store.MAX_PATTERN_LENGTH}" in sql
    for kind in store.RULE_KINDS:
        assert f"'{kind}'" in sql


@pytest.fixture
def fixture_project(request):
    """One org, one project and two Datastreams, dropped at the end of the test."""
    from core.db import get_connection

    org_id, project_id = _uid("org"), _uid("proj")
    datastreams = [_uid("ds"), _uid("ds")]
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO app.organizations (id, name, slug, status, created_by) "
                "VALUES (%s, 'Story 60.3 fixture', %s, 'active', 'owner@example.com')",
                (org_id, org_id.replace("_", "-")),
            )
            cur.execute(
                "INSERT INTO app.projects (id, org_id, name, slug, created_by) "
                "VALUES (%s, %s, 'Story 60.3 fixture', %s, 'owner@example.com')",
                (project_id, org_id, project_id.replace("_", "-")),
            )
            for index, datastream_id in enumerate(datastreams, start=1):
                cur.execute(
                    "INSERT INTO app.datastreams "
                    "(id, project_id, org_id, name, module_name, enabled) "
                    "VALUES (%s, %s, %s, %s, 'example_module', TRUE)",
                    (datastream_id, project_id, org_id, f"Story 60.3 fixture {index}"),
                )
        conn.commit()

    def _drop():
        # Torn down through the repository's own eraser, which walks the FOREIGN
        # KEY graph: a hand-written DELETE list leaves whatever a trigger created
        # behind (creating a Project mints its capabilities) and fails on their FK.
        # The cleanup rules themselves are NOT in that plan and do not need to be:
        # their FK is ON DELETE CASCADE, so the org row takes them -- which is the
        # measurement migration 240's header states.
        from tests.conftest import purge_fixture_org  # noqa: PLC0415

        with get_connection() as cleanup:
            purge_fixture_org(cleanup, org_id)
            cleanup.commit()

    request.addfinalizer(_drop)
    return org_id, project_id, datastreams


@pg_available
def test_postgres_refuses_a_pattern_over_the_bound_even_without_the_module(fixture_project):
    """The database is the second holder of the bound, and it is asked directly.

    The module is bypassed on purpose: this proves the CHECK, not the Python.
    """
    import psycopg
    from core.db import get_connection

    org_id, project_id, _ = fixture_project
    with get_connection() as conn:
        with pytest.raises(psycopg.errors.CheckViolation):
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO app.cleanup_rules (id, org_id, project_id, name, "
                    "source_field, rule_kind, pattern, dry_run_state, created_by) "
                    "VALUES (%s, %s, %s, 'Too long', 'campaign_name', 'exclude_row', "
                    "%s, 'not_attempted', 'owner@example.com')",
                    (_uid("crule"), org_id, project_id, "a" * 501),
                )
        conn.rollback()


@pg_available
def test_postgres_refuses_a_rule_kind_the_module_does_not_emit(fixture_project):
    import psycopg
    from core.db import get_connection

    org_id, project_id, _ = fixture_project
    with get_connection() as conn:
        with pytest.raises(psycopg.errors.CheckViolation):
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO app.cleanup_rules (id, org_id, project_id, name, "
                    "source_field, rule_kind, pattern, dry_run_state, created_by) "
                    "VALUES (%s, %s, %s, 'Unknown kind', 'campaign_name', 'drop_table', "
                    "'_TEST_', 'not_attempted', 'owner@example.com')",
                    (_uid("crule"), org_id, project_id),
                )
        conn.rollback()


@pg_available
def test_a_rule_bound_to_no_datastream_reaches_every_datastream_of_the_project(
    fixture_project,
):
    """The count in the confirmation is the count BEFORE the act, and it is real."""
    from core.db import get_connection

    org_id, project_id, datastreams = fixture_project
    with get_connection() as conn:
        impact = store.assess_rule_impact(conn, project_id=project_id, datastream_id=None)
        assert impact.datastream_count == len(datastreams)
        assert impact.describe() == f"{len(datastreams)} Datastreams"

        rule = store.create_rule(
            conn,
            org_id=org_id,
            project_id=project_id,
            datastream_id=None,
            name="Drop the test campaigns",
            source_field="campaign_name",
            rule_kind="exclude_row",
            pattern="_TEST_",
            identity="owner@example.com",
            dry_run_state="not_attempted",
            dry_run_detail="duckdb: no local warehouse file is present",
        )
        conn.commit()
        assert rule["datastream_count"] == len(datastreams)
        assert rule["condition"] == (
            "Keeps a row only when campaign_name does not match `_TEST_`."
        )
        assert rule["dry_run_state"] == "not_attempted"

        bound = store.create_rule(
            conn,
            org_id=org_id,
            project_id=project_id,
            datastream_id=datastreams[0],
            name="Drop the test campaigns",
            source_field="campaign_name",
            rule_kind="exclude_row",
            pattern="_TEST_",
            identity="owner@example.com",
            dry_run_state="passed",
        )
        conn.commit()
        # Same name, a different reach: the unique index keys on both.
        assert bound["datastream_count"] == 1

        # And the Processing tab reads both, because a Project-wide rule reaches
        # this Datastream too.
        chain = store.list_rules_for_datastream(
            conn, project_id=project_id, datastream_id=datastreams[0]
        )
        assert {row["scope"] for row in chain} == {"project", "datastream"}
        chain_other = store.list_rules_for_datastream(
            conn, project_id=project_id, datastream_id=datastreams[1]
        )
        assert [row["scope"] for row in chain_other] == ["project"]


@pg_available
def test_two_rules_of_the_same_reach_cannot_carry_the_same_name(fixture_project):
    from core.db import get_connection

    org_id, project_id, _ = fixture_project
    with get_connection() as conn:
        store.create_rule(
            conn,
            org_id=org_id,
            project_id=project_id,
            datastream_id=None,
            name="Drop the test campaigns",
            source_field="campaign_name",
            rule_kind="exclude_row",
            pattern="_TEST_",
            identity="owner@example.com",
            dry_run_state="passed",
        )
        conn.commit()
        with pytest.raises(store.CleanupRuleConflict):
            store.create_rule(
                conn,
                org_id=org_id,
                project_id=project_id,
                datastream_id=None,
                name="drop THE test campaigns",
                source_field="campaign_name",
                rule_kind="keep_row",
                pattern="_OTHER_",
                identity="owner@example.com",
                dry_run_state="passed",
            )
        conn.rollback()


@pg_available
def test_a_rule_is_disabled_and_deleted_without_touching_anything_collected(
    fixture_project,
):
    from core.db import get_connection

    org_id, project_id, _ = fixture_project
    with get_connection() as conn:
        rule = store.create_rule(
            conn,
            org_id=org_id,
            project_id=project_id,
            datastream_id=None,
            name="Drop the test campaigns",
            source_field="campaign_name",
            rule_kind="exclude_row",
            pattern="_TEST_",
            identity="owner@example.com",
            dry_run_state="passed",
        )
        conn.commit()
        disabled = store.update_rule(
            conn,
            rule_id=rule["id"],
            project_id=project_id,
            identity="owner@example.com",
            enabled=False,
        )
        conn.commit()
        assert disabled["enabled"] is False
        # The pattern is re-validated on every write, so an edit cannot smuggle in
        # what a creation refuses.
        with pytest.raises(ExpressionError):
            store.update_rule(
                conn,
                rule_id=rule["id"],
                project_id=project_id,
                identity="owner@example.com",
                pattern="a" * 501,
            )
        conn.rollback()

        assert store.delete_rule(
            conn, rule_id=rule["id"], project_id=project_id, identity="owner@example.com"
        )["deleted"]
        conn.commit()
        with pytest.raises(store.CleanupRuleNotFound):
            store.get_rule(conn, rule_id=rule["id"], project_id=project_id)


@pg_available
def test_a_rule_of_another_project_is_not_found_rather_than_refused(fixture_project):
    """AD-5 by construction: a wrong Project returns nothing, it does not 403."""
    from core.db import get_connection

    org_id, project_id, _ = fixture_project
    with get_connection() as conn:
        rule = store.create_rule(
            conn,
            org_id=org_id,
            project_id=project_id,
            datastream_id=None,
            name="Drop the test campaigns",
            source_field="campaign_name",
            rule_kind="exclude_row",
            pattern="_TEST_",
            identity="owner@example.com",
            dry_run_state="passed",
        )
        conn.commit()
        with pytest.raises(store.CleanupRuleNotFound):
            store.get_rule(conn, rule_id=rule["id"], project_id=_uid("proj"))
        assert store.list_rules(conn, project_id=_uid("proj")) == []
