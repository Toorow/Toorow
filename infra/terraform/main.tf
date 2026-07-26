# toorow — dev environment infrastructure-as-code.
#
# [HUMAN GATE] This module is validated locally (`terraform validate`) but is
# NOT applied by any agent. `terraform apply` requires live GCP credentials and
# a dedicated billing account that Jean creates out-of-band. See ../README.md.
#
# Scope (Story 1.1, AC3): dev GCP project + BigQuery datasets (raw_*/marts_*/
# mirror_*) + a local-dev service account with BigQuery data/job roles.
# Prod (Story 2.1) reuses this module via var.environment / workspaces.

terraform {
  required_version = ">= 1.7.0"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 6.0"
    }
  }
  # Remote state backend is provided via backend.tf (copy from backend.tf.example).
  # Left unconfigured here so `terraform validate` runs without a live bucket.
}

provider "google" {
  # Credentials come from ADC (gcloud auth application-default login) or
  # GOOGLE_APPLICATION_CREDENTIALS at apply time — never committed.
  region = var.region
}

# ---------------------------------------------------------------------------
# GCP project (dev). [HUMAN GATE] links billing + org supplied by Jean.
# ---------------------------------------------------------------------------
resource "google_project" "dev" {
  name            = var.project_name
  project_id      = var.project_id
  billing_account = var.billing_account
  # org_id is optional; omit the attribute entirely when empty.
  org_id = var.org_id != "" ? var.org_id : null

  # Prevent accidental deletion of a real project once provisioned.
  lifecycle {
    prevent_destroy = true
  }
}

# Enable the APIs the platform needs for local dev + warehouse.
resource "google_project_service" "services" {
  for_each = toset([
    "bigquery.googleapis.com",
    "iam.googleapis.com",
    "cloudresourcemanager.googleapis.com",
    "serviceusage.googleapis.com",
  ])
  project                    = google_project.dev.project_id
  service                    = each.key
  disable_dependent_services = false
  disable_on_destroy         = false
}

# ---------------------------------------------------------------------------
# BigQuery datasets — canonical naming (ARCHITECTURE-SPINE §Deployment):
#   raw_*    immutable source landings (AD-7)
#   marts_*  semantic marts (module-agnostic, AD-2/AD-4)
#   mirror_* governed read-only mirror of Postgres governance (AD-8)
# ---------------------------------------------------------------------------
locals {
  datasets = {
    raw_ga4 = "Immutable GA4 source landings (raw_*)"
    marts   = "Semantic marts (marts_*) — module-agnostic canonical facts"
    mirror  = "Read-only mirror of Postgres governance entities (mirror_*)"
  }
}

resource "google_bigquery_dataset" "datasets" {
  for_each                    = local.datasets
  project                     = google_project.dev.project_id
  dataset_id                  = each.key
  friendly_name               = each.key
  description                 = each.value
  location                    = var.bigquery_location
  default_table_expiration_ms = null

  depends_on = [google_project_service.services]
}

# ---------------------------------------------------------------------------
# Service account for local dev — BigQuery data editor + job user.
# ---------------------------------------------------------------------------
resource "google_service_account" "local_dev" {
  project      = google_project.dev.project_id
  account_id   = "toorow-local-dev"
  display_name = "toorow local dev (BigQuery)"
  description  = "Local development access to BigQuery. Key issuance is a [HUMAN GATE] step; prefer ADC impersonation over exported keys."

  depends_on = [google_project_service.services]
}

resource "google_project_iam_member" "local_dev_roles" {
  for_each = toset([
    "roles/bigquery.dataEditor",
    "roles/bigquery.jobUser",
  ])
  project = google_project.dev.project_id
  role    = each.key
  member  = "serviceAccount:${google_service_account.local_dev.email}"
}

