# toorow

**A modular, stack-agnostic MCP platform for unified marketing reporting.**

toorow discovers connector modules, extracts and governs source data, harmonizes
metrics through dbt, and serves compact MCP responses alongside self-contained
React widgets and an administration console. It runs on your own stack — you
bring your own accounts and credentials, and nothing is hardcoded to the
maintainers' project.

It answers a simple, expensive problem: pulling GA4, Search Console, Meta Ads and
a dozen other sources into a single, governed, semantically-consistent view that
an AI assistant can query cheaply — without burning tokens on megabytes of raw
JSON, and without leaking one tenant's credentials into another's context.

---

## What it does

- **Multi-tenant identity & OAuth brokering.** Each user links their own
  advertising and analytics accounts. Tokens are isolated per tenant, encrypted
  at rest, and refreshed automatically for scheduled and interactive jobs. Every
  tool call is bound to the human who authorized it (on-behalf-of), and recorded
  in an audit log.
- **A growing set of connectors.** The current module set covers Google
  Analytics 4, Google Search Console, Meta Ads, TikTok Ads, LinkedIn Ads,
  Shopify, Stripe, Klaviyo, GitHub, and a generic connector — auto-discovered
  from `server/modules/`, each shipping its own dbt staging models.
- **Warehouse + semantic marts.** Raw source data lands in a BigQuery warehouse
  and is harmonized into canonical semantic marts through dbt, so concepts like
  "cost" or "sessions" mean the same thing across every source. A business-context
  table lets analyses account for events like sales, launches or tracking
  incidents.
- **Compact MCP responses + rich widgets.** The MCP server returns a light
  textual summary to the LLM and streams the full dataset straight to a
  self-contained React widget via `structuredContent` — keeping large payloads
  out of the model's context window. Widgets are compiled single-file for client
  sandbox / CSP compliance.
- **Governance & observability.** Connection health, sync performance and
  agent traffic are monitored; a governance and audit layer and an admin console
  keep the platform operable at team scale.

toorow follows an explicitly modular, "best-brick" philosophy: standard runtimes
(FastMCP, dbt, Nango) over bespoke glue, so each layer can evolve independently.

## Architecture at a glance

| Path | Purpose |
|---|---|
| `server/core/` | FastMCP server, auth, queue, governance, reports and admin API |
| `server/modules/` | Auto-discovered connector modules and their dbt staging models |
| `ui/` | pnpm workspace for tokens, shared shells, cards, widgets and admin UI |
| `dbt/` | Canonical semantic marts, seeds, macros and data-quality tests |
| `infra/` | Local Nango/Postgres/Langfuse services, Docker and Terraform |
| `.github/workflows/` | CI gates and human-gated Cloud Run deployment |

## Quick start: server and local UI

