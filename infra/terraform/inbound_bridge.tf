# toorow -- inbound async bridge runtime (Epic 38).
#
# [HUMAN GATE] The Terraform binary is unavailable in this workspace, so the
# module was contract-tested but not locally validated or applied. Apply requires
# Jean out-of-band. No secret VALUE is committed here -- only Secret Manager
# *references*; the versions (the actual worker secret and the DB DSN) are set
# manually by the operator, never in git (mirrors the signing-secret posture in
# inbound_runtime.tf).
#
# Scope: a dedicated, DB-enabled, scale-to-zero bridge persists one durable job
# per attachment before dispatching bounded Cloud Tasks. Each task claims and
# commits its attempt before the quarantined bytes are read or scanned. The
# receipt service (inbound_runtime.tf) remains objectCreator-only and has no DB;
# this separate bridge reads quarantine objects and owns the AD-36 job ledger,
# retry/dead-letter evidence and recovery reconciliation. Data stays in the EU
# (AD-6).
#
# Event flow:
#   GCS OBJECT_FINALIZE (quarantine bucket, prefix inbound/)
#     -> google_storage_notification -> Pub/Sub topic (inbound_manifest)
#     -> Pub/Sub PUSH subscription (OIDC-authenticated) -> bridge Cloud Run
#        POST /v1/internal/inbound-process
#     -> bridge validates the reserved manifest and commits attachment jobs
#     -> Cloud Tasks invokes POST /v1/internal/inbound-scan-task per attachment
#     -> a scheduler reconciles lost tasks/attempts and terminal receipt state.
# Manifest redelivery is safe because receipt + attachment ordinal are unique.
#
# ---------------------------------------------------------------------------
# Push authentication -- DECISION (documented for the orchestrator).
# ---------------------------------------------------------------------------
# Two layers guard the public push endpoint (ingress must be ALL because Pub/Sub
# push arrives over the public Cloud Run URL):
#
#   1. Pub/Sub PUSH OIDC: the subscription is configured with an oidc_token whose
#      service account (the pubsub-invoker SA) is granted roles/run.invoker on
#      the bridge service. Cloud Run's built-in IAM then rejects any request that
#      does not carry a valid Google-signed OIDC token for that SA -- i.e. only
#      Pub/Sub can invoke the service. This is the PRIMARY, infrastructure-level
#      gate and needs no application code.
#
#   2. The shared-secret header is the self-hosted/manual transport gate. Pub/Sub
#      push cannot attach that header, so this managed service explicitly sets
#      INBOUND_REQUIRE_WORKER_SECRET=false and relies on Cloud Run IAM/OIDC.
#      Self-hosted deployments leave the default enabled and must provide
#      X-Inbound-Worker-Secret.
#
# The secret reference remains mounted for self-hosted parity, but it is not the
# managed transport credential. Managed unauthenticated traffic is rejected by
# Cloud Run before the application; self-hosted traffic is rejected by the
# header check.
#
# NOTE for the orchestrator: if a stricter posture is desired, replace the
# ingress with INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER + a serverless NEG; the
# simplest correct option (ALL + OIDC invoker) is chosen here and is the Google-
# recommended pattern for Pub/Sub push to Cloud Run.

# ---------------------------------------------------------------------------
# Worker-secret REFERENCE (not value). The bridge SA is granted secretAccessor
# ONLY on this secret. The version (the actual worker secret) is set out-of-band
# by the operator; never committed.
# ---------------------------------------------------------------------------
resource "google_secret_manager_secret" "inbound_worker_secret" {
  project   = google_project.dev.project_id
  secret_id = var.inbound_worker_secret_id

  replication {
    auto {}
  }

  depends_on = [google_project_service.dev_additional_services]
}

