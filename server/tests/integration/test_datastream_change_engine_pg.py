"""The governance engine of story 38.17, RUN -- against the real schema.

WHY THIS FILE EXISTS. Everything that covered `datastream_change` before it read
the module's SOURCE (`test_datastream_change_governance.py`,
`test_datastream_workbench.py:255-262`), handed it a `MagicMock` whose
`fetchone` returns whatever the test wants (`:211-253`), or patched
`prepare_change` out of the call (`test_datastream_workbench_commands_api.py`).
Measured on 2026-08-10, four mutations of the engine -- deleting the AD-27
secret comparison, deleting the single-use/expiry branch, widening the
confirmation TTL from 15 minutes to 3650 days, and deleting the binding
revalidation -- each left `74 passed, 6 xfailed` untouched. Four guards, zero
red. A guard no test can make fall is a guard that can be deleted.

So this file executes `prepare_change` and `confirm_change` themselves: real
Postgres, real schema, real `execute_operation`, no mock and no patch on the
engine. Every test here asserts a CHANGE OF BEHAVIOUR between two states -- a
candidate that exists versus one that does not, a pointer that moved versus one
that did not -- never the presence of a key.

WHAT IT DOES NOT COVER. The `processing` change kind (which appends a plan
version through `_append_plan_version`); the candidate materialization the queue
job performs afterwards; and the pointer move itself, whose single writer is
`datastream_activation.py:695` and whose story is not this one.
"""

from __future__ import annotations

import json
import os
import secrets
import uuid
from copy import deepcopy
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


def _id(prefix: str) -> str:
    return f"{prefix}{uuid.uuid4().hex[:12]}"


def _mint_field() -> str:
    """A FRESH canonical field id per run -- the disposable database is SHARED."""
    return "mdm_" + "".join(secrets.choice(_CROCKFORD) for _ in range(26))


def _missing_tables(conn) -> list[str]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema='app' AND table_name = ANY(%s)",
            (list(_REQUIRED_TABLES),),
        )
        present = {r[0] for r in cur.fetchall()}
    return [t for t in _REQUIRED_TABLES if t not in present]


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
    missing = _missing_tables(connection)
    if missing:
        connection.close()
        pytest.skip(
            "the test database is missing "
            + ", ".join(missing)
            + " -- run `python scripts/disposable_postgres.py up`"
        )
    try:
        yield connection
    finally:
        connection.rollback()
        connection.close()


# ---------------------------------------------------------------------------
# Seeding: one Active Datastream with exact plan and mapping pointers.
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
    """A mapping that compiles. `cost_alias` is what makes two versions DIFFER."""
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


@pytest.fixture
def scope(conn):
    """org + project + Active Datastream + plan version + mapping version + pointers."""
    return _seed_scope(conn)


def _seed_scope(conn, *, config: dict[str, Any] | None = None) -> dict[str, Any]:
    """Seed ONE independent scope. A function, not only a fixture, because the
    organization assertion needs two of them side by side in the same test."""
    from core.datastream_field_mapping import save_field_mapping

    org_id = _id("org_")
    project_id = _id("proj_")
    ds_id = _id("ds_")
    plan_id = _id("dsp_")
    fields = {"DATE": _mint_field(), "COST": _mint_field()}

    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO app.organizations (id,name,slug,created_by) "
            "VALUES (%s,%s,%s,'story-38.17-harness') ON CONFLICT DO NOTHING",
            (org_id, org_id, org_id),
        )
        cur.execute(
            "INSERT INTO app.projects (id,name,slug,org_id,created_by,status) "
            "VALUES (%s,%s,%s,%s,'story-38.17-harness','active') ON CONFLICT DO NOTHING",
            (project_id, project_id, project_id, org_id),
        )
        # `value_type` is NOT NULL since migration 241 (story 64.14) -- "without
        # it a canonical field could never become a Concept". This fixture wrote
        # no value and every test in this file therefore errored on seeding, so
        # the whole engine suite was unrunnable rather than red on a guard.
        for role, kind, name, agg, value_type in (
            ("DATE", "dimension", "media_date", None, "date"),
            ("COST", "metric", "net_cost", "sum", "money"),
        ):
            cur.execute(
                "INSERT INTO app.mdm_canonical_fields "
                "(id,project_id,concept_kind,canonical_name,aggregation,value_type,created_by) "
                "VALUES (%s,%s,%s,%s,%s,%s,'story-38.17-harness')",
                (fields[role], project_id, kind, f"{name}_{project_id[-8:]}", agg, value_type),
            )
        cur.execute(
            """INSERT INTO app.datastreams
               (id,project_id,org_id,name,module_name,connection_ref_id,enabled,
                schedule_mode,config,created_by,lifecycle_state,source_kind,data_role)
               VALUES (%s,%s,%s,'Change engine harness',NULL,NULL,TRUE,'manual',
                       %s::jsonb,'owner@example.com','active','managed_feed','Spend')""",
            (ds_id, project_id, org_id, json.dumps(config or {})),
        )
        cur.execute(
            """INSERT INTO app.datastream_plan_versions
               (id,datastream_id,project_id,version_number,contract_version,source_kind,
                writer_kind,destination_policy,normalized_payload,content_hash,
                capability_fingerprint,idempotency_key_hash,created_by)
               VALUES (%s,%s,%s,1,'1','managed_feed','toorow','managed_raw',
                       %s::jsonb,repeat('a',64),repeat('a',64),repeat('b',64),
                       'story-38.17-harness')""",
            (plan_id, ds_id, project_id, json.dumps({"contract_version": "1"})),
        )
        cur.execute(
            "UPDATE app.datastreams SET current_plan_version_id=%s WHERE id=%s",
            (plan_id, ds_id),
        )
    conn.commit()

    mapping = save_field_mapping(
        datastream_id=ds_id,
        project_id=project_id,
        mapping_payload=_mapping_payload(plan_id, fields, cost_alias="net_cost"),
        identity="owner@example.com",
        idempotency_key=_id("idem_"),
        conn=conn,
        pinned_plan_version_id=plan_id,
        advance_pointer=True,
        commit=True,
    )
    return {
        "org_id": org_id,
        "project_id": project_id,
        "datastream_id": ds_id,
        "plan_id": plan_id,
        "mapping_id": mapping["id"],
        "fields": fields,
    }


