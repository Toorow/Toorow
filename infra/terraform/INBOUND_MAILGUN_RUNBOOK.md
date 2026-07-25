# Inbound datastream (Mailgun) — operational setup runbook

This is the **human-gated** setup that makes an inbound (managed-feed) datastream
actually receive files by email through Mailgun. The code path is complete and
tested; the steps below are the external provisioning that no agent can perform
for you (Mailgun account, DNS on OVH, GCP billing).

End-to-end flow once provisioned:

```
sender ──email──▶ Mailgun EU ──HTTP(signed)──▶ inbound-receipt (Cloud Run, no DB)
   writes bytes + _manifest.json ──▶ quarantine bucket (GCS, EU)
   OBJECT_FINALIZE ──▶ Pub/Sub ──push(OIDC)──▶ inbound-bridge (Cloud Run, DB)
   resolves ds_<token> ──▶ durable receipt ──▶ run_import()  (SAME pipeline as a direct upload)
```

Ingress parity: an email delivery and a direct upload both drive
`core.csv_excel_import.run_import()` against the datastream's locked
`current_plan_version_id` / `current_mapping_version_id` + template contract, so
they produce identical results.

---

## 0. Prerequisites (one-time)

- A configured inbound datastream: created via `core.import_templates.create_inbound_datastream`
  with `channels` including `email`, and it must have completed **one attended
  import / mapping** so `current_plan_version_id` + `current_mapping_version_id`
  are set (unattended email ingest reuses those locked pointers — see
  `core.inbound_ingest`).
- GCP project `toorow` with billing enabled and `terraform` configured.

## 1. Mailgun EU domain

1. In the Mailgun dashboard, select the **EU region** and add the receiving
   domain `ingest.toorow.com` (matches tfvar `inbound_ingest_domain`).
2. Copy the DNS records Mailgun shows: **MX**, **SPF (TXT)**, **DKIM (TXT)**.
3. Copy the **HTTP webhook signing key** (Settings → Webhooks / API security).
   This is the value for `INBOUND_SIGNING_SECRET`. Never commit it.

## 2. OVH DNS (zone toorow.com)

Add, for the `ingest` subdomain:

| Type | Host | Value |
| ---- | ---- | ----- |
| MX   | `ingest` | `mxa.eu.mailgun.org` (priority 10), `mxb.eu.mailgun.org` (10) — use the exact hosts Mailgun shows |
| TXT  | `ingest` | `v=spf1 include:eu.mailgun.org ~all` |
| TXT  | `<dkim-selector>._domainkey.ingest` | the DKIM value from Mailgun |

Wait for propagation, then click **Verify DNS** in Mailgun until the domain is green.

## 3. Mailgun receiving route

Create a route that matches deliveries to `ingest.toorow.com` and **forwards**
(store/notify) to the receipt endpoint:

- Expression: `match_recipient(".*@ingest.toorow.com")`
- Action: `forward("https://<inbound-receipt-url>/v1/webhooks/inbound-email")`

Get `<inbound-receipt-url>` from the Terraform output `inbound_receipt_url` after
step 5 (chicken/egg: apply Terraform first, then create the route).

## 4. Secret Manager (values set out-of-band, never in git)

Terraform creates the secret **references**; you set the versions:

```bash
# Mailgun HTTP webhook signing key (from step 1.3)
printf '%s' "<MAILGUN_SIGNING_KEY>" | \
  gcloud secrets versions add inbound-signing-secret --project=toorow --data-file=-

# Shared secret for the bridge push endpoint (defence-in-depth; generate a random one)
openssl rand -base64 48 | tr -d '\n' | \
  gcloud secrets versions add inbound-worker-secret --project=toorow --data-file=-

# Platform DB URL the bridge reads (same DSN the main server uses; PLATFORM_DB_URL)
printf '%s' "<POSTGRES_DSN>" | \
  gcloud secrets versions add platform-db-url --project=toorow --data-file=-
```

Secret ids are configurable via tfvars: `inbound_signing_secret_id`,
`inbound_worker_secret_id`, `platform_db_url_secret_id`.

## 5. Terraform apply

```bash
cd infra/terraform
cp inbound.auto.tfvars.example inbound.auto.tfvars   # fill real values
terraform init
terraform validate
terraform apply
```

This provisions (see `inbound_runtime.tf` + `inbound_bridge.tf`):

- `inbound-receipt` Cloud Run service (internet-facing, no DB, scale-to-zero) +
  its least-privilege SA (signing-secret accessor + quarantine **objectCreator**).
- Private EU quarantine bucket.
- `inbound-bridge` Cloud Run service (DB-enabled) + SA (worker-secret &
  platform-db accessor + quarantine **objectViewer**).
- Pub/Sub topic + GCS `OBJECT_FINALIZE` notification (prefix `inbound/`) +
  OIDC push subscription → the bridge `/v1/internal/inbound-process`.

Grab outputs: `terraform output inbound_receipt_url` (→ step 3),
`terraform output inbound_bridge_url`.

> **Migration note:** `094_inbound_receipts.sql` (table `app.inbound_receipts`)
> must be applied to the platform Postgres before the bridge processes a
> delivery. Apply it alongside the other `app` migrations.

## 6. Smoke test

1. Email a small CSV to `ds_<token>@ingest.toorow.com`, where `<token>` is the
   address issued from the datastream's inbound panel (or the
   `POST /api/connectors/{connector}/datastreams/{id}/credentials` endpoint).
2. Expect: Mailgun 202 → a `_manifest.json` + the attachment appear under
   `gs://<project>-inbound-quarantine/inbound/<token_hash>/<message_id>/` → the
   bridge processes it → an `app.inbound_receipts` row goes `RECEIVED → PROCESSING
   → LANDED` with an `import_ledger_id` → the datastream's import ledger shows the
   new import (same as an upload).
3. Redeliver the same email → deduplicated on `provider_event_id` (no second
   import). Compare with a direct upload of the same file → identical landing.

## Security invariants (already enforced in code)

- The raw delivery token and raw recipient address are **never** stored: only
  their sha256 hashes reach quarantine / the DB.
- The receipt service verifies the Mailgun HMAC **before** persisting any bytes;
  every rejection is a constant-shape `403`.
- The bridge endpoint requires the worker secret (and Pub/Sub OIDC in prod).
- Data stays in the EU (AD-6).
