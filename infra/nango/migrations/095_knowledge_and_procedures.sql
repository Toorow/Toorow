-- 095: Context-workspace read surfaces — knowledge base + metric procedures.
--
-- Two project-scoped tables the Context workspace reads:
--   app.knowledge_entries  -- governed business definitions/policies (Knowledge)
--   app.metric_procedures  -- per-metric calculation + reconciliation method (Procedures)
-- Both are shared context stored in the platform DB (not git), read by the
-- assistant during analysis. Idempotent seed for the default project.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS app.knowledge_entries (
    id          TEXT        PRIMARY KEY DEFAULT gen_random_uuid()::text,
    project_id  TEXT        NOT NULL,
    title       TEXT        NOT NULL,
    topic       TEXT        NOT NULL,
    body        TEXT,
    author      TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_knowledge_entries_project_title UNIQUE (project_id, title)
);
CREATE INDEX IF NOT EXISTS idx_knowledge_entries_project
    ON app.knowledge_entries (project_id);

CREATE TABLE IF NOT EXISTS app.metric_procedures (
    id          TEXT        PRIMARY KEY DEFAULT gen_random_uuid()::text,
    project_id  TEXT        NOT NULL,
    metric      TEXT        NOT NULL,
    -- reconciliation method (the five marts methods):
    -- sum_then_divide | priority_source | dedup_union | weighted_blend | manual_override
    method      TEXT        NOT NULL,
    description TEXT,
    owner       TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    CONSTRAINT uq_metric_procedures_project_metric UNIQUE (project_id, metric)
);
CREATE INDEX IF NOT EXISTS idx_metric_procedures_project
    ON app.metric_procedures (project_id);

-- Seed — default project (idempotent on the unique keys).
INSERT INTO app.knowledge_entries (project_id, title, topic, body, author) VALUES
    ('default', 'ROAS calculation & deduplication policy', 'Attribution',
     'Return on ad spend is computed on deduplicated conversions: a conversion '
     'attributed by more than one platform is counted once, against the source '
     'with the last non-direct click. Spend and revenue are summed at the '
     'canonical grain, then divided once.', 'Winston (Architect)'),
    ('default', 'Canonical revenue vs commerce gross sales', 'Semantics',
     'Canonical revenue is net of refunds and taxes and is sourced from the '
     'finance feed. Commerce gross sales (platform-reported) is kept for context '
     'only and never summed into canonical revenue.', 'Mary (Analyst)'),
    ('default', 'Post-click attribution reconciliation procedure', 'Procedures',
     'When GA4 and the ad platforms disagree on conversions, GA4 is authoritative '
     'for the canonical count; platform numbers are surfaced as comparison, never '
     'added.', 'Paige (Tech Writer)')
ON CONFLICT (project_id, title) DO NOTHING;

INSERT INTO app.metric_procedures (project_id, metric, method, description, owner) VALUES
    ('default', 'ROAS', 'sum_then_divide',
     'Aggregate revenue and cost across every reporting source first, then divide '
     'the totals. Never average per-source ratios — the blended figure is computed '
     'once at the canonical grain.', 'Winston (Architect)'),
    ('default', 'Conversions', 'priority_source',
     'Take GA4 as the authoritative source of truth. Platform-reported conversions '
     'are kept for context but excluded from the canonical count to avoid '
     'post-click double counting.', 'Mary (Analyst)'),
    ('default', 'Impressions', 'dedup_union',
     'Union impressions across sources, then collapse rows that share the same '
     'campaign, creative, and date so a placement reported by two connectors is '
     'counted a single time.', 'Paige (Tech Writer)'),
    ('default', 'Blended CPA', 'weighted_blend',
     'Combine per-source cost per acquisition weighted by each source spend share. '
     'Sources below the minimum-spend threshold are folded into the dominant '
     'source rather than skewing the blend.', 'Winston (Architect)'),
    ('default', 'Media revenue', 'manual_override',
     'Use the finance-reconciled monthly figure supplied by the client in place of '
     'any connector reading. The override carries an effective date and supersedes '
     'automated collection for closed periods.', 'Mary (Analyst)')
ON CONFLICT (project_id, metric) DO NOTHING;
