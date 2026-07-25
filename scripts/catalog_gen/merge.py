"""Merge engine: official + enrichment sources -> validated api_catalog.json.

Rules (AC4):
- Official source is AUTHORITY for field existence and kind.
- Enrichment contributes: description (when official has none), section.
- Enrichment-only candidates go to the fusion report (NEVER into the catalog).
- Manifest fields in source_capabilities.fields -> exposure "exposed"; others "planned".
- Manifest fields absent from official -> "drift" list in report.
- tier comes from section->tier mapping in catalog_sources.json (default "standard"),
  with optional per-field override.
- generated_at / fetched_at come from the source config, never from the clock.
- Output is byte-deterministic: sorted fields, sort_keys=True, LF newlines,
  trailing newline.
- Output MUST pass validate_catalog_schema with zero issues.

physical_type mapping (Supermetrics data_type tokens -> schema physical_type enum):
  See PHYSICAL_TYPE_MAP below.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# sys.path bootstrap so this module can be imported from scripts/ or tests/
# (follows the pattern in scripts/export_connector_registry.py)
# ---------------------------------------------------------------------------
_REPO_ROOT = Path(__file__).resolve().parents[2]
_SERVER_DIR = _REPO_ROOT / "server"
if str(_SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(_SERVER_DIR))

from core.catalog_contract import validate_catalog_schema  # noqa: E402

# ---------------------------------------------------------------------------
# physical_type mapping table (enrichment data_type -> catalog physical_type)
# ---------------------------------------------------------------------------
# Rules:
#   - Tokens containing "currency" or "decimal" -> "decimal"
#   - Tokens starting with "int" or containing "integer" -> "integer"
#   - Tokens == "date" or starting with "date." (but not "datetime") -> "date"
#   - Tokens == "datetime" or containing "timestamp" -> "datetime"
#   - Tokens == "boolean" or "bool" -> "boolean"
#   - Tokens == "json" or "object" or "array" -> "json"
#   - Default -> "string"
PHYSICAL_TYPE_MAP: dict[str, str] = {
    # Currency / decimal
    "currency": "decimal",
    "decimal": "decimal",
    "float": "decimal",
    "double": "decimal",
    "number": "decimal",
    "percent": "decimal",
    "percentage": "decimal",
    "ratio": "decimal",
    # Integer
    "int": "integer",
    "integer": "integer",
    "long": "integer",
    "short": "integer",
    "byte": "integer",
    "count": "integer",
    # Date / datetime
    "date": "date",
    "datetime": "datetime",
    "timestamp": "datetime",
    # Boolean
    "boolean": "boolean",
    "bool": "boolean",
    # JSON / structured
    "json": "json",
    "object": "json",
    "array": "json",
    # String (explicit)
    "string": "string",
    "text": "string",
    "enum": "string",
    "id": "string",
}

# Allowed values per the schema enum
_VALID_PHYSICAL_TYPES = frozenset(
    ["string", "integer", "decimal", "boolean", "date", "datetime", "json"]
)
_VALID_TIERS = frozenset(["core", "standard", "advanced"])
_VALID_EXPOSURES = frozenset(["exposed", "excluded", "planned"])


def map_physical_type(data_type: str) -> str:
    """Map a Supermetrics/discovery data_type token to a catalog physical_type.

    Matching is case-insensitive. First checks exact match, then prefix/infix rules.
    Default is "string".
    """
    if not data_type:
        return "string"

    token = data_type.lower().strip()

    # Exact match first
    if token in PHYSICAL_TYPE_MAP:
        return PHYSICAL_TYPE_MAP[token]

    # Infix rules (order matters: check more-specific first)
    if "currency" in token or "decimal" in token:
        return "decimal"
    if "timestamp" in token or "datetime" in token:
        return "datetime"
    if token.startswith("date"):
        return "date"
    if "integer" in token or token.startswith("int"):
        return "integer"
    if "boolean" in token or "bool" in token:
        return "boolean"
    if "float" in token or "double" in token or "percent" in token:
        return "decimal"

    return "string"


# ---------------------------------------------------------------------------
# Official field input (from any parser or hand-maintained list)
# ---------------------------------------------------------------------------

@dataclass
class OfficialField:
    """One field from the authority source.

    ``source_field`` is the provider-side field name the connector actually
    requests/parses. It defaults to ``field_id`` and is only distinct when the
    catalog field id differs from the provider token (e.g. Meta ``date`` ->
    ``date_start``). Emitted verbatim into the catalog entry's ``source_field``
    so the manifest<->catalog gate does not report ``source_field_mismatch``
    (Story 25.4 AC2).
    """

    field_id: str
    kind: str  # "metric" | "dimension"
    data_type: str = "string"
    description: str | None = None
    section: str = "UNKNOWN"
    source_field: str | None = None


@dataclass
class EnrichmentField:
    """One candidate from an enrichment source (e.g. Supermetrics)."""

    field_id: str
    kind: str
    data_type: str
    description: str | None
    section: str
    deprecated: bool = False


# ---------------------------------------------------------------------------
# Fusion report
# ---------------------------------------------------------------------------

@dataclass
class FusionReport:
    """Counts and candidate lists produced by the merge engine."""

    official_total: int = 0
    enrichment_total: int = 0
    matched: int = 0
    official_only: int = 0
    enrichment_only: int = 0
    enrichment_only_ids: list[str] = field(default_factory=list)
    drift_ids: list[str] = field(default_factory=list)  # manifest fields not in official
    official_duplicate_ids: list[str] = field(default_factory=list)
    exposure_counts: dict[str, int] = field(default_factory=dict)
    tier_counts: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "official_total": self.official_total,
            "enrichment_total": self.enrichment_total,
            "matched": self.matched,
            "official_only": self.official_only,
            "enrichment_only": self.enrichment_only,
            "enrichment_only_ids": sorted(self.enrichment_only_ids),
            "drift_ids": sorted(self.drift_ids),
            "official_duplicate_ids": sorted(self.official_duplicate_ids),
            "exposure_counts": dict(sorted(self.exposure_counts.items())),
            "tier_counts": dict(sorted(self.tier_counts.items())),
        }


# ---------------------------------------------------------------------------
# Main merge function
# ---------------------------------------------------------------------------

def merge(
    official_fields: list[OfficialField],
    enrichment_fields: list[EnrichmentField],
    manifest: dict[str, Any],
    sources_config: dict[str, Any],
) -> tuple[dict[str, Any], FusionReport]:
    """Merge official + enrichment into a validated catalog dict.

    Parameters
    ----------
    official_fields:
        Fields from the authority source (existence + kind are ground truth).
    enrichment_fields:
        Fields from enrichment sources (contribute description/section only).
    manifest:
        The module's manifest.json dict (used to derive exposure).
    sources_config:
        Per-module catalog_sources.json dict.  Expected keys:
        - ``connector`` (str): connector name (e.g. "facebook-ads")
        - ``api_version`` (str): e.g. "v21.0"
        - ``generated_at`` (str): ISO timestamp (from config, never from clock)
        - ``sources`` (list): list of source dicts with url/kind/fetched_at
        - ``section_tier_map`` (dict[str, str]): section -> tier mapping
        - ``field_tier_overrides`` (dict[str, str]): field_id -> tier overrides
        - ``default_tier`` (str, optional): default tier when no section match
          (defaults to "standard")

    Returns
    -------
    (catalog_dict, fusion_report)
        catalog_dict passes validate_catalog_schema with zero issues.
    """
    report = FusionReport()

    # --- Enrich lookups ---
    enrich_by_id: dict[str, EnrichmentField] = {
        e.field_id: e for e in enrichment_fields
    }
    report.enrichment_total = len(enrichment_fields)

    # --- Manifest exposed fields ---
    source_caps = manifest.get("source_capabilities", {})
    manifest_field_ids: set[str] = {
        f["field_id"]
        for f in source_caps.get("fields", [])
        if f.get("field_id")
    }

    # --- Config ---
    section_tier_map: dict[str, str] = sources_config.get("section_tier_map", {})
    field_tier_overrides: dict[str, str] = sources_config.get("field_tier_overrides", {})
    default_tier: str = sources_config.get("default_tier", "standard")

    # Story 25.8: exposure policy ("manifest" default | "catalog_driven").
    exposure_policy: str = sources_config.get("exposure_policy", "manifest")
    if exposure_policy not in ("manifest", "catalog_driven"):
        raise ValueError(
            f"exposure_policy must be manifest|catalog_driven, got: {exposure_policy!r}"
        )
    excluded_sections: dict[str, str] = sources_config.get("excluded_sections", {})
    for _sec, _reason in excluded_sections.items():
        if not (_reason and str(_reason).strip()):
            raise ValueError(f"excluded_sections[{_sec!r}] requires a non-empty reason")
    # Story 25.9: field-level exclusions (finer than sections) -- same contract.
    excluded_fields: dict[str, str] = sources_config.get("excluded_fields", {})
    for _fid, _reason in excluded_fields.items():
        if not (_reason and str(_reason).strip()):
            raise ValueError(f"excluded_fields[{_fid!r}] requires a non-empty reason")
    # Epic 33 review: PII/sensitive fields declared in catalog_sources propagate a
    # machine-readable `sensitive: true` onto the catalog entry (governance/audit).
    sensitive_fields: set[str] = set(sources_config.get("sensitive_dimensions", []))

    # --- Dedup official fields (review F-B1): a hand-maintained official list
    # may repeat a field_id; keep the first occurrence, report the duplicates
    # (the schema does not enforce field_id uniqueness so we must).
    seen_ids: set[str] = set()
    deduped_official = []
    duplicate_ids: list[str] = []
    for f in official_fields:
        if f.field_id in seen_ids:
            duplicate_ids.append(f.field_id)
            continue
        seen_ids.add(f.field_id)
        deduped_official.append(f)
    official_fields = deduped_official
    report.official_duplicate_ids = sorted(set(duplicate_ids))

    # --- Official field IDs (for drift detection) ---
    official_ids: set[str] = {f.field_id for f in official_fields}
    report.official_total = len(official_fields)

    # --- Drift: manifest fields not present in official ---
    report.drift_ids = sorted(manifest_field_ids - official_ids)

    # --- Enrichment-only candidates (in enrichment but not in official) ---
    enrichment_only_ids = sorted(set(enrich_by_id.keys()) - official_ids)
    report.enrichment_only = len(enrichment_only_ids)
    report.enrichment_only_ids = enrichment_only_ids

    matched_count = len(set(enrich_by_id.keys()) & official_ids)
    report.matched = matched_count
    report.official_only = report.official_total - matched_count

    # --- Build catalog fields (official is authority) ---
    catalog_fields: list[dict[str, Any]] = []
    exposure_counts: dict[str, int] = {}
    tier_counts: dict[str, int] = {}

    for of in sorted(official_fields, key=lambda f: f.field_id):
        enrich = enrich_by_id.get(of.field_id)

        # Description: official first, then enrichment, then placeholder
        description = of.description
        if not description and enrich and enrich.description:
            description = enrich.description
        if not description:
            description = f"{of.field_id}"

        # Section: official first, then enrichment
        section = of.section
        if (not section or section == "UNKNOWN") and enrich:
            section = enrich.section
        if not section:
            section = "UNKNOWN"

        # physical_type: use official data_type, fall back to enrichment
        data_type = of.data_type or (enrich.data_type if enrich else "string")
        physical_type = map_physical_type(data_type)

        # Tier: field override > section map > default
        tier = field_tier_overrides.get(of.field_id)
        if not tier:
            tier = section_tier_map.get(section, default_tier)
        if tier not in _VALID_TIERS:
            tier = default_tier

        # Exposure (Story 25.8): policy-driven.
        #   "manifest" (default): exposed iff the field is in the module manifest
        #     (legacy exact_bundle extraction), everything else planned.
        #   "catalog_driven": the module executes selections against the whole
        #     catalog -> every field is exposed EXCEPT sections listed in
        #     excluded_sections (section -> mandatory reason), which become
        #     excluded. Manifest fields stay exposed regardless (extractable
        #     via the legacy profiles).
        exclusion_reason: str | None = None
        if exposure_policy == "catalog_driven" and of.field_id in excluded_fields:
            # Field-level exclusion wins over everything (even manifest fields:
            # a field can be legacy-extracted yet excluded from catalog_daily --
            # the reason documents it; e.g. gsc ctr ratio rule AD-4).
            exposure = "excluded"
            exclusion_reason = excluded_fields[of.field_id]
        elif of.field_id in manifest_field_ids:
            exposure = "exposed"
        elif exposure_policy == "catalog_driven":
            if section in excluded_sections:
                exposure = "excluded"
                exclusion_reason = excluded_sections[section]
            else:
                exposure = "exposed"
        else:
            exposure = "planned"

        # deprecated flag
        deprecated = False
        if enrich and enrich.deprecated:
            deprecated = True

        # source_field: provider-side token; defaults to field_id (25.4 AC2).
        source_field = of.source_field or of.field_id

        catalog_entry: dict[str, Any] = {
            "field_id": of.field_id,
            "source_field": source_field,
            "kind": of.kind,
            "physical_type": physical_type,
            "description": description,
            "section": section,
            "tier": tier,
            "exposure": exposure,
        }
        if exclusion_reason is not None:
            catalog_entry["exclusion_reason"] = exclusion_reason
        if deprecated:
            catalog_entry["deprecated"] = True
        if of.field_id in sensitive_fields:
            catalog_entry["sensitive"] = True

        catalog_fields.append(catalog_entry)

        exposure_counts[exposure] = exposure_counts.get(exposure, 0) + 1
        tier_counts[tier] = tier_counts.get(tier, 0) + 1

    report.exposure_counts = exposure_counts
    report.tier_counts = tier_counts

    # --- Assemble catalog ---
    # Review F-A1 (CRITICAL): config source entries carry loader-only keys
    # (file, parser, discovery_configs, default_section...) that the emitted
    # schema forbids (source_entry additionalProperties: false). Project ONLY
    # the emitted keys so a real loader config never fails output validation.
    emitted_sources = [
        {key: entry[key] for key in ("url", "kind", "fetched_at") if key in entry}
        for entry in sources_config["sources"]
    ]
    catalog: dict[str, Any] = {
        "catalog_version": "1",
        "connector": sources_config["connector"],
        "api_version": sources_config["api_version"],
        "sources": emitted_sources,
        "generated_at": sources_config["generated_at"],
        "fields": catalog_fields,
    }
    if exposure_policy != "manifest":
        # Story 25.9: stamp the policy so the diff contract knows the projection
        # is dynamic without needing the module config.
        catalog["exposure_policy"] = exposure_policy

    # --- Validate ---
    issues = validate_catalog_schema(catalog)
    if issues:
        msgs = "; ".join(f"{i.path}: {i.message}" for i in issues)
        raise ValueError(f"Generated catalog failed schema validation: {msgs}")

    return catalog, report


def serialize_catalog(catalog: dict[str, Any]) -> str:
    """Serialize catalog to a byte-deterministic JSON string (LF, trailing newline)."""
    return json.dumps(catalog, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
