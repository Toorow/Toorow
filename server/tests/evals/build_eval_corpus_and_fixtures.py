"""Generator for evaluation corpus.yaml and committed result fixtures (Story 14.1).

Executes reference SQL queries against the local DuckDB seed warehouse, serializes
fixtures to JSON, computes SHA-256 hashes, and writes corpus.yaml.

Story 14.2 addendum: each question gains a deterministic ``tool_invocation`` block
derived MECHANICALLY from its canonical_correct reference SQL. The addendum is pure
test code (AD-17 unchanged); it never touches reference_queries / fixtures / SHAs, so
regen stays byte-stable for the existing fields. See ``derive_tool_invocation`` for the
resolution rules and the selector vocabulary (also documented in schema.md).
"""

# ruff: noqa: E501

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

import duckdb
import yaml

EVALS_DIR = Path(__file__).parent
FIXTURES_DIR = EVALS_DIR / "fixtures"
FIXTURES_DIR.mkdir(parents=True, exist_ok=True)

DUCKDB_PATH = Path(__file__).parents[2] / "modules" / "google-analytics" / "seeds" / "local.duckdb"

RAW_QUESTIONS: list[dict[str, Any]] = [
    # -----------------------------------------------------------------------
    # Group 1: daily_report (8 questions)
    # -----------------------------------------------------------------------
    {
        "id": "daily_report_ga4_sessions_30d",
        "question": "Combien de sessions Google Analytics ai-je eu sur les 30 derniers jours ?",
        "as_of": "2026-07-15",
        "surface": "daily_report",
        "difficulty": "easy",
        "tags": ["ga4", "sessions", "additive"],
        "reference_queries": [
            {
                "role": "canonical_correct",
                "note": "Standard 30-day GA4 sessions via country breakdown",
                "reference_sql": (
                    "SELECT SUM(value) AS total_sessions "
                    "FROM marts.fact_daily_kpi "
                    "WHERE project_id = 'default' AND connector = 'google-analytics' "
                    "AND metric = 'sessions' AND breakdown_dimension = 'country' "
                    "AND date BETWEEN '2026-06-16' AND '2026-07-15'"
                ),
                "fixture_name": "daily_report_ga4_sessions_30d.json",
            }
        ],
        "expected_citations": [
            {
                "source_system": "google-analytics",
                "source_field": "fact_daily_kpi",
                "pull_id_required": True,
            }
        ],
        "updated_at": "2026-07-20T00:00:00Z",
    },
    {
        "id": "daily_report_meta_cost_30d",
        "question": "Quel est le coût total de mes campagnes Meta Ads sur les 30 derniers jours ?",
        "as_of": "2026-07-15",
        "surface": "daily_report",
        "difficulty": "easy",
        "tags": ["meta-ads", "cost", "campaign"],
        "reference_queries": [
            {
                "role": "canonical_correct",
                "note": "Total Meta Ads spend at CAMPAIGN data_level",
                "reference_sql": (
                    "SELECT SUM(value) AS total_cost "
                    "FROM marts.fact_daily_kpi "
                    "WHERE project_id = 'default' AND connector = 'meta-ads' "
                    "AND metric = 'cost' AND breakdown_dimension = 'campaign_id' "
                    "AND date BETWEEN '2026-06-16' AND '2026-07-15'"
                ),
                "fixture_name": "daily_report_meta_cost_30d.json",
            }
        ],
        "expected_citations": [
            {
                "source_system": "meta-ads",
                "source_field": "fact_daily_kpi",
                "pull_id_required": True,
            }
        ],
        "updated_at": "2026-07-20T00:00:00Z",
    },
    {
        "id": "daily_report_gsc_clicks_30d",
        "question": "Combien de clics SEO ai-je obtenus d'après Google Search Console sur les 30 derniers jours ?",
        "as_of": "2026-07-15",
        "surface": "daily_report",
        "difficulty": "easy",
        "tags": ["gsc", "clicks", "seo"],
        "reference_queries": [
            {
                "role": "canonical_correct",
                "note": "Total GSC clicks via page breakdown",
                "reference_sql": (
                    "SELECT SUM(value) AS total_clicks "
                    "FROM marts.fact_daily_kpi "
                    "WHERE project_id = 'default' AND connector = 'gsc' "
                    "AND metric = 'clicks' AND breakdown_dimension = 'page' "
                    "AND date BETWEEN '2026-06-16' AND '2026-07-15'"
                ),
                "fixture_name": "daily_report_gsc_clicks_30d.json",
            }
        ],
        "expected_citations": [
            {"source_system": "gsc", "source_field": "fact_daily_kpi", "pull_id_required": True}
        ],
        "updated_at": "2026-07-20T00:00:00Z",
    },
    {
        "id": "daily_report_cross_connector_sessions_conversions",
        "question": "Donne-moi les sessions GA4 et les conversions Meta Ads sur les 7 derniers jours.",
        "as_of": "2026-07-15",
        "surface": "daily_report",
        "difficulty": "medium",
        "tags": ["cross-connector", "ga4", "meta-ads"],
        "reference_queries": [
            {
                "role": "canonical_correct",
                "note": "Cross-connector daily report split by connector",
                "reference_sql": (
                    "SELECT connector, metric, SUM(value) AS total_value "
                    "FROM marts.fact_daily_kpi "
                    "WHERE project_id = 'default' AND ( "
                    "(connector = 'google-analytics' AND metric = 'sessions' AND breakdown_dimension = 'country') OR "
                    "(connector = 'meta-ads' AND metric = 'conversions' AND breakdown_dimension = 'campaign_id') "
                    ") AND date BETWEEN '2026-07-09' AND '2026-07-15' "
                    "GROUP BY connector, metric ORDER BY connector, metric"
                ),
                "fixture_name": "daily_report_cross_connector_sessions_conversions.json",
            }
        ],
        "expected_citations": [
            {
                "source_system": "google-analytics",
                "source_field": "fact_daily_kpi",
                "pull_id_required": True,
            },
            {
                "source_system": "meta-ads",
                "source_field": "fact_daily_kpi",
                "pull_id_required": True,
            },
        ],
        "updated_at": "2026-07-20T00:00:00Z",
    },
    {
        "id": "daily_report_freshness_edge",
        "question": "Quel est le volume de sessions GA4 enregistré le dernier jour disponible (2026-07-15) ?",
        "as_of": "2026-07-15",
        "surface": "daily_report",
        "difficulty": "easy",
        "tags": ["ga4", "freshness", "single_day"],
        "reference_queries": [
            {
                "role": "canonical_correct",
                "note": "GA4 sessions on exact as_of date",
                "reference_sql": (
                    "SELECT date, SUM(value) AS sessions "
                    "FROM marts.fact_daily_kpi "
                    "WHERE project_id = 'default' AND connector = 'google-analytics' "
                    "AND metric = 'sessions' AND breakdown_dimension = 'country' "
                    "AND date = '2026-07-15' GROUP BY date"
                ),
                "fixture_name": "daily_report_freshness_edge.json",
            }
        ],
        "expected_citations": [
            {
                "source_system": "google-analytics",
                "source_field": "fact_daily_kpi",
                "pull_id_required": True,
            }
        ],
        "updated_at": "2026-07-20T00:00:00Z",
    },
    {
        "id": "daily_report_empty_date_range",
        "question": "Quel est le volume de clics GSC sur une plage de dates sans données (janvier 2025) ?",
        "as_of": "2026-07-15",
        "surface": "daily_report",
        "difficulty": "easy",
        "tags": ["gsc", "empty_range"],
        "reference_queries": [
            {
                "role": "canonical_correct",
                # FIX 2026-07-21 (honesty): SUM without GROUP BY always returns exactly one row
                # with NULL value when no rows match — this is real data, not an empty set.
                # Drop expected_empty; the fixture is [{"total_clicks": null}], which honestly
                # tells the agent "no data in this range". expected_empty is reserved for
                # queries that return 0 rows (GROUP BY over no matching data).
                "note": "Out-of-range SUM returns one null row — fixture is [{total_clicks: null}]",
                "reference_sql": (
                    "SELECT SUM(value) AS total_clicks "
                    "FROM marts.fact_daily_kpi "
                    "WHERE project_id = 'default' AND connector = 'gsc' "
                    "AND metric = 'clicks' AND date BETWEEN '2025-01-01' AND '2025-01-07'"
                ),
                "fixture_name": "daily_report_empty_date_range.json",
            }
        ],
        "expected_citations": [
            {"source_system": "gsc", "source_field": "fact_daily_kpi", "pull_id_required": False}
        ],
        "updated_at": "2026-07-21T00:00:00Z",
    },
    {
        "id": "daily_report_project_not_found",
        "question": "Quel est le volume de sessions pour un projet non existant ?",
        "as_of": "2026-07-15",
        "surface": "daily_report",
        "difficulty": "easy",
        "tags": ["ga4", "error_path", "unknown_project"],
        "reference_queries": [
            {
                "role": "canonical_correct",
                # FIX 2026-07-21 (honesty): SUM without GROUP BY returns one null row even when
                # project_id does not exist. Drop expected_empty; fixture is [{"total_sessions": null}],
                # which honestly represents "no project found". expected_empty = strictly 0 rows.
                "note": "Non-existent project SUM returns one null row — fixture is [{total_sessions: null}]",
                "reference_sql": (
                    "SELECT SUM(value) AS total_sessions "
                    "FROM marts.fact_daily_kpi "
                    "WHERE project_id = 'non_existent_project' AND connector = 'google-analytics' "
                    "AND metric = 'sessions' AND date BETWEEN '2026-06-16' AND '2026-07-15'"
                ),
                "fixture_name": "daily_report_project_not_found.json",
            }
        ],
        "expected_citations": [],
        "updated_at": "2026-07-21T00:00:00Z",
    },
    {
        "id": "daily_report_provenance_pull_id_check",
        "question": "Quel est le total de conversions GA4 sur les 30 derniers jours avec vérification de provenance ?",
        "as_of": "2026-07-15",
        "surface": "daily_report",
        "difficulty": "easy",
        "tags": ["ga4", "conversions", "provenance"],
        "reference_queries": [
            {
                "role": "canonical_correct",
                "note": "GA4 30-day conversions for citation checking",
                "reference_sql": (
                    "SELECT SUM(value) AS total_conversions "
                    "FROM marts.fact_daily_kpi "
                    "WHERE project_id = 'default' AND connector = 'google-analytics' "
                    "AND metric = 'conversions' AND breakdown_dimension = 'country' "
                    "AND date BETWEEN '2026-06-16' AND '2026-07-15'"
                ),
                "fixture_name": "daily_report_provenance_pull_id_check.json",
            }
        ],
        "expected_citations": [
            {
                "source_system": "google-analytics",
                "source_field": "fact_daily_kpi",
                "pull_id_required": True,
            }
        ],
        "updated_at": "2026-07-20T00:00:00Z",
    },
    # Additional daily_report questions (to enrich set)
    {
        "id": "daily_report_ga4_users_30d",
        "question": "Combien d'utilisateurs actifs ai-je eu sur GA4 sur les 30 derniers jours ?",
        "as_of": "2026-07-15",
        "surface": "daily_report",
        "difficulty": "easy",
        "tags": ["ga4", "users"],
        "reference_queries": [
            {
                "role": "canonical_correct",
                "note": "Active users sum via country breakdown",
                "reference_sql": (
                    "SELECT SUM(value) AS total_active_users "
                    "FROM marts.fact_daily_kpi "
                    "WHERE project_id = 'default' AND connector = 'google-analytics' "
                    "AND metric = 'active_users' AND breakdown_dimension = 'country' "
                    "AND date BETWEEN '2026-06-16' AND '2026-07-15'"
                ),
                "fixture_name": "daily_report_ga4_users_30d.json",
            }
        ],
        "expected_citations": [
            {
                "source_system": "google-analytics",
                "source_field": "fact_daily_kpi",
                "pull_id_required": True,
            }
        ],
        "updated_at": "2026-07-20T00:00:00Z",
    },
    {
        "id": "daily_report_gsc_impressions_30d",
        "question": "Quel est le volume total d'impressions GSC sur les 30 derniers jours ?",
        "as_of": "2026-07-15",
        "surface": "daily_report",
        "difficulty": "easy",
        "tags": ["gsc", "impressions"],
        "reference_queries": [
            {
                "role": "canonical_correct",
                "note": "GSC impressions sum via page breakdown",
                "reference_sql": (
                    "SELECT SUM(value) AS total_impressions "
                    "FROM marts.fact_daily_kpi "
                    "WHERE project_id = 'default' AND connector = 'gsc' "
                    "AND metric = 'impressions' AND breakdown_dimension = 'page' "
                    "AND date BETWEEN '2026-06-16' AND '2026-07-15'"
                ),
                "fixture_name": "daily_report_gsc_impressions_30d.json",
            }
        ],
        "expected_citations": [
            {"source_system": "gsc", "source_field": "fact_daily_kpi", "pull_id_required": True}
        ],
        "updated_at": "2026-07-20T00:00:00Z",
    },
    # -----------------------------------------------------------------------
    # Group 2: expert_report (8 questions)
    # -----------------------------------------------------------------------
    {
        "id": "expert_report_gsc_position_movements",
        "question": "What is the weighted average position for GSC queries over the last 30 days?",
        "as_of": "2026-07-15",
        "surface": "expert_report",
        "difficulty": "medium",
        "tags": ["gsc", "average_position", "non_additive"],
        "reference_queries": [
            {
                "role": "canonical_correct",
                # FIX 2026-07-21 (Fix 3): the previous SQL returned raw per-date/per-dimension rows.
                # The question asks for "the weighted average position" — a single scalar.
                # Compute the impression-weighted mean over the full 30-day window by re-weighting
                # using semantic_avg_position.impressions_weight (which is SUM(impressions) per row).
                # This matches the grain_trap_note for grain_trap_gsc_average_position and is
                # consistent with how the naive (unweighted AVG) would differ.
                "note": (
                    "Impression-weighted overall GSC average position: "
                    "SUM(average_position * impressions_weight) / NULLIF(SUM(impressions_weight), 0) "
                    "— one scalar for the 30-day window, matching the question intent"
                ),
                "reference_sql": (
                    "SELECT "
                    "ROUND(SUM(average_position * impressions_weight) / NULLIF(SUM(impressions_weight), 0), 2) "
                    "AS overall_avg_position "
                    "FROM marts.semantic_avg_position "
                    "WHERE project_id = 'default' AND connector = 'gsc' "
                    "AND date BETWEEN '2026-06-16' AND '2026-07-15'"
                ),
                "fixture_name": "expert_report_gsc_position_movements.json",
            }
        ],
        "expected_citations": [
            {
                "source_system": "gsc",
                "source_field": "semantic_avg_position",
                "pull_id_required": True,
            }
        ],
        "updated_at": "2026-07-21T00:00:00Z",
    },
    {
        "id": "expert_report_meta_campaign_breakdown",
        "question": "What is the breakdown of Meta Ads spend and impressions by campaign over the last 30 days?",
        "as_of": "2026-07-15",
        "surface": "expert_report",
        "difficulty": "medium",
        "tags": ["meta-ads", "campaign", "breakdown"],
        "reference_queries": [
            {
                "role": "canonical_correct",
                "note": "Meta Ads spend and impressions by campaign",
                "reference_sql": (
                    "SELECT breakdown_value AS campaign_id, metric, SUM(value) AS total "
                    "FROM marts.fact_daily_kpi "
                    "WHERE project_id = 'default' AND connector = 'meta-ads' "
                    "AND breakdown_dimension = 'campaign_id' AND metric IN ('cost', 'impressions') "
                    "AND date BETWEEN '2026-06-16' AND '2026-07-15' "
                    "GROUP BY breakdown_value, metric ORDER BY breakdown_value, metric"
                ),
                "fixture_name": "expert_report_meta_campaign_breakdown.json",
            }
        ],
        "expected_citations": [
            {
                "source_system": "meta-ads",
                "source_field": "fact_daily_kpi",
                "pull_id_required": True,
            }
        ],
        "updated_at": "2026-07-20T00:00:00Z",
    },
    {
        "id": "expert_report_post_deploy_regressions",
        "question": "Are there any custom post-deploy regression events logged in GA4 for January 2026?",
        "as_of": "2026-07-15",
        "surface": "expert_report",
        "difficulty": "easy",
        "tags": ["ga4", "post_deploy_regression", "empty_state"],
        "reference_queries": [
            {
                "role": "canonical_correct",
                # FIX 2026-07-21 (honesty): COUNT(*) without GROUP BY always returns exactly one row
                # with value 0 when no rows match. This is real data — fixture is [{"event_count": 0}].
                # Drop expected_empty; expected_empty is reserved for strictly-0-row results.
                "note": "COUNT of absent metric returns one zero row — fixture is [{event_count: 0}]",
                "reference_sql": (
                    "SELECT COUNT(*) AS event_count "
                    "FROM marts.fact_daily_kpi "
                    "WHERE project_id = 'default' AND connector = 'google-analytics' "
                    "AND metric = 'post_deploy_regression' "
                    "AND date BETWEEN '2026-01-01' AND '2026-01-31'"
                ),
                "fixture_name": "expert_report_post_deploy_regressions.json",
            }
        ],
        "expected_citations": [],
        "updated_at": "2026-07-21T00:00:00Z",
    },
    {
        "id": "expert_report_as_of_override_replay",
        "question": "Replay GA4 daily sessions as of 2026-06-01 for the preceding 14 days.",
        "as_of": "2026-06-01",
        "surface": "expert_report",
        "difficulty": "medium",
        "tags": ["ga4", "as_of_replay"],
        "reference_queries": [
            {
                "role": "canonical_correct",
                "note": "Replayed 14-day window ending on June 1 2026",
                "reference_sql": (
                    "SELECT date, SUM(value) AS sessions "
                    "FROM marts.fact_daily_kpi "
                    "WHERE project_id = 'default' AND connector = 'google-analytics' "
                    "AND metric = 'sessions' AND breakdown_dimension = 'country' "
                    "AND date BETWEEN '2026-05-19' AND '2026-06-01' "
                    "GROUP BY date ORDER BY date"
                ),
                "fixture_name": "expert_report_as_of_override_replay.json",
            }
        ],
        "expected_citations": [
            {
                "source_system": "google-analytics",
                "source_field": "fact_daily_kpi",
                "pull_id_required": True,
            }
        ],
        "updated_at": "2026-07-20T00:00:00Z",
    },
    {
        "id": "expert_report_cross_connector_attribution",
        "question": "What is the cross-source conversion summary across GA4, Meta Ads, and GSC for the last 30 days?",
        "as_of": "2026-07-15",
        "surface": "expert_report",
        "difficulty": "medium",
        "tags": ["cross-connector", "conversions"],
        "reference_queries": [
            {
                "role": "canonical_correct",
                "note": "Total conversions by connector",
                "reference_sql": (
                    "SELECT connector, SUM(value) AS total_conversions "
                    "FROM marts.fact_daily_kpi "
                    "WHERE project_id = 'default' AND metric = 'conversions' "
                    "AND date BETWEEN '2026-06-16' AND '2026-07-15' "
                    "GROUP BY connector ORDER BY connector"
                ),
                "fixture_name": "expert_report_cross_connector_attribution.json",
            }
        ],
        "expected_citations": [
            {
                "source_system": "google-analytics",
                "source_field": "fact_daily_kpi",
                "pull_id_required": True,
            },
            {
                "source_system": "meta-ads",
                "source_field": "fact_daily_kpi",
                "pull_id_required": True,
            },
        ],
        "updated_at": "2026-07-20T00:00:00Z",
    },
    {
        "id": "expert_report_ga4_user_acquisition_channels",
        "question": "What is the breakdown of GA4 sessions by acquisition channel over the last 30 days?",
        "as_of": "2026-07-15",
        "surface": "expert_report",
        "difficulty": "medium",
        "tags": ["ga4", "acquisition", "channels"],
        "reference_queries": [
            {
                "role": "canonical_correct",
                "note": "GA4 sessions breakdown by session_source_medium",
                "reference_sql": (
                    "SELECT breakdown_value AS channel, SUM(value) AS sessions "
                    "FROM marts.fact_daily_kpi "
                    "WHERE project_id = 'default' AND connector = 'google-analytics' "
                    "AND metric = 'sessions' AND breakdown_dimension = 'session_source_medium' "
                    "AND date BETWEEN '2026-06-16' AND '2026-07-15' "
                    "GROUP BY breakdown_value ORDER BY sessions DESC"
                ),
                "fixture_name": "expert_report_ga4_user_acquisition_channels.json",
            }
        ],
        "expected_citations": [
            {
                "source_system": "google-analytics",
                "source_field": "fact_daily_kpi",
                "pull_id_required": True,
            }
        ],
        "updated_at": "2026-07-20T00:00:00Z",
    },
    {
        "id": "expert_report_meta_conversion_performance",
        # FIX 2026-07-20 (scoring-honesty): question mentioned "ROAS" but no ROAS reference
        # query was included (marts.semantic_roas does not exist in the seeds). Removed
        # "ROAS" from the question text to match the actual reference SQL (conversions only).
        "question": "What are the total Meta Ads conversions over the last 30 days?",
        "as_of": "2026-07-15",
        "surface": "expert_report",
        "difficulty": "medium",
        "tags": ["meta-ads", "conversions"],
        "reference_queries": [
            {
                "role": "canonical_correct",
                "note": "Meta Ads 30-day conversions total at campaign_id grain",
                "reference_sql": (
                    "SELECT SUM(value) AS total_conversions "
                    "FROM marts.fact_daily_kpi "
                    "WHERE project_id = 'default' AND connector = 'meta-ads' "
                    "AND metric = 'conversions' AND breakdown_dimension = 'campaign_id' "
                    "AND date BETWEEN '2026-06-16' AND '2026-07-15'"
                ),
                "fixture_name": "expert_report_meta_conversion_performance.json",
            }
        ],
        "expected_citations": [
            {
                "source_system": "meta-ads",
                "source_field": "fact_daily_kpi",
                "pull_id_required": True,
            }
        ],
        "updated_at": "2026-07-20T00:00:00Z",
    },
    {
        "id": "expert_report_gsc_top_pages_by_clicks",
        "question": "Which top 5 GSC pages generated the highest clicks over the last 30 days?",
        "as_of": "2026-07-15",
        "surface": "expert_report",
        "difficulty": "medium",
        "tags": ["gsc", "top_pages"],
        "reference_queries": [
            {
                "role": "canonical_correct",
                "note": "Top 5 GSC pages by total clicks",
                "reference_sql": (
                    "SELECT breakdown_value AS page, SUM(value) AS clicks "
                    "FROM marts.fact_daily_kpi "
                    "WHERE project_id = 'default' AND connector = 'gsc' "
                    "AND metric = 'clicks' AND breakdown_dimension = 'page' "
                    "AND date BETWEEN '2026-06-16' AND '2026-07-15' "
                    "GROUP BY breakdown_value ORDER BY clicks DESC LIMIT 5"
                ),
                "fixture_name": "expert_report_gsc_top_pages_by_clicks.json",
            }
        ],
        "expected_citations": [
            {"source_system": "gsc", "source_field": "fact_daily_kpi", "pull_id_required": True}
        ],
        "updated_at": "2026-07-20T00:00:00Z",
    },
    # Additional expert_report questions
    {
        "id": "expert_report_ga4_landing_pages",
        "question": "What are the top landing pages by sessions in GA4 over the last 30 days?",
        "as_of": "2026-07-15",
        "surface": "expert_report",
        "difficulty": "medium",
        "tags": ["ga4", "landing_pages"],
        "reference_queries": [
            {
                "role": "canonical_correct",
                # FIX 2026-07-20 (scoring-honesty): was breakdown_dimension='screen_page_views'
                # which is the page-view dimension, not the entry-page dimension. Seeds use
                # breakdown_dimension='landing_page' (sessions by landingPage — stg_ga4_landing_daily).
                "note": "GA4 landing page sessions top 5 — breakdown_dimension='landing_page' (entry pages)",
                "reference_sql": (
                    "SELECT breakdown_value AS landing_page, SUM(value) AS sessions "
                    "FROM marts.fact_daily_kpi "
                    "WHERE project_id = 'default' AND connector = 'google-analytics' "
                    "AND metric = 'sessions' AND breakdown_dimension = 'landing_page' "
                    "AND date BETWEEN '2026-06-16' AND '2026-07-15' "
                    "GROUP BY breakdown_value ORDER BY sessions DESC LIMIT 5"
                ),
                "fixture_name": "expert_report_ga4_landing_pages.json",
            }
        ],
        "expected_citations": [
            {
                "source_system": "google-analytics",
                "source_field": "fact_daily_kpi",
                "pull_id_required": True,
            }
        ],
        "updated_at": "2026-07-20T00:00:00Z",
    },
    {
        "id": "expert_report_meta_adset_performance",
        "question": "What is the cost and conversion performance by adset in Meta Ads for the last 30 days?",
        "as_of": "2026-07-15",
        "surface": "expert_report",
        "difficulty": "medium",
        "tags": ["meta-ads", "adset"],
        "reference_queries": [
            {
                "role": "canonical_correct",
                "note": "Meta Ads performance by adset_id",
                "reference_sql": (
                    "SELECT breakdown_value AS adset_id, metric, SUM(value) AS total "
                    "FROM marts.fact_daily_kpi "
                    "WHERE project_id = 'default' AND connector = 'meta-ads' "
                    "AND breakdown_dimension = 'adset_id' AND metric IN ('cost', 'conversions') "
                    "AND date BETWEEN '2026-06-16' AND '2026-07-15' "
                    "GROUP BY breakdown_value, metric ORDER BY breakdown_value, metric"
                ),
                "fixture_name": "expert_report_meta_adset_performance.json",
            }
        ],
        "expected_citations": [
            {
                "source_system": "meta-ads",
                "source_field": "fact_daily_kpi",
                "pull_id_required": True,
            }
        ],
        "updated_at": "2026-07-20T00:00:00Z",
    },
    # -----------------------------------------------------------------------
    # Group 3: card (8 questions) & card_catalog (2 questions)
    # -----------------------------------------------------------------------
    {
        "id": "card_kpi_summary_ga4_sessions",
        "question": "Affiche la carte Synthèse KPI des sessions GA4 sur les 30 derniers jours.",
        "as_of": "2026-07-15",
        "surface": "card",
        "difficulty": "easy",
        "tags": ["ga4", "card", "kpi_summary"],
        "procedure_ref": "get_card(template='kpi', project_id='default')",
        "reference_queries": [
            {
                "role": "canonical_correct",
                "note": "Card template KPI sessions query",
                "reference_sql": (
                    "SELECT SUM(value) AS total_sessions "
                    "FROM marts.fact_daily_kpi "
                    "WHERE project_id = 'default' AND connector = 'google-analytics' "
                    "AND metric = 'sessions' AND breakdown_dimension = 'country' "
                    "AND date BETWEEN '2026-06-16' AND '2026-07-15'"
                ),
                "fixture_name": "card_kpi_summary_ga4_sessions.json",
            }
        ],
        "expected_citations": [
            {
                "source_system": "google-analytics",
                "source_field": "fact_daily_kpi",
                "pull_id_required": True,
            }
        ],
        "updated_at": "2026-07-20T00:00:00Z",
    },
    {
        "id": "card_keywords_gsc_clicks_impressions",
        "question": "Affiche la carte Mots-clés avec les clics et impressions GSC sur les 30 derniers jours.",
        "as_of": "2026-07-15",
        "surface": "card",
        "difficulty": "medium",
        "tags": ["gsc", "card", "keywords"],
        "procedure_ref": "get_card(template='keywords', project_id='default')",
        "reference_queries": [
            {
                "role": "canonical_correct",
                "note": "Card template keywords query",
                "reference_sql": (
                    "SELECT metric, SUM(value) AS total "
                    "FROM marts.fact_daily_kpi "
                    "WHERE project_id = 'default' AND connector = 'gsc' "
                    "AND metric IN ('clicks', 'impressions') AND breakdown_dimension = 'page' "
                    "AND date BETWEEN '2026-06-16' AND '2026-07-15' "
                    "GROUP BY metric ORDER BY metric"
                ),
                "fixture_name": "card_keywords_gsc_clicks_impressions.json",
            }
        ],
        "expected_citations": [
            {"source_system": "gsc", "source_field": "fact_daily_kpi", "pull_id_required": True}
        ],
        "updated_at": "2026-07-20T00:00:00Z",
    },
    {
        "id": "card_conversions_cpa_cross_source",
        "question": "Affiche la carte Conversions & CPA global sur les 30 derniers jours.",
        "as_of": "2026-07-15",
        "surface": "card",
        "difficulty": "medium",
        "tags": ["cross-connector", "card", "conversions"],
        "procedure_ref": "get_card(template='conversions', project_id='default')",
        "reference_queries": [
            {
                "role": "canonical_correct",
                "note": "Card template conversions total",
                "reference_sql": (
                    "SELECT SUM(value) AS total_conversions "
                    "FROM marts.fact_daily_kpi "
                    "WHERE project_id = 'default' AND metric = 'conversions' "
                    "AND date BETWEEN '2026-06-16' AND '2026-07-15'"
                ),
                "fixture_name": "card_conversions_cpa_cross_source.json",
            }
        ],
        "expected_citations": [
            {
                "source_system": "google-analytics",
                "source_field": "fact_daily_kpi",
                "pull_id_required": True,
            },
            {
                "source_system": "meta-ads",
                "source_field": "fact_daily_kpi",
                "pull_id_required": True,
            },
        ],
        "updated_at": "2026-07-20T00:00:00Z",
    },
    {
        "id": "card_usertypes_new_returning",
        "question": "Affiche la carte Nouveaux vs fidèles pour GA4 sur les 30 derniers jours.",
        "as_of": "2026-07-15",
        "surface": "card",
        "difficulty": "medium",
        "tags": ["ga4", "card", "usertypes"],
        "procedure_ref": "get_card(template='usertypes', project_id='default')",
        "reference_queries": [
            {
                "role": "canonical_correct",
                "note": "Card template user_type sessions donut",
                "reference_sql": (
                    "SELECT breakdown_value AS user_type, SUM(value) AS sessions "
                    "FROM marts.fact_daily_kpi "
                    "WHERE project_id = 'default' AND connector = 'google-analytics' "
                    "AND metric = 'sessions' AND breakdown_dimension = 'user_type' "
                    "AND date BETWEEN '2026-06-16' AND '2026-07-15' "
                    "GROUP BY breakdown_value ORDER BY breakdown_value"
                ),
                "fixture_name": "card_usertypes_new_returning.json",
            }
        ],
        "expected_citations": [
            {
                "source_system": "google-analytics",
                "source_field": "fact_daily_kpi",
                "pull_id_required": True,
            }
        ],
        "updated_at": "2026-07-20T00:00:00Z",
    },
    {
        "id": "card_journey_entry_top_pages",
        "question": "Affiche la carte Parcours utilisateur avec les pages d'entrée GA4 sur les 30 derniers jours.",
        "as_of": "2026-07-15",
        "surface": "card",
        "difficulty": "medium",
        "tags": ["ga4", "card", "journey"],
        "procedure_ref": "get_card(template='journey', project_id='default')",
        "reference_queries": [
            {
                "role": "canonical_correct",
                # FIX 2026-07-20 (scoring-honesty): was metric='screen_page_views' AND
                # breakdown_dimension='screen_page_views' — there is no such self-referential
                # breakdown. The page-view profile stores metric='screen_page_views' with
                # breakdown_dimension='page' (stg_ga4_paths_daily). The journey / entry-page
                # card shows TOP pages by page views, so the correct query is:
                "note": "Card template journey — top pages by screen_page_views (breakdown_dimension='page')",
                "reference_sql": (
                    "SELECT breakdown_value AS page, SUM(value) AS views "
                    "FROM marts.fact_daily_kpi "
                    "WHERE project_id = 'default' AND connector = 'google-analytics' "
                    "AND metric = 'screen_page_views' AND breakdown_dimension = 'page' "
                    "AND date BETWEEN '2026-06-16' AND '2026-07-15' "
                    "GROUP BY breakdown_value ORDER BY views DESC LIMIT 10"
                ),
                "fixture_name": "card_journey_entry_top_pages.json",
            }
        ],
        "expected_citations": [
            {
                "source_system": "google-analytics",
                "source_field": "fact_daily_kpi",
                "pull_id_required": True,
            }
        ],
        "updated_at": "2026-07-20T00:00:00Z",
    },
    {
        "id": "card_attribution_session_source",
        "question": "Affiche la carte Attribution par source/medium GA4 sur les 30 derniers jours.",
        "as_of": "2026-07-15",
        "surface": "card",
        "difficulty": "medium",
        "tags": ["ga4", "card", "attribution"],
        "procedure_ref": "get_card(template='attribution', project_id='default')",
        "reference_queries": [
            {
                "role": "canonical_correct",
                "note": "Card template attribution by source/medium",
                "reference_sql": (
                    "SELECT breakdown_value AS source_medium, SUM(value) AS conversions "
                    "FROM marts.fact_daily_kpi "
                    "WHERE project_id = 'default' AND connector = 'google-analytics' "
                    "AND metric = 'conversions' AND breakdown_dimension = 'session_source_medium' "
                    "AND date BETWEEN '2026-06-16' AND '2026-07-15' "
                    "GROUP BY breakdown_value ORDER BY conversions DESC"
                ),
                "fixture_name": "card_attribution_session_source.json",
            }
        ],
        "expected_citations": [
            {
                "source_system": "google-analytics",
                "source_field": "fact_daily_kpi",
                "pull_id_required": True,
            }
        ],
        "updated_at": "2026-07-20T00:00:00Z",
    },
    {
        "id": "card_connectors_context",
        "question": "Affiche la carte contextuelle des connecteurs actifs sur le projet.",
        "as_of": "2026-07-15",
        "surface": "card",
        "difficulty": "easy",
        "tags": ["card", "connectors", "context"],
        "procedure_ref": "get_card(template='connectors', project_id='default')",
        "reference_queries": [
            {
                "role": "canonical_correct",
                "note": "Card template active connectors enumeration",
                "reference_sql": (
                    "SELECT DISTINCT connector "
                    "FROM marts.fact_daily_kpi "
                    "WHERE project_id = 'default' ORDER BY connector"
                ),
                "fixture_name": "card_connectors_context.json",
            }
        ],
        "expected_citations": [],
        "updated_at": "2026-07-20T00:00:00Z",
    },
    {
        "id": "card_dedup_context",
        "question": "Affiche la carte d'estimation de déduplication des conversions sur les 30 derniers jours.",
        "as_of": "2026-07-15",
        "surface": "card",
        "difficulty": "medium",
        "tags": ["card", "dedup", "cross_source"],
        "procedure_ref": "get_card(template='dedup', project_id='default')",
        "reference_queries": [
            {
                "role": "canonical_correct",
                # FIX 2026-07-20 (scoring-honesty): dedup_estimate is OPT-IN per project
                # (dim_project.verification_source_type). The 'default' project in
                # project_preferences.csv has verification_source_type='' (empty/NULL),
                # so dedup_estimate produces ZERO rows for project_id='default'. This is
                # correct by design — the card honestly shows "no dedup configured".
                # FIX 2026-07-21 (honesty / strict-expected_empty): SUM without GROUP BY
                # returns exactly one row with NULL values even when dedup_estimate has no
                # matching rows. Drop expected_empty; fixture is [{"claimed": null, "deduped": null}],
                # which honestly tells the agent "no dedup estimate available for this project".
                "note": "Card dedup estimate — default project not configured; fixture is [{claimed: null, deduped: null}]",
                "reference_sql": (
                    "SELECT SUM(claimed_conversions) AS claimed, SUM(deduplicated_contribution) AS deduped "
                    "FROM marts.dedup_estimate "
                    "WHERE project_id = 'default' AND date BETWEEN '2026-06-16' AND '2026-07-15'"
                ),
                "fixture_name": "card_dedup_context.json",
            }
        ],
        "expected_citations": [
            {
                "source_system": "google-analytics",
                "source_field": "cross_source_conversions",
                "pull_id_required": False,
            }
        ],
        "updated_at": "2026-07-21T00:00:00Z",
    },
    {
        "id": "card_catalog_list_all_templates",
        "question": "Liste tous les modèles de cartes disponibles dans le catalogue.",
        "as_of": "2026-07-15",
        "surface": "card_catalog",
        "difficulty": "easy",
        "tags": ["card_catalog", "list_templates"],
        "procedure_ref": "list_card_templates()",
        "reference_queries": [
            {
                "role": "canonical_correct",
                "note": "Total count of registered card templates",
                "reference_sql": (
                    "SELECT COUNT(*) AS total_templates FROM ("
                    "SELECT 'kpi' UNION SELECT 'keywords' UNION SELECT 'conversions' UNION "
                    "SELECT 'usertypes' UNION SELECT 'journey' UNION SELECT 'attribution' UNION "
                    "SELECT 'connectors' UNION SELECT 'dedup' UNION SELECT 'custom'"
                    ") AS t"
                ),
                "fixture_name": "card_catalog_list_all_templates.json",
            }
        ],
        "expected_citations": [],
        "updated_at": "2026-07-20T00:00:00Z",
    },
    {
        "id": "card_catalog_connectors_explicit_only",
        "question": "Vérifie que la carte connecteurs est de type contextuel et explicite.",
        "as_of": "2026-07-15",
        "surface": "card_catalog",
        "difficulty": "medium",
        "tags": ["card_catalog", "connectors"],
        "reference_queries": [
            {
                "role": "canonical_correct",
                "note": "Explicit connector verification query",
                "reference_sql": (
                    "SELECT DISTINCT connector "
                    "FROM marts.fact_daily_kpi "
                    "WHERE project_id = 'default' AND date BETWEEN '2026-07-01' AND '2026-07-15' "
                    "ORDER BY connector"
                ),
                "fixture_name": "card_catalog_connectors_explicit_only.json",
            }
        ],
        "expected_citations": [],
        "updated_at": "2026-07-20T00:00:00Z",
    },
    # Additional card questions
    {
        "id": "card_kpi_summary_meta_cost",
        "question": "Affiche la carte Synthèse KPI pour le coût Meta Ads sur les 30 derniers jours.",
        "as_of": "2026-07-15",
        "surface": "card",
        "difficulty": "easy",
        "tags": ["meta-ads", "card", "cost"],
        "reference_queries": [
            {
                "role": "canonical_correct",
                "note": "Meta Ads 30-day spend for KPI summary card",
                "reference_sql": (
                    "SELECT SUM(value) AS total_cost "
                    "FROM marts.fact_daily_kpi "
                    "WHERE project_id = 'default' AND connector = 'meta-ads' "
                    "AND metric = 'cost' AND breakdown_dimension = 'campaign_id' "
                    "AND date BETWEEN '2026-06-16' AND '2026-07-15'"
                ),
                "fixture_name": "card_kpi_summary_meta_cost.json",
            }
        ],
        "expected_citations": [
            {
                "source_system": "meta-ads",
                "source_field": "fact_daily_kpi",
                "pull_id_required": True,
            }
        ],
        "updated_at": "2026-07-20T00:00:00Z",
    },
    {
        "id": "card_attribution_campaign",
        "question": "Affiche la carte Attribution par campagne GA4 sur les 30 derniers jours.",
        "as_of": "2026-07-15",
        "surface": "card",
        "difficulty": "medium",
        "tags": ["ga4", "card", "campaign"],
        "reference_queries": [
            {
                "role": "canonical_correct",
                "note": "Attribution by session_campaign",
                "reference_sql": (
                    "SELECT breakdown_value AS campaign, SUM(value) AS conversions "
                    "FROM marts.fact_daily_kpi "
                    "WHERE project_id = 'default' AND connector = 'google-analytics' "
                    "AND metric = 'conversions' AND breakdown_dimension = 'session_campaign' "
                    "AND date BETWEEN '2026-06-16' AND '2026-07-15' "
                    "GROUP BY breakdown_value ORDER BY conversions DESC"
                ),
                "fixture_name": "card_attribution_campaign.json",
            }
        ],
        "expected_citations": [
            {
                "source_system": "google-analytics",
                "source_field": "fact_daily_kpi",
                "pull_id_required": True,
            }
        ],
        "updated_at": "2026-07-20T00:00:00Z",
    },
    # -----------------------------------------------------------------------
    # Group 4: dq (4 questions)
    # -----------------------------------------------------------------------
    {
        "id": "dq_stale_data_detection",
        "question": "Détecte si les données GA4 présentent un retard de fraîcheur sur les 3 derniers jours.",
        "as_of": "2026-07-15",
        "surface": "dq",
        "difficulty": "medium",
        "tags": ["dq", "freshness", "stale"],
        "reference_queries": [
            {
                "role": "canonical_correct",
                "note": "Recent row count check for freshness validation",
                "reference_sql": (
                    "SELECT COUNT(*) AS recent_rows "
                    "FROM marts.fact_daily_kpi "
                    "WHERE project_id = 'default' AND connector = 'google-analytics' "
                    "AND date BETWEEN '2026-07-13' AND '2026-07-15'"
                ),
                "fixture_name": "dq_stale_data_detection.json",
            }
        ],
        "expected_citations": [
            {
                "source_system": "google-analytics",
                "source_field": "fact_daily_kpi",
                "pull_id_required": True,
            }
        ],
        "updated_at": "2026-07-20T00:00:00Z",
    },
    {
        "id": "dq_connector_disabled_check",
        # FIX 2026-07-20 (scoring-honesty): the previous query checked connector='tiktok-ads',
        # which IS present in the seeds (2520 rows). The question said "disabled" but the
        # data contradicts that. Changed to connector='twitter-ads', which is genuinely absent
        # from all seeds. The question is now honest: "is twitter-ads absent?" — the fixture
        # [{row_count: 0}] confirms 0 rows, which is the correct DQ signal for an absent
        # connector.
        # FIX 2026-07-21 (honesty): COUNT(*) without GROUP BY always returns exactly one row
        # even when no rows match (row_count = 0). Drop expected_empty; fixture is [{"row_count": 0}].
        "question": "Détecte si le connecteur twitter-ads est absent du warehouse (aucune donnée active).",
        "as_of": "2026-07-15",
        "surface": "dq",
        "difficulty": "medium",
        "tags": ["dq", "disabled_connector"],
        "reference_queries": [
            {
                "role": "canonical_correct",
                "note": "twitter-ads row count — fixture is [{row_count: 0}] (connector never seeded/activated)",
                "reference_sql": (
                    "SELECT COUNT(*) AS row_count "
                    "FROM marts.fact_daily_kpi "
                    "WHERE project_id = 'default' AND connector = 'twitter-ads' "
                    "AND date BETWEEN '2026-06-16' AND '2026-07-15'"
                ),
                "fixture_name": "dq_connector_disabled_check.json",
            }
        ],
        "expected_citations": [],
        "updated_at": "2026-07-21T00:00:00Z",
    },
    {
        "id": "dq_ga4_anomaly_day_75",
        "question": "Vérifie les anomalies détectées dans les métriques quotidiennes sur les 90 derniers jours.",
        "as_of": "2026-07-15",
        "surface": "dq",
        "difficulty": "medium",
        "tags": ["dq", "anomaly", "day75"],
        "reference_queries": [
            {
                "role": "canonical_correct",
                "note": "Highest z_score anomaly query",
                "reference_sql": (
                    "SELECT date, connector, metric, observed_value, expected_value, zscore "
                    "FROM marts.anomalies_daily "
                    "WHERE project_id = 'default' AND connector = 'google-analytics' "
                    "AND metric = 'sessions' ORDER BY zscore DESC LIMIT 1"
                ),
                "fixture_name": "dq_ga4_anomaly_day_75.json",
            }
        ],
        "expected_citations": [
            {
                "source_system": "google-analytics",
                "source_field": "anomalies_daily",
                "pull_id_required": False,
            }
        ],
        "updated_at": "2026-07-20T00:00:00Z",
    },
    {
        "id": "dq_provenance_gap_null_pull_id",
        "question": "Détecte les enregistrements dans le warehouse n'ayant pas de pull_id associé.",
        "as_of": "2026-07-15",
        "surface": "dq",
        "difficulty": "medium",
        "tags": ["dq", "provenance", "null_pull_id"],
        "reference_queries": [
            {
                "role": "canonical_correct",
                # FIX 2026-07-20 (scoring-honesty): this query uses COUNT(*), which always
                # returns exactly ONE row (with null_pull_count = 0 when all rows have a pull_id).
                # The previous corpus incorrectly set expected_empty=true AND relied on the
                # COUNT=0->[] coercion heuristic in the builder/test to make the SHA match the
                # empty fixture. That heuristic is wrong: a COUNT query returning 0 is a REAL
                # result row, not an absent result set. Drop expected_empty — the fixture will
                # be [{"null_pull_count": 0}] (the honest DQ answer: zero gaps found).
                "note": "Null pull_id rows count — expected 0 gaps (all seed rows have pull_id); fixture is [{null_pull_count: 0}] not []",
                "reference_sql": (
                    "SELECT COUNT(*) AS null_pull_count "
                    "FROM marts.fact_daily_kpi "
                    "WHERE project_id = 'default' AND pull_id IS NULL"
                ),
                "fixture_name": "dq_provenance_gap_null_pull_id.json",
            }
        ],
        "expected_citations": [],
        "updated_at": "2026-07-20T00:00:00Z",
    },
    {
        "id": "dq_baseline_deviation_check",
        "question": "Examine la déviation de la métrique sessions GA4 par rapport à sa baseline sur les 30 derniers jours.",
        "as_of": "2026-07-15",
        "surface": "dq",
        "difficulty": "medium",
        "tags": ["dq", "baseline"],
        "reference_queries": [
            {
                "role": "canonical_correct",
                "note": "Baseline deviation evaluation query",
                "reference_sql": (
                    "SELECT connector, metric, rolling_mean, rolling_stddev "
                    "FROM marts.metric_baselines "
                    "WHERE project_id = 'default' AND connector = 'google-analytics' "
                    "AND metric = 'sessions'"
                ),
                "fixture_name": "dq_baseline_deviation_check.json",
            }
        ],
        "expected_citations": [],
        "updated_at": "2026-07-20T00:00:00Z",
    },
    # -----------------------------------------------------------------------
    # Group 5: as_of (4 questions)
    # -----------------------------------------------------------------------
    {
        "id": "as_of_ga4_sessions_historical_replay",
        "question": "Obtiens les sessions quotidiennes GA4 rejouées à la date as_of du 2026-06-30.",
        "as_of": "2026-06-30",
        "surface": "as_of",
        "difficulty": "medium",
        "tags": ["as_of", "ga4", "sessions"],
        "reference_queries": [
            {
                "role": "canonical_correct",
                "note": "GA4 daily sessions replayed as of June 30 2026",
                "reference_sql": (
                    "SELECT date, SUM(value) AS sessions "
                    "FROM marts.fact_daily_kpi "
                    "WHERE project_id = 'default' AND connector = 'google-analytics' "
                    "AND metric = 'sessions' AND breakdown_dimension = 'country' "
                    "AND date BETWEEN '2026-06-01' AND '2026-06-30' "
                    "GROUP BY date ORDER BY date"
                ),
                "fixture_name": "as_of_ga4_sessions_historical_replay.json",
            }
        ],
        "expected_citations": [
            {
                "source_system": "google-analytics",
                "source_field": "fact_daily_kpi",
                "pull_id_required": True,
            }
        ],
        "updated_at": "2026-07-20T00:00:00Z",
    },
    {
        "id": "as_of_meta_cost_replay",
        "question": "Obtiens le coût Meta Ads rejoué à la date as_of du 2026-06-15 sur les 14 jours précédents.",
        "as_of": "2026-06-15",
        "surface": "as_of",
        "difficulty": "medium",
        "tags": ["as_of", "meta-ads", "cost"],
        "reference_queries": [
            {
                "role": "canonical_correct",
                "note": "Meta cost replayed as of June 15 2026",
                "reference_sql": (
                    "SELECT date, SUM(value) AS cost "
                    "FROM marts.fact_daily_kpi "
                    "WHERE project_id = 'default' AND connector = 'meta-ads' "
                    "AND metric = 'cost' AND breakdown_dimension = 'campaign_id' "
                    "AND date BETWEEN '2026-06-02' AND '2026-06-15' "
                    "GROUP BY date ORDER BY date"
                ),
                "fixture_name": "as_of_meta_cost_replay.json",
            }
        ],
        "expected_citations": [
            {
                "source_system": "meta-ads",
                "source_field": "fact_daily_kpi",
                "pull_id_required": True,
            }
        ],
        "updated_at": "2026-07-20T00:00:00Z",
    },
    {
        "id": "as_of_gsc_clicks_historical",
        "question": "Obtiens les clics GSC enregistrés jusqu'à la date as_of du 2026-05-31.",
        "as_of": "2026-05-31",
        "surface": "as_of",
        "difficulty": "medium",
        "tags": ["as_of", "gsc", "clicks"],
        "reference_queries": [
            {
                "role": "canonical_correct",
                "note": "GSC clicks total for May 2026 as_of anchor",
                "reference_sql": (
                    "SELECT SUM(value) AS clicks "
                    "FROM marts.fact_daily_kpi "
                    "WHERE project_id = 'default' AND connector = 'gsc' "
                    "AND metric = 'clicks' AND breakdown_dimension = 'page' "
                    "AND date BETWEEN '2026-05-01' AND '2026-05-31'"
                ),
                "fixture_name": "as_of_gsc_clicks_historical.json",
            }
        ],
        "expected_citations": [
            {"source_system": "gsc", "source_field": "fact_daily_kpi", "pull_id_required": True}
        ],
        "updated_at": "2026-07-20T00:00:00Z",
    },
    {
        "id": "as_of_notebook_execution_window",
        "question": "Exécute le bloc de notebook pour les conversions GA4 ancré au 2026-06-30.",
        "as_of": "2026-06-30",
        "surface": "as_of",
        "difficulty": "medium",
        "tags": ["as_of", "notebook", "ga4"],
        "reference_queries": [
            {
                "role": "canonical_correct",
                "note": "Notebook execution window as of June 30 2026",
                "reference_sql": (
                    "SELECT date, SUM(value) AS conversions "
                    "FROM marts.fact_daily_kpi "
                    "WHERE project_id = 'default' AND connector = 'google-analytics' "
                    "AND metric = 'conversions' AND breakdown_dimension = 'country' "
                    "AND date BETWEEN '2026-06-16' AND '2026-06-30' "
                    "GROUP BY date ORDER BY date"
                ),
                "fixture_name": "as_of_notebook_execution_window.json",
            }
        ],
        "expected_citations": [
            {
                "source_system": "google-analytics",
                "source_field": "fact_daily_kpi",
                "pull_id_required": True,
            }
        ],
        "updated_at": "2026-07-20T00:00:00Z",
    },
    {
        "id": "as_of_gsc_impressions_replay",
        "question": "Affiche les impressions GSC rejouées jusqu'à la date as_of du 2026-06-30.",
        "as_of": "2026-06-30",
        "surface": "as_of",
        "difficulty": "medium",
        "tags": ["as_of", "gsc", "impressions"],
        "reference_queries": [
            {
                "role": "canonical_correct",
                "note": "GSC impressions total for June 2026 as_of anchor",
                "reference_sql": (
                    "SELECT SUM(value) AS impressions "
                    "FROM marts.fact_daily_kpi "
                    "WHERE project_id = 'default' AND connector = 'gsc' "
                    "AND metric = 'impressions' AND breakdown_dimension = 'page' "
                    "AND date BETWEEN '2026-06-01' AND '2026-06-30'"
                ),
                "fixture_name": "as_of_gsc_impressions_replay.json",
            }
        ],
        "expected_citations": [
            {"source_system": "gsc", "source_field": "fact_daily_kpi", "pull_id_required": True}
        ],
        "updated_at": "2026-07-20T00:00:00Z",
    },
    # -----------------------------------------------------------------------
    # Group 6: grain_trap (8 questions -- 16 query entries total)
    # -----------------------------------------------------------------------
    {
        "id": "grain_trap_ga4_sessions_multi_breakdown",
        "question": "Quel est le total de sessions Google Analytics sur juin 2026 ?",
        "as_of": "2026-07-15",
        "surface": "grain_trap",
        "difficulty": "hard",
        "tags": ["ga4", "sessions", "grain_trap", "double_count"],
        # FIX 2026-07-20 (scoring-honesty / magnitude): the note said "triples" but fact_daily_kpi
        # contains many GA4 parallel breakdown series for sessions (country, country>device,
        # device_category, user_type, landing_page, session_source_medium, session_campaign,
        # first_user_source_medium). The naive sum over ALL of them multiplies the true total by
        # the number of breakdown dimensions that carry sessions, not just 3x.
        "grain_trap_note": "summing sessions across all breakdown_dimension values in fact_daily_kpi inflates the total by N× (where N = number of parallel session series: country, country>device, device_category, user_type, session_source_medium, session_campaign, etc.); pinning to one canonical breakdown_dimension='country' is required",
        "reference_queries": [
            {
                "role": "naive_wrong",
                "note": "Naive sum across all breakdown_dimensions — produces 3x the correct total",
                "reference_sql": (
                    "SELECT SUM(value) AS total_sessions_WRONG "
                    "FROM marts.fact_daily_kpi "
                    "WHERE project_id = 'default' AND connector = 'google-analytics' "
                    "AND metric = 'sessions' AND date BETWEEN '2026-06-01' AND '2026-06-30'"
                ),
                "fixture_name": "grain_trap_ga4_sessions_multi_breakdown__naive_wrong.json",
            },
            {
                "role": "canonical_correct",
                "note": "Pin to country breakdown — the lexicographic MIN for GA4",
                "reference_sql": (
                    "SELECT SUM(value) AS total_sessions "
                    "FROM marts.fact_daily_kpi "
                    "WHERE project_id = 'default' AND connector = 'google-analytics' "
                    "AND metric = 'sessions' AND breakdown_dimension = 'country' "
                    "AND date BETWEEN '2026-06-01' AND '2026-06-30'"
                ),
                "fixture_name": "grain_trap_ga4_sessions_multi_breakdown__canonical_correct.json",
            },
        ],
        "expected_citations": [],
        "updated_at": "2026-07-20T00:00:00Z",
    },
    {
        "id": "grain_trap_ga4_conversions_attribution",
        "question": "Combien de conversions GA4 ai-je eu en tout en juin 2026 ?",
        "as_of": "2026-07-15",
        "surface": "grain_trap",
        "difficulty": "hard",
        "tags": ["ga4", "conversions", "grain_trap", "attribution"],
        # FIX 2026-07-20 (scoring-honesty / magnitude): note said "triples" via 3 attribution dims,
        # but fact_daily_kpi also has country, country>device, device_category, user_type for
        # conversions, so the naive sum is ≥5× (not 3×). Updated to the honest "N×" framing.
        "grain_trap_note": "summing conversions across all breakdown_dimension values inflates the count by N× because each attribution series (country, device_category, user_type, session_source_medium, session_campaign, first_user_source_medium) independently totals the same day's conversions; pinning to breakdown_dimension='country' yields the canonical single total",
        "reference_queries": [
            {
                "role": "naive_wrong",
                "note": "Naive sum across all attribution breakdowns",
                "reference_sql": (
                    "SELECT SUM(value) AS total_conversions_WRONG "
                    "FROM marts.fact_daily_kpi "
                    "WHERE project_id = 'default' AND connector = 'google-analytics' "
                    "AND metric = 'conversions' AND date BETWEEN '2026-06-01' AND '2026-06-30'"
                ),
                "fixture_name": "grain_trap_ga4_conversions_attribution__naive_wrong.json",
            },
            {
                "role": "canonical_correct",
                "note": "Pin to country breakdown — canonical series for GA4",
                "reference_sql": (
                    "SELECT SUM(value) AS total_conversions "
                    "FROM marts.fact_daily_kpi "
                    "WHERE project_id = 'default' AND connector = 'google-analytics' "
                    "AND metric = 'conversions' AND breakdown_dimension = 'country' "
                    "AND date BETWEEN '2026-06-01' AND '2026-06-30'"
                ),
                "fixture_name": "grain_trap_ga4_conversions_attribution__canonical_correct.json",
            },
        ],
        "expected_citations": [],
        "updated_at": "2026-07-20T00:00:00Z",
    },
    {
        "id": "grain_trap_gsc_average_position",
        "question": "What is the overall average position for GSC in June 2026?",
        "as_of": "2026-07-15",
        "surface": "grain_trap",
        "difficulty": "hard",
        "tags": ["gsc", "average_position", "grain_trap", "non_additive"],
        # FIX 2026-07-21 (Fix 1 + Fix 3):
        # - naive_wrong: SUM without GROUP BY on fact_daily_kpi returns one null row
        #   (average_position is NOT in fact_daily_kpi at all). Drop expected_empty; fixture is
        #   [{"avg_position_WRONG": null}], which is the real result and fully distinct from canonical.
        # - canonical_correct: previously returned raw per-date/per-dimension rows from
        #   semantic_avg_position. Change to compute the single scalar impression-weighted average
        #   over the full window: SUM(average_position*impressions_weight)/NULLIF(SUM(impressions_weight),0).
        #   This matches the grain_trap_note ("impression-weighted mean") and gives one clear number.
        "grain_trap_note": (
            "SUM(value) on metric='average_position' in fact_daily_kpi returns null because "
            "average_position is non-additive and NOT stored in fact_daily_kpi at all; the correct "
            "approach queries marts.semantic_avg_position and computes the impression-weighted mean "
            "SUM(average_position * impressions_weight) / NULLIF(SUM(impressions_weight), 0) over "
            "the window — a single scalar"
        ),
        "reference_queries": [
            {
                "role": "naive_wrong",
                # FIX 2026-07-21: SUM aggregate always returns one row; drop expected_empty.
                # Fixture will be [{"avg_position_WRONG": null}] — honest representation of the
                # invalid query result.
                "note": "Naive SUM against fact_daily_kpi returns one null row — avg_position not in this mart",
                "reference_sql": (
                    "SELECT SUM(value) AS avg_position_WRONG "
                    "FROM marts.fact_daily_kpi "
                    "WHERE project_id = 'default' AND connector = 'gsc' "
                    "AND metric = 'average_position' AND date BETWEEN '2026-06-01' AND '2026-06-30'"
                ),
                "fixture_name": "grain_trap_gsc_average_position__naive_wrong.json",
            },
            {
                "role": "canonical_correct",
                # FIX 2026-07-21 (Fix 3): change from raw per-date rows to a single scalar
                # impression-weighted average. semantic_avg_position already computes the per-row
                # weighted position (average_position = SUM(pos*imp)/NULLIF(SUM(imp),0) per dim).
                # To aggregate across all dimensions and dates we re-weight using impressions_weight.
                "note": (
                    "Impression-weighted overall average position: "
                    "SUM(average_position * impressions_weight) / NULLIF(SUM(impressions_weight), 0) "
                    "— one scalar for June 2026, matching the grain_trap_note"
                ),
                "reference_sql": (
                    "SELECT "
                    "ROUND(SUM(average_position * impressions_weight) / NULLIF(SUM(impressions_weight), 0), 2) "
                    "AS overall_avg_position "
                    "FROM marts.semantic_avg_position "
                    "WHERE project_id = 'default' AND connector = 'gsc' "
                    "AND date BETWEEN '2026-06-01' AND '2026-06-30'"
                ),
                "fixture_name": "grain_trap_gsc_average_position__canonical_correct.json",
            },
        ],
        "expected_citations": [],
        "updated_at": "2026-07-21T00:00:00Z",
    },
    {
        "id": "grain_trap_meta_ctr_ratio",
        "question": "What is the CTR for Meta Ads over June 2026?",
        "as_of": "2026-07-15",
        "surface": "grain_trap",
        "difficulty": "hard",
        "tags": ["meta-ads", "ctr", "grain_trap", "ratio"],
        # FIX 2026-07-21 (Fix 1): naive_wrong uses AVG aggregate which always returns one row.
        # When metric='ctr' is not in fact_daily_kpi, result is [{"ctr_WRONG": null}] — real data.
        # Drop expected_empty from naive_wrong; it is distinct from canonical (which has real rows).
        "grain_trap_note": (
            "AVG(value) on metric='ctr' in fact_daily_kpi returns null because ctr is a derived "
            "ratio not stored in fact_daily_kpi; the correct approach queries marts.semantic_ctr "
            "which computes ratio-of-sums SUM(clicks)/NULLIF(SUM(impressions),0) and weights days "
            "correctly by impression volume"
        ),
        "reference_queries": [
            {
                "role": "naive_wrong",
                # FIX 2026-07-21: AVG aggregate always returns one row; drop expected_empty.
                # Fixture will be [{"ctr_WRONG": null}] — honest null result for the invalid query.
                "note": "Naive AVG against fact_daily_kpi returns one null row — ctr not stored in this mart",
                "reference_sql": (
                    "SELECT AVG(value) AS ctr_WRONG "
                    "FROM marts.fact_daily_kpi "
                    "WHERE project_id = 'default' AND connector = 'meta-ads' "
                    "AND metric = 'ctr' AND date BETWEEN '2026-06-01' AND '2026-06-30'"
                ),
                "fixture_name": "grain_trap_meta_ctr_ratio__naive_wrong.json",
            },
            {
                "role": "canonical_correct",
                "note": "Query dedicated semantic_ctr mart for ratio-of-sums (real rows with actual CTR values)",
                "reference_sql": (
                    "SELECT date, breakdown_dimension, breakdown_value, ctr "
                    "FROM marts.semantic_ctr "
                    "WHERE project_id = 'default' AND connector = 'meta-ads' "
                    "AND date BETWEEN '2026-06-01' AND '2026-06-30' ORDER BY date, breakdown_dimension, breakdown_value"
                ),
                "fixture_name": "grain_trap_meta_ctr_ratio__canonical_correct.json",
            },
        ],
        "expected_citations": [],
        "updated_at": "2026-07-21T00:00:00Z",
    },
    {
        "id": "grain_trap_meta_data_level_double_count",
        "question": "Quel est le total de clics Meta Ads sur juin 2026 ?",
        "as_of": "2026-07-15",
        "surface": "grain_trap",
        "difficulty": "hard",
        "tags": ["meta-ads", "clicks", "grain_trap", "data_level"],
        "grain_trap_note": "summing across ALL breakdown_dimension rows (campaign + adset + ad) triples the total because each data_level is a parallel series of the full day",
        "reference_queries": [
            {
                "role": "naive_wrong",
                "note": "Naive sum across all data_levels — triples total",
                "reference_sql": (
                    "SELECT SUM(value) AS clicks_WRONG "
                    "FROM marts.fact_daily_kpi "
                    "WHERE project_id = 'default' AND connector = 'meta-ads' "
                    "AND metric = 'clicks' AND date BETWEEN '2026-06-01' AND '2026-06-30'"
                ),
                "fixture_name": "grain_trap_meta_data_level_double_count__naive_wrong.json",
            },
            {
                "role": "canonical_correct",
                "note": "Pin breakdown_dimension = 'campaign_id'",
                "reference_sql": (
                    "SELECT SUM(value) AS total_clicks "
                    "FROM marts.fact_daily_kpi "
                    "WHERE project_id = 'default' AND connector = 'meta-ads' "
                    "AND metric = 'clicks' AND breakdown_dimension = 'campaign_id' "
                    "AND date BETWEEN '2026-06-01' AND '2026-06-30'"
                ),
                "fixture_name": "grain_trap_meta_data_level_double_count__canonical_correct.json",
            },
        ],
        "expected_citations": [],
        "updated_at": "2026-07-20T00:00:00Z",
    },
    {
        "id": "grain_trap_cross_source_conversions_dedup",
        "question": "Combien de conversions ai-je eu au total (toutes sources) sur juin 2026 ?",
        "as_of": "2026-07-15",
        "surface": "grain_trap",
        "difficulty": "hard",
        "tags": ["cross-connector", "conversions", "grain_trap", "dedup"],
        # FIX 2026-07-21 (Fix 2): the previous canonical queried marts.dedup_estimate, which is
        # OPT-IN per project. The 'default' project has verification_source_type='' (unconfigured),
        # so dedup_estimate returns 0 rows and the canonical SUM returned [{"total_deduped_conversions": null}]
        # — a degenerate null result that teaches nothing (no discriminative power beyond
        # "dedup not set up"). Replace with marts.cross_source_conversions, which implements Rule P
        # deduplication by picking the highest-priority winning connector per (project_id, date)
        # and summing only that connector's conversions on one canonical breakdown dimension.
        # This always yields a substantive non-null total on the seed data, making the trap real:
        # naive (SUM across all connectors on all breakdowns) >> canonical (one connector, one dim).
        "grain_trap_note": (
            "SUM(value) WHERE metric='conversions' across ALL connectors in fact_daily_kpi "
            "counts each conversion attributed by EVERY régie that claimed it — a user converting "
            "via GA4 and Meta Ads is counted twice; the canonical approach queries "
            "marts.cross_source_conversions, which applies Rule P priority deduplication to pick "
            "one winning connector per (project_id, date) and sum only that connector's conversions "
            "on its canonical breakdown dimension"
        ),
        "reference_queries": [
            {
                "role": "naive_wrong",
                "note": "Naive sum across ALL connectors and ALL breakdown dims — over-counts cross-régie attributions by N× the number of parallel series",
                "reference_sql": (
                    "SELECT SUM(value) AS conversions_WRONG "
                    "FROM marts.fact_daily_kpi "
                    "WHERE project_id = 'default' AND metric = 'conversions' "
                    "AND date BETWEEN '2026-06-01' AND '2026-06-30'"
                ),
                "fixture_name": "grain_trap_cross_source_conversions_dedup__naive_wrong.json",
            },
            {
                "role": "canonical_correct",
                # FIX 2026-07-21: use cross_source_conversions (Rule P dedup mart) instead of
                # dedup_estimate (opt-in per project). cross_source_conversions always returns
                # real rows for the seed data — the winning connector per date contributes its
                # canonical conversions_total. The trap discriminates: naive >> canonical.
                "note": (
                    "Rule P dedup via marts.cross_source_conversions: "
                    "one winning connector per (project_id, date), summed over June 2026 — "
                    "a real substantive number, strictly less than the naive total"
                ),
                "reference_sql": (
                    "SELECT SUM(conversions_total) AS total_deduped_conversions "
                    "FROM marts.cross_source_conversions "
                    "WHERE project_id = 'default' "
                    "AND date BETWEEN '2026-06-01' AND '2026-06-30'"
                ),
                "fixture_name": "grain_trap_cross_source_conversions_dedup__canonical_correct.json",
            },
        ],
        "expected_citations": [],
        "updated_at": "2026-07-21T00:00:00Z",
    },
    {
        "id": "grain_trap_klaviyo_campaign_vs_flow",
        # FIX 2026-07-20 (scoring-honesty / GT-7): the previous formulation used
        # metric='attributed_conversions' for both naive and canonical. Because the
        # Klaviyo seed's flow_id rows round to 0 attributed_conversions (very low click
        # rate -> int(clicks * 0.01..0.04) = 0 for most flow rows), both queries
        # returned the same value (34 == 34). The trap was not demonstrable on the seeds.
        #
        # Decision: reformulate using metric='sends', which is non-zero for EVERY
        # campaign AND every flow row in the seed. The trap logic is identical:
        #   naive_wrong = campaign-only sends (misses all flow-originated email sends)
        #   canonical_correct = campaign + flow sends (the complete Klaviyo channel total)
        # The two series are MUTUALLY EXCLUSIVE (a Klaviyo row has either campaign_id or
        # flow_id, never both), so the canonical sum is strictly greater than the naive.
        # This is a real production hazard: filtering on campaign_id alone silently
        # under-counts because automated flow sends are a distinct dimension.
        "question": "What is the total number of Klaviyo email sends (campaigns + flows) in June 2026?",
        "as_of": "2026-07-15",
        "surface": "grain_trap",
        "difficulty": "hard",
        "tags": ["klaviyo", "sends", "grain_trap"],
        "grain_trap_note": "filtering breakdown_dimension='campaign_id' alone excludes all flow-originated email sends; campaign and flow series are mutually exclusive in Klaviyo and must both be included for the complete channel send total",
        "reference_queries": [
            {
                "role": "naive_wrong",
                "note": "Naive filter campaign_id alone — omits all flow sends",
                "reference_sql": (
                    "SELECT SUM(value) AS sends_WRONG "
                    "FROM marts.fact_daily_kpi "
                    "WHERE project_id = 'default' AND connector = 'klaviyo' "
                    "AND metric = 'sends' AND breakdown_dimension = 'campaign_id' "
                    "AND date BETWEEN '2026-06-01' AND '2026-06-30'"
                ),
                "fixture_name": "grain_trap_klaviyo_campaign_vs_flow__naive_wrong.json",
            },
            {
                "role": "canonical_correct",
                "note": "Union campaign_id and flow_id breakdowns for total Klaviyo sends",
                "reference_sql": (
                    "SELECT SUM(value) AS total_sends "
                    "FROM marts.fact_daily_kpi "
                    "WHERE project_id = 'default' AND connector = 'klaviyo' "
                    "AND metric = 'sends' AND breakdown_dimension IN ('campaign_id', 'flow_id') "
                    "AND date BETWEEN '2026-06-01' AND '2026-06-30'"
                ),
                "fixture_name": "grain_trap_klaviyo_campaign_vs_flow__canonical_correct.json",
            },
        ],
        "expected_citations": [],
        "updated_at": "2026-07-20T00:00:00Z",
    },
    {
        "id": "grain_trap_linkedin_campaign_vs_group",
        "question": "What is the total LinkedIn cost for June 2026?",
        "as_of": "2026-07-15",
        "surface": "grain_trap",
        "difficulty": "hard",
        "tags": ["linkedin-ads", "cost", "grain_trap"],
        "grain_trap_note": "summing across both campaign_id and campaign_group_id double-counts because campaign_groups are roll-ups of campaigns; one grain must be picked",
        "reference_queries": [
            {
                "role": "naive_wrong",
                "note": "Naive sum across campaign and campaign_group series",
                "reference_sql": (
                    "SELECT SUM(value) AS cost_WRONG "
                    "FROM marts.fact_daily_kpi "
                    "WHERE project_id = 'default' AND connector = 'linkedin-ads' "
                    "AND metric = 'cost' AND date BETWEEN '2026-06-01' AND '2026-06-30'"
                ),
                "fixture_name": "grain_trap_linkedin_campaign_vs_group__naive_wrong.json",
            },
            {
                "role": "canonical_correct",
                "note": "Pin breakdown_dimension = 'campaign_id'",
                "reference_sql": (
                    "SELECT SUM(value) AS total_cost "
                    "FROM marts.fact_daily_kpi "
                    "WHERE project_id = 'default' AND connector = 'linkedin-ads' "
                    "AND metric = 'cost' AND breakdown_dimension = 'campaign_id' "
                    "AND date BETWEEN '2026-06-01' AND '2026-06-30'"
                ),
                "fixture_name": "grain_trap_linkedin_campaign_vs_group__canonical_correct.json",
            },
        ],
        "expected_citations": [],
        "updated_at": "2026-07-20T00:00:00Z",
    },
]


