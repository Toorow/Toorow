# Display & Video 360 rollout notes

The module independently pins Display & Video API v4 for read-only partner and
advertiser discovery and Bid Manager API v2 for saved-query reporting. The generated
catalog contains 19 curated filters and metrics, with zero planned fields.

Runtime reuses canonical saved queries, creates a query only when no matching title
exists, never deletes provider resources, and persists report references through the
shared asynchronous report core. GCS CSV artifacts land at full provider grain.

Public verification remains blocked until an authorized advertiser proves discovery,
permissions, two report families, saved-query reuse, deferred resume, GCS download,
timezone, currency/micros, quotas and redacted errors.
