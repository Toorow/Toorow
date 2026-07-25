"""Build the curated, deterministic CM360 v5 field snapshot.

Catalog contract (F-CAT-1):
  Every CM360 v5 dimension/metric that is NOT in FIELDS must appear in EXCLUDED_FIELDS
  with an explicit reason. This makes the catalog DIFFABLE against the official v5 reference
  (https://developers.google.com/doubleclick-advertisers/v5/dimensions) and ensures there
  are zero implicit 'planned' fields — every field is either exposed or excluded-with-reason.

Sections in v5 reference (non-exhaustive sweep against public dimension/metric reference):
  - Standard dimensions/metrics (delivery)
  - Floodlight dimensions/metrics (conversion attribution)
  - Reach & Frequency dimensions/metrics
  - Path-to-conversion dimensions/metrics (excluded: separate grain)
  - Cross-environment metrics (excluded: advanced, account-feature-gated)
  - Active View / Viewability metrics (excluded: sampled, non-additive ratios)
  - Custom variables / site-defined segments (excluded: per-advertiser, not canonical)
"""

from __future__ import annotations

import json
from pathlib import Path

# F-CAT-1: Explicit exclusion ledger for notable v5 fields deliberately not exposed.
# Every field in this list was reviewed and rejected for one of the stated reasons.
# A field absent from BOTH FIELDS and EXCLUDED_FIELDS is a gap that must be addressed
# before the catalog can claim exhaustive coverage of a v5 section.
EXCLUDED_FIELDS = [
    # ---- Standard delivery dimensions ----
    ("dfa:advertiserName",      "dimension", "STANDARD",    "label_not_id",
     "Human-readable label; advertiser_id (dfa:advertiser) is the canonical join key."),
    ("dfa:campaignName",        "dimension", "STANDARD",    "label_not_id",
     "Human-readable label; campaign_id (dfa:campaign) is the canonical join key."),
    ("dfa:placementName",       "dimension", "STANDARD",    "label_not_id",
     "Human-readable label; placement_id (dfa:placement) is the canonical join key."),
    ("dfa:creativeName",        "dimension", "STANDARD",    "label_not_id",
     "Human-readable label; creative_id (dfa:creative) is the canonical join key."),
    ("dfa:placementSize",       "dimension", "STANDARD",    "low_priority",
     "Creative dimension string; useful for creative audits, not day-grain KPIs."),
    ("dfa:adType",              "dimension", "STANDARD",    "low_priority",
     "Ad type enum (DISPLAY, VIDEO, etc.); not yet surfaced in a report profile."),
    ("dfa:site",                "dimension", "STANDARD",    "low_priority",
     "Site/publisher key; planned for a site-grain report profile (not story 33-3)."),
    ("dfa:siteName",            "dimension", "STANDARD",    "label_not_id",
     "Human-readable site label; dfa:site is the canonical join key."),
    ("dfa:packageRoadblock",    "dimension", "STANDARD",    "low_priority",
     "Package/roadblock grouping; advanced use case, not required for P0 delivery KPIs."),
    # ---- Standard delivery metrics ----
    ("dfa:richMediaVideoPlays",     "metric", "STANDARD",   "advanced_video",
     "Rich media / VPAID video-specific metric; out of scope for standard display delivery."),
    ("dfa:richMediaVideoCompletions","metric","STANDARD",   "advanced_video",
     "Rich media video completion; out of scope for standard display delivery."),
    ("dfa:richMediaAverageDisplayTime","metric","STANDARD", "non_additive",
     "Time-averaged metric (average display time); non-additive, not projectable to fact."),
    ("dfa:richMediaEngagements",    "metric", "STANDARD",   "advanced_video",
     "Engagement count for rich media; out of scope for standard display delivery."),
    ("dfa:richMediaExpansions",     "metric", "STANDARD",   "advanced_video",
     "Expansion event count; out of scope for standard display delivery."),
    # ---- Floodlight / conversion attribution ----
    ("dfa:activityName",        "dimension", "FLOODLIGHT",  "label_not_id",
     "Human-readable label; floodlight_activity_id (dfa:activity) is the canonical join key."),
    ("dfa:activityGroup",       "dimension", "FLOODLIGHT",  "low_priority",
     "Floodlight activity group key; planned for a future group-level profile."),
    ("dfa:activityGroupName",   "dimension", "FLOODLIGHT",  "label_not_id",
     "Human-readable label for activity group; dfa:activityGroup is the canonical key."),
    ("dfa:clickThroughConversions", "metric","FLOODLIGHT",  "attribution_split",
     "Click-through conversions sub-total; dfa:totalConversions is the additive canonical."),
    ("dfa:viewThroughConversions",  "metric","FLOODLIGHT",  "attribution_split",
     "View-through conversions sub-total; dfa:totalConversions is the additive canonical."),
    ("dfa:clickThroughRevenue",     "metric","FLOODLIGHT",  "attribution_split",
     "Click-through revenue sub-total; dfa:totalConversionsRevenue is the additive canonical."),
    ("dfa:viewThroughRevenue",      "metric","FLOODLIGHT",  "attribution_split",
     "View-through revenue sub-total; dfa:totalConversionsRevenue is the additive canonical."),
    # ---- Reach & Frequency ----
    ("dfa:uniqueReachClickReach",   "metric","REACH",       "reach_variant",
     "Click-based unique reach sub-total; unique_reach (dfa:uniqueReachImpressions) is canonical."),
    ("dfa:reachFrequencyBucket",    "dimension","REACH",    "advanced_reach",
     "Frequency distribution bucket; requires a dedicated frequency-distribution report grain."),
    # ---- Path-to-conversion dimensions/metrics ----
    ("dfa:pathConversionEvent",     "dimension","PATH",     "dedicated_grain",
     "Path-to-conversion event type; requires a separate path-grain report (not implemented)."),
    ("dfa:pathLength",              "dimension","PATH",     "dedicated_grain",
     "Number of interactions in the path; path grain only, not day-grain KPI."),
    ("dfa:pathType",                "dimension","PATH",     "dedicated_grain",
     "Path type (post-click vs post-impression); path grain only."),
    ("dfa:totalConversionsByPathType","metric", "PATH",     "dedicated_grain",
     "Conversions by path type; path grain only, not projectable to standard day grain."),
    # ---- Active View / Viewability ----
    ("dfa:activeViewViewableImpressions","metric","STANDARD","sampled_ratio_input",
     "Sampled Active View viewable impressions; viewability rate is a derived ratio."),
    ("dfa:activeViewMeasurableImpressions","metric","STANDARD","sampled_ratio_input",
     "Sampled Active View measurable impressions; ratio input, not a standalone KPI."),
    ("dfa:activeViewEligibleImpressions","metric","STANDARD","sampled_ratio_input",
     "Active View eligible impressions; ratio denominator, not a standalone KPI."),
    ("dfa:activeViewPercentViewableImpressions","metric","STANDARD","non_additive",
     "Viewability rate (percentage); non-additive, not projectable to fact_daily_kpi."),
    ("dfa:activeViewPercentMeasurableImpressions","metric","STANDARD","non_additive",
     "Measurability rate (percentage); non-additive, not projectable to fact_daily_kpi."),
    # ---- Cross-environment / advanced ----
    ("dfa:cookielessReachImpressions","metric","STANDARD",  "account_feature_gated",
     "Cookieless reach requires advanced CM360 features not universally available."),
    ("dfa:nielsenAverageFrequency", "metric","STANDARD",    "account_feature_gated",
     "Nielsen co-viewing metric; requires a Nielsen integration, account-feature-gated."),
    # ---- Custom variables / segments ----
    ("dfa:customVariable1",     "dimension","STANDARD",     "per_advertiser_config",
     "Custom Floodlight variable; per-advertiser definition, not a canonical platform field."),
    ("dfa:customVariable2",     "dimension","STANDARD",     "per_advertiser_config",
     "Custom Floodlight variable; per-advertiser definition, not a canonical platform field."),
]

