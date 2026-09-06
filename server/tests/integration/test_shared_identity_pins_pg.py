"""A pin through the shared-identity path IS the Mapping tab's overlay (governance.md, 2026-09-05).

`shared_identity_pins.pin_shared_identity` prepares and confirms the same governed
mapping change the Mapping tab plays; a pin moves only a binding, so it is published
as an overlay that re-pulls nothing and moves the pointers. Lives on the overlay
suite's scope, which mints a real Project, Datastream, plan and first publication
on the disposable base.
"""

from __future__ import annotations

import json

from core.shared_identity_pins import pin_shared_identity
from tests.integration.test_datastream_change_engine_pg import (  # noqa: F401 -- `conn` is the suite's fixture
    _mint_field,
    _seed_scope,
    conn,
)
from tests.integration.test_mapping_overlay_pg import _canonical_dimension, _publish

ACTOR = "owner@example.com"


def _canonical_metric(conn, project_id: str, name: str) -> str:
    field_id = _mint_field()
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO app.mdm_canonical_fields (id,project_id,concept_kind,canonical_name,aggregation,value_type,created_by) "
            "VALUES (%s,%s,'metric',%s,'sum','integer','pin-harness')",
            (field_id, project_id, name),
        )
    conn.commit()
    return field_id


def _mapping_versions(conn, datastream_id: str) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM app.datastream_mapping_versions WHERE datastream_id=%s", (datastream_id,))
        return int(cur.fetchone()[0])


def test_a_pin_is_published_as_the_mapping_tabs_overlay_and_replays_as_already_pinned(conn):
    scope = _seed_scope(conn)
    prior_execution, prior_mapping = _publish(conn, scope)
    other_day = _canonical_dimension(conn, scope["project_id"], f"day_{scope['project_id'][-8:]}")
    carriers = [{"datastream_id": scope["datastream_id"], "column": "date"}]

    result = pin_shared_identity(
        conn, project_id=scope["project_id"], canonical_field_id=other_day, carriers=carriers,
        actor=ACTOR, idempotency_key=f"pin-{scope['datastream_id']}",
    )
    conn.commit()

    assert (result["pinned"], result["already_pinned"], result["refused"]) == (1, 0, 0)
    flow = result["flows"][0]
    assert flow["outcome"] == "pinned"
    assert flow["overlay_execution_id"].startswith("dse_") and flow["overlay_of"] == prior_execution
    assert flow["candidate_execution_id"] is None and "nothing re-pulled" in flow["no_candidate_reason"]
    assert "secret" not in json.dumps(result).lower()
    with conn.cursor() as cur:
        cur.execute(
            "SELECT current_published_execution_id, current_mapping_version_id FROM app.datastreams WHERE id=%s",
            (scope["datastream_id"],),
        )
        current_execution, current_mapping = cur.fetchone()
        assert current_execution == flow["overlay_execution_id"]
        assert current_mapping == flow["mapping_version_id"] != prior_mapping
        cur.execute("SELECT mapping_payload FROM app.datastream_mapping_versions WHERE id=%s", (current_mapping,))
        fields = {f["field_id"]: f for f in cur.fetchone()[0]["fields"]}
        assert fields["date"]["binding"]["mdm_target"] == other_day
        assert fields["date"]["binding"]["status"] == "confirmed"

    again = pin_shared_identity(
        conn, project_id=scope["project_id"], canonical_field_id=other_day, carriers=carriers,
        actor=ACTOR, idempotency_key=f"pin-again-{scope['datastream_id']}",
    )
    conn.commit()
    assert again["flows"][0]["outcome"] == "already_pinned"
    assert again["flows"][0]["mapping_version_id"] == current_mapping


def test_a_dimension_column_is_never_pinned_to_a_metric_and_an_unknown_column_is_named(conn):
    scope = _seed_scope(conn)
    _publish(conn, scope)
    metric = _canonical_metric(conn, scope["project_id"], f"count_{scope['project_id'][-8:]}")
    versions_before = _mapping_versions(conn, scope["datastream_id"])

    result = pin_shared_identity(
        conn, project_id=scope["project_id"], canonical_field_id=metric,
        carriers=[
            {"datastream_id": scope["datastream_id"], "column": "date"},
            {"datastream_id": scope["datastream_id"], "column": "nope"},
        ],
        actor=ACTOR, idempotency_key=f"pin-bad-{scope['datastream_id']}",
    )
    conn.commit()

    assert (result["pinned"], result["refused"]) == (0, 2)
    by_column = {f["column"]: f for f in result["flows"]}
    assert by_column["date"]["code"] == "role_mismatch" and "dimension" in by_column["date"]["message"]
    assert by_column["nope"]["code"] == "unknown_column" and "`nope`" in by_column["nope"]["message"]
    assert _mapping_versions(conn, scope["datastream_id"]) == versions_before  # a refusal writes nothing
