# Build context is this folder (mcp/): docker build -t chatwoot-mcp-server mcp/
FROM python:3.12-slim

WORKDIR /app

# curl is used by the HEALTHCHECK
RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.12 /uv /usr/local/bin/uv

COPY pyproject.toml README.md ./
COPY chatwoot_mcp/ ./chatwoot_mcp/
RUN uv pip install --system --no-cache .

# Run as a normal user, not root
RUN groupadd --system --gid 10001 mcp \
    && useradd --system --uid 10001 --gid mcp --home-dir /app --no-create-home mcp \
    && chown -R mcp:mcp /app
USER mcp

ENV MCP_HTTP_HOST=0.0.0.0 \
    MCP_HTTP_PORT=8214 \
    PYTHONUNBUFFERED=1
EXPOSE 8214

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -fsS http://127.0.0.1:${MCP_HTTP_PORT}/healthz || exit 1

CMD ["chatwoot-mcp", "http"]
