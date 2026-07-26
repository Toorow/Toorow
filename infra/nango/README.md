# infra/nango/ -- Nango self-hosted local dev setup

Story 2.2 delivers local Nango OSS via docker-compose. This README covers
everything needed to start the stack, apply migrations, configure Google OAuth,
and run integration tests.

## Prerequisites

- Docker Desktop running (verified: Docker Desktop for Windows, Docker 28.0.4+)
- No other service using ports 3003, 5432, or 5433 on your machine
- `.env` file created from `.env.example` (see Setup below)

## Setup

```bash
# 1. Copy the env template
cp infra/nango/.env.example infra/nango/.env

# 2. Generate a Nango encryption key (256-bit, base64)
python -c "import secrets, base64; print(base64.b64encode(secrets.token_bytes(32)).decode())"
# -> paste the output as NANGO_ENCRYPTION_KEY in .env

# 3. Start the stack
docker compose -f infra/nango/docker-compose.yml up -d
```

## Verify Nango is healthy

```bash
# Should return {"status":"ok"} or similar
curl http://localhost:3003/healthcheck
```

Wait ~30 seconds on first start (Nango runs DB migrations on boot).

## Apply platform Postgres schema migration

After `platform-db` is healthy, apply the first migration:

```bash
docker compose -f infra/nango/docker-compose.yml exec platform-db \
  psql -U connector -d connector -f /migrations/001_create_connection_ref.sql
```

Verify the table was created:

```bash
docker compose -f infra/nango/docker-compose.yml exec platform-db \
  psql -U connector -d connector -c "\dt app.*"
```

## Human gate: Google OAuth client (no billing required)

[HUMAN GATE] Jean must create a Google Cloud OAuth 2.0 client ID.
This can be done in ANY Google account (personal GCP project, free tier --
no billing account required).

Steps:
1. Go to https://console.cloud.google.com/apis/credentials
2. Select or create a project (free tier is fine)
3. Click "Create Credentials" -> "OAuth 2.0 Client ID"
4. Application type: Web application
5. Authorized redirect URIs: `http://localhost:3003/oauth/callback`
6. Click "Create" -- note the client_id and client_secret
7. Copy them into `infra/nango/.env`:
   ```
   GOOGLE_CLIENT_ID=<your-client-id>
   GOOGLE_CLIENT_SECRET=<your-client-secret>
   ```
8. Enable the Google Analytics Data API:
   https://console.cloud.google.com/apis/library/analyticsdata.googleapis.com

## Register google-analytics integration in Nango

After obtaining Google OAuth credentials:

**Method: Nango UI (simplest for local dev)**

1. Open http://localhost:3003 in your browser
2. Navigate to "Integrations" -> "Add Integration"
3. Search for "Google Analytics" or configure manually:
   - Provider: `google-analytics`
   - Client ID: your GOOGLE_CLIENT_ID
   - Client Secret: your GOOGLE_CLIENT_SECRET
   - Scopes: `https://www.googleapis.com/auth/analytics.readonly`
4. Save the integration

Note the Nango Secret Key from Settings -> Secret Key, and add it to `.env`:
```
NANGO_SECRET_KEY=<secret-key-from-nango-ui>
```

## Smoke test: trigger a GA4 OAuth flow manually

Once the `google-analytics` integration is configured in Nango:

1. In the Nango UI, go to "Connections" -> "Add Connection"
2. Select the `google-analytics` integration
3. Follow the OAuth flow -- this confirms the redirect URI and scopes work
4. The resulting connection_id can be used to test `poll_connection_health`

## Running integration tests locally

```bash
# Start the stack first
docker compose -f infra/nango/docker-compose.yml up -d

# Set env vars (or load from .env)
export NANGO_BASE_URL=http://localhost:3003
export NANGO_SECRET_KEY=<from-nango-ui>
export NANGO_INTEGRATION_TESTS=1

# Run integration tests
cd server
uv run pytest tests/integration/test_nango_integration.py -v
```

Integration tests skip automatically when Nango is not reachable
or `NANGO_INTEGRATION_TESTS` is not set.

## Stop / reset

```bash
# Stop (preserve volumes)
docker compose -f infra/nango/docker-compose.yml down

# Full reset (deletes all data including Nango's internal state)
docker compose -f infra/nango/docker-compose.yml down -v
```

## Migration tooling decision

**Raw SQL chosen** for Story 2.2 (over Alembic). Rationale:
- Only one table in this story
- Zero extra Python dependencies
- Fully inspectable and version-controllable
- Revisit in Story 2.6 when `audit_log` table adds complexity

Migration files live in `infra/nango/migrations/`. Validate them with
`python scripts/check_migration_catalog.py`, then apply pending migrations with
`PLATFORM_DB_URL=... python scripts/apply_migrations.py`. The runner serializes
execution and records checksums in `toorow_meta.schema_migrations`.

## Port allocation

| Service       | Container port | Host port | Purpose                        |
|---------------|---------------|-----------|--------------------------------|
| nango-postgres| 5432          | 5433      | Nango internal DB (don't touch)|
| nango         | 3003          | 3003      | Nango UI + API                 |
| platform-db   | 5432          | 5432      | Our application Postgres       |

## Image pins (Story 2.2)

| Image            | Tag      | Purpose           |
|------------------|----------|-------------------|
| nangohq/nango-server | hosted-0.70.9 | Nango OSS server  |
| postgres         | 17       | Both Postgres DBs |

## Prod migration path

In prod, replace `NANGO_BASE_URL` with the Cloud Run service URL and set all
secrets via GCP Secret Manager -- no code changes required. This is a
config-only migration (Story 2.2 design constraint).

Secret Manager keys:
- `nango-encryption-key` -> NANGO_ENCRYPTION_KEY
- `nango-secret-key` -> NANGO_SECRET_KEY
- `google-oauth-client-id` -> GOOGLE_CLIENT_ID
- `google-oauth-client-secret` -> GOOGLE_CLIENT_SECRET
- `platform-database-url` -> PLATFORM_DATABASE_URL
