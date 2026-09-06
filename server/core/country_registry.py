"""Country mounted on the generic Master Data owner (Story 48.2).

This module is Country's *domain*: what an ISO code is, what a Market and a
Region mean, which starting points are offered, and how a country row resolves
to a bucket. It owns no lifecycle. Registries, immutable versions, hierarchy
edges, used-by and publication all live in :mod:`core.master_data`, which knows
nothing about geography -- that separation is what Story 48.2's Implementation
Gate requires and what keeps the next capability from rebuilding it.

Three things it replaces outright:

* ``project_preferences.geographic_mode`` / ``local_markets``: mutable JSON with
  no version, no Region, no effective membership and no configurable catch-all;
* the fixed ``__other_markets__`` identity: Rest of World is a real node with an
  editable label, a hierarchy placement and a drill policy;
* the dbt seed as the runtime source of truth: the seed becomes a generated,
  hash-pinned projection of the Governance vocabulary version.

``Unknown`` is deliberately absent from the node model. It is not a place in the
hierarchy -- it is the evidence state of a value that did not resolve, and it
must never absorb valid countries the operator simply has not grouped yet.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from core.country_vocabulary import Country, load_country_vocabulary
from core.master_data import (
    DraftContent,
    MasterDataConflict,
    MasterDataError,
    MasterDataNotFound,
    Membership,
    create_draft_version,
    create_node,
    create_registry,
    fetch_memberships,
    fetch_registry,
    fetch_vocabulary_version,
    import_vocabulary_version,
    list_nodes,
    require_registry,
    require_version,
)

COUNTRY_OBJECT_KIND = "country"
COUNTRY_VOCABULARY_KEY = "country"
COUNTRY_REGISTRY_LABEL = "Country Registry"

MARKET = "market"
REGION = "region"
REST_OF_WORLD = "rest_of_world"

#: The three buckets a resolved row can land in. `unknown` is not a node.
BUCKET_ASSIGNED = "assigned"
BUCKET_REST_OF_WORLD = "rest_of_world"
BUCKET_UNKNOWN = "unknown"

ASSIGNMENT_OFFICIAL = "officially_assigned"
ASSIGNMENT_EXTENSION = "user_assigned_extension"

ISO_AUTHORITY = "ISO 3166-1 alpha-2"
ISO_REFERENCE = "https://www.iso.org/iso-3166-country-codes.html"

#: Codes the platform retains that ISO does **not** officially assign. Each one
#: is labelled at import, so a screen can never present it as an ISO assignment
#: (AC2). Kosovo has no ISO assignment; `XK` is the user-assigned code every
#: major provider emits, so dropping it would silently push real rows into
#: Unknown -- which is why it is retained and named rather than removed.
USER_ASSIGNED_EXTENSIONS: dict[str, str] = {
    "XK": "User-assigned code widely used for Kosovo. Not an ISO 3166-1 assignment.",
}

DEFAULT_REST_OF_WORLD_LABEL = "Rest of World"
DRILL_AGGREGATE = "aggregate"
DRILL_COUNTRY = "country"
DRILL_POLICIES = (DRILL_AGGREGATE, DRILL_COUNTRY)


class CountryRegistryError(MasterDataError):
    """A Country-specific governed operation was rejected."""

    code = "invalid_country_registry_operation"


# ---------------------------------------------------------------------------
# The vocabulary: one deterministic path from snapshot to both consumers.
# ---------------------------------------------------------------------------


def vocabulary_entries(countries: Sequence[Country] | None = None) -> list[dict[str, Any]]:
    """Turn the canonical country set into governed vocabulary entries.

    The entries are sorted by code and the aliases deduplicated, so the same
    input always produces the same content hash -- the property that lets the
    runtime version and the warehouse seed be *proven* to be the same snapshot
    rather than assumed to be.
    """

    source = list(countries) if countries is not None else list(load_country_vocabulary())
    entries: list[dict[str, Any]] = []
    for country in sorted(source, key=lambda item: item.code):
        extension = country.code in USER_ASSIGNED_EXTENSIONS
        assignment = ASSIGNMENT_EXTENSION if extension else ASSIGNMENT_OFFICIAL
        aliases = sorted({alias for alias in country.aliases if alias.strip()})
        entry: dict[str, Any] = {
            "code": country.code,
            "display_name": country.display_name,
            "status": "active",
            "assignment": assignment,
            "aliases": aliases,
        }
        if assignment == ASSIGNMENT_EXTENSION:
            entry["assignment_note"] = USER_ASSIGNED_EXTENSIONS[country.code]
        entries.append(entry)
    return entries


def import_country_vocabulary(
    conn,
    *,
    actor: str,
    source_version: str,
    effective_date: date | str,
    countries: Sequence[Country] | None = None,
) -> dict[str, Any]:
    """Store the canonical Country snapshot as an immutable Governance version.

    Idempotent by content: re-importing an unchanged snapshot returns the row
    already stored instead of minting a rival version of identical content.
    """

    return import_vocabulary_version(
        conn,
        vocabulary_key=COUNTRY_VOCABULARY_KEY,
        source_authority=ISO_AUTHORITY,
        source_version=source_version,
        source_reference=ISO_REFERENCE,
        effective_date=effective_date,
        entries=vocabulary_entries(countries),
        actor=actor,
    )


SEED_COLUMNS = ("iso_code", "display_name", "aliases", "assignment", "status")


def render_seed_csv(entries: Sequence[Mapping[str, Any]]) -> str:
    """Render the warehouse projection of one vocabulary version.

    The seed keeps the two columns `normalize_dimension` reads and gains the
    two that carry the governed distinction, so a warehouse query can tell an
    official assignment from a retained extension without asking Governance.
    """

    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(SEED_COLUMNS)
    for entry in sorted(entries, key=lambda item: str(item["code"])):
        writer.writerow(
            [
                entry["code"],
                entry["display_name"],
                "|".join(entry.get("aliases") or ()),
                entry.get("assignment", ASSIGNMENT_OFFICIAL),
                entry.get("status", "active"),
            ]
        )
    return buffer.getvalue()


def seed_provenance(vocabulary: Mapping[str, Any]) -> dict[str, Any]:
    """The pin that makes the seed traceable to the exact governed version."""

    return {
        "generated_from": "app.master_data_vocabulary_versions",
        "vocabulary_key": vocabulary["vocabulary_key"],
        "vocabulary_version_id": vocabulary["id"],
        "content_hash": vocabulary["content_hash"],
        "source_authority": vocabulary["source_authority"],
        "source_version": vocabulary["source_version"],
        "source_reference": vocabulary.get("source_reference"),
        "effective_date": str(vocabulary["effective_date"]),
        "entry_count": vocabulary["entry_count"],
        "note": (
            "Generated projection. Governance owns the runtime vocabulary; editing "
            "this file by hand makes the warehouse disagree with the pinned version."
        ),
    }


def write_seed_projection(
    entries: Sequence[Mapping[str, Any]], provenance: Mapping[str, Any], *, seed_path: Path
) -> tuple[Path, Path]:
    """Write both halves of the deterministic export next to each other."""

    seed_path.write_text(render_seed_csv(entries), encoding="utf-8")
    pin_path = seed_path.with_suffix(".provenance.json")
    import json  # noqa: PLC0415 -- local: this is the only writer that needs it

    rendered = json.dumps(dict(provenance), indent=2, sort_keys=True) + "\n"
    pin_path.write_text(rendered, encoding="utf-8")
    return seed_path, pin_path


# ---------------------------------------------------------------------------
# Presets. Inert, versioned, explicit -- and never a live dependency.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PresetNode:
    key: str
    node_kind: str
    label: str
    parent_key: str | None = None


@dataclass(frozen=True, slots=True)
class PresetMember:
    parent_key: str
    value: str
    optional: bool = False
    default_selected: bool = True
    note: str | None = None


@dataclass(frozen=True, slots=True)
class PresetDefinition:
    preset_key: str
    label: str
    description: str
    classification: str
    source_authority: str
    preset_version: str
    nodes: tuple[PresetNode, ...]
    members: tuple[PresetMember, ...]
    source_reference: str | None = None

    def payload(self) -> dict[str, Any]:
        return {
            "nodes": [
                {
                    "key": node.key,
                    "node_kind": node.node_kind,
                    "label": node.label,
                    "parent_key": node.parent_key,
                }
                for node in self.nodes
            ],
            "members": [
                {
                    "parent_key": member.parent_key,
                    "value": member.value,
                    "optional": member.optional,
                    "default_selected": member.default_selected,
                    "note": member.note,
                }
                for member in self.members
            ],
        }


def _france_members() -> tuple[PresetMember, ...]:
    """`FR` is fixed; every overseas territory is offered, never attached silently.

    This is the whole difference between a starting point and an authority: a
    French media plan that reports Réunion separately and one that folds it into
    France are both legitimate, and the platform must not decide.
    """

    territories = (
        ("GP", "Guadeloupe"),
        ("MQ", "Martinique"),
        ("GF", "French Guiana"),
        ("RE", "Réunion"),
        ("YT", "Mayotte"),
        ("PM", "Saint Pierre and Miquelon"),
        ("BL", "Saint Barthélemy"),
        ("MF", "Saint Martin"),
        ("WF", "Wallis and Futuna"),
        ("PF", "French Polynesia"),
        ("NC", "New Caledonia"),
    )
    return (PresetMember("france", "FR"),) + tuple(
        PresetMember(
            "france",
            code,
            optional=True,
            default_selected=False,
            note=f"{name}: include only if your reporting folds it into France.",
        )
        for code, name in territories
    )


COUNTRY_PRESETS: tuple[PresetDefinition, ...] = (
    PresetDefinition(
        preset_key="france-and-territories",
        label="France and overseas territories",
        description=(
            "France as one reporting market, with each overseas territory offered "
            "separately. Nothing beyond FR is attached unless you select it."
        ),
        classification="toorow_curated",
        source_authority="toorow",
        preset_version="2026.07",
        nodes=(PresetNode("france", MARKET, "France"),),
        members=_france_members(),
    ),
    PresetDefinition(
        preset_key="dach",
        label="DACH",
        description="Germany, Austria and Switzerland as one commercial market.",
        classification="toorow_curated",
        source_authority="toorow",
        preset_version="2026.07",
        nodes=(PresetNode("dach", MARKET, "DACH"),),
        members=(
            PresetMember("dach", "DE"),
            PresetMember("dach", "AT"),
            PresetMember("dach", "CH"),
        ),
    ),
    PresetDefinition(
        preset_key="benelux",
        label="Benelux",
        description="Belgium, the Netherlands and Luxembourg as one commercial market.",
        classification="toorow_curated",
        source_authority="toorow",
        preset_version="2026.07",
        nodes=(PresetNode("benelux", MARKET, "Benelux"),),
        members=(
            PresetMember("benelux", "BE"),
            PresetMember("benelux", "NL"),
            PresetMember("benelux", "LU"),
        ),
    ),
    PresetDefinition(
        preset_key="nordics",
        label="Nordics",
        description=(
            "Denmark, Finland, Iceland, Norway and Sweden. The three autonomous "
            "territories are offered separately."
        ),
        classification="toorow_curated",
        source_authority="toorow",
        preset_version="2026.07",
        nodes=(PresetNode("nordics", MARKET, "Nordics"),),
        members=(
            PresetMember("nordics", "DK"),
            PresetMember("nordics", "FI"),
            PresetMember("nordics", "IS"),
            PresetMember("nordics", "NO"),
            PresetMember("nordics", "SE"),
            PresetMember("nordics", "FO", optional=True, default_selected=False,
                        note="Faroe Islands: autonomous territory of Denmark."),
            PresetMember("nordics", "GL", optional=True, default_selected=False,
                        note="Greenland: autonomous territory of Denmark."),
            PresetMember("nordics", "AX", optional=True, default_selected=False,
                        note="Åland Islands: autonomous region of Finland."),
        ),
    ),
    PresetDefinition(
        preset_key="emea-apac-amer",
        label="EMEA / APAC / AMER reporting regions",
        description=(
            "Three reporting regions with a starter set of markets. This is a "
            "commercial convention, not a standard: every boundary is yours to edit."
        ),
        classification="toorow_curated",
        source_authority="toorow",
        preset_version="2026.07",
        nodes=(
            PresetNode("emea", REGION, "EMEA"),
            PresetNode("apac", REGION, "APAC"),
            PresetNode("amer", REGION, "AMER"),
            PresetNode("uk-ireland", MARKET, "UK & Ireland", parent_key="emea"),
            PresetNode("southern-europe", MARKET, "Southern Europe", parent_key="emea"),
            PresetNode("anz", MARKET, "Australia & New Zealand", parent_key="apac"),
            PresetNode("north-america", MARKET, "North America", parent_key="amer"),
        ),
        members=(
            PresetMember("uk-ireland", "GB"),
            PresetMember("uk-ireland", "IE"),
            PresetMember("southern-europe", "ES"),
            PresetMember("southern-europe", "IT"),
            PresetMember("southern-europe", "PT"),
            PresetMember("southern-europe", "GR"),
            PresetMember("anz", "AU"),
            PresetMember("anz", "NZ"),
            PresetMember("north-america", "US"),
            PresetMember("north-america", "CA"),
        ),
    ),
    PresetDefinition(
        preset_key="un-m49-subregions-europe",
        label="UN M49 European sub-regions",
        description=(
            "Northern, Western, Southern and Eastern Europe exactly as UN M49 "
            "groups them. Statistical geography -- it does not define a market."
        ),
        classification="standard_derived",
        source_authority="UN Statistics Division, Standard Country or Area Codes (M49)",
        source_reference="https://unstats.un.org/unsd/methodology/m49/overview/",
        preset_version="2026.07",
        nodes=(
            PresetNode("northern-europe", REGION, "Northern Europe"),
            PresetNode("western-europe", REGION, "Western Europe"),
            PresetNode("southern-europe-m49", REGION, "Southern Europe"),
            PresetNode("eastern-europe", REGION, "Eastern Europe"),
        ),
        members=tuple(
            PresetMember(parent, code)
            for parent, codes in (
                ("northern-europe", ("DK", "EE", "FI", "IE", "IS", "LT", "LV", "NO", "SE", "GB")),
                ("western-europe", ("AT", "BE", "CH", "DE", "FR", "LI", "LU", "MC", "NL")),
                (
                    "southern-europe-m49",
                    ("AD", "AL", "ES", "GR", "HR", "IT", "MT", "PT", "RS", "SI"),
                ),
                ("eastern-europe", ("BG", "BY", "CZ", "HU", "MD", "PL", "RO", "RU", "SK", "UA")),
            )
            for code in codes
        ),
    ),
)


def seed_country_presets(conn) -> list[dict[str, Any]]:
    """Store every preset version. Inert: storing one applies nothing."""

    from core.master_data import content_hash  # noqa: PLC0415 -- shared hasher

    stored: list[dict[str, Any]] = []
    columns = (
        "id",
        "object_kind",
        "preset_key",
        "label",
        "description",
        "classification",
        "source_authority",
        "source_reference",
        "preset_version",
        "payload",
        "content_hash",
    )
    import json  # noqa: PLC0415

    from ulid import ULID  # noqa: PLC0415

    for preset in COUNTRY_PRESETS:
        payload = preset.payload()
        digest = content_hash(
            {
                "preset_key": preset.preset_key,
                "preset_version": preset.preset_version,
                "payload": payload,
            }
        )
        with conn.cursor() as cur:
            cur.execute(
                f"""
                SELECT {", ".join(columns)} FROM app.master_data_preset_versions
                WHERE object_kind = %s AND preset_key = %s AND content_hash = %s
                """,
                (COUNTRY_OBJECT_KIND, preset.preset_key, digest),
            )
            row = cur.fetchone()
            if row is None:
                cur.execute(
                    f"""
                    INSERT INTO app.master_data_preset_versions
                        (id, object_kind, preset_key, label, description, classification,
                         source_authority, source_reference, preset_version, payload, content_hash)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s)
                    RETURNING {", ".join(columns)}
                    """,
                    (
                        f"mdpre_{ULID()}",
                        COUNTRY_OBJECT_KIND,
                        preset.preset_key,
                        preset.label,
                        preset.description,
                        preset.classification,
                        preset.source_authority,
                        preset.source_reference,
                        preset.preset_version,
                        json.dumps(payload),
                        digest,
                    ),
                )
                row = cur.fetchone()
        stored.append(dict(zip(columns, row, strict=False)))
    return stored


def list_presets(conn) -> list[dict[str, Any]]:
    columns = (
        "id",
        "preset_key",
        "label",
        "description",
        "classification",
        "source_authority",
        "source_reference",
        "preset_version",
        "payload",
        "content_hash",
    )
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT {", ".join(columns)} FROM app.master_data_preset_versions
            WHERE object_kind = %s ORDER BY classification, label
            """,
            (COUNTRY_OBJECT_KIND,),
        )
        return [dict(zip(columns, row, strict=False)) for row in cur.fetchall()]


