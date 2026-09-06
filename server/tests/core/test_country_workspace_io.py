from __future__ import annotations

from core.country_workspace import changed_bound_nodes, load_country_workspace


class _Conn:
    pass


def test_load_workspace_offers_code_defined_presets_without_mutating_the_database(monkeypatch):
    import core.country_registry as registry_module
    import core.master_data as master_data

    monkeypatch.setattr(registry_module, "fetch_country_registry", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(registry_module, "list_presets", lambda _conn: [])
    monkeypatch.setattr(
        registry_module,
        "vocabulary_entries",
        lambda: [{"code": "FR", "display_name": "France", "aliases": []}],
    )
    monkeypatch.setattr(master_data, "list_nodes", lambda *_args, **_kwargs: [])

    model = load_country_workspace(_Conn(), project_id="p1")

    assert model["state"] == "preset_required"
    assert {item["key"] for item in model["presets"]} >= {
        "france-and-territories",
        "emea-apac-amer",
    }
    assert model["vocabulary"][0]["code"] == "FR"


def test_changed_bound_nodes_names_only_meaning_changes():
    previous = [
        {"parent_node_id": "market_fr", "child_value": "FR"},
        {"parent_node_id": "region_emea", "child_node_id": "market_fr"},
    ]
    target = [
        {"parent_node_id": "market_fr", "child_value": "FR"},
        {"parent_node_id": "market_fr", "child_value": "RE"},
        {"parent_node_id": "region_emea", "child_node_id": "market_fr"},
    ]
    used_by = [
        {
            "node_id": "market_fr",
            "consumer_kind": "saved_report",
            "consumer_id": "report_1",
            "consumer_label": "Board report",
        },
        {
            "node_id": "region_emea",
            "consumer_kind": "context_path",
            "consumer_id": "path_1",
            "consumer_label": "EMEA brief",
        },
    ]

    impacts = changed_bound_nodes(previous, target, used_by)

    assert impacts == [
        {
            "node_id": "market_fr",
            "previous_country_codes": ["FR"],
            "target_country_codes": ["FR", "RE"],
            "consumers": [used_by[0]],
        },
        {
            "node_id": "region_emea",
            "previous_country_codes": ["FR"],
            "target_country_codes": ["FR", "RE"],
            "consumers": [used_by[1]],
        },
    ]


def test_changed_bound_nodes_reports_a_removed_bound_market():
    impacts = changed_bound_nodes(
        [{"parent_node_id": "market_fr", "child_value": "FR"}],
        [],
        [
            {
                "node_id": "market_fr",
                "consumer_kind": "objective",
                "consumer_id": "objective_1",
                "consumer_label": "France objective",
            }
        ],
    )

    assert impacts[0]["target_country_codes"] == []
    assert impacts[0]["consumers"][0]["consumer_id"] == "objective_1"

def test_workspace_uses_versioned_labels_and_keeps_empty_regions_visible():
    from core.country_workspace import compose_country_workspace

    model = compose_country_workspace(
        registry={"id": "registry", "current_version_id": "version"},
        presets=[],
        nodes=[
            {"id": "market", "label": "Historical market", "node_kind": "market"},
            {"id": "region", "label": "Historical region", "node_kind": "region"},
        ],
        versions=[
            {
                "id": "version",
                "status": "current",
                "payload": {"node_labels": {"market": "France", "region": "EMEA"}},
            }
        ],
        memberships=[
            {"version_id": "version", "parent_node_id": "market", "child_value": "FR"}
        ],
        vocabulary=[{"code": "FR", "display_name": "France"}],
        used_by=[],
    )

    assert {node["id"]: node["label"] for node in model["nodes"]} == {
        "market": "France",
        "region": "EMEA",
    }
