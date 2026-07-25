# YouTube Analytics connector — rollout notes

Module: `server/modules/youtube-analytics/` · API: YouTube Analytics API v2 (reports.query) + Data API v3 (discovery + uploads playlist) · Kind: `kpi` (MIXED: also emits context_events via the `video_upload` profile -- Story 31.3) · Auth: `google_direct` (direct Google OAuth; scopes `yt-analytics.readonly` + `youtube.readonly`, optional `yt-analytics-monetary.readonly`; NOT Nango — AD-21)
Research dossier: `_bmad-output/implementation-artifacts/research/youtube-analytics-catalog-research.md` · Stories: `30-2-connecteur-youtube-analytics.md`, `31-3-youtube-video-upload-events.md`

## Epic-31 -- Mixed connector (Story 31.3)

YouTube is the **first mixed connector** in the toorow platform: it combines KPI profiles
(`channel_daily`, `video_daily` -> `fact_daily_kpi`) with an event profile
(`video_upload` -> `context_events`). This is the reference pattern for any connector
that emits both time-series metrics and temporal event markers.

### video_upload event profile

- **Source**: Data API v3 uploads playlist.
  `channels.list(part=contentDetails, mine=true)` -> `items[0].contentDetails.relatedPlaylists.uploads`
  -> `playlistItems.list(part=snippet, playlistId=<uploads>, maxResults=50)`, paginated.
- **No new scope**: `youtube.readonly` was already declared for `discover_accounts`.
  Quota cost: 1 unit per page (playlistItems), not 100 (search.list avoided).
- **Canonical identity**: `platform=youtube | event_type=video_upload | label=snippet.title`.
  `event_date` = `snippet.publishedAt` truncated to `YYYY-MM-DD` (UTC).
  `description` = `https://www.youtube.com/watch?v=<videoId>` for traceability.
  `value = None` (pulse unitaire -- no MMM magnitude needed at this stage).
- **Landing**: `context_events` (HG-2 respected -- the video_upload profile never
  writes `fact_daily_kpi`). The existing KPI profiles are unchanged.
- **`transform_events()`**: pure function (no I/O), unit-testable.
  Fixtures: `tests/fixtures/golden_events.json` (raw playlistItems) ->
  `tests/fixtures/expected_events.json` (canonical events post-mapping).
- **Dedup**: full_refresh insert. `persist_context_event()` inserts unconditionally
  (no unique constraint on `platform, event_type, event_date, label` in
  `app.context_events`). A re-pull of the same date range produces duplicate rows.
  Recommended practice: scope pulls to non-overlapping windows, or truncate the
  project's `video_upload` events before a full backfill. A dedup-on-insert
  DB constraint (`FR-dedup`) is a future enhancement -- documented, not silently
  dropped.
- **Incremental**: `mode=full_refresh` (the uploads playlist has no server-side date
  filter; the full playlist is paginated on every run). Cadence: daily. Low volume
  (typically tens to low hundreds of videos per channel).
- **Verification**: `status=blocked` -- no YouTube test account (2026-07-21).
  Ratify once a real channel connects: `channels.list(mine=true)` +
  `playlistItems.list` for a channel with >= 2 videos; confirm `context_events`
  rows are persisted with correct `event_date`, `label`, `description`.
- **`dim_event_type.csv`**: `video_upload` row was already present (Epic 31.1).
  `validate_event_type()` in `persist_context_event()` enforces the canonical
  vocabulary -- an unknown type raises a typed error, never a silent drop.

## Catalog generation

```bash
# official_fields.json is transcribed from the Analytics API metrics + dimensions references.
uv run python scripts/build_api_catalog.py --module youtube-analytics \
    --sources-dir server/modules/youtube-analytics/catalog_sources \
    --report server/modules/youtube-analytics/catalog_sources/fusion-report.json
```

Fusion report (2026-07-21): `official_total=50`, `drift_ids=[]`, `exposure {exposed:10, planned:40}`. No enrichment source (YouTube is not a Supermetrics source) — the official metrics/dimensions references are the sole authority.

## Central design facts

- **History-rich — the opposite of Strava/GBP.** Full lifetime data at daily grain (`dimensions=day`), complete from 2013-01-01. A full backfill is available at connect; only a trailing window needs re-pulling for late-settling data (activity ~2–3 days, revenue later). Demographics/geo/traffic rows are suppressed below privacy thresholds (gaps, not errors).
- **Ratio metrics never stored (AD-4).** `averageViewDuration` is derivable (`estimated_minutes_watched*60 / views`) and computed at the semantic layer, like CTR from clicks/impressions. Non-derivable ratios (`averageViewPercentage`, `cpm`, `*ClickRate`) stay planned; when extracted they carry `non_additive=true`.
- **Monetary gate.** Revenue + ad metrics require the `yt-analytics-monetary.readonly` scope, requested optionally in the same consent. Cataloged as planned.
- **columnHeaders mapping.** `reports.query` returns `{columnHeaders[], rows[[...]]}` — the connector maps values by header order, never positional guessing.

## Design deviations / boundaries

- **Dedicated long-format mart** `fact_youtube_daily` (grain date × channel × video × metric). These metrics are additive and belong in the cross-source `fact_daily_kpi`; wiring them there (new canonical metrics + `dim_metric.csv` rows) is a **follow-up**, deferred while the central seeds/mart carry open parallel-session merge conflicts (`google-analytics`, `linkedin-ads`, `shopify` files are mid-conflict — NOT introduced by this module). No central file touched.
- **Report pack deferred** (same reason): needs the metrics registered in `dim_metric.csv`.
- **Exposed = the additive core** (views, estimated_minutes_watched, likes, comments, shares, subscribers_gained/lost) + dims date/video/channel_id. Ratios, revenue/ads (monetary), playlist/cards/annotations, and the geo/device/traffic/demographic dimensions are cataloged as **planned** (exhaustive coverage; extraction widens tier by tier).
- **error_map keyed on HTTP status** (standard Google envelope). Critical shim (mirrors gsc/google): a **403 with reason `quotaExceeded`/`dailyLimitExceeded` is routed to `RateLimitError`** (breaker), not `permission_denied`; 429 likewise.
- **Topology = channel** (`ids=channel==MINE`); `discover_accounts` resolves it via Data API `channels.list(mine=true)`. Content-owner (MCN) multi-channel mode is a later extension. No `*_CHANNEL_ID` env var.

## Verification

`public_catalog.verification.status = "blocked"` — no YouTube test account (2026-07-21). Ratify once a real channel connects: `channels.list(mine=true)` + a 1-day `reports.query` per profile; probe the monetary metrics only with the monetary scope granted.
