"""Deterministic builder for piano official_fields.json (Story 28.1).

This script is the AUDITABLE SOURCE for the committed official snapshot of the
piano (Piano Analytics, ex-AT Internet) connector. It is committed alongside
its output (official_fields.json) so a reviewer can see exactly how the curated
STANDARD BASELINE column space was derived from the Piano standard-tagging Data
Model (research dossier Annexes A + B: 45 metrics + 103 standard property keys)
with section/tier enrichment cross-read from the Supermetrics catalog (a 162-dim
floor that includes per-source permutations + deprecated/system fields).

It is pure-Python, deterministic (no network, no clock), and byte-stable:
running it again reproduces official_fields.json exactly.

WHY A HAND-AUTHORED BASELINE (the central design fact of this connector):
  Piano Analytics has NO fixed reporting schema and NO public metadata endpoint
  (getStructure / getProperties do not exist in the v3 Data API -- the docs send
  users to the Data Model UI; the third-party python wrapper only calls
  getData/getRowCount/getTotal). Properties (dimensions) and metrics are per-site
  Data Model keys. The committed catalog is therefore the STANDARD BASELINE that
  any standard-tagged site exposes; per-site CUSTOM keys are resolved live and
  validated on use (field_discovery.mode = "dynamic"): an unknown key sent to
  getData returns a 400 -> InvalidRequestError with the pull_invalid_request
  drift signal. No list is ever truncated; no unknown key is silently dropped.

Derivation summary (see catalog_sources.json "_derivation"):
  - METRICS = the 45 explicitly-enumerated standard-tagging metrics of research
    Annex A (NO round-number padding -- the baseline is exactly what the dossier
    lists; a real site's live surface EXCEEDS it via custom keys). Piano's
    columns key is the m_-prefixed form (m_visits). Sections drive tiering.
    Visit/visitor-scoped metrics carry NON_ADDITIVE (AD-4): they do NOT sum
    across property breakdowns (Piano "Why can't we sum visits on page
    analyses?"); landed raw, never blindly summed downstream.
  - DIMENSIONS = the standard property keys of research Annex B (bare key, e.g.
    device_type). "date" is the grain dimension (the DATE property at
    granularity D). physical_type is driven by the catalogued type, NEVER by an
    id suffix (google-ads 26.2 review lesson).
  - Unit / physical_type is authored here per field; the connector reads units
    from physical_type, never from an id-suffix heuristic.

Run (orchestrator, local only -- output is committed):
    uv run python server/modules/piano/catalog_sources/build_official_fields.py
"""

from __future__ import annotations

import json
from pathlib import Path

# ---------------------------------------------------------------------------
# Sections (exactly the section_tier_map keys in catalog_sources.json).
# ---------------------------------------------------------------------------
S_TRAFFIC = "AUDIENCE - OVERALL TRAFFIC"
S_GEO = "AUDIENCE - GEOLOCATION"
S_USERS = "AUDIENCE - USERS"
S_PRIVACY = "AUDIENCE - PRIVACY"
S_TIME = "TIME"
S_DEVICE = "TECHNICAL - DEVICE"
S_PAGES = "CONTENT - PAGES"
S_CLICKS = "CONTENT - CLICKS"
S_EVENTS = "CONTENT - EVENTS"
S_ISE = "CONTENT - INTERNAL SEARCH ENGINE"
S_ONSITE = "CONTENT - ONSITE ADS"
S_MVT = "CONTENT - MV TESTING"
S_SOURCE = "SOURCE - ACQUISITION"
S_ECOMMERCE = "ECOMMERCE"
S_AV = "AV INSIGHTS"
S_AV_CONTENTS = "AV INSIGHTS - CONTENTS"
S_AV_OVERVIEW = "AV INSIGHTS - OVERVIEW"
S_AV_FUNNEL = "AV INSIGHTS - COMPLETION FUNNEL"
S_AV_QOS = "AV INSIGHTS - QOS DELIVERY"