def preset_selection(payload: Mapping[str, Any], selected: Iterable[str] | None) -> list[str]:
    """Which vocabulary values a materialization would attach, and no others.

    A caller that passes ``None`` gets the preset's defaults; optional members
    that default to unselected stay out. Nothing is ever attached because it
    happened to be listed.
    """

    chosen = None if selected is None else set(selected)
    values: list[str] = []
    for member in payload.get("members") or []:
        value = str(member.get("value"))
        if not member.get("optional"):
            values.append(value)
        elif chosen is not None:
            if value in chosen:
                values.append(value)
        elif member.get("default_selected"):
            values.append(value)
    return values


# ---------------------------------------------------------------------------
# The Country registry itself.
# ---------------------------------------------------------------------------


def ensure_country_registry(
    conn, *, org_id: str, project_id: str, actor: str
) -> dict[str, Any]:
    """The Project's single Country owner, created once and then reused."""

    return create_registry(
        conn,
        org_id=org_id,
        project_id=project_id,
        object_kind=COUNTRY_OBJECT_KIND,
        label=COUNTRY_REGISTRY_LABEL,
        actor=actor,
    )


def fetch_country_registry(conn, *, project_id: str) -> dict[str, Any] | None:
    return fetch_registry(conn, project_id=project_id, object_kind=COUNTRY_OBJECT_KIND)