def _proposed(scope: dict[str, Any], *, cost_alias: str = "media_cost") -> dict[str, Any]:
    return _mapping_payload(scope["plan_id"], scope["fields"], cost_alias=cost_alias)


def _prepare(conn, scope: dict[str, Any]) -> dict[str, Any]:
    from core.datastream_change import prepare_change

    prepared = prepare_change(
        conn,
        project_id=scope["project_id"],
        datastream_id=scope["datastream_id"],
        kind="mapping",
        proposed_payload=_proposed(scope),
        actor="owner@example.com",
        idempotency_key=_id("chg_"),
    )
    conn.commit()
    return prepared


def _confirm(conn, scope: dict[str, Any], preparation_id: str, secret: str) -> dict[str, Any]:
    from core.datastream_change import confirm_change

    result = confirm_change(
        conn,
        project_id=scope["project_id"],
        datastream_id=scope["datastream_id"],
        preparation_id=preparation_id,
        confirmation_secret=secret,
        actor="owner@example.com",
    )
    conn.commit()
    return result


# ---------------------------------------------------------------------------
# Counters: what "nothing happened" MEANS, measured in the database.
# ---------------------------------------------------------------------------


def _counts(conn, scope: dict[str, Any]) -> dict[str, Any]:
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
            "SELECT COUNT(*) FROM app.operations WHERE command_type='datastream.mapping.change' "
            "AND resource_path::text LIKE %s",
            (f"%{scope['datastream_id']}%",),
        )
        operations = cur.fetchone()[0]
        cur.execute(
            "SELECT current_mapping_version_id,current_plan_version_id "
            "FROM app.datastreams WHERE id=%s",
            (scope["datastream_id"],),
        )
        pointers = cur.fetchone()
    return {
        "mapping_versions": mappings,
        "executions": executions,
        "operations": operations,
        "current_mapping_version_id": pointers[0],
        "current_plan_version_id": pointers[1],
    }


# ---------------------------------------------------------------------------
# AC2 -- the AD-27 confirmation is exact, single-use and short-lived.
# ---------------------------------------------------------------------------


@requires_postgres
def test_a_wrong_confirmation_secret_dispatches_nothing_at_all(conn, scope) -> None:
    """AC2, the secret half. THE mutation the 2026-08-10 review reproduced.

    Replacing the comparison with `pass` left 74 passed / 0 failed: any string
    confirmed any preparation. The proof cannot be that a call raises -- it is
    that after the refusal the DATABASE is exactly where it was: no appended
    mapping version, no candidate execution, no operation row, and the
    preparation still spendable.
    """
    from core.datastream_change import DatastreamChangeError

    before = _counts(conn, scope)
    prepared = _prepare(conn, scope)

    with pytest.raises(DatastreamChangeError):
        _confirm(conn, scope, prepared["preparation_id"], "not-the-secret")
    conn.rollback()

    after = _counts(conn, scope)
    assert after["mapping_versions"] == before["mapping_versions"]
    assert after["executions"] == before["executions"]
    assert after["operations"] == before["operations"]

    with conn.cursor() as cur:
        cur.execute(
            "SELECT state,operation_id,candidate_execution_id "
            "FROM app.datastream_change_preparations WHERE id=%s",
            (prepared["preparation_id"],),
        )
        state, operation_id, candidate_id = cur.fetchone()
    assert (state, operation_id, candidate_id) == ("prepared", None, None)

    # And the RIGHT secret on the SAME preparation does dispatch: without this
    # half, a comparison that refused everything would pass the test above.
    result = _confirm(conn, scope, prepared["preparation_id"], prepared["confirmation_secret"])
    assert result["candidate_execution_id"]
    assert _counts(conn, scope)["executions"] == before["executions"] + 1


@requires_postgres
def test_an_expired_confirmation_dispatches_nothing(conn, scope) -> None:
    """AC2, the expiry half -- with the correct secret, so only expiry can refuse."""
    from core.datastream_change import DatastreamChangeError

    before = _counts(conn, scope)
    prepared = _prepare(conn, scope)
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE app.datastream_change_preparations "
            "SET expires_at = NOW() - INTERVAL '1 second' WHERE id=%s",
            (prepared["preparation_id"],),
        )
    conn.commit()

    with pytest.raises(DatastreamChangeError):
        _confirm(conn, scope, prepared["preparation_id"], prepared["confirmation_secret"])
    conn.rollback()

    after = _counts(conn, scope)
    assert after["executions"] == before["executions"]
    assert after["mapping_versions"] == before["mapping_versions"]
    assert after["operations"] == before["operations"]


@requires_postgres
def test_a_spent_preparation_marked_expired_dispatches_nothing(conn, scope) -> None:
    """AC2, the state half. `state` ROUTES; without the branch, `expired` commits.

    `expired` is a real value of the CHECK on
    `app.datastream_change_preparations.state` (migration 138), and it is the one
    state that must never reach the writer -- `confirmed` legitimately replays
    (AC4), so the `confirmed` branch alone cannot prove the guard exists.
    """
    from core.datastream_change import DatastreamChangeError

    before = _counts(conn, scope)
    prepared = _prepare(conn, scope)
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE app.datastream_change_preparations SET state='expired' WHERE id=%s",
            (prepared["preparation_id"],),
        )
    conn.commit()

    with pytest.raises(DatastreamChangeError):
        _confirm(conn, scope, prepared["preparation_id"], prepared["confirmation_secret"])
    conn.rollback()

    after = _counts(conn, scope)
    assert after["executions"] == before["executions"]
    assert after["mapping_versions"] == before["mapping_versions"]
    assert after["operations"] == before["operations"]


