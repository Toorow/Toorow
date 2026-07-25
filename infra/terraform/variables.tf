# Input variables for the toorow dev environment.
#
# [HUMAN GATE] billing_account and org_id are supplied by Jean at apply time.
# No secrets are committed; pass them via -var or a gitignored *.tfvars file.

variable "project_id" {
  description = "GCP project id for the dev environment (e.g. toorow-dev)."
  type        = string
  default     = "toorow-dev"
}

variable "project_name" {
  description = "Human-readable display name for the GCP project."
  type        = string
  default     = "toorow dev"
}

variable "billing_account" {
  description = "[HUMAN GATE] The dedicated billing account id (XXXXXX-XXXXXX-XXXXXX). Created out-of-band by Jean."
  type        = string
}

variable "org_id" {
  description = "[HUMAN GATE] GCP organization id to attach the project to. Leave empty to create a standalone project under the billing account."
  type        = string
  default     = ""
}

variable "region" {
  description = "Default GCP region."
  type        = string
  default     = "europe-west1"
}

variable "bigquery_location" {
  description = "BigQuery dataset location (multi-region). EU keeps data in-region per project preference (AD-6)."
  type        = string
  default     = "EU"
}

variable "environment" {
  description = "Environment name. Story 1.1 provisions only 'dev'; 'prod' is Story 2.1."
  type        = string
  default     = "dev"
}

# ---------------------------------------------------------------------------
# Prod project variables (Story 2.1, Task 4.1).
# ---------------------------------------------------------------------------

variable "prod_project_id" {
  description = "GCP project id for the prod environment (e.g. toorow-prod)."
  type        = string
  default     = "toorow-prod"
}

variable "prod_project_name" {
  description = "Human-readable display name for the prod GCP project."
  type        = string
  default     = "toorow prod"
}

variable "artifact_registry_location" {
  description = "Location for the Artifact Registry repository (Docker images). Must match Cloud Run region."
  type        = string
  default     = "europe-west1"
}

# ---------------------------------------------------------------------------
# Inbound runtime variables (Epic 38, Story 38.1).
# All have safe defaults; no secrets. The signing key VALUE is set out-of-band
# (a secret version), never here. Managed vs self-hosted differ only in these
# deploy-time values — one artifact, one configuration contract (AC4).
# ---------------------------------------------------------------------------

variable "inbound_provider" {
  description = "Inbound receipt adapter selector (deploy-time, provider-neutral seam per AD-2). 'mailgun' is the managed default (DECISION 2026-07-22: Mailgun EU); 'cloudflare_worker' is the self-hosted seam proof."
  type        = string
  default     = "mailgun"

  validation {
    condition     = contains(["mailgun", "cloudflare_worker"], var.inbound_provider)
    error_message = "inbound_provider must be one of: mailgun, cloudflare_worker."
  }
}

variable "inbound_signing_secret_id" {
  description = "Secret Manager secret id holding the inbound provider signing key. Terraform manages the reference only; the version (value) is set out-of-band and never committed."
  type        = string
  default     = "inbound-signing-secret"
}

variable "inbound_ingest_domain" {
  description = "Receiving domain for inbound email/file (e.g. ingest.toorow.com). Recipients take the shape ds_<token>@<domain>. DNS mapping is an operator [HUMAN GATE] step (see the story's DNS runbook)."
  type        = string
  default     = "ingest.toorow.com"
}

variable "inbound_image" {
  description = "Container image for the inbound-receipt Cloud Run service. Same artifact as the main server; the inbound ASGI entrypoint is selected by env, not a fork. Overridden at deploy time with the pinned sha."
  type        = string
  default     = "europe-west1-docker.pkg.dev/toorow-dev/connector/mcp-server:latest"
}

variable "inbound_max_instances" {
  description = "Upper bound on inbound-receipt Cloud Run instances (blast-radius / cost cap). min is always 0 (scale-to-zero)."
  type        = number
  default     = 10
}

variable "inbound_max_body_bytes" {
  description = "Maximum accepted inbound request body size in bytes. Sized for large XLSX / .sav (SPSS) files (Mailgun EU has no ~5 MiB Cloudflare ceiling). Oversize -> constant-shape 403."
  type        = number
  default     = 26214400 # 25 MiB
}

variable "inbound_max_header_bytes" {
  description = "Maximum accepted total inbound request header size in bytes. Oversize -> constant-shape 403."
  type        = number
  default     = 16384 # 16 KiB
}

variable "inbound_max_attachments" {
  description = "Maximum attachment count per inbound delivery. Over the cap -> constant-shape 403."
  type        = number
  default     = 20
}

variable "inbound_bucket_location" {
  description = "Location for the private inbound quarantine bucket. EU keeps data in-region (AD-6)."
  type        = string
  default     = "EU"
}

variable "inbound_quarantine_retention_days" {
  description = "Lifecycle age (days) for the quarantine bucket cleanup rule STUB. Real retention is wired in Story 38.9; this is an inert default."
  type        = number
  default     = 30
}

# ---------------------------------------------------------------------------
# Inbound BRIDGE variables (Epic 38, async bridge). The bridge is a SEPARATE,
# DB-enabled Cloud Run service that reacts to quarantine manifest finalizations
# (via Pub/Sub push) and runs core.inbound_processing.process_inbound_delivery.
# All have safe defaults; no secrets. The worker-secret and DB-DSN VALUES are set
# out-of-band (secret versions), never here.
# ---------------------------------------------------------------------------

variable "inbound_worker_secret_id" {
  description = "Secret Manager secret id holding the shared worker secret the bridge requires in the X-Inbound-Worker-Secret header. Terraform manages the reference only; the version (value) is set out-of-band and never committed."
  type        = string
  default     = "inbound-worker-secret"
}

variable "platform_db_url_secret_id" {
  description = "[HUMAN GATE] Secret Manager secret id holding the platform Postgres DSN (PLATFORM_DB_URL, e.g. the Supabase pooler connection string). The bridge needs DB access to run the worker. Point this at the SAME secret the main server uses once that service lands; the version (value) is set out-of-band and never committed."
  type        = string
  default     = "platform-db-url"
}

variable "inbound_bridge_max_instances" {
  description = "Upper bound on inbound-bridge Cloud Run instances (blast-radius / cost cap). min is always 0 (scale-to-zero)."
  type        = number
  default     = 5
}

variable "inbound_bridge_ack_deadline_seconds" {
  description = "Pub/Sub push ack deadline for the bridge subscription. Must exceed the worker's typical processing time; redelivery is safe (idempotent on provider_event_id)."
  type        = number
  default     = 120
}

variable "inbound_manifest_topic" {
  description = "Pub/Sub topic name that receives GCS OBJECT_FINALIZE notifications for the quarantine bucket."
  type        = string
  default     = "inbound-manifest"
}

variable "inbound_manifest_subscription" {
  description = "Pub/Sub push subscription name delivering manifest finalizations to the bridge Cloud Run service."
  type        = string
  default     = "inbound-manifest-push"
}

variable "inbound_manifest_object_prefix" {
  description = "GCS object_name_prefix scoping the notification to the inbound/ key space the receipt service writes. The bridge additionally filters for the reserved _manifest.json object."
  type        = string
  default     = "inbound/"
}
