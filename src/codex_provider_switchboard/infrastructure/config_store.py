from __future__ import annotations

import copy
import ipaddress
import json
import math
import os
import tempfile
import threading
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from ..settings import default_data_dir
from .direct_catalog import DIRECT_PLATFORM_IDS

CONFIG_VERSION = 4
CURSOR_BASE_URL = "https://api.cursor.com"
CURSOR_BACKENDS = frozenset({"cli", "cloud_api"})
PROVIDER_IDS = frozenset({"kiro", "cursor", "custom", "direct"})
_SECRET_LIMIT = 4_096
_TEXT_LIMIT = 500


def default_config_path() -> Path:
    override = os.getenv("SWITCHBOARD_CONFIG") or os.getenv("KIRO_PROXY_CONFIG")
    if override:
        return Path(override).expanduser()
    return default_data_dir() / "config.json"


def _defaults(kiro_model: str) -> dict[str, Any]:
    return {
        "version": CONFIG_VERSION,
        "active_provider": "kiro",
        "kiro": {"model_id": kiro_model},
        "cursor": {
            "backend": "cli",
            "api_key": "",
            "base_url": CURSOR_BASE_URL,
            "model_id": "",
            "model_params": [],
            "model_display_name": "Cursor default",
            "follow_codex_effort": True,
            "timeout_seconds": 1_800,
        },
        "custom": {
            "api_key": "",
            "base_url": "",
            "model_id": "",
            "model_display_name": "Third-party default",
            "models_path": "/models",
            "quota_path": "",
            "quota_total_field": "",
            "quota_used_field": "",
            "quota_remaining_field": "",
            "quota_reset_field": "",
            "quota_unit": "credits",
            "timeout_seconds": 300,
        },
        "direct": {
            "platform_id": "openai_codex",
            "model_id": "gpt-5.6-sol",
            "follow_codex_effort": True,
            "timeout_seconds": 600,
        },
    }


def _normalized_secret(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string.")
    value = value.strip()
    if len(value) > _SECRET_LIMIT:
        raise ValueError(f"{field} is too long.")
    return value


def _normalized_model_id(value: Any, field: str, *, fallback: str = "") -> str:
    if value is None:
        return fallback
    if (
        not isinstance(value, str)
        or len(value.strip()) > 200
        or any(ord(char) < 0x20 for char in value)
    ):
        raise ValueError(f"{field} must be a printable model identifier.")
    return value.strip() or fallback


def _normalized_text(value: Any, field: str, *, fallback: str = "") -> str:
    if value is None:
        return fallback
    if (
        not isinstance(value, str)
        or len(value.strip()) > _TEXT_LIMIT
        or any(ord(char) < 0x20 for char in value)
    ):
        raise ValueError(f"{field} must be a short printable string.")
    return value.strip() or fallback


def _normalized_params(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list) or len(value) > 20:
        raise ValueError("cursor.model_params must contain at most 20 items.")
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, dict) or set(item) != {"id", "value"}:
            raise ValueError("Each Cursor model parameter needs id and value fields.")
        param_id = item["id"]
        param_value = item["value"]
        if (
            not isinstance(param_id, str)
            or not param_id.strip()
            or len(param_id) > 200
            or any(ord(char) < 0x20 for char in param_id)
        ):
            raise ValueError("Cursor model parameter IDs must be printable strings.")
        param_id = param_id.strip()
        if param_id in seen:
            raise ValueError(f"Duplicate Cursor model parameter ID: {param_id}")
        if not isinstance(param_value, (str, int, float, bool)):
            raise ValueError("Cursor model parameter values must be JSON scalars.")
        if isinstance(param_value, str) and len(param_value) > 2_000:
            raise ValueError("Cursor model parameter values are too long.")
        if isinstance(param_value, float) and not math.isfinite(param_value):
            raise ValueError("Cursor model parameter numbers must be finite.")
        normalized.append({"id": param_id, "value": param_value})
        seen.add(param_id)
    return normalized


def _normalized_cursor_base_url(value: Any) -> str:
    if value in {None, "", CURSOR_BASE_URL, f"{CURSOR_BASE_URL}/"}:
        return CURSOR_BASE_URL
    raise ValueError(f"cursor.base_url is pinned to {CURSOR_BASE_URL}.")


