# pinterest-ads -- ROLLOUT NOTES (Story 26.3)

Status: `public_catalog.verification: blocked` (`live_evidence_not_ratified`).
Everything below is PROBE-ONLY: it needs live credentials and cannot be proven
by the mocked test suite. Evidence goes to `reports/ratification-<date>.json`
(25.6 contract) and the story Dev Agent Record.

## 1. Nango refresh-token rotation (THE 60-day silent-death risk)

Pinterest moved to **continuous refresh** on 2025-09-25: the access token
lives 30 days, the refresh token lives **60 days and ROTATES** -- every
refresh response carries a NEW `refresh_token` (`refresh_token_expires_in:
5184000`). The legacy 365-day refresh token is no longer issued.

Consequence: if the stored refresh token is NOT re-persisted on every
refresh, the connection dies silently at J+60 with `401 code 2`.

Probe checklist (AI-13, before ratification):
- [ ] Confirm the Nango provider template for Pinterest declares token
      rotation (`refresh_token` update on refresh) -- inspect the Nango
      provider config (`pinterest` template) on our Nango Cloud instance.
- [ ] Force a refresh through `nango_client.get_fresh_token` (force_refresh
      path) TWICE and verify in the Nango dashboard that the connection's
      stored refresh token CHANGED between the two calls (rotation
      persisted), and that the second refresh succeeds with the rotated
      token.
- [ ] Document the observed `expires_in` / `refresh_token_expires_in` values.
- [ ] If Nango does NOT persist the rotated token: fall back to the AD-21
      encadre direct-OAuth pattern (encrypted token in `app.connection_ref`)
      and record the decision at AD-3 -- do NOT ship with a 60-day fuse.
- [ ] Alerting: verify a `401 code 1/2` surfaces as `auth_expired` with the
      reconnect affordance (AD-15) on a revoked sandbox connection.

## 2. App tier / quota

- [ ] Confirm the app has **Standard access** (`ads_analytics` 300 req/min
      per user). Trial tier = 1,000 req/DAY per app: kills any 914-day
      backfill; `403 code 29` must surface as a readable
      `permission_denied`. Open epic-26 question: throttle profile for Trial.
- [ ] Verify `x-ratelimit-reset` header semantics on a real 429 (seconds vs
      epoch) -- `_retry_after_seconds` assumes seconds-to-wait.

## 3. Async report cycle (probe with 1-day window, level=CAMPAIGN)

- [ ] `POST /ad_accounts/{id}/reports` accepts the FULL 626-column `columns`
      array in ONE report (dossier says yes per spec; if the provider caps
      the payload, implement family chunking before ratification).
- [ ] Confirm the download URL dies at ~5 minutes with `403` + "Request has
      expired" (the `DownloadLinkExpired` marker regex) and that a re-created
      report succeeds (single-resubmission path).
- [ ] Confirm the JSON payload row shape: list of `{DATE, <COLUMN>: value}`
      objects or per-entity keyed map -- `_report_rows` handles both, refuse
      anything else (drift signal).
- [ ] **Implicit identity keys of async rows (review 26.3, NEW)**: when the
      payload is the per-entity keyed map, check whether the DICT KEY is the
      entity id (campaign/ad_group/ad id). Today `_report_rows` flattens the
      values and DISCARDS the key: if the key IS the entity id, capture it
      into the row (grain identity) instead of throwing it away -- code
      change + regen of the golden fixtures before ratification.
- [ ] Confirm `report_status` vocabulary matches `_ASYNC_STATUS_MAP`
      (`FINISHED/IN_PROGRESS/EXPIRED/CANCELLED/DOES_NOT_EXIST/FAILED`).
- [ ] CONVERSION DEVICE PATH family (99 columns, `exposure: exposed` since
      review 26.3 F-1 -- same request shape as every async column): run one
      catalog_daily batch covering the family as a row-shape CONTROL. This is
      a probe CONTROL, NOT an exposure gate: if the rows come back with a
      shape change (nested/keyed differently), fix `_report_rows` / the
      flattening -- do not re-exclude the family.
