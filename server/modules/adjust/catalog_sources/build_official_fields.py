"""Deterministic builder for adjust official_fields.json (playbook section 2).

This script is the AUDITABLE SOURCE for the curated official snapshot. It is
committed alongside its output (official_fields.json) so a reviewer can see
exactly how the curated list was derived from the official Adjust references
recorded in catalog_sources.json.

It is pure-Python, deterministic (no network, no clock), and byte-stable:
running it again reproduces official_fields.json exactly.

Derivation summary (see catalog_sources.json "_derivation"):
  - METRICS come from the official Datascape metrics glossary
    (help.adjust.com/en/article/datascape-metrics-glossary). The article body
    is committed verbatim as glossary_body.md (fetched from the site's data
    payload; the HTML shell is JS-rendered so the body markdown IS the
    authoritative snapshot). Each glossary table row carries the API Metric ID
    used in the Report Service ``metrics=`` query parameter.
  - RANGE rows ("conversion_1 to conversion_6") are EXPANDED per the official
    notation into one field per integer in the range, matching how the API
    accepts each id individually.
  - TEMPLATED ids ({event_slug}_..., ..._{cohort_period},
    {event_from}_to_{event_to}) are QUERY TEMPLATES parameterised by
    account-specific event slugs or cohort periods. They are NOT emitted as
    catalog fields (enumerating them would be non-deterministic against the
    account's data) -- the same decision as meta-ads' breakdown-value
    non-enumeration. They are counted and reported at build time.
  - MALFORMED ids in the official doc (a "general " prefix containing a space,
    e.g. "general revenue_events_min") are skipped and reported: inventing a
    corrected id would break the catalog-is-the-contract rule.
  - DIMENSIONS come from the official Report Service API reports endpoint
    reference (dev.adjust.com/en/api/rs-api/reports, Dimensions table),
    curated inline below. The catalog id ``date`` maps to the provider token
    ``day`` (source_field), mirroring meta-ads date -> date_start.

Run (orchestrator, local only):
    uv run python server/modules/adjust/catalog_sources/build_official_fields.py
"""

from __future__ import annotations

import json
import re
from pathlib import Path

_HERE = Path(__file__).parent

# ---------------------------------------------------------------------------
# Sections used by the section_tier_map (glossary section -> catalog section).
# ---------------------------------------------------------------------------
_SECTION_BY_GLOSSARY_TITLE = {
    "Conversion metrics": "CONVERSION",
    "Cohort metrics": "COHORT",
    "Ad Spend metrics": "AD SPEND",
    "Revenue metrics": "REVENUE",
    "SKAdNetwork metrics": "SKAD",
    "Subscription metrics": "SUBSCRIPTION",
    "Fraud metrics": "FRAUD",
    "Assist metrics": "ASSIST",
    "InSight metrics": "INSIGHT",
}

# ---------------------------------------------------------------------------
# data_type heuristics (deterministic keyword rules; the glossary does not
# carry a per-metric type column). Order matters: first match wins.
# ---------------------------------------------------------------------------
_CURRENCY_PATTERNS = (
    re.compile(r"(^|_)cost($|_)"),
    re.compile(r"^ecp[imca]"),
    re.compile(r"(^|_)ecp[imc](_|$)"),
    re.compile(r"revenue"),
    re.compile(r"lifetime_value"),
    re.compile(r"gross_profit"),
    re.compile(r"(^|_)rpu(_|$)"),
    re.compile(r"(^|_)ecpa($|_)"),
    re.compile(r"(^|_)rpm(_|$)"),
    re.compile(r"^arpdau"),
    re.compile(r"ad_spend"),
)
_DECIMAL_PATTERNS = (
    re.compile(r"_rate($|_)"),
    re.compile(r"^ctr$"),
    re.compile(r"roas"),
    re.compile(r"(^|_)roi($|_)"),
    re.compile(r"^return_on_investment$"),
    re.compile(r"revenue_to_cost"),
    re.compile(r"_per_"),
)


