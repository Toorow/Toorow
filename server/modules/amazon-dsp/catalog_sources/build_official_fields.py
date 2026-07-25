"""Generate the autonomous 541-field Amazon DSP Reporting v3 catalog."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[3]
DEFAULT_DOSSIER = (
    REPO_ROOT
    / "_bmad-output"
    / "implementation-artifacts"
    / "research"
    / "amazon-ads-catalog-research.md"
)
DSP_TYPES = (
    "dspCampaign",
    "dspAudioAndVideo",
    "dspAudience",
    "dspBidAdjustment",
    "dspBrandSuitability",
    "dspGeo",
    "dspInventory",
    "dspProduct",
    "dspReachFrequency",
    "dspTech",
    "dspBenchmarks",
)
EXPECTED_UNIQUE_FIELDS = 541
CORE_EXPOSED = {
    "date",
    "intervalStart",
    "intervalEnd",
    "advertiserId",
    "impressions",
    "clicks",
    "totalCost",
    "purchases",
    "sales",
}
TYPE_MAP = {
    "integer": "integer",
    "int": "integer",
    "long": "integer",
    "decimal": "decimal",
    "deciimal": "decimal",
    "deciaml": "decimal",
    "string": "string",
    "boolean": "boolean",
}
DATE_FIELDS = {"date", "startDate", "endDate", "intervalStart", "intervalEnd"}
DIMENSION_SUFFIXES = ("id", "name", "type", "status", "country", "region", "city")


def parse_dictionary(text: str) -> dict[str, dict]:
    marker = "## Annex B"
    if marker not in text:
        raise SystemExit("FATAL: Annex B not found in curated Amazon transcription")
    result: dict[str, dict] = {}
    pattern = re.compile(r"^- `([^`]+)` \(([^)]+)\) .*? (.+)$")
    for line in text.split(marker, 1)[1].splitlines():
        match = pattern.match(line.strip())
        if not match:
            continue
        name, raw_type, availability_text = match.groups()
        availability = {item.strip() for item in availability_text.split(",")}
        dsp_membership = sorted(availability.intersection(DSP_TYPES))
        if not dsp_membership:
            continue
        if name in result:
            raise SystemExit(f"FATAL: duplicate DSP dictionary field: {name}")
        result[name] = {"raw_type": raw_type, "report_types": dsp_membership}
    if len(result) != EXPECTED_UNIQUE_FIELDS:
        raise SystemExit(
            f"FATAL: Amazon DSP field count drift: {len(result)} != {EXPECTED_UNIQUE_FIELDS}"
        )
    ghost = CORE_EXPOSED - result.keys()
    if ghost:
        raise SystemExit(
            "FATAL: CORE_EXPOSED contains fields absent from the 541-field "
            f"dictionary: {sorted(ghost)}"
        )
    return result


def _physical_type(name: str, raw_type: str) -> str:
    if name in DATE_FIELDS:
        return "date"
    return TYPE_MAP.get(raw_type.strip().lower(), "string")


def _kind(name: str, physical_type: str) -> str:
    lowered = name.lower()
    if physical_type in {"date", "string", "boolean"}:
        return "dimension"
    if lowered.endswith(DIMENSION_SUFFIXES) or lowered in {"entityid", "frequencygroupid"}:
        return "dimension"
    return "metric"


def _section(name: str, kind: str) -> str:
    lowered = name.lower()
    if kind == "dimension":
        return "IDENTIFIERS"
    if any(token in lowered for token in ("cost", "fee", "spend", "cpc", "cpm")):
        return "COST"
    if any(token in lowered for token in ("purchase", "sale", "conversion", "roas")):
        return "CONVERSIONS"
    if "video" in lowered:
        return "VIDEO"
    if "audio" in lowered:
        return "AUDIO"
    if any(token in lowered for token in ("viewable", "measurable", "invalid")):
        return "VIEWABILITY"
    return "DELIVERY"


def build(dossier: Path = DEFAULT_DOSSIER) -> None:
    fields = parse_dictionary(dossier.read_text(encoding="utf-8"))
    official = []
    by_report_type = {report_type: [] for report_type in DSP_TYPES}
    excluded: dict[str, str] = {}
    for name in sorted(fields, key=str.lower):
        meta = fields[name]
        physical_type = _physical_type(name, meta["raw_type"])
        kind = _kind(name, physical_type)
        for report_type in meta["report_types"]:
            by_report_type[report_type].append(name)
        non_additive = any(
            token in name.lower()
            for token in ("rate", "ratio", "average", "reach", "frequency", "roas", "benchmark")
        )
        description = (
            f"Amazon DSP Reporting v3 column '{name}' ({meta['raw_type']}); report types: "
            + ", ".join(meta["report_types"])
            + "."
        )
        if non_additive:
            description += " Provider-computed non-additive value; never SUM across rows."
        official.append(
            {
                "field_id": name,
                "source_field": name,
                "kind": kind,
                "data_type": physical_type,
                "section": _section(name, kind),
                "description": description,
            }
        )
        if name not in CORE_EXPOSED:
            excluded[name] = (
                "probe-to-confirm: DSP seat required for physical type and compatibility evidence"
            )

    by_report_type = {key: sorted(value) for key, value in by_report_type.items()}
    (HERE / "official_fields.json").write_text(
        json.dumps(official, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n"
    )
    (HERE / "report_type_columns.json").write_text(
        json.dumps(by_report_type, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    config_path = HERE / "catalog_sources.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["excluded_fields"] = excluded
    config["field_compatibility"]["rules"] = [
        {
            "id": f"selectable_{report_type}",
            "kind": "selectable_set",
            "scope": {"report_type": report_type},
            "allowed_fields": columns,
        }
        for report_type, columns in by_report_type.items()
        if columns
    ]
    for report_type, spec in config["report_type_compatibility"]["report_types"].items():
        if report_type not in DSP_TYPES:
            raise SystemExit(f"FATAL: unknown Amazon DSP report type: {report_type}")
        spec["columns_ref"] = "report_type_columns.json"
    config_path.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(f"Wrote {len(official)} autonomous Amazon DSP fields")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dossier", type=Path, default=DEFAULT_DOSSIER)
    args = parser.parse_args()
    build(args.dossier)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
