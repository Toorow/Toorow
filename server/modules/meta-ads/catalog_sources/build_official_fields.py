"""Deterministic builder for meta-ads official_fields.json (Story 25.4 AC1).

This script is the AUDITABLE SOURCE for the curated official snapshot. It is
committed alongside its output (official_fields.json) so a reviewer can see
exactly how the curated list — and especially the action-family EXPANSION —
was derived from the official Meta Marketing API references recorded in
catalog_sources.json.

It is pure-Python, deterministic (no network, no clock), and byte-stable:
running it again reproduces official_fields.json exactly.

Derivation summary (see catalog_sources.json "_derivation"):
  - ROOT insights fields come from the official /insights reference.
  - BREAKDOWN dimensions come from the official breakdowns reference.
  - The AdsActionStats families (actions, action_values, cost_per_action_type,
    unique_actions, cost_per_unique_action_type, conversions, conversion_values,
    cost_per_conversion, outbound_clicks, ...) are EXPANDED per the official
    ads-action-stats action_type list: for each family F and action_type A the
    flattened field id is "<F>_<A>" with A's dots normalised to underscores,
    matching how the API returns the array keyed by action_type and how
    Supermetrics flattens the same arrays (which is why the merged counts
    converge on the 537 metrics / 254 dimensions figure).

Run (orchestrator, local only):
    uv run python server/modules/meta-ads/catalog_sources/build_official_fields.py
"""

from __future__ import annotations

import json
from pathlib import Path

# ---------------------------------------------------------------------------
# Sections (Supermetrics-like families) used by the section_tier_map.
# ---------------------------------------------------------------------------
S_COST = "COST"
S_CLICKS = "CLICKS"
S_IMPRESSION = "IMPRESSION"
S_ACTION = "ACTION"
S_CONVERSION = "CONVERSION"
S_PERFORMANCE = "PERFORMANCE"
S_VIDEO = "VIDEO"
S_ENGAGEMENT = "ENGAGEMENT"
S_ATTRIBUTION = "ATTRIBUTION"
S_CAMPAIGN = "CAMPAIGN DETAILS"
S_TIME = "TIME"
S_TARGETING = "TARGETING"
S_PLACEMENT = "PLACEMENT"
S_CREATIVE = "CREATIVE ASSET"
S_APP = "APP"

