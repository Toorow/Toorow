# GitHub Provider Setup

## Overview

The `github` connector is a **context module** (Story 4.5).  It fetches
releases and deployments from a GitHub repository and lands them as
`app.context_events` rows in Postgres — not KPI rows.  Events appear on the
same timeline as marketing metrics in the daily-report widget.

## Authentication Options

- **Personal Access Token (PAT):** Create a PAT with `repo` read scope (or
  `public_repo` for public repositories only).  Add to Nango as provider
  `github` with the PAT as `api_key`.

- **OAuth App:** Register a GitHub OAuth App; configure
  `GITHUB_CLIENT_ID` + `GITHUB_CLIENT_SECRET` in Nango's integration panel.
  OAuth grants broader scope but requires the user to complete an auth flow.

- **Nango provider key:** `github` (configure in Nango's Integrations panel).

## Connection Setup

1. In the admin console: **Connections → Add Connection → GitHub**.
2. Choose **PAT** (simpler) or **OAuth**.
3. Enter the repository `owner` and `repo` in the connection config fields.
   - `owner`: GitHub user or organisation name (e.g. `acme`).
   - `repo`:  Repository name (e.g. `website`).
4. The `github` module will use this connection for pulls.

### PAT Setup Steps

1. Go to [GitHub Settings → Developer settings → Personal access tokens](https://github.com/settings/tokens).
2. Generate a new token (classic or fine-grained).
3. Scopes required:
   - Classic PAT: `repo` (includes read access to private repos).
   - Fine-grained PAT: `Contents: Read-only` and `Deployments: Read-only`.
4. Copy the token and add it to Nango (provider key: `github`, type: `api_key`).

## Local Dev: Environment Variable Fallback

For local development without Nango, set:

```bash
export GITHUB_OWNER=acme
export GITHUB_REPO=website
export NANGO_SECRET_KEY=<from nango UI>
```

## Human Gate (HG-GitHub)

Real E2E with a live GitHub repository requires:

1. Create a PAT or OAuth App per the steps above.
2. Add a Nango `github` provider connection in the admin console.
3. Trigger a manual pull:
   ```bash
   # Via admin API
   curl -X POST http://localhost:8000/api/connections/<connection_id>/pull \
     -H "Content-Type: application/json" \
     -d '{"date_from": "2026-07-01", "date_to": "2026-07-10"}'
   ```
4. Verify events appear:
   ```sql
   SELECT * FROM app.context_events
   WHERE type IN ('release', 'deployment')
   ORDER BY event_date DESC
   LIMIT 10;
   ```
5. After the next mirror sync, events should appear in `get_daily_report`
   summaries and as chart markers in the daily-report widget.

## Pull Profiles

| profile_id    | GitHub API endpoint                           | event type    |
|---------------|-----------------------------------------------|---------------|
| `releases`    | `GET /repos/{owner}/{repo}/releases`          | `release`     |
| `deployments` | `GET /repos/{owner}/{repo}/deployments`       | `deployment`  |

## Troubleshooting

- **403 Forbidden:** Token lacks required scopes.  Re-generate with `repo` read scope.
- **No events in daily report:** Check mirror sync is running (`MIRROR_SYNC_TABLES` env var).
- **context_events not visible:** Confirm `app.context_events` table exists (migration 009).
