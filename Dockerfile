FROM python:3.12-slim

ENV TZ=Europe/Warsaw

# Playwright Chromium dependencies + virtual X display + optional noVNC.
# Keep package configuration (including tzdata) unattended during image builds.
# Scope the frontend to this command so it does not affect runtime containers.
RUN apt-get update && DEBIAN_FRONTEND=noninteractive TZ=Europe/Warsaw \
    apt-get install -y --no-install-recommends \
    tzdata \
    libnss3 \
    libnspr4 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libdrm2 \
    libdbus-1-3 \
    libxkbcommon0 \
    libatspi2.0-0 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxrandr2 \
    libgbm1 \
    libpango-1.0-0 \
    libcairo2 \
    libasound2 \
    xvfb \
    xauth \
    openbox \
    x11vnc \
    novnc \
    websockify \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

COPY pyproject.toml uv.lock ./

# Cache dependencies and Chromium independently of application source changes.
RUN uv sync \
    --frozen \
    --no-dev \
    --no-install-project

# Invoke the installed dependency directly: uv run would try to sync the project.
RUN .venv/bin/playwright install chromium

RUN mkdir -p \
    /app/data \
    /app/data/chromium-profile

COPY sync-loop.sh entrypoint-xvfb.sh ./
RUN chmod +x sync-loop.sh entrypoint-xvfb.sh

COPY prompts.toml ./
COPY src/ src/
COPY docs/eduvulcan/ docs/eduvulcan/

# Only the application installation needs to rerun when src/ changes.
RUN uv sync \
    --frozen \
    --no-dev

# Compose supplies separate API and worker commands; the standalone image runs
# the worker with headed Chromium recovery available under Xvfb.
CMD ["./entrypoint-xvfb.sh", "./sync-loop.sh"]