# Physical-type tokens (mapped to the api-catalog physical_type enum by the
# merge engine: integer/decimal/string/date). Authored per field -- the unit is
# driven by THIS type, never by an id suffix.
INT = "integer"
DEC = "decimal"
STR = "string"
DATE = "date"

# ---------------------------------------------------------------------------
# 1. METRICS -- research Annex A (45 standard-tagging metrics). Each tuple:
#    (m_key, physical_type, section, non_additive, description_tail)
#    The Piano columns token is the m_-prefixed key; source_field == field_id.
# ---------------------------------------------------------------------------
# Visit/visitor-scoped metrics are non-additive across property breakdowns
# (Piano's own "can't sum visits" caveat) -- AD-4. Marked True below.
_NON_ADDITIVE = {
    "m_visits",
    "m_unique_visitors",
    "m_bounce_rate",
    "m_avg_pages_per_visit",
    "m_visit_duration",
    "m_exit_rate",
    "m_new_visitors",
}

METRICS: tuple[tuple[str, str, str, str], ...] = (
    # AUDIENCE / OVERALL TRAFFIC (core)
    ("m_visits", INT, S_TRAFFIC, "Number of visits (sessions)."),
    ("m_unique_visitors", INT, S_TRAFFIC, "Number of unique visitors."),
    ("m_page_loads", INT, S_TRAFFIC, "Page displays (page views / page loads)."),
    ("m_bounce_rate", DEC, S_TRAFFIC, "Bounce rate (ratio; recompute at the semantic layer)."),
    ("m_users", INT, S_TRAFFIC, "Number of users."),
    ("m_new_visitors", INT, S_TRAFFIC, "Number of new visitors."),
    ("m_visit_duration", DEC, S_TRAFFIC, "Average visit duration (seconds)."),
    ("m_avg_pages_per_visit", DEC, S_TRAFFIC, "Average pages per visit (ratio)."),
    ("m_entrances", INT, S_TRAFFIC, "Number of entrances."),
    ("m_exit_rate", DEC, S_TRAFFIC, "Exit rate (ratio)."),
    # AV INSIGHTS (standard)
    ("m_av_plays", INT, S_AV, "Audio/video plays."),
    ("m_av_playbacks", INT, S_AV, "Audio/video playbacks."),
    ("m_av_resumes", INT, S_AV, "Audio/video resumes."),
    ("m_av_sessions", INT, S_AV, "Audio/video sessions."),
    ("m_av_shares", INT, S_AV, "Audio/video shares."),
    ("m_av_starts", INT, S_AV, "Audio/video starts."),
    ("m_av_stops", INT, S_AV, "Audio/video stops."),
    ("m_av_average_time_spent", DEC, S_AV, "Average time spent on audio/video."),
    ("m_av_average_time_rebuffering", DEC, S_AV_QOS, "Average rebuffering time."),
    ("m_av_average_global_time_buffering", DEC, S_AV_QOS, "Average global buffering time."),
    ("m_av_average_content_time_consumed", DEC, S_AV, "Average content time consumed."),
    ("m_av_average_playback_completion_rate", DEC, S_AV_FUNNEL, "Avg playback completion rate."),
    ("m_av_average_time_playback", DEC, S_AV, "Average playback time."),
    ("m_av_time_spent", DEC, S_AV, "Total time spent on audio/video."),
    ("m_av_time_playback", DEC, S_AV, "Total playback time."),
    # CONTENT / CLICKS (standard)
    ("m_clicks", INT, S_CLICKS, "Number of clicks."),
    # CONTENT / EVENTS (standard)
    ("m_events", INT, S_EVENTS, "Number of onsite events."),
    # CONTENT / INTERNAL SEARCH (standard)
    ("m_internal_searches", INT, S_ISE, "Number of internal searches."),
    ("m_search_result_clicks", INT, S_ISE, "Number of internal search result clicks."),
    # CONTENT / ONSITE ADS (advanced)
    ("m_onsitead_impressions", INT, S_ONSITE, "Onsite ad impressions."),
    ("m_onsitead_clicks", INT, S_ONSITE, "Onsite ad clicks."),
    ("m_onsitead_click_rate", DEC, S_ONSITE, "Onsite ad click rate (ratio)."),
    # CONTENT / MV TESTING (advanced)
    ("m_mvtesting_impressions", INT, S_MVT, "Multivariate testing impressions."),
    ("m_mvtesting_clicks", INT, S_MVT, "Multivariate testing clicks."),
    # SOURCE / ACQUISITION (standard)
    ("m_visits_from_source", INT, S_SOURCE, "Visits attributed to the source."),
    ("m_entry_page_visits", INT, S_SOURCE, "Entry-page visits."),
    # ECOMMERCE / GOALS (standard)
    ("m_conversions", INT, S_ECOMMERCE, "Number of conversions."),
    ("m_conversion_rate", DEC, S_ECOMMERCE, "Conversion rate (ratio)."),
    ("m_revenue", DEC, S_ECOMMERCE, "Revenue (site currency)."),
    ("m_transactions", INT, S_ECOMMERCE, "Number of transactions."),
    ("m_avg_order_value", DEC, S_ECOMMERCE, "Average order value (site currency)."),
    ("m_cart_additions", INT, S_ECOMMERCE, "Number of cart additions."),
    # PRIVACY (advanced)
    ("m_consent_optin", INT, S_PRIVACY, "Consent opt-in events."),
    ("m_consent_optout", INT, S_PRIVACY, "Consent opt-out events."),
    ("m_consent_exempt", INT, S_PRIVACY, "Consent-exempt events."),
)

