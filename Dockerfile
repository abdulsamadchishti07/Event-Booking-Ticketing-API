# syntax=docker/dockerfile:1
# Multi-stage production Dockerfile for Event Booking & Ticketing API

# ==============================================================================
# Stage 1: Build stage (Dependency resolution and virtual environment)
# ==============================================================================
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS builder

WORKDIR /app

# Enable bytecode compilation and copy link mode for virtual environment portability
ENV UV_COMPILE_BYTECODE=1
ENV UV_LINK_MODE=copy

# Install dependencies into /app/.venv using locked versions
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

# ==============================================================================
# Stage 2: Runtime stage (Lightweight, secure, non-root image)
# ==============================================================================
FROM python:3.12-slim AS runtime

WORKDIR /app

# Install curl for container health checking
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Create dedicated non-root application system user
RUN groupadd -r appgroup && useradd -r -g appgroup -d /app -s /sbin/nologin appuser

# Copy virtualenv from builder stage
COPY --from=builder /app/.venv /app/.venv
ENV PATH="/app/.venv/bin:$PATH"

# Copy application source code, Alembic migrations, and entrypoint
COPY app/ /app/app/
COPY alembic/ /app/alembic/
COPY alembic.ini /app/alembic.ini
COPY docker-entrypoint.sh /app/docker-entrypoint.sh

RUN chmod +x /app/docker-entrypoint.sh && chown -R appuser:appgroup /app

USER appuser

EXPOSE 8000

# Container health check querying the root health endpoint
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:8000/ || exit 1

ENTRYPOINT ["/app/docker-entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "4", "--proxy-headers", "--forwarded-allow-ips", "127.0.0.1,172.16.0.0/12,10.0.0.0/8,192.168.0.0/16"]
