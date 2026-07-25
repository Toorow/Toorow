"""Deterministic builder for google-ads official_fields.json (Story 26.2).

This script is the AUDITABLE SOURCE for the committed official snapshot. It is
committed alongside its output (official_fields.json) so a reviewer can see
exactly how the curated list was derived from the official Google Ads API v24
protos recorded in catalog_sources.json.

It is pure-Python, deterministic (no network, no clock), and byte-stable:
running it again reproduces official_fields.json exactly.

Derivation summary (see catalog_sources.json "_derivation"):
  - METRIC fields = every field of ``message Metrics`` in
    google/ads/googleads/v24/common/metrics.proto -- 278 in v24 (research
    dossier Annex A, fetched 2026-07-21). ZERO truncation: the METRICS tuple
    below is the integral Annex A list.
  - SEGMENT dimensions = every field of ``message Segments`` in
    google/ads/googleads/v24/common/segments.proto -- 151 in v24 (Annex B,
    integral). The four message-typed segments (keyword,
    asset_interaction_target, sk_ad_network_source_app,
    budget_campaign_association_status) are declared ONCE each (they are one
    proto field each) with their primary flattened GAQL path as source_field.
  - STRUCTURAL dimensions = curated attribute fields of the 5 report-profile
    resources (customer, campaign, ad_group, ad_group_ad, ad_group_criterion
    via keyword_view, search_term_view) -- ids/names/status/type needed by the
    exact_bundle profiles. GAQL has a single flat namespace: segments and
    attributes go in the same SELECT as metrics; the FROM resource decides
    legality (catalog_sources.json field_compatibility).
  - Sections are assigned from the ordered prefix-family rules transposed from
    the research draft (_section_prefix_rules); the protos carry no sections.
  - physical types: *_micros -> integer (int64 micros; landed as value/1e6 --
    the platform micros rule, documented per field); curated INT64/ENUM/DATE
    overrides; count-suffix heuristic; default decimal for metric doubles and
    string for segment enums. The orchestrator's per-version regen re-verifies
    types against the fetched protos.
  - money-micros DECLARATION (review 26.2 F-2): every monetary field gets an
    explicit ``"physical_type": "money_micros"`` marker in the snapshot, from
    the EXPLICIT list MONEY_MICROS_METRICS below (the *_micros int64 family
    PLUS the monetary DOUBLES that are micros on the wire without the suffix:
    average_cpc/cpm/cost/cpe, active_view_cpm, the cost_per_* family). The
    connector's transform() divides by 1e6 ON THIS DECLARATION ONLY -- never
    on a name suffix. value_per_* metrics are conversion VALUES (currency
    units, not micros) and are deliberately NOT in the list. Final unit
    ratification field-by-field happens at the 25.6 live probe
    (catalog_sources/ROLLOUT_NOTES.md).
  - field_compatibility rules (review 26.2 F-1): this builder ALSO regenerates
    the ``rules`` array of the ``field_compatibility`` block in
    catalog_sources.json (core 26.1 schema '1': one selectable_set rule per v1
    profile FROM resource, allowed_fields enumerated from this snapshot + the
    _gaql_* scoping metadata). Rules are generated, metadata is human-owned.

Run (orchestrator, local only -- both outputs are committed):
    uv run python server/modules/google-ads/catalog_sources/build_official_fields.py
"""

from __future__ import annotations

import json
from pathlib import Path

# ---------------------------------------------------------------------------
# Sections (29, exactly the section_tier_map keys in catalog_sources.json).
# ---------------------------------------------------------------------------
S_COST = "COST"
S_CLICKS = "CLICKS"
S_IMPRESSIONS = "IMPRESSIONS"
S_CONVERSIONS = "CONVERSIONS"
S_STRUCTURE = "STRUCTURE"
S_TIME = "TIME"
S_NETWORK = "NETWORK & DEVICE"
S_GEO = "GEO"
S_KEYWORD = "KEYWORD & SEARCH"
S_IMPR_SHARE = "IMPRESSION SHARE"
S_QUALITY = "QUALITY SCORE"
S_BUDGET = "BUDGET & BIDDING"
S_ACTIVE_VIEW = "ACTIVE VIEW"
S_VIDEO = "VIDEO"
S_SHOPPING = "SHOPPING & PRODUCT"
S_COMMERCE = "COMMERCE & PROFIT"
S_HOTEL = "HOTEL & TRAVEL"
S_SKAN = "SKAN"
S_LOCATION = "LOCATION ASSETS"
S_CALLS = "CALLS & MESSAGE"
S_GMAIL = "GMAIL"
S_ORGANIC = "ORGANIC & COMBINED"
S_AUCTION = "AUCTION INSIGHT"
S_EXPERIMENT = "EXPERIMENT"
S_REACH = "REACH & FREQUENCY"
S_ASSET = "ASSET INTERACTION"
S_VERTICAL = "VERTICAL ADS"
S_CONV_DETAIL = "CONVERSION DETAIL"
S_MISC = "MISC"

