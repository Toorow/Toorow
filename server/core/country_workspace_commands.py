"""Audited commands for the Country Governance workbench."""

from __future__ import annotations

from typing import Any, Mapping

from core.country_workspace import (
    CountryWorkspaceRefused,
    changed_bound_nodes,
    normalize_hierarchy_edit,
)


class CountryWorkspaceImpactRefused(CountryWorkspaceRefused):
    code = "country_workspace_impact_acknowledgement_required"

    def __init__(self, impacts: list[dict[str, Any]]):
        super().__init__(
            "this hierarchy change would change the meaning of bound Markets or Regions"
        )
        self.impacts = impacts


def _required_text(payload: Mapping[str, Any], key: str) -> str:
    value = str(payload.get(key) or "").strip()
    if not value:
        raise CountryWorkspaceRefused(f"{key} is required")
    return value


def _version_result(version: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": str(version.get("id") or ""),
        "version_number": version.get("version_number"),
        "status": version.get("status"),
        "content_hash": version.get("content_hash"),
    }


def _publish_confirmation_payload(
    *, project_id: str, org_id: str, payload: Mapping[str, Any]
) -> dict[str, str]:
    return {
        "project_id": project_id,
        "org_id": org_id,
        "version_id": _required_text(payload, "version_id"),
        "expected_content_hash": _required_text(payload, "expected_content_hash"),
    }


def _publish_context(*, project_id: str, org_id: str, version_id: str) -> str:
    return (
        f"organization:{org_id}/project:{project_id}/governance/"
        f"master-data/country/version:{version_id}"
    )


def prepare_country_publish_confirmation(
    conn,
    *,
    project_id: str,
    org_id: str,
    actor: str,
    idempotency_key: str,
    payload: Mapping[str, Any],
) -> dict[str, Any]:
    """Issue a short-lived secret bound to one exact Country draft publication."""

    from core.entry_confirmations import (
        COUNTRY_HIERARCHY_PUBLISH_COMMAND,
        issue_entry_confirmation,
    )
    from core.master_data import require_registry, require_version

    request_payload = _publish_confirmation_payload(
        project_id=project_id, org_id=org_id, payload=payload
    )
    registry = require_registry(conn, project_id=project_id, object_kind="country")
    version = require_version(
        conn, project_id=project_id, version_id=request_payload["version_id"]
    )
    if str(version.get("registry_id")) != str(registry["id"]):
        raise CountryWorkspaceRefused("the version does not belong to the Country registry")
    if version.get("status") != "draft":
        raise CountryWorkspaceRefused("only a draft Country version can be published")
    if str(version.get("content_hash")) != request_payload["expected_content_hash"]:
        raise CountryWorkspaceRefused(
            "the Country draft changed since it was reviewed; reload before publishing"
        )
    issued = issue_entry_confirmation(
        conn,
        actor_person_id=actor,
        command_type=COUNTRY_HIERARCHY_PUBLISH_COMMAND,
        request_payload=request_payload,
        idempotency_key=idempotency_key,
        context_reference=_publish_context(
            project_id=project_id,
            org_id=org_id,
            version_id=request_payload["version_id"],
        ),
    )
    return {
        "confirmation_id": issued.confirmation_id,
        "confirmation_secret": issued.confirmation_secret,
        "expires_at": issued.expires_at.isoformat(),
        "version_id": request_payload["version_id"],
        "expected_content_hash": request_payload["expected_content_hash"],
        "confirmation_secret_single_return": True,
    }

