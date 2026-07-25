"""Deterministic builder for tiktok-ads official_fields.json (Story 25.7 AC1).

This script is the AUDITABLE SOURCE for the curated official snapshot. It is
committed alongside its output (official_fields.json) so a reviewer can see
exactly how the curated list -- and especially the conversion/event-family
EXPANSION -- was derived from the official TikTok Business (Marketing) API v1.3
reporting references recorded in catalog_sources.json.

It is pure-Python, deterministic (no network, no clock), and byte-stable:
running it again reproduces official_fields.json exactly.

Derivation summary (see catalog_sources.json "_derivation"):
  - BASIC metrics come from the official "Basic report supported metrics" v1.3
    reference (spend/cost family, clicks, impressions, ctr/cpc/cpm, reach,
    frequency, result, conversion, video-play family, engagement family,
    live/product/onsite/page/interactive families).
  - DIMENSIONS come from the official "Basic report supported dimensions" v1.3
    reference (entity ids + stat_time_day/hour + audience/geo/device/placement
    breakdown dimensions).
  - CONVERSION and EVENT families (conversion, cost_per_conversion,
    conversion_rate, ..., and the standard app/onsite/page/skan event set) are
    EXPANDED per official event-type list: for each family F and event E a
    flattened field id "<F>_<E>" is emitted, mirroring how the reporting API
    keys these per optimization event and how Supermetrics flattens the same
    families (their published catalog: 387 metrics / 153 dimensions). This is
    why the merged counts converge on the same order of magnitude.

Portal-render caveat (AI-53): the business-api.tiktok.com portal is a
client-side SPA -- its metric/dimension tables are NOT retrievable by a plain
HTTP fetch. The curated lists below reflect the v1.3 BASIC reference (URLs in
catalog_sources.json) cross-checked against the Supermetrics enrichment
sections (ACCOUNT|AD|AD GROUP|ATTRIBUTION|AUDIENCE|BASIC|BUSINESS CENTER|
CAMPAIGN|CONVERSION|ENGAGEMENT|IN APP EVENT|IN APP EVENT (SKAN)|ONSITE EVENT|
PAGE EVENT|REACH|VIDEO|...). Every field is a DECLARED contract, ratified live
in a later pass (playbook step 7); nothing non-verifiable is asserted as fact.

Run (orchestrator, local only):
    uv run python server/modules/tiktok-ads/catalog_sources/build_official_fields.py
"""

from __future__ import annotations

import json
from pathlib import Path

# ---------------------------------------------------------------------------
# Sections (Supermetrics-like families) used by the section_tier_map.
# ---------------------------------------------------------------------------
S_ACCOUNT = "ACCOUNT"
S_CAMPAIGN = "CAMPAIGN"
S_ADGROUP = "AD GROUP"
S_AD = "AD"
S_BASIC = "BASIC"
S_COST = "COST"
S_CLICKS = "CLICKS"
S_IMPRESSION = "IMPRESSION"
S_REACH = "REACH"
S_VIDEO = "VIDEO"
S_ENGAGEMENT = "ENGAGEMENT"
S_INTERACTIVE = "INTERACTIVE ADD-ON"
S_CONVERSION = "CONVERSION"
S_ATTRIBUTION = "ATTRIBUTION"
S_INAPP = "IN APP EVENT"
S_INAPP_SKAN = "IN APP EVENT (SKAN)"
S_ONSITE = "ONSITE EVENT"
S_PAGE = "PAGE EVENT"
S_LIVE = "LIVE"
S_PRODUCT = "PRODUCT"
S_AUDIENCE = "AUDIENCE"
S_TIME = "TIME"