# ---------------------------------------------------------------------------
# 1. METRICS -- the INTEGRAL v24 list (research dossier Annex A, 278 fields).
#    GAQL token = "metrics.<name>". Zero truncation (story 26.2 hard rule).
# ---------------------------------------------------------------------------
METRICS: tuple[str, ...] = (
    "absolute_top_impression_percentage",
    "active_view_audibility_invalid_givt_measurable_impressions_rate",
    "active_view_audibility_invalid_measurable_impressions_rate",
    "active_view_audibility_measurable_impressions",
    "active_view_audibility_measurable_impressions_rate",
    "active_view_audible_impressions",
    "active_view_audible_impressions_rate",
    "active_view_audible_quartile_p100_rate",
    "active_view_audible_quartile_p25_rate",
    "active_view_audible_quartile_p50_rate",
    "active_view_audible_quartile_p75_rate",
    "active_view_audible_thirty_seconds_impressions",
    "active_view_audible_thirty_seconds_impressions_rate",
    "active_view_audible_two_seconds_impressions",
    "active_view_audible_two_seconds_impressions_rate",
    "active_view_cpm",
    "active_view_ctr",
    "active_view_impressions",
    "active_view_measurability",
    "active_view_measurable_cost_micros",
    "active_view_measurable_impressions",
    "active_view_viewability",
    "all_average_cart_size",
    "all_average_order_value_micros",
    "all_conversions",
    "all_conversions_by_conversion_date",
    "all_conversions_from_click_to_call",
    "all_conversions_from_directions",
    "all_conversions_from_interactions_rate",
    "all_conversions_from_interactions_value_per_interaction",
    "all_conversions_from_location_asset_click_to_call",
    "all_conversions_from_location_asset_directions",
    "all_conversions_from_location_asset_menu",
    "all_conversions_from_location_asset_order",
    "all_conversions_from_location_asset_other_engagement",
    "all_conversions_from_location_asset_store_visits",
    "all_conversions_from_location_asset_website",
    "all_conversions_from_menu",
    "all_conversions_from_order",
    "all_conversions_from_other_engagement",
    "all_conversions_from_store_visit",
    "all_conversions_from_store_website",
    "all_conversions_value",
    "all_conversions_value_by_conversion_date",
    "all_conversions_value_per_cost",
    "all_cost_of_goods_sold_micros",
    "all_cross_sell_cost_of_goods_sold_micros",
    "all_cross_sell_gross_profit_micros",
    "all_cross_sell_revenue_micros",
    "all_cross_sell_units_sold",
    "all_gross_profit_margin",
    "all_gross_profit_micros",
    "all_lead_cost_of_goods_sold_micros",
    "all_lead_gross_profit_micros",
    "all_lead_revenue_micros",
    "all_lead_units_sold",
    "all_new_customer_lifetime_value",
    "all_orders",
    "all_revenue_micros",
    "all_units_sold",
    "all_value_adjustment",
    "asset_pinned_as_description_position_one_count",
    "asset_pinned_as_description_position_two_count",
    "asset_pinned_as_headline_position_one_count",
    "asset_pinned_as_headline_position_three_count",
    "asset_pinned_as_headline_position_two_count",
    "asset_pinned_total_count",
    "auction_insight_search_absolute_top_impression_percentage",
    "auction_insight_search_impression_share",
    "auction_insight_search_outranking_share",
    "auction_insight_search_overlap_rate",
    "auction_insight_search_position_above_rate",
    "auction_insight_search_top_impression_percentage",
    "average_cart_size",
    "average_cost",
    "average_cpc",
    "average_cpe",
    "average_cpm",
    "average_impression_frequency_per_user",
    "average_order_value_micros",
    "average_page_views",
    "average_target_cpa_micros",
    "average_target_roas",
    "average_time_on_site",
    "average_video_watch_time_duration_millis",
    "benchmark_average_max_cpc",
    "benchmark_ctr",
    "biddable_app_install_conversions",
    "biddable_app_post_install_conversions",
    "biddable_cohort_app_post_install_conversions",
    "biddable_indirect_install_first_in_app_conversion_micros",
    "bounce_rate",
    "clicks",
    "clicks_margin_of_error",
    "clicks_p_value",
    "clicks_point_estimate",
    "clicks_unique_query_clusters",
    "combined_clicks",
    "combined_clicks_per_query",
    "combined_queries",
    "content_budget_lost_impression_share",
    "content_impression_share",
    "content_rank_lost_impression_share",
    "control_clicks",
    "control_conversion_value",
    "control_conversion_value_per_cost",
    "control_conversions",
    "control_cost_micros",
    "control_cost_per_conversion",
    "control_impressions",
    "conversion_last_conversion_date",
    "conversion_last_received_request_date_time",
    "conversion_value_change_point_estimate",
    "conversion_value_margin_of_error",
    "conversion_value_p_value",
    "conversion_value_per_cost_change_point_estimate",
    "conversion_value_per_cost_margin_of_error",
    "conversion_value_per_cost_p_value",
    "conversions",
    "conversions_absolute_change_margin_of_error",
    "conversions_absolute_change_p_value",
    "conversions_absolute_change_point_estimate",
    "conversions_by_conversion_date",
    "conversions_from_interactions_rate",
    "conversions_from_interactions_value_per_interaction",
    "conversions_unique_query_clusters",
    "conversions_value",
    "conversions_value_by_conversion_date",
    "conversions_value_per_cost",
    "cost_converted_currency_per_platform_comparable_conversion",
    "cost_micros",
    "cost_micros_change_point_estimate",
    "cost_micros_margin_of_error",
    "cost_micros_p_value",
    "cost_of_goods_sold_micros",
    "cost_per_all_conversions",
    "cost_per_conversion",
    "cost_per_conversion_change_point_estimate",
    "cost_per_conversion_margin_of_error",
    "cost_per_conversion_p_value",
    "cost_per_current_model_attributed_conversion",
    "cost_per_platform_comparable_conversion",
    "coviewed_impressions",
    "cross_device_conversions",
    "cross_device_conversions_by_conversion_date",
    "cross_device_conversions_value",
    "cross_device_conversions_value_by_conversion_date",
    "cross_device_conversions_value_micros",
    "cross_sell_cost_of_goods_sold_micros",
    "cross_sell_gross_profit_micros",
    "cross_sell_revenue_micros",
    "cross_sell_units_sold",
    "ctr",
    "current_model_attributed_conversions",
    "current_model_attributed_conversions_from_interactions_rate",
    "current_model_attributed_conversions_from_interactions_value_per_interaction",
    "current_model_attributed_conversions_value",
    "current_model_attributed_conversions_value_per_cost",
    "eligible_impressions_from_location_asset_store_reach",
    "engagement_rate",
    "engagements",
    "general_invalid_click_rate",
    "general_invalid_clicks",
    "gmail_forwards",
    "gmail_saves",
    "gmail_secondary_clicks",
    "gross_profit_margin",
    "gross_profit_micros",
    "historical_creative_quality_score",
    "historical_landing_page_quality_score",
    "historical_quality_score",
    "historical_search_predicted_ctr",
    "hotel_average_lead_value_micros",
    "hotel_commission_rate_micros",
    "hotel_eligible_impressions",
    "hotel_expected_commission_cost",
    "hotel_price_difference_percentage",
    "impressions",
    "impressions_from_store_reach",
    "impressions_margin_of_error",
    "impressions_p_value",
    "impressions_point_estimate",
    "impressions_unique_query_clusters",
    "interaction_event_types",
    "interaction_rate",
    "interactions",
    "invalid_click_rate",
    "invalid_clicks",
    "lead_cost_of_goods_sold_micros",
    "lead_gross_profit_micros",
    "lead_revenue_micros",
    "lead_units_sold",
    "linked_entities_count",
    "linked_sample_entities",
    "message_chat_rate",
    "message_chats",
    "message_impressions",
    "mobile_friendly_clicks_percentage",
    "new_customer_lifetime_value",
    "optimization_score_uplift",
    "optimization_score_url",
    "orders",
    "organic_clicks",
    "organic_clicks_per_query",
    "organic_impressions",
    "organic_impressions_per_query",
    "organic_queries",
    "percent_new_visitors",
    "phone_calls",
    "phone_impressions",
    "phone_through_rate",
    "platform_comparable_conversions",
    "platform_comparable_conversions_by_conversion_date",
    "platform_comparable_conversions_from_interactions_rate",
    "platform_comparable_conversions_from_interactions_value_per_interaction",
    "platform_comparable_conversions_value",
    "platform_comparable_conversions_value_by_conversion_date",
    "platform_comparable_conversions_value_per_cost",
    "primary_impressions",
    "publisher_organic_clicks",
    "publisher_purchased_clicks",
    "publisher_unknown_clicks",
    "relative_ctr",
    "results_conversions_purchase",
    "revenue_micros",
    "search_absolute_top_impression_share",
    "search_budget_lost_absolute_top_impression_share",
    "search_budget_lost_impression_share",
    "search_budget_lost_top_impression_share",
    "search_click_share",
    "search_exact_match_impression_share",
    "search_impression_share",
    "search_rank_lost_absolute_top_impression_share",
    "search_rank_lost_impression_share",
    "search_rank_lost_top_impression_share",
    "search_top_impression_share",
    "search_volume",
    "sk_ad_network_installs",
    "sk_ad_network_total_conversions",
    "speed_score",
    "store_visits_last_click_model_attributed_conversions",
    "svr",
    "top_impression_percentage",
    "trueview_average_cpv",
    "unique_users",
    "unique_users_five_plus",
    "unique_users_four_plus",
    "unique_users_ten_plus",
    "unique_users_three_plus",
    "unique_users_two_plus",
    "units_sold",
    "valid_accelerated_mobile_pages_clicks_percentage",
    "value_adjustment",
    "value_per_all_conversions",
    "value_per_all_conversions_by_conversion_date",
    "value_per_conversion",
    "value_per_conversions_by_conversion_date",
    "value_per_current_model_attributed_conversion",
    "value_per_platform_comparable_conversion",
    "value_per_platform_comparable_conversions_by_conversion_date",
    "video_quartile_p100_rate",
    "video_quartile_p25_rate",
    "video_quartile_p50_rate",
    "video_quartile_p75_rate",
    "video_trueview_view_rate",
    "video_trueview_view_rate_in_feed",
    "video_trueview_view_rate_in_stream",
    "video_trueview_view_rate_shorts",
    "video_trueview_views",
    "video_watch_time_duration_millis",
    "view_through_conversions",
    "view_through_conversions_from_location_asset_click_to_call",
    "view_through_conversions_from_location_asset_directions",
    "view_through_conversions_from_location_asset_menu",
    "view_through_conversions_from_location_asset_order",
    "view_through_conversions_from_location_asset_other_engagement",
    "view_through_conversions_from_location_asset_store_visits",
    "view_through_conversions_from_location_asset_website",
)

