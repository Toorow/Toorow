# Outputs consumed by later stories (1.4 warehouse load, 2.1 prod) — this
# infrastructure is an OUTPUT of Story 1.1, not an assumed Given (AC3).

output "project_id" {
  description = "The provisioned dev GCP project id."
  value       = google_project.dev.project_id
}

output "bigquery_datasets" {
  description = "Map of created BigQuery dataset ids to their self links."
  value       = { for k, ds in google_bigquery_dataset.datasets : k => ds.id }
}

output "local_dev_service_account_email" {
  description = "Email of the local-dev service account (BigQuery data editor + job user)."
  value       = google_service_account.local_dev.email
}

output "bigquery_location" {
  description = "BigQuery datasets location."
  value       = var.bigquery_location
}

# Story 2.1 additions (Task 4.8).

output "artifact_registry_url" {
  description = "Base URL for the Artifact Registry Docker repository (dev project). Use <url>/mcp-server:<sha> for image references."
  value       = "${var.artifact_registry_location}-docker.pkg.dev/${google_project.dev.project_id}/${google_artifact_registry_repository.connector.repository_id}"
}

output "github_actions_sa_email" {
  description = "Email of the GitHub Actions deploy service account (used for Workload Identity Federation binding)."
  value       = google_service_account.github_actions.email
}

output "prod_project_id" {
  description = "The provisioned prod GCP project id."
  value       = google_project.prod.project_id
}

# ---------------------------------------------------------------------------
# Inbound runtime outputs (Epic 38, Story 38.1).
# AC3: endpoint URL + readiness identifiers ONLY — NO secret, NO Datastream
# capability (nothing that could route a real tenant delivery). The signing
# secret is never surfaced; only the service URL and non-sensitive identity.
# ---------------------------------------------------------------------------

output "inbound_receipt_url" {
  description = "Public HTTPS URL of the inbound-receipt Cloud Run service. The inbound-email provider (Mailgun EU) POSTs verified deliveries here. Contains no secret."
  value       = google_cloud_run_v2_service.inbound_receipt.uri
}

output "inbound_readiness_id" {
  description = "Readiness identifiers for the inbound runtime (service name, region, runtime SA email, quarantine bucket name). No secret and no Datastream capability — pure provisioning identifiers."
  value = {
    service_name      = google_cloud_run_v2_service.inbound_receipt.name
    region            = var.region
    runtime_sa_email  = google_service_account.inbound_receipt.email
    quarantine_bucket = google_storage_bucket.inbound_quarantine.name
  }
}

# ---------------------------------------------------------------------------
# Inbound bridge outputs (Epic 38, async bridge). Readiness identifiers only —
# NO secret, NO Datastream capability. The bridge URL is the Pub/Sub push
# endpoint; invocation is gated by OIDC (run.invoker) + the worker secret, so
# surfacing the URL alone routes nothing.
# ---------------------------------------------------------------------------

output "inbound_bridge_url" {
  description = "Internal HTTPS URL of the inbound-bridge Cloud Run service (Pub/Sub push target). Contains no secret; invocation requires an OIDC token from the invoker identity."
  value       = google_cloud_run_v2_service.inbound_bridge.uri
}

output "inbound_bridge_readiness_id" {
  description = "Readiness identifiers for the inbound bridge (service name, region, runtime SA email, Pub/Sub invoker SA email, topic, subscription). No secret and no Datastream capability — pure provisioning identifiers."
  value = {
    service_name      = google_cloud_run_v2_service.inbound_bridge.name
    region            = var.region
    runtime_sa        = google_service_account.inbound_bridge.email
    invoker_sa        = google_service_account.inbound_pubsub_invoker.email
    manifest_topic    = google_pubsub_topic.inbound_manifest.name
    push_subscription = google_pubsub_subscription.inbound_manifest_push.name
  }
}
