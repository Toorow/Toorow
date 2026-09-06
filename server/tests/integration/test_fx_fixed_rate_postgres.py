"""A posed FX rate lands, on a REAL Postgres, under its own method.

WHY POSTGRES AND NOT A DOUBLE. Everything this proves is SQL: the CHECK that had
to be widened for `fixed` to be storable at all (migration 279), the immutability
trigger on observations, the uniqueness of a version's content hash and the
activation pointer that decides which version a Project is on. A fake would agree
with whatever the writer believed.

WHAT IT PROVES, in order:

  1. the posed rate is stored as `fixed` -- not `direct`, which is the word
     reserved for an observed quotation and the exact lie the amendment of
     2026-08-17 names;
  2. its provenance is readable without joining anything else: who posed it, when
     it holds, and that no provider was read;
  3. posting the same rate twice does not mint two authorities for one number;
  4. before migration 279 this row was impossible -- asserted by writing `fixed`
     and by checking the constraint itself, so the migration cannot be quietly
     reverted without this failing.
"""

from __future__ import annotations

import json
import os
import uuid

import pytest

os.environ.setdefault("HEALTH_POLLER_ENABLED", "false")
os.environ.setdefault("QUEUE_WORKER_ENABLED", "false")
os.environ.setdefault("SCHEDULER_ENABLED", "false")

ORG = "org_test_fixture"


def _new_project(conn) -> str:
    project_id = f"proj_fx_{uuid.uuid4().hex[:12]}"
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO app.projects (id, name, slug, status, created_by, org_id)
            VALUES (%s, 'Fixed rate', %s, 'active', 'pytest', %s)
            """,
            (project_id, f"fx-{uuid.uuid4().hex[:12]}", ORG),
        )
    return project_id


def _post(conn, project_id: str, **overrides):
    from core.fx_fixed_rates import post_fixed_rate

    payload = {
        "base_currency": "USD",
        "quote_currency": "EUR",
        "rate": "0.92",
        "valid_from": "2026-01-01",
        "valid_to": "2026-12-31",
        "note": "Contract rate agreed with the client",
    }
    payload.update(overrides)
    return post_fixed_rate(
        conn, org_id=ORG, project_id=project_id, actor="operator@example.com", **payload
    )


def test_a_posed_rate_is_stored_under_the_fixed_method(live_postgres):
    conn = live_postgres
    project_id = _new_project(conn)

    result = _post(conn, project_id)

    assert result["method"] == "fixed"
    assert result["replayed"] is False

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT o.method, o.base_currency, o.quote_currency, o.rate,
                   o.derivation, v.method, v.provider, v.created_by, v.status
              FROM app.fx_rate_observations o
              JOIN app.fx_rate_set_versions v ON v.id = o.rate_set_version_id
             WHERE o.project_id = %s
            """,
            (project_id,),
        )
        rows = cur.fetchall()

    assert len(rows) == 1
    (
        method,
        base,
        quote,
        rate,
        derivation,
        version_method,
        provider,
        created_by,
        status,
    ) = rows[0]

    # 1. The word itself. `direct` here would be the defect back.
    assert method == "fixed"
    assert (base, quote) == ("USD", "EUR")
    assert str(rate) == "0.920000000000000000"

    # 2. The provenance a reader can act on, without a second query.
    derivation = derivation if isinstance(derivation, dict) else json.loads(derivation)
    assert derivation["posed"] is True
    assert derivation["valid_from"] == "2026-01-01"
    assert derivation["valid_to"] == "2026-12-31"
    assert derivation["posed_by"] == "operator@example.com"
    assert derivation["note"] == "Contract rate agreed with the client"
    assert version_method == "manual_entry"
    # `provider` becomes `fx_source`, so it must not name a feed nobody read.
    assert provider == "operator"
    assert created_by == "operator@example.com"
    assert status == "validated"


def test_the_posted_version_becomes_the_one_the_project_is_on(live_postgres):
    conn = live_postgres
    project_id = _new_project(conn)

    result = _post(conn, project_id)

    with conn.cursor() as cur:
        cur.execute(
            "SELECT current_version_id FROM app.fx_rate_sets WHERE project_id = %s",
            (project_id,),
        )
        assert cur.fetchone()[0] == result["id"]

    # And the capability read sees it -- the one production reader of this store.
    from core.fx_rate_sets import rate_freshness

    freshness = rate_freshness(conn, project_id=project_id)
    assert freshness["state"] == "current"
    assert freshness["provider"] == "operator"
    assert freshness["observation_count"] == 1


def test_posting_the_same_rate_twice_mints_no_rival_version(live_postgres):
    conn = live_postgres
    project_id = _new_project(conn)

    first = _post(conn, project_id)
    second = _post(conn, project_id)

    assert second["replayed"] is True
    assert second["content_hash"] == first["content_hash"]
    assert second["id"] == first["id"]

    with conn.cursor() as cur:
        cur.execute(
            "SELECT count(*) FROM app.fx_rate_set_versions WHERE project_id = %s",
            (project_id,),
        )
        assert cur.fetchone()[0] == 1


def test_a_new_period_supersedes_the_previous_version(live_postgres):
    conn = live_postgres
    project_id = _new_project(conn)

    first = _post(conn, project_id)
    second = _post(conn, project_id, rate="0.95", valid_from="2027-01-01", valid_to="2027-12-31")

    assert second["id"] != first["id"]
    with conn.cursor() as cur:
        cur.execute(
            "SELECT id, status FROM app.fx_rate_set_versions WHERE project_id = %s "
            "ORDER BY version_number",
            (project_id,),
        )
        statuses = dict(cur.fetchall())

    # The evidence of the first period is kept, marked superseded rather than
    # rewritten: a figure derived under it must stay reproducible.
    assert statuses[first["id"]] == "superseded"
    assert statuses[second["id"]] == "validated"


def test_the_check_admits_fixed_and_still_refuses_an_invention(live_postgres):
    """Migration 279, asserted where it matters rather than by reading the file."""
    conn = live_postgres
    with conn.cursor() as cur:
        cur.execute(
            "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
            "WHERE conname = 'fx_rate_observations_method_check'"
        )
        definition = cur.fetchone()[0]

    assert "'fixed'" in definition, (
        "app.fx_rate_observations no longer admits the `fixed` method. Migration 279 "
        "widened this CHECK so a posed rate would stop borrowing `direct`; without it "
        "core.fx_fixed_rates cannot store anything."
    )
    for observed in ("direct", "triangulated", "carry_forward", "identity"):
        assert f"'{observed}'" in definition
    assert "'plausible'" not in definition


def test_a_posed_observation_cannot_be_edited_afterwards(live_postgres):
    """The posed path inherits the immutability of the ingested one."""
    import psycopg

    conn = live_postgres
    project_id = _new_project(conn)
    _post(conn, project_id)

    with conn.cursor() as cur:
        cur.execute("SAVEPOINT before_mutation")
        with pytest.raises(psycopg.errors.Error):
            cur.execute(
                "UPDATE app.fx_rate_observations SET rate = 1.5 WHERE project_id = %s",
                (project_id,),
            )
        cur.execute("ROLLBACK TO SAVEPOINT before_mutation")