EXPECTED_METRIC_COUNT = 278

# ---------------------------------------------------------------------------
# 2. SEGMENTS -- the INTEGRAL v24 list (research dossier Annex B, 151 fields).
#    GAQL token = "segments.<name>". The 4 message-typed segments carry their
#    primary flattened sub-field path as source_field (they flatten at query
#    time, e.g. segments.keyword.info.text).
# ---------------------------------------------------------------------------
SEGMENTS: tuple[str, ...] = (
    "activity_account_id",
    "activity_city",
    "activity_country",
    "activity_rating",
    "activity_state",
    "ad_destination_type",
    "ad_format_type",
    "ad_group",
    "ad_network_type",
    "ad_sub_network_type",
    "ad_using_product_data",
    "ad_using_video",
    "adjusted_age_range",
    "adjusted_gender",
    "asset_group",
    "asset_interaction_target",
    "auction_insight_domain",
    "budget_campaign_association_status",
    "campaign",
    "click_type",
    "conversion_action",
    "conversion_action_category",
    "conversion_action_name",
    "conversion_adjustment",
    "conversion_attribution_event_type",
    "conversion_lag_bucket",
    "conversion_or_adjustment_lag_bucket",
    "conversion_value_rule_primary_dimension",
    "date",
    "day_of_week",
    "device",
    "external_activity_id",
    "external_conversion_source",
    "geo_target_airport",
    "geo_target_canton",
    "geo_target_city",
    "geo_target_country",
    "geo_target_county",
    "geo_target_district",
    "geo_target_metro",
    "geo_target_most_specific_location",
    "geo_target_postal_code",
    "geo_target_province",
    "geo_target_region",
    "geo_target_state",
    "hotel_booking_window_days",
    "hotel_center_id",
    "hotel_check_in_date",
    "hotel_check_in_day_of_week",
    "hotel_city",
    "hotel_class",
    "hotel_country",
    "hotel_date_selection_type",
    "hotel_length_of_stay",
    "hotel_price_bucket",
    "hotel_rate_rule_id",
    "hotel_rate_type",
    "hotel_state",
    "hour",
    "interaction_on_this_extension",
    "keyword",
    "landing_page_source",
    "match_type",
    "mobile_device_platform",
    "month",
    "month_of_year",
    "new_versus_returning_customers",
    "partner_hotel_id",
    "product_aggregator_id",
    "product_brand",
    "product_category_level1",
    "product_category_level2",
    "product_category_level3",
    "product_category_level4",
    "product_category_level5",
    "product_channel",
    "product_channel_exclusivity",
    "product_condition",
    "product_country",
    "product_custom_attribute0",
    "product_custom_attribute1",
    "product_custom_attribute2",
    "product_custom_attribute3",
    "product_custom_attribute4",
    "product_feed_label",
    "product_item_id",
    "product_language",
    "product_merchant_id",
    "product_sold_brand",
    "product_sold_category_level1",
    "product_sold_category_level2",
    "product_sold_category_level3",
    "product_sold_category_level4",
    "product_sold_category_level5",
    "product_sold_condition",
    "product_sold_custom_attribute0",
    "product_sold_custom_attribute1",
    "product_sold_custom_attribute2",
    "product_sold_custom_attribute3",
    "product_sold_custom_attribute4",
    "product_sold_item_id",
    "product_sold_title",
    "product_sold_type_l1",
    "product_sold_type_l2",
    "product_sold_type_l3",
    "product_sold_type_l4",
    "product_sold_type_l5",
    "product_store_id",
    "product_title",
    "product_type_l1",
    "product_type_l2",
    "product_type_l3",
    "product_type_l4",
    "product_type_l5",
    "quarter",
    "recommendation_type",
    "search_engine_results_page_type",
    "search_subcategory",
    "search_term",
    "search_term_match_source",
    "search_term_match_type",
    "search_term_targeting_status",
    "sk_ad_network_ad_event_type",
    "sk_ad_network_attribution_credit",
    "sk_ad_network_coarse_conversion_value",
    "sk_ad_network_fine_conversion_value",
    "sk_ad_network_postback_sequence_index",
    "sk_ad_network_redistributed_fine_conversion_value",
    "sk_ad_network_source_app",
    "sk_ad_network_source_domain",
    "sk_ad_network_source_type",
    "sk_ad_network_user_type",
    "sk_ad_network_version",
    "slot",
    "travel_destination_city",
    "travel_destination_country",
    "travel_destination_region",
    "vertical_ads_event_participant_display_names",
    "vertical_ads_hotel_class",
    "vertical_ads_listing",
    "vertical_ads_listing_brand",
    "vertical_ads_listing_city",
    "vertical_ads_listing_country",
    "vertical_ads_listing_region",
    "vertical_ads_listing_user_rating",
    "vertical_ads_listing_venue",
    "vertical_ads_partner_account",
    "vertical_ads_vertical",
    "webpage",
    "week",
    "year",
)

