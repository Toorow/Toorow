"""Deterministic builder for google-analytics official_fields.json (Story 25.7).

This script is the AUDITABLE SOURCE for the curated official GA4 Data API
schema snapshot. It is committed alongside its output (official_fields.json)
so a reviewer can see exactly how the curated list was derived from the
official GA4 Data API "API Dimensions & Metrics" schema page recorded in
catalog_sources.json.

It is pure-Python, deterministic (no network, no clock), and byte-stable:
running it again reproduces official_fields.json exactly.

Derivation summary (see catalog_sources.json "_derivation"):
  - Every field is a STANDARD (predefined) GA4 Data API dimension or metric
    from the official api-schema page. Custom / event-scoped / property-scoped
    dynamic fields (customEvent:*, customUser:*, customItem:*, dimensionN,
    metricN, keyEvents:<name>) are DELIBERATELY excluded: they are not part of
    the standard schema and are per-property, so they cannot be cataloged
    statically without a live property.
  - field_id policy: the fifteen fields the manifest's source_capabilities
    already exposes keep their platform-side snake_case id and carry an
    explicit ``source_field`` = the GA4 apiName (e.g. active_users ->
    activeUsers). This keeps drift_ids EMPTY and the manifest<->catalog gate
    green (no source_field_mismatch). Every other field keeps the GA4 apiName
    verbatim as its field_id (camelCase), so source_field defaults to it.
  - source_field discipline (25.4 AC2): the GA4 runReport request uses these
    exact apiNames; the connector's transform() renames via the manifest's
    canonical_*_mapping. sessionSourceMedium / firstUserSourceMedium are real
    GA4 apiNames even though the api-schema page lists them under the combined
    source+medium family.

Run (orchestrator, local only):
    uv run python server/modules/google-analytics/catalog_sources/build_official_fields.py
"""

from __future__ import annotations

import json
from pathlib import Path

# ---------------------------------------------------------------------------
# Sections (Supermetrics-aligned families) used by the section_tier_map.
# ---------------------------------------------------------------------------
S_TIME = "TIME"
S_GEO = "GEO"
S_DEVICE = "DEVICE"
S_CONTENT = "CONTENT"
S_USER_SOURCE = "USER SOURCE"      # first-user (acquisition) attribution
S_SESSION_SOURCE = "SESSION SOURCE"  # session (last-click) attribution
S_MANUAL_SOURCE = "MANUAL SOURCE"  # utm_* manual campaign params
S_ADS = "ADS"                       # Google Ads
S_CM360 = "CM360"
S_DV360 = "DV360"
S_SA360 = "SA360"
S_ECOMMERCE = "ECOMMERCE"
S_EVENT = "EVENT"
S_SEARCH = "SEARCH"
S_LINKS = "LINKS"
S_AUDIENCE = "AUDIENCE"
S_USER = "USER"
S_SESSION = "SESSION"
S_DAILY_COHORT = "DAILY COHORT"
S_PUBLISHER = "PUBLISHER"
S_VIDEO = "VIDEO"
S_GAMING = "GAMING"
S_BASIC = "BASIC"

