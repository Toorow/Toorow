#!/usr/bin/env python3
"""seed_context_platform_defaults.py — Seed platform knowledge entries and skills.

Ensures default platform knowledge topics and agent procedures exist in Postgres.
"""
from __future__ import annotations

import logging

from core import context_store, db

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("seed_context_platform_defaults")

DEFAULT_KNOWLEDGE_TOPICS = [
    {
        "title": "Governed Marketing Context & Reporting Standard",
        "body_md": """# Governed Marketing Context & Reporting Standard

## Overview
This platform unifies multi-channel marketing reporting across GA4, Google Search Console, Meta Ads, TikTok Ads, LinkedIn Ads, Shopify, Stripe, Klaviyo, and 40+ connectors.

## Core Rules
1. **Canonical Metrics**: All financial values are stored in canonical micros and normalized per source before metric calculation.
2. **Non-Additive Measures**: Ratios (e.g. CTR, Conversion Rate) and averages (e.g. Average Position) live exclusively in semantic views and are NEVER summed in analytical marts.
3. **Timezone Provenance**: Daily connectors preserve `report_timezone` per row to prevent day-boundary distortion during reporting.
4. **Traceable Business Paths**: Every report view and metric maps to a versioned evidence path linked to a governed business domain.
""",
        "owner": "governance@toorow.io",
    },
    {
        "title": "MDM Business Domains & Taxonomy Governance",
        "body_md": """# MDM Business Domains & Taxonomy Governance

## Structure
- **Domains**: Top-level business areas (Engineering, Finance, Legal, Marketing, Product, Sales).
- **Descriptive Layers**: Sub-classifications (e.g., product line, geographical market, customer tier).
- **Traceable Routes**: Explicit bindings linking schema docs, datastreams, and semantic views to business context.

## Maintenance
- Changes to domain taxonomy write an immutable version record to `toorow_meta.schema_migrations` and audit ledgers.
- Archived nodes remain readable for historical evidence resolution.
""",
        "owner": "taxonomy@toorow.io",
    },
]

DEFAULT_SKILLS = [
    {
        "name": "platform-operating-procedure",
        "description": "Platform operating procedure: how to connect a source, map it, activate it, drive its cadence, land data, read evidence.",
        "body_md": """# Platform Operating Procedure

## Purpose
Standard operating workflow for connector onboarding, data stream governance, and evidence resolution.

## Steps
1. **Connect Source**: Authorize OAuth broker (Nango or Google Direct) and select project scope.
2. **Configure Datastream**: Set pull schedule, sync mode, and schema mapping.
3. **Land & Harmonize**: Execute dbt staging models (`stg_*`) and seed canonical semantic marts.
4. **Govern & Audit**: Bind datastreams to MDM business domains and review traceable paths.
5. **Serve via MCP**: Expose compact textual summaries to LLM context while streaming full payloads to single-file React widgets.
""",
        "owner": "ops@toorow.io",
    },
    {
        "name": "marketing-analytics-reporting-procedure",
        "description": "Operational procedure for multi-touch attribution analysis, funnel performance audit, and cross-channel campaign evaluation.",
        "body_md": """# Marketing Analytics & Reporting Procedure

## Purpose
Guidelines for executing cross-channel performance reviews and attribution modeling.

## Steps
1. **Collect Touchpoints**: Extract event streams across ad platforms (Meta, TikTok, Google Ads) and web analytics (GA4).
2. **Deduplicate & Align**: Reconcile click-throughs and conversions against canonical order IDs in Stripe / Shopify.
3. **Evaluate Attribution Models**: Compute First-Touch, Last-Touch, and Linear multi-touch distribution weights.
4. **Generate Report Widget**: Emit `structuredContent` payload matching `@toorow/widget-sample` single-file HTML bundle.
""",
        "owner": "analytics@toorow.io",
    },
]

def seed_defaults(project_id: str = "p1") -> None:
    logger.info("Seeding platform knowledge & skills for project_id=%s...", project_id)
    with db.get_connection() as conn:
        # Seed Knowledge Topics
        existing_topics = context_store.list_topics(conn, project_id=project_id)
        existing_topic_titles = {t.get("title") for t in existing_topics}
        for item in DEFAULT_KNOWLEDGE_TOPICS:
            if item["title"] not in existing_topic_titles:
                t = context_store.create_topic(
                    conn,
                    project_id=project_id,
                    title=item["title"],
                    body_md=item["body_md"],
                    owner=item["owner"],
                    created_by="system:seed",
                )
                logger.info("Seeded knowledge topic: %s (id=%s)", item["title"], t.get("id"))
            else:
                logger.info("Knowledge topic already exists: %s", item["title"])

        # Seed Skills / Procedures
        existing_procs = context_store.list_procedures(conn, project_id=project_id)
        existing_proc_names = {p.get("name") for p in existing_procs}
        for item in DEFAULT_SKILLS:
            if item["name"] not in existing_proc_names:
                fm_yaml = f"name: {item['name']}\ndescription: {item['description']}\n"
                p = context_store.create_procedure(
                    conn,
                    project_id=project_id,
                    frontmatter_yaml=fm_yaml,
                    body_md=item["body_md"],
                    owner=item["owner"],
                    created_by="system:seed",
                )
                logger.info("Seeded procedure skill: %s (id=%s)", item["name"], p.get("id"))
            else:
                logger.info("Procedure skill already exists: %s", item["name"])

        # THE SIX BUSINESS DOMAINS ARE NOT SEEDED FROM HERE, AND THEY NEVER WERE.
        #
        # This block called `business_taxonomy.create_domain(..., created_by=...)`,
        # a keyword that writer has never had -- and without the `slug` and
        # `actor` it requires. Every run of it raised `TypeError` before writing
        # anything, so the loop that called it was dead the day it was typed.
        #
        # What actually seeds them is migration 130: `seed_business_domains_for_org`
        # runs on the `app.organizations` insert trigger, for every organization,
        # with the six template rows and their version rows. That is where a
        # delivered catalogue belongs -- "un catalogue livre avec le produit ne se
        # lit pas dans une table", and it certainly is not re-typed in a script.
        #
        # It is removed rather than repaired for a second reason, ratified on
        # 2026-08-25: `create_domain` refuses now. A script that mints a business
        # identity outside the Master Data authority is exactly the second writer
        # the acceptance schedule closed.
        conn.commit()
    logger.info("Seeding finished successfully.")

if __name__ == "__main__":
    seed_defaults("default")
    seed_defaults("p1")