EXPECTED_SEGMENT_COUNT = 151

# Message-typed segments (flatten to sub-fields at query time): primary
# flattened GAQL path used as source_field, documented in the description.
MESSAGE_SEGMENT_SOURCE: dict[str, str] = {
    "keyword": "segments.keyword.info.text",
    "asset_interaction_target": "segments.asset_interaction_target.asset",
    "sk_ad_network_source_app": (
        "segments.sk_ad_network_source_app.sk_ad_network_source_app_id"
    ),
    "budget_campaign_association_status": (
        "segments.budget_campaign_association_status.status"
    ),
}

# ---------------------------------------------------------------------------
# 3. STRUCTURAL dimensions -- curated attributes of the 5 profile resources.
#    (field_id, source_field, data_type, section, description)
# ---------------------------------------------------------------------------
STRUCTURAL_DIMS: list[tuple[str, str, str, str, str]] = [
    ("customer_id", "customer.id", "id", S_STRUCTURE,
     "Reporting customer (client account) ID."),
    ("customer_descriptive_name", "customer.descriptive_name", "string", S_STRUCTURE,
     "Reporting customer display name."),
    ("customer_currency_code", "customer.currency_code", "string", S_STRUCTURE,
     "Account billing currency (ISO 4217). All *_micros amounts are in this currency."),
    ("customer_time_zone", "customer.time_zone", "string", S_STRUCTURE,
     "Account reporting time zone. Daily rows are bucketed in this zone."),
    ("campaign_id", "campaign.id", "id", S_STRUCTURE, "Campaign ID."),
    ("campaign_name", "campaign.name", "string", S_STRUCTURE, "Campaign display name."),
    ("campaign_status", "campaign.status", "enum", S_STRUCTURE,
     "Campaign status (ENABLED / PAUSED / REMOVED)."),
    ("campaign_advertising_channel_type", "campaign.advertising_channel_type", "enum",
     S_STRUCTURE, "Campaign channel (SEARCH / DISPLAY / SHOPPING / VIDEO / PMAX / ...)."),
    ("campaign_bidding_strategy_type", "campaign.bidding_strategy_type", "enum",
     S_STRUCTURE, "Bidding strategy type of the campaign."),
    ("ad_group_id", "ad_group.id", "id", S_STRUCTURE, "Ad group ID."),
    ("ad_group_name", "ad_group.name", "string", S_STRUCTURE, "Ad group display name."),
    ("ad_group_status", "ad_group.status", "enum", S_STRUCTURE, "Ad group status."),
    ("ad_group_type", "ad_group.type", "enum", S_STRUCTURE, "Ad group type."),
    ("ad_id", "ad_group_ad.ad.id", "id", S_STRUCTURE, "Ad ID."),
    ("ad_name", "ad_group_ad.ad.name", "string", S_STRUCTURE, "Ad display name."),
    ("ad_type", "ad_group_ad.ad.type", "enum", S_STRUCTURE,
     "Ad type (RESPONSIVE_SEARCH_AD / RESPONSIVE_DISPLAY_AD / VIDEO_AD / ...)."),
    ("ad_final_urls", "ad_group_ad.ad.final_urls", "array", S_STRUCTURE,
     "Ad final URLs (repeated; landed as canonical JSON array)."),
    ("ad_approval_status", "ad_group_ad.policy_summary.approval_status", "enum",
     S_STRUCTURE, "Policy approval status of the ad."),
    ("ad_group_ad_status", "ad_group_ad.status", "enum", S_STRUCTURE,
     "Status of the ad within its ad group."),
    ("criterion_id", "ad_group_criterion.criterion_id", "id", S_STRUCTURE,
     "Ad group criterion ID (keyword_view PK component)."),
    ("keyword_text", "ad_group_criterion.keyword.text", "string", S_KEYWORD,
     "Keyword text (attribute of the criterion, NOT segments.keyword)."),
    ("keyword_match_type", "ad_group_criterion.keyword.match_type", "enum", S_KEYWORD,
     "Keyword match type (EXACT / PHRASE / BROAD)."),
    ("keyword_status", "ad_group_criterion.status", "enum", S_STRUCTURE,
     "Status of the keyword criterion."),
    ("quality_score", "ad_group_criterion.quality_info.quality_score", "int", S_QUALITY,
     "Keyword quality score (1-10 attribute; NULL when Google withholds it)."),
    ("search_term_text", "search_term_view.search_term", "string", S_KEYWORD,
     "Search term of the search_term_view row (attribute, NOT segments.search_term). "
     "Landed into the raw 'search_term' column (canonical mapping). Low-volume terms "
     "are omitted by Google (completeness caveat, dossier section 8)."),
    ("search_term_status", "search_term_view.status", "enum", S_STRUCTURE,
     "Search term targeting status (ADDED / EXCLUDED / NONE / UNKNOWN)."),
]