def rest_of_world_node(conn, *, project_id: str, registry_id: str) -> dict[str, Any] | None:
    """The Project's single catch-all, or None before one exists.

    The uniqueness this asserts used to be a partial unique index in the generic
    schema, which put a Country rule in a table Business Domains also use.
    Migration 141 removed it; the rule lives here instead, and raises rather
    than silently picking one of two catch-alls -- a second one would make the
    additive reconciliation of AC5 unprovable, so it must be visible.
    """

    found = [
        node
        for node in list_nodes(conn, project_id=project_id, registry_id=registry_id)
        if node["node_kind"] == REST_OF_WORLD
    ]
    if len(found) > 1:
        raise CountryRegistryError(
            f"{len(found)} Rest of World nodes exist in this Country registry; exactly one may"
        )
    return found[0] if found else None


def ensure_rest_of_world(
    conn,
    *,
    org_id: str,
    project_id: str,
    registry_id: str,
    actor: str,
    label: str = DEFAULT_REST_OF_WORLD_LABEL,
) -> dict[str, Any]:
    """Rest of World is a real, singular, renameable node -- not a magic string."""

    existing = rest_of_world_node(conn, project_id=project_id, registry_id=registry_id)
    if existing is not None:
        return existing
    return create_node(
        conn,
        org_id=org_id,
        project_id=project_id,
        registry_id=registry_id,
        node_kind=REST_OF_WORLD,
        label=label,
        actor=actor,
    )


