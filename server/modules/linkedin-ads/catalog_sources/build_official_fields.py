"""Deterministic builder for the LinkedIn Ads curated official field snapshot.

Story 25.7. Not run in CI. Produces ``official_fields.json`` byte-deterministically
from the OFFICIAL LinkedIn Marketing adAnalytics reference (Microsoft Learn,
li-lms-2026-06 moniker):

  - Metrics Available table (adAnalytics ``fields`` parameter):
    https://learn.microsoft.com/en-us/linkedin/marketing/integrations/ads-reporting/ads-reporting-schema
  - Analytics/Statistics finder ``pivot`` enum values (structural + demographic pivots):
    same page, "Analytics Finder Query Parameters" table.
  - Revenue Attribution Metrics (``attributedRevenueMetrics`` finder), flattened.

Run: ``python build_official_fields.py`` (writes official_fields.json next to it).
"""

from __future__ import annotations

import collections
import json
import pathlib

# --- Metrics Available (li-lms-2026-06), field name + doc Type + section bucket. ---
# section buckets mirror the Supermetrics section families used by section_tier_map.
METRICS = [
    ("actionClicks", "long", "SOCIAL ACTIONS"),
    ("adUnitClicks", "long", "SOCIAL ACTIONS"),
    ("appointmentsScheduled", "long", "CONVERSION"),
    ("approximateMemberReach", "long", "PERFORMANCE"),
    ("averageDwellTime", "long", "PERFORMANCE"),
    ("averageEventWatchTime", "double", "VIDEO"),
    ("averageEventWatchTimeOver15Seconds", "double", "VIDEO"),
    ("averageEventWatchTimeOver2Minutes", "double", "VIDEO"),
    ("averageEventWatchTimeOver30Seconds", "double", "VIDEO"),
    ("averageVideoWatchTime", "double", "VIDEO"),
    ("audiencePenetration", "double", "PERFORMANCE"),
    ("cardClicks", "long", "CLICKS"),
    ("cardImpressions", "long", "IMPRESSION"),
    ("clicks", "long", "CLICKS"),
    ("commentLikes", "long", "SOCIAL ACTIONS"),
    ("comments", "long", "SOCIAL ACTIONS"),
    ("companyPageClicks", "long", "CLICKS"),
    ("conversionValueInLocalCurrency", "BigDecimal", "CONVERSION"),
    ("costInLocalCurrency", "BigDecimal", "COST"),
    ("costInUsd", "BigDecimal", "COST"),
    ("costPerEventView", "double", "COST"),
    ("costPerEventViewOver15Seconds", "double", "COST"),
    ("costPerEventViewOver2Minutes", "double", "COST"),
    ("costPerEventViewOver30Seconds", "double", "COST"),
    ("costPerQualifiedLead", "BigDecimal", "COST"),
    ("documentCompletions", "long", "VIDEO"),
    ("documentFirstQuartileCompletions", "long", "VIDEO"),
    ("documentMidpointCompletions", "long", "VIDEO"),
    ("documentThirdQuartileCompletions", "long", "VIDEO"),
    ("downloadClicks", "long", "CLICKS"),
    ("eventViews", "long", "VIDEO"),
    ("eventViewsOver15Seconds", "long", "VIDEO"),
    ("eventViewsOver2Minutes", "long", "VIDEO"),
    ("eventViewsOver30Seconds", "long", "VIDEO"),
    ("eventWatchTime", "long", "VIDEO"),
    ("externalWebsiteConversions", "long", "CONVERSION"),
    ("externalWebsitePostClickConversions", "long", "CONVERSION"),
    ("externalWebsitePostViewConversions", "long", "CONVERSION"),
    ("follows", "long", "SOCIAL ACTIONS"),
    ("fullScreenPlays", "long", "VIDEO"),
    ("headlineClicks", "long", "CLICKS"),
    ("headlineImpressions", "long", "IMPRESSION"),
    ("impressions", "long", "IMPRESSION"),
    ("jobApplications", "BigDecimal", "CONVERSION"),
    ("jobApplyClicks", "BigDecimal", "CONVERSION"),
    ("landingPageClicks", "long", "CLICKS"),
    ("leadGenerationMailContactInfoShares", "long", "CONVERSION"),
    ("leadGenerationMailInterestedClicks", "long", "CLICKS"),
    ("likes", "long", "SOCIAL ACTIONS"),
    ("oneClickLeadFormOpens", "long", "CONVERSION"),
    ("oneClickLeads", "long", "CONVERSION"),
    ("opens", "long", "SOCIAL ACTIONS"),
    ("otherEngagements", "long", "SOCIAL ACTIONS"),
    ("postClickJobApplications", "BigDecimal", "CONVERSION"),
    ("postClickJobApplyClicks", "BigDecimal", "CONVERSION"),
    ("postClickRegistrations", "BigDecimal", "CONVERSION"),
    ("postViewJobApplications", "BigDecimal", "CONVERSION"),
    ("postViewJobApplyClicks", "BigDecimal", "CONVERSION"),
    ("postViewRegistrations", "BigDecimal", "CONVERSION"),
    ("qualifiedLeads", "long", "CONVERSION"),
    ("reactions", "long", "SOCIAL ACTIONS"),
    ("registrations", "BigDecimal", "CONVERSION"),
    ("sends", "long", "SOCIAL ACTIONS"),
    ("shares", "long", "SOCIAL ACTIONS"),
    ("subscriptionClicks", "long", "CLICKS"),
    ("talentLeads", "long", "CONVERSION"),
    ("textUrlClicks", "long", "CLICKS"),
    ("totalEngagements", "long", "SOCIAL ACTIONS"),
    ("validWorkEmailLeads", "long", "CONVERSION"),
    ("videoCompletions", "long", "VIDEO"),
    ("videoFirstQuartileCompletions", "long", "VIDEO"),
    ("videoMidpointCompletions", "long", "VIDEO"),
    ("videoStarts", "long", "VIDEO"),
    ("videoThirdQuartileCompletions", "long", "VIDEO"),
    ("videoViews", "long", "VIDEO"),
    ("videoWatchTime", "long", "VIDEO"),
    ("viralCardClicks", "long", "CLICKS"),
    ("viralCardImpressions", "long", "IMPRESSION"),
    ("viralClicks", "long", "CLICKS"),
    ("viralCommentLikes", "long", "SOCIAL ACTIONS"),
    ("viralComments", "long", "SOCIAL ACTIONS"),
    ("viralCompanyPageClicks", "long", "CLICKS"),
    ("viralDocumentCompletions", "long", "VIDEO"),
    ("viralDocumentFirstQuartileCompletions", "long", "VIDEO"),
    ("viralDocumentMidpointCompletions", "long", "VIDEO"),
    ("viralDocumentThirdQuartileCompletions", "long", "VIDEO"),
    ("viralDownloadClicks", "long", "CLICKS"),
    ("viralExternalWebsiteConversions", "long", "CONVERSION"),
    ("viralExternalWebsitePostClickConversions", "long", "CONVERSION"),
    ("viralExternalWebsitePostViewConversions", "long", "CONVERSION"),
    ("viralFollows", "long", "SOCIAL ACTIONS"),
    ("viralFullScreenPlays", "long", "VIDEO"),
    ("viralImpressions", "long", "IMPRESSION"),
    ("viralJobApplications", "BigDecimal", "CONVERSION"),
    ("viralJobApplyClicks", "BigDecimal", "CONVERSION"),
    ("viralLandingPageClicks", "long", "CLICKS"),
    ("viralLikes", "long", "SOCIAL ACTIONS"),
    ("viralOneClickLeadFormOpens", "long", "CONVERSION"),
    ("viralOneClickLeads", "long", "CONVERSION"),
    ("viralOtherEngagements", "long", "SOCIAL ACTIONS"),
    ("viralPostClickJobApplications", "BigDecimal", "CONVERSION"),
    ("viralPostClickJobApplyClicks", "BigDecimal", "CONVERSION"),
    ("viralPostClickRegistrations", "BigDecimal", "CONVERSION"),
    ("viralPostViewJobApplications", "BigDecimal", "CONVERSION"),
    ("viralPostViewJobApplyClicks", "BigDecimal", "CONVERSION"),
    ("viralPostViewRegistrations", "BigDecimal", "CONVERSION"),
    ("viralReactions", "long", "SOCIAL ACTIONS"),
    ("viralRegistrations", "BigDecimal", "CONVERSION"),
    ("viralShares", "long", "SOCIAL ACTIONS"),
    ("viralSubscriptionClicks", "long", "CLICKS"),
    ("viralTotalEngagements", "long", "SOCIAL ACTIONS"),
    ("viralVideoCompletions", "long", "VIDEO"),
    ("viralVideoFirstQuartileCompletions", "long", "VIDEO"),
    ("viralVideoMidpointCompletions", "long", "VIDEO"),
    ("viralVideoStarts", "long", "VIDEO"),
    ("viralVideoThirdQuartileCompletions", "long", "VIDEO"),
    ("viralVideoViews", "long", "VIDEO"),
]