# ---------------------------------------------------------------------------
# Story 14.2 — deterministic tool_invocation derivation (mechanical addendum).
#
# We resolve a question to a concrete MCP tool call ONLY when its canonical_correct
# reference SQL is a plain fact_daily_kpi aggregation the get_daily_report seam can
# reproduce EXACTLY: single connector, single metric, a PINNED breakdown_dimension
# (so the runner re-aggregates on the same grain the reference used — a naive sum
# over all breakdowns would over-count, and MUST score red). Everything else
# (semantic marts, cross-connector dedup, anomalies/baselines, card catalog, card
# composition, unpinned/multi-metric queries) is left UNRESOLVED (tool_invocation
# omitted) so the runner marks it tool_replay: skipped — never a false green.
#
# result_selector vocabulary (see schema.md):
#   kind = "fact_sum"          -> one scalar: SUM(value) over rows matching
#                                 (connector, metric, breakdown_dimension); compared
#                                 to the reference's single aggregate cell.
#   kind = "fact_sum_by_date"  -> list of {date, value}: SUM(value) grouped by date
#                                 (ordered by date) over the same filter; compared to
#                                 the reference's per-day rows.
# Both carry connector/metric/breakdown_dimension + a `round` (null | int) mirroring
# the reference ROUND(.,N). Whole-number aggregates use round=null (exact equality).
# ---------------------------------------------------------------------------