def rest_of_world_payload(
    *,
    node_id: str,
    label: str = DEFAULT_REST_OF_WORLD_LABEL,
    parent_node_id: str | None = None,
    drill: str = DRILL_COUNTRY,
) -> dict[str, Any]:
    if drill not in DRILL_POLICIES:
        raise CountryRegistryError(f"unknown Rest of World drill policy: {drill!r}")
    return {
        "node_id": node_id,
        "label": label,
        "parent_node_id": parent_node_id,
        "default_drill": drill,
    }


def materialize_preset(
    conn,
    *,
    org_id: str,
    project_id: str,
    registry_id: str,
    preset_version_id: str,
    actor: str,
    vocabulary_version_id: str | None = None,
    selected_optional_values: Iterable[str] | None = None,
    rest_of_world_label: str = DEFAULT_REST_OF_WORLD_LABEL,
) -> dict[str, Any]:
    """Turn an inert preset into a Project-owned, editable draft.

    Every node minted here is new and Project-scoped: the draft records which
    preset version it came from and owes it nothing afterwards. Re-storing or
    retiring the preset can never change a Project that already materialized it.
    """

    registry = require_registry(conn, project_id=project_id, registry_id=registry_id)
    presets = {item["id"]: item for item in list_presets(conn)}
    preset = presets.get(preset_version_id)
    if preset is None:
        raise MasterDataNotFound("preset version not found")

    if vocabulary_version_id is None:
        vocabulary = fetch_vocabulary_version(conn, vocabulary_key=COUNTRY_VOCABULARY_KEY)
        if vocabulary is None:
            raise MasterDataConflict(
                "no Country vocabulary version exists; import one before materializing a preset"
            )
        vocabulary_version_id = vocabulary["id"]

    payload = preset["payload"]
    values = preset_selection(payload, selected_optional_values)

    minted: dict[str, str] = {}
    for node in payload.get("nodes") or []:
        created = create_node(
            conn,
            org_id=org_id,
            project_id=project_id,
            registry_id=registry_id,
            node_kind=str(node["node_kind"]),
            label=str(node["label"]),
            actor=actor,
            origin_preset_version_id=preset["id"],
        )
        minted[str(node["key"])] = created["id"]

    catch_all = ensure_rest_of_world(
        conn,
        org_id=org_id,
        project_id=project_id,
        registry_id=registry_id,
        actor=actor,
        label=rest_of_world_label,
    )

    memberships: list[Membership] = []
    for node in payload.get("nodes") or []:
        parent_key = node.get("parent_key")
        if parent_key:
            memberships.append(
                Membership(
                    parent_node_id=minted[str(parent_key)],
                    child_node_id=minted[str(node["key"])],
                )
            )
    selected = set(values)
    order = 0
    for member in payload.get("members") or []:
        value = str(member["value"])
        if value not in selected:
            continue
        memberships.append(
            Membership(
                parent_node_id=minted[str(member["parent_key"])],
                child_value=value,
                display_order=order,
            )
        )
        order += 1

    content = DraftContent(
        memberships=tuple(memberships),
        payload={
            "node_labels": {
                minted[str(node["key"])]: str(node["label"])
                for node in payload.get("nodes") or []
            },
            "rest_of_world": rest_of_world_payload(
                node_id=catch_all["id"], label=catch_all["label"]
            ),
            "origin_preset": {
                "preset_version_id": preset["id"],
                "preset_key": preset["preset_key"],
                "classification": preset["classification"],
                "content_hash": preset["content_hash"],
            },
        },
    )
    return create_draft_version(
        conn,
        org_id=org_id,
        project_id=project_id,
        registry_id=registry["id"],
        vocabulary_version_id=vocabulary_version_id,
        actor=actor,
        content=content,
        origin_preset_version_id=preset["id"],
    )