# ---------------------------------------------------------------------------
# 1. ROOT scalar insights fields (from the /insights reference, v23.0).
#    (field_id, kind, data_type, section, description, source_field?)
# ---------------------------------------------------------------------------
ROOT_FIELDS: list[tuple] = [
    # --- Campaign / account structure (dimensions) ---
    ("account_id", "dimension", "id", S_CAMPAIGN, "ID number of the ad account."),
    ("account_name", "dimension", "string", S_CAMPAIGN, "Name of the ad account."),
    ("account_currency", "dimension", "string", S_CAMPAIGN, "Currency used by the ad account."),
    ("campaign_id", "dimension", "id", S_CAMPAIGN, "Unique ID of the campaign."),
    ("campaign_name", "dimension", "string", S_CAMPAIGN, "Name of the campaign."),
    ("adset_id", "dimension", "id", S_CAMPAIGN, "Unique ID of the ad set."),
    ("adset_name", "dimension", "string", S_CAMPAIGN, "Name of the ad set."),
    ("ad_id", "dimension", "id", S_CAMPAIGN, "Unique ID of the ad in reporting."),
    ("ad_name", "dimension", "string", S_CAMPAIGN, "Name of the ad in reporting."),
    ("creative_id", "dimension", "id", S_CAMPAIGN, "Unique ID of the ad creative."),
    ("objective", "dimension", "string", S_CAMPAIGN, "Campaign objective or goal."),
    ("optimization_goal", "dimension", "string", S_CAMPAIGN, "Selected optimization goal for the ads."),
    ("buying_type", "dimension", "string", S_CAMPAIGN, "Method of payment and targeting (auction or reach & frequency)."),
    ("attribution_setting", "dimension", "string", S_ATTRIBUTION, "Default attribution window for the account."),
    ("created_time", "dimension", "datetime", S_CAMPAIGN, "Creation timestamp."),
    ("updated_time", "dimension", "datetime", S_CAMPAIGN, "Last update timestamp."),
    # --- Time (dimension) : catalog id `date` maps to provider `date_start` ---
    ("date", "dimension", "date", S_TIME, "Start date of the reporting period.", "date_start"),
    ("date_stop", "dimension", "date", S_TIME, "End date of the reporting period."),
    # --- Spend / cost (metrics) ---
    ("spend", "metric", "currency", S_COST, "Total estimated amount spent on the ads.", "spend"),
    ("social_spend", "metric", "currency", S_COST, "Amount spent on ads shown with social information."),
    ("cpc", "metric", "currency", S_COST, "Average cost per click."),
    ("cpm", "metric", "currency", S_COST, "Average cost per 1,000 impressions."),
    ("cpp", "metric", "currency", S_COST, "Average cost to reach 1,000 people (estimated)."),
    ("cost_per_inline_link_click", "metric", "currency", S_COST, "Average cost per inline link click."),
    ("cost_per_inline_post_engagement", "metric", "currency", S_COST, "Average cost per inline post engagement."),
    ("cost_per_unique_click", "metric", "currency", S_COST, "Average cost per unique click (estimated)."),
    ("cost_per_unique_inline_link_click", "metric", "currency", S_COST, "Average cost per unique inline link click (estimated)."),
    ("cost_per_estimated_ad_recallers", "metric", "currency", S_COST, "Average cost per estimated ad recall lift (people)."),
    # --- Clicks (metrics) ---
    ("clicks", "metric", "int", S_CLICKS, "Total number of clicks on the ads.", "clicks"),
    ("ctr", "metric", "percent", S_CLICKS, "Click-through rate (percentage)."),
    ("inline_link_clicks", "metric", "int", S_CLICKS, "Number of inline link clicks."),
    ("inline_link_click_ctr", "metric", "percent", S_CLICKS, "Percentage of views that resulted in inline link clicks."),
    ("unique_clicks", "metric", "int", S_CLICKS, "Number of unique clicks (estimated)."),
    ("unique_ctr", "metric", "percent", S_CLICKS, "Unique click-through rate (estimated)."),
    ("unique_inline_link_clicks", "metric", "int", S_CLICKS, "Number of unique inline link clicks (estimated)."),
    ("unique_inline_link_click_ctr", "metric", "percent", S_CLICKS, "Unique inline link click-through rate (estimated)."),
    ("unique_link_clicks_ctr", "metric", "percent", S_CLICKS, "Unique link click-through rate (estimated)."),
    ("website_ctr", "metric", "json", S_CLICKS, "Click-through rate for website clicks (by action type)."),
    # --- Impressions / reach (metrics) ---
    ("impressions", "metric", "int", S_IMPRESSION, "Number of times the ads were on screen.", "impressions"),
    ("reach", "metric", "int", S_IMPRESSION, "Number of people who saw the ads at least once (estimated)."),
    ("frequency", "metric", "decimal", S_IMPRESSION, "Average number of times each person saw the ad (estimated)."),
    ("full_view_impressions", "metric", "int", S_IMPRESSION, "Number of full-view impressions on page posts."),
    ("full_view_reach", "metric", "int", S_IMPRESSION, "Number of people who performed a full view of the page post."),
    # --- Conversions (scalar) ---
    ("conversions", "metric", "int", S_CONVERSION, "Number of conversions attributed to the ads.", "conversions"),
    ("converted_product_quantity", "metric", "json", S_CONVERSION, "Number of products purchased attributed to the ads (by action type)."),
    ("converted_product_value", "metric", "json", S_CONVERSION, "Value of products purchased attributed to the ads (by action type)."),
    # --- Performance / rankings ---
    ("quality_ranking", "metric", "string", S_PERFORMANCE, "Ranking of the ad's perceived quality vs competing ads."),
    ("engagement_rate_ranking", "metric", "string", S_PERFORMANCE, "Ranking of the ad's expected engagement rate vs competing ads."),
    ("conversion_rate_ranking", "metric", "string", S_PERFORMANCE, "Ranking of the ad's expected conversion rate vs competing ads."),
    ("estimated_ad_recall_rate", "metric", "percent", S_PERFORMANCE, "Estimated percentage of people who may recall the ad."),
    ("estimated_ad_recallers", "metric", "int", S_PERFORMANCE, "Estimated number of additional people who may recall the ad."),
    ("qualifying_question_qualify_answer_rate", "metric", "percent", S_PERFORMANCE, "Rate of qualifying answers to instant-form questions."),
    ("auction_bid", "metric", "decimal", S_PERFORMANCE, "Bid amount in the auction."),
    ("auction_competitiveness", "metric", "decimal", S_PERFORMANCE, "Competitiveness metric in the auction."),
    ("auction_max_competitor_bid", "metric", "decimal", S_PERFORMANCE, "Maximum competitor bid in the auction."),
    # --- Engagement ---
    ("inline_post_engagement", "metric", "int", S_ENGAGEMENT, "Total number of inline post engagement actions."),
    ("canvas_avg_view_percent", "metric", "percent", S_ENGAGEMENT, "Average percentage of the Instant Experience that was viewed."),
    ("canvas_avg_view_time", "metric", "decimal", S_ENGAGEMENT, "Average time (seconds) spent viewing the Instant Experience."),
    ("instant_experience_clicks_to_open", "metric", "int", S_ENGAGEMENT, "Clicks to open the Instant Experience."),
    ("instant_experience_clicks_to_start", "metric", "int", S_ENGAGEMENT, "Clicks to start the Instant Experience."),
    # --- Video (scalar histogram / averages) ---
    ("video_avg_time_watched_actions", "metric", "json", S_VIDEO, "Average video watch time per impression (by action type)."),
    ("video_play_curve_actions", "metric", "json", S_VIDEO, "Video play retention curve graph data."),
]