# ---------------------------------------------------------------------------
# Platform DB DSN REFERENCE (not value). [HUMAN GATE] The value (a Postgres DSN,
# e.g. the Supabase pooler connection string) is set out-of-band by the
# operator; never committed. `core.db` reads it from the PLATFORM_DB_URL env.
#
# This mirrors how the MAIN server obtains its DB credentials. There is not yet
# a main-server Cloud Run service in this module (deploy is CI-driven today), so
# the exact secret id is left as a variable with a documented default; wire the
# SAME secret the main server uses once that service lands, rather than
# duplicating the DSN. TODO(orchestrator): confirm the canonical DB secret id
# used by the main server and point var.platform_db_url_secret_id at it.
# ---------------------------------------------------------------------------
resource "google_secret_manager_secret" "platform_db_url" {
  project   = google_project.dev.project_id
  secret_id = var.platform_db_url_secret_id

  replication {
    auto {}
  }

  depends_on = [google_project_service.dev_additional_services]
}

# ---------------------------------------------------------------------------
# Bridge runtime service account -- least privilege. It needs to (a) read the
# worker secret + the DB DSN secret, and (b) READ objects in the quarantine
# bucket (objectViewer). Note the RECEIPT SA is objectCreator-only; the bridge
# needs to READ the manifest bytes, so objectViewer is granted to the BRIDGE SA
# here (not the receipt SA). No BigQuery, no broad storage, no project roles.
# ---------------------------------------------------------------------------
resource "google_service_account" "inbound_bridge" {
  project      = google_project.dev.project_id
  account_id   = "inbound-bridge"
  display_name = "toorow inbound bridge (Cloud Run)"
  description  = "Least-privilege runtime identity for the DB-enabled inbound-bridge service. Reads the worker secret + DB DSN; reads quarantine objects; runs the inbound processing worker."

  depends_on = [google_project_service.dev_additional_services]
}

resource "google_secret_manager_secret_iam_member" "inbound_worker_secret_accessor" {
  project   = google_project.dev.project_id
  secret_id = google_secret_manager_secret.inbound_worker_secret.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.inbound_bridge.email}"
}

resource "google_secret_manager_secret_iam_member" "platform_db_url_accessor" {
  project   = google_project.dev.project_id
  secret_id = google_secret_manager_secret.platform_db_url.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.inbound_bridge.email}"
}

# Bridge SA READS the quarantine bucket (objectViewer). The receipt SA keeps its
# objectCreator-only grant (in inbound_runtime.tf) -- read is a distinct grant on
# a distinct identity, preserving the append-only posture of the receipt path.
resource "google_storage_bucket_iam_member" "inbound_quarantine_object_viewer" {
  bucket = google_storage_bucket.inbound_quarantine.name
  role   = "roles/storage.objectViewer"
  member = "serviceAccount:${google_service_account.inbound_bridge.email}"
}

# Legal holds need object metadata get/update, never delete. A custom role avoids
# the destructive storage.objectAdmin grant while making the DB hold effective
# on the corresponding GCS generation.
resource "google_project_iam_custom_role" "inbound_quarantine_hold_manager" {
  project     = google_project.dev.project_id
  role_id     = "inboundQuarantineHoldManager"
  title       = "Inbound quarantine hold manager"
  description = "Read and update quarantine object holds without delete access."
  permissions = [
    "storage.objects.get",
    "storage.objects.update",
  ]
}

resource "google_storage_bucket_iam_member" "inbound_quarantine_hold_manager" {
  bucket = google_storage_bucket.inbound_quarantine.name
  role   = google_project_iam_custom_role.inbound_quarantine_hold_manager.name
  member = "serviceAccount:${google_service_account.inbound_bridge.email}"
}