# --- Revenue Attribution Metrics (attributedRevenueMetrics finder), flattened. ---
REVENUE = [
    ("revenueWonInUsd", "BigDecimal"),
    ("returnOnAdSpend", "double"),
    ("closedWonOpportunities", "long"),
    ("opportunityAmountInUsd", "BigDecimal"),
    ("openOpportunities", "long"),
    ("opportunityWinRate", "double"),
    ("averageDealSizeInUsd", "BigDecimal"),
    ("averageDaysToClose", "double"),
]

# --- Structural / time dimensions the connector actually materialises. ---
# source_field mirrors what the manifest declares (date -> dateRange; the id
# dimensions are derived from pivotValues URNs by _parse_report_row).
STRUCT_DIMS = [
    ("date", "dateRange", "date", "TIME",
     "Date range covered by the report data point (LinkedIn dateRange object)."),
    ("pivotValues", "pivotValues", "json", "STRUCTURE",
     "Serialized pivot URNs for the record (one entry per requested pivot)."),
    ("campaign_id", "campaign_id", "string", "STRUCTURE",
     "Sponsored Campaign id, extracted from the CAMPAIGN pivot URN."),
    ("campaign_group_id", "campaign_group_id", "string", "STRUCTURE",
     "Campaign Group id, extracted from the CAMPAIGN_GROUP pivot URN."),
]

