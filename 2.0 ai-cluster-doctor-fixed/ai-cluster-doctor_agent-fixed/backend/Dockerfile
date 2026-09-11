# AI Cluster Doctor - Host (backend) image.
# Build a specific stage with `--target dev` or `--target prod`
# (docker-compose.yml / docker-compose.override.yml already select the
# right one for you - see the repo root).

FROM python:3.12-slim AS base
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ---- development: source is bind-mounted in by docker-compose.override.yml,
# so nothing is COPYed here; --reload picks up local edits live. ----
FROM base AS dev
EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]

# ---- production: bake the source into the image, no --reload. ----
FROM base AS prod
COPY . .
EXPOSE 8000
# Single worker: db.py uses SQLite, which doesn't handle concurrent writers
# from multiple processes as gracefully as a real client-server database -
# one uvicorn worker keeps all writes serialized safely. Fine for this
# project's scale; swap SQLite for Postgres first if you ever need to scale
# this out to multiple workers/replicas.
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
