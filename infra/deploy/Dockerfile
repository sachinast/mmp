# One image for all four services; the compose file picks the entrypoint.
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.9 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1 \
    PATH=/app/.venv/bin:$PATH

WORKDIR /app
COPY . .
RUN uv sync --frozen --no-dev \
    && useradd --system --home /app mmp \
    && chown -R mmp /app

USER mmp
