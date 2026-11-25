FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    curl \
    unzip \
    ffmpeg \
    procps \
    xvfb \
    openbox \
    x11vnc \
    novnc \
    websockify \
    dbus-x11 \
    libatomic1 \
    fonts-liberation \
    fonts-noto-color-emoji \
    && rm -rf /var/lib/apt/lists/*

# Install Deno JS runtime required by yt-dlp for YouTube EJS challenge solving
RUN ARCH="$(uname -m)" && \
    case "${ARCH}" in \
      x86_64) DENO_ARCH="x86_64-unknown-linux-gnu" ;; \
      aarch64|arm64) DENO_ARCH="aarch64-unknown-linux-gnu" ;; \
      *) echo "Unsupported architecture: ${ARCH}" && exit 1 ;; \
    esac && \
    curl -fsSL "https://github.com/denoland/deno/releases/latest/download/deno-${DENO_ARCH}.zip" -o /tmp/deno.zip && \
    unzip -q /tmp/deno.zip -d /usr/local/bin && \
    rm -f /tmp/deno.zip && \
    chmod +x /usr/local/bin/deno && \
    deno --version

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && \
    playwright install chromium && \
    playwright install-deps chromium

COPY bot/ ./bot/
COPY docker/ ./docker/
RUN chmod +x /app/docker/auth-entrypoint.sh

RUN mkdir -p /data/chrome-profile /data/cookies /data/downloads

CMD ["python", "-m", "bot.main"]