# get_daily_report as_of is imposed for surfaces that replay history. The corpus
# `as_of` is a DATE (YYYY-MM-DD); the tool wants an ISO-8601 datetime — pin to the
# end-of-day instant so the replay window includes the whole as_of date.
_ASOF_SURFACES = {"as_of", "expert_report"}

_MART_RE = re.compile(r"\bFROM\s+marts\.([a-z_]+)", re.IGNORECASE)
_EQ_RE_TMPL = r"\b{col}\s*=\s*'([^']*)'"
_BETWEEN_RE = re.compile(
    r"\bdate\s+BETWEEN\s+'(\d{4}-\d{2}-\d{2})'\s+AND\s+'(\d{4}-\d{2}-\d{2})'",
    re.IGNORECASE,
)
_DATE_EQ_RE = re.compile(r"\bdate\s*=\s*'(\d{4}-\d{2}-\d{2})'", re.IGNORECASE)
_ROUND_RE = re.compile(r"\bROUND\s*\([^,]+,\s*(\d+)\s*\)", re.IGNORECASE)


def _single_eq(sql: str, col: str) -> str | None:
    """Return the value of a single ``col = 'value'`` predicate, or None.

    Returns None when the column is absent OR when it appears MORE THAN ONCE (an
    ``IN (...)`` or an OR-of-connectors makes the grain ambiguous — do NOT resolve).
    """
    matches = re.findall(_EQ_RE_TMPL.format(col=re.escape(col)), sql, re.IGNORECASE)
    if len(matches) == 1:
        return matches[0]
    return None


