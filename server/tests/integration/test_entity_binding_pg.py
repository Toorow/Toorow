"""The key binding in the governed mapping, against a real database (Story 68.2).

WHY THIS FILE EXISTS. `tests/core/test_entity_designations.py` proves the RULES
off base -- the pure validation, the named groups. What it cannot see is the
wiring the story is actually about: the designation resolved at APPEND through
`save_field_mapping` (the `mdm_target` precedent: blocking + stable reason ->
`blocking_count > 0` -> non-executable), and the SAME validation re-run at both
publish doors after the type was archived between draft and publish.

Every write happens inside the caller's transaction (`commit=False`);
`live_postgres` rolls back. The file SKIPS without TEST_POSTGRES_DSN -- that is
the harness's normal posture, not a gap in what is proven here.
"""

from __future__ import annotations

import pytest

psycopg = pytest.importorskip("psycopg")

from core import governed_publication as gp  # noqa: E402
from core import master_data  # noqa: E402
from core import object_kind_registry as okr  # noqa: E402
from core.datastream_activation import (  # noqa: E402
    ActivationValidationError,
    _reverify_entity_designations,
)
from core.datastream_field_mapping import save_field_mapping  # noqa: E402
from ulid import ULID  # noqa: E402

from tests.integration.epic66_fixtures import (  # noqa: E402
    hash64,
    make_canonical_field,
    make_datastream,
    make_project,
    uid,
)


@pytest.fixture()
def world(live_postgres):
    """One project, one declared entity type, one datastream with a live plan."""
    conn = live_postgres
    org_id, project_id = make_project(conn, "Epic 68.2")
    declared = okr.declare_entity_type(
        conn,
        org_id=org_id,
        project_id=project_id,
        object_kind="video",
        canonical_key="video_id",
        display_name="Videos",
        actor="alice@example.com",
    )
    datastream_id = uid("ds")
    plan_id = uid("dpv")
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO app.datastreams "
            "(id, org_id, project_id, name, created_by, source_kind) "
            "VALUES (%s,%s,%s,%s,'tester','managed_feed')",
            (datastream_id, org_id, project_id, "Epic 68.2 DS"),
        )
        cur.execute(
            """
            INSERT INTO app.datastream_plan_versions
                (id, datastream_id, project_id, version_number, contract_version,
                 source_kind, writer_kind, destination_policy, normalized_payload,
                 content_hash, idempotency_key_hash, created_by)
            VALUES (%s,%s,%s,1,'plan.v1','connector_pull','toorow','managed_raw',
                    '{}'::jsonb,%s,%s,'tester')
            """,
            (plan_id, datastream_id, project_id, hash64(), hash64()),
        )
        cur.execute(
            "UPDATE app.datastreams SET current_plan_version_id = %s WHERE id = %s",
            (plan_id, datastream_id),
        )
    return {
        "conn": conn,
        "org_id": org_id,
        "project_id": project_id,
        "datastream_id": datastream_id,
        "registry_id": declared["registry"]["id"],
    }


def _payload(*, designation="__absent__", mdm_target=None, extra_field=None):
    binding = {
        "canonical_target": None,
        "mdm_target": mdm_target,
        "status": "confirmed",
        "blocking_reason": None,
        "confirmed_by": "alice@example.com",
        "confirmed_reason": "the key column",
    }
    if designation != "__absent__":
        binding["designates_object_kind"] = designation
    fields = [
        {
            "field_id": "video_id",
            "physical_type": "string",
            "profile": {
                "nullable": False,
                "unique": True,
                "cardinality_signal": "unique",
                "sample_values": [],
                "confidence": 0.95,
            },
            "suggestion": {
                "semantic_role": "dimension",
                "aggregation": "none",
                "non_additive": False,
                "currency": "unknown",
                "sensitivity": "none",
                "status": "suggested",
                "evidence": ["kind:dimension"],
            },
            "binding": binding,
        }
    ]
    if extra_field is not None:
        fields.append(extra_field)
    return {
        "mapping_contract_version": "1",
        "source_schema_hash": hash64(),
        "plan_version_id": "draft",
        "grain": ["video_id"],
        "fields": fields,
        "ambiguities": [],
    }


def _save(world, payload, *, advance_pointer=True):
    return save_field_mapping(
        datastream_id=world["datastream_id"],
        project_id=world["project_id"],
        mapping_payload=payload,
        identity="tester",
        idempotency_key=f"68-2-{ULID()}",
        conn=world["conn"],
        advance_pointer=advance_pointer,
        commit=False,
    )


def _binding(version, field_id="video_id"):
    for field in version["mapping_payload"]["fields"]:
        if field["field_id"] == field_id:
            return field["binding"]
    raise AssertionError(f"field {field_id} not stored")