# ---------------------------------------------------------------------------
# 1. STRUCTURAL dimensions (entity ids/names + time). From the supported
#    dimensions reference + the entity report fields.
#    (field_id, data_type, section, description, source_field?)
# ---------------------------------------------------------------------------
DIMENSIONS: list[tuple] = [
    # --- Time (catalog id `date` maps to provider `stat_time_day`) ---
    ("date", "date", S_TIME, "Reporting day (grain = day).", "stat_time_day"),
    ("stat_time_hour", "datetime", S_TIME, "Reporting hour (hourly grain)."),
    # --- Account / structure ---
    ("advertiser_id", "id", S_ACCOUNT, "TikTok advertiser (ad account) ID."),
    ("advertiser_name", "string", S_ACCOUNT, "TikTok advertiser (ad account) name."),
    ("campaign_id", "id", S_CAMPAIGN, "Unique ID of the campaign."),
    ("campaign_name", "string", S_CAMPAIGN, "Name of the campaign."),
    ("adgroup_id", "id", S_ADGROUP, "Unique ID of the ad group."),
    ("adgroup_name", "string", S_ADGROUP, "Name of the ad group."),
    ("ad_id", "id", S_AD, "Unique ID of the ad."),
    ("ad_name", "string", S_AD, "Name of the ad."),
    ("ad_text", "string", S_AD, "Primary text/caption of the ad."),
    # --- Campaign / ad group attributes (dimension-like report fields) ---
    ("objective_type", "string", S_CAMPAIGN, "Advertising objective of the campaign."),
    ("campaign_budget", "decimal", S_CAMPAIGN, "Budget configured on the campaign."),
    ("campaign_budget_mode", "string", S_CAMPAIGN, "Budget mode of the campaign (daily / total)."),
    ("promotion_type", "string", S_ADGROUP, "Promotion type of the ad group."),
    ("optimization_goal", "string", S_ADGROUP, "Optimization goal of the ad group."),
    ("billing_event", "string", S_ADGROUP, "Billing event of the ad group (CPC/CPM/oCPM)."),
    ("bid_strategy", "string", S_ADGROUP, "Bid strategy of the ad group."),
    ("placement_type", "string", S_ADGROUP, "Placement type (automatic / manual)."),
    ("call_to_action", "string", S_AD, "Call-to-action button label of the ad."),
    ("image_mode", "string", S_AD, "Creative image/video mode of the ad."),
    ("video_id", "id", S_AD, "ID of the video creative used by the ad."),
    ("landing_page_url", "string", S_AD, "Landing page URL of the ad."),
    # --- Audience / geo / device / placement breakdown dimensions ---
    ("gender", "string", S_AUDIENCE, "Gender of the audience reached."),
    ("age", "string", S_AUDIENCE, "Age range of the audience reached."),
    ("country_code", "string", S_AUDIENCE, "Country/region code of the audience."),
    ("province_id", "id", S_AUDIENCE, "Province/state ID of the audience."),
    ("dma_id", "id", S_AUDIENCE, "Designated Market Area (DMA) ID of the audience."),
    ("language", "string", S_AUDIENCE, "Language of the audience."),
    ("platform", "string", S_AUDIENCE, "Platform where the ad was delivered."),
    ("placement", "string", S_AUDIENCE, "Placement where the ad was delivered."),
    ("interest_category", "string", S_AUDIENCE, "Interest category of the audience."),
    ("ac", "string", S_AUDIENCE, "Network/connection type of the audience (wifi/2g/3g/4g/5g)."),
    ("contextual_tag_id", "id", S_AUDIENCE, "Contextual targeting tag ID."),
]