EXPECTED_STRUCTURAL_COUNT = 26

# ---------------------------------------------------------------------------
# DESCRIPTIVE-MUTABLE attributes (Story 26.6 Part A). These are STATUS/TYPE
# enums that describe the CURRENT state/type of an entity (campaign/ad group/
# ad/keyword/criterion). They are MUTABLE: a re-pull of the same window by the
# 26.1 refetch ladder can return a DIFFERENT value for a past date (a campaign
# ENABLED on the 1st, PAUSED on the 6th, re-pulled for the 1st, comes back
# PAUSED). If they landed in segments_json (part of the supersede grain) the
# changed value would create a SECOND grain row and the old one would survive
# the QUALIFY -> SUM(cost) doubled for the whole re-pulled window.
#
# So they are marked ``descriptive_mutable`` and land OUTSIDE segments_json in a
# separate ``attributes_json`` column (latest-wins, exactly like campaign_name):
# never part of the QUALIFY partition, so a status change never forks the grain.
#
# Prudent default (26.6 contract): every STRUCTURE-section state/type enum is
# descriptive_mutable. NOT ids/names (campaign_id/campaign_name -- grain keys or
# already latest-wins), NOT row-bearing segments (device/date -- distinct
# provider rows), NOT keyword_match_type (a grain-IDENTIFYING attribute of the
# criterion in the KEYWORD & SEARCH section, not a mutable state).
DESCRIPTIVE_MUTABLE_ATTRS: frozenset[str] = frozenset(
    {
        "campaign_status",
        "campaign_advertising_channel_type",
        "campaign_bidding_strategy_type",
        "ad_group_status",
        "ad_group_type",
        "ad_group_ad_status",
        "ad_type",
        "ad_approval_status",
        "keyword_status",
        "search_term_status",
    }
)

_DESCRIPTIVE_MUTABLE_NOTE = (
    " DESCRIPTIVE-MUTABLE attribute (story 26.6 Part A): a MUTABLE state/type of"
    " the entity. Lands in attributes_json (latest-wins), OUTSIDE segments_json"
    " and the dbt supersede grain -- a re-pull with a changed status never forks"
    " the grain nor double-counts metrics."
)


def _validate_descriptive_mutable(field_ids: set[str]) -> None:
    """Every descriptive_mutable id must be a real structural attribute id."""
    unknown = sorted(DESCRIPTIVE_MUTABLE_ATTRS - field_ids)
    if unknown:
        raise ValueError(
            f"descriptive_mutable declares unknown structural attribute(s): {unknown}"
        )


# ---------------------------------------------------------------------------
# Section assignment for metrics (ordered, first match wins) -- transposed
# from the research draft _section_prefix_rules. Cross-family suffixes
# (EXPERIMENT) and report-shape families (AUCTION INSIGHT, SKAN) match first.
# ---------------------------------------------------------------------------


def _metric_section(name: str) -> str:
    if (
        name.startswith("control_")
        or name.endswith("_p_value")
        or name.endswith("_margin_of_error")
        or name.endswith("_point_estimate")
        or name.startswith("conversions_absolute_change_")
    ):
        return S_EXPERIMENT
    if name.startswith("auction_insight_"):
        return S_AUCTION
    if name.startswith("sk_ad_network_"):
        return S_SKAN
    if name.startswith("active_view_"):
        return S_ACTIVE_VIEW
    if "location_asset" in name or name in (
        "all_conversions_from_click_to_call",
        "all_conversions_from_directions",
        "all_conversions_from_menu",
        "all_conversions_from_order",
        "all_conversions_from_other_engagement",
        "all_conversions_from_store_visit",
        "all_conversions_from_store_website",
        "impressions_from_store_reach",
    ):
        return S_LOCATION
    if name.startswith("hotel_"):
        return S_HOTEL
    if name.startswith("gmail_"):
        return S_GMAIL
    if name.startswith(("phone_", "message_")):
        return S_CALLS
    if name.startswith(("organic_", "combined_", "publisher_")):
        return S_ORGANIC
    if name.startswith("video_") or name in (
        "trueview_average_cpv",
        "average_video_watch_time_duration_millis",
    ):
        return S_VIDEO
    if name.startswith("unique_users") or name in (
        "average_impression_frequency_per_user",
        "coviewed_impressions",
        "primary_impressions",
    ):
        return S_REACH
    if name.startswith("asset_pinned_") or name in (
        "linked_entities_count",
        "linked_sample_entities",
    ):
        return S_ASSET
    if name in ("average_target_cpa_micros", "average_target_roas"):
        return S_BUDGET
    if name.startswith("historical_") or name in (
        "speed_score",
        "mobile_friendly_clicks_percentage",
        "valid_accelerated_mobile_pages_clicks_percentage",
        "optimization_score_uplift",
        "optimization_score_url",
    ):
        return S_QUALITY
    if (
        name.endswith("_share")
        or name.startswith("benchmark_")
        or name.startswith("content_")
        or name == "relative_ctr"
    ):
        return S_IMPR_SHARE
    if name.endswith("_unique_query_clusters") or name == "search_volume":
        return S_KEYWORD
    if name in (
        "orders", "all_orders", "units_sold", "all_units_sold",
        "average_cart_size", "all_average_cart_size",
        "average_order_value_micros", "all_average_order_value_micros",
        "revenue_micros", "all_revenue_micros",
        "cost_of_goods_sold_micros", "all_cost_of_goods_sold_micros",
        "gross_profit_margin", "gross_profit_micros",
        "all_gross_profit_margin", "all_gross_profit_micros",
        "new_customer_lifetime_value", "all_new_customer_lifetime_value",
    ) or name.startswith(("cross_sell_", "all_cross_sell_", "lead_", "all_lead_")):
        return S_COMMERCE
    if name in ("value_adjustment", "all_value_adjustment"):
        return S_CONV_DETAIL
    if name in (
        "cost_micros", "average_cost", "average_cpc", "average_cpe", "average_cpm",
    ):
        return S_COST
    if "conversion" in name or name.startswith("biddable_") or name.startswith(
        "store_visits_"
    ):
        return S_CONVERSIONS
    if name in (
        "clicks", "ctr", "invalid_clicks", "invalid_click_rate",
        "general_invalid_clicks", "general_invalid_click_rate",
        "interactions", "interaction_rate", "interaction_event_types",
        "engagements", "engagement_rate",
    ):
        return S_CLICKS
    if name in (
        "impressions", "top_impression_percentage",
        "absolute_top_impression_percentage",
    ):
        return S_IMPRESSIONS
    return S_MISC


