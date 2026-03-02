# GoldasT Bot v2 - Production Docker Image
# Multi-stage build for minimal image size

FROM python:3.11-slim AS builder

WORKDIR /build

# Install build dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# --- Production stage ---
FROM python:3.11-slim

LABEL maintainer="GoldasT"
LABEL description="GoldasT FVG/IFVG Trading Bot v2"

# Create non-root user
RUN groupadd -r goldast && useradd -r -g goldast -d /app -s /sbin/nologin goldast

WORKDIR /app

# Copy installed packages from builder
COPY --from=builder /install /usr/local

# Copy application code
COPY main.py .
COPY src/ src/

# Create directories for persistent data
RUN mkdir -p logs data && chown -R goldast:goldast /app

# Default config (override with volume mount)
COPY config.yaml config.yaml

# Switch to non-root user
USER goldast

# Health check - verify the process is running
HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
    CMD python -c "import os, signal; os.kill(1, signal.SIG_DFL) or True" || exit 1

# Environment variables (override in docker-compose or .env)
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=America/New_York

ENTRYPOINT ["python", "main.py"]