You need Git, Python 3.12, [uv](https://docs.astral.sh/uv/), Node.js 22+, and
pnpm 9.x (`corepack enable` is the simplest route). Docker Desktop / Engine with
Compose is needed only for the full local platform.

Clone the repository and install the locked dependencies:

```bash
git clone <application-repository-url> toorow
cd toorow
uv sync --all-packages --frozen
pnpm -C ui install --frozen-lockfile
pnpm -C ui build:tokens
```

The Python process reads environment variables directly; it does not
automatically load the root `.env`. For a minimal local server, the defaults are
enough (authentication, workers, scheduling and alerts are disabled unless
explicitly enabled):

```bash
uv run --package toorow-server python -m core.main
```

Set an alternate port with `PORT`:

```bash
# bash / zsh
PORT=8000 uv run --package toorow-server python -m core.main
```

```powershell
# PowerShell
$env:PORT = "8000"
uv run --package toorow-server python -m core.main
```

The MCP endpoint is `http://localhost:8000/mcp`. The server also mounts the
built administration console when `ui/admin/dist/` exists:

```bash
pnpm -C ui --filter @toorow/admin build
```

For local UI development with hot reload:

```bash
pnpm -C ui --filter @toorow/admin dev
```

## Full local platform

The full stack (Nango + both PostgreSQL databases) runs in Docker. Copy the
local infrastructure template and replace every placeholder secret:

```bash
cp infra/nango/.env.example infra/nango/.env
```

Generate the required 256-bit Nango encryption key:

```bash
python -c "import base64,secrets; print(base64.b64encode(secrets.token_bytes(32)).decode())"
```

Set the generated value as `NANGO_ENCRYPTION_KEY` in `infra/nango/.env`, then
start Nango and both databases:

```bash
docker compose --env-file infra/nango/.env -f infra/nango/docker-compose.yml up -d
```

Apply the application migrations in numeric order.

```bash
# bash / zsh
for migration in $(find infra/nango/migrations -name '*.sql' | sort -V); do
  docker compose --env-file infra/nango/.env -f infra/nango/docker-compose.yml exec -T platform-db \
    psql -U connector -d connector -v ON_ERROR_STOP=1 < "$migration"
done
```

```powershell
# PowerShell
Get-ChildItem infra/nango/migrations/*.sql | Sort-Object Name | ForEach-Object {
  Get-Content -Raw $_.FullName | docker compose --env-file infra/nango/.env -f infra/nango/docker-compose.yml exec -T platform-db psql -U connector -d connector -v ON_ERROR_STOP=1
}
```

The local services use these host ports:

| Service | Port |
|---|---:|
| MCP server | 8000 |
| Nango UI/API | 3003 |
| Application PostgreSQL | 5432 |
| Nango PostgreSQL | 5433 |

See `infra/nango/README.md` for Google OAuth registration and integration
testing. Never run `docker compose down -v` unless deleting all local platform
and Nango data is intentional.

### Local data pipeline

The seeded DuckDB/dbt loop works without cloud credentials:

```bash
uv run python server/modules/google-analytics/seeds/run_local_loop.py
```

For a manual dbt workflow, create `dbt/profiles/profiles.yml` from the committed
example. The real profile is ignored because it may contain credentials.

## Deploy on your own stack

toorow is stack-agnostic: you provision your own accounts and put their
identifiers in a local `.env`. To run your own instance you need:

- **A Supabase project (or any Postgres 15+)** — the platform database for
  connections, governance, audit log and the job queue (`PLATFORM_DB_URL`).
- **A Google Cloud account + project** — the BigQuery warehouse (raw landings +
  semantic marts), Cloud Run hosting, and the OAuth client for Google sources
  (GA4 / Search Console / Google Ads / Sheets).
- **A Nango account (Cloud or self-hosted)** — the OAuth broker for the
  non-Google connectors (Meta Ads, TikTok, LinkedIn, Shopify, Stripe, Klaviyo…),
  which stores refresh tokens and refreshes access tokens on your behalf.
- Optionally, **Langfuse** for tracing MCP tool calls.

You only need accounts for the sources you actually plan to connect. The full,
step-by-step guide — every account to create, how to wire it, and a pre-launch
security checklist — is in
[`infra/docs/self-hosting.md`](infra/docs/self-hosting.md).

> **Auth mode matters.** The server's default auth is fine for local QA only.
> Any networked or production deploy must use the OAuth/JWT path
> (`TOOROW_AUTH_MODE=oauth`). Never expose a `static`/`disabled` instance to
> the public internet.

## Build and test

```bash
# Python lint and test suite
uv run ruff check server
uv run pytest server/tests -q

# Design tokens and common UI checks
pnpm -C ui build:tokens
pnpm -C ui --filter @toorow/shell typecheck
pnpm -C ui --filter @toorow/admin test

# Single-file sample widget and external-resource gate
pnpm -C ui --filter @toorow/widget-sample build
node ui/scripts/bundle-check.mjs ui/widgets/sample/dist/index.html
```

Additional package-specific tests are defined in each `ui/**/package.json`. CI
also validates dbt, module conformance, tenant isolation and Terraform. See
`.github/workflows/ci.yml` and `CONTRIBUTING.md` for the complete matrix.

## Configuration and secrets

`.env.example` documents the application settings, while service-specific
templates live next to their infrastructure or UI package. Templates are safe to
commit; real `.env` files, dbt profiles, Terraform state/variables, tenant keys,
local databases and service-account files must stay untracked.

Production authentication must use the configured OAuth/JWT path. The default
disabled authentication mode is for local development only.

## Project policy & license

- Architecture invariants and contribution rules are in `CONTRIBUTING.md`.
- Infrastructure deployment and destructive actions remain human-gated.
- Licensed under the [MIT License](LICENSE).

This repository is a curated, **application-only** public projection of a private
development monorepo. The shareable application (`server/`, `ui/`, `dbt/`,
`infra/`, CI) lives here; the marketing website, Sanity Studio, editorial
documentation, internal plans and design material stay private and are not part
of the export. That is why some tooling and content you might expect are absent —
they are not part of the published application.