@requires_postgres
def test_the_confirmation_window_persisted_is_the_short_one(conn, scope) -> None:
    """AC2, "short-lived", read from the ROW rather than from the constant.

    The engine declared the window twice -- `timedelta(minutes=15)` on
    `expires_at` and a literal `900` on the wire. Widening one to ten years was
    invisible to every test. This measures the interval Postgres actually stored
    and pins the wire value to it, so one edit cannot move only one of them.
    """
    prepared = _prepare(conn, scope)
    with conn.cursor() as cur:
        cur.execute(
            "SELECT EXTRACT(EPOCH FROM (expires_at - prepared_at))::bigint "
            "FROM app.datastream_change_preparations WHERE id=%s",
            (prepared["preparation_id"],),
        )
        window_seconds = cur.fetchone()[0]

    assert 0 < window_seconds <= 900, (
        f"the persisted confirmation window is {window_seconds}s. AD-27 asks for a "
        "short-lived confirmation; anything past 15 minutes is a standing authorization."
    )
    assert prepared["expires_in_seconds"] == window_seconds, (
        "the window announced on the wire and the window Postgres enforces have "
        "drifted apart; a caller cannot plan against a number that is not the rule"
    )


# ---------------------------------------------------------------------------
# AC3 -- bindings revalidated, prior executions and publications untouched.
# ---------------------------------------------------------------------------


@requires_postgres
def test_a_change_whose_active_mapping_moved_after_review_dispatches_nothing(conn, scope) -> None:
    """AC3, the revalidation. Deleting the comparison left 0 red on 2026-08-10.

    A preparation is frozen against exact pointers. If the active mapping version
    moves between review and confirmation, the reviewed diff describes a state
    that no longer exists, and committing it would silently overwrite whoever
    moved it.
    """
    from core.datastream_change import DatastreamChangeError
    from core.datastream_field_mapping import save_field_mapping

    prepared = _prepare(conn, scope)

    # Somebody else's work lands between review and confirmation.
    save_field_mapping(
        datastream_id=scope["datastream_id"],
        project_id=scope["project_id"],
        mapping_payload=_mapping_payload(
            scope["plan_id"], scope["fields"], cost_alias="other_session_cost"
        ),
        identity="other@example.com",
        idempotency_key=_id("idem_"),
        conn=conn,
        pinned_plan_version_id=scope["plan_id"],
        advance_pointer=True,
        commit=True,
    )
    before = _counts(conn, scope)

    with pytest.raises(DatastreamChangeError):
        _confirm(conn, scope, prepared["preparation_id"], prepared["confirmation_secret"])
    conn.rollback()

    after = _counts(conn, scope)
    assert after["executions"] == before["executions"], (
        "a candidate was dispatched against pointers the review never saw"
    )
    assert after["mapping_versions"] == before["mapping_versions"]
    assert after["current_mapping_version_id"] == before["current_mapping_version_id"], (
        "the other session's version was overwritten"
    )


@requires_postgres
def test_a_confirmed_change_appends_without_moving_any_active_pointer(conn, scope) -> None:
    """AC3, the pointer. THE RATIFIED DIVISION OF LABOUR, not an omission.

    `docs/product-architecture/datastream-workbench-and-wizard.md:1019` gives the
    Mapping tab `Prepare mapping change` then `Review and confirm`, and `:2128`
    gives the pointer move to a LATER and separate step -- "`Publish and
    activate` atomically makes it current" -- whose single writer is
    `datastream_activation.py:695`. `:2470` states the invariant from the other
    side: the area is incomplete if "a first publication can become current
    before a reviewed candidate exists".

    So the assertion is the one the document makes: a version is appended, a
    candidate is dispatched, and BOTH pointers are exactly where they were.
    """
    before = _counts(conn, scope)
    prepared = _prepare(conn, scope)
    result = _confirm(conn, scope, prepared["preparation_id"], prepared["confirmation_secret"])
    after = _counts(conn, scope)

    assert after["mapping_versions"] == before["mapping_versions"] + 1
    assert after["executions"] == before["executions"] + 1
    assert result["mapping_version_id"] != before["current_mapping_version_id"]
    assert after["current_mapping_version_id"] == before["current_mapping_version_id"]
    assert after["current_plan_version_id"] == before["current_plan_version_id"]
    assert result["active_versions_unchanged"] is True

    # The appended version exists and is NOT the active one: "appended" and
    # "activated" are two facts, and only one of them happened.
    with conn.cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM app.datastream_mapping_versions WHERE id=%s",
            (result["mapping_version_id"],),
        )
        assert cur.fetchone()[0] == 1


@requires_postgres
def test_a_confirmed_change_leaves_earlier_executions_untouched(conn, scope) -> None:
    """AC3, "never mutates prior executions or publications"."""
    prepared = _prepare(conn, scope)
    first = _confirm(conn, scope, prepared["preparation_id"], prepared["confirmation_secret"])

    with conn.cursor() as cur:
        cur.execute(
            "SELECT state,plan_version_id,mapping_version_id FROM app.datastream_executions "
            "WHERE id=%s",
            (first["candidate_execution_id"],),
        )
        snapshot = cur.fetchone()

    # A SECOND governed change, start to finish, on the same Datastream.
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE app.datastream_executions SET state='failed' WHERE id=%s",
            (first["candidate_execution_id"],),
        )
    conn.commit()
    second_prepared = _prepare(conn, scope)
    second = _confirm(
        conn, scope, second_prepared["preparation_id"], second_prepared["confirmation_secret"]
    )
    assert second["candidate_execution_id"] != first["candidate_execution_id"]

    with conn.cursor() as cur:
        cur.execute(
            "SELECT plan_version_id,mapping_version_id FROM app.datastream_executions WHERE id=%s",
            (first["candidate_execution_id"],),
        )
        assert cur.fetchone() == (snapshot[1], snapshot[2]), (
            "the second change rewrote the version bindings of the first candidate"
        )


