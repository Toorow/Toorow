"""Deterministic builder for microsoft-ads official_fields.json (Story 26.4).

This script is the AUDITABLE SOURCE for the committed official snapshot. It is
committed alongside its outputs so a reviewer can see exactly how the curated
list was derived from the LIVE learn.microsoft.com Bing Ads API v13 Reporting
value-set pages recorded in catalog_sources.json (the public GitHub mirror
MicrosoftDocs/Advertising-docs is STALE since 2021 -- never used).

It is pure-Python, deterministic (no network, no clock), and byte-stable:
running it again reproduces its outputs exactly.

Outputs (both committed):
  1. official_fields.json -- one entry per DISTINCT column across the 8
     integrally-covered report types (each column declared ONCE, with the
     per-report availability recorded in its description), PLUS one
     report-surface entry per next-tier report type (the 40 uncovered
     ReportRequest types), which catalog_sources.json excluded_fields turns
     into ``exposure: excluded`` entries with a report-type-next-tier reason.
  2. catalog_sources.json regenerated blocks (single source, zero drift):
       - field_compatibility (core contract schema_version "1"):
           * mutually_exclusive Column Restrictions rules,
           * selectable_set x8 with scope {"report_type": <X>ReportRequest}
             carrying the INTEGRAL per-report column enum as field ids;
       - excluded_fields: the 40 report-surface exclusions with reasons.

Derivation summary (research dossier microsoft-ads-catalog-research.md,
fetched 2026-07-21):
  - The 8 covered report types and their INTEGRAL column enums (zero
    truncation, live XSD ``xs:enumeration`` order):
        CampaignPerformance 132, AccountPerformance 111,
        AdGroupPerformance 104, AdPerformance 92, KeywordPerformance 80,
        GeographicPerformance 80, SearchQueryPerformance 61,
        AgeGenderAudience 38.
  - kind: dimension = ATTRIBUTE column, metric = performance STATISTIC
    (guide "Columns that group the data"). QualityScore-family snapshot
    columns and bid amounts are attributes; Historical* quality columns are
    per-TimePeriod statistics (non-additive).
  - physical types: curated overrides + count/money heuristics; every ratio
    or derived statistic carries a NON-ADDITIVE (AD-4) description note.
    Percent-valued CSV cells arrive with a trailing '%' -- the connector
    coerces by CATALOG physical_type, never by column-name suffix.

Run (orchestrator, local only -- outputs are committed):
    uv run python server/modules/microsoft-ads/catalog_sources/build_official_fields.py
"""

from __future__ import annotations

import json
import re
from pathlib import Path

# ---------------------------------------------------------------------------
# The 8 covered report types -- INTEGRAL column enums (live XSD order,
# fetched 2026-07-21). Zero truncation (story 26.4 hard rule).
# ---------------------------------------------------------------------------

CAMPAIGN_COLUMNS: tuple[str, ...] = (
    "AccountName", "AccountNumber", "AccountId", "TimePeriod", "CampaignStatus",
    "CampaignName", "CampaignId", "CurrencyCode", "AdDistribution", "Impressions",
    "Clicks", "Ctr", "AverageCpc", "Spend", "AveragePosition", "Conversions",
    "ConversionRate", "CostPerConversion", "LowQualityClicks",
    "LowQualityClicksPercent", "LowQualityImpressions",
    "LowQualityImpressionsPercent", "LowQualityConversions",
    "LowQualityConversionRate", "DeviceType", "DeviceOS", "ImpressionSharePercent",
    "ImpressionLostToBudgetPercent", "ImpressionLostToRankAggPercent",
    "QualityScore", "ExpectedCtr", "AdRelevance", "LandingPageExperience",
    "HistoricalQualityScore", "HistoricalExpectedCtr", "HistoricalAdRelevance",
    "HistoricalLandingPageExperience", "PhoneImpressions", "PhoneCalls", "Ptr",
    "Network", "TopVsOther", "BidMatchType", "DeliveredMatchType", "Assists",
    "Revenue", "ReturnOnAdSpend", "CostPerAssist", "RevenuePerConversion",
    "RevenuePerAssist", "TrackingTemplate", "CustomParameters", "AccountStatus",
    "BudgetName", "BudgetStatus", "BudgetAssociationStatus",
    "LowQualityGeneralClicks", "LowQualitySophisticatedClicks", "CampaignLabels",
    "ExactMatchImpressionSharePercent", "CustomerId", "CustomerName",
    "ClickSharePercent", "AbsoluteTopImpressionSharePercent", "FinalUrlSuffix",
    "CampaignType", "TopImpressionShareLostToRankPercent",
    "TopImpressionShareLostToBudgetPercent",
    "AbsoluteTopImpressionShareLostToRankPercent",
    "AbsoluteTopImpressionShareLostToBudgetPercent", "TopImpressionSharePercent",
    "AbsoluteTopImpressionRatePercent", "TopImpressionRatePercent",
    "BaseCampaignId", "AllConversions", "AllRevenue", "AllConversionRate",
    "AllCostPerConversion", "AllReturnOnAdSpend", "AllRevenuePerConversion",
    "ViewThroughConversions", "Goal", "GoalType",
    "AudienceImpressionSharePercent", "AudienceImpressionLostToRankPercent",
    "AudienceImpressionLostToBudgetPercent", "RelativeCtr", "AverageCpm",
    "ConversionsQualified", "LowQualityConversionsQualified",
    "AllConversionsQualified", "ViewThroughConversionsQualified",
    "ViewThroughRevenue", "VideoViews", "ViewThroughRate", "AverageCPV",
    "VideoViewsAt25Percent", "VideoViewsAt50Percent", "VideoViewsAt75Percent",
    "CompletedVideoViews", "VideoCompletionRate", "TotalWatchTimeInMS",
    "AverageWatchTimePerVideoView", "AverageWatchTimePerImpression", "Sales",
    "CostPerSale", "RevenuePerSale", "Installs", "CostPerInstall",
    "RevenuePerInstall", "Downloads", "PostClickDownloadRate", "CostPerDownload",
    "AppInstalls", "PostClickInstallRate", "CPI", "Purchases",
    "PostInstallPurchaseRate", "CPP", "Subscriptions",
    "PostInstallSubscriptionRate", "CPS", "NewCustomerConversions",
    "NewCustomerRevenue", "NewCustomerConversionRate", "NewCustomerCPA",
    "NewCustomerReturnOnAdSpend", "ConversionDelay", "UnknownCustomerConversions",
    "UnknownCustomerRevenue", "NewCustomerCount", "NewCustomerSpend",
)

