"""A binding-only mapping change is an overlay, never a re-pull (governance.md, 2026-09-05).

Measured 2026-09-05 on the reference project: nine flows pinned `date` and
`channel_id`, every candidate re-pulled YouTube Analytics and died on the
provider's 403 -- a pin that adds no row to the scan was held hostage by the
source. Confirming a change that moves only bindings now publishes an overlay:
a new execution that pulls nothing, the prior publication's hash and count, one
output version per prior output on the SAME relation, and the pointers move.
A change that touches a column stays a candidate run. Lives on the change-engine
suite's scope, which mints a real Project, Datastream and plan on the disposable
base.
"""

from __future__ import annotations

from ulid import ULID

from core.datastream_change import confirm_change, prepare_change
from tests.integration.test_datastream_change_engine_pg import (  # noqa: F401 -- `conn` is the suite's fixture
    _id,
    _mapping_payload,
    _mint_field,
    _seed_scope,
    conn,
)

RELATION = "raw_overlay_harness"


def _publish(conn, scope: dict) -> tuple[str, str]:
    """A published execution with one full-grain output, like the product leaves after a first publication."""
    with conn.cursor() as cur:
        cur.execute("SELECT current_mapping_version_id FROM app.datastreams WHERE id=%s", (scope["datastream_id"],))
        mapping_id = cur.fetchone()[0]
        execution_id, output_id, version_id = f"dse_{ULID()}", f"dso_{ULID()}", f"dsov_{ULID()}"
        cur.execute(
            """INSERT INTO app.datastream_executions
               (id, datastream_id, project_id, plan_version_id, mapping_version_id, projection_plan_ref,
                state, content_hash, row_count, created_by)
               VALUES (%s,%s,%s,%s,%s,'{}'::jsonb,'published',repeat('c',64),42,'tester')""",
            (execution_id, scope["datastream_id"], scope["project_id"], scope["plan_id"], mapping_id),
        )
        cur.execute(
            """INSERT INTO app.datastream_outputs (id, org_id, project_id, datastream_id, output_kind, stable_name, created_by)
               VALUES (%s,%s,%s,%s,'full_grain',%s,'tester')""",
            (output_id, scope["org_id"], scope["project_id"], scope["datastream_id"], RELATION),
        )
        cur.execute(
            """INSERT INTO app.datastream_output_versions
               (id, output_id, org_id, project_id, datastream_id, execution_id, plan_version_id,
                mapping_version_id, relation_ref, schema_hash, grain_evidence, evidence, created_by)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,repeat('5',64),'{}'::jsonb,'{}'::jsonb,'tester')""",
            (version_id, output_id, scope["org_id"], scope["project_id"], scope["datastream_id"],
             execution_id, scope["plan_id"], mapping_id, RELATION),
        )
        cur.execute(
            "UPDATE app.datastreams SET current_published_execution_id=%s WHERE id=%s",
            (execution_id, scope["datastream_id"]),
        )
    conn.commit()
    return execution_id, mapping_id


def _canonical_dimension(conn, project_id: str, name: str) -> str:
    field_id = _mint_field()
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO app.mdm_canonical_fields (id,project_id,concept_kind,canonical_name,aggregation,value_type,created_by) "
            "VALUES (%s,%s,'dimension',%s,NULL,'date','overlay-harness')",
            (field_id, project_id, name),
        )
    conn.commit()
    return field_id


def _prepare_and_confirm(conn, scope: dict, payload: dict) -> dict:
    prepared = prepare_change(
        conn, project_id=scope["project_id"], datastream_id=scope["datastream_id"], kind="mapping",
        proposed_payload=payload, actor="owner@example.com", idempotency_key=_id("chg_"),
    )
    conn.commit()
    result = confirm_change(
        conn, project_id=scope["project_id"], datastream_id=scope["datastream_id"],
        preparation_id=prepared["preparation_id"], confirmation_secret=prepared["confirmation_secret"],
        actor="owner@example.com",
    )
    conn.commit()
    return result


def test_a_change_that_moves_only_a_binding_is_published_as_an_overlay(conn):
    scope = _seed_scope(conn)
    prior_execution, prior_mapping = _publish(conn, scope)
    other_day = _canonical_dimension(conn, scope["project_id"], f"day_{scope['project_id'][-8:]}")
    # Same columns, same grain, same aliases: only where `date` points changes.
    payload = _mapping_payload(scope["plan_id"], {"DATE": other_day, "COST": scope["fields"]["COST"]}, cost_alias="net_cost")

    result = _prepare_and_confirm(conn, scope, payload)

    assert result["candidate_execution_id"] is None
    assert result["overlay_execution_id"].startswith("dse_")
    assert result["overlay_of"] == prior_execution
    assert "nothing re-pulled" in result["no_candidate_reason"]
    with conn.cursor() as cur:
        cur.execute(
            "SELECT current_published_execution_id, current_mapping_version_id FROM app.datastreams WHERE id=%s",
            (scope["datastream_id"],),
        )
        current_execution, current_mapping = cur.fetchone()
        assert current_execution == result["overlay_execution_id"]
        assert current_mapping == result["mapping_version_id"] != prior_mapping
        cur.execute(
            "SELECT state, content_hash, row_count, adapter_ref FROM app.datastream_executions WHERE id=%s",
            (current_execution,),
        )
        assert cur.fetchone() == ("published", "c" * 64, 42, "mapping_overlay")
        cur.execute(
            "SELECT relation_ref, mapping_version_id FROM app.datastream_output_versions WHERE execution_id=%s",
            (current_execution,),
        )
        assert cur.fetchall() == [(RELATION, current_mapping)]
        cur.execute(
            "SELECT prior_execution_id, row_count FROM app.datastream_publication_log WHERE execution_id=%s",
            (current_execution,),
        )
        assert cur.fetchone() == (prior_execution, 42)


def test_a_change_that_touches_a_column_still_runs_as_a_candidate(conn):
    scope = _seed_scope(conn)
    prior_execution, _prior_mapping = _publish(conn, scope)
    # The cost column is renamed: a landed column moves, so the rows must be re-read.
    payload = _mapping_payload(scope["plan_id"], scope["fields"], cost_alias="net_cost_renamed")

    result = _prepare_and_confirm(conn, scope, payload)

    assert "overlay_execution_id" not in result
    with conn.cursor() as cur:
        cur.execute("SELECT current_published_execution_id FROM app.datastreams WHERE id=%s", (scope["datastream_id"],))
        assert cur.fetchone()[0] == prior_execution  # the pointer did not move: a candidate decides later