# ---------------------------------------------------------------------------
# AC4 -- a retry replays; an uncertain outcome is reconciled, never resubmitted.
# ---------------------------------------------------------------------------


@requires_postgres
def test_a_retry_returns_the_original_operation_and_dispatches_no_second_candidate(
    conn, scope
) -> None:
    """AC4, first half. The successor the XFAIL of `test_datastream_mapping_api.py`
    never got when 26695dc retired `POST /api/datastreams/{id}/mapping/versions`.

    Before this repair a second confirmation raised "Change preparation is no
    longer confirmable" at `datastream_change.py:218` -- an ERROR, which is the
    one answer that is not idempotent, and which reached that line before
    `execute_operation`'s replay could ever be consulted.
    """
    prepared = _prepare(conn, scope)
    first = _confirm(conn, scope, prepared["preparation_id"], prepared["confirmation_secret"])
    after_first = _counts(conn, scope)

    replay = _confirm(conn, scope, prepared["preparation_id"], prepared["confirmation_secret"])
    after_replay = _counts(conn, scope)

    assert replay["operation_id"] == first["operation_id"]
    assert replay["candidate_execution_id"] == first["candidate_execution_id"]
    assert replay["mapping_version_id"] == first["mapping_version_id"]
    assert replay["replayed"] is True and first["replayed"] is False
    assert after_replay["executions"] == after_first["executions"]
    assert after_replay["mapping_versions"] == after_first["mapping_versions"]
    assert after_replay["operations"] == after_first["operations"]


@requires_postgres
def test_a_retry_still_requires_the_confirmation_secret(conn, scope) -> None:
    """AC4 must not become a hole in AC2: a replay is not a public read."""
    from core.datastream_change import DatastreamChangeError

    prepared = _prepare(conn, scope)
    _confirm(conn, scope, prepared["preparation_id"], prepared["confirmation_secret"])

    with pytest.raises(DatastreamChangeError):
        _confirm(conn, scope, prepared["preparation_id"], "not-the-secret")
    conn.rollback()


@requires_postgres
def test_an_uncertain_outcome_is_reconciled_from_evidence_not_resubmitted(conn, scope) -> None:
    """AC4, second half. `outcome_unknown` had ZERO implementation before this.

    The uncertainty is produced the way production produces it -- through
    `operations.record_delivery_result(uncertain=True)`, not by writing the state
    by hand -- and the reconciliation must read the EFFECT (does the candidate
    execution exist?) rather than run the effect again.
    """
    from core.operations import record_delivery_result

    prepared = _prepare(conn, scope)
    first = _confirm(conn, scope, prepared["preparation_id"], prepared["confirmation_secret"])
    after_first = _counts(conn, scope)

    with conn.cursor() as cur:
        cur.execute(
            "SELECT id FROM app.operation_outbox WHERE operation_id=%s", (first["operation_id"],)
        )
        outbox_id = cur.fetchone()[0]
    record_delivery_result(
        conn, event_id=outbox_id, confirmed=False, error_class="timeout", uncertain=True
    )
    conn.commit()

    with conn.cursor() as cur:
        cur.execute("SELECT state FROM app.operations WHERE id=%s", (first["operation_id"],))
        assert cur.fetchone()[0] == "outcome_unknown", (
            "the harness failed to produce the uncertainty it means to reconcile"
        )

    replay = _confirm(conn, scope, prepared["preparation_id"], prepared["confirmation_secret"])
    after_replay = _counts(conn, scope)

    assert replay["operation_id"] == first["operation_id"]
    assert replay["outcome"] == "succeeded", (
        "the candidate execution is on disk, so the uncertain operation resolves to "
        "succeeded; leaving it uncertain is what invites a blind resubmission"
    )
    assert after_replay["executions"] == after_first["executions"]
    assert after_replay["operations"] == after_first["operations"]

    with conn.cursor() as cur:
        cur.execute(
            "SELECT state,result->>'reconciliation_evidence_hash' FROM app.operations WHERE id=%s",
            (first["operation_id"],),
        )
        state, evidence = cur.fetchone()
    assert state == "succeeded"
    assert evidence and len(evidence) == 64, (
        "a reconciliation without durable evidence is indistinguishable from a guess"
    )


# ---------------------------------------------------------------------------
# AC1 -- what the confirmation SHOWS. The AC enumerates its own list
# (`stories-epic-38-inbound-managed-file-connector.md:381`), so every element
# below is tested by the CHANGE OF ANSWER between two states, never by
# `"x" in review`.
# ---------------------------------------------------------------------------


def _mint_operation(conn, *, org_id: str, command_type: str, resource_path: tuple[str, ...]) -> str:
    """One durable operation through the real seam, for a guard that demands one."""
    from core.operations import MutationResult, OperationSpec, execute_operation

    def mutation(_tx, _operation_id: str) -> MutationResult:
        return MutationResult(
            outcome="succeeded", before_hash=None, after_hash=None, result={}, outbox_payload={}
        )

    operation = execute_operation(
        conn,
        OperationSpec(
            command_type=command_type,
            actor="owner@example.com",
            effective_org_id=org_id,
            resource_path=resource_path,
            idempotency_key=_id("idem_"),
            host_context={},
            versions={},
            request_payload={},
            provider_references={},
            confirmation_mode="none",
            confirmation_reference=None,
            trace_id=None,
        ),
        mutation=mutation,
    )
    conn.commit()
    return operation.operation_id