def run_country_workspace_command(
    conn,
    *,
    project_id: str,
    org_id: str,
    actor: str,
    idempotency_key: str,
    action: str,
    payload: Mapping[str, Any],
    host_context: Mapping[str, Any] | None = None,
    trace_id: str | None = None,
) -> dict[str, Any]:
    """Execute one Country edit as a generic durable operation."""

    from core.operations import MutationResult, OperationSpec, execute_operation

    action = action.strip()
    if action not in {"apply_preset", "start_draft", "save_hierarchy", "publish"}:
        raise CountryWorkspaceRefused(
            "action must be apply_preset, start_draft, save_hierarchy or publish"
        )
    if not idempotency_key.strip():
        raise CountryWorkspaceRefused("idempotency_key is required")

    publish_confirmation = None
    operation_payload = dict(payload)
    confirmation_reference = idempotency_key
    if action == "publish":
        from core.entry_confirmations import (
            COUNTRY_HIERARCHY_PUBLISH_COMMAND,
            consume_entry_confirmation,
        )

        confirmation_id = _required_text(payload, "confirmation_id")
        confirmation_secret = _required_text(payload, "confirmation_secret")
        request_payload = _publish_confirmation_payload(
            project_id=project_id, org_id=org_id, payload=payload
        )
        publish_confirmation = consume_entry_confirmation(
            conn,
            confirmation_id=confirmation_id,
            confirmation_secret=confirmation_secret,
            actor_person_id=actor,
            command_type=COUNTRY_HIERARCHY_PUBLISH_COMMAND,
            request_payload=request_payload,
            idempotency_key=idempotency_key,
            context_reference=_publish_context(
                project_id=project_id,
                org_id=org_id,
                version_id=request_payload["version_id"],
            ),
        )
        operation_payload = {
            "version_id": request_payload["version_id"],
            "expected_content_hash": request_payload["expected_content_hash"],
        }
        confirmation_reference = confirmation_secret

    def mutation(mutation_conn, _operation_id: str) -> MutationResult:
        if action == "apply_preset":
            return _apply_preset(
                mutation_conn,
                project_id=project_id,
                org_id=org_id,
                actor=actor,
                payload=payload,
            )
        if action == "start_draft":
            return _start_draft(
                mutation_conn,
                project_id=project_id,
                org_id=org_id,
                actor=actor,
                payload=payload,
            )
        if action == "save_hierarchy":
            return _save_hierarchy(
                mutation_conn,
                project_id=project_id,
                org_id=org_id,
                actor=actor,
                payload=payload,
            )
        return _publish(
            mutation_conn,
            project_id=project_id,
            actor=actor,
            payload=payload,
        )

    spec = OperationSpec(
        command_type="governance.country.workspace",
        actor=actor,
        effective_org_id=org_id,
        resource_path=(
            f"organization:{org_id}",
            f"project:{project_id}",
            "governance:master-data",
            "registry:country",
        ),
        idempotency_key=idempotency_key,
        host_context=dict(host_context or {}),
        versions={
            "policy": "country-workspace-v1",
            "catalog": "country-workspace-v1",
            "tool": "country-workspace-v1",
        },
        request_payload={"action": action, "payload": operation_payload},
        provider_references={},
        confirmation_mode="human",
        confirmation_reference=confirmation_reference,
        trace_id=trace_id,
    )
    operation = execute_operation(conn, spec, mutation=mutation)
    if publish_confirmation is not None:
        from core.entry_confirmations import bind_entry_confirmation_operation

        bind_entry_confirmation_operation(
            conn,
            confirmation=publish_confirmation,
            operation_id=operation.operation_id,
        )
    return {
        "operation_id": operation.operation_id,
        "outcome": operation.outcome,
        "result": operation.result,
        "idempotent_replay": operation.replayed,
    }