def _normalized_custom_base_url(value: Any) -> str:
    if value in {None, ""}:
        return ""
    if not isinstance(value, str) or len(value) > 2_000:
        raise ValueError("custom.base_url must be a valid URL.")
    parsed = urlsplit(value.strip())
    if (
        not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or (parsed.path not in {"", "/"} and not parsed.path.startswith("/"))
    ):
        raise ValueError(
            "custom.base_url must not contain credentials, query, or fragment."
        )
    is_loopback = False
    try:
        is_loopback = ipaddress.ip_address(parsed.hostname).is_loopback
    except ValueError:
        is_loopback = parsed.hostname.lower() == "localhost"
    if parsed.scheme != "https" and not (parsed.scheme == "http" and is_loopback):
        raise ValueError(
            "custom.base_url requires HTTPS (HTTP is allowed on loopback only)."
        )
    return value.strip().rstrip("/")


def _normalized_endpoint_path(
    value: Any,
    field: str,
    *,
    fallback: str,
    allow_empty: bool,
) -> str:
    if value in {None, ""}:
        return "" if allow_empty else fallback
    if not isinstance(value, str) or len(value) > 500:
        raise ValueError(f"{field} must be a relative endpoint path.")
    value = value.strip()
    parsed = urlsplit(value)
    if (
        not value.startswith("/")
        or value.startswith("//")
        or parsed.scheme
        or parsed.netloc
        or parsed.query
        or parsed.fragment
        or any(ord(char) < 0x20 for char in value)
    ):
        raise ValueError(f"{field} must be a same-origin path beginning with '/'.")
    return value


def _normalized_json_path(value: Any, field: str) -> str:
    if value in {None, ""}:
        return ""
    if not isinstance(value, str) or len(value) > 300:
        raise ValueError(f"{field} must be a dotted JSON path.")
    parts = value.strip().split(".")
    if any(
        not part
        or len(part) > 100
        or any(char not in "_-" and not char.isalnum() for char in part)
        for part in parts
    ):
        raise ValueError(f"{field} must contain only dotted JSON keys or indexes.")
    return ".".join(parts)


def _normalized_timeout(value: Any, field: str, *, fallback: int) -> int:
    if value is None:
        return fallback
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(float(value))
    ):
        raise ValueError(f"{field} must be numeric.")
    return min(7_200, max(30, int(value)))


