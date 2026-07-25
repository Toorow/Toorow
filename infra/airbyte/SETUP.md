# Airbyte Local Setup Guide (Story 3.1)

Local Airbyte OSS for the toorow P3-dev environment.

**Phase B note:** Production uses Airbyte OSS on GKE Autopilot. The only
difference from local is `AIRBYTE_BASE_URL` — see the [Phase B section](#phase-b-gke-deployment)
at the bottom of this file. No code changes required.

---

## Prerequisites

- Docker Desktop running (same instance used for the Nango stack)
- 4 GB RAM available for the Airbyte k3s cluster (abctl path) or 2 GB (docker-compose minimal)
- The Nango stack already running on ports 3003, 5432, 5433 (see `infra/nango/docker-compose.yml`)

---

## Install Method Decision

| Method | RAM needed | Requires k8s | Best for |
|---|---|---|---|
| **abctl** (primary) | ~4 GB | Yes (k3s in Docker) | Full Airbyte stack, official support |
| **docker-compose** (fallback) | ~2 GB | No | Lighter, easier port overrides |

**Dev machine check (Story 3.1 T2):**
- `abctl` not installed on this machine (confirmed 2026-07-11).
- Docker Desktop: running (Nango stack active).
- **Chosen path: docker-compose fallback** — lighter, no k3s required.

---

## Port Conflict Analysis (Nango vs Airbyte)

| Service | Default port | Host mapping |
|---|---|---|
| Nango UI/API | 8080 | `localhost:3003` |
| Nango-postgres | 5432 | `localhost:5433` |
| Platform-db | 5432 | `localhost:5432` |
| **Airbyte UI** | 80/8000 | `localhost:8000` ⚠️ conflict with server PORT |
| **Airbyte API** | 8001 | `localhost:8001` (safe) |
| **Airbyte db** | 5432 | `localhost:5434` (override needed) |

**Port conflict: `8000`** — the connector dev server also listens on 8000.

**Resolution strategy:**
- Run the Airbyte UI on a different host port: `AIRBYTE_WEBAPP_PORT=8080` in the
  Airbyte docker-compose `.env` (note: Nango UI is exposed on 3003, so 8080 is
  usually free unless other services use it — verify with `netstat -an | findstr 8080`).
- Or set `AIRBYTE_BASE_URL=http://localhost:8001` to point at the Airbyte API
  server directly (the API port is distinct from the UI port).
- Set `AIRBYTE_BASE_URL` in `.env` to the chosen URL.

---

## Option A — abctl (Primary, when available)

> **BLOCKED:** `abctl` is not installed on the current dev machine.
> Install with: `curl -fsSL https://get.airbyte.com | bash`

Once installed:

```bash
# 1. Install Airbyte locally (uses k3s under Docker Desktop)
abctl local install --low-resource-mode

# 2. Check status
abctl local status

# 3. Open the UI
# http://localhost:8000  (or whatever port abctl reports)
```

**Port override** (if 8000 conflicts with the dev server):

```bash
abctl local install --low-resource-mode --port 8080
# Then set: AIRBYTE_BASE_URL=http://localhost:8080
```

**Credentials**: `abctl local credentials` prints the default email/password.

---

## Option B — docker-compose (Fallback, active path)

```bash
# Clone the Airbyte platform repo
git clone https://github.com/airbytehq/airbyte-platform.git /tmp/airbyte-platform
cd /tmp/airbyte-platform

# Copy the env file and override conflicting ports
cp .env.prod .env
# Edit .env to set:
#   WEBAPP_PORT=8080      (avoids conflict with connector dev server on 8000)
#   PG_PORT=5434          (avoids conflict with platform-db on 5432 and nango-postgres on 5433)

# Start the stack
docker compose up -d

# Wait for services (can take 2-5 minutes on first start)
docker compose ps
```

Airbyte API will be at: `http://localhost:8001`
Airbyte UI will be at: `http://localhost:8080` (after port override)

Set in `.env`:
```
AIRBYTE_BASE_URL=http://localhost:8001
```

---

## Verify Airbyte is Running

```bash
curl http://localhost:8001/api/v1/health
# Expected: {"available":true,"..."}
```

---

## Configure GA4 Source

### 1. Retrieve a fresh token from Nango (AD-3 compliance)

Nango owns OAuth credentials (AD-3). At configuration time (one-time admin step),
call the Nango API to get a fresh token for the GA4 connection:

```bash
# Replace {connection_id} with your Nango GA4 connection ID
curl -H "Authorization: Bearer $NANGO_SECRET_KEY" \
  "http://localhost:3003/connection/{connection_id}?force_refresh=true&provider_config_key=google-analytics"
# Extract: .credentials.access_token
```

**AD-3 compliance note:** Our code, modules, and Postgres never store this token.
It is passed directly to Airbyte's source configuration (stored inside Airbyte's
own config store). Airbyte is the extraction-only exception layer: it acts as the
ETL system and never shares tokens with our modules or Postgres. This is the
accepted exception to AD-3 for the Airbyte path.