def _metric_data_type(field_id: str) -> str:
    for pat in _CURRENCY_PATTERNS:
        if pat.search(field_id):
            return "currency"
    for pat in _DECIMAL_PATTERNS:
        if pat.search(field_id):
            return "decimal"
    return "integer"


# ---------------------------------------------------------------------------
# Official DIMENSIONS (Report Service reports endpoint reference, Dimensions
# table). (field_id, data_type, section, description[, source_field])
# ---------------------------------------------------------------------------
DIMENSIONS: list[tuple] = [
    ("date", "date", "TIME",
     "Reporting day (YYYY-MM-DD). Provider dimension token is 'day'.", "day"),
    ("hour", "datetime", "TIME", "Reporting hour (YYYY-MM-DDTHH:MM:SS with format_dates=false)."),
    ("week", "string", "TIME", "Reporting week range (YYYY-MM-DD - YYYY-MM-DD)."),
    ("month", "string", "TIME", "Reporting month (YYYY-MM)."),
    ("quarter", "string", "TIME", "Reporting quarter (Q<n> YYYY)."),
    ("year", "string", "TIME", "Reporting year (YYYY)."),
    ("app", "string", "STRUCTURE", "Name of the app."),
    ("app_token", "string", "STRUCTURE", "App ID (token) in the Adjust system."),
    ("ad_account_id", "string", "STRUCTURE", "The ID of the advertising account."),
    ("store_id", "string", "STRUCTURE", "Store App ID (e.g. com.random.app)."),
    ("store_type", "string", "STRUCTURE",
     "Store from where the app was installed (e.g. google_play)."),
    ("currency", "string", "STRUCTURE", "Currency name (e.g. Euro)."),
    ("currency_code", "string", "STRUCTURE", "3-character ISO 4217 currency code (e.g. EUR)."),
    ("network", "string", "STRUCTURE",
     "The name of the advertising network (e.g. Organic, AppLovin)."),
    ("campaign", "string", "STRUCTURE", "Link sub-level 1; usually contains campaign name and id."),
    ("campaign_network", "string", "STRUCTURE", "Campaign name from the network."),
    ("campaign_id_network", "string", "STRUCTURE", "Campaign ID from the network."),
    ("adgroup", "string", "STRUCTURE", "Link sub-level 2; usually contains adgroup name and id."),
    ("adgroup_network", "string", "STRUCTURE", "Adgroup name from the network."),
    ("adgroup_id_network", "string", "STRUCTURE", "Adgroup ID from the network."),
    ("source_network", "string", "STRUCTURE",
     "Name of the source network (optional, network-dependent)."),
    ("source_id_network", "string", "STRUCTURE", "ID of the source network."),
    ("creative", "string", "STRUCTURE", "Link sub-level 3; usually contains creative name and id."),
    ("creative_network", "string", "STRUCTURE", "Creative name from the network."),
    ("creative_id_network", "string", "STRUCTURE", "Creative ID from the network."),
    ("country", "string", "STRUCTURE", "Country name (e.g. United States of America)."),
    ("country_code", "string", "STRUCTURE", "2-character ISO 3166 country code (e.g. US)."),
    ("region", "string", "STRUCTURE", "Business region (e.g. APAC)."),
    ("partner_name", "string", "STRUCTURE", "Partner's name in the Adjust system (e.g. AppLovin)."),
    ("partner_id", "string", "STRUCTURE", "Partner's id in the Adjust system."),
    ("partner", "string", "STRUCTURE", "The unique slug of the partner (e.g. applovin)."),
    ("os_name", "string", "STRUCTURE", "Operating system name (android, ios, ...)."),
    ("device_type", "string", "STRUCTURE", "Device type (phone, tablet, tv, ...)."),
    ("channel", "string", "STRUCTURE", "A combination of partner_name and network."),
    ("platform", "string", "STRUCTURE", "The device platform type (mobile_app, web, undefined)."),
]

