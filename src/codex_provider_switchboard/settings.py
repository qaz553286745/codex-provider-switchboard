from __future__ import annotations

import ipaddress
import os
import sys
from dataclasses import dataclass
from pathlib import Path

APP_NAME = "codex-provider-switchboard"


def default_data_dir() -> Path:
    """Return the per-user directory for configuration and runtime state."""
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_NAME
    if sys.platform == "win32":
        base = Path(os.getenv("APPDATA", Path.home() / "AppData" / "Roaming"))
        return base / APP_NAME
    xdg = os.getenv("XDG_CONFIG_HOME")
    base = Path(xdg).expanduser() if xdg else Path.home() / ".config"
    return base / APP_NAME


def _first_env(*names: str, default: str | None = None) -> str | None:
    for name in names:
        value = os.getenv(name)
        if value is not None:
            return value
    return default


def _env_bool(*names: str, default: bool) -> bool:
    raw = _first_env(*names)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    joined = " or ".join(names)
    raise ValueError(f"{joined} must be a boolean value.")


def _env_int(*names: str, default: int, minimum: int, maximum: int) -> int:
    raw = _first_env(*names)
    try:
        value = default if raw is None else int(raw)
    except ValueError as exc:
        raise ValueError(f"{' or '.join(names)} must be an integer.") from exc
    if not minimum <= value <= maximum:
        raise ValueError(
            f"{' or '.join(names)} must be between {minimum} and {maximum}."
        )
    return value


def _env_float(
    *names: str,
    default: float,
    minimum: float,
    maximum: float,
) -> float:
    raw = _first_env(*names)
    try:
        value = default if raw is None else float(raw)
    except ValueError as exc:
        raise ValueError(f"{' or '.join(names)} must be numeric.") from exc
    if not minimum <= value <= maximum:
        raise ValueError(
            f"{' or '.join(names)} must be between {minimum:g} and {maximum:g}."
        )
    return value


def _loopback_host(value: str | None) -> str:
    host = (value or "127.0.0.1").strip()
    try:
        loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = host.lower() == "localhost"
    if not loopback:
        raise ValueError("SWITCHBOARD_OAUTH_CALLBACK_HOST must be loopback-only.")
    return host


