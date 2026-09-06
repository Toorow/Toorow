from __future__ import annotations

from types import SimpleNamespace

import pytest
from core.country_workspace import CountryWorkspaceRefused
from core.country_workspace_commands import run_country_workspace_command


class _Conn:
    pass


def _execute_operation(conn, spec, *, mutation):
    result = mutation(conn, "op_1")
    return SimpleNamespace(
        operation_id="op_1",
        outcome=result.outcome,
        result=result.result,
        replayed=False,
    )


def test_apply_preset_is_a_single_idempotent_operation(monkeypatch):
    import core.country_registry as country_registry
    import core.master_data as master_data
    import core.operations as operations

    calls = []
    monkeypatch.setattr(operations, "execute_operation", _execute_operation)
    monkeypatch.setattr(
        country_registry,
        "seed_country_presets",
        lambda _conn: [
            {
                "id": "preset_fr_v1",
                "preset_key": "france-and-territories",
                "content_hash": "a" * 64,
            }
        ],
    )
    monkeypatch.setattr(
        country_registry,
        "ensure_country_registry",
        lambda *_args, **_kwargs: {"id": "mdr_country", "current_version_id": None},
    )
    monkeypatch.setattr(master_data, "list_versions", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(
        country_registry,
        "materialize_preset",
        lambda *_args, **kwargs: calls.append(kwargs)
        or {
            "id": "mdv_draft",
            "version_number": 1,
            "status": "draft",
            "content_hash": "b" * 64,
        },
    )

    result = run_country_workspace_command(
        _Conn(),
        project_id="p1",
        org_id="o1",
        actor="person_1",
        idempotency_key="country-preset-1",
        action="apply_preset",
        payload={
            "preset_id": "france-and-territories",
            "selected_optional_values": ["RE"],
        },
    )

    assert result["operation_id"] == "op_1"
    assert result["result"]["draft_version_id"] == "mdv_draft"
    assert calls[0]["preset_version_id"] == "preset_fr_v1"
    assert calls[0]["selected_optional_values"] == ["RE"]


def test_save_hierarchy_refuses_a_bound_meaning_change_before_writing(monkeypatch):
    import core.master_data as master_data
    import core.operations as operations

    monkeypatch.setattr(operations, "execute_operation", _execute_operation)
    monkeypatch.setattr(
        master_data,
        "require_registry",
        lambda *_args, **_kwargs: {
            "id": "mdr_country",
            "current_version_id": "mdv_current",
        },
    )
    monkeypatch.setattr(
        master_data,
        "require_version",
        lambda *_args, **_kwargs: {
            "id": "mdv_draft",
            "registry_id": "mdr_country",
            "vocabulary_version_id": "mdvoc_1",
            "status": "draft",
            "content_hash": "a" * 64,
            "payload": {},
        },
    )
    monkeypatch.setattr(
        master_data,
        "fetch_vocabulary_version",
        lambda *_args, **_kwargs: {"entries": [{"code": "FR"}, {"code": "RE"}]},
    )
    monkeypatch.setattr(
        master_data,
        "list_nodes",
        lambda *_args, **_kwargs: [
            {"id": "market_fr", "label": "France", "node_kind": "market"},
        ],
    )
    monkeypatch.setattr(
        master_data,
        "fetch_memberships",
        lambda _conn, *, version_id, **_kwargs: (
            (master_data.Membership(parent_node_id="market_fr", child_value="FR"),)
            if version_id == "mdv_current"
            else ()
        ),
    )
    monkeypatch.setattr(
        master_data,
        "fetch_used_by",
        lambda *_args, **_kwargs: (
            master_data.UsedByReference(
                node_id="market_fr",
                consumer_kind="saved_report",
                consumer_id="report_1",
                consumer_label="Board report",
            ),
        ),
    )
    writes = []
    monkeypatch.setattr(
        master_data, "create_node", lambda *_args, **_kwargs: writes.append("create")
    )
    monkeypatch.setattr(
        master_data, "rename_node", lambda *_args, **_kwargs: writes.append("rename")
    )
    monkeypatch.setattr(
        master_data,
        "replace_draft_memberships",
        lambda *_args, **_kwargs: writes.append("replace"),
    )

    with pytest.raises(CountryWorkspaceRefused) as caught:
        run_country_workspace_command(
            _Conn(),
            project_id="p1",
            org_id="o1",
            actor="person_1",
            idempotency_key="country-save-1",
            action="save_hierarchy",
            payload={
                "version_id": "mdv_draft",
                "expected_content_hash": "a" * 64,
                "nodes": [
                    {
                        "ref": "market_fr",
                        "id": "market_fr",
                        "kind": "market",
                        "label": "France",
                    }
                ],
                "memberships": [
                    {"parent_ref": "market_fr", "child_value": "FR"},
                    {"parent_ref": "market_fr", "child_value": "RE"},
                ],
            },
        )

    assert caught.value.code == "country_workspace_impact_acknowledgement_required"
    assert caught.value.impacts[0]["consumers"][0]["consumer_label"] == "Board report"
    assert writes == []


def test_start_draft_clones_the_current_version_as_a_new_editable_revision(monkeypatch):
    import core.master_data as master_data
    import core.operations as operations

    monkeypatch.setattr(operations, "execute_operation", _execute_operation)
    current = {
        "id": "mdv_current",
        "registry_id": "mdr_country",
        "version_number": 2,
        "status": "current",
        "content_hash": "c" * 64,
        "vocabulary_version_id": "mdvoc_1",
        "origin_preset_version_id": "preset_fr_v1",
        "payload": {"rest_of_world_node_id": "row"},
    }
    memberships = (master_data.Membership(parent_node_id="market_fr", child_value="FR"),)
    monkeypatch.setattr(
        master_data,
        "require_registry",
        lambda *_args, **_kwargs: {
            "id": "mdr_country",
            "current_version_id": "mdv_current",
        },
    )
    monkeypatch.setattr(master_data, "list_versions", lambda *_args, **_kwargs: [current])
    monkeypatch.setattr(master_data, "require_version", lambda *_args, **_kwargs: current)
    monkeypatch.setattr(master_data, "fetch_memberships", lambda *_args, **_kwargs: memberships)
    created = []
    monkeypatch.setattr(
        master_data,
        "create_draft_version",
        lambda *_args, **kwargs: created.append(kwargs)
        or {
            "id": "mdv_draft",
            "version_number": 3,
            "status": "draft",
            "content_hash": "d" * 64,
        },
    )

    result = run_country_workspace_command(
        _Conn(),
        project_id="p1",
        org_id="o1",
        actor="person_1",
        idempotency_key="country-start-draft-1",
        action="start_draft",
        payload={
            "current_version_id": "mdv_current",
            "expected_content_hash": "c" * 64,
        },
    )

    assert result["result"]["draft_version"]["id"] == "mdv_draft"
    assert created[0]["vocabulary_version_id"] == "mdvoc_1"
    assert created[0]["content"].memberships == memberships
    assert created[0]["content"].payload == {"rest_of_world_node_id": "row"}


def test_publish_rechecks_the_reviewed_content_hash(monkeypatch):
    import core.entry_confirmations as confirmations
    import core.master_data as master_data
    import core.operations as operations

    monkeypatch.setattr(operations, "execute_operation", _execute_operation)
    monkeypatch.setattr(
        confirmations,
        "consume_entry_confirmation",
        lambda *_args, **_kwargs: SimpleNamespace(confirmation_id="econf_1"),
    )
    monkeypatch.setattr(
        master_data,
        "require_registry",
        lambda *_args, **_kwargs: {"id": "mdr_country"},
    )
    monkeypatch.setattr(
        master_data,
        "require_version",
        lambda *_args, **_kwargs: {
            "id": "mdv_draft",
            "registry_id": "mdr_country",
            "status": "draft",
            "content_hash": "new-hash",
        },
    )
    published = []
    monkeypatch.setattr(
        master_data, "mark_candidate", lambda *_args, **_kwargs: published.append("candidate")
    )
    monkeypatch.setattr(
        master_data, "publish_version", lambda *_args, **_kwargs: published.append("published")
    )

    with pytest.raises(CountryWorkspaceRefused, match="changed since it was reviewed"):
        run_country_workspace_command(
            _Conn(),
            project_id="p1",
            org_id="o1",
            actor="person_1",
            idempotency_key="country-publish-1",
            action="publish",
            payload={
                "version_id": "mdv_draft",
                "expected_content_hash": "old-hash",
                "confirmation_id": "econf_1",
                "confirmation_secret": "secret_1",
            },
        )

    assert published == []

def test_prepare_publish_confirmation_is_bound_to_country_registry_and_hash(monkeypatch):
    from datetime import UTC, datetime

    import core.entry_confirmations as confirmations
    import core.master_data as master_data
    from core.country_workspace_commands import prepare_country_publish_confirmation

    monkeypatch.setattr(
        master_data,
        "require_registry",
        lambda *_args, **_kwargs: {"id": "mdr_country"},
    )
    monkeypatch.setattr(
        master_data,
        "require_version",
        lambda *_args, **_kwargs: {
            "id": "mdv_draft",
            "registry_id": "mdr_country",
            "status": "draft",
            "content_hash": "a" * 64,
        },
    )
    issued = []
    monkeypatch.setattr(
        confirmations,
        "issue_entry_confirmation",
        lambda *_args, **kwargs: issued.append(kwargs)
        or SimpleNamespace(
            confirmation_id="econf_1",
            confirmation_secret="single-use-secret",
            expires_at=datetime(2026, 8, 2, tzinfo=UTC),
        ),
    )

    result = prepare_country_publish_confirmation(
        _Conn(),
        project_id="p1",
        org_id="o1",
        actor="person_1",
        idempotency_key="country-publish-1",
        payload={"version_id": "mdv_draft", "expected_content_hash": "a" * 64},
    )

    assert result["confirmation_secret"] == "single-use-secret"
    assert result["confirmation_secret_single_return"] is True
    assert issued[0]["request_payload"] == {
        "project_id": "p1",
        "org_id": "o1",
        "version_id": "mdv_draft",
        "expected_content_hash": "a" * 64,
    }


def test_publish_refuses_a_version_owned_by_another_registry(monkeypatch):
    import core.entry_confirmations as confirmations
    import core.master_data as master_data

    monkeypatch.setattr(
        confirmations,
        "consume_entry_confirmation",
        lambda *_args, **_kwargs: SimpleNamespace(confirmation_id="econf_1"),
    )
    monkeypatch.setattr(
        master_data,
        "require_registry",
        lambda *_args, **_kwargs: {"id": "mdr_country"},
    )
    monkeypatch.setattr(
        master_data,
        "require_version",
        lambda *_args, **_kwargs: {
            "id": "mdv_other",
            "registry_id": "mdr_other",
            "status": "draft",
            "content_hash": "a" * 64,
        },
    )
    import core.operations as operations

    monkeypatch.setattr(operations, "execute_operation", _execute_operation)

    with pytest.raises(CountryWorkspaceRefused, match="does not belong"):
        run_country_workspace_command(
            _Conn(),
            project_id="p1",
            org_id="o1",
            actor="person_1",
            idempotency_key="country-publish-other",
            action="publish",
            payload={
                "version_id": "mdv_other",
                "expected_content_hash": "a" * 64,
                "confirmation_id": "econf_1",
                "confirmation_secret": "secret",
            },
        )