# ---------------------------------------------------------------------------
# 2. DIMENSIONS -- research Annex B (standard property keys). Each tuple:
#    (property_key, physical_type, section, description_tail)
#    Piano columns token is the bare key; "date" is the grain dimension.
# ---------------------------------------------------------------------------
DIMENSIONS: tuple[tuple[str, str, str, str], ...] = (
    # TIME
    ("date", DATE, S_TIME, "Reporting day (Piano DATE property at granularity D, site timezone)."),
    ("day", STR, S_TIME, "Day."),
    ("week", STR, S_TIME, "Week."),
    ("month", STR, S_TIME, "Month."),
    ("quarter", STR, S_TIME, "Quarter."),
    ("year", STR, S_TIME, "Year."),
    ("hour", STR, S_TIME, "Hour."),
    ("day_of_week", STR, S_TIME, "Day of week."),
    ("event_hour", STR, S_TIME, "Event hour."),
    ("event_minute", STR, S_TIME, "Event minute."),
    ("event_second", STR, S_TIME, "Event second."),
    ("hit_time_utc", STR, S_TIME, "Hit time (UTC)."),
    ("event_time", STR, S_TIME, "Event time (site timezone)."),
    ("event_time_utc", STR, S_TIME, "Event time (UTC)."),
    # GEO
    ("geo_country", STR, S_GEO, "Visitor country."),
    ("geo_region", STR, S_GEO, "Visitor region."),
    ("geo_city", STR, S_GEO, "Visitor city."),
    ("geo_metro", STR, S_GEO, "Visitor metro area."),
    ("geo_continent", STR, S_GEO, "Visitor continent."),
    ("browser_language", STR, S_GEO, "Browser language."),
    # TECHNICAL / DEVICE
    ("device_type", STR, S_DEVICE, "Device type (desktop / mobile / tablet)."),
    ("device_brand", STR, S_DEVICE, "Device brand."),
    ("device_name", STR, S_DEVICE, "Device name."),
    ("os", STR, S_DEVICE, "Operating system."),
    ("os_group", STR, S_DEVICE, "Operating system group."),
    ("os_version", STR, S_DEVICE, "Operating system version."),
    ("browser", STR, S_DEVICE, "Browser."),
    ("browser_group", STR, S_DEVICE, "Browser group."),
    ("browser_version", STR, S_DEVICE, "Browser version."),
    ("screen_resolution", STR, S_DEVICE, "Screen resolution."),
    ("connection_type", STR, S_DEVICE, "Connection type."),
    # AUDIENCE / TRAFFIC
    ("site", STR, S_TRAFFIC, "Site name."),
    ("site_id", STR, S_TRAFFIC, "Site id (measurement perimeter)."),
    ("src_portal_domain", STR, S_TRAFFIC, "Portal domain."),
    ("src_portal_url", STR, S_TRAFFIC, "Portal URL."),
    # VISITOR / USERS
    ("user_id", STR, S_USERS, "User id."),
    ("user_category", STR, S_USERS, "User category."),
    ("user_days", STR, S_USERS, "User days."),
    ("visitor_id", STR, S_USERS, "Visitor id."),
    ("visitor_privacy_consent", STR, S_PRIVACY, "Visitor privacy consent status."),
    ("visitor_privacy_mode", STR, S_PRIVACY, "Visitor privacy mode."),
    # SOURCE / ACQUISITION
    ("src_source", STR, S_SOURCE, "Acquisition source."),
    ("src_medium", STR, S_SOURCE, "Acquisition medium."),
    ("src_campaign", STR, S_SOURCE, "Acquisition campaign."),
    ("src_source_detail", STR, S_SOURCE, "Acquisition source detail."),
    ("src_referrer_site_url", STR, S_SOURCE, "Referrer site URL."),
    ("src_type", STR, S_SOURCE, "Source type."),
    ("src_organic_keyword", STR, S_SOURCE, "Organic keyword."),
    ("src_se", STR, S_SOURCE, "Search engine."),
    ("src_direct_access", STR, S_SOURCE, "Direct access marker."),
    # CONTENT / PAGES
    ("page", STR, S_PAGES, "Page."),
    ("page_full_name", STR, S_PAGES, "Page full name."),
    ("page_chapter1", STR, S_PAGES, "Page chapter 1."),
    ("page_chapter2", STR, S_PAGES, "Page chapter 2."),
    ("page_chapter3", STR, S_PAGES, "Page chapter 3."),
    ("pageview_id", STR, S_PAGES, "Page view id."),
    ("page_duration", STR, S_PAGES, "Page duration."),
    ("visit_page_views", STR, S_PAGES, "Visit page views."),
    ("site_level2", STR, S_PAGES, "Site level 2."),
    ("page_content_type", STR, S_PAGES, "Page content type."),
    ("page_title_html", STR, S_PAGES, "Page HTML title."),
    # CONTENT / CLICKS
    ("click", STR, S_CLICKS, "Click."),
    ("click_chapter1", STR, S_CLICKS, "Click chapter 1."),
    ("click_chapter2", STR, S_CLICKS, "Click chapter 2."),
    ("click_chapter3", STR, S_CLICKS, "Click chapter 3."),
    ("click_full_name", STR, S_CLICKS, "Click full name."),
    # CONTENT / EVENTS
    ("event_name", STR, S_EVENTS, "Event name."),
    ("event_id", STR, S_EVENTS, "Event id."),
    ("event_position", STR, S_EVENTS, "Event position."),
    ("event_url", STR, S_EVENTS, "Event URL."),
    ("event_collection_version", STR, S_EVENTS, "Event collection version."),
    ("event_collection_platform", STR, S_EVENTS, "Event collection platform."),
    # CONTENT / INTERNAL SEARCH
    ("ise_keyword", STR, S_ISE, "Internal search keyword."),
    ("ise_page", STR, S_ISE, "Internal search page."),
    ("ise_result", STR, S_ISE, "Internal search result."),
    ("ise_click_rank", STR, S_ISE, "Internal search click rank."),
    # CONTENT / ONSITE ADS
    ("onsitead_advertiser", STR, S_ONSITE, "Onsite ad advertiser."),
    ("onsitead_campaign", STR, S_ONSITE, "Onsite ad campaign."),
    ("onsitead_category", STR, S_ONSITE, "Onsite ad category."),
    ("onsitead_creation", STR, S_ONSITE, "Onsite ad creation."),
    ("onsitead_detailed_placement", STR, S_ONSITE, "Onsite ad detailed placement."),
    ("onsitead_format", STR, S_ONSITE, "Onsite ad format."),
    ("onsitead_general_placement", STR, S_ONSITE, "Onsite ad general placement."),
    ("onsitead_type", STR, S_ONSITE, "Onsite ad type."),
    ("onsitead_url", STR, S_ONSITE, "Onsite ad URL."),
    ("onsitead_variant", STR, S_ONSITE, "Onsite ad variant."),
    # CONTENT / MV TESTING
    ("mv_test", STR, S_MVT, "Multivariate test."),
    ("mv_creation", STR, S_MVT, "Multivariate creation."),
    ("mv_wave", STR, S_MVT, "Multivariate wave."),
    # AV INSIGHTS / CONTENTS
    ("av_content", STR, S_AV_CONTENTS, "Audio/video content."),
    ("av_show", STR, S_AV_CONTENTS, "Audio/video show."),
    ("av_content_genre", STR, S_AV_CONTENTS, "Audio/video content genre."),
    ("av_channel", STR, S_AV_CONTENTS, "Audio/video channel."),
    ("av_broadcasting_type", STR, S_AV_OVERVIEW, "Audio/video broadcasting type."),
    ("av_content_type", STR, S_AV_OVERVIEW, "Audio/video content type."),
    ("av_player", STR, S_AV_QOS, "Audio/video player."),
    ("av_session_id", STR, S_AV_FUNNEL, "Audio/video session id."),
    # ECOMMERCE
    ("transaction_id", STR, S_ECOMMERCE, "Transaction id."),
    ("product_id", STR, S_ECOMMERCE, "Product id."),
    ("product_name", STR, S_ECOMMERCE, "Product name."),
    ("product_category", STR, S_ECOMMERCE, "Product category."),
    ("cart_id", STR, S_ECOMMERCE, "Cart id."),
    ("promotion", STR, S_ECOMMERCE, "Promotion."),
)