def _archive(world):
    master_data.set_registry_state(
        world["conn"],
        project_id=world["project_id"],
        registry_id=world["registry_id"],
        lifecycle_state="disabled",
    )


# ---------------------------------------------------------------------------
# AC1 + AC2: the designation lives in the payload and is validated at append.
# ---------------------------------------------------------------------------


def test_a_valid_designation_appends_an_executable_version(world):
    version = _save(world, _payload(designation="video"))
    assert version["executable"] is True
    assert version["blocking_count"] == 0
    binding = _binding(version)
    assert binding["designates_object_kind"] == "video"
    assert binding["status"] == "confirmed"


def test_an_undeclared_type_blocks_the_binding_with_a_stable_reason(world):
    version = _save(world, _payload(designation="film"))
    assert version["executable"] is False
    assert version["blocking_count"] == 1
    binding = _binding(version)
    assert binding["status"] == "blocking"
    assert binding["blocking_reason"] == "entity_type_not_declared:film"
    # The designation itself is kept -- the version records what was declared,
    # and the reason says why it cannot execute. Nothing is invented.
    assert binding["designates_object_kind"] == "film"


def test_an_archived_type_blocks_the_binding(world):
    _archive(world)
    version = _save(world, _payload(designation="video"))
    assert version["executable"] is False
    assert _binding(version)["blocking_reason"] == "entity_type_archived:video"


def test_a_type_without_a_declared_key_blocks_the_binding(world):
    """A registry that predates the 68.1 door carries no canonical_key."""
    master_data.create_registry(
        world["conn"],
        org_id=world["org_id"],
        project_id=world["project_id"],
        object_kind="venue",
        label="Venues",
        actor="tester",
        version_scope="node",
    )
    version = _save(world, _payload(designation="venue"))
    assert version["executable"] is False
    assert _binding(version)["blocking_reason"] == "entity_type_key_not_declared:venue"


def test_the_save_path_writes_zero_registry_rows(world):
    """The Epic-13 boundary: the lookup is a lookup, never a write."""
    _save(world, _payload(designation="film"))
    with world["conn"].cursor() as cur:
        cur.execute(
            "SELECT COUNT(*) FROM app.master_data_registries WHERE project_id = %s",
            (world["project_id"],),
        )
        assert cur.fetchone()[0] == 1  # only the fixture's `video`


# ---------------------------------------------------------------------------
# AC5: composition with mdm_target, never a fork.
# ---------------------------------------------------------------------------


def _mdm_field_with_object_kind(world, object_kind):
    field_id = make_canonical_field(world["conn"], world["project_id"], "video key")
    with world["conn"].cursor() as cur:
        cur.execute(
            "UPDATE app.mdm_canonical_fields SET object_kind = %s WHERE id = %s",
            (object_kind, field_id),
        )
    return field_id


def test_a_designation_contradicting_the_mdm_target_is_refused(world):
    mdm_target = _mdm_field_with_object_kind(world, "video")
    okr.declare_entity_type(
        world["conn"],
        org_id=world["org_id"],
        project_id=world["project_id"],
        object_kind="restaurant",
        canonical_key="restaurant_id",
        display_name="Restaurants",
        actor="alice@example.com",
    )
    version = _save(world, _payload(designation="restaurant", mdm_target=mdm_target))
    assert version["executable"] is False
    binding = _binding(version)
    # The mdm_target resolves; the CONTRADICTION is what blocks.
    assert binding["blocking_reason"] == "entity_designation_contradicts_mdm_target:restaurant"


def test_a_designation_agreeing_with_the_mdm_target_composes(world):
    mdm_target = _mdm_field_with_object_kind(world, "video")
    version = _save(world, _payload(designation="video", mdm_target=mdm_target))
    assert version["executable"] is True
    assert version["blocking_count"] == 0


# ---------------------------------------------------------------------------
# AC3: both publish doors re-verify under the same FOR UPDATE.
# ---------------------------------------------------------------------------


def _draft_designated_version(world):
    """A live v1 without designation, and a DRAFT v2 designating `video`."""
    v1 = _save(world, _payload())
    v2 = _save(world, _payload(designation="video"), advance_pointer=False)
    assert v2["executable"] is True
    return v1, v2


def test_the_pointer_advance_is_refused_after_the_type_is_archived(world):
    v1, v2 = _draft_designated_version(world)
    _archive(world)
    with pytest.raises(gp.EntityDesignationRefused) as excinfo:
        gp._advance_pointer(
            world["conn"],
            datastream_id=world["datastream_id"],
            project_id=world["project_id"],
            to_version_id=v2["id"],
            expected_from_version_id=v1["id"],
            actor="tester",
            command=gp.COMMAND_PUBLISH,
        )
    assert excinfo.value.object_kind == "video"
    # The pointer never moved.
    with world["conn"].cursor() as cur:
        cur.execute(
            "SELECT current_mapping_version_id FROM app.datastreams WHERE id = %s",
            (world["datastream_id"],),
        )
        assert cur.fetchone()[0] == v1["id"]