# ---------------------------------------------------------------------------
# Section assignment for segments (ordered, first match wins).
# ---------------------------------------------------------------------------

_TIME_SEGMENTS = {
    "date", "week", "month", "quarter", "year", "day_of_week", "hour",
    "month_of_year",
}
_STRUCTURE_SEGMENTS = {"ad_group", "campaign", "asset_group", "webpage"}
_NETWORK_SEGMENTS = {
    "device", "mobile_device_platform", "ad_network_type", "ad_sub_network_type",
    "ad_format_type", "ad_destination_type", "slot",
    "search_engine_results_page_type",
}
_KEYWORD_SEGMENTS = {
    "keyword", "search_term", "search_term_match_source", "search_term_match_type",
    "search_term_targeting_status", "match_type", "search_subcategory",
}
_CONV_DETAIL_SEGMENTS = {
    "conversion_action", "conversion_action_category", "conversion_action_name",
    "conversion_adjustment", "conversion_attribution_event_type",
    "conversion_lag_bucket", "conversion_or_adjustment_lag_bucket",
    "external_conversion_source",
}
_BUDGET_SEGMENTS = {
    "budget_campaign_association_status", "conversion_value_rule_primary_dimension",
}
_ASSET_SEGMENTS = {
    "asset_interaction_target", "interaction_on_this_extension",
    "ad_using_product_data",
}


def _segment_section(name: str) -> str:
    if name in _TIME_SEGMENTS:
        return S_TIME
    if name in _STRUCTURE_SEGMENTS:
        return S_STRUCTURE
    if name in _NETWORK_SEGMENTS:
        return S_NETWORK
    if name.startswith(("geo_target_", "activity_", "travel_destination_")):
        return S_GEO
    if name in _KEYWORD_SEGMENTS:
        return S_KEYWORD
    if name.startswith(("product_",)):
        return S_SHOPPING
    if name.startswith("hotel_") or name == "partner_hotel_id":
        return S_HOTEL
    if name.startswith("sk_ad_network_"):
        return S_SKAN
    if name == "auction_insight_domain":
        return S_AUCTION
    if name in _CONV_DETAIL_SEGMENTS:
        return S_CONV_DETAIL
    if name == "new_versus_returning_customers":
        return S_COMMERCE
    if name in _BUDGET_SEGMENTS:
        return S_BUDGET
    if name.startswith("vertical_ads_") or name == "external_activity_id":
        return S_VERTICAL
    if name in _ASSET_SEGMENTS:
        return S_ASSET
    if name == "ad_using_video":
        return S_VIDEO
    if name == "click_type":
        return S_CLICKS
    return S_MISC


# ---------------------------------------------------------------------------
# physical data_type assignment (metrics). Heuristic documented in the module
# docstring; the schema only requires an enum-valid physical_type, and the
# per-version regen re-verifies against the fetched protos.
# ---------------------------------------------------------------------------

_METRIC_TYPE_OVERRIDES: dict[str, str] = {
    # Enum-typed metrics (QualityScoreBucket) -> string
    "historical_creative_quality_score": "enum",
    "historical_landing_page_quality_score": "enum",
    "historical_search_predicted_ctr": "enum",
    # int32 quality/speed scores
    "historical_quality_score": "int",
    "speed_score": "int",
    # Strings / dates / repeated
    "optimization_score_url": "string",
    "conversion_last_conversion_date": "date",
    "conversion_last_received_request_date_time": "datetime",
    "interaction_event_types": "array",
    "linked_sample_entities": "array",
    # int64 durations (millis)
    "video_watch_time_duration_millis": "int",
    "average_video_watch_time_duration_millis": "int",
    "search_volume": "int",
}

# int64 count metrics (everything else defaults to double/decimal).
_INT_COUNT_SUFFIXES = ("_impressions", "_clicks", "_count")
_INT_COUNT_METRICS = {
    "impressions", "clicks", "engagements", "interactions",
    "invalid_clicks", "general_invalid_clicks",
    "combined_clicks", "combined_queries",
    "organic_clicks", "organic_impressions", "organic_queries",
    "gmail_forwards", "gmail_saves", "gmail_secondary_clicks",
    "phone_calls", "phone_impressions", "message_chats", "message_impressions",
    "video_trueview_views", "hotel_eligible_impressions",
    "impressions_from_store_reach",
    "eligible_impressions_from_location_asset_store_reach",
    "coviewed_impressions", "primary_impressions",
    "sk_ad_network_installs", "sk_ad_network_total_conversions",
    "control_clicks", "control_impressions",
    "unique_users", "unique_users_two_plus", "unique_users_three_plus",
    "unique_users_four_plus", "unique_users_five_plus", "unique_users_ten_plus",
    "linked_entities_count",
}


def _metric_data_type(name: str) -> str:
    if name in _METRIC_TYPE_OVERRIDES:
        return _METRIC_TYPE_OVERRIDES[name]
    if name.endswith("_micros"):
        return "int"  # int64 micros -- landing divides by 1e6 (platform rule)
    if name in _INT_COUNT_METRICS:
        return "int"
    if name.endswith(_INT_COUNT_SUFFIXES) and "rate" not in name:
        return "int"
    return "decimal"


# ---------------------------------------------------------------------------
# Money-micros declaration (review 26.2 F-2). EXPLICIT list -- the suffix
# heuristic misses the monetary DOUBLES (average_cpc & co are micros on the
# wire despite being doubles: a suffix-only rule lands them x1e6 too big).
# transform() divides by 1e6 ONLY for fields declared here (via the
# physical_type: "money_micros" marker in official_fields.json).
# ---------------------------------------------------------------------------

