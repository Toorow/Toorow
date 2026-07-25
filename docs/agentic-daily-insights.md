# Agentic daily insights

Status: proposed exploration (Epic 35), not a ratified runtime contract.
Last reviewed: 2026-07-22.

## Purpose

Explore whether a scheduled task in an LLM client can research a project's fresh
toorow data, construct a bounded card, and publish the resulting insight as durable
JSON for the project team.

The intended outcome is a project-scoped “Daily insights” inbox containing zero to
three evidence-backed cards per day. The published insight must survive the original
chat/task, remain reproducible, and be shareable without granting access to live data.

## Boundary with the web report builder

The admin web report builder configures data ingestion: report, fields, grain,
history, cadence, extraction plan and mapping plan. It remains the only product
surface for that operator-owned configuration.

The proposed agent builder works after publication. It may select or compose a
presentation over already governed datasets. It cannot create Datastreams, change
mappings, issue arbitrary SQL, or introduce new semantic calculations.

| Web report builder | Proposed LLM card builder |
|---|---|
| Operator configures what data enters toorow | Agent investigates data already present |
| Persists versioned extraction/mapping plans | Persists a frozen insight/card artifact |
| Admin web is the construction surface | MCP tools + JSON schema are the construction surface |
| Changes future ingestion | Never changes facts or ingestion |

## Existing foundation

Toorow already exposes dual-channel report/card tools, governed context, DQ,
provenance, a card registry, a typed composition renderer, render snapshots, a
gallery, tokenized frozen sharing, feedback and audit. Epic 35 must reuse them.

The first spike compares two implementations:

- **A — select:** the LLM supplies existing `get_card` keys and promotes the final
  render snapshot.
- **B — compose:** the LLM supplies a versioned declarative card spec made only from
  server-advertised primitives and bindings.

Arbitrary code, HTML, CSS, external URLs and SQL are out of scope.

## Candidate workflow

1. A client-hosted scheduled task targets project date J-1.
2. A readiness tool verifies expected datastream completion, freshness and DQ.
3. The agent reads the daily report, alerts, context and relevant procedures.
4. It forms several hypotheses and drills into only the strongest within a call budget.
5. It selects zero to three non-redundant insights.
6. It previews a card via A or B.
7. The server validates schema, bindings, evidence, freshness, budgets and project scope.
8. A write tool publishes the complete frozen JSON atomically.
9. Project members see the artifact in the Daily insights inbox and may share a frozen link.

“No meaningful insight” is a valid result. “Task did not run”, “data not ready”, and
“no insight” must remain distinct states.

## Published artifact

The durable payload contains:

- an editorial insight: title, observation, why it matters, suggested next check,
  confidence and limitations;
- the analyzed period and comparison basis;
- evidence references, pull IDs, freshness and DQ state resolved by the server;
- the card contract and the fully resolved frozen envelope;
- widget URI, branding, trace, publisher identity, prompt/contract versions and hash.

PostgreSQL owns the published insight object. The warehouse remains the source of
facts and is never queried when a frozen share link is opened.

Candidate persistence:

- `app.daily_insight_runs`: one run per project/date with status and coverage;
- `app.daily_insights`: up to three slots, autonomous `payload JSONB`, hash and render
  snapshot lineage.

The JSONB intentionally freezes the resolved envelope even though render snapshots
exist: snapshot retention is bounded, while a team insight is a durable business artifact.

## Trust and security rules

- The model selects and explains; the server resolves every number.
- Every evidence reference must resolve inside server-produced data.
- Stale or incomplete J-1 data blocks publication or is explicitly published as a
  data-quality insight; it is never treated as current.
- Ratios and non-additive metrics use the semantic layer, never LLM arithmetic.
- Publication is project-scoped, idempotent per date/slot, audited and fail-closed.
- Team access follows existing project membership. External sharing is frozen,
  tokenized, revocable and never a grant over live data.
- A client LLM receives no provider credentials and cannot mutate ingestion.

## Scheduling ownership

The initial schedule belongs to the LLM client, not toorow. Toorow supplies a
copyable task recipe (project, timezone, target date, call budget, focus areas and
publication contract) plus readiness and publication tools.

This avoids model-provider keys, inference billing and agent orchestration in the
toorow backend until the product value is proven. Host support for scheduled MCP
reads and unattended write approval is a mandatory spike result.

## Decision gate

Epic 35 starts with Story 35.0. No implementation story starts until it produces:

- 14-day historical replay plus a live scheduled run;
- an A/B comparison of selection versus declarative composition;
- golden JSON fixtures and a proposed versioned schema;
- measured usefulness, repetition, validity, tool/token cost and unsupported claims;
- a decision: A only, B, or stop and keep the deterministic morning briefing.

The declarative builder is justified only if it materially outperforms selection of
existing cards. Otherwise the smallest correct product is preview + promote.

## Canonical planning reference

See `_bmad-output/planning-artifacts/epic-35-insights-agentiques-quotidiens.md` for
stories, gates, acceptance thresholds, persistence and open decisions.
