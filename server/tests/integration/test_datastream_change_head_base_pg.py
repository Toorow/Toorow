"""A change prepared against the HEAD version, RUN -- against the real schema.

WHY THIS FILE EXISTS. Amendment 4 of the 2026-08-11 review made the Mapping tab
editable on the HEAD version when no mapping pointer is in force, because 4 of
the 6 live Datastreams had `current_mapping_version_id` NULL and every editing
control was therefore unreachable. `prepare_change` was not moved with it: it
selected with an INNER JOIN on both pointers and `lifecycle_state='active'`, so a
person could pick a canonical concept, watch the scope sentence compose, press
`Prepare mapping change` -- and be refused at the end. Re-measured on the live
base on 2026-08-12: 8 non-archived Datastreams, 1 `active` with both pointers, 7
`draft`, 6 of them with no mapping pointer.

WHAT THIS FILE PROVES, and it is behaviour and not shape. Every test asserts a
DIFFERENCE between two states of the database -- a mapping version that exists
versus one that does not, a candidate dispatched versus none, a pointer that is
still NULL after a confirmation. The 2x2 of the optimistic lock:

                          | published / superseded after review | neither
    pointer at prepare    | refuse -- and that is proven in     | confirm
                          | `test_datastream_change_engine_pg`   |
                          | ...moved_after_review... and here    |
    no pointer at prepare | refuse, twice over: a publication   | confirm, and
                          | and a newer head are separate       | nothing becomes
                          | refusals with separate sentences    | live

The `pointer at prepare` row is already covered by
`test_datastream_change_engine_pg.py`; one case of it is repeated here so that
widening the base cannot be shown green while narrowing what the pointer
protected.

A SEPARATE FILE from `test_datastream_change_engine_pg.py`, which stands at 1106
lines.
"""

from __future__ import annotations

import json
import os
import secrets
import uuid
from typing import Any

import psycopg
import pytest

pytestmark = pytest.mark.pg

requires_postgres = pytest.mark.skipif(
    not os.environ.get("TEST_POSTGRES_DSN"),
    reason="TEST_POSTGRES_DSN not set -- run `python scripts/disposable_postgres.py up`",
)

_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"

_REQUIRED_TABLES = (
    "organizations",
    "projects",
    "datastreams",
    "mdm_canonical_fields",
    "datastream_plan_versions",
    "datastream_mapping_versions",
    "datastream_change_preparations",
    "datastream_executions",
    "operations",
    "operation_outbox",
    "datastream_activation_jobs",
)

#: Migration 255. Checked as a column and not as a table, because the table has
#: existed since 138 and its absence would say the wrong thing.
_REQUIRED_COLUMNS = (
    ("datastream_change_preparations", "expected_plan_pointer_in_force"),
    ("datastream_change_preparations", "expected_mapping_pointer_in_force"),
)


def _id(prefix: str) -> str:
    return f"{prefix}{uuid.uuid4().hex[:12]}"


def _mint_field() -> str:
    """A FRESH canonical field id per run -- the disposable database is SHARED."""
    return "mdm_" + "".join(secrets.choice(_CROCKFORD) for _ in range(26))


def _missing(conn) -> list[str]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema='app' AND table_name = ANY(%s)",
            (list(_REQUIRED_TABLES),),
        )
        present = {r[0] for r in cur.fetchall()}
        gaps = [t for t in _REQUIRED_TABLES if t not in present]
        cur.execute(
            "SELECT table_name,column_name FROM information_schema.columns "
            "WHERE table_schema='app' AND table_name = ANY(%s) AND column_name = ANY(%s)",
            ([t for t, _ in _REQUIRED_COLUMNS], [c for _, c in _REQUIRED_COLUMNS]),
        )
        columns = {(r[0], r[1]) for r in cur.fetchall()}
    return gaps + [f"{t}.{c}" for t, c in _REQUIRED_COLUMNS if (t, c) not in columns]


@pytest.fixture
def conn():
    """A connection to the DISPOSABLE test database -- never production."""
    dsn = os.environ.get("TEST_POSTGRES_DSN")
    if not dsn:
        pytest.skip("TEST_POSTGRES_DSN not set")
    lowered = dsn.lower()
    assert "supabase" not in lowered and "pooler" not in lowered, (
        "TEST_POSTGRES_DSN points at the managed production host; refusing to run. "
        "Use `python scripts/disposable_postgres.py up`."
    )
    connection = psycopg.connect(dsn, connect_timeout=5)
    gaps = _missing(connection)
    if gaps:
        connection.close()
        pytest.skip(
            "the test database is missing "
            + ", ".join(gaps)
            + " -- run `python scripts/disposable_postgres.py up`"
        )
    try:
        yield connection
    finally:
        connection.rollback()
        connection.close()