# ---------------------------------------------------------------------------
# 1. DIMENSIONS. Tuple shape: (field_id, apiName, data_type, section, description)
#    apiName == field_id for every field EXCEPT the manifest-exposed ones,
#    where field_id is the platform snake_case id and apiName is the GA4 token.
# ---------------------------------------------------------------------------
DIMENSIONS: list[tuple] = [
    # --- Time ---
    ("date", "date", "date", S_TIME, "The date of the event, formatted as YYYYMMDD."),
    ("dateHour", "dateHour", "string", S_TIME, "The combined values of date and hour (YYYYMMDDHH)."),
    ("dateHourMinute", "dateHourMinute", "string", S_TIME, "The combined values of date, hour, and minute (YYYYMMDDHHMM)."),
    ("day", "day", "string", S_TIME, "The day of the month, a two-digit number from 01 to 31."),
    ("dayOfWeek", "dayOfWeek", "integer", S_TIME, "The integer day of the week (0-6, Sunday first)."),
    ("dayOfWeekName", "dayOfWeekName", "string", S_TIME, "The day of the week in English."),
    ("hour", "hour", "string", S_TIME, "The two-digit hour of day (00-23) in property time zone."),
    ("minute", "minute", "string", S_TIME, "The two-digit minute of the hour (00-59) in property time zone."),
    ("month", "month", "string", S_TIME, "The month of the event, a two-digit integer from 01 to 12."),
    ("isoWeek", "isoWeek", "string", S_TIME, "ISO week number, where each week starts on Monday."),
    ("isoYear", "isoYear", "string", S_TIME, "The ISO year of the event."),
    ("isoYearIsoWeek", "isoYearIsoWeek", "string", S_TIME, "The combined values of isoWeek and isoYear."),
    ("nthDay", "nthDay", "string", S_TIME, "The number of days since the start of the date range."),
    ("nthHour", "nthHour", "string", S_TIME, "The number of hours since the start of the date range."),
    ("nthMinute", "nthMinute", "string", S_TIME, "The number of minutes since the start of the date range."),
    ("nthMonth", "nthMonth", "string", S_TIME, "The number of months since the start of the date range."),
    ("nthWeek", "nthWeek", "string", S_TIME, "The number of weeks since the start of the date range."),
    ("nthYear", "nthYear", "string", S_TIME, "The number of years since the start of the date range."),

    # --- Geography ---
    ("continent", "continent", "string", S_GEO, "The continent from which the user activity originated."),
    ("continentId", "continentId", "string", S_GEO, "The geographic ID of the continent from which activity originated."),
    ("country", "country", "string", S_GEO, "The country from which the user activity originated."),
    ("countryId", "countryId", "string", S_GEO, "The geographic ID of the country (ISO 3166-1 alpha-2)."),
    ("region", "region", "string", S_GEO, "The geographic region from which the user activity originated."),
    ("city", "city", "string", S_GEO, "The city from which the user activity originated."),
    ("cityId", "cityId", "string", S_GEO, "The geographic ID of the city from which activity originated."),

    # --- Platform / Device ---
    ("device_category", "deviceCategory", "string", S_DEVICE, "The type of device: Desktop, Tablet, or Mobile."),
    ("deviceModel", "deviceModel", "string", S_DEVICE, "The mobile device model (example: iPhone 10,6)."),
    ("mobileDeviceBranding", "mobileDeviceBranding", "string", S_DEVICE, "Manufacturer or branded name (Samsung, HTC, Verizon)."),
    ("mobileDeviceMarketingName", "mobileDeviceMarketingName", "string", S_DEVICE, "The branded device name (Galaxy S10 or P30 Pro)."),
    ("mobileDeviceModel", "mobileDeviceModel", "string", S_DEVICE, "The mobile device model name (iPhone X or SM-G950F)."),
    ("operatingSystem", "operatingSystem", "string", S_DEVICE, "The operating systems used by visitors."),
    ("operatingSystemVersion", "operatingSystemVersion", "string", S_DEVICE, "The operating system versions (Android 10, iOS 13.5.1)."),
    ("operatingSystemWithVersion", "operatingSystemWithVersion", "string", S_DEVICE, "The operating system and version combined."),
    ("browser", "browser", "string", S_DEVICE, "The browsers used to view your website."),
    ("browserVersion", "browserVersion", "string", S_DEVICE, "The version of the browser."),
    ("screenResolution", "screenResolution", "string", S_DEVICE, "The screen resolution of the user's monitor (1920x1080)."),
    ("platform", "platform", "string", S_DEVICE, "The platform on which your app or website ran (web, iOS, Android)."),
    ("platformDeviceCategory", "platformDeviceCategory", "string", S_DEVICE, "The platform and type of device combined."),
    ("language", "language", "string", S_DEVICE, "The language setting of the user's browser or device."),
    ("languageCode", "languageCode", "string", S_DEVICE, "The language setting of the user's browser or device (ISO 639)."),

    # --- Page / Screen ---
    ("pageLocation", "pageLocation", "string", S_CONTENT, "The protocol, hostname, page path, and query string for web pages visited."),
    ("page", "pagePath", "string", S_CONTENT, "The portion of the URL between the hostname and the query string."),
    ("pagePathPlusQueryString", "pagePathPlusQueryString", "string", S_CONTENT, "The portion of the URL following the hostname (path + query string)."),
    ("pageTitle", "pageTitle", "string", S_CONTENT, "The web page titles used on your site."),
    ("pageReferrer", "pageReferrer", "string", S_CONTENT, "The full referring URL including the hostname and path."),
    ("hostName", "hostName", "string", S_CONTENT, "Includes the subdomain and domain names of a URL."),
    ("fullPageUrl", "fullPageUrl", "string", S_CONTENT, "The hostname, page path, and query string of visited web pages."),
    ("landing_page", "landingPage", "string", S_CONTENT, "The page path associated with the first pageview in a session."),
    ("landingPagePlusQueryString", "landingPagePlusQueryString", "string", S_CONTENT, "The page path + query string associated with the first pageview in a session."),
    ("contentGroup", "contentGroup", "string", S_CONTENT, "A category that applies to items of published content."),
    ("contentId", "contentId", "string", S_CONTENT, "The identifier of the selected content."),
    ("contentType", "contentType", "string", S_CONTENT, "The category of the selected content."),

    # --- Traffic source: session (last-click) ---
    ("session_source_medium", "sessionSourceMedium", "string", S_SESSION_SOURCE, "The combined source and medium that led to the session."),
    ("session_campaign", "sessionCampaignName", "string", S_SESSION_SOURCE, "The marketing campaign name for a session."),
    ("sessionCampaignId", "sessionCampaignId", "string", S_SESSION_SOURCE, "The marketing campaign ID for a session."),
    ("source", "source", "string", S_SESSION_SOURCE, "The source attributed to the key event."),
    ("medium", "medium", "string", S_SESSION_SOURCE, "The medium attributed to the key event."),
    ("defaultChannelGroup", "defaultChannelGroup", "string", S_SESSION_SOURCE, "The key event's default channel group based on source and medium."),
    ("primaryChannelGroup", "primaryChannelGroup", "string", S_SESSION_SOURCE, "The primary channel group attributed to the key event."),

    # --- Traffic source: first user (acquisition) ---
    ("first_user_source_medium", "firstUserSourceMedium", "string", S_USER_SOURCE, "The combined source and medium that first acquired the user."),
    ("firstUserSource", "firstUserSource", "string", S_USER_SOURCE, "The source that first acquired the user."),
    ("firstUserMedium", "firstUserMedium", "string", S_USER_SOURCE, "The medium that first acquired the user."),
    ("firstUserDefaultChannelGroup", "firstUserDefaultChannelGroup", "string", S_USER_SOURCE, "The default channel group that first acquired the user."),
    ("firstUserPrimaryChannelGroup", "firstUserPrimaryChannelGroup", "string", S_USER_SOURCE, "The primary channel group that first acquired the user."),

    # --- Manual campaign parameters (utm_*) : session-scoped ---
    ("manualSource", "manualSource", "string", S_MANUAL_SOURCE, "The manual source (utm_source) that led to the key event."),
    ("manualMedium", "manualMedium", "string", S_MANUAL_SOURCE, "The manual medium (utm_medium) that led to the key event."),
    ("manualCampaignName", "manualCampaignName", "string", S_MANUAL_SOURCE, "The manual campaign name (utm_campaign) that led to the key event."),
    ("manualCampaignId", "manualCampaignId", "string", S_MANUAL_SOURCE, "The manual campaign ID (utm_id) that led to the key event."),
    ("manualContent", "manualContent", "string", S_MANUAL_SOURCE, "The ad content (utm_content) that led to the key event."),
    ("manualTerm", "manualTerm", "string", S_MANUAL_SOURCE, "The term (utm_term) attributed to the key event."),
    ("manualSourceMedium", "manualSourceMedium", "string", S_MANUAL_SOURCE, "The combination of the manual source and medium."),
    ("manualSourcePlatform", "manualSourcePlatform", "string", S_MANUAL_SOURCE, "The platform (utm_source_platform) that led to the key event."),
    ("manualCreativeFormat", "manualCreativeFormat", "string", S_MANUAL_SOURCE, "The creative format (utm_creative_format) that led to the key event."),
    ("manualMarketingTactic", "manualMarketingTactic", "string", S_MANUAL_SOURCE, "The targeting criteria (utm_marketing_tactic) that led to the key event."),
    ("firstUserManualSource", "firstUserManualSource", "string", S_MANUAL_SOURCE, "The manual source (utm_source) that first acquired the user."),
    ("firstUserManualMedium", "firstUserManualMedium", "string", S_MANUAL_SOURCE, "The manual medium (utm_medium) that first acquired the user."),
    ("firstUserManualCampaignName", "firstUserManualCampaignName", "string", S_MANUAL_SOURCE, "The manual campaign name (utm_campaign) that first acquired the user."),
    ("firstUserManualCampaignId", "firstUserManualCampaignId", "string", S_MANUAL_SOURCE, "The manual campaign ID (utm_id) that first acquired the user."),
    ("firstUserManualContent", "firstUserManualContent", "string", S_MANUAL_SOURCE, "The ad content (utm_content) that first acquired the user."),
    ("firstUserManualTerm", "firstUserManualTerm", "string", S_MANUAL_SOURCE, "The term (utm_term) that first acquired the user."),
    ("firstUserManualSourceMedium", "firstUserManualSourceMedium", "string", S_MANUAL_SOURCE, "The combination of the manual source and medium that first acquired the user."),
    ("firstUserManualSourcePlatform", "firstUserManualSourcePlatform", "string", S_MANUAL_SOURCE, "The manual source platform (utm_source_platform) that first acquired the user."),
    ("firstUserManualCreativeFormat", "firstUserManualCreativeFormat", "string", S_MANUAL_SOURCE, "The manual creative format (utm_creative_format) that first acquired the user."),
    ("firstUserManualMarketingTactic", "firstUserManualMarketingTactic", "string", S_MANUAL_SOURCE, "The manual marketing tactic (utm_marketing_tactic) that first acquired the user."),

    # --- Google Ads ---
    ("googleAdsCampaignName", "googleAdsCampaignName", "string", S_ADS, "The campaign name for the Google Ads campaign attributed to the key event."),
    ("googleAdsCampaignId", "googleAdsCampaignId", "string", S_ADS, "The campaign ID for the Google Ads campaign attributed to the key event."),
    ("googleAdsCampaignType", "googleAdsCampaignType", "string", S_ADS, "The Google Ads campaign type (Search, Display, Shopping, Video, etc.)."),
    ("googleAdsAdGroupName", "googleAdsAdGroupName", "string", S_ADS, "The ad group name attributed to the key event."),
    ("googleAdsAdGroupId", "googleAdsAdGroupId", "string", S_ADS, "The Google Ads ad group ID attributed to the key event."),
    ("googleAdsCreativeId", "googleAdsCreativeId", "string", S_ADS, "The ID of the Google Ads creative."),
    ("googleAdsKeyword", "googleAdsKeyword", "string", S_ADS, "The matched keyword that led to the key event."),
    ("googleAdsQuery", "googleAdsQuery", "string", S_ADS, "The search query that led to the key event."),
    ("googleAdsAdNetworkType", "googleAdsAdNetworkType", "string", S_ADS, "The advertising network type (Google search, Display, etc.)."),
    ("googleAdsAccountName", "googleAdsAccountName", "string", S_ADS, "The account name from Google Ads for the key event."),
    ("googleAdsCustomerId", "googleAdsCustomerId", "string", S_ADS, "The customer ID from Google Ads."),
    ("firstUserGoogleAdsCampaignName", "firstUserGoogleAdsCampaignName", "string", S_ADS, "The name of the Google Ads campaign that first acquired the user."),
    ("firstUserGoogleAdsCampaignId", "firstUserGoogleAdsCampaignId", "string", S_ADS, "The identifier of the Google Ads campaign that first acquired the user."),
    ("firstUserGoogleAdsCampaignType", "firstUserGoogleAdsCampaignType", "string", S_ADS, "The Google Ads campaign type that first acquired the user."),
    ("firstUserGoogleAdsAdGroupName", "firstUserGoogleAdsAdGroupName", "string", S_ADS, "The ad group name that first acquired the user."),
    ("firstUserGoogleAdsAdGroupId", "firstUserGoogleAdsAdGroupId", "string", S_ADS, "The ad group ID that first acquired the user."),
    ("firstUserGoogleAdsCreativeId", "firstUserGoogleAdsCreativeId", "string", S_ADS, "The Google Ads creative ID that first acquired the user."),
    ("firstUserGoogleAdsKeyword", "firstUserGoogleAdsKeyword", "string", S_ADS, "The Google Ads keyword text that first acquired the user."),
    ("firstUserGoogleAdsQuery", "firstUserGoogleAdsQuery", "string", S_ADS, "The search query that first acquired the user."),
    ("firstUserGoogleAdsAdNetworkType", "firstUserGoogleAdsAdNetworkType", "string", S_ADS, "The advertising network that first acquired the user."),
    ("firstUserGoogleAdsAccountName", "firstUserGoogleAdsAccountName", "string", S_ADS, "The account name from Google Ads that first acquired the user."),
    ("firstUserGoogleAdsCustomerId", "firstUserGoogleAdsCustomerId", "string", S_ADS, "The customer ID from Google Ads that first acquired the user."),

    # --- Campaign Manager 360 (CM360) : key-event scoped ---
    ("cm360CampaignName", "cm360CampaignName", "string", S_CM360, "The CM360 campaign name that led to the key event."),
    ("cm360CampaignId", "cm360CampaignId", "string", S_CM360, "The CM360 campaign ID that led to the key event."),
    ("cm360AdvertiserName", "cm360AdvertiserName", "string", S_CM360, "The CM360 advertiser name that led to the key event."),
    ("cm360AdvertiserId", "cm360AdvertiserId", "string", S_CM360, "The CM360 advertiser ID that led to the key event."),
    ("cm360AccountName", "cm360AccountName", "string", S_CM360, "The CM360 account name that led to the key event."),
    ("cm360AccountId", "cm360AccountId", "string", S_CM360, "The CM360 account ID that led to the key event."),
    ("cm360CreativeName", "cm360CreativeName", "string", S_CM360, "The CM360 creative name that led to the key event."),
    ("cm360CreativeId", "cm360CreativeId", "string", S_CM360, "The CM360 creative ID that led to the key event."),
    ("cm360CreativeType", "cm360CreativeType", "string", S_CM360, "The CM360 creative type that led to the key event."),
    ("cm360CreativeTypeId", "cm360CreativeTypeId", "string", S_CM360, "The CM360 creative type ID that led to the key event."),
    ("cm360CreativeFormat", "cm360CreativeFormat", "string", S_CM360, "The CM360 creative format that led to the key event."),
    ("cm360CreativeVersion", "cm360CreativeVersion", "string", S_CM360, "The CM360 creative version that led to the key event."),
    ("cm360Medium", "cm360Medium", "string", S_CM360, "The CM360 medium that led to the key event."),
    ("cm360PlacementCostStructure", "cm360PlacementCostStructure", "string", S_CM360, "How the CM360 media cost is calculated (e.g. CPM)."),
    ("cm360PlacementName", "cm360PlacementName", "string", S_CM360, "The given name for a CM360 placement."),
    ("cm360PlacementId", "cm360PlacementId", "string", S_CM360, "Identifies a CM360 placement."),
    ("cm360RenderingId", "cm360RenderingId", "string", S_CM360, "The CM360 rendering ID that led to the key event."),
    ("cm360SiteName", "cm360SiteName", "string", S_CM360, "The CM360 site name from which the ad space was purchased."),
    ("cm360SiteId", "cm360SiteId", "string", S_CM360, "The CM360 site ID that led to the key event."),
    ("cm360Source", "cm360Source", "string", S_CM360, "The CM360 source (also referred to as site name)."),
    ("cm360SourceMedium", "cm360SourceMedium", "string", S_CM360, "The CM360 source medium that led to the key event."),
    ("firstUserCm360CampaignName", "firstUserCm360CampaignName", "string", S_CM360, "The CM360 campaign name that originally acquired the user."),
    ("firstUserCm360CampaignId", "firstUserCm360CampaignId", "string", S_CM360, "The CM360 campaign ID that originally acquired the user."),
    ("firstUserCm360AdvertiserName", "firstUserCm360AdvertiserName", "string", S_CM360, "The CM360 advertiser name that originally acquired the user."),
    ("firstUserCm360AdvertiserId", "firstUserCm360AdvertiserId", "string", S_CM360, "The CM360 advertiser ID that originally acquired the user."),
    ("firstUserCm360AccountName", "firstUserCm360AccountName", "string", S_CM360, "The CM360 account name that originally acquired the user."),
    ("firstUserCm360AccountId", "firstUserCm360AccountId", "string", S_CM360, "The CM360 account ID that originally acquired the user."),
    ("firstUserCm360CreativeName", "firstUserCm360CreativeName", "string", S_CM360, "The CM360 creative name that originally acquired the user."),
    ("firstUserCm360CreativeId", "firstUserCm360CreativeId", "string", S_CM360, "The CM360 creative ID that originally acquired the user."),
    ("firstUserCm360CreativeType", "firstUserCm360CreativeType", "string", S_CM360, "The CM360 creative type that originally acquired the user."),
    ("firstUserCm360CreativeTypeId", "firstUserCm360CreativeTypeId", "string", S_CM360, "The CM360 creative type ID that originally acquired the user."),
    ("firstUserCm360CreativeFormat", "firstUserCm360CreativeFormat", "string", S_CM360, "The CM360 creative format that originally acquired the user."),
    ("firstUserCm360CreativeVersion", "firstUserCm360CreativeVersion", "string", S_CM360, "The CM360 creative version that originally acquired the user."),
    ("firstUserCm360Medium", "firstUserCm360Medium", "string", S_CM360, "The CM360 medium that originally acquired the user."),
    ("firstUserCm360PlacementCostStructure", "firstUserCm360PlacementCostStructure", "string", S_CM360, "The CM360 placement cost structure that originally acquired the user."),
    ("firstUserCm360PlacementName", "firstUserCm360PlacementName", "string", S_CM360, "The CM360 placement name that originally acquired the user."),
    ("firstUserCm360PlacementId", "firstUserCm360PlacementId", "string", S_CM360, "The CM360 placement ID that originally acquired the user."),
    ("firstUserCm360RenderingId", "firstUserCm360RenderingId", "string", S_CM360, "The CM360 rendering ID that originally acquired the user."),
    ("firstUserCm360SiteName", "firstUserCm360SiteName", "string", S_CM360, "The CM360 site name that originally acquired the user."),
    ("firstUserCm360SiteId", "firstUserCm360SiteId", "string", S_CM360, "The CM360 site ID that originally acquired the user."),
    ("firstUserCm360Source", "firstUserCm360Source", "string", S_CM360, "The CM360 source that originally acquired the user."),
    ("firstUserCm360SourceMedium", "firstUserCm360SourceMedium", "string", S_CM360, "The CM360 source medium that originally acquired the user."),
    ("sessionCm360CampaignName", "sessionCm360CampaignName", "string", S_CM360, "The CM360 campaign name that led to the session."),
    ("sessionCm360CampaignId", "sessionCm360CampaignId", "string", S_CM360, "The CM360 campaign ID that led to the session."),
    ("sessionCm360AdvertiserName", "sessionCm360AdvertiserName", "string", S_CM360, "The CM360 advertiser name that led to the session."),
    ("sessionCm360AdvertiserId", "sessionCm360AdvertiserId", "string", S_CM360, "The CM360 advertiser ID that led to the session."),
    ("sessionCm360AccountName", "sessionCm360AccountName", "string", S_CM360, "The CM360 account name that led to the session."),
    ("sessionCm360AccountId", "sessionCm360AccountId", "string", S_CM360, "The CM360 account ID that led to the session."),
    ("sessionCm360CreativeName", "sessionCm360CreativeName", "string", S_CM360, "The CM360 creative name that led to the session."),
    ("sessionCm360CreativeId", "sessionCm360CreativeId", "string", S_CM360, "The CM360 creative ID that led to the session."),
    ("sessionCm360CreativeType", "sessionCm360CreativeType", "string", S_CM360, "The CM360 creative type that led to the session."),
    ("sessionCm360CreativeTypeId", "sessionCm360CreativeTypeId", "string", S_CM360, "The CM360 creative type ID that led to the session."),
    ("sessionCm360CreativeFormat", "sessionCm360CreativeFormat", "string", S_CM360, "The CM360 creative format that led to the session."),
    ("sessionCm360CreativeVersion", "sessionCm360CreativeVersion", "string", S_CM360, "The CM360 creative version that led to the session."),
    ("sessionCm360Medium", "sessionCm360Medium", "string", S_CM360, "The CM360 medium that led to the session."),
    ("sessionCm360PlacementCostStructure", "sessionCm360PlacementCostStructure", "string", S_CM360, "The CM360 placement cost structure that led to the session."),
    ("sessionCm360PlacementName", "sessionCm360PlacementName", "string", S_CM360, "The CM360 placement name that led to the session."),
    ("sessionCm360PlacementId", "sessionCm360PlacementId", "string", S_CM360, "The CM360 placement ID that led to the session."),

    # --- Display & Video 360 (DV360) : key-event scoped ---
    ("dv360CampaignName", "dv360CampaignName", "string", S_DV360, "The DV360 campaign name that led to the key event."),
    ("dv360CampaignId", "dv360CampaignId", "string", S_DV360, "The DV360 campaign ID that led to the key event."),
    ("dv360AdvertiserName", "dv360AdvertiserName", "string", S_DV360, "The DV360 advertiser name that led to the key event."),
    ("dv360AdvertiserId", "dv360AdvertiserId", "string", S_DV360, "The DV360 advertiser ID that led to the key event."),
    ("dv360LineItemName", "dv360LineItemName", "string", S_DV360, "The DV360 line item name that led to the key event."),
    ("dv360LineItemId", "dv360LineItemId", "string", S_DV360, "The DV360 line item ID that led to the key event."),
    ("dv360InsertionOrderName", "dv360InsertionOrderName", "string", S_DV360, "The DV360 insertion order name that led to the key event."),
    ("dv360InsertionOrderId", "dv360InsertionOrderId", "string", S_DV360, "The DV360 insertion order ID that led to the key event."),
    ("dv360CreativeName", "dv360CreativeName", "string", S_DV360, "The DV360 creative name that led to the key event."),
    ("dv360CreativeId", "dv360CreativeId", "string", S_DV360, "The DV360 creative ID that led to the key event."),
    ("dv360CreativeFormat", "dv360CreativeFormat", "string", S_DV360, "The DV360 creative format (expandable, video, native)."),
    ("dv360Medium", "dv360Medium", "string", S_DV360, "The DV360 medium that led to the key event (e.g. cpm)."),
    ("dv360ExchangeName", "dv360ExchangeName", "string", S_DV360, "The DV360 exchange name that led to the key event."),
    ("dv360ExchangeId", "dv360ExchangeId", "string", S_DV360, "The DV360 exchange ID that led to the key event."),
    ("dv360PartnerName", "dv360PartnerName", "string", S_DV360, "The DV360 partner name that led to the key event."),
    ("dv360PartnerId", "dv360PartnerId", "string", S_DV360, "The DV360 partner ID that led to the key event."),
    ("dv360Source", "dv360Source", "string", S_DV360, "The DV360 source (site name where the ad displayed)."),
    ("dv360SourceMedium", "dv360SourceMedium", "string", S_DV360, "The DV360 source medium that led to the key event."),
    ("firstUserDv360CampaignName", "firstUserDv360CampaignName", "string", S_DV360, "The DV360 campaign name that originally acquired the user."),
    ("firstUserDv360CampaignId", "firstUserDv360CampaignId", "string", S_DV360, "The DV360 campaign ID that originally acquired the user."),
    ("firstUserDv360AdvertiserName", "firstUserDv360AdvertiserName", "string", S_DV360, "The DV360 advertiser name that originally acquired the user."),
    ("firstUserDv360AdvertiserId", "firstUserDv360AdvertiserId", "string", S_DV360, "The DV360 advertiser ID that originally acquired the user."),
    ("firstUserDv360LineItemName", "firstUserDv360LineItemName", "string", S_DV360, "The DV360 line item name that originally acquired the user."),
    ("firstUserDv360LineItemId", "firstUserDv360LineItemId", "string", S_DV360, "The DV360 line item ID that originally acquired the user."),
    ("firstUserDv360InsertionOrderName", "firstUserDv360InsertionOrderName", "string", S_DV360, "The DV360 insertion order name that originally acquired the user."),
    ("firstUserDv360InsertionOrderId", "firstUserDv360InsertionOrderId", "string", S_DV360, "The DV360 insertion order ID that originally acquired the user."),
    ("firstUserDv360CreativeName", "firstUserDv360CreativeName", "string", S_DV360, "The DV360 creative name that originally acquired the user."),
    ("firstUserDv360CreativeId", "firstUserDv360CreativeId", "string", S_DV360, "The DV360 creative ID that originally acquired the user."),
    ("firstUserDv360CreativeFormat", "firstUserDv360CreativeFormat", "string", S_DV360, "The DV360 creative format that originally acquired the user."),
    ("firstUserDv360Medium", "firstUserDv360Medium", "string", S_DV360, "The DV360 medium that originally acquired the user."),
    ("firstUserDv360ExchangeName", "firstUserDv360ExchangeName", "string", S_DV360, "The DV360 exchange name that originally acquired the user."),
    ("firstUserDv360ExchangeId", "firstUserDv360ExchangeId", "string", S_DV360, "The DV360 exchange ID that originally acquired the user."),
    ("firstUserDv360PartnerName", "firstUserDv360PartnerName", "string", S_DV360, "The DV360 partner name that originally acquired the user."),
    ("firstUserDv360PartnerId", "firstUserDv360PartnerId", "string", S_DV360, "The DV360 partner ID that originally acquired the user."),
    ("firstUserDv360Source", "firstUserDv360Source", "string", S_DV360, "The DV360 source that originally acquired the user."),
    ("firstUserDv360SourceMedium", "firstUserDv360SourceMedium", "string", S_DV360, "The DV360 source medium that originally acquired the user."),

    # --- Search Ads 360 (SA360) : key-event scoped ---
    ("sa360CampaignName", "sa360CampaignName", "string", S_SA360, "The SA360 campaign name that led to the key event."),
    ("sa360CampaignId", "sa360CampaignId", "string", S_SA360, "The SA360 campaign ID that led to the key event."),
    ("sa360AdGroupName", "sa360AdGroupName", "string", S_SA360, "The SA360 ad group name that led to the key event."),
    ("sa360AdGroupId", "sa360AdGroupId", "string", S_SA360, "The SA360 ad group ID that led to the key event."),
    ("sa360KeywordText", "sa360KeywordText", "string", S_SA360, "The SA360 keyword text that led to the key event."),
    ("sa360EngineAccountName", "sa360EngineAccountName", "string", S_SA360, "The SA360 engine account name that led to the key event."),
    ("sa360EngineAccountId", "sa360EngineAccountId", "string", S_SA360, "The SA360 engine account ID that led to the key event."),
    ("sa360EngineAccountType", "sa360EngineAccountType", "string", S_SA360, "The SA360 engine account type that led to the key event."),
    ("sa360ManagerAccountName", "sa360ManagerAccountName", "string", S_SA360, "The SA360 manager account name that led to the key event."),
    ("sa360ManagerAccountId", "sa360ManagerAccountId", "string", S_SA360, "The SA360 manager account ID that led to the key event."),
    ("sa360CreativeFormat", "sa360CreativeFormat", "string", S_SA360, "The SA360 creative format that led to the key event."),
    ("sa360Medium", "sa360Medium", "string", S_SA360, "The SA360 medium that led to the key event."),
    ("sa360Query", "sa360Query", "string", S_SA360, "The SA360 query that led to the key event."),
    ("sa360Source", "sa360Source", "string", S_SA360, "The SA360 source that led to the key event."),
    ("sa360SourceMedium", "sa360SourceMedium", "string", S_SA360, "The SA360 source medium that led to the key event."),
    ("firstUserSa360CampaignName", "firstUserSa360CampaignName", "string", S_SA360, "The SA360 campaign name that originally acquired the user."),
    ("firstUserSa360CampaignId", "firstUserSa360CampaignId", "string", S_SA360, "The SA360 campaign ID that originally acquired the user."),
    ("firstUserSa360AdGroupName", "firstUserSa360AdGroupName", "string", S_SA360, "The SA360 ad group name that originally acquired the user."),
    ("firstUserSa360AdGroupId", "firstUserSa360AdGroupId", "string", S_SA360, "The SA360 ad group ID that originally acquired the user."),
    ("firstUserSa360KeywordText", "firstUserSa360KeywordText", "string", S_SA360, "The SA360 keyword text that originally acquired the user."),
    ("firstUserSa360EngineAccountName", "firstUserSa360EngineAccountName", "string", S_SA360, "The SA360 engine account name that originally acquired the user."),
    ("firstUserSa360EngineAccountId", "firstUserSa360EngineAccountId", "string", S_SA360, "The SA360 engine account ID that originally acquired the user."),
    ("firstUserSa360EngineAccountType", "firstUserSa360EngineAccountType", "string", S_SA360, "The SA360 engine account type that originally acquired the user."),
    ("firstUserSa360ManagerAccountName", "firstUserSa360ManagerAccountName", "string", S_SA360, "The SA360 manager account name that originally acquired the user."),
    ("firstUserSa360ManagerAccountId", "firstUserSa360ManagerAccountId", "string", S_SA360, "The SA360 manager account ID that originally acquired the user."),
    ("firstUserSa360CreativeFormat", "firstUserSa360CreativeFormat", "string", S_SA360, "The SA360 creative format that originally acquired the user."),
    ("firstUserSa360Medium", "firstUserSa360Medium", "string", S_SA360, "The SA360 medium that originally acquired the user."),
    ("firstUserSa360Query", "firstUserSa360Query", "string", S_SA360, "The SA360 query that originally acquired the user."),
    ("firstUserSa360Source", "firstUserSa360Source", "string", S_SA360, "The SA360 source that originally acquired the user."),
    ("firstUserSa360SourceMedium", "firstUserSa360SourceMedium", "string", S_SA360, "The SA360 source medium that originally acquired the user."),

    # --- E-commerce (dimensions) ---
    ("transaction_id", "transactionId", "string", S_ECOMMERCE, "The ID of the e-commerce transaction."),
    ("itemName", "itemName", "string", S_ECOMMERCE, "The name of the item."),
    ("itemId", "itemId", "string", S_ECOMMERCE, "The ID of the item."),
    ("itemBrand", "itemBrand", "string", S_ECOMMERCE, "The brand name of the item."),
    ("itemCategory", "itemCategory", "string", S_ECOMMERCE, "The hierarchical category in which the item is classified."),
    ("itemCategory2", "itemCategory2", "string", S_ECOMMERCE, "The hierarchical category in which the item is classified (second level)."),
    ("itemCategory3", "itemCategory3", "string", S_ECOMMERCE, "The hierarchical category in which the item is classified (third level)."),
    ("itemCategory4", "itemCategory4", "string", S_ECOMMERCE, "The hierarchical category in which the item is classified (fourth level)."),
    ("itemCategory5", "itemCategory5", "string", S_ECOMMERCE, "The hierarchical category in which the item is classified (fifth level)."),
    ("itemVariant", "itemVariant", "string", S_ECOMMERCE, "The specific variation of a product (XS, S, M, L)."),
    ("itemAffiliation", "itemAffiliation", "string", S_ECOMMERCE, "The name or code of the affiliate/partner for the item."),
    ("itemListName", "itemListName", "string", S_ECOMMERCE, "The name of the item list."),
    ("itemListId", "itemListId", "string", S_ECOMMERCE, "The ID of the item list."),
    ("itemListPosition", "itemListPosition", "string", S_ECOMMERCE, "The position of an item in a list."),
    ("itemLocationID", "itemLocationID", "string", S_ECOMMERCE, "The physical location associated with the item."),
    ("itemPromotionName", "itemPromotionName", "string", S_ECOMMERCE, "The name of the promotion for the item."),
    ("itemPromotionId", "itemPromotionId", "string", S_ECOMMERCE, "The ID of the item promotion."),
    ("itemPromotionCreativeName", "itemPromotionCreativeName", "string", S_ECOMMERCE, "The name of the item-promotion creative."),
    ("itemPromotionCreativeSlot", "itemPromotionCreativeSlot", "string", S_ECOMMERCE, "The name of the promotional creative slot."),
    ("currencyCode", "currencyCode", "string", S_ECOMMERCE, "The local currency code (ISO 4217) of the e-commerce event."),
    ("orderCoupon", "orderCoupon", "string", S_ECOMMERCE, "The code for the order-level coupon."),

    # --- Events ---
    ("eventName", "eventName", "string", S_EVENT, "The name of the event."),
    ("isKeyEvent", "isKeyEvent", "boolean", S_EVENT, "The string 'true' if the event is a key event."),

    # --- Search ---
    ("searchTerm", "searchTerm", "string", S_SEARCH, "The term searched by the user."),

    # --- Outbound links & file downloads ---
    ("linkDomain", "linkDomain", "string", S_LINKS, "The destination domain of the outbound link."),
    ("linkUrl", "linkUrl", "string", S_LINKS, "The full URL for an outbound link or file download."),
    ("linkText", "linkText", "string", S_LINKS, "The link text of the file download."),
    ("linkId", "linkId", "string", S_LINKS, "The HTML ID attribute for an outbound link or file download."),
    ("linkClasses", "linkClasses", "string", S_LINKS, "The HTML class attribute for an outbound link or file download."),
    ("fileExtension", "fileExtension", "string", S_LINKS, "The extension of the downloaded file (e.g. pdf, txt)."),
    ("fileName", "fileName", "string", S_LINKS, "The page path of the downloaded file."),
    ("outbound", "outbound", "string", S_LINKS, "Returns 'true' if the link led to a site outside the domain."),
    ("percentScrolled", "percentScrolled", "string", S_CONTENT, "The percentage down the page that the user scrolled (e.g. 90)."),

    # --- Audience ---
    ("audienceName", "audienceName", "string", S_AUDIENCE, "The given name of an audience."),
    ("audienceId", "audienceId", "string", S_AUDIENCE, "The numeric identifier of an audience."),
    ("audienceResourceName", "audienceResourceName", "string", S_AUDIENCE, "The resource name of this audience."),

    # --- User ---
    ("newVsReturning", "newVsReturning", "string", S_USER, "New users (0 prior sessions) or returning users (1+ prior sessions)."),
    ("firstSessionDate", "firstSessionDate", "string", S_USER, "The date (YYYYMMDD) the user's first session occurred."),
    ("brandingInterest", "brandingInterest", "string", S_USER, "Interests demonstrated by users higher in the shopping funnel."),

    # --- Cohort ---
    ("cohort", "cohort", "string", S_DAILY_COHORT, "The cohort's name in the request."),
    ("cohortNthDay", "cohortNthDay", "string", S_DAILY_COHORT, "The day offset relative to firstSessionDate within the cohort."),
    ("cohortNthWeek", "cohortNthWeek", "string", S_DAILY_COHORT, "The week offset relative to firstSessionDate within the cohort."),
    ("cohortNthMonth", "cohortNthMonth", "string", S_DAILY_COHORT, "The month offset relative to firstSessionDate within the cohort."),

    # --- AdMob / publisher (dimensions) ---
    ("adFormat", "adFormat", "string", S_PUBLISHER, "Describes the way ads looked and where they appeared."),
    ("adSourceName", "adSourceName", "string", S_PUBLISHER, "The source network that served the ad."),
    ("adUnitName", "adUnitName", "string", S_PUBLISHER, "The name chosen to describe this ad unit."),

    # --- Gaming ---
    ("achievementId", "achievementId", "string", S_GAMING, "The achievement ID in a game for an event."),
    ("character", "character", "string", S_GAMING, "The player character in a game for an event."),
    ("groupId", "groupId", "string", S_GAMING, "The player group ID in a game for an event."),
    ("level", "level", "string", S_GAMING, "The player's level in a game."),
]

