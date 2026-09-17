FROM python:3.11-bookworm

LABEL org.opencontainers.image.title="Antra"
LABEL org.opencontainers.image.description="Antra music library downloader"
LABEL org.opencontainers.image.source="https://github.com/anandprtp/Antra"

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    OUTPUT_DIR=/music \
    ANTRA_OUTPUT_DIR=/music \
    ANTRA_CONFIG_DIR=/config \
    PYTHONPATH=/app

WORKDIR /app

# System dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    git \
    curl \
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

# Copy Antra source
COPY antra/ /app/antra/
COPY antra_shared/ /app/antra_shared/
COPY antra-wails/ /app/antra-wails/
COPY requirements-runtime.txt /app/
COPY docker/web/ /app/web/

# Install Antra dependencies
RUN pip install --upgrade pip setuptools wheel \
    && pip install --no-cache-dir -r /app/requirements-runtime.txt

# Install Web API dependencies
RUN pip install --no-cache-dir \
    fastapi \
    "uvicorn[standard]"

# Verify source layout
RUN ls -la /app && \
    ls -la /app/antra && \
    ls -la /app/antra_shared && \
    ls -la /app/antra-wails && \
    ls -la /app/web

# Install Playwright browser
RUN python -m playwright install chromium

# Persistent directories
RUN mkdir -p /config /music /app/logs

# Runtime configuration
ENV OUTPUT_DIR=/music \
    ANTRA_OUTPUT_DIR=/music \
    ANTRA_CONFIG_DIR=/config \
    SPOTIFY_CLIENT_ID="" \
    ANTRA_ENDPOINT_MANIFEST_URL="" \
    TIDAL_MIRROR_URL="" \
    QOBUZ_MIRROR_URL="" \
    DEEZER_MIRROR_URL=""

EXPOSE 7337

CMD ["uvicorn", "web.main:app", "--host", "0.0.0.0", "--port", "7337"]