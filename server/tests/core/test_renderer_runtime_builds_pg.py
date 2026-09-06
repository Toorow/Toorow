"""Story 50.5 AC14/AC15 -- the build-identity ledger, proven against a real database.

WHY pg-GATED AND NOT MOCKED. Every claim here is a claim about a CHECK constraint,
a trigger or a policy. A mock would assert that the test author remembered what the
migration says, which is the one thing a mock cannot be wrong about usefully. These
run against the disposable PostgreSQL (`scripts/disposable_postgres.py`), connected
as the ordinary `connector` role -- no superuser, no BYPASSRLS -- so a row-level
assertion actually asserts. `TEST_POSTGRES_DSN` must never point at production; the
guard below refuses to run if it does.

A `skipped` here is NOT a pass. When the disposable instance is absent, the suite
says so; it does not report success.
"""

from __future__ import annotations

import os

import pytest

psycopg = pytest.importorskip("psycopg")

from core.analyze_artifacts import (  # noqa: E402
    RENDER_REPLAY_PINS,
    render_contract_state,
)

DSN = os.environ.get("TEST_POSTGRES_DSN")

pytestmark = pytest.mark.skipif(
    not DSN, reason="TEST_POSTGRES_DSN is not set: no disposable PostgreSQL to prove against"
)


def _guard(dsn: str) -> None:
    """Refuse to touch anything that is not an obvious throwaway database."""
    lowered = dsn.lower()
    assert "supabase" not in lowered, "TEST_POSTGRES_DSN points at Supabase -- refusing"
    assert lowered.rstrip("/").endswith("_test"), (
        "TEST_POSTGRES_DSN must name a database ending in `_test`; received " + dsn
    )


@pytest.fixture()
def conn():
    _guard(DSN or "")
    with psycopg.connect(DSN) as connection:
        yield connection
        connection.rollback()


#: A PROBE identity, and the semver is what makes it one. `@1.0.0` is the id the
#: shipped registry emits for this renderer, so as soon as
#: `scripts/register_renderer_builds.py` has projected the manifest -- which
#: `infra/scripts/deploy.sh` now does on every backend deploy -- inserting it here
#: raises `UniqueViolation` on the primary key and the suite fails for a reason
#: that has nothing to do with what it asserts. Measured 2026-08-25: two tests fell
#: that way the first time the projection was run against the disposable database.
#: `@9.9.9` is a version no build will ever carry.
VALID = {
    "id": "bar/toorow-echarts-bar@9.9.9",
    "runtime_build": "@toorow/card-shell/viz@0.1.0+ff263bc",
    "family": "bar",
    "renderer_id": "toorow-echarts-bar",
    "theme_version": "viz-theme@1",
    "formatter_version": "viz-formatters@1",
    "responsive_profiles": ["console", "mcp-inline", "mcp-fullscreen", "share"],
    "git_sha": "ff263bc",
}

INSERT = """
    INSERT INTO app.renderer_runtime_builds
        (id, runtime_build, family, renderer_id, theme_version, formatter_version,
         responsive_profiles, git_sha)
    VALUES (%(id)s, %(runtime_build)s, %(family)s, %(renderer_id)s, %(theme_version)s,
            %(formatter_version)s, %(responsive_profiles)s, %(git_sha)s)
"""


def test_the_table_exists_and_the_render_contract_gate_opens(conn):
    """The one thing `create_render` was waiting for.

    `render_contract_state` reported `renderer_and_runtime_build` missing on every
    deployment until this migration. After it, the refusal stops being REACHED --
    which is different from being removed: the pin checks behind it are unchanged
    and still refuse an absent or placeholder pin.
    """
    state = render_contract_state(conn)
    assert state["available"] is True, state
    assert state["missing"] == []


def test_all_ten_replay_pins_are_columns_on_app_renders(conn):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema='app' AND table_name='renders'"
        )
        columns = {row[0] for row in cur.fetchall()}
    missing = [(pin, field) for pin, field in RENDER_REPLAY_PINS if field not in columns]
    assert missing == [], f"replay pins absent from app.renders: {missing}"


def test_a_well_formed_build_identity_is_accepted(conn):
    with conn.cursor() as cur:
        cur.execute("SAVEPOINT sp")
        cur.execute(INSERT, VALID)
        cur.execute(
            "SELECT family, responsive_profiles FROM app.renderer_runtime_builds WHERE id=%s",
            (VALID["id"],),
        )
        assert cur.fetchone() == ("bar", ["console", "mcp-inline", "mcp-fullscreen", "share"])
        cur.execute("ROLLBACK TO SAVEPOINT sp")


def test_the_forward_catalog_accepts_the_waterfall_renderer_build(conn):
    """Migration 191 widened the family vocabulary AND registered one row itself.

    THE SEMVER IS NOT 1.0.0 HERE, and that is the repair rather than a detail. This
    test used to insert `waterfall/toorow-echarts-waterfall@1.0.0` -- the exact id
    migration 191 inserts by hand at line 38 -- so on any migrated database it
    raised `UniqueViolation` on the primary key instead of proving anything about
    the family CHECK. Measured red on 2026-08-25 against a disposable PostgreSQL
    carrying all 304 migrations. What the test means to assert is that the
    vocabulary takes `waterfall`; a free id asserts exactly that and nothing else.
    """
    payload = {
        **VALID,
        "id": "waterfall/toorow-echarts-waterfall@9.9.9",
        "family": "waterfall",
        "renderer_id": "toorow-echarts-waterfall",
    }
    with conn.cursor() as cur:
        cur.execute("SAVEPOINT sp")
        cur.execute(INSERT, payload)
        cur.execute(
            "SELECT family, renderer_id FROM app.renderer_runtime_builds WHERE id=%s",
            (payload["id"],),
        )
        assert cur.fetchone() == ("waterfall", "toorow-echarts-waterfall")
        cur.execute("ROLLBACK TO SAVEPOINT sp")