# ---------------------------------------------------------------------------
# 2. BREAKDOWN dimensions (from the official Insights breakdowns reference).
#    (field_id, data_type, section, description)
# ---------------------------------------------------------------------------
BREAKDOWN_DIMS: list[tuple] = [
    ("age", "string", S_TARGETING, "Age range of the people reached."),
    ("gender", "string", S_TARGETING, "Gender of the people reached."),
    ("country", "string", S_TARGETING, "Country where the reached people are located."),
    ("region", "string", S_TARGETING, "Region where the reached people are located."),
    ("dma", "string", S_TARGETING, "Designated Market Area (DMA) region."),
    ("frequency_value", "int", S_TARGETING, "Number of times an ad was served to each Accounts Center account."),
    ("user_segment_key", "string", S_TARGETING, "User segment of Advantage+ Shopping Campaigns."),
    ("impression_device", "string", S_PLACEMENT, "Device on which the last ad was served."),
    ("device_platform", "string", S_PLACEMENT, "Mobile or desktop device type."),
    ("publisher_platform", "string", S_PLACEMENT, "Platform where the ad appeared (Facebook, Instagram, ...)."),
    ("platform_position", "string", S_PLACEMENT, "Ad placement position within the platform."),
    ("place_page_id", "id", S_PLACEMENT, "ID of the place page in the impression or click."),
    ("hourly_stats_aggregated_by_advertiser_time_zone", "string", S_TIME, "Hourly breakdown by advertiser time zone."),
    ("hourly_stats_aggregated_by_audience_time_zone", "string", S_TIME, "Hourly breakdown by audience time zone."),
    ("product_id", "id", S_CONVERSION, "ID of the product in the impression, click, or action."),
    ("skan_campaign_id", "id", S_ATTRIBUTION, "Raw campaign ID from the SKAdNetwork postback."),
    ("skan_conversion_id", "id", S_ATTRIBUTION, "Assigned Conversion ID in the SKAdNetwork configuration."),
    ("is_conversion_id_modeled", "boolean", S_ATTRIBUTION, "Flag indicating modeled conversion data."),
    ("app_id", "id", S_APP, "ID of the application associated with the ad account."),
    # creative asset breakdowns
    ("ad_format_asset", "id", S_CREATIVE, "ID of the ad format asset in the impression or click."),
    ("body_asset", "id", S_CREATIVE, "Ad body component identifier."),
    ("call_to_action_asset", "id", S_CREATIVE, "Call-to-action component identifier."),
    ("description_asset", "id", S_CREATIVE, "Description component identifier."),
    ("image_asset", "id", S_CREATIVE, "Image component identifier."),
    ("link_url_asset", "id", S_CREATIVE, "URL asset component identifier."),
    ("title_asset", "id", S_CREATIVE, "Title component identifier."),
    ("video_asset", "id", S_CREATIVE, "Video component identifier."),
]