ADGROUP_COLUMNS: tuple[str, ...] = (
    "AccountName", "AccountNumber", "AccountId", "TimePeriod", "Status",
    "CampaignName", "CampaignId", "AdGroupName", "AdGroupId", "CurrencyCode",
    "AdDistribution", "Impressions", "Clicks", "Ctr", "AverageCpc", "Spend",
    "AveragePosition", "Conversions", "ConversionRate", "CostPerConversion",
    "DeviceType", "Language", "DeviceOS", "ImpressionSharePercent",
    "ImpressionLostToBudgetPercent", "ImpressionLostToRankAggPercent",
    "QualityScore", "ExpectedCtr", "AdRelevance", "LandingPageExperience",
    "HistoricalQualityScore", "HistoricalExpectedCtr", "HistoricalAdRelevance",
    "HistoricalLandingPageExperience", "PhoneImpressions", "PhoneCalls", "Ptr",
    "Network", "TopVsOther", "BidMatchType", "DeliveredMatchType", "Assists",
    "Revenue", "ReturnOnAdSpend", "CostPerAssist", "RevenuePerConversion",
    "RevenuePerAssist", "TrackingTemplate", "CustomParameters", "AccountStatus",
    "CampaignStatus", "AdGroupLabels", "ExactMatchImpressionSharePercent",
    "CustomerId", "CustomerName", "ClickSharePercent",
    "AbsoluteTopImpressionSharePercent", "FinalUrlSuffix", "CampaignType",
    "TopImpressionShareLostToRankPercent", "TopImpressionShareLostToBudgetPercent",
    "AbsoluteTopImpressionShareLostToRankPercent",
    "AbsoluteTopImpressionShareLostToBudgetPercent", "TopImpressionSharePercent",
    "AbsoluteTopImpressionRatePercent", "TopImpressionRatePercent",
    "BaseCampaignId", "AllConversions", "AllRevenue", "AllConversionRate",
    "AllCostPerConversion", "AllReturnOnAdSpend", "AllRevenuePerConversion",
    "ViewThroughConversions", "Goal", "GoalType",
    "AudienceImpressionSharePercent", "AudienceImpressionLostToRankPercent",
    "AudienceImpressionLostToBudgetPercent", "RelativeCtr", "AdGroupType",
    "AverageCpm", "ConversionsQualified", "AllConversionsQualified",
    "ViewThroughConversionsQualified", "ViewThroughRevenue", "VideoViews",
    "ViewThroughRate", "AverageCPV", "VideoViewsAt25Percent",
    "VideoViewsAt50Percent", "VideoViewsAt75Percent", "CompletedVideoViews",
    "VideoCompletionRate", "TotalWatchTimeInMS", "AverageWatchTimePerVideoView",
    "AverageWatchTimePerImpression", "Sales", "CostPerSale", "RevenuePerSale",
    "Installs", "CostPerInstall", "RevenuePerInstall", "GoalId",
)

AD_COLUMNS: tuple[str, ...] = (
    "AccountName", "AccountNumber", "AccountId", "TimePeriod", "CampaignName",
    "CampaignId", "AdGroupName", "AdId", "AdGroupId", "AdTitle", "AdDescription",
    "AdDescription2", "AdType", "CurrencyCode", "AdDistribution", "Impressions",
    "Clicks", "Ctr", "AverageCpc", "Spend", "AveragePosition", "Conversions",
    "ConversionRate", "CostPerConversion", "DestinationUrl", "DeviceType",
    "Language", "DisplayUrl", "AdStatus", "Network", "TopVsOther", "BidMatchType",
    "DeliveredMatchType", "DeviceOS", "Assists", "Revenue", "ReturnOnAdSpend",
    "CostPerAssist", "RevenuePerConversion", "RevenuePerAssist",
    "TrackingTemplate", "CustomParameters", "FinalUrl", "FinalMobileUrl",
    "FinalAppUrl", "AccountStatus", "CampaignStatus", "AdGroupStatus",
    "TitlePart1", "TitlePart2", "TitlePart3", "Headline", "LongHeadline",
    "BusinessName", "Path1", "Path2", "AdLabels", "CustomerId", "CustomerName",
    "CampaignType", "BaseCampaignId", "AllConversions", "AllRevenue",
    "AllConversionRate", "AllCostPerConversion", "AllReturnOnAdSpend",
    "AllRevenuePerConversion", "FinalUrlSuffix", "ViewThroughConversions",
    "Goal", "GoalType", "AbsoluteTopImpressionRatePercent",
    "TopImpressionRatePercent", "AverageCpm", "ConversionsQualified",
    "AllConversionsQualified", "ViewThroughConversionsQualified",
    "ViewThroughRevenue", "VideoViews", "ViewThroughRate", "AverageCPV",
    "VideoViewsAt25Percent", "VideoViewsAt50Percent", "VideoViewsAt75Percent",
    "CompletedVideoViews", "VideoCompletionRate", "TotalWatchTimeInMS",
    "AverageWatchTimePerVideoView", "AverageWatchTimePerImpression", "AdStrength",
    "AdStrengthActionItems", "GoalId",
)

KEYWORD_COLUMNS: tuple[str, ...] = (
    "AccountName", "AccountNumber", "AccountId", "TimePeriod", "CampaignName",
    "CampaignId", "AdGroupName", "AdGroupId", "Keyword", "KeywordId", "AdId",
    "AdType", "DestinationUrl", "CurrentMaxCpc", "CurrencyCode",
    "DeliveredMatchType", "AdDistribution", "Impressions", "Clicks", "Ctr",
    "AverageCpc", "Spend", "AveragePosition", "Conversions", "ConversionRate",
    "CostPerConversion", "BidMatchType", "DeviceType", "QualityScore",
    "ExpectedCtr", "AdRelevance", "LandingPageExperience", "Language",
    "HistoricalQualityScore", "HistoricalExpectedCtr", "HistoricalAdRelevance",
    "HistoricalLandingPageExperience", "QualityImpact", "CampaignStatus",
    "AccountStatus", "AdGroupStatus", "KeywordStatus", "Network", "TopVsOther",
    "DeviceOS", "Assists", "Revenue", "ReturnOnAdSpend", "CostPerAssist",
    "RevenuePerConversion", "RevenuePerAssist", "TrackingTemplate",
    "CustomParameters", "FinalUrl", "FinalMobileUrl", "FinalAppUrl",
    "BidStrategyType", "KeywordLabels", "Mainline1Bid", "MainlineBid",
    "FirstPageBid", "FinalUrlSuffix", "BaseCampaignId", "AllConversions",
    "AllRevenue", "AllConversionRate", "AllCostPerConversion",
    "AllReturnOnAdSpend", "AllRevenuePerConversion", "ViewThroughConversions",
    "Goal", "GoalType", "AbsoluteTopImpressionRatePercent",
    "TopImpressionRatePercent", "AverageCpm", "ConversionsQualified",
    "AllConversionsQualified", "ViewThroughConversionsQualified",
    "ViewThroughRevenue", "GoalId",
)