def _prepare_with(conn, scope: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    from core.datastream_change import prepare_change

    prepared = prepare_change(
        conn,
        project_id=scope["project_id"],
        datastream_id=scope["datastream_id"],
        kind="mapping",
        proposed_payload=payload,
        actor="owner@example.com",
        idempotency_key=_id("chg_"),
    )
    conn.commit()
    return prepared


def _seed_raw_import(conn, scope: dict[str, Any], *, ordinal: int = 0) -> str:
    receipt_id = "inbrx_" + uuid.uuid4().hex[:26]
    raw_import_id = "inbraw_" + uuid.uuid4().hex[:26]
    receipt_operation = _mint_operation(
        conn,
        org_id=scope["org_id"],
        command_type="inbound.receipt.recorded",
        resource_path=(f"datastream:{scope['datastream_id']}", f"receipt:{receipt_id}"),
    )
    import_operation = _mint_operation(
        conn,
        org_id=scope["org_id"],
        command_type="inbound.raw_import.recorded",
        resource_path=(
            f"datastream:{scope['datastream_id']}",
            f"receipt:{receipt_id}",
            f"attachment:{ordinal}",
        ),
    )
    content_hash = "d" * 64
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO app.inbound_receipts "
            "(id,datastream_id,channel,provider_event_id,receipt_fingerprint,operation_id) "
            "VALUES (%s,%s,'upload',%s,repeat('c',64),%s)",
            (receipt_id, scope["datastream_id"], _id("evt_"), receipt_operation),
        )
        cur.execute(
            "INSERT INTO app.inbound_raw_imports "
            "(id,receipt_id,datastream_id,ordinal,size_bytes,content_hash,quarantine_uri,"
            " retention_expires_at,retention_policy_version,retention_days,operation_id) "
            "VALUES (%s,%s,%s,%s,10,%s,%s,NOW() + INTERVAL '30 days',"
            " 'quarantine-retention-v1',30,%s)",
            (
                raw_import_id,
                receipt_id,
                scope["datastream_id"],
                ordinal,
                content_hash,
                f"gs://example-quarantine/inbound/{scope['org_id']}/"
                f"{scope['datastream_id']}/{content_hash}/{ordinal}",
                import_operation,
            ),
        )
    conn.commit()
    return raw_import_id


@requires_postgres
def test_the_review_names_the_organization_it_asks_the_person_to_act_for(conn, scope) -> None:
    """AC1, `organization`. `org_id` was read as `row[0]` and written into the
    preparation row all along -- it never reached the person confirming.

    TWO SCOPES, because a single one cannot tell a real read from a constant: an
    implementation that returned `project_id`, or the string of the first row it
    ever saw, would satisfy one assertion and fail these.
    """
    other = _seed_scope(conn)
    assert other["org_id"] != scope["org_id"]

    mine = _prepare(conn, scope)["review"]
    theirs = _prepare_with(conn, other, _proposed(other))["review"]

    assert mine["organization_id"] == scope["org_id"]
    assert theirs["organization_id"] == other["org_id"]
    assert mine["organization_id"] != theirs["organization_id"]
    assert mine["organization_id"] != mine["project_id"]


@requires_postgres
def test_the_review_names_the_template_version_or_says_why_there_is_none(conn) -> None:
    """AC1, the `template` version -- and the ONE hole it shares with 38.16 AC2.

    THREE states, because `not_applicable`, `unknown` and `bound` are three
    different facts and a null would flatten them into one. A connector-shaped
    Datastream has no Template; a dangling reference is not an absent Template;
    a real binding reports the version number.
    """
    from core.file_source_template import create_file_source_template

    unpinned = _seed_scope(conn)
    review = _prepare(conn, unpinned)["review"]
    assert review["template_version"]["state"] == "not_applicable"
    assert review["template_version"]["reason"]
    assert "version" not in review["template_version"]

    dangling = _seed_scope(
        conn, config={"source_owner": {"managed_feed_template_ref": "fst_does_not_exist"}}
    )
    review = _prepare(conn, dangling)["review"]
    assert review["template_version"]["state"] == "unknown", (
        "a Template reference that resolves to no row must not read as 'no Template'"
    )
    assert review["template_version"]["template_id"] == "fst_does_not_exist"

    pinned = _seed_scope(conn)
    template = create_file_source_template(
        conn,
        project_id=pinned["project_id"],
        org_id=pinned["org_id"],
        template_code=f"tpl_{pinned['project_id'][-8:]}",
        contract={
            "kind": "catalog",
            "required_fields": [pinned["fields"]["DATE"], pinned["fields"]["COST"]],
            "optional_fields": [],
            "grain": "daily",
            "class": "planned",
            "placement": {
                "metric": pinned["fields"]["COST"],
                "period": pinned["fields"]["DATE"],
            },
            "format": "csv",
            "aliases": {
                pinned["fields"]["DATE"]: ["Date"],
                pinned["fields"]["COST"]: ["Cost"],
            },
        },
        created_by="owner@example.com",
    )
    conn.commit()
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE app.datastreams SET config=%s::jsonb WHERE id=%s",
            (
                json.dumps({"source_owner": {"managed_feed_template_ref": template["id"]}}),
                pinned["datastream_id"],
            ),
        )
    conn.commit()

    review = _prepare(conn, pinned)["review"]
    assert review["template_version"]["state"] == "bound"
    assert review["template_version"]["template_id"] == template["id"]
    assert review["template_version"]["version"] == template["version"]


