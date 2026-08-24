from __future__ import annotations

import json
import os
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

_MAX_FILE_BYTES = 512 * 1_024
_MAX_SECRET_CHARS = 32_768
_KIRO_REGIONS = frozenset(
    {
        "us-east-1",
        "eu-west-1",
        "eu-central-1",
        "us-east-2",
        "eu-west-2",
        "eu-west-3",
        "eu-north-1",
        "ap-southeast-1",
        "ap-northeast-1",
        "us-west-2",
    }
)
_DIRECT_TARGETS: dict[str, str] = {
    "openai": "openai",
    "openai-codex": "openai_codex",
    "anthropic": "anthropic",
    "github-copilot": "github_copilot",
    "xai": "xai",
    "openrouter": "openrouter",
    "kiro": "kiro_direct",
}
_CURSOR_SOURCE_IDS = frozenset({"cursor", "cursor-agent"})
_API_KEY_SOURCE_IDS = frozenset({"openai", "anthropic", "xai", "openrouter"})
_OAUTH_SOURCE_IDS = frozenset(
    {"openai-codex", "anthropic", "github-copilot", "xai", "openrouter", "kiro"}
)


class PiCredentialImportError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class PiCredentialCandidate:
    source_provider: str
    target_kind: Literal["direct", "cursor"]
    target_id: str
    credential_type: Literal["api_key", "oauth"]
    api_key: str | None = field(default=None, repr=False)
    access: str | None = field(default=None, repr=False)
    refresh: str | None = field(default=None, repr=False)
    expires_at: int | None = field(default=None, repr=False)
    extra: dict[str, Any] = field(default_factory=dict, repr=False)

    def safe_view(self) -> dict[str, str]:
        return {
            "source_provider": self.source_provider,
            "target_kind": self.target_kind,
            "target_id": self.target_id,
            "credential_type": self.credential_type,
        }


@dataclass(frozen=True, slots=True)
class PiCredentialScan:
    candidates: tuple[PiCredentialCandidate, ...]
    unsupported: tuple[dict[str, str], ...]

    def safe_view(self) -> dict[str, object]:
        return {
            "source": "pi",
            "path": "~/.pi/agent/auth.json",
            "available": True,
            "candidates": [candidate.safe_view() for candidate in self.candidates],
            "unsupported": [dict(item) for item in self.unsupported],
        }


def _secret(
    record: dict[str, Any], field_name: str, *, allow_empty: bool = False
) -> str:
    value = record.get(field_name)
    if not isinstance(value, str):
        raise PiCredentialImportError("credential fields are incomplete")
    result = value.strip()
    if (
        (not allow_empty and not result)
        or len(result) > _MAX_SECRET_CHARS
        or any(ord(character) < 0x20 for character in result)
    ):
        raise PiCredentialImportError("credential fields are incomplete")
    return result


def _optional_secret(record: dict[str, Any], field_name: str) -> str | None:
    value = record.get(field_name)
    if value is None or value == "":
        return None
    return _secret(record, field_name)


def _expiry(record: dict[str, Any]) -> int:
    value = record.get("expires")
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or value < 0
        or value > 9_007_199_254_740_991
    ):
        raise PiCredentialImportError("credential expiry is invalid")
    return int(value)


def _copilot_base_url(access: str, enterprise_url: str | None) -> str:
    match = re.search(r"(?:^|;)proxy-ep=([^;]+)", access)
    host = match.group(1).strip().lower() if match else ""
    if host.startswith("proxy."):
        host = f"api.{host[6:]}"
    if host.endswith(".githubcopilot.com"):
        return f"https://{host}"
    if enterprise_url:
        raise PiCredentialImportError(
            "enterprise GitHub Copilot endpoints require a fresh Switchboard login"
        )
    return "https://api.individual.githubcopilot.com"


