# Admin Console

React + Vite admin UI for the toorow MCP server.

Served by the mcp-server at `/admin` (static files from `dist/`).
Dev server at `http://localhost:5174` (proxies `/api` to `:8000`).

## Dead-Letter Job Visibility (Story 3.4, AC6)

The `GET /api/jobs` endpoint (added in Story 3.4) exposes pull job state for
visibility and admin tooling. A future admin UI panel for dead-letter queues
should use this endpoint.

Relevant query patterns:

```
GET /api/jobs                                -- all jobs (newest first, limit 200)
GET /api/jobs?state=dead_letter              -- jobs that exhausted MAX_ATTEMPTS
GET /api/jobs?state=dead_letter&connection_ref_id=conn_<ULID>
                                             -- dead-letter for one connection
```

Response shape:
```json
{
  "jobs": [
    {
      "id": "job_...",
      "pull_id": "pull_...",
      "connection_ref_id": "conn_...",
      "date_from": "YYYY-MM-DD",
      "date_to": "YYYY-MM-DD",
      "state": "dead_letter",
      "requested_by": "scheduler",
      "error_detail": "...",
      "attempt_count": 3,
      "enqueued_at": "ISO-8601",
      "started_at": "ISO-8601",
      "completed_at": "ISO-8601"
    }
  ]
}
```

A future admin panel component for dead-letter jobs would call
`GET /api/jobs?state=dead_letter` on mount and render the job list
(similar to `ConnectionsList.tsx` pattern for connections).