# ---------------------------------------------------------------------------
# Cloud Run (v2) bridge service -- scale-to-zero.
#   * min_instance_count = 0 -> EUR0 at idle.
#   * ingress = ALL -> Pub/Sub push arrives over the public URL; invocation is
#     gated by run.invoker granted ONLY to the pubsub-invoker SA (OIDC), plus the
#     app-level worker-secret check.
#   * SAME container image as the main server (no code fork); the bridge ASGI
#     entrypoint is selected by the container command override below, mirroring
#     how the receipt service selects inbound.receipt.build_inbound_app.
#
# [HUMAN GATE / orchestrator] Entrypoint selection: the shared image's default
# CMD is `python -m core.main` (the main MCP server). The receipt + bridge apps
# are alternate ASGI apps in the same image. This service overrides the container
# command to serve `inbound.bridge:build_bridge_app()` via uvicorn. Confirm the
# image ships uvicorn (it does -- core.main uses it) and that
# `inbound.bridge:build_bridge_app` is importable on PYTHONPATH=/app/server. If
# the receipt service is later given an explicit command override too, mirror the
# same shape there for consistency (today inbound_runtime.tf relies on the image
# default, which is a pre-existing gap outside this change's scope).
# ---------------------------------------------------------------------------
resource "google_cloud_run_v2_service" "inbound_bridge" {
  project  = google_project.dev.project_id
  name     = "inbound-bridge"
  location = var.region
  ingress  = "INGRESS_TRAFFIC_ALL"

  template {
    service_account = google_service_account.inbound_bridge.email
    timeout         = "${var.inbound_scan_timeout_seconds}s"

    scaling {
      min_instance_count = 0
      max_instance_count = var.inbound_bridge_max_instances
    }

    containers {
      name  = "bridge"
      image = var.inbound_image
      resources {
        limits = {
          cpu    = "1"
          memory = var.inbound_scan_memory
        }
      }

      # Serve the bridge ASGI app from the shared image. `--factory` calls the
      # zero-arg build_bridge_app() to construct the Starlette app. PORT is
      # injected by Cloud Run; uvicorn binds 0.0.0.0:$PORT.
      command = ["python", "-m", "uvicorn"]
      args = [
        "inbound.bridge:build_bridge_app",
        "--factory",
        "--host", "0.0.0.0",
        "--port", "8080",
      ]

      # Provider selector (kept parallel to the receipt service; opaque here).
      env {
        name  = "INBOUND_PROVIDER"
        value = var.inbound_provider
      }
      # Same quarantine bucket the receipt service writes to -- the bridge READS
      # from it (open_quarantine_store selects the GCS backend on this env).
      env {
        name  = "INBOUND_QUARANTINE_BUCKET"
        value = google_storage_bucket.inbound_quarantine.name
      }
      # Cloud Run injects PORT; keep the uvicorn --port above in sync.
      env {
        name  = "PORT"
        value = "8080"
      }
      env {
        name  = "INBOUND_CLAMAV_HOST"
        value = "127.0.0.1"
      }
      env {
        name  = "INBOUND_CLAMAV_PORT"
        value = "3310"
      }
      env {
        name  = "CLOUD_TASKS_PROJECT"
        value = google_project.dev.project_id
      }
      env {
        name  = "CLOUD_TASKS_LOCATION"
        value = var.region
      }
      env {
        name  = "INBOUND_SCAN_TASK_QUEUE"
        value = google_cloud_tasks_queue.inbound_scan.name
      }
      env {
        name  = "INBOUND_SCAN_MAX_ATTEMPTS"
        value = tostring(var.inbound_scan_max_attempts)
      }
      env {
        name  = "INBOUND_BRIDGE_URL"
        value = google_cloud_run_v2_service.inbound_bridge.uri
      }
      env {
        name  = "INBOUND_TASKS_SERVICE_ACCOUNT"
        value = google_service_account.inbound_pubsub_invoker.email
      }
      env {
        name  = "INBOUND_REQUIRE_WORKER_SECRET"
        value = "false"
      }
      # Worker secret injected as a Secret Manager reference (never a value).
      env {
        name = "INBOUND_WORKER_SECRET"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.inbound_worker_secret.secret_id
            version = "latest"
          }
        }
      }
      # Platform DB DSN injected as a Secret Manager reference (never a value).
      # `core.db` reads PLATFORM_DB_URL. [HUMAN GATE] value set out-of-band.
      env {
        name = "PLATFORM_DB_URL"
        value_source {
          secret_key_ref {
            secret  = google_secret_manager_secret.platform_db_url.secret_id
            version = "latest"
          }
        }
      }
    }
    containers {
      name  = "clamav"
      image = var.inbound_clamav_image

      resources {
        limits = {
          cpu    = "1"
          memory = var.inbound_clamav_memory
        }
      }

      ports {
        container_port = 3310
      }

      startup_probe {
        failure_threshold     = 30
        initial_delay_seconds = 5
        period_seconds        = 10
        timeout_seconds       = 5
        tcp_socket {
          port = 3310
        }
      }
    }
  }

  depends_on = [
    google_project_service.dev_additional_services,
    google_secret_manager_secret_iam_member.inbound_worker_secret_accessor,
    google_secret_manager_secret_iam_member.platform_db_url_accessor,
    google_storage_bucket_iam_member.inbound_quarantine_object_viewer,
    google_storage_bucket_iam_member.inbound_quarantine_hold_manager,
  ]
}

