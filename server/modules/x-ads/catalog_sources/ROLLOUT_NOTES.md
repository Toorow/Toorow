# X Ads rollout notes

OAuth 1.0a is brokered and signed by Nango under AD-3. The v12 module has a
deterministic zero-planned compatibility catalog, bounded sync and resumable async
paths. Live verification requires an approved developer app and advertiser account.

## `prefer_sync` escape hatch (M-1)

`_pull_profile` accepts `selection.get("prefer_sync", False)` to route ≤7-day,
non-segmented windows through the synchronous stats path instead of the async job
path. This flag is **not part of the public API contract** and is not exposed in
the manifest or any production MCP tool call.

Intended uses:

- Unit and integration tests that need a predictable synchronous response without
  standing up an async job executor.
- Local developer debugging of short date windows.

Governance:

- `prefer_sync=True` bypasses the async job-id persistence, duplicate-prevention,
  and `run_async_stats` tracking path. It must never be set from production
  selection payloads or manifest-driven pull calls.
- No manifest `report_profile` or production fixture should include `prefer_sync`.
- If this hatch is no longer needed, remove the branch from `connector.py:380`
  and delete this note.