@requires_postgres
def test_file_repair_freezes_its_raw_import_and_rejects_another_scope(conn, scope) -> None:
    """Story 38.16: the reviewed file is a durable optimistic-lock binding."""
    from core.datastream_change import DatastreamChangeError, prepare_change

    raw_import_id = _seed_raw_import(conn, scope)
    prepared = prepare_change(
        conn,
        project_id=scope["project_id"],
        datastream_id=scope["datastream_id"],
        kind="mapping",
        proposed_payload=_proposed(scope),
        actor="owner@example.com",
        idempotency_key=_id("chg_"),
        raw_import_id=raw_import_id,
    )
    conn.commit()
    assert prepared["review"]["raw_import"]["raw_import_id"] == raw_import_id
    with conn.cursor() as cur:
        cur.execute(
            "SELECT expected_raw_import_id,binding_snapshot_version "
            "FROM app.datastream_change_preparations WHERE id=%s",
            (prepared["preparation_id"],),
        )
        assert cur.fetchone() == (raw_import_id, 1)

    other = _seed_scope(conn)
    with pytest.raises(DatastreamChangeError, match="does not belong"):
        prepare_change(
            conn,
            project_id=other["project_id"],
            datastream_id=other["datastream_id"],
            kind="mapping",
            proposed_payload=_proposed(other),
            actor="owner@example.com",
            idempotency_key=_id("chg_"),
            raw_import_id=raw_import_id,
        )
    conn.rollback()


@requires_postgres
def test_confirmation_refuses_a_template_binding_changed_after_review(conn, scope) -> None:
    """Story 38.16: rendering a Template is not enough; confirmation rechecks it."""
    from core.datastream_change import DatastreamChangeError
    from core.file_source_template import create_file_source_template

    template = create_file_source_template(
        conn,
        project_id=scope["project_id"],
        org_id=scope["org_id"],
        template_code=f"tpl_{scope['project_id'][-8:]}",
        contract={
            "kind": "catalog",
            "required_fields": [scope["fields"]["DATE"], scope["fields"]["COST"]],
            "optional_fields": [],
            "grain": "daily",
            "class": "planned",
            "placement": {
                "metric": scope["fields"]["COST"],
                "period": scope["fields"]["DATE"],
            },
            "format": "csv",
            "aliases": {},
        },
        created_by="owner@example.com",
    )
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE app.datastreams SET config=%s::jsonb WHERE id=%s",
            (
                json.dumps({"source_owner": {"managed_feed_template_ref": template["id"]}}),
                scope["datastream_id"],
            ),
        )
    conn.commit()
    prepared = _prepare(conn, scope)
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE app.datastreams SET config='{}'::jsonb WHERE id=%s",
            (scope["datastream_id"],),
        )
    conn.commit()

    before = _counts(conn, scope)
    with pytest.raises(DatastreamChangeError, match="Template binding changed"):
        _confirm(conn, scope, prepared["preparation_id"], prepared["confirmation_secret"])
    conn.rollback()
    assert _counts(conn, scope) == before


@requires_postgres
def test_the_review_counts_the_retained_imports_the_change_would_reach(conn, scope) -> None:
    """AC1, `affected imports`. A COUNT that MOVES with reality, never a `0`
    standing in for "I did not look"."""
    empty = _prepare(conn, scope)["review"]["affected_imports"]
    assert empty == {"state": "counted", "retained_raw_imports": 0}

    # The durability guards of migrations 184/185 refuse a receipt or a raw
    # import whose governed provenance does not name it, so the fixture mints
    # those operations through `execute_operation` rather than inventing ids the
    # triggers would reject. The rows are otherwise minimal: this test measures a
    # COUNT, and the inbound pipeline has its own coverage.
    receipt_id = "inbrx_" + uuid.uuid4().hex[:26]
    content_hash = "d" * 64
    _mint_operation(
        conn,
        org_id=scope["org_id"],
        command_type="inbound.receipt.recorded",
        resource_path=(f"datastream:{scope['datastream_id']}",),
    )
    receipt_operation = _mint_operation(
        conn,
        org_id=scope["org_id"],
        command_type="inbound.receipt.recorded",
        resource_path=(f"datastream:{scope['datastream_id']}", f"receipt:{receipt_id}"),
    )
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO app.inbound_receipts "
            "(id,datastream_id,channel,provider_event_id,receipt_fingerprint,operation_id) "
            "VALUES (%s,%s,'upload',%s,repeat('c',64),%s)",
            (receipt_id, scope["datastream_id"], _id("evt_"), receipt_operation),
        )
    conn.commit()

    for ordinal in range(2):
        import_operation = _mint_operation(
            conn,
            org_id=scope["org_id"],
            command_type="inbound.raw_import.recorded",
            resource_path=(
                f"datastream:{scope['datastream_id']}",
                f"receipt:{receipt_id}",
                f"attachment:{ordinal}",
            ),
        )
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO app.inbound_raw_imports "
                "(id,receipt_id,datastream_id,ordinal,size_bytes,content_hash,quarantine_uri,"
                " retention_expires_at,retention_policy_version,retention_days,operation_id) "
                "VALUES (%s,%s,%s,%s,10,%s,%s,NOW() + INTERVAL '30 days',"
                " 'quarantine-retention-v1',30,%s)",
                (
                    "inbraw_" + uuid.uuid4().hex[:26],
                    receipt_id,
                    scope["datastream_id"],
                    ordinal,
                    content_hash,
                    f"gs://example-quarantine/inbound/{scope['org_id']}/"
                    f"{scope['datastream_id']}/{content_hash}/{ordinal}",
                    import_operation,
                ),
            )
        conn.commit()

    filled = _prepare(conn, scope)["review"]["affected_imports"]
    assert filled == {"state": "counted", "retained_raw_imports": 2}