# ---------------------------------------------------------------------------
# The projection every consumer reads. One contract, one version pin.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class GeographyProjection:
    """Everything needed to resolve a country row, pinned to one version."""

    hierarchy_version_id: str
    vocabulary_version_id: str
    registry_id: str
    as_of: date
    market_of_value: Mapping[str, str]
    region_of_node: Mapping[str, str]
    labels: Mapping[str, str]
    kinds: Mapping[str, str]
    canonical_values: frozenset[str]
    rest_of_world_id: str | None
    rest_of_world_label: str
    rest_of_world_drill: str

    def bucket_for(self, canonical_code: str | None) -> dict[str, Any]:
        """Resolve one canonical country to its governed bucket.

        Three outcomes, and the difference between the last two is the point of
        AC5: a value that did not resolve is `unknown` and repairable; a valid
        country nobody grouped is `rest_of_world` and drillable. Mixing them
        hides real geography inside a data-quality bucket.
        """

        if canonical_code is None or canonical_code not in self.canonical_values:
            return {
                "geography_bucket_kind": BUCKET_UNKNOWN,
                "country_id": None,
                "market_id": None,
                "market_label": None,
                "region_id": None,
                "region_label": None,
                "geography_hierarchy_version_id": self.hierarchy_version_id,
            }
        market_id = self.market_of_value.get(canonical_code)
        if market_id is None:
            return {
                "geography_bucket_kind": BUCKET_REST_OF_WORLD,
                "country_id": canonical_code,
                "market_id": self.rest_of_world_id,
                "market_label": self.rest_of_world_label,
                "region_id": None,
                "region_label": None,
                "geography_hierarchy_version_id": self.hierarchy_version_id,
            }
        region_id = self._region_above(market_id)
        return {
            "geography_bucket_kind": BUCKET_ASSIGNED,
            "country_id": canonical_code,
            "market_id": market_id,
            "market_label": self.labels.get(market_id),
            "region_id": region_id,
            "region_label": self.labels.get(region_id) if region_id else None,
            "geography_hierarchy_version_id": self.hierarchy_version_id,
        }

    def _region_above(self, node_id: str) -> str | None:
        seen: set[str] = set()
        current: str | None = node_id
        while current is not None and current not in seen:
            seen.add(current)
            parent = self.region_of_node.get(current)
            if parent is None:
                return None
            if self.kinds.get(parent) == REGION:
                return parent
            current = parent
        return None

    def rest_of_world_members(self) -> tuple[str, ...]:
        """The exact countries Rest of World stands for, ready to drill (AC5)."""

        return tuple(sorted(self.canonical_values - set(self.market_of_value)))

    def descriptors(self) -> list[dict[str, Any]]:
        """Bindable buckets for a budget, an objective or a filter.

        Rest of World is never bindable: it is a residual whose membership
        changes whenever a Market does, so binding a budget to it would silently
        redefine that budget on the next regroup.
        """

        assigned = sorted(
            {node for node in self.market_of_value.values()},
            key=lambda node: self.labels.get(node, node),
        )
        items = [
            {
                "id": node,
                "label": self.labels.get(node, node),
                "kind": self.kinds.get(node, MARKET),
                "bindable": True,
            }
            for node in assigned
        ]
        if self.rest_of_world_id:
            items.append(
                {
                    "id": self.rest_of_world_id,
                    "label": self.rest_of_world_label,
                    "kind": REST_OF_WORLD,
                    "bindable": False,
                }
            )
        return items