@dataclass(frozen=True, slots=True)
class AppSettings:
    host: str
    port: int
    token: str | None
    max_request_bytes: int
    debug_requests: bool
    session_reuse: bool
    session_ttl_seconds: float
    kiro_cli: str
    kiro_model: str
    kiro_workdir: Path
    kiro_timeout_seconds: float
    kiro_max_concurrency: int
    kiro_max_prompt_bytes: int
    kiro_context_recovery_prompt_bytes: int
    kiro_max_output_bytes: int
    kiro_allow_requested_model: bool
    kiro_tool_batching: bool
    cursor_cli: str
    cursor_workdir: Path
    cursor_max_concurrency: int
    cursor_max_prompt_bytes: int
    cursor_max_output_bytes: int
    cursor_first_output_timeout_seconds: float = 120.0
    kiro_queue_timeout_seconds: float = 60.0
    log_path: Path | None = None
    log_max_bytes: int = 5 * 1_048_576
    log_backup_count: int = 4
    direct_max_concurrency: int = 8
    direct_max_output_bytes: int = 64 * 1_048_576
    direct_oauth_timeout_seconds: float = 900.0
    oauth_callback_host: str = "127.0.0.1"

    @classmethod
    def from_env(cls) -> AppSettings:
        data_dir = default_data_dir()
        token = _first_env("SWITCHBOARD_TOKEN", "KIRO_PROXY_TOKEN")
        default_workdir = str(data_dir / "runtime" / "kiro")
        workdir = _first_env("KIRO_WORKDIR", default=default_workdir) or default_workdir
        default_cursor_workdir = str(data_dir / "runtime" / "cursor-agent")
        cursor_workdir = (
            _first_env("CURSOR_CLI_WORKDIR", default=default_cursor_workdir)
            or default_cursor_workdir
        )
        return cls(
            host=_first_env("SWITCHBOARD_HOST", "KIRO_PROXY_HOST", default="127.0.0.1")
            or "127.0.0.1",
            port=_env_int(
                "SWITCHBOARD_PORT",
                "KIRO_PROXY_PORT",
                default=8787,
                minimum=1,
                maximum=65535,
            ),
            token=token.strip() if token and token.strip() else None,
            max_request_bytes=_env_int(
                "SWITCHBOARD_MAX_REQUEST_BYTES",
                default=8 * 1_048_576,
                minimum=1_024,
                maximum=64 * 1_048_576,
            ),
            debug_requests=_env_bool(
                "SWITCHBOARD_DEBUG_REQUESTS",
                "KIRO_DEBUG_REQUESTS",
                default=False,
            ),
            session_reuse=_env_bool("KIRO_SESSION_REUSE", default=True),
            session_ttl_seconds=_env_float(
                "KIRO_SESSION_TTL_SECONDS",
                default=604_800,
                minimum=0,
                maximum=31_536_000,
            ),
            kiro_cli=_first_env("KIRO_CLI", default="kiro-cli") or "kiro-cli",
            kiro_model=_first_env("KIRO_MODEL", default="gpt-5.6-sol") or "gpt-5.6-sol",
            kiro_workdir=Path(workdir).expanduser(),
            kiro_timeout_seconds=_env_float(
                "KIRO_TIMEOUT_SECONDS",
                default=300,
                minimum=1,
                maximum=7_200,
            ),
            kiro_max_concurrency=_env_int(
                "KIRO_MAX_CONCURRENCY", default=4, minimum=1, maximum=32
            ),
            kiro_max_prompt_bytes=_env_int(
                "KIRO_MAX_PROMPT_BYTES",
                default=4 * 1_048_576,
                minimum=1_024,
                maximum=64 * 1_048_576,
            ),
            kiro_context_recovery_prompt_bytes=_env_int(
                "KIRO_CONTEXT_RECOVERY_PROMPT_BYTES",
                default=768 * 1_024,
                minimum=65_536,
                maximum=64 * 1_048_576,
            ),
            kiro_max_output_bytes=_env_int(
                "KIRO_MAX_OUTPUT_BYTES",
                default=8 * 1_048_576,
                minimum=1_024,
                maximum=64 * 1_048_576,
            ),
            kiro_allow_requested_model=_env_bool(
                "KIRO_ALLOW_REQUESTED_MODEL", default=False
            ),
            kiro_tool_batching=_env_bool("KIRO_TOOL_BATCHING", default=True),
            cursor_cli=_first_env(
                "CURSOR_AGENT_CLI", "CURSOR_CLI", default="cursor-agent"
            )
            or "cursor-agent",
            cursor_workdir=Path(cursor_workdir).expanduser(),
            cursor_max_concurrency=_env_int(
                "CURSOR_CLI_MAX_CONCURRENCY", default=1, minimum=1, maximum=32
            ),
            cursor_max_prompt_bytes=_env_int(
                "CURSOR_CLI_MAX_PROMPT_BYTES",
                default=4 * 1_048_576,
                minimum=1_024,
                maximum=64 * 1_048_576,
            ),
            cursor_max_output_bytes=_env_int(
                "CURSOR_CLI_MAX_OUTPUT_BYTES",
                default=8 * 1_048_576,
                minimum=1_024,
                maximum=64 * 1_048_576,
            ),
            cursor_first_output_timeout_seconds=_env_float(
                "CURSOR_CLI_FIRST_OUTPUT_TIMEOUT_SECONDS",
                default=120,
                minimum=5,
                maximum=7_200,
            ),
            kiro_queue_timeout_seconds=_env_float(
                "KIRO_QUEUE_TIMEOUT_SECONDS",
                default=60,
                minimum=0.1,
                maximum=7_200,
            ),
            log_path=Path(
                _first_env(
                    "SWITCHBOARD_LOG_PATH",
                    default=str(data_dir / "logs" / "switchboard.log"),
                )
                or data_dir / "logs" / "switchboard.log"
            ).expanduser(),
            log_max_bytes=_env_int(
                "SWITCHBOARD_LOG_MAX_BYTES",
                default=5 * 1_048_576,
                minimum=16_384,
                maximum=256 * 1_048_576,
            ),
            log_backup_count=_env_int(
                "SWITCHBOARD_LOG_BACKUP_COUNT",
                default=4,
                minimum=1,
                maximum=20,
            ),
            direct_max_concurrency=_env_int(
                "SWITCHBOARD_DIRECT_MAX_CONCURRENCY",
                default=8,
                minimum=1,
                maximum=64,
            ),
            direct_max_output_bytes=_env_int(
                "SWITCHBOARD_DIRECT_MAX_OUTPUT_BYTES",
                default=64 * 1_048_576,
                minimum=1_024,
                maximum=256 * 1_048_576,
            ),
            direct_oauth_timeout_seconds=_env_float(
                "SWITCHBOARD_OAUTH_TIMEOUT_SECONDS",
                default=900,
                minimum=30,
                maximum=3_600,
            ),
            oauth_callback_host=_loopback_host(
                _first_env("SWITCHBOARD_OAUTH_CALLBACK_HOST", default="127.0.0.1")
            ),
        )
