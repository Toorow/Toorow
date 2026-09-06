"""Story 67.20 -- a declared source currency is a version, not an overwritten row.

The defect this pins, stated exactly: `app.fx_conflict_resolutions` was listed by
migration 145 among the seven stores that "stop being AUTHORITIES", and it was
still written by the console and still read by nine dbt staging models. Its write
was an `ON CONFLICT DO UPDATE` that overwrote `decided_by` and `decided_at` in
place, so re-declaring one currency erased who had decided it -- and, because the
set was one row per pair, editing one pair could not preserve the attribution of
the others either.

Offline (no DB): the rule normalizer, which is PURE. Ordering is content, a
duplicate pair is refused rather than resolved by write order, an unknown key is
refused rather than dropped, and the currency comes from the governed ISO 4217
vocabulary rather than a second hand-written list.

Live Postgres (skipped when TEST_POSTGRES_DSN is unset): the real DDL of migration
282 -- declaring publishes an immutable version, the projection dbt reads carries
the published set, a NEIGHBOURING declaration keeps its own actor and timestamp
when another is re-declared, withdrawing leaves the withdrawn one readable in the
superseded version, and the dethroned table refuses every write.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

from core import source_currency_bindings as bindings  # noqa: E402
from core.governance_rule_sets import RuleSetError, load_profiles  # noqa: E402

_REPO_ROOT = Path(__file__).resolve().parents[3]
MIGRATION_282 = (
    _REPO_ROOT
    / "infra"
    / "nango"
    / "migrations"
    / "282_a_declared_source_currency_is_a_version_not_a_row.sql"
)

IDENTITY = "owner@example.com"
ALICE = "alice@example.com"
BOB = "bob@example.com"


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
# Offline: the normalizer, which is what makes the content hash mean something.
# ---------------------------------------------------------------------------


def _rule(**overrides):
    rule = {
        "target_field": "cost",
        "source_module": "meta-ads",
        "source_currency": "USD",
        "declared_by": ALICE,
        "declared_at": "2026-08-17T09:00:00+00:00",
        "note": None,
    }
    rule.update(overrides)
    return rule


def test_order_is_content_so_two_equal_sets_normalize_identically():
    left = bindings.validate_binding_rules(
        [_rule(source_module="tiktok-ads"), _rule(source_module="meta-ads")]
    )
    right = bindings.validate_binding_rules(
        [_rule(source_module="meta-ads"), _rule(source_module="tiktok-ads")]
    )
    assert left == right
    assert [item["source_module"] for item in left] == ["meta-ads", "tiktok-ads"]


def test_a_pair_declared_twice_is_refused_rather_than_resolved_by_write_order():
    with pytest.raises(RuleSetError) as excinfo:
        bindings.validate_binding_rules(
            [_rule(source_currency="USD"), _rule(source_currency="GBP")]
        )
    assert "declared twice" in str(excinfo.value)


def test_an_unknown_key_is_refused_rather_than_dropped():
    with pytest.raises(RuleSetError) as excinfo:
        bindings.validate_binding_rules([_rule(currency="USD")])
    # The caller who sent `currency` instead of `source_currency` is told, not
    # silently published with whatever survived.
    assert "currency" in str(excinfo.value)


def test_a_currency_outside_the_governed_vocabulary_is_refused():
    with pytest.raises(RuleSetError):
        bindings.validate_binding_rules([_rule(source_currency="XXX")])


def test_a_declaration_with_no_actor_is_refused():
    rule = _rule()
    rule["declared_by"] = ""
    with pytest.raises(RuleSetError) as excinfo:
        bindings.validate_binding_rules([rule])
    assert "declared_by" in str(excinfo.value)


def test_the_payload_of_this_family_carries_no_keys():
    # The whole content is the ordered rules; a payload key would be a second
    # place to write the same fact.
    assert bindings._validate_binding_payload({}) == {}
    with pytest.raises(RuleSetError):
        bindings._validate_binding_payload({"reporting_currency": "EUR"})


def test_the_profile_is_registered_on_the_living_substrate():
    load_profiles()
    from core.governance_rule_sets import get_profile

    profile = get_profile(bindings.PROFILE_SOURCE_CURRENCY)
    assert profile.family == bindings.FAMILY_SOURCE_CURRENCY
    assert profile.validate_rules is not None


# ---------------------------------------------------------------------------
# The migration itself: what it claims in SQL.
# ---------------------------------------------------------------------------


def test_the_migration_seals_the_dethroned_store_and_keeps_the_erasure_hatch():
    sql = MIGRATION_282.read_text(encoding="utf-8")
    # It refuses rather than guessing when the dethroned store still holds rows.
    assert "still holds %" in sql
    # It projects the published version for dbt.
    assert "CREATE OR REPLACE VIEW app.fx_source_currency_bindings_v" in sql
    # INSERT/UPDATE are sealed unconditionally...
    assert "BEFORE INSERT OR UPDATE ON app.fx_conflict_resolutions" in sql
    # ...and the DELETE guard still yields to a flagged tenant erasure (277/278).
    assert "current_setting('app.rgpd_erasure', true) IS DISTINCT FROM 'on'" in sql


def test_no_dbt_staging_model_still_reads_the_dethroned_relation():
    """The last reader is what migration 145 was waiting for."""
    staging = sorted((_REPO_ROOT / "server" / "modules").glob("*/dbt/staging/*.sql"))
    assert staging, "no staging models found -- the guard would pass vacuously"
    offenders = [
        path.relative_to(_REPO_ROOT).as_posix()
        for path in staging
        if "fx_conflict_resolutions" in path.read_text(encoding="utf-8")
    ]
    assert offenders == []


# ---------------------------------------------------------------------------
# Live Postgres.
# ---------------------------------------------------------------------------


@pytest.fixture
def fixture_org():
    """One org and one project, dropped at the end of the test."""
    from core.db import get_connection

    org_id, project_id = _uid("org"), _uid("proj")
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO app.organizations (id, name, slug, status, created_by) "
                "VALUES (%s, 'Story 67.20 fixture', %s, 'active', %s)",
                (org_id, org_id.replace("_", "-"), IDENTITY),
            )
            cur.execute(
                "INSERT INTO app.projects (id, org_id, name, slug, created_by) "
                "VALUES (%s, %s, 'Story 67.20 fixture', %s, %s)",
                (project_id, org_id, project_id.replace("_", "-"), IDENTITY),
            )
        conn.commit()
    yield {"org_id": org_id, "project_id": project_id}
    from tests.conftest import purge_fixture_org

    with get_connection() as conn:
        purge_fixture_org(conn, org_id)
        conn.commit()


@pg_available
def test_declaring_publishes_a_version_that_the_dbt_projection_carries(fixture_org):
    from core.db import get_connection

    project_id = fixture_org["project_id"]
    load_profiles()
    with get_connection() as conn:
        declared = bindings.declare_binding(
            conn,
            project_id=project_id,
            target_field="cost",
            source_module="meta-ads",
            source_currency="USD",
            actor=ALICE,
            note="platform bills in USD",
        )
        conn.commit()

        assert declared["resolved_source_currency"] == "USD"
        assert declared["decided_by"] == ALICE
        assert declared["rule_set_version_id"].startswith("grsv_")

        # The version is real and published, not a row in a flat table.
        with conn.cursor() as cur:
            cur.execute(
                "SELECT status, family, jsonb_array_length(ordered_rules) "
                "FROM app.governance_rule_set_versions WHERE id = %s",
                (declared["rule_set_version_id"],),
            )
            status, family, rule_count = cur.fetchone()
        assert status == "published"
        assert family == bindings.FAMILY_SOURCE_CURRENCY
        assert rule_count == 1

        # And the projection dbt joins carries it, in the old flat shape.
        with conn.cursor() as cur:
            cur.execute(
                "SELECT target_field, source_module, resolved_source_currency, decided_by "
                "FROM app.fx_source_currency_bindings_v WHERE project_id = %s",
                (project_id,),
            )
            assert cur.fetchall() == [("cost", "meta-ads", "USD", ALICE)]


@pg_available
def test_redeclaring_one_binding_does_not_restamp_its_neighbour(fixture_org):
    """THE defect. The old upsert re-stamped nothing else -- because it could not
    hold anything else -- but the set is now published whole, so the risk moves to
    losing the untouched entries' attribution. It must not."""
    from core.db import get_connection

    project_id = fixture_org["project_id"]
    load_profiles()
    with get_connection() as conn:
        bindings.declare_binding(
            conn, project_id=project_id, target_field="cost",
            source_module="tiktok-ads", source_currency="GBP", actor=BOB,
        )
        conn.commit()
        first = bindings.get_binding(
            conn, project_id=project_id, target_field="cost", source_module="tiktok-ads"
        )

        bindings.declare_binding(
            conn, project_id=project_id, target_field="cost",
            source_module="meta-ads", source_currency="USD", actor=ALICE,
        )
        conn.commit()

        after = bindings.get_binding(
            conn, project_id=project_id, target_field="cost", source_module="tiktok-ads"
        )
        assert after["decided_by"] == BOB
        assert after["decided_at"] == first["decided_at"]
        assert after["resolved_source_currency"] == "GBP"

        # Two acts, two versions; the first is superseded, never edited.
        with conn.cursor() as cur:
            cur.execute(
                "SELECT status FROM app.governance_rule_set_versions "
                "WHERE project_id = %s ORDER BY version_number",
                (project_id,),
            )
            assert [row[0] for row in cur.fetchall()] == ["superseded", "published"]