# Monetary doubles that are micros on the wire WITHOUT the _micros suffix.
# cost_per_* family included EXCEPT cost_per_conversion_p_value (a p-value is
# a unit-less probability, never a monetary amount). value_per_* metrics are
# conversion values in currency units (NOT micros) -- deliberately absent.
# Unit ratification field-by-field at the 25.6 live probe (ROLLOUT_NOTES.md).
MONEY_MICROS_DOUBLE_METRICS: tuple[str, ...] = (
    "active_view_cpm",
    "average_cost",
    "average_cpc",
    "average_cpe",
    "average_cpm",
    "cost_per_all_conversions",
    "cost_per_conversion",
    "cost_per_conversion_change_point_estimate",
    "cost_per_conversion_margin_of_error",
    "cost_per_current_model_attributed_conversion",
    "cost_per_platform_comparable_conversion",
)

MONEY_MICROS_METRICS: frozenset[str] = frozenset(
    tuple(m for m in METRICS if m.endswith("_micros")) + MONEY_MICROS_DOUBLE_METRICS
)


def _validate_money_micros_list() -> None:
    """The money list must be a subset of METRICS and cover every *_micros."""
    unknown = sorted(MONEY_MICROS_METRICS - set(METRICS))
    if unknown:
        raise ValueError(f"money_micros declares unknown metrics: {unknown}")
    uncovered = sorted(
        m for m in METRICS if m.endswith("_micros") and m not in MONEY_MICROS_METRICS
    )
    if uncovered:
        raise ValueError(f"*_micros metrics missing from money list: {uncovered}")
    leaked = sorted(m for m in MONEY_MICROS_METRICS if m.startswith("value_per_"))
    if leaked:
        raise ValueError(f"value_per_* are NOT micros (F-2): {leaked}")


_RATIO_MARKERS = (
    "rate", "share", "percentage", "ctr", "roas", "margin", "viewability",
    "measurability",
)


def _is_ratio_or_derived(name: str) -> bool:
    """True for provider-computed ratio/derived metrics (non-additive, AD-4)."""
    if name.startswith(("average_", "cost_per_", "value_per_", "percent_")):
        return True
    if name in ("svr", "bounce_rate", "trueview_average_cpv"):
        return True
    return any(marker in name for marker in _RATIO_MARKERS)


# ---------------------------------------------------------------------------
# physical data_type assignment (segments).
# ---------------------------------------------------------------------------

_SEGMENT_TYPE_OVERRIDES: dict[str, str] = {
    "date": "date",
    "week": "date",
    "month": "date",
    "quarter": "date",
    "hotel_check_in_date": "date",
    "year": "int",
    "hour": "int",
    "hotel_booking_window_days": "int",
    "hotel_class": "int",
    "hotel_length_of_stay": "int",
    "sk_ad_network_fine_conversion_value": "int",
    "sk_ad_network_postback_sequence_index": "int",
    "sk_ad_network_redistributed_fine_conversion_value": "int",
    "ad_using_product_data": "boolean",
    "ad_using_video": "boolean",
    "interaction_on_this_extension": "boolean",
    "vertical_ads_event_participant_display_names": "array",
}


def _segment_data_type(name: str) -> str:
    return _SEGMENT_TYPE_OVERRIDES.get(name, "string")


_MICROS_NOTE = (
    " Monetary value in int64 micros (1e-6 of customer_currency_code); landed as"
    " value/1e6 (platform micros rule, story 26.2; catalog money_micros"
    " declaration -- final unit ratification at the 25.6 live probe)."
)
_MONEY_DOUBLE_NOTE = (
    " Monetary DOUBLE in micros on the wire despite the missing _micros suffix"
    " (review 26.2 F-2); landed as value/1e6 (platform micros rule; catalog"
    " money_micros declaration -- final unit ratification at the 25.6 live"
    " probe)."
)
_RATIO_NOTE = (
    " Provider-computed ratio/derived metric: NON-ADDITIVE (AD-4) -- never SUM"
    " across rows; prefer semantic-layer computation from stored numerators and"
    " denominators."
)
_MILLIS_NOTE = " Duration in milliseconds."


