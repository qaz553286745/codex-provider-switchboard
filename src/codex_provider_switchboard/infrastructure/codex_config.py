from __future__ import annotations

import copy
import hashlib
import json
import os
import tempfile
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import tomlkit
from tomlkit.exceptions import ParseError

from ..settings import AppSettings

PROVIDER_ID = "codex-provider-switchboard"
_STATE_VERSION = 2
_LEGACY_STATE_VERSION = 1
_MAX_CONFIG_BYTES = 4 * 1_048_576
_RESERVED_PROVIDER_IDS = {"openai", "ollama", "lmstudio", "amazon-bedrock"}


class CodexConfigError(RuntimeError):
    def __init__(self, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


@dataclass(frozen=True, slots=True)
class _ManagementPlan:
    mode: str
    provider_id: str
    top_level: dict[str, Any]
    table_entries: dict[str, dict[str, Any]]
    provider_entries: dict[str, dict[str, Any]]

    def state_value(self) -> dict[str, Any]:
        return {
            "top_level": self.top_level,
            "table_entries": self.table_entries,
            "provider_entries": self.provider_entries,
        }


def default_codex_config_path() -> Path:
    override = os.getenv("SWITCHBOARD_CODEX_CONFIG")
    if override:
        return Path(override).expanduser()
    codex_home = os.getenv("CODEX_HOME")
    if codex_home:
        return Path(codex_home).expanduser() / "config.toml"
    return Path.home() / ".codex" / "config.toml"


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _atomic_write(path: Path, value: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            os.chmod(temporary_path, 0o600)
            temporary.write(value)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_path, path)
        os.chmod(path, 0o600)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink(missing_ok=True)


def _unwrapped(value: Any) -> Any:
    unwrap = getattr(value, "unwrap", None)
    return unwrap() if callable(unwrap) else value


class CodexConfigManager:
    """Apply and restore only the Codex fields owned by Switchboard."""

    def __init__(
        self,
        settings: AppSettings,
        state_path: Path,
        *,
        config_path: Path | None = None,
    ) -> None:
        self.settings = settings
        self.config_path = config_path or default_codex_config_path()
        self.state_path = state_path
        self._lock = threading.RLock()

    def _base_url(self) -> str:
        host = self.settings.host
        if host in {"0.0.0.0", "::", "[::]"}:  # noqa: S104 - normalize only
            host = "127.0.0.1"
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        return f"http://{host}:{self.settings.port}/v1"

    def _provider_values(self) -> dict[str, Any]:
        provider: dict[str, Any] = {
            "name": "Local Codex Provider Switchboard",
            "base_url": self._base_url(),
            "wire_api": "responses",
            "requires_openai_auth": False,
            "supports_websockets": True,
            "request_max_retries": 0,
            "stream_max_retries": 0,
        }
        if self.settings.token:
            provider["env_key"] = "SWITCHBOARD_TOKEN"
        return provider

    def _read_state(self) -> dict[str, Any] | None:
        try:
            raw = self.state_path.read_bytes()
            if len(raw) > 128 * 1_024:
                return None
            value = json.loads(raw)
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            return None
        if not isinstance(value, dict) or value.get("version") not in {
            _LEGACY_STATE_VERSION,
            _STATE_VERSION,
        }:
            return None
        return value

    def _write_state(self, value: dict[str, Any]) -> None:
        encoded = (
            json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        ).encode()
        _atomic_write(self.state_path, encoded)

    def _parse_document(self, raw: bytes) -> Any:
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise CodexConfigError("Codex config must be UTF-8 encoded.") from exc
        try:
            return tomlkit.parse(text) if text else tomlkit.document()
        except ParseError as exc:
            raise CodexConfigError(
                "Codex config is not valid TOML; no files were changed."
            ) from exc

    def _read_config(self) -> tuple[bool, bytes, Any]:
        if self.config_path.is_symlink():
            raise CodexConfigError(
                "Codex config symlinks are not modified automatically; "
                "use a regular file."
            )
        try:
            raw = self.config_path.read_bytes()
        except FileNotFoundError:
            return False, b"", tomlkit.document()
        except OSError as exc:
            raise CodexConfigError(
                f"Could not read Codex config: {type(exc).__name__}."
            ) from exc
        if len(raw) > _MAX_CONFIG_BYTES:
            raise CodexConfigError("Codex config exceeds the safety byte limit.")
        return True, raw, self._parse_document(raw)

    @staticmethod
    def _model(value: Any) -> str:
        if (
            not isinstance(value, str)
            or not value.strip()
            or len(value.strip()) > 200
            or any(ord(char) < 0x20 for char in value)
        ):
            raise CodexConfigError("A printable Codex model identifier is required.")
        return value.strip()

    def _plan(self, document: Any, model: str) -> _ManagementPlan:
        configured = _unwrapped(document.get("model_provider"))
        if (
            isinstance(configured, str)
            and configured
            and configured not in _RESERVED_PROVIDER_IDS
        ):
            return _ManagementPlan(
                mode="existing_provider",
                provider_id=configured,
                top_level={"model": model, "model_provider": configured},
                table_entries={},
                provider_entries={configured: self._provider_values()},
            )
        return _ManagementPlan(
            mode="dedicated_provider",
            provider_id=PROVIDER_ID,
            top_level={"model": model, "model_provider": PROVIDER_ID},
            table_entries={},
            provider_entries={PROVIDER_ID: self._provider_values()},
        )

    def _managed_from_state(self, state: dict[str, Any]) -> dict[str, Any]:
        if state.get("version") == _STATE_VERSION:
            managed = state.get("managed_fields")
            if isinstance(managed, dict):
                top_level = managed.get("top_level")
                table_entries = managed.get("table_entries", {})
                entries = managed.get("provider_entries")
                if (
                    isinstance(top_level, dict)
                    and isinstance(table_entries, dict)
                    and all(
                        isinstance(name, str) and isinstance(value, dict)
                        for name, value in table_entries.items()
                    )
                    and isinstance(entries, dict)
                ):
                    return {
                        "top_level": copy.deepcopy(top_level),
                        "table_entries": copy.deepcopy(table_entries),
                        "provider_entries": copy.deepcopy(entries),
                    }
            raise CodexConfigError("The Codex takeover state is invalid.")

        model = self._model(state.get("model"))
        legacy_provider = self._provider_values()
        legacy_provider["supports_websockets"] = False
        return {
            "top_level": {"model": model, "model_provider": PROVIDER_ID},
            "table_entries": {},
            "provider_entries": {PROVIDER_ID: legacy_provider},
        }

    @staticmethod
    def _provider_entry(document: Any, provider_id: str) -> tuple[bool, Any]:
        providers = document.get("model_providers")
        if providers is None or not hasattr(providers, "__contains__"):
            return False, None
        if provider_id not in providers:
            return False, None
        return True, _unwrapped(providers[provider_id])

    def _matches_managed(self, document: Any, managed: dict[str, Any]) -> bool:
        for key, expected in managed["top_level"].items():
            if key not in document or _unwrapped(document[key]) != expected:
                return False
        for table_name, entries in managed["table_entries"].items():
            table = document.get(table_name)
            if table is None or not hasattr(table, "__contains__"):
                return False
            for key, expected in entries.items():
                if key not in table or _unwrapped(table[key]) != expected:
                    return False
        for provider_id, expected in managed["provider_entries"].items():
            present, current = self._provider_entry(document, provider_id)
            if not present or current != expected:
                return False
        return True

    @staticmethod
    def _set_provider_entry(
        document: Any, provider_id: str, value: dict[str, Any]
    ) -> None:
        providers = document.get("model_providers")
        if providers is None or not hasattr(providers, "__setitem__"):
            providers = tomlkit.table()
            document["model_providers"] = providers
        provider = tomlkit.table()
        for key, item in value.items():
            provider.add(key, item)
        providers[provider_id] = provider

    def _apply_managed(self, document: Any, managed: dict[str, Any]) -> None:
        for key, value in managed["top_level"].items():
            document[key] = value
        for table_name, entries in managed["table_entries"].items():
            table = document.get(table_name)
            if table is None or not hasattr(table, "__setitem__"):
                table = tomlkit.table()
                document[table_name] = table
            for key, value in entries.items():
                table[key] = value
        for provider_id, value in managed["provider_entries"].items():
            self._set_provider_entry(document, provider_id, value)

    def _restore_managed(
        self,
        current: Any,
        original: Any,
        managed: dict[str, Any],
    ) -> None:
        for key in managed["top_level"]:
            if key in original:
                current[key] = copy.deepcopy(original[key])
            elif key in current:
                del current[key]

        for table_name, entries in managed["table_entries"].items():
            original_table = original.get(table_name)
            current_table = current.get(table_name)
            for key in entries:
                original_has = bool(
                    original_table is not None
                    and hasattr(original_table, "__contains__")
                    and key in original_table
                )
                if original_has:
                    if current_table is None or not hasattr(
                        current_table, "__setitem__"
                    ):
                        current_table = tomlkit.table()
                        current[table_name] = current_table
                    current_table[key] = copy.deepcopy(original_table[key])
                elif current_table is not None and key in current_table:
                    del current_table[key]
            if (
                current_table is not None
                and not current_table
                and table_name not in original
            ):
                del current[table_name]

        original_providers = original.get("model_providers")
        current_providers = current.get("model_providers")
        for provider_id in managed["provider_entries"]:
            original_has = bool(
                original_providers is not None
                and hasattr(original_providers, "__contains__")
                and provider_id in original_providers
            )
            if original_has:
                if current_providers is None or not hasattr(
                    current_providers, "__setitem__"
                ):
                    current_providers = tomlkit.table()
                    current["model_providers"] = current_providers
                current_providers[provider_id] = copy.deepcopy(
                    original_providers[provider_id]
                )
            elif current_providers is not None and provider_id in current_providers:
                del current_providers[provider_id]

        if (
            current_providers is not None
            and not current_providers
            and "model_providers" not in original
        ):
            del current["model_providers"]

    def _backup_document(self, state: dict[str, Any]) -> Any | None:
        if state.get("original_existed") is not True:
            return tomlkit.document()
        backup_value = state.get("backup_path")
        if not isinstance(backup_value, str) or not backup_value:
            return None
        try:
            backup = Path(backup_value).read_bytes()
        except OSError:
            return None
        expected_hash = state.get("backup_sha256") or state.get("original_sha256")
        if not isinstance(expected_hash, str) or _sha256(backup) != expected_hash:
            return None
        try:
            return self._parse_document(backup)
        except CodexConfigError:
            return None

    def _managed_route_present(
        self,
        document: Any,
        state: dict[str, Any],
        managed: dict[str, Any],
    ) -> bool:
        """Return whether Codex still routes through this takeover.

        This deliberately ignores the managed model value. Codex can rewrite or
        a user can change a model without changing the active provider route.
        Conversely, a stale state file must not keep the UI locked after the
        Switchboard routing keys have already been removed.
        """

        top_level = managed["top_level"]
        if "openai_base_url" in top_level:
            return (
                "openai_base_url" in document
                and _unwrapped(document["openai_base_url"])
                == top_level["openai_base_url"]
            )

        provider_ids = set(managed["provider_entries"])
        configured = _unwrapped(document.get("model_provider"))
        if configured not in provider_ids:
            return False

        mode = str(state.get("management_mode") or "legacy")
        if mode in {"dedicated_provider", "legacy"}:
            return True

        expected = managed["provider_entries"].get(configured)
        present, current = self._provider_entry(document, configured)
        return bool(
            present
            and isinstance(expected, dict)
            and isinstance(current, dict)
            and current.get("base_url") == expected.get("base_url")
        )

    @staticmethod
    def _remove_matching_managed_values(
        document: Any,
        managed: dict[str, Any],
    ) -> None:
        """Conservatively detach routing when the verified backup is unavailable.

        Only values that still equal Switchboard's recorded takeover values are
        removed. The model is retained because its pre-takeover value cannot be
        reconstructed without the backup.
        """

        for key, expected in managed["top_level"].items():
            if key == "model":
                continue
            if key in document and _unwrapped(document[key]) == expected:
                del document[key]

        for table_name, entries in managed["table_entries"].items():
            table = document.get(table_name)
            if table is None or not hasattr(table, "__contains__"):
                continue
            for key, expected in entries.items():
                if key in table and _unwrapped(table[key]) == expected:
                    del table[key]
            if not table:
                del document[table_name]

        providers = document.get("model_providers")
        if providers is None or not hasattr(providers, "__contains__"):
            return
        for provider_id, expected in managed["provider_entries"].items():
            if provider_id not in providers:
                continue
            provider = providers[provider_id]
            if not hasattr(provider, "__contains__"):
                continue
            for key, value in expected.items():
                if key in provider and _unwrapped(provider[key]) == value:
                    del provider[key]
            if not provider:
                del providers[provider_id]
        if not providers:
            del document["model_providers"]

    def _matches_original_fields(
        self,
        document: Any,
        state: dict[str, Any],
        managed: dict[str, Any],
    ) -> bool:
        original = self._backup_document(state)
        if original is not None:
            for key in managed["top_level"]:
                candidate_present = key in document
                original_present = key in original
                if candidate_present != original_present:
                    return False
                if original_present and _unwrapped(document[key]) != _unwrapped(
                    original[key]
                ):
                    return False
            for table_name, entries in managed["table_entries"].items():
                candidate_table = document.get(table_name)
                original_table = original.get(table_name)
                for key in entries:
                    candidate_present = bool(
                        candidate_table is not None
                        and hasattr(candidate_table, "__contains__")
                        and key in candidate_table
                    )
                    original_present = bool(
                        original_table is not None
                        and hasattr(original_table, "__contains__")
                        and key in original_table
                    )
                    if candidate_present != original_present:
                        return False
                    if original_present and _unwrapped(
                        candidate_table[key]
                    ) != _unwrapped(original_table[key]):
                        return False
            for provider_id in managed["provider_entries"]:
                current_present, current_value = self._provider_entry(
                    document, provider_id
                )
                original_present, original_value = self._provider_entry(
                    original, provider_id
                )
                if current_present != original_present or (
                    current_present and current_value != original_value
                ):
                    return False
            return True

        if state.get("version") == _LEGACY_STATE_VERSION:
            provider_present, _ = self._provider_entry(document, PROVIDER_ID)
            return (
                _unwrapped(document.get("model_provider")) != PROVIDER_ID
                and not provider_present
            )
        return False

    def _reconcile_external_restore(
        self,
        state: dict[str, Any],
        document: Any,
    ) -> dict[str, Any]:
        if state.get("active") is not True:
            return state
        managed = self._managed_from_state(state)
        if self._matches_managed(document, managed):
            return state
        if self._matches_original_fields(document, state, managed):
            restore_method = "external"
        elif not self._managed_route_present(document, state, managed):
            restore_method = "external_detach"
        else:
            return state
        reconciled = {**state}
        reconciled["active"] = False
        reconciled["restored_at"] = datetime.now(UTC).isoformat()
        reconciled["restore_method"] = restore_method
        self._write_state(reconciled)
        return reconciled

    def _status_locked(self) -> dict[str, Any]:
        state = self._read_state()
        document: Any | None = None
        try:
            _, _, document = self._read_config()
        except CodexConfigError:
            document = None
        if state and document is not None:
            state = self._reconcile_external_restore(state, document)

        active = bool(state and state.get("active") is True)
        matches = False
        if active and document is not None and state is not None:
            try:
                matches = self._matches_managed(
                    document, self._managed_from_state(state)
                )
            except CodexConfigError:
                matches = False
        backup_path = str(state.get("backup_path") or "") if state else ""
        backup_exists = bool(backup_path and Path(backup_path).is_file())
        mode = str(state.get("management_mode") or "legacy") if state else None
        provider_id = (
            str(state.get("target_provider_id") or PROVIDER_ID)
            if state
            else PROVIDER_ID
        )
        managed_names: list[str] = []
        if state:
            try:
                managed = self._managed_from_state(state)
                managed_names.extend(sorted(managed["top_level"]))
                managed_names.extend(
                    f"{table_name}.{key}"
                    for table_name in sorted(managed["table_entries"])
                    for key in sorted(managed["table_entries"][table_name])
                )
                managed_names.extend(
                    f"model_providers.{provider_id}"
                    for provider_id in sorted(managed["provider_entries"])
                )
            except CodexConfigError:
                pass
        backup_available = bool(
            state is not None
            and (state.get("original_existed") is not True or backup_exists)
        )
        return {
            "config_path": str(self.config_path),
            "provider_id": provider_id,
            "proxy_base_url": self._base_url(),
            "active": active,
            "current_matches_managed": matches,
            "modified_after_enable": active and not matches,
            "backup_path": backup_path or None,
            "backup_exists": backup_exists,
            "can_restore": active and backup_available,
            "can_disable": active,
            "restore_strategy": (
                "field_level"
                if active and backup_available
                else "managed_cleanup"
                if active
                else None
            ),
            "management_mode": mode,
            "history_provider_preserved": mode
            in {"builtin_openai_base_url", "existing_provider"},
            "managed_fields": managed_names,
            "enabled_at": state.get("enabled_at") if state else None,
            "restored_at": state.get("restored_at") if state else None,
            "restore_method": state.get("restore_method") if state else None,
            "restore_warning": state.get("restore_warning") if state else None,
            "confirmation_enable": "ENABLE",
            "confirmation_restore": "RESTORE",
        }

    def status(self) -> dict[str, Any]:
        with self._lock:
            return self._status_locked()

    def enable(self, *, confirmation: Any, model: Any) -> dict[str, Any]:
        if confirmation != "ENABLE":
            raise CodexConfigError("Type ENABLE to confirm Codex config takeover.")
        normalized_model = self._model(model)
        with self._lock:
            existing_state = self._read_state()
            if existing_state and existing_state.get("active") is True:
                _, _, current_document = self._read_config()
                existing_state = self._reconcile_external_restore(
                    existing_state, current_document
                )
                if existing_state.get("active") is True:
                    managed = self._managed_from_state(existing_state)
                    if not self._matches_managed(current_document, managed):
                        raise CodexConfigError(
                            "A Switchboard-managed Codex field changed after "
                            "takeover; refusing to overwrite it.",
                            status_code=409,
                        )
                    return self._status_locked()

            original_existed, original, document = self._read_config()
            plan = self._plan(document, normalized_model)
            managed = plan.state_value()
            self._apply_managed(document, managed)
            managed_bytes = tomlkit.dumps(document).encode("utf-8")

            timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
            backup_path: Path | None = None
            if original_existed:
                backup_path = self.config_path.with_name(
                    f"{self.config_path.name}.switchboard-backup-{timestamp}"
                )
                try:
                    with backup_path.open("xb") as backup:
                        os.chmod(backup_path, 0o600)
                        backup.write(original)
                        backup.flush()
                        os.fsync(backup.fileno())
                except OSError as exc:
                    raise CodexConfigError(
                        f"Could not create Codex config backup: {type(exc).__name__}."
                    ) from exc

            state = {
                "version": _STATE_VERSION,
                "active": True,
                "config_path": str(self.config_path),
                "target_provider_id": plan.provider_id,
                "management_mode": plan.mode,
                "model": normalized_model,
                "managed_fields": managed,
                "original_existed": original_existed,
                "original_sha256": _sha256(original) if original_existed else None,
                "backup_sha256": _sha256(original) if original_existed else None,
                "backup_path": str(backup_path) if backup_path else None,
                "enabled_at": datetime.now(UTC).isoformat(),
            }
            try:
                _atomic_write(self.config_path, managed_bytes)
                self._write_state(state)
            except OSError as exc:
                try:
                    if original_existed:
                        _atomic_write(self.config_path, original)
                    else:
                        self.config_path.unlink(missing_ok=True)
                except OSError:
                    pass
                raise CodexConfigError(
                    f"Could not activate Codex config: {type(exc).__name__}."
                ) from exc
            return self._status_locked()

    def disable(self, *, confirmation: Any) -> dict[str, Any]:
        if confirmation != "RESTORE":
            raise CodexConfigError("Type RESTORE to confirm backup restoration.")
        with self._lock:
            state = self._read_state()
            if not state or state.get("active") is not True:
                return self._status_locked()
            if state.get("config_path") != str(self.config_path):
                raise CodexConfigError(
                    "Managed Codex config path does not match the current path.",
                    status_code=409,
                )

            _, _, current = self._read_config()
            state = self._reconcile_external_restore(state, current)
            if state.get("active") is not True:
                return self._status_locked()
            managed = self._managed_from_state(state)
            original = self._backup_document(state)
            if original is not None:
                self._restore_managed(current, original, managed)
                restore_method = "field_level"
                restore_warning = None
            else:
                self._remove_matching_managed_values(current, managed)
                restore_method = "managed_cleanup"
                restore_warning = "backup_missing_or_invalid"
            restored_bytes = tomlkit.dumps(current).encode("utf-8")
            if state.get("original_existed") is not True and not restored_bytes.strip():
                self.config_path.unlink(missing_ok=True)
            else:
                _atomic_write(self.config_path, restored_bytes)

            restored_state = {**state}
            restored_state["version"] = _STATE_VERSION
            restored_state["active"] = False
            restored_state["restored_at"] = datetime.now(UTC).isoformat()
            restored_state["restore_method"] = restore_method
            if restore_warning is None:
                restored_state.pop("restore_warning", None)
            else:
                restored_state["restore_warning"] = restore_warning
            self._write_state(restored_state)
            return self._status_locked()