SEARCH_QUERY_COLUMNS: tuple[str, ...] = (
    "AccountName", "AccountNumber", "AccountId", "TimePeriod", "CampaignName",
    "CampaignId", "AdGroupName", "AdGroupId", "AdId", "AdType", "DestinationUrl",
    "BidMatchType", "DeliveredMatchType", "CampaignStatus", "AdStatus",
    "Impressions", "Clicks", "Ctr", "AverageCpc", "Spend", "AveragePosition",
    "SearchQuery", "Keyword", "AdGroupCriterionId", "Conversions",
    "ConversionRate", "CostPerConversion", "Language", "KeywordId", "Network",
    "TopVsOther", "DeviceType", "DeviceOS", "Assists", "Revenue",
    "ReturnOnAdSpend", "CostPerAssist", "RevenuePerConversion",
    "RevenuePerAssist", "AccountStatus", "AdGroupStatus", "KeywordStatus",
    "CampaignType", "CustomerId", "CustomerName", "AllConversions", "AllRevenue",
    "AllConversionRate", "AllCostPerConversion", "AllReturnOnAdSpend",
    "AllRevenuePerConversion", "Goal", "GoalType",
    "AbsoluteTopImpressionRatePercent", "TopImpressionRatePercent", "AverageCpm",
    "ConversionsQualified", "AllConversionsQualified", "AssetGroupName",
    "AssetGroupId", "AssetGroupStatus",
)

ACCOUNT_COLUMNS: tuple[str, ...] = (
    "AccountName", "AccountNumber", "AccountId", "TimePeriod", "CurrencyCode",
    "AdDistribution", "Impressions", "Clicks", "Ctr", "AverageCpc", "Spend",
    "AveragePosition", "Conversions", "ConversionRate", "CostPerConversion",
    "LowQualityClicks", "LowQualityClicksPercent", "LowQualityImpressions",
    "LowQualityImpressionsPercent", "LowQualityConversions",
    "LowQualityConversionRate", "DeviceType", "DeviceOS",
    "ImpressionSharePercent", "ImpressionLostToBudgetPercent",
    "ImpressionLostToRankAggPercent", "PhoneImpressions", "PhoneCalls", "Ptr",
    "Network", "TopVsOther", "BidMatchType", "DeliveredMatchType", "Assists",
    "Revenue", "ReturnOnAdSpend", "CostPerAssist", "RevenuePerConversion",
    "RevenuePerAssist", "AccountStatus", "LowQualityGeneralClicks",
    "LowQualitySophisticatedClicks", "ExactMatchImpressionSharePercent",
    "CustomerId", "CustomerName", "ClickSharePercent",
    "AbsoluteTopImpressionSharePercent", "TopImpressionShareLostToRankPercent",
    "TopImpressionShareLostToBudgetPercent",
    "AbsoluteTopImpressionShareLostToRankPercent",
    "AbsoluteTopImpressionShareLostToBudgetPercent", "TopImpressionSharePercent",
    "AbsoluteTopImpressionRatePercent", "TopImpressionRatePercent",
    "AllConversions", "AllRevenue", "AllConversionRate", "AllCostPerConversion",
    "AllReturnOnAdSpend", "AllRevenuePerConversion", "ViewThroughConversions",
    "Goal", "GoalType", "AudienceImpressionSharePercent",
    "AudienceImpressionLostToRankPercent", "AudienceImpressionLostToBudgetPercent",
    "AverageCpm", "ConversionsQualified", "LowQualityConversionsQualified",
    "AllConversionsQualified", "ViewThroughConversionsQualified",
    "ViewThroughRevenue", "VideoViews", "ViewThroughRate", "AverageCPV",
    "VideoViewsAt25Percent", "VideoViewsAt50Percent", "VideoViewsAt75Percent",
    "CompletedVideoViews", "VideoCompletionRate", "TotalWatchTimeInMS",
    "AverageWatchTimePerVideoView", "AverageWatchTimePerImpression", "Sales",
    "CostPerSale", "RevenuePerSale", "Installs", "CostPerInstall",
    "RevenuePerInstall", "Downloads", "PostClickDownloadRate", "CostPerDownload",
    "AppInstalls", "PostClickInstallRate", "CPI", "Purchases",
    "PostInstallPurchaseRate", "CPP", "Subscriptions",
    "PostInstallSubscriptionRate", "CPS", "NewCustomerConversions",
    "NewCustomerRevenue", "NewCustomerConversionRate", "NewCustomerCPA",
    "NewCustomerReturnOnAdSpend", "ConversionDelay", "UnknownCustomerConversions",
    "UnknownCustomerRevenue", "NewCustomerCount", "NewCustomerSpend",
)

GEOGRAPHIC_COLUMNS: tuple[str, ...] = (
    "AccountName", "AccountNumber", "AccountId", "TimePeriod", "CampaignName",
    "CampaignId", "AdGroupName", "AdGroupId", "Country", "State", "MetroArea",
    "City", "CurrencyCode", "AdDistribution", "Impressions", "Clicks", "Ctr",
    "AverageCpc", "Spend", "AveragePosition", "ProximityTargetLocation",
    "Radius", "Language", "BidMatchType", "DeliveredMatchType", "Network",
    "TopVsOther", "DeviceType", "DeviceOS", "Assists", "Conversions",
    "ConversionRate", "Revenue", "ReturnOnAdSpend", "CostPerConversion",
    "CostPerAssist", "RevenuePerConversion", "RevenuePerAssist", "LocationType",
    "MostSpecificLocation", "AccountStatus", "CampaignStatus", "AdGroupStatus",
    "County", "PostalCode", "LocationId", "BaseCampaignId", "AllConversions",
    "AllRevenue", "AllConversionRate", "AllCostPerConversion",
    "AllReturnOnAdSpend", "AllRevenuePerConversion", "ViewThroughConversions",
    "Goal", "GoalType", "AbsoluteTopImpressionRatePercent",
    "TopImpressionRatePercent", "AverageCpm", "ConversionsQualified",
    "AllConversionsQualified", "ViewThroughConversionsQualified", "Neighborhood",
    "ViewThroughRevenue", "CampaignType", "AssetGroupId", "AssetGroupName",
    "AssetGroupStatus", "Downloads", "PostClickDownloadRate", "CostPerDownload",
    "AppInstalls", "PostClickInstallRate", "CPI", "Purchases",
    "PostInstallPurchaseRate", "CPP", "Subscriptions",
    "PostInstallSubscriptionRate", "CPS",
)

