# Evaluation / Non-Regression Benchmark Corpus (Story 14.1)

> **AD-17 Boundary:** The files in this directory (`corpus.yaml`, `schema.md`, `fixtures/*.json`, `validate_corpus.py`, and the meta-tests) are **TEST CODE**. They evaluate agent accuracy and citation rate in CI. They must NEVER be loaded into `app.context_events`, `app.knowledge_cards`, or any application context table.
>
> **The one writer, named on purpose.** `seed_eval_platform.py` is the single file here that writes to a platform database, and it writes no corpus record: it creates the ENVIRONMENT a run reads — the declared evaluation identity's membership and one governed business link. It refuses any database whose name does not end in `_test`, and refuses to run outside a declared evaluation environment. Both refusals are asserted in `test_seed_eval_platform.py`.

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

## The full loop (what a run needs beyond DuckDB)

The runner's header says "offline", and that is half true: the seam drives the
REAL tools, which read a platform Postgres for the briefing, the module filter,
the branding, the adherence gate and the governed business routes. Three steps,
in this order:

```bash
# 1. a disposable platform database (never a shared one -- the seeder refuses
#    any database whose name does not end in `_test`)
python scripts/disposable_postgres.py up --port 55490

# 2. the rows an evaluation run READS: the declared evaluation identity's
#    membership + view grant, and the governed link the business-path question
#    traverses. Idempotent.
TOOROW_ENVIRONMENT=evaluation \
PLATFORM_DB_URL=postgresql://connector:...@127.0.0.1:55490/toorow_test \
  python server/tests/evals/seed_eval_platform.py

# 3. the run. It declares itself an evaluation environment for its own duration
#    and carries `person_EVALUATION` -- see `core/evaluation_identity.py` and the
#    ratified decision in docs/product-architecture/analyze-and-test.md.
TOOROW_DUCKDB_PATH=server/modules/google-analytics/seeds/local.duckdb \
PLATFORM_DB_URL=postgresql://connector:...@127.0.0.1:55490/toorow_test \
  python scripts/run_evals.py
```

The summary names the identity, the environment and **the warehouse the seam
opens** — not the one the runner resolved for itself, which is how a run once
reported `duckdb_available: True` while every tool beside it read an empty
in-memory database.

Rebuilding the DuckDB warehouse itself (only needed when a mart or a seed
changed) is `server/modules/google-analytics/seeds/run_local_loop.py`.