_RE_ROW = re.compile(
    r"<tr>\s*<th>(.*?)</th>\s*<td>(.*?)</td>\s*<td>(.*?)</td>\s*<td>(.*?)</td>\s*</tr>",
    flags=re.S,
)
_RE_RANGE = re.compile(r"^([a-z0-9_]+_)(\d+)\s+to\s+([a-z0-9_]+_)(\d+)$")
_RE_CONCRETE = re.compile(r"^[a-z0-9_]+$")


def _clean(cell: str) -> str:
    text = re.sub(r"<[^>]+>", "", cell)
    for entity, char in (("&nbsp;", " "), ("&amp;", "&"), ("&gt;", ">"), ("&lt;", "<")):
        text = text.replace(entity, char)
    return re.sub(r"\s+", " ", text).strip()


def build() -> dict:
    body = (_HERE / "glossary_body.md").read_text(encoding="utf-8")
    parts = re.split(r"^##\s+\[?([^\]\n(]+)\]?.*$", body, flags=re.M)

    fields: list[dict] = []
    seen: set[str] = set()
    skipped_templates: list[str] = []
    skipped_malformed: list[str] = []
    duplicates: list[str] = []

    def _emit_metric(field_id: str, section: str, label: str, definition: str) -> None:
        if field_id in seen:
            duplicates.append(field_id)
            return
        seen.add(field_id)
        description = definition if definition and definition != "-" else label
        fields.append(
            {
                "field_id": field_id,
                "kind": "metric",
                "data_type": _metric_data_type(field_id),
                "section": section,
                "description": description,
            }
        )

    for i in range(1, len(parts), 2):
        title = parts[i].strip()
        section = _SECTION_BY_GLOSSARY_TITLE.get(title)
        if section is None:
            continue
        for row in _RE_ROW.finditer(parts[i + 1]):
            label = _clean(row.group(1))
            definition = _clean(row.group(2))
            raw_id = _clean(row.group(4))

            m_range = _RE_RANGE.match(raw_id)
            if m_range:
                prefix_a, start, prefix_b, end = m_range.groups()
                if prefix_a != prefix_b:
                    skipped_malformed.append(raw_id)
                    continue
                for n in range(int(start), int(end) + 1):
                    _emit_metric(f"{prefix_a}{n}", section, label, definition)
                continue

            if "{" in raw_id or "}" in raw_id:
                skipped_templates.append(raw_id)
                continue
            if not _RE_CONCRETE.match(raw_id):
                skipped_malformed.append(raw_id)
                continue
            _emit_metric(raw_id, section, label, definition)

    for entry in DIMENSIONS:
        field_id, data_type, section, description = entry[0], entry[1], entry[2], entry[3]
        record = {
            "field_id": field_id,
            "kind": "dimension",
            "data_type": data_type,
            "section": section,
            "description": description,
        }
        if len(entry) == 5:
            record["source_field"] = entry[4]
        fields.append(record)

    return {
        "_note": (
            "Curated official snapshot for the adjust connector. Generated by "
            "build_official_fields.py from glossary_body.md (Datascape metrics "
            "glossary) + the Report Service reports endpoint Dimensions table. "
            "Do not edit by hand."
        ),
        "_skipped_templated_ids": sorted(set(skipped_templates)),
        "_skipped_malformed_ids": sorted(set(skipped_malformed)),
        "_duplicate_glossary_ids": sorted(set(duplicates)),
        "fields": fields,
    }


def main() -> None:
    result = build()
    out = _HERE / "official_fields.json"
    out.write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    metrics = sum(1 for f in result["fields"] if f["kind"] == "metric")
    dims = sum(1 for f in result["fields"] if f["kind"] == "dimension")
    print(f"official_fields.json written: {metrics} metrics, {dims} dimensions")
    print(
        f"skipped: {len(result['_skipped_templated_ids'])} templated ids, "
        f"{len(result['_skipped_malformed_ids'])} malformed ids, "
        f"{len(result['_duplicate_glossary_ids'])} duplicate ids"
    )


if __name__ == "__main__":
    main()
