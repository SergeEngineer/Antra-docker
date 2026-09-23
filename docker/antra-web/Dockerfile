FROM python:3.11-slim-bookworm

LABEL org.opencontainers.image.title="Antra" \
      org.opencontainers.image.description="Antra music library downloader" \
      org.opencontainers.image.source="https://github.com/anandprtp/Antra"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    OUTPUT_DIR=/music \
    ANTRA_OUTPUT_DIR=/music \
    ANTRA_CONFIG_DIR=/config \
    PYTHONPATH=/app \
    SPOTIFY_CLIENT_ID="" \
    ANTRA_ENDPOINT_MANIFEST_URL="" \
    TIDAL_MIRROR_URL="" \
    QOBUZ_MIRROR_URL="" \
    DEEZER_MIRROR_URL=""

WORKDIR /app

# Runtime system dependencies
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        ffmpeg \
        ca-certificates \
        libnss3 \
        libatk1.0-0 \
        libatk-bridge2.0-0 \
        libcups2 \
        libdrm2 \
        libxkbcommon0 \
        libxcomposite1 \
        libxdamage1 \
        libxfixes3 \
        libxrandr2 \
        libgbm1 \
        libasound2 \
        libpangocairo-1.0-0 \
        libpango-1.0-0 \
        libcairo2 \
        libgtk-3-0 \
    && rm -rf /var/lib/apt/lists/*

# Copy dependency definition first.
# This allows Docker to cache Python dependencies when source code changes.
COPY requirements-runtime.txt .

# Install Python dependencies
RUN pip install --no-cache-dir \
        -r requirements-runtime.txt \
        fastapi \
        "uvicorn[standard]"

# Install Chromium required by Playwright
RUN python -m playwright install chromium

# Copy application source last.
# Changes to application files won't invalidate dependency layers.
COPY antra/ ./antra/
COPY antra_shared/ ./antra_shared/
COPY antra-wails/ ./antra-wails/
COPY docker/web/ ./web/

# Create persistent directories
RUN mkdir -p /config /music /app/logs

EXPOSE 7337

CMD ["uvicorn", "web.main:app", "--host", "0.0.0.0", "--port", "7337"]