def _connector_to_source_system(connector: str) -> str:
    """The corpus already stores canonical source_system names in `connector`."""
    return connector


def derive_tool_invocation(question: dict[str, Any]) -> dict[str, Any] | None:
    """Derive a deterministic get_daily_report tool_invocation, or None (skip).

    Rules (all must hold on the canonical_correct query):
      * table is marts.fact_daily_kpi
      * exactly one connector = '...'
      * exactly one metric = '...'
      * a single pinned breakdown_dimension = '...'
      * project_id = 'default' (the only seeded project)
      * a date window (BETWEEN or a single date =) is present
      * the SELECT is a bare SUM(value)  -> fact_sum
        OR  date, SUM(value) ... GROUP BY date -> fact_sum_by_date
    Any deviation -> return None (the runner will mark it tool_replay: skipped).
    """
    refs = question.get("reference_queries", [])
    canon = next((r for r in refs if r.get("role") == "canonical_correct"), None)
    # daily_report questions carry a single unlabeled canonical query.
    if canon is None and len(refs) == 1 and refs[0].get("role") in (None, "canonical_correct"):
        canon = refs[0]
    if canon is None:
        return None

    sql = " ".join(str(canon.get("reference_sql", "")).split())

    mart = _MART_RE.search(sql)
    if not mart or mart.group(1).lower() != "fact_daily_kpi":
        return None

    project = _single_eq(sql, "project_id")
    if project != "default":
        return None

    connector = _single_eq(sql, "connector")
    metric = _single_eq(sql, "metric")
    breakdown = _single_eq(sql, "breakdown_dimension")
    if not connector or not metric or not breakdown:
        return None

    # Reject anything with an IN(...) or OR (ambiguous grain / multi-series).
    upper = sql.upper()
    if " IN (" in upper or " OR " in upper:
        return None

    # Date window.
    bt = _BETWEEN_RE.search(sql)
    if bt:
        start, end = bt.group(1), bt.group(2)
    else:
        de = _DATE_EQ_RE.search(sql)
        if not de:
            return None
        start = end = de.group(1)

    # Shape: bare SUM(value) vs date-grouped SUM(value).
    has_group_by_date = "GROUP BY DATE" in upper
    # The SELECT must be SUM(value) (optionally with a leading `date,`). Reject any
    # other aggregate/expression (AVG, breakdown_value SELECTs, LIMIT, etc.).
    select_body = sql[len("SELECT "):upper.index(" FROM")].strip() if upper.startswith("SELECT ") else ""
    select_norm = select_body.upper()
    if has_group_by_date:
        if not select_norm.startswith("DATE,") or "SUM(VALUE)" not in select_norm:
            return None
        if "LIMIT" in upper:
            return None
        kind = "fact_sum_by_date"
    else:
        # bare aggregate: exactly SUM(value) AS <alias>, no GROUP BY, no LIMIT.
        if not select_norm.startswith("SUM(VALUE)"):
            return None
        if "GROUP BY" in upper or "LIMIT" in upper:
            return None
        kind = "fact_sum"

    round_m = _ROUND_RE.search(sql)
    round_to = int(round_m.group(1)) if round_m else None

    args: dict[str, Any] = {
        "project_id": project,
        "connectors": [connector],
        "date_range": {"start": start, "end": end},
    }
    # Impose as_of (end-of-day) for replay surfaces so the window is deterministic
    # and never wall-clock relative.
    if question.get("surface") in _ASOF_SURFACES:
        args["as_of"] = f"{question['as_of']}T23:59:59Z"

    selector = {
        "kind": kind,
        "connector": connector,
        "metric": metric,
        "breakdown_dimension": breakdown,
        "round": round_to,
    }
    return {"tool": "get_daily_report", "args": args, "result_selector": selector}


