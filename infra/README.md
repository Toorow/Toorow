# infra/ — toorow infrastructure-as-code

Terraform IaC for the GCP **dev** environment. Story 1.1 delivers the *code*;
live provisioning is **human-gated**. No agent runs `terraform apply`.

> [!IMPORTANT]
> Every step below tagged **[HUMAN GATE]** requires live GCP credentials and/or
> a billing account that only Jean can create. Agents stop at
> `terraform validate`.

## What this provisions (dev)

- A GCP **project** (`toorow-dev` by default), linked to the dedicated
  billing account and (optionally) an org.
- **BigQuery datasets** following the canonical naming
  (ARCHITECTURE-SPINE §Deployment):
  - `raw_ga4` — immutable source landings (`raw_*`, AD-7)
  - `marts` — semantic marts (`marts_*`, module-agnostic, AD-2/AD-4)
  - `mirror` — read-only mirror of Postgres governance (`mirror_*`, AD-8)
- A **service account** `toorow-local-dev@<project>.iam.gserviceaccount.com`
  with `roles/bigquery.dataEditor` + `roles/bigquery.jobUser`.

These are **outputs of Story 1.1** consumed by later stories (1.4 warehouse
load, 2.1 prod) — never re-assumed as a Given (AC3).

**Prod** is out of scope here — that is Story 2.1.

## Two-step human process

### Step 1 — [HUMAN GATE] Create the dedicated billing account

Jean creates a **dedicated** GCP billing account (separate from any other
workload), out-of-band in the Cloud Console. This is a manual, non-automated
prerequisite. Record the billing account id (`XXXXXX-XXXXXX-XXXXXX`).

### Step 2 — [HUMAN GATE] Provision dev

```bash
cd infra/terraform

# Authenticate (one of):
gcloud auth application-default login
# or export GOOGLE_APPLICATION_CREDENTIALS=/path/to/key.json  (do not commit)

# Bootstrap state locally on the first apply, then optionally migrate to GCS
# (copy backend.tf.example -> backend.tf and run: terraform init -migrate-state).

terraform init
terraform validate      # <- this is the only step an agent may run
terraform plan  -var billing_account=<BILLING_ID> [-var org_id=<ORG_ID>]
terraform apply -var billing_account=<BILLING_ID> [-var org_id=<ORG_ID>]   # [HUMAN GATE]
```

After apply, capture the outputs (`project_id`, `bigquery_datasets`,
`local_dev_service_account_email`) — Story 1.4 wires the dbt profile to them.

### [HUMAN GATE] Service-account key issuance

Prefer **ADC impersonation** over exported JSON keys. If a key is unavoidable,
Jean issues it out-of-band and stores it outside the repo (never committed).

## CI branch-protection gates (Story 1.8)

The following CI jobs are **required** status checks on the `main` branch
(configured in GitHub → Settings → Branches → Branch protection rules).
All jobs are defined in `.github/workflows/ci.yml`.

| Job name | Hard merge gate | Description |
|----------|-----------------|-------------|
| `python` | **yes** | Python 3.12 assert, ruff lint, pytest unit + integration tests, AD-2 source-agnostic guard, AD-1 narrative-no-raw check |
| `tokens-and-shell` | **yes** | Design token build (Style Dictionary → `dist/theme.ts`), TypeScript type-check `@toorow/shell` |
| `widget-tests` | **yes** | Vitest + React Testing Library tests for the GA4 widget (needs `tokens-and-shell`) |
| `bundle-check` | **yes** | Widget builds (`sample`, `google-analytics`), AD-11 external-URL gate, AC7 bundle-size guard (< 1.5 MB); JS stack pin assertion (needs `tokens-and-shell`) |
| `admin-console` | **yes** | Admin console build + bundle gate + Vitest tests (needs `tokens-and-shell`) |
| `dbt-test` | **yes** | dbt seed → DuckDB load → `dbt run` → `dbt test` full local pipeline |
| `conformance` | **yes** | Module conformance suite: manifest, envelope, bundle, and golden-pull layers for `google-analytics` (needs `bundle-check`) |
| `isolation` | **yes** | Cross-project isolation suite against ephemeral Postgres with all 001–021 migrations applied; a failure **blocks merge** (Story 7.4, FR12/AD-5) |
| `terraform` | **yes** | `terraform fmt -check` + `terraform validate` (no apply — human-gated); secret-grep gate asserts no literal secret assignments in tracked source |