def test_the_publish_mutation_reports_a_named_failure_never_an_uncertainty(world):
    v1, v2 = _draft_designated_version(world)
    _archive(world)
    result = gp._publish_mutation(
        world["conn"],
        "op_68_2",
        confirmation={
            "id": "pubc_68_2",
            "datastream_id": world["datastream_id"],
            "project_id": world["project_id"],
            "actor": "tester",
        },
        command=gp.COMMAND_PUBLISH,
        to_version_id=v2["id"],
        from_version_id=v1["id"],
    )
    assert result.outcome == "failed"
    assert result.result["reason"] == "entity_designation_invalid:video"
    assert "video" in result.result["detail"]


def test_the_activation_door_reverifies_and_refuses_after_archive(world):
    _v1, v2 = _draft_designated_version(world)
    _archive(world)
    with pytest.raises(ActivationValidationError, match="video"):
        _reverify_entity_designations(
            world["conn"],
            project_id=world["project_id"],
            datastream_id=world["datastream_id"],
            mapping_version_id=v2["id"],
        )


def test_a_version_without_designations_publishes_without_a_registry_read(world):
    v1 = _save(world, _payload())
    _archive(world)  # irrelevant here: nothing designates the archived type
    # v2 stays a DRAFT: appending it with `advance_pointer=True` would move the
    # pointer itself, and the door under test would then be asked to advance
    # from a version that is no longer current.
    v2 = _save(world, _payload(), advance_pointer=False)
    prior, rowcount = gp._advance_pointer(
        world["conn"],
        datastream_id=world["datastream_id"],
        project_id=world["project_id"],
        to_version_id=v2["id"],
        expected_from_version_id=v1["id"],
        actor="tester",
        command=gp.COMMAND_PUBLISH,
    )
    assert (prior, rowcount) == (v1["id"], 1)


# ---------------------------------------------------------------------------
# AC4: absent vs declared-untyped, through the store.
# ---------------------------------------------------------------------------


def test_absent_and_declared_untyped_survive_the_round_trip(world):
    untyped = {
        "field_id": "date",
        "physical_type": "date",
        "profile": {
            "nullable": False,
            "unique": False,
            "cardinality_signal": "high",
            "sample_values": [],
            "confidence": 0.95,
        },
        "suggestion": {
            "semantic_role": "primary_date",
            "aggregation": "none",
            "non_additive": False,
            "currency": "unknown",
            "sensitivity": "none",
            "status": "suggested",
            "evidence": ["kind:date"],
        },
        "binding": {
            "canonical_target": None,
            "mdm_target": None,
            "status": "confirmed",
            "blocking_reason": None,
            "confirmed_by": "alice@example.com",
            "confirmed_reason": "the date column",
            "designates_object_kind": None,
        },
    }
    version = _save(world, _payload(extra_field=untyped))
    stored = _binding(version, "video_id")
    assert "designates_object_kind" not in stored
    assert _binding(version, "date")["designates_object_kind"] is None

    groups = okr.entity_designation_groups(version["mapping_payload"])
    assert groups["designated"] == {}
    assert groups["declared_untyped"] == ["date"]
    # `video_id` profiles unique and declares nothing: the named group counts it.
    assert groups[okr.UNDECLARED_KEY_CANDIDATES] == ["video_id"]


def test_the_datastream_re_read_names_a_column_that_EXISTS(live_postgres):
    """`_datastreams_by_id` selected `capability_fingerprint` from `app.datastreams`.

    The column lives on the PLAN VERSION, not on the Datastream, so Postgres
    raised `UndefinedColumn` on the first row this function was ever asked for.
    Its caller guards `if not ids: return {}`, so the failure was reachable ONLY
    with an applicable proposal -- green on the empty set, red on any real one.
    Found 2026-08-23 by preparing every literal SQL string of `server/core`
    against a migrated schema.

    Asserting the KEY is present, not merely that no exception escaped: dropping
    the column from the SELECT would also stop the crash, and would silently pin
    every binding to a fingerprint of `None`.
    """
    from core import entity_bindings

    conn = live_postgres
    org_id, project_id = make_project(conn, "Binding re-read")
    datastream_id = make_datastream(conn, org_id, project_id, "ds-re-read")

    rows = entity_bindings._datastreams_by_id(
        conn, project_id=project_id, ids=[datastream_id]
    )

    assert datastream_id in rows, "the re-read found no row for a live Datastream"
    assert "capability_fingerprint" in rows[datastream_id], (
        "the fingerprint the binding pin is built from must be READ, not defaulted"
    )