def execute_query(conn: duckdb.DuckDBPyConnection, sql: str) -> list[dict[str, Any]]:
    """Execute SQL against DuckDB and return list of row dicts."""
    rel = conn.execute(sql)
    cols = [desc[0] for desc in rel.description] if rel.description else []
    rows = rel.fetchall()
    result = []
    for r in rows:
        row_dict = {}
        for col, val in zip(cols, r):
            if hasattr(val, "isoformat"):
                val = val.isoformat()
            row_dict[col] = val
        result.append(row_dict)
    return result


def build_corpus_and_fixtures() -> None:
    if not DUCKDB_PATH.is_file():
        raise FileNotFoundError(f"Seeded DuckDB file not found at {DUCKDB_PATH}")

    conn = duckdb.connect(str(DUCKDB_PATH))
    # Determinism: pin single-threaded, insertion-ordered aggregation so SUM() over
    # floats is byte-reproducible (parallel reduction reorders adds -> IEEE-754 noise
    # in trailing bits, e.g. 27056.899999999998 vs ...987). Required for the corpus to
    # be a stable ground truth (fixture_sha256) and for byte-stable regen (Story 14.2).
    conn.execute("SET threads TO 1")
    conn.execute("SET preserve_insertion_order TO true")
    conn.execute("CREATE SCHEMA IF NOT EXISTS marts")
    marts_tables = conn.execute("SHOW TABLES FROM main_marts").fetchall()
    for (tbl,) in marts_tables:
        conn.execute(f"CREATE VIEW IF NOT EXISTS marts.{tbl} AS SELECT * FROM main_marts.{tbl}")

    final_questions = []

    for q in RAW_QUESTIONS:
        q_copy = dict(q)
        queries = q_copy.pop("reference_queries")
        processed_queries = []

        for q_entry in queries:
            sql = q_entry["reference_sql"]
            fixture_name = q_entry.pop("fixture_name")
            fixture_path = FIXTURES_DIR / fixture_name

            # Execute SQL
            rows = execute_query(conn, sql)
            # FIX 2026-07-20 (scoring-honesty): removed the COUNT=0->[] coercion heuristic.
            # A single row with all-zero/null values is a REAL result (COUNT(*) returning 0
            # means "no rows matched" — that is information the agent must surface, not silently
            # suppress). The only valid empty-result mechanism is expected_empty=true on queries
            # that return 0 rows by design (absent connector, out-of-range date, unknown project).

            expected_empty = q_entry.get("expected_empty", False)
            if expected_empty:
                # FIX 2026-07-21 (Fix 1 — strict expected_empty): expected_empty means EXACTLY
                # 0 rows. The previous is_null_aggregate soft catch-all (accepting a single
                # all-NULL/all-zero aggregate row) was a disguised version of the removed
                # COUNT=0->[] heuristic — it masked COUNT/SUM queries that legitimately return
                # one row. Those queries now have expected_empty removed and their real single-row
                # fixture pinned. Only queries with GROUP BY / non-aggregate that genuinely return
                # 0 rows on the seed data should carry expected_empty=true.
                if len(rows) != 0:
                    raise ValueError(
                        f"Query for {q['id']} marked expected_empty=true but returned "
                        f"{len(rows)} row(s): {rows}. "
                        "expected_empty is strictly 0 rows. If the query is an aggregate "
                        "(COUNT/SUM without GROUP BY) that returns one null/zero row, "
                        "drop expected_empty and let the fixture capture the real result."
                    )

            # Serialize fixture JSON (canonical form)
            fixture_bytes = json.dumps(rows, sort_keys=True, indent=2, ensure_ascii=False).encode(
                "utf-8"
            )
            fixture_path.write_bytes(fixture_bytes)

            sha256 = hashlib.sha256(fixture_bytes).hexdigest()

            processed_entry = {
                "reference_sql": sql,
                "expected_result_fixture": f"fixtures/{fixture_name}",
                "fixture_sha256": sha256,
            }
            if "role" in q_entry:
                processed_entry["role"] = q_entry["role"]
            if "note" in q_entry:
                processed_entry["note"] = q_entry["note"]
            if expected_empty:
                processed_entry["expected_empty"] = True

            processed_queries.append(processed_entry)

        q_copy["reference_queries"] = processed_queries

        # Story 14.2: mechanical tool_invocation addendum. Derived from the ORIGINAL
        # question metadata (surface, as_of) + the canonical reference SQL. Only emitted
        # (as the LAST field) when the question deterministically resolves to a
        # get_daily_report call; otherwise omitted so the runner marks it skipped.
        tool_invocation = derive_tool_invocation(q)
        if tool_invocation is not None:
            q_copy["tool_invocation"] = tool_invocation

        final_questions.append(q_copy)

    corpus_data = {
        "schema_version": "1",
        "as_of_anchor": "2026-07-15",
        "seeds_commit": "a6b3c60",
        "dbt_build_commit": "a6b3c60",
        "created_by": "human+agent",
        "questions": final_questions,
    }

    # Format header comment
    header = "# AD-17: this record is TEST CODE, not knowledge. It judges the context layer; it is not part of it.\n"
    corpus_yaml_str = header + yaml.dump(corpus_data, sort_keys=False, allow_unicode=True)

    corpus_path = EVALS_DIR / "corpus.yaml"
    corpus_path.write_text(corpus_yaml_str, encoding="utf-8")
    print(f"Successfully generated {len(final_questions)} questions in {corpus_path}")
    print(f"Committed {len(list(FIXTURES_DIR.glob('*.json')))} fixture files under {FIXTURES_DIR}")


