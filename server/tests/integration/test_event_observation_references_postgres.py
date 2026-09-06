"""Live-Postgres proof for Story 49.6 AC9's Event resolver.

`test_event_observation_references.py` proves what the module DECIDES about the
rows it gets. It cannot prove that it gets the right rows: its cursor is a fake
that returns whatever the test hands it, so a WHERE clause that filtered on
nothing would pass every one of those tests.

That gap is the whole point of this file, and it is not academic. AC9 turns on
two properties that live entirely in SQL:

* **the project scope is in the query**, so an observation of another Project
  is never resolved — the overlay must not become a way to ask whether another
  Project's Event exists;
* **the join to `app.event_configuration_versions` is a LEFT JOIN**, so an
  observation whose version row cannot be reached collapses to `unavailable`
  instead of vanishing from the answer entirely. An inner join would have made
  a half-bound observation DISAPPEAR — and a disappeared Event reads as "the
  path went through none", which is a different and worse lie than
  "unavailable".

Written around the two traps `test_ai_paths_postgres.py` records: a bare
``conn.rollback()`` destroys the fixtures, and a suite run as the owner proves
nothing about what the application role can see. Nothing here needs either --
the resolver only reads -- but the fixtures are inserted and torn down inside
one transaction the fixture rolls back.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import os
import uuid

import pytest
from core.event_configurations import (
    EVENT_BINDING_LINKED,
    EVENT_BINDING_UNAVAILABLE,
    resolve_event_observation_references,
)


def _crockford(seed: str) -> str:
    alphabet = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
    digest = hashlib.sha256(seed.encode("utf-8")).digest()
    return "".join(alphabet[byte % 32] for byte in digest[:26])


def _hash(seed: str) -> str:
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()


def _org(conn) -> str:
    org_id = f"org_evt_{uuid.uuid4().hex[:12]}"
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO app.organizations (id, name, slug, status, created_by) "
            "VALUES (%s, %s, %s, 'active', 'test@example.com')",
            (org_id, "Event ref test", org_id.lower()),
        )
    return org_id


def _project(conn, org_id: str) -> str:
    project_id = f"proj_evt_{uuid.uuid4().hex[:12]}"
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO app.projects (id, org_id, name, slug, status, created_by) "
            "VALUES (%s, %s, %s, %s, 'active', 'test@example.com')",
            (project_id, org_id, "Event ref test", project_id.lower()),
        )
    return project_id


def _datastream(conn, org_id: str, project_id: str) -> str:
    ds_id = f"ds_evt_{uuid.uuid4().hex[:12]}"
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO app.datastreams (id, org_id, project_id, name, module_name, created_by) "
            "VALUES (%s, %s, %s, %s, 'github', 'test@example.com')",
            (ds_id, org_id, project_id, f"Events {ds_id}"),
        )
    return ds_id


def _configuration(conn, org_id: str, project_id: str, datastream_id: str) -> tuple[str, str]:
    """One Event Configuration and one version of it, as Data owns them."""
    config_id = "ecfg_" + _crockford(f"cfg{uuid.uuid4()}")
    version_id = "ecv_" + _crockford(f"ver{uuid.uuid4()}")
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO app.event_configurations "
            "(id, datastream_id, project_id, org_id, name, created_by) "
            "VALUES (%s, %s, %s, %s, %s, 'test@example.com')",
            (config_id, datastream_id, project_id, org_id, f"Releases {config_id}"),
        )
        cur.execute(
            """
            INSERT INTO app.event_configuration_versions
                (id, event_configuration_id, datastream_id, project_id, version_number,
                 source_mapping, collection_policy, normalized_payload_hash, created_by)
            VALUES (%s, %s, %s, %s, 7, '{}'::jsonb, '{}'::jsonb, %s, 'test@example.com')
            """,
            (version_id, config_id, datastream_id, project_id, _hash(version_id)),
        )
    return config_id, version_id


def _observation(conn, project_id: str, **columns) -> str:
    """One row of `app.context_events` -- the Event landing table (epic 31)."""
    event_id = f"evt_{uuid.uuid4().hex[:20]}"
    base = {
        "event_date": dt.date(2026, 8, 1),
        "type": "release",
        "label": "v2.1 shipped",
        "created_by": "test@example.com",
        "source": "github",
    }
    base.update(columns)
    names = ", ".join(base)
    placeholders = ", ".join(["%s"] * len(base))
    with conn.cursor() as cur:
        cur.execute(
            f"INSERT INTO app.context_events (id, project_id, {names}) "
            f"VALUES (%s, %s, {placeholders})",
            (event_id, project_id, *base.values()),
        )
    return event_id


def _legacy_shape(conn, event_id: str, *, keep_datastream: str | None = None) -> None:
    """Put a row into the shape a PRE-133 observation still has today.

    A pre-133 row is, exactly, a row that passed through NEITHER trigger — it
    was in the table before migration 133 wrote them. Migration 133 says so in
    its own WHERE: it bound only the observations where a single owning
    Datastream could be proven (`candidate_count = 1`), and every other row kept
    the column DEFAULT of `'unavailable'`. Those rows are what `unavailable` is
    for, and they cannot be produced by any write the application role is
    allowed to make:

    * `app.require_event_observation_binding` (133) is BEFORE INSERT — a new
      observation without a Datastream-owned version is refused outright;
    * `app.freeze_event_observation_binding` (212, AI-191) is BEFORE UPDATE — a
      binding, once made, can no longer be unmade, and `binding_state` is
      recomputed from the columns rather than taken from the writer.

    Until 212 this helper simply used the second hole: it INSERTed a bound row
    and then UPDATEd the binding away. That is the AI-191 attack, and the fact
    that a test fixture reached for it is the clearest evidence the hole was
    reachable. It is now refused, as
    `test_event_binding_is_frozen_postgres.py` proves from the application role.

    So the shape is fabricated the only honest way: with the UPDATE trigger
    disabled by the OWNER — which is precisely "this row did not come through
    the trigger". The disabling is done ONCE for the module by
    :func:`_pre_133_rows_are_possible` below, not around each statement:
    `ALTER TABLE ... DISABLE TRIGGER` takes an ACCESS EXCLUSIVE lock, and a test
    transaction that has already written to `app.context_events` holds a
    conflicting one — the per-statement version would block on the very
    transaction that needs it.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE app.context_events
               SET event_configuration_version_id = NULL,
                   datastream_id = %s,
                   binding_state = 'unavailable'
             WHERE id = %s
            """,
            (keep_datastream, event_id),
        )


@pytest.fixture(scope="module", autouse=True)
def _pre_133_rows_are_possible():
    """Let this module fabricate the one shape the schema now forbids.

    Module-scoped and autouse so the lock is taken before any test transaction
    touches the table, and released after the last one. Nothing in the module
    proves anything ABOUT the trigger — that is
    `test_event_binding_is_frozen_postgres.py`, which runs with it armed.
    """
    import psycopg  # noqa: PLC0415

    runtime_dsn = os.environ.get("TEST_POSTGRES_DSN")
    owner_dsn = os.environ.get("TEST_POSTGRES_OWNER_DSN")
    if not runtime_dsn and not owner_dsn:
        pytest.skip(
            "TEST_POSTGRES_OWNER_DSN is not set: a pre-133 observation is a row that "
            "passed through neither binding trigger, and only the owner can stand one "
            "up. `python scripts/disposable_postgres.py env` prints it.",
        )
    if runtime_dsn and not owner_dsn:
        pytest.fail(
            "TEST_POSTGRES_DSN is set but TEST_POSTGRES_OWNER_DSN is missing: "
            "the pre-133 proof cannot be prepared with the runtime role",
            pytrace=False,
        )

    def _switch(state: str) -> None:
        with psycopg.connect(owner_dsn, connect_timeout=5, autocommit=True) as owner:
            with owner.cursor() as cur:
                cur.execute(
                    f"ALTER TABLE app.context_events {state} TRIGGER "
                    "trg_context_events_freeze_binding"
                )

    _switch("DISABLE")
    try:
        yield
    finally:
        _switch("ENABLE")


@pytest.fixture()
def scope(live_postgres):
    conn = live_postgres
    org_id = _org(conn)
    project_id = _project(conn, org_id)
    datastream_id = _datastream(conn, org_id, project_id)
    config_id, version_id = _configuration(conn, org_id, project_id, datastream_id)
    return conn, org_id, project_id, datastream_id, config_id, version_id


def test_a_bound_observation_resolves_to_its_datastream_and_version(scope):
    conn, _org_id, project_id, datastream_id, config_id, version_id = scope
    event_id = _observation(
        conn,
        project_id,
        datastream_id=datastream_id,
        event_configuration_version_id=version_id,
        binding_state=EVENT_BINDING_LINKED,
    )

    resolved = resolve_event_observation_references(
        conn, project_id=project_id, event_ids=[event_id]
    )
    reference = resolved[event_id]
    assert reference["binding_state"] == EVENT_BINDING_LINKED
    assert reference["datastream_id"] == datastream_id
    assert reference["event_configuration_id"] == config_id
    assert reference["event_configuration_version_id"] == version_id
    # Read through the JOIN, not echoed back from the observation row: this is
    # the value the fake cursor could never have proven.
    assert reference["version_number"] == 7
    assert reference["owner_route"]["object_id"] == config_id
    assert reference["owner_route"]["tab"] == "usage"


def test_an_observation_of_another_project_is_never_resolved(scope):
    """The scope lives in the WHERE. If it were a post-hoc filter — or absent —
    this overlay would answer whether another Project's Event exists."""
    conn, org_id, project_id, datastream_id, _config_id, version_id = scope
    other_project = _project(conn, org_id)
    other_ds = _datastream(conn, org_id, other_project)
    _cfg, other_version = _configuration(conn, org_id, other_project, other_ds)
    foreign_event = _observation(
        conn,
        other_project,
        datastream_id=other_ds,
        event_configuration_version_id=other_version,
        binding_state=EVENT_BINDING_LINKED,
    )
    mine = _observation(
        conn,
        project_id,
        datastream_id=datastream_id,
        event_configuration_version_id=version_id,
        binding_state=EVENT_BINDING_LINKED,
    )

    resolved = resolve_event_observation_references(
        conn, project_id=project_id, event_ids=[mine, foreign_event]
    )
    assert set(resolved) == {mine}
    # Absent, not `unavailable` — and absent is exactly how a NONEXISTENT id
    # comes back too, so neither confirms the other.
    assert foreign_event not in resolved