AGE_GENDER_COLUMNS: tuple[str, ...] = (
    "AccountName", "AccountNumber", "AccountId", "TimePeriod", "CampaignName",
    "CampaignId", "AdGroupName", "AdGroupId", "AdDistribution", "AgeGroup",
    "Gender", "Impressions", "Clicks", "Conversions", "Spend", "Revenue",
    "ExtendedCost", "Assists", "Language", "AccountStatus", "CampaignStatus",
    "AdGroupStatus", "BaseCampaignId", "AllConversions", "AllRevenue",
    "ViewThroughConversions", "Goal", "GoalType",
    "AbsoluteTopImpressionRatePercent", "TopImpressionRatePercent",
    "ConversionsQualified", "AllConversionsQualified",
    "ViewThroughConversionsQualified", "ViewThroughRevenue", "CampaignType",
    "AssetGroupId", "AssetGroupName", "AssetGroupStatus",
)

# report type -> (column tuple, expected count). Counts are the frozen dossier
# numbers -- a transcription drift fails the build loudly.
COVERED_REPORTS: dict[str, tuple[tuple[str, ...], int]] = {
    "CampaignPerformanceReportRequest": (CAMPAIGN_COLUMNS, 132),
    "AccountPerformanceReportRequest": (ACCOUNT_COLUMNS, 111),
    "AdGroupPerformanceReportRequest": (ADGROUP_COLUMNS, 104),
    "AdPerformanceReportRequest": (AD_COLUMNS, 92),
    "KeywordPerformanceReportRequest": (KEYWORD_COLUMNS, 80),
    "GeographicPerformanceReportRequest": (GEOGRAPHIC_COLUMNS, 80),
    "SearchQueryPerformanceReportRequest": (SEARCH_QUERY_COLUMNS, 61),
    "AgeGenderAudienceReportRequest": (AGE_GENDER_COLUMNS, 38),
}

# The 40 next-tier report surfaces (48-object inventory minus the 8 covered).
EXCLUDED_REPORT_TYPES: tuple[str, ...] = (
    "AdDynamicTextPerformanceReportRequest",
    "AdExtensionByAdReportRequest",
    "AdExtensionByKeywordReportRequest",
    "AdExtensionDetailReportRequest",
    "AppsPerformanceReportRequest",
    "AssetGroupPerformanceReportRequest",
    "AssetPerformanceReportRequest",
    "AudiencePerformanceReportRequest",
    "BidStrategyReportRequest",
    "BudgetSummaryReportRequest",
    "CallDetailReportRequest",
    "CategoryClickCoverageReportRequest",
    "CategoryInsightsReportRequest",
    "CombinationPerformanceReportRequest",
    "ConversionPerformanceReportRequest",
    "DestinationUrlPerformanceReportRequest",
    "DSAAutoTargetPerformanceReportRequest",
    "DSACategoryPerformanceReportRequest",
    "DSASearchQueryPerformanceReportRequest",
    "FeedItemPerformanceReportRequest",
    "GoalsAndFunnelsReportRequest",
    "HotelDimensionPerformanceReportRequest",
    "HotelGroupPerformanceReportRequest",
    "MMMPerformanceReportRequest",
    "MSClickIdPerformanceReportRequest",
    "NegativeKeywordConflictReportRequest",
    "ProductDimensionPerformanceReportRequest",
    "ProductMatchCountReportRequest",
    "ProductNegativeKeywordConflictReportRequest",
    "ProductPartitionPerformanceReportRequest",
    "ProductPartitionUnitPerformanceReportRequest",
    "ProductSearchQueryPerformanceReportRequest",
    "ProfessionalDemographicsAudienceReportRequest",
    "PublisherUsagePerformanceReportRequest",
    "SearchCampaignChangeHistoryReportRequest",
    "SearchInsightPerformanceReportRequest",
    "SearchTermLandingPageReportRequest",
    "ShareOfVoiceReportRequest",
    "TravelQueryInsightReportRequest",
    "UserLocationPerformanceReportRequest",
)

EXPECTED_EXCLUDED_REPORT_COUNT = 40
EXPECTED_TOTAL_REPORT_COUNT = 48

# ---------------------------------------------------------------------------
# Column Restrictions (guide #columnrestrictions -- the provenance also lives
# in catalog_sources.json column_restrictions with the doc URL).
# ---------------------------------------------------------------------------

IMPRESSION_SHARE_STATISTICS: tuple[str, ...] = (
    "ImpressionSharePercent",
    "ImpressionLostToBudgetPercent",
    "ImpressionLostToRankAggPercent",
    "ExactMatchImpressionSharePercent",
    "ClickSharePercent",
    "TopImpressionSharePercent",
    "TopImpressionShareLostToRankPercent",
    "TopImpressionShareLostToBudgetPercent",
    "AbsoluteTopImpressionSharePercent",
    "AbsoluteTopImpressionShareLostToRankPercent",
    "AbsoluteTopImpressionShareLostToBudgetPercent",
    "AudienceImpressionSharePercent",
    "AudienceImpressionLostToRankPercent",
    "AudienceImpressionLostToBudgetPercent",
    "RelativeCtr",
)

RESTRICTED_ATTRIBUTES: tuple[str, ...] = (
    "BidMatchType", "BudgetAssociationStatus", "BudgetName", "BudgetStatus",
    "DeviceOS", "Goal", "GoalType", "TopVsOther",
)

AUDIENCE_SHARE_COLUMNS: tuple[str, ...] = (
    "AudienceImpressionSharePercent",
    "AudienceImpressionLostToRankPercent",
    "AudienceImpressionLostToBudgetPercent",
    "RelativeCtr",
)

AUDIENCE_SHARE_EXCLUDED_ATTRIBUTES: tuple[str, ...] = (
    "CustomerId", "CustomerName", "DeliveredMatchType",
)

