#!/usr/bin/env bash
#
# Cohandle on Cloud Run. Idempotent: safe to re-run.
#
#   ./deploy/deploy.sh setup     one-off: APIs, registry, service account, secrets
#   ./deploy/deploy.sh api       build and deploy the main service
#   ./deploy/deploy.sh renderer  build and deploy the browser renderer
#   ./deploy/deploy.sh poller    create/update the reply-poller job and its schedule
#   ./deploy/deploy.sh all       everything above, in order
#
set -euo pipefail

PROJECT_ID="${PROJECT_ID:?set PROJECT_ID to your Google Cloud project id}"
# europe-west3 is Frankfurt, sitting beside the Neon project. Keep them
# together: the app makes many round trips per request, the user makes one.
REGION="${REGION:-europe-west3}"
REPO="${REPO:-cohandle}"
SA="cohandle-run@${PROJECT_ID}.iam.gserviceaccount.com"
IMAGE_BASE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPO}"

gcloud config set project "${PROJECT_ID}" >/dev/null

setup() {
  gcloud services enable \
    run.googleapis.com \
    cloudbuild.googleapis.com \
    artifactregistry.googleapis.com \
    secretmanager.googleapis.com \
    cloudscheduler.googleapis.com

  gcloud artifacts repositories describe "${REPO}" --location="${REGION}" >/dev/null 2>&1 \
    || gcloud artifacts repositories create "${REPO}" \
         --repository-format=docker --location="${REGION}" \
         --description="Cohandle images"

  gcloud iam service-accounts describe "${SA}" >/dev/null 2>&1 \
    || gcloud iam service-accounts create cohandle-run \
         --display-name="Cohandle Cloud Run runtime"

  # Secrets are created empty here and filled by hand, so no credential
  # ever passes through this script, a shell history or a CI log.
  for name in cohandle-database-url cohandle-anthropic-key \
              cohandle-zoho-client-secret cohandle-zoho-refresh-token \
              cohandle-internal-token cohandle-app-password; do
    gcloud secrets describe "${name}" >/dev/null 2>&1 \
      || gcloud secrets create "${name}" --replication-policy=automatic
    gcloud secrets add-iam-policy-binding "${name}" \
      --member="serviceAccount:${SA}" \
      --role="roles/secretmanager.secretAccessor" >/dev/null
  done

  cat <<'NOTE'

Secrets exist but are empty. Add each value yourself, for example:

  printf '%s' 'postgresql://...' | gcloud secrets versions add cohandle-database-url --data-file=-

Do this from your own shell so the values never enter this repo.

cohandle-app-password is the login for the review queue. An empty value
means the service has no login at all, which is only correct when
Identity-Aware Proxy is doing the checking instead. To set a long random
one:

  python -c "import secrets; print(secrets.token_urlsafe(18), end='')" \
    | gcloud secrets versions add cohandle-app-password --data-file=-
NOTE
}

api() {
  gcloud builds submit --tag "${IMAGE_BASE}/api:latest" .

  gcloud run deploy cohandle-api \
    --image="${IMAGE_BASE}/api:latest" \
    --region="${REGION}" \
    --service-account="${SA}" \
    --allow-unauthenticated \
    --port=8080 \
    --cpu=1 --memory=1Gi \
    --min-instances=0 \
    --max-instances=4 \
    --concurrency=20 \
    --timeout=900 \
    --startup-probe="httpGet.path=/healthz,initialDelaySeconds=2,periodSeconds=3,failureThreshold=10" \
    --set-env-vars="RENDER_SERVICE_URL=$(renderer_url),ZOHO_FROM_ADDRESS=${ZOHO_FROM_ADDRESS:-}" \
    --set-secrets="DATABASE_URL=cohandle-database-url:latest,\
ANTHROPIC_API_KEY=cohandle-anthropic-key:latest,\
ZOHO_CLIENT_SECRET=cohandle-zoho-client-secret:latest,\
ZOHO_REFRESH_TOKEN=cohandle-zoho-refresh-token:latest,\
INTERNAL_TOKEN=cohandle-internal-token:latest,\
APP_PASSWORD=cohandle-app-password:latest"
}

renderer() {
  gcloud builds submit --tag "${IMAGE_BASE}/renderer:latest" \
    --config=/dev/stdin . <<CFG
steps:
  - name: gcr.io/cloud-builders/docker
    args: ['build', '-f', 'Dockerfile.render', '-t', '${IMAGE_BASE}/renderer:latest', '.']
images: ['${IMAGE_BASE}/renderer:latest']
CFG

  # No public ingress: only the API service calls this, authenticated as
  # the runtime service account. A public headless browser is an open
  # proxy and will be found and abused.
  gcloud run deploy cohandle-renderer \
    --image="${IMAGE_BASE}/renderer:latest" \
    --region="${REGION}" \
    --service-account="${SA}" \
    --no-allow-unauthenticated \
    --port=8080 \
    --cpu=1 --memory=2Gi \
    --min-instances=0 \
    --max-instances=2 \
    --concurrency=4 \
    --timeout=120 \
    --set-secrets="INTERNAL_TOKEN=cohandle-internal-token:latest"

  gcloud run services add-iam-policy-binding cohandle-renderer \
    --region="${REGION}" \
    --member="serviceAccount:${SA}" \
    --role="roles/run.invoker" >/dev/null
}

renderer_url() {
  gcloud run services describe cohandle-renderer --region="${REGION}" \
    --format='value(status.url)' 2>/dev/null || true
}

poller() {
  local api_url
  api_url="$(gcloud run services describe cohandle-api --region="${REGION}" \
    --format='value(status.url)')"

  # Cloud Run services scale to zero, so a long-lived polling process
  # would simply be shut down. Scheduler waking the endpoint every ten
  # minutes is the pattern that actually works here, and it costs nothing
  # while idle.
  gcloud scheduler jobs describe cohandle-poll-replies --location="${REGION}" >/dev/null 2>&1 \
    && gcloud scheduler jobs delete cohandle-poll-replies --location="${REGION}" --quiet

  gcloud scheduler jobs create http cohandle-poll-replies \
    --location="${REGION}" \
    --schedule="*/10 * * * *" \
    --time-zone="Africa/Lagos" \
    --uri="${api_url}/internal/poll-replies" \
    --http-method=POST \
    --headers="x-internal-token=$(gcloud secrets versions access latest --secret=cohandle-internal-token)" \
    --attempt-deadline=300s
}

case "${1:-}" in
  setup)    setup ;;
  api)      api ;;
  renderer) renderer ;;
  poller)   poller ;;
  all)      setup; renderer; api; poller ;;
  *)        sed -n '2,12p' "$0"; exit 1 ;;
esac