# Action-level breakdown dimensions (the action_breakdowns parameter). These
# further split each AdsActionStats family; declared as catalogue dimensions.
ACTION_BREAKDOWN_DIMS: list[tuple] = [
    ("action_type", "string", S_ACTION, "Kind of action taken on the ad, Page, app, or event."),
    ("action_device", "string", S_ACTION, "Device on which the conversion event occurred."),
    ("action_destination", "string", S_ACTION, "Destination where people go after clicking the ad."),
    ("action_target_id", "id", S_ACTION, "ID of the destination where people go after clicking."),
    ("action_reaction", "string", S_ACTION, "Type of reaction on the ad or boosted post."),
    ("action_video_type", "string", S_ACTION, "Video-specific action type."),
    ("action_video_sound", "string", S_ACTION, "Audio status during video ad playback."),
    ("action_canvas_component_name", "string", S_ACTION, "Component identifier within the Canvas ad."),
    ("action_carousel_card_id", "id", S_ACTION, "ID of the carousel card that people engaged with."),
    ("action_carousel_card_name", "string", S_ACTION, "Carousel card identified by its headline."),
]

# ---------------------------------------------------------------------------
# 3. AdsActionStats FAMILIES + the official action_type list.
# ---------------------------------------------------------------------------
# Each family: (family_id, kind, data_type, section, human label, expand?).
#   expand=True  -> emit the raw-array field AND one flattened "<family>_<at>"
#                   metric per action_type (this is what drives the count toward
#                   Supermetrics' ~537 flattened metrics).
#   expand=False -> emit ONLY the raw-array json field (the long-tail families
#                   Supermetrics does not itemise per action_type).
# The expand set is intentionally the report-relevant families (actions,
# values, costs, conversions, ROAS, outbound clicks) so the flattened count
# lands on the same order of magnitude Supermetrics publishes rather than the
# full 21x90 cross-product.
ACTION_FAMILIES: list[tuple] = [
    ("actions", "metric", "int", S_ACTION, "actions", True),
    ("action_values", "metric", "currency", S_ACTION, "action value", True),
    ("unique_actions", "metric", "int", S_ACTION, "unique actions", False),
    ("cost_per_action_type", "metric", "currency", S_COST, "cost per action", True),
    ("cost_per_unique_action_type", "metric", "currency", S_COST, "cost per unique action", False),
    ("conversions", "metric", "int", S_CONVERSION, "conversions", True),
    ("conversion_values", "metric", "currency", S_CONVERSION, "conversion value", True),
    ("cost_per_conversion", "metric", "currency", S_COST, "cost per conversion", True),
    ("catalog_segment_value", "metric", "currency", S_CONVERSION, "catalog segment value", False),
    ("purchase_roas", "metric", "decimal", S_CONVERSION, "purchase ROAS", False),
    ("website_purchase_roas", "metric", "decimal", S_CONVERSION, "website purchase ROAS", False),
    ("mobile_app_purchase_roas", "metric", "decimal", S_CONVERSION, "mobile app purchase ROAS", False),
    ("outbound_clicks", "metric", "int", S_CLICKS, "outbound clicks", False),
    ("outbound_clicks_ctr", "metric", "percent", S_CLICKS, "outbound click-through rate", False),
    ("unique_outbound_clicks", "metric", "int", S_CLICKS, "unique outbound clicks", False),
    ("unique_outbound_clicks_ctr", "metric", "percent", S_CLICKS, "unique outbound click-through rate", False),
    ("cost_per_outbound_click", "metric", "currency", S_COST, "cost per outbound click", False),
    ("cost_per_unique_outbound_click", "metric", "currency", S_COST, "cost per unique outbound click", False),
    ("ad_click_actions", "metric", "int", S_CLICKS, "ad click actions", False),
    ("ad_impression_actions", "metric", "int", S_IMPRESSION, "ad impression actions", False),
    ("instant_experience_outbound_clicks", "metric", "int", S_CLICKS, "Instant Experience outbound clicks", False),
]