def _apply_preset(
    conn,
    *,
    project_id: str,
    org_id: str,
    actor: str,
    payload: Mapping[str, Any],
):
    from core.country_registry import (
        ensure_country_registry,
        materialize_preset,
        seed_country_presets,
    )
    from core.master_data import content_hash, list_versions
    from core.operations import MutationResult

    requested_preset = _required_text(payload, "preset_id")
    registry = ensure_country_registry(conn, org_id=org_id, project_id=project_id, actor=actor)
    mutable = [
        version
        for version in list_versions(conn, project_id=project_id, registry_id=str(registry["id"]))
        if version.get("status") in {"draft", "candidate"}
    ]
    if mutable:
        raise CountryWorkspaceRefused(
            "finish or discard the existing Country draft before applying another preset"
        )

    presets = seed_country_presets(conn)
    preset = next(
        (
            item
            for item in presets
            if str(item.get("id")) == requested_preset
            or str(item.get("preset_key")) == requested_preset
        ),
        None,
    )
    if preset is None:
        raise CountryWorkspaceRefused("the selected Country preset does not exist")
    draft = materialize_preset(
        conn,
        org_id=org_id,
        project_id=project_id,
        registry_id=str(registry["id"]),
        preset_version_id=str(preset["id"]),
        actor=actor,
        selected_optional_values=[
            str(value) for value in (payload.get("selected_optional_values") or ())
        ],
        rest_of_world_label=str(payload.get("rest_of_world_label") or "Rest of World"),
    )
    return MutationResult(
        outcome="succeeded",
        before_hash=content_hash(
            {
                "registry_id": registry["id"],
                "current_version_id": registry.get("current_version_id"),
            }
        ),
        after_hash=str(draft["content_hash"]),
        result={
            "action": "apply_preset",
            "registry_id": registry["id"],
            "preset_version_id": preset["id"],
            "draft_version_id": draft["id"],
            "draft_content_hash": draft["content_hash"],
        },
        outbox_payload={
            "project_id": project_id,
            "registry_id": registry["id"],
            "draft_version_id": draft["id"],
        },
    )


def _start_draft(
    conn,
    *,
    project_id: str,
    org_id: str,
    actor: str,
    payload: Mapping[str, Any],
):
    from core.master_data import (
        DraftContent,
        content_hash,
        create_draft_version,
        fetch_memberships,
        list_versions,
        require_registry,
        require_version,
    )
    from core.operations import MutationResult

    current_version_id = _required_text(payload, "current_version_id")
    expected_hash = _required_text(payload, "expected_content_hash")
    registry = require_registry(conn, project_id=project_id, object_kind="country")
    if str(registry.get("current_version_id") or "") != current_version_id:
        raise CountryWorkspaceRefused(
            "the published Country version changed; reload before opening a draft"
        )
    mutable = [
        version
        for version in list_versions(conn, project_id=project_id, registry_id=str(registry["id"]))
        if version.get("status") in {"draft", "candidate"}
    ]
    if mutable:
        raise CountryWorkspaceRefused(
            "finish or discard the existing Country draft before opening another"
        )

    current = require_version(conn, project_id=project_id, version_id=current_version_id)
    if str(current.get("registry_id")) != str(registry["id"]):
        raise CountryWorkspaceRefused(
            "the published version does not belong to the Country registry"
        )
    if str(current.get("content_hash")) != expected_hash:
        raise CountryWorkspaceRefused(
            "the published Country version changed; reload before opening a draft"
        )
    memberships = fetch_memberships(conn, project_id=project_id, version_id=current_version_id)
    draft = create_draft_version(
        conn,
        org_id=org_id,
        project_id=project_id,
        registry_id=str(registry["id"]),
        vocabulary_version_id=current.get("vocabulary_version_id"),
        actor=actor,
        content=DraftContent(
            memberships=memberships,
            payload=dict(current.get("payload") or {}),
        ),
        origin_preset_version_id=current.get("origin_preset_version_id"),
    )
    return MutationResult(
        outcome="succeeded",
        before_hash=content_hash(_version_result(current)),
        after_hash=str(draft["content_hash"]),
        result={
            "action": "start_draft",
            "registry_id": registry["id"],
            "source_version_id": current_version_id,
            "draft_version": _version_result(draft),
        },
        outbox_payload={
            "project_id": project_id,
            "registry_id": registry["id"],
            "draft_version_id": draft["id"],
        },
    )


