from __future__ import annotations

import argparse
import ipaddress
import logging
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

import uvicorn

from . import __version__
from .infrastructure.log_history import configure_log_history
from .runtime import build_runtime
from .settings import AppSettings, default_data_dir
from .web.app import create_app


def _is_loopback(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="codex-provider-switchboard",
        description="Run the local Codex provider switchboard.",
    )
    parser.add_argument("--host", help="Bind host (default: SWITCHBOARD_HOST).")
    parser.add_argument("--port", type=int, help="Bind port (default: 8787).")
    parser.add_argument(
        "--config",
        type=Path,
        help="Configuration file path (default: per-user application data).",
    )
    parser.add_argument(
        "--log-level",
        choices=("critical", "error", "warning", "info", "debug"),
        default="info",
    )
    parser.add_argument(
        "--no-access-log", action="store_true", help="Disable HTTP access logs."
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )
    return parser


def main(argv: Sequence[str] | None = None) -> None:
    args = _parser().parse_args(argv)
    try:
        settings = AppSettings.from_env()
    except ValueError as exc:
        raise SystemExit(f"Configuration error: {exc}") from exc
    if args.host is not None:
        settings = replace(settings, host=args.host)
    if args.port is not None:
        if not 1 <= args.port <= 65_535:
            raise SystemExit("--port must be between 1 and 65535.")
        settings = replace(settings, port=args.port)
    if not _is_loopback(settings.host) and settings.token is None:
        raise SystemExit("Refusing a non-loopback bind without SWITCHBOARD_TOKEN.")

    try:
        configure_log_history(
            path=settings.log_path or default_data_dir() / "logs" / "switchboard.log",
            level=args.log_level,
            max_bytes=settings.log_max_bytes,
            backup_count=settings.log_backup_count,
        )
    except (OSError, ValueError) as exc:
        raise SystemExit(f"Log configuration error: {exc}") from exc
    logging.getLogger(__name__).info(
        "Switchboard starting host=%s port=%d", settings.host, settings.port
    )

    runtime = build_runtime(settings=settings, config_path=args.config)
    app = create_app(runtime)
    uvicorn.run(
        app,
        host=settings.host,
        port=settings.port,
        log_level=args.log_level,
        access_log=not args.no_access_log,
        log_config=None,
        proxy_headers=False,
        server_header=False,
    )