# ---------------------------------------------------------------------------
# 2. METRICS. Tuple shape: (field_id, apiName, data_type, section, description)
# ---------------------------------------------------------------------------
METRICS: list[tuple] = [
    # --- User ---
    ("active_users", "activeUsers", "integer", S_USER, "The number of distinct users who visited your site or app."),
    ("newUsers", "newUsers", "integer", S_USER, "The number of users who interacted with your site or launched your app for the first time."),
    ("totalUsers", "totalUsers", "integer", S_USER, "The total number of unique users who logged an event."),
    ("active1DayUsers", "active1DayUsers", "integer", S_USER, "The number of distinct active users on your site or app within a 1-day period."),
    ("active7DayUsers", "active7DayUsers", "integer", S_USER, "The number of distinct active users on your site or app within a 7-day period."),
    ("active28DayUsers", "active28DayUsers", "integer", S_USER, "The number of distinct active users on your site or app within a 28-day period."),
    ("dauPerMau", "dauPerMau", "decimal", S_USER, "The rolling percentage of 30-day active users who are also 1-day active users."),
    ("dauPerWau", "dauPerWau", "decimal", S_USER, "The rolling percentage of 7-day active users who are also 1-day active users."),
    ("wauPerMau", "wauPerMau", "decimal", S_USER, "The rolling percentage of 30-day active users who are also 7-day active users."),

    # --- Session ---
    ("sessions", "sessions", "integer", S_SESSION, "The number of sessions that began on your site or app."),
    ("sessionsPerUser", "sessionsPerUser", "decimal", S_SESSION, "The average number of sessions per user."),
    ("averageSessionDuration", "averageSessionDuration", "decimal", S_SESSION, "The average duration (seconds) of users' sessions."),
    ("engagedSessions", "engagedSessions", "integer", S_SESSION, "The number of sessions that lasted longer than 10 seconds, had a key event, or had 2+ screen/page views."),
    ("engagementRate", "engagementRate", "decimal", S_SESSION, "The percentage of engaged sessions."),
    ("bounceRate", "bounceRate", "decimal", S_SESSION, "The percentage of sessions that were not engaged."),

    # --- Event ---
    ("eventCount", "eventCount", "integer", S_EVENT, "The count of events."),
    ("eventCountPerUser", "eventCountPerUser", "decimal", S_EVENT, "The average number of events per user."),
    ("eventValue", "eventValue", "decimal", S_EVENT, "The sum of the event parameter named 'value'."),
    ("eventsPerSession", "eventsPerSession", "decimal", S_EVENT, "The average number of events per session."),
    ("conversions", "conversions", "integer", S_EVENT, "The count of key events (legacy apiName 'conversions'; GA4 UI 'Key events')."),
    ("keyEvents", "keyEvents", "integer", S_EVENT, "The count of key events."),
    ("sessionKeyEventRate", "sessionKeyEventRate", "decimal", S_EVENT, "The percentage of sessions in which any key event was triggered."),
    ("userKeyEventRate", "userKeyEventRate", "decimal", S_EVENT, "The percentage of users who triggered any key event."),

    # --- Page / Screen ---
    ("screen_page_views", "screenPageViews", "integer", S_CONTENT, "The number of app screens or web pages your users viewed."),
    ("screenPageViewsPerSession", "screenPageViewsPerSession", "decimal", S_CONTENT, "The average number of screens/pages viewed per session."),
    ("screenPageViewsPerUser", "screenPageViewsPerUser", "decimal", S_CONTENT, "The average number of screens/pages viewed per user."),

    # --- E-commerce (metrics) ---
    ("totalRevenue", "totalRevenue", "decimal", S_ECOMMERCE, "The sum of revenue from purchases, subscriptions, and advertising."),
    ("purchase_revenue", "purchaseRevenue", "decimal", S_ECOMMERCE, "The sum of revenue from purchases made in your app or site."),
    ("grossPurchaseRevenue", "grossPurchaseRevenue", "decimal", S_ECOMMERCE, "The sum of revenue from purchases, before refunds."),
    ("refundAmount", "refundAmount", "decimal", S_ECOMMERCE, "The total refunded transaction revenues."),
    ("grossItemRevenue", "grossItemRevenue", "decimal", S_ECOMMERCE, "The total revenue from items only, before refunds."),
    ("itemRevenue", "itemRevenue", "decimal", S_ECOMMERCE, "The total revenue from items only."),
    ("shippingAmount", "shippingAmount", "decimal", S_ECOMMERCE, "The shipping amount associated with a transaction."),
    ("taxAmount", "taxAmount", "decimal", S_ECOMMERCE, "The tax amount associated with a transaction."),
    ("transactions", "transactions", "integer", S_ECOMMERCE, "The count of transaction events with purchase revenue."),
    ("transactionsPerPurchaser", "transactionsPerPurchaser", "decimal", S_ECOMMERCE, "The average number of transactions per purchaser."),
    ("ecommercePurchases", "ecommercePurchases", "integer", S_ECOMMERCE, "The number of times users completed a purchase."),
    ("purchaserRate", "purchaserRate", "decimal", S_ECOMMERCE, "The percentage of active users who made one or more purchases."),
    ("firstTimePurchaserRate", "firstTimePurchaserRate", "decimal", S_ECOMMERCE, "The percentage of active users who made their first purchase."),
    ("firstTimePurchasers", "firstTimePurchasers", "integer", S_ECOMMERCE, "The number of users who made their first purchase."),
    ("firstTimePurchaserConversionRate", "firstTimePurchaserConversionRate", "decimal", S_ECOMMERCE, "The percentage of active users who made their first-ever purchase."),
    ("totalPurchasers", "totalPurchasers", "integer", S_ECOMMERCE, "The number of users who made a purchase in the reporting date range."),
    ("averagePurchaseRevenue", "averagePurchaseRevenue", "decimal", S_ECOMMERCE, "The average purchase revenue in the transaction group of events."),
    ("averagePurchaseRevenuePerUser", "averagePurchaseRevenuePerUser", "decimal", S_ECOMMERCE, "The average purchase revenue per active user."),
    ("averagePurchaseRevenuePerPayingUser", "averagePurchaseRevenuePerPayingUser", "decimal", S_ECOMMERCE, "The average revenue per paying user (ARPPU)."),
    ("averageRevenuePerUser", "averageRevenuePerUser", "decimal", S_ECOMMERCE, "The average revenue per active user (ARPU)."),
    ("itemsViewed", "itemsViewed", "integer", S_ECOMMERCE, "The number of units viewed for a single item."),
    ("itemsClickedInList", "itemsClickedInList", "integer", S_ECOMMERCE, "The number of units clicked in a list for a single item."),
    ("itemsClickedInPromotion", "itemsClickedInPromotion", "integer", S_ECOMMERCE, "The number of units clicked in a promotion for a single item."),
    ("itemsAddedToCart", "itemsAddedToCart", "integer", S_ECOMMERCE, "The number of units added to cart for a single item."),
    ("itemsCheckedOut", "itemsCheckedOut", "integer", S_ECOMMERCE, "The number of units checked out for a single item."),
    ("itemsPurchased", "itemsPurchased", "integer", S_ECOMMERCE, "The number of units for a single item included in purchase events."),
    ("itemsViewedInList", "itemsViewedInList", "integer", S_ECOMMERCE, "The number of units viewed in a list for a single item."),
    ("itemsViewedInPromotion", "itemsViewedInPromotion", "integer", S_ECOMMERCE, "The number of units viewed in a promotion for a single item."),
    ("itemListClickThroughRate", "itemListClickThroughRate", "decimal", S_ECOMMERCE, "The number of users who selected a list divided by the number who viewed the same list."),
    ("itemListViewEvents", "itemListViewEvents", "integer", S_ECOMMERCE, "The number of times an item list was viewed."),
    ("itemListClickEvents", "itemListClickEvents", "integer", S_ECOMMERCE, "The number of times users clicked an item that appeared in a list."),
    ("itemPromotionClickThroughRate", "itemPromotionClickThroughRate", "decimal", S_ECOMMERCE, "The number of users who selected a promotion divided by the number who viewed it."),
    ("itemViewEvents", "itemViewEvents", "integer", S_ECOMMERCE, "The number of times the item details were viewed."),
    ("cartToViewRate", "cartToViewRate", "decimal", S_ECOMMERCE, "The number of users who added a product to their cart divided by the number who viewed it."),
    ("itemRefundAmount", "itemRefundAmount", "decimal", S_ECOMMERCE, "The item refund amount is the total refunded transaction revenue from items only."),
    ("refunds", "refunds", "integer", S_ECOMMERCE, "The number of refunded transactions."),

    # --- Advertising (Google Ads cost/clicks/impressions imported into GA4) ---
    ("advertiserAdCost", "advertiserAdCost", "decimal", S_ADS, "The total amount you paid for your ads."),
    ("advertiserAdClicks", "advertiserAdClicks", "integer", S_ADS, "The total number of times users clicked on your ad."),
    ("advertiserAdCostPerClick", "advertiserAdCostPerClick", "decimal", S_ADS, "The ad cost per click (CPC)."),
    ("advertiserAdCostPerKeyEvent", "advertiserAdCostPerKeyEvent", "decimal", S_ADS, "The ad cost per key event."),
    ("advertiserAdImpressions", "advertiserAdImpressions", "integer", S_ADS, "The total number of impressions."),
    ("returnOnAdSpend", "returnOnAdSpend", "decimal", S_ADS, "Return on ad spend (ROAS): total revenue divided by advertiser ad cost."),

    # --- Publisher / AdMob (metrics) ---
    ("totalAdRevenue", "totalAdRevenue", "decimal", S_PUBLISHER, "The total advertising revenue from both AdMob and third-party sources."),
    ("publisherAdImpressions", "publisherAdImpressions", "integer", S_PUBLISHER, "The number of ad impressions logged by publisher_ad_impression events."),
    ("publisherAdClicks", "publisherAdClicks", "integer", S_PUBLISHER, "The number of publisher_ad_click events."),
    ("adUnitExposure", "adUnitExposure", "integer", S_PUBLISHER, "The time (milliseconds) an ad unit was exposed to a user."),

    # --- Cohort (metrics) ---
    ("cohortActiveUsers", "cohortActiveUsers", "integer", S_DAILY_COHORT, "The number of users in the cohort who are active in the time window."),
    ("cohortTotalUsers", "cohortTotalUsers", "integer", S_DAILY_COHORT, "The total number of users in the cohort."),

    # --- Engagement & app health ---
    ("userEngagementDuration", "userEngagementDuration", "decimal", S_SESSION, "The total time (seconds) your website or app was in the foreground of users' devices."),
    ("crashAffectedUsers", "crashAffectedUsers", "integer", S_EVENT, "The number of users who logged a crash in this row of the report."),
    ("crashFreeUsersRate", "crashFreeUsersRate", "decimal", S_EVENT, "The percentage of users without crash events in this row of the report."),
    ("scrolledUsers", "scrolledUsers", "integer", S_EVENT, "The number of unique users who scrolled down at least 90% of the page."),
]


def build() -> list[dict]:
    out: list[dict] = []
    seen: set[str] = set()

    def add(field_id, api_name, kind, data_type, section, description):
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
        # source_field only when it differs from field_id (25.4 AC2): keeps the
        # manifest<->catalog gate free of source_field_mismatch.
        if api_name != field_id:
            entry["source_field"] = api_name
        out.append(entry)

    for field_id, api_name, data_type, section, description in DIMENSIONS:
        add(field_id, api_name, "dimension", data_type, section, description)
    for field_id, api_name, data_type, section, description in METRICS:
        add(field_id, api_name, "metric", data_type, section, description)

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
