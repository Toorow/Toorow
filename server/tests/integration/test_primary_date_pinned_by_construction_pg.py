"""The primary date is the canonical date, by construction (governance.md, 2026-09-05).

Measured 2026-09-04 on the reference project: ten flows carry `date`, none of
the published mappings pinned it, and the crossing waited on ten identical human
gestures. At every mapping save the product now pins each `primary_date` field
to the canonical `date` dimension visible to the Project, unless the binding
already names a canonical field; the pin is part of the stored version and of
its content hash. Lives on the change-engine suite's scope, which mints a real
Project, Datastream and plan on the disposable base.
"""

from __future__ import annotations

import json

from core.datastream_field_mapping import _canonical_json, _sha256, save_field_mapping
from tests.integration.test_datastream_change_engine_pg import (  # noqa: F401 -- `conn` is the suite's fixture
    _id,
    _mapping_payload,
    _mint_field,
    _seed_scope,
    conn,
)


def _canonical_date(conn, project_id: str | None) -> str:
    field_id = _mint_field()  # a well-formed `mdm_<ULID>`, as the CHECK on the registry demands
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO app.mdm_canonical_fields "
            "(id,project_id,concept_kind,canonical_name,aggregation,value_type,created_by) "
            "VALUES (%s,%s,'dimension','date',NULL,'date','primary-date-harness')",
            (field_id, project_id),
        )
    conn.commit()
    return field_id


def _saved_payload(conn, version_id: str) -> tuple[dict, str]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT mapping_payload, content_hash FROM app.datastream_mapping_versions WHERE id = %s",
            (version_id,),
        )
        payload, content_hash = cur.fetchone()
    return (payload if isinstance(payload, dict) else json.loads(payload)), content_hash


def _payload_without_date_pin(scope) -> dict:
    payload = _mapping_payload(scope["plan_id"], scope["fields"], cost_alias="net_cost_pinned")
    for field in payload["fields"]:
        if field["field_id"] == "date":
            field["binding"]["mdm_target"] = None
    return payload


def test_a_primary_date_without_a_pin_is_pinned_to_the_projects_canonical_date(conn):
    scope = _seed_scope(conn)
    project_date = _canonical_date(conn, scope["project_id"])

    saved = save_field_mapping(
        datastream_id=scope["datastream_id"],
        project_id=scope["project_id"],
        mapping_payload=_payload_without_date_pin(scope),
        identity="owner@example.com",
        idempotency_key=f"primary-date-{_id('k')}",
        conn=conn,
    )

    payload, content_hash = _saved_payload(conn, saved["id"])
    date = next(f for f in payload["fields"] if f["field_id"] == "date")
    assert date["binding"]["mdm_target"] == project_date
    # The pin is part of the version: the stored hash is the hash of the stored payload.
    assert content_hash == _sha256(_canonical_json(payload))


def test_a_primary_date_already_pinned_is_left_as_the_person_pinned_it(conn):
    scope = _seed_scope(conn)
    _canonical_date(conn, scope["project_id"])
    payload_in = _mapping_payload(scope["plan_id"], scope["fields"], cost_alias="net_cost_kept")
    theirs = scope["fields"]["DATE"]  # the scope's own `media_date_*` dimension

    saved = save_field_mapping(
        datastream_id=scope["datastream_id"],
        project_id=scope["project_id"],
        mapping_payload=payload_in,
        identity="owner@example.com",
        idempotency_key=f"primary-date-kept-{_id('k')}",
        conn=conn,
    )

    payload, _ = _saved_payload(conn, saved["id"])
    date = next(f for f in payload["fields"] if f["field_id"] == "date")
    assert date["binding"]["mdm_target"] == theirs


def test_the_projects_own_date_wins_over_the_platforms(conn):
    scope = _seed_scope(conn)
    platform_date = _canonical_date(conn, None)
    project_date = _canonical_date(conn, scope["project_id"])
    try:
        saved = save_field_mapping(
            datastream_id=scope["datastream_id"],
            project_id=scope["project_id"],
            mapping_payload=_payload_without_date_pin(scope),
            identity="owner@example.com",
            idempotency_key=f"primary-date-scope-{_id('k')}",
            conn=conn,
        )
        payload, _ = _saved_payload(conn, saved["id"])
        date = next(f for f in payload["fields"] if f["field_id"] == "date")
        assert date["binding"]["mdm_target"] == project_date
    finally:
        # A platform row is shared by every test of the base: it does not outlive this one.
        with conn.cursor() as cur:
            cur.execute("DELETE FROM app.mdm_canonical_fields WHERE id = %s", (platform_date,))
        conn.commit()
