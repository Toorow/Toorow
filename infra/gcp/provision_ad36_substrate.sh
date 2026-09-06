#!/usr/bin/env bash
# Provision the AD-36 execution substrate: Cloud Tasks + Pub/Sub + Cloud Scheduler.
#
# Story 56.8. These resources are NOT per-deploy, so they do not belong in
# infra/scripts/deploy.sh -- that script only declares the env vars that point at
# them. This script is the record of how they were created, so the next
# environment (or the next person) reproduces them instead of guessing.
#
# WHAT THIS DOES NOT DO: it never flips the running service. The env vars live in
# deploy.yml, because a `gcloud run services update` is overwritten by the next
# run of that workflow -- a flip applied by hand does not survive.
#
# Idempotent: every create is guarded, so re-running is safe.
#
#   bash infra/gcp/provision_ad36_substrate.sh [PROJECT] [REGION]

set -euo pipefail

PROJECT="${1:-toorow}"
REGION="${2:-europe-west1}"
QUEUE="toorow-work"
TOPIC="toorow-facts"
SECRET="toorow-internal-auth"

SERVICE_URL="$(gcloud run services describe mcp-server \
  --project="$PROJECT" --region="$REGION" --format='value(status.url)')"

echo "project=$PROJECT region=$REGION service=$SERVICE_URL"

# --- APIs -------------------------------------------------------------------
gcloud services enable \
  cloudtasks.googleapis.com cloudscheduler.googleapis.com pubsub.googleapis.com \
  --project="$PROJECT"

# --- Cloud Tasks ------------------------------------------------------------
# max-attempts is the queue's own retry ceiling. It sits ON TOP of the job row's
# attempt_count, which is why a failed pull answers 200: stacking the two would
# multiply attempts against the provider (story 56.2).
if ! gcloud tasks queues describe "$QUEUE" --project="$PROJECT" --location="$REGION" >/dev/null 2>&1; then
  gcloud tasks queues create "$QUEUE" --project="$PROJECT" --location="$REGION" \
    --max-attempts=5 --max-concurrent-dispatches=8 --max-dispatches-per-second=5
fi

# --- Pub/Sub ----------------------------------------------------------------
if ! gcloud pubsub topics describe "$TOPIC" --project="$PROJECT" >/dev/null 2>&1; then
  gcloud pubsub topics create "$TOPIC" --project="$PROJECT"
fi

# --- The platform secret ----------------------------------------------------
# ONE value for the whole deployment. It authenticates the platform calling
# ITSELF (Scheduler and Tasks -> this service). Nothing per-user is stored here:
# user tokens live encrypted in app.connection_ref, or at Nango. An OIDC token
# minted for this service would remove this secret entirely -- that is the
# stronger Phase B form, deliberately not half-built.
# The secret is written as RAW BYTES with no trailing newline, and read back
# with the CR stripped. Both halves matter, and the first version created by
# this script on Windows proved why: `print()` emits `\r\n` there, the pipe
# added another `\r`, and the stored value ended `\r\r\n`. Every Scheduler job
# then carried `X-Internal-Auth: <secret>\r`, which the service compares against
# a value with two more bytes -- so EVERY scheduled invocation would have failed
# authentication, and the substrate would have looked broken rather than
# mis-provisioned. Measured 2026-08-02; secret rotated to a clean version 2.
if ! gcloud secrets describe "$SECRET" --project="$PROJECT" >/dev/null 2>&1; then
  python -c "import secrets,sys;sys.stdout.buffer.write(secrets.token_urlsafe(32).encode())" \
    | gcloud secrets create "$SECRET" --project="$PROJECT" \
        --replication-policy=automatic --data-file=-
fi
# Command substitution strips trailing NEWLINES, never a lone CR -- hence the tr.
INTERNAL_SECRET="$(gcloud secrets versions access latest --secret="$SECRET" --project="$PROJECT" | tr -d '\r\n')"

