# Adobe Analytics rollout notes

The committed catalog is the Analytics 2.0 schema baseline. Runtime snapshots are
keyed by `(global_company_id, rsid)` and contain reportable dimensions, metrics,
calculated metrics and segments. A component from one suite is never reused for
another suite. OAuth Server-to-Server is the default; delegated User OAuth is an
explicit alternative. JWT, SOAP and API 1.4 are forbidden.

Public verification remains blocked until an authorized Adobe product profile proves
discovery, suite-specific drift, pagination, bounded breakdowns, HTTP 206 handling,
timezone, quota and superseding at the full grain.