# ---------------------------------------------------------------------------
# 2. SCALAR metrics (non-expanded). From the BASIC supported-metrics reference.
#    (field_id, data_type, section, description, source_field?)
# ---------------------------------------------------------------------------
SCALAR_METRICS: list[tuple] = [
    # --- Cost / spend ---
    ("spend", "currency", S_COST, "Total amount spent on the ads.", "spend"),
    ("cash_spend", "currency", S_COST, "Spend paid with cash (non-voucher)."),
    ("voucher_spend", "currency", S_COST, "Spend paid with vouchers/credits."),
    ("cpc", "currency", S_COST, "Average cost per click."),
    ("cpm", "currency", S_COST, "Average cost per 1,000 impressions."),
    ("cost_per_1000_reached", "currency", S_COST, "Average cost to reach 1,000 unique users."),
    ("cost_per_result", "currency", S_COST, "Average cost per result (optimization outcome)."),
    ("cost_per_conversion", "currency", S_COST, "Average cost per conversion."),
    ("cost_per_secondary_goal_result", "currency", S_COST, "Average cost per secondary-goal result."),
    # --- Clicks ---
    ("clicks", "int", S_CLICKS, "Total number of clicks.", "clicks"),
    ("ctr", "percent", S_CLICKS, "Click-through rate (clicks / impressions)."),
    ("clicks_on_music_disc", "int", S_CLICKS, "Clicks on the music disc element."),
    ("real_time_clicks", "int", S_CLICKS, "Clicks measured in real time."),
    # --- Impressions / reach / frequency ---
    ("impressions", "int", S_IMPRESSION, "Number of times the ads were shown.", "impressions"),
    ("real_time_impressions", "int", S_IMPRESSION, "Impressions measured in real time."),
    ("reach", "int", S_REACH, "Number of unique users who saw the ads."),
    ("frequency", "decimal", S_REACH, "Average number of times each user saw the ad."),
    # --- Result / conversion scalars ---
    # `conversions` is the toorow canonical carrier metric (the value landed in the
    # raw table and rolled to fact_daily_kpi). TikTok exposes it via the `conversion`
    # reporting metric; the manifest source_field is `conversions` (the raw column).
    ("conversions", "int", S_CONVERSION, "Total conversions attributed to the ads (canonical carrier).", "conversions"),
    ("result", "int", S_CONVERSION, "Number of results for the optimization goal.", "result"),
    ("result_rate", "percent", S_CONVERSION, "Result rate (results / impressions)."),
    ("real_time_result", "int", S_CONVERSION, "Results measured in real time."),
    ("real_time_result_rate", "percent", S_CONVERSION, "Real-time result rate."),
    ("real_time_cost_per_result", "currency", S_COST, "Real-time cost per result."),
    ("conversion", "int", S_CONVERSION, "Total conversions attributed to the ads (TikTok reporting metric)."),
    ("conversion_rate", "percent", S_CONVERSION, "Conversion rate (conversions / clicks)."),
    ("conversion_rate_v2", "percent", S_CONVERSION, "Conversion rate (conversions / impressions)."),
    ("real_time_conversion", "int", S_CONVERSION, "Conversions measured in real time."),
    ("real_time_conversion_rate", "percent", S_CONVERSION, "Real-time conversion rate."),
    ("real_time_conversion_rate_v2", "percent", S_CONVERSION, "Real-time conversion rate (per impression)."),
    ("real_time_cost_per_conversion", "currency", S_COST, "Real-time cost per conversion."),
    ("secondary_goal_result", "int", S_CONVERSION, "Secondary-goal results."),
    ("secondary_goal_result_rate", "percent", S_CONVERSION, "Secondary-goal result rate."),
    ("total_purchase_value", "currency", S_CONVERSION, "Total value of purchase conversions."),
    ("total_onsite_shopping_value", "currency", S_ONSITE, "Total value of onsite shopping events."),
    # --- Attribution windows (scalar rollups) ---
    ("cta_conversion", "int", S_ATTRIBUTION, "Click-through attributed conversions."),
    ("vta_conversion", "int", S_ATTRIBUTION, "View-through attributed conversions."),
    ("evta_conversion", "int", S_ATTRIBUTION, "Engaged-view-through attributed conversions."),
    # --- Video play family (retention curve) ---
    ("video_play_actions", "int", S_VIDEO, "Number of times the video started playing."),
    ("video_watched_2s", "int", S_VIDEO, "Number of 2-second video views."),
    ("video_watched_6s", "int", S_VIDEO, "Number of 6-second video views."),
    ("average_video_play", "decimal", S_VIDEO, "Average watch time per video play (seconds)."),
    ("average_video_play_per_user", "decimal", S_VIDEO, "Average watch time per user (seconds)."),
    ("video_views_p25", "int", S_VIDEO, "Video views reaching 25%."),
    ("video_views_p50", "int", S_VIDEO, "Video views reaching 50%."),
    ("video_views_p75", "int", S_VIDEO, "Video views reaching 75%."),
    ("video_views_p100", "int", S_VIDEO, "Video views reaching 100%."),
    ("total_time_watched", "decimal", S_VIDEO, "Total time the video was watched (seconds)."),
    # --- Engagement family ---
    ("engagements", "int", S_ENGAGEMENT, "Total engagements on the ad."),
    ("engagement_rate", "percent", S_ENGAGEMENT, "Engagement rate."),
    ("likes", "int", S_ENGAGEMENT, "Number of likes."),
    ("comments", "int", S_ENGAGEMENT, "Number of comments."),
    ("shares", "int", S_ENGAGEMENT, "Number of shares."),
    ("follows", "int", S_ENGAGEMENT, "Number of new followers gained."),
    ("profile_visits", "int", S_ENGAGEMENT, "Number of profile visits."),
    ("profile_visits_rate", "percent", S_ENGAGEMENT, "Profile-visit rate."),
    ("clicks_on_hashtag_challenge", "int", S_ENGAGEMENT, "Clicks on the hashtag challenge."),
    ("duet_clicks", "int", S_ENGAGEMENT, "Clicks on the duet element."),
    ("stitch_clicks", "int", S_ENGAGEMENT, "Clicks on the stitch element."),
    ("sound_usage_clicks", "int", S_ENGAGEMENT, "Clicks to use the ad's sound."),
    ("anchor_clicks", "int", S_ENGAGEMENT, "Clicks on the creative anchor."),
    ("ix_page_view_rate", "percent", S_ENGAGEMENT, "Instant-page view rate."),
    # --- Interactive add-on ---
    ("interactive_add_on_impressions", "int", S_INTERACTIVE, "Impressions of the interactive add-on."),
    ("interactive_add_on_destination_clicks", "int", S_INTERACTIVE, "Destination clicks from the interactive add-on."),
    ("interactive_add_on_activity_clicks", "int", S_INTERACTIVE, "Activity clicks on the interactive add-on."),
    ("countdown_sticker_reminder_clicks", "int", S_INTERACTIVE, "Reminder clicks on the countdown sticker."),
    ("gift_code_pop_out_clicks", "int", S_INTERACTIVE, "Clicks on the gift-code pop-out."),
    ("vote_option_a", "int", S_INTERACTIVE, "Votes cast for interactive option A."),
    ("vote_option_b", "int", S_INTERACTIVE, "Votes cast for interactive option B."),
    # --- Live ---
    ("live_views", "int", S_LIVE, "Views of the LIVE room from the ad."),
    ("live_unique_views", "int", S_LIVE, "Unique viewers of the LIVE room."),
    ("live_effective_views", "int", S_LIVE, "Effective LIVE views."),
    ("live_product_clicks", "int", S_LIVE, "Clicks on products during the LIVE."),
    ("live_comments", "int", S_LIVE, "Comments during the LIVE."),
    ("live_likes", "int", S_LIVE, "Likes during the LIVE."),
    ("live_new_followers", "int", S_LIVE, "New followers gained during the LIVE."),
    # --- Product / catalog (shopping) ---
    ("product_impressions", "int", S_PRODUCT, "Product card impressions."),
    ("product_clicks", "int", S_PRODUCT, "Product card clicks."),
    ("onsite_add_to_wishlist", "int", S_ONSITE, "Onsite add-to-wishlist events."),
    ("onsite_shopping", "int", S_ONSITE, "Onsite shopping (checkout) events."),
    ("onsite_shopping_rate", "percent", S_ONSITE, "Onsite shopping rate."),
    ("cost_per_onsite_shopping", "currency", S_COST, "Cost per onsite shopping event."),
    ("onsite_initiate_checkout_count", "int", S_ONSITE, "Onsite initiate-checkout events."),
    ("onsite_add_billing_count", "int", S_ONSITE, "Onsite add-billing events."),
    ("onsite_on_web_detail", "int", S_ONSITE, "Onsite product-detail page views."),
    ("onsite_form", "int", S_ONSITE, "Onsite lead-form submissions."),
    ("onsite_download_start", "int", S_ONSITE, "Onsite download-start events."),
    # --- Reach / frequency extended ---
    ("gross_impressions", "int", S_IMPRESSION, "Gross impressions including invalid traffic."),
    ("average_frequency", "decimal", S_REACH, "Average delivery frequency per user."),
    # --- Video engaged/retention extended ---
    ("video_views_p25_rate", "percent", S_VIDEO, "Rate of video views reaching 25%."),
    ("video_views_p50_rate", "percent", S_VIDEO, "Rate of video views reaching 50%."),
    ("video_views_p75_rate", "percent", S_VIDEO, "Rate of video views reaching 75%."),
    ("video_views_p100_rate", "percent", S_VIDEO, "Rate of video views reaching 100%."),
    ("engaged_view", "int", S_VIDEO, "Number of engaged views (6s or interaction)."),
    ("engaged_view_15s", "int", S_VIDEO, "Number of 15-second engaged views."),
    # --- Purchase value / ROAS family ---
    ("complete_payment_roas", "decimal", S_CONVERSION, "Return on ad spend from complete-payment value."),
    ("onsite_shopping_roas", "decimal", S_ONSITE, "Return on ad spend from onsite shopping value."),
    ("total_complete_payment_rate", "percent", S_CONVERSION, "Complete-payment rate."),
    ("value_per_complete_payment", "currency", S_CONVERSION, "Average value per complete-payment event."),
    # --- Currency-normalized cost variants (Supermetrics-style splits) ---
    ("cost_usd", "currency", S_COST, "Spend converted to USD."),
    ("cost_eur", "currency", S_COST, "Spend converted to EUR."),
    ("cost_gbp", "currency", S_COST, "Spend converted to GBP."),
]