# Video watch families keyed by action_type (video_view sub-types). Expanded the
# same way over VIDEO_ACTION_TYPES below.
VIDEO_FAMILIES: list[tuple] = [
    ("video_play_actions", "int", "video plays"),
    ("video_thruplay_watched_actions", "int", "ThruPlay watched actions"),
    ("video_30_sec_watched_actions", "int", "30-second video views"),
    ("video_p25_watched_actions", "int", "video plays to 25%"),
    ("video_p50_watched_actions", "int", "video plays to 50%"),
    ("video_p75_watched_actions", "int", "video plays to 75%"),
    ("video_p95_watched_actions", "int", "video plays to 95%"),
    ("video_p100_watched_actions", "int", "video plays to 100%"),
    ("video_continuous_2_sec_watched_actions", "int", "continuous 2-second video views"),
    ("cost_per_thruplay", "currency", "cost per ThruPlay"),
    ("cost_per_15_sec_video_view", "currency", "cost per 15-second video view"),
    ("cost_per_2_sec_continuous_video_view", "currency", "cost per 2-second continuous video view"),
]

# Official action_type values (ads-action-stats reference). Dots -> underscores
# when flattened into a field id.
ACTION_TYPES: list[str] = [
    # Standard on-Facebook actions
    "link_click",
    "post_engagement",
    "page_engagement",
    "post_reaction",
    "comment",
    "like",
    "post",
    "rsvp",
    "photo_view",
    "checkin",
    "outbound_click",
    "landing_page_view",
    "onsite_conversion.post_save",
    "video_view",
    # Offsite pixel conversions
    "offsite_conversion.fb_pixel_purchase",
    "offsite_conversion.fb_pixel_add_to_cart",
    "offsite_conversion.fb_pixel_initiate_checkout",
    "offsite_conversion.fb_pixel_add_payment_info",
    "offsite_conversion.fb_pixel_lead",
    "offsite_conversion.fb_pixel_complete_registration",
    "offsite_conversion.fb_pixel_view_content",
    "offsite_conversion.fb_pixel_search",
    "offsite_conversion.fb_pixel_add_to_wishlist",
    "offsite_conversion.fb_pixel_custom",
    # On-Facebook conversions / messaging
    "onsite_conversion.purchase",
    "onsite_conversion.lead_grouped",
    "onsite_conversion.flow_complete",
    "onsite_conversion.messaging_conversation_started_7d",
    "onsite_conversion.messaging_first_reply",
    "onsite_conversion.messaging_block",
    "onsite_conversion.messaging_user_subscribed",
    # Mobile app events
    "mobile_app_install",
    "app_install",
    "app_use",
    "app_custom_event.fb_mobile_purchase",
    "app_custom_event.fb_mobile_add_to_cart",
    "app_custom_event.fb_mobile_initiate_checkout",
    "app_custom_event.fb_mobile_add_payment_info",
    "app_custom_event.fb_mobile_lead",
    "app_custom_event.fb_mobile_complete_registration",
    "app_custom_event.fb_mobile_content_view",
    "app_custom_event.fb_mobile_search",
    "app_custom_event.fb_mobile_add_to_wishlist",
    "app_custom_event.fb_mobile_activate_app",
    "app_custom_event.fb_mobile_level_achieved",
    "app_custom_event.fb_mobile_achievement_unlocked",
    "app_custom_event.fb_mobile_tutorial_completion",
    "app_custom_event.fb_mobile_rate",
    "app_custom_event.fb_mobile_spent_credits",
    "app_custom_event.other",
    # Omni / omnichannel grouped outcomes
    "omni_purchase",
    "omni_add_to_cart",
    "omni_initiated_checkout",
    "omni_complete_registration",
    "omni_view_content",
    "omni_search",
    "omni_add_to_wishlist",
    "omni_app_install",
    "omni_activate_app",
    "omni_level_achieved",
    "omni_achievement_unlocked",
    "omni_rate",
    "omni_spend_credits",
    "omni_tutorial_completion",
    "omni_custom",
    # Business-outcome totals
    "contact_total",
    "donate_total",
    "find_location_total",
    "schedule_total",
    "start_trial_total",
    "submit_application_total",
    "subscribe_total",
    "customize_product_total",
    "recurring_subscription_payment_total",
    "cancel_subscription_total",
    # Lead grouping / click-to-call
    "lead",
    "leadgen_grouped",
    "click_to_call_call_confirm",
    "click_to_call_native_call_placed",
    "click_to_call_native_20s_call_connect",
    "click_to_call_native_60s_call_connect",
]