# ---------------------------------------------------------------------------
# Seeding: a DRAFT Datastream whose versions are recorded and NOT in force --
# the shape of 4 of the 8 rows of the live base.
# ---------------------------------------------------------------------------


def _field(field_id: str, *, role: str, aggregation: str, mdm_target: str) -> dict[str, Any]:
    return {
        "field_id": field_id,
        "physical_type": "date" if role == "primary_date" else "number",
        "profile": {
            "nullable": False,
            "unique": False,
            "cardinality_signal": "low",
            "sample_values": [],
            "confidence": 0.9,
        },
        "suggestion": {
            "semantic_role": role,
            "aggregation": aggregation,
            "non_additive": False,
            "currency": "unknown",
            "sensitivity": "none",
            "status": "suggested",
            "evidence": [],
        },
        "binding": {
            "canonical_target": field_id,
            "mdm_target": mdm_target,
            "status": "confirmed",
            "blocking_reason": None,
            "confirmed_by": "owner@example.com",
            "confirmed_reason": "Confirmed in Datastream final review",
        },
    }


def _mapping_payload(plan_id: str, fields: dict[str, str], *, cost_alias: str) -> dict[str, Any]:
    return {
        "mapping_contract_version": "1",
        "source_schema_hash": "a" * 64,
        "plan_version_id": plan_id,
        "capability_fingerprint": "b" * 64,
        "grain": ["date"],
        "fields": [
            _field("date", role="primary_date", aggregation="none", mdm_target=fields["DATE"]),
            _field(cost_alias, role="measure", aggregation="sum", mdm_target=fields["COST"]),
        ],
        "ambiguities": [],
    }


def _seed(
    conn,
    *,
    lifecycle: str = "draft",
    advance_pointer: bool = False,
    with_mapping: bool = True,
) -> dict[str, Any]:
    """org + project + Datastream + one plan version + one mapping version.

    `advance_pointer=False` and no `current_plan_version_id` UPDATE is exactly
    what `datastream_activation.materialize_draft_mutation` leaves behind: the
    versions exist, nothing is in force. This is the state the wizard produces
    and the state amendment 4 made editable.
    """
    from core.datastream_field_mapping import save_field_mapping

    org_id, project_id, ds_id, plan_id = _id("org_"), _id("proj_"), _id("ds_"), _id("dsp_")
    fields = {"DATE": _mint_field(), "COST": _mint_field()}

    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO app.organizations (id,name,slug,created_by) "
            "VALUES (%s,%s,%s,'head-base-harness') ON CONFLICT DO NOTHING",
            (org_id, org_id, org_id),
        )
        cur.execute(
            "INSERT INTO app.projects (id,name,slug,org_id,created_by,status) "
            "VALUES (%s,%s,%s,%s,'head-base-harness','active') ON CONFLICT DO NOTHING",
            (project_id, project_id, project_id, org_id),
        )
        # `value_type` is NOT NULL since migration 241 -- a canonical field that
        # does not say what kind of value it carries is refused by the schema.
        for role, kind, name, agg, value_type in (
            ("DATE", "dimension", "media_date", None, "date"),
            ("COST", "metric", "net_cost", "sum", "money"),
        ):
            cur.execute(
                "INSERT INTO app.mdm_canonical_fields "
                "(id,project_id,concept_kind,canonical_name,aggregation,value_type,created_by) "
                "VALUES (%s,%s,%s,%s,%s,%s,'head-base-harness')",
                (fields[role], project_id, kind, f"{name}_{project_id[-8:]}", agg, value_type),
            )
        cur.execute(
            """INSERT INTO app.datastreams
               (id,project_id,org_id,name,module_name,connection_ref_id,enabled,
                schedule_mode,config,created_by,lifecycle_state,source_kind,data_role)
               VALUES (%s,%s,%s,'Head base harness',NULL,NULL,TRUE,'manual',
                       %s::jsonb,'owner@example.com',%s,'managed_feed','Spend')""",
            (ds_id, project_id, org_id, json.dumps({}), lifecycle),
        )
        cur.execute(
            """INSERT INTO app.datastream_plan_versions
               (id,datastream_id,project_id,version_number,contract_version,source_kind,
                writer_kind,destination_policy,normalized_payload,content_hash,
                capability_fingerprint,idempotency_key_hash,created_by)
               VALUES (%s,%s,%s,1,'1','managed_feed','toorow','managed_raw',
                       %s::jsonb,repeat('a',64),repeat('a',64),repeat('b',64),
                       'head-base-harness')""",
            (plan_id, ds_id, project_id, json.dumps({"contract_version": "1"})),
        )
        if advance_pointer:
            cur.execute(
                "UPDATE app.datastreams SET current_plan_version_id=%s WHERE id=%s",
                (plan_id, ds_id),
            )
    conn.commit()

    # `with_mapping=False` is NOT a deletion: `app.datastream_mapping_versions`
    # refuses every UPDATE and every hand-written DELETE
    # (`reject_datastream_mapping_version_mutation`), which is the append-only
    # guarantee working. A Datastream that never had one is seeded by never
    # writing one -- and 2 of the 8 live rows are exactly that.
    mapping_id = None
    if with_mapping:
        mapping_id = save_field_mapping(
            datastream_id=ds_id,
            project_id=project_id,
            mapping_payload=_mapping_payload(plan_id, fields, cost_alias="net_cost"),
            identity="owner@example.com",
            idempotency_key=_id("idem_"),
            conn=conn,
            pinned_plan_version_id=plan_id,
            advance_pointer=advance_pointer,
            commit=True,
        )["id"]
    return {
        "org_id": org_id,
        "project_id": project_id,
        "datastream_id": ds_id,
        "plan_id": plan_id,
        "mapping_id": mapping_id,
        "fields": fields,
    }