- [ ] **Column x level legality of the heuristic lineage (review 26.3, NEW)**:
      the `field_compatibility` selectable_set rules derive from a HEURISTIC
      lineage (LEVEL_DIMS in build_official_fields.py: per-level response
      attributes read off the OpenAPI spec). Verify live, per whitelisted
      level, that (a) each lineage dimension actually returns at that level
      and (b) an out-of-lineage dimension is actually rejected by the
      provider the way we pre-refuse it. Any divergence = regen the rules
      from the observed matrix.
- [ ] **KEYWORD level ratification (review 26.3 F-3, NEW)**: KEYWORD is OUT
      of the v1 whitelist because the enum carries no observable keyword
      identity column -- N keywords of an ad group would collapse onto one
      grain key and the dbt QUALIFY supersede would keep ONE arbitrary row.
      Ratify the level at the probe ONLY IF the response rows carry an
      observable identity key -- including the per-entity keyed map check
      above (is the dict key the keyword id? then CAPTURE it instead of
      discarding it). Without an identity key the level stays out.

## 4. Sync analytics path

- [ ] Confirm `campaign_ids`/`ad_group_ids`/`ad_ids` accept the comma-joined
      form (the connector sends CSV in one param; switch to repeated params
      if the API rejects CSV).
- [ ] Response row keys: requested column tokens + `DATE` at granularity DAY
      (the `_flatten_row` contract).
- [ ] `QUIZ_PIN_RESULT_OPEN` (sync-only, `exposure: excluded`): probe it on
      `GET /ad_accounts/{id}/campaigns/analytics` -- it is the ONE column
      only reachable there; a future sync-columns profile re-exposes it.
- [ ] Probe the 184 sync-shared columns via the `fields_param` style
      (batches of 20, 1-day window); async-only columns (442) either get a
      per-batch async probe or stay explicitly `unprobed` in coverage.

## 5. Attribution metadata

- [ ] The pinned spec `{click: 30, engagement: 30, view: 1,
      conversion_report_time: TIME_OF_AD_ACTION}` is stamped on every
      request and returned in every pull result (`attribution` key): verify
      the scheduler persists it into the datastream config/metadata so two
      datastreams with different windows can never be merged silently.
      Post-2025-04 UI divergence (UI dropped the engagement window) is
      documented in catalog_sources.json `_request_limits.attribution_pinned`.

## 6. Error taxonomy confirmation (ratify_connector --probe-auth)

- [ ] `401:1` / `401:2` -> auth_expired (revoked sandbox token).
- [ ] `403:3` (consumer type) / `403:29` (Trial feature) -> permission_denied.
- [ ] `400:12` / message "Retry after" -> provider_transient (NEVER
      invalid_request -- breaker safety).
- [ ] `429:8` -> RateLimitError with honored retry_after.
- [ ] Any other 400 on a cataloged column -> invalid_request + the
      `pull_invalid_request_drift` signal.

## Orchestrator commands (BLOCKED for the dev agent -- no shell)

```bash
# Regen (byte-stability check of the committed artifacts):
uv run python server/modules/pinterest-ads/catalog_sources/build_official_fields.py
uv run python scripts/build_api_catalog.py --module pinterest-ads \
    --sources-dir server/modules/pinterest-ads/catalog_sources \
    --report server/modules/pinterest-ads/catalog_sources/fusion-report.json

# Tests:
uv run pytest server/tests/modules/pinterest_ads/ -v
uv run pytest server/tests/conformance/ --module-path server/modules/pinterest-ads/ -v
uv run pytest server/tests/conformance/test_all_module_capabilities.py -q
uv run ruff check server/modules/pinterest-ads server/tests/modules/pinterest_ads

# dbt (local gate, not CI):
(cd dbt && dbt parse)
```
