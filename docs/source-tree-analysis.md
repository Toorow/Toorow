# Source Tree Analysis

```text
connector/
|-- server/
|   |-- core/                 # Source-agnostic MCP kernel and admin API
|   |-- modules/              # Auto-discovered source plugins
|   `-- tests/                # Unit, integration, conformance and isolation gates
|-- ui/
|   |-- admin/                # Standalone administration console
|   |-- shell/                # Shared MCP App shell
|   |-- cards/                # Reusable business card applications/primitives
|   |-- widgets/              # Source-specific and sample widgets
|   |-- tokens/               # DTCG tokens -> generated theme
|   `-- scripts/              # Single-file bundle policy checks
|-- dbt/
|   |-- models/marts/         # Canonical facts and semantic views
|   |-- macros/               # Shared SQL behavior
|   |-- seeds/                # Canonical dimensions/preferences
|   |-- tests/                # Generic and singular data-quality tests
|   `-- profiles/             # Safe example only; real profile ignored
|-- infra/
|   |-- nango/                # Local Nango + Postgres and app migrations
|   |-- langfuse/             # Optional observability stack
|   |-- airbyte/              # Extraction setup guidance
|   |-- docker/mcp-server/    # Production server image
|   `-- terraform/            # GCP infrastructure, apply human-gated
|-- .github/workflows/        # CI and human-gated deployment
|-- scripts/                  # Application/project utilities
|-- distribution/             # Public application projection policy
|-- web/                      # PRIVATE marketing site
|-- studio/                   # PRIVATE Sanity Studio
|-- docs/, doc/               # PRIVATE docs and project knowledge
|-- _bmad/, _bmad-output/     # PRIVATE planning/workflow artifacts
|-- reviews/, _screenshots/   # PRIVATE review and visual evidence
|-- pyproject.toml            # uv workspace root
|-- uv.lock                   # Reproducible Python dependency lock
|-- Makefile                  # Cross-project developer shortcuts
`-- README.md                 # Public application installation/setup
```

## Important coupling points

- The root uv workspace joins `server/` and `dbt/`.
- `dbt/dbt_project.yml` includes module-owned staging directories from
  `server/modules/*/dbt` and centralizes source-agnostic marts.
- The server resolves prebuilt widget/card HTML from `ui/**/dist` and serves the
  built admin application.
- Admin UI requests are handled by the Starlette routes in
  `server/core/admin_api.py`.
- PostgreSQL schema changes are ordered SQL files in
  `infra/nango/migrations/` and are exercised by CI isolation tests.
- `.github/workflows/ci.yml` is the executable definition of merge readiness.