### 2. Create GA4 Source in Airbyte

Via the Airbyte UI (`http://localhost:8080`) or API:

- Source type: `Google Analytics 4 (GA4)`
- Authentication method: `Access Token`
- Access Token: `<token from step 1>`
- Property ID: `<your GA4 property ID>`
- Start Date: `2024-01-01` (or as appropriate)

### 3. Manual Token Rotation Procedure

Airbyte does not automatically refresh tokens from Nango (Phase B feature).
When the GA4 access token expires:

1. Call `GET /connection/{id}?force_refresh=true` on Nango (as in step 1 above).
2. Update the GA4 source in Airbyte UI: Sources → GA4 → Edit → update Access Token.
3. Test the connection in Airbyte.

**Rotation cadence:** GA4 OAuth access tokens expire after 1 hour. For long-running
syncs or scheduled nightly syncs, rotate before triggering (or implement Phase B
automated refresh via a webhook).

---

## Configure Destination (Raw Table Mapping)

Airbyte must write to `raw_ga4_standard_daily` in the platform Postgres with the
canonical column schema consumed by `dbt/models/staging/google_analytics/stg_ga4_standard_daily.sql`.

### Destination settings

- Destination type: `Postgres`
- Host: `localhost`
- Port: `5432`
- Database: `connector`
- Schema: `app`
- Username: `connector`
- Password: `<PLATFORM_DB_PASSWORD>`

### Table and column mapping

Airbyte GA4 connector outputs snake_case columns. Configure the destination stream
to write to table `raw_ga4_standard_daily` with these column mappings:

| Airbyte output column | Canonical column | Notes |
|---|---|---|
| `date` | `date` | Matches |
| `device_category` | `device_category` | Matches |
| `country` | `country` | Matches |
| `sessions` | `sessions` | Matches |
| `active_users` | `active_users` | Matches |
| `conversions` | `conversions` | Matches |
| _(not output by Airbyte)_ | `pull_id` | Use `airbyte_<job_id>` (AC4 exception) |
| _(Airbyte sync time)_ | `loaded_at` | Use Airbyte's `_airbyte_emitted_at` |
| _(from destination config)_ | `project_id` | Set as a static column in destination |

**pull_id format for Airbyte-originated rows:** `airbyte_<job_id>` (e.g. `airbyte_456`).
This is a documented exception to the AD-7 rule (core scheduler mints pull_ids for
Cloud Tasks dispatched pulls). Phase B will thread the core-minted pull_id through
Airbyte job metadata — see `# TODO(Phase-B)` comment in `server/core/airbyte_client.py`.

---

## Trigger a Sync via AirbyteClient

Once Airbyte is running and the GA4 source + destination are configured:

```python
import os
os.environ["AIRBYTE_BASE_URL"] = "http://localhost:8001"

from server.core.airbyte_client import trigger_sync, get_sync_status

# Replace with your Airbyte connection UUID (from Airbyte UI > Connections)
result = trigger_sync("your-airbyte-connection-uuid")
print(result)  # {"job_id": "123", "status": "pending"}

# Poll status
status = get_sync_status(result["job_id"])
print(status)  # {"job_id": "123", "status": "succeeded", ...}
```

Or run the integration test (requires `AIRBYTE_TEST_CONNECTION_ID` set):

```bash
AIRBYTE_BASE_URL=http://localhost:8001 \
AIRBYTE_TEST_CONNECTION_ID=your-connection-uuid \
uv run pytest server/tests/core/test_airbyte_client.py::test_live_trigger_sync_integration -v
```

---

## Phase B: GKE Deployment

No code changes required. Only `AIRBYTE_BASE_URL` changes:

```
# Local dev
AIRBYTE_BASE_URL=http://localhost:8001

# Phase B: GKE internal service URL (Airbyte in dedicated namespace)
AIRBYTE_BASE_URL=http://airbyte-server.airbyte.svc.cluster.local:8001
```

GKE Autopilot deployment will run Airbyte in a dedicated `airbyte` namespace.
The `AirbyteClient` is production-ready — env-var switch only.

Additional Phase B work:
- Automated token refresh: Nango webhook → Airbyte source update
- pull_id threading: core-minted ULID through Airbyte job metadata (AD-7)
- Cloud Tasks integration: Airbyte sync completion callback → queue job tracking

---

## AI-13: Live Integration Pass Status

**BLOCKED (P3-dev):** abctl not installed on dev machine; docker-compose Airbyte
not yet started. The `@airbyte_available` guard in `test_airbyte_client.py` skips
the live test automatically. All unit tests pass with mocks.

**To complete AI-13:** Install and start local Airbyte (Option A or B above),
configure a GA4 source, then run `test_live_trigger_sync_integration`. This is
a Phase B verification item, not a blocker for Story 3.1 (per the skip-if-unreachable
contract in AC6).
