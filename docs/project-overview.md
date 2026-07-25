# Project Overview

## Executive summary

toorow is a multi-part Python/TypeScript/data monorepo implementing a modular MCP
reporting platform. Its architecture combines a FastMCP plugin kernel, a
governed Postgres control plane, DuckDB/BigQuery analytical storage, dbt semantic
models and single-file React MCP Apps.

Its product target is a sovereign control plane for trustworthy agentic
analytics: flexible ingestion, governed semantics, shared business context,
quality/provenance before narrative and measurable agent reliability. See
`product-direction.md` for the reconciled ambition and `working-method.md` for
the delivery process.

## Parts

| Part | Type | Root | Primary stack |
|---|---|---|---|
| MCP platform | Backend | `server/` | Python 3.12, FastMCP, Starlette, DuckDB, psycopg |
| Presentation | Web/UI workspace | `ui/` | React 19, TypeScript, Vite 8, MUI, pnpm 9 |
| Semantic data | Data | `dbt/` | dbt-core 1.11, dbt-duckdb, dbt-bigquery, SQL |
| Platform infrastructure | Infrastructure | `infra/` | Docker Compose, PostgreSQL, Nango, Langfuse, Terraform/GCP |
| Presence website | Private web | `web/` | Astro 7, React, Tailwind, Sanity client |
| Content studio | Private web | `studio/` | Sanity Studio 6, React |

## Primary documentation

- `project-context.md`: durable AI/developer memory.
- `repository-boundary.md`: public/private publication decision.
- `source-tree-analysis.md`: annotated repository structure.
- `integration-architecture.md`: runtime and build-time relationships.
- `development-guide.md`: setup, test and operational references.
- Root `README.md`: application consumer onboarding.