# The subset of action_types meaningful for the video-watch families.
VIDEO_ACTION_TYPES: list[str] = ["video_view"]


def _norm(action_type: str) -> str:
    """offsite_conversion.fb_pixel_purchase -> offsite_conversion_fb_pixel_purchase."""
    return action_type.replace(".", "_")


def build() -> list[dict]:
    out: list[dict] = []
    seen: set[str] = set()

    def add(field_id, kind, data_type, section, description, source_field=None):
        if field_id in seen:
            return
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
        out.append(entry)

    # 1. Root scalar fields
    for row in ROOT_FIELDS:
        field_id, kind, data_type, section, description = row[:5]
        source_field = row[5] if len(row) > 5 else None
        add(field_id, kind, data_type, section, description, source_field)

    # 2. Breakdown dimensions + action breakdown dimensions
    for field_id, data_type, section, description in BREAKDOWN_DIMS:
        add(field_id, "dimension", data_type, section, description)
    for field_id, data_type, section, description in ACTION_BREAKDOWN_DIMS:
        add(field_id, "dimension", data_type, section, description)

    # 3. Expand the AdsActionStats families over the action_type list.
    for family_id, kind, data_type, section, label, expand in ACTION_FAMILIES:
        # Keep the un-expanded family field too (the raw array field).
        add(family_id, kind, "json", section, f"Total {label} attributed to the ads (array keyed by action_type).")
        if not expand:
            continue
        for at in ACTION_TYPES:
            fid = f"{family_id}_{_norm(at)}"
            add(fid, kind, data_type, section, f"{label.capitalize()} for action_type '{at}'.")

    # 4. Expand the video-watch families over the video action types.
    for family_id, data_type, label in VIDEO_FAMILIES:
        add(family_id, "metric", "json", S_VIDEO, f"Total {label} (array keyed by action_type).")
        for at in VIDEO_ACTION_TYPES:
            fid = f"{family_id}_{_norm(at)}"
            add(fid, "metric", data_type, S_VIDEO, f"{label.capitalize()} for action_type '{at}'.")

    # Deterministic order: sort by field_id.
    out.sort(key=lambda e: e["field_id"])
    return out


def main() -> None:
    fields = build()
    metrics = sum(1 for f in fields if f["kind"] == "metric")
    dims = sum(1 for f in fields if f["kind"] == "dimension")
    out_path = Path(__file__).with_name("official_fields.json")
    out_path.write_text(
        json.dumps(fields, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(f"Wrote {len(fields)} fields ({metrics} metrics / {dims} dimensions) to {out_path}")


if __name__ == "__main__":
    main()
