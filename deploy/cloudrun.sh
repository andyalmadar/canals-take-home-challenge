#!/usr/bin/env bash
# Provision (idempotently) and deploy the Orders API to Cloud Run with Cloud SQL.
#
#   PROJECT_ID=my-gcp-project ./deploy/cloudrun.sh
#
# What it creates (all in $REGION):
#   * Artifact Registry repo          $REPO
#   * Cloud SQL Postgres 16 instance  $SQL_INSTANCE  (db-f1-micro, ~USD 10/month)
#   * Secret Manager secret           orders-database-url
#   * Service account                 $SERVICE-sa (Cloud SQL client + secret accessor)
#   * Cloud Run job                   $SERVICE-migrate  (alembic upgrade + seed)
#   * Cloud Run service               $SERVICE
#
# Re-running builds a new image from HEAD, re-runs migrations, and redeploys.
# Tear everything down with ./deploy/teardown.sh.
set -euo pipefail

: "${PROJECT_ID:?Set PROJECT_ID to your GCP project id}"
REGION="${REGION:-us-central1}"
SERVICE="${SERVICE:-orders-api}"
REPO="${REPO:-orders}"
SQL_INSTANCE="${SQL_INSTANCE:-orders-pg}"
SQL_TIER="${SQL_TIER:-db-f1-micro}"
DB_NAME="${DB_NAME:-orders}"
DB_USER="${DB_USER:-orders}"
SECRET="${SECRET:-orders-database-url}"
MAX_INSTANCES="${MAX_INSTANCES:-4}"
DB_POOL_SIZE="${DB_POOL_SIZE:-4}"

SA_NAME="${SERVICE}-sa"
SA="${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"
CONNECTION="${PROJECT_ID}:${REGION}:${SQL_INSTANCE}"
GIT_SHA="$(git rev-parse --short HEAD 2>/dev/null || date +%s)"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPO}/${SERVICE}:${GIT_SHA}"

log() { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }

# IAM is eventually consistent: a service account created a moment ago can be
# invisible to other services for several seconds. Retry with backoff.
retry() {
  local attempt=1 max=8 delay=3
  until "$@"; do
    if (( attempt >= max )); then
      echo "command failed after $max attempts: $*" >&2
      return 1
    fi
    echo "   (attempt $attempt failed, retrying in ${delay}s...)"
    sleep "$delay"
    attempt=$((attempt + 1))
    delay=$((delay * 2 > 30 ? 30 : delay * 2))
  done
}

gcloud config set project "$PROJECT_ID" >/dev/null

log "Enabling APIs"
gcloud services enable run.googleapis.com sqladmin.googleapis.com artifactregistry.googleapis.com \
  cloudbuild.googleapis.com secretmanager.googleapis.com iam.googleapis.com

log "Artifact Registry: $REPO"
gcloud artifacts repositories describe "$REPO" --location "$REGION" >/dev/null 2>&1 ||
  gcloud artifacts repositories create "$REPO" --repository-format docker --location "$REGION"

log "Cloud SQL: $SQL_INSTANCE ($SQL_TIER)"
if ! gcloud sql instances describe "$SQL_INSTANCE" >/dev/null 2>&1; then
  gcloud sql instances create "$SQL_INSTANCE" \
    --database-version POSTGRES_16 --edition ENTERPRISE --tier "$SQL_TIER" \
    --region "$REGION" --storage-size 10 --storage-auto-increase \
    --backup-start-time 03:00 --availability-type zonal
fi
gcloud sql databases describe "$DB_NAME" --instance "$SQL_INSTANCE" >/dev/null 2>&1 ||
  gcloud sql databases create "$DB_NAME" --instance "$SQL_INSTANCE"

log "Secret: $SECRET (database URL)"
if ! gcloud secrets describe "$SECRET" >/dev/null 2>&1; then
  DB_PASSWORD="$(openssl rand -base64 30 | tr -dc 'A-Za-z0-9' | head -c 32)"
  if gcloud sql users list --instance "$SQL_INSTANCE" --format 'value(name)' | grep -qx "$DB_USER"; then
    gcloud sql users set-password "$DB_USER" --instance "$SQL_INSTANCE" --password "$DB_PASSWORD"
  else
    gcloud sql users create "$DB_USER" --instance "$SQL_INSTANCE" --password "$DB_PASSWORD"
  fi
  # asyncpg connects over the Cloud SQL Auth Proxy unix socket mounted by Cloud Run.
  printf 'postgresql+asyncpg://%s:%s@/%s?host=/cloudsql/%s' \
    "$DB_USER" "$DB_PASSWORD" "$DB_NAME" "$CONNECTION" |
    gcloud secrets create "$SECRET" --data-file=- --replication-policy automatic
  unset DB_PASSWORD
fi

log "Service account: $SA"
gcloud iam service-accounts describe "$SA" >/dev/null 2>&1 ||
  gcloud iam service-accounts create "$SA_NAME" --display-name "Orders API runtime"
retry gcloud projects add-iam-policy-binding "$PROJECT_ID" --member "serviceAccount:$SA" \
  --role roles/cloudsql.client --condition None --quiet >/dev/null
retry gcloud secrets add-iam-policy-binding "$SECRET" --member "serviceAccount:$SA" \
  --role roles/secretmanager.secretAccessor --quiet >/dev/null

log "Building image: $IMAGE"
gcloud builds submit --tag "$IMAGE" --quiet .

log "Migrations + seed (Cloud Run job)"
gcloud run jobs deploy "${SERVICE}-migrate" \
  --image "$IMAGE" --region "$REGION" --service-account "$SA" \
  --set-cloudsql-instances "$CONNECTION" \
  --set-secrets "DATABASE_URL=${SECRET}:latest" \
  --set-env-vars "APP_ENV=production" \
  --command sh --args '^|^-c|alembic upgrade head && python -m scripts.seed' \
  --max-retries 0 --task-timeout 10m --quiet
gcloud run jobs execute "${SERVICE}-migrate" --region "$REGION" --wait

log "Deploying service: $SERVICE"
# Connection budget: db-f1-micro allows ~25 connections.
# MAX_INSTANCES * (DB_POOL_SIZE + DB_MAX_OVERFLOW) must stay well under that.
gcloud run deploy "$SERVICE" \
  --image "$IMAGE" --region "$REGION" --service-account "$SA" \
  --add-cloudsql-instances "$CONNECTION" \
  --set-secrets "DATABASE_URL=${SECRET}:latest" \
  --set-env-vars "APP_ENV=production,LOG_JSON=true,DB_POOL_SIZE=${DB_POOL_SIZE},DB_MAX_OVERFLOW=0" \
  --port 8080 --cpu 1 --memory 512Mi --concurrency 80 \
  --min-instances 0 --max-instances "$MAX_INSTANCES" \
  --allow-unauthenticated --quiet

URL="$(gcloud run services describe "$SERVICE" --region "$REGION" --format 'value(status.url)')"
log "Deployed: $URL"
echo "  curl $URL/readyz"
echo "  open $URL/docs"
