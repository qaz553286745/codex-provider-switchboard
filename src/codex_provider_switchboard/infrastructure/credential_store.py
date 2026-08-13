from __future__ import annotations

import copy
import json
import os
import tempfile
import threading
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .direct_catalog import DIRECT_PLATFORM_IDS, direct_platform

try:  # pragma: no cover - Windows fallback is exercised by type checking.
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]

_VERSION = 1
_MAX_FILE_BYTES = 512 * 1_024
_MAX_SECRET_CHARS = 32_768
_MAX_EXTRA_BYTES = 128 * 1_024
_PUBLIC_EXTRA_FIELDS = frozenset(
    {
        "account_id",
        "auth_method",
        "enterprise_domain",
        "region",
        "subscription",
    }
)


class CredentialStoreError(RuntimeError):
    pass


def _timestamp() -> str:
    return datetime.now(UTC).isoformat()


def _secret(value: Any, field: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be a string.")
    result = value.strip()
    if not allow_empty and not result:
        raise ValueError(f"{field} must not be empty.")
    if len(result) > _MAX_SECRET_CHARS or any(ord(char) < 0x20 for char in result):
        raise ValueError(f"{field} is invalid or too long.")
    return result


def _json_extra(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError("OAuth extra data must be an object.")
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as exc:
        raise ValueError("OAuth extra data must be JSON serializable.") from exc
    if len(encoded) > _MAX_EXTRA_BYTES:
        raise ValueError("OAuth extra data is too large.")
    return copy.deepcopy(value)


class CredentialStore:
    """Atomic credential storage kept separate from ordinary app settings.

    The file is deliberately local-only and permission restricted. It is not an
    encrypted vault; environment variables or an OS secret manager remain better
    choices on shared machines. No caller should return ``read()`` values to the UI.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock_path = path.with_suffix(f"{path.suffix}.lock")
        self._thread_lock = threading.RLock()

    @staticmethod
    def _empty() -> dict[str, Any]:
        return {"version": _VERSION, "credentials": {}}

    def _ensure_safe_path(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.path.is_symlink() or self.lock_path.is_symlink():
            raise CredentialStoreError("Credential paths must not be symbolic links.")
        with suppress(OSError):
            os.chmod(self.path.parent, 0o700)

    @contextmanager
    def _locked(self, *, exclusive: bool) -> Iterator[None]:
        with self._thread_lock:
            self._ensure_safe_path()
            flags = os.O_CREAT | os.O_RDWR
            if hasattr(os, "O_NOFOLLOW"):
                flags |= os.O_NOFOLLOW
            try:
                descriptor = os.open(self.lock_path, flags, 0o600)
            except OSError as exc:
                raise CredentialStoreError(
                    "Could not open the credential lock."
                ) from exc
            try:
                os.chmod(self.lock_path, 0o600)
                if fcntl is not None:
                    operation = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
                    fcntl.flock(descriptor, operation)
                yield
            finally:
                if fcntl is not None:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                os.close(descriptor)

    def _read_unlocked(self) -> dict[str, Any]:
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(self.path, flags)
        except FileNotFoundError:
            return self._empty()
        except OSError as exc:
            raise CredentialStoreError(
                "Credential file could not be opened safely."
            ) from exc
        try:
            stat = os.fstat(descriptor)
            if stat.st_size > _MAX_FILE_BYTES:
                raise CredentialStoreError("Credential file exceeds the size limit.")
            with os.fdopen(descriptor, encoding="utf-8") as source:
                descriptor = -1
                value = json.load(source)
        except CredentialStoreError:
            raise
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise CredentialStoreError(
                "Credential file could not be read safely."
            ) from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        if (
            not isinstance(value, dict)
            or value.get("version") != _VERSION
            or not isinstance(value.get("credentials"), dict)
        ):
            raise CredentialStoreError("Credential file has an unsupported format.")
        return value

    def _write_unlocked(self, value: dict[str, Any]) -> None:
        try:
            encoded = (
                json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
            )
        except (TypeError, ValueError, RecursionError) as exc:
            raise CredentialStoreError("Credential data could not be encoded.") from exc
        if len(encoded.encode("utf-8")) > _MAX_FILE_BYTES:
            raise CredentialStoreError("Credential data exceeds the size limit.")

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
        except OSError as exc:
            raise CredentialStoreError("Credential file could not be written.") from exc
        finally:
            if temporary_path is not None and temporary_path.exists():
                temporary_path.unlink(missing_ok=True)

    def read(self, platform_id: str) -> dict[str, Any] | None:
        if platform_id not in DIRECT_PLATFORM_IDS:
            raise ValueError(f"Unknown direct platform: {platform_id}")
        with self._locked(exclusive=False):
            value = self._read_unlocked()["credentials"].get(platform_id)
        if not isinstance(value, dict):
            return None
        return copy.deepcopy(value)

    def set_api_key(self, platform_id: str, api_key: str) -> None:
        platform = direct_platform(platform_id)
        if "api_key" not in platform.auth_modes:
            raise ValueError(f"{platform.name} does not support API-key setup.")
        key = _secret(api_key, "api_key")
        with self._locked(exclusive=True):
            value = self._read_unlocked()
            value["credentials"][platform_id] = {
                "type": "api_key",
                "key": key,
                "updated_at": _timestamp(),
            }
            self._write_unlocked(value)

    def set_oauth(
        self,
        platform_id: str,
        *,
        access: str,
        refresh: str,
        expires_at: int,
        extra: dict[str, Any] | None = None,
    ) -> None:
        platform = direct_platform(platform_id)
        if "oauth" not in platform.auth_modes:
            raise ValueError(f"{platform.name} does not support OAuth setup.")
        if not isinstance(expires_at, int) or expires_at < 0:
            raise ValueError("expires_at must be a Unix timestamp in milliseconds.")
        record = {
            "type": "oauth",
            "access": _secret(access, "access token"),
            "refresh": _secret(refresh, "refresh token", allow_empty=True),
            "expires_at": expires_at,
            "extra": _json_extra(extra),
            "updated_at": _timestamp(),
        }
        with self._locked(exclusive=True):
            value = self._read_unlocked()
            value["credentials"][platform_id] = record
            self._write_unlocked(value)

    def delete(self, platform_id: str) -> bool:
        if platform_id not in DIRECT_PLATFORM_IDS:
            raise ValueError(f"Unknown direct platform: {platform_id}")
        with self._locked(exclusive=True):
            value = self._read_unlocked()
            removed = value["credentials"].pop(platform_id, None) is not None
            if removed:
                self._write_unlocked(value)
        return removed

    def resolve(self, platform_id: str) -> tuple[dict[str, Any] | None, str]:
        """Resolve environment credentials before local credentials."""
        platform = direct_platform(platform_id)
        for name in platform.env_names:
            value = os.getenv(name)
            if value and value.strip():
                return {"type": "api_key", "key": value.strip()}, f"env:{name}"
        credential = self.read(platform_id)
        if credential is None:
            return None, "none"
        return credential, "credential_file"

    def safe_status(self) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        for platform_id in DIRECT_PLATFORM_IDS:
            credential, source = self.resolve(platform_id)
            safe: dict[str, Any] = {
                "configured": credential is not None,
                "source": source,
                "type": credential.get("type") if credential else None,
            }
            if credential and credential.get("type") == "oauth":
                expires_at = credential.get("expires_at")
                safe["expires_at"] = expires_at if isinstance(expires_at, int) else None
                extra = credential.get("extra")
                if isinstance(extra, dict):
                    safe["metadata"] = {
                        key: extra[key]
                        for key in _PUBLIC_EXTRA_FIELDS
                        if key in extra
                        and isinstance(extra[key], (str, int, float, bool, type(None)))
                    }
            result[platform_id] = safe
        return result

    def safe_view(self) -> dict[str, Any]:
        return {
            "path": str(self.path),
            "permissions": "0600",
            "encrypted": False,
            "providers": self.safe_status(),
        }