# ---------------------------------------------------------------------------
# 3. EVENT/CONVERSION families expanded over the official event-type list.
#    Each family: (family_id, data_type, section, human label).
#    A flattened "<family>_<event>" field is emitted per event type. This
#    mirrors how the reporting API surfaces the per-event metrics and how
#    Supermetrics flattens the CONVERSION / IN APP EVENT / ONSITE / PAGE
#    sections -- driving the metric total toward their 387 figure.
# ---------------------------------------------------------------------------

# Web/onsite pixel + app (MMP) standard events (the "complete_payment" family etc.).
CONVERSION_EVENTS: list[str] = [
    "complete_payment",
    "value_complete_payment",
    "on_web_order",
    "value_on_web_order",
    "initiate_checkout",
    "value_initiate_checkout",
    "add_billing",
    "add_to_cart",
    "value_add_to_cart",
    "add_to_wishlist",
    "view_content",
    "page_view",
    "click_button",
    "search",
    "form",
    "contact",
    "subscribe",
    "download_start",
    "registration",
    "complete_registration",
    "start_trial",
    "submit_form",
    "generate_lead",
    "user_registration",
    "purchase",
    "value_purchase",
    "product_details_page_browse",
    "landing_page_view",
]

# In-app (MMP) event types for the app-install objective.
INAPP_EVENTS: list[str] = [
    "app_install",
    "registration",
    "purchase",
    "value_purchase",
    "add_to_cart",
    "checkout",
    "add_payment_info",
    "add_to_wishlist",
    "launch_app",
    "complete_tutorial",
    "create_group",
    "join_group",
    "create_gamerole",
    "in_app_ad_click",
    "in_app_ad_impr",
    "next_day_open",
    "add_review",
    "rate",
    "achieve_level",
    "unlock_achievement",
    "spend_credits",
    "loan_apply",
    "loan_credit",
    "loan_disbursement",
    "complete_order",
    "value_total_purchase",
    "value_total_in_app_ad",
]

