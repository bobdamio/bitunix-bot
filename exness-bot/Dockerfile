# Exness Bot - Docker Image
# MT5 Python on Linux via Wine + Xvfb (headless display)
# Based on: https://www.mql5.com/en/docs/python_metatrader5

FROM python:3.11-slim AS builder

WORKDIR /build

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# --- Production stage ---
FROM python:3.11-slim

LABEL maintainer="GoldasT"
LABEL description="Exness FVG/IFVG + Supply/Demand Trading Bot v1.0"

# Install Wine (needed for MT5 terminal on Linux)
RUN dpkg --add-architecture i386 && \
    apt-get update && \
    apt-get install -y --no-install-recommends \
        wine64 wine32 \
        xvfb \
        wget \
        cabextract \
        winbind \
    && rm -rf /var/lib/apt/lists/*

# Create non-root user
RUN groupadd -r exbot && useradd -r -g exbot -d /app -s /sbin/nologin exbot

WORKDIR /app

# Copy installed packages from builder
COPY --from=builder /install /usr/local

# Copy application code
COPY src/ ./src/
COPY main.py .
COPY config.yaml .

# Create directories
RUN mkdir -p logs data && chown -R exbot:exbot /app

# Environment
ENV PYTHONUNBUFFERED=1
ENV DISPLAY=:99
ENV WINEDLLOVERRIDES="mscoree,mshtml="
ENV WINEPREFIX=/app/.wine

# Health check
HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
    CMD python3 -c "import os; exit(0 if os.path.exists('logs/exness_bot.log') else 1)"

USER exbot

# Run with virtual display (needed for MT5 terminal)
CMD ["sh", "-c", "Xvfb :99 -screen 0 1024x768x16 & sleep 2 && python main.py"]