# ---------------------------------------------------------------------------
# Story 26.6 Part A -- DESCRIPTIVE MUTABLE attributes (grain vs supersede).
#
# The refetch ladder (26.1: nightly 3-14 d, monthly to 90 d) re-pulls past
# days. A STATUS/TYPE column of an entity is MUTABLE: a campaign Active on
# July 1 can be re-pulled as Paused for the SAME July 1. If such a column sits
# in segments_json (part of the dbt QUALIFY supersede grain), the re-pull lands
# a row with a DIFFERENT segments_json and the old row SURVIVES the QUALIFY ->
# SUM(cost) doubles for the whole re-tirée window (finding F-1). These columns
# must therefore land in a SEPARATE ``attributes_json`` column (out of the
# grain, latest-wins) -- NOT segments_json.
#
# What belongs here (prudent default = any entity STATE/TYPE column):
#   - lifecycle STATUS of an entity: CampaignStatus, AdGroupStatus (and its
#     alias Status on the AdGroup report), AdStatus, KeywordStatus,
#     AccountStatus, BudgetStatus/BudgetAssociationStatus, AssetGroupStatus;
#   - TYPE of an entity: CampaignType, AdGroupType, AdType.
# What must NOT be here (stays in the grain / segments_json):
#   - grain IDs and NAMES (campaign_id/name, ad_group_id/name, keyword, ...):
#     those key the row, they are not mutable attributes;
#   - SEGMENTING dimensions that carry genuinely distinct provider rows:
#     AgeGroup, Gender, Country/State/City/geo, DeviceType/DeviceOS, Network,
#     TopVsOther, BidMatchType, Language, TimePeriod, ... -- a status change
#     does not merge these, so they remain part of the supersede grain.
# GoalType/AdStrength* are excluded on purpose: Goal(Type) is a targeting/goal
# label (segmenting), AdStrength is a quality snapshot, neither is an entity
# lifecycle state/type.
# ---------------------------------------------------------------------------

DESCRIPTIVE_MUTABLE_COLUMNS: tuple[str, ...] = (
    # Entity lifecycle STATUS (mutable across re-pulls of the same day).
    "AccountStatus",
    "CampaignStatus",
    "AdGroupStatus",
    "Status",  # AdGroupPerformance's own name for the ad group status
    "AdStatus",
    "KeywordStatus",
    "AssetGroupStatus",
    "BudgetStatus",
    "BudgetAssociationStatus",
    # Entity TYPE (mutable/reclassifiable metadata, not a segmenting row key).
    "CampaignType",
    "AdGroupType",
    "AdType",
)

# ---------------------------------------------------------------------------
# Attribute vs statistic classification (guide "Columns that group the data").
# STATISTICS = kind metric; everything else = kind dimension (attribute).
# ---------------------------------------------------------------------------

STATISTIC_COLUMNS: frozenset[str] = frozenset({
    # Core delivery statistics
    "Impressions", "Clicks", "Ctr", "AverageCpc", "Spend", "AveragePosition",
    "AverageCpm",
    # Conversions / revenue
    "Conversions", "ConversionRate", "CostPerConversion", "ConversionsQualified",
    "Assists", "CostPerAssist", "Revenue", "ReturnOnAdSpend",
    "RevenuePerConversion", "RevenuePerAssist",
    "AllConversions", "AllRevenue", "AllConversionRate", "AllCostPerConversion",
    "AllReturnOnAdSpend", "AllRevenuePerConversion", "AllConversionsQualified",
    "ViewThroughConversions", "ViewThroughConversionsQualified",
    "ViewThroughRevenue", "ViewThroughRate", "ConversionDelay",
    # Low quality traffic
    "LowQualityClicks", "LowQualityClicksPercent", "LowQualityImpressions",
    "LowQualityImpressionsPercent", "LowQualityConversions",
    "LowQualityConversionRate", "LowQualityGeneralClicks",
    "LowQualitySophisticatedClicks", "LowQualityConversionsQualified",
    # Prominence / impression share
    *IMPRESSION_SHARE_STATISTICS,
    "AbsoluteTopImpressionRatePercent", "TopImpressionRatePercent",
    # Call tracking
    "PhoneImpressions", "PhoneCalls", "Ptr",
    # Historical quality (per-TimePeriod statistics, unlike snapshot QualityScore)
    "HistoricalQualityScore", "HistoricalExpectedCtr", "HistoricalAdRelevance",
    "HistoricalLandingPageExperience",
    # Video
    "VideoViews", "AverageCPV", "VideoViewsAt25Percent", "VideoViewsAt50Percent",
    "VideoViewsAt75Percent", "CompletedVideoViews", "VideoCompletionRate",
    "TotalWatchTimeInMS", "AverageWatchTimePerVideoView",
    "AverageWatchTimePerImpression",
    # Commerce / app campaigns
    "Sales", "CostPerSale", "RevenuePerSale", "Installs", "CostPerInstall",
    "RevenuePerInstall", "Downloads", "PostClickDownloadRate", "CostPerDownload",
    "AppInstalls", "PostClickInstallRate", "CPI", "Purchases",
    "PostInstallPurchaseRate", "CPP", "Subscriptions",
    "PostInstallSubscriptionRate", "CPS",
    # New customer
    "NewCustomerConversions", "NewCustomerRevenue", "NewCustomerConversionRate",
    "NewCustomerCPA", "NewCustomerReturnOnAdSpend", "UnknownCustomerConversions",
    "UnknownCustomerRevenue", "NewCustomerCount", "NewCustomerSpend",
    # AgeGender extended
    "ExtendedCost",
})

# ---------------------------------------------------------------------------
# physical data types.
# ---------------------------------------------------------------------------

INTEGER_COLUMNS: frozenset[str] = frozenset({
    "Impressions", "Clicks", "LowQualityClicks", "LowQualityImpressions",
    "LowQualityConversions", "LowQualityGeneralClicks",
    "LowQualitySophisticatedClicks", "PhoneImpressions", "PhoneCalls", "Assists",
    "Conversions", "ViewThroughConversions", "VideoViews",
    "VideoViewsAt25Percent", "VideoViewsAt50Percent", "VideoViewsAt75Percent",
    "CompletedVideoViews", "TotalWatchTimeInMS", "NewCustomerCount",
    # Attribute integers
    "QualityScore", "ExpectedCtr", "AdRelevance", "LandingPageExperience",
    "QualityImpact", "Radius",
})

DECIMAL_ATTRIBUTE_COLUMNS: frozenset[str] = frozenset({
    "CurrentMaxCpc", "Mainline1Bid", "MainlineBid", "FirstPageBid",
})