class ConfigStore:
    """Atomic, permission-restricted storage for local provider settings."""

    def __init__(
        self,
        path: Path | None = None,
        *,
        kiro_model: str = "gpt-5.6-sol",
    ) -> None:
        self.path = path or default_config_path()
        self.kiro_model = _normalized_model_id(
            kiro_model, "kiro.model_id", fallback="gpt-5.6-sol"
        )
        self._lock = threading.RLock()

    @property
    def runtime_dir(self) -> Path:
        return self.path.parent / "runtime"

    def _normalized(self, value: Any) -> dict[str, Any]:
        result = _defaults(self.kiro_model)
        if not isinstance(value, dict) or value.get("version") not in {1, 2, 3, 4}:
            return result

        provider = value.get("active_provider")
        if provider in PROVIDER_IDS:
            result["active_provider"] = provider

        kiro = value.get("kiro")
        if isinstance(kiro, dict):
            result["kiro"]["model_id"] = _normalized_model_id(
                kiro.get("model_id"), "kiro.model_id", fallback=self.kiro_model
            )

        cursor = value.get("cursor")
        if isinstance(cursor, dict):
            backend = cursor.get("backend")
            if backend in CURSOR_BACKENDS:
                result["cursor"]["backend"] = backend
            result["cursor"]["api_key"] = _normalized_secret(
                cursor.get("api_key", ""), "cursor.api_key"
            )
            result["cursor"]["base_url"] = _normalized_cursor_base_url(
                cursor.get("base_url")
            )
            result["cursor"]["model_id"] = _normalized_model_id(
                cursor.get("model_id"), "cursor.model_id"
            )
            result["cursor"]["model_display_name"] = _normalized_text(
                cursor.get("model_display_name"),
                "cursor.model_display_name",
                fallback="Cursor default",
            )
            result["cursor"]["model_params"] = _normalized_params(
                cursor.get("model_params", [])
            )
            if isinstance(cursor.get("follow_codex_effort"), bool):
                result["cursor"]["follow_codex_effort"] = cursor["follow_codex_effort"]
            result["cursor"]["timeout_seconds"] = _normalized_timeout(
                cursor.get("timeout_seconds"),
                "cursor.timeout_seconds",
                fallback=1_800,
            )

        custom = value.get("custom")
        if isinstance(custom, dict):
            result["custom"]["api_key"] = _normalized_secret(
                custom.get("api_key", ""), "custom.api_key"
            )
            result["custom"]["base_url"] = _normalized_custom_base_url(
                custom.get("base_url")
            )
            result["custom"]["model_id"] = _normalized_model_id(
                custom.get("model_id"), "custom.model_id"
            )
            result["custom"]["model_display_name"] = _normalized_text(
                custom.get("model_display_name"),
                "custom.model_display_name",
                fallback="Third-party default",
            )
            result["custom"]["models_path"] = _normalized_endpoint_path(
                custom.get("models_path"),
                "custom.models_path",
                fallback="/models",
                allow_empty=False,
            )
            result["custom"]["quota_path"] = _normalized_endpoint_path(
                custom.get("quota_path"),
                "custom.quota_path",
                fallback="",
                allow_empty=True,
            )
            for key in (
                "quota_total_field",
                "quota_used_field",
                "quota_remaining_field",
                "quota_reset_field",
            ):
                result["custom"][key] = _normalized_json_path(
                    custom.get(key), f"custom.{key}"
                )
            result["custom"]["quota_unit"] = _normalized_text(
                custom.get("quota_unit"), "custom.quota_unit", fallback="credits"
            )
            result["custom"]["timeout_seconds"] = _normalized_timeout(
                custom.get("timeout_seconds"),
                "custom.timeout_seconds",
                fallback=300,
            )

        direct = value.get("direct")
        if isinstance(direct, dict):
            platform_id = direct.get("platform_id")
            if platform_id in DIRECT_PLATFORM_IDS:
                result["direct"]["platform_id"] = platform_id
            result["direct"]["model_id"] = _normalized_model_id(
                direct.get("model_id"),
                "direct.model_id",
                fallback=result["direct"]["model_id"],
            )
            if isinstance(direct.get("follow_codex_effort"), bool):
                result["direct"]["follow_codex_effort"] = direct["follow_codex_effort"]
            result["direct"]["timeout_seconds"] = _normalized_timeout(
                direct.get("timeout_seconds"),
                "direct.timeout_seconds",
                fallback=600,
            )
        return result

    def read(self) -> dict[str, Any]:
        with self._lock:
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                normalized = self._normalized(raw)
            except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
                normalized = _defaults(self.kiro_model)
            return copy.deepcopy(normalized)

    def write(self, value: dict[str, Any]) -> dict[str, Any]:
        normalized = self._normalized(value)
        encoded = (
            json.dumps(normalized, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
        )
        with self._lock:
            parent_existed = self.path.parent.exists()
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if not parent_existed or self.path == default_data_dir() / "config.json":
                os.chmod(self.path.parent, 0o700)
            temporary_path: Path | None = None
            try:
                with tempfile.NamedTemporaryFile(
                    mode="w",
                    encoding="utf-8",
                    dir=self.path.parent,
                    prefix=f".{self.path.name}.",
                    suffix=".tmp",
                    delete=False,
                ) as temporary:
                    temporary_path = Path(temporary.name)
                    os.chmod(temporary_path, 0o600)
                    temporary.write(encoded)
                    temporary.flush()
                    os.fsync(temporary.fileno())
                os.replace(temporary_path, self.path)
                os.chmod(self.path, 0o600)
            finally:
                if temporary_path is not None and temporary_path.exists():
                    temporary_path.unlink(missing_ok=True)
        return copy.deepcopy(normalized)

    @staticmethod
    def _apply_secret_update(target: dict[str, Any], update: dict[str, Any]) -> None:
        if update.get("clear_api_key") is True:
            target["api_key"] = ""
        api_key = update.get("api_key")
        if api_key is not None:
            normalized = _normalized_secret(api_key, "api_key")
            if normalized:
                target["api_key"] = normalized

    def update_from_api(self, payload: dict[str, Any]) -> dict[str, Any]:
        unknown = set(payload) - {
            "active_provider",
            "kiro",
            "cursor",
            "custom",
            "direct",
        }
        if unknown:
            raise ValueError(f"Unknown settings field: {sorted(unknown)[0]}")

        current = self.read()
        provider = payload.get("active_provider")
        if provider is not None:
            if provider not in PROVIDER_IDS:
                raise ValueError(
                    "active_provider must be kiro, cursor, custom, or direct."
                )
            current["active_provider"] = provider

        kiro_update = payload.get("kiro")
        if kiro_update is not None:
            if not isinstance(kiro_update, dict):
                raise ValueError("kiro settings must be an object.")
            unknown_kiro = set(kiro_update) - {"model_id"}
            if unknown_kiro:
                raise ValueError(
                    f"Unknown kiro settings field: {sorted(unknown_kiro)[0]}"
                )
            if "model_id" in kiro_update:
                current["kiro"]["model_id"] = kiro_update["model_id"]

        cursor_update = payload.get("cursor")
        if cursor_update is not None:
            if not isinstance(cursor_update, dict):
                raise ValueError("cursor settings must be an object.")
            allowed = {
                "backend",
                "api_key",
                "clear_api_key",
                "base_url",
                "model_id",
                "model_params",
                "model_display_name",
                "follow_codex_effort",
                "timeout_seconds",
            }
            unknown_cursor = set(cursor_update) - allowed
            if unknown_cursor:
                raise ValueError(
                    f"Unknown cursor settings field: {sorted(unknown_cursor)[0]}"
                )
            cursor = current["cursor"]
            self._apply_secret_update(cursor, cursor_update)
            backend = cursor_update.get("backend")
            if backend is not None and backend not in CURSOR_BACKENDS:
                raise ValueError("cursor.backend must be cli or cloud_api.")
            for key in allowed - {"api_key", "clear_api_key"}:
                if key in cursor_update:
                    cursor[key] = cursor_update[key]

        custom_update = payload.get("custom")
        if custom_update is not None:
            if not isinstance(custom_update, dict):
                raise ValueError("custom settings must be an object.")
            allowed = {
                "api_key",
                "clear_api_key",
                "base_url",
                "model_id",
                "model_display_name",
                "models_path",
                "quota_path",
                "quota_total_field",
                "quota_used_field",
                "quota_remaining_field",
                "quota_reset_field",
                "quota_unit",
                "timeout_seconds",
            }
            unknown_custom = set(custom_update) - allowed
            if unknown_custom:
                raise ValueError(
                    f"Unknown custom settings field: {sorted(unknown_custom)[0]}"
                )
            custom = current["custom"]
            self._apply_secret_update(custom, custom_update)
            for key in allowed - {"api_key", "clear_api_key"}:
                if key in custom_update:
                    custom[key] = custom_update[key]

        direct_update = payload.get("direct")
        if direct_update is not None:
            if not isinstance(direct_update, dict):
                raise ValueError("direct settings must be an object.")
            allowed = {
                "platform_id",
                "model_id",
                "follow_codex_effort",
                "timeout_seconds",
            }
            unknown_direct = set(direct_update) - allowed
            if unknown_direct:
                raise ValueError(
                    f"Unknown direct settings field: {sorted(unknown_direct)[0]}"
                )
            platform_id = direct_update.get("platform_id")
            if platform_id is not None and platform_id not in DIRECT_PLATFORM_IDS:
                raise ValueError("direct.platform_id is not supported.")
            for key in allowed:
                if key in direct_update:
                    current["direct"][key] = direct_update[key]
        return self.write(current)

    def api_key(self) -> str:
        """Return the Cursor key (kept for compatibility with earlier releases)."""
        environment_key = os.getenv("CURSOR_API_KEY")
        if environment_key is not None:
            return environment_key.strip()
        return str(self.read()["cursor"]["api_key"]).strip()

    def custom_api_key(self) -> str:
        for name in ("THIRD_PARTY_API_KEY", "CUSTOM_API_KEY"):
            environment_key = os.getenv(name)
            if environment_key is not None:
                return environment_key.strip()
        return str(self.read()["custom"]["api_key"]).strip()

    @staticmethod
    def _secret_source(section: dict[str, Any], env_names: tuple[str, ...]) -> str:
        if any(os.getenv(name) for name in env_names):
            return "environment"
        if section.get("api_key"):
            return "config"
        return "none"

    def safe_view(self) -> dict[str, Any]:
        value = self.read()
        cursor = value["cursor"]
        custom = value["custom"]
        safe = {
            **value,
            "config_path": str(self.path),
            "cursor": {
                **cursor,
                "api_key_configured": bool(self.api_key()),
                "api_key_source": self._secret_source(cursor, ("CURSOR_API_KEY",)),
            },
            "custom": {
                **custom,
                "api_key_configured": bool(self.custom_api_key()),
                "api_key_source": self._secret_source(
                    custom, ("THIRD_PARTY_API_KEY", "CUSTOM_API_KEY")
                ),
            },
        }
        safe["cursor"].pop("api_key", None)
        safe["custom"].pop("api_key", None)
        return safe