def _prepare(conn, scope: dict[str, Any], *, cost_alias: str = "media_cost") -> dict[str, Any]:
    from core.datastream_change import prepare_change

    prepared = prepare_change(
        conn,
        project_id=scope["project_id"],
        datastream_id=scope["datastream_id"],
        kind="mapping",
        proposed_payload=_mapping_payload(scope["plan_id"], scope["fields"], cost_alias=cost_alias),
        actor="owner@example.com",
        idempotency_key=_id("chg_"),
    )
    conn.commit()
    return prepared


def _confirm(conn, scope: dict[str, Any], prepared: dict[str, Any]) -> dict[str, Any]:
    from core.datastream_change import confirm_change

    result = confirm_change(
        conn,
        project_id=scope["project_id"],
        datastream_id=scope["datastream_id"],
        preparation_id=prepared["preparation_id"],
        confirmation_secret=prepared["confirmation_secret"],
        actor="owner@example.com",
    )
    conn.commit()
    return result


def _counts(conn, scope: dict[str, Any]) -> dict[str, Any]:
    """What "nothing happened" MEANS, measured in the database."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM app.datastream_mapping_versions "
            "WHERE datastream_id=%s AND project_id=%s",
            (scope["datastream_id"], scope["project_id"]),
        )
        mappings = cur.fetchone()[0]
        cur.execute(
            "SELECT COUNT(*) FROM app.datastream_executions "
            "WHERE datastream_id=%s AND project_id=%s",
            (scope["datastream_id"], scope["project_id"]),
        )
        executions = cur.fetchone()[0]
        cur.execute(
            "SELECT current_mapping_version_id,current_plan_version_id "
            "FROM app.datastreams WHERE id=%s",
            (scope["datastream_id"],),
        )
        pointers = cur.fetchone()
    return {
        "mapping_versions": mappings,
        "executions": executions,
        "current_mapping_version_id": pointers[0],
        "current_plan_version_id": pointers[1],
    }


def _publish(conn, scope: dict[str, Any], mapping_version_id: str) -> None:
    """Make one mapping version live, the way a publication does.

    The pointer UPDATE only -- this harness is not a second publication path and
    does not pretend to be one; what the tests below need is the STATE a
    publication leaves, which is a non-NULL `current_mapping_version_id`.
    """
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE app.datastreams SET current_mapping_version_id=%s "
            "WHERE id=%s AND project_id=%s",
            (mapping_version_id, scope["datastream_id"], scope["project_id"]),
        )
    conn.commit()


def _append_mapping_version(conn, scope: dict[str, Any], *, cost_alias: str) -> str:
    """A second recorded mapping version that does NOT become live."""
    from core.datastream_field_mapping import save_field_mapping

    appended = save_field_mapping(
        datastream_id=scope["datastream_id"],
        project_id=scope["project_id"],
        mapping_payload=_mapping_payload(scope["plan_id"], scope["fields"], cost_alias=cost_alias),
        identity="someone-else@example.com",
        idempotency_key=_id("idem_"),
        conn=conn,
        pinned_plan_version_id=scope["plan_id"],
        advance_pointer=False,
        commit=True,
    )
    return appended["id"]


# ---------------------------------------------------------------------------
# The repair: a Datastream with no pointer in force can be changed.
# ---------------------------------------------------------------------------


@requires_postgres
def test_a_draft_with_no_pointer_in_force_can_prepare_a_change(conn) -> None:
    """The defect, inverted. This call raised before 2026-08-12.

    The INNER JOIN returned no row for a Datastream missing either pointer, so
    the answer was "An Active Datastream with exact versions is required" -- on
    6 of the 8 non-archived rows of the live base.
    """
    scope = _seed(conn)
    prepared = _prepare(conn, scope)

    review = prepared["review"]
    assert review["expected_mapping_version_id"] == scope["mapping_id"], (
        "the base is the head of the ledger, which is the version the Mapping tab edits"
    )
    assert review["expected_plan_version_id"] == scope["plan_id"]
    assert review["base_versions"] == {
        "plan": {
            "state": "head_of_ledger",
            "version_id": scope["plan_id"],
            "reason": review["base_versions"]["plan"]["reason"],
        },
        "mapping": {
            "state": "head_of_ledger",
            "version_id": scope["mapping_id"],
            "reason": review["base_versions"]["mapping"]["reason"],
        },
    }
    assert review["rollback"]["state"] == "no_version_becomes_live", (
        "'the live pointers do not move' is true of nothing here, and a review that "
        "said it would be reassuring about an object that does not exist"
    )
    assert review["rollback"]["mapping_version_id"] is None


@requires_postgres
def test_a_confirmed_change_on_a_pointerless_datastream_makes_nothing_live(conn) -> None:
    """The whole widening in one measurement: it appends, and it activates nothing.

    `docs/product-architecture/governance.md` keeps the pointer move in governed
    publication. If confirming a head-based change moved it, this seam would have
    become a second way to make a version live -- so the pointer is asserted still
    NULL AFTER a confirmation that demonstrably did something.
    """
    scope = _seed(conn)
    before = _counts(conn, scope)
    assert before["current_mapping_version_id"] is None

    result = _confirm(conn, scope, _prepare(conn, scope))
    after = _counts(conn, scope)

    assert after["mapping_versions"] == before["mapping_versions"] + 1, "the version was appended"
    assert after["executions"] == before["executions"] + 1, "one candidate was dispatched"
    assert result["candidate_execution_id"]
    assert result["active_versions_unchanged"] is True
    assert after["current_mapping_version_id"] is None, (
        "confirming a change made a mapping version live; publication is the only "
        "step allowed to do that"
    )
    assert after["current_plan_version_id"] is None


# ---------------------------------------------------------------------------
# The optimistic lock, with no pointer at prepare time.
# ---------------------------------------------------------------------------


@requires_postgres
def test_a_publication_between_the_review_and_the_confirmation_dispatches_nothing(conn) -> None:
    """THE case this widening could most easily have let land silently.

    The publication activates the very version the review is based on. A
    confirmation that re-derived "was a pointer in force?" would look, find one,
    compare it to `expected_mapping_version_id`, find them EQUAL -- and append on
    top of a Datastream whose state changed under the person in exactly the way
    the lock exists to catch. `expected_mapping_pointer_in_force` is stored for
    this reason and no other.
    """
    from core.datastream_change import DatastreamChangeError

    scope = _seed(conn)
    prepared = _prepare(conn, scope)
    _publish(conn, scope, scope["mapping_id"])
    before = _counts(conn, scope)

    with pytest.raises(DatastreamChangeError) as refusal:
        _confirm(conn, scope, prepared)
    conn.rollback()

    assert "published while this review was open" in str(refusal.value)
    after = _counts(conn, scope)
    assert after["mapping_versions"] == before["mapping_versions"], "no version was appended"
    assert after["executions"] == before["executions"], "no candidate was dispatched"
    assert after["current_mapping_version_id"] == scope["mapping_id"], (
        "the refusal left the publication exactly where it was"
    )


@requires_postgres
def test_a_newer_version_recorded_after_the_review_dispatches_nothing(conn) -> None:
    """Two people preparing a change on the same pointerless Datastream.

    The second confirmation would otherwise append on top of a base it never
    reviewed: its `before_hash` describes a version the screen no longer shows.
    """
    from core.datastream_change import DatastreamChangeError

    scope = _seed(conn)
    prepared = _prepare(conn, scope)
    _append_mapping_version(conn, scope, cost_alias="gross_cost")
    before = _counts(conn, scope)

    with pytest.raises(DatastreamChangeError) as refusal:
        _confirm(conn, scope, prepared)
    conn.rollback()

    assert "newer mapping version was recorded" in str(refusal.value)
    after = _counts(conn, scope)
    assert after["mapping_versions"] == before["mapping_versions"]
    assert after["executions"] == before["executions"]


# ---------------------------------------------------------------------------
# The pointer's own guarantee, unweakened.
# ---------------------------------------------------------------------------


@requires_postgres
def test_a_pointer_in_force_that_moved_after_the_review_still_dispatches_nothing(conn) -> None:
    """The guarantee that existed before amendment 4, re-measured after it.

    Repeated from `test_datastream_change_engine_pg.py` on purpose: a widening is
    only a widening if what it widened around still holds, and the two files can
    be run separately.
    """
    from core.datastream_change import DatastreamChangeError

    scope = _seed(conn, lifecycle="active", advance_pointer=True)
    prepared = _prepare(conn, scope)
    assert prepared["review"]["base_versions"]["mapping"]["state"] == "in_force"

    moved = _append_mapping_version(conn, scope, cost_alias="gross_cost")
    _publish(conn, scope, moved)
    before = _counts(conn, scope)

    with pytest.raises(DatastreamChangeError) as refusal:
        _confirm(conn, scope, prepared)
    conn.rollback()

    assert "moved after this review" in str(refusal.value)
    after = _counts(conn, scope)
    assert after["mapping_versions"] == before["mapping_versions"]
    assert after["executions"] == before["executions"]
    assert after["current_mapping_version_id"] == moved


# ---------------------------------------------------------------------------
# What the widening did NOT open.
# ---------------------------------------------------------------------------


@requires_postgres
def test_an_archived_datastream_is_still_refused(conn) -> None:
    """65 of the live rows are archived, and every one of them reads `draft`.

    The soft archive of `core/datastreams.py` sets `archived_at` and leaves
    `lifecycle_state` alone, so admitting `draft` without reading the timestamp
    would have admitted all 65 at once.
    """
    from core.datastream_change import DatastreamChangeError

    scope = _seed(conn)
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE app.datastreams SET archived_at=NOW(),enabled=FALSE WHERE id=%s",
            (scope["datastream_id"],),
        )
    conn.commit()

    with pytest.raises(DatastreamChangeError) as refusal:
        _prepare(conn, scope)
    conn.rollback()
    assert "restored" in str(refusal.value)


@requires_postgres
def test_a_datastream_with_no_recorded_version_names_the_gesture_that_records_one(conn) -> None:
    """2 of the 8 live Datastreams carry zero plan and zero mapping versions.

    They are not "changeable with an empty base": there is nothing to diff
    against, and the refusal points at the wizard rather than fabricating a
    document.
    """
    from core.datastream_change import DatastreamChangeError

    scope = _seed(conn, with_mapping=False)

    with pytest.raises(DatastreamChangeError) as refusal:
        _prepare(conn, scope)
    conn.rollback()
    assert "no recorded mapping version" in str(refusal.value)
    assert "wizard" in str(refusal.value)


# ---------------------------------------------------------------------------
# What the preparation ROW carries, because the confirmation reads it from there.
# ---------------------------------------------------------------------------


@requires_postgres
def test_the_preparation_records_whether_each_base_was_in_force(conn) -> None:
    """Stored, not re-derived. The stored flag is what makes the publication case
    above refuse; a confirmation that recomputed it would find the pointer that
    publication created and believe it had been there all along."""
    scope = _seed(conn)
    prepared = _prepare(conn, scope)

    with conn.cursor() as cur:
        cur.execute(
            "SELECT expected_plan_pointer_in_force,expected_mapping_pointer_in_force "
            "FROM app.datastream_change_preparations WHERE id=%s",
            (prepared["preparation_id"],),
        )
        assert cur.fetchone() == (False, False)

    in_force = _seed(conn, lifecycle="active", advance_pointer=True)
    prepared_in_force = _prepare(conn, in_force)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT expected_plan_pointer_in_force,expected_mapping_pointer_in_force "
            "FROM app.datastream_change_preparations WHERE id=%s",
            (prepared_in_force["preparation_id"],),
        )
        assert cur.fetchone() == (True, True)