DATE_COLUMNS: frozenset[str] = frozenset({"TimePeriod"})

# Statistics not in INTEGER_COLUMNS default to decimal; attributes not in the
# integer/decimal/date sets default to string.

# Non-additive markers (AD-4): ratio / average / cost-per / share / historical.
_NON_ADDITIVE_EXACT: frozenset[str] = frozenset({
    "Ctr", "AverageCpc", "AverageCpm", "AveragePosition", "ConversionRate",
    "CostPerConversion", "CostPerAssist", "ReturnOnAdSpend",
    "RevenuePerConversion", "RevenuePerAssist", "AllConversionRate",
    "AllCostPerConversion", "AllReturnOnAdSpend", "AllRevenuePerConversion",
    "LowQualityClicksPercent", "LowQualityImpressionsPercent",
    "LowQualityConversionRate", "Ptr", "ViewThroughRate", "AverageCPV",
    "VideoCompletionRate", "AverageWatchTimePerVideoView",
    "AverageWatchTimePerImpression", "CostPerSale", "RevenuePerSale",
    "CostPerInstall", "RevenuePerInstall", "PostClickDownloadRate",
    "CostPerDownload", "PostClickInstallRate", "CPI", "PostInstallPurchaseRate",
    "CPP", "PostInstallSubscriptionRate", "CPS", "NewCustomerConversionRate",
    "NewCustomerCPA", "NewCustomerReturnOnAdSpend", "ConversionDelay",
    "AbsoluteTopImpressionRatePercent", "TopImpressionRatePercent",
    "HistoricalQualityScore", "HistoricalExpectedCtr", "HistoricalAdRelevance",
    "HistoricalLandingPageExperience",
    *IMPRESSION_SHARE_STATISTICS,
})

# ---------------------------------------------------------------------------
# Section assignment (Supermetrics-mirrored section names, dossier section 6,
# + two official-only sections + REPORT SURFACES).
# ---------------------------------------------------------------------------

_SECTION_BY_COLUMN: dict[str, str] = {}


def _assign(section: str, *columns: str) -> None:
    for column in columns:
        _SECTION_BY_COLUMN[column] = section


_assign("TIME", "TimePeriod")
_assign("ACCOUNT", "AccountName", "AccountNumber", "AccountId", "AccountStatus",
        "CustomerId", "CustomerName", "CurrencyCode")
_assign("CAMPAIGN", "CampaignName", "CampaignId", "CampaignStatus",
        "CampaignType", "CampaignLabels", "BaseCampaignId", "AssetGroupId",
        "AssetGroupName", "AssetGroupStatus")
_assign("AD GROUP", "AdGroupName", "AdGroupId", "AdGroupStatus", "AdGroupLabels",
        "AdGroupType", "Status")
_assign("AD", "AdId", "AdTitle", "AdDescription", "AdDescription2", "AdType",
        "AdStatus", "TitlePart1", "TitlePart2", "TitlePart3", "Headline",
        "LongHeadline", "BusinessName", "Path1", "Path2", "AdLabels",
        "DisplayUrl", "AdStrength", "AdStrengthActionItems")
_assign("BASIC", "Impressions", "Clicks", "Ctr", "AverageCpc", "Spend",
        "AveragePosition", "AverageCpm", "Assists", "ExtendedCost")
_assign("CONVERSIONS", "Conversions", "ConversionRate", "CostPerConversion",
        "ConversionsQualified", "CostPerAssist", "Revenue", "ReturnOnAdSpend",
        "RevenuePerConversion", "RevenuePerAssist", "AllConversions",
        "AllRevenue", "AllConversionRate", "AllCostPerConversion",
        "AllReturnOnAdSpend", "AllRevenuePerConversion", "AllConversionsQualified",
        "ViewThroughConversions", "ViewThroughConversionsQualified",
        "ViewThroughRevenue", "ViewThroughRate", "Sales", "CostPerSale",
        "RevenuePerSale")
_assign("KEYWORD", "Keyword", "KeywordId", "KeywordStatus", "KeywordLabels",
        "CurrentMaxCpc", "Mainline1Bid", "MainlineBid", "FirstPageBid",
        "QualityScore", "ExpectedCtr", "AdRelevance", "LandingPageExperience",
        "QualityImpact", "HistoricalQualityScore", "HistoricalExpectedCtr",
        "HistoricalAdRelevance", "HistoricalLandingPageExperience")
_assign("QUERY", "SearchQuery", "AdGroupCriterionId")
_assign("DESTINATION", "DestinationUrl", "FinalUrl", "FinalMobileUrl",
        "FinalAppUrl", "TrackingTemplate", "CustomParameters", "FinalUrlSuffix")
_assign("LOCATION", "Country", "State", "MetroArea", "City", "County",
        "PostalCode", "LocationId", "LocationType", "MostSpecificLocation",
        "ProximityTargetLocation", "Radius", "Neighborhood")
_assign("VISITOR", "AgeGroup", "Gender", "DeviceType", "DeviceOS", "Language",
        "AdDistribution", "Network", "TopVsOther", "BidMatchType",
        "DeliveredMatchType")
_assign("GOAL", "Goal", "GoalType", "GoalId")
_assign("PROMINENCE", *IMPRESSION_SHARE_STATISTICS,
        "AbsoluteTopImpressionRatePercent", "TopImpressionRatePercent")
_assign("BUDGET", "BudgetName", "BudgetStatus", "BudgetAssociationStatus",
        "BidStrategyType")
_assign("CALL TRACKING", "PhoneImpressions", "PhoneCalls", "Ptr")
_assign("LOW QUALITY TRAFFIC", "LowQualityClicks", "LowQualityClicksPercent",
        "LowQualityImpressions", "LowQualityImpressionsPercent",
        "LowQualityConversions", "LowQualityConversionRate",
        "LowQualityGeneralClicks", "LowQualitySophisticatedClicks",
        "LowQualityConversionsQualified")
_assign("VIDEO VIEW", "VideoViews", "AverageCPV", "VideoViewsAt25Percent",
        "VideoViewsAt50Percent", "VideoViewsAt75Percent", "CompletedVideoViews",
        "VideoCompletionRate", "TotalWatchTimeInMS",
        "AverageWatchTimePerVideoView", "AverageWatchTimePerImpression")
