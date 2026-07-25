# thetradedesk -- rollout notes

## Story 28.2 -- The Trade Desk MyReports v3 async connector (275-field DRAFT catalog)

Everything in this module is mocked-only (respx + fake-clock socle stores);
`public_catalog.verification` stays `blocked` until the live probe. The catalog
is **DRAFT**: the field vocabulary is the Supermetrics 2026-04-23 snapshot (275
fields, Annex A of the research dossier). The TRUE field authority is the live
ReportTemplate facet enum -- transcribe it at the probe and diff against this
baseline.

## Auth / credentials (ACTION REQUIRED -- orchestrator)

Auth is `POST /v3/authentication {Login, Password}` -> `{Token,
ExpirationDateUtc}`; send `TTD-Auth: <token>` on every call. Set the PLATFORM
env vars (never per-user, never in the manifest):

1. `THETRADEDESK_API_LOGIN` / `THETRADEDESK_API_PASSWORD` -- the API user/secret.
   (Probe decision: a long-lived token generated once in the partner portal is
   an alternative to Login/Password -- decide Nango vs platform-api_key path at
   probe. The module currently authenticates with Login/Password and caches the
   token until `ExpirationDateUtc`.)
2. `THETRADEDESK_PARTNER_ID` -- the partner seat the credential is scoped to
   (feeds `discover_accounts`; the advertiser is selected by the topology flow,
   NOT an env var -- `THETRADEDESK_*_ID` account vars are forbidden by doctrine).
3. Amazon Ads-style: the client secret / API credential is a platform secret,
   never logged.

## Probe-only verifications (cannot be verified mocked) -- SETTLE THE DRAFT

1. **ReportTemplate facet enum (THE field authority).** FIRST probe action:
   `GET /v3/myreports/reportschedule/facets` -- transcribe the real dimension /
   metric enum VERBATIM and diff against the 275-field Supermetrics baseline
   (Annex A). Known probe-to-confirm deltas:
   - `Selling party name` / `Selling party ID` are the NEW columns superseding
     the deprecated `SupplyVendor` / `SupplyVendorIntegerID` (LEGACY section).
     They are NOT in the Supermetrics snapshot yet (snapshot lag) -> ADD them if
     the facet enum lists them (inverted drift: the provider is ahead of the
     enrichment source; extend Annex A + re-run the generator, never drop).
   - Any facet-only field absent from Annex A -> add it (never a silent drop).
   Promote the catalog out of DRAFT (`catalog_sources.json._status`) once the
   facet enum is transcribed and diffed.