_BASE_DESC = (
    "Piano Analytics standard-tagging {kind} '{key}' (Data Query API v3 baseline;"
    " research dossier {annex}). {tail}"
)


def _metric_entries() -> list[dict]:
    out: list[dict] = []
    for key, ptype, section, tail in METRICS:
        desc = _BASE_DESC.format(kind="metric", key=key, annex="Annex A", tail=tail)
        if key in _NON_ADDITIVE:
            desc += (
                " NON-ADDITIVE (AD-4): visit/visitor-scoped, does NOT sum across"
                " property breakdowns (use getTotal for site totals); landed raw."
            )
        out.append(
            {
                "field_id": key,
                "source_field": key,
                "kind": "metric",
                "data_type": ptype,
                "section": section,
                "description": desc,
                "non_additive": key in _NON_ADDITIVE,
            }
        )
    return out


def _dimension_entries() -> list[dict]:
    out: list[dict] = []
    for key, ptype, section, tail in DIMENSIONS:
        desc = _BASE_DESC.format(kind="property", key=key, annex="Annex B", tail=tail)
        out.append(
            {
                "field_id": key,
                "source_field": key,
                "kind": "dimension",
                "data_type": ptype,
                "section": section,
                "description": desc,
                "non_additive": False,
            }
        )
    return out


def build_official_fields() -> list[dict]:
    """Return the byte-stable official_fields.json content (metrics + dims)."""
    fields = _metric_entries() + _dimension_entries()
    # Deterministic order: kind then field_id (stable, reviewable).
    fields.sort(key=lambda f: (f["kind"], f["field_id"]))
    return fields


def main() -> None:
    fields = build_official_fields()
    out_path = Path(__file__).parent / "official_fields.json"
    out_path.write_text(
        json.dumps(fields, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    metrics = sum(1 for f in fields if f["kind"] == "metric")
    dimensions = sum(1 for f in fields if f["kind"] == "dimension")
    non_additive = sum(1 for f in fields if f.get("non_additive"))
    print(
        f"official_fields.json written: {len(fields)} fields "
        f"({metrics} metrics, {dimensions} dimensions, "
        f"{non_additive} non-additive) -> {out_path}"
    )


if __name__ == "__main__":
    main()
