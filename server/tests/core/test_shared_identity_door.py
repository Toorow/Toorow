"""The shared identities' MCP door calls the console's own functions, and hides the secret.

`mcp-tool-surface.md`, amendment of 2026-09-05. Two tools under `governance`:
`propose_shared_identities` (a read of `mdm_common_keys.propose_shared_identities`)
and `publish_shared_identity` (a write through `shared_identity_pins.pin_shared_identity`,
`canonical_field_registry.declare_project_field`, `mdm_common_keys.create_common_key`).
What is under test is COMPOSITION and refusal, never the store: the pin's own
pg-gated test is `tests/integration/test_shared_identity_pins_pg.py`. The doubles
are those of `test_semantic_model_mcp_door.py`, reused rather than reinvented.
"""

from __future__ import annotations

import json

import pytest
from fastmcp.exceptions import ToolError

from tests.core.test_semantic_model_mcp_door import _tool, a_holder  # noqa: F401 -- fixture

PROJECT = "proj_x"


def _code(exc: ToolError) -> str:
    return json.loads(str(exc))["code"]


def test_both_tools_are_declared_under_governance_with_their_effects():
    _handler, read = _tool("propose_shared_identities")
    _handler, write = _tool("publish_shared_identity")
    assert (read["profile"], read["effect"], read["confirmation_mode"]) == ("governance", "read", "none")
    assert (write["profile"], write["effect"], write["confirmation_mode"]) == (
        "governance",
        "confirmed_write",
        "human",
    )


def test_the_read_returns_the_projects_proposal_as_the_console_computes_it(a_holder, monkeypatch):  # noqa: F811
    seen: dict = {}

    def _propose(conn, *, project_id):
        seen["project_id"] = project_id
        return {
            "state": "available",
            "proposals": [
                {"identity": "date", "role": "dimension", "to_pin": ["ds_b"]},
                {"identity": "views", "role": "metric", "to_pin": []},
            ],
            "datastreams_read": 2,
            "unmapped_datastreams": [],
            "truncated": False,
        }

    monkeypatch.setattr("core.mdm_common_keys.propose_shared_identities", _propose)
    handler, _ = _tool("propose_shared_identities")
    result = handler(project_id=PROJECT)
    assert seen == {"project_id": PROJECT}
    data = result.structured_content["data"]
    assert [p["identity"] for p in data["proposals"]] == ["date", "views"]
    assert data["proposals"][0]["to_pin"] == 1 and data["proposals"][0]["role"] == "dimension"
    assert "1 dimension(s), 1 measure(s); 1 pin(s) still to make" in result.content[0].text


def _a_large_project(identities: int, flows: int) -> dict:
    """What the console computes on a project the size of the reference one, and larger."""
    proposals = []
    for i in range(identities):
        carriers = [
            {
                "datastream_id": f"ds_{j:026d}",
                "datastream_name": f"Flow number {j} of the reference project",
                "mapping_version_id": f"dmap_{j:026d}",
                "column": f"column_{i}",
                "pinned_to": None,
                "pending_in_version": None,
                "pending_to": None,
                "role": "dimension" if i < 2 else "metric",
                "aggregation": None if i < 2 else "sum",
            }
            for j in range(flows)
        ]
        proposals.append(
            {
                "identity": f"identity_{i}",
                "kind": "column",
                "role": "dimension" if i < 2 else "metric",
                "aggregation": None if i < 2 else "sum",
                "also_known_as": [f"alias_{i}_{k}" for k in range(3)],
                "carrier_count": flows,
                "carriers": carriers,
                "already_pinned": [],
                "pending_publication": [],
                "to_pin": [c["datastream_id"] for c in carriers],
                "canonical_field_id": f"mdm_{i:026d}",
                "canonical_name": f"identity_{i}",
                "gesture": f"Pin `identity_{i}` to `identity_{i}` on the Mapping of {flows} flow(s), then declare a common key over it.",
            }
        )
    return {
        "state": "available",
        "proposals": proposals,
        "truncated": False,
        "unmapped_datastreams": [{"id": "ds_x", "name": "a flow without a mapping"}],
        "datastreams_read": flows,
    }


