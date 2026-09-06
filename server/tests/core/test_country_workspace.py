from __future__ import annotations

import pytest
from core.country_workspace import (
    CountryWorkspaceRefused,
    compose_country_workspace,
    normalize_hierarchy_edit,
)


def _registry():
    return {
        "id": "mdr_country",
        "object_kind": "country",
        "label": "Country Registry",
        "current_version_id": "mdv_current",
        "pending_version_id": "mdv_draft",
        "last_known_good_version_id": None,
        "lifecycle_state": "active",
    }


def _nodes():
    return [
        {"id": "market_fr", "label": "France", "node_kind": "market"},
        {"id": "region_emea", "label": "EMEA", "node_kind": "region"},
        {"id": "row", "label": "Rest of World", "node_kind": "rest_of_world"},
    ]


def test_workspace_offers_qualified_presets_before_a_registry_exists():
    model = compose_country_workspace(
        registry=None,
        presets=[
            {
                "id": "preset_fr",
                "preset_key": "france-and-territories",
                "label": "France and selectable territories",
                "description": "A starting point.",
                "classification": "toorow_curated",
                "source_authority": "toorow",
                "source_reference": "https://example.test",
                "preset_version": "1",
                "payload": {"nodes": [], "members": []},
                "content_hash": "a" * 64,
            }
        ],
        nodes=[],
        versions=[],
        memberships=[],
        vocabulary=[{"code": "FR", "display_name": "France", "aliases": ["French Republic"]}],
        used_by=[],
    )

    assert model["state"] == "preset_required"
    assert model["presets"][0]["id"] == "preset_fr"
    assert model["presets"][0]["classification"] == "toorow_curated"
    assert model["presets"][0]["source_authority"] == "toorow"
    assert model["vocabulary"][0]["code"] == "FR"
    assert model["draft"] is None


def test_workspace_projects_an_editable_nested_draft_and_named_used_by():
    versions = [
        {
            "id": "mdv_draft",
            "version_number": 2,
            "status": "draft",
            "content_hash": "b" * 64,
            "payload": {},
        },
        {
            "id": "mdv_current",
            "version_number": 1,
            "status": "current",
            "content_hash": "a" * 64,
            "payload": {},
        },
    ]
    model = compose_country_workspace(
        registry=_registry(),
        presets=[],
        nodes=_nodes(),
        versions=versions,
        memberships=[
            {"version_id": "mdv_draft", "parent_node_id": "market_fr", "child_value": "FR"},
            {
                "version_id": "mdv_draft",
                "parent_node_id": "region_emea",
                "child_node_id": "market_fr",
            },
        ],
        vocabulary=[{"code": "FR", "display_name": "France", "aliases": []}],
        used_by=[
            {
                "node_id": "market_fr",
                "consumer_kind": "saved_report",
                "consumer_id": "report_1",
                "consumer_label": "Board report",
            }
        ],
    )

    assert model["state"] == "draft"
    assert model["draft"]["id"] == "mdv_draft"
    assert model["draft"]["memberships"][1]["child_node_id"] == "market_fr"
    assert model["used_by"][0]["consumer_label"] == "Board report"


def test_hierarchy_edit_resolves_new_node_refs_and_preserves_stable_existing_ids():
    edit = normalize_hierarchy_edit(
        nodes=_nodes(),
        requested_nodes=[
            {
                "ref": "market_fr",
                "id": "market_fr",
                "kind": "market",
                "label": "France + territories",
            },
            {"ref": "new:benelux", "kind": "market", "label": "Benelux"},
            {"ref": "region_emea", "id": "region_emea", "kind": "region", "label": "EMEA"},
        ],
        requested_memberships=[
            {"parent_ref": "market_fr", "child_value": "FR"},
            {"parent_ref": "new:benelux", "child_value": "BE"},
            {"parent_ref": "region_emea", "child_ref": "new:benelux"},
        ],
        canonical_values={"FR", "BE"},
        minted_ids={"new:benelux": "market_be"},
    )

    assert edit.nodes[0].id == "market_fr"
    assert edit.nodes[0].label == "France + territories"
    assert edit.nodes[1].id == "market_be"
    assert edit.memberships[2].child_node_id == "market_be"


def test_hierarchy_edit_rejects_duplicate_country_assignment():
    with pytest.raises(CountryWorkspaceRefused, match="assigned more than once"):
        normalize_hierarchy_edit(
            nodes=_nodes(),
            requested_nodes=[
                {"ref": "market_fr", "id": "market_fr", "kind": "market", "label": "France"},
                {"ref": "new:other", "kind": "market", "label": "Other market"},
            ],
            requested_memberships=[
                {"parent_ref": "market_fr", "child_value": "FR"},
                {"parent_ref": "new:other", "child_value": "FR"},
            ],
            canonical_values={"FR"},
            minted_ids={"new:other": "market_other"},
        )


def test_hierarchy_edit_rejects_unknown_country_and_cycles():
    with pytest.raises(CountryWorkspaceRefused, match="unknown country code"):
        normalize_hierarchy_edit(
            nodes=_nodes(),
            requested_nodes=[
                {"ref": "market_fr", "id": "market_fr", "kind": "market", "label": "France"}
            ],
            requested_memberships=[{"parent_ref": "market_fr", "child_value": "ZZ"}],
            canonical_values={"FR"},
            minted_ids={},
        )

    with pytest.raises(CountryWorkspaceRefused, match="node edges must be Region to Market"):
        normalize_hierarchy_edit(
            nodes=_nodes(),
            requested_nodes=[
                {"ref": "a", "id": "market_fr", "kind": "market", "label": "France"},
                {"ref": "b", "id": "region_emea", "kind": "region", "label": "EMEA"},
            ],
            requested_memberships=[
                {"parent_ref": "a", "child_value": "FR"},
                {"parent_ref": "a", "child_ref": "b"},
            ],
            canonical_values={"FR"},
            minted_ids={},
        )


def test_hierarchy_edit_rejects_empty_markets_and_region_country_edges():
    with pytest.raises(CountryWorkspaceRefused, match="at least one canonical country"):
        normalize_hierarchy_edit(
            nodes=_nodes(),
            requested_nodes=[
                {"ref": "market_fr", "id": "market_fr", "kind": "market", "label": "France"}
            ],
            requested_memberships=[],
            canonical_values={"FR"},
            minted_ids={},
        )

    with pytest.raises(CountryWorkspaceRefused, match="belong directly to a Market"):
        normalize_hierarchy_edit(
            nodes=_nodes(),
            requested_nodes=[
                {"ref": "region_emea", "id": "region_emea", "kind": "region", "label": "EMEA"}
            ],
            requested_memberships=[
                {"parent_ref": "region_emea", "child_value": "FR"}
            ],
            canonical_values={"FR"},
            minted_ids={},
        )
