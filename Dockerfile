# ─────────────────────────────────────────────────────────────────────────────
# Production Dockerfile — Pothole Detection Ingestion Server
#
# Two stages. The dashboard is built with node and its output copied into the
# python image, because app/main.py mounts dashboard/dist only if the directory
# exists — a python-only image boots fine and silently serves no dashboard,
# which is how the operator console went missing from the container.
# ─────────────────────────────────────────────────────────────────────────────

# ── Stage 1: build the operator dashboard ────────────────────────────────────
FROM node:22-slim AS dashboard

WORKDIR /build

# Manifests first so `npm ci` is cached independently of the source.
COPY dashboard/package.json dashboard/package-lock.json ./
RUN npm ci

# scripts/ is needed before the build: the prebuild hook copies MapLibre's
# worker out of node_modules into public/. Without it every vector source
# silently fails to load, so a missing worker is a blank map, not an error.
COPY dashboard/ ./

# The operator guide is rendered into the bundle as the console's Help page by
# scripts/build-guide.mjs. It lives in docs/ because it is documentation first, so
# the dashboard stage has to be handed it explicitly — .dockerignore re-includes
# docs/guides for exactly this. Without it the prebuild hook fails the build
# rather than shipping a Help link to a blank page.
COPY docs/guides/operator-console.md /guide/operator-console.md
ENV GUIDE_SOURCE=/guide/operator-console.md

RUN npm run build


# ── Stage 2: the API server ──────────────────────────────────────────────────
FROM python:3.12-slim AS base

# Prevent Python from writing .pyc and enable unbuffered stdout/stderr
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /opt/server

# Install system dependencies required by asyncpg
RUN apt-get update && \
    apt-get install -y --no-install-recommends libpq5 && \
    rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY app/ ./app/
COPY migrations/ ./migrations/

# The built dashboard. Path must match settings.dashboard_dist_path, which
# resolves relative to the repo root (= WORKDIR here).
COPY --from=dashboard /build/dist ./dashboard/dist

# Create storage directory for local frame storage fallback
RUN mkdir -p /opt/server/storage/frames

# Declared so collected JPEGs survive a container replacement even when nobody
# passes -v. Scoped to frames/ deliberately: storage/basemap is per-deployment
# data supplied by a read-only mount, and declaring the parent would shadow it.
VOLUME ["/opt/server/storage/frames"]

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "2"]
