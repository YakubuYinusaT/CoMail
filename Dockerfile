# Cohandle API and worker.
#
# Deliberately slim: no browser. Rendering JS-heavy pages is a separate
# service built from Dockerfile.render, because bundling Chromium here
# would take the image past a gigabyte and every cold start and every
# deploy would pay for it, on every request, for the minority of sites
# that need it.

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# ca-certificates is needed to fetch https pages at all. Everything else
# the dependencies need ships in their wheels.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Dependencies first, so edits to application code reuse this layer.
COPY requirements.txt ./
RUN pip install -r requirements.txt

COPY app ./app

RUN useradd --system --uid 1001 cohandle \
    && chown -R cohandle:cohandle /app
USER cohandle

# Cloud Run sets PORT and the container must listen on it. 8080 is the
# default it uses and the value to assume when running locally.
ENV PORT=8080
EXPOSE 8080

# exec form via shell so ${PORT} expands, while uvicorn still becomes PID 1
# and receives SIGTERM directly. Cloud Run sends SIGTERM and allows 10
# seconds before SIGKILL.
CMD exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT} --timeout-graceful-shutdown 8
