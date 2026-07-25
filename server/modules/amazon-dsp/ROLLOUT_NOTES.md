# Amazon DSP — rollout notes

Amazon DSP is a fully separate connector from Sponsored Ads (`amazon-ads`). It owns its
own DSP seat, regional `adsAccountId`, advertiser selection, 11 report types, and an
autonomous 541-field catalog. It shares the same Login with Amazon (LwA) OAuth
infrastructure but has a distinct topology, distinct API paths, and a distinct Nango
integration.

## Nango configuration required (ACTION REQUIRED — orchestrator)

Auth is Nango-brokered (`auth_type: oauth2`, Login with Amazon):

1. **Nango provider key: `amazon-dsp`** — the provider string used in
   `nango_client.get_fresh_token(connection_id, provider="amazon-dsp")`.
   Enable the Nango Cloud `amazon-dsp` provider template for the toorow workspace.
   Scope: `advertising::campaign_management`.
2. **Multi-region token URL (probe-only decision point).** One LwA grant covers all
   three regions. Nango refreshes against a single token URL. Per the amazon-ads
   precedent, use the NA token URL for all regions:
   `https://api.amazon.com/auth/o2/token`
   Record the outcome here after the live probe (see Probe Checklist #2 below).
3. **Set platform env var `AMAZON_ADS_CLIENT_ID`** (the LwA security-profile client id,
   format `amzn1.application-oa2-client.xxx`) — it feeds the `Amazon-Ads-ClientId`
   header. Platform credential (never per-user, never in the manifest). Client secret
   lives in Nango only.
4. Amazon DSP API access must be granted to the LwA security profile (Amazon developer
   console — human step during onboarding).

## Regional API hosts

All three regional endpoints must be reachable. The connector fans out to all three
during discovery; report submissions and polling use the region matched to the
advertiser:

| Region | API host |
|--------|----------|
| NA | `https://advertising-api.amazon.com` |
| EU | `https://advertising-api-eu.amazon.com` |
| FE | `https://advertising-api-fe.amazon.com` |

These are already encoded in `REGIONAL_HOSTS` in `connector.py` and must be kept in
sync with the manifest `provider_api_hosts` field (fix F-5, to be applied separately).

## Backfill and refetch policy

`REFETCH_DAYS = (3, 14, 45)` — three refetch thresholds:

- **3 days**: DSP conversion data (purchases, sales) is typically finalized within 3
  days of the event date. Nightly pipelines re-pull the last 3 days to capture
  same-day and next-day attribution corrections.
- **14 days**: View-through attribution and some audience-based conversions can be
  restated up to 14 days after the impression. Weekly pipelines re-pull the last 14
  days to capture these late-arriving updates.
- **45 days**: DSP Sponsored Display and audience-overlap signals can undergo late
  restatement for up to 45 days (particularly ROAS and cross-device attribution).
  Monthly pipelines re-pull the last 45 days to ensure long-window attribution
  accuracy.

These thresholds match the `amazon-ads` refetch ladder (same LwA attribution model).
Report-type-specific backfill bounds (from `catalog_sources.json`) apply to the
initial pull window; the refetch ladder applies to subsequent pipeline re-pulls.

## Catalog contract

- **541 autonomous fields** from the DSP Reporting v3 public dictionary (Annex B of
  the curated dossier). Count is test-enforced; drift → `SystemExit`.
- **9 fields exposed** (safe core): `advertiserId`, `clicks`, `date`, `impressions`,
  `intervalEnd`, `intervalStart`, `purchases`, `sales`, `totalCost`.
- **532 fields excluded** with `probe-to-confirm: DSP seat required` — cannot be
  verified without a live DSP seat.
- **11 report types**: `dspCampaign`, `dspAudioAndVideo`, `dspAudience`,
  `dspBidAdjustment`, `dspBrandSuitability`, `dspGeo`, `dspInventory`, `dspProduct`,
  `dspReachFrequency`, `dspTech`, `dspBenchmarks`.
- `dspBenchmarks` is permanently blocked (non-additive benchmark grain).

## Probe-only verifications (live DSP seat required)

1. **Account discovery**: `GET /dsp/advertisers` endpoint returns the list of
   advertisers for the DSP seat. Cannot be verified mocked.
2. **Nango multi-region token refresh**: confirm NA-token refresh works for EU/FE
   advertisers (Airbyte behavior). If rejected, configure per-region Nango integrations
   (`amazon-dsp-na` / `amazon-dsp-eu` / `amazon-dsp-fe`).
3. **Report type compatibility**: each of the 11 report types must be confirmed live
   with the actual field set and groupBy options the DSP seat accepts.
4. **GZIP/JSON/CSV payload encoding**: presigned download URL content type varies by
   report type and region. Autodetect in `_download_report()` handles all three;
   confirm on live data.
5. **401/425/429 behavior**: bounded-poll 401 tolerance (2 retries), 425 report reuse,
   429 + Retry-After — all tested mocked; probe confirms real provider behavior.
6. **Refetch thresholds**: confirm 3/14/45 day windows are sufficient for the DSP
   attribution model used by the customer's advertiser.
7. **`dspBenchmarks` access**: the report type is blocked at `validate_selection()` and
   manifest profile availability. Probe confirms whether a DSP seat can request it
   (permanently blocked regardless — non-additive grain).

`verification.status: "blocked"` maintained in manifest and public_catalog until the
live probe completes.

## Catalog regeneration (deterministic, local-only)

```
uv run python server/modules/amazon-dsp/catalog_sources/build_official_fields.py
uv run python scripts/build_api_catalog.py --module amazon-dsp \
    --sources-dir server/modules/amazon-dsp/catalog_sources \
    --report server/modules/amazon-dsp/catalog_sources/fusion-report.json
CATALOG_GATE_MODE=fail uv run pytest server/tests/conformance/test_api_catalog.py -q
```

Count contract (test-enforced): **541 columns total**, 9 exposed / 532 excluded,
0 planned. The `CORE_EXPOSED` set in `build_official_fields.py` is now guarded: any
entry absent from the 541-field dictionary causes `SystemExit` at build time.

## Orchestrator verification commands

```
uv run pytest server/modules/amazon-dsp/tests/ -q
uv run pytest server/tests/conformance/ --module-path server/modules/amazon-dsp/ -v
CATALOG_GATE_MODE=fail uv run pytest server/tests/conformance/test_api_catalog.py -q
uv run ruff check server/modules/amazon-dsp server/modules/amazon-dsp/tests
cd dbt && dbt parse
uv run python scripts/export_connector_registry.py
```
