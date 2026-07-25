# monday.com rollout notes

The committed connector is locally complete but live verification is blocked until a consented monday account is available. Do not change `public_catalog.verification` before the evidence below is ratified.

1. Complete OAuth 2.1 authorization with PKCE S256, compare the well-known document to the pinned endpoints, rotate a refresh token atomically, revoke it, and confirm the six-month reauthorization UX.
2. Run discovery on an account whose main workspace is not returned. Select an opaque board ID and confirm account, workspace and board labels without coercing IDs to numbers.
3. Pull a board containing groups, subitems, common columns and one unsupported/new column type. Confirm `items_page` cursors are exhausted, the runtime schema fingerprint is stable, and unknown values remain raw JSON.
4. Re-pull after an item edit and deletion. Confirm the board snapshot is superseded at `(project_id, board_id, item_id)` while prior context-event history is not deleted outside the requested reconciliation window.
5. Exercise update/webhook challenge, secret validation, retry and duplicate delivery. Confirm one canonical `milestone` event per provider change.
6. Capture GraphQL HTTP-200 error envelopes for complexity, daily, minute, concurrency and IP budgets plus auth, permission and missing-resource failures. Ratify `RateLimit-Policy`, `RateLimit`, `Retry-After` and `retry_in_seconds` handling.
7. Compare the response `API-Version` with `2026-07`. Repeat this compatibility gate quarterly before advancing the manifest pin.

Secrets and real payloads belong only in the private evidence store. Sanitized evidence may replace the documentation-derived fixtures after review.