# ---------------------------------------------------------------------------
# Pub/Sub topic that receives GCS OBJECT_FINALIZE notifications for the
# quarantine bucket.
# ---------------------------------------------------------------------------
resource "google_pubsub_topic" "inbound_manifest" {
  project = google_project.dev.project_id
  name    = var.inbound_manifest_topic

  depends_on = [google_project_service.dev_additional_services]
}

# The GCS service agent must be allowed to publish to the topic before the
# notification can be created. `google_storage_project_service_account` returns
# the per-project GCS service agent email.
data "google_storage_project_service_account" "gcs_agent" {
  project = google_project.dev.project_id
}

resource "google_pubsub_topic_iam_member" "gcs_publisher" {
  project = google_project.dev.project_id
  topic   = google_pubsub_topic.inbound_manifest.name
  role    = "roles/pubsub.publisher"
  member  = "serviceAccount:${data.google_storage_project_service_account.gcs_agent.email_address}"
}

# GCS notification: OBJECT_FINALIZE on the quarantine bucket -> the topic.
# object_name_prefix scopes it to the inbound/ key space the receipt writes.
# The bridge additionally filters for the reserved _manifest.json object, so
# attachment finalizations are ACKed as ignored (204) rather than processed.
resource "google_storage_notification" "inbound_manifest" {
  bucket         = google_storage_bucket.inbound_quarantine.name
  payload_format = "JSON_API_V1"
  topic          = google_pubsub_topic.inbound_manifest.id
  event_types    = ["OBJECT_FINALIZE"]

  object_name_prefix = var.inbound_manifest_object_prefix

  depends_on = [google_pubsub_topic_iam_member.gcs_publisher]
}

# ---------------------------------------------------------------------------
# Pub/Sub PUSH subscription -> bridge Cloud Run, OIDC-authenticated.
# A dedicated invoker SA presents a Google-signed OIDC token; Cloud Run IAM
# (run.invoker granted to that SA below) rejects anything else. This is the
# transport-level gate for the public push endpoint.
# ---------------------------------------------------------------------------
resource "google_service_account" "inbound_pubsub_invoker" {
  project      = google_project.dev.project_id
  account_id   = "inbound-bridge-invoker"
  display_name = "toorow inbound bridge Pub/Sub invoker"
  description  = "Identity Pub/Sub push presents (OIDC) to invoke the inbound-bridge Cloud Run service. Granted run.invoker on that service only."

  depends_on = [google_project_service.dev_additional_services]
}

resource "google_cloud_run_v2_service_iam_member" "bridge_pubsub_invoker" {
  project  = google_project.dev.project_id
  location = var.region
  name     = google_cloud_run_v2_service.inbound_bridge.name
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.inbound_pubsub_invoker.email}"
}

resource "google_pubsub_subscription" "inbound_manifest_push" {
  project = google_project.dev.project_id
  name    = var.inbound_manifest_subscription
  topic   = google_pubsub_topic.inbound_manifest.id

  # Redelivery is safe: process_inbound_delivery is idempotent on
  # provider_event_id. ack_deadline gives the worker room to finish the import.
  ack_deadline_seconds = var.inbound_bridge_ack_deadline_seconds

  # Dead-letter after repeated failures so a poison manifest cannot loop forever.
  retry_policy {
    minimum_backoff = "10s"
    maximum_backoff = "600s"
  }

  dead_letter_policy {
    dead_letter_topic     = google_pubsub_topic.inbound_manifest_dead_letter.id
    max_delivery_attempts = var.inbound_scan_max_attempts
  }

  push_config {
    push_endpoint = "${google_cloud_run_v2_service.inbound_bridge.uri}/v1/internal/inbound-process"

    oidc_token {
      service_account_email = google_service_account.inbound_pubsub_invoker.email
      audience              = google_cloud_run_v2_service.inbound_bridge.uri
    }
  }

  depends_on = [
    google_cloud_run_v2_service_iam_member.bridge_pubsub_invoker,
  ]
}