_assign("APP CAMPAIGNS", "Installs", "CostPerInstall", "RevenuePerInstall",
        "Downloads", "PostClickDownloadRate", "CostPerDownload", "AppInstalls",
        "PostClickInstallRate", "CPI", "Purchases", "PostInstallPurchaseRate",
        "CPP", "Subscriptions", "PostInstallSubscriptionRate", "CPS")
_assign("CONVERSIONS", "ConversionDelay")
_assign("NEW CUSTOMER", "NewCustomerConversions", "NewCustomerRevenue",
        "NewCustomerConversionRate", "NewCustomerCPA",
        "NewCustomerReturnOnAdSpend", "UnknownCustomerConversions",
        "UnknownCustomerRevenue", "NewCustomerCount", "NewCustomerSpend")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SNAKE_1 = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_SNAKE_2 = re.compile(r"(?<=[A-Z])(?=[A-Z][a-z])")


def snake_case(name: str) -> str:
    """PascalCase provider column -> stable snake_case field_id."""
    return _SNAKE_2.sub("_", _SNAKE_1.sub("_", name)).lower()


_SHORT_REPORT = re.compile(r"ReportRequest$")


def report_short(report_type: str) -> str:
    return _SHORT_REPORT.sub("", report_type)


def _data_type(column: str) -> str:
    if column in DATE_COLUMNS:
        return "date"
    if column in INTEGER_COLUMNS:
        return "int"
    if column in STATISTIC_COLUMNS:
        return "decimal"
    if column in DECIMAL_ATTRIBUTE_COLUMNS:
        return "decimal"
    return "string"


_NON_ADDITIVE_NOTE = (
    " Provider-computed ratio/derived/averaged statistic: NON-ADDITIVE (AD-4)"
    " -- never SUM across rows; lands RAW at day grain when selected; prefer"
    " semantic-layer computation from stored numerators and denominators."
    " Percent-valued CSV cells carry a trailing '%' -- coerced by CATALOG"
    " physical_type (decimal), never by column-name suffix."
)


def build_fields() -> tuple[list[dict], dict[str, list[str]]]:
    """Return (official_fields, report_type -> sorted field_id list)."""
    # Verify the frozen dossier counts first (loud transcription guard).
    for report_type, (columns, expected) in COVERED_REPORTS.items():
        if len(columns) != expected:
            raise ValueError(
                f"{report_type}: column count drifted "
                f"({len(columns)} != {expected})"
            )
        if len(set(columns)) != len(columns):
            dupes = sorted({c for c in columns if columns.count(c) > 1})
            raise ValueError(f"{report_type}: duplicate columns {dupes}")
    if len(EXCLUDED_REPORT_TYPES) != EXPECTED_EXCLUDED_REPORT_COUNT:
        raise ValueError(
            f"excluded report types drifted: {len(EXCLUDED_REPORT_TYPES)}"
            f" != {EXPECTED_EXCLUDED_REPORT_COUNT}"
        )
    if (
        len(COVERED_REPORTS) + len(EXCLUDED_REPORT_TYPES)
        != EXPECTED_TOTAL_REPORT_COUNT
    ):
        raise ValueError("covered + excluded must equal the 48-object inventory")

    # Per-column availability across the covered reports (declaration order).
    availability: dict[str, list[str]] = {}
    for report_type, (columns, _expected) in COVERED_REPORTS.items():
        for column in columns:
            availability.setdefault(column, []).append(report_short(report_type))

    out: list[dict] = []
    report_field_ids: dict[str, list[str]] = {
        rt: sorted(snake_case(c) for c in cols)
        for rt, (cols, _e) in COVERED_REPORTS.items()
    }

    seen_ids: set[str] = set()
    for column in sorted(availability):
        field_id = snake_case(column)
        if field_id in seen_ids:
            raise ValueError(f"snake_case collision on field_id {field_id!r}")
        seen_ids.add(field_id)

        kind = "metric" if column in STATISTIC_COLUMNS else "dimension"
        section = _SECTION_BY_COLUMN.get(column)
        if section is None:
            raise ValueError(f"column {column!r} has no section assignment")
        reports = availability[column]
        desc = (
            f"Microsoft Ads v13 report column '{column}' "
            f"({'performance statistic' if kind == 'metric' else 'attribute'}"
            " -- guide 'Columns that group the data'). Available in: "
            + ", ".join(reports) + "."
        )
        if column in _NON_ADDITIVE_EXACT:
            desc += _NON_ADDITIVE_NOTE
        if column == "TimePeriod":
            desc += (
                " Required for every aggregation except Summary; carries the"
                " Daily bucket date (canonical 'date')."
            )
        entry = {
            "field_id": field_id,
            "kind": kind,
            "data_type": _data_type(column),
            "description": desc,
            "section": section,
            "source_field": column,
        }
        out.append(entry)

    # The 40 next-tier report surfaces: declared (never silently absent),
    # excluded with a report-type-next-tier reason via catalog_sources.json.
    for report_type in sorted(EXCLUDED_REPORT_TYPES):
        short = report_short(report_type)
        field_id = "report_surface_" + snake_case(short)
        if field_id in seen_ids:
            raise ValueError(f"report surface id collision {field_id!r}")
        seen_ids.add(field_id)
        out.append({
            "field_id": field_id,
            "kind": "dimension",
            "data_type": "string",
            "description": (
                f"Report surface marker for '{report_type}' (v13 reporting"
                " data object). This report type exists in the API but its"
                " column enum is NOT yet integrally cataloged -- it is"
                " excluded (report-type-next-tier), never planned. The 8"
                " covered report types are the executable surface of"
                " catalog_daily."
            ),
            "section": "REPORT SURFACES",
            "source_field": report_type,
        })

    out.sort(key=lambda e: e["field_id"])
    return out, report_field_ids


