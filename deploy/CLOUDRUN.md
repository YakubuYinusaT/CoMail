# Cohandle on Cloud Run

Three pieces of infrastructure, all free or near-free at this volume.

| Piece | What it is | Why |
|---|---|---|
| `cohandle-api` | Cloud Run service, public | FastAPI. The API and the review queue |
| `cohandle-renderer` | Cloud Run service, **private** | Chromium behind an HTTP endpoint. Called only when a plain fetch returns an empty shell |
| `cohandle-poll-replies` | Cloud Scheduler job | Wakes the API every 10 minutes to check Zoho for replies |

Postgres is the Neon project `Cohandle` (`curly-snow-63610598`), Frankfurt.

## First run

```bash
export PROJECT_ID=your-gcp-project-id
export REGION=europe-west3
./deploy/deploy.sh setup
```

That enables the APIs, creates the Artifact Registry repo and the runtime
service account, and creates five **empty** secrets. Fill them from your own
shell so no value touches this repo:

```bash
printf '%s' 'postgresql://...pooler...neon.tech/cohandle?sslmode=require' \
  | gcloud secrets versions add cohandle-database-url --data-file=-

printf '%s' "$(openssl rand -hex 32)" \
  | gcloud secrets versions add cohandle-internal-token --data-file=-
```

Then deploy, renderer first because the API needs its URL:

```bash
./deploy/deploy.sh renderer
./deploy/deploy.sh api
./deploy/deploy.sh poller
```

Afterwards `./deploy/deploy.sh api` alone is the normal deploy.

## Choices worth knowing

**Region is Frankfurt (`europe-west3`) to sit beside Neon.** The app makes
many queries per request while a user makes one, so app and database
belong together. Moving the app to the US would put the Atlantic in the
middle of every query.

**The renderer is a separate service.** Chromium takes the image past a
gigabyte. Bundling it into the API would make every deploy and every cold
start pay for a capability most targets never need.

**The renderer is not public.** A reachable headless browser is an open
proxy. It accepts calls only from the runtime service account, and checks
a shared token on top.

**Health checks are split.** `/healthz` never touches Postgres, because a
Neon compute waking from suspend would otherwise read as a failed deploy.
`/readyz` does query, and is the one to call when you want to know the
database is genuinely reachable.

**The poller is a scheduler job, not a background process.** Cloud Run
scales to zero, so a long-lived polling loop would just be shut down.
Scheduler waking the endpoint costs nothing while idle.

**`--timeout=900`** gives a batch enrichment request the full 15 minutes
Cloud Run allows. Anything longer has to become a queue.

## Cost

Within the Cloud Run free tier at this volume: 2 million requests, 360,000
GB-seconds and 180,000 vCPU-seconds a month. A raise batch is a few
thousand requests. Cloud Scheduler allows three free jobs and this uses
one. Artifact Registry storage for two images is pennies.

The thing that would actually cost money is `--min-instances=1` to kill
cold starts. Leave it at 0 until a cold start genuinely annoys someone.

## Local

```bash
cp .env.example .env     # fill it in
pip install -r requirements.txt
uvicorn app.main:app --reload --port 8080
curl localhost:8080/readyz
```

`/readyz` returning the Postgres version means the Neon connection works.

## Not done yet

The API is a skeleton: health checks and a stub poller endpoint. The
pipeline in the build spec (ingest, enrich, qualify, generate, review,
send) is still to be written. The deploy exists first so there is
somewhere to put it, and so the Neon and Zoho wiring is proven before any
logic depends on it.