# ---------------------------------------------------------------------------
# GCP project (prod). Story 2.1 Task 4.2.
# [HUMAN GATE] prod_project_id and prod_project_name supplied by Jean.
# ---------------------------------------------------------------------------
resource "google_project" "prod" {
  name            = var.prod_project_name
  project_id      = var.prod_project_id
  billing_account = var.billing_account
  org_id          = var.org_id != "" ? var.org_id : null

  lifecycle {
    prevent_destroy = true
  }
}

# Enable the same base APIs in prod (Story 2.1 Task 4.4).
resource "google_project_service" "prod_services" {
  for_each = toset([
    "bigquery.googleapis.com",
    "iam.googleapis.com",
    "cloudresourcemanager.googleapis.com",
    "serviceusage.googleapis.com",
    "run.googleapis.com",
    "artifactregistry.googleapis.com",
    "secretmanager.googleapis.com",
  ])
  project                    = google_project.prod.project_id
  service                    = each.key
  disable_dependent_services = false
  disable_on_destroy         = false
}

# Enable Cloud Run, Artifact Registry, Secret Manager and Storage in dev too
# (Story 2.1 Task 4.4; Storage added for the Epic 38 inbound quarantine bucket —
# without it `terraform apply` of google_storage_bucket fails on a fresh project).
resource "google_project_service" "dev_additional_services" {
  for_each = toset([
    "run.googleapis.com",
    "artifactregistry.googleapis.com",
    "secretmanager.googleapis.com",
    "storage.googleapis.com",
    "pubsub.googleapis.com",
  ])
  project                    = google_project.dev.project_id
  service                    = each.key
  disable_dependent_services = false
  disable_on_destroy         = false

  depends_on = [google_project_service.services]
}

# ---------------------------------------------------------------------------
# Artifact Registry — shared Docker registry for dev and prod images.
# Story 2.1 Task 4.3: format DOCKER, location europe-west1, repository connector.
# Images are separated by tag/sha, not by repository (MVP decision).
# Hosted in the dev project; prod deploy pulls images from here.
# ---------------------------------------------------------------------------
#
# Retention: Artifact Registry bills storage per GB per month, forever. The
# deploy workflow pushes a full image on EVERY push to main (tag = git sha,
# plus `latest`), so without a cleanup policy this repository grows without
# bound and the bill grows with it. The policies below are the same set the
# `cloud-run-source-deploy` repository gets via `make retention-apply`
# (infra/scripts/apply_retention.sh) -- that one is auto-created by
# `gcloud run deploy --source` and is therefore not managed here.
#
# Ordering matters and is not obvious: KEEP policies take precedence over
# DELETE policies. `keep-recent` is what makes the two delete rules safe --
# the newest versions of each package survive regardless of age, and the image
# a live Cloud Run revision serves is always among them.
#
# keep_count = 2 (decision Jean 2026-07-26): one developer, one dev instance and
# one deployment, so "current + previous" is the whole rollback need. Note the
# count is PER PACKAGE, not per repository -- mcp-server, toorow-admin and
# inbound each keep their own 2.
#
# Because keep_count is this low, the age windows are what actually bound the
# size: with a long `older_than` a busy week still piles up images that KEEP
# does not protect but DELETE does not yet touch. 1 day / 7 days keeps the
# steady state at roughly two images per service.
resource "google_artifact_registry_repository" "connector" {
  project       = google_project.dev.project_id
  location      = var.artifact_registry_location
  repository_id = "connector"
  format        = "DOCKER"
  description   = "toorow Docker images (mcp-server). Dev + prod images separated by tag."

  # Safety net: current + previous version of each package, always.
  cleanup_policies {
    id     = "keep-recent"
    action = "KEEP"
    most_recent_versions {
      keep_count = 2
    }
  }

  # Untagged layers are build leftovers (superseded `latest`, overwritten tags).
  # Nothing rolls back to an untagged image that keep-recent does not already
  # hold, so these go fast.
  cleanup_policies {
    id     = "delete-untagged"
    action = "DELETE"
    condition {
      tag_state  = "UNTAGGED"
      older_than = "86400s" # 1 day
    }
  }

  # Anything older than a week that keep-recent does not protect.
  cleanup_policies {
    id     = "delete-stale"
    action = "DELETE"
    condition {
      tag_state  = "ANY"
      older_than = "604800s" # 7 days
    }
  }

  depends_on = [google_project_service.dev_additional_services]
}