# --- pivot enum values (Analytics + Statistics finder pivot list, verbatim). ---
# Emitted as parameterised dimensions (field_id pivot_<ENUM>) so the catalog
# declares every grouping the API supports without enumerating pivot VALUES
# (those are data, resolved per-account at query time).
PIVOTS = [
    ("ACCOUNT", "STRUCTURE", "Group results by ad account."),
    ("CAMPAIGN", "STRUCTURE", "Group results by campaign."),
    ("CAMPAIGN_GROUP", "STRUCTURE", "Group results by campaign group."),
    ("CREATIVE", "STRUCTURE", "Group results by creative."),
    ("COMPANY", "STRUCTURE", "Group results by advertiser's company."),
    ("SHARE", "STRUCTURE", "Group results by sponsored share."),
    ("CONVERSION", "CONVERSION", "Group results by conversion."),
    ("CONVERSATION_NODE", "STRUCTURE", "Information for each node of the conversation tree."),
    ("CONVERSATION_NODE_OPTION_INDEX", "STRUCTURE", "Conversation node button index."),
    ("SERVING_LOCATION", "STRUCTURE", "Group results by serving location (onsite/offsite)."),
    ("CARD_INDEX", "STRUCTURE", "Group results by carousel card index."),
    ("OBJECTIVE_TYPE", "STRUCTURE", "Group results by campaign objective type (statistics finder)."),
    ("PLACEMENT_NAME", "STRUCTURE", "Group results by placement."),
    ("IMPRESSION_DEVICE_TYPE", "STRUCTURE", "Group results by impression device type."),
    ("EVENT_STAGE", "STRUCTURE", "Group results by live event stage (PRE_LIVE/LIVE/POST_LIVE)."),
    ("MEMBER_COMPANY_SIZE", "DEMOGRAPHICS", "Group results by member company size."),
    ("MEMBER_INDUSTRY", "DEMOGRAPHICS", "Group results by member industry."),
    ("MEMBER_SENIORITY", "DEMOGRAPHICS", "Group results by member seniority."),
    ("MEMBER_JOB_TITLE", "DEMOGRAPHICS", "Group results by member job title."),
    ("MEMBER_JOB_FUNCTION", "DEMOGRAPHICS", "Group results by member job function."),
    ("MEMBER_COUNTRY_V2", "DEMOGRAPHICS", "Group results by member country (Bing geo)."),
    ("MEMBER_REGION_V2", "DEMOGRAPHICS", "Group results by member region (Bing geo)."),
    ("MEMBER_COUNTY", "DEMOGRAPHICS", "Group results by member county (Bing geo)."),
    ("MEMBER_COMPANY", "DEMOGRAPHICS", "Group results by member company."),
]