def test_the_read_stays_inside_the_model_channel_on_a_large_project(a_holder, monkeypatch):  # noqa: F811
    """Measured 2026-09-05 on the reference project: 9 identities x 9 flows made
    16 830 bytes of `proposals`, and the channel guard moved the whole list to the
    app channel -- the model could not read its own proposal. The list is compact
    and one identity travels in full, each inside the 4096-byte budget."""
    from core import model_channel

    monkeypatch.setattr("core.mdm_common_keys.propose_shared_identities", lambda conn, *, project_id: _a_large_project(14, 12))
    handler, _ = _tool("propose_shared_identities")

    listed = handler(project_id=PROJECT)
    visible, _app = model_channel.partition_envelope(listed.structured_content, tool_name="propose_shared_identities")
    model_channel.enforce_model_channel("propose_shared_identities", listed.content, visible)
    assert isinstance(visible["data"]["proposals"], list), "the list itself was withheld from the model"
    assert visible["data"]["listed"] == 12 and visible["data"]["total"] == 14 and visible["data"]["truncated"] is True
    assert "carriers_detail" not in visible["data"]["proposals"][0]

    one = handler(project_id=PROJECT, identity="identity_3")
    visible, _app = model_channel.partition_envelope(one.structured_content, tool_name="propose_shared_identities")
    model_channel.enforce_model_channel("propose_shared_identities", one.content, visible)
    detail = visible["data"]["proposal"]
    assert isinstance(detail["to_pin_carriers"], list) and len(detail["to_pin_carriers"]) == 12
    assert detail["to_pin_carriers"][0] == {"datastream_id": "ds_" + "0" * 26, "column": "column_3"}

    metrics = handler(project_id=PROJECT, role="metric", limit=5)
    assert metrics.structured_content["data"]["total"] == 12 and metrics.structured_content["data"]["listed"] == 5
    with pytest.raises(ToolError) as unknown:
        handler(project_id=PROJECT, identity="nobody")
    assert _code(unknown.value) == "not_found"


def test_a_pin_travels_through_the_mapping_tabs_path_and_the_secret_never_returns(a_holder, monkeypatch):  # noqa: F811
    seen: dict = {}

    def _pin(conn, *, project_id, canonical_field_id, carriers, actor, idempotency_key, reason=None):
        seen.update(
            project_id=project_id,
            canonical_field_id=canonical_field_id,
            carriers=carriers,
            idempotency_key=idempotency_key,
        )
        return {
            "canonical_field_id": canonical_field_id,
            "canonical_name": "date",
            "concept_kind": "dimension",
            "flows": [{"datastream_id": "ds_b", "column": "day", "outcome": "pinned", "overlay_execution_id": "dse_1"}],
            "pinned": 1,
            "already_pinned": 0,
            "refused": 0,
        }

    monkeypatch.setattr("core.shared_identity_pins.pin_shared_identity", _pin)
    handler, _ = _tool("publish_shared_identity")
    result = handler(
        project_id=PROJECT,
        intent={
            "action": "pin_identity",
            "canonical_field_id": "mdm_date",
            "carriers": [{"datastream_id": "ds_b", "column": "day"}],
        },
        idempotency_key="idem-1",
    )
    assert seen == {
        "project_id": PROJECT,
        "canonical_field_id": "mdm_date",
        "carriers": [{"datastream_id": "ds_b", "column": "day"}],
        "idempotency_key": "idem-1",
    }
    data = result.structured_content["data"]
    assert data["pinned"] == 1 and data["flows"][0]["outcome"] == "pinned"
    assert "secret" not in json.dumps(result.structured_content).lower()
    assert "1 flow(s) pinned" in result.content[0].text