def inject_tool_invocations_only() -> None:
    """Story 14.2 addendum, DB-FREE: add ``tool_invocation`` to the EXISTING corpus
    without re-executing any reference SQL.

    ``derive_tool_invocation`` reads only question metadata (surface / as_of) and the
    committed reference SQL -- never the warehouse -- so the golden tool resolution is
    independent of the seed. Regenerating fixtures from a re-seeded DuckDB re-introduces
    IEEE-754 trailing-bit noise on float SUMs (parallel/insertion-order dependent), which
    would silently rewrite the pinned ground-truth fixture_sha256. This mode keeps the
    committed fixtures/sha byte-identical and only appends ``tool_invocation`` (idempotent:
    an existing block is recomputed, so re-running is a no-op). Use the full
    ``build_corpus_and_fixtures`` only to (re)pin fixtures against an authoritative seed.
    """
    corpus_path = EVALS_DIR / "corpus.yaml"
    raw = corpus_path.read_text(encoding="utf-8")
    header_lines = [ln for ln in raw.splitlines(keepends=True) if ln.startswith("#")]
    header = "".join(header_lines) if header_lines else (
        "# AD-17: this record is TEST CODE, not knowledge. It judges the context layer;"
        " it is not part of it.\n"
    )
    corpus_data = yaml.safe_load(raw)

    injected = 0
    for question in corpus_data.get("questions", []):
        question.pop("tool_invocation", None)  # idempotent: recompute from scratch
        tool_invocation = derive_tool_invocation(question)
        if tool_invocation is not None:
            question["tool_invocation"] = tool_invocation  # last key (append)
            injected += 1

    corpus_yaml_str = header + yaml.dump(corpus_data, sort_keys=False, allow_unicode=True)
    corpus_path.write_text(corpus_yaml_str, encoding="utf-8")
    total = len(corpus_data.get("questions", []))
    print(f"Injected tool_invocation into {injected}/{total} questions ({corpus_path}).")
    print(f"{total - injected} question(s) left without tool_invocation -> runner marks skipped.")


if __name__ == "__main__":
    import sys

    if "--addendum-only" in sys.argv:
        inject_tool_invocations_only()
    else:
        build_corpus_and_fixtures()
