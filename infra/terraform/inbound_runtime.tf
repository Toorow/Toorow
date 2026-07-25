# toorow — inbound runtime (Epic 38, Story 38.1).
#
# [HUMAN GATE] Validated locally (`terraform validate`) but NOT applied by any
# agent. `terraform apply` requires live GCP credentials + billing that Jean
# supplies out-of-band. See ../README.md. No secret value is committed here —
# only Secret Manager *references*; the version (the actual signing key) is set
# manually by the operator, never in git (E38-NFR03).
#
# Scope (Story 38.1): a dedicated, internet-facing, scale-to-zero HTTP receipt
# service for inbound Email/File provider webhooks, its least-privilege runtime
# identity, a signing-secret reference, and a private EU quarantine bucket. This
# slice is migration-free and writes nothing durable — the receipt spine only.
# Data stays in the EU (AD-6). Provider selection is deploy-time config (AD-2 /
# E38-NFR14): the same artifact serves the managed and self-hosted variants —
# only var.inbound_provider and the signing secret differ (no code fork).

# ---------------------------------------------------------------------------
# Runtime service account — least privilege. It only needs to (a) read the
# signing secret and (b) create objects in the quarantine bucket (write wired
# in 38.9). No BigQuery, no broad storage, no project-level roles.
# ---------------------------------------------------------------------------
resource "google_service_account" "inbound_receipt" {
  project      = google_project.dev.project_id
  account_id   = "inbound-receipt"
  display_name = "toorow inbound receipt (Cloud Run)"
  description  = "Least-privilege runtime identity for the internet-facing inbound-receipt service. Reads the inbound signing secret; creates quarantine objects only."

  depends_on = [google_project_service.dev_additional_services]
}

# ---------------------------------------------------------------------------
# Signing-secret REFERENCE (not value). The runtime SA is granted
# secretAccessor ONLY on this secret. The version (the provider signing key) is
# set out-of-band by the operator; never committed (mirrors the existing
# nango-encryption-key posture).
# ---------------------------------------------------------------------------
resource "google_secret_manager_secret" "inbound_signing_secret" {
  project   = google_project.dev.project_id
  secret_id = var.inbound_signing_secret_id

  replication {
    auto {}
  }

  depends_on = [google_project_service.dev_additional_services]
}

resource "google_secret_manager_secret_iam_member" "inbound_signing_secret_accessor" {
  project   = google_project.dev.project_id
  secret_id = google_secret_manager_secret.inbound_signing_secret.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.inbound_receipt.email}"
}

# ---------------------------------------------------------------------------
# Private quarantine bucket — EU, uniform bucket-level access, public access
# prevention ENFORCED, versioning on. Nothing is written here in this slice
# (38.1); the write path is 38.6+ and retention is wired in 38.9. The runtime
# SA gets objectCreator ONLY (append-only receipt posture — cannot read back
# or delete other tenants' objects).
# ---------------------------------------------------------------------------
resource "google_storage_bucket" "inbound_quarantine" {
  project                     = google_project.dev.project_id
  name                        = "${var.project_id}-inbound-quarantine"
  location                    = var.inbound_bucket_location
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"

  versioning {
    enabled = true
  }

  # Lifecycle rule STUB — retention/expiry is wired in Story 38.9. Kept as an
  # inert, valid rule (no-op tombstone cleanup) so the bucket shape is stable
  # and 38.9 only has to tune the age, not add the block. Adjust in 38.9.
  lifecycle_rule {
    condition {
      age                = var.inbound_quarantine_retention_days
      with_state         = "ARCHIVED"
      num_newer_versions = 1
    }
    action {
      type = "Delete"
    }
  }

  depends_on = [google_project_service.dev_additional_services]
}

resource "google_storage_bucket_iam_member" "inbound_quarantine_object_creator" {
  bucket = google_storage_bucket.inbound_quarantine.name
  role   = "roles/storage.objectCreator"
  member = "serviceAccount:${google_service_account.inbound_receipt.email}"
}

# ---------------------------------------------------------------------------
# Cloud Run (2nd-gen / v2) service — internet-facing, scale-to-zero.
#   * min_instance_count = 0  -> €0 at idle (hosting-split memory).
#   * max_instance_count bounded (var) -> caps blast radius / cost.
#   * ingress = all -> must receive public provider webhooks.
#   * runs the SAME container image as the main server (no code fork); the
#     inbound ASGI entrypoint is selected by deploy-time env, not a fork.
# The image tag is a deploy-time input; Terraform only wires config + identity.
# ---------------------------------------------------------------------------
resource "google_cloud_run_v2_service" "inbound_receipt" {
  project  = google_project.dev.project_id
  name     = "inbound-receipt"
  location = var.region
  ingress  = "INGRESS_TRAFFIC_ALL"

  template {
    service_account = google_service_account.inbound_receipt.email

    scaling {
      min_instance_count = 0
      max_instance_count = var.inbound_max_instances
    }

    containers {
      # The image is supplied at deploy time (CI pushes to Artifact Registry).
      # A stable placeholder keeps `terraform validate` offline-green; the real
      # deploy overrides it with the pinned sha.
      image = var.inbound_image

      # Deploy-time configuration contract (see server/inbound/README.md). No
      # secret value here — only the reference + non-sensitive bounds.
      env {
        name  = "INBOUND_PROVIDER"
        value = var.inbound_provider
      }
      env {
        name  = "INBOUND_INGEST_DOMAIN"
        value = var.inbound_ingest_domain
      }
      env {
        name  = "INBOUND_MAX_BODY_BYTES"
        value = tostring(var.inbound_max_body_bytes)
      }
      env {
        name  = "INBOUND_MAX_HEADER_BYTES"
        value = tostring(var.inbound_max_header_bytes)
      }
      env {
        name  = "INBOUND_MAX_ATTACHMENTS"
        value = tostring(var.inbound_max_attachments)
      }
      env {
        name  = "INBOUND_QUARANTINE_BUCKET"
        value = google_storage_bucket.inbound_quarantine.name
      }
      # Signing secret injected as a Secret Manager reference (never a value).
      env {
        name = "INBOUND_SIGNING_SECRET"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.inbound_signing_secret.secret_id
            version = "latest"
          }
        }
      }
    }
  }

  depends_on = [
    google_project_service.dev_additional_services,
    google_secret_manager_secret_iam_member.inbound_signing_secret_accessor,
  ]
}