def build_field_compatibility(report_field_ids: dict[str, list[str]]) -> dict:
    """The core-contract field_compatibility block (schema_version 1)."""
    impression_share_ids = sorted(
        snake_case(c) for c in IMPRESSION_SHARE_STATISTICS
    )
    restricted_ids = sorted(snake_case(c) for c in RESTRICTED_ATTRIBUTES)
    audience_ids = sorted(snake_case(c) for c in AUDIENCE_SHARE_COLUMNS)
    audience_excluded = sorted(
        snake_case(c) for c in AUDIENCE_SHARE_EXCLUDED_ATTRIBUTES
    )

    rules: list[dict] = [
        {
            "id": "impression-share-vs-restricted-attributes",
            "kind": "mutually_exclusive",
            "_source": (
                "https://learn.microsoft.com/en-us/advertising/guides/"
                "reports?view=bingads-13#columnrestrictions"
            ),
            "sets": [impression_share_ids, restricted_ids],
        },
        {
            "id": "audience-share-vs-customer-attributes",
            "kind": "mutually_exclusive",
            "_source": (
                "https://learn.microsoft.com/en-us/advertising/guides/"
                "reports?view=bingads-13#columnrestrictions"
            ),
            "sets": [audience_ids, audience_excluded],
        },
    ]
    for report_type in sorted(report_field_ids):
        rules.append({
            "id": "columns-" + snake_case(report_short(report_type)).replace(
                "_", "-"
            ),
            "kind": "selectable_set",
            "scope": {"report_type": report_type},
            "allowed_fields": report_field_ids[report_type],
        })

    return {
        "schema_version": "1",
        "_note": (
            "GENERATED by build_official_fields.py -- do not edit by hand."
            " Core contract (core/field_compat.py, schemas/"
            "field-compatibility.schema.json): mutually_exclusive = the"
            " Column Restrictions (impression-share statistics vs restricted"
            " attributes; audience-share family vs customer attributes);"
            " selectable_set x8 (scope {'report_type': ...}) = the INTEGRAL"
            " per-report column enums, so an out-of-enum selection is refused"
            " pre-call by core.field_compat.enforce_selection with context."
            " The >=1 attribute + >=1 non-impression-share statistic request"
            " invariant is not expressible in the typed rule kinds; the"
            " connector's request builder enforces it as a typed pre-call"
            " InvalidRequestError (rule id"
            " 'request-invariants-min-attr-min-stat', tested)."
            " _non_additive_statistics (F-2, review 26.4) marks every"
            " ratio/average/share/cost-per/historical statistic (AD-4"
            " superset of _impression_share_statistics): the connector"
            " refuses report-type inference when several covering types of"
            " different grains exist AND one of these is selected."
        ),
        "_impression_share_statistics": impression_share_ids,
        "_non_additive_statistics": sorted(
            snake_case(c) for c in _NON_ADDITIVE_EXACT
        ),
        "rules": rules,
    }


def build_descriptive_mutable() -> dict:
    """Story 26.6 Part A: the top-level descriptive_mutable block.

    Entity STATUS/TYPE attributes that MUTATE across the refetch ladder re-pulls
    (a campaign Active on a day re-pulled as Paused for the SAME day). The
    connector lands them in a separate attributes_json column (latest-wins, OUT
    of the dbt QUALIFY supersede grain) instead of segments_json (a partition
    key), so a status change on a re-pull never forks the grain and doubles the
    window's metrics (finding F-1). Grain ids/names and SEGMENTING dimensions
    (age/geo/device/network/match type/time) are NEVER listed here.

    Loud guards: a listed column that is not a cataloged attribute of a covered
    report, or that classified as a statistic, is a transcription bug.
    """
    all_columns: set[str] = set()
    for _cols, _e in COVERED_REPORTS.values():
        all_columns.update(_cols)
    for column in DESCRIPTIVE_MUTABLE_COLUMNS:
        if column not in all_columns:
            raise ValueError(
                f"descriptive_mutable column {column!r} is not in any covered "
                "report enum (transcription drift)"
            )
        if column in STATISTIC_COLUMNS:
            raise ValueError(
                f"descriptive_mutable column {column!r} classified as a "
                "statistic -- only attributes can be descriptive_mutable"
            )
        # A grain-bearing *_Id must never be descriptive_mutable.
        if column.endswith("Id"):
            raise ValueError(
                f"grain-bearing column {column!r} must never be "
                "descriptive_mutable"
            )
    field_ids = sorted(snake_case(c) for c in DESCRIPTIVE_MUTABLE_COLUMNS)
    return {
        "schema_version": "1",
        "_note": (
            "Story 26.6 Part A -- GENERATED by build_official_fields.py (do not"
            " edit by hand). Entity STATUS/TYPE attributes that mutate over time"
            " for a fixed grain (campaign/ad group/ad/keyword status,"
            " campaign/ad group/ad type, account/budget/asset-group status): the"
            " connector lands them in attributes_json (latest-wins, HORS"
            " partition) instead of segments_json (the QUALIFY supersede grain"
            " key), so a refetch re-reading a window with a changed status does"
            " NOT double the window's metrics. Grain-bearing ids/names and"
            " segmenting dimensions (age/geo/device/network/match type/time) are"
            " NEVER listed here."
        ),
        "field_ids": field_ids,
    }


def build_excluded_fields() -> dict[str, str]:
    """excluded_fields (field_id -> reason) for the 40 report surfaces."""
    out: dict[str, str] = {}
    for report_type in sorted(EXCLUDED_REPORT_TYPES):
        field_id = "report_surface_" + snake_case(report_short(report_type))
        out[field_id] = (
            f"report-type-next-tier: {report_type} is a distinct v13 report"
            " surface whose column enum is not yet integrally cataloged"
            " (the catalog integrally covers the 8 primary report types:"
            " Campaign, Account, AdGroup, Ad, Keyword, SearchQuery,"
            " Geographic, AgeGenderAudience). It ships in a next tier with"
            " its full live XSD enum -- never 'planned'."
        )
    return out


def main() -> None:
    here = Path(__file__).parent
    fields, report_field_ids = build_fields()

    official_path = here / "official_fields.json"
    official_path.write_text(
        json.dumps(fields, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )

    # Regenerate the derived blocks inside catalog_sources.json (single
    # source of truth = this script's column tuples; zero drift possible).
    sources_path = here / "catalog_sources.json"
    sources = json.loads(sources_path.read_text(encoding="utf-8"))
    sources["field_compatibility"] = build_field_compatibility(report_field_ids)
    sources["excluded_fields"] = build_excluded_fields()
    sources["descriptive_mutable"] = build_descriptive_mutable()
    sources_path.write_text(
        json.dumps(sources, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )

    metrics = sum(1 for f in fields if f["kind"] == "metric")
    dims = sum(1 for f in fields if f["kind"] == "dimension")
    surfaces = sum(
        1 for f in fields if f["field_id"].startswith("report_surface_")
    )
    print(
        f"Wrote {len(fields)} fields ({metrics} metrics / {dims} dimensions,"
        f" of which {surfaces} excluded report surfaces) to {official_path}"
    )
    union = len(fields) - surfaces
    print(f"Distinct columns across the 8 covered reports: {union}")
    for report_type in sorted(report_field_ids):
        print(f"  {report_type}: {len(report_field_ids[report_type])} columns")


if __name__ == "__main__":
    main()