class PiCredentialImporter:
    """Read Pi's fixed auth file without importing or executing Pi code."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or Path.home() / ".pi" / "agent" / "auth.json"

    def _read(self) -> dict[str, Any]:
        for candidate in (self.path, self.path.parent, self.path.parent.parent):
            if candidate.is_symlink():
                raise PiCredentialImportError(
                    "Pi authentication path must not contain a symbolic link."
                )
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(self.path, flags)
        except FileNotFoundError as exc:
            raise PiCredentialImportError(
                "Pi authentication file was not found."
            ) from exc
        except OSError as exc:
            raise PiCredentialImportError(
                "Pi authentication file could not be opened safely."
            ) from exc
        try:
            file_stat = os.fstat(descriptor)
            if not stat.S_ISREG(file_stat.st_mode):
                raise PiCredentialImportError(
                    "Pi authentication source must be a regular file."
                )
            if file_stat.st_mode & 0o077:
                raise PiCredentialImportError(
                    "Pi authentication file must not be accessible by other users."
                )
            if file_stat.st_size > _MAX_FILE_BYTES:
                raise PiCredentialImportError(
                    "Pi authentication file exceeds the size limit."
                )
            with os.fdopen(descriptor, "rb") as source:
                descriptor = -1
                raw = source.read(_MAX_FILE_BYTES + 1)
            if len(raw) > _MAX_FILE_BYTES:
                raise PiCredentialImportError(
                    "Pi authentication file exceeds the size limit."
                )
            value = json.loads(raw)
        except PiCredentialImportError:
            raise
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise PiCredentialImportError(
                "Pi authentication file has an invalid format."
            ) from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        if not isinstance(value, dict):
            raise PiCredentialImportError(
                "Pi authentication file has an invalid format."
            )
        return value

    @staticmethod
    def _api_key_candidate(
        source_provider: str,
        target_kind: Literal["direct", "cursor"],
        target_id: str,
        record: dict[str, Any],
    ) -> PiCredentialCandidate:
        return PiCredentialCandidate(
            source_provider=source_provider,
            target_kind=target_kind,
            target_id=target_id,
            credential_type="api_key",
            api_key=_secret(record, "key"),
        )

    @staticmethod
    def _oauth_candidate(
        source_provider: str, target_id: str, record: dict[str, Any]
    ) -> PiCredentialCandidate:
        access = _secret(record, "access")
        refresh = _secret(record, "refresh", allow_empty=True)
        expires_at = _expiry(record)
        extra: dict[str, Any] = {}

        if source_provider == "openai-codex":
            account_id = _secret(record, "accountId")
            extra = {"account_id": account_id, "subscription": True}
        elif source_provider == "github-copilot":
            enterprise_url = _optional_secret(record, "enterpriseUrl")
            extra = {
                "base_url": _copilot_base_url(access, enterprise_url),
                "available_model_ids": [],
                "subscription": True,
            }
        elif source_provider in {"anthropic", "xai"}:
            extra = {"subscription": True}
        elif source_provider == "kiro":
            client_id = _optional_secret(record, "clientId")
            client_secret = _optional_secret(record, "clientSecret")
            auth_method = _optional_secret(record, "authMethod")
            if "|" in refresh:
                parts = refresh.split("|")
                if len(parts) != 4 or not all(parts):
                    raise PiCredentialImportError("Kiro refresh metadata is invalid")
                refresh, packed_id, packed_secret, packed_method = parts
                for explicit, packed in (
                    (client_id, packed_id),
                    (client_secret, packed_secret),
                    (auth_method, packed_method),
                ):
                    if explicit is not None and explicit != packed:
                        raise PiCredentialImportError(
                            "Kiro credential metadata does not match"
                        )
                client_id = packed_id
                client_secret = packed_secret
                auth_method = packed_method
            region = _secret(record, "region")
            if (
                client_id is None
                or client_secret is None
                or auth_method not in {"idc", "builder-id"}
                or region not in _KIRO_REGIONS
            ):
                raise PiCredentialImportError("Kiro credential metadata is invalid")
            extra = {
                "client_id": client_id,
                "client_secret": client_secret,
                "region": region,
                "auth_method": auth_method,
                "subscription": True,
            }

        return PiCredentialCandidate(
            source_provider=source_provider,
            target_kind="direct",
            target_id=target_id,
            credential_type="oauth",
            access=access,
            refresh=refresh,
            expires_at=expires_at,
            extra=extra,
        )

    def scan(self) -> PiCredentialScan:
        value = self._read()
        candidates: list[PiCredentialCandidate] = []
        unsupported: list[dict[str, str]] = []
        mapped_targets: set[tuple[str, str]] = set()
        for source_provider, raw_record in sorted(value.items()):
            if not isinstance(source_provider, str) or not isinstance(raw_record, dict):
                continue
            credential_type = raw_record.get("type")
            target_id = _DIRECT_TARGETS.get(source_provider)
            target_kind: Literal["direct", "cursor"] | None = None
            if target_id is not None:
                target_kind = "direct"
            elif source_provider in _CURSOR_SOURCE_IDS:
                target_kind = "cursor"
                target_id = "cursor"
            if target_kind is None or target_id is None:
                unsupported.append(
                    {
                        "source_provider": "unsupported",
                        "reason": "no compatible Switchboard provider",
                    }
                )
                continue
            try:
                if credential_type == "api_key" and (
                    source_provider in _API_KEY_SOURCE_IDS
                    or source_provider in _CURSOR_SOURCE_IDS
                ):
                    candidate = self._api_key_candidate(
                        source_provider, target_kind, target_id, raw_record
                    )
                elif (
                    credential_type == "oauth"
                    and target_kind == "direct"
                    and source_provider in _OAUTH_SOURCE_IDS
                ):
                    candidate = self._oauth_candidate(
                        source_provider, target_id, raw_record
                    )
                else:
                    raise PiCredentialImportError(
                        "credential type is not compatible with the target"
                    )
            except PiCredentialImportError as exc:
                unsupported.append(
                    {
                        "source_provider": source_provider[:200],
                        "reason": str(exc)[:300],
                    }
                )
                continue
            target = (candidate.target_kind, candidate.target_id)
            if target in mapped_targets:
                unsupported.append(
                    {
                        "source_provider": source_provider[:200],
                        "reason": "another Pi record already maps to this target",
                    }
                )
                continue
            mapped_targets.add(target)
            candidates.append(candidate)
        return PiCredentialScan(tuple(candidates), tuple(unsupported))
