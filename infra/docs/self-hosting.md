# Deploy toorow on your own stack

This guide is for **anyone who wants to run their own toorow instance** — not the
maintainers. It lists the accounts you must create, the services you must
provision, and how to wire them together. toorow is stack-agnostic: nothing here
is hardcoded to the maintainers' project. You bring your own accounts and put
their identifiers in a local `.env`.

> The maintainers run their own private instance with their own credentials.
> None of those secrets ship in this repository. Every value below is yours to
> create.

## Privacy — a self-hosted instance sends us nothing

The shipped product (this repo: MCP server, connector modules, widgets) contains
**no analytics or telemetry**. There is no Google Analytics, no tracking pixel and
no phone-home in the code you run. The marketing site and docs analytics live in
separate surfaces we operate (`web/`, Mintlify) and never reach a self-hosted
deployment.

The maintainers measure **their own hosted instance** with the GA4 **Measurement
Protocol** (server-side): the backend POSTs aggregate product-usage events to GA4,
gated entirely by two env vars — `GA4_MP_MEASUREMENT_ID` and `GA4_MP_API_SECRET`.
These are **unset in this repo and in `.env.example`**. With them unset the emitter
is a no-op, so a self-hosted instance measures nothing. This is the same
opt-in-by-env pattern as the optional Langfuse tracing (item 5 below). If you *want*
your own product analytics, set your own GA4 credentials — the data goes to *your*
GA4 property, never ours.

## 1. Accounts to create (prerequisites)

| # | Account / service | What toorow uses it for | Free tier is enough to start? |
|---|---|---|---|
| 1 | **GitHub** | Clone this repository. | Yes |
| 2 | **Supabase project** (or any Postgres 15+) | Platform database: connections, governance, audit log, job queue (`PLATFORM_DB_URL`). | Yes |
| 3 | **Google Cloud account + project** | BigQuery warehouse (raw landings + semantic marts), Cloud Run hosting, and the OAuth client for Google sources (GA4 / Search Console / Google Ads / Sheets). | Yes, within quotas |
| 4 | **Nango** (Nango Cloud or self-hosted) | OAuth broker for the non-Google connectors (Meta Ads, TikTok, LinkedIn, Shopify, Stripe, Klaviyo…). Stores refresh tokens, refreshes access tokens on your behalf. | Yes (Cloud free tier or Docker) |
| 5 | **Langfuse** *(optional)* | Tracing / observability for MCP tool calls. | Yes (optional) |

You only need accounts for the sources you actually plan to connect. A minimal
install (platform DB + one connector) needs items 1–3 plus, for non-Google
sources, item 4.

## 2. Local prerequisites

Same as [the top-level README](../../README.md#prerequisites):

- Git, Python 3.12, [uv](https://docs.astral.sh/uv/)
- Node.js 22+, pnpm 9.x (`corepack enable`)
- Docker Desktop / Engine + Compose (for the full local platform)
- Terraform 1.7+ (only if you provision cloud infra with the bundled IaC)

## 3. Provision each service

### 3a. Platform database — Supabase (or self-hosted Postgres)

1. Create a Supabase project. In **Project Settings → Database**, copy the
   connection string (use the connection pooler URI for serverless deploys).
2. Set it in your `.env`:
   ```
   PLATFORM_DB_URL=postgresql://USER:PASSWORD@HOST:PORT/postgres
   ```
3. Apply the schema. The platform tables and `audit_log` migrations live in
   [`infra/`](../) — run them against your database before first boot.

For a purely local run you can skip Supabase and use the bundled dev Postgres
from `infra/nango/docker-compose.yml` (the default `.env.example` value points
at it).

### 3b. Warehouse + OAuth + hosting — Google Cloud

1. Create a GCP **project** (this is your equivalent of the maintainers'
   internal project — name it whatever you like).
2. Enable billing. Set a low budget + alert if you are just evaluating.
3. Enable the APIs you need: BigQuery, Cloud Run, and (per source) the Analytics
   Data, Search Console, Google Ads and Sheets APIs.
4. Provision BigQuery datasets + a service account with
   `roles/bigquery.dataEditor` + `roles/bigquery.jobUser`. The bundled Terraform
   in [`infra/terraform/`](../terraform/) does this — see
   [infra/README.md](../README.md). Run `terraform plan` / `apply` **with your
   own** `-var billing_account=<YOUR_BILLING_ID>`.
5. Create an OAuth 2.0 client (Web application) for the Google sources and record
   the client id / secret in your `.env`. Google sources use a direct server-side
   OAuth flow — one consent grants all requested scopes.

### 3c. Connector OAuth broker — Nango

1. Create a Nango Cloud account, or run Nango yourself with
   `infra/nango/docker-compose.yml`.
2. For each non-Google connector you want, add the provider in Nango and paste
   your own app credentials (Meta app, TikTok app, LinkedIn app, etc.).
3. Put the Nango host + secret key in your `.env`.

### 3d. Auth mode for the MCP server

`.env` controls how the server authenticates callers (`TOOROW_AUTH_MODE`):

- `static` — a shared token, fine for local QA (default).
- `oauth` — RS256 JWT verification, **required for any networked/production
  deploy**. Provide `TOOROW_JWT_PUBLIC_KEY` **or** `TOOROW_JWKS_URI`.

Never expose a `static` or `disabled` instance to the public internet.

## 4. Configure

```bash
cp .env.example .env
# then fill in the values you gathered above
```

Every variable is documented inline in [`.env.example`](../../.env.example).

## 5. Run locally

```bash
make install-server        # uv sync
make install-ui            # pnpm install
make dev                   # FastMCP server on 0.0.0.0:8000
make test                  # server test suite
```

## 6. Deploy to Cloud Run

The application ships a human-gated Cloud Run deployment workflow under
[`.github/workflows/`](../../.github/workflows/) and the Terraform under
[`infra/terraform/`](../terraform/). Deployment is deliberately **not**
automatic: you review and trigger it with your own GCP credentials. Follow
[infra/README.md](../README.md), substituting your own project id and billing
account for the maintainers' values.

## 7. Security checklist before going live

- [ ] `TOOROW_AUTH_MODE=oauth` (never `static`/`disabled` on a public host).
- [ ] Secrets live only in `.env` / your secret manager — never committed.
- [ ] `PLATFORM_DB_URL` points at **your** database with a strong password.
- [ ] BigQuery service account is least-privilege and scoped to your project.
- [ ] Host-header guard configured (`ALLOWED_HOST`, `FASTMCP_HTTP_ALLOWED_HOSTS`).
- [ ] Every connector's OAuth app is registered under your own developer account.

## Where things live

| Concern | Path |
|---|---|
| MCP server, auth, queue, governance | [`server/`](../../server/) |
| Connector modules + dbt staging | [`server/modules/`](../../server/modules/) |
| Admin console, cards, widgets | [`ui/`](../../ui/) |
| Semantic marts, seeds, tests | [`dbt/`](../../dbt/) |
| Local services, Docker, Terraform | [`infra/`](../) |
| CI + human-gated deploy | [`.github/workflows/`](../../.github/workflows/) |
