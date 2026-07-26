#!/usr/bin/env bash
# Apply storage retention to everything that accumulates on every deploy.
#
# Why this exists: Artifact Registry and Cloud Storage bill per GB per month,
# forever, and nothing here expires on its own. Every `gcloud run deploy` and
# every push to main leaves behind a full container image plus a source tarball
# and a build log. On 2026-07-26 that was already 3.3 GB of images and 456 MB of
# build artefacts, none of it reachable by any running service.
#
# Two of the three things below cannot live in Terraform:
#   - `cloud-run-source-deploy` is auto-created by `gcloud run deploy --source`;
#   - the build/source buckets are auto-created by Cloud Run and Cloud Build.
# The `connector` repository IS managed in infra/terraform/main.tf and gets the
# same policies there. This script sets it here too so a `terraform apply` is not
# required to make the retention effective -- the two definitions are identical
# and must be changed together.
#
# Safety: KEEP policies take precedence over DELETE policies in Artifact
# Registry. `keep-recent` is what makes the delete rules safe -- the image a
# live Cloud Run revision serves is always the most recent one for its package,
# so it is never a deletion candidate.
#
# keepCount is 2 (decision Jean 2026-07-26): one developer, one dev instance and
# one deployment, so "current + previous" is the whole rollback need. It is PER
# PACKAGE -- mcp-server, toorow-admin and inbound each keep their own 2.
#
# Usage:
#   infra/scripts/apply_retention.sh            # apply
#   infra/scripts/apply_retention.sh --dry-run  # show current state, change nothing
#
# Idempotent: re-running it overwrites the policies with the same values.

set -euo pipefail

PROJECT="${GCP_PROJECT:-toorow}"
LOCATION="${GCP_REGION:-europe-west1}"

# Every Docker repository in the project. `connector` is the CI target (one image
# per push to main); `cloud-run-source-deploy` is where hand-run
# `gcloud run deploy --source` lands.
REPOSITORIES=(connector cloud-run-source-deploy)

# Buckets holding build by-products only. NOT toorow-inbound-quarantine: that one
# holds application data (epic 38 inbound), and its retention is a product
# decision, not a cost cleanup.
BUILD_BUCKETS=(toorow_cloudbuild "run-sources-${PROJECT}-${LOCATION}")

DRY_RUN=0
[ "${1:-}" = "--dry-run" ] && DRY_RUN=1

WORKDIR="$(mktemp -d)"
trap 'rm -rf "$WORKDIR"' EXIT

# --- Artifact Registry -------------------------------------------------------
# keep-recent      KEEP   current + previous version of each package, any age
# delete-untagged  DELETE untagged versions older than 1 day (build leftovers)
# delete-stale     DELETE anything older than 7 days not held by keep-recent
#
# The age windows are short on purpose. With keepCount at 2 they are what
# actually bounds the size: a long `olderThan` lets a busy week pile up images
# that KEEP does not protect but DELETE does not yet touch.
cat >"$WORKDIR/cleanup-policies.json" <<'JSON'
[
  {
    "name": "keep-recent",
    "action": {"type": "Keep"},
    "mostRecentVersions": {"keepCount": 2}
  },
  {
    "name": "delete-untagged",
    "action": {"type": "Delete"},
    "condition": {"tagState": "UNTAGGED", "olderThan": "1d"}
  },
  {
    "name": "delete-stale",
    "action": {"type": "Delete"},
    "condition": {"tagState": "ANY", "olderThan": "7d"}
  }
]
JSON

for repo in "${REPOSITORIES[@]}"; do
  if ! gcloud artifacts repositories describe "$repo" \
        --project="$PROJECT" --location="$LOCATION" >/dev/null 2>&1; then
    echo "SKIP  artifact registry: $repo does not exist in $PROJECT/$LOCATION"
    continue
  fi

  if [ "$DRY_RUN" = "1" ]; then
    echo "DRY-RUN  artifact registry: $repo current policies:"
    gcloud artifacts repositories describe "$repo" \
      --project="$PROJECT" --location="$LOCATION" \
      --format="value(cleanupPolicies)" || true
    continue
  fi

  echo "APPLY  artifact registry: $repo"
  gcloud artifacts repositories set-cleanup-policies "$repo" \
    --project="$PROJECT" --location="$LOCATION" \
    --policy="$WORKDIR/cleanup-policies.json" \
    --no-dry-run --quiet
done

# --- Cloud Storage -----------------------------------------------------------
# Source tarballs and build logs. Deleting them past 30 days costs you the
# ability to re-run `--source` deploys of old commits from the staged copy; the
# source itself lives in git, so this is not a loss of anything unique.
cat >"$WORKDIR/lifecycle.json" <<'JSON'
{
  "rule": [
    {
      "action": {"type": "Delete"},
      "condition": {"age": 30}
    }
  ]
}
JSON

for bucket in "${BUILD_BUCKETS[@]}"; do
  if ! gcloud storage buckets describe "gs://$bucket" \
        --project="$PROJECT" >/dev/null 2>&1; then
    echo "SKIP  bucket: gs://$bucket does not exist"
    continue
  fi

  if [ "$DRY_RUN" = "1" ]; then
    echo "DRY-RUN  bucket gs://$bucket current lifecycle:"
    gcloud storage buckets describe "gs://$bucket" \
      --project="$PROJECT" --format="value(lifecycle_config)" || true
    continue
  fi

  echo "APPLY  bucket: gs://$bucket (delete objects older than 30 days)"
  gcloud storage buckets update "gs://$bucket" \
    --project="$PROJECT" \
    --lifecycle-file="$WORKDIR/lifecycle.json"
done

echo
echo "Done. Artifact Registry cleanup policies run asynchronously -- deletions"
echo "appear over the following hours, not immediately."