@pg_available
def test_withdrawing_leaves_the_withdrawn_declaration_readable(fixture_org):
    from core.db import get_connection

    project_id = fixture_org["project_id"]
    load_profiles()
    with get_connection() as conn:
        declared = bindings.declare_binding(
            conn, project_id=project_id, target_field="cost",
            source_module="meta-ads", source_currency="USD", actor=ALICE,
        )
        conn.commit()
        bindings.withdraw_binding(
            conn, project_id=project_id, target_field="cost",
            source_module="meta-ads", actor=BOB,
        )
        conn.commit()

        # Gone from what is in force...
        assert bindings.list_bindings(conn, project_id=project_id) == []
        # ...and still readable in the version that was in force when a figure
        # was published under it.
        with conn.cursor() as cur:
            cur.execute(
                "SELECT ordered_rules FROM app.governance_rule_set_versions WHERE id = %s",
                (declared["rule_set_version_id"],),
            )
            kept = cur.fetchone()[0]
        assert kept[0]["source_currency"] == "USD"
        assert kept[0]["declared_by"] == ALICE

        with pytest.raises(RuleSetError):
            bindings.withdraw_binding(
                conn, project_id=project_id, target_field="cost",
                source_module="meta-ads", actor=BOB,
            )


@pg_available
def test_the_dethroned_store_refuses_every_write(fixture_org):
    import psycopg
    from core.db import get_connection

    project_id = fixture_org["project_id"]
    with get_connection() as conn:
        # SQLSTATE 23000, the class migration 144 already uses to freeze a
        # published version -- so a sealed store refuses in the same language.
        with pytest.raises(psycopg.errors.IntegrityConstraintViolation) as excinfo:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO app.fx_conflict_resolutions "
                    "(project_id, target_field, source_module, "
                    " resolved_source_currency, decided_by) "
                    "VALUES (%s, 'cost', 'meta-ads', 'USD', %s)",
                    (project_id, ALICE),
                )
        conn.rollback()
    # The refusal names the door that works, not only the cause.
    assert "source_currency" in str(excinfo.value)


@pg_available
def test_the_seam_the_console_calls_now_writes_the_living_store(fixture_org):
    """`conflict_resolutions` kept its signatures; only the store moved."""
    from core.conflict_resolutions import (
        delete_fx_resolution,
        get_fx_resolution,
        list_fx_resolutions,
        upsert_fx_resolution,
    )
    from core.db import get_connection

    project_id = fixture_org["project_id"]
    load_profiles()
    with get_connection() as conn:
        row = upsert_fx_resolution(
            project_id=project_id,
            target_field="cost",
            source_module="meta-ads",
            resolved_source_currency="usd",
            decided_by=ALICE,
            note=None,
            conn=conn,
        )
        assert row["resolved_source_currency"] == "USD"
        assert row["rule_set_version_id"].startswith("grsv_")

        assert get_fx_resolution(project_id, "cost", "meta-ads", conn)["decided_by"] == ALICE
        assert len(list_fx_resolutions(project_id, conn)) == 1

        delete_fx_resolution(project_id, "cost", "meta-ads", conn)
        assert list_fx_resolutions(project_id, conn) == []
