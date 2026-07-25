"""Parse a Google API discovery document JSON into official field candidates.

Generic traversal — no provider names are hardcoded here.
Provider-specific traversal configuration lives in per-module catalog_sources.json
(story 25.4), passed as a ``config`` dict at call time.

Config schema (example for GAM ReportDefinition enums):
{
    "schema_path": ["schemas", "ReportDefinition", "properties"],
    "enum_property": "Dimension",       // property name whose enum lists dimensions
    "kind": "dimension"                 // "dimension" | "metric"
}

For sources with separate dimension/metric enum properties, call the function twice
with two configs (one per kind) and concatenate the results.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class DiscoveryEntry:
    """One field candidate extracted from a Google discovery document."""

    field_id: str
    kind: str  # "metric" | "dimension"
    description: str | None


def _get_nested(obj: Any, path: list[str]) -> Any:
    """Traverse a nested dict following ``path`` and return the value, or None."""
    current = obj
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def parse_discovery_document(
    doc: dict[str, Any],
    config: dict[str, Any],
) -> list[DiscoveryEntry]:
    """Extract field candidates from a Google discovery document.

    Parameters
    ----------
    doc:
        The parsed JSON discovery document (as a Python dict).
    config:
        Traversal configuration dict with the following keys:
        - ``schema_path`` (list[str]): path to the container object in ``doc``.
        - ``enum_property`` (str): key within that container whose value is a
          property with an ``enum`` list (or a sub-object with per-item
          descriptions in ``enumDescriptions``).
        - ``kind`` (str): "dimension" or "metric" (any other value raises --
          review F-B3: an unknown kind must fail loudly, not flow into the
          catalog and die later at schema validation).

    Returns
    -------
    list[DiscoveryEntry]
        Candidates in document order (enum order).
    """
    schema_path: list[str] = config["schema_path"]
    enum_property: str = config["enum_property"]
    kind: str = config.get("kind", "dimension")
    if kind not in ("dimension", "metric"):
        raise ValueError(
            f"discovery config kind must be dimension|metric, got: {kind!r}"
        )

    container = _get_nested(doc, schema_path)
    if container is None:
        return []

    # The enum_property may be:
    # 1. A direct property with {"enum": [...], "enumDescriptions": [...]}
    # 2. A property name pointing to a sub-property that has enum values
    # Try to find enum values at container[enum_property]["enum"]
    prop = container.get(enum_property)
    if prop is None:
        return []

    enum_values: list[str] = []
    enum_descriptions: list[str] = []

    if isinstance(prop, dict):
        enum_values = prop.get("enum", [])
        enum_descriptions = prop.get("enumDescriptions", [])

    entries: list[DiscoveryEntry] = []
    for i, value in enumerate(enum_values):
        if not isinstance(value, str) or not value:
            continue
        description: str | None = None
        if i < len(enum_descriptions) and enum_descriptions[i]:
            description = str(enum_descriptions[i])
        entries.append(
            DiscoveryEntry(
                field_id=value,
                kind=kind,
                description=description,
            )
        )

    return entries