def build_projection(
    *,
    hierarchy_version_id: str,
    vocabulary_version_id: str,
    registry_id: str,
    memberships: Sequence[Membership],
    nodes: Sequence[Mapping[str, Any]],
    canonical_values: Iterable[str],
    rest_of_world: Mapping[str, Any] | None,
    as_of: date | None = None,
) -> GeographyProjection:
    """Assemble the projection from already-read evidence. Pure and testable."""

    moment = as_of or date.today()
    labels = {str(node["id"]): str(node["label"]) for node in nodes}
    kinds = {str(node["id"]): str(node["node_kind"]) for node in nodes}

    market_of_value: dict[str, str] = {}
    region_of_node: dict[str, str] = {}
    for edge in memberships:
        if edge.effective_from > moment:
            continue
        if edge.effective_to is not None and moment >= edge.effective_to:
            continue
        if edge.child_value is not None:
            market_of_value[edge.child_value] = edge.parent_node_id
        elif edge.child_node_id is not None:
            region_of_node[edge.child_node_id] = edge.parent_node_id

    catch_all = dict(rest_of_world or {})
    return GeographyProjection(
        hierarchy_version_id=hierarchy_version_id,
        vocabulary_version_id=vocabulary_version_id,
        registry_id=registry_id,
        as_of=moment,
        market_of_value=market_of_value,
        region_of_node=region_of_node,
        labels=labels,
        kinds=kinds,
        canonical_values=frozenset(canonical_values),
        rest_of_world_id=catch_all.get("node_id"),
        rest_of_world_label=str(catch_all.get("label") or DEFAULT_REST_OF_WORLD_LABEL),
        rest_of_world_drill=str(catch_all.get("default_drill") or DRILL_COUNTRY),
    )


