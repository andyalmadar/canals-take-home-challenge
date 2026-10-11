#!/usr/bin/env bash
# Delete everything deploy/cloudrun.sh created. Irreversible: the database goes too.
#   PROJECT_ID=my-gcp-project ./deploy/teardown.sh
set -euo pipefail

: "${PROJECT_ID:?Set PROJECT_ID}"
REGION="${REGION:-us-central1}"
SERVICE="${SERVICE:-orders-api}"
REPO="${REPO:-orders}"
SQL_INSTANCE="${SQL_INSTANCE:-orders-pg}"
SECRET="${SECRET:-orders-database-url}"
SA="${SERVICE}-sa@${PROJECT_ID}.iam.gserviceaccount.com"

gcloud config set project "$PROJECT_ID" >/dev/null
gcloud run services delete "$SERVICE" --region "$REGION" --quiet || true
gcloud run jobs delete "${SERVICE}-migrate" --region "$REGION" --quiet || true
gcloud sql instances delete "$SQL_INSTANCE" --quiet || true
gcloud secrets delete "$SECRET" --quiet || true
gcloud artifacts repositories delete "$REPO" --location "$REGION" --quiet || true
gcloud iam service-accounts delete "$SA" --quiet || true
echo "done"