# --- Cloud Scheduler: the clock leaves the process --------------------------
# Times are Europe/Paris, matching SCHEDULER_TIMEZONE's default. Each job is one
# invocation with a status code and a log line -- which is the point: a sleeping
# thread that does not fire leaves NOTHING, and missed_run_count stays at 0,
# which reads exactly like health.
create_job() {
  local name="$1" schedule="$2" path="$3"
  if gcloud scheduler jobs describe "toorow-$name" \
      --project="$PROJECT" --location="$REGION" >/dev/null 2>&1; then
    echo "scheduler job toorow-$name already exists"
    return 0
  fi
  gcloud scheduler jobs create http "toorow-$name" \
    --project="$PROJECT" --location="$REGION" \
    --schedule="$schedule" --time-zone="Europe/Paris" \
    --http-method=POST --uri="${SERVICE_URL}${path}" \
    --headers="X-Internal-Auth=${INTERNAL_SECRET}" \
    --attempt-deadline=600s
}

create_job dispatch-nightly "0 2 * * *"     /internal/scheduler/dispatch-nightly
create_job dispatch-hourly  "0 * * * *"     /internal/scheduler/dispatch-hourly
create_job reconcile-queues "*/10 * * * *"  /internal/scheduler/reconcile-queues
create_job poll-health      "0 6 * * *"     /internal/scheduler/poll-health
create_job drain-outbox     "*/5 * * * *"   /internal/scheduler/drain-outbox
# AI-117 -- deux horloges que ce fichier ne creait pas, mesurees le 2026-08-02.
#
# run-dq-monitors : l'endpoint est SERVI par admin_api et n'avait AUCUN job, donc
# les moniteurs de qualite n'ont jamais tourne d'eux-memes et rien ne le disait.
# */15 parce que le tick ne porte aucune echeance propre : il ne fixe que la
# RESOLUTION de la detection de retard, et le moniteur compare le temps ecoule a
# `expected_interval_minutes * 2`. Une cadence horaire signalerait un flux de 15
# minutes une heure apres qu'il soit deja deux fois en retard. Pas plus fin : un
# balayage evalue cinq moniteurs sur chaque Datastream actif de chaque projet.
#
# reconcile-clocks : l'horloge qui surveille les horloges. Horaire suffit -- elle
# doit reperer une edition a la main, un job en pause ou un job absent d'un
# nouvel environnement avant que ca coute une journee, pas dans la minute. A la
# minute 17 et pas 0 : toutes les autres tirent sur une minute divisible par 5,
# et une observation prise a ce moment echantillonne Cloud Scheduler EN PLEIN
# dispatch -- un job sain serait enregistre dans un etat transitoire. 17 est
# premier avec 5, donc ce tick ne tombe jamais sur celui d'une autre.
create_job run-dq-monitors  "*/15 * * * *"  /internal/scheduler/run-dq-monitors
create_job reconcile-clocks "17 * * * *"    /internal/scheduler/reconcile-clocks

# --- Pub/Sub push subscriptions: the consumers of `pull.landed` -------------
# The service account Pub/Sub mints its OIDC token as. Declare
# INTERNAL_OIDC_SERVICE_ACCOUNT with the same value on the service so a token
# from any OTHER Google principal is refused.
PUSH_SA="${PUSH_SA:-$(gcloud run services describe mcp-server --project="$PROJECT"   --region="$REGION" --format='value(spec.template.spec.serviceAccountName)')}"
# One subscription per consumer, so each gets its own invocation, status and
# retry. They used to be named calls inside the worker, whose failure was one
# WARNING line on a shared log stream.
create_subscription() {
  local name="$1" path="$2"
  if gcloud pubsub subscriptions describe "$name" --project="$PROJECT" >/dev/null 2>&1; then
    echo "subscription $name already exists"
    return 0
  fi
  # OIDC, not the shared secret: a push subscription cannot send a custom
  # header, so `X-Internal-Auth` is unreachable here. The service validates the
  # token's signature and audience (`_google_oidc_caller_is_ours`).
  gcloud pubsub subscriptions create "$name" --project="$PROJECT"     --topic="$TOPIC"     --push-endpoint="${SERVICE_URL}${path}"     --push-auth-service-account="$PUSH_SA"     --push-auth-token-audience="$SERVICE_URL"     --ack-deadline=120     --min-retry-delay=10s --max-retry-delay=600s
}

create_subscription toorow-verification  /internal/facts/pull-landed/verification
create_subscription toorow-context-seed  /internal/facts/pull-landed/context-seed

echo
echo "Substrate provisioned. The flip itself lives in infra/scripts/deploy.sh"
echo "(QUEUE_BACKEND=cloud_tasks). Verify with:"
echo "  uv run python scripts/verify_push_substrate.py"