FIELDS = [
    ("date", "date", "dimension", "date", "STANDARD", "CM360 reporting day."),
    ("advertiser_id", "dfa:advertiser", "dimension", "string", "STANDARD", "Advertiser id."),
    ("campaign_id", "dfa:campaign", "dimension", "string", "STANDARD", "Campaign id."),
    ("placement_id", "dfa:placement", "dimension", "string", "STANDARD", "Placement id."),
    ("creative_id", "dfa:creative", "dimension", "string", "STANDARD", "Creative id."),
    (
        "floodlight_activity_id",
        "dfa:activity",
        "dimension",
        "string",
        "FLOODLIGHT",
        "Floodlight activity id.",
    ),
    ("country", "dfa:country", "dimension", "string", "REACH", "Country or provider sentinel."),
    ("impressions", "dfa:impressions", "metric", "integer", "STANDARD", "Served impressions."),
    ("clicks", "dfa:clicks", "metric", "integer", "STANDARD", "Recorded clicks."),
    ("cost", "dfa:mediaCost", "metric", "decimal", "STANDARD", "Media cost."),
    (
        "conversions",
        "dfa:totalConversions",
        "metric",
        "decimal",
        "FLOODLIGHT",
        "Attributed conversions.",
    ),
    (
        "conversion_value",
        "dfa:totalConversionsRevenue",
        "metric",
        "decimal",
        "FLOODLIGHT",
        "Conversion value.",
    ),
    ("unique_reach", "dfa:uniqueReachImpressions", "metric", "integer", "REACH", "Unique reach."),
    (
        "average_frequency",
        "dfa:averageFrequency",
        "metric",
        "decimal",
        "REACH",
        "Average frequency.",
    ),
]


def build() -> list[dict]:
    return [
        {
            "field_id": field_id,
            "source_field": source_field,
            "kind": kind,
            "data_type": data_type,
            "section": section,
            "description": description,
        }
        for field_id, source_field, kind, data_type, section, description in FIELDS
    ]


def build_excluded() -> list[dict]:
    """Return the explicit exclusion ledger for diffability against the v5 reference.

    F-CAT-1: every field deliberately NOT in FIELDS must have an entry here with a
    machine-readable excluded_reason code so the catalog is provably non-implicit.
    """
    return [
        {
            "source_field": source_field,
            "kind": kind,
            "section": section,
            "exposure": "excluded",
            "excluded_reason": excluded_reason,
            "description": description,
        }
        for source_field, kind, section, excluded_reason, description in EXCLUDED_FIELDS
    ]


if __name__ == "__main__":
    target = Path(__file__).with_name("official_fields.json")
    target.write_text(json.dumps(build(), indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {len(FIELDS)} CM360 exposed fields to {target}")

    excluded_target = Path(__file__).with_name("excluded_fields.json")
    excluded_target.write_text(json.dumps(build_excluded(), indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {len(EXCLUDED_FIELDS)} CM360 excluded fields to {excluded_target}")