def load_projection(
    conn, *, project_id: str, version_id: str | None = None, as_of: date | None = None
) -> GeographyProjection | None:
    """Read the Project's governed geography at an exact version.

    ``None`` means the Project has no published Country meaning yet. Every
    caller must treat that as "cannot decide" -- never as "global".
    """

    registry = fetch_country_registry(conn, project_id=project_id)
    if registry is None:
        return None
    pinned = version_id or registry["current_version_id"]
    if not pinned:
        return None
    version = require_version(conn, project_id=project_id, version_id=pinned)
    vocabulary = fetch_vocabulary_version(
        conn, vocabulary_version_id=version["vocabulary_version_id"]
    )
    if vocabulary is None:
        raise MasterDataConflict("the pinned Country vocabulary version is unreadable")
    node_labels = {
        str(node_id): str(label)
        for node_id, label in ((version.get("payload") or {}).get("node_labels") or {}).items()
    }
    rest_of_world = (version.get("payload") or {}).get("rest_of_world") or {}
    version_node_ids = set(node_labels)
    if rest_of_world.get("node_id"):
        version_node_ids.add(str(rest_of_world["node_id"]))
    version_nodes = []
    for item in list_nodes(
        conn, project_id=project_id, registry_id=registry["id"], include_archived=True
    ):
        node = dict(item)
        node_id = str(node.get("id"))
        if version_node_ids and node_id not in version_node_ids:
            continue
        if node_id in node_labels:
            node["label"] = node_labels[node_id]
        version_nodes.append(node)
    return build_projection(
        hierarchy_version_id=version["id"],
        vocabulary_version_id=version["vocabulary_version_id"],
        registry_id=registry["id"],
        memberships=fetch_memberships(conn, project_id=project_id, version_id=pinned),
        nodes=version_nodes,
        canonical_values=[str(entry["code"]) for entry in vocabulary["entries"]],
        rest_of_world=rest_of_world,
        as_of=as_of,
    )
