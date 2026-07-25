# Cloud Run Host-Guard (421) Setup

Story 2.1 AC6 — how to activate the 421/Host-header guard on Cloud Run.

## Background

The `HostHeaderValidationMiddleware` in `server/core/main.py` returns
**421 Misdirected Request** when `HOST_HEADER_VALIDATION=strict` is set and the
incoming `Host` header does not match `ALLOWED_HOST`.

FastMCP 3.4.3+ also ships a native host guard controlled by
`FASTMCP_HTTP_ALLOWED_HOSTS`. Both are set in the `deploy.yml` workflow.

For Cloud Run, both variables must be set to the service's `.run.app` hostname.
This hostname is only known **after the first successful deploy** — the Cloud Run
service name is deterministic but the full URL includes a generated suffix.

See `server/core/README.md` for the detailed middleware documentation.

## Step-by-step: activating the guard

### 1. Deploy the service for the first time

On the very first run of `deploy.yml`, the GitHub Actions Variable
`CLOUD_RUN_HOSTNAME_DEV` will be empty. The deploy will succeed but the guard
will be in **fail-open** mode (all hosts allowed when `ALLOWED_HOST` is unset,
per the middleware logic).

### 2. Retrieve the assigned hostname

After the first deploy, run:

```bash
gcloud run services describe mcp-server \
  --project <GCP_PROJECT_DEV> \
  --region europe-west1 \
  --format="value(status.url)"
```

Example output:
```
https://mcp-server-abcd1234-ew.a.run.app
```

The hostname is `mcp-server-abcd1234-ew.a.run.app` (without `https://`).

### 3. Update the GitHub Actions Variable

In the GitHub repo: **Settings > Secrets and variables > Actions > Variables**,
set:

| Variable | Value |
|---|---|
| `CLOUD_RUN_HOSTNAME_DEV` | `mcp-server-abcd1234-ew.a.run.app` |
| `CLOUD_RUN_HOSTNAME_PROD` | _(repeat for prod after prod deploy)_ |

These are Variables (not Secrets) — the hostname is not sensitive.

### 4. Re-run deploy.yml

On the next run, `deploy.yml` passes:
```
--set-env-vars "HOST_HEADER_VALIDATION=strict,ALLOWED_HOST=<hostname>,FASTMCP_HTTP_ALLOWED_HOSTS=<hostname>,PORT=8080"
```

The guard is now active. Requests with a different or missing `Host` header
receive **421 Misdirected Request** with body:
```json
{"code": "misdirected_request", "message": "Host header not allowed", "provenance": null}
```

### 5. Verify the guard

```bash
# Valid request (should succeed):
curl -H "Host: mcp-server-abcd1234-ew.a.run.app" https://mcp-server-abcd1234-ew.a.run.app/mcp

# Spoofed host (should return 421):
curl -H "Host: evil.example.com" https://mcp-server-abcd1234-ew.a.run.app/mcp
# Expected: HTTP 421
```

## AD-14 reference

The env vars `HOST_HEADER_VALIDATION=strict` and `ALLOWED_HOST` activate the
guard defined in `server/core/main.py` and tested in `server/tests/test_health.py`.
This is the normative Story 1.1 implementation per AD-14.

## Notes for local dev

Local dev leaves both guards **off** (no env vars set) so `curl` and MCP clients
on `localhost` work without configuration. Do not set `HOST_HEADER_VALIDATION`
in local `.env` files.