def _save_hierarchy(
    conn,
    *,
    project_id: str,
    org_id: str,
    actor: str,
    payload: Mapping[str, Any],
):
    from core.master_data import (
        create_node,
        fetch_memberships,
        fetch_used_by,
        fetch_vocabulary_version,
        list_nodes,
        replace_draft_memberships,
        require_registry,
        require_version,
        update_draft_payload,
    )
    from core.operations import MutationResult

    version_id = _required_text(payload, "version_id")
    expected_hash = _required_text(payload, "expected_content_hash")
    registry = require_registry(conn, project_id=project_id, object_kind="country")
    version = require_version(conn, project_id=project_id, version_id=version_id)
    if version.get("status") != "draft":
        raise CountryWorkspaceRefused("only a draft Country version can be edited")
    if str(version.get("registry_id")) != str(registry["id"]):
        raise CountryWorkspaceRefused("the draft does not belong to the Country registry")
    if str(version.get("content_hash")) != expected_hash:
        raise CountryWorkspaceRefused(
            "the Country draft changed since it was reviewed; reload before saving"
        )

    vocabulary = fetch_vocabulary_version(
        conn, vocabulary_version_id=version.get("vocabulary_version_id")
    )
    if vocabulary is None:
        raise CountryWorkspaceRefused("the pinned Country vocabulary is unavailable")
    canonical_values = {
        str(entry.get("code")) for entry in (vocabulary.get("entries") or ()) if entry.get("code")
    }
    existing_nodes = list_nodes(
        conn,
        project_id=project_id,
        registry_id=str(registry["id"]),
        include_archived=True,
    )
    requested_nodes = list(payload.get("nodes") or ())
    requested_memberships = list(payload.get("memberships") or ())
    placeholder_ids = {
        str(item.get("ref")): f"new-placeholder:{index}"
        for index, item in enumerate(requested_nodes)
        if not item.get("id")
    }
    preflight = normalize_hierarchy_edit(
        nodes=existing_nodes,
        requested_nodes=requested_nodes,
        requested_memberships=requested_memberships,
        canonical_values=canonical_values,
        minted_ids=placeholder_ids,
    )

    current_version = (
        require_version(
            conn,
            project_id=project_id,
            version_id=str(registry["current_version_id"]),
        )
        if registry.get("current_version_id")
        else None
    )
    previous = (
        fetch_memberships(
            conn,
            project_id=project_id,
            version_id=str(registry["current_version_id"]),
        )
        if current_version
        else ()
    )
    existing_labels = {
        str(item["id"]): str(item.get("label") or item["id"])
        for item in existing_nodes
    }
    previous_payload = dict((current_version or {}).get("payload") or {})
    previous_labels = {
        **existing_labels,
        **{
            str(node_id): str(label)
            for node_id, label in (previous_payload.get("node_labels") or {}).items()
        },
    }
    target_labels = {
        node.id: node.label
        for node in preflight.nodes
        if node.existing
    }
    rest_of_world = dict((version.get("payload") or {}).get("rest_of_world") or {})
    if rest_of_world.get("node_id"):
        previous_rest = dict(previous_payload.get("rest_of_world") or {})
        previous_labels[str(rest_of_world["node_id"])] = str(
            previous_rest.get("label") or rest_of_world.get("label") or "Rest of World"
        )
        target_labels[str(rest_of_world["node_id"])] = str(
            payload.get("rest_of_world_label")
            or rest_of_world.get("label")
            or "Rest of World"
        )
    used_by = fetch_used_by(conn, project_id=project_id, registry_id=str(registry["id"]))
    impacts = changed_bound_nodes(
        previous,
        preflight.memberships,
        used_by,
        previous_labels=previous_labels,
        target_labels=target_labels,
    )
    if impacts and not bool(payload.get("acknowledge_impact")):
        raise CountryWorkspaceImpactRefused(impacts)

    minted_ids: dict[str, str] = {}
    for item in requested_nodes:
        if item.get("id"):
            continue
        created = create_node(
            conn,
            org_id=org_id,
            project_id=project_id,
            registry_id=str(registry["id"]),
            node_kind=str(item.get("kind") or ""),
            label=str(item.get("label") or ""),
            actor=actor,
        )
        minted_ids[str(item.get("ref"))] = str(created["id"])

    edit = normalize_hierarchy_edit(
        nodes=existing_nodes,
        requested_nodes=requested_nodes,
        requested_memberships=requested_memberships,
        canonical_values=canonical_values,
        minted_ids=minted_ids,
    )
    version_payload = dict(version.get("payload") or {})
    version_payload["node_labels"] = {node.id: node.label for node in edit.nodes}
    rest_payload = dict(version_payload.get("rest_of_world") or {})
    if rest_payload:
        rest_payload["label"] = str(
            payload.get("rest_of_world_label")
            or rest_payload.get("label")
            or "Rest of World"
        )
        drill = str(
            payload.get("rest_of_world_drill")
            or rest_payload.get("default_drill")
            or "country"
        )
        if drill not in {"country", "aggregate"}:
            raise CountryWorkspaceRefused("Rest of World drill must be country or aggregate")
        rest_payload["default_drill"] = drill
        parent_ref = str(payload.get("rest_of_world_parent_ref") or "").strip()
        if parent_ref:
            parent = next((node for node in edit.nodes if node.ref == parent_ref), None)
            if parent is None or parent.kind != "region":
                raise CountryWorkspaceRefused("Rest of World parent must be a reporting Region")
            rest_payload["parent_node_id"] = parent.id
        else:
            rest_payload["parent_node_id"] = None
        version_payload["rest_of_world"] = rest_payload

    saved = replace_draft_memberships(
        conn,
        project_id=project_id,
        version_id=version_id,
        memberships=edit.memberships,
    )
    saved = update_draft_payload(
        conn,
        project_id=project_id,
        version_id=version_id,
        payload=version_payload,
    )
    return MutationResult(
        outcome="succeeded",
        before_hash=expected_hash,
        after_hash=str(saved["content_hash"]),
        result={
            "action": "save_hierarchy",
            "registry_id": registry["id"],
            "draft_version": _version_result(saved),
            "created_node_ids": minted_ids,
            "acknowledged_impacts": impacts if bool(payload.get("acknowledge_impact")) else [],
        },
        outbox_payload={
            "project_id": project_id,
            "registry_id": registry["id"],
            "draft_version_id": version_id,
        },
    )


