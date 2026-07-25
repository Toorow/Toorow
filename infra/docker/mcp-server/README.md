# mcp-server Docker image

Story 2.1 AC1/AC2 — local build and smoke-test documentation.

## Build

Run from the **repo root** (the build context must include `server/` and `uv.lock`):

```bash
docker build -f infra/docker/mcp-server/Dockerfile -t connector/mcp-server:local .
```

The multi-stage build:
1. **builder** (`python:3.12`) — copies workspace files, installs all deps via
   `uv sync --frozen --no-dev` into a virtual environment.
2. **runtime** (`python:3.12-slim`) — copies the virtualenv and `server/` source,
   creates a non-root `appuser`, sets `PYTHONPATH=/app/server`, exposes `$PORT`.

## Run

```bash
docker run --rm -e PORT=8080 -p 8080:8080 --name mcp-server-local connector/mcp-server:local
```

The server binds `0.0.0.0:$PORT` and serves the MCP endpoint at `/mcp`.
Set `PORT=8000` to match local dev convention.

## Health check (smoke test — AC2)

The MCP streamable-HTTP transport requires a JSON-RPC handshake before tool
calls; a plain `curl GET /health` will not work. Use the FastMCP in-process
client instead (no network needed):

```bash
# In-process smoke test (no running container required):
uv run python - <<'PY'
import asyncio
from fastmcp import Client
from core.main import mcp

async def main():
    async with Client(mcp) as c:
        res = await c.call_tool("health", {"project_id": "default"})
        print(res.structured_content or res.data)

asyncio.run(main())
PY
# Expected: AD-1 envelope with data.status == "ok"
```

Against a **running container**, replace the in-process client with an HTTP URL:

```bash
# Start the container first:
docker run --rm -e PORT=8080 -p 8080:8080 -d --name mcp-server-local connector/mcp-server:local

# Then call via HTTP:
uv run python - <<'PY'
import asyncio
from fastmcp import Client

async def main():
    async with Client("http://localhost:8080/mcp") as c:
        res = await c.call_tool("health", {"project_id": "default"})
        print(res.structured_content or res.data)

asyncio.run(main())
PY

# Cleanup:
docker stop mcp-server-local
```

Expected response (AD-1 envelope):
```json
{
  "schema_version": "1",
  "meta": { "freshness": "...", "provenance": { ... }, "alerts": [] },
  "data": { "status": "ok" }
}
```

## Cloud Run notes

Cloud Run injects `PORT=8080` by default. The `deploy.yml` workflow also
passes `--set-env-vars "PORT=8080,..."` explicitly.

The 421/Host-guard is **disabled** in this local smoke-test (no `HOST_HEADER_VALIDATION=strict`
or `ALLOWED_HOST` env vars set). See `infra/docs/cloud-run-host-guard.md` for
the Cloud Run activation procedure.

## BLOCKED status (AI-01 convention)

If Docker Desktop is not running or the Docker daemon is unavailable in the
current environment, AC2 is BLOCKED. Run the smoke-test command above manually
once Docker is available:

```bash
# Verify daemon is up:
docker version

# Build:
docker build -f infra/docker/mcp-server/Dockerfile -t connector/mcp-server:local .

# Run and health-check:
docker run --rm -e PORT=8080 -p 8080:8080 -d --name mcp-server-local connector/mcp-server:local
# ... then run the HTTP Client snippet above ...
docker stop mcp-server-local
```
