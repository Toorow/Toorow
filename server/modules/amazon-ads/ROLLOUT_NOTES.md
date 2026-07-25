# amazon-ads — rollout notes

## Story 26.5 — Reporting v3 async connector (3 regions, 742-column catalog)

Everything in this module is mocked-only (respx + fake-clock socle stores);
`public_catalog.verification` stays `blocked` until the 25.6 live probe.

## Nango configuration required (ACTION REQUIRED — orchestrator)

Auth is Nango-brokered (`auth_type: oauth2`, Login with Amazon):

1. Enable the Nango Cloud `amazon-ads` provider template for the toorow
   workspace. Scope: `advertising::campaign_management` (legacy pre-2020 LwA
   clients would use `cpc_advertising:campaign_management` — not our case).
2. **Multi-region token URLs (probe-only decision point).** The official docs
   prescribe REGION-MATCHED token endpoints:
   - NA `https://api.amazon.com/auth/o2/token`
   - EU `https://api.amazon.co.uk/auth/o2/token`
   - FE `https://api.amazon.co.jp/auth/o2/token`

   One LwA grant covers all three regions, but the Nango provider template
   refreshes against a single token URL. Airbyte refreshes EVERY region
   against the NA URL and works in practice (dossier section 1.3 divergence).
   Options, in preference order:
   - a) verify at probe time that NA-token refresh works for EU/FE profiles
     (Airbyte behavior) and keep ONE Nango integration;
   - b) if refresh is rejected for EU/FE, configure one Nango integration per
     region (`amazon-ads-na` / `amazon-ads-eu` / `amazon-ads-fe`) and connect
     the customer on the region of their primary marketplace — discovery
     still fans out over the 3 API hosts with the same token.

   Record the outcome here after the probe.
3. Set the platform env var `AMAZON_ADS_CLIENT_ID` (the LwA security-profile
   client id, format `amzn1.application-oa2-client.xxx`) — it feeds the
   `Amazon-Ads-ClientId` header. It is a PLATFORM credential (never per-user,
   never in the manifest). The client secret lives in Nango only.
4. Amazon Ads API access must be assigned to the LwA security profile during
   onboarding (Amazon developer console — human step).

## Probe-only verifications (25.6 live probe — cannot be verified mocked)

1. **The 3 page-vs-dictionary divergences** (dossier section 5 drift):
   - `linkOuts` — dictionary lists it for sdCampaigns/sbCampaigns/stCampaigns
     but NO report-type page lists it: cataloged `excluded:
     probe-to-confirm`. Probe: request it on sdCampaigns; flip to `exposed`
     if accepted.
   - sdTargeting video family (`videoFirstQuartileViews`,
     `videoMidpointViews`, `videoThirdQuartileViews`, `videoUnmutes`,
     `viewabilityRate`, ...) — dictionary includes them for sdTargeting, the
     sdTargeting PAGE omits them; Airbyte requests them live successfully.
     They are EXPOSED and allowed in the sdTargeting selectable_set
     (dictionary authority); probe confirms.
   - `targeting` on `spPurchasedProduct` — the dictionary scopes `targeting`
     to spTargeting/spSearchTerm only, but Airbyte ships it on the
     spPurchasedProduct asins_targets stream and it works live. The module
     REFUSES it pre-call on spPurchasedProduct (dictionary authority, strict);
     probe decides whether to widen the selectable_set.
2. **`longTermSales` / `longTermROAS` on sb/sd/stCampaigns (F-3, page-only).**
   Documented on the sbCampaigns / sdCampaigns / stCampaigns report-type pages
   (groupBy `campaign` additional metrics) AND on dspCampaign (Annex A), but
   ABSENT from the 740-column dictionary. Cataloged `excluded:
   probe-to-confirm` per the dossier section-4 "dictionary OR page" rule (that
   is why the catalog is 742, not 740). Probe: request each on sbCampaigns /
   sdCampaigns / stCampaigns groupBy `campaign`; confirm the real physical type
   (assumed Decimal, page-context-derived) and flip to `exposed` if accepted.
3. **Page-vs-dictionary MEMBERSHIP deltas to bisect** (dossier section 4 —
   ratification-harness bisection; the catalog keeps the UNION and the probe
   settles each contested column). Per report type, dictionary count vs pages
   union: `spTargeting` 59/60, `spPurchasedProduct` 46/49, `sbCampaigns`
   63/65, `sdCampaigns` 69/71, `sdAdvertisedProduct` 68/71, `stCampaigns`
   53/56, `stTargeting` 55/57, `sbAudiences` 21/22, `spAudiences` (dictionary
   `spGlobalAudiences` 41) / 42, plus `sbTargeting` 71/61 (dictionary WIDER
   than the page — video/viewability columns the page omits) and `sdTargeting`
   68/70. Bisect each delta live; record which side wins per column.
