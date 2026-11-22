FROM python:3.12-slim-bookworm

WORKDIR /app

# Install UV
RUN pip install --no-cache-dir uv

# Install python dependencies, without the dev tools (ruff); UV_NO_DEV also
# keeps `uv run` in the entrypoint from installing them.
ENV UV_NO_DEV=1
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-cache

# Copy source code and default config to docker image
COPY ./src ./src
COPY config.yaml ./

# Single volume for everything the bot writes (cache/, logs/) and optional
# user files (.env, config.yaml). Environment variables override the .env.
VOLUME /app/data

# Unhealthy when the bot's event loop stopped touching its heartbeat file
# (every 30 s). The bot exits by itself after MATCHBOT_WATCHDOG_SECONDS of
# that, and the restart policy (compose.yaml) starts it again.
HEALTHCHECK --interval=60s --timeout=10s --start-period=120s --retries=3 \
    CMD python -c "import os, sys, time; f = os.environ.get('MATCHBOT_DATA_DIR', '/app/data') + '/cache/heartbeat'; sys.exit(time.time() - os.path.getmtime(f) > 120)"

# This keeps container alive while doing nothing. Useful for debugging.
# CMD ["tail", "-f", "/dev/null"]

ENTRYPOINT ["uv", "run", "src/aoe_matchbot.py"]
