FROM python:3.12-slim

# Playwright Chromium dependencies + virtual X display + optional noVNC.
RUN apt-get update && apt-get install -y --no-install-recommends \
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

COPY entrypoint-xvfb.sh ./
RUN chmod +x entrypoint-xvfb.sh

COPY entrypoint.sh ./
RUN chmod +x entrypoint.sh

COPY prompts.toml ./
COPY src/ src/

# Only the application installation needs to rerun when src/ changes.
RUN uv sync \
    --frozen \
    --no-dev

ENTRYPOINT ["./entrypoint.sh"]
