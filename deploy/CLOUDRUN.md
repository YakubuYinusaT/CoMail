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

**The poller is a scheduler job, not a background process.** Scheduler
waking the endpoint costs nothing while idle.

**The intake worker is a background process, and that is why the API is
pinned warm.** `--min-instances=1` on its own would not be enough: Cloud
Run throttles CPU to near zero the moment a request finishes, so the
queue would only move while somebody had the intake page open in a tab.
`--no-cpu-throttling` is the flag that actually buys background
execution. Both are set on the API. The renderer stays at zero, because
it only ever works inside a request.

**`--timeout=900`** gives a batch enrichment request the full 15 minutes
Cloud Run allows. Anything longer has to become a queue.

## Cost

Requests and the renderer sit inside the Cloud Run free tier at this
volume. Cloud Scheduler allows three free jobs and this uses one.
Artifact Registry storage for two images is pennies.

The API no longer does. An instance held warm with CPU always allocated
is billed for every second it exists rather than only while it serves, so
one 1-vCPU, 1-GiB instance is a real monthly line item in the region of
forty to sixty dollars at list prices. Check the pricing calculator for
europe-west3 before assuming a figure; that range is an estimate, not a
quote.

**There is a second bill, and it is the one people miss.** A worker that
polls keeps Neon's compute from ever auto-suspending. Neon charges by
compute-hour, and an endpoint that never sleeps is roughly 730 of them a
month against a free-plan allowance far below that. Idle polling
therefore backs off from two seconds to sixty, and `enqueue()` wakes the
loop directly so the backoff costs nothing in responsiveness. That cuts
the query volume by well over an order of magnitude, but it does not make
the compute suspend: anything that polls at all keeps it awake.

If that bill is unwelcome, the cheaper shape is to put min-instances back
to 0 and have Cloud Scheduler call a drain endpoint every few minutes,
the same pattern the reply poller already uses. Unattended batches then
run on a schedule instead of continuously, and both Cloud Run and Neon go
back to sleep between them.

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
