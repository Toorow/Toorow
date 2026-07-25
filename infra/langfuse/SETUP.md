# Langfuse Local Setup Guide (Story 5.1)

Self-hosted Langfuse v3 for the toorow tracing pipeline (AD-13, NFR7 — no
trace data leaves the project). Bring-up is **OPTIONAL**: the compose file ships,
CI stays fully mocked, and integration tests skip when Langfuse is unreachable —
exactly the pattern of [`infra/airbyte/SETUP.md`](../airbyte/SETUP.md).

**Phase B note:** Production runs Langfuse on GKE Autopilot (may share the Airbyte
cluster). Only `LANGFUSE_HOST` changes — same env-switch pattern as
`AIRBYTE_BASE_URL`. See the [Phase B section](#phase-b-gke-deployment) at the bottom.

---

## Prerequisites

- Docker Desktop running (same instance used for the Nango stack).
- ~2–3 GB free RAM — the v3 stack is heavy (ClickHouse + Redis + MinIO + Postgres +
  web + worker). Do **not** bring it up unless you actually need live traces.
- The Nango stack already using ports 3003 / 5432 / 5433.

---

## Port Layout (no conflicts with the Nango or Airbyte stacks)

| Service | Container port | Host port (env) | Notes |
|---|---|---|---|
| Langfuse UI/API | 3000 | **3004** (`LANGFUSE_UI_PORT`) | avoids Nango UI 3003 |
| Langfuse Postgres | 5432 | **5435** (`LANGFUSE_POSTGRES_PORT`) | 5432=platform-db, 5433=nango-postgres, 5434=airbyte-db |
| ClickHouse HTTP | 8123 | 8123 (`CLICKHOUSE_HTTP_PORT`) | override if 8123 is taken |
| ClickHouse native | 9000 | 9000 (`CLICKHOUSE_NATIVE_PORT`) | override if 9000 is taken |
| MinIO API | 9000 | **9002** (`MINIO_API_PORT`) | 9001 is MinIO's own console default → API moved to 9002 |
| MinIO console | 9001 | **9091** (`MINIO_CONSOLE_PORT`) | |
| Redis | 6379 | **6380** (`LANGFUSE_REDIS_PORT`) | avoids collision with any local Redis on 6379 |

**Conflict resolution decisions (Story 5.1 T1.4):**

- **3004** for the UI keeps clear of Nango's 3003 and the connector dev server (8000).
- **5435** for Postgres continues the 543x sequence (platform-db 5432, nango 5433,
  airbyte 5434) so all four Postgres instances can run side by side.
- **MinIO API → 9002** (not the spec's 9001): 9001 is MinIO's built-in *console*
  port, so binding the API there would collide with the console inside the same
  container. API on 9002, console on 9091.
- **Redis → 6380** (not the spec's 6379): 6379 is the default any locally installed
  Redis grabs; 6380 avoids a silent clash. All ports are env-overridable.

Verify a port is free before bringing up (Windows):
`netstat -an | findstr 3004`

---

## Bring-up (optional)

```bash
cp infra/langfuse/.env.example infra/langfuse/.env
# Edit infra/langfuse/.env: set a real LANGFUSE_ENCRYPTION_KEY (openssl rand -hex 32)
# and unique SALT / NEXTAUTH_SECRET before any non-throwaway use.

docker compose -f infra/langfuse/docker-compose.yml --env-file infra/langfuse/.env up -d
# First start pulls images + runs ClickHouse/Postgres migrations (2–5 min).

docker compose -f infra/langfuse/docker-compose.yml ps
```

Open the UI at `http://localhost:3004`, create the initial org/project, then mint an
API key pair (Settings → API Keys). Put them in the connector's own `.env`:

```
TRACING_ENABLED=true
LANGFUSE_HOST=http://localhost:3004
LANGFUSE_PUBLIC_KEY=pk-lf-...
LANGFUSE_SECRET_KEY=sk-lf-...
```

Tear down (keeps volumes): `docker compose -f infra/langfuse/docker-compose.yml down`
Full reset (drops data): add `-v`.

---

## How tracing connects (no live Langfuse needed for CI)

- `server/core/tracing.py` builds an OTel `TracerProvider` with a `BatchSpanProcessor`
  exporting via **OTLP HTTP** to `<LANGFUSE_HOST>/api/public/otel/v1/traces`, using
  HTTP Basic auth derived from the public/secret key pair.
- Everything is gated by `TRACING_ENABLED` (default `false`). When false — as in CI
  and the default `.env.example` — **no exporter is initialised and no OTLP call is
  ever made**. All spans no-op.
- The OTel SDK is an **optional** dependency (`pyproject` `[tracing]` extra). The
  server starts normally when it is absent from the venv.
- Unit tests use an in-memory span exporter (`InMemorySpanExporter`) — they never
  touch a live Langfuse. The one integration test skips when Langfuse is unreachable.

---

## Verify Langfuse is running

```bash
curl -s http://localhost:3004/api/public/health
# Expected: {"status":"OK",...}
```

---

## Phase B: GKE Deployment

No code changes required — only `LANGFUSE_HOST` changes:

```
# Local dev
LANGFUSE_HOST=http://localhost:3004

# Phase B: GKE internal service URL (Langfuse in its own namespace, possibly on the
# shared Airbyte Autopilot cluster)
LANGFUSE_HOST=http://langfuse-web.langfuse.svc.cluster.local:3000
```

The OTLP exporter and `TRACING_ENABLED`/`TRACING_SAMPLE_RATE` switches are
production-ready; the GKE move is an env-var switch, mirroring the Airbyte pattern.