# The SKAN mirror of the in-app events (post-iOS14 attribution).
SKAN_EVENTS: list[str] = [
    "app_install",
    "registration",
    "purchase",
    "add_to_cart",
    "checkout",
    "add_payment_info",
    "launch_app",
    "in_app_ad_click",
    "in_app_ad_impr",
    "total_conversion",
    "value_total_purchase",
    "value_first_purchase",
]

# Onsite (TikTok native destination) events.
ONSITE_EVENTS: list[str] = [
    "on_web_detail",
    "shopping",
    "add_to_cart",
    "initiate_checkout",
    "add_billing",
    "on_web_order",
    "form",
    "download_start",
    "app_download",
    "web_in_page_impression",
]

# Page (TikTok Instant/native page) events.
PAGE_EVENTS: list[str] = [
    "page_view",
    "button_click",
    "form_submit",
    "phone_click",
    "download_click",
    "product_click",
    "banner_click",
]

# The families and which event set each expands over.
# (family_id, data_type, section, label, events)
EVENT_FAMILIES: list[tuple] = [
    ("conversion", "int", S_CONVERSION, "conversions", CONVERSION_EVENTS),
    ("cost_per_conversion", "currency", S_COST, "cost per conversion", CONVERSION_EVENTS),
    ("conversion_rate", "percent", S_CONVERSION, "conversion rate", CONVERSION_EVENTS),
    ("real_time_conversion", "int", S_CONVERSION, "real-time conversions", CONVERSION_EVENTS),
    ("total", "int", S_INAPP, "in-app event total", INAPP_EVENTS),
    ("cost_per", "currency", S_COST, "in-app event cost", INAPP_EVENTS),
    ("skan_total", "int", S_INAPP_SKAN, "SKAN event total", SKAN_EVENTS),
    ("skan_cost_per", "currency", S_INAPP_SKAN, "SKAN event cost", SKAN_EVENTS),
    ("onsite", "int", S_ONSITE, "onsite event", ONSITE_EVENTS),
    ("cost_per_onsite", "currency", S_ONSITE, "onsite event cost", ONSITE_EVENTS),
    ("page_event", "int", S_PAGE, "page event", PAGE_EVENTS),
]


def _norm(event: str) -> str:
    return event.replace(".", "_").replace("-", "_")


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

    # 1. Structural dimensions
    for row in DIMENSIONS:
        field_id, data_type, section, description = row[:4]
        source_field = row[4] if len(row) > 4 else None
        add(field_id, "dimension", data_type, section, description, source_field)

    # 2. Scalar metrics
    for row in SCALAR_METRICS:
        field_id, data_type, section, description = row[:4]
        source_field = row[4] if len(row) > 4 else None
        add(field_id, "metric", data_type, section, description, source_field)

    # 3. Expand the event/conversion families over their event-type lists.
    for family_id, data_type, section, label, events in EVENT_FAMILIES:
        for ev in events:
            fid = f"{family_id}_{_norm(ev)}"
            add(fid, "metric", data_type, section, f"{label.capitalize()} for event '{ev}'.")

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