2. **The reference managed ReportTemplate(s).** MyReports is NOT
   create-on-the-fly: a ReportTemplate (dimensions+metrics+filters) must
   pre-exist (My Reports app UI or a cloned predefined template). Confirm the
   API `POST /v3/myreports/reportschedule` can reference a template id/name
   without a UI step (Datorama says UI-first). Capture the real
   `ReportScheduleId` / `ReportTemplateName` shape and the schedule body field
   names (`ReportTemplateName` / `AdvertiserFilters` / `Configuration.Dimensions
   |Metrics` are the module's best-effort names -- align to the real body).
3. **Token TTL / refresh behaviour.** The exact `ExpirationDateUtc` cadence and
   sliding-window behaviour are UNDOCUMENTED (commonly a 90-day window). Confirm
   the proactive-refresh margin (`_TOKEN_REFRESH_MARGIN_SECONDS = 60`) and
   whether a 401 mid-flow always means an expired token (the flow re-auths once
   on a 401 before classifying).
4. **Rate limits + 429 behaviour.** UNDOCUMENTED per partner/advertiser; no
   published `Retry-After`. The module raises `core.quota.RateLimitError` with
   `retry_after=None` (default backoff) and the manifest quota block is
   deliberately CONSERVATIVE (20 points/min, report creation = write cost 3).
   Capture the observed throttle to calibrate; a breaker is in place because the
   limits are undocumented.
5. **Attribution / restatement windows.** Conversion families
   (click/view-through/touch/time-weighted-decay, pixels 01-06) restate as
   attribution windows close. The exact window is ReportTemplate-configured (set
   in the My Reports UI), NOT a global API constant -- confirm the real windows;
   the refetch ladder is nightly 3 / weekly 14 / monthly 45.
6. **Download URL TTL + format.** `ReportDeliveries[].DownloadURL` is a
   pre-signed link (~1 h TTL). Confirm CSV vs gzip vs JSON and whether executions
   are paginated. The download callable handles gzip-magic + JSON-array + CSV
   (`_parse_report_payload`); align to the real format.
7. **ReportExecutionState / failureReason vocabulary.** The poll maps
   `Pending/Queued/Scheduled -> pending`, `Running/Processing/Generating ->
   processing`, `Complete -> completed`, `Failed -> expired` (single socle
   resubmission) UNLESS the failureReason is configuration
   (`invalid`/`not supported`/`unknown dimension`/`unknown metric`/`template` ->
   invalid_request). Capture the ACTUAL state strings and free-text
   failureReason vocabulary and extend `_FAILURE_CONFIG_MARKERS` if a genuine
   configuration failure uses an unlisted phrase (an unlisted reason takes the
   resubmit-once -> provider_transient route -- never a silent success).
8. **Advertiser discovery shape.** `POST /v3/advertiser/query/partner` result
   key (`Result` vs `results`) and advertiser field names (`AdvertiserId`,
   `AdvertiserName`, `CurrencyCode`) are best-effort -- confirm at probe.
9. **reportschedule body field names (review 28.2 -- probe-only).** Every field
   name the module writes in `_build_schedule_body` is a best-effort transcription
   and MUST be confirmed VERBATIM against the live `POST
   /v3/myreports/reportschedule` contract before the DRAFT is promoted:
   - `AdvertiserFilters` (the advertiser-scope filter key -- may be
     `AdvertiserIds` / a nested filter object);
   - `ReportStartDateInclusive` / `ReportEndDateExclusive` (the rolling-window
     bounds -- confirm inclusivity/exclusivity semantics, not just the names);
   - `Configuration.Dimensions` / `Configuration.Metrics` (the column split
     container -- may be flat `Dimensions`/`Metrics` or `Columns`);
   - `ReportFrequency` (`"Once"`) and `TimeframeType` (`"Custom"`) -- confirm the
     enum values a one-off custom-window schedule requires.
   A wrong key here is a silent no-op filter (whole-partner data) or a 400 --
   align the body to the real contract at probe.
10. **`date` force-injected as a dimension (review 28.2 -- probe-only).**
   `_build_schedule_body` force-inserts `date` as the first dimension (daily
   grain requires it). Validate `date` is the REAL facet-enum id for the daily
   time dimension (TTD may expose it as `Date` / `ReportDate` / a
   time-granularity facet rather than a plain dimension). If the facet id
   differs, map it in the generator -- never ship a force-injected id the enum
   rejects.
11. **poll result key + ReportExecutionState casing (review 28.2 -- probe-only).**
   `poll` reads `payload.get("Result") or payload.get("results")` and lowercases
   `ReportExecutionState` before matching. Confirm at probe: (a) the real result
   key of `POST /v3/myreports/reportexecution/query/advertisers` (`Result` vs
   `results` vs `ReportExecutions`), and (b) the exact CASING of the state
   strings (`Complete`/`Pending`/`Running`/`Failed`) -- the module lower-cases so
   casing drift is tolerated, but a renamed key or an unlisted state string would
   fall through to the `unknown ReportExecutionState` typed refusal.

## Catalog regeneration (deterministic, local-only)

```
uv run python server/modules/thetradedesk/catalog_sources/build_official_fields.py
uv run python scripts/build_api_catalog.py --module thetradedesk \
    --sources-dir server/modules/thetradedesk/catalog_sources \
    --report server/modules/thetradedesk/catalog_sources/fusion-report.json
CATALOG_GATE_MODE=fail uv run pytest server/tests/conformance/test_api_catalog.py -q
```

Counts contract (test-enforced): **275 fields total** = the Annex A Supermetrics
catalog (164 dimensions + 111 metrics, 22 sections, ZERO truncation). **263
exposed / 12 excluded** (the 12 DATASOURCE/QUERY `system_metadata.*` /
`dataSourceName` fields excluded `enrichment-only`); **ZERO planned**. The 14
DEPRECATED (10) + LEGACY (4) TTD columns stay `exposed` with a deprecated note
(historical windows still query them). 7 NON-ADDITIVE ratio metrics
(`Cpm`/`Cpc`/`Ctr`/`CpaClick`/`CpaView`/`CpaTouch`/`CpaTimeWeightedDecay`) carry
the AD-4 note; `PlayerRewind` is an ADDITIVE count (not mis-tagged).

## Orchestrator verification commands

```
uv run pytest server/tests/modules/thetradedesk/ -q
uv run pytest server/tests/conformance/ --module-path server/modules/thetradedesk/ -v
CATALOG_GATE_MODE=fail uv run pytest server/tests/conformance/test_api_catalog.py -q
uv run ruff check server/modules/thetradedesk server/tests/modules/thetradedesk
cd dbt && dbt parse
uv run python scripts/export_connector_registry.py
```