def test_a_nonexistent_id_and_a_foreign_one_answer_identically(scope):
    conn, _org_id, project_id, _ds, _cfg, _version = scope
    resolved = resolve_event_observation_references(
        conn, project_id=project_id, event_ids=["evt_does_not_exist_at_all"]
    )
    assert resolved == {}


def test_an_unbound_observation_survives_the_join_as_unavailable(scope):
    """THE reason the join is a LEFT JOIN.

    Legacy rows predate migration 133's binding and carry no version at all. An
    inner join would drop them from the result, and a dropped Event reads as
    "the path went through none" — a different and worse lie than "unavailable",
    because the reader never learns there was something to look at.
    """
    conn, _org_id, project_id, datastream_id, _cfg, version_id = scope
    event_id = _observation(
        conn,
        project_id,
        datastream_id=datastream_id,
        event_configuration_version_id=version_id,
    )
    _legacy_shape(conn, event_id)  # the pre-133 shape: no version, no datastream

    resolved = resolve_event_observation_references(
        conn, project_id=project_id, event_ids=[event_id]
    )
    assert resolved[event_id] == {
        "event_id": event_id,
        "binding_state": EVENT_BINDING_UNAVAILABLE,
    }


def test_an_observation_naming_a_version_that_cannot_be_reached_is_unavailable(scope):
    """`binding_state` says `linked` and the pointer is half there. The join
    finds nothing, so nothing is published — the rule is all of the pointer or
    none of it, and here the database is what decides it."""
    conn, _org_id, project_id, datastream_id, _cfg, version_id = scope
    event_id = _observation(
        conn,
        project_id,
        datastream_id=datastream_id,
        event_configuration_version_id=version_id,
    )
    # Half a pointer: the Datastream survives, the version does not.
    _legacy_shape(conn, event_id, keep_datastream=datastream_id)
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE app.context_events SET binding_state = 'linked' WHERE id = %s", (event_id,)
        )

    resolved = resolve_event_observation_references(
        conn, project_id=project_id, event_ids=[event_id]
    )
    assert resolved[event_id]["binding_state"] == EVENT_BINDING_UNAVAILABLE
    assert "datastream_id" not in resolved[event_id], "non-disclosing: no pointer leaks"


def test_many_ids_resolve_in_one_query_and_keep_their_own_verdicts(scope):
    """An overlay asks for every Event of a path at once. Mixing a bound and an
    unbound one must not make either take the other's answer."""
    conn, _org_id, project_id, datastream_id, config_id, version_id = scope
    bound = _observation(
        conn,
        project_id,
        datastream_id=datastream_id,
        event_configuration_version_id=version_id,
    )
    unbound = _observation(
        conn,
        project_id,
        datastream_id=datastream_id,
        event_configuration_version_id=version_id,
    )
    _legacy_shape(conn, unbound)

    resolved = resolve_event_observation_references(
        conn, project_id=project_id, event_ids=[bound, unbound]
    )
    assert resolved[bound]["event_configuration_id"] == config_id
    assert resolved[unbound]["binding_state"] == EVENT_BINDING_UNAVAILABLE
