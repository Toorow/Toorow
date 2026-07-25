# Meta Ads — Standard Access Ramp Plan (AI-07)

This document addresses action item **AI-07**. It describes how the Meta Ads
module moves from **Development / Basic access** to **Standard Access** on the
Meta Marketing API. Content is derived from Meta's public developer
documentation. No confidential information is included.

> **Real Meta E2E test with live credentials is a HUMAN GATE.** All automated
> tests in this repo mock the Meta Marketing API via `respx` (HG-3). Running the
> module against a live ad account requires Jean's Meta App credentials.

## Current tier at story start

- **Development / Basic access.**
- Rate budget: **60 points / 5 minutes** (read = 1 pt, write = 3 pts). This is
  the profile declared in `manifest.json` → `quota` and enforced by the AD-12
  quota engine.
- Limited to **test ad accounts** and app admins/developers/testers. Test ad
  accounts can be created in Meta Business Manager (no real billing).

## Standard Access requirements

To be granted Standard Access to the Marketing API, an app typically needs:

1. **~1,500 successful API requests** accumulated by the app against the
   Marketing API. Meta tracks this on the App Dashboard.
2. **App Review submission** with a written use-case description.
3. **Business verification** of the owning business (if not already completed).
4. Typical **approval timeline: 1–4 weeks** after submission.

## Ramp procedure

1. Run the module in dev mode against a **test ad account** (create one in
   Business Manager if needed).
2. **Monitor the request count** in the Meta App Dashboard.
3. At **~1,200 successful requests**, prepare the App Review submission (leave
   headroom before the ~1,500 threshold).
4. **Submit the review** with:
   - Use case: "marketing analytics platform for clients".
   - Scopes / permissions requested: `ads_read`, `business_management`.
   - Privacy policy URL.
   - A short video demo of the integration.

## After Standard Access is granted

- **Standard Access grants ~9,000 points / 5 minutes** (read = 1 pt,
  write = 3 pts).
- Update `manifest.json` → `quota.budget_points` from `60` to `9000` (keep
  `window_seconds: 300`, `read_cost: 1`, `write_cost: 3`). No code change is
  required — the loader reads the quota block from the manifest and re-registers
  the platform budget with the quota engine.

## Circuit breaker role during the ramp

The AD-12 quota engine (`server/core/quota.py`) protects against accidental
quota burns during the ramp:

- In dev the budget is **60 pts / 300 s**; under Standard it becomes
  **9,000 pts / 300 s**.
- On an HTTP 429 from Meta, `pull()` raises `RateLimitError("meta-ads", retry_after)`,
  which trips the per-platform circuit breaker in the queue worker (Story 3.3).
- The breaker stays open for `max(retry_after, window_seconds)`, so the module
  never hammers the API past its rate window during the low-budget dev phase.

## Notes

- Marketing API version pinned in the module: **v20.0**
  (`https://graph.facebook.com/v20.0/`).
- The `conversions` metric at day grain is pixel-attributed (see
  `connector.py` `_extract_conversions`). Meta `conversions` and GA4
  `conversions` measure different things and are stored as **separate rows** in
  `fact_daily_kpi` (distinguished by `connector`). Cross-source dedup is
  Story 3.7's responsibility.
