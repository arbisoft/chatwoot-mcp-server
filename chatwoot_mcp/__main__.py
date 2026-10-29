"""Command-line entry point: ``chatwoot-mcp stdio`` or ``chatwoot-mcp http``.

``http`` runs platform mode (Cognito sign-in, each user's own key) when ``COGNITO_USER_POOL_ID`` is set, and the
community server otherwise.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from collections.abc import Sequence

import uvicorn

from chatwoot_mcp import platform
from chatwoot_mcp.config import ConfigError, Settings
from chatwoot_mcp.server import build_http_app, build_server

TRANSPORTS = ("stdio", "http")


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="chatwoot-mcp", description="Chatwoot MCP server")
    parser.add_argument("transport", nargs="?", default="stdio", choices=TRANSPORTS)
    return parser.parse_args(argv)


def configure_logging() -> None:
    logging.basicConfig(level=os.environ.get("MCP_LOG_LEVEL", "INFO").upper(), stream=sys.stderr)
    # httpx logs every full URL, and a customer search puts the customer's email or phone in the URL.
    logging.getLogger("httpx").setLevel(logging.WARNING)


def main(argv: Sequence[str] | None = None) -> None:
    transport = parse_args(argv).transport
    if transport == "http" and platform.enabled():
        platform.run()
        return
    configure_logging()
    try:
        settings = Settings.from_env()
    except ConfigError as error:
        sys.exit(f"chatwoot-mcp: {error}")
    mcp = build_server(settings)
    if transport == "stdio":
        mcp.run(show_banner=False)
        return
    try:
        app = build_http_app(mcp, settings)
    except ConfigError as error:
        sys.exit(f"chatwoot-mcp: {error}")
    uvicorn.run(app, host=settings.http_host, port=settings.http_port, access_log=False)


if __name__ == "__main__":
    main()