resource "google_cloud_tasks_queue" "inbound_scan" {
  project  = google_project.dev.project_id
  name     = var.inbound_scan_task_queue
  location = var.region

  rate_limits {
    max_concurrent_dispatches = var.inbound_bridge_max_instances
    max_dispatches_per_second = 5
  }
  retry_config {
    max_attempts       = var.inbound_scan_max_attempts
    min_backoff        = "10s"
    max_backoff        = "600s"
    max_doublings      = 5
    max_retry_duration = "3600s"
  }
  stackdriver_logging_config {
    sampling_ratio = 1
  }
  depends_on = [google_project_service.dev_additional_services]
}

resource "google_project_iam_member" "inbound_bridge_task_enqueuer" {
  project = google_project.dev.project_id
  role    = "roles/cloudtasks.enqueuer"
  member  = "serviceAccount:${google_service_account.inbound_bridge.email}"
}

resource "google_service_account_iam_member" "inbound_bridge_task_identity_user" {
  service_account_id = google_service_account.inbound_pubsub_invoker.name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${google_service_account.inbound_bridge.email}"
}

resource "google_cloud_scheduler_job" "inbound_scan_reconcile" {
  project          = google_project.dev.project_id
  region           = var.region
  name             = "inbound-scan-reconcile"
  schedule         = "*/5 * * * *"
  time_zone        = "Etc/UTC"
  attempt_deadline = "60s"

  retry_config {
    retry_count          = 3
    min_backoff_duration = "10s"
    max_backoff_duration = "60s"
    max_doublings        = 2
  }
  http_target {
    http_method = "POST"
    uri = "${google_cloud_run_v2_service.inbound_bridge.uri}/v1/internal/inbound-scan-reconcile"
    oidc_token {
      service_account_email = google_service_account.inbound_pubsub_invoker.email
      audience              = google_cloud_run_v2_service.inbound_bridge.uri
    }
  }
  depends_on = [
    google_cloud_run_v2_service_iam_member.bridge_pubsub_invoker,
    google_cloud_tasks_queue.inbound_scan,
  ]
}

resource "google_pubsub_topic" "inbound_manifest_dead_letter" {
  project = google_project.dev.project_id
  name    = "${var.inbound_manifest_topic}-dead-letter"
  depends_on = [google_project_service.dev_additional_services]
}
resource "google_pubsub_subscription" "inbound_manifest_dead_letter" {
  project                    = google_project.dev.project_id
  name                       = "${var.inbound_manifest_topic}-dead-letter-inspection"
  topic                      = google_pubsub_topic.inbound_manifest_dead_letter.id
  ack_deadline_seconds       = 60
  message_retention_duration = "604800s"

  depends_on = [google_project_service.dev_additional_services]
}

resource "google_pubsub_topic_iam_member" "inbound_dead_letter_publisher" {
  project = google_project.dev.project_id
  topic   = google_pubsub_topic.inbound_manifest_dead_letter.name
  role    = "roles/pubsub.publisher"
  member  = "serviceAccount:service-${google_project.dev.number}@gcp-sa-pubsub.iam.gserviceaccount.com"
}

resource "google_pubsub_subscription_iam_member" "inbound_dead_letter_subscriber" {
  project      = google_project.dev.project_id
  subscription = google_pubsub_subscription.inbound_manifest_push.name
  role         = "roles/pubsub.subscriber"
  member       = "serviceAccount:service-${google_project.dev.number}@gcp-sa-pubsub.iam.gserviceaccount.com"
}