def build() -> list[dict]:
    out: list[dict] = []
    seen: set[str] = set()
    _validate_money_micros_list()

    def add(field_id, kind, data_type, section, description, source_field=None,
            money_micros=False, descriptive_mutable=False):
        if field_id in seen:
            raise ValueError(f"duplicate field_id in official snapshot: {field_id}")
        seen.add(field_id)
        entry = {
            "field_id": field_id,
            "kind": kind,
            "data_type": data_type,
            "description": description,
            "section": section,
        }
        if source_field is not None:
            entry["source_field"] = source_field
        if money_micros:
            # Review 26.2 F-2: the EXPLICIT landing declaration transform()
            # divides on -- never a name-suffix heuristic.
            entry["physical_type"] = "money_micros"
        if descriptive_mutable:
            # Story 26.6 Part A: the EXPLICIT landing declaration the connector
            # reads to land the field in attributes_json (latest-wins), OUTSIDE
            # segments_json and the dbt supersede grain.
            entry["descriptive_mutable"] = True
        out.append(entry)

    # 1. Metrics (278) -- GAQL token metrics.<name>.
    if len(METRICS) != EXPECTED_METRIC_COUNT:
        raise ValueError(
            f"metrics count drifted: {len(METRICS)} != {EXPECTED_METRIC_COUNT}"
        )
    for name in METRICS:
        gaql = f"metrics.{name}"
        desc = f"Google Ads metric '{gaql}' (v24 metrics.proto)."
        if name.endswith("_micros"):
            desc += _MICROS_NOTE
        elif name in MONEY_MICROS_METRICS:
            desc += _MONEY_DOUBLE_NOTE
        elif name.endswith("_millis"):
            desc += _MILLIS_NOTE
        if _is_ratio_or_derived(name):
            desc += _RATIO_NOTE
        add(name, "metric", _metric_data_type(name), _metric_section(name), desc,
            source_field=gaql, money_micros=name in MONEY_MICROS_METRICS)

    # 2. Segments (151) -- GAQL token segments.<name>.
    if len(SEGMENTS) != EXPECTED_SEGMENT_COUNT:
        raise ValueError(
            f"segments count drifted: {len(SEGMENTS)} != {EXPECTED_SEGMENT_COUNT}"
        )
    for name in SEGMENTS:
        gaql = f"segments.{name}"
        source = MESSAGE_SEGMENT_SOURCE.get(name, gaql)
        desc = (
            f"Google Ads segment '{gaql}' (v24 segments.proto). Selecting it splits"
            " the report rows (implicit GROUP BY -- dossier section 1.5)."
        )
        if name in MESSAGE_SEGMENT_SOURCE:
            desc += (
                f" Message-typed segment: flattens to sub-fields at query time;"
                f" primary flattened path is '{source}'."
            )
        if name in ("date", "week", "month", "quarter", "year"):
            desc += (
                " Core date segment: selecting it REQUIRES a finite date range in"
                " WHERE (EXPECTED_FILTERS_ON_DATE_RANGE); 37-month lookback cap."
            )
        if name == "keyword":
            desc += (
                " GRAIN TRAP: silently restricts rows to Search keyword-criterion"
                " traffic (excludes DSA/Shopping/PMax)."
            )
        if name.startswith("conversion_action"):
            desc += (
                " GRAIN TRAP: restricts rows to those with conversions and splits"
                " conversion metrics per action; never mix with plain volume"
                " metrics in one stored grain."
            )
        add(name, "dimension", _segment_data_type(name), _segment_section(name),
            desc, source_field=source)

    # 3. Structural dimensions (26) -- curated resource attributes.
    if len(STRUCTURAL_DIMS) != EXPECTED_STRUCTURAL_COUNT:
        raise ValueError(
            f"structural count drifted: {len(STRUCTURAL_DIMS)} !="
            f" {EXPECTED_STRUCTURAL_COUNT}"
        )
    _validate_descriptive_mutable({fid for fid, *_ in STRUCTURAL_DIMS})
    for field_id, source, data_type, section, desc in STRUCTURAL_DIMS:
        mutable = field_id in DESCRIPTIVE_MUTABLE_ATTRS
        full_desc = f"{desc} Resource attribute '{source}' (v24 resource protos)."
        if mutable:
            full_desc += _DESCRIPTIVE_MUTABLE_NOTE
        add(field_id, "dimension", data_type, section, full_desc,
            source_field=source, descriptive_mutable=mutable)

    # Deterministic order: sort by field_id.
    out.sort(key=lambda e: e["field_id"])
    return out


# ---------------------------------------------------------------------------
# field_compatibility rules generation (review 26.2 F-1): project the module's
# static GAQL scoping metadata to the core 26.1 schema-'1' contract -- one
# selectable_set rule per v1 profile FROM resource, allowed_fields enumerated
# as catalog FIELD IDS (the vocabulary core.field_compat validates against).
# ---------------------------------------------------------------------------


def build_compat_rules(fields: list[dict], compat_block: dict) -> list[dict]:
    """One selectable_set rule per FROM resource, from the snapshot + metadata.

    A field id is allowed on a resource when:
      - it is a metric (per-resource metric legality is NOT statically known:
        the fieldservice dump / 25.6 probe is the authority -- schema note);
      - it is a segment NOT scoped away from the resource by
        ``_gaql_resource_scoped_segments`` / ``..._segment_prefixes``;
      - it is a resource attribute whose GAQL token starts with one of the
        resource's ``_gaql_selectable_attr_prefixes``.
    """
    attr_prefixes: dict = compat_block["_gaql_selectable_attr_prefixes"]
    scoped_segments: dict = compat_block["_gaql_resource_scoped_segments"]
    scoped_prefixes: dict = compat_block["_gaql_resource_scoped_segment_prefixes"]

    rules: list[dict] = []
    for resource in sorted(attr_prefixes):
        prefixes = tuple(attr_prefixes[resource])
        allowed: list[str] = []
        for f in fields:
            field_id = f["field_id"]
            token = f.get("source_field", field_id)
            if f["kind"] == "metric":
                allowed.append(field_id)
                continue
            if token.startswith("segments."):
                segment = token.split(".")[1]
                scoped_to = scoped_segments.get(segment)
                if scoped_to is None:
                    for prefix, prefix_scope in scoped_prefixes.items():
                        if segment.startswith(prefix):
                            scoped_to = prefix_scope
                            break
                if scoped_to is None or resource in scoped_to:
                    allowed.append(field_id)
                continue
            if prefixes and token.startswith(prefixes):
                allowed.append(field_id)
        rules.append(
            {
                "id": f"gaql-selectable-set-{resource}",
                "kind": "selectable_set",
                "scope": {"resource": resource},
                "allowed_fields": sorted(allowed),
                "_note": (
                    f"GENERATED by build_official_fields.py: catalog field ids "
                    f"selectable FROM {resource} (attributes limited to "
                    + "/".join(attr_prefixes[resource])
                    + " ; resource-scoped segments removed; every metric "
                    "included -- per-resource metric legality is probe/"
                    "fieldservice authority)."
                ),
            }
        )
    return rules


def main() -> None:
    fields = build()
    metrics = sum(1 for f in fields if f["kind"] == "metric")
    dims = sum(1 for f in fields if f["kind"] == "dimension")
    segments = sum(
        1 for f in fields if f.get("source_field", "").startswith("segments.")
    )
    money = sum(1 for f in fields if f.get("physical_type") == "money_micros")
    out_path = Path(__file__).with_name("official_fields.json")
    out_path.write_text(
        json.dumps(fields, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(
        f"Wrote {len(fields)} fields ({metrics} metrics / {dims} dimensions,"
        f" of which {segments} segments; {money} money_micros) to {out_path}"
    )

    # Review 26.2 F-1: regenerate the core-schema field_compatibility rules in
    # catalog_sources.json (metadata human-owned, rules generated).
    sources_path = Path(__file__).with_name("catalog_sources.json")
    sources = json.loads(sources_path.read_text(encoding="utf-8"))
    block = sources["field_compatibility"]
    if block.get("schema_version") != "1":
        raise ValueError(
            "field_compatibility block must declare schema_version '1' "
            "(core 26.1 contract opt-in)"
        )
    block["rules"] = build_compat_rules(fields, block)
    sources_path.write_text(
        json.dumps(sources, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(
        f"Wrote {len(block['rules'])} generated field_compatibility rules to "
        f"{sources_path}"
    )


if __name__ == "__main__":
    main()
