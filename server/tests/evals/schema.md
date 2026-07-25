# Evaluation / Non-Regression Benchmark Corpus Schema (Story 14.1)

This document defines the strict schema for `server/tests/evals/corpus.yaml`.

> **AD-17 Boundary Note:** All records in `corpus.yaml` are **TEST CODE**, not context or domain knowledge. They exist solely to evaluate agent accuracy and citation rate in CI. They must NEVER be loaded into `app.context_events`, `app.knowledge_cards`, or any application context table.

## Corpus Header

The root of `corpus.yaml` is a mapping with top-level metadata and a `questions` array:

```yaml
schema_version: "1"
as_of_anchor: "2026-07-15"
seeds_commit: "a6b3c60"
dbt_build_commit: "a6b3c60"
created_by: "human+agent"
questions:
  - ... # list of question records
```

## Question Record Schema

Each item in `questions` must conform to the following fields:

| Field | Type | Required | Description / Rules |
| ----- | ---- | -------- | ------------------- |
| `id` | string | Yes | Unique slug in kebab-case / snake_case (`^[a-z0-9_]+$`) |
| `question` | string | Yes | Natural language query (French for FR surfaces, English permitted for expert/ad-hoc) |
| `as_of` | string | Yes | Canonical ISO date (`YYYY-MM-DD`) anchoring all relative windows |
| `surface` | string | Yes | Enum: `daily_report`, `expert_report`, `card`, `card_catalog`, `dq`, `as_of`, `grain_trap` |
| `difficulty` | string | Yes | Enum: `easy`, `medium`, `hard` |
| `tags` | list[string] | Yes | Keywords (e.g. `ga4`, `sessions`, `additive`, `grain_trap`) |
| `reference_queries` | list[QueryEntry] | Yes | 1..N query entries (see below) |
| `expected_citations` | list[Citation] | Yes | List of expected citations (`[]` for write-ack / catalog tools) |
| `grain_trap_note` | string | Conditional | Required when `surface == "grain_trap"` |
| `procedure_ref` | string | No | Optional tool call signature reference |
| `tool_invocation` | ToolInvocation \| absent | No | Story 14.2 addendum: a deterministic MCP tool call + result selector, present ONLY when the question resolves mechanically (see below). Absent => the runner marks it `tool_replay: skipped`. |
| `updated_at` | string | Yes | ISO 8601 timestamp |

### ToolInvocation Sub-schema (`tool_invocation`) — Story 14.2

Added by `build_eval_corpus_and_fixtures.py::derive_tool_invocation` as a **mechanical** addendum (AD-17 unchanged: still TEST CODE, never loaded into context tables). It is emitted ONLY when the question's `canonical_correct` `reference_sql` is a plain `fact_daily_kpi` aggregation the `get_daily_report` seam can reproduce EXACTLY: a single `connector`, a single `metric`, a **pinned** `breakdown_dimension`, `project_id = 'default'`, a date window, and a `SUM(value)` shape. Otherwise the field is **omitted** (the runner counts the question as `tool_replay: skipped`, never a false green). Regen is byte-stable and idempotent.

| Field | Type | Required | Description / Rules |
| ----- | ---- | -------- | ------------------- |
| `tool` | string | Yes | MCP tool name. Enum (v1): `get_daily_report`. |
| `args` | mapping | Yes | Tool arguments: `project_id` (`default`), `connectors` (list, 1 entry), `date_range` (`{start, end}` ISO dates), optional `as_of` (ISO-8601 datetime, imposed for replay surfaces). |
| `result_selector` | ResultSelector | Yes | How to extract the comparable value(s) from `structuredContent` (see below). |

#### ResultSelector vocabulary

| Field | Type | Required | Description / Rules |
| ----- | ---- | -------- | ------------------- |
| `kind` | string | Yes | Enum: `fact_sum` (one scalar = `SUM(value)` over rows matching connector+metric+breakdown_dimension) or `fact_sum_by_date` (ordered `[(date, value)]` grouped by date over the same filter). |
| `connector` | string | Yes | Connector to filter `data.rows` by. |
| `metric` | string | Yes | Metric to filter by. |
| `breakdown_dimension` | string | Yes | The **pinned** grain — the runner re-aggregates on THIS dimension only, so a naive sum over all breakdowns (a grain trap) can never masquerade as correct. |
| `round` | int \| null | Yes | Mirrors the reference `ROUND(.,N)`. `null` => whole aggregate, EXACT equality. An int => bounded float tolerance. |

### QueryEntry Sub-schema (`reference_queries[*]`)

| Field | Type | Required | Description / Rules |
| ----- | ---- | -------- | ------------------- |
| `role` | string | No | Enum: `canonical_correct`, `naive_wrong`, `supplementary`. Required when `surface == "grain_trap"` |
| `note` | string | No | Rationale or description for this specific query |
| `reference_sql` | string | Yes | Executable SQL against DuckDB marts (`marts.fact_daily_kpi`, etc.) |
| `expected_result_fixture` | string | Yes | Relative path to fixture JSON under `fixtures/` |
| `fixture_sha256` | string | Yes | 64-hex SHA-256 of canonical fixture JSON |
| `expected_empty` | boolean | No | Default `false`. Set `true` **only** when the query is expected to return **exactly 0 rows** by design (e.g. `GROUP BY` over absent data, `SELECT … WHERE` with no matching rows where the result set is genuinely empty). **Never set for aggregate queries** (`COUNT(*)`, `SUM()`, `AVG()` without `GROUP BY`): those always return exactly one row (with value `0` or `NULL`), which is real data that must be captured in the fixture with `expected_empty: false`. Validated strictly: the test asserts `len(rows) == 0` — no soft null/zero aggregate exception. |

### Citation Sub-schema (`expected_citations[*]`)

| Field | Type | Required | Description / Rules |
| ----- | ---- | -------- | ------------------- |
| `source_system` | string | Yes | Connector name (e.g. `google-analytics`, `meta-ads`, `gsc`) |
| `source_field` | string | Yes | Mart or view name (e.g. `fact_daily_kpi`, `semantic_avg_position`) |
| `pull_id_required` | boolean | No | Default `false`. `true` when testing AD-9 `pull_id` provenance |

## Grain-Trap Multi-Query Requirement

For `surface == "grain_trap"` records:

- `reference_queries` MUST contain at least two entries:
  1. `role: "naive_wrong"`
  2. `role: "canonical_correct"`
- `grain_trap_note` MUST describe why the naive query causes double-counting or invalid metric calculation.
- Both queries must be executed against seeds, producing different fixture SHA-256 hashes.
