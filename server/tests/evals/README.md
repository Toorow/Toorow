# Evaluation / Non-Regression Benchmark Corpus (Story 14.1)

> **AD-17 Boundary:** The files in this directory (`corpus.yaml`, `schema.md`, `fixtures/*.json`, `validate_corpus.py`, and the meta-tests) are **TEST CODE**. They evaluate agent accuracy and citation rate in CI. They must NEVER be loaded into `app.context_events`, `app.knowledge_cards`, or any application context table.

## Purpose

This corpus contains 50 evaluation benchmark questions with:
- Fixed `as_of` anchor date (`2026-07-15`)
- Deterministic reference SQL queries targeting seeded DuckDB marts
- Committed result fixtures in `fixtures/`
- Provenance / citation expectations (AD-9 pull_id checks)
- Adversarial grain-trap multi-query pairs (naive_wrong vs canonical_correct)

## Human Review Checklist for Jean

Before Story 14.2 runs automated evals against this ground truth, please review the following:

- [ ] **1. Question Selection & Domain Correctness**
  - Verify that the 50 questions cover representative user inquiries across daily reports, expert reports, cards, DQ, and temporal replay.
- [ ] **2. Grain-Trap Adversarial Scenarios (GT-1 to GT-8)**
  - Confirm that the 8 grain-trap notes and naive vs canonical SQL pairs capture real double-counting hazards in the product.
- [ ] **3. French Phrasing & Operator Vocabulary**
  - Verify that French questions targeting user-facing surfaces (`daily_report`, `card`) match standard platform terminology ("Synthèse KPI", "Mots-clés", "Nouveaux vs fidèles").
- [ ] **4. Anchor Date Alignment (`as_of_anchor: 2026-07-15`)**
  - Confirm `2026-07-15` falls appropriately within the seeded data window.

## How to Run Meta-Tests

```powershell
# Validate YAML schema and completeness (no DB needed)
uv run pytest server/tests/evals/test_corpus_schema.py server/tests/evals/test_fixtures_complete.py -q

# Standalone validator CLI
uv run python server/tests/evals/validate_corpus.py server/tests/evals/corpus.yaml

# Full meta-test suite with seeded DuckDB (requires local.duckdb)
$env:TOOROW_DUCKDB_PATH = "server/modules/google-analytics/seeds/local.duckdb"
uv run pytest server/tests/evals/ -q
```
