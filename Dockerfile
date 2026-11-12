# Build context is the repository root: docker build -t chatwoot-mcp .
# Base images are pinned by digest (multi-arch index), so a pushed tag cannot change what is built; Dependabot
# proposes updates (.github/dependabot.yml).
FROM python:3.12-slim@sha256:5e2bdb1b0d542d5039cffb8181c65930093949194a65c365dfb1b0fe13a4fa5b

WORKDIR /app

COPY --from=ghcr.io/astral-sh/uv:0.12@sha256:3af4716e991d6956a41e573eab705d0ee08500cd829ed30293eb8472f372c65a /uv /usr/local/bin/uv

ENV PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    PATH="/app/.venv/bin:$PATH"

COPY pyproject.toml uv.lock README.md LICENSE ./
RUN uv sync --frozen --no-dev --extra platform --no-install-project

COPY chatwoot_mcp/ ./chatwoot_mcp/
RUN uv sync --frozen --no-dev --extra platform

# The code stays owned by root, so the server cannot rewrite itself; it runs as an unprivileged user and writes
# nothing (run it with a read-only root filesystem).
RUN groupadd --system --gid 10001 mcp \
    && useradd --system --uid 10001 --gid mcp --home-dir /app --no-create-home mcp
USER mcp

EXPOSE 8215
ENV MCP_HTTP_HOST=0.0.0.0 \
    MCP_HTTP_PORT=8215

ENTRYPOINT ["python", "-m", "chatwoot_mcp"]
CMD ["http"]

HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=10 \
    CMD ["python", "-c", "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.environ.get('MCP_HTTP_PORT', '8215') + '/healthz', timeout=4)"]