def test_a_pin_may_declare_the_projects_own_field_first(a_holder, monkeypatch):  # noqa: F811
    declared: dict = {}
    pinned_to: dict = {}

    def _declare(conn, *, project_id, canonical_name, concept_kind, value_type, actor, **kwargs):
        declared.update(project_id=project_id, name=canonical_name, kind=concept_kind, value_type=value_type,
                        aggregation=kwargs.get("aggregation"))
        return {"id": "mdm_new", "canonical_name": canonical_name}

    def _pin(conn, *, project_id, canonical_field_id, carriers, actor, idempotency_key, reason=None):
        pinned_to["canonical_field_id"] = canonical_field_id
        return {"canonical_field_id": canonical_field_id, "canonical_name": "watch_minutes", "concept_kind": "metric",
                "flows": [], "pinned": 0, "already_pinned": 0, "refused": 0}

    monkeypatch.setattr("core.canonical_field_registry.declare_project_field", _declare)
    monkeypatch.setattr("core.shared_identity_pins.pin_shared_identity", _pin)
    handler, _ = _tool("publish_shared_identity")
    result = handler(
        project_id=PROJECT,
        intent={
            "action": "pin_identity",
            "canonical": {"name": "watch_minutes", "concept_kind": "metric", "value_type": "integer", "aggregation": "sum"},
            "carriers": [{"datastream_id": "ds_a", "column": "estimated_minutes_watched"}],
        },
        idempotency_key="idem-2",
    )
    assert declared == {"project_id": PROJECT, "name": "watch_minutes", "kind": "metric", "value_type": "integer",
                        "aggregation": "sum"}
    assert pinned_to == {"canonical_field_id": "mdm_new"}
    assert result.structured_content["data"]["declared_canonical_field"]["id"] == "mdm_new"


def test_a_common_key_is_declared_once_and_replayed_by_name(a_holder, monkeypatch):  # noqa: F811
    created: list = []
    keys: list = []

    def _create(conn, *, project_id, name, canonical_field_ids, actor, description=None):
        created.append((project_id, name, list(canonical_field_ids)))
        keys.append({"id": "mck_1", "name": name})
        return {"id": "mck_1", "name": name, "version_id": "mckv_1"}

    monkeypatch.setattr("core.mdm_common_keys.create_common_key", _create)
    monkeypatch.setattr("core.mdm_common_keys.list_common_keys", lambda conn, *, project_id: list(keys))
    monkeypatch.setattr(
        "core.mdm_common_keys.read_common_key",
        lambda conn, *, project_id, common_key_id: {"id": common_key_id, "name": "channel-day"},
    )
    handler, _ = _tool("publish_shared_identity")
    intent = {"action": "declare_common_key", "name": "channel-day", "canonical_field_ids": ["mdm_date", "mdm_channel"]}
    first = handler(project_id=PROJECT, intent=intent, idempotency_key="idem-3")
    second = handler(project_id=PROJECT, intent=intent, idempotency_key="idem-4")
    assert created == [(PROJECT, "channel-day", ["mdm_date", "mdm_channel"])]
    assert first.structured_content["data"]["replayed"] is False
    assert second.structured_content["data"]["replayed"] is True
    assert second.structured_content["data"]["common_key"]["id"] == "mck_1"


def test_a_flat_or_unkeyed_intent_is_refused_by_name(a_holder):  # noqa: F811
    handler, _ = _tool("publish_shared_identity")
    with pytest.raises(ToolError) as flat:
        handler(project_id=PROJECT, intent={"canonical_field_id": "mdm_date"}, idempotency_key="k")
    assert _code(flat.value) == "unknown_action"
    with pytest.raises(ToolError) as unkeyed:
        handler(project_id=PROJECT, intent={"action": "pin_identity"}, idempotency_key="")
    assert _code(unkeyed.value) == "missing_idempotency_key"
    with pytest.raises(ToolError) as nothing_to_pin_to:
        handler(project_id=PROJECT, intent={"action": "pin_identity", "carriers": []}, idempotency_key="k")
    assert _code(nothing_to_pin_to.value) == "missing_param"