4. **`spGrossAndInvalids` 365-day window/retention (F-4).** The connector now
   refuses windows by the PER-TYPE bound: 31 days for most types but 365 for
   `spGrossAndInvalids` (max range AND retention), 731 for `sbPurchasedProduct`,
   90 for the prompt-ad-extension types. Probe: submit a >31-day
   `spGrossAndInvalids` window (e.g. 90 days) and confirm it is accepted (the
   uniform 31-day cap would have wrongly refused it). `sb/sdGrossAndInvalids`
   are ABSENT from the section-2 table — kept CONSERVATIVE (31 + product floor)
   pending the probe (they MAY be wider like the SP gross shape).
5. **Real `failureReason` vocabulary (F-9).** The FAILED-report configuration
   markers were tightened to the documented forms only (`column` / `groupBy` /
   `filter` + the exact documented phrases: KDP / "Not authorized to access
   scope" / "Tactic T00020..." / "Report date is too far in the past"). Capture
   the ACTUAL free-text `failureReason` strings the provider returns and extend
   `_FAILURE_CONFIG_MARKERS` if a genuine configuration failure uses a phrase
   not yet listed (an unlisted reason takes the resubmit-once →
   provider_transient route — never a silent success).
6. **Re-poll a COMPLETED reportId recovered via 425 → fresh S3 URL (F-6).** A
   residual duplicate-report 425 reuses the reportId from the 425 BODY (the
   store re-read branch was removed as unreachable). Probe: force a duplicate
   submit, take the 425's `reportId`, poll it to COMPLETED, and confirm the
   download URL it yields is a FRESH pre-signed S3 link (a stale/expired link
   takes the DownloadLinkExpired → single-resubmission path). Also confirm the
   425 body actually carries `reportId` (if it does not, only the socle
   store-based resume recovers — see item below).
7. **Real report-generation latencies** per report type and region (official
   FAQ says up to 3 h; the per-run deadline + deferred/resume path absorbs
   this, but the poll cadence should be tuned on evidence).
8. **Dynamic regional 429 tiers**: no published fixed limits; reporting
   endpoints throttle on regional report-queue size. Capture observed
   Retry-After distributions per region to calibrate the manifest quota
   block (currently 30 points/min, write_cost 3 — deliberately conservative).
   Discovery re-raises a 429 IMMEDIATELY (F-7, breaker) rather than tolerating
   it per host — confirm the throttle is account-wide across the 3 hosts.
9. **`profileTypeFilter=seller,vendor` vs agency accounts.** Discovery pins
   `apiProgram=report&accessLevel=view&profileTypeFilter=seller,vendor`
   (Airbyte parity), so `type: agency` profiles (DSP/Data-Provider facing) are
   NOT returned. Probe on an agency-managed account: confirm the seller/vendor
   advertiser profiles still surface and whether any needed profile is hidden
   by the filter (widen only if a real gap is observed).
10. **Nango multi-region token refresh** (see above).
11. **425 response body shape**: whether the duplicate-report 425 carries the
    existing `reportId` (the submit callable reuses it when present).
12. **SB v3 preview gap**: campaigns with `isMultiAdGroupsEnabled=False` are
    absent from v3 SB data until GA — DQ volume monitors must not read the gap
    as connector failure (dossier section 9.5).

## Catalog regeneration (deterministic, local-only)

```
uv run python server/modules/amazon-ads/catalog_sources/build_official_fields.py
uv run python scripts/build_api_catalog.py --module amazon-ads \
    --sources-dir server/modules/amazon-ads/catalog_sources \
    --report server/modules/amazon-ads/catalog_sources/fusion-report.json
CATALOG_GATE_MODE=fail uv run pytest server/tests/conformance/test_api_catalog.py -q
```

Counts contract (test-enforced): **742 columns total** = the 740-column Annex B
dictionary + 2 page-only columns (`longTermSales` / `longTermROAS`, F-3, dossier
section-4 "dictionary OR page" rule). **202 exposed / 540 excluded** (486
`dsp-seat-gated` + 51 non-daily + 1 `linkOuts` probe + 2 page-only probe); zero
`planned`. Per ad product SP 95 / SB 152 / SD 91 / ST 60 / DSP 541 — these stay
at the Annex B DICTIONARY values because page-only columns are deliberately kept
OUT of `report_type_columns.json` (they are refused at the excluded-columns gate,
never made field_compat-selectable). DSP-only columns excluded `dsp-seat-gated`
(epic-26 open question #2 — flip when a DSP seat exists); conversionPath/
benchmarks-only columns excluded (non-daily shape); `leads` / `leadFormOpens`
are Airbyte v2 leftovers ABSENT from the official dictionary and are deliberately
NOT cataloged.

## Orchestrator verification commands

```
uv run pytest server/tests/modules/amazon_ads/ -q
uv run pytest server/tests/conformance/ --module-path server/modules/amazon-ads/ -v
CATALOG_GATE_MODE=fail uv run pytest server/tests/conformance/test_api_catalog.py -q
uv run ruff check server/modules/amazon-ads server/tests/modules/amazon_ads
cd dbt && dbt parse
uv run python scripts/export_connector_registry.py
```