def _phys(doc_type: str) -> str:
    if doc_type in ("BigDecimal", "double"):
        return "decimal"
    if doc_type == "long":
        return "integer"
    return "string"


# The connector's manifest (source_capabilities.fields) declares CANONICAL
# field_ids that differ from the LinkedIn provider token for four metrics and
# the date dimension. The manifest<->catalog gate (core.catalog_contract) matches
# by field_id and asserts source_field agreement, so the official snapshot must
# carry the SAME canonical field_id + the provider token as source_field. We
# rename these raw LinkedIn tokens to their manifest field_id and set source_field.
# (impressions/clicks are already identical on both sides -> no rename.)
_MANIFEST_RENAME = {
    # provider token -> (canonical field_id, source_field to emit)
    "costInLocalCurrency": ("cost", "costInLocalCurrency"),
    "externalWebsiteConversions": ("conversions", "externalWebsiteConversions"),
    "leadGenerationMailContactInfoShares": ("leads", "leadGenerationMailContactInfoShares"),
}


def build() -> list[dict]:
    fields: list[dict] = []
    for name, doc_type, section in METRICS:
        if name in _MANIFEST_RENAME:
            canonical, source_field = _MANIFEST_RENAME[name]
            fields.append({
                "data_type": _phys(doc_type),
                "description": f"{canonical} ({name}) metric from the LinkedIn Marketing adAnalytics endpoint.",
                "field_id": canonical,
                "kind": "metric",
                "section": section,
                "source_field": source_field,
            })
            continue
        fields.append({
            "data_type": _phys(doc_type),
            "description": f"{name} metric from the LinkedIn Marketing adAnalytics endpoint.",
            "field_id": name,
            "kind": "metric",
            "section": section,
        })
    for name, doc_type in REVENUE:
        fields.append({
            "data_type": _phys(doc_type),
            "description": f"{name} revenue-attribution metric (attributedRevenueMetrics finder).",
            "field_id": name,
            "kind": "metric",
            "section": "REVENUE ATTRIBUTION",
        })
    for fid, src, dt, section, desc in STRUCT_DIMS:
        entry = {
            "data_type": dt,
            "description": desc,
            "field_id": fid,
            "kind": "dimension",
            "section": section,
        }
        if src != fid:
            # date -> dateRange etc.; keep source_field agreement with the manifest.
            entry["source_field"] = src
        fields.append(entry)
    for enum, section, desc in PIVOTS:
        fields.append({
            "data_type": "string",
            "description": f"{enum} pivot -- {desc}",
            "field_id": f"pivot_{enum}",
            "kind": "dimension",
            "section": section,
            "source_field": f"pivot:{enum}",
        })

    fields.sort(key=lambda f: f["field_id"])
    ids = [f["field_id"] for f in fields]
    dups = [i for i, c in collections.Counter(ids).items() if c > 1]
    if dups:
        raise SystemExit(f"duplicate field_id(s): {dups}")
    return fields


def main() -> None:
    fields = build()
    out = pathlib.Path(__file__).with_name("official_fields.json")
    out.write_text(json.dumps(fields, indent=2, ensure_ascii=True) + "\n", encoding="utf-8")
    n_m = sum(1 for f in fields if f["kind"] == "metric")
    n_d = sum(1 for f in fields if f["kind"] == "dimension")
    print(f"wrote {out.name}: {len(fields)} fields ({n_m} metrics, {n_d} dimensions)")


if __name__ == "__main__":
    main()