# ---------------------------------------------------------------------------
# BigQuery datasets for prod (Story 2.1 Task 4.5).
# Mirrors the dev dataset map.
# ---------------------------------------------------------------------------
resource "google_bigquery_dataset" "prod_datasets" {
  for_each                    = local.datasets
  project                     = google_project.prod.project_id
  dataset_id                  = each.key
  friendly_name               = each.key
  description                 = each.value
  location                    = var.bigquery_location
  default_table_expiration_ms = null

  depends_on = [google_project_service.prod_services]
}

# ---------------------------------------------------------------------------
# GitHub Actions service account — Workload Identity Federation (Story 2.1 Task 4.6).
# Roles: run.admin, artifactregistry.writer, iam.serviceAccountTokenCreator.
# (review-2-1: storage.admin dropped — artifactregistry.writer suffices for pushes.)
# The WIF pool and provider must be created manually (they require the GCP project to exist
# and a one-time bootstrap; they are not Terraform-managed in this MVP to avoid
# the chicken-and-egg bootstrap problem with remote state).
# ---------------------------------------------------------------------------
resource "google_service_account" "github_actions" {
  project      = google_project.dev.project_id
  account_id   = "github-actions-deploy"
  display_name = "toorow GitHub Actions deploy SA"
  description  = "Service account for GitHub Actions CI/CD (Workload Identity Federation). Manages Cloud Run + Artifact Registry deploys."

  depends_on = [google_project_service.dev_additional_services]
}

resource "google_project_iam_member" "github_actions_dev_roles" {
  for_each = toset([
    "roles/run.admin",
    "roles/artifactregistry.writer",
    "roles/iam.serviceAccountTokenCreator",
  ])
  project = google_project.dev.project_id
  role    = each.key
  member  = "serviceAccount:${google_service_account.github_actions.email}"
}

resource "google_project_iam_member" "github_actions_prod_roles" {
  for_each = toset([
    "roles/run.admin",
    "roles/iam.serviceAccountTokenCreator",
  ])
  project = google_project.prod.project_id
  role    = each.key
  member  = "serviceAccount:${google_service_account.github_actions.email}"
}

# ---------------------------------------------------------------------------
# Secret Manager — NANGO_ENCRYPTION_KEY placeholder (Story 2.1 Task 5.2).
# Terraform manages the secret resource; the actual value (version) is set
# manually by Jean — never committed. See NFR3.
# ---------------------------------------------------------------------------
resource "google_secret_manager_secret" "nango_encryption_key" {
  project   = google_project.dev.project_id
  secret_id = "nango-encryption-key"

  replication {
    auto {}
  }

  depends_on = [google_project_service.dev_additional_services]
}

resource "google_secret_manager_secret" "nango_encryption_key_prod" {
  project   = google_project.prod.project_id
  secret_id = "nango-encryption-key"

  replication {
    auto {}
  }

  depends_on = [google_project_service.prod_services]
}

# Grant the GitHub Actions SA permission to access secrets at deploy time.
resource "google_secret_manager_secret_iam_member" "github_actions_nango_secret_dev" {
  project   = google_project.dev.project_id
  secret_id = google_secret_manager_secret.nango_encryption_key.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.github_actions.email}"
}

resource "google_secret_manager_secret_iam_member" "github_actions_nango_secret_prod" {
  project   = google_project.prod.project_id
  secret_id = google_secret_manager_secret.nango_encryption_key_prod.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.github_actions.email}"
}
