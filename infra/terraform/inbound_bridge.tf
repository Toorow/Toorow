# toorow -- inbound async bridge runtime (Epic 38).
#
# [HUMAN GATE] Validated locally (`terraform validate`) but NOT applied by any
# agent. `terraform apply` requires live GCP credentials + billing supplied by
# Jean out-of-band. No secret VALUE is committed here -- only Secret Manager
# *references*; the versions (the actual worker secret and the DB DSN) are set
# manually by the operator, never in git (mirrors the signing-secret posture in
# inbound_runtime.tf).
#
# Scope: a dedicated, DB-enabled, scale-to-zero HTTP "bridge" service that reacts
# when a manifest object is finalized in the quarantine bucket and invokes the
# already-built worker `core.inbound_processing.process_inbound_delivery`. The
# receipt service (inbound_runtime.tf) is objectCreator-ONLY and has no DB; this
# bridge is a SEPARATE service that READS the quarantine bucket and has DB
# access -- mirroring how the receipt app is its own service, selected by the
# container entrypoint. Data stays in the EU (AD-6).
#
# Event flow:
#   GCS OBJECT_FINALIZE (quarantine bucket, prefix inbound/)
#     -> google_storage_notification -> Pub/Sub topic (inbound_manifest)
#     -> Pub/Sub PUSH subscription (OIDC-authenticated) -> bridge Cloud Run
#        POST /v1/internal/inbound-process
#     -> bridge filters for the reserved _manifest.json object, reads it, and
#        runs the worker (idempotent on provider_event_id, so redelivery-safe).
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
#   2. Shared-secret header (defence in depth): the application enforces an
#      X-Inbound-Worker-Secret check. Pub/Sub push has NO mechanism to attach a
#      custom request header (push_config exposes no header field, and message
#      attributes are not turned into request headers), so the MANAGED Pub/Sub
#      path does NOT satisfy this header -- it passes on OIDC alone (layer 1).
#      The header gate exists for SELF-HOSTED / manual-smoke invocations that
#      front the endpoint themselves and drive it directly with the secret.
#
# The INBOUND_WORKER_SECRET env is ALWAYS required: the app 500s (never accepts
# unauthenticated) when it is unset. Managed deployments rely on OIDC (layer 1)
# as the transport gate; self-hosted deployments that do not use Pub/Sub OIDC can
# drive the endpoint with the header alone (layer 2).
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

    scaling {
      min_instance_count = 0
      max_instance_count = var.inbound_bridge_max_instances
    }

    containers {
      image = var.inbound_image

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
  }

  depends_on = [
    google_project_service.dev_additional_services,
    google_secret_manager_secret_iam_member.inbound_worker_secret_accessor,
    google_secret_manager_secret_iam_member.platform_db_url_accessor,
    google_storage_bucket_iam_member.inbound_quarantine_object_viewer,
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
  # (The topic is reused as a simple DLT target; a dedicated DLT topic can be
  # split out later if desired.)
  retry_policy {
    minimum_backoff = "10s"
    maximum_backoff = "600s"
  }

  push_config {
    push_endpoint = "${google_cloud_run_v2_service.inbound_bridge.uri}/v1/internal/inbound-process"

    oidc_token {
      service_account_email = google_service_account.inbound_pubsub_invoker.email
      # Audience defaults to the push endpoint; Cloud Run validates it.
    }
  }

  depends_on = [
    google_cloud_run_v2_service_iam_member.bridge_pubsub_invoker,
  ]
}
