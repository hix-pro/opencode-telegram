# syntax=docker/dockerfile:1

# The bridge talks to an OpenCode server over HTTP. It does not bundle the
# server or the agent; point OPENCODE_URL at the host that runs OpenCode.
FROM python:3.12-slim

ARG VERSION=dev
LABEL org.opencontainers.image.title="OpenCode Telegram" \
      org.opencontainers.image.description="Full remote control of a local OpenCode server from Telegram" \
      org.opencontainers.image.version="$VERSION" \
      org.opencontainers.image.licenses="MIT" \
      org.opencontainers.image.source="https://github.com/hix-pro/opencode-telegram"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    HOME=/home/bridge \
    PROJECT_DIR=/workspace \
    STATE_FILE=/data/state.json \
    LOG_FILE=/data/bridge.log

WORKDIR /app

# Copy the packaging metadata first so a dependency-only layer is not
# invalidated every time bridge.py changes.
COPY pyproject.toml README.md LICENSE ./
COPY bridge.py ./
COPY bridgekit ./bridgekit
RUN pip install --no-cache-dir .

RUN groupadd --gid 10001 bridge \
    && useradd --uid 10001 --gid 10001 --create-home --shell /usr/sbin/nologin bridge \
    && mkdir -p /data /workspace \
    && chown -R bridge:bridge /data /workspace /app
USER 10001:10001

# State, logs, and downloaded attachments live here. Mount it to keep sessions
# and schedules across container upgrades.
VOLUME ["/data"]

STOPSIGNAL SIGTERM
ENTRYPOINT ["opencode-telegram"]
