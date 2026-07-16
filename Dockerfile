FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

WORKDIR /app

ARG GIT_HASH=latest
ARG BUILD_TIME
ENV GIT_HASH=${GIT_HASH} \
    BUILD_TIME=${BUILD_TIME} \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-install-project --no-dev

COPY miles ./miles
COPY README.md ./
RUN uv sync --locked --no-dev

ENV PATH="/app/.venv/bin:${PATH}"

EXPOSE 8000

CMD ["miles-api", "--host", "0.0.0.0", "--port", "8000", "--no-reload"]