The `conformance` job depends on `bundle-check` so widget artifacts are
built before the conformance bundle layer runs.
The `widget-tests` and `admin-console` jobs also depend on `tokens-and-shell`.

## Agent boundary

An agent may run, at most:

```bash
terraform init -backend=false
terraform validate
terraform fmt -check
```

An agent must **never** run `terraform apply`/`plan` against live GCP, create
billing accounts, or issue service-account keys.

## Layout

```
infra/
  README.md
  terraform/
    main.tf              # project + BigQuery datasets + SA + IAM
    variables.tf         # billing_account, org_id, project_id, region, ...
    outputs.tf           # consumed by stories 1.4 / 2.1
    backend.tf.example   # GCS remote state (copy -> backend.tf to enable)
  nango/                 # Story 2.2: Nango OSS self-hosted local dev (docker-compose)
    docker-compose.yml   # Nango + nango-postgres + platform-db (Postgres 17)
    .env.example         # env var template (committed); copy to .env (gitignored)
    README.md            # setup, migration, OAuth, integration test instructions
    migrations/
      001_create_connection_ref.sql  # platform Postgres schema (app.connection_ref)
```

`infra/` is a single root module for now; dev/prod separation via Terraform
workspaces or `var.environment` arrives in Story 2.1.

### nango/ local dev

`infra/nango/` provides the local P2-dev environment for Nango OSS and our
platform Postgres. All secrets come from `infra/nango/.env` (gitignored).
See `infra/nango/README.md` for full setup instructions.

---

## Phase B: Cloud Monitoring Alerts

> **Status: DOCUMENTATION ONLY — not implemented in Story 5.2.**
> Story 5.2 delivers a local-first alert evaluator (no GCP required).
> The mapping below documents the Cloud Monitoring equivalent for each signal
> when GCP billing is active (HG-A cleared).

When GCP is active, the following Cloud Monitoring alert policies replace or
augment the local evaluator in `server/core/infra_alerts.py`:

| Signal | Local (Story 5.2) | Phase B: Cloud Monitoring |
|---|---|---|
| Dead-letter count | DB query on `app.pull_jobs WHERE state='dead_letter'` | Cloud Tasks → Cloud Monitoring metric `cloudtasks.googleapis.com/queue/depth`; or a log-based metric on `dead_letter` state change events |
| Mirror sync lag | `mirror_sync._last_sync_result["lag_seconds"]` | Cloud Scheduler job completion metric; or a log-based alert on `mirror_sync_complete` structured log field `lag_seconds > threshold` |
| dbt error rate | Scheduler span exit code (`dbt.exit_code != 0`) | Cloud Monitoring log-based alert policy on `jsonPayload.dbt.exit_code != 0` structured log |
| Server latency | Not in Story 5.2 (tracing → Story 5.1) | Cloud Run `run.googleapis.com/request_latencies` alert policy, P99 > 30 s |
| Health poller staleness | DB query on `MAX(connection_health.last_checked_at)` | Cloud Monitoring custom metric from a structured log emitted by the health poller; or a Synthetic Monitor |
| Notification channel | SMTP email via `smtplib` | Cloud Monitoring notification channels (email, PagerDuty, Slack, PubSub) — replaces `EmailChannel` entirely |

### Phase B activation steps (human-gated)

1. **[HUMAN GATE]** Provision GCP project and enable Cloud Monitoring API.
2. Create alert policies via Terraform (add `google_monitoring_alert_policy`
   resources to `infra/terraform/main.tf`).
3. Set `ALERTS_ENABLED=false` in production once Cloud Monitoring policies
   are live (the local evaluator becomes redundant).
4. Keep `ConsoleChannel` enabled: its structured JSON output feeds
   Cloud Logging log-based metrics automatically.

No code changes to `server/core/infra_alerts.py` are required for Phase B —
the local evaluator is complementary and can run alongside Cloud Monitoring
during a transition period.
