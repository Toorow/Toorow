# `inbound/` — inbound receipt spine (Epic 38)

The internet-facing, scale-to-zero entry point for inbound **Email** and **File**
provider deliveries. One application artifact serves both the **toorow-managed**
and **self-hosted** hosting modes — they differ **only** in deploy-time
configuration, never in code (AC4, no fork).

Story 38.1 is the **receipt spine only**: `verify -> bound -> shape-check -> 202`.
It writes **nothing durable** (no quarantine object, no DB row, no parsing). The
durable receipt + outbox is 38.8; quarantine writes/parsing/DQ are 38.6+.

## Endpoints

| Method | Path | Purpose |
| ------ | ---- | ------- |
| `POST` | `/v1/webhooks/inbound-email` | Inbound email delivery (provider-fronted). |
| `POST` | `/v1/webhooks/inbound-file` | Transport-neutral file delivery. |

Both share one bounded handler. A verified, in-bounds, well-shaped delivery →
`202 Accepted` + `{"correlation_id": "inbrx_…"}`. **Every** rejection (unsigned,
oversize, bad recipient, replay) → a **constant-shape `403`** with the identical
body `{"code":"forbidden","message":"forbidden"}` — no existence disclosure, no
secret, no capability (E38-NFR03).

## Provider-neutral seam (AC5)

Provider vocabulary lives **only** in `inbound/adapters/`, never in `server/core`
(AD-2 / E38-NFR14). A `ReceiptAdapter.verify(request) -> InboundDelivery | None`
verifies signature/replay and normalizes the delivery. The concrete adapter is
picked by `INBOUND_PROVIDER` at deploy time:

- `mailgun` — **managed default** (DECISION 2026-07-22, Jean: Mailgun EU). HMAC
  of `timestamp + token` against the signing key, constant-time compare.
- `cloudflare_worker` — the **seam proof** (self-hosted may use it). Shared-secret
  header compare. Not the shipped default: Cloudflare Email Routing's ~5 MiB
  per-message ceiling is disqualifying for large XLSX / `.sav` (SPSS) files.

## Deploy-time variable contract

The **same** contract is honored by the Terraform `inbound-receipt` Cloud Run
service (`infra/terraform/inbound_runtime.tf`) and by any self-hosted runtime.

| Env var | Source | Default | Meaning |
| ------- | ------ | ------- | ------- |
| `INBOUND_PROVIDER` | tfvar `inbound_provider` | `mailgun` | Adapter selector. |
| `INBOUND_SIGNING_SECRET` | Secret Manager ref (`inbound_signing_secret_id`) | — | Provider signing key. **Value never in git**; injected as a reference. |
| `INBOUND_INGEST_DOMAIN` | tfvar `inbound_ingest_domain` | `ingest.toorow.com` | Receiving domain; recipients are `ds_<token>@<domain>`. |
| `INBOUND_MAX_BODY_BYTES` | tfvar `inbound_max_body_bytes` | `26214400` (25 MiB) | Body-size bound → 403 on oversize. |
| `INBOUND_MAX_HEADER_BYTES` | tfvar `inbound_max_header_bytes` | `16384` (16 KiB) | Header-size bound → 403. |
| `INBOUND_MAX_ATTACHMENTS` | tfvar `inbound_max_attachments` | `20` | Attachment-count bound → 403. |
| `INBOUND_QUARANTINE_BUCKET` | Terraform bucket name | — | Private EU quarantine bucket (written in 38.6+; unused in 38.1). |

## Security posture (fail-closed order)

1. Select adapter by `INBOUND_PROVIDER`.
2. **Verify signature/replay FIRST** (before any body parsing).
3. Enforce max body size, max header size, max attachment count.
4. Recipient must parse to the `ds_<token>@<domain>` **shape** (token → ENABLED
   Datastream resolution is **38.8**, not here).

No secret / token / capability appears in logs, traces, error bodies, or the
Terraform outputs. Anything not from the configured provider is rejected with the
constant-shape `403` before any body is trusted.
