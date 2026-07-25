# CHANGELOG

All notable changes to toorow are recorded here.

## Convention

- One entry per deploy / tag.
- Tags follow the `vYYYY.MM.DD` scheme (e.g. `v2026.07.12`).
  If multiple deploys occur on the same day, append a counter: `v2026.07.12-2`.
- Every future deploy or tag MUST add a CHANGELOG line before merging.
- Format: `YYYY-MM-DD -- <summary>` followed by bullet points as needed.

---

## 2026-07-12 -- Phase A complete (7 epics / 44 stories), global gap review + fixes batch

- Phase A delivery: Epics 1-7 complete (44 stories), covering MCP server skeleton,
  Nango OAuth, auth layer, admin console, data pipeline, queue, alerting, anomaly
  detection, tracing, report pack, multi-tenancy, per-tenant encryption, and
  cross-project isolation.
- Global gap review applied: ops/deployment fixes (Dockerfile HEALTHCHECK, uv pin,
  deploy workflow CI-gate + prod auth hardening, .env.example auth default,
  infra/README CI table, CONTRIBUTING runbook, CHANGELOG bootstrap).
- See full findings and rationale in:
  `_bmad-output/implementation-artifacts/reviews/review-global-gaps.md`