@requires_postgres
def test_the_review_counts_the_impact_before_the_act(conn, scope) -> None:
    """AC1, `impact`. The Mapping tab already sets this bar for its own controls
    -- "N of 46 columns stop landing" BEFORE an exclusion is confirmed
    (`datastream-workbench-and-wizard.md:1019`). A hash diff is not a count.

    Three proposals, three different answers: a drop, an addition, a rebind.
    """
    base = _mapping_payload(scope["plan_id"], scope["fields"], cost_alias="net_cost")

    dropped = deepcopy(base)
    dropped["fields"] = [f for f in dropped["fields"] if f["field_id"] == "date"]
    impact = _prepare_with(conn, scope, dropped)["review"]["impact"]
    assert impact["state"] == "counted"
    assert impact["fields_landing_before"] == 2
    assert impact["fields_landing_after"] == 1
    assert impact["fields_no_longer_landing"] == 1
    assert impact["fields_newly_landing"] == 0

    added = deepcopy(base)
    added["fields"].append(
        _field("impressions", role="measure", aggregation="sum", mdm_target=scope["fields"]["COST"])
    )
    impact = _prepare_with(conn, scope, added)["review"]["impact"]
    assert impact["fields_newly_landing"] == 1
    assert impact["fields_no_longer_landing"] == 0
    assert impact["fields_landing_after"] == 3

    rebound = deepcopy(base)
    rebound["fields"][1]["binding"]["mdm_target"] = scope["fields"]["DATE"]
    impact = _prepare_with(conn, scope, rebound)["review"]["impact"]
    assert impact["fields_rebound"] == 1
    assert impact["fields_no_longer_landing"] == 0
    assert impact["fields_newly_landing"] == 0


@requires_postgres
def test_an_excluded_column_stops_landing_and_the_count_says_so(conn, scope) -> None:
    """AC1, `impact`, the branch that decides which authority is read.

    `binding.status == "excluded"` is the SOLE authority of exclusion
    (`datastream-workbench-and-wizard.md:1019`); the `included` boolean it
    replaced had one writer that hard-coded `True`. A count that read the wrong
    one would report nothing stops landing while a column stops landing.
    """
    excluded = _mapping_payload(scope["plan_id"], scope["fields"], cost_alias="net_cost")
    excluded["fields"][1]["binding"]["status"] = "excluded"
    impact = _prepare_with(conn, scope, excluded)["review"]["impact"]
    assert impact["fields_no_longer_landing"] == 1
    assert impact["fields_landing_after"] == 1


@requires_postgres
def test_the_review_says_what_changes_in_VALUES_and_names_the_concept(conn, scope) -> None:
    """2026-08-18. A confirmation that shows two hashes shows nothing.

    `diff` carries `before_hash`/`after_hash` per top-level key, so excluding one
    column of forty-six reached the person as `$.fields 9f2c… 4b70…`. The review
    now also carries the same difference in VALUES, composed against the base the
    server resolved and with the registry identity resolved to the name a person
    reads — which is exactly what needs a live vocabulary to be proved at all.
    """
    excluded = _mapping_payload(scope["plan_id"], scope["fields"], cost_alias="net_cost")
    excluded["fields"][1]["binding"]["status"] = "excluded"
    review = _prepare_with(conn, scope, excluded)["review"]

    assert review["value_diff"]["state"] == "composed"
    landing = [
        entry for entry in review["value_diff"]["entries"] if entry["reading"] == "Landing"
    ]
    assert [entry["subject"] for entry in landing] == [excluded["fields"][1]["field_id"]]
    assert landing[0]["before"] == "Lands"
    assert landing[0]["after"] == "Does not land (excluded)"

    # THE CONCEPT IS NAMED, and the name comes from `app.mdm_canonical_fields` --
    # the read this test exists to exercise. A rebind that only said
    # `mdm_6D13WZ…` would be the identity where the row shows a name.
    rebound = _mapping_payload(scope["plan_id"], scope["fields"], cost_alias="net_cost")
    rebound["fields"][1]["binding"]["mdm_target"] = scope["fields"]["DATE"]
    entries = _prepare_with(conn, scope, rebound)["review"]["value_diff"]["entries"]
    targets = [entry for entry in entries if entry["reading"] == "Governed target"]
    assert targets, "a rebind must be reported as a rebind"
    assert scope["fields"]["DATE"] in targets[0]["after"]
    assert targets[0]["after"] != scope["fields"]["DATE"], (
        "the registry identity travels beside the canonical NAME, never alone"
    )

    # And the hashed reading is untouched: it is what `MutationResult` compares.
    assert all("before_hash" in change for change in review["diff"])


@requires_postgres
def test_the_review_carries_the_verdict_of_the_normalizer_the_confirmation_will_run(
    conn, scope
) -> None:
    """AC1, `validation evidence`. Two states, and the verdict comes from the
    SAME function `save_field_mapping` will call -- a second derivation of
    "is this acceptable" is a copy free to drift from the one that decides."""
    good = _prepare(conn, scope)["review"]["validation"]
    assert good["state"] == "contract_satisfied"
    assert good["checked_by"] == "normalize_mapping"
    assert len(good["proposed_content_hash"]) == 64

    broken = _mapping_payload(scope["plan_id"], scope["fields"], cost_alias="media_cost")
    broken.pop("ambiguities")
    bad = _prepare_with(conn, scope, broken)["review"]["validation"]
    assert bad["state"] == "contract_violated"
    assert bad["issue_count"] >= 1
    assert "proposed_content_hash" not in bad