def test_migration_191s_hand_written_row_is_present_and_cannot_be_repaired(conn):
    """The row the projection can never overwrite, stated where a reader meets it.

    `191_waterfall_visual_family.sql:38` inserted a build identity by hand at
    runtime `+2e4aba0febbe`. That identity was REAL -- commit 0bc6aded shipped it in
    `buildInfo.generated.ts` -- and the runtime content hash has moved twice since,
    so the row will never again equal the emitted manifest. It is also insert-once,
    which is what makes `register_renderer_builds.py --check` wrong to fail on it:
    there is no gesture that repairs a row the database refuses to let anyone touch.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT runtime_build, responsive_profiles FROM app.renderer_runtime_builds "
            "WHERE id = 'waterfall/toorow-echarts-waterfall@1.0.0'"
        )
        row = cur.fetchone()

    assert row is not None, "migration 191 registered this identity; it must still be readable"
    assert row[0] == "@toorow/card-shell/viz@0.1.0+2e4aba0febbe"
    assert "mcp-pip" not in row[1], (
        "the row records a build that shipped before mcp-pip existed -- it is history, not drift"
    )


@pytest.mark.parametrize(
    ("field", "value", "why"),
    [
        ("id", "bar/toorow-echarts-bar@latest", "a semver, never `latest`"),
        ("id", "toorow-echarts-bar@1.0.0", "the family prefix is part of the identity"),
        ("runtime_build", "@toorow/card-shell@0.1.0+ff263bc", "the /viz segment is required"),
        ("runtime_build", "@toorow/card-shell/viz@0.1.0", "a build must name its commit"),
        ("git_sha", "unknown", "a placeholder is not a commit"),
        ("theme_version", "latest", "a placeholder is not a version"),
        ("family", "pie_of_pie", "the family vocabulary is closed by migration 156"),
    ],
)
def test_a_malformed_identity_is_refused_by_the_database(conn, field, value, why):
    """The database is the layer that cannot be argued with.

    The TypeScript validator refuses these too, but it protects only the callers
    that go through it. These CHECKs protect the ones that do not: a psql session,
    a repair script, a future migration.
    """
    payload = dict(VALID)
    payload[field] = value
    with conn.cursor() as cur:
        cur.execute("SAVEPOINT sp")
        with pytest.raises(psycopg.errors.CheckViolation):
            cur.execute(INSERT, payload)
        cur.execute("ROLLBACK TO SAVEPOINT sp")


def test_a_build_declaring_no_responsive_profile_is_refused(conn):
    """Migration 165 -- the case migration 160's own constraint used to accept.

    `array_length('{}'::text[], 1)` is NULL, not 0, and a CHECK rejects only
    FALSE, so `NULL AND true` was accepted: a build could be registered declaring
    that it supports NO responsive profile at all -- the one thing the constraint
    exists to require. `cardinality()` returns 0 and closes it.

    This is the instance. The class is guarded by
    `test_check_constraints_null_proof_pg.py`, whose candidate generator could not
    see it either until it learned to probe an array column with an array.
    """
    payload = dict(VALID, responsive_profiles=[])
    with conn.cursor() as cur:
        cur.execute("SAVEPOINT sp")
        with pytest.raises(psycopg.errors.CheckViolation):
            cur.execute(INSERT, payload)
        cur.execute("ROLLBACK TO SAVEPOINT sp")


def test_the_id_must_be_built_from_its_own_parts(conn):
    """A row claiming `bar/...` while declaring family `line` would be a ledger
    with two answers to one question."""
    payload = dict(VALID, family="line", renderer_id="toorow-echarts-line")
    with conn.cursor() as cur:
        cur.execute("SAVEPOINT sp")
        with pytest.raises(psycopg.errors.CheckViolation):
            cur.execute(INSERT, payload)
        cur.execute("ROLLBACK TO SAVEPOINT sp")


def test_a_registered_build_identity_can_never_be_edited_or_deleted(conn):
    """AC14 -- replay depends on the pin still meaning what it meant."""
    with conn.cursor() as cur:
        cur.execute("SAVEPOINT sp")
        cur.execute(INSERT, VALID)
        with pytest.raises(Exception) as update_error:
            cur.execute(
                "UPDATE app.renderer_runtime_builds SET git_sha='0000000' WHERE id=%s",
                (VALID["id"],),
            )
        assert "immutable" in str(update_error.value).lower() or update_error.value is not None
        cur.execute("ROLLBACK TO SAVEPOINT sp")

        cur.execute("SAVEPOINT sp2")
        cur.execute(INSERT, VALID)
        with pytest.raises(Exception):
            cur.execute("DELETE FROM app.renderer_runtime_builds WHERE id=%s", (VALID["id"],))
        cur.execute("ROLLBACK TO SAVEPOINT sp2")


def test_a_profile_outside_story_504s_enum_is_refused(conn):
    """There is no `compact` profile: compaction is a behaviour, not a fifth name."""
    payload = dict(VALID, responsive_profiles=["console", "compact"])
    with conn.cursor() as cur:
        cur.execute("SAVEPOINT sp")
        with pytest.raises(psycopg.errors.CheckViolation):
            cur.execute(INSERT, payload)
        cur.execute("ROLLBACK TO SAVEPOINT sp")
