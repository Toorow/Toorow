# Google Business Profile connector — rollout notes

Module: `server/modules/google-business-profile/` · API: Performance API v1 (+ Reviews v4 legacy) · Kind: `kpi` · Auth: `google_direct` (direct Google OAuth, scope `business.manage`, NOT Nango — AD-21)
Research dossier: `_bmad-output/implementation-artifacts/research/google-business-profile-catalog-research.md` · Story: `30-1-connecteur-google-business-profile.md`

## Catalog generation

```bash
# official_fields.json is transcribed from the DailyMetric enum + v4/monthly schemas.
uv run python scripts/build_api_catalog.py --module google-business-profile \
    --sources-dir server/modules/google-business-profile/catalog_sources \
    --report server/modules/google-business-profile/catalog_sources/fusion-report.json
```

Fusion report (2026-08-04): `official_total=32`, `drift_ids=[]`, `exposure {exposed:31, planned:1}`. No enrichment source (GBP is not a Supermetrics source) — the official DailyMetric enum + v4/monthly schemas are the sole authority.

The single remaining `planned` field is `location_title` (the human label of a location, from Business Information). It is not extracted by any profile, and saying so is the point of the flag: 31 exposed / 1 planned is a statement about what this connector reaches, not a rounding.

*(Previously, 2026-07-21: `official_total=25`, `exposure {exposed:13, planned:12}` — reviews and monthly keywords were transcribed but unbuilt. Story 30.1 built them on 2026-08-04 and the exposures moved with the manifest, which is where the generator reads them from.)*

## Central design facts (surface to the client)

- **History EXISTS but is hard-capped at ~18 months** (daily). Data older than 18 months from the request date is unreachable, and there is no backfill beyond it — the connector must sync early and persist for anything longer (YoY). Recent days lag ~3–7 days; **zero-days are omitted** from `datedValues` (the pull leaves them NULL; gap-fill to 0 is a mart concern, AD-9).
- **0-QPM access gate.** A newly-enabled GCP project has **0 QPM** until Google manually approves an access/quota request (no sandbox); approved default is 300 QPM. The **v4 Reviews** host is additionally **allowlist-gated**. These are provisioning gates, not auth-flow changes — but they mean the connector is un-runnable (403) until granted.
- The 11 `DailyMetric` values are all **additive daily counts**. Total impressions = **sum of the 4 `business_impressions_*`** (Google exposes no single combined metric — computed downstream, never stored raw).

## Three profiles, three grains — and they never meet (Story 30.1, built 2026-08-04)

| Profile | Grain | Mart | The thing that must not be done to it |
| --- | --- | --- | --- |
| `location_daily` (core) | day × location | `fact_gbp_location_daily` | — (all 11 metrics additive) |
| `reviews` | review, rolled up to day × location | `fact_gbp_review_rollup` | **Never SUM a rating.** `new_reviews_avg_star_rating` averages 1..5 levels; `location_average_rating` / `location_total_review_count` are the provider's CURRENT levels, carried as-of `location_rating_observed_at`. Only `new_reviews` is additive. |
| `search_keywords_monthly` | month × location × keyword | `fact_gbp_search_keyword_monthly` | **Never turn a threshold into a count.** A row with `is_thresholded = true` has a NULL count and a FLOOR ("fewer than N"). Summing `search_keyword_impressions` returns the exact keywords only — a visible understatement, not a hidden one. |

- **The reviews v4 seam.** `mybusiness.googleapis.com/v4` is deprecated with **no v1 replacement** at research time, so the migration is a matter of when. It is contained in `connector._reviews_v4`: the URL shape, the 50-row page cap and the v4 field names live in that one class, and `test_reviews_v4.py` pins that they do.
- **The month is asked for, not inferred.** `searchkeywords.impressions.monthly.list` takes a monthly *range* and answers one aggregate per keyword with **no month in the response**. The profile therefore requests **one month at a time**. It costs one call per month per location (well inside 300 QPM once granted) and it is the only way `month` is an observation rather than an assumption.

## Access preconditions — a gate is not a failure

Three situations answer **403** and Google names none of them: the **0-QPM quota grant** has not landed, the **v4 allowlist** grant has not landed, or the caller is genuinely forbidden. All three classify as `permission_denied` (that part is pure HTTP and core decides it). What the module adds is the product reading:

- Every 403 from a **named gated surface** carries `precondition` + `precondition_message` on the typed error — that is what a UI reads to say *"Google access pending"* instead of *"permission denied"*. Read it with `connector.precondition_of(exc)`; the taxonomy gains **no sixth class**.
- A 403 on an **optional profile** (`reviews`, `social_post`, `search_keywords_monthly`) never leaves the connector: `pull_*` returns a skip envelope (`skipped`, `skip_reason`, `message`, `row_count: 0`) and the connection keeps working.
- A 403 on the **core daily profile still raises**. A pull that lands nothing is a failure, and reporting it as a success would fabricate an empty day. `permission_denied` is `retryable=False`, so there is no crash-loop either way.
- A **401 still raises** everywhere. The skip is for a grant that has not arrived, not for a credential that stopped working — swallowing it would hide a broken connection behind a profile quietly reporting zero rows forever.

## Design deviations / boundaries

- **Dedicated wide mart** `fact_gbp_location_daily` for v1. These metrics are additive and belong in the cross-source `fact_daily_kpi`, but wiring them there (new canonical metrics + `dim_metric.csv` rows) is a **follow-up**, deliberately deferred while the central seeds/mart carry open parallel-session merge conflicts (`google-analytics`, `linkedin-ads`, `shopify` files are mid-conflict — NOT introduced by this module). No central file touched.
- **Report pack deferred** (same reason): `reports/*.json` needs the metrics registered in `dim_metric.csv` for `report_dictionary.is_known_metric`.
- **error_map keyed on HTTP status** (standard Google error envelope, no numeric subcodes). 429 RESOURCE_EXHAUSTED → `RateLimitError` (breaker).
- **Topology account→location**; `discover_accounts` walks `accounts.list` → `accounts.locations.list`; the reporting entity is `locations/{id}`. No `*_LOCATION_ID` env var.

## Verification

`public_catalog.verification.status = "blocked"` — **doubly**: no GBP test account (2026-07-21) AND the default 0-QPM access gate. Ratify once a real GBP account with granted quota exists: probe `fetchMultiDailyMetricsTimeSeries` for one location over a 1-day window (all 11 metrics), then the v4 reviews + monthly keywords once allowlisted.

What is proven **without** an account, and what is not:

```bash
# The three transforms, the v4 seam, the precondition skips (no network, no DB).
cd server && python -m pytest tests/core/test_google_business_profile_connector.py \
    tests/modules/google_business_profile/ -q

# The three marts, built from the seed and queried (real dbt, real DuckDB).
cd server && python -m pytest tests/integration/test_seed_to_mart_loop.py -q
```

Not proven offline, and only a granted account can: that the provider's payloads have the shape transcribed here, that a real `threshold` reads as expected, and that the reviews allowlist behaves as the 403 branch assumes.
