"""Country Governance workbench read model and hierarchy edit validation.

The Country registry already owns presets, stable nodes, immutable versions and
memberships.  This module is the thin application layer that makes that owner
usable by the Governance console without introducing another Country store.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

from core.master_data import Membership

EDITABLE_NODE_KINDS = frozenset({"market", "region"})


class CountryWorkspaceRefused(ValueError):
    """An editor request would create an invalid or ambiguous hierarchy."""

    code = "country_workspace_refused"


@dataclass(frozen=True, slots=True)
class EditableNode:
    ref: str
    id: str
    kind: str
    label: str
    existing: bool


@dataclass(frozen=True, slots=True)
class HierarchyEdit:
    nodes: tuple[EditableNode, ...]
    memberships: tuple[Membership, ...]


def _mapping(value: Any) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    if hasattr(value, "as_dict"):
        rendered = value.as_dict()
        if isinstance(rendered, Mapping):
            return rendered
    if hasattr(value, "__dict__"):
        return vars(value)
    return {}


def _membership_dict(value: Any) -> dict[str, Any]:
    row = _mapping(value)
    return {
        key: row.get(key)
        for key in (
            "version_id",
            "parent_node_id",
            "child_node_id",
            "child_value",
            "effective_from",
            "effective_to",
            "display_order",
        )
        if row.get(key) is not None
    }


def compose_country_workspace(
    *,
    registry: Mapping[str, Any] | None,
    presets: Sequence[Mapping[str, Any]],
    nodes: Sequence[Mapping[str, Any]],
    versions: Sequence[Mapping[str, Any]],
    memberships: Sequence[Any],
    vocabulary: Sequence[Mapping[str, Any]],
    used_by: Sequence[Any],
) -> dict[str, Any]:
    """Compose the complete, bounded Country editor envelope.

    A Project with no registry is not rendered as a blank form: qualified,
    versioned presets remain visible and one must be materialized explicitly.
    """

    preset_rows = [
        {
            "id": str(item["id"]),
            "key": str(item.get("preset_key") or ""),
            "label": str(item.get("label") or item["id"]),
            "description": str(item.get("description") or ""),
            "classification": str(item.get("classification") or ""),
            "source_authority": str(item.get("source_authority") or ""),
            "source_reference": item.get("source_reference"),
            "version": str(item.get("preset_version") or ""),
            "content_hash": item.get("content_hash"),
            "payload": dict(item.get("payload") or {}),
        }
        for item in presets
    ]
    vocabulary_rows = [
        {
            "code": str(item.get("code") or ""),
            "display_name": str(item.get("display_name") or item.get("code") or ""),
            "aliases": sorted(str(alias) for alias in (item.get("aliases") or ())),
            "assignment": item.get("assignment"),
        }
        for item in vocabulary
        if item.get("code")
    ]
    if registry is None:
        return {
            "state": "preset_required",
            "registry": None,
            "presets": preset_rows,
            "vocabulary": vocabulary_rows,
            "nodes": [],
            "draft": None,
            "current": None,
            "versions": [],
            "used_by": [],
        }

    version_rows = [dict(item) for item in versions]
    draft = next((item for item in version_rows if item.get("status") == "draft"), None)
    current = next(
        (
            item
            for item in version_rows
            if str(item.get("id")) == str(registry.get("current_version_id"))
        ),
        None,
    )
    selected = draft or current
    selected_id = str(selected.get("id")) if selected else None
    selected_memberships = [
        row
        for row in (_membership_dict(item) for item in memberships)
        if row.get("version_id") in (None, selected_id)
    ]
    if draft is not None:
        draft = {**draft, "memberships": selected_memberships}
    if current is not None:
        current_memberships = [
            row
            for row in (_membership_dict(item) for item in memberships)
            if row.get("version_id") in (None, str(current.get("id")))
        ]
        current = {**current, "memberships": current_memberships}

    selected_payload = (
        dict(selected.get("payload") or {})
        if selected and isinstance(selected.get("payload"), Mapping)
        else {}
    )
    label_overrides = {
        str(node_id): str(label)
        for node_id, label in (selected_payload.get("node_labels") or {}).items()
    }
    selected_node_ids = {
        str(row.get("parent_node_id") or "")
        for row in selected_memberships
        if row.get("parent_node_id")
    } | {
        str(row.get("child_node_id") or "")
        for row in selected_memberships
        if row.get("child_node_id")
    } | set(label_overrides)
    rest_of_world = selected_payload.get("rest_of_world") or {}
    if rest_of_world.get("node_id"):
        selected_node_ids.add(str(rest_of_world["node_id"]))
    output_nodes = []
    for item in nodes:
        node = dict(item)
        node_id = str(node.get("id") or "")
        if selected_id and node_id not in selected_node_ids:
            continue
        if node_id in label_overrides:
            node["label"] = label_overrides[node_id]
        if node.get("node_kind") == "rest_of_world" and rest_of_world.get("label"):
            node["label"] = str(rest_of_world["label"])
        output_nodes.append(node)
    used_by_rows = []
    for reference in used_by:
        row = _mapping(reference)
        used_by_rows.append(
            {
                "node_id": str(row.get("node_id") or ""),
                "consumer_kind": str(row.get("consumer_kind") or ""),
                "consumer_id": str(row.get("consumer_id") or ""),
                "consumer_label": row.get("consumer_label"),
                "consumer_version_id": row.get("consumer_version_id"),
                "hierarchy_version_id": row.get("hierarchy_version_id"),
            }
        )

    return {
        "state": "draft" if draft else "published" if current else "preset_required",
        "registry": dict(registry),
        "presets": preset_rows,
        "vocabulary": vocabulary_rows,
        "nodes": output_nodes,
        "draft": draft,
        "current": current,
        "versions": version_rows,
        "used_by": used_by_rows,
    }


def _resolve_ref(ref: str, refs: Mapping[str, str]) -> str:
    try:
        return refs[ref]
    except KeyError as exc:
        raise CountryWorkspaceRefused(f"unknown node reference: {ref}") from exc


def _assert_acyclic(edges: Iterable[tuple[str, str]]) -> None:
    children: dict[str, set[str]] = {}
    for parent, child in edges:
        children.setdefault(parent, set()).add(child)

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node_id: str) -> None:
        if node_id in visiting:
            raise CountryWorkspaceRefused("hierarchy contains a cycle")
        if node_id in visited:
            return
        visiting.add(node_id)
        for child_id in children.get(node_id, ()):
            visit(child_id)
        visiting.remove(node_id)
        visited.add(node_id)

    for node_id in tuple(children):
        visit(node_id)


def normalize_hierarchy_edit(
    *,
    nodes: Sequence[Mapping[str, Any]],
    requested_nodes: Sequence[Mapping[str, Any]],
    requested_memberships: Sequence[Mapping[str, Any]],
    canonical_values: set[str],
    minted_ids: Mapping[str, str],
) -> HierarchyEdit:
    """Resolve client-local refs into stable IDs and validate the full draft.

    The edit replaces memberships wholesale.  Validation therefore sees the
    complete target graph and can reject duplicate country assignment or cycles
    before any database write occurs.
    """

    existing = {str(item["id"]): item for item in nodes}
    refs: dict[str, str] = {}
    normalized_nodes: list[EditableNode] = []
    labels: set[str] = set()
    for requested in requested_nodes:
        ref = str(requested.get("ref") or requested.get("id") or "").strip()
        if not ref:
            raise CountryWorkspaceRefused("every node needs a reference")
        node_id = str(requested.get("id") or minted_ids.get(ref) or "").strip()
        if not node_id:
            raise CountryWorkspaceRefused(f"new node {ref} has no server-minted id")
        is_existing = node_id in existing
        if requested.get("id") and not is_existing:
            raise CountryWorkspaceRefused(f"unknown existing node: {node_id}")
        kind = str(requested.get("kind") or "").strip()
        if kind not in EDITABLE_NODE_KINDS:
            raise CountryWorkspaceRefused(f"node kind must be market or region: {ref}")
        if is_existing and str(existing[node_id].get("node_kind")) != kind:
            raise CountryWorkspaceRefused("an existing node kind cannot change")
        label = str(requested.get("label") or "").strip()
        if not label:
            raise CountryWorkspaceRefused(f"node {ref} needs a label")
        if label.casefold() in labels:
            raise CountryWorkspaceRefused(f"duplicate node label: {label}")
        labels.add(label.casefold())
        refs[ref] = node_id
        normalized_nodes.append(
            EditableNode(ref=ref, id=node_id, kind=kind, label=label, existing=is_existing)
        )

    kind_by_id = {node.id: node.kind for node in normalized_nodes}
    normalized_memberships: list[Membership] = []
    assigned_values: set[str] = set()
    node_edges: list[tuple[str, str]] = []
    for order, requested in enumerate(requested_memberships):
        parent_ref = str(requested.get("parent_ref") or "").strip()
        parent_id = _resolve_ref(parent_ref, refs)
        child_ref = str(requested.get("child_ref") or "").strip()
        child_value = str(requested.get("child_value") or "").strip().upper()
        if bool(child_ref) == bool(child_value):
            raise CountryWorkspaceRefused(
                "each membership needs exactly one child_ref or child_value"
            )
        parent_kind = kind_by_id.get(parent_id)
        if child_value:
            if parent_kind != "market":
                raise CountryWorkspaceRefused(
                    "canonical countries must belong directly to a Market"
                )
            if child_value not in canonical_values:
                raise CountryWorkspaceRefused(f"unknown country code: {child_value}")
            if child_value in assigned_values:
                raise CountryWorkspaceRefused(f"country {child_value} is assigned more than once")
            assigned_values.add(child_value)
            normalized_memberships.append(
                Membership(
                    parent_node_id=parent_id,
                    child_value=child_value,
                    display_order=order,
                )
            )
        else:
            child_id = _resolve_ref(child_ref, refs)
            if parent_kind != "region" or kind_by_id.get(child_id) != "market":
                raise CountryWorkspaceRefused("node edges must be Region to Market")
            if parent_id == child_id:
                raise CountryWorkspaceRefused("hierarchy contains a cycle")
            node_edges.append((parent_id, child_id))
            normalized_memberships.append(
                Membership(
                    parent_node_id=parent_id,
                    child_node_id=child_id,
                    display_order=order,
                )
            )
    _assert_acyclic(node_edges)
    assigned_market_ids = {
        membership.parent_node_id
        for membership in normalized_memberships
        if membership.child_value is not None
    }
    empty_markets = sorted(
        node.label
        for node in normalized_nodes
        if node.kind == "market" and node.id not in assigned_market_ids
    )
    if empty_markets:
        raise CountryWorkspaceRefused(
            "every Market requires at least one canonical country: " + ", ".join(empty_markets)
        )
    return HierarchyEdit(tuple(normalized_nodes), tuple(normalized_memberships))


def _preset_definition_rows() -> list[dict[str, Any]]:
    from core.country_registry import COUNTRY_PRESETS
    from core.master_data import content_hash

    return [
        {
            "id": preset.preset_key,
            "preset_key": preset.preset_key,
            "label": preset.label,
            "description": preset.description,
            "classification": preset.classification,
            "source_authority": preset.source_authority,
            "source_reference": preset.source_reference,
            "preset_version": preset.preset_version,
            "payload": preset.payload(),
            "content_hash": content_hash(
                {
                    "preset_key": preset.preset_key,
                    "preset_version": preset.preset_version,
                    "payload": preset.payload(),
                }
            ),
        }
        for preset in COUNTRY_PRESETS
    ]


def load_country_workspace(conn, *, project_id: str) -> dict[str, Any]:
    """Read the exact Country owner plus qualified setup choices.

    Reading a Project with no Country registry performs no write. Preset
    definitions are code-owned, versioned reference data and remain visible so
    the first screen is a qualified proposal rather than an empty editor.
    """

    from core.country_registry import (
        fetch_country_registry,
        list_presets,
        vocabulary_entries,
    )
    from core.master_data import (
        fetch_memberships,
        fetch_used_by,
        list_nodes,
        list_versions,
    )

    registry = fetch_country_registry(conn, project_id=project_id)
    stored_presets = list_presets(conn)
    presets = stored_presets or _preset_definition_rows()
    if registry is None:
        return compose_country_workspace(
            registry=None,
            presets=presets,
            nodes=[],
            versions=[],
            memberships=[],
            vocabulary=vocabulary_entries(),
            used_by=[],
        )

    nodes = list_nodes(
        conn,
        project_id=project_id,
        registry_id=str(registry["id"]),
        include_archived=True,
    )
    versions = list_versions(conn, project_id=project_id, registry_id=str(registry["id"]))
    relevant_ids = {
        str(version["id"])
        for version in versions
        if version.get("status") in {"draft", "current"}
        or str(version.get("id"))
        in {
            str(registry.get("current_version_id") or ""),
            str(registry.get("pending_version_id") or ""),
        }
    }
    memberships: list[dict[str, Any]] = []
    for version_id in relevant_ids:
        for membership in fetch_memberships(conn, project_id=project_id, version_id=version_id):
            memberships.append({"version_id": version_id, **membership.as_dict()})
    used_by = fetch_used_by(conn, project_id=project_id, registry_id=str(registry["id"]))
    return compose_country_workspace(
        registry=registry,
        presets=presets,
        nodes=nodes,
        versions=versions,
        memberships=memberships,
        vocabulary=vocabulary_entries(),
        used_by=used_by,
    )


def _membership_index(
    memberships: Sequence[Any],
) -> dict[str, tuple[set[str], set[str]]]:
    index: dict[str, tuple[set[str], set[str]]] = {}
    for item in memberships:
        row = _mapping(item)
        parent = str(row.get("parent_node_id") or "")
        if not parent:
            continue
        countries, children = index.setdefault(parent, (set(), set()))
        if row.get("child_value"):
            countries.add(str(row["child_value"]))
        if row.get("child_node_id"):
            children.add(str(row["child_node_id"]))
    return index


def _country_closure(
    index: Mapping[str, tuple[set[str], set[str]]],
    node_id: str,
    *,
    visiting: set[str] | None = None,
) -> set[str]:
    path = set(visiting or ())
    if node_id in path:
        raise CountryWorkspaceRefused("hierarchy contains a cycle")
    path.add(node_id)
    countries, children = index.get(node_id, (set(), set()))
    result = set(countries)
    for child_id in children:
        result.update(_country_closure(index, child_id, visiting=path))
    return result


def changed_bound_nodes(
    previous: Sequence[Any],
    target: Sequence[Any],
    used_by: Sequence[Any],
    *,
    previous_labels: Mapping[str, str] | None = None,
    target_labels: Mapping[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Name bound nodes whose effective governed meaning changes.

    A Region's meaning is the transitive union of its Markets. Comparing only
    direct edges would miss a country moving inside an unchanged child Market.
    """

    before = _membership_index(previous)
    after = _membership_index(target)
    before_labels = dict(previous_labels or {})
    after_labels = dict(target_labels or {})
    consumers_by_node: dict[str, list[dict[str, Any]]] = {}
    for reference in used_by:
        row = dict(_mapping(reference))
        node_id = str(row.get("node_id") or "")
        if node_id:
            consumers_by_node.setdefault(node_id, []).append(row)

    impacts: list[dict[str, Any]] = []
    for node_id in sorted(consumers_by_node):
        before_countries = _country_closure(before, node_id)
        after_countries = _country_closure(after, node_id)
        before_children = before.get(node_id, (set(), set()))[1]
        after_children = after.get(node_id, (set(), set()))[1]
        previous_label = before_labels.get(node_id)
        target_label = after_labels.get(node_id, previous_label)
        if (
            before_countries == after_countries
            and before_children == after_children
            and previous_label == target_label
        ):
            continue
        impact: dict[str, Any] = {
            "node_id": node_id,
            "previous_country_codes": sorted(before_countries),
            "target_country_codes": sorted(after_countries),
            "consumers": sorted(
                consumers_by_node[node_id],
                key=lambda item: (
                    str(item.get("consumer_kind") or ""),
                    str(item.get("consumer_id") or ""),
                ),
            ),
        }
        if before_children != after_children:
            impact["previous_child_node_ids"] = sorted(before_children)
            impact["target_child_node_ids"] = sorted(after_children)
        if previous_label != target_label:
            impact["previous_label"] = previous_label
            impact["target_label"] = target_label
        impacts.append(impact)
    return impacts

__all__ = [
    "CountryWorkspaceRefused",
    "EditableNode",
    "HierarchyEdit",
    "changed_bound_nodes",
    "compose_country_workspace",
    "load_country_workspace",
    "normalize_hierarchy_edit",
]