def _publish(
    conn,
    *,
    project_id: str,
    actor: str,
    payload: Mapping[str, Any],
):
    from core.master_data import (
        content_hash,
        mark_candidate,
        publish_version,
        require_registry,
        require_version,
    )
    from core.operations import MutationResult

    version_id = _required_text(payload, "version_id")
    expected_hash = _required_text(payload, "expected_content_hash")
    registry = require_registry(conn, project_id=project_id, object_kind="country")
    version = require_version(conn, project_id=project_id, version_id=version_id)
    if str(version.get("registry_id")) != str(registry["id"]):
        raise CountryWorkspaceRefused("the version does not belong to the Country registry")
    if str(version.get("content_hash")) != expected_hash:
        raise CountryWorkspaceRefused(
            "the Country draft changed since it was reviewed; reload before publishing"
        )
    candidate = mark_candidate(conn, project_id=project_id, version_id=version_id)
    published = publish_version(
        conn,
        project_id=project_id,
        version_id=version_id,
        actor=actor,
        expected_content_hash=expected_hash,
    )
    return MutationResult(
        outcome="succeeded",
        before_hash=content_hash(_version_result(version)),
        after_hash=str(published["content_hash"]),
        result={
            "action": "publish",
            "registry_id": published["registry_id"],
            "candidate_version": _version_result(candidate),
            "published_version": _version_result(published),
        },
        outbox_payload={
            "project_id": project_id,
            "registry_id": published["registry_id"],
            "published_version_id": version_id,
        },
    )


__all__ = [
    "CountryWorkspaceImpactRefused",
    "prepare_country_publish_confirmation",
    "run_country_workspace_command",
]
