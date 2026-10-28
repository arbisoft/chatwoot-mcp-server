"""Start the server: `chatwoot-mcp stdio` on a laptop, `chatwoot-mcp http` when hosted."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from collections.abc import Mapping, Sequence

from chatwoot_mcp.config import ConfigError, Settings
from chatwoot_mcp.credentials.header import request_token
from chatwoot_mcp.credentials.static import static_token
from chatwoot_mcp.server import build_http_app, build_server

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8214


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="chatwoot-mcp", description="MCP server for Chatwoot.")
    sub = parser.add_subparsers(dest="mode", required=True)
    sub.add_parser("stdio", help="Run for one person on their own machine. Needs CHATWOOT_API_TOKEN.")
    http = sub.add_parser("http", help="Run hosted. Each caller sends their own key as X-Chatwoot-Token.")
    http.add_argument("--host", help=f"Address to listen on (default {DEFAULT_HOST}, or MCP_HTTP_HOST).")
    http.add_argument("--port", type=int, help=f"Port to listen on (default {DEFAULT_PORT}, or MCP_HTTP_PORT).")
    return parser.parse_args(argv)


def http_address(args: argparse.Namespace, env: Mapping[str, str]) -> tuple[str, int]:
    host = args.host or env.get("MCP_HTTP_HOST") or DEFAULT_HOST
    port = args.port if args.port is not None else env.get("MCP_HTTP_PORT", str(DEFAULT_PORT))
    try:
        number = int(port)
    except ValueError:
        raise ConfigError(f"MCP_HTTP_PORT must be a port number, got {port!r}") from None
    if not 1 <= number <= 65535:
        raise ConfigError(f"The port must be between 1 and 65535, got {number}")
    return host, number


def main(argv: Sequence[str] | None = None, env: Mapping[str, str] | None = None) -> int:
    env = os.environ if env is None else env
    args = parse_args(argv)
    logging.basicConfig(level=env.get("MCP_LOG_LEVEL", "INFO").upper(), stream=sys.stderr)
    # httpx logs every full URL, and a customer search puts the customer's email or phone in the URL.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        settings = Settings.from_env(env)
        if args.mode == "stdio":
            token = env.get("CHATWOOT_API_TOKEN", "").strip()
            if not token:
                raise ConfigError("CHATWOOT_API_TOKEN is required for stdio mode (Chatwoot: Profile, Access Token).")
            server = build_server(settings, token_getter=static_token(token))
            server.run(transport="stdio", show_banner=False)
        else:
            import uvicorn

            host, port = http_address(args, env)
            app = build_http_app(build_server(settings, token_getter=request_token))
            uvicorn.run(app, host=host, port=port, log_level="info")
    except ConfigError as error:
        print(f"chatwoot-mcp: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
