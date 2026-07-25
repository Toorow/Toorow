# Strava connector — rollout notes

Module: `server/modules/strava/` · API: Strava API v3, **Clubs domain** · Kind: `kpi` · Auth: `oauth2` (Nango, scope `read`)
Research dossier: `_bmad-output/implementation-artifacts/research/strava-catalog-research.md` · Story: `29-1-connecteur-strava-clubs.md`

## Catalog generation

```bash
curl -sL https://developers.strava.com/swagger/swagger.json -o /tmp/strava-sources/swagger.json
curl -sL https://developers.strava.com/swagger/activity.json -o /tmp/strava-sources/activity.json   # SportType enum
# official_fields.json is transcribed from the DetailedClub/SummaryClub/ClubActivity/ClubAthlete/DetailedAthlete swagger models.
uv run python scripts/build_api_catalog.py --module strava \
    --sources-dir server/modules/strava/catalog_sources \
    --report server/modules/strava/catalog_sources/fusion-report.json
```

Fusion report (2026-07-21): `official_total=40`, `drift_ids=[]`, `enrichment_only=[]`, `exposure {exposed:13, planned:27}`. No enrichment source (Strava is not a Supermetrics source; `tap-strava` covers athlete/activities, not clubs) — the swagger is the sole authority, sufficient because the Clubs schema is static and small.

## What this connector proves — and the hard boundaries (surface to the client)

- **Snapshot-only, NO history.** `GET /clubs/{id}` returns the current value; there is no date parameter and no time-series endpoint. `member_count`/`following_count` are **non-additive point-in-time levels** (`aggregation_rule=latest`). The connector owns the series by snapshotting daily into `raw_strava_club_daily` → `fact_strava_club_snapshot`. **Any period before the first connection is unrecoverable; there is no backfill.** Growth = `member_count(t) − member_count(t−1)`, derived downstream, null on the first snapshot.
- **Competitor = PUBLIC clubs only, headline metrics only.** `GET /clubs/{id}` does not require membership, so any public club id is snapshotable. But: private/absent clubs return **404** (recorded `unreachable`, skipped with an alert — never a fabricated zero); there is **no club search endpoint** (competitor ids are per-project config, resolved once from `strava.com/clubs/<slug>`); and `members`/`admins`/`activities` are **membership-gated** (own clubs only) and **anonymized** (first name + last initial, no athlete id). For a competitor you get `member_count` + `following_count` + metadata, **nothing else**.
- **Own vs competitor** separated by the stamped `is_own_club` dimension.

## Design deviations from the KPI template (intentional)

- **Dedicated mart** `fact_strava_club_snapshot` instead of the additive cross-source `fact_daily_kpi`. A non-additive level has no honest `SUM` row in the central mart — exactly the GSC `average_position` precedent (which lives in a semantic view). The central `fact_daily_kpi.sql` and the `dim_metric.csv`/`metric_source_priority.csv` seeds are therefore **untouched**.
- **Competitor ids = per-project config**, NOT `account_topology`. Strava has no club-list endpoint, so competitor clubs are arbitrary public ids the token can read but does not own. `account_topology` covers only the connected athlete's **own** clubs (`discover_accounts` → `GET /athlete/clubs`). Competitor ids flow in via the project-config seam (`strava_competitor_club_ids`) — no `*_CLUB_ID` env var (doctrine).
- **error_map keyed on HTTP status only** (Fault `code` is a free-form string). Two connector shims in `_raise_for_status` / `_fetch_club`: (a) a 403 whose Fault `code=='exceeded'` → `RateLimitError` (breaker), not `permission_denied`; (b) a 404 on the competitor path → skip + alert, never raised.

## Open decision (recorded in the story, defaulted here)

`own_club_full` persists the **club snapshot** only; the anonymized members/admins/activities feeds are **cataloged as `planned`** and intended as on-demand MCP reads, **not persisted** in v1 (privacy-lean: anonymized PII with no join value). Flip to persistence only if a client explicitly needs roster/activity history — dedup would then be best-effort/lossy on `(club_id, snapshot_date, firstname, lastinitial, name, distance)`.

## Known follow-ups

- **Report card (`reports/daily_summary.json`) deferred.** The optional report pack is not shipped yet: `member_count`/`following_count` must first be registered in the central `dbt/seeds/dim_metric.csv` (as `additive=false, aggregation_rule=latest`) so `report_dictionary.is_known_metric` recognizes them. That seed is deliberately left untouched here to avoid colliding with the several parallel-session merge conflicts currently open in the working tree (`google-analytics`, `linkedin-ads`, `shopify` catalog/manifest files are mid-conflict — NOT introduced by this module). Add the two rows + the report pack once the tree is clean.
- The MCP tool `get_strava_report` and both pull profiles are fully functional without the report pack.

## Verification

`public_catalog.verification.status = "blocked"` (`live_evidence_not_ratified`). No Strava test account (2026-07-21). Ratify once a connection exists: probe one known **public** club id for the full DetailedClub field set; `own_club_full` requires the connecting athlete to belong to ≥1 club. A deliberately-private/absent id must classify as `unreachable` (404 skip), confirming the competitor boundary.
