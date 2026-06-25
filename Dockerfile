# syntax=docker/dockerfile:1.7
# ---------- Build stage ----------
FROM python:3.12-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /build

# Install build dependencies (only what's needed to compile wheels)
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        libffi-dev \
    && rm -rf /var/lib/apt/lists/*

# Copy dependency manifest first for better layer caching
COPY pyproject.toml ./
COPY README.md LICENSE ./

# Install dependencies into a virtual environment
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

# Copy application source
COPY app/ ./app/

# Install the package and dependencies (incl. the MCP server extra so the same
# image can run either the web app or `poct-mcp`)
RUN pip install --upgrade pip setuptools wheel \
    && pip install ".[mcp]"

# ---------- Runtime stage ----------
FROM python:3.12-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    POCT_DATA_DIR=/data \
    POCT_BIND_HOST=0.0.0.0 \
    POCT_BIND_PORT=8010

# Install runtime-only dependencies (curl for healthcheck)
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system --gid 1000 poct \
    && useradd --system --uid 1000 --gid poct --home-dir /app --shell /usr/sbin/nologin poct

# Copy the virtualenv from the build stage
COPY --from=builder /opt/venv /opt/venv

# Copy application source
WORKDIR /app
COPY --chown=poct:poct app/ ./app/
COPY --chown=poct:poct alembic.ini ./
COPY --chown=poct:poct alembic/ ./alembic/

# Create the data directory and set ownership
RUN mkdir -p /data && chown -R poct:poct /data /app

USER poct

VOLUME ["/data"]

# 8010 = web app (default CMD); 8011 = MCP server (when run as `poct-mcp`)
EXPOSE 8010 8011

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:8010/health || exit 1

CMD ["python", "-m", "app.main"]
