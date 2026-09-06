-- Story 41.6 -- activate the standard waterfall family only after the generic
-- waterfall_v1 Result shape and compatibility predicate exist.

ALTER TABLE app.visualization_spec_versions
    DROP CONSTRAINT IF EXISTS ck_visualization_spec_versions_family;
ALTER TABLE app.visualization_spec_versions
    ADD CONSTRAINT ck_visualization_spec_versions_family CHECK (
        family IN (
            'table', 'kpi', 'line', 'area', 'bar', 'stacked_bar', 'scatter',
            'waterfall'
        )
    );

ALTER TABLE app.renderer_runtime_builds
    DROP CONSTRAINT IF EXISTS ck_renderer_runtime_builds_family;
ALTER TABLE app.renderer_runtime_builds
    ADD CONSTRAINT ck_renderer_runtime_builds_family CHECK (
        family IN (
            'table', 'kpi', 'line', 'area', 'bar', 'stacked_bar', 'scatter',
            'waterfall'
        )
    );

-- Build identity shipped by the shared ECharts runtime. The runtime content
-- hash is regenerated from ui/cards/shell/src/viz before this migration is
-- catalogued; Render replay compares both exact pins.
INSERT INTO app.renderer_runtime_builds (
    id,
    runtime_build,
    family,
    renderer_id,
    theme_version,
    formatter_version,
    responsive_profiles,
    git_sha
) VALUES (
    'waterfall/toorow-echarts-waterfall@1.0.0',
    '@toorow/card-shell/viz@0.1.0+2e4aba0febbe',
    'waterfall',
    'toorow-echarts-waterfall',
    'viz-theme@1',
    'viz-formatters@1',
    ARRAY['console', 'mcp-inline', 'mcp-fullscreen', 'share']::TEXT[],
    '98d787925c35f95bd9d780ea8ffce73543c6018b'
)
ON CONFLICT (id) DO NOTHING;
