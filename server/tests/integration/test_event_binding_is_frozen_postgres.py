"""Live-Postgres proof that an Event observation binding cannot be undone (AI-191).

Migration 133 posed `app.require_event_observation_binding` as a BEFORE
**INSERT** trigger only, so an UPDATE went straight past it. Verified on a
disposable cluster: the application role `connector` could set
`event_configuration_version_id` back to NULL on a bound row, then write
`binding_state = 'linked'` by hand. Nothing refused either statement.

That matters because `binding_state` is what
`resolve_event_observation_references` (Story 49.6 AC9) reads to decide whether
an observation may point at a Datastream at all. A column that governs a
disclosure must not be writable into a lie.

Migration 212 adds `app.freeze_event_observation_binding` on BEFORE UPDATE.
Two rules, and each has a test here:

  1. a binding is IMMUTABLE — the two columns of a bound row may not go back to
     NULL, nor be repointed at another version or another Datastream;
  2. `binding_state` is DERIVED — recomputed from the columns on every write, so
     an unbound row cannot claim `linked`.

**This file must run as the application role.** A superuser or the table owner
would be a different question: the hole AI-191 records is one the DEPLOYED role
could walk through, and `pytest.mark.pg_app_role` is what makes the suite refuse
to answer it as anyone else (conftest `_enforce_declared_role`).

The sibling `test_event_observation_references_postgres.py` disables this
trigger for its module, because it needs to fabricate the pre-133 shape the
schema now forbids. Nothing here is disabled — that is the point.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import uuid

import psycopg
import pytest

pytestmark = pytest.mark.pg_app_role


def _crockford(seed: str) -> str:
    alphabet = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
    digest = hashlib.sha256(seed.encode("utf-8")).digest()
    return "".join(alphabet[byte % 32] for byte in digest[:26])


def _hash(seed: str) -> str:
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


@pytest.fixture()
def bound_observation(live_postgres):
    """One Project, one Datastream, one Event Configuration version, one bound row.

    The observation is INSERTed bound, which is the only way migration 133 lets
    one exist, so the fixture itself is never the thing under test.
    """
    conn = live_postgres
    suffix = uuid.uuid4().hex[:12]
    org_id = f"org_frz_{suffix}"
    project_id = f"proj_frz_{suffix}"
    ds_id = f"ds_frz_{suffix}"
    other_ds_id = f"ds_frz2_{suffix}"
    config_id = "ecfg_" + _crockford(f"cfg{suffix}")
    version_id = "ecv_" + _crockford(f"ver{suffix}")
    other_version_id = "ecv_" + _crockford(f"ver2{suffix}")
    event_id = f"evt_{uuid.uuid4().hex[:20]}"

    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO app.organizations (id, name, slug, status, created_by) "
            "VALUES (%s, %s, %s, 'active', 'test@example.com')",
            (org_id, "Binding freeze test", org_id.lower()),
        )
        cur.execute(
            "INSERT INTO app.projects (id, org_id, name, slug, status, created_by) "
            "VALUES (%s, %s, %s, %s, 'active', 'test@example.com')",
            (project_id, org_id, "Binding freeze test", project_id.lower()),
        )
        for one in (ds_id, other_ds_id):
            cur.execute(
                "INSERT INTO app.datastreams "
                "(id, org_id, project_id, name, module_name, created_by) "
                "VALUES (%s, %s, %s, %s, 'github', 'test@example.com')",
                (one, org_id, project_id, f"Events {one}"),
            )
        # An Event Configuration belongs to ONE Datastream, and a version to its
        # configuration -- so the second version needs its own configuration under
        # the second Datastream, not a foreign row of the first.
        other_config_id = "ecfg_" + _crockford(f"cfg2{suffix}")
        for cfg, owner in ((config_id, ds_id), (other_config_id, other_ds_id)):
            cur.execute(
                "INSERT INTO app.event_configurations "
                "(id, datastream_id, project_id, org_id, name, lifecycle_state, created_by) "
                "VALUES (%s, %s, %s, %s, 'Releases', 'active', 'test@example.com')",
                (cfg, owner, project_id, org_id),
            )
        for one, cfg, owner, number in (
            (version_id, config_id, ds_id, 7),
            (other_version_id, other_config_id, other_ds_id, 8),
        ):
            cur.execute(
                """
                INSERT INTO app.event_configuration_versions
                    (id, event_configuration_id, datastream_id, project_id, version_number,
                     source_mapping, collection_policy, normalized_payload_hash,
                     review_state, created_by)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'confirmed', 'test@example.com')
                """,
                (
                    one, cfg, owner, project_id, number,
                    json.dumps({"event_type": "release"}), json.dumps({"mode": "pull"}),
                    _hash(one),
                ),
            )
        cur.execute(
            """
            INSERT INTO app.context_events
                (id, project_id, event_date, type, label, created_by, source,
                 datastream_id, event_configuration_version_id)
            VALUES (%s, %s, %s, 'release', 'v2.1 shipped', 'test@example.com', 'github', %s, %s)
            """,
            (event_id, project_id, dt.date(2026, 8, 1), ds_id, version_id),
        )
        cur.execute(
            "SELECT binding_state FROM app.context_events WHERE id = %s", (event_id,)
        )
        assert cur.fetchone()[0] == "linked", "133's INSERT trigger should have derived this"

    return conn, event_id, ds_id, other_ds_id, version_id, other_version_id


def _binding(conn, event_id: str) -> tuple:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT event_configuration_version_id, datastream_id, binding_state "
            "FROM app.context_events WHERE id = %s",
            (event_id,),
        )
        return cur.fetchone()


def test_the_update_trigger_is_actually_posed(live_postgres):
    """Named on purpose: AI-191 was a trigger that existed for one verb only, and
    the way that hides is that everything else about it looks right."""
    with live_postgres.cursor() as cur:
        cur.execute(
            """
            SELECT p.proname,
                   (t.tgtype & 4) > 0 AS on_insert,
                   (t.tgtype & 16) > 0 AS on_update
              FROM pg_trigger t
              JOIN pg_proc p ON p.oid = t.tgfoid
             WHERE t.tgrelid = 'app.context_events'::regclass
               AND NOT t.tgisinternal
               AND t.tgenabled <> 'D'
             ORDER BY p.proname
            """
        )
        posed = {row[0]: (row[1], row[2]) for row in cur.fetchall()}

    assert posed.get("require_event_observation_binding") == (True, False)
    assert posed.get("freeze_event_observation_binding") == (False, True), (
        "migration 212 is not applied on this cluster, so nothing below proves anything"
    )


def test_a_binding_cannot_be_undone_by_the_application_role(bound_observation):
    """The AI-191 attack, exactly as it was verified open."""
    conn, event_id, _ds_id, _other_ds, _version_id, _other_version = bound_observation

    with conn.cursor() as cur, pytest.raises(psycopg.errors.CheckViolation) as raised:
        cur.execute(
            "UPDATE app.context_events "
            "SET event_configuration_version_id = NULL, datastream_id = NULL WHERE id = %s",
            (event_id,),
        )
    assert "immutable" in str(raised.value)
    # The statement raised, so the transaction is aborted and nothing is read
    # back here. That is deliberate: recovering would mean `conn.rollback()`,
    # which destroys the fixtures (the trap recorded in test_ai_paths_postgres.py).
    # The refusal IS the assertion; what a surviving row looks like is the two
    # tests below.


def test_a_binding_cannot_be_repointed_at_another_datastream(bound_observation):
    conn, event_id, _ds_id, other_ds_id, _version_id, other_version_id = bound_observation
    with conn.cursor() as cur, pytest.raises(psycopg.errors.CheckViolation):
        cur.execute(
            "UPDATE app.context_events "
            "SET datastream_id = %s, event_configuration_version_id = %s WHERE id = %s",
            (other_ds_id, other_version_id, event_id),
        )


def test_binding_state_is_derived_and_cannot_be_asserted(bound_observation):
    """Writing `unavailable` onto a genuinely bound row is not an error — it is
    simply not believed. The column is recomputed from the pointer."""
    conn, event_id, ds_id, _other_ds, version_id, _other_version = bound_observation
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE app.context_events SET binding_state = 'unavailable' WHERE id = %s",
            (event_id,),
        )
    assert _binding(conn, event_id) == (version_id, ds_id, "linked")


def test_an_ordinary_update_to_a_bound_row_still_passes(bound_observation):
    """The guard freezes the binding, not the row. A label edit must still work —
    a guard that refused everything would be indistinguishable from a broken table."""
    conn, event_id, ds_id, _other_ds, version_id, _other_version = bound_observation
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE app.context_events SET label = %s WHERE id = %s",
            ("v2.2 shipped", event_id),
        )
    assert _binding(conn, event_id) == (version_id, ds_id, "linked")
