# Development Guide

The canonical application installation and setup procedure is the root
`README.md`. Detailed conventions, connector authoring, isolation tests and the
operations runbook remain in `CONTRIBUTING.md`.

The repository-wide delivery loop and commit discipline are defined in
`working-method.md`.

## Minimal local loop

```bash
uv sync --all-packages --frozen
pnpm -C ui install --frozen-lockfile
pnpm -C ui build:tokens
uv run --package toorow-server python -m core.main
```

## High-value validation

```bash
uv run ruff check server
uv run pytest server/tests -q
pnpm -C ui --filter @toorow/admin test
pnpm -C ui --filter @toorow/widget-sample build
node ui/scripts/bundle-check.mjs ui/widgets/sample/dist/index.html
python scripts/export_public_app.py
```

## Non-regression eval gate (AD-19 / NFR13)

When a change touches the **context layer** — governed metric/dimension definitions,
procedures, or the surfaces that read them (`server/core` context/cards/reports/envelope,
the dbt marts feeding them) — run the eval gate before deploy. A non-zero exit blocks the
deploy: the agent's answer accuracy and citation rate must not silently regress.

```bash
# requires the docker stack up (Postgres) + the seeded DuckDB, like the other pg-gated
# suites -- this is a LOCAL pre-deploy gate, deliberately NOT a CI job.
TOOROW_EVALS_E2E=1 \
TOOROW_DUCKDB_PATH=server/modules/google-analytics/seeds/local.duckdb \
  python scripts/eval_gate.py            # exit != 0 on any PASS->FAIL regression
```

The gate compares against `server/tests/evals/baseline.json` (committed). Update the
baseline only on an explicit, reviewed gesture — never automatically:

```bash
TOOROW_EVALS_E2E=1 TOOROW_DUCKDB_PATH=... python scripts/eval_gate.py --update-baseline
# then commit server/tests/evals/baseline.json in a dedicated PR explaining the change
```

## Local infrastructure

Use `infra/nango/.env.example` to create a private service environment, start
`infra/nango/docker-compose.yml`, and apply every migration in numeric order.
Do not assume the Python server loads `.env` automatically: export required
variables into the process environment or use the relevant Compose `--env-file`.

## Human gates

- Provider OAuth client registration
- Terraform apply
- Cloud deployment and production environment approval
- Destructive Docker volume deletion
- Creation and first push of the public application repository
- License selection
