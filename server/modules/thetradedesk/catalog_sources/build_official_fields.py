"""Build official_fields.json (+ generated compat blocks) for thetradedesk.

Story 28.2. The Trade Desk MyReports (REST v3) has NO machine-readable field
enum: the Partner Portal is a client-side-rendered SPA (WebFetch + r.jina.ai
both return empty -- same failure mode as the amazon-ads docs site). The
field-vocabulary AUTHORITY is therefore the Supermetrics catalog
(docs.supermetrics.com/docs/the-trade-desk-fields.md, 2026-04-23), transcribed
VERBATIM (275 fields = 164 dimensions + 111 metrics, 22 sections, ZERO
truncation) into Annex A of the committed research dossier
`_bmad-output/implementation-artifacts/research/thetradedesk-catalog-research.md`.

STATUS: DRAFT. The true field authority is the live ReportTemplate facet enum
(GET /v3/myreports/reportschedule/facets), transcribed at probe time and diffed
against this Supermetrics baseline (e.g. 'Selling party name/ID' superseding
'SupplyVendor'). See ROLLOUT_NOTES.md + catalog_sources.json `_status`.

This script is DETERMINISTIC and LOCAL-ONLY (no network). It parses:
  - Annex A -> the 275-field catalog (api_id, kind, physical type, description,
               section). Sections DATASOURCE (3) + QUERY (9) are Supermetrics
               connector plumbing -> excluded: enrichment-only. DEPRECATED (10)
               + LEGACY (4) are real TTD columns kept for history -> exposed
               with a deprecated note.

and emits, next to itself:
  - official_fields.json        input for scripts/build_api_catalog.py
  - template_columns.json        report_template -> sorted column list
  - and REWRITES two generated blocks inside catalog_sources.json:
      * field_compatibility.rules  (core schema_version "1" selectable_set rules,
                                    one per managed ReportTemplate)
      * excluded_fields            (the 12 enrichment-only DATASOURCE/QUERY ids)

Run:  uv run python server/modules/thetradedesk/catalog_sources/build_official_fields.py
Then: uv run python scripts/build_api_catalog.py --module thetradedesk \
          --sources-dir server/modules/thetradedesk/catalog_sources \
          --report server/modules/thetradedesk/catalog_sources/fusion-report.json

ASCII-only stdout (AI-03).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parents[3]
_DEFAULT_DOSSIER = (
    _REPO_ROOT
    / "_bmad-output"
    / "implementation-artifacts"
    / "research"
    / "thetradedesk-catalog-research.md"
)

# ---------------------------------------------------------------------------
# Section vocabulary (Annex A headers, verbatim). DATASOURCE + QUERY are
# Supermetrics connector plumbing (excluded: enrichment-only). DEPRECATED +
# LEGACY are real TTD columns kept for history (exposed + deprecated note).
# ---------------------------------------------------------------------------

ENRICHMENT_ONLY_SECTIONS = {"DATASOURCE", "QUERY"}
DEPRECATED_SECTIONS = {"DEPRECATED", "LEGACY"}

REASON_ENRICHMENT_ONLY = (
    "enrichment-only: Supermetrics connector run metadata "
    "(system_metadata.* / dataSourceName), NOT a TTD MyReports column. Never "
    "emitted to the manifest or a report body."
)

# NON-ADDITIVE ratio/derived metrics (AD-4): stored raw per row, recomputed in
# the semantic layer from stored numerator (cost) + denominator. The dossier
# section 3.2 pins EXACTLY these seven. NOTE: PlayerRewind matched a naive
# "Rewind" ratio heuristic but is an ADDITIVE count -- it is NOT in this set
# (documented so the generator never mis-tags it).
NON_ADDITIVE_METRICS = frozenset(
    {"Cpm", "Cpc", "Ctr", "CpaClick", "CpaView", "CpaTouch", "CpaTimeWeightedDecay"}
)

# ReportTemplate profiles (dossier section 8). Each == one managed template;
# the field_compat selectable_set rule scopes {"report_template": <id>} and the
# allowed_fields = that template's legal dimension+metric set. These are
# RECOMMENDED baselines; the LIVE facet enum is authority and confirms/extends
# them at probe time (catalog_sources `_status` DRAFT + ROLLOUT_NOTES).
REPORT_TEMPLATES: dict[str, dict[str, list[str]]] = {
    "daily_performance": {
        "dimensions": ["date", "AdvertiserId", "CampaignId", "AdGroupId"],
        "metrics": [
            "Impressions", "Clicks", "Bids", "AdvertiserCostAdvCurrency",
            "AdvertiserCostUSD", "TtdCostUsd", "PartnerCostUsd",
            "TotalClickConversions", "TotalViewThroughConversions",
        ],
    },
    "conversions": {
        "dimensions": ["date", "AdvertiserId", "CampaignId"],
        "metrics": [
            f"{family}{idx:02d}{suffix}"
            for family in (
                "ClickConversion", "ConversionTouch", "TimeWeightedDecayConversion",
                "ViewThroughConversion",
            )
            for idx in range(1, 7)
            for suffix in ("", "Revenue")
        ],
    },
    "creative": {
        "dimensions": [
            "date", "CampaignId", "AdGroupId", "CreativeId", "CreativeName",
            "AdFormat",
        ],
        "metrics": [
            "Impressions", "Clicks", "AdvertiserCostAdvCurrency",
            "TotalClickConversions",
        ],
    },
    "geo_platform": {
        "dimensions": [
            "date", "Country", "CountryCode", "Region", "Metro", "City",
            "DeviceType", "Browser", "OperatingSystem",
        ],
        "metrics": ["Impressions", "Clicks", "AdvertiserCostAdvCurrency"],
    },
    "video_player": {
        "dimensions": ["date", "AdGroupId", "CreativeId"],
        "metrics": [
            "Player25Complete", "Player50Complete", "Player75Complete",
            "PlayerClose", "PlayerCollapse", "PlayerCompletedViews",
            "PlayerEngagedViews", "PlayerErrors", "PlayerExpansion",
            "PlayerFullscreen", "PlayerInvitationAccept", "PlayerMute",
            "PlayerPause", "PlayerResume", "PlayerRewind", "PlayerSkip",
            "PlayerStarts", "PlayerUnmute", "PlayerViews",
        ],
    },
}

# Physical-type normalization (Supermetrics data-type tokens -> catalog enum).
# The live report CSV / probe is authority for coercion; here we transpose the
# Supermetrics data_type column verbatim, mapped to the catalog physical_type
# enum via merge.map_physical_type at fusion. This map is ONLY the raw token
# passed through as OfficialField.data_type.
_TYPE_NORMALIZE = {
    "string": "string",
    "decimal": "decimal",
    "integer": "integer",
    "date": "date",
    "datetime": "datetime",
}

_DATE_FIELDS = {"date", "today"}

# Annex A line: "- `api_id` (kind, physical type) — description"
# (em dash U+2014). api_id may contain dots (system_metadata.*), digits, etc.
_ANNEX_LINE = re.compile(r"^- `([^`]+)` \(([a-z]+), ([a-z]+)\)(?: \[NON-ADDITIVE\])? [—-] (.+)$")
_SECTION_HEADER = re.compile(r"^### ([A-Z0-9 &]+) \(\d+ dim / \d+ met\)$")


def parse_annex_a(text: str) -> list[dict]:
    """Parse Annex A -> ordered list of {field_id, kind, raw_type, section, desc}.

    A list line inside the Annex A range that the parser cannot read is a
    FATAL, exactly like amazon-ads F-5 -- never a silent drop of a column.
    """
    lines = text.split("\n")
    try:
        start = next(i for i, ln in enumerate(lines) if ln.startswith("## Annex A"))
    except StopIteration as exc:  # pragma: no cover - dossier contract
        raise SystemExit("FATAL: Annex A marker not found in the dossier") from exc

    fields: list[dict] = []
    section: str | None = None
    for line in lines[start:]:
        header = _SECTION_HEADER.match(line.strip())
        if header:
            section = header.group(1).strip()
            continue
        stripped = line.strip()
        if not stripped.startswith("- "):
            continue
        m = _ANNEX_LINE.match(stripped)
        if not m:
            raise SystemExit(
                "FATAL: Annex A list line does not match the field parser (fix "
                "the dossier line or the regex, never drop silently): "
                + stripped[:160]
            )
        if section is None:
            raise SystemExit(
                "FATAL: Annex A field line before any section header: " + stripped[:160]
            )
        api_id, kind, raw_type, desc = (
            m.group(1), m.group(2), m.group(3), m.group(4)
        )
        fields.append(
            {
                "field_id": api_id,
                "kind": kind,
                "raw_type": raw_type,
                "section": section,
                "desc": desc,
            }
        )
    return fields


def build(dossier_path: Path) -> None:
    text = dossier_path.read_text(encoding="utf-8")
    parsed = parse_annex_a(text)

    total = len(parsed)
    dims = sum(1 for f in parsed if f["kind"] == "dimension")
    mets = sum(1 for f in parsed if f["kind"] == "metric")
    print(f"annex_a_fields_total={total}")
    print(f"dimensions={dims}")
    print(f"metrics={mets}")

    # Duplicate-id guard (Annex A must be unique).
    seen: set[str] = set()
    for f in parsed:
        if f["field_id"] in seen:
            raise SystemExit(f"FATAL: duplicate field_id in Annex A: {f['field_id']}")
        seen.add(f["field_id"])

    official: list[dict] = []
    excluded_fields: dict[str, str] = {}
    exposure_counts = {"exposed": 0, "excluded_enrichment_only": 0}

    for f in sorted(parsed, key=lambda x: x["field_id"].lower()):
        name = f["field_id"]
        section = f["section"]
        raw_type = f["raw_type"]
        physical = "date" if name in _DATE_FIELDS else _TYPE_NORMALIZE.get(
            raw_type.strip().lower(), "string"
        )
        # Kind is authoritative from Annex A (metric | dimension).
        kind = f["kind"]

        desc = (
            f"The Trade Desk MyReports column '{name}' ({raw_type}, Supermetrics "
            "catalog 2026-04-23). Section: " + section + "."
        )
        if section in ENRICHMENT_ONLY_SECTIONS:
            desc = (
                f"Supermetrics connector plumbing '{name}' ({raw_type}) -- run "
                "metadata (" + section + " section), NOT a TTD MyReports column. "
                "Excluded: enrichment-only."
            )
        elif section in DEPRECATED_SECTIONS:
            desc += (
                " DEPRECATED/LEGACY TTD column: still queryable for historical "
                "windows (data before the deprecation date). For newer data "
                "prefer the superseding column (e.g. 'Selling party name/ID' "
                "replaces SupplyVendor -- probe-to-confirm facet)."
            )
        if name in NON_ADDITIVE_METRICS and kind == "metric":
            desc += (
                " Provider-computed ratio/derived metric: NON-ADDITIVE (AD-4) -- "
                "never SUM across rows; recompute at the semantic layer from "
                "stored numerator (cost) and denominator "
                "(impressions/clicks/conversions)."
            )

        if section in ENRICHMENT_ONLY_SECTIONS:
            excluded_fields[name] = REASON_ENRICHMENT_ONLY
            exposure_counts["excluded_enrichment_only"] += 1
        else:
            exposure_counts["exposed"] += 1

        official.append(
            {
                "field_id": name,
                "source_field": name,
                "kind": kind,
                "data_type": physical,
                "description": desc,
                "section": section,
            }
        )

    print("exposure_plan=" + json.dumps(exposure_counts, sort_keys=True))

    (_HERE / "official_fields.json").write_text(
        json.dumps(official, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )

    # template_columns.json: report_template -> sorted union of dims + metrics.
    template_columns: dict[str, list[str]] = {
        tid: sorted(set(spec["dimensions"]) | set(spec["metrics"]))
        for tid, spec in REPORT_TEMPLATES.items()
    }
    (_HERE / "template_columns.json").write_text(
        json.dumps(template_columns, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )

    # ------------------------------------------------------------------ #
    # catalog_sources.json: regenerate the GENERATED blocks in place.     #
    # ------------------------------------------------------------------ #
    sources_path = _HERE / "catalog_sources.json"
    cfg = json.loads(sources_path.read_text(encoding="utf-8"))

    official_ids = {f["field_id"] for f in official}
    rules: list[dict] = []
    for tid in sorted(REPORT_TEMPLATES):
        cols = template_columns[tid]
        missing = sorted(c for c in cols if c not in official_ids)
        if missing:
            raise SystemExit(
                f"FATAL: ReportTemplate {tid!r} references column(s) absent from "
                "Annex A (fix the template or the dossier, never drop silently): "
                + ", ".join(missing)
            )
        rules.append(
            {
                "id": f"selectable_{tid}",
                "kind": "selectable_set",
                "scope": {"report_template": tid},
                "allowed_fields": cols,
                "_source": (
                    "recommended managed ReportTemplate columns (dossier section "
                    "8); the LIVE facet enum is authority and must confirm/extend "
                    "this at probe time"
                ),
            }
        )

    cfg["field_compatibility"]["rules"] = rules
    cfg["excluded_fields"] = excluded_fields

    # Attach the generated column sets to report_template_compatibility.
    for tid, spec in cfg["report_template_compatibility"]["templates"].items():
        spec["columns_ref"] = "template_columns.json"

    sources_path.write_text(
        json.dumps(cfg, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print("wrote official_fields.json, template_columns.json, catalog_sources.json")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dossier", type=Path, default=_DEFAULT_DOSSIER)
    args = parser.parse_args(argv)
    build(args.dossier)
    return 0


if __name__ == "__main__":
    sys.exit(main())