@requires_postgres
def test_the_review_names_a_rollback_path_that_is_still_live_after_the_change(conn, scope) -> None:
    """AC1, `rollback path`. The promise is checkable, not decorative: the
    versions the review names as the way back are the versions the database
    still has in force once the candidate has been dispatched."""
    prepared = _prepare(conn, scope)
    rollback = prepared["review"]["rollback"]
    assert rollback["mapping_version_id"] == scope["mapping_id"]
    assert rollback["plan_version_id"] == scope["plan_id"]

    result = _confirm(conn, scope, prepared["preparation_id"], prepared["confirmation_secret"])
    assert rollback["mapping_version_id"] != result["mapping_version_id"], (
        "the review named the version it was about to append as the way back to itself"
    )

    after = _counts(conn, scope)
    assert after["current_mapping_version_id"] == rollback["mapping_version_id"]
    assert after["current_plan_version_id"] == rollback["plan_version_id"]


@requires_postgres
def test_a_preparation_frozen_under_the_older_review_shape_still_confirms(conn, scope) -> None:
    """The `review_hash` freeze, checked rather than assumed.

    AC1 added seven keys to a payload that is content-hashed and stored. If
    `confirm_change` recomputed the hash instead of carrying the stored one, every
    preparation in flight at deploy time would have died on a shape change it had
    no part in. This rewrites a stored review to the nine-key shape that predates
    this story and confirms it.
    """
    prepared = _prepare(conn, scope)
    legacy = {
        key: prepared["review"][key]
        for key in (
            "kind",
            "project_id",
            "datastream_id",
            "expected_plan_version_id",
            "expected_mapping_version_id",
            "before_hash",
            "after_hash",
            "diff",
            "consequence",
        )
    }
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE app.datastream_change_preparations "
            "SET review=%s::jsonb,binding_snapshot_version=0 WHERE id=%s",
            (json.dumps(legacy), prepared["preparation_id"]),
        )
    conn.commit()

    result = _confirm(conn, scope, prepared["preparation_id"], prepared["confirmation_secret"])
    assert result["candidate_execution_id"]

    with conn.cursor() as cur:
        cur.execute(
            "SELECT review_hash,review FROM app.datastream_change_preparations WHERE id=%s",
            (prepared["preparation_id"],),
        )
        stored_hash, stored_review = cur.fetchone()
    assert stored_hash == prepared["review_hash"], (
        "the confirmation rewrote the frozen review hash; a preparation is immutable"
    )
    assert "template_version" not in stored_review, (
        "the fixture did not actually confirm the older nine-key shape"
    )


@requires_postgres
def test_the_review_never_names_a_candidate_that_does_not_exist_yet(conn, scope) -> None:
    """AC1, the `candidate` version -- the one element that is structurally absent.

    It cannot be shown because it is not minted until the confirmation, and the
    honest answer to "which candidate?" before there is one is a named state, not
    a null that a screen would render as "none". The discriminator is that NO key
    of the frozen review carries an execution id, while the confirmation's result
    does: a future edit that resolves the candidate early would fail here.
    """
    prepared = _prepare(conn, scope)
    candidate = prepared["review"]["candidate_version"]
    assert candidate["state"] == "not_yet_minted"
    assert candidate["reason"]
    assert "dse_" not in json.dumps(prepared["review"]), (
        "the review names an execution id before the confirmation has minted one"
    )

    result = _confirm(conn, scope, prepared["preparation_id"], prepared["confirmation_secret"])
    assert result["candidate_execution_id"].startswith("dse_")


# ---------------------------------------------------------------------------
# The UNREADABLE-STORE branch. A cursor that raises is not a fake of the subject
# here -- it IS the condition under test, and it is the only way to reach a
# branch that exists precisely because a deployment may lack the epic-38 tables.
# Everything else in this file refuses mocks for exactly the opposite reason.
# ---------------------------------------------------------------------------


class _RaisingCursor:
    """A cursor that answers SAVEPOINT/ROLLBACK and raises on anything else."""

    def __init__(self) -> None:
        self.statements: list[str] = []

    def execute(self, sql: str, params: Any = None) -> None:
        self.statements.append(sql)
        if sql.startswith(("SAVEPOINT", "ROLLBACK TO SAVEPOINT", "RELEASE SAVEPOINT")):
            return
        raise psycopg.errors.UndefinedTable("relation does not exist")

    def fetchone(self):  # pragma: no cover -- never reached
        raise AssertionError("the raising cursor never returns a row")


def test_an_unreadable_import_ledger_says_so_and_never_answers_zero() -> None:
    """`0` and "I could not look" are two answers, and only one of them is a count.

    A deployment whose epic-38 migrations have not landed raises `UndefinedTable`
    here. Reporting `retained_raw_imports: 0` would tell a reviewer that this
    change reaches no retained file -- a statement nobody made.
    """
    from core.datastream_change import _affected_imports

    cursor = _RaisingCursor()
    answer = _affected_imports(cursor, datastream_id="ds_EXAMPLE")

    assert answer["state"] == "unavailable"
    assert answer["reason"]
    assert "retained_raw_imports" not in answer
    assert any(s.startswith("ROLLBACK TO SAVEPOINT") for s in cursor.statements), (
        "the failed read left the caller's transaction aborted, so the whole "
        "preparation would die of a count it did not need"
    )


def test_an_unreadable_template_registry_says_so_and_never_answers_none() -> None:
    """Same rule for the Template: unreadable is not absent."""
    from core.datastream_change import _template_version

    cursor = _RaisingCursor()
    answer = _template_version(
        cursor,
        datastream_id="ds_EXAMPLE",
        project_id="proj_EXAMPLE",
        config={"source_owner": {"managed_feed_template_ref": "fst_EXAMPLE"}},
    )

    assert answer["state"] == "unavailable"
    assert answer["template_id"] == "fst_EXAMPLE"
    assert "version" not in answer
    assert any(s.startswith("ROLLBACK TO SAVEPOINT") for s in cursor.statements)
