# Two-stage uv build, same pattern as brian /
# dispatch. Lockfile-deterministic; runtime image is
# python:3.12-slim + the venv.

FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS builder
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PROJECT_ENVIRONMENT=/app/.venv
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-install-project --no-default-groups
COPY src ./src
RUN uv sync --frozen --no-default-groups

FROM python:3.12-slim AS runtime
WORKDIR /app
ENV PATH="/app/.venv/bin:$PATH"
COPY --from=builder /app /app
RUN useradd -r -u 1000 townsfolk && chown -R townsfolk /app
USER townsfolk
EXPOSE 8000
CMD ["townsfolk"]
